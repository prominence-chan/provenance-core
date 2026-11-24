"""Context strain flags (provenance_core/context_signal.py): noted while a request is served, stored on its ledger
entry, summed per turn in GET /v1/pcore/usage ("context")."""

import asyncio
import json

import httpx
import yaml
from fastapi.testclient import TestClient
from test_adapters import parse_sse
from test_compact import session

from provenance_core.context import context_signal
from provenance_core.routing.catalog import load
from provenance_core.accounting.ledger import Ledger
from provenance_core.routing.router import Router
from provenance_core.api.server import create_app
from provenance_core.routing.state import State


def test_notes_only_inside_a_request_that_asked():
    context_signal.note("compacted")  # no begin(): nothing to note into, and no error
    notes = context_signal.begin()
    context_signal.note_compaction({"trimmed": 0})
    context_signal.note_web({"restarted": True, "compacted": True, "compaction": None})
    context_signal.note("bogus")
    assert context_signal.flags(notes) == ["compacted", "shortened", "restarted"]
    notes = context_signal.begin()
    context_signal.note_compaction({"trimmed": 3})
    context_signal.note_web({"compaction": {"trimmed": 0}})
    assert context_signal.flags(notes) == ["compacted", "trimmed", "summarised"]
    assert context_signal.flags(None) == []


def test_ledger_counts_flags_and_keeps_over_out_of_the_models(tmp_path):
    p = tmp_path / "l.jsonl"
    led = Ledger(p, cap=100)
    led.record("t", "a/x", "spark", "turn1", 10, 1, ctx=["compacted"])
    led.record("t", "a/x", "spark", "turn1", 10, 1, ctx=["compacted", "trimmed"])
    led.record("t", "a/x", "spark", "turn1", 10, 1)
    led.record("t", "", "spark", "turn1", ctx=["over"])
    q = led.query("t", "turn1")
    assert q["requests"] == 3 and [m["model"] for m in q["models"]] == ["a/x"]
    assert q["context"] == {"compacted": 2, "trimmed": 1, "over": 1}
    assert Ledger(p, cap=100).query("t", "turn1")["context"] == q["context"]  # read back after a restart
    assert led.query("t", "other")["context"] == {}


def make_client(tmp_path, monkeypatch, compact_on=True, fail=False):
    """A Responses front end over one 40k-token API model, compaction on or off; `fail`: the model answers 500."""
    monkeypatch.setenv("A_KEY", "x")
    for k in ("PCORE_COMPACT", "PCORE_PRUNE", "PCORE_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    cat = {
        "tiers": [{"name": "solstice", "min_score": 50}, {"name": "spark", "min_score": 0}],
        "providers": {"a": {"base_url": "https://a.test/v1", "api_key_env": "A_KEY"}},
        "models": [{"provider": "a", "model": "small", "score": 90, "context": 40000}],
    }
    if compact_on:
        cat["compact"] = {"api": True}
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(cat))

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        if fail:
            return httpx.Response(500, text="busy")
        if body.get("stream"):
            chunks = [{"choices": [{"delta": {"content": "ok"}, "index": 0}]}]
            chunks.append({"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]})
            text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    router = Router(load(path), State(), client, home=tmp_path / "home")
    return TestClient(create_app(router=router))


def as_items(msgs):
    """Chat messages (test_compact.session) as Responses input items."""
    out = []
    for m in msgs:
        if m["role"] == "tool":
            out.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
        elif m["role"] == "assistant":
            if m.get("content"):
                out.append({"type": "message", "role": "assistant", "content": m["content"]})
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                out.append(
                    {"type": "function_call", "call_id": tc["id"], "name": fn["name"], "arguments": fn["arguments"]}
                )
        else:
            out.append({"type": "message", "role": m["role"], "content": m["content"]})
    return out


def test_a_compacted_request_is_flagged_on_its_turn(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    big = as_items(session(70))  # far over the model's 40k tokens: compaction has to cut it
    meta = {"thread_id": "thr", "turn_id": "t1"}
    r = c.post("/v1/responses", json={"model": "solstice", "input": big, "client_metadata": meta})
    assert r.status_code == 200, r.text
    r = c.post("/v1/responses", json={"model": "solstice", "input": big, "stream": True, "client_metadata": meta})
    assert parse_sse(r.text)[-1][0] == "response.completed"
    c.post("/v1/responses", json={"model": "solstice", "input": "hi", "client_metadata": {**meta, "turn_id": "t2"}})
    u = c.get("/v1/pcore/usage", params={"key": "thr", "turn": "t1"}).json()
    assert u["requests"] == 2 and u["context"].get("compacted") == 2
    assert c.get("/v1/pcore/usage", params={"key": "thr", "turn": "t2"}).json()["context"] == {}


def test_a_history_no_model_can_take_is_flagged_over(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch, compact_on=False)
    big = as_items(session(70))
    meta = {"thread_id": "thr", "turn_id": "t1"}
    r = c.post("/v1/responses", json={"model": "solstice", "input": big, "client_metadata": meta})
    assert r.status_code == 503
    u = c.get("/v1/pcore/usage", params={"key": "thr", "turn": "t1"}).json()
    assert u["requests"] == 0 and u["models"] == [] and u["context"] == {"over": 1}


def test_a_failure_that_is_not_about_size_is_not_flagged(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch, fail=True)
    meta = {"thread_id": "thr", "turn_id": "t1"}
    r = c.post("/v1/responses", json={"model": "solstice", "input": "hi", "client_metadata": meta})
    assert r.status_code == 503
    assert c.get("/v1/pcore/usage", params={"key": "thr", "turn": "t1"}).json()["context"] == {}


def test_web_restart_and_brief_are_flagged(tmp_path, monkeypatch):
    """The router notes what the web bridge reports for a reply (the record its `done` logs)."""
    monkeypatch.setenv("PCORE_CHATGPT_WEB", "1")
    cat = {
        "tiers": [{"name": "solstice", "min_score": 0}],
        "providers": {"w": {"type": "chatgpt_web", "base_url": "https://chatgpt.test", "api_key_env": "none"}},
        "models": [{"provider": "w", "model": "luna", "score": 20, "context": 64000}],
    }
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(cat))
    router = Router(load(path), State(), httpx.AsyncClient(), home=tmp_path / "home")

    class Bridge:
        async def complete(self, model, body, **kw):
            report = {"before": 9, "after": 5, "summarised": {"turns": 4, "messages": 9}, "brief_chars": 3}
            report |= {"writer": "extractive", "cached": False, "ms": 1, "kept": {"messages": 2}, "trimmed": 1}
            web = {"restarted": True, "compaction": report, "sent_chars": 10}
            return {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"total_tokens": 5},
                "pcore": {"web": web},
            }

    router.bridges["w"] = Bridge()
    m = router.catalog.models["w/luna"]

    async def go():
        notes = context_signal.begin()
        await router.call(m, {"messages": [{"role": "user", "content": "hi"}]})
        return notes

    assert context_signal.flags(asyncio.run(go())) == ["trimmed", "summarised", "restarted"]
