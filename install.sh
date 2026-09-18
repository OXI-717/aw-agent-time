#!/usr/bin/env bash
# Bootstrap ActivityWatch or render optional background jobs.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON:-python3}"

if [ "${1:-}" = "--help" ]; then
  cat <<'HELP'
Usage: bash install.sh [--render-launchd [OPTIONS]]
Default: install/open ActivityWatch on macOS using Homebrew. Existing preferences
and services are preserved. No custom background jobs are loaded automatically.
--render-launchd: render jobs only; see scripts/render_launchd.py --help.
Set PYTHON to the path of Python 3.11+ when python3 is older.
HELP
  exit 0
fi
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else "Python 3.11+ required; set PYTHON to a supported interpreter")'
if [ "${1:-}" = "--render-launchd" ]; then
  shift
  exec "$PYTHON_BIN" "$REPO_DIR/scripts/render_launchd.py" "$@"
fi
[ "$#" -eq 0 ] || { echo "Unknown option; use --help" >&2; exit 2; }
[ "$(uname -s)" = Darwin ] || { echo 'Automatic installation supports macOS; see README for manual use.' >&2; exit 2; }
if [ ! -d /Applications/ActivityWatch.app ]; then
  command -v brew >/dev/null || { echo 'Homebrew is required to install ActivityWatch.' >&2; exit 2; }
  brew install --cask activitywatch
fi
open -ga ActivityWatch
cat <<'NEXT'
ActivityWatch opened. Allow its window/AFK watchers in macOS Privacy & Security.
Open http://localhost:5600 and confirm that recent watcher events appear.

Next, copy config.example.toml to config.toml and set your local paths/categories.
Read README.md for collection, report, and optional launchd commands.
To generate jobs without loading them:
  bash install.sh --render-launchd

Optional tmux focus watcher: add --include-tmux when rendering jobs.
Optional fine-grained input watcher: install aw-watcher-input with pipx, then use
--include-input. Stock window/AFK watchers are already managed by ActivityWatch.
NEXT
