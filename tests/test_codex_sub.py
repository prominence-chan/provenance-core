"""The Codex pass-through (provenance_core/codex_sub.py) and the Responses pruning adapter (prune_responses.py),
against a fake upstream: forwarding, streaming, errors, the key, the ledger, nothing secret logged, pruning only old
tool outputs, prefix stability across consecutive requests, fail-open."""

import gzip
import json
import logging
import socket
import threading
import time

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from test_adapters import CAT

from provenance_core.api import codex_sub
from provenance_core.context import prune_responses
from provenance_core.routing.catalog import load
from provenance_core.routing.router import Router
from provenance_core.api.server import create_app
from provenance_core.routing.state import State

ACCESS = "eyJhbGciOiJSUzI1NiJ9.SECRET-ACCESS-TOKEN.sig"
ACCOUNT = "acct-SECRET-ACCOUNT-1234"
TOKEN = "router-SECRET-token"
KEY = codex_sub.path_key(TOKEN)
BASE = f"/codex-sub/{KEY}/backend-api/codex"
CALLER = {
    "authorization": f"Bearer {ACCESS}",
    "chatgpt-account-id": ACCOUNT,
    "originator": "codex_cli_rs",
    "session_id": "sess-1",
    "user-agent": "codex_cli_rs/0.0 (test)",
    "accept": "text/event-stream",
}


# ---- a Codex-like history --------------------------------------------------------------------------------------------
FILE = "\n".join(f"def f{i}(x):\n    return x * {i}  # line {i} of the module" for i in range(60))
HEADER = "Chunk ID: c{n}\nWall time: 0.1021 seconds\nProcess exited with code 0\nOriginal token count: 900\nOutput:\n"


def step(n: int, cmd: str, output: str) -> list[dict]:
    """One sampling step: encrypted reasoning, an exec_command call, its output."""
    cid = f"call_{n}"
    return [
        {"type": "reasoning", "id": f"rs_{n}", "summary": [], "encrypted_content": f"gAAAA-ENCRYPTED-{n}"},
        {
            "type": "function_call",
            "id": f"fc_{n}",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd, "workdir": "/w"}),
            "call_id": cid,
        },
        {"type": "function_call_output", "call_id": cid, "output": HEADER.format(n=n) + output},
    ]


def history(steps: int) -> list[dict]:
    items = [
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "You are Codex. " * 50}]},
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "<environment_context><cwd>/w</cwd></environment_context>"}],
        },
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Fix the module."}]},
    ]
    for n in range(steps):
        if n % 3 == 0:
            items += step(n, "cat src/mod.py", FILE)  # the same read again and again: dup_output
        elif n % 3 == 1:
            items += step(n, "pytest -q", ("FAILED test_mod.py::test_x - assert 1 == 2\n" * 200))  # long_output
        else:
            items += step(n, "git status", "On branch main\nnothing to commit, working tree clean\n" * 4)
        if n % 5 == 4:
            items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "id": f"msg_{n}",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "Let me look again."}],
                }
            )
    return items


CODE_HEADER = "Script completed\nWall time 0.1 seconds\nOutput:\n"


def code_step(n: int, cmd: str, output: str) -> list[dict]:
    """One code-mode step (what current Codex models do): an `exec` custom tool call with a script, its output as
    text parts (the status header, then the script's text)."""
    cid = f"exec_{n}"
    script = f"const r = await tools.exec_command({{cmd: {json.dumps(cmd)}}});\ntext(r.output);"
    return [
        {"type": "custom_tool_call", "id": f"ctc_{n}", "call_id": cid, "name": "exec", "input": script},
        {
            "type": "custom_tool_call_output",
            "call_id": cid,
            "output": [{"type": "input_text", "text": CODE_HEADER}, {"type": "input_text", "text": output}],
        },
    ]


def code_history(steps: int) -> list[dict]:
    items = history(0) + [{"type": "additional_tools", "role": "developer", "tools": [{"name": "exec"}]}]
    for n in range(steps):
        if n % 3 == 0:
            items += code_step(n, "cat src/mod.py", FILE)
        elif n % 3 == 1:
            items += code_step(n, "pytest -q", "FAILED test_mod.py::test_x - assert 1 == 2\n" * 200)
        else:
            items += code_step(n, "git status", "On branch main\nnothing to commit, working tree clean\n" * 4)
    return items


def body_of(items: list[dict], **extra) -> dict:
    return {
        "model": "gpt-test-codex",
        "stream": True,
        "input": items,
        "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"effort": "medium", "summary": "auto"},
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": "thr-1",
        "client_metadata": {"thread_id": "thr-1", "turn_id": "turn-1"},
        **extra,
    }


OPTS = prune_responses.config(None)


# ---- the adapter -----------------------------------------------------------------------------------------------------
def test_only_old_tool_outputs_change():
    items = history(30)
    body = body_of(items)
    snapshot = json.dumps(body)
    out, rep = prune_responses.prune_request(body, OPTS)
    assert json.dumps(body) == snapshot  # the input is left as it is
    assert rep["saved"] > 0 and rep["after"] == len(json.dumps(out["input"], ensure_ascii=False, separators=(",", ":")))
    assert set(rep["rules"]) <= set(prune_responses.SAFE_RULES) and {"dup_output", "long_output"} <= set(rep["rules"])
    new = out["input"]
    assert len(new) == len(items)
    assert {k: v for k, v in out.items() if k != "input"} == {k: v for k, v in body.items() if k != "input"}
    changed = [i for i, (a, b) in enumerate(zip(items, new, strict=True)) if a != b]
    assert changed and all(items[i]["type"] == "function_call_output" for i in changed)
    assert max(changed) < rep["boundary"]  # only the part before the boundary
    for a, b in zip(items, new, strict=False):
        if a["type"] != "function_call_output":
            assert b is a  # reasoning, calls, messages: the very same objects
        else:
            assert b["call_id"] == a["call_id"] and set(b) == set(a)
    assert all(a == b for a, b in zip(items[rep["boundary"] :], new[rep["boundary"] :], strict=True))
    stubs = [b["output"] for a, b in zip(items, new, strict=False) if a != b]
    assert any("same output as a later exec_command call" in s for s in stubs)
    assert any("of old output omitted" in s for s in stubs)


def test_old_write_and_custom_tool_call():
    patch = "*** Begin Patch\n*** Add File: a.txt\n" + "".join(f"+line {i}\n" for i in range(40)) + "*** End Patch"
    env_msg = "<environment_context><cwd>/w</cwd></environment_context>"
    items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": env_msg}]}]
    items.append({"type": "custom_tool_call", "call_id": "p1", "name": "apply_patch", "input": patch})
    items.append({"type": "custom_tool_call_output", "call_id": "p1", "output": "Success. Updated: A a.txt"})
    secret_call = {
        "type": "function_call",
        "call_id": "w2",
        "name": "exec_command",
        "arguments": json.dumps({"cmd": "cat > b.txt <<'EOF'\n" + "x\n" * 30 + "EOF", "workdir": "/w"}),
        "encrypted_function_args": ["opaque"],
    }
    items += [
        secret_call,
        {"type": "function_call_output", "call_id": "w2", "output": "Exit code: 0\nWall time: 0.1 seconds\nOutput:\n"},
    ]
    for n in range(14):  # read both files whole later, then many more turns so the writes are old
        items += step(100 + n, "cat a.txt" if n % 2 else "cat b.txt", ("alpha\n" if n % 2 else "beta\n") * 40)
    out, rep = prune_responses.prune_request(body_of(items), {**OPTS, "keep_turns": 2, "step": 2})
    new = out["input"]
    assert "old_write" in rep["rules"]
    assert new[1]["type"] == "custom_tool_call" and "+[… 40 lines of file content omitted" in new[1]["input"]
    assert new[1]["call_id"] == "p1" and new[1]["name"] == "apply_patch"
    assert new[3] is secret_call  # a call with encrypted arguments keeps them
    assert rep["rules"]["old_write"]["count"] == 1  # and the report counts only what was changed


def test_code_mode_text_parts():
    items = code_history(30)
    out, rep = prune_responses.prune_request(body_of(items), OPTS)
    new = out["input"]
    assert rep["saved"] > 10_000 and {"dup_output", "long_output"} <= set(rep["rules"])
    changed = [i for i, (a, b) in enumerate(zip(items, new, strict=True)) if a != b]
    assert changed and all(items[i]["type"] == "custom_tool_call_output" for i in changed)
    assert max(changed) < rep["boundary"]
    for i in changed:
        parts = new[i]["output"]
        assert new[i]["call_id"] == items[i]["call_id"]
        assert parts and all(p["type"] == "input_text" and isinstance(p["text"], str) for p in parts)
    texts = ["".join(p["text"] for p in new[i]["output"]) for i in changed]
    assert any("same output as a later exec call" in t for t in texts)
    cut = [new[i]["output"] for i in changed if "of old output omitted" in new[i]["output"][-1]["text"]]
    assert cut and all(p[0]["text"] == CODE_HEADER and len(p) == 2 for p in cut)  # the status header stays
    for a, b in zip(items, new, strict=True):
        if a["type"] != "custom_tool_call_output":
            assert b is a


def test_code_mode_prefix_is_stable():
    full = code_history(45)
    ends = [i + 1 for i, it in enumerate(full) if it["type"] == "custom_tool_call_output"]
    same, prev = 0, None
    for end in ends:
        out, rep = prune_responses.prune_request(body_of(full[:end]), OPTS)
        enc = [json.dumps(it, separators=(",", ":")) for it in out["input"]]
        if prev and prev[1]["boundary"] == rep["boundary"]:
            assert enc[: len(prev[0])] == prev[0]
            same += 1
        prev = (enc, rep)
    assert same >= 30


def test_structured_outputs_and_unknown_items_pass():
    items = history(14)
    img = {
        "type": "function_call_output",
        "call_id": "call_0",
        "output": [{"type": "input_image", "image_url": "data:x"}],
    }
    items[
        items.index(next(i for i in items if i.get("call_id") == "call_0" and i["type"] == "function_call_output"))
    ] = img
    items.insert(4, {"type": "compaction", "encrypted_content": "opaque"})
    items.insert(9, {"type": "web_search_call", "id": "ws_1", "status": "completed", "action": {"type": "search"}})
    out, _ = prune_responses.prune_request(body_of(items), OPTS)
    assert img in out["input"] and out["input"][4] is items[4] and out["input"][9] is items[9]


@pytest.mark.parametrize(
    "bad",
    [
        "a string input",
        [{"type": "message", "role": "tool", "content": "?"}],
        [{"type": "function_call", "name": "x", "arguments": "{}"}],  # no call id
        [{"type": "function_call", "call_id": "a", "name": "x", "arguments": "{}"}] * 2,  # duplicate call id
        ["not an item"],
    ],
)
def test_unreadable_raises(bad):
    with pytest.raises(prune_responses.Unsupported):
        prune_responses.prune_request(body_of(bad), OPTS)


def test_consecutive_requests_keep_their_prefix_byte_for_byte():
    """Request k+1 is request k plus the new step's items. Between boundary moves, the pruned request k+1 starts with
    exactly the bytes of the pruned request k: the provider's prompt cache sees the same prefix."""
    full = history(60)
    starts = [i + 1 for i, it in enumerate(full) if it["type"] == "function_call_output"]
    same, moved, saved = 0, 0, 0
    prev = None
    for end in starts:
        out, rep = prune_responses.prune_request(body_of(full[:end]), OPTS)
        enc = [json.dumps(it, ensure_ascii=False, separators=(",", ":")) for it in out["input"]]
        if prev is not None:
            prev_enc, prev_rep = prev
            if rep["boundary"] == prev_rep["boundary"]:
                assert enc[: len(prev_enc)] == prev_enc
                assert "".join(enc).startswith("".join(prev_enc))
                same += 1
            else:
                moved += 1
        saved = max(saved, rep["saved"])
        prev = (enc, rep)
    assert same >= 40 and 3 <= moved <= 8 and saved > 10_000, (same, moved, saved)


def test_config():
    assert prune_responses.config(None) is not None
    assert prune_responses.config({"web": True}) is not None  # other scopes only: on here
    assert prune_responses.config({"codex": False}) is None
    assert prune_responses.config(False) is None
    assert prune_responses.config({"codex": {"step": 16}})["step"] == 16


def test_config_env(monkeypatch):
    monkeypatch.setenv("PCORE_PRUNE", "0")
    assert prune_responses.config(None) is None
    monkeypatch.setenv("PCORE_PRUNE", "codex")
    assert prune_responses.config({"codex": False}) is not None


# ---- the route, against a fake upstream ----------------------------------------------------------------------------
class Chunks(httpx.AsyncByteStream):
    """A body that arrives in pieces, like a real streamed answer (MockTransport reads a `content=` body at once)."""

    def __init__(self, data: bytes, size: int = 37):
        self.parts = [data[i : i + size] for i in range(0, len(data), size)] or [b""]

    async def __aiter__(self):
        for p in self.parts:
            yield p


class FakeUpstream:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.reply = None  # (status, headers, content) or None for the default SSE

    def sse(self, usage=None) -> bytes:
        evs = [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.output_text.delta", "delta": "hi"},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "usage": usage
                    or {"input_tokens": 1234, "input_tokens_details": {"cached_tokens": 1024}, "output_tokens": 56},
                },
            },
        ]
        return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in evs)

    def handler(self, req: httpx.Request):
        req.read()
        self.requests.append(req)
        if self.reply:
            status, headers, content = self.reply
            return httpx.Response(status, headers=headers, stream=Chunks(content))
        if req.url.path.endswith("/responses"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(self.sse()))
        doc = json.dumps({"models": [], "path": req.url.path, "query": str(req.url.query)}).encode()
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=Chunks(doc))

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


@pytest.fixture
def sub(tmp_path, monkeypatch):
    for k in ("A_KEY", "B_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("PCORE_TOKEN", TOKEN)
    monkeypatch.delenv("PCORE_PRUNE", raising=False)
    monkeypatch.setenv("PCORE_CODEX_SUB_UPSTREAM", "https://upstream.test")
    p = tmp_path / "catalog.yaml"
    p.write_text(yaml.safe_dump(CAT))
    router = Router(load(p), State(), httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    app = create_app(router=router)
    up = FakeUpstream()
    app.state.codex_sub_client._transport = httpx.MockTransport(up.handler)
    return TestClient(app), up, app


def test_forwards_headers_and_streams_sse_untouched(sub):
    c, up, app = sub
    body = body_of(history(2))
    r = c.post(f"{BASE}/responses", headers=CALLER, content=json.dumps(body))
    assert r.status_code == 200
    assert r.content == up.sse()  # byte for byte
    assert r.headers["content-type"] == "text/event-stream"
    req = up.last
    assert str(req.url) == "https://upstream.test/backend-api/codex/responses"
    assert req.headers["host"] == "upstream.test"
    for k, v in CALLER.items():
        assert req.headers[k] == v
    assert json.loads(req.content) == body  # nothing to prune in a short history: sent as it came
    e = app.state.usage_ledger.entries[-1]
    assert (e["key"], e["turn"], e["model"], e["tier"]) == ("thr-1", "turn-1", "gpt-test-codex", "codex")
    assert (e["in"], e["out"], e["cached"]) == (1234, 56, 1024)
    assert e["pruned"]["before"] == e["pruned"]["after"]


def test_unchanged_body_goes_on_byte_for_byte(sub):
    c, up, _ = sub
    raw = json.dumps(body_of(history(3)), indent=1).encode()  # whitespace the router must not touch
    c.post(f"{BASE}/responses", headers=CALLER, content=raw)
    assert up.last.content == raw


def test_long_history_is_pruned_and_recorded(sub):
    c, up, app = sub
    body = body_of(history(30))
    raw = json.dumps(body).encode()
    r = c.post(f"{BASE}/responses", headers={**CALLER, "content-encoding": "gzip"}, content=gzip.compress(raw))
    assert r.status_code == 200
    sent = json.loads(up.last.content)
    assert "content-encoding" not in up.last.headers and int(up.last.headers["content-length"]) == len(up.last.content)
    assert len(up.last.content) < len(raw) - 10_000
    assert sent["input"][0] == body["input"][0] and len(sent["input"]) == len(body["input"])
    assert [i.get("call_id") for i in sent["input"]] == [i.get("call_id") for i in body["input"]]
    assert {k: v for k, v in sent.items() if k != "input"} == {k: v for k, v in body.items() if k != "input"}
    q = c.get(
        "/v1/pcore/usage", params={"key": "thr-1", "turn": "turn-1"}, headers={"authorization": f"Bearer {TOKEN}"}
    )
    j = q.json()
    assert j["requests"] == 1 and j["models"][0]["model"] == "gpt-test-codex"
    assert j["pruned"]["saved"] > 10_000 and j["pruned"]["before"] > j["pruned"]["after"]
    assert j["cached_tokens"] == 1024


def test_prune_can_be_turned_off(sub):
    c, up, _ = sub
    body = body_of(history(30))
    raw = json.dumps(body).encode()
    c.post(f"{BASE}/responses", headers={**CALLER, "x-pcore-prune": "off"}, content=raw)
    assert up.last.content == raw and "x-pcore-prune" not in up.last.headers
    c.post(f"{BASE}/responses", headers=CALLER, content=json.dumps({**body, "pcore": {"prune": False}}))
    assert json.loads(up.last.content) == body  # the flag itself is not sent on


def test_fails_open(sub):
    c, up, _ = sub
    for raw, headers in [
        (b'{"model": "x", "input": "just text", "stream": true}', {}),
        (json.dumps(body_of([{"type": "message", "role": "wizard", "content": "?"}])).encode(), {}),
        (b"not json at all", {}),
        (b"\x28\xb5\x2f\xfd-pretend-zstd", {"content-encoding": "zstd"}),
    ]:
        r = c.post(f"{BASE}/responses", headers={**CALLER, **headers}, content=raw)
        assert r.status_code == 200
        assert up.last.content == raw
        assert up.last.headers.get("content-encoding") == headers.get("content-encoding")


def test_errors_pass_through(sub):
    c, up, app = sub
    n = len(app.state.usage_ledger.entries)
    detail = b'{"error":{"message":"You have hit your usage limit","type":"usage_limit_reached"}}'
    up.reply = (
        429,
        {"retry-after": "17", "content-type": "application/json", "x-codex-primary-used-percent": "100"},
        detail,
    )
    r = c.post(f"{BASE}/responses", headers=CALLER, content=json.dumps(body_of(history(1))))
    assert (r.status_code, r.content, r.headers["retry-after"]) == (429, detail, "17")
    assert r.headers["x-codex-primary-used-percent"] == "100"
    up.reply = (401, {"content-type": "application/json"}, b'{"detail":"token expired"}')
    r = c.post(f"{BASE}/responses", headers=CALLER, content=json.dumps(body_of(history(1))))
    assert (r.status_code, r.json()) == (401, {"detail": "token expired"})
    assert len(app.state.usage_ledger.entries) == n  # failed requests aren't usage


def test_compressed_answers_and_other_endpoints(sub):
    c, up, _ = sub
    payload = gzip.compress(b'{"models":[{"slug":"gpt-test"}]}')
    up.reply = (200, {"content-encoding": "gzip", "content-type": "application/json", "etag": '"v1"'}, payload)
    r = c.get(f"{BASE}/models?client_version=1.2.3", headers={**CALLER, "accept-encoding": "gzip"})
    assert r.status_code == 200 and r.json() == {"models": [{"slug": "gpt-test"}]} and r.headers["etag"] == '"v1"'
    assert str(up.last.url) == "https://upstream.test/backend-api/codex/models?client_version=1.2.3"
    assert up.last.headers["accept-encoding"] == "gzip" and up.last.headers["authorization"] == f"Bearer {ACCESS}"
    up.reply = None
    raw = json.dumps(body_of(history(30))).encode()
    c.post(f"{BASE}/responses/compact", headers=CALLER, content=raw)  # Codex's own compaction: untouched
    assert up.last.content == raw and up.last.url.path == "/backend-api/codex/responses/compact"


def test_key_is_required(sub):
    c, up, _ = sub
    n = len(up.requests)
    for path in ("/codex-sub/wrong/backend-api/codex/responses", f"/codex-sub/{TOKEN}/backend-api/codex/responses"):
        r = c.post(path, headers=CALLER, content=b"{}")
        assert r.status_code == 401
    assert c.get(f"/codex-sub/{KEY}/v1/models", headers=CALLER).status_code == 404  # only backend-api is forwarded
    assert len(up.requests) == n  # nothing went out
    assert c.get(f"/codex-sub/{KEY}/_health").json() == {"ok": True, "upstream": "https://upstream.test", "prune": True}
    assert c.get("/codex-sub/nope/_health").status_code == 401
    assert c.get("/health").status_code == 401  # the router's own protection is unchanged


def test_upstream_down(sub):
    c, up, app = sub

    def down(req):
        raise httpx.ConnectError("no route")

    app.state.codex_sub_client._transport = httpx.MockTransport(down)
    r = c.post(f"{BASE}/responses", headers=CALLER, content=b"{}")
    assert r.status_code == 502 and "couldn't reach" in r.json()["error"]["message"]


def test_nothing_secret_is_logged_or_stored(sub, capsys, caplog, tmp_path):
    c, up, app = sub
    caplog.set_level(logging.DEBUG)
    c.post(f"{BASE}/responses", headers=CALLER, content=json.dumps(body_of(history(30))))
    up.reply = (500, {"content-type": "text/plain"}, b"upstream broke")
    c.post(f"{BASE}/responses", headers=CALLER, content=json.dumps(body_of(history(2))))
    c.post(f"{BASE}/responses", headers={**CALLER, "content-encoding": "br"}, content=b"???")
    out = capsys.readouterr()
    text = out.out + out.err + caplog.text + (tmp_path / "usage-ledger.jsonl").read_text()
    assert "codex-sub:" in text  # it did log something about the requests
    for secret in (ACCESS, ACCOUNT, TOKEN, "SECRET"):
        assert secret not in text


# ---- a real server: sockets, the websocket answer, the access log ---------------------------------------------------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(sub):
    import uvicorn

    _, up, app = sub
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info", lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", up
    server.should_exit = True
    t.join(5)


def test_live_stream_websocket_and_access_log(live, caplog):
    url, up = live
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    with httpx.Client() as h:
        with h.stream("POST", f"{url}{BASE}/responses", headers=CALLER, content=json.dumps(body_of(history(3)))) as r:
            got = b"".join(r.iter_raw())
        assert r.status_code == 200 and got == up.sse()
        ws = h.get(
            f"{url}{BASE}/responses",
            headers={
                **CALLER,
                "connection": "Upgrade",
                "upgrade": "websocket",
                "sec-websocket-version": "13",
                "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
            },
        )
        assert ws.status_code == 426  # the binary falls back to HTTP for the session
    # a bare client (no Accept-Encoding, no User-Agent): the router adds none of its own on the way up
    import http.client

    conn = http.client.HTTPConnection(url.removeprefix("http://"))
    conn.putrequest("GET", f"{BASE}/models", skip_accept_encoding=True)
    conn.putheader("authorization", f"Bearer {ACCESS}")
    conn.endheaders()
    assert conn.getresponse().status == 200
    conn.close()
    assert "accept-encoding" not in up.last.headers and "user-agent" not in up.last.headers
    assert up.last.headers["authorization"] == f"Bearer {ACCESS}"
    time.sleep(0.2)
    lines = "\n".join(r.getMessage() for r in caplog.records if r.name == "uvicorn.access")
    assert "/codex-sub/***/backend-api/codex/responses" in lines and KEY not in lines
