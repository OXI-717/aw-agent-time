import json
from datetime import datetime, timezone

import jsonl_indexer
import tracker_layer25
from jsonl_indexer import SPAN_GAP_SEC, _connect, _index_claude_file, _index_codex_file


def _iso(h: int, m: int) -> str:
    return datetime(2026, 9, 1, h, m, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _write(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _codex(path, originator, stamps, cwd="/work/proj-a"):
    recs = [{"timestamp": _iso(*stamps[0]), "type": "session_meta",
             "payload": {"cwd": cwd, "originator": originator, "source": "exec" if originator == "codex_exec" else "cli"}}]
    recs += [{"timestamp": _iso(h, m), "type": "response_item",
              "payload": {"type": "message", "role": "assistant", "content": []}} for h, m in stamps[1:]]
    _write(path, recs)


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "index.sqlite"
    monkeypatch.setattr(jsonl_indexer, "DB_PATH", db)
    monkeypatch.setattr(tracker_layer25, "INDEX_DB_PATH", db)
    return _connect()


def test_spans_split_on_gap(tmp_path, monkeypatch):
    con = _setup(tmp_path, monkeypatch)
    f = tmp_path / "rollout-a.jsonl"
    _codex(f, "codex_exec", [(10, 0), (10, 5), (10, 10), (13, 0), (13, 10)])
    _index_codex_file(con, f)
    spans = con.execute("SELECT start_ts, end_ts FROM jsonl_spans ORDER BY start_ts").fetchall()
    assert len(spans) == 2
    assert spans[0][1] - spans[0][0] == 600
    assert spans[1][1] - spans[1][0] == 600
    assert SPAN_GAP_SEC < 170 * 60


def test_incremental_append_extends_last_span(tmp_path, monkeypatch):
    con = _setup(tmp_path, monkeypatch)
    f = tmp_path / "rollout-b.jsonl"
    _codex(f, "codex_exec", [(10, 0), (10, 5)])
    _index_codex_file(con, f)
    with f.open("a") as fh:
        fh.write(json.dumps({"timestamp": _iso(10, 12), "type": "response_item",
                             "payload": {"type": "message", "role": "assistant"}}) + "\n")
    import os, time
    os.utime(f, (time.time() + 5, time.time() + 5))
    _index_codex_file(con, f)
    spans = con.execute("SELECT start_ts, end_ts FROM jsonl_spans").fetchall()
    assert len(spans) == 1 and spans[0][1] - spans[0][0] == 720


def test_layer25_reads_autonomous_sessions_only(tmp_path, monkeypatch):
    con = _setup(tmp_path, monkeypatch)
    _codex(tmp_path / "rollout-exec.jsonl", "codex_exec", [(10, 0), (10, 10), (10, 20), (10, 30)])
    _codex(tmp_path / "rollout-cli.jsonl", "codex_cli_rs", [(11, 0), (11, 10)])
    for name in ("rollout-exec.jsonl", "rollout-cli.jsonl"):
        _index_codex_file(con, tmp_path / name)
    con.commit()
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end = datetime(2026, 9, 2, tzinfo=timezone.utc)
    events = tracker_layer25.events_from_session_index(start, end, skip_worktrees=False)
    assert [e["data"]["task_id"] for e in events] == ["rollout-exec"]
    assert events[0]["duration"] == 1800
    assert events[0]["data"]["source"] == "session_index"


def test_claude_worktree_session_is_autonomous_and_skippable(tmp_path, monkeypatch):
    con = _setup(tmp_path, monkeypatch)
    f = tmp_path / "s1.jsonl"
    cwd = "/work/proj-a/.task-runner/worktrees/t1"
    _write(f, [{"timestamp": _iso(9, m), "type": "assistant", "cwd": cwd, "message": {"content": []}} for m in (0, 10)])
    _index_claude_file(con, f)
    con.commit()
    assert con.execute("SELECT autonomous FROM jsonl_files").fetchone()[0] == 1
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end = datetime(2026, 9, 2, tzinfo=timezone.utc)
    assert len(tracker_layer25.events_from_session_index(start, end, skip_worktrees=False)) == 1
    assert tracker_layer25.events_from_session_index(start, end, skip_worktrees=True) == []
