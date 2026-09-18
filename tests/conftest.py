"""Pytest bootstrap.

`scripts/` has no `__init__.py` (deliberate — these are standalone CLI tools),
so to import `aggregate` in tests we put the `scripts/` dir on sys.path.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
for _p in (_ROOT, _SCRIPTS):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)
