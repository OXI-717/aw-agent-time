#!/usr/bin/env bash
# Export the exact reviewed public manifest; no live services are touched.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "${PYTHON:-python3}" "$ROOT/scripts/export_public.py" "$@"
