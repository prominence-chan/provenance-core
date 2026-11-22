"""Summarising compaction (provenance_core/compact.py): the cut and its stability, what the brief keeps, its size
bound, the cache, the optional model narrative and its fallbacks, and where it is wired in (API models whose context
is too small, the web bridge's fresh prompts; a continued chat never affected; the blunt cut's pairing fix)."""

import asyncio
import copy
import json

import httpx
import pytest
import yaml
from test_prune import CWD, Convo, PagelessBridge, agent_run

from provenance_core import chatgpt_web as cw
from provenance_core import compact, prune
from provenance_core.catalog import load
from provenance_core.router import NoCapacity, Router
from provenance_core.state import State

API = compact.config("api", True)
WEB = compact.config("web", True)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    for k in ("PCORE_COMPACT", "TIERLM_COMPACT", "PCORE_PRUNE", "TIERLM_PRUNE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PCORE_HOME", str(tmp_path / "home"))


def session(turns=40):
    """A run with what a brief must keep: constraints stated mid-run, a file created then edited, a failing test that
    passes later, a plan, a finding in the agent's own words, plus bulk (long outputs) to compact."""
    c = Convo(task="Add a --json flag to the report tool.")
    patch_add = "*** Begin Patch\n*** Add File: report/out.py\n+def emit(rows):\n+    return rows\n*** End Patch"
    c.call(
        "",
        "Success. Updated the following files:\nA report/out.py\n",
        name="apply_patch",
        args={"input": patch_add},
        raw=True,
    )
    c.user("Constraint: do not touch report/legacy.py, and keep Python 3.9 compatible.")
    c.call("pytest -q tests/test_out.py", "E   ImportError: cannot import name 'emit_json'\n1 failed\n", code=1)
    c.call(
        "",
        json.dumps({"status": "ok"}),
        name="update_plan",
        args={
            "plan": [{"step": "write emit_json", "status": "in_progress"}, {"step": "wire --json", "status": "pending"}]
        },
        raw=True,
    )
    patch_upd = "*** Begin Patch\n*** Update File: report/out.py\n@@\n+def emit_json(rows):\n+    return json.dumps(rows)\n*** End Patch"
    c.call("", "Success. Updated the following files:\nM report/out.py\n", name="apply_patch", args={"input": patch_upd}, raw=True,
           text="The import failed because emit_json did not exist yet; adding it next to emit.")  # fmt: skip
    c.call("pytest -q tests/test_out.py", "3 passed in 0.12s\n")
    for i in range(turns):
        c.call(f"cat src/part{i}.py", f"token {i} " + "z" * 3000 + "\n")
    return c.messages


def requests_of(msgs):
    return [msgs[:i] for i, m in enumerate(msgs) if m["role"] == "assistant"] + [msgs]


def pairs_ok(messages):
    """Every tool result has its call and every call its result (the last turn may still be waiting)."""
    calls = {tc["id"] for m in messages for tc in m.get("tool_calls") or []}
    results = {m["tool_call_id"] for m in messages if m["role"] == "tool"}
    last_calls = {tc["id"] for tc in (messages[-1].get("tool_calls") or [])} if messages else set()
    return results <= calls and (calls - results) <= last_calls


# ---- settings ------------------------------------------------------------------------------------------------------------
def test_off_by_default_and_switches(monkeypatch):
    assert compact.config("api", None) is None and compact.config("web", False) is None
    assert compact.config("api", True)["step"] == 8 and compact.config("web", True)["step"] == 4
    assert compact.config("web", True)["use_room"] and not compact.config("api", True)["use_room"]
    assert (
        compact.config("api", {"api": {"model": "spark"}})["model"] == "spark"
        and compact.config("web", {"api": True}) is None
    )
    assert compact.config("web", {"enabled": False}) is None
    monkeypatch.setenv("PCORE_COMPACT", "web")
    assert compact.config("web", None) and compact.config("api", True) is None
    monkeypatch.setenv("PCORE_COMPACT", "0")
    assert compact.config("web", True) is None
    monkeypatch.setenv("PCORE_COMPACT", "1")
    assert compact.config("api", None)["keep_turns"] == compact.DEFAULTS["keep_turns"]


# ---- the cut -----------------------------------------------------------------------------------------------------------------
def test_the_cut_sits_on_the_grid_keeps_the_last_turns_and_only_moves_forward():
    msgs = session(60)
    asst = [i for i, m in enumerate(msgs) if m["role"] == "assistant"]
    budget = 60000
    cuts = []
    for req in requests_of(msgs):
        got = compact.boundary(req, budget, API)
        if got is None:
            continue
        b, opening, _ = got
        here = [i for i, m in enumerate(req) if m["role"] == "assistant"]
        assert b in here and here.index(b) % API["step"] == 0  # every 8th assistant turn
        assert len(here) - here.index(b) >= API["keep_turns"]
        assert opening == asst[0]
        cuts.append(b)
    assert cuts == sorted(cuts) and 1 < len(set(cuts)) < len(cuts) / 3  # forward only, and seldom
    assert compact.boundary(msgs[: asst[5]], 10, API) is None  # too few turns to summarise


# ---- the brief -----------------------------------------------------------------------------------------------------------------
def brief_of(msgs, budget=40000, o=API, c=None):
    c = c or compact.Compactor()
    out, rep = c.compact(msgs, msgs, budget, o)
    return out, rep, next(m["content"] for m in out if m["role"] == "user" and m["content"].startswith("[Context note"))


def test_the_brief_keeps_what_matters():
    msgs = session(40)
    out, rep, text = brief_of(msgs)
    assert "Constraint: do not touch report/legacy.py, and keep Python 3.9 compatible." in text  # verbatim
    assert "- report/out.py: created at turn 1; edited 1x, last at turn 4" in text
    assert "+def emit_json(rows):" in text  # the last change
    assert (
        "`pytest -q tests/test_out.py` (ran fine later, at turn 5): E ImportError: cannot import name 'emit_json'"
        in text
    )
    assert "3 passed in 0.12s" in text and "2 runs" in text
    assert "[in_progress] write emit_json" in text and "[pending] wire --json" in text
    assert "The import failed because emit_json did not exist yet" in text  # the agent's own finding
    assert "src/part3.py" in text  # files only read are still named
    assert rep["writer"] == "extractive" and rep["brief_chars"] == len(text) <= compact.brief_limit(40000, API)
    # what is sent: system + opening verbatim, the brief, the newest turns verbatim, and nothing over the budget
    assert out[:3] == msgs[:3] and out[-1] == msgs[-1]
    assert out[4:] == msgs[rep["boundary"] :]
    assert rep["after"] == sum(compact.json_size(m) for m in out) <= 40000 < rep["before"]
    assert pairs_ok(out)


def test_the_brief_is_bounded_and_deterministic():
    msgs = agent_run(3, turns=300)
    for budget in (8000, 30000, 200000):
        out, rep = compact.Compactor().compact(msgs, msgs, budget, API)
        assert rep["brief_chars"] <= compact.brief_limit(budget, API)
        assert pairs_ok(out)
        again, _ = compact.Compactor().compact(copy.deepcopy(msgs), copy.deepcopy(msgs), budget, API)
        assert json.dumps(again) == json.dumps(out)


def test_kept_turns_too_big_are_shortened_not_dropped():
    c = Convo()
    for i in range(20):
        c.call(f"make step{i}", "x" * 20000)
    out, rep = compact.Compactor().compact(c.messages, c.messages, 30000, API)
    assert rep["trimmed"] > 0 and rep["after"] <= 30000 + 2000  # the calls and the brief stay; outputs shrink
    assert pairs_ok(out) and [m["role"] for m in out[4:]] == [m["role"] for m in c.messages[rep["boundary"] :]]


# ---- stability and the cache ----------------------------------------------------------------------------------------------------
def test_same_cut_byte_identical_prefix_and_one_brief_per_cut():
    msgs = session(80)
    c = compact.Compactor()
    prev, cuts, same = None, set(), 0
    for req in requests_of(msgs):
        out, rep = c.compact(req, req, 60000, API)
        if rep is None:
            prev = None
            continue
        cuts.add(rep["boundary"])
        if prev and prev[1] == rep["boundary"]:
            # same cut: the earlier request's whole compacted history is the start of this one, byte for byte
            assert json.dumps(out[: len(prev[0])]) == json.dumps(prev[0])
            assert rep["cached"]
            same += 1
        prev = (out, rep["boundary"])
    assert same > 20 and len(cuts) > 2
    assert c.stats["misses"] == len(cuts)  # one brief per cut, every other request reuses it


def test_the_cache_ignores_call_ids_and_is_bounded():
    msgs = session(40)
    c = compact.Compactor(entries=2)
    _, rep = c.compact(msgs, msgs, 40000, API)
    renamed = copy.deepcopy(msgs)
    for m in renamed:
        for tc in m.get("tool_calls") or []:
            tc["id"] = "x" + tc["id"]
        if m["role"] == "tool":
            m["tool_call_id"] = "x" + m["tool_call_id"]
    _, rep2 = c.compact(renamed, renamed, 40000, API)
    assert rep2["cached"] and rep2["key"] == rep["key"]
    for n in range(3):
        other = session(40 + n)
        other[2] = {"role": "user", "content": f"task {n}"}
        c.compact(other, other, 40000, API)
    assert len(c.mem) <= 2 and len(c.facts) <= 1


# ---- the model's narrative ---------------------------------------------------------------------------------------------------
NARRATIVE = "## Decisions made\n- emit_json lives next to emit in report/out.py\n## In progress and the next step\n- wire --json"


def model_opts(**o):
    return {**API, "model": "spark", "model_min_chars": 1000, **o}


def test_a_model_writes_the_narrative_once_per_cut_and_it_is_kept_on_disk(tmp_path):
    calls = []

    async def summarize(system, digest, who, n):
        calls.append((who, n, len(digest)))
        assert "Constraint: do not touch report/legacy.py" in digest  # it reads the digest, not the history
        return NARRATIVE, "a/cheap"

    msgs = session(40)
    c = compact.Compactor(home=tmp_path)
    out, rep = asyncio.run(c.acompact(msgs, msgs, 40000, model_opts(), summarize=summarize))
    text = out[3]["content"]
    assert rep["writer"] == "model:a/cheap" and "## Summary of the work so far (written by a/cheap)" in text
    assert NARRATIVE in text and "Constraint: do not touch report/legacy.py" in text  # the rules' sections stay
    more = msgs + [msgs[-2], msgs[-1]]  # a turn later, same cut
    out2, rep2 = asyncio.run(c.acompact(more, more, 40000, model_opts(), summarize=summarize))
    assert rep2["cached"] and out2[3] == out[3] and len(calls) == 1
    # a restart: a new Compactor finds the narrative on disk; the sync path (no waiting) uses it too
    out3, rep3 = compact.Compactor(home=tmp_path).compact(msgs, msgs, 40000, model_opts())
    assert rep3["writer"] == "model:a/cheap" and out3[3] == out[3] and len(calls) == 1
    assert list((tmp_path / "compact").glob("*.json"))


@pytest.mark.parametrize("trouble", ["timeout", "error", "empty"])
def test_a_failing_model_gives_the_rules_brief(trouble):
    async def summarize(system, digest, who, n):
        if trouble == "timeout":
            await asyncio.sleep(5)
        if trouble == "error":
            raise RuntimeError("no model in spark could serve this")
        return "ok"

    msgs = session(40)
    c = compact.Compactor()
    out, rep = asyncio.run(c.acompact(msgs, msgs, 40000, model_opts(model_timeout_s=0.05), summarize=summarize))
    plain, _ = compact.Compactor().compact(msgs, msgs, 40000, API)
    assert rep["writer"] == "extractive" and rep["model_error"] and out == plain
    assert c.stats["model_failures"] == 1


def test_the_model_text_is_bounded_and_only_asked_for_big_prefixes():
    asked = []

    async def summarize(system, digest, who, n):
        asked.append(n)
        return "## Decisions made\n" + "- a decision\n" * 2000, "a/cheap"

    msgs = session(40)
    out, rep = asyncio.run(
        compact.Compactor().acompact(msgs, msgs, 40000, model_opts(model_chars=500), summarize=summarize)
    )
    assert asked == [500] and rep["brief_chars"] <= compact.brief_limit(40000, API)
    nar = out[3]["content"].split("(written by a/cheap)\n", 1)[1]
    assert "cut to its size bound" in nar[:700]
    _, rep = asyncio.run(
        compact.Compactor().acompact(msgs, msgs, 40000, model_opts(model_min_chars=10**9), summarize=summarize)
    )
    assert rep["writer"] == "extractive" and asked == [500]


def test_concurrent_requests_share_one_model_call():
    n = []

    async def summarize(system, digest, who, k):
        n.append(1)
        await asyncio.sleep(0.05)
        return NARRATIVE, "a/cheap"

    async def go():
        c = compact.Compactor()
        msgs = session(40)
        return await asyncio.gather(
            *(c.acompact(msgs, msgs, 40000, model_opts(), summarize=summarize) for _ in range(3))
        )

    outs = asyncio.run(go())
    assert len(n) == 1 and outs[0][0] == outs[1][0] == outs[2][0]


# ---- wiring: API models ---------------------------------------------------------------------------------------------------------
def make_router(tmp_path, monkeypatch, compact_setting, prune_setting=None, models=None, web=False):
    monkeypatch.setenv("A_KEY", "x")
    cat = {
        "tiers": [{"name": "solstice", "min_score": 50}, {"name": "spark", "min_score": 0}],
        "providers": {"a": {"base_url": "https://a.test/v1", "api_key_env": "A_KEY"}},
        "models": models or [{"provider": "a", "model": "small", "score": 90, "context": 40000}],
    }
    if web:
        monkeypatch.setenv("PCORE_CHATGPT_WEB", "1")
        cat["providers"]["w"] = {"type": "chatgpt_web", "base_url": "https://chatgpt.test", "api_key_env": "none"}
        cat["models"] = cat["models"] + [{"provider": "w", "model": "luna", "score": 20, "context": 64000}]
    if compact_setting is not None:
        cat["compact"] = compact_setting
    if prune_setting is not None:
        cat["prune"] = prune_setting
    p = tmp_path / "catalog.yaml"
    p.write_text(yaml.safe_dump(cat))
    seen = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(body)
        if body["messages"][0]["content"].startswith("You condense"):
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": NARRATIVE}}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Router(load(p), State(), client, home=tmp_path / "home"), seen


def test_api_models_with_a_small_context_get_a_compacted_history_only_when_on(tmp_path, monkeypatch, capsys):
    full = session(70)  # ~230k characters: far over a 40,000-token model
    budget = int((40000 * 0.85 - 2000) * 4)
    # two consecutive requests of the run whose cut is the same (the cut moves every few requests)
    reqs = requests_of(full)
    k = next(
        k
        for k in range(len(reqs) - 1, 0, -1)
        if (a := compact.boundary(reqs[k - 1], budget, API)) and a == compact.boundary(reqs[k], budget, API)
    )
    msgs, more = reqs[k - 1], reqs[k]
    router, seen = make_router(tmp_path, monkeypatch, None)
    with pytest.raises(NoCapacity):  # off by default: as before, no model can take it
        asyncio.run(router.complete({"messages": msgs}, "solstice"))
    router, seen = make_router(tmp_path, monkeypatch, {"api": True})
    data, m = asyncio.run(router.complete({"messages": msgs, "max_tokens": 2000}, "solstice"))
    rep = data["pcore"]["compact"]
    sent = seen[-1]["messages"]
    assert m.id == "a/small" and sent[3]["content"].startswith("[Context note")
    assert len(json.dumps(sent)) // 4 + 2000 <= 40000 and rep["after"] <= rep["budget"] == budget
    assert pairs_ok(sent) and sent[-1] == msgs[-1]
    assert "compact: a/small history" in capsys.readouterr().out
    # the next request of the run, same cut: the brief is reused and the start is byte-identical
    data2, _ = asyncio.run(router.complete({"messages": more, "max_tokens": 2000}, "solstice"))
    assert data2["pcore"]["compact"]["boundary"] == rep["boundary"] and data2["pcore"]["compact"]["cached"]
    assert seen[-1]["messages"][: len(sent)] == sent


def test_with_pruning_the_brief_comes_from_the_history_as_sent(tmp_path, monkeypatch):
    msgs = session(60)
    router, seen = make_router(tmp_path, monkeypatch, {"api": True}, {"api": True})
    asyncio.run(router.complete({"messages": msgs, "max_tokens": 2000}, "solstice"))
    view, _ = prune.prune_body({"messages": msgs}, "stable")
    expect, _ = compact.Compactor().compact(msgs, view["messages"], int((40000 * 0.85 - 2000) * 4), API)
    assert seen[-1]["messages"] == expect


def test_a_model_that_takes_the_whole_history_goes_first(tmp_path, monkeypatch):
    models = [
        {"provider": "a", "model": "small", "score": 95, "context": 40000},
        {"provider": "a", "model": "big", "score": 80, "context": 1000000},
        {"provider": "a", "model": "tiny", "score": 85, "context": 30000},  # under min_context: never compacted for
    ]
    router, seen = make_router(tmp_path, monkeypatch, {"api": True}, models=models)
    tokens = 60000
    order = [m.id for m in router.candidates("solstice", tokens, compactable=router.compact_options())]
    assert order == ["a/big", "a/small"]
    assert [m.id for m in router.candidates("solstice", tokens)] == ["a/big"]  # compaction off: as before


def test_the_narrative_is_asked_of_api_models_never_the_web_bridge(tmp_path, monkeypatch):
    router, seen = make_router(
        tmp_path, monkeypatch, {"api": {"model": "spark", "model_min_chars": 1000}}, models=[
            {"provider": "a", "model": "small", "score": 90, "context": 40000},
            {"provider": "a", "model": "cheap", "score": 30, "context": 100000},
        ], web=True,
    )  # fmt: skip

    class NoBridge:
        def __getattr__(self, name):
            raise AssertionError("the web bridge was used for a summary")

    router.bridges["w"] = NoBridge()
    msgs = session(60)
    data, _ = asyncio.run(router.complete({"messages": msgs, "max_tokens": 2000}, "solstice"))
    assert data["pcore"]["compact"]["writer"] == "model:a/cheap"
    asked = [b for b in seen if b["messages"][0]["content"].startswith("You condense")]
    assert len(asked) == 1 and asked[0]["model"] == "cheap" and "_pcore_internal" not in asked[0]
    with pytest.raises(ValueError):
        asyncio.run(router.summarize("s", "d", "w/luna", 100))


# ---- wiring: the web bridge ---------------------------------------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {"name": "exec_command", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}},
    },
    {
        "type": "function",
        "function": {"name": "apply_patch", "parameters": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}},
    },
]  # fmt: skip
CALL = '```tool_call\n{"name": "exec_command", "arguments": {"cmd": "ls"}}\n```'


def follow_up(body, reply, out="a.py\n"):
    return {
        **body,
        "messages": body["messages"]
        + [reply, {"role": "tool", "tool_call_id": reply["tool_calls"][0]["id"], "content": out}],
    }


def test_web_fresh_prompt_gets_the_brief_and_a_continued_chat_is_untouched(tmp_path):
    msgs = session(60)
    body = {"messages": msgs, "tools": TOOLS}

    async def go(**opts):
        b = PagelessBridge(tmp_path, [CALL, CALL, "done"], reuse_chat=True, fresh_prompt_chars=40000, **opts)
        d1 = await b.complete("luna", body)
        d2 = await b.complete("luna", follow_up(body, d1["choices"][0]["message"]))
        return b, d1, d2

    b0, off1, off2 = asyncio.run(go())
    b1, on1, on2 = asyncio.run(go(compact=True))
    assert off1["pcore"]["web"]["compacted"] and off1["pcore"]["web"]["compaction"] is None  # the blunt cut
    rep = on1["pcore"]["web"]["compaction"]
    assert rep and rep["scope"] == "web" and len(b1.typed[0]) <= 40000
    assert "[Context note from provenance-core" in b1.typed[0]
    assert "Constraint: do not touch report/legacy.py" in b1.typed[0]
    assert "Add a --json flag to the report tool." in b1.typed[0]
    # continued: only the new events are typed, exactly as with compaction off, and no record of a compaction
    assert on2["pcore"]["web"]["continued"] and on2["pcore"]["web"]["compaction"] is None
    assert b1.typed[1] == b0.typed[1] and b1.typed[1].startswith("# New harness events")
    # the session compares against the client's own messages, not the compacted ones
    key = next(iter(b1._sessions))
    assert b1._sessions[key]["seen"][: len(msgs) - 1] == msgs[1:]


def test_web_restarted_chat_gets_the_brief(tmp_path):
    msgs = session(60)
    body = {"messages": msgs, "tools": TOOLS}

    class FullBridge(PagelessBridge):
        async def _send(self, page, text, on_text=None):
            if text.startswith("# New harness events") and not getattr(self, "full", False):
                self.full = True
                raise cw.WebUnsent("the chat's context window is full")
            return await super()._send(page, text, on_text)

        async def _dump(self, page, probe, why):
            pass

    async def go():
        b = FullBridge(tmp_path, [CALL, "done"], reuse_chat=True, fresh_prompt_chars=40000, compact=True)
        d1 = await b.complete("luna", body)
        d2 = await b.complete("luna", follow_up(body, d1["choices"][0]["message"]))
        return b, d2

    b, d2 = asyncio.run(go())
    web = d2["pcore"]["web"]
    assert web["restarted"] and not web["continued"] and web["compaction"]
    assert "[Context note from provenance-core" in b.typed[-1] and len(b.typed[-1]) <= 40000
    assert "<<<tool result: exec_command>>>\na.py" in b.typed[-1]  # the newest events are there verbatim


def test_web_model_brief_is_written_before_the_window_is_taken(tmp_path):
    msgs = session(60)
    held = []

    async def summarize(system, digest, who, n):
        held.append(b.lock.locked())
        return NARRATIVE, "a/cheap"

    b = PagelessBridge(
        tmp_path, ["done"], fresh_prompt_chars=40000, compact={"model": "spark", "model_min_chars": 1000}
    )
    b.summarize = summarize
    d = asyncio.run(b.complete("luna", {"messages": msgs, "tools": TOOLS}))
    assert held == [False]  # the model was asked while the window was free
    assert d["pcore"]["web"]["compaction"]["writer"] == "model:a/cheap" and NARRATIVE in b.typed[0]


def test_web_too_few_turns_falls_back_to_the_blunt_cut(tmp_path):
    c = Convo()
    c.call("cat big.log", "q" * 100000)
    b = PagelessBridge(tmp_path, ["done"], fresh_prompt_chars=20000, compact=True)
    d = asyncio.run(b.complete("luna", {"messages": c.messages, "tools": TOOLS}))
    assert d["pcore"]["web"]["compacted"] and d["pcore"]["web"]["compaction"] is None and len(b.typed[0]) <= 20000


def test_web_takes_the_catalog_setting(tmp_path, monkeypatch):
    router, _ = make_router(tmp_path, monkeypatch, {"web": {"step": 2}}, web=True)
    assert compact.config("web", router.bridge("w").o["compact"])["step"] == 2
    assert router.bridge("w").compactor is router.compactor and router.bridge("w").summarize == router.summarize
    assert router.compact_options() is None


# ---- the blunt cut's fix: a result always keeps its command ----------------------------------------------------------------------------
def test_blunt_cut_keeps_every_result_with_its_call():
    c = Convo(task="Find the slow query.")
    for i in range(30):
        c.call(f"psql -c 'explain {i}'\nselect {i}", f"plan {i}\n" + "row\n" * 800)
    old = cw.compact_initial_prompt({"messages": c.messages, "tools": TOOLS}, 30000)
    prompt = old[0]
    assert len(prompt) <= 30000 and "Find the slow query." in prompt
    assert "<<<tool result: tool>>>" not in prompt  # no result is shown without its call
    shown = prompt.count("<<<tool result: exec_command>>>")
    assert shown and prompt.count("```exec_command") + prompt.count('"name": "exec_command"') >= shown
    assert "[... the rest of this earlier command omitted ...]" in prompt or "psql -c 'explain" in prompt


def test_short_call_arguments():
    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n" + "+x\n" * 200 + "*** End Patch"
    got = json.loads(cw.short_call_arguments(json.dumps({"input": patch})))
    assert got["input"].splitlines()[:2] == ["*** Begin Patch", "*** Update File: a.py"] and len(got["input"]) < 200
    got = json.loads(cw.short_call_arguments(json.dumps({"cmd": "echo hi\n" + "y" * 500, "workdir": CWD})))
    assert got["cmd"].startswith("echo hi [... the rest") and got["workdir"] == CWD
    assert json.loads(cw.short_call_arguments('{"cmd": "ls"}')) == {"cmd": "ls"}
