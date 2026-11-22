"""chatgpt_web provider: the text protocol (prompt out, tool calls back), the router wiring (opt-in, failover,
cooldowns, daily cap) and the page driver against a local mock page (tests/mock_chatgpt.py) in headless Chrome."""

import asyncio
import json
import os
import time

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from mock_chatgpt import Mock, code, tool_call

from provenance_core import chatgpt_web as cw
from provenance_core.adapters import UpstreamStream
from provenance_core.catalog import load
from provenance_core.router import NoCapacity, Router
from provenance_core.server import create_app
from provenance_core.state import State

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Run a command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "array", "items": {"type": "string"}}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a patch.",
            "parameters": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        },
    },
]
SCHEMAS = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}


def run(c):
    return asyncio.run(c)


# ---- protocol ---------------------------------------------------------------------------------------------------------
def test_prompt_holds_the_whole_request():
    body = {
        "messages": [
            {"role": "system", "content": "You are Provenance."},
            {"role": "user", "content": "list the files"},
            {
                "role": "assistant",
                "content": "Looking.",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": '{"command": ["ls"]}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "a.py\nb.py"},
            {"role": "user", "content": [{"type": "text", "text": "and now?"}]},
        ],
        "tools": TOOLS,
        "_single_tool_call": True,
    }
    prompt, tools, rules = cw.render_prompt(body)
    assert set(tools) == {"shell", "apply_patch"} and rules == {"single": True, "must": False, "only": None}
    for piece in (
        "You are Provenance.",
        "type shell = (_: {\n  command: string[],\n}) => any;",
        "<<<user>>>\nlist the files",
        '```tool_call\n{"name": "shell", "arguments": {"command": ["ls"]}}\n```',
        "<<<tool result: shell>>>\na.py\nb.py",
        "<<<user>>>\nand now?",
        "At most one tool block",
        "not on my computer",
        "Shorthand for `apply_patch` (body = `input`)",
    ):
        assert piece in prompt
    assert prompt.rstrip().endswith("answer without a block.")  # the rules again, next to the reply
    # the model's own earlier call, rendered in the protocol, parses back
    text, calls = cw.parse_reply(prompt[prompt.index("<<<assistant>>>\n") : prompt.index("<<<tool result:")], tools)
    assert calls[0]["name"] == "shell" and json.loads(calls[0]["arguments"]) == {"command": ["ls"]}


def test_tool_documentation_is_bounded_without_losing_call_shape():
    verbose = json.loads(json.dumps(TOOLS[0]))
    verbose["function"]["description"] = "Run a command. " + "background detail " * 2000
    verbose["function"]["parameters"]["properties"]["command"]["description"] = "argv " * 2000
    prompt, _, _ = cw.render_prompt({"messages": [{"role": "user", "content": "inspect"}], "tools": [verbose]})
    assert len(prompt) < 3000
    assert "type shell" in prompt and "command: string[]" in prompt and "Run a command." in prompt


def test_long_initial_transcript_keeps_task_and_recent_evidence():
    messages = [{"role": "user", "content": "Find the regression in module alpha."}]
    for i in range(12):
        messages += [
            {"role": "assistant", "content": f"Checking step {i}."},
            {"role": "tool", "content": ("old output " * 1000) if i < 8 else f"result {i}"},
        ]
    prompt, _, _, compacted = cw.compact_initial_prompt({"messages": messages, "tools": TOOLS}, 9000)
    assert compacted and len(prompt) <= 9000
    assert "Find the regression in module alpha." in prompt
    assert "result 11" in prompt
    assert "old output " * 100 not in prompt


def test_compaction_bounds_recent_large_tool_output():
    body = {
        "messages": [
            {"role": "user", "content": "Find the bug."},
            {"role": "assistant", "content": "Reading."},
            {"role": "tool", "content": "A" * 100000 + " END_MARKER"},
            {"role": "user", "content": "Use the result and finish."},
        ]
    }
    prompt, _, _, compacted = cw.compact_initial_prompt(body, 20000)
    assert compacted and len(prompt) <= 20000
    assert "Find the bug." in prompt and "END_MARKER" in prompt and "Use the result and finish." in prompt


def test_prompt_tool_choice_and_images():
    _, tools, rules = cw.render_prompt(
        {"messages": [{"role": "user", "content": "x"}], "tools": TOOLS, "tool_choice": "none"}
    )
    assert tools == {}
    p, _, rules = cw.render_prompt(
        {
            "messages": [{"role": "user", "content": "x"}],
            "tools": TOOLS,
            "tool_choice": {"type": "function", "function": {"name": "shell"}},
        }
    )
    assert rules["must"] and rules["only"] == "shell" and "must call the tool `shell`" in p
    with pytest.raises(cw.WebSkip):
        cw.render_prompt(
            {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:..."}}]}]}
        )


EXEC = {
    "type": "function",
    "function": {
        "name": "exec_command",
        "description": "Runs a command in a PTY.\nReturns output.",
        "parameters": {
            "type": "object",
            "required": ["cmd"],
            "properties": {
                "cmd": {"type": "string", "description": "Shell command to execute."},
                "workdir": {"type": "string"},
                "yield_time_ms": {"type": "number"},
                "sandbox_permissions": {"type": "string", "enum": ["use_default", "require_escalated"]},
                "prefix_rule": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}
STDIN = {
    "type": "function",
    "function": {
        "name": "write_stdin",
        "description": "",
        "parameters": {
            "type": "object",
            "required": ["session_id"],
            "properties": {"session_id": {"type": "number"}, "chars": {"type": "string"}},
        },
    },
}


def test_prompt_for_a_codex_request():
    """What Provenance / Codex sends: a prompt that speaks of function calls and an apply_patch tool, and exec_command."""
    patch = 'apply_patch <<\'EOF\'\n*** Begin Patch\n*** Update File: a.py\n@@\n-x = "1"\n+x = "2"\n*** End Patch\nEOF'
    body = {
        "messages": [
            {"role": "system", "content": "You are a coding agent. Use the `apply_patch` tool to edit files."},
            {"role": "user", "content": "fix it"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "exec_command", "arguments": json.dumps({"cmd": "ls"})},
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "exec_command", "arguments": json.dumps({"cmd": patch})},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "a.py"},
            {"role": "tool", "tool_call_id": "c2", "content": "Done"},
        ],
        "tools": [EXEC, STDIN],
    }
    prompt, tools, rules = cw.render_prompt(body)
    for piece in (
        "no separate apply_patch tool here: to edit a file, run `apply_patch` as a shell command through exec_command",
        "Shorthand for `exec_command` (body = `cmd`)",
        "// Runs a command in a PTY. Returns output.\ntype exec_command = (_: {\n  // Shell command to execute.\n  cmd: string,\n",
        '  sandbox_permissions?: "use_default" | "require_escalated",\n  prefix_rule?: string[],\n}) => any;',
        "type write_stdin = (_: {\n  session_id: number,\n  chars?: string,\n}) => any;",
        '```tool_call\n{"name": "exec_command", "arguments": {"cmd": "ls"}}\n```',
        "```exec_command\n" + patch + "\n```",  # its earlier multi-line command, shown unescaped
    ):
        assert piece in prompt
    assert "write_stdin` (body" not in prompt  # no free-text argument: no shorthand
    # both of its earlier calls parse back to what they were
    text, calls = cw.parse_reply(prompt[prompt.index("<<<assistant>>>\n") : prompt.index("<<<tool result:")], tools)
    assert [json.loads(c["arguments"]) for c in calls] == [{"cmd": "ls"}, {"cmd": patch}]


def test_ts_types():
    assert cw.ts_type({"type": ["string", "null"]}) == "string | null"
    assert (
        cw.ts_type({"anyOf": [{"type": "integer"}, {"type": "array", "items": {"enum": ["a", "b"]}}]})
        == 'number | ("a" | "b")[]'
    )
    assert cw.ts_type({"type": "object"}) == "{}" and cw.ts_type({}) == "any"
    assert (
        cw.ts_type({"type": "object", "properties": {"a-b": {"type": "boolean", "default": False}}})
        == '{\n  // default: false\n  "a-b"?: boolean,\n}'
    )


def test_parse_shorthand_blocks():
    tools = {t["function"]["name"]: t["function"]["parameters"] for t in (EXEC, STDIN)}
    md = (
        'Patching.\n\n```exec_command\ncat > a.txt <<\'EOF\'\n{"k": "v"}\n  two\nEOF\n```\n\n'
        '```tool_call\n{"name": "write_stdin", "arguments": {"session_id": 3}}\n```\nand then it printed ok'
    )
    text, calls = cw.parse_reply(md, tools)
    assert text == "Patching." and [c["name"] for c in calls] == ["exec_command", "write_stdin"]
    assert json.loads(calls[0]["arguments"]) == {"cmd": 'cat > a.txt <<\'EOF\'\n{"k": "v"}\n  two\nEOF'}
    # JSON inside the shorthand block is taken as arguments when it is exactly that
    for inner in (
        '{"cmd": "ls", "workdir": "/tmp"}',
        '{"name": "exec_command", "arguments": {"cmd": "ls", "workdir": "/tmp"}}',
    ):
        assert json.loads(cw.parse_reply(f"```exec_command\n{inner}\n```", tools)[1][0]["arguments"]) == {
            "cmd": "ls",
            "workdir": "/tmp",
        }
    assert json.loads(cw.parse_reply('```exec_command\n{"a": 1}\n```', tools)[1][0]["arguments"]) == {"cmd": '{"a": 1}'}
    assert cw.parse_reply("```write_stdin\n3\n```", tools) == ("```write_stdin\n3\n```", [])  # not a shorthand tool
    assert cw.parse_reply("```bash\nls\n```", tools)[1] == []  # an ordinary code block
    with pytest.raises(cw.Malformed, match="empty"):
        cw.parse_reply("```exec_command\n   \n```", tools)


def test_parse_tool_calls():
    md = 'I will look.\n\n```tool_call\n{"name": "shell", "arguments": {"command": ["ls", "-la"]}}\n```'
    text, calls = cw.parse_reply(md, SCHEMAS)
    assert text == "I will look." and calls[0]["name"] == "shell" and calls[0]["id"].startswith("call_")
    assert json.loads(calls[0]["arguments"]) == {"command": ["ls", "-la"]}
    # two calls; an untagged / json-tagged block that is a call; a patch whose text contains code fences
    patch = "*** Begin Patch\n*** Add File: a.md\n+```py\n+x = 1\n+```\n*** End Patch"
    md = (
        "```json\n" + json.dumps({"name": "shell", "arguments": {"command": ["pwd"]}}) + "\n```\n\n"
        "```tool_call\n" + json.dumps({"name": "apply_patch", "arguments": {"input": patch}}) + "\n```"
    )
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
    md = (
        'Patching.\n\n```tool_call\n{"name": "shell", "arguments": {"command": ["ls"]}}\n```\n\n'
        "<<<tool result: shell>>>\na.py\n\nDone, the file is fixed."
    )
    text, calls = cw.parse_reply(md, SCHEMAS)
    assert text == "Patching." and len(calls) == 1
    md = 'Note.\n\n```tool_call\n{"name": "shell", "arguments": {"command": ["ls"]}}\n```\n\nThe output will be a.py.'
    assert cw.parse_reply(md, SCHEMAS)[0] == "Note."
    assert cw.parse_reply("<<<assistant>>>\nAll good.\n\n<<<user>>>\nthanks", SCHEMAS) == ("All good.", [])


def test_parse_leaves_ordinary_replies_alone():
    md = 'Here is a config:\n\n```json\n{"name": "demo", "version": 1}\n```\n\nand code:\n\n```python\nprint("hi")\n```'
    assert cw.parse_reply(md, SCHEMAS) == (md, [])
    assert cw.parse_reply("```tool_call\nnot even json\n```", {}) == ("```tool_call\nnot even json\n```", [])


@pytest.mark.parametrize(
    "md,why",
    [
        ('```tool_call\n{"name": "shell", "arguments": {"command": ["ls",]}}\n```', "not valid JSON"),
        ('```tool_call\n{"name": "rm_rf", "arguments": {}}\n```', "unknown tool"),
        ('```tool_call\n{"name": "shell", "arguments": {"cmd": "ls"}}\n```', "missing required"),
        ('```tool_call\n{"name": "shell", "arguments": [1, 2]}\n```', "must be a JSON object"),
        ('```tool_call\n{"command": ["ls"]}\n```', '{"name"'),
        ('tool_call\n{"name": "shell", "arguments": {"command": [“ls”]}}', "fenced block"),
    ],
)
def test_parse_malformed(md, why):
    with pytest.raises(cw.Malformed, match=why):
        cw.parse_reply(md, SCHEMAS)


def test_parse_required_call():
    with pytest.raises(cw.Malformed, match="must contain a tool_call"):
        cw.parse_reply("Sure, done.", SCHEMAS, {"must": True})
    with pytest.raises(cw.Malformed, match="must call the tool shell"):
        cw.parse_reply(
            '```tool_call\n{"name": "apply_patch", "arguments": {"input": "x"}}\n```',
            SCHEMAS,
            {"must": True, "only": "shell"},
        )


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
    "providers": {
        "chatgpt-web": {
            "type": "chatgpt_web",
            "base_url": "https://chatgpt.test",
            "api_key_env": "none",
            "web": {"endpoints": ["responses"]},
        },
        "a": {"base_url": "https://a.test/v1", "api_key_env": "A_KEY"},
    },
    "models": [
        {"provider": "chatgpt-web", "model": "luna", "score": 96, "context": 64000},
        {"provider": "a", "model": "big", "score": 92},
        {"provider": "a", "model": "mid", "score": 70},
    ],
}


class FakeBridge:
    def __init__(self):
        self.calls, self.fail = [], None

    def capacity(self, accounts=None, model=None):
        return {"remaining": 1.0, "busy": 0, "accounts": 1, "available": True}

    async def complete(self, model, body, model_id=None, wait=None, on_text=None):
        self.calls.append(body)
        if self.fail:
            raise self.fail
        prompt, tools, rules = cw.render_prompt(body)
        if tools:
            text, calls = cw.parse_reply(
                '```tool_call\n{"name": "shell", "arguments": {"command": ["ls"]}}\n```', tools, rules
            )
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
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": f"from {body['model']}"}}],
                "usage": {"total_tokens": 10},
            },
        )

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
    assert [m.id for m in router.candidates("nova", 100)] == ["a/big"]  # default: not offered
    assert [m.id for m in router.candidates("nova", 100, web=True)] == ["chatgpt-web/luna", "a/big"]
    _, m = run(router.complete(dict(USER), "nova"))
    assert m.id == "a/big" and not fake.calls
    run(router.ensemble(dict(USER), "nova", k=2, strategy="first"))  # ensembles never use it
    assert not fake.calls
    data, m = run(router.complete(dict(USER), "nova", web=True))
    assert m.id == "chatgpt-web/luna" and data["choices"][0]["message"]["content"] == "from the web"


def test_unavailable_fails_over_and_cools_down(wired):
    router, fake, seen = wired
    fake.fail = cw.WebUnavailable("not signed in to ChatGPT", 1800, "login")
    data, m = run(router.complete(dict(USER), "nova", web=True))
    assert m.id == "a/big" and len(fake.calls) == 1
    run(router.complete(dict(USER), "nova", web=True))  # cooling down: the page isn't touched again
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
    req = {
        "model": "nova",
        "input": "list files",
        "parallel_tool_calls": False,
        "tools": [{"type": "function", "name": "shell", "description": "Run", "parameters": SCHEMAS["shell"]}],
    }
    r = c.post("/v1/responses", json=req)
    assert r.status_code == 200 and r.headers["x-pcore-model"] == "chatgpt-web/luna"
    item = r.json()["output"][0]
    assert (
        item["type"] == "function_call"
        and item["name"] == "shell"
        and json.loads(item["arguments"]) == {"command": ["ls"]}
    )
    assert fake.calls[-1]["_single_tool_call"] is True
    # the tool result goes back in; streaming carries the same items
    req2 = {
        **req,
        "stream": True,
        "input": [
            {"role": "user", "content": "list files"},
            item,
            {"type": "function_call_output", "call_id": item["call_id"], "output": "a.py"},
        ],
    }
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
    now[0] += 86400  # a new day: the count starts over
    assert b.status()["today"] == 0
    b._block(cw.WebUnavailable("not signed in", 1800, "login"))
    b2 = cw.Bridge("https://chatgpt.test", {"notify": False}, home=tmp_path, clock=lambda: now[0])  # survives a restart
    st = b2.status()
    assert st["blocked"] and st["kind"] == "login" and st["signed_in"] is False
    with pytest.raises(cw.WebUnavailable, match="not signed in"):
        run(b2.complete("luna", dict(USER)))
    b2.reset()
    assert not b2.status()["blocked"]
    with pytest.raises(cw.WebSkip, match="over the bridge"):
        run(
            cw.Bridge("https://chatgpt.test", {"max_prompt_chars": 50, "notify": False}, home=tmp_path).complete(
                "luna", dict(USER)
            )
        )


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

    b = cw.Bridge(
        mock.url,
        {
            "driver": os.environ.get("PCORE_TEST_DRIVER", "playwright"),
            "headless": True,
            "notify": False,
            "min_gap_s": 3,
            "max_gap_s": 3,
            "challenge_wait_s": 1,
            "profile": str(tmp_path / "profile"),
            **opts,
        },
        home=tmp_path,
        sleep=sleep,
    )
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
    mock.replies = [
        "<p>The answer is <strong>42</strong>, see <code>f(x)</code>.</p>"
        "<ol><li>first</li><li>second<ul><li>nested</li></ul></li></ol>"
        + code("python", "def f(x):\n    return x * 2  # ``` not a fence")
        + "<h3>Done</h3><table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"
    ]

    async def go(b):
        d1 = await b.complete(
            "luna",
            {
                "messages": [
                    {"role": "system", "content": "Be brief."},
                    {"role": "user", "content": "line one\n\n    indented <b>&amp;\nline three"},
                ]
            },
        )
        d2 = await b.complete("luna", dict(USER))
        return d1, d2, b.status()

    d1, d2, st = scenario(mock, tmp_path, go)
    text = d1["choices"][0]["message"]["content"]
    assert text == (
        "The answer is **42**, see `f(x)`.\n\n1. first\n2. second\n   - nested\n\n"
        "````python\ndef f(x):\n    return x * 2  # ``` not a fence\n````\n\n### Done\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"
    )
    assert d1["choices"][0]["finish_reason"] == "stop" and d1["pcore"]["web"]["model_slug"] == "gpt-6-luna"
    # each request: its own page load, in temporary chat, holding the whole prompt as typed
    assert len(mock.sent) == 2 and all(s["temp"] for s in mock.sent)
    assert (
        "Be brief." in mock.sent[0]["prompt"]
        and "line one\n\n    indented <b>&amp;\nline three" in mock.sent[0]["prompt"]
    )
    assert "line one" not in mock.sent[1]["prompt"]  # nothing carried over
    assert st["today"] == 2 and st["signed_in"] is True and st["model_seen"] == "gpt-6-luna"


@browser
def test_temporary_chat_reuses_only_its_own_conversation(mock, tmp_path):
    mock.replies = [
        tool_call("shell", {"command": ["pwd"]}),
        tool_call("shell", {"command": ["ls"]}),
        "<p>done</p>",
    ]

    async def go(b):
        a = {
            "messages": [{"role": "system", "content": "Work carefully."}, {"role": "user", "content": "task A"}],
            "tools": TOOLS,
        }
        b_body = {
            "messages": [{"role": "system", "content": "Work carefully."}, {"role": "user", "content": "task B"}],
            "tools": TOOLS,
        }
        first = await b.complete("luna", a)
        await b.complete("luna", b_body)
        follow = {
            **a,
            "messages": a["messages"]
            + [
                first["choices"][0]["message"],
                {
                    "role": "tool",
                    "tool_call_id": first["choices"][0]["message"]["tool_calls"][0]["id"],
                    "content": "/repo",
                },
            ],
        }
        last = await b.complete("luna", follow)
        return last, len(b._sessions)

    last, sessions = scenario(mock, tmp_path, go, reuse_chat=True)
    assert last["choices"][0]["message"]["content"] == "done"
    assert sessions == 2
    assert all(sent["temp"] for sent in mock.sent)
    assert "task A" in mock.sent[0]["prompt"] and "task B" in mock.sent[1]["prompt"]
    assert "<<<tool result: shell>>>\n/repo" in mock.sent[2]["prompt"]
    assert "task A" not in mock.sent[2]["prompt"] and "Work carefully." not in mock.sent[2]["prompt"]


@browser
def test_full_chat_moves_to_a_new_chat_in_the_same_request(mock, tmp_path):
    mock.state["full_after"] = 1
    mock.replies = [tool_call("shell", {"command": ["pwd"]}), "<p>done</p>"]

    async def go(b):
        a = {"messages": [{"role": "user", "content": "task A"}], "tools": TOOLS}
        first = await b.complete("luna", a)
        msg = first["choices"][0]["message"]
        follow = {
            **a,
            "messages": a["messages"]
            + [msg, {"role": "tool", "tool_call_id": msg["tool_calls"][0]["id"], "content": "/repo"}],
        }
        return await b.complete("luna", follow), b.load().get("strikes", 0)

    last, strikes = scenario(mock, tmp_path, go, reuse_chat=True)
    assert last["choices"][0]["message"]["content"] == "done"
    assert last["pcore"]["web"]["restarted"] and not last["pcore"]["web"]["continued"] and not strikes
    assert len(mock.sent) == 2 and "task A" in mock.sent[1]["prompt"] and "/repo" in mock.sent[1]["prompt"]


@browser
def test_a_guest_bridge_works_in_the_hosts_chrome(mock, tmp_path):
    async def go(b):
        guest = cw.Bridge(mock.url, {**b.o, "reuse_chat": True}, home=tmp_path / "guest", host=b)
        first = await b.complete("auto", dict(USER))
        second = await guest.complete("auto", dict(USER))
        same, tabs = guest._ctx is b._ctx, len(b._ctx.pages)
        await guest._shutdown()  # closes the guest's tabs only
        third = await b.complete("auto", dict(USER))
        return (
            [r["choices"][0]["message"]["content"] for r in (first, second, third)],
            same,
            tabs,
            guest.profile == b.profile,
        )

    texts, same, tabs, same_profile = scenario(mock, tmp_path, go)
    assert texts == ["ok", "ok", "ok"] and same and tabs == 2
    assert same_profile and len(mock.sent) == 3


@browser
def test_named_model_can_fall_back_to_auto(mock, tmp_path):
    mock.state["model"] = "Auto"
    mock.state["models"] = ["Auto"]
    result = scenario(mock, tmp_path, lambda b: b.complete("luna", dict(USER)), allow_auto_fallback=True)
    assert result["choices"][0]["message"]["content"] == "ok"
    assert mock.sent[0]["model"] == "Auto"


@browser
def test_page_tool_call_and_repair(mock, tmp_path):
    good = "<p>Listing.</p>" + tool_call("shell", {"command": ["ls", "-la"]})
    mock.replies = [
        good,
        "<p>Sorry.</p>" + code("tool_call", '{"name": "shell", "arguments": {"command": ["ls",]}}'),
        good,
    ]

    async def go(b):
        body = {"messages": [{"role": "user", "content": "list"}], "tools": TOOLS}
        return await b.complete("luna", body), await b.complete("luna", body)

    d1, d2 = scenario(mock, tmp_path, go)
    for d in (d1, d2):
        msg = d["choices"][0]["message"]
        assert msg["content"] == "Listing." and d["choices"][0]["finish_reason"] == "tool_calls"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"command": ["ls", "-la"]}
    assert not d1["pcore"]["web"]["repaired"] and d2["pcore"]["web"]["repaired"]
    assert (
        len(mock.sent) == 3 and "not valid JSON" in mock.sent[2]["prompt"] and "```tool_call" in mock.sent[0]["prompt"]
    )


def test_stalled_replies():
    for t in (
        "I’m ready to fix it, but the local shell execution interface isn't available to me in this turn. Please retry with the tool enabled.",
        "I don't have access to your filesystem.",
        "I can't run commands here, so here is what to do:",
        "I'm unable to execute the tests directly.",
        "`rg` isn’t installed, so I’ll use standard shell utilities to inspect the project.\n\nThe initial inspection command "
        "hit an environment error. I haven't changed any files yet. Please retry the shell inspection so I can continue.",
        "Next, I'll patch the config.",
        "Let me check the tests first.",
        "I couldn’t find a directory matching “Olympus” under /Users/yufan. Would you like me to search the rest?",
        "The previous search process is no longer accessible. Should I rerun the Olympus project search?",
    ):
        assert cw.stalled(t, SCHEMAS), t
    for t in (
        "Fixed: `mean()` used floor division. Tests pass.",
        "Which of the two configs should I keep?",
        "Done. Run `make deploy` when you want it live.",
        "The endpoint is not available before v2. " + "x" * 1600,
    ):
        assert not cw.stalled(t, SCHEMAS), t
    assert not cw.stalled("I can't run commands here.", {})


@browser
def test_page_nudges_a_model_that_says_it_has_no_tools(mock, tmp_path):
    exec_tools = [EXEC]
    mock.replies = [
        "<p>The shell interface isn't available to me in this turn.</p>",
        "<p>Running it.</p>" + code("exec_command", "python3 test_calc.py"),
        "<p>I can't run this: the sandbox is read-only.</p>",
        "<p>I can't run this: the sandbox is read-only.</p>",
    ]

    async def go(b):
        body = {"messages": [{"role": "user", "content": "fix the test"}], "tools": exec_tools}
        return await b.complete("luna", body), await b.complete("luna", body)

    d1, d2 = scenario(mock, tmp_path, go)
    msg = d1["choices"][0]["message"]
    assert msg["content"] == "Running it." and json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {
        "cmd": "python3 test_calc.py"
    }
    assert (
        d1["pcore"]["web"]["nudged"]
        and "just text in your reply" in mock.sent[1]["prompt"]
        and "```exec_command" in mock.sent[1]["prompt"]
    )
    # a model that means it is asked once and then taken at its word
    assert (
        d2["pcore"]["web"]["nudged"]
        and d2["choices"][0]["message"]["content"].startswith("I can't run this")
        and len(mock.sent) == 4
    )


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
@pytest.mark.parametrize(
    "trouble,kind",
    [
        ({"login": False}, "login"),
        ({"challenge": True}, "challenge"),
        ({"temp": False}, "tempchat"),
        ({"limit": True}, "limit"),
    ],
)
def test_page_trouble_means_unavailable_and_nothing_sent(mock, tmp_path, trouble, kind):
    mock.state.update(trouble)

    async def go(b):
        with pytest.raises(cw.WebUnavailable) as e:
            await b.complete("luna", dict(USER))
        with pytest.raises(cw.WebUnavailable):  # blocked now: no second visit
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
    assert len(sleeps) == 2 and all(2 < s <= 3 for s in sleeps)  # a pause before the 2nd and 3rd request


@browser
def test_page_without_a_model_picker(mock, tmp_path):
    """The Free plan has one model and no picker: a named model is whatever the account answers with."""
    mock.state.update(picker=False)

    async def go(b):
        return await b.complete("luna", dict(USER)), await b.check("luna")

    d, chk = scenario(mock, tmp_path, go)
    assert (
        d["choices"][0]["message"]["content"] == "ok"
        and len(mock.sent) == 1
        and chk["ok"]
        and chk["page_model"] is None
    )


@browser
def test_page_error_reply_and_closed_window(mock, tmp_path):
    mock.replies = [{"error": "Something went wrong while generating the response."}, "<p>fine</p>"]

    async def go(b):
        with pytest.raises(cw.WebError, match="Something went wrong"):
            await b.complete("luna", dict(USER))
        ok = await b.complete("luna", dict(USER))
        await b._page.close()  # the user closes the window
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
    router = Router(
        load(tmp_path / "catalog.yaml"),
        State(),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200, json={"choices": [{"message": {"role": "assistant", "content": "from the pool"}}]}
                )
            )
        ),
    )
    router.bridges["chatgpt-web"] = bridge_for(mock, tmp_path, min_gap_s=0, max_gap_s=0)
    mock.replies = [tool_call("shell", {"command": ["ls"]}), "<p>Two files: a.py and b.py.</p>"]
    tools = [{"type": "function", "name": "shell", "description": "Run", "parameters": SCHEMAS["shell"]}]
    with TestClient(create_app(router=router)) as c:
        r = c.post("/v1/responses", json={"model": "nova", "input": "list files", "tools": tools})
        call = r.json()["output"][0]
        assert r.headers["x-pcore-model"] == "chatgpt-web/luna" and call["type"] == "function_call"
        r = c.post(
            "/v1/responses",
            json={
                "model": "nova",
                "tools": tools,
                "previous_response_id": r.json()["id"],
                "input": [{"type": "function_call_output", "call_id": call["call_id"], "output": "a.py\nb.py"}],
            },
        )
        assert r.json()["output"][0]["content"][0]["text"] == "Two files: a.py and b.py."
        assert "<<<tool result: shell>>>\na.py\nb.py" in mock.sent[1]["prompt"]
        assert c.get("/chatgpt-web/status").json()["today"] == 2
        mock.state["login"] = False  # the login expires: the request is served by the pool
        r = c.post("/v1/responses", json={"model": "luna/nova", "input": "hi"})
        assert r.headers["x-pcore-model"] == "a/big" and r.json()["output"][0]["content"][0]["text"] == "from the pool"
        st = c.get("/chatgpt-web/status").json()
        assert st["blocked"] and st["kind"] == "login" and st["models"][0]["available"] is False
        mock.state["login"] = True  # signed in again: a live check clears the block
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
    assert '**bold text**, inline code `print("Hello")`, and a [link to example.com](https://example.com).' in md
    assert "- Nested item" in md and "1. First step\n2. Second step" in md and "> This is a blockquote." in md
    assert "| Column 1 | Column 2 |\n| --- | --- |\n| A | B |\n| C | D |" in md
    assert '```python\ndef f(x):\n    # <tag> & "q"\n    return x**2\n```' in md
    assert "$x^2$" in md and "\n\n---\n\n" in md
    text, calls = cw.parse_reply(
        md, {"read_file": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}
    )
    assert [(c["name"], json.loads(c["arguments"])) for c in calls] == [("read_file", {"path": "/etc/hosts"})]
    assert "Python\n" not in md and "Run" not in md and "tool_call" not in text


def test_lean_body_cuts_the_harness_prompt():
    body = {
        "messages": [
            {"role": "developer", "content": "You are a coding agent running in the Codex CLI. " + "rule " * 3000},
            {"role": "developer", "content": "<skills_instructions>\n## Skills\n- imagegen</skills_instructions>"},
            {"role": "developer", "content": "Reply in French."},
            {
                "role": "user",
                "content": "<environment_context>\n  <cwd>/repo</cwd>\n  <shell>zsh</shell>\n"
                "  <current_date>2026-11-21</current_date>\n  <filesystem>"
                + "x" * 900
                + "</filesystem>\n</environment_context>",
            },
            {"role": "user", "content": "fix the bug"},
        ],
        "tools": TOOLS + [{"type": "function", "function": {"name": "update_goal", "parameters": {}}}],
    }
    lean = cw.lean_body(body)
    prompt, tools, _ = cw.render_prompt(lean)
    assert len(prompt) < 4500 and "update_goal" not in tools and "shell" in tools
    for piece in ("apply_patch", "Reply in French.", "cwd: /repo, shell: zsh, current_date: 2026-11-21", "fix the bug"):
        assert piece in prompt
    assert "Skills" not in prompt and "xxxx" not in prompt
    assert cw.lean_body(body) == lean  # stable, so a continued chat still lines up


def test_agents_in_one_directory_get_their_own_chat():
    env = {"role": "user", "content": "<environment_context><cwd>/repo</cwd></environment_context>"}
    a = {"messages": [env, {"role": "user", "content": "task A"}]}
    b = {"messages": [env, {"role": "user", "content": "task B"}]}
    later = {"messages": a["messages"] + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "more"}]}
    assert cw.continuation_key(a, "luna") != cw.continuation_key(b, "luna")
    assert cw.continuation_key(a, "luna") == cw.continuation_key(later, "luna")


def test_accounts_have_their_own_profile_and_state(tmp_path):
    pool = cw.make_bridge("https://chatgpt.com", {"accounts": ["main", "b"], "daily_cap": 5})
    assert isinstance(pool, cw.BridgePool) and list(pool.bridges) == ["main", "b"]
    pool = cw.BridgePool("https://chatgpt.com", {"accounts": ["main", "b"], "daily_cap": 5}, home=tmp_path)
    m, b = pool.bridges["main"], pool.bridges["b"]
    assert m.profile.name == "chatgpt-web-profile" and b.profile.name == "chatgpt-web-profile-b"
    assert m.state_path.name == "chatgpt_web.json" and b.state_path.name == "chatgpt_web-b.json"
    m.save(count=5, signed_in=True)
    b.save(signed_in=True)
    st = pool.status()
    assert st["today"] == 5 and st["daily_cap"] == 10 and not st["blocked"] and st["signed_in"]
    assert pool._order("luna", dict(USER))[0] is b  # the one with room goes first
    b.busy = 2
    assert pool._order("luna", dict(USER))[0] is m


@browser
def test_pool_spreads_and_fails_over(mock, tmp_path):
    mock.replies = ["<p>one</p>", "<p>two</p>", "<p>three</p>"]

    async def go():
        pool = cw.BridgePool(
            mock.url,
            {
                "accounts": ["main", "b"],
                "headless": True,
                "min_gap_s": 0,
                "max_gap_s": 0,
                "notify": False,
                "profile": str(tmp_path / "profile"),
                "driver": os.environ.get("PCORE_TEST_DRIVER", "playwright"),
            },
            home=tmp_path,
        )
        try:
            pool.bridges["main"].save(blocked_until=time.time() + 600, reason="temporary limit", kind="busy")
            r1 = await pool.complete("auto", dict(USER))
            pool.bridges["main"].reset()
            r2, r3 = await asyncio.gather(pool.complete("auto", dict(USER)), pool.complete("auto", dict(USER)))
            return [r1, r2, r3], {n: b.load()["count"] for n, b in pool.bridges.items()}
        finally:
            await pool.release()

    replies, counts = run(go())
    assert sorted(r["choices"][0]["message"]["content"] for r in replies) == ["one", "three", "two"]
    assert counts["b"] >= 1 and counts["main"] >= 1 and sum(counts.values()) == 3


@browser
def test_temporary_limit_backs_off_and_doubles(mock, tmp_path):
    mock.state["notice"] = "Temporary limits are stopping this chat from loading. Try again later."

    async def go(b):
        waits = []
        for _ in range(2):
            try:
                await b.complete("auto", dict(USER))
            except cw.WebUnavailable as e:
                waits.append((e.kind, round(e.cooldown)))
            b.save(blocked_until=0)
        return waits, mock.sent

    waits, sent = scenario(mock, tmp_path, go)
    assert waits == [("busy", 60), ("busy", 120)] and not sent


def test_live_stream_sends_prose_early_then_the_rest():
    async def go():
        gate = asyncio.Event()

        async def start(on_text):
            on_text("Looking at the files.")
            await gate.wait()
            calls = [{"id": "call_1", "name": "shell", "arguments": '{"command": ["ls"]}'}]
            return cw.chat_completion("chatgpt-web/luna", "Looking at the files.\n\nNow listing.", calls, "p")

        live = await cw.LiveStream(start, "chatgpt-web/luna").first()  # returns at the first text, before the end
        assert not live.task.done()
        gate.set()
        up = UpstreamStream(live)
        return [e async for e in up.events()], up

    events, up = run(go())
    texts = [v for k, v in events if k == "text"]
    assert texts[0] == "Looking at the files." and "".join(texts) == "Looking at the files.\n\nNow listing."
    assert up.finish_reason == "tool_calls"


def test_live_stream_fails_over_before_any_text_and_reports_after():
    async def go():
        async def early(on_text):
            raise cw.WebUnavailable("signed out", 60, "login")

        with pytest.raises(cw.WebUnavailable):
            await cw.LiveStream(early, "m").first()

        async def late(on_text):
            on_text("Half a")
            await asyncio.sleep(0)
            raise cw.WebError("the window went away")

        live = await cw.LiveStream(late, "m").first()
        with pytest.raises(RuntimeError, match="window went away"):
            [e async for e in UpstreamStream(live).events()]

    run(go())


# ---- the bundled browser (`browser: bundled`), against the mock page ---------------------------------------------------
def _bundled_ready():
    """The bundled browser already prepared under <PCORE_HOME>/browser, new enough for the installed Chrome. These
    tests never download or prepare one (`python -m provenance_core.browser_host` does)."""
    if not _chrome_ok():
        return None
    from provenance_core import browser_host as bh

    return bh.current(need=bh.required_major())


bundled = pytest.mark.skipif(
    not _bundled_ready(), reason="no bundled browser prepared (python -m provenance_core.browser_host)"
)


def _bundled_bridge(mock, tmp_path, monkeypatch, source="cft", **opts):
    from provenance_core import browser_host as bh

    if source == "cft":
        rec = _bundled_ready()
        monkeypatch.setattr(bh, "ensure", lambda profile=None, **k: rec)  # what is prepared; never a download
    else:  # the clone of the installed Chrome is made per test, in a scratch directory (an APFS clone: instant)
        monkeypatch.setenv("PCORE_BROWSER_DIR", str(tmp_path / "browser"))
    return bridge_for(mock, tmp_path, headless=False, browser="bundled", browser_source=source, **opts)


def _app_type(pid: int) -> str:
    import subprocess

    sh = lambda *c: subprocess.run(c, capture_output=True, text=True).stdout.strip()  # noqa: E731
    return sh("lsappinfo", "info", "-only", "ApplicationType", sh("lsappinfo", "find", f"pid={pid}"))


def _left_running(profile) -> str:
    import subprocess

    return subprocess.run(["pgrep", "-f", f"user-data-dir={profile}"], capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("cft", marks=bundled),
        pytest.param(
            "chrome",
            marks=pytest.mark.skipif(
                not _chrome_ok() or not os.uname().sysname == "Darwin", reason="needs macOS and Google Chrome"
            ),
        ),
    ],
)
def test_bundled_browser_runs_the_window_unseen(mock, tmp_path, monkeypatch, source):
    async def go():
        b = _bundled_bridge(mock, tmp_path, monkeypatch, source)
        try:
            first = await b.complete("auto", dict(USER))
            proc = b._proc
            guest = cw.Bridge(mock.url, {**b.o, "reuse_chat": True}, home=tmp_path / "guest", host=b)
            second = await guest.complete("auto", dict(USER))
            same, tabs = guest._ctx is b._ctx, len(b._ctx.pages)
            cdp = await b._ctx.new_cdp_session(guest._page)
            win = await cdp.send("Browser.getWindowForTarget")
            await cdp.detach()
            win["screen"] = await guest._page.evaluate("[screen.width, screen.height]")
            brands = await guest._page.evaluate("navigator.userAgentData.brands.map(b => b.brand)")
            # the guest's page is a background tab of the off-screen window: its timers must still run
            ticks = await guest._page.evaluate(
                "() => new Promise(r => { let i = 0; const t = setInterval(() => i++, 50);"
                " setTimeout(() => { clearInterval(t); r(i); }, 1000); })"
            )
            st, gst = b.status(), guest.status()
            await guest._shutdown()  # closes the guest's tabs only
            third = await b.complete("auto", dict(USER))
            app_type = _app_type(proc.pid)
        finally:
            await b.release()
        return dict(
            texts=[r["choices"][0]["message"]["content"] for r in (first, second, third)],
            same=same,
            tabs=tabs,
            win=win,
            ticks=ticks,
            st=st,
            gst=gst,
            app_type=app_type,
            exit=proc.poll(),
            left=_left_running(b.profile),
            after=b.status(),
            brands=brands,
        )

    r = run(go())
    # Chrome for Testing has no "Google Chrome" brand; the clone of the installed Chrome is that Chrome
    assert ("Google Chrome" in r["brands"]) == (source == "chrome") and "Chromium" in r["brands"]
    assert r["texts"] == ["ok", "ok", "ok"] and r["same"] and r["tabs"] == 2 and len(mock.sent) == 3
    assert "BackgroundOnly" in r["app_type"]  # no Dock icon, never the active app
    # created off screen: macOS keeps only a corner of about 40x41 px of it inside the display
    (w, h), bounds = r["win"]["screen"], r["win"]["bounds"]
    assert bounds["left"] >= w - 64 and bounds["top"] >= h - 64
    assert r["ticks"] >= 12  # 20 expected; a throttled background tab manages about 1
    assert r["st"]["browser"].startswith("bundled ") and r["gst"]["browser"] == r["st"]["browser"]
    assert r["exit"] is not None and not r["left"]  # release() quit it; nothing left on the scratch profile
    assert r["after"]["browser"] is None and not r["after"]["window_open"]


@bundled
def test_bundled_browser_goes_when_the_bridge_goes(mock, tmp_path, monkeypatch):
    """A background-only browser is never seen: if the process that drives it dies, its watchdog ends it. And a
    vanished bundled window is started again on the next request (nobody can have closed it by hand)."""

    async def go():
        b = _bundled_bridge(mock, tmp_path, monkeypatch)
        try:
            await b.complete("auto", dict(USER))
            proc = b._proc
            b._watch.stdin.close()  # what the kernel does to the pipe when this process dies, even by SIGKILL
            for _ in range(150):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            gone = proc.poll() is not None
            await asyncio.sleep(0.5)
            again = await b.complete("auto", dict(USER))
            relaunched = b._proc is not proc and b._proc.poll() is None
        finally:
            await b.release()
        return gone, again, relaunched, b.status(), _left_running(b.profile)

    gone, again, relaunched, st, left = run(go())
    assert gone and relaunched and again["choices"][0]["message"]["content"] == "ok"
    assert not st["blocked"] and not left


def test_browser_option_picks_the_browser(tmp_path, monkeypatch):
    from provenance_core import browser_host as bh

    rec = {"exe": "/nonexistent/bundled", "version": "157.0.1.2"}
    monkeypatch.setattr(bh, "ensure", lambda profile=None, **k: rec)
    monkeypatch.setattr(bh, "ensure_soon", lambda profile=None, **k: None)

    def mk(**o):
        return cw.Bridge("http://x", {"profile": str(tmp_path / "p"), "notify": False, **o}, home=tmp_path)

    async def go():
        assert cw.DEFAULTS["browser"] == "chrome" and await mk()._bundled_browser() is None
        assert await mk(browser="bundled")._bundled_browser() is rec
        assert await mk(browser="bundled", headless=True)._bundled_browser() is None  # headless: as before
        # not ready: skipped while it is prepared (nothing that can show is opened instead), unless asked to
        with pytest.raises((cw.WebSkip, cw.WebUnavailable)):
            await mk(browser="auto")._bundled_browser()
        assert await mk(browser="auto", browser_fallback="chrome")._bundled_browser() is None
        with pytest.raises(cw.WebUnavailable) as e:
            await mk(browser="firefox")._bundled_browser()
        assert e.value.kind == "setup"
        # a bundled browser that cannot start: unavailable for a while, and nothing else is opened instead
        b = mk(browser="bundled", driver=os.environ.get("PCORE_TEST_DRIVER", "playwright"))
        with pytest.raises(cw.WebUnavailable) as e:
            await b._ensure_page()
        assert e.value.kind == "browser" and "bundled" in e.value.reason and b._proc is None and b._pw is None

    run(go())


def test_model_allowance_is_learned_from_the_limit_notice(tmp_path):
    """A model with its own small allowance: capacity follows what was used, the count reached when the site says
    the limit is hit becomes the allowance, only that model stops, and a new window starts a day later."""
    now = [1_000_000.0]
    b = cw.Bridge("http://x", {"notify": False, "daily_cap": 300, "model_caps": {"max": 6}}, home=tmp_path,
                  clock=lambda: now[0])  # fmt: skip
    b.site = {"name": "Site"}
    assert b.capacity(model="max")["remaining"] == 1.0
    assert b.capacity(model="other")["remaining"] == 1.0
    for _ in range(3):
        b._model_replied("max")
    assert b.capacity(model="max")["remaining"] == 0.5
    b._model_limited("max", cw.WebUnavailable("limit", 3600, "limit"))
    assert b.model_usage("max")["cap"] == 3 and b.model_usage("max")["learned"]
    assert b.capacity(model="max")["available"] is False
    assert b.capacity(model="other")["available"] is True  # the account itself is not blocked
    with pytest.raises(cw.WebUnavailable):
        b._precheck("max")
    b._precheck("other")
    now[0] += 3601  # tried again, still limited: the allowance learned is kept, not replaced by 0
    b._model_limited("max", cw.WebUnavailable("limit", 3600, "limit"))
    assert b.model_usage("max")["cap"] == 3
    now[0] += 90000  # a day on: a new window
    u = b.model_usage("max")
    assert u["used"] == 0 and u["cap"] == 3 and not u["limited_for_s"]
    assert b.capacity(model="max")["remaining"] == 1.0
    for _ in range(5):  # the site allowed more than was learned: the allowance grows with it
        b._model_replied("max")
    assert b.model_usage("max")["cap"] == 6 and 0 < b.capacity(model="max")["remaining"] < 0.2


@browser
def test_a_message_too_long_for_the_box_is_shortened_and_sent(mock, tmp_path):
    """The page's box holds less than the message: the bridge learns the size, shortens the history to it and
    sends in the same request; the next fresh chat on this account starts at that size."""
    mock.state["box_max"] = 9000
    mock.replies = ["<p>done</p>"]
    old = [m for i in range(40) for m in ({"role": "user", "content": f"step {i} " + "x" * 400},
                                          {"role": "assistant", "content": f"did {i} " + "y" * 400})]  # fmt: skip
    body = {"messages": old + [{"role": "user", "content": "and now the last question"}]}

    async def go(b):
        d = await b.complete("luna", body)
        return d, b.load()

    d, st = scenario(mock, tmp_path, go)
    assert d["choices"][0]["message"]["content"] == "done"
    assert 4000 <= st["page_chars"] <= 9000
    assert len(mock.sent) == 1 and len(mock.sent[0]["prompt"]) <= 9000
    assert "and now the last question" in mock.sent[0]["prompt"]
