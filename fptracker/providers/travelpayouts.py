"""Aviasales / Travelpayouts Data API v3 (cached prices from recent user searches; not live).

The API only returns tickets that other users searched recently, so exact-date queries are almost
always empty. With ``date_granularity: month`` (the default) the searched leg's month is queried
(``departure_at=YYYY-MM``, and ``return_at=YYYY-MM`` for round trips) and every cached ticket of
that window is stored as an itinerary with its real dates. Tickets whose dates differ from the
searched ones are marked ``exact_date=0`` in ``extensions`` and do not count towards the search's
``min_price``.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from ..config import Search
from .base import Provider, Result, http_request

URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
MAX_LIMIT = 1000


def _fmt(d: dt.date, granularity: str) -> str:
    return d.strftime("%Y-%m") if granularity == "month" else d.isoformat()


class TravelpayoutsProvider(Provider):
    name = "travelpayouts"

    @property
    def granularity(self) -> str:
        g = str(self.cfg.get("date_granularity", "month")).lower()
        if g not in ("month", "day"):
            raise ValueError(f"travelpayouts: date_granularity must be 'month' or 'day', not {g!r}")
        return g

    def build_params(self, search: Search) -> dict[str, Any]:
        p = search.params
        g = self.granularity
        leg_from, leg_to, leg_date = search.leg
        codes = self.cfg.get("city_codes") or {}
        params: dict[str, Any] = {
            "origin": codes.get(leg_from, leg_from),
            "destination": codes.get(leg_to, leg_to),
            "departure_at": _fmt(leg_date, g),
            "one_way": "false" if search.is_round_trip else "true",
            "direct": "false",
            "currency": str(p.get("currency", "EUR")).lower(),
            "market": str(p.get("market", "de")).lower(),
            "sorting": "price",
            "unique": "false",
            "limit": min(int(self.cfg.get("limit", MAX_LIMIT)), MAX_LIMIT),
            "page": 1,
        }
        if search.is_round_trip:
            params["return_at"] = _fmt(search.return_date, g)
        params.update(self.cfg.get("extra_params") or {})
        if params.get("market") in (None, ""):
            params.pop("market", None)
        return params

    def fetch(self, search: Search) -> Result:
        params = self.build_params(search)
        res = Result(request_params=params)
        max_pages = int(self.cfg.get("max_pages", 3))
        pages: list[Any] = []
        tickets: list[dict[str, Any]] = []
        data: dict[str, Any] = {}
        for page in range(1, max_pages + 1):
            r = http_request("GET", URL, params={**params, "page": page}, headers={"X-Access-Token": self.secret})
            res.api_calls += 1
            try:
                data = r.json()
            except ValueError:
                data = {"success": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}
            pages.append(data)
            if r.status_code >= 400 or not data.get("success", False):
                res.raw = pages[0] if len(pages) == 1 else {"pages": pages}
                res.status, res.error = "error", str(data.get("error") or f"HTTP {r.status_code}")
                return res
            batch = data.get("data") or []
            tickets.extend(batch)
            if len(batch) < int(params["limit"]):
                break
        res.raw = pages[0] if len(pages) == 1 else {"pages": pages}
        merged = {**data, "data": tickets}
        leg_from, leg_to, leg_date = search.leg
        parse_response(merged, res, search.is_round_trip, want_out=leg_date,
                       want_in=search.return_date if search.is_round_trip else None,
                       window=self.granularity, want_airports=(leg_from, leg_to))
        res.status = "ok" if res.itineraries else "no_results"
        return res


def _same_day(ts: Any, want: dt.date | None) -> bool:
    return want is None or (isinstance(ts, str) and ts[:10] == want.isoformat())


def parse_response(data: dict[str, Any], res: Result, is_rt: bool, *, want_out: dt.date | None = None,
                   want_in: dt.date | None = None, window: str = "day",
                   want_airports: tuple[str, str] | None = None) -> None:
    """Tickets -> itineraries. A ticket is exact when its dates (and airports, if the API names them)
    match the searched ones; inexact tickets carry ``_exact_date=False`` and are left out of min_price."""
    currency = (data.get("currency") or "").upper()
    for rank, t in enumerate(data.get("data") or []):
        fn = f"{t.get('airline', '')} {t.get('flight_number', '')}".strip()
        o_ap = t.get("origin_airport") or t.get("origin")
        d_ap = t.get("destination_airport") or t.get("destination")
        exact = _same_day(t.get("departure_at"), want_out) and (not is_rt or _same_day(t.get("return_at"), want_in))
        if want_airports and exact:
            exact = (not t.get("origin_airport") or t["origin_airport"] == want_airports[0]) and \
                    (not t.get("destination_airport") or t["destination_airport"] == want_airports[1])
        res.itineraries.append({
            "list_name": "cached",
            "rank": rank,
            "price": t.get("price"),
            "currency": currency,
            "price_status": "cached",
            "price_scope": "round_trip_total" if is_rt else "one_way",
            "out_departure_time": t.get("departure_at"),
            "out_duration_min": t.get("duration_to") or t.get("duration"),
            "out_n_stops": t.get("transfers"),
            "out_airlines": [t.get("airline")],
            "out_flight_numbers": [fn],
            "out_route": [o_ap, d_ap],
            "in_departure_time": t.get("return_at") or None,
            "in_duration_min": t.get("duration_back"),
            "in_n_stops": t.get("return_transfers"),
            "extensions": [f"date_window={window}", f"exact_date={int(exact)}"],
            "provider_itinerary_id": t.get("link"),
            "_exact_date": exact,
        })
    res.summary["currency"] = currency
