"""Choosing a model for a request, calling it, and the ensemble modes.

Selection inside a tier: a session sticks to the model it last used (no mid-conversation personality swaps), then
models are ranked by score, quota headroom and latency. Falling back to a weaker tier only happens when the request
allows it (fallback="down1"/"any"); the default stays in the asked tier and fails loudly instead of silently
degrading. Ensembles fan a request out to k models of the same tier and combine the answers."""

# Inspired in part by tashfeenahmed/freellmapi.
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from dataclasses import dataclass

import httpx

from . import compact, context_signal, prune
from .catalog import Catalog, Model
from .state import State


class NoCapacity(Exception):
    pass


def _home():
    from .settings import home_dir

    return home_dir()


def estimate_tokens(body: dict) -> int:
    text = json.dumps(body.get("messages", []))
    return len(text) // 4 + int(body.get("max_tokens") or body.get("max_completion_tokens") or 1024)


@dataclass
class Choice:
    model: Model
    rank: float


class Router:
    def __init__(
        self,
        catalog: Catalog,
        state: State | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
        home=None,
    ):
        self.catalog = catalog
        self.state = state or State()
        self.client = client or httpx.AsyncClient(timeout=timeout)
        self.bridges: dict = {}  # provider name -> chatgpt_web.Bridge (made on first use)
        self.inflight: dict[str, int] = {}  # model id -> requests being served right now
        self._view: tuple | None = None  # the last api_view: (body, pruned body, report)
        # summarising compaction's briefs, shared by API models and the web bridges; model-written ones are also
        # kept on disk under PCORE_HOME (only written when compaction with a model is switched on)
        self.compactor = compact.Compactor(home or _home())

    # ---- selection -------------------------------------------------------------------------------------------
    def tiers_for(self, tier: str, fallback: str) -> list[str]:
        names = [t for t, _ in self.catalog.tiers]
        i = names.index(tier)
        if fallback == "any":
            return names[i:]
        if fallback == "down1":
            return names[i : i + 2]
        return [tier]

    def bridge(self, provider: str):
        from .chatgpt_web import make_bridge

        if provider not in self.bridges:
            p = self.catalog.providers[provider]
            opts = dict(p.options)
            for k in ("prune", "compact"):  # the catalog's top-level setting, unless the provider has its own
                opts.setdefault(k, getattr(self.catalog, k))
            share = opts.get("share")  # work inside another web provider's Chrome windows
            b = self.bridges[provider] = make_bridge(p.base_url, opts, self.bridge(share) if share else None)
            for one in getattr(b, "bridges", {"": b}).values():
                one.compactor, one.summarize = self.compactor, self.summarize
        return self.bridges[provider]

    def web_ok(self, endpoint: str) -> bool:
        """May tier requests arriving on this front end (responses / messages / chat) use a chatgpt_web provider?"""
        from .chatgpt_web import DEFAULTS

        return any(
            endpoint in (p.options.get("endpoints") or DEFAULTS["endpoints"])
            for p in self.catalog.providers.values()
            if p.type == "chatgpt_web"
        )

    # A tier is served from sources: each web model is one (a signed-in chat site: "GPT-6 Luna", "Qwen4-Max"), and
    # all the API models of the tier together are one more, the token pool. A request that names only the tier goes
    # to the source with the most of its usage left for the work it already has; a client may also pin a source.
    def source_id(self, m: Model) -> str:
        return m.model.lower() if self.catalog.is_web(m) else "pool"

    def _source_load(self, models: list[Model], tokens: int = 1024) -> dict:
        """remaining: share (0-1) of the source's usage that is left; busy: requests it is serving now."""
        busy = sum(self.inflight.get(m.id, 0) for m in models)
        if self.catalog.is_web(models[0]):
            m = models[0]
            c = self.bridge(m.provider).capacity(m.accounts)
            ok = c["available"] and self.state.available(m)
            return {
                "remaining": c["remaining"] if ok else 0.0,
                "busy": max(busy, c["busy"]),
                "slots": max(1, c["accounts"]),
                "available": ok,
                "needs": None if c.get("signed_in", True) else "signin",
            }
        heads = [self.state.headroom(m, tokens) for m in models if self.state.available(m)]
        heads = [h for h in heads if h > 0]
        return {
            "remaining": sum(heads) / len(models) if heads else 0.0,
            "busy": busy,
            "slots": max(1, len(heads)),
            "available": bool(heads),
            "needs": None,
        }

    def sources(self, tier: str, web: bool = True) -> list[dict]:
        """The tier's sources, best first: most usage left per request already running on it."""
        groups: dict[str, list[Model]] = {}
        for m in self.catalog.models_in(tier):
            if web or not self.catalog.is_web(m):
                groups.setdefault(self.source_id(m), []).append(m)
        out = []
        for sid, ms in groups.items():
            load = self._source_load(ms)
            is_web = self.catalog.is_web(ms[0])
            out.append(
                {
                    "id": sid,
                    "label": (ms[0].label or ms[0].model) if is_web else "Token pool",
                    "kind": "web" if is_web else "pool",
                    "models": [m.id for m in ms],
                    **load,
                    "rank": load["remaining"] / (1 + load["busy"] / load["slots"]) if load["available"] else -1.0,
                    # worth offering as a pin only while a fair part of its usage is left
                    "pinnable": load["available"] and load["remaining"] >= 0.1,
                }
            )
        if "pool" not in groups and any(
            not self.catalog.is_web(m) for m in self.catalog.models_in(tier, usable_only=False)
        ):  # the tier has API models, none with a key: the pool is listed as needing keys rather than left out
            out.append({"id": "pool", "label": "Token pool", "kind": "pool", "models": [], "remaining": 0.0,
                        "busy": 0, "slots": 1, "available": False, "needs": "keys", "rank": -1.0,
                        "pinnable": False})  # fmt: skip
        return sorted(out, key=lambda s: -s["rank"])

    def candidates(
        self,
        tier: str,
        tokens: int,
        session: str | None = None,
        fallback: str = "none",
        web: bool = False,
        source: str | None = None,
        compactable: dict | None = None,
    ) -> list[Model]:
        """`web`: include chatgpt_web models. Off by default so ensembles, calibration and anything that didn't ask
        never reach the web bridge; the front ends pass it per the provider's `endpoints`. `source`: the pinned
        source of the first tier, tried before the others. A conversation stays on the model that served it.
        `compactable`: compaction's options when it is on for API models: then a model whose context window is
        smaller than the request still qualifies (it is sent a compacted history; see api_body), after the models
        of its tier that take the history whole."""
        out: list[Model] = []
        sticky = self.state.sticky(session)
        for i, t in enumerate(self.tiers_for(tier, fallback)):
            by_source: dict[str, list[Choice]] = {}
            short: set[str] = set()  # models that would get a compacted history
            for m in self.catalog.models_in(t):
                if not self.state.available(m):
                    continue
                need = tokens
                if m.context < tokens:
                    if self.catalog.is_web(m) or not compactable or m.context < int(compactable["min_context"]):
                        continue
                    need = int(m.context * float(compactable["fill"]))
                    short.add(m.id)
                if not web and self.catalog.is_web(m):
                    continue
                head = self.state.headroom(m, need)
                if head <= 0:
                    continue
                lat = self.state.health.get(m.id)
                lat_ms = lat.latency_ms if lat and lat.latency_ms else 3000
                # inside the pool: score dominates (stability), headroom spreads load, latency breaks ties
                rank = m.score + 6 * head - min(lat_ms, 30000) / 10000
                by_source.setdefault(self.source_id(m), []).append(Choice(m, rank))
            order = [s["id"] for s in self.sources(t, web) if s["id"] in by_source]
            if source and i == 0 and source in by_source:
                order = [source] + [s for s in order if s != source]
            tier_models = [c.model for s in order for c in sorted(by_source[s], key=lambda c: -c.rank)]
            tier_models.sort(key=lambda m: m.id in short)  # stable: the whole-history models first
            pinned_here = source and i == 0 and source in by_source
            for m in tier_models:
                if m.id == sticky and (not pinned_here or self.source_id(m) == source):
                    tier_models = [m] + [x for x in tier_models if x is not m]
                    break
            out += tier_models
        return out

    # ---- calling ---------------------------------------------------------------------------------------------
    def api_view(self, body: dict) -> tuple[dict, dict | None]:
        """(the body as an API model gets it, what pruning removed or None). With `prune` on for "api" the history is
        pruned in prune.py's stable mode: only the part before an epoch boundary that moves every `step` assistant
        turns, judged on that part alone, so consecutive requests still share their prefix byte for byte and provider
        prompt caches keep hitting. The web bridge always gets the body as sent (it prunes its own fresh prompts)."""
        opts = prune.config("api", self.catalog.prune)
        if not opts or body.get("_pcore_internal"):
            return body, None
        if self._view and self._view[0] is body:
            return self._view[1], self._view[2]
        pruned, report = prune.prune_body(body, "stable", opts)
        self._view = (body, pruned, report)
        return pruned, report

    def compact_options(self) -> dict | None:
        return compact.config("api", self.catalog.compact)

    async def api_body(self, m: Model, body: dict) -> tuple[dict, dict | None, dict | None]:
        """(the body model `m` is sent, what pruning removed, what compaction did). Compaction (`compact` on for
        "api") only touches a request that does not fit the model's context window: the history before a cut on
        prune's grid becomes a brief, made from the history as the client sent it and cached per cut, so every
        request with the same cut starts with the same bytes; the rest is sent as api_view has it."""
        view, pruned = self.api_view(body)
        o = self.compact_options()
        if not o or body.get("_pcore_internal") or estimate_tokens(view) <= m.context:
            return view, pruned, None
        max_t = int(view.get("max_tokens") or view.get("max_completion_tokens") or 1024)
        budget = int((m.context * float(o["fill"]) - max_t) * 4)  # in estimate_tokens' characters
        msgs, rep = await self.compactor.acompact(
            body.get("messages") or [], view.get("messages") or [], budget, o, compact.json_size, "api", self.summarize
        )
        return ({**view, "messages": msgs} if rep else view), pruned, rep

    async def summarize(self, system: str, text: str, who: str, max_chars: int) -> tuple[str, str]:
        """compact.py's narrative writer: one request to the API models of tier `who` (or to the API model with that
        id). Never a web bridge: it is called with the history the bridge could not take."""
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
            "temperature": 0,
            "max_tokens": max(512, max_chars // 2),
            "_pcore_internal": True,
        }
        if who in self.catalog.models:
            m = self.catalog.models[who]
            if self.catalog.is_web(m):
                raise ValueError(f"{who} is a web model; compaction only asks API models")
            data = await self.call(m, body)
        else:
            tier = self.catalog.resolve_tier(who)
            if not tier:
                raise ValueError(f"compaction model {who!r} is neither a tier nor a model id")
            data, m = await self.complete(body, tier, attempts=2, web=False)
        return (data["choices"][0]["message"].get("content") or ""), m.id

    async def call(self, m: Model, body: dict, stream: bool = False):
        p = self.catalog.providers[m.provider]
        if p.type == "chatgpt_web":
            return await self._web_call(m, body, stream)
        body, pruned, compacted = await self.api_body(m, body)
        if pruned and pruned["saved"]:
            print(f"prune: {m.id} history {prune.summary(pruned)}", flush=True)
        if compacted:
            print(f"compact: {m.id} history {compact.summary(compacted)}", flush=True)
        context_signal.note_compaction(compacted)
        payload = {**{k: v for k, v in body.items() if not k.startswith("_")}, "model": m.model, "stream": stream}
        headers = {**p.auth_headers(), "Content-Type": "application/json", **p.headers}
        t0 = time.time()
        req = self.client.build_request("POST", f"{p.base_url}/chat/completions", json=payload, headers=headers)
        resp = await self.client.send(req, stream=stream)
        if resp.status_code == 429:
            ra = resp.headers.get("retry-after")
            self.state.failure(
                m, retry_after=float(ra) if ra and ra.replace(".", "").isdigit() else None, rate_limited=True
            )
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
        if pruned and pruned["saved"]:
            data.setdefault("pcore", {})["prune"] = pruned
        if compacted:
            data.setdefault("pcore", {})["compact"] = compacted
        self.state.success(m, (time.time() - t0) * 1000)
        used = (data.get("usage") or {}).get("total_tokens") or estimate_tokens(body)
        self.state.record(m, used)
        return data

    async def _web_call(self, m: Model, body: dict, stream: bool, wait: float | None = None):
        """A chatgpt_web model: the whole reply is read from the page first, so a failure (login wall, challenge,
        closed window, bad tool call) surfaces here and the caller can still fail over."""
        from .chatgpt_web import LiveStream, WebSkip, WebUnavailable

        t0 = time.time()
        size = len(json.dumps(body.get("messages", []), default=str))

        def log(what: str):  # one line per attempt in the server log: the bridge is slow and worth being able to follow
            print(f"chatgpt-web: {m.id} {what} ({size} chars in, {time.time() - t0:.0f} s)", flush=True)

        def done(data):
            msg = data["choices"][0]["message"]
            web = data["pcore"]["web"]
            context_signal.note_web(web)
            log(
                f"ok: {len(msg.get('content') or '')} chars out, {len(msg.get('tool_calls') or [])} tool call(s)"
                + (", repaired" if web.get("repaired") else "")
                + (", nudged" if web.get("nudged") else "")
                + (", continued chat" if web.get("continued") else ", new chat")
                + (", previous chat was full" if web.get("restarted") else "")
                + (f", history {prune.summary(web['pruned'])}" if (web.get("pruned") or {}).get("saved") else "")
                + (
                    f", history summarised ({compact.summary(web['compaction'])})"
                    if web.get("compaction")
                    else ", history shortened"
                    if web.get("compacted")
                    else ""
                )
                + f", {web.get('sent_chars', '?')} chars typed"
                + (f", account {web['account']}" if web.get("account") else "")
            )

        bridge = self.bridge(m.provider)
        try:
            only = {"accounts": m.accounts} if m.accounts and hasattr(bridge, "bridges") else {}
            if stream:
                live = LiveStream(
                    lambda on_text: bridge.complete(m.model, body, model_id=m.id, wait=wait, on_text=on_text, **only),
                    m.id,
                    on_done=done,
                )
                await live.first()
                self.state.success(m, (time.time() - t0) * 1000)
                return live, t0
            data = await bridge.complete(m.model, body, model_id=m.id, wait=wait, **only)
        except WebUnavailable as e:
            self.state.failure(m, retry_after=max(1.0, e.cooldown), rate_limited=True)
            log(f"unavailable for {int(e.cooldown)} s: {e.reason}")
            raise RuntimeError(f"{m.id}: unavailable: {e.reason}") from e
        except WebSkip as e:
            log(f"skipped: {e}")
            raise RuntimeError(f"{m.id}: skipped: {e}") from e
        except Exception as e:  # noqa: BLE001
            self.state.failure(m)
            log(f"failed: {e}")
            raise RuntimeError(f"{m.id}: {e}") from e
        self.state.success(m, (time.time() - t0) * 1000)
        done(data)
        self.state.record(m, data["usage"]["total_tokens"])
        return data

    async def complete(
        self,
        body: dict,
        tier: str,
        session: str | None = None,
        fallback: str = "none",
        attempts: int = 4,
        web: bool = False,
        source: str | None = None,
    ) -> tuple[dict, Model]:
        tokens = estimate_tokens(self.api_view(body)[0])  # the size an API model would be sent
        errors = []
        for m in self.candidates(tier, tokens, session, fallback, web, source, self.compact_options())[:attempts]:
            self.inflight[m.id] = self.inflight.get(m.id, 0) + 1
            try:
                data = await self.call(m, body)
                self.state.remember(session, m.id)
                return data, m
            except Exception as e:  # noqa: BLE001 - every provider error means "try the next one"
                errors.append(str(e)[:200])
            finally:
                self.inflight[m.id] -= 1
        raise NoCapacity(f"no model in {tier} (fallback={fallback}) could serve this: {errors or 'none available'}")

    async def stream(
        self,
        body: dict,
        tier: str,
        session: str | None = None,
        fallback: str = "none",
        web: bool = False,
        source: str | None = None,
    ):
        tokens = estimate_tokens(self.api_view(body)[0])
        errors = []
        for m in self.candidates(tier, tokens, session, fallback, web, source, self.compact_options())[:4]:
            self.inflight[m.id] = self.inflight.get(m.id, 0) + 1
            try:
                resp, t0 = await self.call(m, body, stream=True)
            except Exception as e:  # noqa: BLE001
                errors.append(str(e)[:200])
                continue
            finally:
                self.inflight[m.id] -= 1
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
            m, d = max(
                ((m, d) for (m, d), (_, t) in zip(results, texts, strict=False) if norm(t) == top),
                key=lambda x: x[0].score,
            )
            d.setdefault("pcore", {})["votes"] = dict(votes)
            return d, [m for m, _ in results]
        judge_m = max((m for m, _ in results), key=lambda m: m.score)
        question = body.get("messages", [])[-1].get("content", "") if body.get("messages") else ""
        listing = "\n\n".join(f"### Answer {i + 1}\n{t}" for i, (_, t) in enumerate(texts))
        jbody = {
            "messages": [
                {
                    "role": "system",
                    "content": "You compare candidate answers to the same request and "
                    "write the single best final answer. Fix mistakes you can verify; keep the best "
                    "answer's form. Output only the final answer.",
                },
                {"role": "user", "content": f"Request:\n{question}\n\n{listing}"},
            ],
            "temperature": 0,
        }
        try:
            judged = await self.call(judge_m, jbody)
        except Exception:  # noqa: BLE001 - judge failed: fall back to the strongest member's own answer
            return max(results, key=lambda r: r[0].score)[1], [m for m, _ in results]
        judged.setdefault("pcore", {})["members"] = [m.id for m, _ in results]
        return judged, [m for m, _ in results]
