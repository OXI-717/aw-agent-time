import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import backfill_history


DAY = datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_run_day_runs_trackers_then_report_with_check(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr(backfill_history.subprocess, "run", fake_run)

    backfill_history.run_day(DAY, ["tracker_layer1.py", "tracker_layer2.py"], reports=True)

    script_dir = Path(backfill_history.__file__).parent
    assert [call[0] for call in calls] == [
        [sys.executable, str(script_dir / "tracker_layer1.py"), "--date", "2026-09-01"],
        [sys.executable, str(script_dir / "tracker_layer2.py"), "--date", "2026-09-01"],
        [
            sys.executable,
            str(script_dir / "aggregate.py"),
            "--date",
            "2026-09-01",
            "--skip-trackers",
        ],
    ]
    assert all(kwargs["check"] is True for _command, kwargs in calls)


def test_run_day_can_skip_reports(monkeypatch):
    calls = []
    monkeypatch.setattr(
        backfill_history.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command),
    )

    backfill_history.run_day(DAY, ["tracker_layer1.py"], reports=False)

    assert len(calls) == 1
    assert calls[0][1].endswith("tracker_layer1.py")


def test_run_day_propagates_child_failure(monkeypatch):
    def fail(command, **_kwargs):
        raise subprocess.CalledProcessError(2, command)

    monkeypatch.setattr(backfill_history.subprocess, "run", fail)

    with pytest.raises(subprocess.CalledProcessError):
        backfill_history.run_day(DAY, ["tracker_layer1.py"], reports=True)


def test_refresh_index_runs_indexer_with_failure_check(monkeypatch):
    calls = []
    monkeypatch.setattr(
        backfill_history.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    backfill_history.refresh_index()

    command, kwargs = calls[0]
    assert command[1].endswith("jsonl_indexer.py")
    assert kwargs["check"] is True
