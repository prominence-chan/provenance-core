"""Usage ledger (provenance_core/ledger.py) and GET /v1/pcore/usage."""

import json

from test_adapters import env, parse_sse  # noqa: F401  (fixture)

from provenance_core.ledger import Ledger, request_keys


def test_request_keys_prefers_thread_and_turn():
    req = {"prompt_cache_key": "sess", "client_metadata": {"thread_id": "th", "turn_id": "tu", "session_id": "s"}}
    assert request_keys(req, "h:abc|solstice") == ("th", "tu")
    assert request_keys({"prompt_cache_key": "sess"}, "h:abc|solstice") == ("sess", None)
    assert request_keys({}, "h:abc|solstice") == ("h:abc", None)


def test_query_counts_by_turn_and_window(tmp_path):
    led = Ledger(tmp_path / "l.jsonl", cap=100)
    led.record("t1", "a/x", "spark", "turnA", 10, 5, ts=100)
    led.record("t1", "a/x", "spark", "turnA", 20, 5, ts=101)
    led.record("t1", "b/y", "spark", "turnA", 5, 1, ts=102)
    led.record("t1", "a/x", "spark", "turnB", 1, 1, ts=200)
    led.record("t1", "c/z", "spark", None, 1, 1, ts=150)  # no turn id: matched by window only
    led.record("t2", "a/x", "spark", "turnA", 1, 1, ts=100)  # other conversation
    q = led.query("t1", "turnA", since=99, until=160)
    assert q["requests"] == 4
    assert [(m["model"], m["requests"]) for m in q["models"]] == [("a/x", 2), ("b/y", 1), ("c/z", 1)]
    assert q["models"][0]["input_tokens"] == 30 and q["input_tokens"] == 36
    assert led.query("t1", "turnB")["requests"] == 1
    assert led.query("t1", since=199)["requests"] == 1
    assert led.query("nope")["models"] == []


def test_persists_bounded_and_compacts(tmp_path):
    p = tmp_path / "l.jsonl"
    led = Ledger(p, cap=5)
    for i in range(12):
        led.record("k", f"m{i}", ts=i)
    assert len(led.entries) == 5
    assert len(p.read_text().splitlines()) <= 10  # compacted past 2x cap
    again = Ledger(p, cap=5)
    assert [e["model"] for e in again.entries] == [f"m{i}" for i in range(7, 12)]
    p.write_text(p.read_text() + "not json\n")
    assert len(Ledger(p, cap=5).entries) == 5


def test_memory_only(tmp_path):
    led = Ledger("", cap=3)
    led.record("k", "m")
    assert led.path is None and led.query("k")["requests"] == 1


def test_responses_requests_land_in_ledger(env):  # noqa: F811
    c, up, _ = env
    meta = {"thread_id": "thr-1", "turn_id": "turn-1"}
    r = c.post("/v1/responses", json={"model": "solstice", "input": "hi", "client_metadata": meta})
    assert r.status_code == 200, r.text
    r = c.post(
        "/v1/responses",
        json={"model": "solstice", "input": "hi", "stream": True, "client_metadata": meta, "prompt_cache_key": "s"},
    )
    assert r.status_code == 200 and parse_sse(r.text)[-1][0] == "response.completed"
    c.post("/v1/responses", json={"model": "solstice", "input": "hi", "client_metadata": {**meta, "turn_id": "turn-2"}})
    j = c.get("/v1/pcore/usage", params={"key": "thr-1", "turn": "turn-1"}).json()
    assert j["requests"] == 2 and len(j["models"]) == 1
    m = j["models"][0]
    assert m["model"] == "a/front" and m["tier"] == "solstice" and m["requests"] == 2
    assert m["input_tokens"] > 0 and m["output_tokens"] > 0
    assert c.get("/v1/pcore/usage", params={"key": "thr-1"}).json()["requests"] == 3
    assert c.get("/v1/pcore/usage", params={"key": "thr-1", "since": 9e12}).json()["requests"] == 0
    assert c.get("/v1/pcore/usage").status_code == 422  # key is required


def test_usage_endpoint_needs_token(env, monkeypatch):  # noqa: F811
    from provenance_core.server import create_app

    _, _, router = env
    monkeypatch.setenv("PCORE_TOKEN", "sekrit")
    from fastapi.testclient import TestClient

    c = TestClient(create_app(router=router))
    assert c.get("/v1/pcore/usage", params={"key": "k"}).status_code == 401
    ok = c.get("/v1/pcore/usage", params={"key": "k"}, headers={"Authorization": "Bearer sekrit"})
    assert ok.status_code == 200 and json.loads(ok.text)["requests"] == 0
