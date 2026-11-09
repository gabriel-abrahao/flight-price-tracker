"""Date rules: resolve trip definitions (fixed dates or Carnival-relative rules) into concrete dates."""
from __future__ import annotations

import datetime as dt
from typing import Any

from dateutil.easter import easter
from dateutil.relativedelta import relativedelta

WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def anchors_for_year(year: int) -> dict[str, dt.date]:
    """Movable-feast anchors for a given year (Brazilian Carnival is tied to Easter)."""
    e = easter(year)
    carnival_tuesday = e - dt.timedelta(days=47)
    return {
        "easter": e,
        "carnival_friday": carnival_tuesday - dt.timedelta(days=4),
        "carnival_saturday": carnival_tuesday - dt.timedelta(days=3),
        "carnival_sunday": carnival_tuesday - dt.timedelta(days=2),
        "carnival_monday": carnival_tuesday - dt.timedelta(days=1),
        "carnival_tuesday": carnival_tuesday,
        "ash_wednesday": carnival_tuesday + dt.timedelta(days=1),
        "jan_1": dt.date(year, 1, 1),
    }


def snap_to_weekday(d: dt.date, weekday: str, mode: str = "nearest") -> dt.date:
    """Move ``d`` to a weekday. mode: nearest | next | previous (``next``/``previous`` keep d if it matches)."""
    target = WEEKDAYS[weekday.upper()[:3]]
    fwd = (target - d.weekday()) % 7
    back = (d.weekday() - target) % 7
    if mode == "next":
        return d + dt.timedelta(days=fwd)
    if mode == "previous":
        return d - dt.timedelta(days=back)
    if mode == "nearest":
        return d + dt.timedelta(days=fwd) if fwd <= back else d - dt.timedelta(days=back)
    raise ValueError(f"unknown snap mode {mode!r}")


def _resolve_spec(spec: dict[str, Any], year: int, resolved: dict[str, dt.date]) -> dt.date:
    if "date" in spec:
        base = spec["date"]
        base = base if isinstance(base, dt.date) else dt.date.fromisoformat(str(base))
    elif "anchor" in spec:
        anchors = anchors_for_year(year)
        if spec["anchor"] not in anchors:
            raise ValueError(f"unknown anchor {spec['anchor']!r}; options: {sorted(anchors)}")
        base = anchors[spec["anchor"]]
    elif "from" in spec:
        base = resolved[spec["from"]]
    else:
        raise ValueError(f"date spec needs one of date/anchor/from: {spec}")
    base = base + relativedelta(
        months=int(spec.get("offset_months", 0)),
        weeks=int(spec.get("offset_weeks", 0)),
        days=int(spec.get("offset_days", 0)),
    )
    if spec.get("weekday"):
        base = snap_to_weekday(base, spec["weekday"], spec.get("snap", "nearest"))
    return base


def resolve_trip_for_year(trip: dict[str, Any], year: int) -> tuple[dt.date, dt.date | None]:
    """Return (outbound, return) dates for a trip definition in a given anchor year."""
    specs = {"outbound": trip["outbound"]}
    if trip.get("return"):
        specs["return"] = trip["return"]
    resolved: dict[str, dt.date] = {}
    pending = dict(specs)
    for _ in range(3):  # at most two dependency levels
        for key, spec in list(pending.items()):
            dep = spec.get("from")
            if dep is None or dep in resolved:
                resolved[key] = _resolve_spec(spec, year, resolved)
                pending.pop(key)
    if pending:
        raise ValueError(f"circular or missing 'from' references in trip {trip.get('id')}: {pending}")
    out, ret = resolved["outbound"], resolved.get("return")
    if ret is not None and ret <= out:
        raise ValueError(f"trip {trip.get('id')} ({year}): return {ret} not after outbound {out}")
    return out, ret


def is_fixed(trip: dict[str, Any]) -> bool:
    return all("anchor" not in (trip.get(k) or {}) for k in ("outbound", "return"))


def resolve_trip_instances(
    trip: dict[str, Any], today: dt.date, min_days_ahead: int = 1
) -> list[tuple[int | None, dt.date, dt.date | None]]:
    """Concrete (anchor_year, outbound, return) instances worth searching today.

    ``year`` in the trip may be ``next`` (default: the first anchor year whose outbound is still in the
    future), an int, or a list of ints. Instances whose outbound is less than ``min_days_ahead`` away
    are dropped, so trips stop being queried once they depart.
    """
    cutoff = today + dt.timedelta(days=min_days_ahead)
    if is_fixed(trip):
        out, ret = resolve_trip_for_year(trip, today.year)
        return [(None, out, ret)] if out >= cutoff else []
    year = trip.get("year", "next")
    if year == "next":
        for y in range(today.year, today.year + 3):
            out, ret = resolve_trip_for_year(trip, y)
            if out >= cutoff:
                return [(y, out, ret)]
        return []
    years = year if isinstance(year, list) else [year]
    instances = []
    for y in years:
        out, ret = resolve_trip_for_year(trip, int(y))
        if out >= cutoff:
            instances.append((int(y), out, ret))
    return instances
