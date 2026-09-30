from datetime import datetime, timezone

import bb_threads
import tracker_layer1

T = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)


def _with_map(monkeypatch, mapping):
    monkeypatch.setattr(bb_threads, "_MAP", mapping)


def test_bb_title_resolves_to_thread_project(monkeypatch):
    _with_map(monkeypatch, {"rebeam codex": [["rebeam", 1, 2]]})
    project, source, ambiguous, _ = tracker_layer1._resolve_attribution(
        {"app": "bb", "title": "REBEAM CODEX"}, T, [], [])
    assert (project, source, ambiguous) == ("rebeam", "bb_thread", False)


def test_same_title_picks_thread_alive_at_event_time(monkeypatch):
    ms = T.timestamp() * 1000
    _with_map(monkeypatch, {"n8n watchdog": [["whai", ms - 10, ms + 10], ["aimindset-private", ms + 100, ms + 900]]})
    assert bb_threads.project_for_title("n8n watchdog", T) == ("whai", False)
    assert bb_threads.project_for_title("n8n watchdog") == ("aimindset-private", True)


def test_worktree_path_folds_to_repo(monkeypatch):
    calls = {("environment", "list"): [{"id": "e1", "path": "/work/src/proj-a/.worktrees/fix/login"}],
             ("thread", "list"): [{"id": "t1", "title": "Fix", "environmentId": "e1", "createdAt": 1, "updatedAt": 2}]}
    monkeypatch.setattr(bb_threads, "_bb_cli", lambda: "bb")
    monkeypatch.setattr(bb_threads, "_bb_json", lambda cli, *a: calls.get(a[:2], []) if len(a) <= 4 and "--status" not in a and "--archived" not in a and "--include-hidden" not in a else [])
    monkeypatch.setattr(bb_threads, "real_project_from_cwd", lambda p: p.rsplit("/", 1)[-1])
    assert bb_threads._build_map() == {"fix": [["proj-a", 1, 2]]}


def test_bb_failure_keeps_stale_cache(monkeypatch, tmp_path):
    cache = tmp_path / "bb.json"
    cache.write_text('{"version": 2, "built_at": 0, "map": {"x": [["proj-a", 1, 2]]}}')
    monkeypatch.setattr(bb_threads, "CACHE_FILE", cache)
    monkeypatch.setattr(bb_threads, "_MAP", None)
    def boom():
        raise RuntimeError("daemon down")
    monkeypatch.setattr(bb_threads, "_build_map", boom)
    assert bb_threads.title_map() == {"x": [["proj-a", 1, 2]]}


def test_unknown_bb_title_falls_back_to_application(monkeypatch):
    _with_map(monkeypatch, {})
    _project, source, _, _ = tracker_layer1._resolve_attribution({"app": "bb", "title": "bb"}, T, [], [])
    assert source != "bb_thread"


def test_build_map_skips_generic_titles_and_threads_without_path(monkeypatch):
    calls = {
        ("environment", "list"): [{"id": "e1", "path": "/work/src/proj-a"}],
        ("thread", "list"): [
            {"id": "t1", "title": "Fix login", "environmentId": "e1", "createdAt": 1, "updatedAt": 3},
            {"id": "t2", "title": "bb", "environmentId": "e1", "updatedAt": 4},
            {"id": "t3", "title": "No env", "environmentId": "e9", "updatedAt": 5},
        ],
    }
    monkeypatch.setattr(bb_threads, "_bb_cli", lambda: "bb")
    monkeypatch.setattr(bb_threads, "_bb_json", lambda cli, *a: calls.get(a[:2], []) if "--status" not in a and "--archived" not in a and "--include-hidden" not in a else [])
    monkeypatch.setattr(bb_threads, "real_project_from_cwd", lambda p: p.rsplit("/", 1)[-1])
    assert bb_threads._build_map() == {"fix login": [["proj-a", 1, 3]]}


def test_missing_cli_is_a_failure_not_an_empty_map(monkeypatch, tmp_path):
    cache = tmp_path / "bb.json"
    cache.write_text('{"version": 2, "built_at": 0, "map": {"x": [["proj-a", 1, 2]]}}')
    monkeypatch.setattr(bb_threads, "CACHE_FILE", cache)
    monkeypatch.setattr(bb_threads, "_MAP", None)
    monkeypatch.setattr(bb_threads, "_bb_cli", lambda: None)
    assert bb_threads.title_map() == {"x": [["proj-a", 1, 2]]}
    assert "proj-a" in cache.read_text()


def test_old_cache_format_is_rebuilt(monkeypatch, tmp_path):
    cache = tmp_path / "bb.json"
    cache.write_text('{"built_at": 9999999999, "map": {"x": [["old", 5]]}}')
    monkeypatch.setattr(bb_threads, "CACHE_FILE", cache)
    monkeypatch.setattr(bb_threads, "_MAP", None)
    monkeypatch.setattr(bb_threads, "_build_map", lambda: {"x": [["new", 1, 2]]})
    assert bb_threads.title_map() == {"x": [["new", 1, 2]]}
