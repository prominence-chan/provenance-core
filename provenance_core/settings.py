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
