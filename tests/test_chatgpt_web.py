"""chatgpt_web provider: the text protocol (prompt out, tool calls back), the router wiring (opt-in, failover,
cooldowns, daily cap) and the page driver against a local mock page (tests/mock_chatgpt.py) in headless Chrome."""
import asyncio
import json

import httpx
import os

import pytest
import yaml
from fastapi.testclient import TestClient

from provenance_core import chatgpt_web as cw
from provenance_core.adapters import UpstreamStream
from provenance_core.catalog import load
from provenance_core.router import NoCapacity, Router
from provenance_core.server import create_app
from provenance_core.state import State

from mock_chatgpt import Mock, code, tool_call

TOOLS = [{"type": "function", "function": {"name": "shell", "description": "Run a command.", "parameters": {
            "type": "object", "properties": {"command": {"type": "array", "items": {"type": "string"}}},
            "required": ["command"]}}},
         {"type": "function", "function": {"name": "apply_patch", "description": "Apply a patch.", "parameters": {
             "type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}}}]
SCHEMAS = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}


def run(c):
    return asyncio.run(c)


# ---- protocol ---------------------------------------------------------------------------------------------------------
def test_prompt_holds_the_whole_request():
    body = {"messages": [
        {"role": "system", "content": "You are Provenance."},
        {"role": "user", "content": "list the files"},
        {"role": "assistant", "content": "Looking.", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": '{"command": ["ls"]}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "a.py\nb.py"},
        {"role": "user", "content": [{"type": "text", "text": "and now?"}]}],
        "tools": TOOLS, "_single_tool_call": True}
    prompt, tools, rules = cw.render_prompt(body)
    assert set(tools) == {"shell", "apply_patch"} and rules == {"single": True, "must": False, "only": None}
    for piece in ("You are Provenance.", "## shell", '"required":["command"]', "<<<user>>>\nlist the files",
                  '```tool_call\n{"name": "shell", "arguments": {"command": ["ls"]}}\n```',
                  "<<<tool result: shell>>>\na.py\nb.py", "<<<user>>>\nand now?", "At most one tool_call block"):
        assert piece in prompt
    # the model's own earlier call, rendered in the protocol, parses back
    text, calls = cw.parse_reply(prompt[prompt.index("<<<assistant>>>"):prompt.index("<<<tool result")], tools)
    assert calls[0]["name"] == "shell" and json.loads(calls[0]["arguments"]) == {"command": ["ls"]}


def test_prompt_tool_choice_and_images():
    _, tools, rules = cw.render_prompt({"messages": [{"role": "user", "content": "x"}], "tools": TOOLS, "tool_choice": "none"})
    assert tools == {}
    p, _, rules = cw.render_prompt({"messages": [{"role": "user", "content": "x"}], "tools": TOOLS,
                                    "tool_choice": {"type": "function", "function": {"name": "shell"}}})
    assert rules["must"] and rules["only"] == "shell" and "must call the tool `shell`" in p
    with pytest.raises(cw.WebSkip):
        cw.render_prompt({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:..."}}]}]})


def test_parse_tool_calls():
    md = 'I will look.\n\n```tool_call\n{"name": "shell", "arguments": {"command": ["ls", "-la"]}}\n```'
    text, calls = cw.parse_reply(md, SCHEMAS)
    assert text == "I will look." and calls[0]["name"] == "shell" and calls[0]["id"].startswith("call_")
    assert json.loads(calls[0]["arguments"]) == {"command": ["ls", "-la"]}
    # two calls; an untagged / json-tagged block that is a call; a patch whose text contains code fences
    patch = "*** Begin Patch\n*** Add File: a.md\n+```py\n+x = 1\n+```\n*** End Patch"
    md = ("```json\n" + json.dumps({"name": "shell", "arguments": {"command": ["pwd"]}}) + "\n```\n\n"
          "```tool_call\n" + json.dumps({"name": "apply_patch", "arguments": {"input": patch}}) + "\n```")
    text, calls = cw.parse_reply(md, SCHEMAS)
    assert text == "" and [c["name"] for c in calls] == ["shell", "apply_patch"]
    assert json.loads(calls[1]["arguments"])["input"] == patch
    assert len(cw.parse_reply(md, SCHEMAS, {"single": True})[1]) == 1
    # raw newlines inside a JSON string, arguments as a string, a raw string for a one-string tool
    md = '```tool_call\n{"name": "apply_patch", "arguments": {"input": "line1\nline2"}}\n```'
    assert json.loads(cw.parse_reply(md, SCHEMAS)[1][0]["arguments"])["input"] == "line1\nline2"
    md = '```tool_call\n{"name": "apply_patch", "arguments": "just text"}\n```'
    assert json.loads(cw.parse_reply(md, SCHEMAS)[1][0]["arguments"]) == {"input": "just text"}
    # a call written without its fence
    md = 'Running it.\n\ntool_call\n{"name": "shell", "arguments": {"command": ["ls"]}}'
    text, calls = cw.parse_reply(md, SCHEMAS)
    assert text == "Running it." and calls[0]["name"] == "shell"


def test_parse_drops_what_the_model_wrote_past_its_turn():
    md = ('Patching.\n\n```tool_call\n{"name": "shell", "arguments": {"command": ["ls"]}}\n```\n\n'
          '<<<tool result: shell>>>\na.py\n\nDone, the file is fixed.')
    text, calls = cw.parse_reply(md, SCHEMAS)
    assert text == "Patching." and len(calls) == 1
    md = 'Note.\n\n```tool_call\n{"name": "shell", "arguments": {"command": ["ls"]}}\n```\n\nThe output will be a.py.'
    assert cw.parse_reply(md, SCHEMAS)[0] == "Note."
    assert cw.parse_reply("<<<assistant>>>\nAll good.\n\n<<<user>>>\nthanks", SCHEMAS) == ("All good.", [])


def test_parse_leaves_ordinary_replies_alone():
    md = 'Here is a config:\n\n```json\n{"name": "demo", "version": 1}\n```\n\nand code:\n\n```python\nprint("hi")\n```'
    assert cw.parse_reply(md, SCHEMAS) == (md, [])
    assert cw.parse_reply("```tool_call\nnot even json\n```", {}) == ("```tool_call\nnot even json\n```", [])


@pytest.mark.parametrize("md,why", [
    ('```tool_call\n{"name": "shell", "arguments": {"command": ["ls",]}}\n```', "not valid JSON"),
    ('```tool_call\n{"name": "rm_rf", "arguments": {}}\n```', "unknown tool"),
    ('```tool_call\n{"name": "shell", "arguments": {"cmd": "ls"}}\n```', "missing required"),
    ('```tool_call\n{"name": "shell", "arguments": [1, 2]}\n```', "must be a JSON object"),
    ('```tool_call\n{"command": ["ls"]}\n```', '{"name"'),
    ('tool_call\n{"name": "shell", "arguments": {"command": [“ls”]}}', "fenced block"),
])
def test_parse_malformed(md, why):
    with pytest.raises(cw.Malformed, match=why):
        cw.parse_reply(md, SCHEMAS)


def test_parse_required_call():
    with pytest.raises(cw.Malformed, match="must contain a tool_call"):
        cw.parse_reply("Sure, done.", SCHEMAS, {"must": True})
    with pytest.raises(cw.Malformed, match="must call the tool shell"):
        cw.parse_reply('```tool_call\n{"name": "apply_patch", "arguments": {"input": "x"}}\n```', SCHEMAS,
                       {"must": True, "only": "shell"})


def test_buffered_stream_reads_like_an_upstream():
    calls = [{"id": "call_1", "name": "shell", "arguments": '{"command": ["ls"]}'}]
    data = cw.chat_completion("chatgpt-web/luna", "Looking.", calls, "prompt")
    assert data["choices"][0]["finish_reason"] == "tool_calls"

    async def go():
        up = UpstreamStream(cw.BufferedStream(data))
        return [e async for e in up.events()], up
    events, up = run(go())
    assert events == [("text", "Looking.")] and up.tool_calls == calls and up.finish_reason == "tool_calls"


# ---- router wiring (a fake bridge, no browser) ------------------------------------------------------------------------
CAT = {
    "tiers": [{"name": "nova", "min_score": 90}, {"name": "solstice", "min_score": 0}],
    "providers": {"chatgpt-web": {"type": "chatgpt_web", "base_url": "https://chatgpt.test", "api_key_env": "none",
                                  "web": {"endpoints": ["responses"]}},
                  "a": {"base_url": "https://a.test/v1", "api_key_env": "A_KEY"}},
    "models": [{"provider": "chatgpt-web", "model": "luna", "score": 96, "context": 64000},
               {"provider": "a", "model": "big", "score": 92}, {"provider": "a", "model": "mid", "score": 70}],
}


class FakeBridge:
    def __init__(self):
        self.calls, self.fail = [], None

    async def complete(self, model, body, model_id=None, wait=None):
        self.calls.append(body)
        if self.fail:
            raise self.fail
        prompt, tools, rules = cw.render_prompt(body)
        if tools:
            text, calls = cw.parse_reply('```tool_call\n{"name": "shell", "arguments": {"command": ["ls"]}}\n```', tools, rules)
            return cw.chat_completion(model_id, text, calls, prompt)
        return cw.chat_completion(model_id, "from the web", [], prompt)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("A_KEY", "x")
    monkeypatch.setenv("PCORE_CHATGPT_WEB", "1")
    monkeypatch.setenv("PCORE_HOME", str(tmp_path))
    p = tmp_path / "catalog.yaml"
    p.write_text(yaml.safe_dump(CAT))
    seen = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": f"from {body['model']}"}}],
                                         "usage": {"total_tokens": 10}})
    router = Router(load(p), State(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    fake = FakeBridge()
    router.bridges["chatgpt-web"] = fake
    return router, fake, seen


USER = {"messages": [{"role": "user", "content": "hi"}]}


def test_off_unless_enabled(wired, monkeypatch):
    router, fake, _ = wired
    web = router.catalog.models["chatgpt-web/luna"]
    assert router.catalog.providers["chatgpt-web"].usable and router.catalog.tier_of(web) == "nova"
    monkeypatch.delenv("PCORE_CHATGPT_WEB")
    assert not router.catalog.providers["chatgpt-web"].usable
    assert [m.id for m in router.candidates("nova", 100, web=True)] == ["a/big"]
    monkeypatch.setenv("PCORE_CHATGPT_WEB", "0")
    assert not cw.enabled({"enabled": True}) and not fake.calls


def test_only_used_when_the_front_end_allows_it(wired):
    router, fake, _ = wired
    assert router.web_ok("responses") and not router.web_ok("chat") and not router.web_ok("messages")
    assert [m.id for m in router.candidates("nova", 100)] == ["a/big"]                     # default: not offered
    assert [m.id for m in router.candidates("nova", 100, web=True)] == ["chatgpt-web/luna", "a/big"]
    _, m = run(router.complete(dict(USER), "nova"))
    assert m.id == "a/big" and not fake.calls
    run(router.ensemble(dict(USER), "nova", k=2, strategy="first"))                        # ensembles never use it
    assert not fake.calls
    data, m = run(router.complete(dict(USER), "nova", web=True))
    assert m.id == "chatgpt-web/luna" and data["choices"][0]["message"]["content"] == "from the web"


def test_unavailable_fails_over_and_cools_down(wired):
    router, fake, seen = wired
    fake.fail = cw.WebUnavailable("not signed in to ChatGPT", 1800, "login")
    data, m = run(router.complete(dict(USER), "nova", web=True))
    assert m.id == "a/big" and len(fake.calls) == 1
    run(router.complete(dict(USER), "nova", web=True))        # cooling down: the page isn't touched again
    assert len(fake.calls) == 1 and not router.state.available(router.catalog.models["chatgpt-web/luna"])
    # a skipped request (busy window, too long) costs the provider nothing
    router.state.health.clear()
    fake.fail = cw.WebSkip("another request is using the ChatGPT window")
    _, m = run(router.complete(dict(USER), "nova", web=True))
    assert m.id == "a/big" and router.state.available(router.catalog.models["chatgpt-web/luna"])
    # nothing else in the tier and no fallback: a loud 503-style error, not a silent downgrade
    fake.fail = cw.WebUnavailable("challenge", 60, "challenge")
    router.state.failure(router.catalog.models["a/big"], rate_limited=True)
    with pytest.raises(NoCapacity, match="challenge"):
        run(router.complete(dict(USER), "nova", web=True))


def test_private_hints_never_reach_http_providers(wired):
    router, fake, seen = wired
    run(router.complete({**USER, "_single_tool_call": True}, "nova"))
    assert "_single_tool_call" not in seen[-1]


def test_responses_api_tool_call_through_the_bridge(wired):
    router, fake, seen = wired
    c = TestClient(create_app(router=router))
    req = {"model": "nova", "input": "list files", "parallel_tool_calls": False,
           "tools": [{"type": "function", "name": "shell", "description": "Run", "parameters": SCHEMAS["shell"]}]}
    r = c.post("/v1/responses", json=req)
    assert r.status_code == 200 and r.headers["x-pcore-model"] == "chatgpt-web/luna"
    item = r.json()["output"][0]
    assert item["type"] == "function_call" and item["name"] == "shell" and json.loads(item["arguments"]) == {"command": ["ls"]}
    assert fake.calls[-1]["_single_tool_call"] is True
    # the tool result goes back in; streaming carries the same items
    req2 = {**req, "stream": True, "input": [
        {"role": "user", "content": "list files"}, item,
        {"type": "function_call_output", "call_id": item["call_id"], "output": "a.py"}]}
    with c.stream("POST", "/v1/responses", json=req2) as s:
        events = [json.loads(line[5:]) for line in s.iter_lines() if line.startswith("data:")]
    done = events[-1]
    assert done["type"] == "response.completed" and done["response"]["output"][0]["type"] == "function_call"
    assert "<<<tool result: shell>>>\na.py" in cw.render_prompt(fake.calls[-1])[0]
    # other front ends stay on the normal pool; naming the model is asking for it
    r = c.post("/v1/chat/completions", json={"model": "nova", **USER})
    assert r.headers["x-pcore-model"] == "a/big"
    r = c.post("/v1/chat/completions", json={"model": "chatgpt-web/luna", **USER})
    assert r.headers["x-pcore-model"] == "chatgpt-web/luna"
    # failover on the streaming path too
    fake.fail = cw.WebUnavailable("the ChatGPT window was closed", 600, "closed")
    r = c.post("/v1/responses", json={"model": "nova", "input": "hi"})
    assert r.status_code == 200 and r.headers["x-pcore-model"] == "a/big"


def test_status_endpoint_and_calibration_skip(wired, tmp_path):
    router, fake, _ = wired
    router.bridges["chatgpt-web"] = cw.Bridge("https://chatgpt.test", {"notify": False}, home=tmp_path)
    c = TestClient(create_app(router=router))
    st = c.get("/chatgpt-web/status").json()
    assert st["enabled"] and st["signed_in"] is None and st["today"] == 0 and st["models"][0]["tier"] == "nova"
    from provenance_core.calibrate import calibrate
    router.bridges["chatgpt-web"] = fake
    scores = run(calibrate(router, tmp_path / "scores.yaml"))
    assert "chatgpt-web/luna" not in scores and "a/big" in scores and not fake.calls


def test_daily_cap_blocks_and_persists(tmp_path):
    now = [1_800_000_000.0]
    b = cw.Bridge("https://chatgpt.test", {"daily_cap": 2, "notify": False}, home=tmp_path, clock=lambda: now[0])
    b.save(count=2)
    with pytest.raises(cw.WebUnavailable) as e:
        run(b.complete("luna", dict(USER)))
    assert e.value.kind == "cap" and 0 < e.value.cooldown <= 86400
    now[0] += 86400                                   # a new day: the count starts over
    assert b.status()["today"] == 0
    b._block(cw.WebUnavailable("not signed in", 1800, "login"))
    b2 = cw.Bridge("https://chatgpt.test", {"notify": False}, home=tmp_path, clock=lambda: now[0])   # survives a restart
    st = b2.status()
    assert st["blocked"] and st["kind"] == "login" and st["signed_in"] is False
    with pytest.raises(cw.WebUnavailable, match="not signed in"):
        run(b2.complete("luna", dict(USER)))
    b2.reset()
    assert not b2.status()["blocked"]
    with pytest.raises(cw.WebSkip, match="over the bridge"):
        run(cw.Bridge("https://chatgpt.test", {"max_prompt_chars": 50, "notify": False}, home=tmp_path).complete("luna", dict(USER)))


# ---- the page driver, against the mock page in headless Chrome --------------------------------------------------------
def _chrome_ok():
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    import os
    return os.path.exists(cw.CHROME)


browser = pytest.mark.skipif(not _chrome_ok(), reason="needs playwright and Google Chrome")


@pytest.fixture
def mock():
    m = Mock()
    yield m
    m.close()


def bridge_for(mock, tmp_path, **opts):
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
    b = cw.Bridge(mock.url, {"driver": os.environ.get("PCORE_TEST_DRIVER", "playwright"), "headless": True, "notify": False, "min_gap_s": 3, "max_gap_s": 3, "challenge_wait_s": 1,
                             "profile": str(tmp_path / "profile"), **opts}, home=tmp_path, sleep=sleep)
    b.sleeps = sleeps
    return b


def scenario(mock, tmp_path, fn, **opts):
    async def go():
        b = bridge_for(mock, tmp_path, **opts)
        try:
            return await fn(b)
        finally:
            await b.release()
    return run(go())


@browser
def test_page_plain_reply_and_markdown(mock, tmp_path):
    mock.replies = ["<p>The answer is <strong>42</strong>, see <code>f(x)</code>.</p>"
                    "<ol><li>first</li><li>second<ul><li>nested</li></ul></li></ol>"
                    + code("python", "def f(x):\n    return x * 2  # ``` not a fence")
                    + "<h3>Done</h3><table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"]

    async def go(b):
        d1 = await b.complete("luna", {"messages": [{"role": "system", "content": "Be brief."},
                                                     {"role": "user", "content": "line one\n\n    indented <b>&amp;\nline three"}]})
        d2 = await b.complete("luna", dict(USER))
        return d1, d2, b.status()
    d1, d2, st = scenario(mock, tmp_path, go)
    text = d1["choices"][0]["message"]["content"]
    assert text == ("The answer is **42**, see `f(x)`.\n\n1. first\n2. second\n   - nested\n\n"
                    "````python\ndef f(x):\n    return x * 2  # ``` not a fence\n````\n\n### Done\n\n| a | b |\n| --- | --- |\n| 1 | 2 |")
    assert d1["choices"][0]["finish_reason"] == "stop" and d1["pcore"]["web"]["model_slug"] == "gpt-6-luna"
    # each request: its own page load, in temporary chat, holding the whole prompt as typed
    assert len(mock.sent) == 2 and all(s["temp"] for s in mock.sent)
    assert "Be brief." in mock.sent[0]["prompt"] and "line one\n\n    indented <b>&amp;\nline three" in mock.sent[0]["prompt"]
    assert "line one" not in mock.sent[1]["prompt"]        # nothing carried over
    assert st["today"] == 2 and st["signed_in"] is True and st["model_seen"] == "gpt-6-luna"


@browser
def test_page_tool_call_and_repair(mock, tmp_path):
    good = "<p>Listing.</p>" + tool_call("shell", {"command": ["ls", "-la"]})
    mock.replies = [good, "<p>Sorry.</p>" + code("tool_call", '{"name": "shell", "arguments": {"command": ["ls",]}}'), good]

    async def go(b):
        body = {"messages": [{"role": "user", "content": "list"}], "tools": TOOLS}
        return await b.complete("luna", body), await b.complete("luna", body)
    d1, d2 = scenario(mock, tmp_path, go)
    for d in (d1, d2):
        msg = d["choices"][0]["message"]
        assert msg["content"] == "Listing." and d["choices"][0]["finish_reason"] == "tool_calls"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"command": ["ls", "-la"]}
    assert not d1["pcore"]["web"]["repaired"] and d2["pcore"]["web"]["repaired"]
    assert len(mock.sent) == 3 and "not valid JSON" in mock.sent[2]["prompt"] and "```tool_call" in mock.sent[0]["prompt"]


@browser
def test_page_gives_up_after_one_repair(mock, tmp_path):
    mock.replies = [code("tool_call", '{"name": "nope", "arguments": {}}')]

    async def go(b):
        with pytest.raises(cw.WebError, match="after one repair"):
            await b.complete("luna", {"messages": [{"role": "user", "content": "x"}], "tools": TOOLS})
        return b.status()
    st = scenario(mock, tmp_path, go)
    assert len(mock.sent) == 2 and not st["blocked"]


@browser
@pytest.mark.parametrize("trouble,kind", [({"login": False}, "login"), ({"challenge": True}, "challenge"),
                                          ({"temp": False}, "tempchat"), ({"limit": True}, "limit")])
def test_page_trouble_means_unavailable_and_nothing_sent(mock, tmp_path, trouble, kind):
    mock.state.update(trouble)

    async def go(b):
        with pytest.raises(cw.WebUnavailable) as e:
            await b.complete("luna", dict(USER))
        with pytest.raises(cw.WebUnavailable):      # blocked now: no second visit
            await b.complete("luna", dict(USER))
        return e.value, b.status(), await b.check("luna")
    err, st, chk = scenario(mock, tmp_path, go)
    assert err.kind == kind and st["blocked"] and st["kind"] == kind and mock.sent == [] and st["today"] == 0
    assert chk["ok"] is False and chk["kind"] == kind


@browser
def test_page_temp_toggle_model_picker_and_gap(mock, tmp_path):
    mock.state.update(temp=False, temp_toggle=True)

    async def go(b):
        d = await b.complete("Nimbus", dict(USER))
        await b.complete("auto", dict(USER))
        with pytest.raises(cw.WebUnavailable) as e:
            await b.complete("Orion", dict(USER))
        return d, e.value, b.sleeps
    d, err, sleeps = scenario(mock, tmp_path, go)
    assert [s["model"] for s in mock.sent] == ["Nimbus", "Luna"] and all(s["temp"] for s in mock.sent)
    assert d["pcore"]["web"]["model_slug"] == "gpt-6-nimbus" and err.kind == "model"
    assert len(sleeps) == 2 and all(2 < s <= 3 for s in sleeps)      # a pause before the 2nd and 3rd request


@browser
def test_page_error_reply_and_closed_window(mock, tmp_path):
    mock.replies = [{"error": "Something went wrong while generating the response."}, "<p>fine</p>"]

    async def go(b):
        with pytest.raises(cw.WebError, match="Something went wrong"):
            await b.complete("luna", dict(USER))
        ok = await b.complete("luna", dict(USER))
        await b._page.close()                        # the user closes the window
        with pytest.raises(cw.WebUnavailable) as e:
            await b.complete("luna", dict(USER))
        return ok, e.value, b.status()
    ok, err, st = scenario(mock, tmp_path, go)
    assert ok["choices"][0]["message"]["content"] == "fine"
    assert err.kind == "closed" and st["blocked"] and st["kind"] == "closed"


@browser
def test_page_end_to_end_through_the_responses_api(mock, tmp_path, monkeypatch):
    monkeypatch.setenv("A_KEY", "x")
    monkeypatch.setenv("PCORE_CHATGPT_WEB", "1")
    cat = json.loads(json.dumps(CAT))
    cat["providers"]["chatgpt-web"]["base_url"] = mock.url
    (tmp_path / "catalog.yaml").write_text(yaml.safe_dump(cat))
    router = Router(load(tmp_path / "catalog.yaml"), State(), httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "from the pool"}}]}))))
    router.bridges["chatgpt-web"] = bridge_for(mock, tmp_path, min_gap_s=0, max_gap_s=0)
    mock.replies = [tool_call("shell", {"command": ["ls"]}), "<p>Two files: a.py and b.py.</p>"]
    tools = [{"type": "function", "name": "shell", "description": "Run", "parameters": SCHEMAS["shell"]}]
    with TestClient(create_app(router=router)) as c:
        r = c.post("/v1/responses", json={"model": "nova", "input": "list files", "tools": tools})
        call = r.json()["output"][0]
        assert r.headers["x-pcore-model"] == "chatgpt-web/luna" and call["type"] == "function_call"
        r = c.post("/v1/responses", json={"model": "nova", "tools": tools, "previous_response_id": r.json()["id"],
                                          "input": [{"type": "function_call_output", "call_id": call["call_id"], "output": "a.py\nb.py"}]})
        assert r.json()["output"][0]["content"][0]["text"] == "Two files: a.py and b.py."
        assert "<<<tool result: shell>>>\na.py\nb.py" in mock.sent[1]["prompt"]
        assert c.get("/chatgpt-web/status").json()["today"] == 2
        mock.state["login"] = False                   # the login expires: the request is served by the pool
        r = c.post("/v1/responses", json={"model": "nova", "input": "hi"})
        assert r.headers["x-pcore-model"] == "a/big" and r.json()["output"][0]["content"][0]["text"] == "from the pool"
        st = c.get("/chatgpt-web/status").json()
        assert st["blocked"] and st["kind"] == "login" and st["models"][0]["available"] is False
        mock.state["login"] = True                    # signed in again: a live check clears the block
        st = c.get("/chatgpt-web/status?probe=1").json()
        assert st["ok"] and not st["blocked"] and st["models"][0]["available"]
        c.post("/chatgpt-web/release")


@browser
def test_reads_the_2026_11_markup():
    """chatgpt.com's markup as captured on 2026-11-21 (no data-message-author-role; an editor widget for code)."""
    from pathlib import Path

    from playwright.async_api import async_playwright
    html = (Path(__file__).parent / "real_page_2026_11.html").read_text()

    async def go():
        async with async_playwright() as pw:
            br = await pw.chromium.launch(channel="chrome", headless=True)
            page = await br.new_page()
            await page.set_content(html)
            out = await page.evaluate(cw.PROBE_JS, False), await page.evaluate(cw.READ_JS)
            await br.close()
            return out
    p, r = run(go())
    assert (p["users"], p["assistants"], p["actions"], p["stop"]) == (1, 1, 1, False) and p["lastLen"] > 0
    md = r["md"]
    assert md.startswith("## **Example Formatting**")
    assert "**bold text**, inline code `print(\"Hello\")`, and a [link to example.com](https://example.com)." in md
    assert "- Nested item" in md and "1. First step\n2. Second step" in md and "> This is a blockquote." in md
    assert "| Column 1 | Column 2 |\n| --- | --- |\n| A | B |\n| C | D |" in md
    assert '```python\ndef f(x):\n    # <tag> & "q"\n    return x**2\n```' in md
    assert "$x^2$" in md and "\n\n---\n\n" in md
    text, calls = cw.parse_reply(md, {"read_file": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}})
    assert [(c["name"], json.loads(c["arguments"])) for c in calls] == [("read_file", {"path": "/etc/hosts"})]
    assert "Python\n" not in md and "Run" not in md and "tool_call" not in text
