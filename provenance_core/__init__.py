"""provenance-core: pooled free LLM capacity behind one OpenAI/Anthropic-compatible endpoint, sorted into strength
tiers.

The modules live in subpackages (api, routing, context, accounting, keys, web). The flat names they had before
(`provenance_core.usage`, `from provenance_core import router`, `python -m provenance_core.pool_help`) keep working:
_Moved below resolves each old name to the module at its new place, on first import, as the same module object."""

import importlib as _importlib
import importlib.util as _util
import sys as _sys

__version__ = "0.2.0"

# old flat module name -> the subpackage it lives in now
_MOVED = {
    **dict.fromkeys(["server", "anthropic_api", "responses_api", "adapters", "codex_sub"], "api"),
    **dict.fromkeys(["router", "catalog", "calibrate", "provider_settings", "state"], "routing"),
    **dict.fromkeys(["compact", "prune", "prune_responses", "context_signal"], "context"),
    **dict.fromkeys(["ledger", "usage"], "accounting"),
    **dict.fromkeys(["pool_keys", "pool_help"], "keys"),
    **dict.fromkeys(["chatgpt_web", "browser_host", "cookie_guard", "web_setup", "web_signin", "credstore"], "web"),
}


class _Moved:
    """Meta-path finder and loader for the old names: `import provenance_core.usage` gets the module object of
    provenance_core.accounting.usage (imported once, under its real name); `python -m provenance_core.<old>` runs the
    new module as __main__. Nothing is imported until an old name is asked for."""

    def __init__(self, target: str):
        self.target = target

    @classmethod
    def find_spec(cls, name, path=None, target=None):
        pkg, _, mod = name.rpartition(".")
        if pkg != __name__ or mod not in _MOVED:
            return None
        new = f"{__name__}.{_MOVED[mod]}.{mod}"
        real = _util.find_spec(new)
        return _util.spec_from_loader(name, cls(new), origin=real.origin if real else None)

    def create_module(self, spec):
        module = _importlib.import_module(self.target)
        self.spec = module.__spec__  # the import system sets the alias spec on it next; exec_module puts it back
        return module

    def exec_module(self, module):
        module.__spec__ = self.spec

    def get_code(self, name):  # runpy: python -m provenance_core.<old>
        src = f"import runpy\nrunpy.run_module({self.target!r}, run_name='__main__', alter_sys=True)\n"
        return compile(src, f"<{name} -> {self.target}>", "exec")


if not any(f is _Moved for f in _sys.meta_path):
    _sys.meta_path.append(_Moved)
