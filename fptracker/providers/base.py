"""Provider interface and shared helpers."""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from ..config import Search

USER_AGENT = "flight-price-tracker/1.0 (+https://github.com)"
CABIN_NAMES = ("economy", "premium_economy", "business", "first")


class ProviderUnavailable(Exception):
    """Provider cannot run at all. ``broken=False`` (missing key) is expected; ``broken=True`` is a bug."""

    def __init__(self, msg: str, broken: bool = False):
        super().__init__(msg)
        self.broken = broken


@dataclass
class Result:
    status: str = "ok"                  # ok | no_results | error
    error: str = ""
    api_calls: int = 0                  # billable calls spent (for budgeting)
    raw: Any = None                     # full response(s), stored as JSON
    request_params: dict[str, Any] = field(default_factory=dict)
    itineraries: list[dict[str, Any]] = field(default_factory=list)  # may carry "_parent" (index)
    segments: list[dict[str, Any]] = field(default_factory=list)     # carry "_itin" (index)
    price_history: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)            # price_level, typical_*, currency...


class Provider:
    name = "base"
    needs_secret = True

    def __init__(self, cfg: dict[str, Any], secret: str | None):
        self.cfg = cfg
        self.secret = secret
        if self.needs_secret and not secret:
            raise ProviderUnavailable(f"{self.name}: environment variable {cfg.get('secret_env')} is not set")

    def calls_per_search(self, search: Search) -> int:
        return 1

    def fetch(self, search: Search) -> Result:  # pragma: no cover - interface
        raise NotImplementedError

    def pause(self) -> None:
        time.sleep(float(self.cfg.get("pause_seconds", 0)))


def http_request(method: str, url: str, *, retries: int = 3, backoff: float = 5.0, **kw) -> requests.Response:
    """HTTP with retries on network errors, 429 and 5xx. Returns the last response (caller checks status)."""
    kw.setdefault("timeout", 120)
    headers = {"User-Agent": USER_AGENT, **kw.pop("headers", {})}
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, headers=headers, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                    continue
            return r
        except requests.RequestException as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_exc or RuntimeError("request failed")


def minutes_between(a: str | None, b: str | None) -> int | None:
    """Minutes between two ISO-ish timestamps ('YYYY-MM-DD HH:MM' or ISO 8601)."""
    if not a or not b:
        return None
    try:
        ta = dt.datetime.fromisoformat(a.replace("Z", "+00:00").replace(" ", "T"))
        tb = dt.datetime.fromisoformat(b.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if (ta.tzinfo is None) != (tb.tzinfo is None):
        return None
    return int((tb - ta).total_seconds() // 60)


def leg_summary(prefix: str, segs: list[dict[str, Any]], total_duration: int | None = None) -> dict[str, Any]:
    """Itinerary-level columns (out_* / in_*) derived from a leg's segment rows."""
    if not segs:
        return {}
    layover_airports = [s.get("layover_after_airport") or s.get("arr_airport") for s in segs[:-1]]
    layover_min = [s.get("layover_after_min") for s in segs[:-1]]
    airlines: list[str] = []
    for s in segs:
        a = s.get("airline") or s.get("operating_carrier") or s.get("marketing_carrier")
        if a and a not in airlines:
            airlines.append(a)
    if total_duration is None:
        total_duration = minutes_between(segs[0].get("dep_time_utc"), segs[-1].get("arr_time_utc"))
    out = {
        f"{prefix}_departure_time": segs[0].get("dep_time_local"),
        f"{prefix}_arrival_time": segs[-1].get("arr_time_local"),
        f"{prefix}_duration_min": total_duration,
        f"{prefix}_n_stops": len(segs) - 1,
        f"{prefix}_airlines": airlines,
        f"{prefix}_flight_numbers": [s.get("flight_number") for s in segs],
        f"{prefix}_route": [segs[0].get("dep_airport")] + [s.get("arr_airport") for s in segs],
        f"{prefix}_layovers": layover_airports,
        f"{prefix}_layover_min": layover_min,
    }
    if prefix == "out":
        out["out_overnight_layover"] = any(s.get("_layover_overnight") for s in segs)
    return out


def cabin_index(cabin: str) -> int:
    """1-based index used by Google-style APIs (1 economy ... 4 first)."""
    return CABIN_NAMES.index(cabin.replace("-", "_")) + 1
