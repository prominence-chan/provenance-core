import asyncio
import json

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from provenance_core.catalog import load
from provenance_core.router import NoCapacity, Router
from provenance_core.server import create_app
from provenance_core.state import State

CAT = {
    "tiers": [
        {"name": "tier-1", "min_score": 80},
        {"name": "tier-2", "min_score": 60},
        {"name": "tier-3", "min_score": 0},
    ],
    "providers": {
        "a": {"base_url": "https://a.test/v1", "api_key_env": "A_KEY"},
        "b": {"base_url": "https://b.test/v1", "api_key_env": "B_KEY"},
        "c": {"base_url": "https://c.test/v1", "api_key_env": "C_KEY"},
    },
    "models": [
        {"provider": "a", "model": "big", "score": 90, "limits": {"rpm": 2}},
        {"provider": "b", "model": "big2", "score": 85},
        {"provider": "c", "model": "mid", "score": 70},
        {"provider": "c", "model": "small", "score": 30},
    ],
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
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": content}}], "usage": {"total_tokens": 100}},
        )

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
    assert calls == ["big2"]  # big hit its rpm=2
    clock.t += 61
    calls.clear()
    run(router.complete({"messages": []}, "tier-1"))
    assert calls == ["big"]


def test_sticky_sessions(setup):
    router, calls, behavior, _ = setup
    behavior["big"] = "429"
    run(router.complete({"messages": []}, "tier-1", session="s1"))  # served by big2
    router.state.health.clear()  # big recovers
    calls.clear()
    _, m = run(router.complete({"messages": []}, "tier-1", session="s1"))
    assert m.model == "big2" and calls == ["big2"]  # conversation keeps its model


def test_majority_ensemble(setup):
    router, calls, behavior, _ = setup
    behavior["big:text"] = "work...\n42"
    behavior["big2:text"] = "42"
    data, members = run(
        router.ensemble({"messages": [{"role": "user", "content": "q"}]}, "tier-1", k=2, strategy="majority")
    )
    assert data["choices"][0]["message"]["content"].endswith("42") and data["pcore"]["votes"]["42"] == 2


def test_judge_ensemble(setup):
    router, calls, behavior, _ = setup
    data, members = run(
        router.ensemble({"messages": [{"role": "user", "content": "q"}]}, "tier-1", k=2, strategy="judge")
    )
    assert len(members) == 2 and calls[-1] == "big"  # strongest member judges


def test_http_api(setup):
    router, calls, *_ = setup
    app = create_app(router=router)
    c = TestClient(app)
    ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
    assert "tier-1" in ids and "c/small" in ids
    r = c.post("/v1/chat/completions", json={"model": "tier-2", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.headers["x-pcore-model"] == "c/mid"
    r = c.post("/v1/chat/completions", json={"model": "nope", "messages": []})
    assert r.status_code == 404
    assert "tier-1" in c.get("/status").json()


def test_calibration_grading():
    from provenance_core.calibrate import grade

    assert grade("so the answer is\n391", r"^\s*391\s*$")
    assert grade("6/(1-3/4)", "EVAL24:1346") and not grade("4*6*1*3", "EVAL24:1346")


class FakeWeb:
    """Stands in for a web bridge pool: only what the router asks about capacity."""

    def __init__(self, remaining, busy=0):
        self.remaining, self.busy = remaining, busy

    def capacity(self, accounts=None):
        return {"remaining": self.remaining, "busy": self.busy, "accounts": 3, "available": self.remaining > 0}


def web_setup(tmp_path, monkeypatch):
    for k in ("A_KEY", "B_KEY", "C_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("PCORE_CHATGPT_WEB", "1")
    cat = json.loads(json.dumps(CAT))
    cat["providers"]["gpt"] = {"type": "chatgpt_web", "base_url": "https://gpt.test", "api_key_env": "none"}
    cat["providers"]["qw"] = {"type": "chatgpt_web", "base_url": "https://qw.test", "api_key_env": "none"}
    cat["models"] += [
        {"provider": "gpt", "model": "Luna", "score": 88, "label": "GPT-6 Luna"},
        {"provider": "qw", "model": "qwen4-max", "score": 86, "accounts": ["main"]},
    ]
    p = tmp_path / "catalog.yaml"
    p.write_text(yaml.safe_dump(cat))
    router = Router(load(p), State(clock=Clock()), httpx.AsyncClient(transport=httpx.MockTransport(lambda r: None)))
    router.bridges = {"gpt": FakeWeb(0.5), "qw": FakeWeb(0.7)}
    return router


def test_sources_share_by_usage_left_and_can_be_pinned(tmp_path, monkeypatch):
    router = web_setup(tmp_path, monkeypatch)
    ids = lambda **kw: [m.id for m in router.candidates("tier-1", 100, web=True, **kw)]  # noqa: E731
    src = {s["id"]: s for s in router.sources("tier-1")}
    assert set(src) == {"luna", "qwen4-max", "pool"} and src["luna"]["label"] == "GPT-6 Luna"
    assert src["pool"]["models"] == ["a/big", "b/big2"] and src["pool"]["label"] == "Token pool"
    # nothing running: the fullest source first (the pool is untouched, then Qwen at 70%, then Luna at 50%)
    assert ids() == ["a/big", "b/big2", "qw/qwen4-max", "gpt/Luna"]
    router.inflight["a/big"] = 3  # a busy pool gives way
    assert ids()[0] == "qw/qwen4-max"
    assert ids(source="luna")[0] == "gpt/Luna" and len(ids(source="luna")) == 4  # the others stay as a fallback
    router.state.remember("s", "gpt/Luna")  # a conversation stays where it was served
    assert ids(session="s")[0] == "gpt/Luna"
    assert ids(session="s", source="pool")[0] == "a/big"  # unless the user now pins something else
    router.bridges["qw"].remaining = 0.0
    assert "qw/qwen4-max" not in ids() or ids()[-1] == "qw/qwen4-max"
    assert [m.id for m in router.candidates("tier-1", 100, web=False)] == ["a/big", "b/big2"]


def test_pinned_model_names_resolve(tmp_path, monkeypatch):
    from provenance_core.adapters import resolve_target

    router = web_setup(tmp_path, monkeypatch)
    t = resolve_target(router, "luna/tier-1", "down1")
    assert (t.tier, t.source, t.label) == ("tier-1", "luna", "luna/tier-1")
    assert resolve_target(router, "pool/tier-1", "none").source == "pool"
    assert resolve_target(router, "nosuch/tier-1", "none").source is None
    assert resolve_target(router, "gpt/Luna", "none").concrete == "gpt/Luna"
