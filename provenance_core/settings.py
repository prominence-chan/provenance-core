"""Environment settings. Every variable is read as PCORE_<NAME>, falling back to the pre-rename TIERLM_<NAME>, so
setups written for tierlm keep working."""

from __future__ import annotations

import os

PREFIXES = ("PCORE_", "TIERLM_")


def env(name: str, default: str | None = None) -> str | None:
    """PCORE_<name>, else TIERLM_<name>, else default. An empty string counts as set (it can mean "off")."""
    for p in PREFIXES:
        v = os.environ.get(p + name)
        if v is not None:
            return v
    return default


def scoped(name: str, scope: str, value, defaults: dict, scopes=("api", "web")) -> dict | None:
    """A history pass's options for one place it runs ("api" / "web"), or None when it is off there. `value` is the
    catalog setting as written: true / false, a dict of options, or {"api": ..., "web": ...} (each true / false /
    options). Off by default. PCORE_<name> overrides it: 0 / off, 1 / on / all, or the name of one scope."""
    if isinstance(value, dict) and any(s in value for s in scopes):
        value = value.get(scope)
    e = (env(name) or "").strip().lower()
    if e:
        if not (e in ("1", "true", "on", "yes", "all") or e == scope):
            return None
        value = value if isinstance(value, dict) else True
    if not value:
        return None
    o = {**defaults, **(value if isinstance(value, dict) else {})}
    if o.pop("enabled", True) is False:
        return None
    return o
