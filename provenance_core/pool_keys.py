"""API keys for the token pool: any number per provider, each from a different account of the person's.

A provider's FIRST key is where it has always been: the environment variable the catalog names (`api_key_env`, plus
any `${VAR}` in its base_url, such as Cloudflare's account id), set in the shell or keys.env, or set from the app
(PUT /pool/keys/{provider}), which puts it into this process's environment and keeps it in <PCORE_HOME>/keys.json
(mode 0600, the directory 0700) for the next start. Its id is "main". A key set from the app wins over the same
variable from the shell or keys.env, since setting it was the later, deliberate act.

FURTHER keys (POST /pool/keys/{provider}) live in the same keys.json, under the one top-level name that is not a
variable, "@keys":

    {"GROQ_API_KEY": "<first key>",
     "@keys": {"groq": [{"id": "main", "label": "work account"},              # what was said about the first key
                        {"id": "k3fa91c", "key": "<second key>", "label": "Pro", "tier": "paid",
                         "limits": {"scale": 5}, "extras": {}, "enabled": true, "added": "2026-11-23"}]}}

A key has: a stable short id; an optional label; an optional `tier` hint ("free" | "paid"; absent = unknown);
optional `limits`, laid over the catalog's free-tier numbers for that key alone ({"scale": 5} multiplies them,
{"rpm": 100, "rpd": null} replaces single fields, null lifting one, {"models": {"<model>": {...}}} replaces them for
one model); `extras`, the provider's ${VAR} values for that key (each Cloudflare token has its own account id); and
`enabled`. An older router reading the file sees only the variables, so the first keys keep working if this change
is rolled back (see docs/TECHNICAL.md for what a rollback loses).

A provider usable without a key (`auth: none`) keeps its anonymous access beside any keys: it is one more slot, id
"anon". A key the provider rejects as invalid or revoked is marked (REJECTED: this process only) and skipped until
it is replaced, re-enabled or checked again; the others go on working.

Values are never returned, logged or put in an error message: a view carries a key's id, label, tier, whether it
is usable and why not, the day it was added and its last 4 characters."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .settings import home_dir

_VAR = re.compile(r"\$\{(\w+)\}")
MAX_LEN = 4096
MAIN, ANON = "main", "anon"
SECTION = "@keys"
TIERS = ("free", "paid")
LIMIT_FIELDS = ("rpm", "rpd", "tpm", "tpd", "rph")
FROM_FILE: set[str] = set()  # variables this process took from keys.json or set from the app
SHADOWED: dict[str, str] = {}  # the environment's own value of a variable keys.json replaced, put back on clear
EXTRA: dict[str, list[dict]] = {}  # the "@keys" section as last read or written: provider -> entries
REJECTED: dict[tuple[str, str], tuple[str, str]] = {}  # (provider, key id) -> (reason, fingerprint of the value)


class KeySetupError(ValueError):
    """A key that cannot be set or removed; `str(e)` is written for the person using the app (never holds a value)."""


class DuplicateKey(KeySetupError):
    """That exact key is already one of the provider's."""


@dataclass
class PoolKey:
    id: str
    provider: str
    value: str | None  # None: the anonymous slot
    label: str | None = None
    tier: str | None = None
    limits: dict | None = None
    extras: dict = field(default_factory=dict)
    enabled: bool = True
    added: str | None = None
    source: str = "app"  # "app" (keys.json) | "env" (the shell, keys.env) | "anonymous"

    def __repr__(self):  # never the value, wherever a key ends up being printed
        return f"PoolKey({self.provider}/{self.id})"


# ---- the file ------------------------------------------------------------------------------------------------------
def keys_file(home: Path | None = None) -> Path:
    return Path(home or home_dir()) / "keys.json"


def _read_all(home: Path | None = None) -> dict:
    try:
        d = json.loads(keys_file(home).read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _read(home: Path | None = None) -> dict[str, str]:
    """The variables (the first keys): every top-level string."""
    return {str(k): v for k, v in _read_all(home).items() if isinstance(v, str)}


def _entries(d: dict) -> dict[str, list[dict]]:
    """The "@keys" section, with anything malformed (a hand-edited file) left out."""
    sec = d.get(SECTION)
    out: dict[str, list[dict]] = {}
    for prov, items in (sec if isinstance(sec, dict) else {}).items():
        seen: set[str] = set()
        for e in items if isinstance(items, list) else []:
            kid = e.get("id") if isinstance(e, dict) else None
            if not isinstance(kid, str) or not kid or kid in seen or kid == ANON:
                continue
            if kid != MAIN and not (isinstance(e.get("key"), str) and e["key"]):
                continue
            seen.add(kid)
            out.setdefault(str(prov), []).append(e)
    return out


def _write(d: dict, home: Path | None = None):
    p = keys_file(home)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # created user-only, never readable by others
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=1)
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    _cache(d)


def _cache(d: dict):
    EXTRA.clear()
    EXTRA.update(_entries(d))


def _set(k: str, v: str):
    if k not in FROM_FILE and os.environ.get(k):
        SHADOWED[k] = os.environ[k]
    os.environ[k] = v
    FROM_FILE.add(k)


def load_into_env(home: Path | None = None):
    d = _read_all(home)
    for k, v in d.items():
        if isinstance(v, str):
            _set(k, v)
    _cache(d)


# ---- a provider's keys ---------------------------------------------------------------------------------------------
def provider_vars(p) -> list[str]:
    """The environment variables provider `p` needs: its key, then any ${VAR} in its base_url."""
    out = [p.api_key_env] if p.api_key_env else []
    return out + [v for v in _VAR.findall(p.url) if v not in out]


def url_vars(p) -> list[str]:
    return list(dict.fromkeys(_VAR.findall(p.url)))


def _meta(e: dict) -> dict:
    tier = e.get("tier")
    lim = e.get("limits")
    ex = e.get("extras")
    return {
        "label": e["label"] if isinstance(e.get("label"), str) and e["label"] else None,
        "tier": tier if tier in TIERS else None,
        "limits": lim if isinstance(lim, dict) and lim else None,
        "extras": {str(k): v for k, v in ex.items() if isinstance(v, str) and v} if isinstance(ex, dict) else {},
        "enabled": e.get("enabled") is not False,
        "added": e["added"] if isinstance(e.get("added"), str) else None,
    }


def keys_of(p) -> list[PoolKey]:
    """Every key provider `p` holds, the first one (its environment variable) first. Not the anonymous slot."""
    if p.type != "openai":
        return []
    out = []
    entries = EXTRA.get(p.name, [])
    first = os.environ.get(p.api_key_env) if p.api_key_env else None
    if first:
        meta = next((_meta(e) for e in entries if e["id"] == MAIN), _meta({}))
        meta["extras"] = {}  # the first key's ${VAR} values are the environment's own
        out.append(PoolKey(MAIN, p.name, first, source="app" if p.api_key_env in FROM_FILE else "env", **meta))
    if p.api_key_env:  # a provider that takes no key at all holds none
        out += [PoolKey(e["id"], p.name, e["key"], **_meta(e)) for e in entries if e["id"] != MAIN]
    return out


def _fingerprint(value: str | None) -> str:
    return hashlib.sha256((value or "").encode()).hexdigest()[:16]


def missing_vars(p, key: PoolKey | None) -> list[str]:
    """The ${VAR}s of the provider's address this key has no value for (its own `extras`, else the environment)."""
    ex = key.extras if key else {}
    return [v for v in url_vars(p) if not (ex.get(v) or os.environ.get(v))]


def unusable_reason(p, key: PoolKey) -> str | None:
    """Why this key is skipped, or None when it is used."""
    if not key.enabled:
        return "Turned off."
    rej = REJECTED.get((p.name, key.id))
    if rej and rej[1] == _fingerprint(key.value):
        return rej[0]
    miss = missing_vars(p, key)
    if miss:
        return f"Needs {', '.join(miss)}."
    return None


def slots(p) -> list[PoolKey]:
    """What requests to `p` can be sent with right now: its usable keys, then its anonymous access if it has one."""
    if p.type != "openai":
        return []
    out = [k for k in keys_of(p) if unusable_reason(p, k) is None]
    if p.no_auth and not missing_vars(p, None):
        out.append(PoolKey(ANON, p.name, None, source="anonymous"))
    return out


def reject(p, key: PoolKey, reason: str):
    """The provider refused this key as invalid or revoked: skip it from now on (the anonymous slot has no key)."""
    if key is not None and key.value:
        REJECTED[(p.name, key.id)] = (reason, _fingerprint(key.value))


def accept(p, key: PoolKey):
    REJECTED.pop((p.name, key.id), None)


def scrub(p, text: str) -> str:
    """`text` (a provider's error body) with every key of `p` cut out, should the provider have echoed one."""
    for k in keys_of(p):
        if k.value and k.value in text:
            text = text.replace(k.value, "[key]")
    return text


# ---- limits per key ------------------------------------------------------------------------------------------------
def limits_for(key: PoolKey | None, lim, model: str, pool=None):
    """(the model's limits, the provider's pool limits) as they apply to `key`: the catalog's numbers, changed by the
    key's own `limits`. A single field replaces the catalog's wherever the catalog has that field (the model's limits
    or the provider's pool), and is added to the model's where it has it nowhere."""
    spec = key.limits if key is not None else None
    if not spec:
        return lim, pool
    scale = spec.get("scale")
    scale = float(scale) if isinstance(scale, (int, float)) and not isinstance(scale, bool) and scale > 0 else None
    per = (spec.get("models") or {}).get(model) if isinstance(spec.get("models"), dict) else None
    per = per if isinstance(per, dict) else {}

    def one(L, is_pool: bool):
        vals = {}
        for f in LIMIT_FIELDS:
            v = getattr(L, f)
            if v and scale:
                v = max(1, int(v * scale))
            if f in spec and (v if is_pool else (v or not (pool and getattr(pool, f)))):
                v = spec[f]
            if not is_pool and f in per:
                v = per[f]
            vals[f] = v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None
        return type(L)(**vals)

    return one(lim, False), (one(pool, True) if pool is not None else None)


# ---- views (never a value) -----------------------------------------------------------------------------------------
def last4(value: str | None) -> str:
    """The last 4 characters of a key long enough that they give nothing away; else nothing."""
    return value[-4:] if value and len(value) >= 12 else ""


def key_view(p, k: PoolKey) -> dict:
    reason = unusable_reason(p, k)
    return {
        "id": k.id,
        "label": k.label,
        "tier": k.tier,
        "usable": reason is None,
        "reason": reason,
        "added": k.added,
        "last4": last4(k.value),
        "limits": k.limits,
        "enabled": k.enabled,
        "source": k.source,
    }


def _help(pid: str) -> dict | None:
    from . import pool_help

    try:
        return pool_help.provider(pid)
    except KeyError:
        return None


def provider_view(catalog, name: str) -> dict:
    from . import pool_help

    p = catalog.providers[name]
    models = [m for m in catalog.models.values() if m.provider == name]
    expired = p.expired
    note = None
    if p.until:
        note = (
            f"{name} stopped on {p.until.isoformat()}; its models are no longer offered."
            if expired
            else f"Temporary: {name} is used until {p.until.isoformat()}, when its API shuts down."
        )
    return {
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
        "keys": [key_view(p, k) for k in keys_of(p)],
        "multiple_keys": pool_help.multiple_keys(name),
        "until": p.until.isoformat() if p.until else None,
        "expired": expired,
        "note": note,
    }


def view(catalog) -> list[dict]:
    """Every API provider of the catalog: its variables (set or not, and from where), whether it is usable, what its
    models are worth (how many, in which tiers), and its keys."""
    return [provider_view(catalog, name) for name, p in catalog.providers.items() if p.type == "openai"]


def removed_view(catalog) -> list[dict]:
    """Providers taken out of the pool (pool_help.yaml `removed:`), each with whether a key for it is still saved:
    such a key is ignored, and said to be."""
    from . import pool_help

    saved = _read_all()
    out = []
    for r in pool_help.load().get("removed") or []:
        if r["id"] in catalog.providers:  # a catalog of the person's own that still has it: not removed here
            continue
        var = r.get("key_var")
        left = bool(var and (os.environ.get(var) or isinstance(saved.get(var), str))) or bool(EXTRA.get(r["id"]))
        out.append(
            {
                "id": r["id"],
                "name": r.get("name") or r["id"],
                "removed": r.get("removed"),
                "reason": r.get("reason"),
                "leftover_key": left,
                "note": (
                    f"A key for {r.get('name') or r['id']} is still saved; it is ignored. "
                    "DELETE /pool/keys/" + r["id"] + " removes it."
                )
                if left
                else None,
            }
        )
    return out


# ---- changes -------------------------------------------------------------------------------------------------------
def _provider(catalog, provider: str):
    p = catalog.providers.get(provider)
    if p is None or p.type != "openai":
        from . import pool_help

        gone = next((r for r in pool_help.load().get("removed") or [] if r["id"] == provider), None)
        if gone and p is None:
            raise KeyError(
                f"{gone.get('name') or provider} was removed from the pool on {gone.get('removed')}: {gone.get('reason')}"
            )
        raise KeyError(f"There is no API provider called {provider}.")
    return p


def _value(v, name: str) -> str:
    v = v.strip() if isinstance(v, str) else ""
    if not v or len(v) > MAX_LEN or any(c in v for c in "\r\n\0"):
        raise KeySetupError(f"{name} must be one line of text (up to {MAX_LEN} characters).")
    return v


def _clean_limits(v) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise KeySetupError('limits is an object such as {"scale": 5} or {"rpm": 100, "rpd": 5000}, or null.')

    def fields(d: dict, where: str) -> dict:
        out = {}
        for k, x in d.items():
            if k not in LIMIT_FIELDS:
                raise KeySetupError(f"{where} has no limit called {k}; the limits are {', '.join(LIMIT_FIELDS)}.")
            if x is not None and (isinstance(x, bool) or not isinstance(x, int) or not 1 <= x <= 10**12):
                raise KeySetupError(f"{k} is a whole number above 0, or null for no limit.")
            out[k] = x
        return out

    out = fields({k: x for k, x in v.items() if k not in ("scale", "models")}, "limits")
    if v.get("scale") is not None:
        s = v["scale"]
        if isinstance(s, bool) or not isinstance(s, (int, float)) or not 0.01 <= s <= 1000:
            raise KeySetupError("limits.scale is a number from 0.01 to 1000.")
        if s != 1:
            out["scale"] = s
    if v.get("models") is not None:
        if not isinstance(v["models"], dict):
            raise KeySetupError("limits.models is an object of model name to limits.")
        ms = {
            str(m): fields(x if isinstance(x, dict) else {"?": 0}, f"limits.models.{m}") for m, x in v["models"].items()
        }
        ms = {m: x for m, x in ms.items() if x}
        if ms:
            out["models"] = ms
    return out or None


def _clean_meta(body: dict, value: str | None = None) -> dict:
    """label / tier / limits / enabled out of a request body: only the ones present, checked."""
    out = {}
    if "label" in body:
        lab = body["label"]
        if lab is not None and (not isinstance(lab, str) or len(lab) > 80 or any(c in lab for c in "\r\n\0")):
            raise KeySetupError("label is a short line of text (up to 80 characters), or null.")
        lab = lab.strip() if lab else None
        if lab and value and (value in lab or lab in value and len(lab) >= 8):
            raise KeySetupError("The label must not contain the key.")
        out["label"] = lab or None
    if "tier" in body:
        t = body["tier"]
        t = None if t in (None, "", "unknown") else t
        if t is not None and t not in TIERS:
            raise KeySetupError('tier is "free", "paid" or "unknown".')
        out["tier"] = t
    if "limits" in body or "limits_scale" in body:
        lim = body.get("limits")
        if body.get("limits_scale") is not None:
            lim = {**(lim if isinstance(lim, dict) else {}), "scale": body["limits_scale"]}
        out["limits"] = _clean_limits(lim)
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise KeySetupError("enabled is true or false.")
        out["enabled"] = body["enabled"]
    return out


def _today() -> str:
    return datetime.date.today().isoformat()


def _store(e: dict) -> dict:
    """An entry as kept in the file: nothing empty."""
    return {k: v for k, v in e.items() if v not in (None, {}, "") and not (k == "enabled" and v is True)}


def _save_entry(d: dict, provider: str, entry: dict):
    sec = d.get(SECTION) if isinstance(d.get(SECTION), dict) else {}
    items = [e for e in _entries(d).get(provider, []) if e["id"] != entry["id"]]
    entry = _store(entry)
    if entry["id"] != MAIN or len(entry) > 1:
        items.append(entry)
    items.sort(key=lambda e: e["id"] != MAIN)  # the first key's entry stays first
    sec = {**sec, provider: items}
    if not items:
        sec.pop(provider)
    d[SECTION] = sec
    if not sec:
        d.pop(SECTION)


def _drop_entry(d: dict, provider: str, key_id: str | None):
    """Remove one entry (or, with None, all of the provider's) from the section."""
    sec = d.get(SECTION) if isinstance(d.get(SECTION), dict) else {}
    items = [] if key_id is None else [e for e in _entries(d).get(provider, []) if e["id"] != key_id]
    sec = {k: v for k, v in sec.items() if k != provider}
    if items:
        sec[provider] = items
    if sec:
        d[SECTION] = sec
    else:
        d.pop(SECTION, None)


def set_keys(catalog, provider: str, values: dict, meta: dict | None = None) -> dict:
    """Set or replace the provider's first key (and its ${VAR} values)."""
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
        clean[k] = _value(v, k)
    new = clean.get(p.api_key_env) if p.api_key_env else None
    if new and any(k.value == new for k in keys_of(p) if k.id != MAIN):
        raise DuplicateKey(f"That key is already one of {provider}'s keys.")
    m = _clean_meta(meta or {}, new)
    d = _read_all()
    d.update(clean)
    if new or m:
        old = next((e for e in _entries(d).get(provider, []) if e["id"] == MAIN), {"id": MAIN})
        if new and new != os.environ.get(p.api_key_env):  # a different key: added today
            old = {**old, "added": _today()}
        _save_entry(d, provider, {**old, **m})
    _write(d)
    for k, v in clean.items():
        _set(k, v)
    REJECTED.pop((provider, MAIN), None)
    return provider_view(catalog, provider)


def add_key(catalog, provider: str, body: dict) -> tuple[dict, PoolKey]:
    """One more key for the provider (its first, if it has none). Returns (the provider's view, the new key)."""
    p = _provider(catalog, provider)
    if not p.api_key_env:
        raise KeySetupError(f"{provider} does not take a key.")
    value = _value(body.get("key"), "key")
    have = keys_of(p)
    if any(k.value == value for k in have):
        raise DuplicateKey(f"That key is already one of {provider}'s keys.")
    meta = _clean_meta(body, value)
    extras = body.get("extras") if body.get("extras") is not None else {}
    if not isinstance(extras, dict):
        raise KeySetupError('extras is an object such as {"<variable>": "<value>"}.')
    need = url_vars(p)
    ex = {}
    for k, v in extras.items():
        if k not in need:
            raise KeySetupError(
                f"{provider} does not use {k}" + (f"; it uses {', '.join(need)}." if need else "; it takes no extras.")
            )
        ex[k] = _value(v, k)
    if not any(k.id == MAIN for k in have):  # the provider's first key: the variable, as PUT sets it
        miss = [v for v in need if not (ex.get(v) or os.environ.get(v))]
        if miss:
            raise KeySetupError(f"{provider} also needs {', '.join(miss)} (send it in extras).")
        view = set_keys(catalog, provider, {p.api_key_env: value, **ex}, meta)
        return view, next(k for k in keys_of(p) if k.id == MAIN)
    miss = [v for v in need if not (ex.get(v) or os.environ.get(v))]
    if miss:
        raise KeySetupError(f"{provider} also needs {', '.join(miss)} for this key (send it in extras).")
    d = _read_all()
    ids = {e["id"] for e in _entries(d).get(provider, [])} | {MAIN, ANON}
    kid = next(i for i in iter(lambda: "k" + secrets.token_hex(3), None) if i not in ids)
    _save_entry(d, provider, {"id": kid, "key": value, **meta, "extras": ex, "added": _today()})
    _write(d)
    return provider_view(catalog, provider), next(k for k in keys_of(p) if k.id == kid)


def _key(p, key_id: str) -> PoolKey:
    k = next((k for k in keys_of(p) if k.id == key_id), None)
    if k is None:
        raise KeyError(f"{p.name} has no key with the id {key_id}.")
    return k


def update_key(catalog, provider: str, key_id: str, body: dict) -> dict:
    """Change what is known about a key: label, tier, limits, enabled. Turning a key on again also forgets that the
    provider once rejected it (it is tried again)."""
    p = _provider(catalog, provider)
    k = _key(p, key_id)
    unknown = [x for x in body if x not in ("label", "tier", "limits", "limits_scale", "enabled")]
    if unknown:
        raise KeySetupError(f"{', '.join(unknown)} cannot be changed; label, tier, limits and enabled can.")
    meta = _clean_meta(body, k.value)
    if not meta:
        raise KeySetupError("Nothing to change.")
    d = _read_all()
    old = next((e for e in _entries(d).get(provider, []) if e["id"] == key_id), {"id": key_id})
    _save_entry(d, provider, {**old, **meta})
    _write(d)
    if meta.get("enabled") is True:
        REJECTED.pop((provider, key_id), None)
    return provider_view(catalog, provider)


def _clear_first(p, d: dict) -> list[str]:
    """Take the provider's variables set from the app out of `d` and the environment (the environment's own value,
    where it had one, applies again). Returns the variables that only the environment sets."""
    names = provider_vars(p)
    outside = [n for n in names if os.environ.get(n) and n not in FROM_FILE]
    for n in names:
        d.pop(n, None)
        if n in FROM_FILE:
            FROM_FILE.discard(n)
            if n in SHADOWED:
                os.environ[n] = SHADOWED.pop(n)
            else:
                os.environ.pop(n, None)
    return outside


_OUTSIDE = "{} comes from the router's environment (keys.env or the shell), not from the app; remove it there."


def delete_key(catalog, provider: str, key_id: str) -> dict:
    p = _provider(catalog, provider)
    _key(p, key_id)
    d = _read_all()
    outside = []
    if key_id == MAIN:
        outside = _clear_first(p, d)
        if p.api_key_env not in outside:  # what was said about a key goes with it
            _drop_entry(d, provider, MAIN)
    else:
        _drop_entry(d, provider, key_id)
    _write(d)
    REJECTED.pop((provider, key_id), None)
    if outside:
        raise KeySetupError(_OUTSIDE.format(", ".join(outside)))
    return provider_view(catalog, provider)


def clear_keys(catalog, provider: str) -> dict:
    """Remove all of the provider's keys set from the app; where the environment had its own value (keys.env, the
    shell), that one applies again. A variable only the environment sets cannot be removed from here. For a provider
    that was removed from the pool, this forgets the key that was saved for it."""
    from . import pool_help

    gone = next((r for r in pool_help.load().get("removed") or [] if r["id"] == provider), None)
    if gone and provider not in catalog.providers:
        d = _read_all()
        var = gone.get("key_var")
        if var:
            d.pop(var, None)
            if var in FROM_FILE:
                FROM_FILE.discard(var)
                os.environ.pop(var, None)
        _drop_entry(d, provider, None)
        _write(d)
        left = next((r for r in removed_view(catalog) if r["id"] == provider), None)
        if left and left["leftover_key"]:
            raise KeySetupError(_OUTSIDE.format(var))
        return left
    p = _provider(catalog, provider)
    d = _read_all()
    outside = _clear_first(p, d)
    main = next((e for e in _entries(d).get(provider, []) if e["id"] == MAIN), None)
    _drop_entry(d, provider, None)
    if main and p.api_key_env in outside:  # the environment's key stays, and so does what was said about it
        _save_entry(d, provider, main)
    _write(d)
    for pk in [k for k in REJECTED if k[0] == provider]:
        REJECTED.pop(pk, None)
    if outside:
        raise KeySetupError(_OUTSIDE.format(", ".join(outside)))
    return provider_view(catalog, provider)


# ---- is a key valid? -----------------------------------------------------------------------------------------------
_REJECT_TEXT = re.compile(
    r"api[ _-]?key[^.\n]{0,40}(not valid|invalid|incorrect|revoked|expired|disabled)|invalid[ _-]?api[ _-]?key"
    r"|API_KEY_INVALID|invalid[ _](token|credentials)|authentication (error|failed)|unauthori[sz]ed|key (was )?revoked"
    r"|user not found|no auth credentials",
    re.I,
)


def looks_rejected(status: int, text: str) -> bool:
    """Does this answer say the KEY is bad (invalid, revoked), rather than the request or the quota?"""
    if status == 401:
        return True
    return status in (400, 403) and bool(_REJECT_TEXT.search(text or ""))


async def check(client, p, key: PoolKey) -> dict:
    """A cheap validity check, where the catalog names one for the provider (`check:`): one GET that needs the key
    and spends no quota. {"ok": true | false | null, "detail"}: null = no check for this provider, or no clear
    answer. A key found bad is marked and skipped (unless the provider's check is `soft`: then it is only said)."""
    spec = getattr(p, "check", None)
    if not spec:
        return {
            "ok": None,
            "detail": "This provider has no free check; the first request will show whether the key works.",
        }
    miss = missing_vars(p, key)
    if miss:
        return {"ok": None, "detail": f"Not checked: {', '.join(miss)} is missing."}
    url = spec.get("url")
    url = p.fill(url, key) if url else p.base_url_for(key) + str(spec.get("path") or "/models")
    try:
        r = await client.get(url, headers={**p.auth_headers(key), **p.headers}, timeout=15)
        status, text = r.status_code, r.text[:2000]
    except Exception:  # noqa: BLE001 - whatever went wrong on the way says nothing about the key
        return {"ok": None, "detail": "The provider could not be reached to check the key; it was saved."}
    if 200 <= status < 300:
        accept(p, key)
        return {"ok": True, "detail": "The provider accepted the key."}
    if looks_rejected(status, text) or status == 403:
        why = f"The provider did not accept this key (HTTP {status})."
        if spec.get("soft"):
            return {"ok": False, "detail": why + " It was saved and will be tried; check it and its extras."}
        reject(p, key, why)
        return {"ok": False, "detail": why + " It was saved but is not used; replace or remove it."}
    return {"ok": None, "detail": f"The check gave no clear answer (HTTP {status}); the key was saved."}
