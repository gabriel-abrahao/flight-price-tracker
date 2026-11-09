"""Google Flights via SerpApi (https://serpapi.com/google-flights-api)."""
from __future__ import annotations

from typing import Any

from ..config import Search
from .base import Provider, Result, cabin_index, http_request, leg_summary

URL = "https://serpapi.com/search.json"


class SerpApiProvider(Provider):
    name = "serpapi"

    def calls_per_search(self, search: Search) -> int:
        n = int(self.cfg.get("return_legs_for_top", 0))
        return 1 + (n if search.is_round_trip else 0)

    def build_params(self, search: Search) -> dict[str, Any]:
        p = search.params
        leg_from, leg_to, leg_date = search.leg
        params: dict[str, Any] = {
            "engine": "google_flights",
            "departure_id": leg_from,
            "arrival_id": leg_to,
            "outbound_date": leg_date.isoformat(),
            "type": 1 if search.is_round_trip else 2,
            "currency": p.get("currency", "EUR"),
            "hl": p.get("language", "en"),
            "gl": str(p.get("market", "de")).lower(),
            "adults": p.get("adults", 1),
            "travel_class": cabin_index(p.get("cabin", "economy")),
        }
        if search.is_round_trip:
            params["return_date"] = search.return_date.isoformat()
        if self.cfg.get("deep_search"):
            params["deep_search"] = "true"
        if p.get("max_stops") is not None:
            params["stops"] = int(p["max_stops"]) + 1   # SerpApi: 1 nonstop, 2 <=1 stop, 3 <=2 stops
        params.update(self.cfg.get("extra_params") or {})
        return params

    def _get(self, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        r = http_request("GET", URL, params={**params, "api_key": self.secret}, timeout=180)
        try:
            body = r.json()
        except ValueError:
            body = {"error": f"HTTP {r.status_code}: {r.text[:300]}"}
        return r.status_code, body

    def fetch(self, search: Search) -> Result:
        params = self.build_params(search)
        res = Result(request_params=params)
        status, body = self._get(params)
        res.api_calls = 1 if status < 500 else 0
        res.raw = {"main": body}
        if body.get("error"):
            err = str(body["error"])
            res.status = "no_results" if "hasn't returned any results" in err else "error"
            res.error = err
            return res
        parse_response(body, res, list_prefix="")
        # Optional: fetch return-leg options for the N best outbound itineraries of a round trip.
        top = int(self.cfg.get("return_legs_for_top", 0)) if search.is_round_trip else 0
        returns = []
        for parent_idx, it in enumerate(res.itineraries[:top]):
            token = it.pop("_departure_token", None)
            if not token:
                continue
            st, rb = self._get({**params, "departure_token": token})
            res.api_calls += 1
            returns.append(rb)
            if not rb.get("error"):
                parse_response(rb, res, list_prefix="return_", parent=parent_idx, direction="inbound")
        if returns:
            res.raw["return_legs"] = returns
        for it in res.itineraries:
            it.pop("_departure_token", None)
        res.status = "ok" if res.itineraries else "no_results"
        return res


def _segment(f: dict[str, Any], direction: str, idx: int, layover: dict[str, Any] | None) -> dict[str, Any]:
    dep, arr = f.get("departure_airport") or {}, f.get("arrival_airport") or {}
    fn = f.get("flight_number") or ""
    return {
        "direction": direction,
        "seg_index": idx,
        "dep_airport": dep.get("id"),
        "dep_airport_name": dep.get("name"),
        "dep_time_local": dep.get("time"),
        "arr_airport": arr.get("id"),
        "arr_airport_name": arr.get("name"),
        "arr_time_local": arr.get("time"),
        "duration_min": f.get("duration"),
        "airline": f.get("airline"),
        "marketing_carrier": fn.split(" ")[0] if " " in fn else "",
        "flight_number": fn,
        "operating_carrier": f.get("plane_and_crew_by") or "",
        "aircraft": f.get("airplane"),
        "travel_class": f.get("travel_class"),
        "legroom": f.get("legroom"),
        "overnight": bool(f.get("overnight")),
        "often_delayed": bool(f.get("often_delayed_by_over_30_min")),
        "layover_after_min": (layover or {}).get("duration"),
        "layover_after_airport": (layover or {}).get("id"),
        "_layover_overnight": bool((layover or {}).get("overnight")),
        "extensions": f.get("extensions") or [],
    }


def parse_response(body: dict[str, Any], res: Result, list_prefix: str = "", parent: int | None = None,
                   direction: str = "outbound") -> None:
    is_rt = (body.get("search_parameters") or {}).get("type") in (1, "1")
    currency = (body.get("search_parameters") or {}).get("currency", "")
    rank = 0
    for list_name in ("best_flights", "other_flights"):
        for opt in body.get(list_name) or []:
            itin_idx = len(res.itineraries)
            layovers = opt.get("layovers") or []
            segs = [
                _segment(f, direction, i, layovers[i] if i < len(layovers) else None)
                for i, f in enumerate(opt.get("flights") or [])
            ]
            carbon = opt.get("carbon_emissions") or {}
            prefix = "in" if direction == "inbound" else "out"
            itin = {
                "list_name": list_prefix + list_name.replace("_flights", ""),
                "rank": rank,
                "price": opt.get("price"),
                "currency": currency,
                "price_status": "live",
                "price_scope": "round_trip_total" if is_rt or parent is not None else "one_way",
                "self_transfer": any("Self transfer" in str(e) for e in opt.get("extensions") or []),
                "carbon_g": carbon.get("this_flight"),
                "carbon_typical_g": carbon.get("typical_for_this_route"),
                "carbon_diff_pct": carbon.get("difference_percent"),
                "extensions": (opt.get("extensions") or []) + [f"type={opt.get('type')}"],
                "provider_itinerary_id": opt.get("booking_token") or "",
                "_departure_token": opt.get("departure_token"),
                **leg_summary(prefix, segs, opt.get("total_duration")),
            }
            if parent is not None:
                itin["_parent"] = parent
            res.itineraries.append(itin)
            for s in segs:
                s["_itin"] = itin_idx
                res.segments.append(s)
            rank += 1
    if parent is None:
        pi = body.get("price_insights") or {}
        rng = pi.get("typical_price_range") or [None, None]
        res.summary.update({
            "currency": currency,
            "price_level": pi.get("price_level"),
            "typical_price_low": rng[0],
            "typical_price_high": rng[1] if len(rng) > 1 else None,
            "provider_search_id": (body.get("search_metadata") or {}).get("id"),
        })
        for ts_ms, price in pi.get("price_history") or []:
            res.price_history.append({"history_ts_ms": ts_ms, "history_price": price, "currency": currency})
