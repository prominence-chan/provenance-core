"""Model catalog: providers (OpenAI-compatible endpoints + one API key each), models with a strength score
(0-100, from `tierlm calibrate` or a prior), free-tier limits, and the tier each score falls into.

Tiers are the point of the project: a request asks for a tier ("nova", "solstice", "spark", "ember"; "tier-N" works too) and is only ever served by
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
    rpm: int | None = None   # requests per minute
    rpd: int | None = None   # requests per day
    tpm: int | None = None   # tokens per minute
    tpd: int | None = None   # tokens per day


@dataclass
class Provider:
    name: str
    base_url: str
    api_key_env: str
    headers: dict = field(default_factory=dict)

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


@dataclass
class Model:
    id: str            # catalog id, unique (e.g. "groq/llama-3.3-70b")
    provider: str
    model: str         # the provider's model name
    score: float       # strength 0-100 (calibrated or prior)
    context: int = 32768
    limits: Limits = field(default_factory=Limits)
    calibrated: bool = False
    tags: list[str] = field(default_factory=list)


@dataclass
class Catalog:
    providers: dict[str, Provider]
    models: dict[str, Model]
    tiers: list[tuple[str, float]]   # (name, minimum score), strongest first
    aliases: dict[str, str] = field(default_factory=dict)   # e.g. tier-1 -> nova, auto -> solstice

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

    def models_in(self, tier: str, usable_only: bool = True) -> list[Model]:
        out = [m for m in self.models.values() if self.tier_of(m) == tier]
        if usable_only:
            out = [m for m in out if self.providers[m.provider].api_key]
        return sorted(out, key=lambda m: -m.score)


def load(path: str | Path, scores_path: str | Path | None = None) -> Catalog:
    data = yaml.safe_load(Path(path).read_text())
    providers = {name: Provider(name=name, base_url=p["base_url"].rstrip("/"), api_key_env=p["api_key_env"],
                                headers=p.get("headers", {}))
                 for name, p in data["providers"].items()}
    models = {}
    for m in data["models"]:
        lim = Limits(**(m.get("limits") or {}))
        mid = m.get("id") or f"{m['provider']}/{m['model']}"
        if m["provider"] not in providers:
            raise ValueError(f"model {mid}: unknown provider {m['provider']}")
        models[mid] = Model(id=mid, provider=m["provider"], model=m["model"], score=float(m.get("score", 50)),
                            context=int(m.get("context", 32768)), limits=lim, tags=m.get("tags", []))
    # calibrated scores (written by `tierlm calibrate`) override the priors in the catalog
    if scores_path and Path(scores_path).exists():
        for mid, s in (yaml.safe_load(Path(scores_path).read_text()) or {}).items():
            if mid in models:
                models[mid].score = float(s["score"] if isinstance(s, dict) else s)
                models[mid].calibrated = True
    tiers = sorted(((t["name"], float(t["min_score"])) for t in data["tiers"]), key=lambda t: -t[1])
    return Catalog(providers=providers, models=models, tiers=tiers, aliases=dict(data.get("aliases") or {}))
