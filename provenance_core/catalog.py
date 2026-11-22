"""Model catalog: providers (OpenAI-compatible endpoints + one API key each), models with a strength score
(0-100, from `provenance_core calibrate` or a prior), free-tier limits, and the tier each score falls into.

A provider with `api_key_env: none` (or `auth: none`) is used without a key and sent no Authorization header.
`${VAR}` in a base_url is filled from the environment (a provider with an unset variable is unusable). A provider's
`pool` limits apply across all of its models together (e.g. an anonymous per-IP request cap), and `shared_tpd: true`
means each model's tpd is its share of one provider-wide daily allowance (Cloudflare's daily neurons).

Tiers are the point of the project: a request asks for a tier ("nova", "solstice", "ember", "spark"; "tier-N" works too) and is only ever served by
models of comparable strength, so answers don't swing between a frontier model and a tiny one depending on which
free quota happens to be left."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Limits:
    rpm: int | None = None  # requests per minute
    rpd: int | None = None  # requests per day
    tpm: int | None = None  # tokens per minute
    tpd: int | None = None  # tokens per day
    rph: int | None = None  # requests per hour

    def any(self) -> bool:
        return any(v for v in (self.rpm, self.rpd, self.tpm, self.tpd, self.rph))


_VAR = re.compile(r"\$\{(\w+)\}")


@dataclass
class Provider:
    name: str
    url: str  # base URL as written, may contain ${VAR}
    api_key_env: str | None  # None: no key at all
    headers: dict = field(default_factory=dict)
    no_auth: bool = False  # usable without a key; a key (if api_key_env is set and present) is still sent
    pool: Limits = field(default_factory=Limits)
    shared_tpd: bool = False
    type: str = "openai"  # "openai" (chat completions over HTTP) or "chatgpt_web" (see chatgpt_web.py)
    options: dict = field(default_factory=dict)  # type-specific settings (the provider's `web:` block)

    @property
    def base_url(self) -> str:
        return _VAR.sub(lambda m: os.environ.get(m.group(1), ""), self.url).rstrip("/")

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None

    @property
    def usable(self) -> bool:
        if self.type == "chatgpt_web":  # off unless turned on (enabled: true, or PCORE_CHATGPT_WEB=1)
            from .chatgpt_web import enabled

            return enabled(self.options)
        if any(not os.environ.get(v) for v in _VAR.findall(self.url)):
            return False
        return self.no_auth or bool(self.api_key)

    def auth_headers(self) -> dict:
        key = self.api_key
        return {"Authorization": f"Bearer {key}"} if key else {}


@dataclass
class Model:
    id: str  # catalog id, unique (e.g. "groq/llama-3.3-70b")
    provider: str
    model: str  # the provider's model name
    score: float  # strength 0-100 (calibrated or prior)
    context: int = 32768
    limits: Limits = field(default_factory=Limits)
    calibrated: bool = False
    tags: list[str] = field(default_factory=list)
    pool: Limits | None = None  # the provider's shared limits (copied at load)
    shared_tpd: bool = False
    label: str | None = None  # the name shown where a source can be picked (web models), e.g. "GPT-6 Luna"
    accounts: list[str] | None = None  # web models: only these accounts of the provider have this model


@dataclass
class Catalog:
    providers: dict[str, Provider]
    models: dict[str, Model]
    tiers: list[tuple[str, float]]  # (name, minimum score), strongest first
    aliases: dict[str, str] = field(default_factory=dict)  # e.g. tier-1 -> nova, auto -> solstice
    # history pruning (prune.py): `prune: true`, a dict of its options, or {api: ..., web: ...}; off when absent
    prune: object = None

    def resolve_tier(self, name: str) -> str | None:
        """A tier name, or an alias of one; None if it's neither (a concrete model id, or unknown)."""
        names = [t for t, _ in self.tiers]
        name = self.aliases.get(name, name)
        if name in names:
            return name
        if name in ("auto", "default"):
            return names[min(1, len(names) - 1)]
        m = re.fullmatch(r"tier-(\d+)", name)
        if m and 1 <= int(m.group(1)) <= len(names):
            return names[int(m.group(1)) - 1]
        return None

    def tier_of(self, model: Model) -> str:
        for name, floor in self.tiers:
            if model.score >= floor:
                return name
        return self.tiers[-1][0]

    def tier_index(self, name: str) -> int:
        return [t for t, _ in self.tiers].index(name)

    def is_web(self, m: Model) -> bool:
        return self.providers[m.provider].type == "chatgpt_web"

    def models_in(self, tier: str, usable_only: bool = True) -> list[Model]:
        out = [m for m in self.models.values() if self.tier_of(m) == tier]
        if usable_only:
            out = [m for m in out if self.providers[m.provider].usable]
        return sorted(out, key=lambda m: -m.score)


def load(path: str | Path, scores_path: str | Path | None = None) -> Catalog:
    data = yaml.safe_load(Path(path).read_text())
    providers = {}
    for name, p in data["providers"].items():
        key_env = p.get("api_key_env")
        if str(key_env).lower() in ("none", "null", ""):
            key_env = None
        providers[name] = Provider(
            name=name,
            url=p["base_url"],
            api_key_env=key_env,
            headers=p.get("headers", {}),
            no_auth=key_env is None or str(p.get("auth", "")).lower() == "none",
            pool=Limits(**(p.get("pool") or {})),
            shared_tpd=bool(p.get("shared_tpd")),
            type=str(p.get("type") or "openai"),
            options={**(p.get("web") or {}), **({"enabled": True} if p.get("enabled") else {})},
        )
    models = {}
    for m in data["models"]:
        lim = Limits(**(m.get("limits") or {}))
        mid = m.get("id") or f"{m['provider']}/{m['model']}"
        if m["provider"] not in providers:
            raise ValueError(f"model {mid}: unknown provider {m['provider']}")
        models[mid] = Model(
            id=mid,
            provider=m["provider"],
            model=m["model"],
            score=float(m.get("score", 50)),
            context=int(m.get("context", 32768)),
            limits=lim,
            tags=m.get("tags", []),
            label=m.get("label"),
            accounts=[str(a) for a in m["accounts"]] if m.get("accounts") else None,
        )
        prov = providers[m["provider"]]
        models[mid].pool = prov.pool if prov.pool.any() else None
        models[mid].shared_tpd = prov.shared_tpd and bool(lim.tpd)
    # calibrated scores (written by `provenance_core calibrate`) override the priors in the catalog
    if scores_path and Path(scores_path).exists():
        for mid, s in (yaml.safe_load(Path(scores_path).read_text()) or {}).items():
            if mid in models:
                models[mid].score = float(s["score"] if isinstance(s, dict) else s)
                models[mid].calibrated = True
    tiers = sorted(((t["name"], float(t["min_score"])) for t in data["tiers"]), key=lambda t: -t[1])
    return Catalog(
        providers=providers,
        models=models,
        tiers=tiers,
        aliases=dict(data.get("aliases") or {}),
        prune=data.get("prune"),
    )
