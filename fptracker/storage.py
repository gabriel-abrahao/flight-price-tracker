"""Append-only storage: one immutable gzipped CSV per (table, run, provider) plus raw JSON responses.

Layout (all under data/):
    raw/<provider>/YYYY/MM/DD/<run_id>__<search_id>.json.gz   full provider response
    tables/<table>/YYYY/MM/<run_id>__<provider>.csv.gz       normalized rows

Immutable small files keep git history cheap and are trivially globbed by pandas, polars or DuckDB:
    duckdb: SELECT * FROM read_csv_auto('data/tables/itineraries/**/*.csv.gz', union_by_name=true)
"""
from __future__ import annotations

import csv
import glob
import gzip
import io
import json
from pathlib import Path
from typing import Any, Iterable

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Shared key columns that start every table, so tables join on (run_id, provider, search_id).
KEY_COLUMNS = [
    "run_id", "provider", "search_id", "query_ts_utc", "query_ts_local", "query_date_local",
    "query_weekday", "query_hour_local", "days_to_departure",
    "group", "route_id", "origin", "destination", "trip_id", "trip_year", "mode",
    "leg_origin", "leg_destination", "outbound_date", "return_date", "flight_date",
    "trip_length_days", "priority",
]

TABLE_COLUMNS: dict[str, list[str]] = {
    "searches": KEY_COLUMNS + [
        "status", "error", "api_calls", "n_itineraries", "min_price", "currency",
        "price_level", "typical_price_low", "typical_price_high", "provider_search_id",
        "request_params", "raw_path", "duration_s",
    ],
    "itineraries": KEY_COLUMNS + [
        "itinerary_uid", "parent_itinerary_uid", "list_name", "rank", "price", "currency",
        "price_status", "price_scope", "self_transfer",
        "out_departure_time", "out_arrival_time", "out_duration_min", "out_n_stops",
        "out_airlines", "out_flight_numbers", "out_route", "out_layovers", "out_layover_min",
        "out_overnight_layover",
        "in_departure_time", "in_arrival_time", "in_duration_min", "in_n_stops",
        "in_airlines", "in_flight_numbers", "in_route", "in_layovers", "in_layover_min",
        "carbon_g", "carbon_typical_g", "carbon_diff_pct",
        "bags_carry_on", "bags_checked", "has_mixed_carriers", "extensions",
        "provider_itinerary_id",
    ],
    "segments": KEY_COLUMNS + [
        "itinerary_uid", "direction", "seg_index",
        "dep_airport", "dep_airport_name", "dep_time_local", "dep_time_utc",
        "arr_airport", "arr_airport_name", "arr_time_local", "arr_time_utc",
        "duration_min", "airline", "marketing_carrier", "flight_number", "operating_carrier",
        "aircraft", "travel_class", "legroom", "overnight", "often_delayed",
        "layover_after_min", "layover_after_airport", "extensions",
    ],
    "price_history": KEY_COLUMNS + ["history_ts_utc", "history_date", "history_price", "currency"],
}


def _csv_bytes(columns: list[str], rows: Iterable[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for row in rows:
        w.writerow({k: _cell(v) for k, v in row.items()})
    return gzip.compress(buf.getvalue().encode("utf-8"), mtime=0)


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (list, tuple)):
        return "|".join("" if x is None else str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
    return v


def write_table(table: str, run_id: str, provider: str, rows: list[dict[str, Any]], data_dir: Path = DATA_DIR) -> Path | None:
    if not rows:
        return None
    unknown = set().union(*(r.keys() for r in rows)) - set(TABLE_COLUMNS[table])
    if unknown:
        raise ValueError(f"{table}: unknown columns {sorted(unknown)}")
    path = data_dir / "tables" / table / run_id[:4] / run_id[4:6] / f"{run_id}__{provider}.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_csv_bytes(TABLE_COLUMNS[table], rows))
    return path


def write_raw(provider: str, run_id: str, search_id: str, payload: Any, data_dir: Path = DATA_DIR) -> str:
    rel = Path("raw") / provider / run_id[:4] / run_id[4:6] / run_id[6:8] / f"{run_id}__{search_id}.json.gz"
    path = data_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_scrub(payload), ensure_ascii=False, default=str, indent=None)
    path.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))
    return str(rel)


def _scrub(obj: Any) -> Any:
    """Remove anything that looks like a credential before it is committed."""
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in {"api_key", "apikey", "token", "x-api-key", "x-access-token"} else _scrub(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    if isinstance(obj, str) and "api_key=" in obj:
        import re
        return re.sub(r"api_key=[^&\s\"]+", "api_key=***", obj)
    return obj


def read_table_rows(table: str, pattern_month: str | None = None, data_dir: Path = DATA_DIR) -> list[dict[str, str]]:
    """Read rows of a table (optionally only YYYYMM) without pandas; used for budget accounting."""
    sub = f"{pattern_month[:4]}/{pattern_month[4:6]}" if pattern_month else "*/*"
    rows: list[dict[str, str]] = []
    for f in sorted(glob.glob(str(data_dir / "tables" / table / sub / "*.csv.gz"))):
        with gzip.open(f, "rt", encoding="utf-8", newline="") as fh:
            rows.extend(csv.DictReader(fh))
    return rows
