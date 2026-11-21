"""OpenAI-compatible HTTP API.

  POST /v1/chat/completions   model = "tier-1" | "tier-2" | ... | "auto" | a concrete catalog id
                              optional body fields: "tierlm": {"fallback": "none|down1|any",
                                                               "ensemble": {"k": 3, "strategy": "judge|majority|first"}}
                              optional header X-Tierlm-Session: sticky model per conversation
  GET  /v1/models             the tier aliases plus every usable concrete model
  GET  /status                tiers, models, scores, headroom, health"""
from __future__ import annotations

import os
import time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .catalog import load
from .router import NoCapacity, Router, estimate_tokens


def create_app(catalog_path: str | None = None, scores_path: str | None = None, router: Router | None = None) -> FastAPI:
    if router is None:
        catalog = load(catalog_path or os.environ.get("TIERLM_CATALOG", "catalog.yaml"),
                       scores_path or os.environ.get("TIERLM_SCORES", "scores.yaml"))
        router = Router(catalog)
    app = FastAPI(title="tierlm")
    token = os.environ.get("TIERLM_TOKEN")  # optional bearer token for the proxy itself

    def check(auth: str | None):
        if token and auth != f"Bearer {token}":
            raise HTTPException(401, "bad token")

    def resolve(name: str) -> tuple[str | None, str | None]:
        """(tier, concrete model id)"""
        tiers = [t for t, _ in router.catalog.tiers]
        if name in tiers:
            return name, None
        if name in ("auto", "default"):
            return tiers[0], None
        if name in router.catalog.models:
            return None, name
        raise HTTPException(404, f"unknown model {name}; use one of {tiers} or a catalog id")

    @app.get("/v1/models")
    async def models(authorization: str | None = Header(None)):
        check(authorization)
        now = int(time.time())
        data = [{"id": t, "object": "model", "created": now, "owned_by": "tierlm"} for t, _ in router.catalog.tiers]
        data += [{"id": m.id, "object": "model", "created": now, "owned_by": m.provider}
                 for m in router.catalog.models.values() if router.catalog.providers[m.provider].api_key]
        return {"object": "list", "data": data}

    @app.get("/status")
    async def status(authorization: str | None = Header(None)):
        check(authorization)
        out = {}
        for t, floor in router.catalog.tiers:
            out[t] = {"min_score": floor, "models": [
                {"id": m.id, "score": m.score, "calibrated": m.calibrated,
                 "usable": bool(router.catalog.providers[m.provider].api_key),
                 "available": router.state.available(m), "headroom": round(router.state.headroom(m, 1000), 3),
                 "latency_ms": (router.state.health.get(m.id).latency_ms if router.state.health.get(m.id) else None)}
                for m in router.catalog.models_in(t, usable_only=False)]}
        return out

    @app.post("/v1/chat/completions")
    async def chat(request: Request, authorization: str | None = Header(None),
                   x_tierlm_session: str | None = Header(None)):
        check(authorization)
        body = await request.json()
        opts = body.pop("tierlm", None) or {}
        tier, concrete = resolve(body.get("model", "auto"))
        fallback = opts.get("fallback", "none")
        try:
            if concrete:
                m = router.catalog.models[concrete]
                if body.get("stream"):
                    resp, t0 = await router.call(m, body, stream=True)
                    router.state.record(m, estimate_tokens(body))
                    return _stream(resp, m, router, t0)
                data = await router.call(m, body)
                return JSONResponse(data, headers={"X-Tierlm-Model": m.id})
            ens = opts.get("ensemble")
            if ens:
                data, members = await router.ensemble(body, tier, int(ens.get("k", 3)), ens.get("strategy", "judge"))
                return JSONResponse(data, headers={"X-Tierlm-Model": ",".join(m.id for m in members)})
            if body.get("stream"):
                resp, m, t0 = await router.stream(body, tier, x_tierlm_session, fallback)
                return _stream(resp, m, router, t0)
            data, m = await router.complete(body, tier, x_tierlm_session, fallback)
            return JSONResponse(data, headers={"X-Tierlm-Model": m.id, "X-Tierlm-Tier": router.catalog.tier_of(m)})
        except NoCapacity as e:
            raise HTTPException(503, str(e)) from e

    return app


def _stream(resp, m, router, t0):
    async def gen():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
            router.state.success(m, (time.time() - t0) * 1000)
        finally:
            await resp.aclose()
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Tierlm-Model": m.id})
