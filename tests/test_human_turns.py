import json
import sqlite3
from datetime import datetime, timezone

from jsonl_indexer import (
    PARSER_VERSION,
    _connect,
    _index_claude_file,
    _index_codex_file,
    _is_autonomous_session,
    _is_claude_human_user,
    _is_codex_human_user,
)
import jsonl_indexer
from tracker_layer2 import build_blocks


def _dt(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 1, 10, minute, second, tzinfo=timezone.utc)


def test_claude_plain_user_text_is_human_turn():
    record = {"type": "user", "message": {"content": "fix the report"}}
    assert _is_claude_human_user(record)


def test_claude_tool_result_is_not_human_turn():
    record = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "x"}]},
    }
    assert not _is_claude_human_user(record)


def test_claude_meta_and_sidechain_text_are_not_human_turns():
    meta = {"type": "user", "isMeta": True, "message": {"content": "context"}}
    sidechain = {"type": "user", "isSidechain": True, "message": {"content": "task"}}
    assert not _is_claude_human_user(meta)
    assert not _is_claude_human_user(sidechain)


def test_codex_user_message_is_human_turn():
    record = {
        "type": "response_item",
        "payload": {"type": "message", "role": "user"},
    }
    assert _is_codex_human_user(record)


def test_codex_synthetic_context_records_are_not_human_turns():
    for tag in ("<environment_context>", "<recommended_plugins>"):
        record = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"{tag}\nsome injected context\n</...>"}],
            },
        }
        assert not _is_codex_human_user(record)


def test_codex_service_and_tool_records_are_not_human_turns():
    assert not _is_codex_human_user(
        {"type": "event_msg", "payload": {"type": "item_completed"}}
    )
    assert not _is_codex_human_user(
        {"type": "response_item", "payload": {"type": "custom_tool_call"}}
    )
    assert not _is_codex_human_user(
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant"},
        }
    )


def test_exec_and_task_runner_sessions_are_autonomous():
    assert _is_autonomous_session("codex", "/repo", originator="codex_exec", source="exec")
    assert _is_autonomous_session("codex", "/repo", originator="codex-tui", source="exec")
    assert _is_autonomous_session(
        "claude",
        "/repo/.task-runner/worktrees/task-1",
        originator="",
        source="",
    )


def test_interactive_tui_session_is_not_autonomous():
    assert not _is_autonomous_session(
        "codex", "/repo/.worktrees/issue-16", originator="codex-tui", source="cli"
    )


def test_codex_subagent_session_is_autonomous():
    assert _is_autonomous_session(
        "codex",
        "/repo",
        originator="codex-tui",
        source={"subagent": {"thread_spawn": {"depth": 1}}},
        thread_source="subagent",
    )


def test_engagement_blocks_require_user_anchor():
    turns = [(_dt(0), "assistant", "/repo"), (_dt(1), "assistant", "/repo")]
    assert build_blocks(turns, "claude", "session") == []


def test_single_user_turn_creates_bounded_engagement_pulse():
    blocks = build_blocks([(_dt(0), "user", "/repo")], "codex", "session")
    assert len(blocks) == 1
    assert blocks[0]["timestamp"] == "2026-09-01T10:00:00Z"
    assert blocks[0]["duration"] == 30


def test_user_turns_merge_but_assistant_output_does_not_extend_block():
    turns = [
        (_dt(0), "user", "/repo"),
        (_dt(1), "assistant", "/repo"),
        (_dt(2), "user", "/repo"),
        (_dt(4), "assistant", "/repo"),
    ]
    blocks = build_blocks(turns, "claude", "session")
    assert len(blocks) == 1
    assert blocks[0]["duration"] == 150


def _write_jsonl(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def test_claude_index_keeps_only_human_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(jsonl_indexer, "DB_PATH", tmp_path / "index.sqlite")
    session = tmp_path / "claude.jsonl"
    base = {"timestamp": "2026-09-01T10:00:00Z", "cwd": "/repo", "sessionId": "s"}
    _write_jsonl(
        session,
        [
            {**base, "type": "user", "message": {"content": "real prompt"}},
            {
                **base,
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "x"}]},
            },
            {**base, "type": "assistant", "message": {"content": "answer"}},
        ],
    )
    con = _connect()

    assert _index_claude_file(con, session) == 1
    assert con.execute(
        "SELECT kind, session_source, autonomous FROM jsonl_turns"
    ).fetchall() == [("user", "claude-code", 0)]
    assert con.execute(
        "SELECT parser_version FROM jsonl_files"
    ).fetchone() == (PARSER_VERSION,)


def test_codex_index_excludes_service_events_and_marks_exec_autonomous(tmp_path, monkeypatch):
    monkeypatch.setattr(jsonl_indexer, "DB_PATH", tmp_path / "index.sqlite")
    session = tmp_path / "codex.jsonl"
    _write_jsonl(
        session,
        [
            {
                "timestamp": "2026-09-01T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "cwd": "/repo",
                    "originator": "codex_exec",
                    "source": "exec",
                },
            },
            {
                "timestamp": "2026-09-01T10:00:01Z",
                "type": "event_msg",
                "payload": {"type": "item_completed"},
            },
            {
                "timestamp": "2026-09-01T10:00:02Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "user"},
            },
        ],
    )
    con = _connect()

    assert _index_codex_file(con, session) == 1
    assert con.execute(
        "SELECT kind, session_source, autonomous FROM jsonl_turns"
    ).fetchall() == [("user", "codex_exec:exec", 1)]


def test_codex_index_fails_closed_without_session_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(jsonl_indexer, "DB_PATH", tmp_path / "index.sqlite")
    session = tmp_path / "codex.jsonl"
    _write_jsonl(
        session,
        [{
            "timestamp": "2026-09-01T10:00:02Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user"},
        }],
    )
    con = _connect()

    assert _index_codex_file(con, session) == 1
    assert con.execute(
        "SELECT project, session_source, autonomous FROM jsonl_turns"
    ).fetchall() == [("codex", "unverified", 1)]


def test_codex_index_fails_closed_with_incomplete_session_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(jsonl_indexer, "DB_PATH", tmp_path / "index.sqlite")
    session = tmp_path / "codex.jsonl"
    _write_jsonl(
        session,
        [
            {
                "timestamp": "2026-09-01T10:00:00Z",
                "type": "session_meta",
                "payload": {"cwd": "/repo"},
            },
            {
                "timestamp": "2026-09-01T10:00:02Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "user"},
            },
        ],
    )
    con = _connect()

    assert _index_codex_file(con, session) == 1
    assert con.execute(
        "SELECT project, session_source, autonomous FROM jsonl_turns"
    ).fetchall() == [("repo", "unverified", 1)]


def test_codex_turn_before_valid_session_metadata_stays_autonomous(tmp_path, monkeypatch):
    monkeypatch.setattr(jsonl_indexer, "DB_PATH", tmp_path / "index.sqlite")
    session = tmp_path / "codex.jsonl"
    _write_jsonl(
        session,
        [
            {
                "timestamp": "2026-09-01T10:00:01Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "user"},
            },
            {
                "timestamp": "2026-09-01T10:00:02Z",
                "type": "session_meta",
                "payload": {
                    "cwd": "/repo",
                    "originator": "codex-tui",
                    "source": "cli",
                },
            },
            {
                "timestamp": "2026-09-01T10:00:03Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "user"},
            },
        ],
    )
    con = _connect()

    assert _index_codex_file(con, session) == 2
    assert con.execute(
        "SELECT session_source, autonomous FROM jsonl_turns ORDER BY ts_unix"
    ).fetchall() == [("unverified", 1), ("codex-tui:cli", 0)]


def test_legacy_index_schema_is_migrated_and_reparsed(tmp_path, monkeypatch):
    db = tmp_path / "index.sqlite"
    session = tmp_path / "claude.jsonl"
    _write_jsonl(
        session,
        [{
            "timestamp": "2026-09-01T10:00:00Z",
            "cwd": "/repo",
            "sessionId": "s",
            "type": "user",
            "message": {"content": "human"},
        }],
    )
    legacy = sqlite3.connect(db)
    legacy.executescript(
        """
        CREATE TABLE jsonl_files (
          path TEXT PRIMARY KEY, engine TEXT NOT NULL, mtime REAL NOT NULL,
          size INTEGER NOT NULL, indexed_offset INTEGER NOT NULL,
          indexed_at REAL NOT NULL, turn_count INTEGER NOT NULL DEFAULT 0,
          cwd_hint TEXT
        );
        CREATE TABLE jsonl_turns (
          path TEXT NOT NULL, ts_unix REAL NOT NULL, kind TEXT NOT NULL,
          engine TEXT NOT NULL, project TEXT NOT NULL, cwd TEXT,
          session_id TEXT, git_branch TEXT
        );
        """
    )
    stat = session.stat()
    legacy.execute(
        "INSERT INTO jsonl_files VALUES (?,?,?,?,?,?,?,?)",
        (str(session), "claude", stat.st_mtime, stat.st_size, stat.st_size, 0, 99, None),
    )
    legacy.execute(
        "INSERT INTO jsonl_turns VALUES (?,?,?,?,?,?,?,?)",
        (str(session), 0, "user", "claude", "wrong", "/wrong", "s", None),
    )
    legacy.commit()
    legacy.close()
    monkeypatch.setattr(jsonl_indexer, "DB_PATH", db)

    con = _connect()
    assert _index_claude_file(con, session) == 1

    assert con.execute("SELECT project, kind FROM jsonl_turns").fetchall() == [("repo", "user")]
    assert con.execute("SELECT parser_version FROM jsonl_files").fetchone() == (PARSER_VERSION,)


def test_agents_md_instruction_bundle_is_not_a_human_turn():
    """Codex injects the AGENTS.md rule bundle through the real-user envelope."""
    record = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": "# AGENTS.md instructions for /Users/example/src/project-a\n\n<INSTRUCTIONS>\n...",
            }],
        },
    }
    assert jsonl_indexer._is_codex_human_user(record) is False


def test_real_prompt_mentioning_agents_md_stays_a_human_turn():
    record = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "обнови AGENTS.md instructions в репо"}],
        },
    }
    assert jsonl_indexer._is_codex_human_user(record) is True


def test_parser_version_forces_reparse_of_synthetic_era_index():
    """Rows indexed before the synthetic filter must be invalidated, not kept."""
    assert PARSER_VERSION >= 4
