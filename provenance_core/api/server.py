"""OpenAI-compatible HTTP API.

POST /v1/chat/completions   model = a tier name or alias (see config/catalog.yaml) | "auto" | a concrete catalog id
                            optional body fields: "pcore": {"fallback": "none|down1|any",
                                                             "ensemble": {"k": 3, "strategy": "judge|majority|first"}}
                            optional header X-PCore-Session: sticky model per conversation
GET  /v1/models             the tier aliases plus every usable concrete model
GET  /status                tiers, models, scores, headroom, health
POST /v1/messages           Anthropic Messages API (Claude Code), see anthropic_api.py
POST /v1/responses          OpenAI Responses API (Codex), see responses_api.py
/web/*                      account setup for the web-chat bridge from the app, see web_setup.py
GET  /health                is it up, which process, what it serves (for the app that started it)
/pool/keys                  API keys for the token pool, any number per provider, set from the app, see pool_keys.py
                            GET /pool/keys | PUT, POST, DELETE /pool/keys/{provider} | PATCH, DELETE
                            /pool/keys/{provider}/{key_id}
GET  /pool/help             how to get a free key from each pool provider (?provider=<id> for one), see pool_help.py
POST /pool/help/{provider}/outcome   how following a provider's steps went (no keys, nothing personal)
/codex-sub/<key>/backend-api/*  Codex on your own ChatGPT sign-in, forwarded to OpenAI with history pruning, see
                            codex_sub.py (the key is derived from PCORE_TOKEN; Authorization is the caller's own)"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import __version__
from . import anthropic_api, codex_sub, responses_api
from ..keys import pool_help, pool_keys
from ..web import web_setup
from ..routing.catalog import load
from ..routing.router import NoCapacity, Router, estimate_tokens
from ..settings import config_path, env, home_dir


def create_app(
    catalog_path: str | None = None, scores_path: str | None = None, router: Router | None = None
) -> FastAPI:
    if router is None:
        catalog = load(
            config_path(catalog_path or env("CATALOG"), "catalog.yaml"),
            config_path(scores_path or env("SCORES"), "scores.yaml"),
        )
        router = Router(catalog)
    app = FastAPI(title="provenance-core")
    token = env("TOKEN")  # optional bearer token for the proxy itself

    def check(auth: str | None):
        if token and auth != f"Bearer {token}":
            raise HTTPException(401, "bad token")

    def resolve(name: str) -> tuple[str | None, str | None]:
        """(tier, concrete model id)"""
        tier = router.catalog.resolve_tier(name)
        if tier:
            return tier, None
        if name in router.catalog.models:
            return None, name
        tiers = [t for t, _ in router.catalog.tiers]
        raise HTTPException(404, f"unknown model {name}; use one of {tiers} or a catalog id")

    started = time.time()

    @app.get("/health")
    async def health(authorization: str | None = Header(None)):
        """For the app that started this router (and anyone checking which router answers): the process, its home,
        and what it can serve right now. Cheap: nothing is opened or fetched."""
        check(authorization)
        cat = router.catalog
        api = [p for p in cat.providers.values() if p.type == "openai"]
        web = [n for n, p in cat.providers.items() if p.type == "chatgpt_web"]
        return {
            "ok": True,
            "service": "provenance-core",
            "version": __version__,
            "pid": os.getpid(),
            "home": str(home_dir()),
            "uptime_s": int(time.time() - started),
            "api": {
                "providers": len(api),
                "usable": sum(p.usable for p in api),
                "models": sum(1 for m in cat.models.values() if not cat.is_web(m) and cat.providers[m.provider].usable),
                # the keys in use, over all providers (not their anonymous slots), and what they add up to: one
                # slot per usable model per key or anonymous access it can be called with
                "keys": sum(1 for p in api if p.usable for k in p.slots() if k.value),
                "slots": sum(
                    len(cat.providers[m.provider].slots())
                    for m in cat.models.values()
                    if not cat.is_web(m) and cat.providers[m.provider].usable
                ),
            },
            "web": {"providers": len(web), "enabled": [n for n in web if cat.providers[n].usable]},
            "tiers": [t for t, _ in cat.tiers],
        }

    @app.get("/v1/sources")
    async def sources(authorization: str | None = Header(None)):
        """Per tier: what it can be served from (each signed-in chat model, and the pooled API models as one), how
        much of each one's usage is left and how busy it is. `model` is the name a client sends to pin that source."""
        check(authorization)
        out = []
        for t, _ in router.catalog.tiers:
            out.append(
                {
                    "tier": t,
                    "sources": [
                        {
                            "id": s["id"],
                            "label": s["label"],
                            "kind": s["kind"],
                            "model": f"{s['id']}/{t}",
                            "remaining": round(s["remaining"], 3),
                            # the pool: what is left added up over every model and key (1.0 = one model on one
                            # key with its whole allowance), and how many keys that is; null for a chat site
                            "capacity": round(s["capacity"], 3) if s.get("capacity") is not None else None,
                            "keys": s.get("keys"),
                            "busy": s["busy"],
                            "available": s["available"],
                            "pinnable": s["pinnable"],
                            # why it cannot be used: "keys" (no API key for any of its models), "signin" (no
                            # account signed in), else null
                            "needs": s["needs"],
                        }
                        for s in router.sources(t, web=router.web_ok("responses"))
                    ],
                }
            )
        return {"tiers": out}

    @app.get("/v1/models")
    async def models(authorization: str | None = Header(None)):
        check(authorization)
        now = int(time.time())
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        # OpenAI list shape plus the Anthropic one (type/display_name/created_at, has_more/first_id/last_id), so
        # both SDKs' models.list() parse it
        # tier entries keep owned_by "tierlm": existing clients (the Provenance UI) pick out tiers by it
        data = [{"id": t, "object": "model", "created": now, "owned_by": "tierlm"} for t, _ in router.catalog.tiers]
        data += [
            {"id": m.id, "object": "model", "created": now, "owned_by": m.provider}
            for m in router.catalog.models.values()
            if router.catalog.providers[m.provider].usable
        ]
        for d in data:
            d.update(type="model", display_name=d["id"], created_at=iso)
        return {
            "object": "list",
            "data": data,
            "has_more": False,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
        }

    @app.get("/status")
    async def status(authorization: str | None = Header(None)):
        check(authorization)
        out = {}
        for t, floor in router.catalog.tiers:
            out[t] = {
                "min_score": floor,
                "models": [
                    {
                        "id": m.id,
                        "score": m.score,
                        "calibrated": m.calibrated,
                        "usable": router.catalog.providers[m.provider].usable,
                        "available": router.state.available(m),
                        # headroom: the best of the model's keys (0-1); capacity: all its keys added up
                        "headroom": round(router.state.headroom(m, 1000), 3),
                        "capacity": round(router.state.capacity(m, 1000), 3),
                        "keys": [
                            {
                                "id": k.id,
                                "available": router.state.available(m, k),
                                "headroom": round(router.state.headroom(m, 1000, k), 3),
                            }
                            for k in router.state.keys(m)
                            if k is not None
                        ],
                        "latency_ms": (
                            router.state.health.get(m.id).latency_ms if router.state.health.get(m.id) else None
                        ),
                    }
                    for m in router.catalog.models_in(t, usable_only=False)
                ],
            }
        return out

    @app.post("/v1/chat/completions")
    async def chat(
        request: Request,
        authorization: str | None = Header(None),
        x_pcore_session: str | None = Header(None),
        x_tierlm_session: str | None = Header(None),
    ):
        check(authorization)
        body = await request.json()
        opts = body.pop("pcore", None) or body.pop("tierlm", None) or {}
        body.pop("tierlm", None)
        session = x_pcore_session or x_tierlm_session
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
                return JSONResponse(data, headers={"X-PCore-Model": m.id})
            ens = opts.get("ensemble")
            if ens:
                data, members = await router.ensemble(body, tier, int(ens.get("k", 3)), ens.get("strategy", "judge"))
                return JSONResponse(data, headers={"X-PCore-Model": ",".join(m.id for m in members)})
            if body.get("stream"):
                resp, m, t0 = await router.stream(body, tier, session, fallback, web=router.web_ok("chat"))
                return _stream(resp, m, router, t0)
            data, m = await router.complete(body, tier, session, fallback, web=router.web_ok("chat"))
            return JSONResponse(data, headers={"X-PCore-Model": m.id, "X-PCore-Tier": router.catalog.tier_of(m)})
        except NoCapacity as e:
            raise HTTPException(503, str(e)) from e
        except (RuntimeError, httpx.HTTPError) as e:  # a concrete model failed: report it instead of a bare 500
            raise HTTPException(429 if "rate limited" in str(e) else 502, str(e)[:300]) from e

    # ---- chatgpt_web provider: status, live check, freeing the profile for `weblogin` ----------------------------
    def web_provider() -> str | None:
        return next((n for n, p in router.catalog.providers.items() if p.type == "chatgpt_web"), None)

    def acct(b, account: str | None) -> dict:  # only a pool of accounts takes an account name
        if not account or not hasattr(b, "bridges"):
            return {}
        if account not in b.bridges:
            raise HTTPException(404, f"no account {account!r}")
        return {"account": account}

    def web_models(name: str):
        return [m for m in router.catalog.models.values() if m.provider == name]

    @app.get("/chatgpt-web/status")
    async def web_status(probe: int = 0, account: str | None = None, authorization: str | None = Header(None)):
        check(authorization)
        name = web_provider()
        if not name:
            raise HTTPException(404, "no chatgpt_web provider in the catalog")
        on = router.catalog.providers[name].usable
        b = router.bridge(name)
        if probe and on:  # opens a fresh temporary chat, sends nothing
            st = await b.check(next((m.model for m in web_models(name)), "auto"), **acct(b, account))
            if st["ok"]:
                for m in web_models(name):
                    router.state.health.pop(m.id, None)
        else:
            st = b.status()
        return {
            "enabled": on,
            **st,
            "models": [
                {
                    "id": m.id,
                    "tier": router.catalog.tier_of(m),
                    "score": m.score,
                    "calibrated": m.calibrated,
                    "available": router.state.available(m),
                }
                for m in web_models(name)
            ],
        }

    @app.post("/chatgpt-web/release")
    async def web_release(reset: int = 0, account: str | None = None, authorization: str | None = Header(None)):
        check(authorization)
        name = web_provider()
        if not name:
            raise HTTPException(404, "no chatgpt_web provider in the catalog")
        b = router.bridge(name)
        await b.release(**acct(b, account))
        if reset:  # after a fresh sign-in: forget the block and the router's cooldown
            b.reset(**acct(b, account))
            for m in web_models(name):
                router.state.health.pop(m.id, None)
        return {"released": True, **b.status()}

    # ---- account setup from the app: accounts, sign-in windows, the bundled browser (web_setup.py) -------------
    setup = web_setup.WebSetup(router)
    app.state.web_setup = setup
    app.router.on_shutdown.append(setup.close)

    async def close_browsers():  # on shutdown: no browser is left running (a sign-in window was closed just above)
        async def one(b):
            try:  # release waits for a chat in progress: not for long when shutting down (the watchdogs remain)
                await asyncio.wait_for(b.release(), 10)
            except Exception:  # noqa: BLE001 - shutting down regardless
                pass

        await asyncio.gather(*(one(b) for b in list(router.bridges.values())))  # together: launchd allows ~20 s

    app.router.on_shutdown.append(close_browsers)

    def fail(e: Exception, state: str | None = None):
        msg = str(e).strip("'\"")
        if isinstance(e, KeyError):
            return JSONResponse({"detail": msg}, status_code=404)
        if isinstance(e, web_setup.BadRequest):
            return JSONResponse({"detail": msg, "reason": msg}, status_code=400)
        return JSONResponse({"detail": msg, "reason": msg, **({"state": state} if state else {})}, status_code=409)

    async def body_of(request: Request) -> dict:
        try:
            b = await request.json()
        except ValueError:
            b = {}
        return b if isinstance(b, dict) else {}

    @app.get("/web/accounts")
    async def web_accounts(authorization: str | None = Header(None)):
        check(authorization)
        return setup.view()

    @app.post("/web/accounts")
    async def web_account_add(request: Request, authorization: str | None = Header(None)):
        check(authorization)
        try:
            return await setup.add_account((await body_of(request)).get("account"))
        except (web_setup.SetupError, KeyError) as e:
            return fail(e)

    @app.delete("/web/accounts/{account}")
    async def web_account_remove(account: str, authorization: str | None = Header(None)):
        check(authorization)
        try:
            return await setup.remove_account(account)
        except (web_setup.SetupError, KeyError) as e:
            return fail(e)

    # a saved sign-in and automatic sign-in for one provider + account (web_signin.py). The password is write-only:
    # it goes to the system's credential store and no answer ever carries it
    @app.put("/web/accounts/{provider}/{account}/autologin")
    async def web_autologin_save(
        provider: str, account: str, request: Request, authorization: str | None = Header(None)
    ):
        check(authorization)
        try:
            return setup.autologin_save(provider, account, await body_of(request))
        except (web_setup.SetupError, KeyError) as e:
            return fail(e)

    @app.patch("/web/accounts/{provider}/{account}/autologin")
    async def web_autologin_enable(
        provider: str, account: str, request: Request, authorization: str | None = Header(None)
    ):
        check(authorization)
        try:
            return setup.autologin_enable(provider, account, await body_of(request))
        except (web_setup.SetupError, KeyError) as e:
            return fail(e)

    @app.delete("/web/accounts/{provider}/{account}/autologin")
    async def web_autologin_forget(provider: str, account: str, authorization: str | None = Header(None)):
        check(authorization)
        try:
            return setup.autologin_forget(provider, account)
        except (web_setup.SetupError, KeyError) as e:
            return fail(e)

    @app.post("/web/accounts/{provider}/{account}/autologin/try")
    async def web_autologin_try(
        provider: str, account: str, request: Request, wait: int = 1, authorization: str | None = Header(None)
    ):
        check(authorization)
        try:
            b = await body_of(request)
            return await setup.autologin_try(provider, account, wait=bool(wait) and b.get("wait") is not False)
        except (web_setup.SetupError, KeyError) as e:
            return fail(e)

    @app.post("/web/accounts/{provider}/{account}/confirm")
    async def web_account_confirm(
        provider: str, account: str, request: Request, authorization: str | None = Header(None)
    ):
        check(authorization)
        try:
            return setup.confirm(provider, account, await body_of(request))
        except (web_setup.SetupError, KeyError) as e:
            return fail(e)

    @app.post("/web/login")
    async def web_login(request: Request, authorization: str | None = Header(None)):
        check(authorization)
        b = await body_of(request)
        try:
            return await setup.start(str(b.get("provider") or ""), b.get("account"), b.get("bounds"), b.get("method"))
        except web_setup.SetupError as e:
            return fail(e, setup.login_state(str(b.get("provider")), b.get("account"))["state"])
        except KeyError as e:
            return fail(e)

    @app.get("/web/login")
    async def web_login_state(provider: str, account: str | None = None, authorization: str | None = Header(None)):
        check(authorization)
        try:
            return setup.login_state(provider, account)
        except KeyError as e:
            return fail(e)

    @app.post("/web/login/show")
    async def web_login_show(request: Request, authorization: str | None = Header(None)):
        check(authorization)
        b = await body_of(request)
        try:
            return await setup.show(str(b.get("provider") or ""), b.get("account"))
        except web_setup.SetupError as e:
            return fail(e, setup.login_state(str(b.get("provider")), b.get("account"))["state"])
        except KeyError as e:
            return fail(e)

    # a pool provider's key page, opened for the person in a visible window of a profile already signed in to that
    # site (Google AI Studio with the Gemini account). Only pages listed in pool_help.yaml; nothing drives the window
    @app.post("/web/open")
    async def web_open(request: Request, authorization: str | None = Header(None)):
        check(authorization)
        b = await body_of(request)
        try:
            return await setup.open_page(str(b.get("provider") or ""), b.get("account"), b.get("url"), b.get("bounds"))
        except web_setup.SetupError as e:
            return fail(e, setup.open_state(str(b.get("provider")), b.get("account"))["state"])
        except KeyError as e:
            return fail(e)

    @app.get("/web/open")
    async def web_open_state(
        provider: str | None = None, account: str | None = None, authorization: str | None = Header(None)
    ):
        check(authorization)
        try:
            if not provider:  # no account named: whichever key page is open (one at a time), else idle
                for f in list(setup.opens.values()):
                    v = f.view()
                    if v.get("state") in ("preparing", "open"):
                        return v
                return {"state": "idle", "reason": None, "provider": None, "account": None, "url": None}
            return setup.open_state(provider, account)
        except KeyError as e:
            return fail(e)

    @app.post("/web/open/cancel")
    async def web_open_cancel(request: Request, authorization: str | None = Header(None)):
        check(authorization)
        b = await body_of(request)
        try:
            return await setup.open_cancel(str(b.get("provider") or ""), b.get("account"))
        except KeyError as e:
            return fail(e)

    @app.patch("/web/providers/{provider}")
    async def web_provider_update(provider: str, request: Request, authorization: str | None = Header(None)):
        check(authorization)
        try:
            return await setup.update_provider(provider, await body_of(request))
        except (web_setup.SetupError, KeyError) as e:
            return fail(e)

    @app.post("/web/login/cancel")
    async def web_login_cancel(request: Request, authorization: str | None = Header(None)):
        check(authorization)
        b = await body_of(request)
        try:
            prov, acc = str(b.get("provider") or ""), b.get("account")
            if (prov, acc or "main") in setup.opens:  # a key page open in that account's window closes the same way
                return await setup.open_cancel(prov, acc)
            return await setup.cancel(prov, acc)
        except KeyError as e:
            return fail(e)

    @app.post("/web/browser/prepare")
    async def web_browser_prepare(authorization: str | None = Header(None)):
        check(authorization)
        try:
            return setup.prepare_browser()
        except web_setup.SetupError as e:
            return fail(e)

    # ---- API keys for the token pool, set from the app (pool_keys.py; values are never sent back) ---------------
    # Any number of keys per provider. PUT sets or replaces the first (the provider's variable, as before); POST
    # adds one more; PATCH / DELETE with a key id change or remove one; DELETE without one removes all.
    @app.get("/pool/keys")
    async def pool_keys_view(authorization: str | None = Header(None)):
        check(authorization)
        return {"providers": pool_keys.view(router.catalog), "removed": pool_keys.removed_view(router.catalog)}

    async def checked(provider: str, key_id: str) -> dict:
        """The provider's view after a key was set, with the validity check's answer and the terms note."""
        p = router.catalog.providers[provider]
        key = next((k for k in p.keys if k.id == key_id), None)
        res = await pool_keys.check(router.client, p, key) if key else {"ok": None, "detail": "No key was set."}
        for m in router.catalog.models.values():  # a new or replaced key starts without the old one's cooldowns
            if m.provider == provider:
                router.state.health.pop(f"{m.id}#{key_id}", None)
        return {**pool_keys.provider_view(router.catalog, provider), "key_id": key_id if key else None, "check": res}

    @app.put("/pool/keys/{provider}")
    async def pool_keys_set(provider: str, request: Request, authorization: str | None = Header(None)):
        check(authorization)
        try:
            b = await body_of(request)
            meta = {k: b[k] for k in ("label", "tier", "limits", "limits_scale") if k in b}
            pool_keys.set_keys(router.catalog, provider, b.get("values"), meta)
            return await checked(provider, pool_keys.MAIN)
        except (pool_keys.KeySetupError, KeyError) as e:
            return fail(e)

    @app.post("/pool/keys/{provider}")
    async def pool_keys_add(provider: str, request: Request, authorization: str | None = Header(None)):
        check(authorization)
        try:
            _, key = pool_keys.add_key(router.catalog, provider, await body_of(request))
            return await checked(provider, key.id)
        except (pool_keys.KeySetupError, KeyError) as e:
            return fail(e)

    @app.patch("/pool/keys/{provider}/{key_id}")
    async def pool_keys_update(provider: str, key_id: str, request: Request, authorization: str | None = Header(None)):
        check(authorization)
        try:
            return pool_keys.update_key(router.catalog, provider, key_id, await body_of(request))
        except (pool_keys.KeySetupError, KeyError) as e:
            return fail(e)

    @app.delete("/pool/keys/{provider}/{key_id}")
    async def pool_keys_delete(provider: str, key_id: str, authorization: str | None = Header(None)):
        check(authorization)
        try:
            return pool_keys.delete_key(router.catalog, provider, key_id)
        except (pool_keys.KeySetupError, KeyError) as e:
            return fail(e)

    @app.delete("/pool/keys/{provider}")
    async def pool_keys_clear(provider: str, authorization: str | None = Header(None)):
        check(authorization)
        try:
            return pool_keys.clear_keys(router.catalog, provider)
        except (pool_keys.KeySetupError, KeyError) as e:
            return fail(e)

    # ---- how to get each provider's free key (pool_help.yaml: text for the app and for an assistant) ------------
    def help_entry(p: dict, rows: list) -> dict:
        # `open_with` [{provider, account}]: the signed-in web accounts whose browser profile can open the key page
        # already signed in (POST /web/open), absent when there is none; `experience`: how following the steps has
        # gone so far (POST /pool/help/{provider}/outcome)
        return {**setup.help_view(p), "experience": pool_help.experience(p["id"], rows)}

    @app.get("/pool/help")
    async def pool_help_view(provider: str | None = None, authorization: str | None = Header(None)):
        check(authorization)
        rows = pool_help.outcomes()
        if provider is None:
            d = pool_help.load()
            return {**d, "providers": [help_entry(p, rows) for p in d["providers"]]}
        try:
            return {"checked": pool_help.load()["checked"], "provider": help_entry(pool_help.provider(provider), rows)}
        except KeyError as e:
            return fail(e)

    @app.post("/pool/help/{provider}/outcome")
    async def pool_help_outcome(provider: str, request: Request, authorization: str | None = Header(None)):
        check(authorization)
        try:
            row = pool_help.record_outcome(provider, await body_of(request))
        except KeyError as e:
            return fail(e)
        except pool_help.OutcomeError as e:
            return JSONResponse({"detail": str(e), "reason": str(e)}, status_code=400)
        return {"recorded": row, "experience": pool_help.experience(provider)}

    anthropic_api.register(app, router, token)
    responses_api.register(app, router, token)
    codex_sub.register(app, router, token)

    @app.api_route(
        "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"], include_in_schema=False
    )
    async def not_found(path: str):
        # Anthropic-style error body (also readable by OpenAI clients): Claude Code probes a few extra endpoints and
        # treats a plain 404 as "not supported here"
        return JSONResponse(
            {
                "type": "error",
                "error": {"type": "not_found_error", "message": f"/{path} is not served by provenance-core"},
            },
            status_code=404,
        )

    return app


def _stream(resp, m, router, t0):
    async def gen():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
            router.state.success(m, (time.time() - t0) * 1000)
        finally:
            await resp.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-PCore-Model": m.id})
