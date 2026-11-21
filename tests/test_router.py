import asyncio, json

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from tierlm.catalog import load
from tierlm.router import NoCapacity, Router
from tierlm.server import create_app
from tierlm.state import State

CAT = {
    "tiers": [{"name": "tier-1", "min_score": 80}, {"name": "tier-2", "min_score": 60}, {"name": "tier-3", "min_score": 0}],
    "providers": {"a": {"base_url": "https://a.test/v1", "api_key_env": "A_KEY"},
                  "b": {"base_url": "https://b.test/v1", "api_key_env": "B_KEY"},
                  "c": {"base_url": "https://c.test/v1", "api_key_env": "C_KEY"}},
    "models": [{"provider": "a", "model": "big", "score": 90, "limits": {"rpm": 2}},
               {"provider": "b", "model": "big2", "score": 85},
               {"provider": "c", "model": "mid", "score": 70},
               {"provider": "c", "model": "small", "score": 30}],
}


class Clock:
    t = 1000.0
    def __call__(self):
        return self.t


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for k in ("A_KEY", "B_KEY", "C_KEY"):
        monkeypatch.setenv(k, "x")
    p = tmp_path / "catalog.yaml"
    p.write_text(yaml.safe_dump(CAT))
    calls = []
    behavior = {}

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append(body["model"])
        b = behavior.get(body["model"], "ok")
        if b == "429":
            return httpx.Response(429, headers={"retry-after": "30"})
        if b == "500":
            return httpx.Response(500, text="boom")
        content = behavior.get(body["model"] + ":text", f"answer from {body['model']}")
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}],
                                         "usage": {"total_tokens": 100}})
    clock = Clock()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    router = Router(load(p), State(clock=clock), client)
    return router, calls, behavior, clock


def run(c):
    return asyncio.get_event_loop().run_until_complete(c) if False else asyncio.run(c)


def test_tiers(setup):
    router, *_ = setup
    assert [m.model for m in router.catalog.models_in("tier-1")] == ["big", "big2"]
    assert router.catalog.tier_of(router.catalog.models["c/mid"]) == "tier-2"


def test_stays_in_tier_and_falls_over_within_it(setup):
    router, calls, behavior, _ = setup
    behavior["big"] = "429"
    data, m = run(router.complete({"messages": [{"role": "user", "content": "hi"}]}, "tier-1"))
    assert m.model == "big2" and calls == ["big", "big2"]
    # big is now cooling down (retry-after 30s), so the next request goes straight to big2
    calls.clear()
    run(router.complete({"messages": []}, "tier-1"))
    assert calls == ["big2"]


def test_no_silent_downgrade_unless_allowed(setup):
    router, calls, behavior, _ = setup
    behavior["big"] = behavior["big2"] = "500"
    with pytest.raises(NoCapacity):
        run(router.complete({"messages": []}, "tier-1"))
    calls.clear()
    behavior.clear()
    router.state.health.clear()
    for m in ("big", "big2"):
        router.state.failure(router.catalog.models[f"{'a' if m == 'big' else 'b'}/{m}"], rate_limited=True)
    data, m = run(router.complete({"messages": []}, "tier-1", fallback="down1"))
    assert m.model == "mid"


def test_rate_limit_caps_and_window(setup):
    router, calls, _, clock = setup
    for _ in range(2):
        run(router.complete({"messages": []}, "tier-1"))
    calls.clear()
    run(router.complete({"messages": []}, "tier-1"))
    assert calls == ["big2"]   # big hit its rpm=2
    clock.t += 61
    calls.clear()
    run(router.complete({"messages": []}, "tier-1"))
    assert calls == ["big"]


def test_sticky_sessions(setup):
    router, calls, behavior, _ = setup
    behavior["big"] = "429"
    run(router.complete({"messages": []}, "tier-1", session="s1"))   # served by big2
    router.state.health.clear()                                     # big recovers
    calls.clear()
    _, m = run(router.complete({"messages": []}, "tier-1", session="s1"))
    assert m.model == "big2" and calls == ["big2"]                  # conversation keeps its model


def test_majority_ensemble(setup):
    router, calls, behavior, _ = setup
    behavior["big:text"] = "work...\n42"
    behavior["big2:text"] = "42"
    data, members = run(router.ensemble({"messages": [{"role": "user", "content": "q"}]}, "tier-1", k=2, strategy="majority"))
    assert data["choices"][0]["message"]["content"].endswith("42") and data["tierlm"]["votes"]["42"] == 2


def test_judge_ensemble(setup):
    router, calls, behavior, _ = setup
    data, members = run(router.ensemble({"messages": [{"role": "user", "content": "q"}]}, "tier-1", k=2, strategy="judge"))
    assert len(members) == 2 and calls[-1] == "big"   # strongest member judges


def test_http_api(setup):
    router, calls, *_ = setup
    app = create_app(router=router)
    c = TestClient(app)
    ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
    assert "tier-1" in ids and "c/small" in ids
    r = c.post("/v1/chat/completions", json={"model": "tier-2", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.headers["x-tierlm-model"] == "c/mid"
    r = c.post("/v1/chat/completions", json={"model": "nope", "messages": []})
    assert r.status_code == 404
    assert "tier-1" in c.get("/status").json()


def test_calibration_grading():
    from tierlm.calibrate import grade
    assert grade("so the answer is\n391", r"^\s*391\s*$")
    assert grade("6/(1-3/4)", "EVAL24:1346") and not grade("4*6*1*3", "EVAL24:1346")
