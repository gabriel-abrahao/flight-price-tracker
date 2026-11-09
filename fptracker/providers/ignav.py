"""Ignav fares API (https://ignav.com/docs). Live fares; round trips include both legs."""
from __future__ import annotations

from typing import Any

from ..config import Search
from .base import Provider, Result, http_request, leg_summary, minutes_between

BASE = "https://ignav.com/api"


class IgnavProvider(Provider):
    name = "ignav"

    def build_body(self, search: Search) -> dict[str, Any]:
        p = search.params
        leg_from, leg_to, leg_date = search.leg
        body: dict[str, Any] = {
            "origin": leg_from,
            "destination": leg_to,
            "departure_date": leg_date.isoformat(),
            "adults": int(p.get("adults", 1)),
            "cabin_class": p.get("cabin", "economy").replace("-", "_"),
            "market": str(p.get("market", "DE")).upper(),
        }
        if search.is_round_trip:
            body["return_date"] = search.return_date.isoformat()
        if p.get("max_stops") is not None:
            body["max_stops"] = int(p["max_stops"])
        body.update(self.cfg.get("extra_params") or {})
        return body

    def fetch(self, search: Search) -> Result:
        body = self.build_body(search)
        endpoint = "round-trip" if search.is_round_trip else "one-way"
        res = Result(request_params={"endpoint": endpoint, **body})
        r = http_request("POST", f"{BASE}/fares/{endpoint}", json=body, timeout=180,
                         headers={"X-Api-Key": self.secret, "Content-Type": "application/json"})
        try:
            data = r.json()
        except ValueError:
            data = {"error": {"code": f"http_{r.status_code}", "message": r.text[:300]}}
        res.raw = data
        if r.status_code >= 400 or "error" in data:
            err = data.get("error") or {}
            res.status, res.error = "error", f"HTTP {r.status_code} {err.get('code', '')}: {err.get('message', '')}"
            res.api_calls = 0  # failed requests are not billed
            return res
        res.api_calls = 1
        parse_response(data, res, is_rt=search.is_round_trip)
        res.status = "ok" if res.itineraries else "no_results"
        return res


def _segments(leg: dict[str, Any] | None, direction: str) -> list[dict[str, Any]]:
    segs = []
    raw = (leg or {}).get("segments") or []
    for i, s in enumerate(raw):
        code, num = s.get("marketing_carrier_code") or "", str(s.get("flight_number") or "")
        fn = num if (not code or num.upper().startswith(code.upper())) else f"{code} {num}"
        nxt = raw[i + 1] if i + 1 < len(raw) else None
        segs.append({
            "direction": direction,
            "seg_index": i,
            "dep_airport": s.get("departure_airport"),
            "dep_time_local": s.get("departure_time_local"),
            "dep_time_utc": s.get("departure_time_utc"),
            "arr_airport": s.get("arrival_airport"),
            "arr_time_local": s.get("arrival_time_local"),
            "arr_time_utc": s.get("arrival_time_utc"),
            "duration_min": s.get("duration_minutes"),
            "airline": s.get("operating_carrier_name") or code,
            "marketing_carrier": code,
            "flight_number": fn,
            "operating_carrier": s.get("operating_carrier_name"),
            "aircraft": s.get("aircraft"),
            "layover_after_min": minutes_between(s.get("arrival_time_utc"), nxt.get("departure_time_utc")) if nxt else None,
            "layover_after_airport": s.get("arrival_airport") if nxt else None,
        })
    return segs


def parse_response(data: dict[str, Any], res: Result, is_rt: bool) -> None:
    currency = ""
    for rank, it in enumerate(data.get("itineraries") or []):
        price = it.get("price") or {}
        currency = currency or price.get("currency", "")
        out = _segments(it.get("outbound"), "outbound")
        inb = _segments(it.get("inbound"), "inbound")
        bags = it.get("bags") or {}
        idx = len(res.itineraries)
        res.itineraries.append({
            "list_name": "results",
            "rank": rank,
            "price": price.get("amount"),
            "currency": price.get("currency"),
            "price_status": price.get("status"),
            "price_scope": "round_trip_total" if is_rt else "one_way",
            "bags_carry_on": bags.get("carry_on"),
            "bags_checked": bags.get("checked"),
            "has_mixed_carriers": it.get("has_mixed_carriers"),
            "extensions": [f"cabin={it.get('cabin_class')}"] + (
                [f"baggage_pricing={it['baggage_pricing']}"] if it.get("baggage_pricing") else []),
            "provider_itinerary_id": it.get("ignav_id"),
            **leg_summary("out", out, (it.get("outbound") or {}).get("duration_minutes")),
            **leg_summary("in", inb, (it.get("inbound") or {}).get("duration_minutes")),
        })
        for s in out + inb:
            s["_itin"] = idx
            res.segments.append(s)
    res.summary.update({"currency": currency, "provider_search_id": data.get("observed_at")})
