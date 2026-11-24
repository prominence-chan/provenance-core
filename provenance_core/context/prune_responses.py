"""prune.py's rules on an OpenAI Responses request that goes on to OpenAI itself (the Codex pass-through,
codex_sub.py), working on the `input` items directly.

The items are read into the chat-message view prune.py judges (an assistant turn per run of calls and assistant text,
a tool message per call output), the pass runs on that view in stable mode, and only what it changed is written back
into the original items. Nothing is converted back from chat: every item the pass did not change is the same object,
so encrypted reasoning, ids, statuses, phases and anything this module does not know pass through as they came.

What can change, exactly as prune.py judges it (its docstring has the rules):
- the text of an old tool output: function_call_output / custom_tool_call_output / local_shell_call_output whose
  `output` is a plain string, or a list of text parts only (code mode's "Script completed / Wall time / Output:"
  part plus the script's text parts), judged as one text (exec_header, stale_read, dup_output, repeated_lines,
  write_echo, long_output). A changed list keeps its first part when the new text still starts with it and gets the
  rest as one text part; an output with any other part (an image) is left as it is
- the file text inside an old whole-file write: a function_call's `arguments` or a custom_tool_call's `input`
  (old_write), unless that call carries encrypted content or encrypted arguments
What never changes: `instructions`, tools, every message (user, developer, system, assistant), reasoning items, ids,
call ids, names, the number and order of items, and call/output pairing. dup_system and filler (which edit developer
and assistant messages) are never used here.

Stable mode only: the part before an epoch boundary that moves every `step` assistant turns is pruned, judged on that
part alone, so consecutive requests of one thread start with the same items byte for byte between moves and OpenAI's
prompt cache keeps hitting. A request this module cannot read raises Unsupported; the caller forwards it unchanged."""

from __future__ import annotations

import json

from . import prune
from ..settings import scoped

# the rules that only touch tool outputs and old write calls
SAFE_RULES = tuple(r for r in prune.RULES if r not in ("dup_system", "filler"))
CALLS = ("function_call", "custom_tool_call", "local_shell_call")
OUTPUTS = ("function_call_output", "custom_tool_call_output", "local_shell_call_output")
SCOPES = ("api", "web", "codex")


class Unsupported(ValueError):
    """The request is not something this module understands: send it on as it is."""


def config(value) -> dict | None:
    """Options for the pass-through ("codex" scope), or None when it is off. `value` is the catalog's `prune`
    setting: {codex: true / false / options} decides; true / false / options without scopes apply here too; a dict
    with only other scopes ({web: true}) or no setting at all leaves it on with prune.py's defaults. PCORE_PRUNE=0 /
    off turns it off, PCORE_PRUNE=codex keeps only this one."""
    if isinstance(value, dict) and any(s in value for s in SCOPES):
        v = value.get("codex", True)
    else:
        v = True if value is None else value
    return scoped("PRUNE", "codex", v, prune.DEFAULTS, scopes=("codex",))


def _text(content):
    """Message content as one string when it is only text, else the content itself (which the pass leaves alone)."""
    if content is None or isinstance(content, str):
        return content or ""
    if not isinstance(content, list):
        raise Unsupported("message content")
    parts = []
    for p in content:
        if not isinstance(p, dict):
            raise Unsupported("content part")
        if p.get("type") not in ("input_text", "output_text", "text"):
            return content
        parts.append(p.get("text") if isinstance(p.get("text"), str) else "")
    return "\n".join(parts)


def to_messages(items: list) -> tuple[list[dict], list, list[int]]:
    """-> (chat messages for prune.py; per message where it came from: an item index for a tool message, a dict
    call id -> item index for an assistant turn, None otherwise; per message the index of its first item)."""
    if not isinstance(items, list):
        raise Unsupported("input is not a list of items")
    msgs: list[dict] = []
    refs: list = []
    starts: list[int] = []
    seen: set[str] = set()

    def turn(idx: int) -> dict:
        if msgs and msgs[-1]["role"] == "assistant":
            return msgs[-1]
        msgs.append({"role": "assistant", "content": None})
        refs.append({})
        starts.append(idx)
        return msgs[-1]

    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            raise Unsupported("item")
        t = it.get("type") or ("message" if "role" in it else None)
        if t == "message":
            role = it.get("role")
            if role == "assistant":
                m = turn(idx)
                text = _text(it.get("content"))
                if isinstance(text, str) and text:
                    m["content"] = (m["content"] or "") + text
            elif role in ("user", "developer", "system"):
                msgs.append({"role": role, "content": _text(it.get("content"))})
                refs.append(None)
                starts.append(idx)
            else:
                raise Unsupported(f"message role {role!r}")
        elif t in CALLS:
            call_id = it.get("call_id")
            if t == "local_shell_call" and not call_id:
                call_id = f"#item-{idx}"  # nothing can answer it: its output (if any) stays unpaired, untouched
            if not isinstance(call_id, str) or not call_id or call_id in seen:
                raise Unsupported("call id")
            seen.add(call_id)
            if t == "function_call":
                name, args = it.get("name"), it.get("arguments")
            elif t == "custom_tool_call":
                name, args = it.get("name"), json.dumps({"input": it.get("input")}, ensure_ascii=False)
            else:
                a = it.get("action") if isinstance(it.get("action"), dict) else {}
                name = "local_shell"
                args = json.dumps({"command": a.get("command"), "workdir": a.get("working_directory")})
            if not isinstance(name, str) or not isinstance(args, str):
                raise Unsupported("call")
            if t == "local_shell_call" or it.get("encrypted_content") or it.get("encrypted_function_args"):
                # still evidence (what it reads and writes), but old_write only edits string arguments: as parsed
                # arguments this call is never edited (a local shell call's argv can't take the note back)
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            m = turn(idx)
            m.setdefault("tool_calls", []).append(
                {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}
            )
            refs[-1][call_id] = idx
        elif t in OUTPUTS:
            out = it.get("output")
            if _text_parts(out):
                out = "".join(p["text"] for p in out)
            msgs.append({"role": "tool", "tool_call_id": it.get("call_id"), "content": out if out is not None else ""})
            refs.append(idx)
            starts.append(idx)
        # reasoning, web_search_call, compaction, tool_search_*, additional_tools, ...: not part of the view
    return msgs, refs, starts


def _text_parts(out) -> bool:
    """A structured output made of text parts only (what code mode returns for a script without images)."""
    return (
        isinstance(out, list)
        and bool(out)
        and all(isinstance(p, dict) and p.get("type") == "input_text" and isinstance(p.get("text"), str) for p in out)
    )


def _parts_with(parts: list[dict], text: str) -> list[dict]:
    """A text-parts output carrying `text`: its first part kept when the text still starts with it (code mode's
    status header), the rest as one text part."""
    first = parts[0]["text"]
    if len(parts) > 1 and text.startswith(first) and len(text) > len(first):
        return [parts[0], {**parts[1], "text": text[len(first) :]}]
    return [{**parts[0], "text": text}]


def _write_back(items: list, before: list[dict], after: list[dict], refs: list) -> list:
    """The items with the pass's changes in place: a new list, changed items copied, the rest the same objects."""
    new = None
    for i, (a, b) in enumerate(zip(before, after, strict=True)):
        if a["role"] == "tool":
            if isinstance(b.get("content"), str) and isinstance(a.get("content"), str) and b["content"] != a["content"]:
                new = new or list(items)
                it = items[refs[i]]
                out = _parts_with(it["output"], b["content"]) if _text_parts(it.get("output")) else b["content"]
                new[refs[i]] = {**it, "output": out}
        elif a["role"] == "assistant" and a.get("tool_calls"):
            for ta, tb in zip(a["tool_calls"], b.get("tool_calls") or [], strict=True):
                old, now = ta["function"]["arguments"], tb["function"]["arguments"]
                if old == now or not isinstance(now, str):
                    continue
                idx = refs[i][ta["id"]]
                it = items[idx]
                if it["type"] == "function_call":
                    changed = {**it, "arguments": now}
                elif it["type"] == "custom_tool_call":
                    v = json.loads(now).get("input")
                    if not isinstance(v, str):
                        continue
                    changed = {**it, "input": v}
                else:
                    continue
                new = new or list(items)
                new[idx] = changed
    return items if new is None else new


def _chars(items: list) -> int:
    return len(json.dumps(items, ensure_ascii=False, separators=(",", ":")))


def prune_request(body: dict, options: dict) -> tuple[dict, dict]:
    """A Responses request body with its old tool outputs pruned (a new dict when anything changed, else `body`
    itself) and the report: {"before", "after", "saved"} in characters of the serialized `input`, "boundary" (the
    index of the first item left as sent), "rules" (prune.py's per-rule counts)."""
    if not isinstance(body, dict):
        raise Unsupported("body")
    items = body.get("input")
    msgs, refs, starts = to_messages(items)
    o = {**prune.DEFAULTS, **(options or {})}
    o["rules"] = [r for r in o.get("rules") or () if r in SAFE_RULES]
    out, rep = prune.prune_messages(msgs, "stable", o)
    new_items = _write_back(items, msgs, out, refs)
    cut = rep["boundary"]
    before = _chars(items)
    after = before if new_items is items else _chars(new_items)
    report = {
        "before": before,
        "after": after,
        "saved": before - after,
        "boundary": starts[cut] if cut else 0,  # the first item the pass left as sent
        "rules": rep["rules"],
    }
    return (body if new_items is items else {**body, "input": new_items}), report
