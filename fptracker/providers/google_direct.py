"""Google Flights scraped directly with the open-source `fast-flights` library (no key, no quota).

Unofficial: Google can change its page and break this at any time, and may rate-limit an IP.
That is why it runs alongside the API providers rather than instead of them.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
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
            raise ProviderUnavailable(f"google_direct: fast-flights not installed ({e})") from e

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
        from fast_flights import FlightsNotFound, get_flights

        q = self.build_query(search)
        res = Result(request_params={"url": q.url(), "trip": "round-trip" if search.is_round_trip else "one-way"})
        res.api_calls = 1
        try:
            flights = get_flights(q)
        except FlightsNotFound as e:
            res.status, res.error = "no_results", str(e)
            return res
        except Exception as e:  # parsing failures usually mean Google served a consent/captcha page
            res.status, res.error = "error", f"{type(e).__name__}: {e}"[:500]
            return res
        meta = getattr(flights, "metadata", None)
        res.raw = {
            "flights": [dataclasses.asdict(f) for f in flights],
            "metadata": dataclasses.asdict(meta) if meta is not None else None,
        }
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
            "list_name": "results",
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
