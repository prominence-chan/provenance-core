"""History pruning (provenance_core/prune.py): each rule, the structural guarantees (every call keeps its result,
recent turns untouched, nothing else changes), determinism, stability under growth, and where it is wired in (API
models in stable mode, the web bridge only on a fresh chat's first message, continuation unaffected)."""

import asyncio
import copy
import json
import random

import httpx
import pytest
import yaml

from provenance_core import chatgpt_web as cw
from provenance_core import prune
from provenance_core.catalog import load
from provenance_core.router import Router
from provenance_core.state import State

CWD = "/repo"


def header(code=0, tokens=10):
    return f"Chunk ID: ab12cd\nWall time: 0.0012 seconds\nProcess exited with code {code}\nOriginal token count: {tokens}\nOutput:\n"


class Convo:
    """Builds an OpenAI chat transcript the way responses_api turns a Codex session into one."""

    def __init__(self, task="Fix the parser."):
        self.messages = [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": f"<environment_context>\n  <cwd>{CWD}</cwd>\n</environment_context>"},
            {"role": "user", "content": task},
        ]
        self.n = 0

    def call(self, cmd, output, text=None, name="exec_command", code=0, raw=False, args=None):
        self.n += 1
        cid = f"call_{self.n}"
        a = args if args is not None else {"cmd": cmd, "workdir": CWD}
        self.messages.append(
            {
                "role": "assistant",
                "content": text,
                "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(a)}}],
            }
        )
        self.messages.append({"role": "tool", "tool_call_id": cid, "content": output if raw else header(code) + output})
        return self

    def say(self, text):
        self.messages.append({"role": "assistant", "content": text})
        return self

    def user(self, text):
        self.messages.append({"role": "user", "content": text})
        return self

    def pad(self, n=4):
        """n small unrelated turns, so what came before is out of the protected window."""
        for i in range(n):
            self.call(f"echo step {self.n}-{i}", f"step {self.n}-{i}\n")
        return self


def lines(prefix, n, width=40):
    return "".join(f"{prefix} line {i:03d} " + "x" * width + "\n" for i in range(n))


def tool_text(messages, call_id):
    return next(m["content"] for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == call_id)


def pruned(messages, mode="fresh", **o):
    out, rep = prune.prune_messages(messages, mode, o or None)
    return out, rep


# ---- settings ------------------------------------------------------------------------------------------------------
def test_off_by_default_and_switches(monkeypatch):
    monkeypatch.delenv("PCORE_PRUNE", raising=False)
    monkeypatch.delenv("TIERLM_PRUNE", raising=False)
    assert prune.config("api", None) is None and prune.config("web", False) is None
    assert prune.config("api", True)["keep_turns"] == prune.DEFAULTS["keep_turns"]
    assert prune.config("web", {"keep_turns": 6})["keep_turns"] == 6
    assert prune.config("api", {"api": True}) and prune.config("web", {"api": True}) is None
    assert prune.config("web", {"web": {"step": 3}})["step"] == 3
    assert prune.config("api", {"enabled": False}) is None
    monkeypatch.setenv("PCORE_PRUNE", "web")
    assert prune.config("web", None) and prune.config("api", True) is None
    monkeypatch.setenv("PCORE_PRUNE", "0")
    assert prune.config("web", True) is None
    monkeypatch.setenv("PCORE_PRUNE", "1")
    assert prune.config("api", None) and prune.config("web", {"web": {"step": 2}})["step"] == 2


# ---- rules -----------------------------------------------------------------------------------------------------------
def test_exec_header_keeps_the_exit_code_and_a_running_session():
    c = Convo().call("make", "built\n" * 3)
    c.call("npm run dev", "listening\n", raw=True)
    c.messages[-1]["content"] = (
        "Chunk ID: 1\nWall time: 10.0 seconds\nProcess running with session ID 4242\nOutput:\nlistening\n"
    )
    c.pad()
    out, rep = pruned(c.messages, rules=["exec_header"])
    assert tool_text(out, "call_1") == "Exit code: 0\n" + "built\n" * 3
    assert tool_text(out, "call_2") == "Process running with session ID 4242\nlistening\n"
    assert rep["rules"]["exec_header"]["count"] == 2
    assert tool_text(out, f"call_{c.n}") == tool_text(c.messages, f"call_{c.n}")  # recent: as sent


def test_identical_outputs_keep_only_the_last():
    status = "On branch main\nChanges not staged for commit:\n" + "".join(
        f"\tmodified:   src/mod{i}.py\n" for i in range(8)
    )
    c = Convo().call("git status", status).call("ls", "a\nb\n").call("git status", status).pad()
    out, rep = pruned(c.messages)
    assert tool_text(out, "call_1") == "Exit code: 0\n[… same output as a later exec_command call, omitted here …]"
    assert tool_text(out, "call_3").endswith(status)  # the later copy stays
    assert tool_text(out, "call_2") == "Exit code: 0\na\nb\n"  # too small to be worth a stub
    assert rep["rules"]["dup_output"] == {"count": 1, "saved": len(status) - len(prune._stub_dup("exec_command"))}


def test_a_slice_read_again_in_a_later_full_read_goes():
    whole = lines("parser.py", 60)
    part = "".join(whole.splitlines(True)[10:30])
    c = Convo().call("sed -n '11,30p' parser.py", part)
    c.call("rg -n TODO", "parser.py:3: TODO\n")
    c.call("cat parser.py", whole).pad()
    out, rep = pruned(c.messages)
    assert tool_text(out, "call_1") == "Exit code: 0\n[… 20 lines omitted: repeated in a later tool result …]\n"
    assert tool_text(out, "call_3").endswith(whole)
    assert rep["rules"]["repeated_lines"]["count"] == 1


def test_only_repeated_runs_go_and_what_changed_stays():
    old = lines("a.py", 30)
    new = old.replace("a.py line 015 ", "a.py line 015 CHANGED ")
    c = Convo().call("cat a.py", old).call("python fix.py", "ok\n").call("cat a.py", new).pad()
    out, _ = pruned(c.messages, rules=["repeated_lines"])
    first = tool_text(out, "call_1")
    assert "a.py line 015 " + "x" * 40 in first  # the line that changed is still there, in its old form
    assert first.count("omitted: repeated") == 2 and "a.py line 000" not in first
    # a short run (under min_run_chars) is not worth a marker
    out, _ = pruned(c.messages, rules=["repeated_lines"], min_run_chars=10_000)
    assert tool_text(out, "call_1") == tool_text(c.messages, "call_1")


def test_a_stub_always_points_at_something_kept():
    text = lines("same.py", 40)
    c = Convo()
    for _ in range(4):
        c.call("cat same.py", text)
    c.pad()
    out, _ = pruned(c.messages)
    kept = [m["content"] for m in out if m.get("role") == "tool" and text in m["content"]]
    assert len(kept) == 1 and kept[0] == tool_text(out, "call_4")


def test_stale_read_after_a_successful_patch_and_a_new_read():
    old = lines("cfg.py", 20)
    patch = "apply_patch <<'EOF'\n*** Begin Patch\n*** Update File: cfg.py\n@@\n-x\n+y\n*** End Patch\nEOF"
    new = lines("cfg2.py", 20)

    def convo(code):
        return (
            Convo()
            .call("sed -n '1,20p' cfg.py", old)
            .call(patch, "Success. Updated the following files:\nM cfg.py\n", code=code)
            .call("cat cfg.py", new)
            .pad()
        )

    out, rep = pruned(convo(0).messages)
    assert "earlier read of /repo/cfg.py omitted: the file was changed" in tool_text(out, "call_1")
    assert rep["rules"]["stale_read"]["count"] == 1
    out, rep = pruned(convo(1).messages)  # the patch failed: the old text is still the file's text
    assert tool_text(out, "call_1").endswith(old) and "stale_read" not in rep["rules"]
    # changed but never read whole again: the old read is all the model has
    c = Convo().call("sed -n '1,20p' cfg.py", old).call(patch, "Success.\n").call("sed -n '1,5p' cfg.py", "x\n").pad()
    assert tool_text(pruned(c.messages)[0], "call_1").endswith(old)
    # a "whole" read that the harness cut short did not show the whole file
    cut = new[:300] + "\n…4000 chars truncated…\n" + new[-300:]
    c = Convo().call("sed -n '1,20p' cfg.py", old).call(patch, "Success.\n").call("cat cfg.py", cut).pad()
    assert tool_text(pruned(c.messages)[0], "call_1").endswith(old)


def test_what_counts_as_a_read_or_a_write():
    def call(cmd, **kw):
        return prune.Call("exec_command", json.dumps({"cmd": cmd, **kw}), "/repo")

    assert call("cat src/a.py").read == ("/repo/src/a.py", True)
    assert call("cd sub && nl -ba a.py").read == ("/repo/sub/a.py", True)
    assert call("sed -n '5,40p' a.py", workdir="/w").read == ("/w/a.py", False)
    assert call("cat -n a.py").read == ("/repo/a.py", True)
    assert call("find . -name '*.py' | head -20").read is None and not call("find . | head -20").viewer
    assert call("cat a.py b.py").read is None and call("cat a.py b.py").viewer
    assert call("echo == && sed -n 1,9p a.py && rg x").viewer
    assert call("pytest -q").viewer is False and call("pytest -q").writes == []
    assert call("sed -i 's/a/b/' a.py").writes == ["/repo/a.py"]
    assert call("python gen.py > out.txt 2>/dev/null").writes == ["/repo/out.txt"]
    w = call("cat > a.py <<'EOF'\nx = 1 > 0\nprint('> b')\nEOF\npython a.py")
    assert w.full_writes == ["/repo/a.py"] and w.writes == ["/repo/a.py"]  # the heredoc's text is not commands
    p = call(
        "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: n.md\n+x\n*** Update File: m.py\n@@\n*** End Patch\nEOF"
    )
    assert p.writes == ["/repo/n.md", "/repo/m.py"] and p.full_writes == ["/repo/n.md"]
    assert prune.Call("Read", '{"file_path": "/x/a.py", "offset": 10}').read == ("/x/a.py", False)
    assert prune.Call("Write", '{"file_path": "b.py", "content": ""}', "/x").full_writes == ["/x/b.py"]


def test_a_read_of_a_file_rewritten_whole_is_stale_and_the_old_write_body_goes():
    v1, v2 = lines("app.ts v1", 30), lines("app.ts v2", 30)
    write = "cat > src/app.ts <<'EOF'\n{}EOF"
    c = Convo().call("cat src/app.ts", lines("app.ts v0", 30))
    c.call(write.format(v1), "").call("npm test", "1 failed\n").call(write.format(v2), "").pad()
    out, rep = pruned(c.messages)
    assert "earlier read of /repo/src/app.ts omitted" in tool_text(out, "call_1")
    args = [json.loads(m["tool_calls"][0]["function"]["arguments"]) for m in out if m.get("tool_calls")]
    assert args[1]["cmd"].startswith("cat > src/app.ts <<'EOF'\n[… 30 lines of file content omitted: /repo/src/app.ts")
    assert args[1]["cmd"].endswith("…]\nEOF") and args[1]["workdir"] == CWD
    assert args[3]["cmd"] == write.format(v2)  # the last write keeps its text
    assert rep["rules"]["old_write"]["count"] == 1 and rep["rules"]["stale_read"]["count"] == 1


def test_old_write_in_patches_and_write_tools():
    body = "".join(f"+line {i}\n" for i in range(20))
    add = "*** Begin Patch\n*** Add File: notes.md\n" + body + "*** End Patch"
    c = Convo().call(
        None, "Success. Updated the following files:\nA notes.md\n", name="apply_patch", raw=True, args={"input": add}
    )
    text = lines("x.txt", 12)
    c.call(None, "done", name="Write", raw=True, args={"file_path": "x.txt", "content": text})
    c.call("cat notes.md", body.replace("+", "")).call("cat /repo/x.txt", text).pad()
    out, rep = pruned(c.messages)
    a1, a2 = [json.loads(m["tool_calls"][0]["function"]["arguments"]) for m in out if m.get("tool_calls")][:2]
    assert (
        "*** Add File: notes.md\n+[… 20 lines of file content omitted" in a1["input"] and "+line 5" not in a1["input"]
    )
    assert a1["input"].endswith("*** End Patch")
    assert a2["file_path"] == "x.txt" and "13 lines of file content omitted: /repo/x.txt" in a2["content"]
    assert rep["rules"]["old_write"]["count"] == 2


def test_write_echo_keeps_the_summary():
    echo = "Success. Updated the following files:\nM src/a.py\n" + "".join(f"+ diff line {i}\n" for i in range(40))
    patch = "apply_patch <<'EOF'\n*** Begin Patch\n*** Update File: src/a.py\n@@\n-a\n+b\n*** End Patch\nEOF"
    c = Convo().call(patch, echo).pad()
    out, rep = pruned(c.messages)
    t = tool_text(out, "call_1")
    assert t.startswith("Exit code: 0\nSuccess. Updated the following files:\nM src/a.py\n[… ") and "diff line" not in t
    assert rep["rules"]["write_echo"]["count"] == 1


def test_long_output_keeps_head_and_tail_but_never_cuts_a_file_view():
    log = "".join(f"test_{i} PASSED\n" for i in range(800)) + "FAILED test_x: boom\n"
    big_file = lines("big.py", 300)
    c = Convo().call("pytest -q", log).call("echo == && sed -n '1,300p' big.py", big_file).pad()
    out, rep = pruned(c.messages)
    t = tool_text(out, "call_1")
    assert t.startswith("Exit code: 0\ntest_0 PASSED") and t.endswith("FAILED test_x: boom\n")
    assert "of old output omitted" in t and len(t) < 4500
    assert tool_text(out, "call_2").endswith(big_file)
    assert rep["rules"]["long_output"]["count"] == 1
    # in the recent turns, even a long log is as sent
    c = Convo().pad(2).call("pytest -q", log)
    assert pruned(c.messages)[0] == c.messages


def test_filler_is_recognised_conservatively():
    c = Convo()
    c.call("ls", "a\n", text="Let me look at the layout.")
    c.call("ls src", "b\n", text="Let me check whether the parser handles tabs, since the bug report mentions them.")
    c.call("ls tests", "c\n", text="I'll run the tests?")
    c.call("ls docs", "d\n", text="Now I'll open the config.")
    c.say("Let me think about this.")  # no call: a plain answer, kept
    c.pad()
    out, rep = pruned(c.messages)
    texts = [m.get("content") for m in out if m.get("role") == "assistant"]
    assert texts[:5] == [
        None,
        "Let me check whether the parser handles tabs, since the bug report mentions them.",
        "I'll run the tests?",
        None,
        "Let me think about this.",
    ]
    assert rep["rules"]["filler"]["count"] == 2


def test_repeated_instructions_in_the_system_message():
    block = "\n\n".join(f"Rule {i}: " + "be careful " * 20 for i in range(6))
    sysmsg = "Base prompt.\n\n" + block + "\n\n<collaboration_mode>default</collaboration_mode>\n\n" + block
    c = Convo()
    c.messages[0]["content"] = sysmsg
    out, rep = pruned(c.messages)
    s = out[0]["content"]
    assert s.startswith("Base prompt.\n\n" + block + "\n\n<collaboration_mode>")
    assert s.count("Rule 3:") == 1 and "repeating instructions given above omitted" in s
    assert rep["rules"]["dup_system"]["count"] == 1


def test_user_messages_and_content_parts_are_never_changed():
    text = lines("dup", 30)
    c = Convo(task=text).user([{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": "x"}}])
    c.call("cat f", text).call("cat f", text).pad()
    c.messages.append({"role": "tool", "tool_call_id": "call_1", "content": [{"type": "text", "text": text}]})
    out, _ = pruned(c.messages)
    assert [m for m in out if m["role"] == "user"] == [m for m in c.messages if m["role"] == "user"]
    assert out[-1] == c.messages[-1]


# ---- the structural guarantees, on a long synthetic agent run -----------------------------------------------------------
def agent_run(seed=1, turns=60):
    """A deterministic fake agent session: reads (whole and slices) of a few files that change, patches, rewrites,
    repeated git status, test logs, greps, narration."""
    rng = random.Random(seed)
    files = {f"src/m{i}.py": [f"m{i} def f{j}(): return {j}  # " + "y" * 30 for j in range(40)] for i in range(4)}
    c = Convo()
    for t in range(turns):
        f = rng.choice(sorted(files))
        k = rng.randrange(7)
        text = rng.choice([None, "Let me look.", "Now I'll check the tests.", "The helper looks wrong, fixing it."])
        if k == 0:
            c.call(f"cat {f}", "\n".join(files[f]) + "\n", text=text)
        elif k == 1:
            a = rng.randrange(30)
            c.call(f"sed -n '{a + 1},{a + 10}p' {f}", "\n".join(files[f][a : a + 10]) + "\n", text=text)
        elif k == 2:
            j = rng.randrange(40)
            files[f][j] = files[f][j].replace("return", f"return {t} +")
            p = f"apply_patch <<'EOF'\n*** Begin Patch\n*** Update File: {f}\n@@\n-old\n+new\n*** End Patch\nEOF"
            c.call(p, f"Success. Updated the following files:\nM {f}\n", text=text)
        elif k == 3:
            c.call("git status --short", "".join(f" M {x}\n" for x in sorted(files)) * 3, text=text)
        elif k == 4:
            c.call("pytest -q", "".join(f"t{i} ok\n" for i in range(700)) + f"{t % 3} failed\n", text=text)
        elif k == 5:
            body = "\n".join(files[f]) + "\n"
            c.call(f"cat > {f} <<'EOF'\n{body}EOF", "", text=text)
        else:
            c.call(f"rg -n f1 {f}", f"{f}:2:" + files[f][1] + "\n", text=text)
        if t % 17 == 16:
            c.user("keep going")
    return c.messages


def check_structure(before, after, keep_from):
    assert len(before) == len(after)
    for i, (a, b) in enumerate(zip(before, after, strict=True)):
        assert a["role"] == b["role"]
        assert a.get("tool_call_id") == b.get("tool_call_id")
        ids_a = [(tc["id"], tc["function"]["name"]) for tc in a.get("tool_calls") or []]
        ids_b = [(tc["id"], tc["function"]["name"]) for tc in b.get("tool_calls") or []]
        assert ids_a == ids_b
        for tc in b.get("tool_calls") or []:
            json.loads(tc["function"]["arguments"])  # still valid JSON
        if a["role"] == "user" or i >= keep_from:
            assert a == b, f"message {i} changed"
        assert prune.size([b]) <= prune.size([a])
    results = {m["tool_call_id"] for m in after if m["role"] == "tool"}
    assert all(tc["id"] in results for m in after for tc in m.get("tool_calls") or [])


@pytest.mark.parametrize("mode", ["fresh", "stable"])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_structure_is_kept_and_recent_turns_untouched(mode, seed):
    msgs = agent_run(seed)
    frozen = copy.deepcopy(msgs)
    out, rep = prune.prune_messages(msgs, mode)
    assert msgs == frozen  # the input is not changed
    asst = [i for i, m in enumerate(msgs) if m["role"] == "assistant"]
    assert rep["boundary"] <= asst[-prune.DEFAULTS["keep_turns"]]
    check_structure(msgs, out, rep["boundary"])
    assert rep["saved"] == rep["before"] - rep["after"] == sum(r["saved"] for r in rep["rules"].values()) > 0
    assert rep["after"] == prune.size(out)


def test_deterministic():
    msgs = agent_run(4)
    a = prune.prune_messages(msgs, "fresh")
    b = prune.prune_messages(copy.deepcopy(msgs), "fresh")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def requests_of(msgs):
    """Every request an agent makes in a session: the history up to each assistant turn."""
    return [msgs[:i] for i, m in enumerate(msgs) if m["role"] == "assistant"] + [msgs]


def test_stable_mode_prefix_is_unchanged_under_growth():
    msgs = agent_run(5, turns=80)
    o = {"keep_turns": 4, "step": 8}
    prev = None
    boundaries = []
    for req in requests_of(msgs):
        out, rep = prune.prune_messages(req, "stable", o)
        boundaries.append(rep["boundary"])
        if prev and prev[1] == rep["boundary"]:
            # same epoch: the earlier request's whole pruned history is the start of this one, byte for byte
            assert json.dumps(out[: len(prev[0])]) == json.dumps(prev[0])
        assert out[rep["boundary"] :] == req[rep["boundary"] :]  # past the boundary: as sent
        prev = (out, rep["boundary"])
    assert boundaries == sorted(boundaries)  # the boundary only moves forward
    assert 3 <= len(set(boundaries)) <= 80 // 8 + 1  # it moves once per `step` turns, not on every request


def test_stable_mode_prunes_the_frozen_part_on_its_own_evidence():
    """Whatever follows the boundary, even copies of every earlier result, the part before it prunes the same."""
    msgs = agent_run(6, turns=50)
    out, rep = prune.prune_messages(msgs, "stable")
    b = rep["boundary"]
    assert b and out[:b] != msgs[:b]
    earlier = [m["content"] for m in msgs[:b] if m["role"] == "tool"]
    tail = [{**m, "content": earlier[i % len(earlier)]} if m["role"] == "tool" else m for i, m in enumerate(msgs[b:])]
    out2, rep2 = prune.prune_messages(msgs[:b] + tail, "stable")
    assert rep2["boundary"] == b and out2[:b] == out[:b]
    fresh, _ = prune.prune_messages(msgs[:b] + tail, "fresh")
    assert fresh[:b] != out[:b]  # fresh mode does look past it (which is why only fresh prompts use it)


def test_fresh_mode_uses_the_recent_turns_as_evidence_stable_mode_does_not():
    text = lines("f.py", 30)
    c = Convo().call("cat f.py", text).pad(12).call("cat f.py", text).pad(1)
    fresh, _ = pruned(c.messages, "fresh")
    stable, _ = pruned(c.messages, "stable")
    assert "same output as a later" in tool_text(fresh, "call_1")
    assert tool_text(stable, "call_1").endswith(text)  # its copy is past the boundary: not evidence yet


def test_pruning_is_fast_on_a_big_history():
    import time

    msgs = agent_run(7, turns=400)
    t0 = time.perf_counter()
    prune.prune_messages(msgs, "fresh")
    assert time.perf_counter() - t0 < 2.0


# ---- wiring: API models ---------------------------------------------------------------------------------------------------
CAT = {
    "tiers": [{"name": "solstice", "min_score": 0}],
    "providers": {"a": {"base_url": "https://a.test/v1", "api_key_env": "A_KEY"}},
    "models": [{"provider": "a", "model": "big", "score": 90, "context": 1000000}],
}


def make_router(tmp_path, monkeypatch, prune_setting):
    monkeypatch.setenv("A_KEY", "x")
    monkeypatch.delenv("PCORE_PRUNE", raising=False)
    cat = dict(CAT, **({"prune": prune_setting} if prune_setting is not None else {}))
    p = tmp_path / "catalog.yaml"
    p.write_text(yaml.safe_dump(cat))
    seen = []

    def handler(req: httpx.Request):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    return Router(load(p), State(), httpx.AsyncClient(transport=httpx.MockTransport(handler))), seen


def test_api_models_get_the_stable_pruned_history_only_when_on(tmp_path, monkeypatch, capsys):
    msgs = agent_run(8, turns=40)
    router, seen = make_router(tmp_path, monkeypatch, None)
    asyncio.run(router.complete({"messages": msgs}, "solstice"))
    assert seen[-1]["messages"] == msgs  # off by default
    router, seen = make_router(tmp_path, monkeypatch, {"api": True})
    data, _ = asyncio.run(router.complete({"messages": msgs}, "solstice"))
    expect, rep = prune.prune_messages(msgs, "stable")
    assert seen[-1]["messages"] == expect and rep["saved"] > 0
    assert data["pcore"]["prune"]["saved"] == rep["saved"]
    assert "prune: a/big history saved" in capsys.readouterr().out
    # the next request of the same run (same epoch) starts with exactly what this one sent
    more = msgs + [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "z", "type": "function", "function": {"name": "exec_command", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "z", "content": "fine"},
    ]
    asyncio.run(router.complete({"messages": more}, "solstice"))
    assert prune.boundary(more, "stable") == rep["boundary"]
    assert seen[-1]["messages"][: len(expect)] == expect


# ---- wiring: the web bridge -----------------------------------------------------------------------------------------------
class FakePage:
    def is_closed(self):
        return False


class PagelessBridge(cw.Bridge):
    """The bridge's own session logic with the browser replaced: records what it would type."""

    def __init__(self, tmp_path, replies, **opts):
        super().__init__(
            "https://chatgpt.test", {"notify": False, "min_gap_s": 0, "max_gap_s": 0, **opts}, home=tmp_path
        )
        self.replies, self.typed = list(replies), []

    async def _ensure_page(self):
        self._page = self._page or FakePage()
        return self._page

    async def _new_page(self):
        return FakePage()

    async def _open_chat(self, model, page=None):
        return {}

    async def _probe(self, page, fresh):
        return {"temp": True}

    def _notices(self, p):
        pass

    async def _send(self, page, text, on_text=None):
        self.typed.append(text)
        return {"md": self.replies.pop(0), "slug": "gpt-test"}


def test_web_bridge_prunes_only_the_fresh_prompt_and_still_continues(tmp_path, monkeypatch):
    monkeypatch.delenv("PCORE_PRUNE", raising=False)
    msgs = agent_run(9, turns=30)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "exec_command",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "apply_patch",
                "parameters": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
            },
        },
    ]
    call = '```tool_call\n{"name": "exec_command", "arguments": {"cmd": "ls"}}\n```'

    async def go(**opts):
        b = PagelessBridge(tmp_path, [call, call, "done"], reuse_chat=True, fresh_prompt_chars=10**7, **opts)
        body = {"messages": msgs, "tools": tools}
        d1 = await b.complete("luna", body)
        reply = d1["choices"][0]["message"]
        follow = {
            **body,
            "messages": msgs
            + [reply, {"role": "tool", "tool_call_id": reply["tool_calls"][0]["id"], "content": "a.py\n"}],
        }
        d2 = await b.complete("luna", follow)
        return b, d1, d2

    b0, d1_off, _ = asyncio.run(go())
    b1, d1, d2 = asyncio.run(go(prune=True))
    assert d1_off["pcore"]["web"]["pruned"] is None
    rep = d1["pcore"]["web"]["pruned"]
    assert rep and rep["saved"] > 0 and rep["mode"] == "fresh"
    assert len(b1.typed[0]) < len(b0.typed[0])
    assert b1.typed[0] == cw.render_prompt(prune.prune_body({"messages": msgs, "tools": tools}, "fresh")[0])[0]
    # the second request continues the chat: only the new events are typed, as sent, same as with pruning off
    assert d2["pcore"]["web"]["continued"] and d2["pcore"]["web"]["pruned"] is None
    assert b1.typed[1] == b0.typed[1] and b1.typed[1].startswith("# New harness events")
    # and what the session compares against is the client's own history, not the pruned one
    key = next(iter(b1._sessions))
    convo = [m for m in msgs if m["role"] != "system"]
    assert b1._sessions[key]["seen"][: len(convo)] == convo


def test_web_bridge_takes_the_catalog_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("A_KEY", "x")
    monkeypatch.setenv("PCORE_CHATGPT_WEB", "1")
    cat = {
        **CAT,
        "prune": {"web": {"keep_turns": 6}},
        "providers": {
            **CAT["providers"],
            "w": {"type": "chatgpt_web", "base_url": "https://chatgpt.test", "api_key_env": "none"},
        },
    }
    p = tmp_path / "catalog.yaml"
    p.write_text(yaml.safe_dump(cat))
    router = Router(load(p), State())
    assert prune.config("web", router.bridge("w").o["prune"])["keep_turns"] == 6
    assert prune.config("api", router.catalog.prune) is None
