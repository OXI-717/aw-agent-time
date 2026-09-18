"""Tests for the interval helpers in scripts/aggregate.py.

  - _merge_intervals(intervals) → float seconds of the union
  - _find_l2_project(t, l2_spans) → which project's span covers time t

Both are pure: they take already-fetched data and return a value.
"""
from datetime import datetime

import pytest

import aggregate
from aggregate import _find_l2_project, _merge_intervals


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ── _merge_intervals ────────────────────────────────────────────────────────

def test_merge_empty_returns_zero():
    assert _merge_intervals([]) == 0.0


def test_merge_single_interval():
    intervals = [(_dt("2026-01-01T10:00:00"), _dt("2026-01-01T10:05:00"))]
    assert _merge_intervals(intervals) == 300.0


def test_merge_two_disjoint_intervals():
    intervals = [
        (_dt("2026-01-01T10:00:00"), _dt("2026-01-01T10:05:00")),  # 5 min
        (_dt("2026-01-01T11:00:00"), _dt("2026-01-01T11:10:00")),  # 10 min
    ]
    assert _merge_intervals(intervals) == 900.0


def test_merge_overlapping_intervals():
    intervals = [
        (_dt("2026-01-01T10:00:00"), _dt("2026-01-01T10:10:00")),  # 10 min
        (_dt("2026-01-01T10:05:00"), _dt("2026-01-01T10:15:00")),  # overlaps 5 min
    ]
    # Union = 10:00 → 10:15 = 15 min
    assert _merge_intervals(intervals) == 900.0


def test_merge_adjacent_intervals_treated_as_one():
    # First ends exactly when second starts (s == cur_e) → merged, no double-count
    intervals = [
        (_dt("2026-01-01T10:00:00"), _dt("2026-01-01T10:05:00")),  # ends 10:05
        (_dt("2026-01-01T10:05:00"), _dt("2026-01-01T10:10:00")),  # starts 10:05
    ]
    assert _merge_intervals(intervals) == 600.0


def test_merge_nested_intervals():
    # Second interval entirely inside first → outer wins, no inflation
    intervals = [
        (_dt("2026-01-01T10:00:00"), _dt("2026-01-01T11:00:00")),  # 60 min
        (_dt("2026-01-01T10:15:00"), _dt("2026-01-01T10:30:00")),  # inside
    ]
    assert _merge_intervals(intervals) == 3600.0


def test_merge_unsorted_input_is_sorted_internally():
    intervals = [
        (_dt("2026-01-01T11:00:00"), _dt("2026-01-01T11:10:00")),  # later, listed first
        (_dt("2026-01-01T10:00:00"), _dt("2026-01-01T10:05:00")),  # earlier, listed second
    ]
    assert _merge_intervals(intervals) == 900.0


def test_merge_three_overlapping_chains_into_one():
    intervals = [
        (_dt("2026-01-01T10:00:00"), _dt("2026-01-01T10:10:00")),
        (_dt("2026-01-01T10:05:00"), _dt("2026-01-01T10:20:00")),
        (_dt("2026-01-01T10:15:00"), _dt("2026-01-01T10:30:00")),
    ]
    # All chain together: 10:00 → 10:30 = 30 min
    assert _merge_intervals(intervals) == 1800.0


def test_merge_zero_length_interval_does_not_inflate():
    intervals = [
        (_dt("2026-01-01T10:00:00"), _dt("2026-01-01T10:00:00")),  # zero-length
    ]
    assert _merge_intervals(intervals) == 0.0


# ── _find_l2_project ────────────────────────────────────────────────────────

SPANS = [
    (_dt("2026-01-01T10:00:00"), _dt("2026-01-01T10:30:00"), "proj-a"),
    (_dt("2026-01-01T11:00:00"), _dt("2026-01-01T11:15:00"), "proj-b"),
]


def test_find_l2_empty_spans():
    assert _find_l2_project(_dt("2026-01-01T10:00:00"), []) == ""


def test_find_l2_before_all_spans():
    # 09:00 < first span start (10:00) → early return ""
    assert _find_l2_project(_dt("2026-01-01T09:00:00"), SPANS) == ""


def test_find_l2_at_start_of_first_span():
    assert _find_l2_project(_dt("2026-01-01T10:00:00"), SPANS) == "proj-a"


def test_find_l2_inside_first_span():
    assert _find_l2_project(_dt("2026-01-01T10:15:00"), SPANS) == "proj-a"


def test_find_l2_at_end_of_span_is_exclusive():
    # Span end is exclusive (s <= t < e) → 10:30 belongs to gap, not proj-a
    assert _find_l2_project(_dt("2026-01-01T10:30:00"), SPANS) == ""


def test_find_l2_in_gap_between_spans():
    # 10:45 is after first span ends (early-exit on next span start)
    assert _find_l2_project(_dt("2026-01-01T10:45:00"), SPANS) == ""


def test_find_l2_in_second_span():
    assert _find_l2_project(_dt("2026-01-01T11:10:00"), SPANS) == "proj-b"


def test_find_l2_after_all_spans():
    assert _find_l2_project(_dt("2026-01-01T12:00:00"), SPANS) == ""


def test_find_l2_unsorted_spans_does_not_early_return_correctly():
    # Documented assumption: spans must be sorted by start time.
    # If caller passes unsorted spans, the early-return-on-s>t can mask a later
    # match — this test pins that behaviour so future refactors are deliberate.
    unsorted = [
        (_dt("2026-01-01T11:00:00"), _dt("2026-01-01T11:15:00"), "proj-b"),
        (_dt("2026-01-01T10:00:00"), _dt("2026-01-01T10:30:00"), "proj-a"),
    ]
    # 10:15 < first listed span's start (11:00) → early exit ""
    assert _find_l2_project(_dt("2026-01-01T10:15:00"), unsorted) == ""


def test_direct_layer1_project_is_not_overwritten_by_layer2(monkeypatch):
    monkeypatch.setattr(
        aggregate,
        "_layer1_active_intervals",
        lambda _day: [
            (_dt("2026-01-01T10:00:00"), _dt("2026-01-01T10:30:00"), "oxi-skills")
        ],
    )
    monkeypatch.setattr(
        aggregate,
        "_layer2_engagement_spans",
        lambda _day: (_ for _ in ()).throw(AssertionError("Layer 2 must not be consulted")),
    )
    monkeypatch.setattr(
        aggregate,
        "categorize_project",
        lambda project: ["Work", "OXI", project],
    )

    summary = aggregate.summarise_by_umbrella(_dt("2026-01-01T00:00:00"))

    assert summary["coding"]["OXI"]["oxi-skills"] == 1800
    assert "project-d" not in summary["coding"]["OXI"]


def test_attribution_quality_uses_interval_unions(monkeypatch):
    events = [
        {
            "timestamp": "2026-01-01T10:00:00Z",
            "duration": 60,
            "data": {
                "state": "ACTIVE_EDIT",
                "attribution_source": "tmux_focus",
                "attribution_ambiguous": False,
            },
        },
        {
            "timestamp": "2026-01-01T10:01:00Z",
            "duration": 30,
            "data": {
                "state": "ACTIVE_REVIEW",
                "attribution_source": "human_turn",
                "attribution_ambiguous": True,
            },
        },
    ]
    monkeypatch.setattr(aggregate, "get_events", lambda *_args, **_kwargs: events)

    quality = aggregate.summarise_attribution(_dt("2026-01-01T00:00:00"))

    assert quality == {
        "direct": 60.0,
        "inferred": 30.0,
        "application": 0.0,
        "unattributed": 0.0,
        "ambiguous": 30.0,
        "wallclock": 90.0,
    }


def test_tmux_summary_reads_only_new_focus_bucket(monkeypatch):
    seen = {}
    events = [
        {
            "timestamp": "2026-01-01T10:00:00Z",
            "duration": 0,
            "data": {"session": "alpha", "project": "alpha"},
        },
        {
            "timestamp": "2026-01-01T10:00:05Z",
            "duration": 0,
            "data": {"session": "beta", "project": "beta"},
        },
    ]

    def fake_get_events(bucket, *_args):
        seen["bucket"] = bucket
        return events

    monkeypatch.setattr(aggregate, "get_events", fake_get_events)
    summary = aggregate.summarise_tmux_panes(_dt("2026-01-01T00:00:00"))

    assert seen["bucket"].startswith("aw-watcher-tmux-focus_")
    assert summary == {("alpha", "alpha"): 5.0, ("beta", "beta"): 10.0}
