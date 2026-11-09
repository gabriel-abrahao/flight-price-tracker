"""Per-provider scheduling and API-call budgeting."""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .storage import DATA_DIR, read_table_rows

DEFAULT_EPOCH = dt.date(2026, 11, 10)


def is_run_day(pcfg: dict[str, Any], day: dt.date) -> bool:
    n = int(pcfg.get("every_n_days", 1))
    epoch = pcfg.get("epoch", DEFAULT_EPOCH)
    epoch = epoch if isinstance(epoch, dt.date) else dt.date.fromisoformat(str(epoch))
    return (day - epoch).days % n == 0


def remaining_run_days_in_month(pcfg: dict[str, Any], day: dt.date) -> int:
    last = calendar.monthrange(day.year, day.month)[1]
    return sum(is_run_day(pcfg, day.replace(day=d)) for d in range(day.day, last + 1))


def calls_used(provider: str, month: str | None, data_dir: Path = DATA_DIR) -> int:
    """API calls recorded in the searches table (month = 'YYYYMM', or None for all time)."""
    total = 0
    for row in read_table_rows("searches", month, data_dir):
        if row.get("provider") == provider:
            try:
                total += int(row.get("api_calls") or 0)
            except ValueError:
                pass
    return total


@dataclass
class Allowance:
    calls: int | None          # None = unlimited
    reason: str


def run_allowance(provider: str, pcfg: dict[str, Any], day: dt.date, data_dir: Path = DATA_DIR) -> Allowance:
    """How many API calls this provider may spend in today's run."""
    parts = []
    limit: int | None = None
    monthly = pcfg.get("monthly_budget")
    if monthly is not None:
        used = calls_used(provider, day.strftime("%Y%m"), data_dir)
        runs_left = max(1, remaining_run_days_in_month(pcfg, day))
        share = max(0, int(monthly) - used) // runs_left
        limit = share
        parts.append(f"month {used}/{monthly} used, {runs_left} runs left -> {share}/run")
    total = pcfg.get("total_budget")
    if total is not None:
        used_all = calls_used(provider, None, data_dir)
        left = max(0, int(total) - used_all)
        limit = left if limit is None else min(limit, left)
        parts.append(f"lifetime {used_all}/{total} used")
    return Allowance(limit, "; ".join(parts) or "unlimited")


def select_by_priority(items: list[Any], cost: list[int], allowance: int | None) -> tuple[list[Any], list[Any]]:
    """Greedy pick in priority order (items must already be sorted) until the allowance is spent."""
    if allowance is None:
        return list(items), []
    chosen, skipped, spent = [], [], 0
    for item, c in zip(items, cost):
        if spent + c <= allowance:
            chosen.append(item)
            spent += c
        else:
            skipped.append(item)
    return chosen, skipped
