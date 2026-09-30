"""Pytest bootstrap.

`scripts/` has no `__init__.py` (deliberate — these are standalone CLI tools),
so to import `aggregate` in tests we put the `scripts/` dir on sys.path.
"""
import os
import sys
from pathlib import Path

# Tests must not read the developer's private config.toml at the repo root:
# use the bundled example (it lives beside config.toml, so relative paths match).
if "AW_TRACKER_CONFIG" not in os.environ:
    os.environ["AW_TRACKER_CONFIG"] = str(Path(__file__).resolve().parent.parent / "config.example.toml")

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
for _p in (_ROOT, _SCRIPTS):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)
