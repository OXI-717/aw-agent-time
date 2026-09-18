# aw-agent-time

Local time tracking for people working with AI agents, built on ActivityWatch.

| Layer | Measures | Source |
|---|---|---|
| 1 | Human computer activity and project attribution | Window, AFK, optional input and focused tmux pane events |
| 2 | Human engagement with AI sessions | Local Claude Code and Codex JSONL logs |
| 2.5 | Autonomous agent runtime | Optional task-runner state and events |

**These layers overlap; never add them into total human working hours.** Autonomous
runs may also overlap each other. Missing observations are not proof of no work.
All daily reports currently use **UTC calendar days**, including `--today`.

```text
ActivityWatch window/AFK/input ──> Layer 1 ─┐
Claude Code / Codex JSONL ───────> Layer 2 ─┼─> local Markdown report
Task-runner state / events ─────> Layer 2.5┘
```

[Русская инструкция](README.ru.md) · [Synthetic report](docs/example-report.md)

## Install in about 10 minutes

Automatic setup supports macOS with Homebrew and **Python 3.11+**. The tracker uses
only the Python standard library. ActivityWatch is installed separately; tmux and
`aw-watcher-input` are optional. Downloading dependencies and granting macOS
permissions may take longer on a new computer.

```bash
git clone https://github.com/OXI-717/aw-agent-time.git
cd aw-agent-time
python3 --version  # must be 3.11 or newer
python3 -m venv .venv
source .venv/bin/activate
bash install.sh
cp config.example.toml config.toml
```

If the default Python is older, create the environment with a supported interpreter
(e.g. `python3.13 -m venv .venv`). The installer opens ActivityWatch and preserves
existing preferences and services. Grant its watchers the permissions requested
in System Settings → Privacy & Security. Confirm recent events at
<http://localhost:5600> before collecting derived time.

Edit private `config.toml` to select source paths, categories, and an optional
`paths.obsidian_folder` report directory (an ordinary directory; Obsidian is not
required). Copy `categories.example.json` to ignored `categories.json`, set
`paths.categories_file = "categories.json"`, and replace the synthetic rules with
your own. You can import the same rules in ActivityWatch's categorization settings.

```bash
python3 scripts/lint_categories.py --no-data
python3 scripts/jsonl_indexer.py
python3 scripts/aggregate.py --today --no-obsidian
```

The indexer builds or updates the local AI-session index. The aggregation command **runs the three trackers and replaces their derived daily
buckets**, then prints a report. Original watcher events are not modified. Use
this when you want to collect current results. Layer 2.5 needs compatible local
task-runner data; configure `paths.layer25_project_roots` for per-project discovery.
It is not a universal parser for every agent runner.

For reporting from existing buckets without rerunning trackers or writing reports:

```bash
python3 scripts/aggregate.py --date 2026-09-18 --skip-trackers --no-obsidian
```

To save a report, configure `paths.obsidian_folder`, ensure its parent directory
exists, and omit `--no-obsidian`. Reports are named `time-YYYY-MM-DD.md`. A scheduled
run after UTC rollover also finalizes the previous day's report.

## Configuration

See [config.example.toml](config.example.toml) for all supported settings.
Precedence is defaults → `config.toml` → supported environment overrides:
`AW_TRACKER_CONFIG`, `AW_TRACKER_BUCKET_PREFIX`, `AW_TRACKER_CLIENT_NAME`, and
`AW_TRACKER_CATEGORIES_FILE`. An explicitly selected missing config fails.
Relative paths resolve beside the configuration file, independent of the working
directory. Private configs, categories, logs and indexed data are git-ignored.

The default derived buckets are `aw-agent-time-{states,engagement,autonomous}_<host>`.
Use the previous bucket prefix, event type prefix and client name when migrating an
existing installation; keep its paths and category mappings in private config.
Do not load duplicate watcher jobs over an existing ActivityWatch installation.

Optional project discovery accepts a JSON manifest of this shape:

```json
{"projects": {"project-a": {"path_prefixes": ["~/projects/client-a"]}}}
```

`classification.project_umbrellas` maps manifest keys to report labels.
`paths.excluded_scan_roots` excludes broad or irrelevant directories.
Portfolio targets and grouping aliases are optional configuration, not built-in
claims about how a person should allocate time.

## Optional background jobs

The default renderer creates an aggregation job (30 minutes) and a fast index /
Layer 1 refresh job (5 minutes). It **renders files only** and refuses to replace
an existing different job. Configure `launchd.python` with the absolute path to
your environment's Python for reliable unattended execution.

```bash
bash install.sh --render-launchd
```

Generated files are under `state/launchd/`. Inspect them, then install only the jobs
you want. For example, with the default label prefix:

```bash
mkdir -p "$HOME/Library/LaunchAgents"
cp -n state/launchd/com.example.aw-agent-time.tracker.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.example.aw-agent-time.tracker.plist"
cp -n state/launchd/com.example.aw-agent-time.tracker-fast.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.example.aw-agent-time.tracker-fast.plist"
```

The fast job keeps the AI-session index current; run the indexer once before the
first aggregation as shown above.

Add `--include-tmux` to render the optional focused-pane watcher. It needs tmux.
Add `--include-input` only after installing `aw-watcher-input` (for example through
pipx) and granting its required permissions. Stock window/AFK watchers are managed
by ActivityWatch; this installer does not duplicate them. Logs use `launchd.log_dir`.

## Limits and privacy

Focused-pane attribution samples the most recently active tmux client. Multiple
attached clients, stale paths, and window managers that expose only a generic title
(such as Orca) can make attribution uncertain. Reports distinguish inferred and
ambiguous activity; these signals are estimates, not a presence or billing audit.
Review grace is capped at eight minutes in the Layer 1 state machine. Actual AFK
watcher settings can further restrict which intervals are counted.

AI log schemas vary across versions. Layer 2 measures human-anchored interaction
blocks, not every minute an agent process is alive. No cloud account is required by
these scripts. Source logs and local events can contain prompts, titles, file paths
and client names: keep them private and review reports before sharing them.

## Development and clean export

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q tests
bash scripts/export-public.sh ../aw-agent-time-public
```

`public-files.txt` is the exact export allowlist. New files require deliberate
addition. The exporter rejects symlinks and binary content, excludes private
configuration/runtime data/plans, and requires a new destination. It preserves
executable bits and creates a new repository with exactly one initial commit and
public author/committer identity. No source Git history or remotes are copied.
Before publishing, review every exported file and commit metadata, run tests in
that copy, and scan for secrets and identifying content.

## License

[MIT](LICENSE). ActivityWatch and optional dependencies retain their own licenses
and are not bundled in this repository.
