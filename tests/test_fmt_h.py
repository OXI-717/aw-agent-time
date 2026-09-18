import pytest

from aggregate import fmt_h


@pytest.mark.parametrize(
    "secs, expected",
    [
        # Sub-minute — collapses to 0m
        (0, "0m"),
        (30, "0m"),
        (59, "0m"),
        (59.9, "0m"),  # float truncated by int()
        # Minutes (< 1h)
        (60, "1m"),
        (125, "2m"),
        (3599, "59m"),
        # Hour boundary
        (3600, "1h00m"),
        (3661, "1h01m"),
        # Hour + minutes
        (5400, "1h30m"),
        (36000, "10h00m"),
        (39959, "11h05m"),
    ],
)
def test_fmt_h(secs, expected):
    assert fmt_h(secs) == expected


def test_fmt_h_accepts_int_and_float():
    assert fmt_h(60) == fmt_h(60.0) == "1m"
