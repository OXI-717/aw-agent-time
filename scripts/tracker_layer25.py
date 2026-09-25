#!/usr/bin/env python3
"""tracker_layer25.py — autonomous agent activity.

Sources:
  session index (jsonl_indexer.py)  — activity spans of sessions classified as
      autonomous: `codex exec`, Codex subagents, sessions inside
      `.task-runner/worktrees/`. Works with any orchestrator or none.
  ~/.task-runner/pipelines/*/state.json + run.log         (optional enrichment)
  <project>/.task-runner/status.json                       (optional enrichment)

Worktree sessions are taken from task-runner when its state exists (task ids,
results) and from the session index otherwise, never from both.

Each task becomes one AW event with `{engine, project, task_id, pipeline_id, result}`.
Bucket: `oxi-autonomous_<host>`.

This layer is INDEPENDENT of user wall-clock — it represents agent runtime that the
user delegated. Do NOT add this to Layer 1 totals.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).parent))
from lib_aw import (
    INDEX_DB_PATH,
    TASK_RUNNER_CACHE_PATH,
    TRACKER_CONFIG,
    bucket_name,
    ensure_bucket,
    iso,
    real_project_from_cwd,
    replace_events_for_day,
)

OUTPUT_BUCKET = bucket_name("autonomous")
OUTPUT_EVENT_TYPE = f"{TRACKER_CONFIG.event_type_prefix}.autonomous"

HOME = Path.home()
GLOBAL_TR = TRACKER_CONFIG.task_runner_global_dir
# Discovery is the slow step (scanning configured project roots). Cache the list of
# project dirs with task-runner state and refresh only every hour.
CACHE_FILE = TASK_RUNNER_CACHE_PATH
CACHE_TTL_SEC = 3600


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _project_from_path(p: Path) -> str:
    """`<project>/.task-runner/...` → project basename."""
    parts = p.parts
    try:
        i = parts.index(".task-runner")
    except ValueError:
        return "unknown"
    return parts[i - 1] if i > 0 else "unknown"


# ── per-project status.json reader ────────────────────────────────────────────
def _discover_status_files_fs() -> list[str]:
    """Slow path — actually walk filesystem with `find`. Called only on cache miss."""
    roots = TRACKER_CONFIG.layer25_project_roots
    existing = [str(r) for r in roots if r.exists()]
    if not existing:
        return []
    import subprocess
    exclusions = []
    for excluded in TRACKER_CONFIG.excluded_scan_roots:
        exclusions.extend(["-path", str(excluded), "-prune", "-o"])
    cmd = [
        "find", *existing,
        "-maxdepth", "6",
        *exclusions,
        "(", "-name", "node_modules", "-o", "-name", ".venv", "-o", "-name", "venv",
             "-o", "-name", ".git", "-o", "-name", "__pycache__", ")", "-prune",
        "-o",
        "-path", "*/.task-runner/status.json", "-not", "-path", "*/.task-runner/worktrees/*",
        "-print",
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=30).decode()
    except subprocess.TimeoutExpired:
        # Visible warning — silent fallback would hide missing task-runner data in reports.
        print("[layer25] WARN: find timed out (30s); task-runner data may be incomplete this run.",
              file=sys.stderr)
        return []
    except subprocess.CalledProcessError as e:
        print(f"[layer25] find failed: {e}", file=sys.stderr)
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def iter_project_status_files() -> Iterator[Path]:
    """Yield <project>/.task-runner/status.json paths.

    Cached: the filesystem walk runs at most once per CACHE_TTL_SEC. Existing
    cached paths are validated cheaply (just os.path.exists) on every call.
    """
    import time

    paths: list[str] | None = None
    now = time.time()
    scan_config = {
        "roots": [str(p) for p in TRACKER_CONFIG.layer25_project_roots],
        "excluded": [str(p) for p in TRACKER_CONFIG.excluded_scan_roots],
    }
    if CACHE_FILE.exists():
        try:
            doc = json.loads(CACHE_FILE.read_text())
            if doc.get("scan_config") == scan_config and now - doc.get("scanned_at", 0) < CACHE_TTL_SEC:
                paths = doc.get("paths") or []
        except Exception:
            pass

    if paths is None:
        paths = _discover_status_files_fs()
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps({"scanned_at": now, "paths": paths, "scan_config": scan_config}))

    for p in paths:
        # Cheap existence check — if user deleted a project, skip silently.
        if Path(p).exists():
            yield Path(p)


def events_from_status_json(path: Path) -> list[dict]:
    """Parse a status.json and yield AW events for completed/running tasks."""
    project = _project_from_path(path)
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        print(f"[layer25] skip {path}: {e}", file=sys.stderr)
        return []

    tasks = data.get("tasks") or {}
    out: list[dict] = []
    for task_id, task in tasks.items():
        if not isinstance(task, dict):
            continue
        start = _parse_iso(task.get("launched_at"))
        end = _parse_iso(task.get("completed_at"))
        if not start:
            continue
        # If still running (no completed_at) — use file's mtime as a snapshot
        if not end:
            end = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        dur = (end - start).total_seconds()
        if dur < 1:
            continue
        out.append({
            "timestamp": iso(start),
            "duration": dur,
            "data": {
                "engine": task.get("engine") or "unknown",
                "project": project,
                "task_id": task_id,
                "status": task.get("status") or "?",
                "result": task.get("result") or "?",
                "pid": task.get("pid"),
                "source": "project_status",
            },
        })
    return out


# ── global pipeline reader ────────────────────────────────────────────────────
def _events_from_run_log(pdir: Path, project: str, engine: str, pipeline_id: str) -> list[dict]:
    """Per-task events from run.log JSONL (python oxi_tr engine, v5+).

    Pairs `headless_launched` / `headless_complete` records by task_id in order
    (a task may relaunch on retry). A launch without a matching complete
    (crash/kill) is bounded by the run.log mtime.
    """
    log_file = pdir / "run.log"
    if not log_file.exists():
        return []
    launches: dict[str, list[dict]] = {}
    completes: dict[str, list[dict]] = {}
    for line in log_file.read_text().splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        ev = rec.get("event")
        if ev == "headless_launched":
            launches.setdefault(rec.get("task_id") or "?", []).append(rec)
        elif ev == "headless_complete":
            completes.setdefault(rec.get("task_id") or "?", []).append(rec)
        elif ev == "worktree_created" and project == "unknown":
            # Old dirs without state.json: derive project from worktree path
            # (".../<project>/.task-runner/worktrees/<task>").
            guess = _project_from_path(Path(rec.get("path") or ""))
            if guess != "unknown":
                project = guess

    log_mtime = datetime.fromtimestamp(log_file.stat().st_mtime, tz=timezone.utc)
    out: list[dict] = []
    for task_id, launched in launches.items():
        done = completes.get(task_id, [])
        for i, rec in enumerate(launched):
            start = _parse_iso(rec.get("timestamp"))
            if not start:
                continue
            if i < len(done):
                dur = float(done[i].get("duration_seconds") or 0)
                exit_code = done[i].get("exit_code")
            else:
                # crashed / killed / still running — bound by log mtime
                dur = (log_mtime - start).total_seconds()
                exit_code = None
            if dur < 1:
                continue
            out.append({
                "timestamp": iso(start),
                "duration": dur,
                "data": {
                    "engine": rec.get("engine") or engine,
                    "project": project,
                    "task_id": task_id,
                    "status": "done" if exit_code == 0 else ("failed" if exit_code is not None else "interrupted"),
                    "result": str(exit_code) if exit_code is not None else "?",
                    "pid": rec.get("pid"),
                    "pipeline_id": pipeline_id,
                    "source": "run_log",
                },
            })
    return out


def _iter_pipeline_dirs() -> Iterator[Path]:
    """Yield pipeline dirs from both live `pipelines/` and `pipelines-archive/`.

    Since oxi-skills#1010 cleanup MOVES finished pipeline dirs into
    `pipelines-archive/` instead of deleting them, so history survives there.
    The archive also holds manually-created container dirs (e.g.
    `manual-2026-07-16-project-a/tr-*`) — walk one nesting level deep and treat any
    dir holding state.json or run.log as a pipeline dir.
    """
    live = GLOBAL_TR / "pipelines"
    if live.exists():
        for pdir in live.iterdir():
            if pdir.is_dir():
                yield pdir
    archive = GLOBAL_TR / "pipelines-archive"
    if not archive.exists():
        return
    for entry in archive.iterdir():
        if not entry.is_dir():
            continue
        if (entry / "state.json").exists() or (entry / "run.log").exists():
            yield entry
            continue
        for sub in entry.iterdir():
            if sub.is_dir() and ((sub / "state.json").exists() or (sub / "run.log").exists()):
                yield sub


def events_from_global_pipelines() -> list[dict]:
    out: list[dict] = []
    for pdir in _iter_pipeline_dirs():
        state: dict = {}
        state_file = pdir / "state.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
            except Exception:
                pass
        pipeline_id = state.get("pipeline_id") or pdir.name
        repo = state.get("repo") or "unknown"
        # bash engine (≤2026-06) wrote created_ts/updated_ts + options.engine;
        # python oxi_tr (v5+) writes started_at/finished_at + top-level engine.
        created = _parse_iso(state.get("created_ts") or state.get("started_at"))
        updated = _parse_iso(state.get("updated_ts") or state.get("finished_at"))
        engine = state.get("options", {}).get("engine") or state.get("engine") or "unknown"
        # project guess: basename of repo path or "owner/repo" slug
        proj = Path(repo).name if repo and repo != "unknown" else "unknown"

        # Preferred: per-task granularity from run.log (python engine).
        task_events = _events_from_run_log(pdir, proj, engine, pipeline_id)
        if task_events:
            out.extend(task_events)
            continue

        # Fallback: one coarse event per pipeline (bash-era state.json without
        # run.log task records; per-task granularity came from status.json then).
        if not created:
            continue
        end = updated or datetime.fromtimestamp(state_file.stat().st_mtime, tz=timezone.utc)
        dur = (end - created).total_seconds()
        # A hung pipeline (no finished_at, mtime hours later) inflates runtime:
        # cap by its own deadline — the orchestrator wouldn't run longer anyway.
        if not updated and state.get("deadline_minutes"):
            dur = min(dur, float(state["deadline_minutes"]) * 60)
        if dur < 1:
            continue
        out.append({
            "timestamp": iso(created),
            "duration": dur,
            "data": {
                "engine": engine,
                "project": proj,
                "task_id": "<pipeline>",
                "status": state.get("status") or "?",
                "result": state.get("mode") or "?",
                "pipeline_id": pipeline_id,
                "repo": repo,
                "source": "global_pipeline",
            },
        })
    return out


# ── session index reader ──────────────────────────────────────────────────────
def _covered_by_task_runner(cwd: str, a: float, b: float, tr_events: list[dict]) -> bool:
    """True when a task-runner event of the same project overlaps this span."""
    for ev in tr_events:
        t = _parse_iso(ev["timestamp"])
        if not t:
            continue
        ea = t.timestamp()
        eb = ea + ev["duration"]
        if eb < a or ea > b:
            continue
        if f"/{ev['data'].get('project')}/.task-runner/" in cwd:
            return True
    return False


def events_from_session_index(start: datetime, end: datetime, tr_events: list[dict]) -> list[dict]:
    """Autonomous sessions from the local session index, one event per activity span.

    A worktree session already represented by an overlapping task-runner event of
    the same project is skipped, so it is not counted twice.
    """
    if not INDEX_DB_PATH.exists():
        print(f"[layer25] index DB not found: {INDEX_DB_PATH} — run jsonl_indexer.py", file=sys.stderr)
        return []
    con = sqlite3.connect(INDEX_DB_PATH)
    con.execute("PRAGMA query_only = 1")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "jsonl_spans" not in tables:
        con.close()
        print("[layer25] index schema is stale — run jsonl_indexer.py", file=sys.stderr)
        return []
    rows = con.execute(
        "SELECT s.path, s.start_ts, s.end_ts, f.engine, f.cwd_hint, f.session_source "
        "FROM jsonl_spans s JOIN jsonl_files f ON f.path = s.path "
        "WHERE f.autonomous = 1 AND s.end_ts >= ? AND s.start_ts < ?",
        (start.timestamp(), end.timestamp()),
    ).fetchall()
    con.close()
    out: list[dict] = []
    for path, a, b, engine, cwd, source in rows:
        cwd = cwd or ""
        norm = cwd.replace("\\", "/")
        if "/.task-runner/worktrees/" in norm and _covered_by_task_runner(norm, a, b, tr_events):
            continue
        # A span of a single record still means the agent did something.
        dur = max(b - a, 60.0)
        out.append({
            "timestamp": iso(datetime.fromtimestamp(a, tz=timezone.utc)),
            "duration": dur,
            "data": {
                "engine": engine,
                "project": real_project_from_cwd(cwd) if cwd else engine,
                "task_id": Path(path).stem,
                "status": "?",
                "result": source or "?",
                "source": "session_index",
            },
        })
    return out


# ── filtering by day ──────────────────────────────────────────────────────────
def _within(ev: dict, start: datetime, end: datetime) -> bool:
    t = _parse_iso(ev["timestamp"])
    if not t:
        return False
    ev_end = t + timedelta(seconds=ev["duration"])
    return not (ev_end < start or t >= end)


def collect_for_day(day: datetime) -> list[dict]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    events: list[dict] = []
    for path in iter_project_status_files():
        events.extend(events_from_status_json(path))
    events.extend(events_from_global_pipelines())
    events.extend(events_from_session_index(start, end, events))

    # Filter to events that overlap with target day. Also clip duration so we don't
    # count a 17-hour-long pipeline against just today.
    filtered: list[dict] = []
    for ev in events:
        t = _parse_iso(ev["timestamp"])
        if not t:
            continue
        ev_end = t + timedelta(seconds=ev["duration"])
        if ev_end < start or t >= end:
            continue
        clip_start = max(t, start)
        clip_end = min(ev_end, end)
        clipped = dict(ev)
        clipped["timestamp"] = iso(clip_start)
        clipped["duration"] = (clip_end - clip_start).total_seconds()
        if clipped["duration"] < 1:
            continue
        filtered.append(clipped)

    return filtered


# ── CLI ──────────────────────────────────────────────────────────────────────
def run_for_day(day: datetime, dry_run: bool) -> None:
    events = collect_for_day(day)
    print(f"[layer25] {len(events)} autonomous task-events for {day.date().isoformat()}", file=sys.stderr)
    by: dict[tuple[str, str], float] = {}
    for ev in events:
        key = (ev["data"]["project"], ev["data"]["engine"])
        by[key] = by.get(key, 0) + ev["duration"]
    for (proj, eng), secs in sorted(by.items(), key=lambda x: -x[1])[:25]:
        print(f"  {proj:<30s} engine={eng:<10s} {secs/60:6.1f}m", file=sys.stderr)

    if dry_run:
        print("[layer25] DRY RUN — not writing", file=sys.stderr)
        return
    ensure_bucket(OUTPUT_BUCKET, OUTPUT_EVENT_TYPE)
    replace_events_for_day(OUTPUT_BUCKET, day, events)
    print(f"[layer25] wrote {len(events)} events to {OUTPUT_BUCKET}", file=sys.stderr)


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
