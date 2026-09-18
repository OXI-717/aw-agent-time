#!/usr/bin/env python3
"""Export exactly the reviewed public manifest into a fresh Git repository."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
IDENTITY = "OXI-717"
EMAIL = "OXI-717@users.noreply.github.com"
PRIVATE_PARTS = {".git", ".gh-account", ".env", "config.toml", "categories.json", "state", ".task-runner", ".worktrees", "__pycache__", "superpowers"}


def public_files(root: Path) -> list[tuple[str, bytes, int]]:
    manifest = root / "public-files.txt"
    if manifest.is_symlink():
        raise ValueError("refuse symlink manifest")
    names = [line.strip() for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    if len(names) != len(set(names)):
        raise ValueError("duplicate manifest path")
    files = []
    for name in names:
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or str(rel) != name or "\\" in name or any(part in PRIVATE_PARTS for part in rel.parts):
            raise ValueError(f"unsafe manifest path: {name}")
        source = root
        for part in rel.parts:
            source /= part
            if source.is_symlink():
                raise ValueError(f"refuse symlink source or ancestor: {name}")
        if not source.is_file():
            raise ValueError(f"allowlisted file missing: {name}")
        content = source.read_bytes()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"binary content: {name}") from exc
        if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
            raise ValueError(f"binary/control content: {name}")
        files.append((name, content, 0o755 if source.stat().st_mode & 0o111 else 0o644))
    if "public-files.txt" not in names:
        raise ValueError("manifest must include itself")
    return files


def export(root: Path, destination: Path) -> None:
    if destination.is_symlink() or destination.exists():
        raise ValueError("destination must not exist (including symlinks)")
    files = public_files(root)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        for name, content, mode in files:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(mode)
        # Do not inherit another repository's Git directories or private identity.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_AUTHOR_NAME=IDENTITY, GIT_AUTHOR_EMAIL=EMAIL,
                   GIT_COMMITTER_NAME=IDENTITY, GIT_COMMITTER_EMAIL=EMAIL)
        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=destination, env=env, check=True)
        marker = root / ".gh-account"
        if marker.is_file() and not marker.is_symlink():
            shutil.copyfile(marker, destination / ".gh-account")
        git("init", "-q", "-b", "main")
        with (destination / ".git/info/exclude").open("a", encoding="utf-8") as handle:
            handle.write("\n.gh-account\n")
        git("config", "user.name", IDENTITY)
        git("config", "user.email", EMAIL)
        git("add", "--", *(name for name, _, _ in files))
        git("commit", "-q", "-m", "Initial public aw-agent-time export")
        (destination / ".gh-account").unlink(missing_ok=True)
    except BaseException:
        shutil.rmtree(destination)
        raise


def main() -> int:
    if sys.version_info < (3, 11):
        raise SystemExit("export-public requires Python 3.11+")
    if len(sys.argv) > 2:
        raise SystemExit("usage: export-public.sh [NEW_DESTINATION]")
    dest = Path(sys.argv[1]).absolute() if len(sys.argv) == 2 else ROOT.parent / "aw-agent-time"
    try:
        export(ROOT, dest)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"export-public: {exc}", file=sys.stderr)
        return 2
    print(f"exported clean public tree to {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
