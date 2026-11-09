"""Orchestrates one collection run across providers."""
from __future__ import annotations

import datetime as dt
import os
import re
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__
from .budget import is_run_day, run_allowance, select_by_priority
from .config import Search, expand_searches
from .dates import WEEKDAY_NAMES
from .providers import REGISTRY, Provider, ProviderUnavailable, Result
from .storage import DATA_DIR, write_raw, write_table


@dataclass
class ProviderPlan:
    name: str
    status: str                      # run | not_scheduled | disabled | unavailable
    note: str = ""
    provider: Provider | None = None
    chosen: list[Search] = field(default_factory=list)
    skipped: list[Search] = field(default_factory=list)
    planned_calls: int = 0


def make_plan(cfg: dict[str, Any], now_local: dt.datetime, *, force: bool = False,
              only_providers: list[str] | None = None, only_pattern: str | None = None,
              data_dir: Path = DATA_DIR, instantiate: bool = True) -> tuple[list[Search], list[ProviderPlan]]:
    today = now_local.date()
    searches = expand_searches(cfg, today)
    if only_pattern:
        rx = re.compile(only_pattern)
        searches = [s for s in searches if rx.search(s.search_id)]
    plans = []
    for name, pcfg in cfg["providers"].items():
        if name not in REGISTRY:
            raise ValueError(f"unknown provider {name!r}; known: {sorted(REGISTRY)}")
        pcfg = pcfg or {}
        if only_providers and name not in only_providers:
            continue
        if not pcfg.get("enabled", True):
            plans.append(ProviderPlan(name, "disabled"))
            continue
        if not force and not is_run_day(pcfg, today):
            plans.append(ProviderPlan(name, "not_scheduled", f"runs every {pcfg.get('every_n_days', 1)} days"))
            continue
        provider = None
        cls = REGISTRY[name]
        if instantiate:
            secret = os.environ.get(pcfg["secret_env"]) if pcfg.get("secret_env") else None
            try:
                provider = cls(pcfg, secret)
            except ProviderUnavailable as e:
                plans.append(ProviderPlan(name, "broken" if e.broken else "unavailable", str(e)))
                continue
        mine = sorted((s for s in searches if s.providers is None or name in s.providers),
                      key=lambda s: (s.priority, s.search_id))
        costs = [cls.calls_per_search(_Probe(pcfg), s) for s in mine]  # type: ignore[arg-type]
        allowance = run_allowance(name, pcfg, today, data_dir)
        chosen, skipped = select_by_priority(mine, costs, allowance.calls)
        planned = sum(c for s, c in zip(mine, costs) if s in chosen)
        plans.append(ProviderPlan(name, "run", allowance.reason, provider, chosen, skipped, planned))
    return searches, plans


class _Probe:
    """Lets calls_per_search be evaluated without instantiating a provider (no secrets needed)."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg


def _key_row(run_id: str, provider: str, s: Search, ts_utc: dt.datetime, tz: ZoneInfo) -> dict[str, Any]:
    local = ts_utc.astimezone(tz)
    row = {
        "run_id": run_id,
        "provider": provider,
        "query_ts_utc": ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "query_ts_local": local.isoformat(timespec="seconds"),
        "query_date_local": local.date().isoformat(),
        "query_weekday": WEEKDAY_NAMES[local.weekday()],
        "query_hour_local": local.hour,
        "days_to_departure": (s.leg[2] - local.date()).days,
    }
    row.update(s.key_fields())
    return row


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def collect(provider_name: str, s: Search, res: Result, run_id: str, key: dict[str, Any],
            raw_path: str, duration: float) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {"searches": [], "itineraries": [], "segments": [], "price_history": []}
    uid = [f"{run_id}|{provider_name}|{s.search_id}|{i}" for i in range(len(res.itineraries))]
    for i, it in enumerate(res.itineraries):
        row = {**key, **_clean(it), "itinerary_uid": uid[i]}
        if "_parent" in it:
            row["parent_itinerary_uid"] = uid[it["_parent"]]
        tables["itineraries"].append(row)
    for seg in res.segments:
        tables["segments"].append({**key, **_clean(seg), "itinerary_uid": uid[seg["_itin"]]})
    for ph in res.price_history:
        v = float(ph["history_ts_ms"])
        ts = dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=dt.timezone.utc)  # SerpApi sends seconds
        tables["price_history"].append({**key, "history_ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                        "history_date": ts.date().isoformat(),
                                        "history_price": ph["history_price"], "currency": ph.get("currency")})
    # min_price only counts priced top-level itineraries for the searched dates; providers that return a
    # wider date window (travelpayouts month mode) flag other-date tickets with _exact_date=False.
    main_prices = [it["price"] for it in res.itineraries
                   if it.get("price") is not None and "_parent" not in it and it.get("_exact_date", True)]
    tables["searches"].append({
        **key,
        "status": res.status,
        "error": res.error,
        "api_calls": res.api_calls,
        "n_itineraries": len(res.itineraries),
        "min_price": min(main_prices) if main_prices else None,
        "currency": res.summary.get("currency") or s.params.get("currency"),
        "price_level": res.summary.get("price_level"),
        "typical_price_low": res.summary.get("typical_price_low"),
        "typical_price_high": res.summary.get("typical_price_high"),
        "provider_search_id": res.summary.get("provider_search_id"),
        "request_params": {k: v for k, v in res.request_params.items() if k != "api_key"},
        "raw_path": raw_path,
        "duration_s": round(duration, 1),
    })
    return tables


def execute(cfg: dict[str, Any], plans: list[ProviderPlan], now_utc: dt.datetime,
            data_dir: Path = DATA_DIR, log=print) -> dict[str, Any]:
    tz = ZoneInfo(cfg.get("timezone", "Europe/Berlin"))
    run_id = now_utc.strftime("%Y%m%dT%H%M%SZ")
    stats = {"run_id": run_id, "providers": {}}
    for plan in plans:
        if plan.status != "run" or plan.provider is None:
            continue
        tables: dict[str, list[dict[str, Any]]] = {"searches": [], "itineraries": [], "segments": [], "price_history": []}
        counts = {"ok": 0, "no_results": 0, "error": 0, "api_calls": 0}
        for i, s in enumerate(plan.chosen):
            if i:
                plan.provider.pause()
            ts = dt.datetime.now(dt.timezone.utc)
            t0 = time.monotonic()
            try:
                res = plan.provider.fetch(s)
            except Exception as e:
                res = Result(status="error", error=f"{type(e).__name__}: {e}"[:500],
                             raw={"traceback": traceback.format_exc()})
            dur = time.monotonic() - t0
            raw_path = write_raw(plan.name, run_id, s.search_id, {
                "provider": plan.name, "search": s.key_fields(), "query_ts_utc": ts.isoformat(),
                "tool_version": __version__, "request_params": res.request_params, "status": res.status,
                "error": res.error, "response": res.raw}, data_dir)
            key = _key_row(run_id, plan.name, s, ts, tz)
            for t, rows in collect(plan.name, s, res, run_id, key, raw_path, dur).items():
                tables[t].extend(rows)
            counts[res.status] = counts.get(res.status, 0) + 1
            counts["api_calls"] += res.api_calls
            min_p = tables["searches"][-1]["min_price"]
            log(f"  [{plan.name}] {s.search_id}: {res.status} {len(res.itineraries)} itins, "
                f"min {min_p} {res.error[:120]}")
        for t, rows in tables.items():
            write_table(t, run_id, plan.name, rows, data_dir)
        stats["providers"][plan.name] = counts
    return stats
