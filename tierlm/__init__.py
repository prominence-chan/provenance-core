"""Compatibility shim: the package is now `provenance_core`. `from tierlm import usage` and `python -m tierlm` keep
working; new code should import provenance_core."""
import importlib
import sys

from provenance_core import *  # noqa: F401,F403
from provenance_core import __version__  # noqa: F401
from provenance_core import usage  # stdlib-only, so callers without the server deps can import it

sys.modules[__name__ + ".usage"] = usage


def __getattr__(name):   # tierlm.router, tierlm.catalog, ... on first use
    try:
        mod = importlib.import_module(f"provenance_core.{name}")
    except ModuleNotFoundError as e:
        raise AttributeError(name) from e
    sys.modules[f"{__name__}.{name}"] = mod
    return mod
