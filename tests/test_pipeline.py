"""Offline end-to-end tests: provider parsing on recorded-shape fixtures, storage, and budgeting."""
import datetime as dt
import gzip
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fptracker import runner
from fptracker.budget import calls_used, run_allowance, select_by_priority
from fptracker.config import expand_searches, load_config
from fptracker.providers import REGISTRY, Result
from fptracker.providers import google_direct, ignav, serpapi, travelpayouts
from fptracker.storage import TABLE_COLUMNS, read_table_rows

FIX = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("Europe/Berlin")


def fixture(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def cfg():
    return load_config()


def test_expand_default_config(cfg):
    searches = expand_searches(cfg, dt.date(2026, 11, 9))
    assert len(searches) == 12
    assert {s.route_id for s in searches} == {"BER-GIG", "BER-CNF"}
    ret = [s for s in searches if s.mode == "one_way_return"][0]
    assert ret.leg[0] in ("GIG", "CNF") and ret.leg[1] == "BER" and ret.leg[2] == ret.return_date


def test_serpapi_parse():
    res = Result()
    serpapi.parse_response(fixture("serpapi_rt.json"), res)
    assert [i["price"] for i in res.itineraries] == [812, 1105]
    a, b = res.itineraries
    assert a["out_route"] == ["BER", "LIS", "GIG"] and a["out_n_stops"] == 1 and a["out_layover_min"] == [105]
    assert a["price_scope"] == "round_trip_total" and a["_departure_token"] == "DEP_TOKEN_1"
    assert b["self_transfer"] and b["out_airlines"] == ["Lufthansa", "LATAM"]
    assert len(res.segments) == 5 and res.segments[3]["overnight"]
    assert res.summary["price_level"] == "typical" and res.summary["typical_price_low"] == 700
    assert len(res.price_history) == 2


def test_ignav_parse():
    res = Result()
    ignav.parse_response(fixture("ignav_rt.json"), res, is_rt=True)
    [it] = res.itineraries
    assert it["price"] == 799.4 and it["price_status"] == "verified"
    assert it["out_flight_numbers"] == ["TP 541", "TP 73"] and it["out_layover_min"] == [105]
    assert it["in_route"] == ["GIG", "LIS", "BER"] and it["in_layover_min"] == [80]
    assert {s["direction"] for s in res.segments} == {"outbound", "inbound"}


def test_travelpayouts_parse():
    res = Result()
    travelpayouts.parse_response(fixture("travelpayouts_rt.json"), res, True)
    assert [it["price"] for it in res.itineraries] == [640, 700, 760]
    it = res.itineraries[2]
    assert it["currency"] == "EUR" and it["price_status"] == "cached" and it["out_route"] == ["BER", "GIG"]
    assert all(it["_exact_date"] for it in res.itineraries)  # no wanted dates given -> nothing to compare


class _FakeResponse:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def _tp_search(cfg, mode):
    return next(s for s in expand_searches(cfg, dt.date(2026, 11, 9))
                if s.route_id == "BER-GIG" and s.trip_id == "wed_before_carnival_plus_1m" and s.mode == mode)


@pytest.mark.parametrize("granularity", ["month", "day"])
def test_travelpayouts_params(cfg, granularity):
    prov = travelpayouts.TravelpayoutsProvider({**cfg["providers"]["travelpayouts"], "date_granularity": granularity}, "t")
    rt = prov.build_params(_tp_search(cfg, "round_trip"))
    ret = prov.build_params(_tp_search(cfg, "one_way_return"))
    if granularity == "month":
        assert (rt["departure_at"], rt["return_at"]) == ("2027-02", "2027-03")
        assert ret["departure_at"] == "2027-03" and "return_at" not in ret
    else:
        assert (rt["departure_at"], rt["return_at"]) == ("2027-02-03", "2027-03-03")
    assert rt["limit"] == 1000 and ret["one_way"] == "true"
    assert ret["origin"] == (prov.cfg.get("city_codes") or {}).get("GIG", "GIG") and rt["origin"] == "BER"
    plain = travelpayouts.TravelpayoutsProvider({"date_granularity": granularity}, "t")
    assert plain.build_params(_tp_search(cfg, "one_way_return"))["origin"] == "GIG"
    coded = travelpayouts.TravelpayoutsProvider({"city_codes": {"GIG": "RIO"}}, "t")
    assert coded.build_params(_tp_search(cfg, "round_trip"))["destination"] == "RIO"


def test_travelpayouts_month_mode_exact_min_price(cfg, monkeypatch):
    calls = []

    def fake_request(method, url, params=None, headers=None, **kw):
        calls.append(params)
        return _FakeResponse(fixture("travelpayouts_rt.json"))

    monkeypatch.setattr(travelpayouts, "http_request", fake_request)
    s = _tp_search(cfg, "round_trip")
    assert (s.outbound_date, s.return_date) == (dt.date(2027, 2, 3), dt.date(2027, 3, 3))
    res = travelpayouts.TravelpayoutsProvider(cfg["providers"]["travelpayouts"], "t").fetch(s)
    assert res.status == "ok" and res.api_calls == 1 and len(calls) == 1  # short page -> no pagination
    assert res.request_params["departure_at"] == "2027-02" and res.request_params["return_at"] == "2027-03"
    # all cached tickets are kept; only the GIG ticket on the searched dates is exact
    assert [it["extensions"] for it in res.itineraries] == [
        ["date_window=month", "exact_date=0"],   # other dates
        ["date_window=month", "exact_date=0"],   # right dates, but SDU instead of GIG
        ["date_window=month", "exact_date=1"]]
    key = runner._key_row("R", "travelpayouts", s, dt.datetime(2026, 11, 9, tzinfo=dt.timezone.utc), TZ)
    tables = runner.collect("travelpayouts", s, res, "R", key, "raw.json.gz", 0.1)
    [row] = tables["searches"]
    assert row["min_price"] == 760 and row["n_itineraries"] == 3
    assert row["request_params"]["departure_at"] == "2027-02"
    assert all("_exact_date" not in r for r in tables["itineraries"])
    assert tables["itineraries"][0]["out_departure_time"].startswith("2027-02-17")


def test_travelpayouts_pagination(cfg, monkeypatch):
    pages = {1: [{"price": i, "departure_at": "2027-02-03T06:00:00+01:00"} for i in range(1000)],
             2: [{"price": 5000, "departure_at": "2027-02-04T06:00:00+01:00"}]}
    monkeypatch.setattr(travelpayouts, "http_request",
                        lambda *a, params=None, **kw: _FakeResponse({"success": True, "currency": "eur",
                                                                     "data": pages[params["page"]]}))
    res = travelpayouts.TravelpayoutsProvider(cfg["providers"]["travelpayouts"], "t").fetch(_tp_search(cfg, "one_way_outbound"))
    assert res.api_calls == 2 and len(res.itineraries) == 1001 and len(res.raw["pages"]) == 2


def test_google_direct_parse():
    res = Result()
    google_direct.parse_flights(fixture("google_direct_rt.json")["flights"], res, True, "EUR")
    [it] = res.itineraries
    assert it["out_layover_min"] == [105] and it["out_duration_min"] == 930 and it["carbon_g"] == 812000


class FakeProvider:
    """Stands in for every provider: returns fixture data, counts calls."""

    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg

    def pause(self):
        pass

    def fetch(self, search):
        res = Result(request_params={"api_key": "SECRET", "x": 1}, api_calls=1)
        if self.name == "serpapi":
            body = fixture("serpapi_rt.json")
            res.raw = body
            serpapi.parse_response(body, res)
        elif self.name == "ignav":
            res.raw = fixture("ignav_rt.json")
            ignav.parse_response(res.raw, res, search.is_round_trip)
        elif self.name == "travelpayouts":
            res.status, res.error = "error", "simulated outage"
            return res
        else:
            res.raw = fixture("google_direct_rt.json")
            google_direct.parse_flights(res.raw["flights"], res, search.is_round_trip, "EUR")
        for it in res.itineraries:
            it.pop("_departure_token", None)
        return res


def test_full_run_offline(cfg, tmp_path, monkeypatch):
    for name in REGISTRY:
        monkeypatch.setenv(cfg["providers"][name].get("secret_env") or "UNUSED", "dummy")
    now_local = dt.datetime(2026, 11, 10, 7, 0, tzinfo=TZ)  # serpapi epoch day -> all four providers run
    _, plans = runner.make_plan(cfg, now_local, data_dir=tmp_path)
    assert [p.status for p in plans] == ["run"] * 4
    for p in plans:
        p.provider = FakeProvider(p.name, cfg["providers"][p.name])
    stats = runner.execute(cfg, plans, now_local.astimezone(dt.timezone.utc), tmp_path, log=lambda *a: None)
    assert stats["providers"]["travelpayouts"]["error"] == 12
    assert stats["providers"]["serpapi"]["ok"] == 12

    searches = read_table_rows("searches", "202611", tmp_path)
    assert len(searches) == 48
    s0 = searches[0]  # query timestamps are the real wall clock of each request
    assert s0["query_weekday"] == dt.date.fromisoformat(s0["query_date_local"]).strftime("%a")
    assert int(s0["days_to_departure"]) == (dt.date.fromisoformat(s0["flight_date"])
                                            - dt.date.fromisoformat(s0["query_date_local"])).days
    assert "SECRET" not in s0["request_params"]
    itins = read_table_rows("itineraries", None, tmp_path)
    segs = read_table_rows("segments", None, tmp_path)
    assert {r["provider"] for r in itins} == {"serpapi", "ignav", "google_direct"}
    uids = {r["itinerary_uid"] for r in itins}
    assert all(s["itinerary_uid"] in uids for s in segs)
    assert set(itins[0]) == set(TABLE_COLUMNS["itineraries"])
    raw = list((tmp_path / "raw").rglob("*.json.gz"))
    assert len(raw) == 48
    assert all(b"SECRET" not in gzip.decompress(p.read_bytes()) for p in raw)

    # budget accounting reads back what was written
    assert calls_used("ignav", "202611", tmp_path) == 12
    al = run_allowance("ignav", cfg["providers"]["ignav"], dt.date(2026, 11, 11), tmp_path)
    assert al.calls == (450 - 12) // 20


def test_budget_drops_lowest_priority_first():
    chosen, skipped = select_by_priority(["p0", "p1", "p2"], [2, 1, 1], 3)
    assert chosen == ["p0", "p1"] and skipped == ["p2"]
    assert select_by_priority(["a"], [1], None) == (["a"], [])


def _sf(frm, to, d, dep, arr, dur):
    sf = [None] * 23
    sf[3], sf[4], sf[5], sf[6] = frm, f"{frm} airport", f"{to} airport", to
    sf[8], sf[10], sf[11], sf[17] = dep, arr, dur, "Airbus A350"
    sf[20], sf[21], sf[22] = d, d, ["TP", "73", None, "TAP Air Portugal"]
    return sf


def test_google_tolerant_parser_reads_top_list_when_other_is_empty():
    flight = [["TP", ["TAP Air Portugal"], [_sf("BER", "LIS", [2027, 2, 3], [6], [8, 45], 225),
                                            _sf("LIS", "CNF", [2027, 2, 3], [10, 30], [17, 0], 570)]]
              + [None] * 19 + [[None] * 7 + [812000, 790000]], [[None, 950]]]
    payload = [None, None, [[flight]], None, None, None, None, [None, [[], [["TP", "TAP"]]]]]
    html = ('<html><script class="ds:1">AF_initDataCallback({key: \'ds:1\', hash: \'1\', data:'
            + json.dumps(payload) + ', sideChannel: {}});</script></html>')
    flights, bad = google_direct.parse_html(html)
    assert bad == 0 and len(flights) == 1 and flights[0]["list"] == "top" and flights[0]["price"] == 950
    res = Result()
    google_direct.parse_flights(flights, res, True, "EUR")
    assert res.itineraries[0]["out_layover_min"] == [105]
    assert res.segments[0]["flight_number"] == "TP 73" and res.itineraries[0]["carbon_g"] == 812000


def test_price_history_timestamps_are_seconds():
    res = Result()
    serpapi.parse_response(fixture("serpapi_rt.json"), res)
    from fptracker.config import Search
    s = expand_searches(load_config(), dt.date(2026, 11, 9))[0]
    rows = runner.collect("serpapi", s, res, "20261110T000000Z", {}, "", 0.0)["price_history"]
    assert rows[0]["history_date"] == "2025-11-08"
