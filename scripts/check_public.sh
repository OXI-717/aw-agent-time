#!/usr/bin/env bash
# Public-repo gate: fail on real home paths or committed private config/state.
# Examples in code and tests must use the synthetic user `example`.
# CHECK_RANGE (optional, e.g. base..head) also scans every commit of a PR/push:
# a leak added in one commit and removed in a later one is still published.
set -euo pipefail
fail=0
PATHS='(/Users|/home)/[A-Za-z0-9][A-Za-z0-9._-]*'
PRIVATE='(^|/)(config\.toml|categories\.json|\.env(\.[A-Za-z0-9_.-]+)?|\.gh-account)$|(^|/)state/|\.(sqlite|db)$'

# Every path occurrence is checked on its own, so an `example` path on the same
# line cannot hide a real one.
if git grep -n -I -o -E "$PATHS" -- . ':!scripts/check_public.sh' \
    | grep -v -E ':(/Users|/home)/example$'; then
  echo "ERROR: real home path found; use /Users/example" >&2; fail=1
fi
if git ls-files | grep -E "$PRIVATE"; then
  echo "ERROR: private config, local state, database or env file is tracked" >&2; fail=1
fi

if [ -n "${CHECK_RANGE:-}" ]; then
  # An unreadable range must fail the gate, not look like "no matches".
  if ! git rev-list "$CHECK_RANGE" >/dev/null; then
    echo "ERROR: cannot read commit range $CHECK_RANGE" >&2; exit 1
  fi
  # -m: merge commits are diffed against each parent, so conflict resolutions count.
  if git log -m -p --no-color --format= "$CHECK_RANGE" -- . ':!scripts/check_public.sh' \
      | grep -E '^\+' | grep -o -E "$PATHS" \
      | grep -v -x -E '(/Users|/home)/example'; then
    echo "ERROR: real home path added in $CHECK_RANGE" >&2; fail=1
  fi
  if git log -m --no-color --format= --name-only "$CHECK_RANGE" | grep -E "$PRIVATE"; then
    echo "ERROR: private file touched in $CHECK_RANGE" >&2; fail=1
  fi
fi
exit $fail
