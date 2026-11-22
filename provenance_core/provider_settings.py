"""Per-provider settings a person changes from the app: whether a web chat is used at all, and its daily cap.

Kept in <PCORE_HOME>/settings.json and laid over the catalog when it is loaded (catalog.load), the way web_accounts.json
is: catalog.yaml is never rewritten, so a hand-written catalog keeps its comments and a shipped one stays as shipped.

    {"providers": {"chatgpt-web": {"enabled": false}, "qwen-web": {"daily_cap": 120}}}

`enabled` lands in the provider's options as `user_enabled`, which chatgpt_web.enabled() ranks above PCORE_CHATGPT_WEB=1
(the app turned the bridge on as a whole; a person then turned one site off) but below PCORE_CHATGPT_WEB=0, the
machine-wide off switch. `daily_cap` replaces the catalog's `web.daily_cap` (per account). A change made while the
router runs reaches the running bridges at once (update)."""

from __future__ import annotations

import json
from pathlib import Path

from .settings import home_dir

DAILY_CAP_MAX = 10000


class SettingError(ValueError):
    """A change that cannot be made; `str(e)` is written for the person using the app."""


def settings_file(home: Path | None = None) -> Path:
    return Path(home or home_dir()) / "settings.json"


def load(home: Path | None = None) -> dict:
    try:
        d = json.loads(settings_file(home).read_text())
    except (OSError, ValueError):
        d = {}
    provs = d.get("providers") if isinstance(d, dict) else None
    return {"providers": {str(k): dict(v) for k, v in (provs or {}).items() if isinstance(v, dict)}}


def save(d: dict, home: Path | None = None):
    p = settings_file(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(p)


def _apply_one(p, s: dict):
    if "enabled" in s:
        p.options["user_enabled"] = bool(s["enabled"])
    if "daily_cap" in s and p.type == "chatgpt_web":
        p.options["daily_cap"] = int(s["daily_cap"])


def apply(providers: dict, home: Path | None = None):
    """catalog.load: the saved settings over the catalog's providers (names no longer in the catalog are ignored)."""
    for name, s in load(home)["providers"].items():
        if name in providers:
            _apply_one(providers[name], s)


def validate(p, changes: dict) -> dict:
    """The changes that may be made to provider `p`, checked; SettingError otherwise."""
    out = {}
    for k, v in changes.items():
        if k == "enabled":
            if not isinstance(v, bool):
                raise SettingError("enabled is true or false.")
            if p.type != "chatgpt_web":
                raise SettingError(
                    "Only the web chats can be turned on or off here; an API provider is on when it has a key."
                )
            out[k] = v
        elif k == "daily_cap":
            if p.type != "chatgpt_web":
                raise SettingError("Only the web chats have a daily cap.")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or not 1 <= v <= DAILY_CAP_MAX:
                raise SettingError(f"The daily cap is a whole number of chats from 1 to {DAILY_CAP_MAX}.")
            out[k] = int(v)
        else:
            raise SettingError(f"{k} cannot be changed from the app.")
    if not out:
        raise SettingError("Nothing to change.")
    return out


def update(router, provider: str, changes: dict) -> dict:
    """Validate, save, and apply to the running router: the catalog's provider and every bridge already made for it
    (each Bridge keeps its own copy of the options). Returns the provider's saved settings."""
    if provider not in router.catalog.providers:
        raise KeyError(f"There is no provider called {provider}.")
    p = router.catalog.providers[provider]
    ch = validate(p, changes)
    d = load()
    saved = {**d["providers"].get(provider, {}), **ch}
    d["providers"][provider] = saved
    save(d)
    _apply_one(p, ch)
    if "daily_cap" in ch:
        b = router.bridges.get(provider)
        if b is not None:
            for one in [b, *getattr(b, "bridges", {}).values()]:
                for opts in (getattr(one, "o", None), getattr(one, "options", None)):
                    if isinstance(opts, dict):
                        opts["daily_cap"] = ch["daily_cap"]
    if "enabled" in ch:  # a site turned on or off: the router's cooldowns for its models start over
        for m in router.catalog.models.values():
            if m.provider == provider:
                router.state.health.pop(m.id, None)
    return saved


def unused_hosts(router) -> list[str]:
    """Web providers that run a browser of their own (no `share:`) which no enabled provider needs any more: neither
    they nor a guest working in their windows is on. Their browsers can be closed."""
    provs = router.catalog.providers
    web = {n: p for n, p in provs.items() if p.type == "chatgpt_web"}
    out = []
    for n, p in web.items():
        if p.options.get("share"):
            continue
        users = [p] + [q for q in web.values() if q.options.get("share") == n]
        if not any(q.usable for q in users) and n in router.bridges:
            out.append(n)
    return out
