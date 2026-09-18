"""Tests for pure classification helpers in scripts/aggregate.py.

Covers:
  - is_coding(category_path)
  - umbrella(category_path)
  - is_shell_noise(project)
  - _is_noise_project(project)
  - categorize_project(project) — caches monkeypatched so no filesystem hit
"""
import re

import pytest

import aggregate
from aggregate import (
    _is_noise_project,
    categorize_project,
    is_coding,
    is_shell_noise,
    umbrella,
)


# ── is_coding ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "path, expected",
    [
        ([], False),
        (["Messages"], False),                     # not in CODING_TOP_ROOTS
        (["Browser"], False),
        (["Notes", "NotesApp"], False),
        (["Work"], True),                       # top-level Work is coding
        (["Work", "Client A"], True),
        (["Work", "Client A", "trainer"], True),
        # CODING_EXCLUDE_UMBRELLAS — even though under Work, not coding
        (["Work", "Editing"], False),
        (["Work", "Editing", "VS Code"], False),
        (["Work", "Terminal (other)"], False),
        (["Work", "Terminal (other)", "Warp"], False),
        (["Work", "AI tools"], False),
        (["Work", "AI tools", "Claude Desktop"], False),
    ],
)
def test_is_coding(path, expected):
    assert is_coding(path) is expected


# ── umbrella ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "path, expected",
    [
        ([], "?"),
        (["Work"], "Work"),
        (["Messages"], "Messages"),
        (["Work", "Client A"], "Client A"),
        (["Work", "Client A", "trainer"], "Client A"),    # always 2nd element
        (["Messages", "ChatApp"], "ChatApp"),
        (["Uncategorized", "foo"], "foo"),
    ],
)
def test_umbrella(path, expected):
    assert umbrella(path) == expected


# ── is_shell_noise ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "project, expected",
    [
        # Empty / clean
        ("", False),
        ("vim", False),
        ("my-project", False),
        ("Chrome", False),
        ("VS Code", False),
        ("obsidian", False),
        ("project-c", False),
        ("git", False),                       # bare command w/o arg — pattern needs trailing space
        # tmux / whitespace markers
        ("* task", True),
        ("✳ foo", True),
        (" hello", True),
        ("\ttabbed", True),
        # ENV var assignment
        ("FOO=bar", True),
        ("_PRIVATE=1", True),
        # Raw filesystem path — note the ^/[a-z] regex only matches lowercase
        # after the slash, so /tmp matches but /Users/... does NOT (quirk).
        ("/tmp", True),
        ("/users/example/foo", True),
        ("/Users/example/foo", False),   # capital U after / — pattern misses it
        # Shell command prefixes
        ("sudo rm -rf /", True),
        ("brew install", True),
        ("npm install", True),
        ("pnpm run", True),
        ("yarn add", True),
        ("git status", True),
        ("gh pr create", True),
        ("curl http://example.com", True),
        ("ssh host", True),
        ("gws something", True),
        ("kubectl get", True),
        # user@host shell prompts
        ("developer@host", True),
        ("user@server", True),
    ],
)
def test_is_shell_noise(project, expected):
    assert is_shell_noise(project) is expected


# ── _is_noise_project ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "project, expected",
    [
        # Falsy / sentinel — always noise
        (None, True),
        ("", True),
        ("unknown", True),
        ("?", True),
        # Delegates to is_shell_noise
        ("* foo", True),
        ("FOO=bar", True),
        ("/etc/passwd", True),
        ("developer@host", True),
        # Real projects
        ("vim", False),
        ("project-c", False),
        ("VS Code", False),
        ("project-a", False),
    ],
)
def test_is_noise_project(project, expected):
    assert _is_noise_project(project) is expected


# ── categorize_project ──────────────────────────────────────────────────────
# The function consults two module-level caches (_CLASSES_CACHE and
# _FS_PROJECT_MAP); we monkeypatch both to keep the test offline & deterministic.

@pytest.fixture
def reset_caches():
    """Save & restore the categorize_project caches around each test."""
    saved_classes = aggregate._CLASSES_CACHE
    saved_fs = aggregate._FS_PROJECT_MAP
    aggregate._CLASSES_CACHE = None
    aggregate._FS_PROJECT_MAP = None
    yield
    aggregate._CLASSES_CACHE = saved_classes
    aggregate._FS_PROJECT_MAP = saved_fs


def _set_classes(classes):
    """Pre-populate the regex cache (already sorted as _load_classes would)."""
    aggregate._CLASSES_CACHE = [
        (name, re.compile(rx, re.I)) for name, rx in classes
    ]


def _set_fs_map(d):
    aggregate._FS_PROJECT_MAP = dict(d)


def test_categorize_uncategorized_when_no_match(reset_caches):
    _set_classes([])
    _set_fs_map({})
    assert categorize_project("totally-unknown") == ["Uncategorized", "totally-unknown"]


def test_categorize_uncategorized_when_caches_empty(reset_caches):
    # Both caches empty/None is the no-config edge case; must still return a path
    _set_classes([])
    _set_fs_map({})
    assert categorize_project("anything") == ["Uncategorized", "anything"]


def test_categorize_regex_match_wins(reset_caches):
    _set_classes([
        (["Work", "Client A", "trainer"], r"claude_(grid_)?project-a$|project-a"),
    ])
    _set_fs_map({"project-a": "MyUmbrella"})  # fs-map present but regex must win
    assert categorize_project("project-a") == ["Work", "Client A", "trainer"]


def test_categorize_fs_map_used_when_no_regex_match(reset_caches):
    _set_classes([])
    _set_fs_map({"myrepo": "Client A"})
    assert categorize_project("myrepo") == ["Work", "Client A", "myrepo"]


def test_categorize_fs_map_miss_falls_through_to_uncategorized(reset_caches):
    _set_classes([])
    _set_fs_map({"otherrepo": "X"})
    assert categorize_project("not-in-map") == ["Uncategorized", "not-in-map"]


def test_categorize_first_regex_in_cache_wins(reset_caches):
    """categorize_project iterates the cache in order; the first match wins.
    _load_classes sorts by depth desc — we mimic that ordering here."""
    _set_classes([
        (["Work", "Client B", "project-b"], r"project-b"),                  # deeper, listed first
        (["Work", "Client B"], r"project-b"),                             # shallower, would also match
    ])
    assert categorize_project("project-b") == ["Work", "Client B", "project-b"]


def test_categorize_regex_is_case_insensitive(reset_caches):
    _set_classes([(["Browser"], r"chrome")])
    _set_fs_map({})
    # categories.json uses re.I — confirm the cache we build mirrors that
    assert categorize_project("Google Chrome") == ["Browser"]
    assert categorize_project("CHROME") == ["Browser"]
