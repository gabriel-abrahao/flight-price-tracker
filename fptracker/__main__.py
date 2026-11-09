"""CLI:  python -m fptracker plan | run   (see --help)."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load_config
from .runner import execute, make_plan
from .storage import DATA_DIR


def _now(cfg, date_arg: str | None) -> tuple[dt.datetime, dt.datetime]:
    tz = ZoneInfo(cfg.get("timezone", "Europe/Berlin"))
    now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    if date_arg:  # simulate another day (planning only)
        d = dt.date.fromisoformat(date_arg)
        now_local = dt.datetime.combine(d, dt.time(7, 0), tz)
        return now_local.astimezone(dt.timezone.utc), now_local
    return now_utc, now_utc.astimezone(tz)


def _print_plan(searches, plans, out=print) -> None:
    out(f"{len(searches)} searches expanded from config:")
    for s in sorted(searches, key=lambda s: (s.priority, s.search_id)):
        ret = s.return_date.isoformat() if s.return_date else "-"
        out(f"  p{s.priority}  {s.search_id:<75} out {s.outbound_date} ({s.outbound_date:%a})  ret {ret}")
    out("")
    for p in plans:
        line = f"{p.name:<14} {p.status:<13} {p.note}"
        if p.status == "run":
            line += f" | {len(p.chosen)} searches, {p.planned_calls} calls"
            if p.skipped:
                line += f", {len(p.skipped)} skipped by budget"
        out(line)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fptracker")
    ap.add_argument("--config", default=None, help="path to searches.yaml")
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("--force", action="store_true", help="ignore every_n_days schedules")
        sp.add_argument("--providers", default="", help="comma-separated subset of providers")
        sp.add_argument("--only", default=None, help="regex on search_id")
        if name == "plan":
            sp.add_argument("--date", default=None, help="plan as if today were YYYY-MM-DD")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    data_dir = Path(args.data_dir)
    now_utc, now_local = _now(cfg, getattr(args, "date", None))
    only_providers = [p for p in args.providers.split(",") if p] or None
    searches, plans = make_plan(cfg, now_local, force=args.force, only_providers=only_providers,
                                only_pattern=args.only, data_dir=data_dir, instantiate=args.cmd == "run")
    _print_plan(searches, plans)
    if args.cmd == "plan":
        return 0

    print(f"\nRun at {now_local.isoformat(timespec='seconds')}")
    stats = execute(cfg, plans, now_utc, data_dir)
    lines = [f"### Flight price run {stats['run_id']}", "", "| provider | ok | no results | errors | API calls |",
             "|---|---|---|---|---|"]
    for name, c in stats["providers"].items():
        lines.append(f"| {name} | {c.get('ok', 0)} | {c.get('no_results', 0)} | {c.get('error', 0)} | {c['api_calls']} |")
    for p in plans:
        if p.status != "run":
            lines.append(f"\n- {p.name}: {p.status} {p.note}")
        elif p.skipped:
            lines.append(f"\n- {p.name}: skipped by budget: {', '.join(s.search_id for s in p.skipped)}")
    summary = "\n".join(lines)
    print("\n" + summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary + "\n")
    attempted = sum(sum(v for k, v in c.items() if k != "api_calls") for c in stats["providers"].values())
    ok = sum(c.get("ok", 0) + c.get("no_results", 0) for c in stats["providers"].values())
    if attempted and not ok:
        print("Every search failed.", file=sys.stderr)
        return 1
    if any(p.status == "broken" for p in plans):
        print("A provider is broken (see above).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
