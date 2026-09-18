#!/bin/bash
# fast_tick.sh — minimal-cost 5-minute pipeline.
# Order: update SQLite index (incremental, ~0.3s) → refresh today's oxi-states (~0.4s)
set -e

PY="${PYTHON:-python3}"
DIR="$(cd "$(dirname "$0")" && pwd)"
export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

"$PY" "$DIR/jsonl_indexer.py" -q
"$PY" "$DIR/tracker_layer1.py" --today
