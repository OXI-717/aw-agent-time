from datetime import datetime, timezone

import tracker_layer1 as layer1


def _dt(second: int = 0) -> datetime:
    return datetime(2026, 9, 1, 10, 0, second, tzinfo=timezone.utc)


def _focus_event(second: int, project: str, duration: float = 0) -> dict:
    return {
        "timestamp": _dt(second).isoformat().replace("+00:00", "Z"),
        "duration": duration,
        "data": {"project": project, "session": f"claude_{project}"},
    }


def test_focus_samples_become_bounded_non_overlapping_intervals():
    intervals = layer1._focus_intervals(
        [_focus_event(0, "alpha"), _focus_event(5, "beta")]
    )
    assert intervals == [
        (_dt(0), _dt(5), "alpha"),
        (_dt(5), _dt(15), "beta"),
    ]


def test_merged_focus_heartbeat_extends_from_recorded_duration():
    intervals = layer1._focus_intervals([_focus_event(0, "alpha", duration=30)])
    assert intervals == [(_dt(0), _dt(40), "alpha")]


def test_orca_focus_has_priority_over_human_turns():
    result = layer1._resolve_attribution(
        {"app": "Orca", "title": "Orca"},
        _dt(5),
        [(_dt(0), _dt(10), "direct-project")],
        [(_dt(5), "other-project")],
    )
    assert result == ("direct-project", "tmux_focus", False, _dt(10))


def test_orca_uses_nearest_bounded_human_turn_without_focus():
    result = layer1._resolve_attribution(
        {"app": "Orca", "title": "Orca"},
        _dt(5),
        [],
        [(_dt(0), "older"), (_dt(8), "nearer")],
    )
    # Evidence expires 120s after the turn at _dt(8), not after the probe at _dt(5).
    assert result == ("nearer", "human_turn", True, _dt(8) + layer1.timedelta(seconds=layer1.HUMAN_TURN_RADIUS_SEC))


def test_distant_human_turn_does_not_claim_orca_activity():
    result = layer1._resolve_attribution(
        {"app": "Orca", "title": "Orca"},
        _dt(5),
        [],
        [(datetime(2026, 9, 1, 9, 55, tzinfo=timezone.utc), "distant")],
    )
    assert result == ("orca", "application", False, None)


def test_tmux_focus_never_claims_another_foreground_app():
    result = layer1._resolve_attribution(
        {"app": "ChatApp", "title": "ChatApp"},
        _dt(5),
        [(_dt(0), _dt(10), "background-project")],
        [],
    )
    assert result == ("chatapp", "application", False, None)


def test_human_turn_never_claims_another_foreground_app():
    result = layer1._resolve_attribution(
        {"app": "ChatApp", "title": "ChatApp"},
        _dt(5),
        [],
        [(_dt(5), "background-project")],
    )
    assert result == ("chatapp", "application", False, None)


def test_explicit_legacy_terminal_title_beats_human_turn():
    result = layer1._resolve_attribution(
        {"app": "iTerm2", "title": "claude_grid_demo"},
        _dt(5),
        [],
        [(_dt(5), "other-project")],
    )
    assert result == ("demo", "window", False, None)


def test_legacy_hyphen_terminal_title_extracts_project():
    result = layer1._resolve_attribution(
        {"app": "iTerm2", "title": "claude-projectd"},
        _dt(5),
        [],
        [],
    )
    assert result == ("projectd", "window", False, None)


def test_equal_distance_human_tie_prefers_later_turn_deterministically():
    result = layer1._resolve_attribution(
        {"app": "Orca", "title": "Orca"},
        _dt(5),
        [],
        [(_dt(0), "before"), (_dt(10), "after")],
    )
    assert result == ("after", "human_turn", True, _dt(10) + layer1.timedelta(seconds=layer1.HUMAN_TURN_RADIUS_SEC))


def test_build_segments_splits_when_focus_project_changes(monkeypatch):
    monkeypatch.setattr(
        layer1,
        "_FOCUS_TIMELINE",
        [(_dt(0), _dt(30), "alpha"), (_dt(30), _dt(59), "beta")],
    )
    monkeypatch.setattr(layer1, "_HUMAN_TURNS", [])
    window = [{
        "timestamp": _dt(0).isoformat().replace("+00:00", "Z"),
        "duration": 60,
        "data": {"app": "Orca", "title": "Orca"},
    }]
    afk = [{
        "timestamp": _dt(0).isoformat().replace("+00:00", "Z"),
        "duration": 60,
        "data": {"status": "not-afk"},
    }]

    segments = layer1.build_segments(window, afk)

    # Focus evidence covers 0-59s of a 60s event; the 1s tail past the evidence
    # falls back to the application and is kept, so the event stays conserved.
    assert [segment["data"]["project"] for segment in segments] == ["alpha", "beta", "orca"]
    assert [segment["data"]["attribution_source"] for segment in segments] == [
        "tmux_focus", "tmux_focus", "application",
    ]
    assert sum(segment["duration"] for segment in segments) == 60


def test_build_segments_splits_at_focus_evidence_boundary_mid_probe(monkeypatch):
    # A 10s focus sample at t=0 is only trustworthy through t=20 (FOCUS_SAMPLE_LIMIT_SEC=10),
    # well inside the first 30s probe window — the resulting slice must not run past it.
    monkeypatch.setattr(layer1, "_FOCUS_TIMELINE", [(_dt(0), _dt(20), "alpha")])
    monkeypatch.setattr(layer1, "_HUMAN_TURNS", [])
    window = [{
        "timestamp": _dt(0).isoformat().replace("+00:00", "Z"),
        "duration": 30,
        "data": {"app": "Orca", "title": "Orca"},
    }]
    afk = [{
        "timestamp": _dt(0).isoformat().replace("+00:00", "Z"),
        "duration": 30,
        "data": {"status": "not-afk"},
    }]

    segments = layer1.build_segments(window, afk)

    assert segments[0]["data"]["project"] == "alpha"
    assert segments[0]["duration"] == 20
    assert segments[1]["data"]["attribution_source"] != "tmux_focus"


def test_human_turn_evidence_end_is_measured_from_the_turn_not_the_probe():
    """Probes later in the same turn window must not push the evidence end out."""
    turn = _dt(0)
    expected = turn + layer1.timedelta(seconds=layer1.HUMAN_TURN_RADIUS_SEC)
    for offset in (0, 30, 60, 90, 119):
        result = layer1._resolve_attribution(
            {"app": "Orca", "title": "Orca"},
            turn + layer1.timedelta(seconds=offset),
            [],
            [(turn, "Client A")],
        )
        assert result == ("Client A", "human_turn", False, expected), offset


def test_build_segments_stops_human_turn_slice_at_the_evidence_edge(monkeypatch):
    """A silent Orca stretch must not be attributed past the turn's 120s window."""
    turn = _dt(0)
    monkeypatch.setattr(layer1, "_FOCUS_TIMELINE", [])
    monkeypatch.setattr(layer1, "_HUMAN_TURNS", [(turn, "Client A")])
    window = [{
        "timestamp": turn.isoformat().replace("+00:00", "Z"),
        "duration": 600,
        "data": {"app": "Orca", "title": "Orca"},
    }]
    afk = [{
        "timestamp": turn.isoformat().replace("+00:00", "Z"),
        "duration": 600,
        "data": {"status": "not-afk"},
    }]

    segments = layer1.build_segments(window, afk)

    human = [s for s in segments if s["data"]["attribution_source"] == "human_turn"]
    assert human, segments
    attributed = sum(s["duration"] for s in human)
    assert attributed == layer1.HUMAN_TURN_RADIUS_SEC, segments


def test_evidence_boundary_tail_is_not_dropped_as_noise(monkeypatch):
    """A sub-MIN_SEGMENT_SEC tail left by the boundary probe must not vanish."""
    monkeypatch.setattr(layer1, "_FOCUS_TIMELINE", [(_dt(0), _dt(0) + layer1.timedelta(seconds=28), "alpha")])
    monkeypatch.setattr(layer1, "_HUMAN_TURNS", [])
    event = lambda data: {
        "timestamp": _dt(0).isoformat().replace("+00:00", "Z"),
        "duration": 30,
        "data": data,
    }

    segments = layer1.build_segments(
        [event({"app": "Orca", "title": "Orca"})],
        [event({"status": "not-afk"})],
    )

    assert sum(s["duration"] for s in segments) == 30, segments
    assert [s["duration"] for s in segments] == [28, 2], segments


def test_tiny_standalone_event_is_still_dropped_as_noise(monkeypatch):
    """The noise filter must survive: a 2s event with no boundary stays dropped."""
    monkeypatch.setattr(layer1, "_FOCUS_TIMELINE", [])
    monkeypatch.setattr(layer1, "_HUMAN_TURNS", [])
    event = lambda data: {
        "timestamp": _dt(0).isoformat().replace("+00:00", "Z"),
        "duration": 2,
        "data": data,
    }

    segments = layer1.build_segments(
        [event({"app": "Orca", "title": "Orca"})],
        [event({"status": "not-afk"})],
    )

    assert segments == []


def test_ordinary_state_split_also_keeps_its_short_tail(monkeypatch):
    """Conservation is not special-cased to evidence boundaries."""
    monkeypatch.setattr(layer1, "_FOCUS_TIMELINE", [])
    monkeypatch.setattr(layer1, "_HUMAN_TURNS", [])
    window = [{
        "timestamp": _dt(0).isoformat().replace("+00:00", "Z"),
        "duration": 32,
        "data": {"app": "Orca", "title": "Orca"},
    }]
    afk = [
        {
            "timestamp": _dt(0).isoformat().replace("+00:00", "Z"),
            "duration": 5,
            "data": {"status": "not-afk"},
        },
        {
            "timestamp": (_dt(0) + layer1.timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
            "duration": 27,
            "data": {"status": "afk"},
        },
    ]

    segments = layer1.build_segments(window, afk)

    assert len(segments) > 1, segments
    assert sum(s["duration"] for s in segments) == 32, segments
