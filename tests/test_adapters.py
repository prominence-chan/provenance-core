"""Anthropic Messages (/v1/messages) and OpenAI Responses (/v1/responses) adapters against a fake OpenAI-chat upstream."""
import json

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from provenance_core.catalog import load
from provenance_core.router import Router
from provenance_core.server import create_app
from provenance_core.state import State

CAT = {
    "tiers": [{"name": "nova", "min_score": 94}, {"name": "solstice", "min_score": 82},
              {"name": "spark", "min_score": 62}, {"name": "ember", "min_score": 0}],
    "providers": {"a": {"base_url": "https://a.test/v1", "api_key_env": "A_KEY"},
                  "b": {"base_url": "https://b.test/v1", "api_key_env": "B_KEY"}},
    "models": [{"provider": "a", "model": "top", "score": 96},
               {"provider": "a", "model": "front", "score": 88},
               {"provider": "b", "model": "front2", "score": 85},
               {"provider": "b", "model": "mid", "score": 70},
               {"provider": "b", "model": "tiny", "score": 30}],
}


class Upstream:
    """Fake OpenAI chat upstream. `script[model]` = list of replies consumed in order (last one repeats):
    a dict (non-stream message), a list of chunk dicts (stream), or an int HTTP status."""

    def __init__(self):
        self.bodies: list[dict] = []
        self.script: dict[str, list] = {}

    def handler(self, req: httpx.Request):
        body = json.loads(req.content)
        self.bodies.append(body)
        replies = self.script.get(body["model"]) or [{"content": f"answer from {body['model']}"}]
        r = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(r, int):
            return httpx.Response(r, text="nope")
        if body.get("stream"):
            chunks = r if isinstance(r, list) else [{"choices": [{"delta": {"content": r.get("content")}, "index": 0}]},
                                                    {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
            text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})
        msg = {"role": "assistant", "content": r.get("content")}
        if r.get("tool_calls"):
            msg["tool_calls"] = r["tool_calls"]
        return httpx.Response(200, json={"choices": [{"index": 0, "message": msg,
                                                      "finish_reason": r.get("finish", "tool_calls" if r.get("tool_calls") else "stop")}],
                                         "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}})

    @property
    def last(self) -> dict:
        return self.bodies[-1]


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("A_KEY", "B_KEY"):
        monkeypatch.setenv(k, "x")
    for k in ("TOKEN", "MAP_OPUS", "MAP_SONNET", "MAP_HAIKU", "MAP_DEFAULT"):
        for prefix in ("PCORE_", "TIERLM_"):
            monkeypatch.delenv(prefix + k, raising=False)
    p = tmp_path / "catalog.yaml"
    p.write_text(yaml.safe_dump(CAT))
    up = Upstream()
    router = Router(load(p), State(), httpx.AsyncClient(transport=httpx.MockTransport(up.handler)))
    return TestClient(create_app(router=router)), up, router


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        ev, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        out.append((ev, data))
    return out


TOOL_CHUNKS = [
    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Let me "}}]},
    {"choices": [{"index": 0, "delta": {"content": "look."}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                        "function": {"name": "read_file", "arguments": ""}}]}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"path\": "}}]}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"a.py\"}"}}]}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 1, "id": "call_2", "type": "function",
                                                        "function": {"name": "ls", "arguments": "{}"}}]}}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
     "usage": {"prompt_tokens": 50, "completion_tokens": 9, "total_tokens": 59}},
]


# ---- Anthropic Messages --------------------------------------------------------------------------------------------
def test_messages_text_round_trip(env):
    c, up, _ = env
    r = c.post("/v1/messages?beta=true", headers={"anthropic-version": "2023-06-01"}, json={
        "model": "claude-sonnet-4-5-20250929", "max_tokens": 64000, "temperature": 0.2, "stop_sequences": ["END"],
        "system": [{"type": "text", "text": "You are terse.", "cache_control": {"type": "ephemeral"}},
                   {"type": "text", "text": "Be kind."}],
        "thinking": {"type": "enabled", "budget_tokens": 2000},
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]})
    assert r.status_code == 200, r.text
    assert r.headers["x-pcore-tier"] == "solstice" and r.headers["x-pcore-model"] == "a/front"
    j = r.json()
    assert j["type"] == "message" and j["role"] == "assistant" and j["model"] == "claude-sonnet-4-5-20250929"
    assert j["content"] == [{"type": "text", "text": "answer from front"}]
    assert j["stop_reason"] == "end_turn" and j["usage"]["input_tokens"] == 11 and j["usage"]["output_tokens"] == 7
    b = up.last
    assert b["messages"] == [{"role": "system", "content": "You are terse.\n\nBe kind."}, {"role": "user", "content": "hi"}]
    assert b["temperature"] == 0.2 and b["stop"] == ["END"] and b["max_tokens"] == 16384
    assert "thinking" not in b and "system" not in b


def test_messages_tool_round_trip_and_tool_result_translation(env):
    c, up, _ = env
    up.script["front"] = [{"content": None, "tool_calls": [
        {"id": "call_9", "type": "function", "function": {"name": "Bash", "arguments": "{\"command\": \"ls\"}"}}]}]
    r = c.post("/v1/messages", json={
        "model": "claude-sonnet-4-5", "max_tokens": 1000,
        "tools": [{"name": "Bash", "description": "run", "input_schema": {
            "$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
            "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"type": "web_search_20250305", "name": "web_search"}],
        "tool_choice": {"type": "any"},
        "messages": [
            {"role": "user", "content": "list files"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm", "signature": "x"},
                                              {"type": "text", "text": "Running."},
                                              {"type": "tool_use", "id": "toolu_01", "name": "Bash", "input": {"command": "ls"}},
                                              {"type": "tool_use", "id": "toolu_02", "name": "Bash", "input": {"command": "pwd"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": "a.py\nb.py"},
                {"type": "tool_result", "tool_use_id": "toolu_02", "is_error": True,
                 "content": [{"type": "text", "text": "permission denied"},
                             {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]},
                {"type": "text", "text": "continue"}]}]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["stop_reason"] == "tool_use"
    assert j["content"] == [{"type": "tool_use", "id": "call_9", "name": "Bash", "input": {"command": "ls"}}]
    b = up.last
    assert b["tool_choice"] == "required"
    assert [t["function"]["name"] for t in b["tools"]] == ["Bash"]   # server tool dropped
    assert "$schema" not in b["tools"][0]["function"]["parameters"]
    roles = [m["role"] for m in b["messages"]]
    assert roles == ["user", "assistant", "tool", "tool", "user"]
    a = b["messages"][1]
    assert a["content"] == "Running." and [tc["id"] for tc in a["tool_calls"]] == ["toolu_01", "toolu_02"]
    assert json.loads(a["tool_calls"][1]["function"]["arguments"]) == {"command": "pwd"}
    assert b["messages"][2] == {"role": "tool", "tool_call_id": "toolu_01", "content": "a.py\nb.py"}
    assert b["messages"][3]["tool_call_id"] == "toolu_02"
    assert b["messages"][3]["content"].startswith("[tool error]") and "permission denied" in b["messages"][3]["content"]
    last = b["messages"][4]["content"]
    assert {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}} in last
    assert last[-1] == {"type": "text", "text": "continue"}

    # the tool_use comes back to us as history on the next turn, with the same id
    r = c.post("/v1/messages", json={"model": "claude-sonnet-4-5", "max_tokens": 100, "messages": [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": j["content"]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_9", "content": "ok"}]}]})
    assert up.last["messages"][1]["tool_calls"][0]["id"] == up.last["messages"][2]["tool_call_id"] == "call_9"


def test_messages_system_role_inside_messages_becomes_user(env):
    c, up, _ = env
    r = c.post("/v1/messages", json={"model": "claude-sonnet-4-5", "max_tokens": 10, "system": "top", "messages": [
        {"role": "user", "content": "a"}, {"role": "system", "content": "env info"},
        {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]})
    assert r.status_code == 200
    assert [m["role"] for m in up.last["messages"]] == ["system", "user", "user", "assistant", "user"]


def test_messages_model_mapping(env, monkeypatch):
    c, up, _ = env

    def tier(model):
        r = c.post("/v1/messages", json={"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": model}]})
        assert r.status_code == 200, r.text
        return r.headers["x-pcore-tier"]
    assert tier("claude-opus-4-6[1m]") == "nova"
    assert tier("claude-sonnet-4-5-20250929") == "solstice"
    assert tier("claude-3-5-haiku-20241022") == "spark"
    assert tier("solstice") == "solstice" and tier("tier-4") == "ember"
    r = c.post("/v1/messages", json={"model": "b/tiny", "max_tokens": 10, "messages": [{"role": "user", "content": "x"}]})
    assert r.headers["x-pcore-model"] == "b/tiny"
    monkeypatch.setenv("PCORE_MAP_OPUS", "spark")
    assert tier("claude-opus-4-1") == "spark"


def test_messages_falls_back_down(env):
    c, up, _ = env
    up.script["top"] = [500]
    r = c.post("/v1/messages", json={"model": "claude-opus-4-5", "max_tokens": 10,
                                     "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200 and r.headers["x-pcore-tier"] == "solstice"   # nova failed, fallback "any"


def test_messages_no_capacity_is_overloaded(env):
    c, up, _ = env
    for m in ("top", "front", "front2", "mid", "tiny"):
        up.script[m] = [500]
    r = c.post("/v1/messages", json={"model": "claude-opus-4-5", "max_tokens": 10, "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 529 and r.json()["error"]["type"] == "overloaded_error"


def test_messages_streaming_event_sequence(env):
    c, up, _ = env
    up.script["front"] = [TOOL_CHUNKS]
    r = c.post("/v1/messages", json={"model": "claude-sonnet-4-5", "max_tokens": 100, "stream": True,
                                     "tools": [{"name": "read_file", "input_schema": {"type": "object"}},
                                               {"name": "ls", "input_schema": {"type": "object"}}],
                                     "messages": [{"role": "user", "content": "go"}]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = parse_sse(r.text)
    names = [e for e, _ in evs]
    assert names == ["message_start", "ping",
                     "content_block_start", "content_block_delta", "content_block_delta", "content_block_stop",
                     "content_block_start", "content_block_delta", "content_block_stop",
                     "content_block_start", "content_block_delta", "content_block_stop",
                     "message_delta", "message_stop"]
    assert all(d["type"] == e for e, d in evs)
    start = evs[0][1]["message"]
    assert start["role"] == "assistant" and start["content"] == [] and start["usage"]["input_tokens"] > 0
    assert evs[2][1] == {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
    assert "".join(d["delta"]["text"] for e, d in evs if d.get("delta", {}).get("type") == "text_delta") == "Let me look."
    assert evs[6][1]["index"] == 1 and evs[6][1]["content_block"] == {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {}}
    assert evs[7][1]["delta"]["type"] == "input_json_delta" and json.loads(evs[7][1]["delta"]["partial_json"]) == {"path": "a.py"}
    assert evs[9][1]["content_block"]["name"] == "ls" and evs[9][1]["index"] == 2
    md = evs[12][1]
    assert md["delta"]["stop_reason"] == "tool_use" and md["usage"]["output_tokens"] == 9
    # the upstream was asked to stream
    assert up.last["stream"] is True


def test_messages_streaming_text_only_and_parallel_disabled(env):
    c, up, _ = env
    r = c.post("/v1/messages", json={"model": "claude-haiku-4-5", "max_tokens": 10, "stream": True,
                                     "messages": [{"role": "user", "content": "x"}]})
    evs = parse_sse(r.text)
    assert [e for e, _ in evs][-2:] == ["message_delta", "message_stop"] and evs[-2][1]["delta"]["stop_reason"] == "end_turn"
    up.script["front"] = [TOOL_CHUNKS]
    r = c.post("/v1/messages", json={"model": "claude-sonnet-4-5", "max_tokens": 10, "stream": True,
                                     "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
                                     "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
                                     "messages": [{"role": "user", "content": "y"}]})
    starts = [d["content_block"]["type"] for e, d in parse_sse(r.text) if e == "content_block_start"]
    assert starts == ["text", "tool_use"]


def test_messages_auth_count_tokens_and_unknown_endpoints(env, monkeypatch):
    _, up, router = env
    monkeypatch.setenv("PCORE_TOKEN", "sekrit")
    c = TestClient(create_app(router=router))
    body = {"model": "claude-sonnet-4-5", "max_tokens": 10, "messages": [{"role": "user", "content": "hello there"}]}
    assert c.post("/v1/messages", json=body).status_code == 401
    assert c.post("/v1/messages", json=body, headers={"x-api-key": "sekrit"}).status_code == 200
    assert c.post("/v1/messages", json=body, headers={"authorization": "Bearer sekrit"}).status_code == 200
    r = c.post("/v1/messages/count_tokens", json=body, headers={"x-api-key": "sekrit"})
    assert r.status_code == 200 and r.json()["input_tokens"] > 0
    r = c.post("/api/event_logging/v2/batch", json={})
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found_error"
    assert c.get("/api/hello").status_code == 404
    m = c.get("/v1/models", headers={"x-api-key": "sekrit"})
    assert m.status_code == 401   # /v1/models keeps the bearer-only check
    m = c.get("/v1/models", headers={"authorization": "Bearer sekrit"}).json()
    assert m["has_more"] is False and m["data"][0]["type"] == "model" and m["first_id"] == "nova"


def test_messages_sticky_session_from_first_user_message(env):
    c, up, router = env
    up.script["front"] = [500, {"content": "ok"}]
    first = {"role": "user", "content": "build me a thing"}
    r = c.post("/v1/messages", json={"model": "claude-sonnet-4-5", "max_tokens": 10, "messages": [first]})
    assert r.headers["x-pcore-model"] == "b/front2"   # front failed once
    router.state.health.clear()                        # front is healthy again
    r = c.post("/v1/messages", json={"model": "claude-sonnet-4-5", "max_tokens": 10, "messages": [
        first, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "more"}]})
    assert r.headers["x-pcore-model"] == "b/front2"   # same conversation stays put
    r = c.post("/v1/messages", json={"model": "claude-sonnet-4-5", "max_tokens": 10,
                                     "messages": [{"role": "user", "content": "another conversation"}]})
    assert r.headers["x-pcore-model"] == "a/front"
    r = c.post("/v1/messages", headers={"x-tierlm-session": "s1"}, json={
        "model": "claude-sonnet-4-5", "max_tokens": 10, "messages": [first]})
    assert r.headers["x-pcore-model"] == "a/front"   # an explicit session header wins over the hash


# ---- OpenAI Responses ------------------------------------------------------------------------------------------------
def test_responses_text_round_trip_and_previous_response_id(env):
    c, up, _ = env
    r = c.post("/v1/responses", json={"model": "solstice", "instructions": "Be brief.", "input": "hi",
                                      "max_output_tokens": 500})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["object"] == "response" and j["status"] == "completed" and j["id"].startswith("resp_")
    assert j["output"][0]["type"] == "message" and j["output"][0]["content"][0] == {
        "type": "output_text", "text": "answer from front", "annotations": [], "logprobs": []}
    assert j["usage"]["input_tokens"] == 11 and j["usage"]["output_tokens"] == 7 and j["usage"]["total_tokens"] == 18
    assert up.last["messages"] == [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]
    assert up.last["max_tokens"] == 500
    r2 = c.post("/v1/responses", json={"model": "solstice", "previous_response_id": j["id"], "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "again"}]}]})
    assert r2.status_code == 200
    assert up.last["messages"] == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "answer from front"},
                                   {"role": "user", "content": "again"}]
    assert c.get(f"/v1/responses/{j['id']}").json()["id"] == j["id"]
    r3 = c.post("/v1/responses", json={"model": "solstice", "previous_response_id": "resp_nope", "input": "x"})
    assert r3.status_code == 400 and r3.json()["error"]["code"] == "previous_response_not_found"


def test_responses_tool_round_trip(env):
    c, up, _ = env
    up.script["top"] = [{"content": None, "tool_calls": [
        {"id": "call_7", "type": "function", "function": {"name": "shell", "arguments": "{\"command\": [\"ls\"]}"}}]}]
    r = c.post("/v1/responses", json={
        "model": "nova", "tool_choice": "auto", "parallel_tool_calls": False,
        "tools": [{"type": "function", "name": "shell", "description": "run", "strict": False,
                   "parameters": {"type": "object", "properties": {"command": {"type": "array", "items": {"type": "string"}}}}},
                  {"type": "web_search"}],
        "input": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "sandbox: none"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "list"},
                                                             {"type": "input_image", "image_url": "data:image/png;base64,QQ=="}]},
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "zzz"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Checking."}]},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "shell", "arguments": "{\"command\":[\"pwd\"]}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "/tmp"}]})
    assert r.status_code == 200, r.text
    out = r.json()["output"]
    assert len(out) == 1 and out[0]["type"] == "function_call" and out[0]["call_id"] == "call_7"
    assert out[0]["name"] == "shell" and json.loads(out[0]["arguments"]) == {"command": ["ls"]}
    b = up.last
    assert [t["function"]["name"] for t in b["tools"]] == ["shell"] and b["tool_choice"] == "auto"
    assert b["messages"][0] == {"role": "system", "content": "sandbox: none"}
    assert b["messages"][1]["content"][1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}}
    a = b["messages"][2]
    assert a["role"] == "assistant" and a["content"] == "Checking." and a["tool_calls"][0]["id"] == "call_1"
    assert b["messages"][3] == {"role": "tool", "tool_call_id": "call_1", "content": "/tmp"}


def test_responses_custom_and_local_shell_tools(env):
    c, up, _ = env
    up.script["front"] = [{"content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "apply_patch", "arguments": "{\"input\": \"*** Begin Patch\"}"}},
        {"id": "c2", "type": "function", "function": {"name": "local_shell", "arguments": "{\"command\": [\"ls\", \"-la\"]}"}}]}]
    r = c.post("/v1/responses", json={"model": "solstice", "input": "patch it", "tools": [
        {"type": "custom", "name": "apply_patch", "description": "patch",
         "format": {"type": "grammar", "syntax": "lark", "definition": "start: x"}},
        {"type": "local_shell"}]})
    out = r.json()["output"]
    assert out[0] == {**out[0], "type": "custom_tool_call", "call_id": "c1", "name": "apply_patch", "input": "*** Begin Patch"}
    assert out[1]["type"] == "local_shell_call" and out[1]["action"]["command"] == ["ls", "-la"]
    assert "start: x" in up.last["tools"][0]["function"]["description"]
    # and they translate back as history
    r = c.post("/v1/responses", json={"model": "solstice", "input": [
        {"type": "message", "role": "user", "content": "patch it"}, *out,
        {"type": "custom_tool_call_output", "call_id": "c1", "output": "Done!"},
        {"type": "local_shell_call_output", "call_id": "c2", "output": "total 0"}]})
    msgs = up.last["messages"]
    assert [tc["function"]["name"] for tc in msgs[1]["tool_calls"]] == ["apply_patch", "local_shell"]
    assert msgs[2] == {"role": "tool", "tool_call_id": "c1", "content": "Done!"}


def test_responses_streaming_event_sequence(env):
    c, up, _ = env
    up.script["front"] = [TOOL_CHUNKS]
    r = c.post("/v1/responses", json={"model": "solstice", "stream": True, "input": "go", "store": False,
                                      "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}},
                                                {"type": "function", "name": "ls", "parameters": {"type": "object"}}]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = parse_sse(r.text)
    names = [e for e, _ in evs]
    assert names == ["response.created", "response.in_progress",
                     "response.output_item.added", "response.content_part.added",
                     "response.output_text.delta", "response.output_text.delta", "response.output_text.done",
                     "response.content_part.done", "response.output_item.done",
                     "response.output_item.added", "response.function_call_arguments.delta",
                     "response.function_call_arguments.done", "response.output_item.done",
                     "response.output_item.added", "response.function_call_arguments.delta",
                     "response.function_call_arguments.done", "response.output_item.done",
                     "response.completed"]
    assert all(d["type"] == e for e, d in evs)
    seqs = [d["sequence_number"] for _, d in evs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert evs[0][1]["response"]["status"] == "in_progress" and evs[0][1]["response"]["output"] == []
    assert "".join(d["delta"] for e, d in evs if e == "response.output_text.delta") == "Let me look."
    msg_done = evs[8][1]["item"]
    assert msg_done["type"] == "message" and msg_done["content"][0]["text"] == "Let me look."
    fc = evs[12][1]["item"]
    assert fc["type"] == "function_call" and fc["call_id"] == "call_1" and json.loads(fc["arguments"]) == {"path": "a.py"}
    assert evs[12][1]["output_index"] == 1 and evs[16][1]["output_index"] == 2
    done = evs[-1][1]["response"]
    assert done["status"] == "completed" and done["id"] == evs[0][1]["response"]["id"]
    assert [i["type"] for i in done["output"]] == ["message", "function_call", "function_call"]
    assert done["usage"] == {"input_tokens": 50, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 9,
                             "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 59}
    assert c.get(f"/v1/responses/{done['id']}").status_code == 404   # store: false


def test_responses_stream_upstream_error_and_unknown_model(env):
    c, up, _ = env
    up.script["front"] = [[{"choices": [{"index": 0, "delta": {"content": "par"}}]}, {"error": {"message": "boom"}}]]
    evs = parse_sse(c.post("/v1/responses", json={"model": "solstice:none", "stream": True, "input": "x"}).text)
    assert evs[-1][0] == "response.failed" and "boom" in evs[-1][1]["response"]["error"]["message"]
    r = c.post("/v1/responses", json={"model": "gpt-5-codex", "input": "x"})
    assert r.status_code == 200 and r.headers["x-pcore-tier"] == "nova"   # unknown -> strongest tier
