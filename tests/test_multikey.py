"""Several keys per provider (pool_keys.py), counted per (model, key) (state.py, router.py); the pool's clean-up
(removed providers, a provider with an end date); and the record of how setting a provider up went (pool_help.py).
Fake keys and a fake upstream only: nothing here reaches a provider."""

import asyncio
import datetime
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from provenance_core.routing import catalog as catalog_mod
from provenance_core.keys import pool_help, pool_keys
from provenance_core.routing.catalog import Limits, load
from provenance_core.accounting.ledger import Ledger
from provenance_core.routing.router import NoCapacity, Router
from provenance_core.api.server import create_app
from provenance_core.routing.state import State

ROOT = Path(__file__).resolve().parents[1]

# made-up keys, long and random-looking like real ones (none is a real key of anything)
K1 = "sk-test-" + "A1b2C3d4" * 4 + "0001"
K2 = "sk-test-" + "E5f6G7h8" * 4 + "0002"
K3 = "sk-test-" + "J9k0L1m2" * 4 + "0003"
BAD = "sk-test-" + "Z9y8X7w6" * 4 + "dead"
FAKES = (K1, K2, K3, BAD)
VARS = ("ACME_KEY", "BETA_KEY", "CF_TOKEN", "CF_ACCT", "OPEN_KEY")

CAT = {
    "tiers": [{"name": "hi", "min_score": 80}, {"name": "lo", "min_score": 0}],
    "providers": {
        "acme": {"base_url": "https://acme.test/v1", "api_key_env": "ACME_KEY", "check": {"path": "/models"}},
        "beta": {"base_url": "https://beta.test/v1", "api_key_env": "BETA_KEY"},
        "cf": {
            "base_url": "https://cf.test/accounts/${CF_ACCT}/ai/v1",
            "api_key_env": "CF_TOKEN",
            "shared_tpd": True,
        },
        "open": {"base_url": "https://open.test/v1", "auth": "none", "api_key_env": "OPEN_KEY"},
        "gate": {"base_url": "https://gate.test/v1", "api_key_env": "GATE_KEY", "pool": {"rpd": 50}},
    },
    "models": [
        {"provider": "acme", "model": "big", "score": 90, "limits": {"rpm": 4}},
        {"provider": "beta", "model": "big2", "score": 85},
        {"provider": "cf", "model": "@cf/a", "score": 60, "limits": {"tpd": 100000}},
        {"provider": "open", "model": "anon-ok", "score": 50},
        {"provider": "gate", "model": "g1", "score": 40, "limits": {"rpm": 20}},
    ],
}


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class Upstream:
    """A fake provider: answers chat completions and GET /models, and remembers what it was sent. `how[key]` makes
    it refuse that key: "429", "401" or "revoked" (a 403 that says so)."""

    def __init__(self):
        self.seen: list[tuple[str, str | None]] = []  # (url, the key sent, or None)
        self.how: dict[str | None, str] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        auth = req.headers.get("authorization")
        key = auth.removeprefix("Bearer ") if auth else None
        self.seen.append((str(req.url), key))
        how = self.how.get(key, "ok")
        if how == "429":
            return httpx.Response(429, headers={"retry-after": "30"})
        if how == "401":
            return httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {key}"}})
        if how == "revoked":
            return httpx.Response(403, json={"error": {"message": "This API key was revoked."}})
        if req.method == "GET":
            return httpx.Response(200, json={"data": []})
        body = json.loads(req.content)
        if body.get("stream"):
            chunks = [{"delta": {"content": "streamed"}}, {"delta": {}, "finish_reason": "stop"}]
            sse = "".join(f"data: {json.dumps({'choices': [ch]})}\n\n" for ch in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        msg = {"role": "assistant", "content": f"answer from {body['model']}"}
        return httpx.Response(200, json={"choices": [{"message": msg}], "usage": {"total_tokens": 100}})

    def keys(self, host: str = "") -> list[str | None]:
        return [k for u, k in self.seen if host in u and "/chat/completions" in u]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PCORE_HOME", str(tmp_path / "home"))
    for v in (*VARS, "GATE_KEY", "PCORE_TOKEN", "TIERLM_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    pool_keys.FROM_FILE.clear()
    pool_keys.SHADOWED.clear()
    yield tmp_path / "home"
    for v in list(pool_keys.FROM_FILE):
        os.environ.pop(v, None)
    pool_keys.FROM_FILE.clear()
    pool_keys.SHADOWED.clear()


def make(tmp_path, cat=None, path=None):
    """(router, client, upstream, clock) on a catalog (CAT unless given; `path` for one on disk)."""
    if path is None:
        path = tmp_path / "catalog.yaml"
        path.write_text(yaml.safe_dump(cat or CAT))
    up, clock = Upstream(), Clock()
    router = Router(
        load(path, tmp_path / "no-scores.yaml"),
        State(clock=clock),
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
    )
    return router, TestClient(create_app(router=router)), up, clock


def ask(router, tier="hi", **kw):
    return asyncio.run(router.complete({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}, tier, **kw))


def status_of(c, tier, mid):
    return next(m for m in c.get("/status").json()[tier]["models"] if m["id"] == mid)


def pool_source(c, tier):
    t = next(t for t in c.get("/v1/sources").json()["tiers"] if t["tier"] == tier)
    return next(s for s in t["sources"] if s["id"] == "pool")


# ---- routing -------------------------------------------------------------------------------------------------------
def test_two_keys_double_the_headroom_and_requests_spread(home, tmp_path, monkeypatch):
    monkeypatch.setenv("ACME_KEY", K1)  # the first key, where it always was: the provider's variable
    router, c, up, _ = make(tmp_path)
    with c:
        one = status_of(c, "hi", "acme/big")
        src1, h1 = pool_source(c, "hi"), c.get("/health").json()["api"]
        assert [k["id"] for k in one["keys"]] == ["main"] and one["capacity"] == one["headroom"] == 0.75
        assert (src1["keys"], h1["keys"]) == (1, 1)
        r = c.post("/pool/keys/acme", json={"key": K2, "label": "second account"})
        assert r.status_code == 200 and r.json()["check"]["ok"] is True
        kid = r.json()["key_id"]
        two = status_of(c, "hi", "acme/big")
        src2, h2 = pool_source(c, "hi"), c.get("/health").json()["api"]
    assert [k["id"] for k in two["keys"]] == ["main", kid]
    assert two["capacity"] == 2 * one["capacity"] and two["headroom"] == one["headroom"]  # the sum; the best key
    assert src2["capacity"] == 2 * src1["capacity"] and (src2["keys"], h2["keys"], h2["slots"]) == (
        2,
        2,
        h1["slots"] + 1,
    )
    assert src2["remaining"] == src1["remaining"]  # still a share (0-1) of what the pool has
    up.seen.clear()
    for _ in range(8):  # 4 a minute on each key
        assert ask(router)[1].id == "acme/big"
    assert sorted(up.keys("acme")) == [K1] * 4 + [K2] * 4
    assert up.keys("acme")[:4].count(K1) == 2  # taken in turn, not one key first
    with pytest.raises(NoCapacity):  # both keys are at their limit (and the tier's other model has no key)
        ask(router)
    assert router.state.capacity(router.catalog.models["acme/big"], 100) == 0


def test_a_429_moves_to_the_other_key_and_cools_only_that_one(home, tmp_path, monkeypatch):
    monkeypatch.setenv("ACME_KEY", K1)
    router, c, up, clock = make(tmp_path)
    with c:
        kid = c.post("/pool/keys/acme", json={"key": K2}).json()["key_id"]
    up.seen.clear()
    up.how[K1] = "429"
    m = router.catalog.models["acme/big"]
    data, served = ask(router)
    assert served.id == "acme/big" and up.keys("acme") == [K1, K2]  # refused on the first, served by the second
    st = router.state
    assert not st.available(m, "main") and st.available(m, kid) and st.available(m)
    assert st.health[m.id].cooldown_until == 0  # the model itself is not cooling down
    assert st.headroom(m, 100, "main") > 0  # its allowance is untouched; it only sits out
    ask(router)
    assert up.keys("acme") == [K1, K2, K2]  # the cooled key is not tried again
    clock.t += 31  # Retry-After: 30
    up.how.clear()
    assert st.available(m, "main")
    ask(router)
    assert up.keys("acme")[-1] == K1  # back, and it has more left than the other
    # both keys refused: the request moves on to the next model, as it does between models
    monkeypatch.setenv("BETA_KEY", K3)
    up.how.update({K1: "429", K2: "429"})
    assert ask(router)[1].id == "beta/big2" and not st.available(m)


def test_an_invalid_key_is_marked_and_skipped(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACME_KEY", K1)
    monkeypatch.setenv("BETA_KEY", K3)
    router, c, up, _ = make(tmp_path)
    with c:
        kid = c.post("/pool/keys/acme", json={"key": BAD}).json()["key_id"]
        up.how[BAD] = "401"  # revoked after it was added
        up.seen.clear()
        for _ in range(3):
            assert ask(router)[1].id == "acme/big"
        assert up.keys("acme").count(BAD) == 1  # tried once, found bad, never again
        v = next(p for p in c.get("/pool/keys").json()["providers"] if p["id"] == "acme")
        bad = next(k for k in v["keys"] if k["id"] == kid)
        assert bad["usable"] is False and "rejected" in bad["reason"] and "401" in bad["reason"]
        assert v["usable"] and next(k for k in v["keys"] if k["id"] == "main")["usable"]  # the other goes on
        assert router.catalog.providers["beta"].usable and status_of(c, "hi", "acme/big")["available"]
        assert [k["id"] for k in status_of(c, "hi", "acme/big")["keys"]] == ["main"]
        # the upstream echoed the key in its error: it is in no answer and no log line
        assert BAD not in json.dumps(v) and BAD not in capsys.readouterr().out
        # turned on again by the person: tried again (and found bad again)
        assert c.patch(f"/pool/keys/acme/{kid}", json={"enabled": True}).json()["keys"][1]["usable"] is True
        ask(router)
        assert up.keys("acme").count(BAD) == 2
        # a key that is bad when it is added: said at once by the check, and not used
        up.how[K2] = "revoked"
        r = c.post("/pool/keys/acme", json={"key": K2})
        assert r.status_code == 200 and r.json()["check"]["ok"] is False and K2 not in r.text
        assert next(k for k in r.json()["keys"] if k["id"] == r.json()["key_id"])["usable"] is False
    # a provider whose only key is bad is not usable; a 500 is the model's fault, not the key's
    pool_keys.REJECTED.clear()
    up.how.clear()
    up.how[K3] = "401"
    with pytest.raises(RuntimeError, match="key was rejected"):
        asyncio.run(router.call(router.catalog.models["beta/big2"], {"messages": []}))
    assert not router.catalog.providers["beta"].usable


def test_anonymous_access_stays_beside_a_key(home, tmp_path):
    router, c, up, _ = make(tmp_path)
    m = router.catalog.models["open/anon-ok"]
    assert [k.id for k in router.state.keys(m)] == ["anon"] and router.catalog.providers["open"].usable
    assert ask(router, "lo")[1].id == "open/anon-ok" and up.keys("open") == [None]
    with c:
        assert c.post("/pool/keys/open", json={"key": K1}).status_code == 200  # its first key
        assert [k["id"] for k in status_of(c, "lo", "open/anon-ok")["keys"]] == ["main", "anon"]
        assert c.get("/health").json()["api"]["keys"] == 1  # the anonymous slot is not a key
    ask(router, "lo")
    assert up.keys("open")[-1] == K1  # a key is preferred while it has as much left
    up.how[K1] = "429"
    ask(router, "lo")
    assert up.keys("open")[-2:] == [K1, None]  # and the anonymous access takes over when the key is limited
    up.how[None] = "401"  # refused without a key: nothing to mark, the model's own failure
    with pytest.raises(NoCapacity):
        ask(router, "lo")
    assert not pool_keys.REJECTED


def test_each_key_has_its_own_account_id_and_daily_share(home, tmp_path):
    router, c, up, _ = make(tmp_path)
    with c:
        r = c.post("/pool/keys/cf", json={"key": K1})
        assert r.status_code == 409 and "CF_ACCT" in r.json()["detail"]  # a token alone goes nowhere
        assert c.post("/pool/keys/cf", json={"key": K1, "extras": {"CF_ACCT": "acct-one"}}).status_code == 200
        r = c.post("/pool/keys/cf", json={"key": K2, "extras": {"CF_ACCT": "acct-two"}, "tier": "paid"})
        assert r.status_code == 200 and "acct-two" not in r.text
        assert c.post("/pool/keys/cf", json={"key": K3, "extras": {"OTHER": "x"}}).status_code == 409
    m = router.catalog.models["cf/@cf/a"]
    for _ in range(2):
        asyncio.run(router.call(m, {"messages": []}))
    assert sorted(up.seen) == [
        ("https://cf.test/accounts/acct-one/ai/v1/chat/completions", K1),
        ("https://cf.test/accounts/acct-two/ai/v1/chat/completions", K2),
    ]
    kid = r.json()["key_id"]
    router.state.record(m, 99000, key="main")  # one account's day is nearly used up; the other's is not
    assert router.state.headroom(m, 5000, "main") == 0 and router.state.headroom(m, 5000, kid) > 0.9


def test_limits_per_key(home, tmp_path, monkeypatch):
    monkeypatch.setenv("ACME_KEY", K1)
    monkeypatch.setenv("GATE_KEY", K2)
    router, c, up, _ = make(tmp_path)
    big, g1 = router.catalog.models["acme/big"], router.catalog.models["gate/g1"]
    st = router.state
    with c:
        # a paid account: five times the catalog's free numbers
        r = c.patch("/pool/keys/acme/main", json={"tier": "paid", "limits_scale": 5})
        assert r.status_code == 200 and r.json()["keys"][0]["limits"] == {"scale": 5}
        assert st.limits(big, st.keys(big)[0])[0].rpm == 20 and st.headroom(big, 100) == 1 - 1 / 20
        # explicit numbers replace the catalog's where it has them: here the provider's shared daily allowance
        c.patch("/pool/keys/gate/main", json={"limits": {"rpd": 1000, "tpm": 5000}})
        L, P = st.limits(g1, st.keys(g1)[0])
        assert (P.rpd, L.rpd, L.rpm, L.tpm) == (1000, None, 20, 5000)
        c.patch("/pool/keys/gate/main", json={"limits": {"models": {"g1": {"rpm": 2}}, "rpd": None}})
        L, P = st.limits(g1, st.keys(g1)[0])
        assert (P.rpd, L.rpm) == (None, 2)  # null lifts a limit; one model's own numbers
        assert c.patch("/pool/keys/gate/main", json={"limits": None}).json()["keys"][0]["limits"] is None
        assert st.limits(g1, st.keys(g1)[0]) == (g1.limits, g1.pool)
        for bad in ({"limits": {"rps": 3}}, {"limits": {"rpm": -1}}, {"limits": {"scale": 0}}, {"limits": "lots"}):
            assert c.patch("/pool/keys/gate/main", json=bad).status_code == 409, bad
    assert pool_keys.limits_for(None, Limits(rpm=3), "m") == (Limits(rpm=3), None)


def test_streams_and_the_ledger_know_the_key(home, tmp_path, monkeypatch):
    monkeypatch.setenv("ACME_KEY", K1)
    router, c, up, _ = make(tmp_path)
    m = router.catalog.models["acme/big"]
    with c:
        kid = c.post("/pool/keys/acme", json={"key": K2}).json()["key_id"]
        up.how[K1] = "429"
        meta = {"thread_id": "thr", "turn_id": "t1"}
        r = c.post("/v1/responses", json={"model": "hi", "input": "hi", "client_metadata": meta})
        assert r.status_code == 200, r.text
        r = c.post("/v1/responses", json={"model": "hi", "input": "hi", "stream": True, "client_metadata": meta})
        assert r.status_code == 200
        u = c.get("/v1/pcore/usage", params={"key": "thr", "turn": "t1"}).json()
    assert u["models"][0]["model"] == "acme/big" and u["models"][0]["keys"] == {kid: 2}
    entries = list(create_app(router=router).state.usage_ledger.entries)
    assert [e["kid"] for e in entries] == [kid, kid] and all(k not in json.dumps(entries) for k in FAKES)
    # the usage of both requests (one streamed) is counted on the key that served them
    assert router.state.headroom(m, 100, kid) == 1 - 3 / 4 and router.state.headroom(m, 100, "main") == 1 - 1 / 4
    led = Ledger(tmp_path / "l.jsonl")
    led.record("k", "a/x", kid="main")
    led.record("k", "a/x")
    assert led.query("k")["models"][0]["keys"] == {"main": 1}


# ---- storage -------------------------------------------------------------------------------------------------------
def test_legacy_single_key_files_still_load(home, tmp_path, monkeypatch):
    home.mkdir()
    (home / "keys.json").write_text(json.dumps({"ACME_KEY": K1}))  # as PUT /pool/keys wrote it before this
    monkeypatch.setenv("BETA_KEY", K3)  # as keys.env gives it
    router, c, up, _ = make(tmp_path)
    assert os.environ["ACME_KEY"] == K1 and router.catalog.providers["acme"].usable
    with c:
        v = {p["id"]: p for p in c.get("/pool/keys").json()["providers"]}
        assert [(k["id"], k["source"], k["usable"], k["last4"]) for k in v["acme"]["keys"]] == [
            ("main", "app", True, "0001")
        ]
        assert [(k["id"], k["source"]) for k in v["beta"]["keys"]] == [("main", "env")]
        assert v["acme"]["vars"] == [{"name": "ACME_KEY", "set": True, "from": "app"}]
        assert ask(router)[1].id == "acme/big" and up.keys("acme") == [K1]
        # a further key leaves the first where an older router reads it: the file's top-level strings
        kid = c.post("/pool/keys/acme", json={"key": K2, "label": "Pro", "tier": "paid"}).json()["key_id"]
        d = json.loads((home / "keys.json").read_text())
        assert {k: x for k, x in d.items() if isinstance(x, str)} == {"ACME_KEY": K1}
        assert d["@keys"]["acme"] == [
            {"id": kid, "key": K2, "label": "Pro", "tier": "paid", "added": datetime.date.today().isoformat()}
        ]
        assert stat.S_IMODE((home / "keys.json").stat().st_mode) == 0o600
    # the next start reads both back
    os.environ.pop("ACME_KEY")
    pool_keys.FROM_FILE.clear()
    pool_keys.EXTRA.clear()
    again = load(tmp_path / "catalog.yaml")
    assert [(k.id, k.label, k.tier) for k in again.providers["acme"].keys] == [
        ("main", None, None),
        (kid, "Pro", "paid"),
    ]
    # a hand-edited file with nonsense in it loads what it can
    (home / "keys.json").write_text(
        json.dumps({"ACME_KEY": K1, "@keys": {"acme": [{"id": "x"}, 7, {"id": "ok", "key": K2}], "nope": "?"}})
    )
    pool_keys.load_into_env()
    assert [k.id for k in again.providers["acme"].keys] == ["main", "ok"]
    assert repr(again.providers["acme"].keys[1]) == "PoolKey(acme/ok)"  # a key never prints its value


def test_add_patch_delete(home, tmp_path):
    router, c, up, _ = make(tmp_path)
    today = datetime.date.today().isoformat()
    with c:
        # PUT keeps its meaning: the first key
        r = c.put("/pool/keys/acme", json={"values": {"ACME_KEY": K1}, "label": "work account"})
        assert r.status_code == 200 and r.json()["check"] == {"ok": True, "detail": "The provider accepted the key."}
        assert r.json()["keys"] == [
            {
                "id": "main",
                "label": "work account",
                "tier": None,
                "usable": True,
                "reason": None,
                "added": today,
                "last4": "0001",
                "limits": None,
                "enabled": True,
                "source": "app",
            }
        ]
        r = c.post("/pool/keys/acme", json={"key": f"  {K2}  ", "label": "Pro", "tier": "paid"})
        kid = r.json()["key_id"]
        assert r.status_code == 200 and kid not in ("main", "anon") and r.json()["multiple_keys"]["allowed"]
        assert [(k["id"], k["label"], k["tier"], k["last4"]) for k in r.json()["keys"]] == [
            ("main", "work account", None, "0001"),
            (kid, "Pro", "paid", "0002"),
        ]
        for dup in (K1, K2):  # that exact key is already there
            r = c.post("/pool/keys/acme", json={"key": dup})
            assert r.status_code == 409 and "already" in r.json()["detail"] and dup not in r.text
        assert c.put("/pool/keys/acme", json={"values": {"ACME_KEY": K2}}).status_code == 409
        for bad in ({}, {"key": ""}, {"key": "a\nb"}, {"key": K3, "tier": "gold"}, {"key": K3, "label": K3}):
            assert c.post("/pool/keys/acme", json=bad).status_code == 409, bad
        assert c.post("/pool/keys/nope", json={"key": K3}).status_code == 404
        # PATCH
        r = c.patch(f"/pool/keys/acme/{kid}", json={"label": "personal", "tier": "unknown", "enabled": False})
        k = r.json()["keys"][1]
        assert (k["label"], k["tier"], k["enabled"], k["usable"], k["reason"]) == (
            "personal",
            None,
            False,
            False,
            "Turned off.",
        )
        assert [x.id for x in router.state.keys(router.catalog.models["acme/big"])] == ["main"]
        assert c.patch(f"/pool/keys/acme/{kid}", json={"enabled": True}).json()["keys"][1]["usable"] is True
        assert c.patch("/pool/keys/acme/zzz", json={"label": "x"}).status_code == 404
        for bad in ({}, {"key": K3}, {"enabled": "yes"}, {"label": 3}):
            assert c.patch(f"/pool/keys/acme/{kid}", json=bad).status_code == 409, bad
        # replacing the first key keeps what was said about it, and the others
        r = c.put("/pool/keys/acme", json={"values": {"ACME_KEY": K3}})
        assert [(k["id"], k["label"], k["last4"]) for k in r.json()["keys"]] == [
            ("main", "work account", "0003"),
            (kid, "personal", "0002"),
        ]
        # DELETE one, then all
        r = c.delete(f"/pool/keys/acme/{kid}")
        assert r.status_code == 200 and [k["id"] for k in r.json()["keys"]] == ["main"] and r.json()["usable"]
        assert c.delete(f"/pool/keys/acme/{kid}").status_code == 404
        kid2 = c.post("/pool/keys/acme", json={"key": K2}).json()["key_id"]
        r = c.delete("/pool/keys/acme/main")  # the first can go while a further one stays
        assert [k["id"] for k in r.json()["keys"]] == [kid2] and r.json()["usable"] and "ACME_KEY" not in os.environ
        assert ask(router)[1].id == "acme/big" and up.keys("acme")[-1] == K2
        c.put("/pool/keys/acme", json={"values": {"ACME_KEY": K1}})
        r = c.delete("/pool/keys/acme")
        assert r.status_code == 200 and r.json()["keys"] == [] and not r.json()["usable"]
        assert json.loads((home / "keys.json").read_text()) == {}


def test_a_key_from_the_environment_cannot_be_removed_from_the_app(home, tmp_path, monkeypatch):
    monkeypatch.setenv("ACME_KEY", K1)
    router, c, _, _ = make(tmp_path)
    with c:
        kid = c.post("/pool/keys/acme", json={"key": K2}).json()["key_id"]
        c.patch("/pool/keys/acme/main", json={"label": "from keys.env"})
        r = c.delete("/pool/keys/acme/main")
        assert r.status_code == 409 and "keys.env" in r.json()["detail"] and K1 not in r.text
        r = c.delete("/pool/keys/acme")  # the app's own go; the environment's stays, with its label
        assert r.status_code == 409 and "ACME_KEY" in r.json()["detail"]
        keys = c.get("/pool/keys").json()["providers"][0]["keys"]
        assert [(k["id"], k["label"], k["source"]) for k in keys] == [("main", "from keys.env", "env")]
        assert kid not in (home / "keys.json").read_text() and K2 not in (home / "keys.json").read_text()


def test_no_answer_and_no_file_but_the_keys_file_holds_a_key(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BETA_KEY", K3)
    router, c, up, _ = make(tmp_path)
    texts = []

    def note(r):
        texts.append(r.text + json.dumps(dict(r.headers)))
        return r

    with c:
        note(c.put("/pool/keys/acme", json={"values": {"ACME_KEY": K1}, "label": "one"}))
        kid = note(c.post("/pool/keys/acme", json={"key": K2, "label": "two", "tier": "paid"})).json()["key_id"]
        note(c.post("/pool/keys/acme", json={"key": K2}))  # 409
        note(c.post("/pool/keys/acme", json={"key": BAD, "label": BAD}))  # 409: the key as its own label
        up.how[BAD] = "401"
        note(c.post("/pool/keys/acme", json={"key": BAD}))  # saved, rejected by the check
        note(c.patch(f"/pool/keys/acme/{kid}", json={"limits": {"scale": 2}}))
        note(c.post("/pool/keys/cf", json={"key": K1, "extras": {"CF_ACCT": "acct-one"}}))
        note(c.post("/v1/chat/completions", json={"model": "hi", "messages": [{"role": "user", "content": "x"}]}))
        up.how[K3] = "401"
        note(c.post("/v1/chat/completions", json={"model": "beta/big2", "messages": []}))  # 502, key echoed upstream
        note(c.post("/v1/responses", json={"model": "hi", "input": "hi"}))
        for path in ("/pool/keys", "/pool/help", "/status", "/v1/sources", "/health", "/v1/models"):
            note(c.get(path))
        note(c.get("/v1/pcore/usage", params={"key": "x"}))
        note(c.delete(f"/pool/keys/acme/{kid}"))
        note(c.delete("/pool/keys/acme"))
        note(c.put("/pool/keys/acme", json={"values": {"ACME_KEY": K1}}))
    assert len(texts) == 20 and "0001" in "".join(texts)  # the last 4 characters are all that is shown
    out = capsys.readouterr()
    for k in FAKES:
        assert all(k not in t for t in texts), k
        assert k not in out.out and k not in out.err
    held = [p for p in tmp_path.rglob("*") if p.is_file() and any(k in p.read_text(errors="replace") for k in FAKES)]
    assert held == [home / "keys.json"]
    assert "acct-one" not in "".join(texts)  # nor an account id


# ---- terms ---------------------------------------------------------------------------------------------------------
def test_the_terms_note_comes_before_a_second_key(home, tmp_path):
    router, c, up, _ = make(tmp_path, path=ROOT / "config" / "catalog.yaml")
    with c:
        keys = {p["id"]: p for p in c.get("/pool/keys").json()["providers"]}
        helps = {p["id"]: p for p in c.get("/pool/help").json()["providers"]}
        assert set(keys) == set(helps)
        for pid, p in keys.items():
            mk = p["multiple_keys"]
            assert mk == helps[pid]["multiple_keys"] and mk["allowed"] in ("yes", "check_terms", "no"), pid
            assert mk["note"].strip(), pid
        for pid in ("openrouter", "kilo", "groq", "cohere"):  # their own terms plainly forbid a second account
            assert keys[pid]["multiple_keys"]["allowed"] == "no", pid
        assert "multiple accounts" in keys["openrouter"]["multiple_keys"]["note"]
        assert keys["cloudflare"]["multiple_keys"]["allowed"] == "check_terms"
        # "no" is said, not enforced: a person may hold a legitimately separate account
        first = "sk-or-v1-" + "0a1b2c3d" * 8
        second = "sk-or-v1-" + "9f8e7d6c" * 8
        assert c.put("/pool/keys/openrouter", json={"values": {"OPENROUTER_API_KEY": first}}).status_code == 200
        r = c.post("/pool/keys/openrouter", json={"key": second, "label": "company account"})
        assert r.status_code == 200 and len(r.json()["keys"]) == 2 and r.json()["multiple_keys"]["allowed"] == "no"
        assert first not in r.text and second not in r.text
        # OpenRouter's 50 a day is one allowance per account: each key has its own
        m = router.catalog.models["openrouter/google/gemma-4-31b-it:free"]
        assert [router.state.limits(m, k)[1].rpd for k in router.state.keys(m)] == [50, 50]
    assert pool_help.multiple_keys("made-up")["allowed"] == "check_terms"


# ---- the pool's clean-up -------------------------------------------------------------------------------------------
def test_removed_providers_and_a_key_left_over(home, tmp_path, monkeypatch):
    home.mkdir()
    left = "sk-test-" + "M1s2T3r4" * 4 + "left"
    (home / "keys.json").write_text(
        json.dumps({"MISTRAL_API_KEY": left, "@keys": {"cerebras": [{"id": "k1", "key": K3}]}})
    )
    for path in (ROOT / "config" / "catalog.yaml", ROOT / "config" / "catalog.app.yaml"):
        cat = load(path, tmp_path / "none.yaml")
        assert not {"mistral", "cerebras"} & set(cat.providers)
        assert not [m for m in cat.models if m.startswith(("mistral/", "cerebras/"))]
        assert {"kilo", "groq", "openrouter", "cloudflare", "inception", "requesty", "ovh"} <= set(cat.providers)
    router, c, up, _ = make(tmp_path, path=ROOT / "config" / "catalog.app.yaml")
    with c:
        r = c.get("/pool/keys")
        assert r.status_code == 200 and left not in r.text and K3 not in r.text
        gone = {x["id"]: x for x in r.json()["removed"]}
        assert set(gone) == {"mistral", "cerebras"}
        for x in gone.values():
            assert x["removed"] == "2026-11-23" and x["reason"] and x["leftover_key"] and "ignored" in x["note"]
        assert "free" in gone["mistral"]["reason"].lower() and "card" in gone["cerebras"]["reason"].lower()
        assert not {"mistral", "cerebras"} & {p["id"] for p in r.json()["providers"]}
        assert c.get("/status").status_code == 200 and c.get("/v1/sources").status_code == 200
        r = c.put("/pool/keys/mistral", json={"values": {"MISTRAL_API_KEY": K1}})  # said why, not a bare 404
        assert r.status_code == 404 and "removed from the pool on 2026-11-23" in r.json()["detail"]
        assert c.post("/pool/keys/cerebras", json={"key": K1}).status_code == 404
        for pid in ("mistral", "cerebras"):  # the leftover can be forgotten
            r = c.delete(f"/pool/keys/{pid}")
            assert r.status_code == 200 and r.json()["leftover_key"] is False
        assert json.loads((home / "keys.json").read_text()) == {} and "MISTRAL_API_KEY" not in os.environ
        h = c.get("/pool/help").json()
        assert [x["id"] for x in h["removed"]] == ["mistral", "cerebras"] and len(h["considered"]) >= 15
    # OVH: its anonymous models stay; the instructions no longer walk anyone to its pay-per-token key
    owner = load(ROOT / "config" / "catalog.yaml", tmp_path / "none.yaml")
    assert owner.providers["ovh"].no_auth and [k.id for k in owner.providers["ovh"].slots()] == ["anon"]
    ovh = pool_help.provider("ovh")
    assert len(ovh["steps"]) == 1 and ovh["time_minutes"] == 0 and ovh["key_var"] == "OVH_AI_ENDPOINTS_ACCESS_TOKEN"
    monkeypatch.setenv("OVH_AI_ENDPOINTS_ACCESS_TOKEN", K2)  # one that is set is still used
    assert [k.id for k in owner.providers["ovh"].slots()] == ["main", "anon"]
    assert load(ROOT / "config" / "catalog.app.yaml", tmp_path / "none.yaml").providers["ovh"].usable


def test_a_provider_is_used_until_its_date(home, tmp_path, monkeypatch):
    monkeypatch.setenv("INCEPTION_API_KEY", K1)
    now = {"t": datetime.datetime(2026, 12, 28, 23, 30, tzinfo=datetime.UTC).timestamp()}
    monkeypatch.setattr(catalog_mod, "clock", lambda: now["t"])  # the clock is injected
    router, c, up, _ = make(tmp_path, path=ROOT / "config" / "catalog.yaml")
    inc, m = router.catalog.providers["inception"], router.catalog.models["inception/mercury-3"]
    tier = router.catalog.tier_of(m)

    def offered():
        return m in router.candidates(tier, 100)

    with c:
        assert inc.until == datetime.date(2026, 12, 28) and inc.usable and not inc.expired and offered()
        v = next(p for p in c.get("/pool/keys").json()["providers"] if p["id"] == "inception")
        assert v["until"] == "2026-12-28" and v["expired"] is False and "Temporary" in v["note"] and v["usable"]
        now["t"] += 3600  # the day after
        assert inc.expired and not inc.usable and not offered()
        v = next(p for p in c.get("/pool/keys").json()["providers"] if p["id"] == "inception")
        assert v["expired"] is True and not v["usable"] and "2026-12-28" in v["note"] and v["keys"][0]["usable"]
        assert "inception/mercury-3" not in [d["id"] for d in c.get("/v1/models").json()["data"]]
        assert not status_of(c, tier, "inception/mercury-3")["usable"]
        r = c.post("/v1/chat/completions", json={"model": "inception/mercury-3", "messages": []})  # not even named
        assert r.status_code == 502 and "stopped on 2026-12-28" in r.json()["detail"] and not up.keys("inception")
    h = pool_help.provider("inception")
    assert h["until"] == "2026-12-28" and "TEMPORARY" in h["what_you_get"] and h["recommended"] is False
    assert "**Temporary: used until 2026-12-28.**" in pool_help.render()
    bad = {**CAT, "providers": {**CAT["providers"], "acme": {**CAT["providers"]["acme"], "until": "soon"}}}
    (tmp_path / "bad.yaml").write_text(yaml.safe_dump(bad))
    with pytest.raises(ValueError, match="until"):
        load(tmp_path / "bad.yaml")


def test_the_kept_and_the_new_providers(home, tmp_path):
    cat = load(ROOT / "config" / "catalog.yaml", tmp_path / "none.yaml")
    ids = set(cat.models)
    assert "cloudflare/@cf/nvidia/nemotron-3-120b-a12b" not in ids  # deprecated 2026-11-20
    assert "nvidia/meta/llama-3.3-70b-instruct" not in ids and "nvidia/moonshotai/kimi-k3.1-pro" in ids
    order = [p["id"] for p in pool_help.load()["providers"]]
    assert order.index("sambanova") > order.index("cohere") and order[-2:] == ["inception", "ovh"]
    assert {"cloudflare/@cf/google/gemma-4-26b-a4b-it", "requesty/google/gemma-4-31b-it"} <= ids
    assert not cat.providers["requesty"].usable and not cat.models["requesty/google/gemma-4-31b-it"].calibrated
    assert (cat.providers["requesty"].pool.rpd, cat.providers["openrouter"].pool.rpd) == (180, 50)
    kilo = cat.providers["kilo"]
    assert kilo.no_auth and kilo.usable and kilo.pool_per == "ip" and kilo.api_key_env == "KILO_API_KEY"
    # Kilo's hourly limit is per IP address: a key does not add a second allowance
    st, m = State(), cat.models["kilo/reflection/beam-free"]
    os.environ["KILO_API_KEY"] = "eyJ" + "x" * 30
    try:
        main, anon = st.keys(m)
        st.record(m, 10, key=main)
        assert st._pool_key(m, main) == st._pool_key(m, anon) == "@kilo"
        assert len(st.usage["@kilo"].day) == 1 and st.headroom(m, 10, anon) == 0.9  # its own 10 a minute untouched
    finally:
        os.environ.pop("KILO_API_KEY")
    doc = (ROOT / "docs" / "pool-keys.md").read_text(encoding="utf-8")
    assert doc == pool_help.render()  # the doc is generated from the data
    assert "## Removed from the pool" in doc and "## Looked at and not added" in doc and "(`requesty`)" in doc
    assert "Keys from several accounts: the provider's terms forbid a second account." in doc


# ---- remembering what worked ---------------------------------------------------------------------------------------
GOOD = {
    "result": "saved_working",
    "minutes": 4,
    "source": "assistant",
    "step_notes": [
        {"step": 1, "ok": True},
        {"step": 2, "ok": False, "note": "The button is called Create API Key, at the top right."},
        {"step": "Verify your phone number", "ok": False, "note": "An extra step the instructions do not have."},
    ],
    "labels_seen": ["API Keys", "Create API Key", "Display Name"],
}


def test_outcomes_are_recorded_and_become_experience(home, tmp_path):
    router, c, _, _ = make(tmp_path)
    with c:
        r = c.post("/pool/help/groq/outcome", json=GOOD)
        assert r.status_code == 200 and r.json()["recorded"]["provider"] == "groq"
        assert r.json()["experience"]["tries"] == 1 and r.json()["experience"]["worked"] == 1
        again = {
            "result": "gave_up",
            "source": "manual",
            "step_notes": [{"step": 2, "ok": False, "note": "No such button."}],
        }
        assert c.post("/pool/help/groq/outcome", json=again).status_code == 200
        assert (
            c.post("/pool/help/openrouter/outcome", json={"result": "skipped", "source": "manual"}).status_code == 200
        )
        lines = [json.loads(x) for x in (home / "pool-help-outcomes.jsonl").read_text().splitlines()]
        today = datetime.datetime.now(datetime.UTC).date().isoformat()
        assert [(x["provider"], x["result"], x["date"]) for x in lines] == [
            ("groq", "saved_working", today),
            ("groq", "gave_up", today),
            ("openrouter", "skipped", today),
        ]
        assert lines[0]["minutes"] == 4 and lines[0]["labels_seen"] == GOOD["labels_seen"] and "minutes" not in lines[1]
        h = {p["id"]: p["experience"] for p in c.get("/pool/help").json()["providers"]}
        assert h["groq"] == {
            "tries": 2,
            "worked": 1,
            "last_worked": today,
            "common_trouble": [
                {
                    "step": 2,
                    "count": 2,
                    "notes": ["The button is called Create API Key, at the top right.", "No such button."],
                },
                {
                    "step": "Verify your phone number",
                    "count": 1,
                    "notes": ["An extra step the instructions do not have."],
                },
            ],
        }
        assert h["openrouter"] == {"tries": 1, "worked": 0, "last_worked": None, "common_trouble": []}
        assert h["cloudflare"]["tries"] == 0
        one = c.get("/pool/help", params={"provider": "groq"}).json()["provider"]
        assert one["experience"]["tries"] == 2
        # not taken: nothing of it is written
        before = (home / "pool-help-outcomes.jsonl").read_text()
        assert c.post("/pool/help/nope/outcome", json=GOOD).status_code == 404
        for bad in (
            {},
            {**GOOD, "result": "great"},
            {**GOOD, "source": "robot"},
            {**GOOD, "minutes": -1},
            {**GOOD, "step_notes": [{"step": 1}]},
            {**GOOD, "labels_seen": "API Keys"},
            {**GOOD, "key": "x"},
        ):
            assert c.post("/pool/help/groq/outcome", json=bad).status_code == 400, bad
        assert (home / "pool-help-outcomes.jsonl").read_text() == before
    # the instructions themselves are never rewritten
    assert pool_help.provider("groq")["steps"][1] == "On the **API Keys** page click **Create API Key**."


def test_outcomes_with_anything_like_a_key_are_refused(home, tmp_path):
    router, c, _, _ = make(tmp_path)
    secrets = [
        "gsk_" + "a1B2" * 13,  # each provider's documented shape (the help file's key_pattern)
        "sk-or-v1-" + "0a1b2c3d" * 8,
        "cfut_" + "Ab3d" * 12,
        "eyJ" + "hbGciOi" * 3 + "." + "eyJzdWIi" * 4 + "." + "c2lnbmF0dXJl" * 3,
        "Qx7" * 13 + "Z",  # 40 letters and digits: an old Cloudflare token, or anything
        "AIzaSyD4" + "k9Xw2Lq7" * 4,  # no documented pattern: a known prefix
        "9f8e7d6c5b4a39281706f5e4d3c2b1a0",  # an account id
        "nB7xQ2mK9pL4wZ8vR3tY6uJ1hG5fD0sA",  # no prefix at all: random-looking
        K1,
    ]
    with c:
        for s in secrets:
            for body in (
                {**GOOD, "step_notes": [{"step": 2, "ok": False, "note": f"I pasted {s} and it said invalid"}]},
                {**GOOD, "step_notes": [{"step": f"copy {s}", "ok": True}]},
                {**GOOD, "labels_seen": ["API Keys", s]},
            ):
                r = c.post("/pool/help/groq/outcome", json=body)
                assert r.status_code == 400 and s not in r.text and "nothing was recorded" in r.json()["detail"], s
        r = c.post("/pool/help/groq/outcome", json={**GOOD, "labels_seen": ["Signed in as someone@example.com"]})
        assert r.status_code == 400 and "email" in r.json()["detail"] and "someone" not in r.text
        assert not (home / "pool-help-outcomes.jsonl").exists()
        # ordinary notes pass: page addresses, button names, long words
        fine = {
            **GOOD,
            "step_notes": [
                {
                    "step": 3,
                    "ok": False,
                    "note": "The page is https://console.groq.com/keys now; the dialog asks for a Display Name "
                    "and an expiration (30 days, 90 days, no expiration). Step 2 of 4 took 20 seconds.",
                }
            ],
            "labels_seen": ["Create a Workers AI API Token", "Continue-to-summary", "Workers AI - Read"],
        }
        assert c.post("/pool/help/groq/outcome", json=fine).status_code == 200
    assert not pool_help.looks_like_key("Open **Settings** > **API Keys** and click **Create API Key**.")
    for p in pool_help.load()["providers"]:  # none of the written instructions would be refused
        for text in [*p["steps"], *p["gotchas"]]:
            assert not pool_help.looks_like_key(text), (p["id"], text)


def test_review_prints_what_disagrees_with_the_written_steps(home, tmp_path):
    assert "No outcomes recorded yet" in pool_help.review()
    pool_help.record_outcome("groq", GOOD)
    pool_help.record_outcome(
        "groq", {"result": "gave_up", "source": "manual", "step_notes": [{"step": 2, "ok": False}]}
    )
    pool_help.record_outcome("cloudflare", {"result": "saved_working", "source": "assistant", "minutes": 6})
    out = pool_help.review()
    groq = out[out.index("== groq") : out.index("== cloudflare")]
    assert "2 tries, 1 saved_working, 1 gave_up" in groq and "median 4 (written: 3)" in groq
    assert "step 2, reported wrong 2x. Written: On the **API Keys** page click **Create API Key**." in groq
    assert "- The button is called Create API Key, at the top right." in groq
    assert "a step not among the written ones, reported wrong 1x: Verify your phone number" in groq
    assert "labels seen on the page that the steps do not mention: Display Name (1x)" in groq  # the others are there
    assert "no step was reported as wrong" in out[out.index("== cloudflare") :]
    r = subprocess.run(
        [sys.executable, "-m", "provenance_core.keys.pool_help", "--review", str(home)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert r.returncode == 0 and r.stdout == out
    before = (ROOT / "provenance_core" / "keys" / "pool_help.yaml").read_text(encoding="utf-8")
    assert "Create API Key, at the top right" not in before  # reviewing changes nothing
