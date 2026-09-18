"""Shared helpers for talking to ActivityWatch REST API.

We deliberately use plain `urllib` to avoid adding a hard dependency on `aw-client`
package — `urllib` is in stdlib.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from tracker_config import CONFIG as TRACKER_CONFIG

AW_HOST = TRACKER_CONFIG.aw_host

# Repo-local state directory — cache/index/etc. lives here so it's easy to find
# and gets covered by the repo's .gitignore.
REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = TRACKER_CONFIG.state_dir
INDEX_DB_PATH = TRACKER_CONFIG.index_db
TASK_RUNNER_CACHE_PATH = TRACKER_CONFIG.task_runner_cache
# Use full hostname to match aw-server-rust convention (it keeps `.local` suffix).
HOSTNAME = socket.gethostname()


# Default timeout bumped 5s→20s: heavy buckets (e.g. aw-watcher-tmux-pane with
# 5s heartbeats × many panes/day, fetched at limit=100000) routinely exceeded 5s
# under load → TimeoutError propagated and crashed the whole tracker run (exit 1).
# See aw-tracker / aw-tracker-fast intermittent exit-1.
_DEFAULT_TIMEOUT = 20.0
_RETRIES = 3


def _request(method: str, path: str, body: Any = None,
             timeout: float = _DEFAULT_TIMEOUT, retries: int = _RETRIES) -> Any:
    # Restrict retries to idempotent requests to avoid duplicates on timeout/dropped connections.
    # GET, DELETE, and PUT are standard idempotent HTTP methods.
    # POST is generally not idempotent, but bucket creation is idempotent.
    is_idempotent = method in ("GET", "PUT", "DELETE") or (method == "POST" and "/events" not in path)
    if not is_idempotent:
        retries = 1

    url = f"{AW_HOST}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError:
            # A real HTTP status (404/304/…) — callers handle these; never retry.
            raise
        except (socket.timeout, TimeoutError, urllib.error.URLError) as exc:
            # Transient: server busy/slow/briefly unreachable. Retry with backoff.
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(0.5 * (2 ** attempt))  # 0.5s, 1.0s
    assert last_exc is not None
    raise last_exc


def server_info() -> dict | None:
    try:
        return _request("GET", "/api/0/info")
    except urllib.error.URLError:
        return None


def list_buckets() -> dict[str, Any]:
    return _request("GET", "/api/0/buckets/") or {}


def ensure_bucket(bucket_id: str, event_type: str, client: str | None = None) -> None:
    """Idempotent bucket creation. AW returns 304 if exists."""
    body = {
        "client": client or TRACKER_CONFIG.client_name,
        "type": event_type,
        "hostname": HOSTNAME,
    }
    try:
        _request("POST", f"/api/0/buckets/{bucket_id}", body=body)
    except urllib.error.HTTPError as exc:
        # 304 = already exists, that's fine
        if exc.code not in (200, 201, 304):
            raise


def get_events(bucket_id: str, start: datetime, end: datetime, limit: int = 100000) -> list[dict]:
    # Use iso() helper so `+00:00` becomes `Z`; URL parsers reject the `+` literal.
    params = f"start={iso(start)}&end={iso(end)}&limit={limit}"
    try:
        return _request("GET", f"/api/0/buckets/{bucket_id}/events?{params}") or []
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise


def insert_events(bucket_id: str, events: Iterable[dict]) -> None:
    """events: list of {timestamp, duration, data} dicts."""
    payload = list(events)
    if not payload:
        return
    try:
        # POST /events is not idempotent, so explicitly opt out of retries.
        _request("POST", f"/api/0/buckets/{bucket_id}/events", body=payload, retries=1)
    except urllib.error.URLError as exc:
        # AW server down or unreachable — log and continue rather than crash the tracker.
        import sys
        print(f"[lib_aw] insert_events({bucket_id}) failed: {exc}", file=sys.stderr)


def replace_events_for_day(bucket_id: str, day: datetime, events: list[dict]) -> None:
    """Delete existing events in [day, day+1) and insert fresh.

    Used by daily aggregation to make re-runs idempotent.
    """
    from datetime import timedelta as _td
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = start + _td(days=1)  # exclusive — covers full 24h incl. 23:59:59.x
    existing = get_events(bucket_id, start, end)
    for ev in existing:
        try:
            _request("DELETE", f"/api/0/buckets/{bucket_id}/events/{ev['id']}")
        except urllib.error.HTTPError:
            pass
    insert_events(bucket_id, events)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def bucket_name(suffix: str) -> str:
    """Convention: configurable prefix, hostname suffix mirrors stock AW watchers."""
    return f"{TRACKER_CONFIG.bucket_prefix}-{suffix}_{HOSTNAME}"


# ── shared cwd → project resolver ──────────────────────────────────────────────
_WORKTREE_SEPARATORS = ("/.task-runner/worktrees/", "/.git/worktrees/")


def real_project_from_cwd(cwd: str | None) -> str:
    """Resolve project name from a working directory.

    Collapses task-runner / git worktrees to their **parent** project, so
    autonomous offload sessions are attributed to the project that spawned
    them, not to the worktree's anonymous slug.

    Examples:
        /Users/example/projects/project-a/.task-runner/worktrees/offload-20260430-1604
        -> "project-a"

        /Users/example/src/project-core/.task-runner/worktrees/fix-reviews-X
        -> "project-core"

        /Users/example/src/toolkit
        -> "toolkit"
    """
    if not cwd:
        return "unknown"
    from pathlib import Path as _P
    for sep in _WORKTREE_SEPARATORS:
        if sep in cwd:
            return _P(cwd.split(sep, 1)[0]).name
    return _P(cwd).name
