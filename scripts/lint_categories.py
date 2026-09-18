#!/usr/bin/env python3
"""lint_categories.py — validate categories.json + smoke-test on real data.

Checks:
  1. Duplicate IDs (the bug that hid Client A under Editing>VS Code).
  2. Duplicate category-name paths.
  3. Invalid regex (re.compile fails).
  4. Suspiciously broad regex (empty / `.*`).
  5. Ambiguous matches at the same depth — two depth-N categories matching
     the same sample → AW UI tie-breaks unpredictably.
  6. Coverage: which project names from the last 7 days end up Uncategorized.

Exit code:
  0 = clean
  1 = warnings only (e.g. uncategorized projects, ambiguous matches)
  2 = errors (dup IDs / dup names / invalid regex) — block CI / pre-commit

CLI:
  python3 lint_categories.py                  # full lint + 7-day coverage
  python3 lint_categories.py --no-data        # skip live-data probe (faster)
  python3 lint_categories.py --strict         # warnings → errors (exit 2)
  python3 lint_categories.py --days 30        # change coverage window
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib_aw import AW_HOST, TRACKER_CONFIG, bucket_name
CATEGORIES_FILE = TRACKER_CONFIG.categories_file

# Color codes (skip on non-TTY)
def _c(code: str) -> str:
    return code if sys.stderr.isatty() else ""

RED = _c("\033[31m")
YELLOW = _c("\033[33m")
GREEN = _c("\033[32m")
CYAN = _c("\033[36m")
DIM = _c("\033[2m")
RESET = _c("\033[0m")


def _err(msg: str) -> None:
    print(f"{RED}✗ ERROR{RESET}   {msg}", file=sys.stderr)


def _warn(msg: str) -> None:
    print(f"{YELLOW}⚠ WARN{RESET}    {msg}", file=sys.stderr)


def _ok(msg: str) -> None:
    print(f"{GREEN}✓{RESET}        {msg}", file=sys.stderr)


def _info(msg: str) -> None:
    print(f"{CYAN}ℹ{RESET}        {msg}", file=sys.stderr)


# ── checks ────────────────────────────────────────────────────────────────────
def check_duplicate_ids(cats: list[dict]) -> int:
    """Error if any two categories share an `id`."""
    errors = 0
    counts = Counter(c["id"] for c in cats)
    dups = {i: n for i, n in counts.items() if n > 1}
    if not dups:
        _ok(f"no duplicate IDs ({len(cats)} categories)")
        return 0
    for dup_id, n in dups.items():
        offenders = [" > ".join(c["name"]) for c in cats if c["id"] == dup_id]
        _err(f"id={dup_id} used {n} times: {offenders}")
        errors += 1
    return errors


def check_duplicate_names(cats: list[dict]) -> int:
    """Error if two categories have identical name paths."""
    errors = 0
    by_path = defaultdict(list)
    for c in cats:
        by_path[tuple(c["name"])].append(c["id"])
    for path, ids in by_path.items():
        if len(ids) > 1:
            _err(f"name {list(path)!r} defined by {len(ids)} entries (ids={ids})")
            errors += 1
    if not errors:
        _ok("no duplicate category-name paths")
    return errors


def check_regexes(cats: list[dict]) -> tuple[int, int, list[tuple[list[str], re.Pattern]]]:
    """Compile each regex. Return (errors, warnings, [(name, compiled)])."""
    errors = warnings = 0
    compiled: list[tuple[list[str], re.Pattern]] = []
    for c in cats:
        rule = c.get("rule") or {}
        if rule.get("type") != "regex":
            continue
        rx = rule.get("regex", "")
        if not rx:
            _err(f"empty regex in {' > '.join(c['name'])} (id={c['id']})")
            errors += 1
            continue
        try:
            p = re.compile(rx, re.I)
        except re.error as e:
            _err(f"invalid regex in {' > '.join(c['name'])} (id={c['id']}): {rx!r} — {e}")
            errors += 1
            continue
        # Suspiciously broad
        if rx in (".*", ".+", "^.*$", ".*?"):
            _warn(f"overly broad regex {rx!r} in {' > '.join(c['name'])} (id={c['id']})")
            warnings += 1
        compiled.append((c["name"], p))
    if not errors:
        _ok(f"all regexes compile ({len(compiled)} active)")
    return errors, warnings, compiled


def check_ambiguous_at_same_depth(
    cats: list[dict],
    compiled: list[tuple[list[str], re.Pattern]],
    probe_strings: list[str],
) -> int:
    """Warn if two categories at the same depth both match the same probe.
    AW UI picks one unpredictably."""
    warnings = 0
    seen_pairs = set()
    for probe in probe_strings:
        matches = [(name, p) for name, p in compiled if p.search(probe)]
        if len(matches) < 2:
            continue
        # Group by depth
        by_depth = defaultdict(list)
        for name, _p in matches:
            by_depth[len(name)].append(name)
        for depth, names in by_depth.items():
            if len(names) < 2:
                continue
            pair = tuple(sorted(" > ".join(n) for n in names))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            _warn(
                f"probe {probe!r} matches {len(names)} categories at depth {depth}: "
                f"{[' > '.join(n) for n in names]}"
            )
            warnings += 1
    if not warnings:
        _ok("no ambiguous matches at same depth")
    return warnings


def check_coverage(
    compiled: list[tuple[list[str], re.Pattern]],
    days: int,
) -> tuple[int, list[str]]:
    """Pull project names from the last `days` of the configured states bucket; report uncategorized."""
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        url = (
            f"{AW_HOST}/api/0/buckets/{bucket_name('states')}/events"
            f"?start={start.isoformat().replace('+00:00','Z')}"
            f"&end={end.isoformat().replace('+00:00','Z')}&limit=10000"
        )
        evs = json.load(urllib.request.urlopen(url, timeout=5))
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        _warn(f"could not query AW for coverage data: {e}")
        return 0, []

    projects: dict[str, float] = defaultdict(float)
    for ev in evs:
        if ev["data"].get("state") not in ("ACTIVE_EDIT", "ACTIVE_REVIEW"):
            continue
        p = ev["data"].get("project", "")
        if p:
            projects[p] += ev["duration"]

    # Sort deepest-first like AW does
    rules = sorted(compiled, key=lambda x: -len(x[0]))

    def classify(s: str):
        for name, pat in rules:
            if pat.search(s):
                return name
        return None

    uncat = []
    for p, secs in sorted(projects.items(), key=lambda x: -x[1]):
        if not classify(p):
            uncat.append((p, secs))

    warnings = 0
    if uncat:
        total = sum(s for _, s in uncat) / 60
        _warn(
            f"{len(uncat)} project names uncategorized over last {days}d "
            f"(total {int(total)}m of refined Layer-1 time):"
        )
        for p, s in uncat[:15]:
            print(f"             {DIM}- {p:<35s} {int(s/60)}m{RESET}", file=sys.stderr)
        if len(uncat) > 15:
            print(f"             {DIM}… and {len(uncat) - 15} more{RESET}", file=sys.stderr)
        warnings += 1
    else:
        _ok(f"all project names covered over last {days}d ({len(projects)} unique)")
    return warnings, [p for p, _ in uncat]


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--no-data", action="store_true", help="skip live AW coverage probe")
    p.add_argument("--days", type=int, default=7, help="coverage window in days (default 7)")
    p.add_argument("--strict", action="store_true", help="treat warnings as errors")
    p.add_argument("--file", default=str(CATEGORIES_FILE), help="categories.json path")
    args = p.parse_args()

    print(f"\n{CYAN}== lint_categories.py == {args.file}{RESET}\n", file=sys.stderr)

    try:
        cats = json.load(open(args.file))["categories"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        _err(f"cannot parse {args.file}: {e}")
        return 2

    errors = 0
    warnings = 0

    errors += check_duplicate_ids(cats)
    errors += check_duplicate_names(cats)
    e, w, compiled = check_regexes(cats)
    errors += e
    warnings += w

    # Always test against known-good fixtures (regression: project-a bug)
    fixtures = [
        "project-a", "project-b", "project-c", "project-a-bot",
        "claude_demo", "claude_grid_demo", "Cursor", "Code", "VS Code",
        "aw-agent-time", "project-core",
    ]
    warnings += check_ambiguous_at_same_depth(cats, compiled, fixtures)

    if not args.no_data:
        cov_warn, _ = check_coverage(compiled, args.days)
        warnings += cov_warn

    print(file=sys.stderr)
    if errors:
        _err(f"FAIL — {errors} error(s), {warnings} warning(s)")
        return 2
    if warnings and args.strict:
        _err(f"FAIL (strict) — {warnings} warning(s)")
        return 2
    if warnings:
        _warn(f"PASS with {warnings} warning(s)")
        return 1
    _ok("CLEAN — no errors, no warnings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
