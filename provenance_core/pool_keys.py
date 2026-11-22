"""API keys for the token pool, set from the app (GET/PUT/DELETE /pool/keys).

A provider reads its key from the environment (`api_key_env`, plus any `${VAR}` in its base_url, such as Cloudflare's
account id), so a key set here is put into this process's environment, where it takes effect at once, and kept in
<PCORE_HOME>/keys.json (mode 0600, the directory 0700) for the next start. catalog.load puts the file's keys into
the environment; a key set from the app wins over the same variable from the shell or keys.env, since setting it
was the later, deliberate act.

Values are never returned, logged or put in an error message: the view says only whether each variable is set and
where it came from."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .settings import home_dir

_VAR = re.compile(r"\$\{(\w+)\}")
MAX_LEN = 4096
FROM_FILE: set[str] = set()  # variables this process took from keys.json or set from the app


class KeySetupError(ValueError):
    """A key that cannot be set or removed; `str(e)` is written for the person using the app (never holds a value)."""


def keys_file(home: Path | None = None) -> Path:
    return Path(home or home_dir()) / "keys.json"


def _read(home: Path | None = None) -> dict[str, str]:
    try:
        d = json.loads(keys_file(home).read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in d.items() if isinstance(v, str)} if isinstance(d, dict) else {}


def _write(d: dict[str, str], home: Path | None = None):
    p = keys_file(home)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # created user-only, never readable by others
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=1)
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def load_into_env(home: Path | None = None):
    for k, v in _read(home).items():
        os.environ[k] = v
        FROM_FILE.add(k)


def provider_vars(p) -> list[str]:
    """The environment variables provider `p` needs: its key, then any ${VAR} in its base_url."""
    out = [p.api_key_env] if p.api_key_env else []
    return out + [v for v in _VAR.findall(p.url) if v not in out]


def view(catalog) -> list[dict]:
    """Every API provider of the catalog: its variables (set or not, and from where), whether it is usable, and what
    its models are worth (how many, in which tiers)."""
    out = []
    for name, p in catalog.providers.items():
        if p.type != "openai":
            continue
        models = [m for m in catalog.models.values() if m.provider == name]
        out.append(
            {
                "id": name,
                "usable": p.usable,
                "anonymous": p.no_auth and not _VAR.findall(p.url),  # works without a key (a key is still sent)
                "vars": [
                    {
                        "name": v,
                        "set": bool(os.environ.get(v)),
                        "from": ("app" if v in FROM_FILE else "env") if os.environ.get(v) else None,
                    }
                    for v in provider_vars(p)
                ],
                "models": len(models),
                "tiers": sorted({catalog.tier_of(m) for m in models}, key=lambda t: catalog.tier_index(t)),
            }
        )
    return out


def _provider(catalog, provider: str):
    p = catalog.providers.get(provider)
    if p is None or p.type != "openai":
        raise KeyError(f"There is no API provider called {provider}.")
    return p


def set_keys(catalog, provider: str, values: dict) -> dict:
    p = _provider(catalog, provider)
    names = provider_vars(p)
    if not names:
        raise KeySetupError(f"{provider} does not take a key.")
    if not isinstance(values, dict) or not values:
        raise KeySetupError('Send the key as {"values": {"<variable>": "<key>"}}.')
    clean = {}
    for k, v in values.items():
        if k not in names:
            raise KeySetupError(f"{provider} does not use {k}; it uses {', '.join(names)}.")
        v = v.strip() if isinstance(v, str) else ""
        if not v or len(v) > MAX_LEN or any(c in v for c in "\r\n\0"):
            raise KeySetupError(f"{k} must be one line of text (up to {MAX_LEN} characters).")
        clean[k] = v
    d = _read()
    d.update(clean)
    _write(d)
    for k, v in clean.items():
        os.environ[k] = v
        FROM_FILE.add(k)
    return next(x for x in view(catalog) if x["id"] == provider)


def clear_keys(catalog, provider: str) -> dict:
    """Remove the provider's keys set from the app. A variable that comes from the environment (keys.env, the shell)
    cannot be removed from here."""
    p = _provider(catalog, provider)
    names = provider_vars(p)
    outside = [n for n in names if os.environ.get(n) and n not in FROM_FILE]
    d = _read()
    for n in names:
        d.pop(n, None)
        if n in FROM_FILE:
            os.environ.pop(n, None)
            FROM_FILE.discard(n)
    _write(d)
    if outside:
        raise KeySetupError(
            f"{', '.join(outside)} comes from the router's environment (keys.env or the shell), not from the app; "
            "remove it there."
        )
    return next(x for x in view(catalog) if x["id"] == provider)
