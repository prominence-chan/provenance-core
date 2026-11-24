"""No-auth providers, ${VAR} base URLs, provider pools, and the PCORE_/TIERLM_ env fallback."""

import asyncio
import importlib
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from provenance_core.routing.catalog import load
from provenance_core.routing.router import Router
from provenance_core.api.server import create_app
from provenance_core.settings import env
from provenance_core.routing.state import State

ROOT = Path(__file__).resolve().parents[1]

CAT = {
    "tiers": [{"name": "hi", "min_score": 80}, {"name": "lo", "min_score": 0}],
    "providers": {
        "anon": {"base_url": "https://anon.test/v1", "api_key_env": "none"},
        "anon2": {"base_url": "https://anon2.test/v1", "auth": "none", "api_key_env": "ANON2_KEY"},
        "keyed": {"base_url": "https://keyed.test/v1", "api_key_env": "KEYED_KEY"},
        "cf": {"base_url": "https://cf.test/accounts/${CF_ACCT}/ai/v1", "api_key_env": "CF_TOKEN", "shared_tpd": True},
        "pooled": {"base_url": "https://pooled.test/v1", "api_key_env": "none", "pool": {"rph": 3}},
    },
    "models": [
        {"provider": "anon", "model": "free-big", "score": 90},
        {"provider": "anon2", "model": "m", "score": 85},
        {"provider": "keyed", "model": "k", "score": 88},
        {"provider": "cf", "model": "@cf/a", "score": 86, "limits": {"tpd": 1000}},
        {"provider": "cf", "model": "@cf/b", "score": 84, "limits": {"tpd": 4000}},
        {"provider": "pooled", "model": "p1", "score": 50},
        {"provider": "pooled", "model": "p2", "score": 40},
    ],
}


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def cat(tmp_path, monkeypatch):
    for k in ("KEYED_KEY", "ANON2_KEY", "CF_TOKEN", "CF_ACCT"):
        monkeypatch.delenv(k, raising=False)
    p = tmp_path / "catalog.yaml"
    p.write_text(yaml.safe_dump(CAT))
    return p


def make_router(path, clock=None):
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {"total_tokens": 100}}
        )

    router = Router(
        load(path), State(clock=clock) if clock else State(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return router, seen


def test_no_auth_provider_is_usable_and_sends_no_authorization(cat):
    router, seen = make_router(cat)
    c = router.catalog
    assert c.providers["anon"].usable and c.providers["anon"].api_key_env is None
    assert not c.providers["keyed"].usable  # missing key -> unusable
    assert [m.id for m in c.models_in("hi")] == ["anon/free-big", "anon2/m"]
    data, m = asyncio.run(router.complete({"messages": [{"role": "user", "content": "hi"}]}, "hi"))
    assert m.id == "anon/free-big"
    assert "authorization" not in seen[-1].headers
    assert str(seen[-1].url) == "https://anon.test/v1/chat/completions"


def test_auth_none_still_sends_a_key_when_present(cat, monkeypatch):
    router, seen = make_router(cat)
    asyncio.run(router.call(router.catalog.models["anon2/m"], {"messages": []}))
    assert "authorization" not in seen[-1].headers
    monkeypatch.setenv("ANON2_KEY", "k2")
    asyncio.run(router.call(router.catalog.models["anon2/m"], {"messages": []}))
    assert seen[-1].headers["authorization"] == "Bearer k2"


def test_keyed_provider_sends_bearer(cat, monkeypatch):
    monkeypatch.setenv("KEYED_KEY", "sekrit")
    router, seen = make_router(cat)
    asyncio.run(router.call(router.catalog.models["keyed/k"], {"messages": []}))
    assert seen[-1].headers["authorization"] == "Bearer sekrit"


def test_base_url_var_expansion(cat, monkeypatch):
    monkeypatch.setenv("CF_TOKEN", "t")
    router, seen = make_router(cat)
    cf = router.catalog.providers["cf"]
    assert not cf.usable  # ${CF_ACCT} unset: unusable even with a token
    monkeypatch.setenv("CF_ACCT", "abc123")
    assert cf.usable and cf.base_url == "https://cf.test/accounts/abc123/ai/v1"
    asyncio.run(router.call(router.catalog.models["cf/@cf/a"], {"messages": []}))
    assert str(seen[-1].url) == "https://cf.test/accounts/abc123/ai/v1/chat/completions"


def test_models_endpoint_lists_no_auth_models(cat):
    router, _ = make_router(cat)
    ids = [d["id"] for d in TestClient(create_app(router=router)).get("/v1/models").json()["data"]]
    assert "anon/free-big" in ids and "keyed/k" not in ids and "cf/@cf/a" not in ids


def test_provider_pool_rph_spans_models(cat):
    clock = Clock()
    router, _ = make_router(cat, clock)
    st, p1, p2 = router.state, router.catalog.models["pooled/p1"], router.catalog.models["pooled/p2"]
    for m in (p1, p1, p2):
        assert st.headroom(m, 10) > 0
        st.record(m, 10)
    assert st.headroom(p1, 10) == 0 and st.headroom(p2, 10) == 0  # 3 requests/hour across the provider
    clock.t += 3601
    assert st.headroom(p2, 10) > 0


def test_shared_tpd_is_one_daily_allowance(cat, monkeypatch):
    monkeypatch.setenv("CF_TOKEN", "t")
    monkeypatch.setenv("CF_ACCT", "x")
    clock = Clock()
    router, _ = make_router(cat, clock)
    st, a, b = router.state, router.catalog.models["cf/@cf/a"], router.catalog.models["cf/@cf/b"]
    st.record(a, 500)  # half of a's share -> half of the provider's day
    assert st.headroom(b, 2004) == 0  # just over half a day on b would exceed it
    assert st.headroom(b, 1900) > 0
    st.record(b, 1900)
    assert st.headroom(a, 100) == 0  # 0.5 + 0.475 + 0.1 > 1


def test_env_prefers_pcore_then_tierlm(monkeypatch):
    monkeypatch.delenv("PCORE_X_TEST", raising=False)
    monkeypatch.delenv("TIERLM_X_TEST", raising=False)
    assert env("X_TEST", "d") == "d"
    monkeypatch.setenv("TIERLM_X_TEST", "old")
    assert env("X_TEST") == "old"
    monkeypatch.setenv("PCORE_X_TEST", "new")
    assert env("X_TEST") == "new"


def test_legacy_tierlm_env_still_maps(cat, monkeypatch):
    for k in ("PCORE_TOKEN", "TIERLM_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TIERLM_TOKEN", "old-token")
    router, _ = make_router(cat)
    c = TestClient(create_app(router=router))
    assert c.get("/v1/models").status_code == 401
    assert c.get("/v1/models", headers={"authorization": "Bearer old-token"}).status_code == 200


def test_tierlm_shim_reexports_usage():
    import provenance_core.accounting.usage

    tierlm = importlib.import_module("tierlm")
    from tierlm import usage

    assert usage is provenance_core.accounting.usage and tierlm.catalog is importlib.import_module("provenance_core.routing.catalog")
    out = subprocess.run(
        [sys.executable, "-m", "tierlm", "tiers", "--catalog", str(ROOT / "config" / "catalog.yaml"), "--scores", "/nonexistent"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert out.returncode == 0 and "calibrated" in out.stdout


def test_catalog_yaml_loads():
    c = load(ROOT / "config" / "catalog.yaml")
    assert c.providers["ovh"].no_auth and c.providers["kilo"].no_auth
    assert c.models["kilo/nvidia/nemotron-3-ultra-550b-a55b:free"].pool.rph == 200
    assert "${CLOUDFLARE_ACCOUNT_ID}" in c.providers["cloudflare"].url
    assert json.dumps([m.id for m in c.models.values()])


def test_concrete_model_failure_is_reported_not_500(cat):
    def handler(req):
        return httpx.Response(429, headers={"retry-after": "100"})

    router = Router(load(cat), State(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    r = TestClient(create_app(router=router)).post(
        "/v1/chat/completions", json={"model": "anon/free-big", "messages": []}
    )
    assert r.status_code == 429 and "rate limited" in r.json()["detail"]
