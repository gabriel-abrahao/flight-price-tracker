"""Aviasales / Travelpayouts Data API v3 (cached prices from recent user searches; not live)."""
from __future__ import annotations

from typing import Any

from ..config import Search
from .base import Provider, Result, http_request

URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


class TravelpayoutsProvider(Provider):
    name = "travelpayouts"

    def build_params(self, search: Search) -> dict[str, Any]:
        p = search.params
        leg_from, leg_to, leg_date = search.leg
        params: dict[str, Any] = {
            "origin": leg_from,
            "destination": leg_to,
            "departure_at": leg_date.isoformat(),
            "one_way": "false" if search.is_round_trip else "true",
            "direct": "false",
            "currency": str(p.get("currency", "EUR")).lower(),
            "market": str(p.get("market", "de")).lower(),
            "sorting": "price",
            "unique": "false",
            "limit": int(self.cfg.get("limit", 100)),
            "page": 1,
        }
        if search.is_round_trip:
            params["return_at"] = search.return_date.isoformat()
        params.update(self.cfg.get("extra_params") or {})
        return params

    def fetch(self, search: Search) -> Result:
        params = self.build_params(search)
        res = Result(request_params=params)
        r = http_request("GET", URL, params=params, headers={"X-Access-Token": self.secret})
        res.api_calls = 1
        try:
            data = r.json()
        except ValueError:
            data = {"success": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}
        res.raw = data
        if r.status_code >= 400 or not data.get("success", False):
            res.status, res.error = "error", str(data.get("error") or f"HTTP {r.status_code}")
            return res
        parse_response(data, res, search.is_round_trip)
        res.status = "ok" if res.itineraries else "no_results"
        return res


def parse_response(data: dict[str, Any], res: Result, is_rt: bool) -> None:
    currency = (data.get("currency") or "").upper()
    for rank, t in enumerate(data.get("data") or []):
        fn = f"{t.get('airline', '')} {t.get('flight_number', '')}".strip()
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
            "out_route": [t.get("origin_airport") or t.get("origin"), t.get("destination_airport") or t.get("destination")],
            "in_departure_time": t.get("return_at") or None,
            "in_duration_min": t.get("duration_back"),
            "in_n_stops": t.get("return_transfers"),
            "provider_itinerary_id": t.get("link"),
        })
    res.summary["currency"] = currency
