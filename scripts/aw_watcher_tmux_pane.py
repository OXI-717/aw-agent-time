#!/usr/bin/env python3
"""aw_watcher_tmux_pane.py — heartbeat AW watcher for the focused tmux pane.

Independent of tmux config: every POLL_SEC seconds it asks tmux for the pane belonging
to its best (most recently active) client and sends one heartbeat event to bucket
`aw-watcher-tmux-focus_<host>`.

This lets us see sub-project granularity (which subdir inside a long-running tmux session
the user is actually looking at) WITHOUT touching ~/.tmux.conf.

Designed to be cheap: ~10ms per poll. Restartable. No tmux config needed.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tracker_config import CONFIG

POLL_SEC = 5            # how often to check
PULSE_SEC = POLL_SEC + 5  # merge heartbeats within this window
AW_HOST = CONFIG.aw_host
HOSTNAME = socket.gethostname()
BUCKET = f"aw-watcher-tmux-focus_{HOSTNAME}"
EVENT_TYPE = "tmux.focusedpane"


_WORKTREE_SEPARATORS = ("/.task-runner/worktrees/", "/.git/worktrees/")


def _real_project_from_cwd(cwd: str) -> str:
    """Collapse task-runner / git worktrees to their parent project name."""
    if not cwd:
        return "?"
    for sep in _WORKTREE_SEPARATORS:
        if sep in cwd:
            return Path(cwd.split(sep, 1)[0]).name
    return Path(cwd).name or "?"


def _request(method: str, path: str, body=None, timeout: float = 3.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{AW_HOST}{path}", data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def ensure_bucket() -> None:
    """Create the bucket; retry on transient errors so a flaky AW-server during
    boot doesn't take the daemon down (launchd KeepAlive will respawn anyway,
    but we don't want noisy exit codes in logs)."""
    body = {"client": CONFIG.client_name, "type": EVENT_TYPE, "hostname": HOSTNAME}
    for attempt in range(10):
        try:
            _request("POST", f"/api/0/buckets/{BUCKET}", body=body)
            return
        except urllib.error.HTTPError as exc:
            if exc.code in (200, 201, 304):
                return
            print(f"[tmux-pane] ensure_bucket HTTP {exc.code}: {exc}", file=sys.stderr)
            return
        except (urllib.error.URLError, TimeoutError) as exc:
            wait = min(2 ** attempt, 30)
            print(f"[tmux-pane] ensure_bucket retry in {wait}s: {exc}", file=sys.stderr)
            time.sleep(wait)
    print("[tmux-pane] ensure_bucket: AW server unreachable after 10 attempts", file=sys.stderr)


def heartbeat(data: dict) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    body = {"timestamp": now, "duration": 0, "data": data}
    try:
        _request("POST", f"/api/0/buckets/{BUCKET}/heartbeat?pulsetime={PULSE_SEC}", body=body)
    except Exception as e:
        print(f"[tmux-pane] heartbeat failed: {e}", file=sys.stderr)


# ── tmux interaction ─────────────────────────────────────────────────────────
def _tmux(args: list[str]) -> str:
    # A manually started watcher may itself run inside tmux. Remove caller-client
    # context so display-message uses tmux's global best-client selection, just as
    # it does when this watcher is launched by launchd.
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    try:
        out = subprocess.check_output(
            ["tmux"] + args,
            stderr=subprocess.DEVNULL,
            timeout=3,
            env=env,
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def get_focused_pane() -> dict | None:
    """Return the active pane of tmux's most recently active client."""
    fmt = (
        "#{client_tty}|"
        "#{session_name}|"
        "#{client_activity}|"
        "#{window_index}|"
        "#{window_name}|"
        "#{pane_index}|"
        "#{pane_pid}|"
        "#{pane_current_command}|"
        "#{pane_current_path}|"
        "#{pane_title}"
    )
    raw = _tmux(["display-message", "-p", "-F", fmt])
    if not raw:
        return None
    parts = raw.split("|", 9)
    if len(parts) != 10:
        return None
    client_tty, session, activity_raw, win_idx, win_name, pane_idx, pane_pid, cmd, cwd, title = parts
    try:
        client_activity = int(activity_raw)
    except ValueError:
        return None

    # Layer 1: pane's own cwd (with worktree-parent collapse)
    shell_project = _real_project_from_cwd(cwd)
    # Layer 2: real AI-tool working project (claude/codex JSONL) — overrides shell_project if found
    ai_proj, ai_engine, ai_cwd = _detect_ai_project(int(pane_pid) if pane_pid.isdigit() else 0)

    return {
        "session": session,
        "window": f"{win_idx}:{win_name}",
        "pane": pane_idx,
        "command": cmd,
        "shell_cwd": cwd,
        "shell_project": shell_project,
        # `project` reflects whatever's most informative for categorization:
        #   AI tool's project (claude/codex) if running, else shell cwd basename.
        "project": ai_proj or shell_project,
        "ai_project": ai_proj,
        "ai_engine": ai_engine,
        "ai_cwd": ai_cwd,
        "title": title,
        "client_tty": client_tty,
        "client_activity": client_activity,
    }


# ── AI subprocess detection ──────────────────────────────────────────────────
_AI_CACHE: dict[int, tuple[float, tuple[str | None, str | None, str | None]]] = {}
_AI_CACHE_TTL = 15.0  # seconds — pids and JSONL paths rarely change for long sessions


def _detect_ai_project(pane_pid: int) -> tuple[str | None, str | None, str | None]:
    """If a `claude` or `codex` runs inside the pane process tree, return its
    (project_name, engine, cwd-or-jsonl-path).

    Result cached per-pid for _AI_CACHE_TTL to keep poll cheap.
    """
    if pane_pid <= 0:
        return (None, None, None)
    now = time.time()
    cached = _AI_CACHE.get(pane_pid)
    if cached and now - cached[0] < _AI_CACHE_TTL:
        return cached[1]
    # Bound cache size — periodically evict expired entries so dead pids don't accumulate.
    if len(_AI_CACHE) > 500:
        cutoff = now - _AI_CACHE_TTL * 10
        for k in [k for k, v in _AI_CACHE.items() if v[0] < cutoff]:
            _AI_CACHE.pop(k, None)
    result = _detect_ai_project_inner(pane_pid)
    _AI_CACHE[pane_pid] = (now, result)
    return result


def _children_pids(parent_pid: int) -> list[int]:
    """Recursive descendants — ps is fastest, single call."""
    try:
        raw = subprocess.check_output(["ps", "-A", "-o", "pid=,ppid=,comm="],
                                      stderr=subprocess.DEVNULL, timeout=2).decode()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    tree: dict[int, list[tuple[int, str]]] = {}
    for line in raw.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        tree.setdefault(ppid, []).append((pid, parts[2]))
    out: list[tuple[int, str]] = []
    stack = [parent_pid]
    while stack:
        cur = stack.pop()
        for pid, comm in tree.get(cur, []):
            out.append((pid, comm))
            stack.append(pid)
    return [(pid, comm) for pid, comm in out]  # type: ignore[misc]


def _detect_ai_project_inner(pane_pid: int) -> tuple[str | None, str | None, str | None]:
    # Walk process tree, look for a process whose basename contains claude or codex
    descendants = _children_pids(pane_pid)
    for pid, comm in descendants:
        comm_l = comm.lower()
        engine = None
        if "claude" in comm_l:
            engine = "claude"
        elif "codex" in comm_l and "codex_helper" not in comm_l:
            engine = "codex"
        else:
            continue
        # We found a candidate. Use lsof to find which JSONL it has open.
        try:
            lsof = subprocess.check_output(
                ["lsof", "-p", str(pid), "-F", "n"],
                stderr=subprocess.DEVNULL, timeout=2,
            ).decode()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            continue
        for line in lsof.splitlines():
            if not line.startswith("n"):
                continue
            path = line[1:]
            # Match Claude Code JSONL
            if engine == "claude" and Path(path).is_relative_to(CONFIG.claude_projects_dir) and path.endswith(".jsonl"):
                encoded = Path(path).relative_to(CONFIG.claude_projects_dir).parts[0]
                decoded_path = _decode_claude_project_path_full(encoded)
                project = _real_project_from_cwd(decoded_path)
                return (project, "claude", path)
            # Codex session JSONL (rollout-*.jsonl in .codex/sessions/YYYY/MM/DD/)
            if engine == "codex" and Path(path).is_relative_to(CONFIG.codex_sessions_dir) and path.endswith(".jsonl"):
                # Codex JSONL doesn't encode cwd in name — fall back to lsof cwd of the proc
                cwd = _proc_cwd(pid)
                project = _real_project_from_cwd(cwd) if cwd else "codex"
                return (project, "codex", cwd or path)
        # No JSONL found but engine is running — read cwd of the process
        cwd = _proc_cwd(pid)
        if cwd:
            return (_real_project_from_cwd(cwd), engine, cwd)
    return (None, None, None)


def _decode_claude_project_path_full(encoded: str) -> str:
    """`-Users-example-src-project-a` -> `/Users/example/src/project-a`.

    Returns full filesystem path. Caller decides what to do with it (basename,
    worktree-parent collapse, etc).

    Greedy filesystem matching handles paths with dashes in their names.
    """
    parts = encoded.lstrip("-").split("-")
    if not parts:
        return "/" + encoded
    current = "/"
    i = 0
    while i < len(parts):
        matched_advance = 0
        for grp in (4, 3, 2, 1):
            if i + grp > len(parts):
                continue
            cand_name = "-".join(parts[i:i + grp])
            cand_path = f"{current.rstrip('/')}/{cand_name}"
            if Path(cand_path).exists():
                current = cand_path
                matched_advance = grp
                break
        if matched_advance == 0:
            current = f"{current.rstrip('/')}/{parts[i]}"
            i += 1
        else:
            i += matched_advance
    return current


def _proc_cwd(pid: int) -> str | None:
    """`lsof -a -d cwd -p PID -F n` → cwd path (macOS)."""
    try:
        out = subprocess.check_output(
            ["lsof", "-a", "-d", "cwd", "-p", str(pid), "-F", "n"],
            stderr=subprocess.DEVNULL, timeout=2,
        ).decode()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def loop() -> None:
    ensure_bucket()
    last_signature = ""
    while True:
        info = get_focused_pane()
        if info:
            # Always heartbeat — AW merges identical-data heartbeats within pulsetime.
            heartbeat(info)
            sig = f"{info['session']}/{info['pane']}/{info['project']}"
            if last_signature != sig:
                print(f"[tmux-focus] {info['client_tty']}: {sig}", flush=True)
                last_signature = sig
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        loop()
    except KeyboardInterrupt:
        pass
