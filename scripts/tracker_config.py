"""Configuration for the portable ActivityWatch tracker.

Public defaults are deliberately neutral. Put local/private overrides in
`config.toml` or point `AW_TRACKER_CONFIG` at another TOML file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib  # Python 3.11 or newer is required.


REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class TrackerConfig:
    bucket_prefix: str
    event_type_prefix: str
    state_dir: Path
    index_db: Path
    task_runner_cache: Path
    claude_projects_dir: Path
    codex_sessions_dir: Path
    task_runner_global_dir: Path
    excluded_scan_roots: list[Path]
    log_dir: Path
    client_name: str
    categories_file: Path
    project_manifest: Path | None
    obsidian_folder: Path | None
    layer25_project_roots: list[Path]
    project_umbrellas: dict[str, str]
    portfolio_targets: dict[str, float]
    portfolio_groups: dict[str, str]
    launchd_label_prefix: str
    python: str
    aw_host: str


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _path(value: str | None, base: Path = REPO_ROOT) -> Path | None:
    if not value:
        return None
    expanded = Path(value).expanduser()
    return expanded if expanded.is_absolute() else base / expanded


def _str_env(name: str, current: str) -> str:
    return os.environ.get(name, current)


def load_config(config_path: str | Path | None = None) -> TrackerConfig:
    explicit = config_path is not None or bool(os.environ.get("AW_TRACKER_CONFIG"))
    if config_path is None:
        config_path = os.environ.get("AW_TRACKER_CONFIG") or REPO_ROOT / "config.toml"
    path = Path(config_path).expanduser().resolve()
    if explicit and not path.is_file():
        raise FileNotFoundError(f"Tracker configuration file not found: {path}")
    data = _read_toml(path) if path.exists() else {}

    tracker = data.get("tracker") or {}
    paths = data.get("paths") or {}
    classification = data.get("classification") or {}
    launchd = data.get("launchd") or {}

    bucket_prefix = str(tracker.get("bucket_prefix") or "aw-agent-time")
    client_name = str(tracker.get("client_name") or "aw-agent-time")
    categories_file = _path(paths.get("categories_file") or "categories.example.json", path.parent) or (REPO_ROOT / "categories.example.json")
    project_manifest = _path(paths.get("project_manifest"), path.parent)
    obsidian_folder = _path(paths.get("obsidian_folder"), path.parent)
    roots = [
        p for p in (_path(str(item), path.parent) for item in (paths.get("layer25_project_roots") or []))
        if p is not None
    ]

    bucket_prefix = _str_env("AW_TRACKER_BUCKET_PREFIX", bucket_prefix)
    client_name = _str_env("AW_TRACKER_CLIENT_NAME", client_name)
    env_categories = os.environ.get("AW_TRACKER_CATEGORIES_FILE")
    if env_categories:
        categories_file = _path(env_categories, path.parent)

    state_dir = _path(paths.get("state_dir") or "state", path.parent)
    assert state_dir is not None
    return TrackerConfig(
        event_type_prefix=str(tracker.get("event_type_prefix") or "aw-agent-time"),
        state_dir=state_dir,
        index_db=_path(paths.get("index_db"), path.parent) or state_dir / "jsonl_index.sqlite",
        task_runner_cache=_path(paths.get("task_runner_cache"), path.parent) or state_dir / "task_runner_projects.json",
        claude_projects_dir=_path(paths.get("claude_projects_dir") or "~/.claude/projects", path.parent),
        codex_sessions_dir=_path(paths.get("codex_sessions_dir") or "~/.codex/sessions", path.parent),
        task_runner_global_dir=_path(paths.get("task_runner_global_dir") or "~/.task-runner", path.parent),
        excluded_scan_roots=[_path(str(item), path.parent) for item in paths.get("excluded_scan_roots", [])],
        log_dir=_path(launchd.get("log_dir"), path.parent) or state_dir / "logs",
        bucket_prefix=bucket_prefix,
        client_name=client_name,
        categories_file=categories_file,
        project_manifest=project_manifest,
        obsidian_folder=obsidian_folder,
        layer25_project_roots=roots,
        project_umbrellas={str(k): str(v) for k, v in (classification.get("project_umbrellas") or {}).items()},
        portfolio_targets={str(k): float(v) for k, v in (classification.get("portfolio_targets") or {}).items()},
        portfolio_groups={str(k): str(v) for k, v in (classification.get("portfolio_groups") or {}).items()},
        launchd_label_prefix=str(launchd.get("label_prefix") or "com.example.aw-agent-time"),
        python=str(launchd.get("python") or os.environ.get("PYTHON", "python3")),
        aw_host=str(tracker.get("aw_host") or "http://localhost:5600").rstrip("/"),
    )


CONFIG = load_config()
