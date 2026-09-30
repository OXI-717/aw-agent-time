"""bb_threads.py — map a bb window title to the project of its thread.

bb shows the focused thread title as its window title. Each thread belongs to an
environment whose `path` is a checkout, so the path goes through the same
`real_project_from_cwd` classification as Claude/Codex session cwds.

The map is cached in the state dir for CACHE_TTL_SEC: the tracker runs every few
minutes and `bb thread list` over ~1000 threads takes seconds.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib_aw import STATE_DIR, real_project_from_cwd

CACHE_FILE = STATE_DIR / "bb_title_projects.json"
CACHE_TTL_SEC = 600
# Per bb call; the whole rebuild is 5 calls and must fit the tracker cadence.
BB_TIMEOUT_SEC = 30
# Bump when the cached entry shape changes; older caches are rebuilt, not trusted.
CACHE_VERSION = 2
BB_APP_CLI = "/Applications/bb.app/Contents/Resources/app.asar.unpacked/node_modules/bb-app/host-daemon/dist/bb"
# Titles that are bb chrome, not a thread.
GENERIC_TITLES = {"bb", "проект", "project", "new thread", "новый тред", ""}


def _bb_cli() -> str | None:
    for candidate in (os.environ.get("BB_CLI"), shutil.which("bb"), BB_APP_CLI):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _bb_json(cli: str, *args: str) -> list[dict]:
    out = subprocess.run([cli, *args, "--json"], capture_output=True, text=True, timeout=BB_TIMEOUT_SEC)
    if out.returncode != 0:
        raise RuntimeError(f"bb {' '.join(args)} failed: {out.stderr.strip()[:200]}")
    data = json.loads(out.stdout)
    return data if isinstance(data, list) else data.get("items") or data.get("environments") or []


def _build_map() -> dict[str, list[list]]:
    """title(lower) → [[project, createdAt_ms, updatedAt_ms], ...] across live, archived and hidden threads."""
    cli = _bb_cli()
    if not cli:
        raise RuntimeError("bb CLI not found")
    # Finished worktree threads keep pointing at destroyed environments.
    envs = {e["id"]: e.get("path") or ""
            for status in ([], ["--status", "destroyed"])
            for e in _bb_json(cli, "environment", "list", "--limit", "100000", *status)}
    threads: dict[str, dict] = {}
    for flag in ([], ["--archived"], ["--include-hidden"]):
        for t in _bb_json(cli, "thread", "list", *flag):
            threads[t["id"]] = t
    out: dict[str, list[list]] = {}
    for t in threads.values():
        title = (t.get("title") or "").strip().lower()
        path = envs.get(t.get("environmentId") or "", "")
        if title in GENERIC_TITLES or not path:
            continue
        # `<repo>/.worktrees/<branch…>` belongs to the repo, not to a project named after the branch.
        project = real_project_from_cwd(path.split("/.worktrees/", 1)[0])
        if project and project not in ("unknown", "?"):
            out.setdefault(title, []).append([project, t.get("createdAt") or 0, t.get("updatedAt") or 0])
    return out


_MAP: dict[str, list[list]] | None = None


def _read_cache() -> tuple[dict[str, list[list]] | None, float]:
    try:
        doc = json.loads(CACHE_FILE.read_text())
        if doc.get("version") != CACHE_VERSION:
            return None, 0.0
        return doc["map"], float(doc.get("built_at", 0))
    except (OSError, ValueError, KeyError):
        return None, 0.0


def title_map() -> dict[str, list[list]]:
    global _MAP
    if _MAP is not None:
        return _MAP
    cached, built_at = _read_cache()
    if cached is not None and time.time() - built_at < CACHE_TTL_SEC:
        _MAP = cached
        return _MAP
    try:
        fresh = _build_map()
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        # A transient bb failure must not wipe a day's attribution: keep the last good map.
        print(f"[bb] WARN: cannot read bb threads ({exc}); "
              + ("using stale map" if cached is not None else "bb time stays unattributed"), file=sys.stderr)
        _MAP = cached or {}
        return _MAP
    # Keep titles that no longer exist (renamed or deleted threads): a day's window
    # events are re-attributed on every run and during backfills, and they still
    # carry the old title. Current titles override their old entries.
    _MAP = {**(cached or {}), **fresh}
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({"version": CACHE_VERSION, "built_at": time.time(), "map": _MAP}, ensure_ascii=False))
    return _MAP


def project_for_title(title: str, at: datetime | None = None) -> tuple[str, bool]:
    """Return (project, ambiguous) for a bb window title observed at `at`.

    Empty project when the title is not a known thread. With the same title in
    several projects, prefer threads alive at `at` (created before it, updated
    after it), then the latest one created before it, then the latest overall.
    """
    candidates = [c for c in title_map().get(title.strip().lower()) or [] if len(c) == 3]
    projects = {c[0] for c in candidates}
    if not projects:
        return "", False
    if len(projects) == 1:
        return projects.pop(), False
    ts = at.timestamp() * 1000 if at else None
    if ts is not None:
        alive = [c for c in candidates if c[1] <= ts <= c[2]]
        if len({c[0] for c in alive}) == 1:
            return alive[0][0], False
        earlier = [c for c in (alive or candidates) if c[1] <= ts]
        if earlier:
            return max(earlier, key=lambda c: c[1])[0], True
    return max(candidates, key=lambda c: c[2])[0], True
