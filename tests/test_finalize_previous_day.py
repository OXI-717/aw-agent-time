"""Tests for finalize_day — the post-rollover redraw of yesterday's time-*.md.

The 30-minute launchd run only rewrites *today's* file, so a day's last
snapshot is taken before its UTC window closes and would keep the
«День не закрыт» callout forever (issue #15 follow-up). finalize_day redraws
such a file once the window has closed; the callout itself is the staleness
marker, so the pass self-closes. Network-touching summarisers and tracker
subprocesses are monkeypatched throughout — render_markdown with empty
summaries stays pure.
"""
import sys
from datetime import datetime, timedelta, timezone

import pytest

import aggregate
from aggregate import finalize_day, render_markdown


DAY = datetime(2026, 1, 15, tzinfo=timezone.utc)          # the (closed) report day
NOW = datetime(2026, 1, 16, 4, 15, tzinfo=timezone.utc)   # first run after rollover
MIDDAY = DAY.replace(hour=12)                             # window still open here


def _stale_md(day: datetime) -> str:
    """A pre-rollover snapshot: rendered mid-day, carries the open-day callout."""
    return render_markdown(day, {}, {}, {}, now=day.replace(hour=12))


def _final_md(day: datetime) -> str:
    """What a post-close render of the same day looks like."""
    return render_markdown(day, {}, {}, {}, now=NOW)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    folder = tmp_path / "_AGENT_SESSIONS" / "time"
    folder.mkdir(parents=True)
    monkeypatch.setattr(aggregate, "OBSIDIAN_FOLDER", folder)
    return folder


@pytest.fixture
def runs(monkeypatch):
    calls: list[tuple[str, datetime, bool]] = []
    monkeypatch.setattr(
        aggregate,
        "_run",
        lambda script, day, dry: (calls.append((script, day, dry)), 0)[1],
    )
    return calls


@pytest.fixture
def rendered(monkeypatch):
    """Replace the AW-collecting pipeline with the pure formatter, recording calls."""
    seen: list[tuple[datetime, datetime]] = []

    def fake(day, now):
        seen.append((day, now))
        return render_markdown(day, {}, {}, {}, now=now)

    monkeypatch.setattr(aggregate, "_render_day_report", fake)
    return seen


def _write_stale(vault, day=DAY):
    out = vault / f"time-{day.strftime('%Y-%m-%d')}.md"
    out.write_text(_stale_md(day), encoding="utf-8")
    return out


# ── the marker itself ────────────────────────────────────────────────────────

def test_marker_is_callout_shaped_not_footer_shaped():
    # A closed-day render still quotes «День не закрыт» in its footer — the
    # staleness marker must match only the callout, never the footer.
    assert aggregate.OPEN_DAY_CALLOUT in _stale_md(DAY)
    assert aggregate.OPEN_DAY_CALLOUT not in _final_md(DAY)
    assert "«День не закрыт»" in _final_md(DAY)


# ── finalize_day unit behaviour ──────────────────────────────────────────────

def test_finalizes_stale_file_and_removes_marker(vault, runs, rendered):
    out = _write_stale(vault)
    assert finalize_day(DAY, NOW) is True
    new = out.read_text(encoding="utf-8")
    assert aggregate.OPEN_DAY_CALLOUT not in new
    # Redrawn for the closed window with the finalize-run `now` as snapshot.
    assert rendered == [(DAY, NOW)]


def test_reruns_all_three_trackers_for_the_closed_window(vault, runs, rendered):
    _write_stale(vault)
    finalize_day(DAY, NOW)
    assert runs == [
        ("tracker_layer1.py", DAY, False),
        ("tracker_layer2.py", DAY, False),
        ("tracker_layer25.py", DAY, False),
    ]


def test_dry_run_flag_propagates_to_trackers(vault, runs, rendered):
    _write_stale(vault)
    finalize_day(DAY, NOW, dry=True)
    assert runs and all(dry is True for _script, _day, dry in runs)


def test_skip_trackers_still_redraws_from_existing_aw_data(vault, runs, rendered):
    out = _write_stale(vault)
    assert finalize_day(DAY, NOW, skip_trackers=True) is True
    assert runs == []
    assert aggregate.OPEN_DAY_CALLOUT not in out.read_text(encoding="utf-8")


def test_missing_file_is_a_noop(vault, runs, rendered):
    assert finalize_day(DAY, NOW) is False
    assert runs == []
    assert rendered == []


def test_already_final_file_is_left_untouched(vault, runs, rendered):
    out = vault / "time-2026-01-15.md"
    final = _final_md(DAY)
    out.write_text(final, encoding="utf-8")
    assert finalize_day(DAY, NOW) is False
    assert out.read_text(encoding="utf-8") == final
    assert runs == []
    assert rendered == []


def test_open_window_is_never_finalized(vault, runs, rendered):
    # The pass must not rewrite a file whose day is still accumulating —
    # its partial snapshot and callout are legitimate until the window closes.
    out = _write_stale(vault)
    stale = out.read_text(encoding="utf-8")
    assert finalize_day(DAY, MIDDAY) is False
    assert out.read_text(encoding="utf-8") == stale
    assert runs == []
    assert rendered == []


def test_snapshot_time_is_the_finalize_run(vault, runs, rendered):
    out = _write_stale(vault)
    finalize_day(DAY, NOW)
    assert NOW.astimezone().strftime("%Y-%m-%d %H:%M") in out.read_text(encoding="utf-8")


def test_other_days_files_are_not_touched(vault, runs, rendered):
    _write_stale(vault)
    other = vault / "time-2026-01-16.md"
    other.write_text("today snapshot", encoding="utf-8")
    finalize_day(DAY, NOW)
    assert other.read_text(encoding="utf-8") == "today snapshot"


def test_pass_self_closes(vault, runs, rendered):
    # Second run finds no marker → no trackers, no rewrite, returns False.
    out = _write_stale(vault)
    assert finalize_day(DAY, NOW) is True
    runs.clear()
    rendered.clear()
    assert finalize_day(DAY, NOW) is False
    assert runs == []
    assert rendered == []


# ── main() wiring ────────────────────────────────────────────────────────────

def _argv(monkeypatch, *flags):
    monkeypatch.setattr(sys, "argv", ["aggregate.py", *flags])


def _real_yesterday() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=1)


def test_main_today_mode_finalizes_stale_yesterday(monkeypatch, vault, runs, rendered):
    stale = _write_stale(vault, day=_real_yesterday())
    _argv(monkeypatch, "--today", "--skip-trackers")
    aggregate.main()
    assert aggregate.OPEN_DAY_CALLOUT not in stale.read_text(encoding="utf-8")
    # One render for today + one finalize render for yesterday.
    assert len(rendered) == 2


def test_main_today_runs_yesterday_trackers_without_skip_flag(
    monkeypatch, vault, runs, rendered
):
    yesterday = _real_yesterday()
    _write_stale(vault, day=yesterday)
    _argv(monkeypatch, "--today")
    aggregate.main()
    assert [day.date() for _s, day, _d in runs].count(yesterday.date()) == 3


def test_main_explicit_date_skips_finalize(monkeypatch, vault, runs, rendered):
    stale = _write_stale(vault, day=_real_yesterday())
    _argv(monkeypatch, "--date", "2026-01-10", "--skip-trackers")
    aggregate.main()
    assert aggregate.OPEN_DAY_CALLOUT in stale.read_text(encoding="utf-8")
    assert len(rendered) == 1


def test_main_yesterday_flag_skips_finalize(monkeypatch, vault, runs, rendered):
    # Manual --yesterday re-renders that day itself; the finalize pass must
    # not add a second rewrite on top.
    _write_stale(vault, day=_real_yesterday())
    _argv(monkeypatch, "--yesterday", "--skip-trackers")
    aggregate.main()
    assert len(rendered) == 1


def test_main_no_obsidian_skips_finalize(monkeypatch, capsys, vault, runs, rendered):
    stale = _write_stale(vault, day=_real_yesterday())
    _argv(monkeypatch, "--today", "--skip-trackers", "--no-obsidian")
    aggregate.main()
    assert aggregate.OPEN_DAY_CALLOUT in stale.read_text(encoding="utf-8")
    assert len(rendered) == 1
    assert "# Тайм-трекер" in capsys.readouterr().out


def test_tracker_failure_keeps_the_day_stale_for_retry(vault, rendered, monkeypatch):
    """A failed tracker must not freeze incomplete figures behind a dropped marker."""
    out = _write_stale(vault)
    codes = iter([1, 0, 0])
    monkeypatch.setattr(aggregate, "_run", lambda script, day, dry: next(codes, 0))

    assert aggregate.finalize_day(DAY, NOW) is False
    assert out.read_text(encoding="utf-8") == _stale_md(DAY)


def test_next_run_retries_a_day_left_stale_by_a_failed_tracker(vault, rendered, monkeypatch):
    """The retry actually happens once the trackers recover."""
    out = _write_stale(vault)
    codes = [1, 0, 0]
    monkeypatch.setattr(
        aggregate, "_run", lambda script, day, dry: codes.pop(0) if codes else 0
    )

    assert aggregate.finalize_day(DAY, NOW) is False
    assert aggregate.finalize_day(DAY, NOW) is True
    assert out.read_text(encoding="utf-8") == _final_md(DAY)


def test_run_reports_the_tracker_exit_code(tmp_path, monkeypatch):
    """The whole retry guarantee rests on _run surfacing the real exit code."""
    monkeypatch.setattr(aggregate, "SCRIPT_DIR", tmp_path)
    (tmp_path / "ok.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (tmp_path / "boom.py").write_text("raise SystemExit(3)\n", encoding="utf-8")

    assert aggregate._run("ok.py", DAY, dry=False) == 0
    assert aggregate._run("boom.py", DAY, dry=False) == 3
