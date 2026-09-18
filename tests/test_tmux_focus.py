import os

import aw_watcher_tmux_pane as watcher


SAMPLE = (
    "/dev/ttys023|claude_orca|1788359160|0|main|1|4242|codex|"
    "/Users/example/workspaces/activitywatch-setup/worktree-26.08-87|working"
)


def test_watcher_uses_new_single_focus_bucket():
    assert watcher.BUCKET == f"aw-watcher-tmux-focus_{watcher.HOSTNAME}"
    assert watcher.EVENT_TYPE == "tmux.focusedpane"


def test_get_focused_pane_queries_tmux_best_client(monkeypatch):
    calls = []
    monkeypatch.setattr(watcher, "_tmux", lambda args: calls.append(args) or SAMPLE)
    monkeypatch.setattr(
        watcher,
        "_detect_ai_project",
        lambda _pid: ("activitywatch-setup", "codex", "/repo"),
    )

    pane = watcher.get_focused_pane()

    assert len(calls) == 1
    assert calls[0][:3] == ["display-message", "-p", "-F"]
    assert "-t" not in calls[0]
    assert pane == {
        "session": "claude_orca",
        "window": "0:main",
        "pane": "1",
        "command": "codex",
        "shell_cwd": "/Users/example/workspaces/activitywatch-setup/worktree-26.08-87",
        "shell_project": "worktree-26.08-87",
        "project": "activitywatch-setup",
        "ai_project": "activitywatch-setup",
        "ai_engine": "codex",
        "ai_cwd": "/repo",
        "title": "working",
        "client_tty": "/dev/ttys023",
        "client_activity": 1788359160,
    }


def test_get_focused_pane_rejects_malformed_output(monkeypatch):
    monkeypatch.setattr(watcher, "_tmux", lambda _args: "broken|output")
    assert watcher.get_focused_pane() is None


def test_get_focused_pane_rejects_invalid_client_activity(monkeypatch):
    monkeypatch.setattr(
        watcher,
        "_tmux",
        lambda _args: SAMPLE.replace("1788359160", "not-a-timestamp"),
    )
    assert watcher.get_focused_pane() is None


def test_tmux_command_does_not_inherit_caller_client(monkeypatch):
    captured = {}

    def fake_check_output(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return b"ok\n"

    monkeypatch.setenv("TMUX", "/tmp/tmux.sock,1,2")
    monkeypatch.setenv("TMUX_PANE", "%42")
    monkeypatch.setattr(watcher.subprocess, "check_output", fake_check_output)

    assert watcher._tmux(["display-message", "-p", "hello"]) == "ok"
    assert captured["command"] == ["tmux", "display-message", "-p", "hello"]
    assert "TMUX" not in captured["env"]
    assert "TMUX_PANE" not in captured["env"]
    assert captured["env"]["PATH"] == os.environ["PATH"]
