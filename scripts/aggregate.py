#!/usr/bin/env python3
"""aggregate.py — daily summary across all three tracker layers.

Runs the three trackers (or reads their AW buckets) and emits a markdown summary
to the configured report folder.

Designed to be safe to re-run any number of times for the same day.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib_aw import AW_HOST, HOSTNAME, TRACKER_CONFIG, bucket_name, get_events, iso


# ── umbrella project categorization ─────────────────────────────────────────
CATEGORIES_FILE = TRACKER_CONFIG.categories_file

# Roots considered "coding work". Everything else (Messages/Browser/Notes/Media/System)
# rolls up into "Other activity".
CODING_TOP_ROOTS = {"Work"}
# Within "Work", these umbrellas are NOT coding (editing tools, IDE itself, etc).
CODING_EXCLUDE_UMBRELLAS = {"Editing", "Terminal (other)", "AI tools"}


def _load_classes() -> list[tuple[list[str], re.Pattern]]:
    try:
        cats = json.load(CATEGORIES_FILE.open())["categories"]
    except Exception:
        return []
    out = []
    for c in cats:
        rule = c.get("rule") or {}
        if rule.get("type") != "regex":
            continue
        try:
            out.append((c["name"], re.compile(rule["regex"], re.I)))
        except re.error:
            continue
    # Sort by depth desc so the deepest match wins (matches AW UI behavior)
    out.sort(key=lambda x: -len(x[0]))
    return out


_CLASSES_CACHE: list[tuple[list[str], re.Pattern]] | None = None


# ── filesystem-derived project map ─────────────────────────────────────────
# Reads _PROJECTS/projects-repos.json (single source of truth shared with
# weekly sync) and walks each path_prefix to build basename → umbrella map.
# This replaces hand-written regexes for every repo/sub-folder.
PROJECTS_MANIFEST = TRACKER_CONFIG.project_manifest
# Generic basenames that occur in many projects (tests, docs, src, ...) — never
# auto-classify them; let regexes or "Uncategorized" handle them. Otherwise
# the last-walked project would silently win and mislabel time.
GENERIC_AMBIGUOUS_NAMES = {
    "tests", "test", "docs", "doc", "src", "lib", "public", "static",
    "build", "dist", "output", "out", "tmp", "_tmp", "data", "config",
    "scripts", "examples", "samples", "research", "research_output",
    "demos", "demo", "shared", "common", "utils", "internal", "vendor",
    "packages", "apps", "app", "frontend", "backend", "infra", "infrastructure",
    "ui", "api", "core", "main", "private", "node_modules", "templates",
    "state", "launchd", "hooks", "assets", "archive", "audits", "obsidian",
    "inbox", "context", "tools", "domains", "branding", "fixtures",
    "credentials", "agent-skill", "skill", "factory",
}
# Skip path_prefixes that are too broad to walk (vault root has hundreds of
# unrelated dated folders we don't want to attribute to VAULT umbrella).
FS_NO_WALK_PREFIXES: set[str] = {str(p) for p in TRACKER_CONFIG.excluded_scan_roots}
_DATELIKE_RE = re.compile(r"^\d{4}[-_]\d{1,2}([-_]\d{1,2})?")
# Manifest project_key → display name shown in time reports.
# If not listed, project_key is used as-is.
UMBRELLA_DISPLAY = TRACKER_CONFIG.project_umbrellas

# Target weekly portfolio allocation from issue #8. These are independent
# target/cap shares of total coding time, so their approximate total is not
# normalized to 100%.
PORTFOLIO_TARGETS = TRACKER_CONFIG.portfolio_targets

PORTFOLIO_GROUPS = TRACKER_CONFIG.portfolio_groups

_FS_PROJECT_MAP: dict[str, str] | None = None


def _build_fs_project_map() -> dict[str, str]:
    """Build basename → umbrella display name map from projects-repos.json.

    Strategy: walk only direct children of each path_prefix (depth 1). The
    AW window watcher reports the current cwd's basename — which for normal
    work is a git repo directly under the project's parent folder.

    Conflict policy: if the same basename appears under multiple umbrellas
    (e.g. "auth" exists in both Client B and Client A), drop it entirely. Better
    Uncategorized than mislabeled.

    Skipped: generic names (tests/src/docs/...), dotted dirs, build artifacts,
    date-prefixed folders (`2026-04-30-foo`), and overly broad prefixes like
    the vault root.
    """
    try:
        if PROJECTS_MANIFEST is None:
            return {}
        data = json.loads(PROJECTS_MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {}

    # basename → set of umbrellas claiming it
    claims: dict[str, set[str]] = defaultdict(set)
    for key, meta in data.get("projects", {}).items():
        display = UMBRELLA_DISPLAY.get(key, key)
        for prefix in meta.get("path_prefixes", []):
            root = Path(prefix).expanduser()
            if not root.is_absolute():
                root = PROJECTS_MANIFEST.parent / root
            if any(root.is_relative_to(Path(p)) for p in FS_NO_WALK_PREFIXES):
                continue
            if not root.exists():
                continue
            # Root basename itself
            claims[root.name].add(display)
            # Direct children only
            try:
                entries = list(root.iterdir())
            except (PermissionError, OSError):
                continue
            for sub in entries:
                if any(sub.is_relative_to(Path(p)) for p in FS_NO_WALK_PREFIXES):
                    continue
                if not sub.is_dir():
                    continue
                name = sub.name
                if name.startswith(".") or name.startswith("_"):
                    continue
                if name in GENERIC_AMBIGUOUS_NAMES:
                    continue
                if _DATELIKE_RE.match(name):
                    continue
                claims[name].add(display)

    # Resolve: unique-owner → keep; multi-owner → drop (ambiguous).
    return {n: next(iter(u)) for n, u in claims.items() if len(u) == 1}


def _fs_project_map() -> dict[str, str]:
    global _FS_PROJECT_MAP
    if _FS_PROJECT_MAP is None:
        _FS_PROJECT_MAP = _build_fs_project_map()
    return _FS_PROJECT_MAP


def categorize_project(project: str) -> list[str]:
    """Map a project name to its full category path.

    Order:
      1) categories.json regex (for system apps, browsers, IDE, etc.)
      2) Filesystem-derived map from projects-repos.json (auto-discovered repos)
      3) Fallback: ['Uncategorized', project]
    """
    global _CLASSES_CACHE
    if _CLASSES_CACHE is None:
        _CLASSES_CACHE = _load_classes()
    for name, pat in _CLASSES_CACHE:
        if pat.search(project):
            return name
    # Auto-derived from filesystem walk
    fs_map = _fs_project_map()
    if project in fs_map:
        return ["Work", fs_map[project], project]
    return ["Uncategorized", project]


def is_coding(category_path: list[str]) -> bool:
    if not category_path:
        return False
    if category_path[0] not in CODING_TOP_ROOTS:
        return False
    if len(category_path) >= 2 and category_path[1] in CODING_EXCLUDE_UMBRELLAS:
        return False
    return True


def umbrella(category_path: list[str]) -> str:
    """The 2nd-level group: 'Work > Client A > trainer' → 'Client A'.
    For non-Work or shallow paths, use the top level."""
    if len(category_path) >= 2:
        return category_path[1]
    return category_path[0] if category_path else "?"


# Tmux pane titles / shell env vars / command lines bleed into the "project"
# field when the window watcher couldn't infer a real project. Filter them out.
SHELL_NOISE_PATTERNS = [
    re.compile(r"^[✳✶✱✻*\s]"),        # tmux task indicators
    re.compile(r"[A-Z_]+=\S"),         # env var assignment
    re.compile(r"^/[a-z]"),            # raw filesystem paths
    re.compile(r"^(sudo |brew |npm |pnpm |yarn |git |gh |curl |ssh |gws |kubectl )"),
    re.compile(r"^[a-z]+@[a-z]+"),     # user@host shell prompts
]

def is_shell_noise(project: str) -> bool:
    return any(p.search(project) for p in SHELL_NOISE_PATTERNS)

SCRIPT_DIR = Path(__file__).parent
OBSIDIAN_FOLDER = TRACKER_CONFIG.obsidian_folder


def _run(script: str, day: datetime, dry: bool) -> int:
    """Run one tracker for `day`. Returns its exit code (0 = success)."""
    cmd = [sys.executable, str(SCRIPT_DIR / script), "--date", day.strftime("%Y-%m-%d")]
    if dry:
        cmd.append("--dry-run")
    return subprocess.run(cmd, check=False).returncode


def fmt_h(secs: float) -> str:
    h, m = divmod(int(secs) // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


# The NotesApp callout that flags an open (still-accumulating) day. It doubles
# as the staleness marker for finalize_day: a *closed* window whose report file
# still carries it was last drawn before the window closed and needs a redraw.
# The «Как читать» footer quotes the callout title in guillemets, so the marker
# must stay callout-shaped (`[!warning] ...`) to never match a closed file.
OPEN_DAY_CALLOUT = "[!warning] День не закрыт"


def day_completeness(
    day: datetime, now: datetime | None = None
) -> tuple[bool, float, datetime]:
    """Is the report day still open? Return (is_open, uncovered_secs, window_end).

    Every summariser here queries the UTC midnight..midnight window of `day`, so
    a day only stops accumulating events once `now` passes that UTC edge — which
    in local time is *not* local midnight (for MSK it is 03:00 the next day).
    Until then the file is a snapshot: the 30-minute launchd run keeps extending
    it, and the totals a reader sees in the evening are 2-3h short of the day's
    real figure (issue #15). Callers use this to say so out loud.
    """
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    uncovered = (end - now).total_seconds()
    return uncovered > 0, max(0.0, uncovered), end


def summarise_states(day: datetime) -> dict[str, dict[str, float]]:
    """Return {state: {project: seconds}} from oxi-states bucket."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    events = get_events(bucket_name("states"), start, end)
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for ev in events:
        s = ev["data"].get("state", "?")
        p = ev["data"].get("project", "unknown")
        out[s][p] += ev["duration"]
    return out


def summarise_engagement(day: datetime) -> dict[tuple[str, str], float]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    events = get_events(bucket_name("engagement"), start, end)
    out: dict[tuple[str, str], float] = defaultdict(float)
    for ev in events:
        out[(ev["data"].get("project", "?"), ev["data"].get("engine", "?"))] += ev["duration"]
    return out


def summarise_autonomous(day: datetime) -> dict[tuple[str, str], float]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    events = get_events(bucket_name("autonomous"), start, end)
    out: dict[tuple[str, str], float] = defaultdict(float)
    for ev in events:
        out[(ev["data"].get("project", "?"), ev["data"].get("engine", "?"))] += ev["duration"]
    return out


def _is_noise_project(project: str) -> bool:
    """Drop AW events whose project is meaningless: 'unknown' (window watcher
    failed to tag), or shell-pane / env-var leakage."""
    if not project or project == "unknown" or project == "?":
        return True
    return is_shell_noise(project)


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> float:
    """Sum of merged interval lengths (seconds). Resolves overlapping events
    so the union of Layer 1 ACTIVE + Layer 2 engagement isn't double-counted."""
    if not intervals:
        return 0.0
    intervals = sorted(intervals)
    total = 0.0
    cur_s, cur_e = intervals[0]
    for s, e in intervals[1:]:
        if s <= cur_e:
            if e > cur_e:
                cur_e = e
        else:
            total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
    total += (cur_e - cur_s).total_seconds()
    return total


def _layer1_active_intervals(day: datetime) -> list[tuple[datetime, datetime, str]]:
    """Return Layer 1 ACTIVE_EDIT + ACTIVE_REVIEW intervals as (start, end, project).
    Noise projects (unknown / shell artefacts) are kept but tagged with empty
    project so they can still contribute to wall-clock total but get re-attributed
    by Layer 2 if possible."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    out: list[tuple[datetime, datetime, str]] = []
    for ev in get_events(bucket_name("states"), start, end):
        if ev["data"].get("state") not in ("ACTIVE_EDIT", "ACTIVE_REVIEW"):
            continue
        project = ev["data"].get("project", "")
        if _is_noise_project(project):
            project = ""  # eligible for L2 re-attribution
        ts = datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00"))
        out.append((ts, ts + timedelta(seconds=ev["duration"]), project))
    return out


def _layer2_engagement_spans(day: datetime) -> list[tuple[datetime, datetime, str]]:
    """Return Layer 2 (Claude/Codex conversation) intervals as (start, end, project)."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    out: list[tuple[datetime, datetime, str]] = []
    for ev in get_events(bucket_name("engagement"), start, end):
        project = ev["data"].get("project", "")
        if _is_noise_project(project):
            continue
        ts = datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00"))
        out.append((ts, ts + timedelta(seconds=ev["duration"]), project))
    out.sort()
    return out


def _find_l2_project(t: datetime, l2_spans: list[tuple[datetime, datetime, str]]) -> str:
    """Linear-scan: which Layer 2 span covers time t? Empty string if none."""
    for s, e, p in l2_spans:
        if s <= t < e:
            return p
        if s > t:
            return ""
    return ""


def summarise_by_umbrella(day: datetime) -> dict[str, dict[str, dict[str, float]]]:
    """Group the project attribution already resolved by Layer 1.

    Layer 2 is deliberately not consulted here: direct Orca/tmux focus evidence
    must never be overwritten by a concurrent background conversation.
    """
    l1 = _layer1_active_intervals(day)

    by_project: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for start, end, project in l1:
        if project:
            by_project[project].append((start, end))

    out: dict[str, dict[str, dict[str, float]]] = {
        "coding": defaultdict(lambda: defaultdict(float)),
        "other": defaultdict(lambda: defaultdict(float)),
    }
    for project, intervals in by_project.items():
        merged_secs = _merge_intervals(intervals)
        if merged_secs < 1:
            continue
        cat = categorize_project(project)
        bucket = "coding" if is_coding(cat) else "other"
        out[bucket][umbrella(cat)][project] += merged_secs
    return out


def wallclock_work_seconds(day: datetime) -> float:
    """Wall-clock work = Layer 1 ACTIVE intervals union. This is the real keyboard
    activity time. Layer 2 is NOT added — it only re-attributes."""
    intervals = [(s, e) for s, e, _p in _layer1_active_intervals(day)]
    return _merge_intervals(intervals)


def engaged_with_ai_seconds(day: datetime) -> float:
    """Total time in human-anchored Claude/Codex interaction blocks."""
    intervals = [(s, e) for s, e, _p in _layer2_engagement_spans(day)]
    return _merge_intervals(intervals)


def summarise_attribution(day: datetime) -> dict[str, float]:
    """Return union seconds by Layer 1 evidence class and ambiguity."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    groups: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    all_active: list[tuple[datetime, datetime]] = []
    ambiguous: list[tuple[datetime, datetime]] = []
    for event in get_events(bucket_name("states"), start, end):
        data = event.get("data") or {}
        if data.get("state") not in ("ACTIVE_EDIT", "ACTIVE_REVIEW"):
            continue
        event_start = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        event_end = event_start + timedelta(seconds=float(event.get("duration") or 0))
        interval = (event_start, event_end)
        all_active.append(interval)
        source = data.get("attribution_source") or "legacy"
        if source in ("tmux_focus", "window"):
            group = "direct"
        elif source == "human_turn":
            group = "inferred"
        elif source == "application":
            group = "application"
        else:
            group = "unattributed"
        groups[group].append(interval)
        if data.get("attribution_ambiguous"):
            ambiguous.append(interval)
    result = {
        name: _merge_intervals(groups.get(name, []))
        for name in ("direct", "inferred", "application", "unattributed")
    }
    result["ambiguous"] = _merge_intervals(ambiguous)
    result["wallclock"] = _merge_intervals(all_active)
    return result


def summarise_daily(start_day: datetime, days: int) -> list[tuple[datetime, dict[str, float], float, float]]:
    """For each day, return (day, {umbrella: seconds}, wallclock_work, other_total).
    wallclock_work = union across all projects (real time worked, ≤ 24h)."""
    rows: list[tuple[datetime, dict[str, float], float, float]] = []
    for n in range(days):
        d = start_day - timedelta(days=days - 1 - n)
        umb = summarise_by_umbrella(d)
        coding_per_umb: dict[str, float] = {}
        for u, projs in umb["coding"].items():
            coding_per_umb[u] = sum(projs.values())
        wallclock = wallclock_work_seconds(d)
        other_total = sum(sum(p.values()) for p in umb["other"].values())
        rows.append((d, coding_per_umb, wallclock, other_total))
    return rows


def portfolio_reconciliation(
    daily_rows: list[tuple[datetime, dict[str, float], float, float]],
    targets: dict[str, float] | None = None,
    groups: dict[str, str] | None = None,
) -> list[tuple[str, float, float, float, float]]:
    """Return weekly actual-vs-target rows.

    Each row is (umbrella, seconds, actual_share, target_share, delta_share).
    Shares are fractions, so 0.25 means 25%.
    """
    target_map = PORTFOLIO_TARGETS if targets is None else targets
    group_map = PORTFOLIO_GROUPS if groups is None else groups
    weekly: dict[str, float] = defaultdict(float)
    for _day, umb_dict, _work_total, _other_total in daily_rows:
        for umb, secs in umb_dict.items():
            weekly[group_map.get(umb, umb)] += secs

    total = sum(weekly.values())
    names = set(weekly) | set(target_map)

    rows: list[tuple[str, float, float, float, float]] = []
    for name in names:
        seconds = weekly.get(name, 0.0)
        actual = seconds / total if total else 0.0
        target = target_map.get(name, 0.0)
        rows.append((name, seconds, actual, target, actual - target))

    return sorted(rows, key=lambda r: (-abs(r[4]), r[0]))


def fmt_pct(share: float) -> str:
    return f"{share * 100:.0f}%"


def fmt_pp(share: float) -> str:
    sign = "+" if share > 0 else ""
    return f"{sign}{share * 100:.0f} п.п."


def summarise_tmux_panes(day: datetime) -> dict[tuple[str, str], float]:
    """Read the single-stream focused tmux bucket."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    events = get_events(f"aw-watcher-tmux-focus_{HOSTNAME}", start, end)
    observations = []
    for event in events:
        data = event.get("data") or {}
        event_start = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        observed_end = event_start + timedelta(seconds=float(event.get("duration") or 0))
        observations.append((event_start, observed_end, data))
    observations.sort(key=lambda item: item[0])
    out: dict[tuple[str, str], float] = defaultdict(float)
    for index, (event_start, observed_end, data) in enumerate(observations):
        event_end = observed_end + timedelta(seconds=10)
        if index + 1 < len(observations):
            event_end = min(event_end, observations[index + 1][0])
        event_start = max(event_start, start)
        event_end = min(event_end, end)
        if event_end <= event_start:
            continue
        key = (data.get("session", "?"), data.get("project", "?"))
        out[key] += (event_end - event_start).total_seconds()
    return out


def render_markdown(
    day: datetime,
    states,
    engagement,
    autonomous,
    panes=None,
    umbrella_today=None,
    daily_rows=None,
    attribution=None,
    now=None,
) -> str:
    date_s = day.strftime("%Y-%m-%d")
    generated_at = (now or datetime.now(timezone.utc)).astimezone()
    lines = [
        f"# Тайм-трекер — {date_s}",
        "",
        f"_Generated by `aw-agent-time/scripts/aggregate.py` at "
        f"{generated_at.strftime('%Y-%m-%d %H:%M:%S')} (local)._",
        "",
    ]

    # ── «день не закрыт» ───────────────────────────────────────────────
    # Without this the reader has no way to tell a finished day from a
    # mid-flight snapshot: both files look identical, so an evening glance at
    # today's file silently under-reports the day by the hours still to come.
    day_is_open, uncovered_secs, window_end = day_completeness(day, now)
    if day_is_open:
        uncovered_secs = min(86400.0, uncovered_secs)
        covered_secs = 86400.0 - uncovered_secs
        close_local = window_end.astimezone()
        lines += [
            f"> {OPEN_DAY_CALLOUT} — данные частичные",
            f"> Учтено до {generated_at.strftime('%H:%M')} (локально): "
            f"{fmt_h(covered_secs)} из 24h суточного окна, "
            f"ещё {fmt_h(uncovered_secs)} не собрано.",
            f"> Файл дописывается каждые 30 минут и закрывается только после "
            f"{close_local.strftime('%H:%M %d.%m')} (локально) — окно дня считается в UTC.",
            "> Итоги ниже ещё вырастут: не оценивай по ним, сколько сделано за день.",
            "",
        ]

    # ── Работа по проектам + Прочая активность ─────────────────────────
    if umbrella_today:
        lines += ["## Работа по проектам", ""]
        coding = umbrella_today.get("coding") or {}
        other_now = umbrella_today.get("other") or {}
        wallclock = wallclock_work_seconds(day)
        engaged = engaged_with_ai_seconds(day)
        coding_secs = sum(sum(p.values()) for p in coding.values())
        other_secs = sum(sum(p.values()) for p in other_now.values())
        # wallclock = union of ALL Layer-1 ACTIVE time. It splits into
        # coding (named projects below) + other (Прочая активность) +
        # a small unattributed remainder (L1 activity with a noise/unknown
        # project that Layer 2 could not re-attribute). Show the balance so
        # the numbers add up visibly and the remainder is never hidden.
        #
        # Note this is a BALANCE, not a directly-measured quantity:
        # coding_secs/other_secs are per-project unions while wallclock is the
        # global union, so overlap between projects (or sub-second slices that
        # summarise_by_umbrella drops at merged_secs<1) can nudge it slightly
        # negative or positive. Clamp at 0 so a tiny drift never renders as a
        # bogus remainder, and only surface it above rounding noise (1 min).
        unattributed = max(0.0, wallclock - coding_secs - other_secs)
        # "Работы нет" only when there is genuinely no keyboard time at all —
        # not merely no *named* project. A day with only unattributed / other
        # keyboard time must still surface the balance.
        if wallclock < 60 and not coding:
            lines.append("_сегодня работы нет_")
        else:
            lines.append(f"**Реальное время за клавиатурой:** {fmt_h(wallclock)}")
            lines.append(f"- В проектах: {fmt_h(coding_secs)}")
            lines.append(f"- Прочая активность: {fmt_h(other_secs)}")
            if unattributed >= 60:
                lines.append(f"- Не атрибутировано: {fmt_h(unattributed)} "
                             "(печать в окне без проекта, диалог с AI не покрыл момент)")
            lines.append(
                f"**В диалоге с AI:** {fmt_h(engaged)}  "
                "(только блоки, подтверждённые пользовательскими ходами)"
            )
            if attribution:
                wallclock_quality = attribution.get("wallclock", 0.0)
                ambiguous = attribution.get("ambiguous", 0.0)
                ambiguous_pct = ambiguous / wallclock_quality if wallclock_quality else 0.0
                lines.append(
                    "**Качество атрибуции:** "
                    f"напрямую {fmt_h(attribution.get('direct', 0.0))}; "
                    f"восстановлено {fmt_h(attribution.get('inferred', 0.0))}; "
                    f"неоднозначно {fmt_h(ambiguous)} ({ambiguous_pct * 100:.0f}%)"
                )
            lines.append("")
            lines.append("_Считается так: время Layer 1 (печать/мышь) распределяется по проектам — "
                         "если в этот момент шёл диалог с Claude/Codex (Layer 2), время приписывается "
                         "тому проекту. Реальное время за клавиатурой = проекты + прочая активность "
                         "(+ неатрибуцированный остаток). Автономные агенты (Layer 2.5) НЕ включены._")
            lines.append("")
            ordered = sorted(coding.items(), key=lambda x: -sum(x[1].values()))
            for umb, projs in ordered:
                u_total = sum(projs.values())
                lines.append(f"### {umb} — {fmt_h(u_total)}")
                for p, secs in sorted(projs.items(), key=lambda x: -x[1]):
                    lines.append(f"- `{p}` — {fmt_h(secs)}")
                lines.append("")

        lines += ["## Прочая активность", ""]
        other = umbrella_today.get("other") or {}
        if not other:
            lines.append("_прочей активности нет_")
        else:
            other_total = sum(sum(p.values()) for p in other.values())
            lines.append(f"**Всего:** {fmt_h(other_total)}")
            lines.append("")
            other_sorted = sorted(other.items(), key=lambda x: -sum(x[1].values()))
            TOP_N = 15
            head, tail = other_sorted[:TOP_N], other_sorted[TOP_N:]
            for umb, projs in head:
                u_total = sum(projs.values())
                items = sorted(projs.items(), key=lambda x: -x[1])
                top = ", ".join(f"{p} {fmt_h(s)}" for p, s in items[:3])
                lines.append(f"- **{umb}** — {fmt_h(u_total)}  ({top})")
            if tail:
                tail_total = sum(sum(p.values()) for _, p in tail)
                names = ", ".join(u for u, _ in tail[:8])
                if len(tail) > 8:
                    names += f", +{len(tail) - 8}"
                lines.append(f"- _+ {len(tail)} мелких ({fmt_h(tail_total)}): {names}_")
            lines.append("")

    # ── Последние 7 дней ───────────────────────────────────────────────
    if daily_rows:
        lines += ["## Последние 7 дней", ""]
        lines += ["| День | Работа | Прочее | Топ проектов |"]
        lines += ["|---|---|---|---|"]
        ru_weekdays = {"Mon": "Пн", "Tue": "Вт", "Wed": "Ср", "Thu": "Чт",
                       "Fri": "Пт", "Sat": "Сб", "Sun": "Вс"}
        ru_months = {"Jan": "янв", "Feb": "фев", "Mar": "мар", "Apr": "апр",
                     "May": "мая", "Jun": "июн", "Jul": "июл", "Aug": "авг",
                     "Sep": "сен", "Oct": "окт", "Nov": "ноя", "Dec": "дек"}
        any_open = False
        for d, umb_dict, c_total, o_total in daily_rows:
            top = sorted(umb_dict.items(), key=lambda x: -x[1])[:4]
            top_str = ", ".join(f"{u} {fmt_h(s)}" for u, s in top if s > 0) or "—"
            wd, dd, mn = d.strftime("%a %d %b").split()
            day_label = f"{ru_weekdays.get(wd, wd)} {dd} {ru_months.get(mn, mn)}"
            # A row whose UTC window has not closed yet is a partial figure that
            # later runs will raise — flag it inline so the table can't be read
            # as seven comparable days.
            if day_completeness(d, now)[0]:
                any_open = True
                day_label += " ⏳"
            lines.append(f"| {day_label} | {fmt_h(c_total)} | {fmt_h(o_total)} | {top_str} |")
        lines.append("")
        if any_open:
            lines.append("_⏳ — день не закрыт, значения ещё вырастут; "
                         "сравнивать с остальными днями нельзя._")
            lines.append("")

        portfolio_rows = portfolio_reconciliation(daily_rows)
        if portfolio_rows:
            lines += ["## Недельная сверка портфеля", ""]
            lines += ["| Проект | Факт | Доля | Цель | Отклонение |"]
            lines += ["|---|---:|---:|---:|---:|"]
            for umb, seconds, actual, target, delta in portfolio_rows:
                if seconds <= 0 and target <= 0:
                    continue
                lines.append(
                    f"| {umb} | {fmt_h(seconds)} | {fmt_pct(actual)} | "
                    f"{fmt_pct(target)} | {fmt_pp(delta)} |"
                )
            lines.append("")

    lines += ["## Слой 1 — wall-clock (печать/мышь)", ""]
    state_labels = {
        "ACTIVE_EDIT": "Активно печатал",
        "ACTIVE_REVIEW": "Читал/думал (≤8 мин после ввода)",
        "IDLE": "Не работал",
        "AWAY_BLUR": "Окно не в фокусе",
    }
    if not states:
        lines.append("_данных нет — AW не запущен?_")
    else:
        for state in ("ACTIVE_EDIT", "ACTIVE_REVIEW", "IDLE", "AWAY_BLUR"):
            projs = states.get(state, {})
            if not projs:
                continue
            total = sum(projs.values())
            lines.append(f"### {state_labels.get(state, state)} — {fmt_h(total)}")
            for p, secs in sorted(projs.items(), key=lambda x: -x[1])[:15]:
                lines.append(f"- `{p}` — {fmt_h(secs)}")
            lines.append("")

    lines += ["## Слой 2 — диалог с AI (Claude/Codex)", ""]
    if not engagement:
        lines.append("_диалога не было_")
    else:
        total = sum(engagement.values())
        lines.append(f"**Всего в диалоге:** {fmt_h(total)}")
        lines.append("")
        for (p, eng), secs in sorted(engagement.items(), key=lambda x: -x[1])[:20]:
            lines.append(f"- `{p}` · {eng} — {fmt_h(secs)}")
        lines.append("")

    if panes:
        lines += ["## Слой 1b — tmux-панели (где какой суб-проект)", ""]
        by_session: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for (session, proj), secs in panes.items():
            by_session[session][proj] += secs
        ordered = sorted(by_session.items(), key=lambda x: -sum(x[1].values()))
        for session, projs in ordered[:15]:
            total = sum(projs.values())
            lines.append(f"### `{session}` — {fmt_h(total)}")
            for p, secs in sorted(projs.items(), key=lambda x: -x[1])[:8]:
                lines.append(f"- `{p}` — {fmt_h(secs)}")
            lines.append("")

    lines += ["## Слой 2.5 — автономные агенты (task-runner)", ""]
    if not autonomous:
        lines.append("_автономных задач не запускалось_")
    else:
        total = sum(autonomous.values())
        lines.append(f"**Всего времени агентов:** {fmt_h(total)} (параллельно — wall-clock может быть меньше)")
        lines.append("")
        for (p, eng), secs in sorted(autonomous.items(), key=lambda x: -x[1])[:20]:
            lines.append(f"- `{p}` · {eng} — {fmt_h(secs)}")
        lines.append("")

    lines += [
        "---",
        "",
        "## Как читать",
        "",
        "- **Слой 1** = где ты физически был. Активно печатал; Читал/думал (≤8 мин после ввода); Не работал; Окно не в фокусе.",
        "- **Слой 2** = подтверждённые пользовательские ходы в Claude/Codex. Используется только как ограниченный fallback, когда прямого сигнала проекта нет.",
        "- **Слой 2.5** = что молотили автономные агенты после `/oxi-task-runner`. НЕ включено в «Работа по проектам».",
        "- **Реальное время за клавиатурой = проекты + прочая активность + неатрибуцированный остаток**. Не раздувается параллельностью; остаток — это печать в окне без распознанного проекта.",
        "- **⏳ / «День не закрыт»** = снимок на момент генерации, а не итог дня. Сутки считаются по "
        f"UTC-окну, поэтому файл дописывается до "
        f"{window_end.astimezone().strftime('%H:%M %d.%m')} локального времени; "
        "вечерние цифры занижены на несколько часов.",
        "",
    ]
    return "\n".join(lines)


def _render_day_report(day: datetime, now: datetime) -> str:
    """Collect all AW summaries for `day` and render the markdown report."""
    return render_markdown(
        day,
        summarise_states(day),
        summarise_engagement(day),
        summarise_autonomous(day),
        summarise_tmux_panes(day),
        summarise_by_umbrella(day),
        summarise_daily(day, days=7),
        summarise_attribution(day),
        now=now,
    )


def finalize_day(
    day: datetime, now: datetime, dry: bool = False, skip_trackers: bool = False
) -> bool:
    """Redraw a closed day's report left stale by the UTC rollover.

    The 30-minute launchd run only ever rewrites *today's* file, so a day's
    last snapshot is taken just before its UTC window closes — without this
    pass that file keeps the open-day callout and pre-close totals forever.
    The callout doubles as the staleness marker: when a closed window's file
    still carries it, re-run the trackers for that closed window and redraw
    the report with final figures. The pass self-closes — a redrawn file has
    no callout, so later runs skip it. Manual --date / --yesterday /
    --no-obsidian invocations never reach here.
    """
    if day_completeness(day, now)[0]:
        return False  # window still open — the snapshot is legitimately partial
    out = OBSIDIAN_FOLDER / f"time-{day.strftime('%Y-%m-%d')}.md"
    try:
        stale = OPEN_DAY_CALLOUT in out.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False  # no report file (or unreadable) — nothing to finalize
    if not stale:
        return False
    if not skip_trackers:
        # Finalizing is a one-way door: the redraw drops the staleness marker,
        # so a later run will never retry this day. A tracker that failed means
        # the figures are incomplete — leave the marker and let the next run
        # try again rather than freezing a partial day forever.
        failed = [
            script
            for script in ("tracker_layer1.py", "tracker_layer2.py", "tracker_layer25.py")
            if _run(script, day, dry) != 0
        ]
        if failed:
            print(
                f"[aggregate] finalize {day:%Y-%m-%d} skipped: "
                f"{', '.join(failed)} exited non-zero; day stays stale for retry",
                file=sys.stderr,
            )
            return False
    out.write_text(_render_day_report(day, now), encoding="utf-8")
    print(f"[aggregate] finalized {out}", file=sys.stderr)
    return True


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--today", action="store_true")
    p.add_argument("--yesterday", action="store_true")
    p.add_argument("--date", help="YYYY-MM-DD")
    p.add_argument("--skip-trackers", action="store_true", help="don't re-run layer scripts")
    p.add_argument("--no-obsidian", action="store_true", help="print to stdout instead of file")
    p.add_argument("--dry-run", action="store_true", help="trackers run in dry mode")
    args = p.parse_args()

    now = datetime.now(timezone.utc)
    if args.date:
        day = datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc)
    elif args.yesterday:
        day = now - timedelta(days=1)
    else:
        day = now

    if not args.skip_trackers:
        _run("tracker_layer1.py", day, args.dry_run)
        _run("tracker_layer2.py", day, args.dry_run)
        _run("tracker_layer25.py", day, args.dry_run)

    md = _render_day_report(day, now)

    if args.no_obsidian or OBSIDIAN_FOLDER is None or not OBSIDIAN_FOLDER.parent.exists():
        if not args.no_obsidian:
            print(
                f"[aggregate] WARNING: report folder is not configured or missing — "
                "report NOT written to vault (vault moved/renamed?)",
                file=sys.stderr,
            )
        print(md)
        return

    assert OBSIDIAN_FOLDER is not None
    OBSIDIAN_FOLDER.mkdir(parents=True, exist_ok=True)
    out = OBSIDIAN_FOLDER / f"time-{day.strftime('%Y-%m-%d')}.md"
    out.write_text(md)
    print(f"[aggregate] wrote {out}", file=sys.stderr)

    # First scheduled run after the UTC rollover: yesterday's file still
    # carries the open-day callout from its last pre-rollover snapshot with
    # pre-close totals — redraw it with final figures. No-op afterwards:
    # the callout is the staleness marker, and a redrawn file has none.
    if not args.date and not args.yesterday:
        finalize_day(
            now - timedelta(days=1), now,
            dry=args.dry_run, skip_trackers=args.skip_trackers,
        )


if __name__ == "__main__":
    main()
