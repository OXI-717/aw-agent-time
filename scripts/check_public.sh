#!/usr/bin/env bash
# Public-repo gate: fail on real home paths or committed private config/state.
# Examples in code and tests must use the synthetic user `example`.
set -euo pipefail
fail=0
# Every path occurrence is checked on its own, so an `example` path on the same
# line cannot hide a real one.
if git grep -n -I -o -E '(/Users|/home)/[A-Za-z0-9][A-Za-z0-9._-]*' -- . ':!scripts/check_public.sh' \
    | grep -v -E ':(/Users|/home)/example$'; then
  echo "ERROR: real home path found; use /Users/example" >&2; fail=1
fi
if git ls-files | grep -E '(^|/)(config\.toml|categories\.json|\.env(\.[A-Za-z0-9_-]+)?|\.gh-account)$|(^|/)state/|\.(sqlite|db)$'; then
  echo "ERROR: private config, local state, database or env file is tracked" >&2; fail=1
fi
exit $fail
