#!/usr/bin/env python3
"""jsonl_indexer.py — incremental SQLite index over Claude/Codex JSONL turns.

DB: <repo>/state/jsonl_index.sqlite

Idempotent + fast: only re-reads JSONL files whose mtime or size changed since
the previous run. Stores `(ts_unix, type, engine, project, cwd, session_id,
git_branch)` per user/assistant turn — Layer 2 (engagement) reads from here
instead of re-parsing 3.7GB of files every tick.

CLI:
  python3 jsonl_indexer.py            # incremental update (default)
  python3 jsonl_indexer.py --full     # forget everything, re-index from scratch
  python3 jsonl_indexer.py --stats    # show row counts + freshness
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Local lib_aw for the worktree-collapse helper
sys.path.insert(0, str(Path(__file__).parent))
from lib_aw import INDEX_DB_PATH, TRACKER_CONFIG, real_project_from_cwd

HOME = Path.home()
CLAUDE_DIR = TRACKER_CONFIG.claude_projects_dir
CODEX_DIR = TRACKER_CONFIG.codex_sessions_dir
DB_PATH = INDEX_DB_PATH
# 4: synthetic Codex context records (<environment_context>, <recommended_plugins>,
#    the AGENTS.md rule bundle) no longer count as human turns. Rows indexed under
#    3 still carry them, so the bump forces a reparse of already-indexed files.
PARSER_VERSION = 4


def _is_claude_human_user(record: dict) -> bool:
    if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and bool(str(block.get("text") or "").strip())
            for block in content
        ) and not any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
    return False


# Codex injects environment/plugin context into the conversation using the same
# response_item/message/role=user envelope as a real prompt; these wrapper tags mark
# synthetic context rather than something the human typed.
_CODEX_SYNTHETIC_CONTEXT_TAGS = (
    "<environment_context>",
    "<recommended_plugins>",
    # Codex injects the project rule bundle through the same user envelope.
    "# AGENTS.md instructions for ",
)


def _is_codex_human_user(record: dict) -> bool:
    payload = record.get("payload") or {}
    if not (
        record.get("type") == "response_item"
        and payload.get("type") == "message"
        and payload.get("role") == "user"
    ):
        return False
    content = payload.get("content")
    if isinstance(content, list):
        for block in content:
            text = (block.get("text") or "") if isinstance(block, dict) else ""
            if text.strip().startswith(_CODEX_SYNTHETIC_CONTEXT_TAGS):
                return False
    return True


def _is_autonomous_session(
    engine: str,
    cwd: str,
    *,
    originator: str,
    source: object,
    thread_source: str = "",
) -> bool:
    normalized = cwd.replace("\\", "/")
    if "/.task-runner/worktrees/" in normalized:
        return True
    return engine == "codex" and (
        originator == "codex_exec"
        or source == "exec"
        or thread_source == "subagent"
        or isinstance(source, dict)
    )


def _codex_source_label(originator: str, source: object, thread_source: str) -> str:
    if thread_source == "subagent" or isinstance(source, dict):
        return "subagent"
    parts = [part for part in (originator, source) if isinstance(part, str) and part]
    return ":".join(parts)


def _classify_codex_metadata(
    cwd: object,
    originator: object,
    source: object,
    thread_source: object,
) -> tuple[str, int]:
    """Return a source label and fail-closed autonomous flag for session metadata."""
    cwd_text = cwd if isinstance(cwd, str) else ""
    originator_text = originator if isinstance(originator, str) else ""
    thread_source_text = thread_source if isinstance(thread_source, str) else ""
    has_source_identity = (
        bool(originator_text)
        or (isinstance(source, str) and bool(source))
        or isinstance(source, dict)
        or bool(thread_source_text)
    )
    in_task_runner = "/.task-runner/worktrees/" in cwd_text.replace("\\", "/")
    if not has_source_identity and not in_task_runner:
        return "unverified", 1
    label = _codex_source_label(originator_text, source, thread_source_text)
    return label or "task-runner", int(_is_autonomous_session(
        "codex",
        cwd_text,
        originator=originator_text,
        source=source,
        thread_source=thread_source_text,
    ))

SCHEMA = """
CREATE TABLE IF NOT EXISTS jsonl_files (
  path TEXT PRIMARY KEY,
  engine TEXT NOT NULL,             -- 'claude' or 'codex'
  mtime REAL NOT NULL,
  size INTEGER NOT NULL,
  indexed_offset INTEGER NOT NULL,  -- byte offset up to which we've parsed
  indexed_at REAL NOT NULL,
  turn_count INTEGER NOT NULL DEFAULT 0,
  cwd_hint TEXT,                    -- cached cwd from session_meta (codex)
  session_source TEXT,
  autonomous INTEGER NOT NULL DEFAULT 0,
  parser_version INTEGER NOT NULL DEFAULT 3
);
-- Backfill column if missing (idempotent for existing DBs).


CREATE TABLE IF NOT EXISTS jsonl_turns (
  path TEXT NOT NULL,
  ts_unix REAL NOT NULL,
  kind TEXT NOT NULL,               -- human interaction kind ('user')
  engine TEXT NOT NULL,
  project TEXT NOT NULL,
  cwd TEXT,
  session_id TEXT,
  git_branch TEXT,
  session_source TEXT,
  autonomous INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_turns_ts        ON jsonl_turns(ts_unix);
CREATE INDEX IF NOT EXISTS ix_turns_project   ON jsonl_turns(project);
CREATE INDEX IF NOT EXISTS ix_turns_path      ON jsonl_turns(path);
CREATE INDEX IF NOT EXISTS ix_turns_eng_proj  ON jsonl_turns(engine, project);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    con.executescript(SCHEMA)
    # Existing indexes predate interaction/source classification. A parser_version
    # default of 1 deliberately marks their file rows for one-time reindexing.
    file_cols = {r[1] for r in con.execute("PRAGMA table_info(jsonl_files)").fetchall()}
    migrations = {
        "cwd_hint": "TEXT",
        "session_source": "TEXT",
        "autonomous": "INTEGER NOT NULL DEFAULT 0",
        "parser_version": "INTEGER NOT NULL DEFAULT 1",
    }
    for name, declaration in migrations.items():
        if name not in file_cols:
            con.execute(f"ALTER TABLE jsonl_files ADD COLUMN {name} {declaration}")
    turn_cols = {r[1] for r in con.execute("PRAGMA table_info(jsonl_turns)").fetchall()}
    for name, declaration in {
        "session_source": "TEXT",
        "autonomous": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if name not in turn_cols:
            con.execute(f"ALTER TABLE jsonl_turns ADD COLUMN {name} {declaration}")
    return con


def _parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ── per-engine parsers ────────────────────────────────────────────────────────
def _parse_claude_lines(path: Path, start_offset: int):
    """Yield (byte_offset, line) for new content since `start_offset`."""
    with path.open("rb") as f:
        f.seek(start_offset)
        while True:
            line = f.readline()
            if not line:
                break
            yield f.tell(), line.decode("utf-8", errors="replace")


def _index_claude_file(con: sqlite3.Connection, path: Path) -> int:
    """Returns number of new turns inserted."""
    st = path.stat()
    row = con.execute(
        "SELECT mtime, size, indexed_offset, parser_version FROM jsonl_files WHERE path=?",
        (str(path),),
    ).fetchone()
    start_offset = 0
    was_reset = False
    if row:
        old_mtime, old_size, indexed_offset, parser_version = row
        # File truncated/replaced — re-index from scratch
        if parser_version < PARSER_VERSION or st.st_size < indexed_offset or st.st_mtime < old_mtime - 1:
            con.execute("DELETE FROM jsonl_turns WHERE path=?", (str(path),))
            start_offset = 0
            was_reset = True
        else:
            if abs(st.st_mtime - old_mtime) < 1 and st.st_size == old_size:
                return 0   # unchanged
            start_offset = indexed_offset

    inserted = 0
    new_offset = start_offset
    rows_to_insert: list[tuple] = []
    for offset, line in _parse_claude_lines(path, start_offset):
        new_offset = offset
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not _is_claude_human_user(d):
            continue
        ts = _parse_iso(d.get("timestamp"))
        if not ts:
            continue
        cwd = d.get("cwd") or ""
        proj = real_project_from_cwd(cwd) if cwd else "unknown"
        autonomous = int(_is_autonomous_session(
            "claude", cwd, originator="", source="claude-code"
        ))
        rows_to_insert.append((
            str(path),
            ts,
            "user",
            "claude",
            proj,
            cwd,
            d.get("sessionId"),
            d.get("gitBranch"),
            "claude-code",
            autonomous,
        ))
    if rows_to_insert:
        con.executemany(
            "INSERT INTO jsonl_turns(path, ts_unix, kind, engine, project, cwd, session_id, git_branch, session_source, autonomous) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows_to_insert,
        )
        inserted = len(rows_to_insert)

    # After reset turn_count starts from `inserted`; otherwise it accumulates.
    new_count_clause = "?" if was_reset else "jsonl_files.turn_count + ?"
    con.execute(
        f"INSERT INTO jsonl_files(path, engine, mtime, size, indexed_offset, indexed_at, turn_count, session_source, autonomous, parser_version) "
        f"VALUES (?,?,?,?,?,?,?,?,?,?) "
        f"ON CONFLICT(path) DO UPDATE SET "
        f"  mtime=excluded.mtime, size=excluded.size, indexed_offset=excluded.indexed_offset, "
        f"  indexed_at=excluded.indexed_at, turn_count={new_count_clause}, "
        f"  session_source=excluded.session_source, autonomous=excluded.autonomous, "
        f"  parser_version=excluded.parser_version",
        (
            str(path), "claude", st.st_mtime, st.st_size, new_offset, time.time(),
            inserted, "claude-code", 0, PARSER_VERSION, inserted,
        ),
    )
    return inserted


def _index_codex_file(con: sqlite3.Connection, path: Path) -> int:
    st = path.stat()
    row = con.execute(
        "SELECT mtime, size, indexed_offset, cwd_hint, session_source, autonomous, parser_version "
        "FROM jsonl_files WHERE path=?",
        (str(path),),
    ).fetchone()
    start_offset = 0
    was_reset = False
    cwd_hint = ""
    originator = ""
    source = ""
    thread_source = ""
    session_source = "unverified"
    autonomous = 1
    if row:
        old_mtime, old_size, indexed_offset, cached_hint, cached_source, cached_auto, parser_version = row
        if parser_version < PARSER_VERSION or st.st_size < indexed_offset or st.st_mtime < old_mtime - 1:
            con.execute("DELETE FROM jsonl_turns WHERE path=?", (str(path),))
            start_offset = 0
            was_reset = True
            cwd_hint = ""
        else:
            if abs(st.st_mtime - old_mtime) < 1 and st.st_size == old_size:
                return 0
            start_offset = indexed_offset
            cwd_hint = cached_hint or ""  # ← key fix: use cached cwd from previous index
            session_source = cached_source or "unverified"
            autonomous = int(cached_auto) if cached_source else 1

    inserted = 0
    new_offset = start_offset
    # If we have no cached cwd_hint but file was previously indexed, peek at line 0
    # to grab session_meta.cwd that we missed on incremental update before this fix.
    if start_offset > 0 and not cwd_hint:
        try:
            with path.open("rb") as _f:
                first = _f.readline()
            d = json.loads(first.decode("utf-8", errors="replace"))
            if d.get("type") == "session_meta":
                pl = d.get("payload") or {}
                if isinstance(pl, dict):
                    cwd_hint = pl.get("cwd") or ""
                    originator = pl.get("originator") or ""
                    source = pl.get("source") or ""
                    thread_source = pl.get("thread_source") or ""
                    session_source, autonomous = _classify_codex_metadata(
                        cwd_hint, originator, source, thread_source
                    )
        except Exception:
            pass
    rows_to_insert: list[tuple] = []
    with path.open("rb") as f:
        f.seek(start_offset)
        while True:
            line = f.readline()
            if not line:
                break
            new_offset = f.tell()
            try:
                d = json.loads(line.decode("utf-8", errors="replace"))
            except Exception:
                continue
            ts = _parse_iso(d.get("timestamp"))
            if not ts:
                continue
            t = d.get("type") or ""
            pl = d.get("payload") or {}
            if t == "session_meta":
                if isinstance(pl, dict):
                    cwd_hint = pl.get("cwd") or cwd_hint
                    originator = pl.get("originator") or originator
                    source = pl.get("source") or source
                    thread_source = pl.get("thread_source") or thread_source
                    session_source, autonomous = _classify_codex_metadata(
                        cwd_hint, originator, source, thread_source
                    )
                continue
            if not _is_codex_human_user(d):
                continue
            proj = real_project_from_cwd(cwd_hint) if cwd_hint else "codex"
            rows_to_insert.append((
                str(path),
                ts,
                "user",
                "codex",
                proj,
                cwd_hint or None,
                None,
                None,
                session_source,
                autonomous,
            ))
    if rows_to_insert:
        con.executemany(
            "INSERT INTO jsonl_turns(path, ts_unix, kind, engine, project, cwd, session_id, git_branch, session_source, autonomous) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows_to_insert,
        )
        inserted = len(rows_to_insert)
    new_count_clause = "?" if was_reset else "jsonl_files.turn_count + ?"
    con.execute(
        f"INSERT INTO jsonl_files(path, engine, mtime, size, indexed_offset, indexed_at, turn_count, cwd_hint, session_source, autonomous, parser_version) "
        f"VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        f"ON CONFLICT(path) DO UPDATE SET "
        f"  mtime=excluded.mtime, size=excluded.size, indexed_offset=excluded.indexed_offset, "
        f"  indexed_at=excluded.indexed_at, turn_count={new_count_clause}, cwd_hint=excluded.cwd_hint, "
        f"  session_source=excluded.session_source, autonomous=excluded.autonomous, "
        f"  parser_version=excluded.parser_version",
        (
            str(path), "codex", st.st_mtime, st.st_size, new_offset, time.time(),
            inserted, cwd_hint or None, session_source or None, autonomous,
            PARSER_VERSION, inserted,
        ),
    )
    return inserted


# ── orchestration ─────────────────────────────────────────────────────────────
def run(full: bool = False, quiet: bool = False) -> dict:
    con = _connect()
    if full:
        con.execute("DELETE FROM jsonl_turns")
        con.execute("DELETE FROM jsonl_files")
        con.commit()

    stats = {"files_scanned": 0, "files_updated": 0, "turns_inserted": 0}
    t0 = time.time()

    # Claude
    if CLAUDE_DIR.exists():
        for proj_dir in CLAUDE_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            for f in proj_dir.glob("*.jsonl"):
                stats["files_scanned"] += 1
                n = _index_claude_file(con, f)
                if n > 0:
                    stats["files_updated"] += 1
                    stats["turns_inserted"] += n
        con.commit()

    # Codex
    if CODEX_DIR.exists():
        for f in CODEX_DIR.rglob("rollout-*.jsonl"):
            stats["files_scanned"] += 1
            n = _index_codex_file(con, f)
            if n > 0:
                stats["files_updated"] += 1
                stats["turns_inserted"] += n
        con.commit()

    elapsed = time.time() - t0
    if not quiet:
        total_turns = con.execute("SELECT COUNT(*) FROM jsonl_turns").fetchone()[0]
        print(
            f"[indexer] scanned={stats['files_scanned']} "
            f"updated={stats['files_updated']} "
            f"new_turns={stats['turns_inserted']} "
            f"total_turns={total_turns} "
            f"elapsed={elapsed:.1f}s",
            file=sys.stderr,
        )
    return stats


def show_stats() -> None:
    con = _connect()
    total = con.execute("SELECT COUNT(*) FROM jsonl_turns").fetchone()[0]
    by_engine = con.execute(
        "SELECT engine, COUNT(*) FROM jsonl_turns GROUP BY engine"
    ).fetchall()
    earliest, latest = con.execute(
        "SELECT MIN(ts_unix), MAX(ts_unix) FROM jsonl_turns"
    ).fetchone()
    n_files = con.execute("SELECT COUNT(*) FROM jsonl_files").fetchone()[0]
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    print(f"DB:        {DB_PATH}")
    print(f"size:      {db_size/1024/1024:.1f} MB")
    print(f"files:     {n_files}")
    print(f"turns:     {total}")
    for eng, n in by_engine:
        print(f"  {eng}:    {n}")
    if earliest and latest:
        e_ = datetime.fromtimestamp(earliest, tz=timezone.utc).isoformat()
        l_ = datetime.fromtimestamp(latest, tz=timezone.utc).isoformat()
        print(f"period:    {e_}  →  {l_}")
    top = con.execute(
        "SELECT project, COUNT(*) n FROM jsonl_turns GROUP BY project ORDER BY n DESC LIMIT 15"
    ).fetchall()
    print("\nTop 15 projects by turn count:")
    for p, n in top:
        print(f"  {p:<40s} {n}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--full", action="store_true", help="re-index from scratch")
    p.add_argument("--stats", action="store_true", help="print DB stats and exit")
    p.add_argument("-q", "--quiet", action="store_true")
    args = p.parse_args()
    if args.stats:
        show_stats()
        return
    run(full=args.full, quiet=args.quiet)


if __name__ == "__main__":
    main()
