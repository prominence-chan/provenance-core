"""Choosing a model for a request, calling it, and the ensemble modes.

Selection inside a tier: a session sticks to the model it last used (no mid-conversation personality swaps), then
models are ranked by score, quota headroom and latency. Falling back to a weaker tier only happens when the request
allows it (fallback="down1"/"any"); the default stays in the asked tier and fails loudly instead of silently
degrading. Ensembles fan a request out to k models of the same tier and combine the answers."""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from dataclasses import dataclass

import httpx

from .catalog import Catalog, Model
from .state import State


class NoCapacity(Exception):
    pass


def estimate_tokens(body: dict) -> int:
    text = json.dumps(body.get("messages", []))
    return len(text) // 4 + int(body.get("max_tokens") or body.get("max_completion_tokens") or 1024)


@dataclass
class Choice:
    model: Model
    rank: float


class Router:
    def __init__(self, catalog: Catalog, state: State | None = None, client: httpx.AsyncClient | None = None,
                 timeout: float = 120.0):
        self.catalog = catalog
        self.state = state or State()
        self.client = client or httpx.AsyncClient(timeout=timeout)

    # ---- selection -------------------------------------------------------------------------------------------
    def tiers_for(self, tier: str, fallback: str) -> list[str]:
        names = [t for t, _ in self.catalog.tiers]
        i = names.index(tier)
        if fallback == "any":
            return names[i:]
        if fallback == "down1":
            return names[i:i + 2]
        return [tier]

    def candidates(self, tier: str, tokens: int, session: str | None = None, fallback: str = "none") -> list[Model]:
        out: list[Model] = []
        sticky = self.state.sticky(session)
        for t in self.tiers_for(tier, fallback):
            ranked: list[Choice] = []
            for m in self.catalog.models_in(t):
                if not self.state.available(m) or m.context < tokens:
                    continue
                head = self.state.headroom(m, tokens)
                if head <= 0:
                    continue
                lat = self.state.health.get(m.id)
                lat_ms = lat.latency_ms if lat and lat.latency_ms else 3000
                # score dominates (stability), headroom spreads load before a model hits its cap, latency breaks ties
                rank = m.score + 6 * head - min(lat_ms, 30000) / 10000
                if m.id == sticky:
                    rank += 1000
                ranked.append(Choice(m, rank))
            out += [c.model for c in sorted(ranked, key=lambda c: -c.rank)]
        return out

    # ---- calling ---------------------------------------------------------------------------------------------
    async def call(self, m: Model, body: dict, stream: bool = False):
        p = self.catalog.providers[m.provider]
        payload = {**body, "model": m.model, "stream": stream}
        headers = {"Authorization": f"Bearer {p.api_key}", "Content-Type": "application/json", **p.headers}
        t0 = time.time()
        req = self.client.build_request("POST", f"{p.base_url}/chat/completions", json=payload, headers=headers)
        resp = await self.client.send(req, stream=stream)
        if resp.status_code == 429:
            ra = resp.headers.get("retry-after")
            self.state.failure(m, retry_after=float(ra) if ra and ra.replace(".", "").isdigit() else None,
                               rate_limited=True)
            await resp.aclose()
            raise RuntimeError(f"{m.id}: rate limited")
        if resp.status_code >= 400:
            text = (await resp.aread()).decode(errors="replace")[:300]
            await resp.aclose()
            self.state.failure(m)
            raise RuntimeError(f"{m.id}: HTTP {resp.status_code} {text}")
        if stream:
            return resp, t0
        data = resp.json()
        self.state.success(m, (time.time() - t0) * 1000)
        used = (data.get("usage") or {}).get("total_tokens") or estimate_tokens(body)
        self.state.record(m, used)
        return data

    async def complete(self, body: dict, tier: str, session: str | None = None, fallback: str = "none",
                       attempts: int = 4) -> tuple[dict, Model]:
        tokens = estimate_tokens(body)
        errors = []
        for m in self.candidates(tier, tokens, session, fallback)[:attempts]:
            try:
                data = await self.call(m, body)
                self.state.remember(session, m.id)
                return data, m
            except Exception as e:  # noqa: BLE001 - every provider error means "try the next one"
                errors.append(str(e)[:200])
        raise NoCapacity(f"no model in {tier} (fallback={fallback}) could serve this: {errors or 'none available'}")

    async def stream(self, body: dict, tier: str, session: str | None = None, fallback: str = "none"):
        tokens = estimate_tokens(body)
        errors = []
        for m in self.candidates(tier, tokens, session, fallback)[:4]:
            try:
                resp, t0 = await self.call(m, body, stream=True)
            except Exception as e:  # noqa: BLE001
                errors.append(str(e)[:200])
                continue
            self.state.remember(session, m.id)
            self.state.record(m, tokens)
            return resp, m, t0
        raise NoCapacity(f"no model in {tier} could stream this: {errors or 'none available'}")

    # ---- ensembles ---------------------------------------------------------------------------------------------
    async def ensemble(self, body: dict, tier: str, k: int = 3, strategy: str = "judge") -> tuple[dict, list[Model]]:
        """Ask k distinct models of the tier in parallel, then combine:
        - majority: the most common normalized final answer (good for short exact answers)
        - judge: the strongest available model of the tier reads all answers and returns the best (or a merged) one
        - first: the first successful answer (latency hedging)"""
        tokens = estimate_tokens(body)
        pool = self.candidates(tier, tokens)[:k]
        if not pool:
            raise NoCapacity(f"no model in {tier} for an ensemble")

        async def one(m):
            try:
                return m, await self.call(m, body)
            except Exception:  # noqa: BLE001
                return m, None

        if strategy == "first":
            tasks = [asyncio.create_task(one(m)) for m in pool]
            for fut in asyncio.as_completed(tasks):
                m, data = await fut
                if data:
                    for t in tasks:
                        t.cancel()
                    return data, [m]
            raise NoCapacity("every ensemble member failed")
        results = [(m, d) for m, d in await asyncio.gather(*(one(m) for m in pool)) if d]
        if not results:
            raise NoCapacity("every ensemble member failed")
        texts = [(m, d["choices"][0]["message"].get("content") or "") for m, d in results]
        if strategy == "majority" or len(results) == 1:
            def norm(s):
                last = s.strip().splitlines()[-1] if s.strip() else ""
                return re.sub(r"[^a-z0-9.\-]", "", last.lower())
            votes = Counter(norm(t) for _, t in texts)
            top = votes.most_common(1)[0][0]
            # among the agreeing answers, return the strongest model's full response
            m, d = max(((m, d) for (m, d), (_, t) in zip(results, texts) if norm(t) == top), key=lambda x: x[0].score)
            d.setdefault("tierlm", {})["votes"] = dict(votes)
            return d, [m for m, _ in results]
        judge_m = max((m for m, _ in results), key=lambda m: m.score)
        question = body.get("messages", [])[-1].get("content", "") if body.get("messages") else ""
        listing = "\n\n".join(f"### Answer {i + 1}\n{t}" for i, (_, t) in enumerate(texts))
        jbody = {"messages": [{"role": "system", "content": "You compare candidate answers to the same request and "
                               "write the single best final answer. Fix mistakes you can verify; keep the best "
                               "answer's form. Output only the final answer."},
                              {"role": "user", "content": f"Request:\n{question}\n\n{listing}"}],
                 "temperature": 0}
        try:
            judged = await self.call(judge_m, jbody)
        except Exception:  # noqa: BLE001 - judge failed: fall back to the strongest member's own answer
            return max(results, key=lambda r: r[0].score)[1], [m for m, _ in results]
        judged.setdefault("tierlm", {})["members"] = [m.id for m, _ in results]
        return judged, [m for m, _ in results]
