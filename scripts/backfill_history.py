#!/usr/bin/env python3
"""backfill_history.py — one-shot processing of all historical days.

Refreshes the JSONL index, then walks every day from `--since` (or earliest
human turn) to today. It rebuilds selected oxi-* buckets and daily reports.

Idempotent: each daily run uses `replace_events_for_day`, so a repeat just
overwrites the same day.

Usage:
  python3 backfill_history.py                # since earliest JSONL turn
  python3 backfill_history.py --since 2026-04-01
  python3 backfill_history.py --days 30
  python3 backfill_history.py --layer 2      # only Layer 2
  python3 backfill_history.py --no-reports   # buckets only
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib_aw import INDEX_DB_PATH, iso

INDEX_DB = INDEX_DB_PATH
SCRIPT_DIR = Path(__file__).parent


def _earliest_turn() -> datetime:
    if not INDEX_DB.exists():
        print("[backfill] index DB not found, run jsonl_indexer.py first", file=sys.stderr)
        sys.exit(2)
    con = sqlite3.connect(INDEX_DB)
    row = con.execute("SELECT MIN(ts_unix) FROM jsonl_turns").fetchone()
    con.close()
    if not row or row[0] is None:
        print("[backfill] index is empty", file=sys.stderr)
        sys.exit(2)
    return datetime.fromtimestamp(row[0], tz=timezone.utc)


def _run_script(script: str, *args: str) -> None:
    # Use the same interpreter we run under — avoids accidentally invoking a
    # different Python (anaconda x86 under Rosetta, etc.) than the one that
    # imports lib_aw correctly.
    cmd = [sys.executable, str(SCRIPT_DIR / script), *args]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)


def refresh_index() -> None:
    """Migrate and incrementally refresh the human-turn index."""
    _run_script("jsonl_indexer.py", "--quiet")


def run_layer(script: str, day: datetime) -> None:
    _run_script(script, "--date", day.strftime("%Y-%m-%d"))


def run_day(day: datetime, scripts: list[str], *, reports: bool) -> None:
    for script in scripts:
        run_layer(script, day)
    if reports:
        _run_script(
            "aggregate.py",
            "--date",
            day.strftime("%Y-%m-%d"),
            "--skip-trackers",
        )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--since", help="YYYY-MM-DD start date (inclusive)")
    p.add_argument("--days", type=int, help="process last N days")
    p.add_argument("--layer", choices=["1", "2", "25", "all"], default="all",
                   help="which layer to backfill")
    p.add_argument("--no-reports", action="store_true", help="rebuild buckets only")
    p.add_argument("--no-index", action="store_true", help="skip JSONL index refresh/migration")
    args = p.parse_args()

    if not args.no_index:
        print("[backfill] refreshing human-turn index", file=sys.stderr)
        refresh_index()

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    if args.days:
        start = today - timedelta(days=args.days - 1)
    elif args.since:
        start = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    else:
        start = _earliest_turn().replace(hour=0, minute=0, second=0, microsecond=0)

    total_days = (today - start).days + 1
    print(f"[backfill] {start.date()} → {today.date()} ({total_days} days)", file=sys.stderr)
    print(f"[backfill] layer={args.layer}", file=sys.stderr)

    scripts: list[str] = []
    if args.layer in ("1", "all"):
        scripts.append("tracker_layer1.py")
    if args.layer in ("2", "all"):
        scripts.append("tracker_layer2.py")
    if args.layer in ("25", "all"):
        scripts.append("tracker_layer25.py")

    t0 = time.time()
    for i in range(total_days):
        day = start + timedelta(days=i)
        ts = time.time()
        run_day(day, scripts, reports=not args.no_reports)
        dur = time.time() - ts
        print(f"  [{i+1}/{total_days}] {day.date()}  {dur:.1f}s", flush=True)

    print(f"[backfill] done in {time.time()-t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
