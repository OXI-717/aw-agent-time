#!/usr/bin/env python3
"""tracker_layer1.py — state machine over raw ActivityWatch events.

Reads `aw-watcher-window` + `aw-watcher-afk` (+ optional `aw-watcher-input`) for the
target day, applies a 4-state machine, and writes labelled segments to
`oxi-states_<host>` bucket.

States:
  ACTIVE_EDIT    last keyboard/mouse input within IDLE_TIMEOUT_SEC
  ACTIVE_REVIEW  last input >IDLE_TIMEOUT_SEC but <REVIEW_CAP_SEC and window still focused
  IDLE           review cap exhausted (formally AFK)
  AWAY_BLUR      no window-watcher event (screen locked / app switched away)

Each segment carries `{state, project, app, title, reason}`.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from lib_aw import (
    TRACKER_CONFIG,
    HOSTNAME,
    INDEX_DB_PATH,
    bucket_name,
    ensure_bucket,
    get_events,
    iso,
    list_buckets,
    real_project_from_cwd,
    replace_events_for_day,
)

# ── tunables ──────────────────────────────────────────────────────────────────
IDLE_TIMEOUT_SEC = 90      # after this much idle, leave ACTIVE_EDIT
REVIEW_CAP_SEC = 8 * 60    # how long REVIEW lasts before degrading to IDLE
MIN_SEGMENT_SEC = 5        # ignore tiny noise segments
FOCUS_SAMPLE_LIMIT_SEC = 10
HUMAN_TURN_RADIUS_SEC = 120

OUTPUT_BUCKET = bucket_name("states")
OUTPUT_EVENT_TYPE = f"{TRACKER_CONFIG.event_type_prefix}.state"


# ── project extraction ────────────────────────────────────────────────────────
HOME = Path.home()
INDEX_DB = INDEX_DB_PATH

# Evidence timelines are built once per run.
_FOCUS_TIMELINE: list[tuple[datetime, datetime, str]] = []
_HUMAN_TURNS: list[tuple[datetime, str]] = []


def _build_human_turn_timeline(start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    """Pull bounded, non-autonomous human turns from the SQLite index."""
    import sqlite3
    out: list[tuple[datetime, str]] = []
    if not INDEX_DB.exists():
        print(f"[layer1] WARN: index DB not found at {INDEX_DB}; human-turn fallback disabled", file=sys.stderr)
        return out
    margin = timedelta(seconds=HUMAN_TURN_RADIUS_SEC)
    q_start = (start - margin).timestamp()
    q_end = (end + margin).timestamp()
    con = sqlite3.connect(INDEX_DB)
    con.execute("PRAGMA query_only = 1")
    cols = {row[1] for row in con.execute("PRAGMA table_info(jsonl_turns)")}
    if "autonomous" not in cols:
        con.close()
        print("[layer1] WARN: stale JSONL index; human-turn fallback disabled", file=sys.stderr)
        return out
    rows = con.execute(
        "SELECT ts_unix, project FROM jsonl_turns "
        "WHERE ts_unix BETWEEN ? AND ? AND kind='user' AND autonomous=0 "
        "ORDER BY ts_unix",
        (q_start, q_end),
    ).fetchall()
    con.close()
    for ts_unix, project in rows:
        if project and project not in ("unknown", "codex", "?"):
            out.append((datetime.fromtimestamp(ts_unix, tz=timezone.utc), project))
    return out


def _focus_intervals(events: list[dict]) -> list[tuple[datetime, datetime, str]]:
    """Turn focus heartbeats into bounded, non-overlapping observation intervals."""
    observations: list[tuple[datetime, datetime, str]] = []
    for event in events:
        project = (event.get("data") or {}).get("project") or ""
        if not project or project in ("unknown", "?"):
            continue
        start = _parse_ts(event["timestamp"])
        observed_end = start + timedelta(seconds=float(event.get("duration") or 0))
        observations.append((start, observed_end, project))
    observations.sort()

    intervals: list[tuple[datetime, datetime, str]] = []
    for index, (start, observed_end, project) in enumerate(observations):
        end = observed_end + timedelta(seconds=FOCUS_SAMPLE_LIMIT_SEC)
        if index + 1 < len(observations):
            end = min(end, observations[index + 1][0])
        if end > start:
            intervals.append((start, end, project))
    return intervals


def _project_at(t: datetime, intervals: list[tuple[datetime, datetime, str]]) -> str:
    for start, end, project in intervals:
        if start <= t < end:
            return project
        if start > t:
            break
    return ""


def _nearest_human_turn(
    t: datetime,
    turns: list[tuple[datetime, str]],
) -> tuple[str, bool, datetime] | None:
    # Half-open window: the turn justifies [turn - radius, turn + radius), so
    # `turn + radius` is a true exclusive end and a probe landing exactly there
    # is no longer covered by this evidence.
    candidates = [
        turn for turn in turns
        if abs((turn[0] - t).total_seconds()) < HUMAN_TURN_RADIUS_SEC
    ]
    if not candidates:
        return None
    best_ts, best_project = min(
        candidates,
        key=lambda turn: (
            abs((turn[0] - t).total_seconds()),
            -turn[0].timestamp(),
            turn[1],
        ),
    )
    return (
        best_project,
        len({project for _ts, project in candidates}) > 1,
        best_ts,
    )


def project_from_window(data: dict[str, Any], event_ts: datetime | None = None) -> str:
    """Pull an explicit project tag from legacy terminal window data."""
    app = (data.get("app") or "").lower()
    title = (data.get("title") or "").strip()

    terminal_apps = {"iterm2", "iterm", "warp", "terminal", "ghostty", "alacritty"}
    is_terminal = any(t in app for t in terminal_apps)

    if is_terminal:
        for prefix in ("claude_grid_", "claude_", "claude-"):
            if title.startswith(prefix):
                return title[len(prefix):].split("-")[0] or "cc"
        return title or "terminal"

    return app or "unknown"


def _evidence_end_at(
    t: datetime, intervals: list[tuple[datetime, datetime, str]]
) -> datetime | None:
    """End of the focus-evidence interval covering `t`, if any."""
    for start, end, _project in intervals:
        if start <= t < end:
            return end
        if start > t:
            break
    return None


def _resolve_attribution(
    data: dict[str, Any],
    t: datetime,
    focus_timeline: list[tuple[datetime, datetime, str]],
    human_turns: list[tuple[datetime, str]],
) -> tuple[str, str, bool, datetime | None]:
    """Resolve project, evidence source, ambiguity, and evidence-validity end for one slice."""
    app = (data.get("app") or "").strip().lower()
    title = (data.get("title") or "").strip()
    terminal_apps = ("iterm", "warp", "terminal", "ghostty", "alacritty")

    if app == "orca":
        focused = _project_at(t, focus_timeline)
        if focused:
            return focused, "tmux_focus", False, _evidence_end_at(t, focus_timeline)
        human = _nearest_human_turn(t, human_turns)
        if human:
            project, ambiguous, turn_ts = human
            # Evidence expires HUMAN_TURN_RADIUS_SEC after the turn itself, not
            # after the probe that happened to observe it.
            valid_until = turn_ts + timedelta(seconds=HUMAN_TURN_RADIUS_SEC)
            return project, "human_turn", ambiguous, valid_until

    if any(name in app for name in terminal_apps) and title.startswith(
        ("claude_grid_", "claude_", "claude-")
    ):
        return project_from_window(data, event_ts=t), "window", False, None

    project = project_from_window(data, event_ts=t)
    if not project or project in ("unknown", "?"):
        return "", "unattributed", False, None
    return project, "application", False, None


# ── core logic ────────────────────────────────────────────────────────────────
def _intervals_active_afk(afk_events: list[dict]) -> list[tuple[datetime, datetime]]:
    """Return list of (start, end) where status == not-afk."""
    out: list[tuple[datetime, datetime]] = []
    for ev in afk_events:
        if ev["data"].get("status") != "not-afk":
            continue
        start = _parse_ts(ev["timestamp"])
        end = start + timedelta(seconds=ev["duration"])
        out.append((start, end))
    return out


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _last_input_before(intervals: list[tuple[datetime, datetime]], t: datetime) -> datetime | None:
    """Find latest non-afk interval ending at-or-before t."""
    candidates = [end for s, end in intervals if s <= t]
    return max(candidates) if candidates else None


def build_segments(window_events: list[dict], afk_events: list[dict]) -> list[dict]:
    """Produce labelled state segments aligned with window events."""
    active_intervals = _intervals_active_afk(afk_events)

    segments: list[dict] = []
    for ev in window_events:
        start = _parse_ts(ev["timestamp"])
        dur = float(ev.get("duration") or 0)
        if dur < MIN_SEGMENT_SEC:
            continue
        end = start + timedelta(seconds=dur)
        data = ev.get("data") or {}
        app = data.get("app") or ""
        title = data.get("title") or ""
        # Walk the window-event in 30-second slices; cheap and lets us flip state
        # mid-window if the user transitioned EDIT→REVIEW→IDLE without changing app.
        step = timedelta(seconds=30)
        cursor = start
        cur_key: tuple[str, str, str, bool] | None = None
        emitted_for_event = False
        cur_start = start
        cur_reason = ""
        next_probe = start
        while cursor < end:
            t_probe = cursor
            within_active = any(s <= t_probe <= e for s, e in active_intervals)
            last_in = _last_input_before(active_intervals, t_probe)
            if within_active:
                state, reason = "ACTIVE_EDIT", "input_within_window"
            elif last_in and (t_probe - last_in).total_seconds() < REVIEW_CAP_SEC:
                state, reason = "ACTIVE_REVIEW", f"idle_{int((t_probe - last_in).total_seconds())}s"
            else:
                state, reason = "IDLE", "review_cap_exceeded" if last_in else "no_input_recorded"

            project, source, ambiguous, evidence_until = _resolve_attribution(
                data, t_probe, _FOCUS_TIMELINE, _HUMAN_TURNS
            )
            key = (state, project, source, ambiguous)

            if cur_key is None:
                cur_key = key
                cur_start = cursor
                cur_reason = reason
            elif key != cur_key:
                old_state, old_project, old_source, old_ambiguous = cur_key
                emitted_for_event = True
                segments.append({
                    "timestamp": iso(cur_start),
                    "duration": (cursor - cur_start).total_seconds(),
                    "data": {
                        "state": old_state,
                        "project": old_project,
                        "app": app,
                        "title": title,
                        "reason": cur_reason,
                        "attribution_source": old_source,
                        "attribution_ambiguous": old_ambiguous,
                    },
                })
                cur_key = key
                cur_start = cursor
                cur_reason = reason

            # Re-probe at the evidence boundary (not just every 30s) so a slice
            # never outlives the focus/human evidence that justified its attribution.
            if evidence_until is not None and cursor < evidence_until < cursor + step:
                next_probe = evidence_until
            else:
                next_probe = cursor + step
            cursor = next_probe

        # Flush final slice of this window event. MIN_SEGMENT_SEC exists to drop
        # tiny standalone events as noise. Once an event has already produced a
        # segment, its remainder is not noise but the rest of a real event —
        # whether the split came from an evidence boundary or from an ordinary
        # state/project change — and dropping it silently loses that time.
        tail = (end - cur_start).total_seconds()
        if cur_key is not None and (tail >= MIN_SEGMENT_SEC or emitted_for_event):
            state, project, source, ambiguous = cur_key
            segments.append({
                "timestamp": iso(cur_start),
                "duration": (end - cur_start).total_seconds(),
                "data": {
                    "state": state,
                    "project": project,
                    "app": app,
                    "title": title,
                    "reason": cur_reason,
                    "attribution_source": source,
                    "attribution_ambiguous": ambiguous,
                },
            })

    return segments


# ── bucket discovery ──────────────────────────────────────────────────────────
def find_source_buckets() -> tuple[str | None, str | None, str | None]:
    buckets = list_buckets()
    win = afk = focus = None
    for bid in buckets:
        if bid.startswith("aw-watcher-window_"):
            win = bid
        elif bid.startswith("aw-watcher-afk_"):
            afk = bid
        elif bid.startswith("aw-watcher-tmux-focus_"):
            focus = bid
    return win, afk, focus


# ── CLI ──────────────────────────────────────────────────────────────────────
def run_for_day(day: datetime, dry_run: bool) -> None:
    win, afk, focus = find_source_buckets()
    if not win or not afk:
        print(f"!! source buckets not found (window={win}, afk={afk}). Is AW running?", file=sys.stderr)
        sys.exit(2)

    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    print(f"[layer1] period {iso(start)} → {iso(end)}", file=sys.stderr)
    print(f"[layer1] sources: {win}, {afk}", file=sys.stderr)

    win_events = get_events(win, start, end)
    afk_events = get_events(afk, start, end)
    print(f"[layer1] raw: window={len(win_events)} afk={len(afk_events)}", file=sys.stderr)

    global _FOCUS_TIMELINE, _HUMAN_TURNS
    focus_events = get_events(focus, start, end) if focus else []
    _FOCUS_TIMELINE = _focus_intervals(focus_events)
    _HUMAN_TURNS = _build_human_turn_timeline(start, end)
    print(
        f"[layer1] evidence: focus={len(_FOCUS_TIMELINE)} "
        f"human_turns={len(_HUMAN_TURNS)}",
        file=sys.stderr,
    )

    segments = build_segments(win_events, afk_events)
    print(f"[layer1] built {len(segments)} state segments", file=sys.stderr)

    # Summary table
    by = {}
    for s in segments:
        key = (s["data"]["state"], s["data"]["project"])
        by[key] = by.get(key, 0) + s["duration"]
    print("\n[layer1] per-state×project (seconds):", file=sys.stderr)
    for (state, proj), secs in sorted(by.items(), key=lambda x: -x[1])[:25]:
        print(f"  {state:<14s} {proj:<28s} {secs/60:6.1f}m", file=sys.stderr)

    if dry_run:
        print("[layer1] DRY RUN — not writing to AW", file=sys.stderr)
        return
    ensure_bucket(OUTPUT_BUCKET, OUTPUT_EVENT_TYPE)
    replace_events_for_day(OUTPUT_BUCKET, start, segments)
    print(f"[layer1] wrote {len(segments)} events to {OUTPUT_BUCKET}", file=sys.stderr)


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
        day = now  # default = today

    run_for_day(day, args.dry_run)


if __name__ == "__main__":
    main()
