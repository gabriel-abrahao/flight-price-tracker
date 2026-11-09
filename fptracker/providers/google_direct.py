"""Google Flights scraped directly with the open-source `fast-flights` library (no key, no quota).

Unofficial: Google can change its page and break this at any time, and may rate-limit an IP.
That is why it runs alongside the API providers rather than instead of them.
"""
from __future__ import annotations

import json
import datetime as dt
import time
from typing import Any

from ..config import Search
from .base import Provider, ProviderUnavailable, Result, leg_summary


class GoogleDirectProvider(Provider):
    name = "google_direct"
    needs_secret = False

    def __init__(self, cfg: dict[str, Any], secret: str | None):
        super().__init__(cfg, secret)
        try:
            import fast_flights  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise ProviderUnavailable(f"google_direct: fast-flights cannot be imported ({e})", broken=True) from e

    def build_query(self, search: Search):
        from fast_flights import FlightQuery, Passengers, create_query

        p = search.params
        leg_from, leg_to, leg_date = search.leg
        flights = [FlightQuery(date=leg_date.isoformat(), from_airport=leg_from, to_airport=leg_to)]
        if search.is_round_trip:
            flights.append(FlightQuery(date=search.return_date.isoformat(),
                                       from_airport=search.destination, to_airport=search.origin))
        return create_query(
            flights=flights,
            trip="round-trip" if search.is_round_trip else "one-way",
            seat=p.get("cabin", "economy").replace("_", "-"),
            passengers=Passengers(adults=int(p.get("adults", 1))),
            language=p.get("language", "en"),
            currency=p.get("currency", "EUR"),
            max_stops=p.get("max_stops"),
        )

    def fetch(self, search: Search) -> Result:
        from fast_flights import FlightsNotFound, fetch_flights_html

        q = self.build_query(search)
        res = Result(request_params={"url": q.url(), "trip": "round-trip" if search.is_round_trip else "one-way"})
        # Google intermittently answers with an error payload (looks like throttling): retry with backoff.
        retries = int(self.cfg.get("retries", 3))
        wait = float(self.cfg.get("retry_wait_seconds", 20))
        flights = None
        for attempt in range(retries + 1):
            res.api_calls += 1
            try:
                flights, n_bad = parse_html(fetch_flights_html(q))
                break
            except FlightsNotFound as e:
                res.status, res.error = "no_results", f"{e} (after {attempt + 1} attempts)"
            except Exception as e:  # e.g. Google served a consent/captcha page
                res.status, res.error = "error", f"{type(e).__name__}: {e} (after {attempt + 1} attempts)"[:500]
            if attempt < retries:
                time.sleep(wait * (attempt + 1))
        if flights is None:
            return res
        res.status, res.error = "ok", (f"{n_bad} unparseable options skipped" if n_bad else "")
        res.raw = {"flights": flights}
        parse_flights(res.raw["flights"], res, search.is_round_trip, search.params.get("currency", "EUR"))
        res.status = "ok" if res.itineraries else "no_results"
        return res


def _fmt(sd: dict[str, Any]) -> str | None:
    try:
        y, m, d = sd["date"]
        hh, mm = sd["time"]
        return dt.datetime(y, m, d, hh, mm).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def parse_flights(flights: list[dict[str, Any]], res: Result, is_rt: bool, currency: str) -> None:
    from .base import minutes_between

    for rank, f in enumerate(flights):
        segs = []
        raw = f.get("flights") or []
        for i, s in enumerate(raw):
            nxt = raw[i + 1] if i + 1 < len(raw) else None
            arr = _fmt(s["arrival"])
            segs.append({
                "direction": "outbound",
                "seg_index": i,
                "dep_airport": s["from_airport"]["code"],
                "dep_airport_name": s["from_airport"]["name"],
                "dep_time_local": _fmt(s["departure"]),
                "arr_airport": s["to_airport"]["code"],
                "arr_airport_name": s["to_airport"]["name"],
                "arr_time_local": arr,
                "duration_min": s.get("duration"),
                "aircraft": s.get("plane_type"),
                "airline": s.get("airline"),
                "marketing_carrier": s.get("airline_code"),
                "flight_number": s.get("flight_number"),
                # same airport, so local-time difference is the layover
                "layover_after_min": minutes_between(arr, _fmt(nxt["departure"])) if nxt else None,
                "layover_after_airport": s["to_airport"]["code"] if nxt else None,
            })
        carbon = f.get("carbon") or {}
        idx = len(res.itineraries)
        summary = leg_summary("out", segs)
        summary["out_airlines"] = f.get("airlines") or []
        # times are local (different zones), so total duration = flight times + layovers
        if segs:
            summary["out_duration_min"] = sum(int(s["duration_min"] or 0) for s in segs) + sum(
                int(s["layover_after_min"] or 0) for s in segs[:-1])
        res.itineraries.append({
            "list_name": f.get("list", "results"),
            "rank": rank,
            "price": f.get("price"),
            "currency": currency,
            "price_status": "live",
            "price_scope": "round_trip_total" if is_rt else "one_way",
            "carbon_g": carbon.get("emission"),
            "carbon_typical_g": carbon.get("typical_on_route"),
            "extensions": [f"type={f.get('type')}"],
            **summary,
        })
        for s in segs:
            s["_itin"] = idx
            res.segments.append(s)


def _g(obj: Any, *idx: int) -> Any:
    """Nested index access that returns None instead of raising."""
    for i in idx:
        if not isinstance(obj, list) or i >= len(obj):
            return None
        obj = obj[i]
    return obj


def _time(v: Any) -> list[int]:
    v = list(v or []) + [None, None]
    return [v[0] or 0, v[1] or 0]


def parse_html(html: str) -> tuple[list[dict[str, Any]], int]:
    """Tolerant version of fast-flights' parser.

    Google puts results in two lists (payload[2] = "top" flights, payload[3] = the rest); fast-flights
    3.1 only reads payload[3] and crashes when it is empty. Each option is parsed independently so one
    odd entry doesn't lose the whole search. Returns (flights as dicts, number of skipped options).
    """
    from fast_flights import FlightsNotFound
    from selectolax.lexbor import LexborHTMLParser

    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise RuntimeError("no flight data in page (consent or captcha page?)")
    data = script.text().split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise FlightsNotFound("no flights found; received error")
    payload = json.loads(data)
    out: list[dict[str, Any]] = []
    bad = 0
    for list_name, pos in (("top", 2), ("other", 3)):
        for k in _g(payload, pos, 0) or []:
            try:
                flight = k[0]
                segs = []
                for sf in flight[2]:
                    segs.append({
                        "from_airport": {"code": sf[3], "name": sf[4]},
                        "to_airport": {"code": sf[6], "name": sf[5]},
                        "departure": {"date": list(sf[20]), "time": _time(sf[8])},
                        "arrival": {"date": list(sf[21]), "time": _time(sf[10])},
                        "duration": sf[11],
                        "plane_type": _g(sf, 17),
                        "airline_code": _g(sf, 22, 0),
                        "flight_number": " ".join(str(x) for x in (_g(sf, 22, 0), _g(sf, 22, 1)) if x),
                        "airline": _g(sf, 22, 3),
                        "_raw": sf,  # kept in the raw JSON so new fields can be mined later
                    })
                out.append({
                    "list": list_name,
                    "type": flight[0],
                    "price": _g(k, 1, 0, 1),
                    "airlines": flight[1],
                    "flights": segs,
                    "carbon": {"emission": _g(flight, 22, 7), "typical_on_route": _g(flight, 22, 8)},
                })
            except (TypeError, IndexError, KeyError):
                bad += 1
    return out, bad
