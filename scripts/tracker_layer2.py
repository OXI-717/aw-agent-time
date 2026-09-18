#!/usr/bin/env python3
"""tracker_layer2.py — AI engagement from Claude Code + Codex JSONL files.

Reads:
  ~/.claude/projects/*/*.jsonl       (Claude Code conversation logs)
  ~/.codex/sessions/YYYY/MM/DD/*.jsonl (Codex CLI rollouts)

Computes engagement blocks from genuine human user turns with gaps ≤ GAP_LIMIT.
Each block becomes one AW event in bucket `oxi-engagement_<host>` with
`{engine, project, session_id}`.

This complements `agentlytics` (which has its own dashboard). We pipe minimal data
into AW so it shows up on the same timeline as Layer 1 / Layer 2.5.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib_aw import (
    TRACKER_CONFIG,
    INDEX_DB_PATH,
    bucket_name,
    ensure_bucket,
    iso,
    real_project_from_cwd,
    replace_events_for_day,
)

# We now read from SQLite index instead of re-parsing JSONL files every run.
INDEX_DB = INDEX_DB_PATH

# ── tunables ──
GAP_LIMIT_SEC = 5 * 60   # gap > 5min ends an engagement block
MIN_BLOCK_SEC = 30       # ignore single-prompt bursts

OUTPUT_BUCKET = bucket_name("engagement")
OUTPUT_EVENT_TYPE = f"{TRACKER_CONFIG.event_type_prefix}.engagement"

HOME = Path.home()
CLAUDE_DIR = TRACKER_CONFIG.claude_projects_dir
CODEX_DIR = TRACKER_CONFIG.codex_sessions_dir


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


# ── Claude Code reader ────────────────────────────────────────────────────────
def claude_turns(jsonl: Path) -> list[tuple[datetime, str, str]]:
    """Yield (ts, role, cwd) for user/assistant entries in one Claude session file."""
    out: list[tuple[datetime, str, str]] = []
    try:
        for line in jsonl.open():
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            if t not in ("user", "assistant"):
                continue
            ts = _parse_iso(d.get("timestamp") or "")
            if not ts:
                continue
            cwd = d.get("cwd") or ""
            out.append((ts, t, cwd))
    except Exception as e:
        print(f"[layer2] skip {jsonl}: {e}", file=sys.stderr)
    return out


# ── Codex reader ──────────────────────────────────────────────────────────────
def codex_turns(jsonl: Path) -> list[tuple[datetime, str, str]]:
    """Codex rollout files: `{timestamp, type, payload}` where payload may have cwd."""
    out: list[tuple[datetime, str, str]] = []
    cwd_hint = ""
    try:
        for line in jsonl.open():
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = _parse_iso(d.get("timestamp") or "")
            if not ts:
                continue
            t = d.get("type") or ""
            pl = d.get("payload") or {}
            if t == "session_meta":
                cwd_hint = pl.get("cwd") or cwd_hint
                continue
            # Treat response_item / event_msg as engagement signal
            if t in ("response_item", "event_msg"):
                # Codex doesn't cleanly separate user vs assistant in rollout — collapse.
                out.append((ts, "turn", cwd_hint))
    except Exception as e:
        print(f"[layer2] skip {jsonl}: {e}", file=sys.stderr)
    return out


# ── block builder ────────────────────────────────────────────────────────────
def build_blocks(turns: list[tuple[datetime, str, str]], engine: str, session_id: str) -> list[dict]:
    # Engagement is anchored exclusively by human input. Assistant output is useful
    # session data but must not turn unattended agent execution into human time.
    user_turns = sorted(turn for turn in turns if turn[1] == "user")
    if not user_turns:
        return []
    blocks: list[dict] = []
    block_start = user_turns[0][0]
    block_cwd = user_turns[0][2]
    prev = user_turns[0][0]
    for ts, _role, cwd in user_turns[1:]:
        if (ts - prev).total_seconds() > GAP_LIMIT_SEC:
            blocks.append(_engagement_block(block_start, prev, block_cwd, engine, session_id))
            block_start = ts
            block_cwd = cwd or block_cwd
        prev = ts
    blocks.append(_engagement_block(block_start, prev, block_cwd, engine, session_id))
    return blocks


def _engagement_block(
    start: datetime,
    last_user: datetime,
    cwd: str,
    engine: str,
    session_id: str,
) -> dict:
    return {
        "timestamp": iso(start),
        "duration": (last_user - start).total_seconds() + MIN_BLOCK_SEC,
        "data": {
            "engine": engine,
            "project": real_project_from_cwd(cwd) if cwd else "unknown",
            "session_id": session_id,
            "cwd": cwd,
        },
    }


def _overlap(ev: dict, start: datetime, end: datetime) -> dict | None:
    """Clip event to [start, end). Return None if no overlap."""
    t = _parse_iso(ev["timestamp"])
    if not t:
        return None
    ev_end = t + timedelta(seconds=ev["duration"])
    if ev_end < start or t >= end:
        return None
    c_start = max(t, start)
    c_end = min(ev_end, end)
    if (c_end - c_start).total_seconds() < 1:
        return None
    clipped = dict(ev)
    clipped["timestamp"] = iso(c_start)
    clipped["duration"] = (c_end - c_start).total_seconds()
    return clipped


def collect_for_day(day: datetime) -> list[dict]:
    """Read pre-indexed turns from SQLite, build engagement blocks per session, clip to day."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    # Include turns up to GAP_LIMIT before/after the day, so blocks straddling
    # the midnight boundary get clipped correctly.
    pad = timedelta(seconds=GAP_LIMIT_SEC + 60)
    q_start = (start - pad).timestamp()
    q_end = (end + pad).timestamp()

    if not INDEX_DB.exists():
        print(f"[layer2] index DB not found: {INDEX_DB} — run jsonl_indexer.py first", file=sys.stderr)
        return []

    con = sqlite3.connect(INDEX_DB)
    con.execute("PRAGMA query_only = 1")

    # Group turns by (engine, path) — path uniquely identifies a session file.
    cols = {row[1] for row in con.execute("PRAGMA table_info(jsonl_turns)")}
    if "autonomous" not in cols:
        con.close()
        print("[layer2] index schema is stale — run jsonl_indexer.py", file=sys.stderr)
        return []
    rows = con.execute(
        "SELECT path, ts_unix, kind, engine, cwd FROM jsonl_turns "
        "WHERE ts_unix BETWEEN ? AND ? AND kind='user' AND autonomous=0 "
        "ORDER BY path, ts_unix",
        (q_start, q_end),
    ).fetchall()
    con.close()

    by_session: dict[tuple[str, str], list[tuple[datetime, str, str]]] = defaultdict(list)
    for path, ts, kind, engine, cwd in rows:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        by_session[(engine, path)].append((dt, kind, cwd or ""))

    raw_blocks: list[dict] = []
    for (engine, path), turns in by_session.items():
        # session_id = jsonl filename without extension
        session_id = Path(path).stem
        raw_blocks.extend(build_blocks(turns, engine, session_id))

    out: list[dict] = []
    for b in raw_blocks:
        clipped = _overlap(b, start, end)
        if clipped:
            out.append(clipped)
    return out


def run_for_day(day: datetime, dry_run: bool) -> None:
    events = collect_for_day(day)
    print(f"[layer2] {len(events)} engagement blocks for {day.date().isoformat()}", file=sys.stderr)
    by: dict[tuple[str, str], float] = defaultdict(float)
    for ev in events:
        by[(ev["data"]["project"], ev["data"]["engine"])] += ev["duration"]
    for (proj, eng), secs in sorted(by.items(), key=lambda x: -x[1])[:25]:
        print(f"  {proj:<30s} engine={eng:<10s} {secs/60:6.1f}m", file=sys.stderr)
    if dry_run:
        print("[layer2] DRY RUN — not writing", file=sys.stderr)
        return
    ensure_bucket(OUTPUT_BUCKET, OUTPUT_EVENT_TYPE)
    replace_events_for_day(OUTPUT_BUCKET, day, events)
    print(f"[layer2] wrote {len(events)} events to {OUTPUT_BUCKET}", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--today", action="store_true")
    p.add_argument("--yesterday", action="store_true")
    p.add_argument("--date", help="YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    now = datetime.now(timezone.utc)
    if args.date:
        day = datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc)
    elif args.yesterday:
        day = now - timedelta(days=1)
    else:
        day = now
    run_for_day(day, args.dry_run)


if __name__ == "__main__":
    main()
