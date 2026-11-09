"""Load the YAML config and expand it into concrete searches."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .dates import resolve_trip_instances

MODES = ("round_trip", "one_way_outbound", "one_way_return")
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "searches.yaml"


@dataclass(frozen=True)
class Search:
    """One concrete query, independent of provider."""

    group: str
    route_id: str
    origin: str
    destination: str
    trip_id: str
    trip_year: int | None
    mode: str
    outbound_date: dt.date
    return_date: dt.date | None
    priority: int
    params: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    providers: tuple[str, ...] | None = None

    @property
    def search_id(self) -> str:
        year = f"{self.trip_year}" if self.trip_year else "fixed"
        return f"{self.group}__{self.route_id}__{self.trip_id}__{year}__{self.mode}"

    @property
    def is_round_trip(self) -> bool:
        return self.mode == "round_trip"

    @property
    def leg(self) -> tuple[str, str, dt.date]:
        """(from, to, date) of the searched leg; for round trips, the outbound leg."""
        if self.mode == "one_way_return":
            assert self.return_date is not None
            return self.destination, self.origin, self.return_date
        return self.origin, self.destination, self.outbound_date

    def key_fields(self) -> dict[str, Any]:
        leg_from, leg_to, leg_date = self.leg
        return {
            "search_id": self.search_id,
            "group": self.group,
            "route_id": self.route_id,
            "origin": self.origin,
            "destination": self.destination,
            "trip_id": self.trip_id,
            "trip_year": self.trip_year or "",
            "mode": self.mode,
            "leg_origin": leg_from,
            "leg_destination": leg_to,
            "outbound_date": self.outbound_date.isoformat(),
            "return_date": self.return_date.isoformat() if self.return_date else "",
            "flight_date": leg_date.isoformat(),
            "trip_length_days": (self.return_date - self.outbound_date).days if self.return_date else "",
            "priority": self.priority,
        }


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("defaults", {})
    cfg.setdefault("providers", {})
    cfg.setdefault("mode_priority", {})
    return cfg


def expand_searches(cfg: dict[str, Any], today: dt.date) -> list[Search]:
    defaults = cfg["defaults"]
    min_days = int(defaults.get("min_days_ahead", 1))
    searches: list[Search] = []
    for group in cfg.get("groups", []):
        if not group.get("enabled", True):
            continue
        params = {**defaults, **(group.get("params") or {})}
        providers = tuple(group["providers"]) if group.get("providers") else None
        modes = group.get("modes", list(MODES))
        for mode in modes:
            if mode not in MODES:
                raise ValueError(f"unknown mode {mode!r} in group {group['id']}")
        for trip in group["trips"]:
            for year, out, ret in resolve_trip_instances(trip, today, int(trip.get("min_days_ahead", min_days))):
                for route in group["routes"]:
                    for mode in modes:
                        if mode != "one_way_outbound" and ret is None:
                            continue  # trips without a return date only support one-way outbound
                        prio = (
                            int(cfg["mode_priority"].get(mode, 0))
                            + int(route.get("priority", 0))
                            + int(trip.get("priority", 0))
                            + int(group.get("priority", 0))
                        )
                        searches.append(
                            Search(
                                group=group["id"],
                                route_id=route.get("id", f"{route['origin']}-{route['destination']}"),
                                origin=route["origin"],
                                destination=route["destination"],
                                trip_id=trip["id"],
                                trip_year=year,
                                mode=mode,
                                outbound_date=out,
                                return_date=ret,
                                priority=prio,
                                params=params,
                                providers=providers,
                            )
                        )
    ids = [s.search_id for s in searches]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate search ids: {sorted(dupes)}")
    return searches
