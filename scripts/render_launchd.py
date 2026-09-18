#!/usr/bin/env python3
"""Render optional user LaunchAgents without loading or replacing any service."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import re
import shutil

from tracker_config import REPO_ROOT, load_config


def render(args: argparse.Namespace) -> list[Path]:
    config = load_config(args.config)
    prefix = args.label_prefix or config.launchd_label_prefix
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', prefix):
        raise ValueError('launchd label prefix must contain only letters, digits, dots and hyphens')
    python = shutil.which(args.python or config.python)
    if python is None:
        raise ValueError('configured Python executable was not found')
    python = os.path.abspath(python)
    repo = args.repo_dir.expanduser().absolute()
    output = args.launchd_output.expanduser().absolute()
    log_dir = config.log_dir
    jobs = ['tracker', 'tracker-fast']
    if args.include_tmux:
        jobs.append('watcher-tmux-pane')
    if args.include_input:
        jobs.append('watcher-input')
    replacements = {'{{REPO_DIR}}': str(repo), '{{PYTHON}}': python, '{{LABEL_PREFIX}}': prefix, '{{LOG_DIR}}': str(log_dir)}

    def replace(value):
        if isinstance(value, str):
            for key, replacement in replacements.items():
                value = value.replace(key, replacement)
            return value
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        return value

    explicit_config = args.config or os.environ.get('AW_TRACKER_CONFIG')
    config_path = Path(explicit_config).expanduser().absolute() if explicit_config else repo/'config.toml'
    pending = []
    for job in jobs:
        template = REPO_ROOT/'launchd'/f'{job}.plist.template'
        document = replace(plistlib.loads(template.read_bytes()))
        environment = document.setdefault('EnvironmentVariables', {})
        environment.update({'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                            'PYTHON': python, 'PYTHONUNBUFFERED': '1'})
        for key in ('AW_TRACKER_BUCKET_PREFIX', 'AW_TRACKER_CLIENT_NAME', 'AW_TRACKER_CATEGORIES_FILE'):
            if key in os.environ:
                environment[key] = os.environ[key]
        if config_path.is_file():
            environment['AW_TRACKER_CONFIG'] = str(config_path)
        document['ProcessType'] = 'Standard'
        if job == 'watcher-input':
            executable = shutil.which('aw-watcher-input')
            if executable is None:
                raise ValueError('aw-watcher-input not found; install it before using --include-input')
            document['ProgramArguments'][0] = os.path.abspath(executable)
        path = output/f'{prefix}.{job}.plist'
        contents = plistlib.dumps(document)
        if path.is_symlink() or (path.exists() and path.read_bytes() != contents):
            raise ValueError(f'refusing to overwrite existing job: {path}; use a separate output directory')
        pending.append((path, contents))
    # Preflight every destination before writing any of them.
    output.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    for path, contents in pending:
        if not path.exists():
            with path.open('xb') as handle:
                handle.write(contents)
    return [path for path, _ in pending]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launchd-output', type=Path, default=REPO_ROOT/'state'/'launchd')
    parser.add_argument('--repo-dir', type=Path, default=REPO_ROOT)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--label-prefix')
    parser.add_argument('--python')
    parser.add_argument('--include-tmux', action='store_true')
    parser.add_argument('--include-input', action='store_true')
    args = parser.parse_args()
    try:
        paths = render(args)
    except (ValueError, OSError) as exc:
        parser.exit(2, f'render-launchd: {exc}\n')
    for path in paths:
        print(f'rendered: {path}')


if __name__ == '__main__':
    main()
