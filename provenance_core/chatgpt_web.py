"""ChatGPT web as a provider (`type: chatgpt_web` in the catalog): your own signed-in chatgpt.com chat.
The harness owns tools and memory. A request starts a temporary chat or safely continues its own tab, and the
reply is parsed back into text and tool calls.

How it stays honest and gentle (read docs/TECHNICAL.md before turning it on):
- a real, visible Chrome window on its own profile (~/.tierlm/chatgpt-web-profile) that you sign into by hand once
  (`provenance-core weblogin`); nothing is read from any other browser profile. No password is handled, unless you
  turn on automatic sign-in for an account and save its sign-in yourself (web_signin.py: the password then lives in
  the system's credential store only, and is typed into the site's own sign-in form when the site has signed the
  account out)
- the page is driven like a user would: open, type, press send, read the answer. No forged sentinel /
  proof-of-work / Turnstile tokens, no captcha solving or clicking, no calls to the private backend API
- two drivers. `playwright` (default) is stock: the window says it is automated, and chatgpt.com answers it with a
  browser check that never clears. `patchright` (opt-in: `driver: patchright` or PCORE_CHATGPT_WEB_DRIVER=patchright;
  the approach CatGPT-Gateway uses) drives the same Chrome and profile without the automation markers, so the
  automatic check passes as it does for you. That is hiding the automation from the site: your call, your account
- a challenge, captcha, "unusual activity" notice, login wall, usage-limit banner or a missing temporary-chat mode
  marks the provider unavailable (with a reason) and the router fails over to its normal pool
- one request at a time, a human-scale pause between requests, a daily cap, off unless enabled
- the prompt is written for a chat model in a chat app (it has its own sandbox and no function-calling interface for
  the harness tools): see "Prompt design" in docs/TECHNICAL.md

Automating the web app is against OpenAI's terms of use; the realistic downside is the ChatGPT account being
limited or banned. That is why this is opt-in and deliberately slow."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import secrets
import subprocess
import time
from pathlib import Path

from . import compact, cookie_guard, prune, settings
from .settings import env


# ---- errors ---------------------------------------------------------------------------------------------------------
class WebError(RuntimeError):
    """This request failed (counts as a model failure; the router tries the next model)."""


class WebSkip(WebError):
    """The bridge can't take this request (busy, prompt too long, images): no health penalty."""


class WebTooLong(WebError):
    """The page's message box took only part of the message; `limit` is a length it would take."""

    def __init__(self, msg: str, limit: int):
        super().__init__(msg)
        self.limit = limit


class WebUnsent(WebError):
    """The chat would not take the message (its context window is full, or the page ignored the send). Nothing was
    sent, so the same request can go to a fresh chat."""


STARTED = time.monotonic()  # when this process loaded the bridge


class WebUnavailable(WebError):
    """The provider is out until `cooldown` seconds pass (or the login is redone): login, challenge, limit, ..."""

    def __init__(self, reason: str, cooldown: float, kind: str):
        super().__init__(reason)
        self.reason, self.cooldown, self.kind = reason, cooldown, kind


class Malformed(ValueError):
    """The reply contains a tool call that can't be used as written."""


# ---- the text protocol: request -> one prompt -----------------------------------------------------------------------
# The prompt is written for a chat model sitting in a consumer chat app, which is a different reader from the API model
# a harness prompt assumes (docs/TECHNICAL.md, "Prompt design"):
# - it has working tools of its own (a code sandbox, search) and reaches for them: say plainly where those run and that
#   the user's files are not there, instead of only forbidding them
# - the harness prompt speaks of "function calls" and may name tools this session doesn't have: say how to read it
# - tools are shown the way OpenAI shows function tools to its models (TypeScript signatures), not as JSON schema
# - a command is written raw in a block named after the tool, so heredocs and patches need no JSON escaping
# - the rules that matter are repeated after the conversation, next to where the reply starts
FRAME = """You are operating an interactive terminal on my computer through this chat. A program on my machine \
runs every tool block you write and sends back its output as the next message; nothing else you do reaches my \
computer.
- You know nothing about this machine, its files, projects, processes or anything "local" or "mine" except what \
appears in this conversation. Find out by running commands, never from memory or by guessing.
- A tool block is plain text in your reply. It will not appear in your own tool list and does not need to: writing \
the block is the call, and it always works.
- Do not use web search, browsing, Python / code execution, file analysis, canvas, memory or image tools. They run \
in a sandbox on OpenAI's servers, not on my computer: a path like /Users/... does not exist there, so whatever \
they find (or fail to find) is wrong, and it wastes the turn. If the task really needs the internet, fetch it with \
a command (curl) on my machine.
- Do not ask me whether to go on, search further or retry: just do it with the next block.
- Think and plan as much as you need, then write every independent call this turn's tool rules allow, and stop. \
Never write a tool's output yourself or claim something you have not seen in a tool result.
- A failed command is information: try another way. A reply without a tool block ends the run, so only do that \
when the task is done or you need a decision from me.
Write the next assistant turn, without a role marker."""

INSTRUCTIONS_HEAD = """# Agent instructions
Where these say to call a function, write a tool block. Only tools listed below exist%s."""

TOOLS_HEAD = """# Tools
Call a tool by writing this block in your reply (it need not appear in your native tool list):

```tool_call
{"name": "<tool name>", "arguments": {<arguments>}}
```

- Use valid JSON (escape newlines inside strings).
- %s
- Text after your last tool block is discarded."""

RAW_HEAD = """Shorthand for %s: a block tagged with the tool's name whose body is the raw %s, exactly as it should \
be used (no JSON, no escaping, may span lines). Prefer it for anything with quotes or several lines:

```%s
%s
```

To pass other arguments as well, use the tool_call form."""

REMINDER_TOOLS = """Next turn: act only through ```tool_call JSON%s on my computer (no web search or other built-in
tools). Stop after the call blocks and wait for results. If the task is complete, answer without a block."""

FRAME_PLAIN = """This message was put together by a program on my computer, which will read your reply and pass it on. Below are its instructions and the conversation so far. Write the assistant's next turn. Do not use your built-in tools (code execution, web search, canvas, memory, image generation) and do not mention this framing in your answer."""

# The chat page eats curly braces written outside a code block (a reply "RUN: python3 -c 'p=[{...}]'" arrives as
# "RUN: python3 -c 'p=["; its own Copy button gives the same), and likewise text between < and > ("NR <= 75 ... NR >"
# vanishes like a tag), so literal text travels in a fence tagged raw, which parse_reply takes off again.
BRACES = (
    " This chat page changes literal text that is outside a code block: it drops anything in curly braces or "
    "between < and >, and can turn a written \\n into a line break. Text inside any fenced code block is safe. "
    "So when a line that is NOT already in a code block must arrive character for character (a command, JSON, a "
    "path pattern), put just that line or those lines in a fenced block tagged raw (```raw ... ```); the program "
    "removes that fence and keeps the text. Do not put raw around other code blocks or around the whole turn."
)
REMINDER_PLAIN = "Now write the assistant's next turn (only the turn's content, no <<<assistant>>> line)." + BRACES

TOOL_TAGS = ("tool_call", "tool-call", "toolcall", "tool_calls", "tool")
SHELL_TOOLS = ("exec_command", "shell_command", "shell", "local_shell", "container.exec")


def _text(content) -> str:
    """Message content as text. Images can't be passed through the page: the request is skipped."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out = []
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type") in ("image_url", "input_image", "image"):
            raise WebSkip("images are not supported by the chatgpt-web bridge")
        out.append(p.get("text", ""))
    return "".join(out)


def raw_arg(schema: dict) -> str | None:
    """The argument a tool's shorthand block carries: a freeform tool's `input` or a shell tool's command string
    (exec_command's `cmd`), i.e. the one required argument when it is free text. None for every other tool."""
    props = (schema or {}).get("properties") or {}
    req = (schema or {}).get("required") or (list(props) if len(props) == 1 else [])
    if (
        len(req) == 1
        and req[0] in ("cmd", "command", "input", "script")
        and (props.get(req[0]) or {}).get("type") == "string"
    ):
        return req[0]
    return None


def call_block(name: str, arguments, tools: dict | None = None) -> str:
    """A tool call as the model should have written it (used to show its own earlier calls back to it)."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            arguments = {"raw_arguments": arguments}
    key = raw_arg((tools or {}).get(name) or {}) if tools and name in tools else None
    if key and isinstance(arguments, dict) and list(arguments) == [key] and isinstance(arguments[key], str):
        body = arguments[key]
        if body.strip() and ("\n" in body or '"' in body or "\\" in body):  # where the shorthand reads better than JSON
            ticks = max([3] + [len(x) + 1 for x in re.findall(r"`{3,}", body)])
            return "`" * ticks + name + "\n" + body.strip("\n") + "\n" + "`" * ticks
    return "```tool_call\n" + json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False) + "\n```"


def ts_type(schema, indent: str = "") -> str:
    """A JSON schema as a TypeScript type, the way OpenAI renders function tools into a model's prompt."""
    if not isinstance(schema, dict):
        return "any"
    if "enum" in schema:
        return " | ".join(json.dumps(v, ensure_ascii=False) for v in schema["enum"]) or "never"
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    for k in ("anyOf", "oneOf"):
        if isinstance(schema.get(k), list):
            return " | ".join(dict.fromkeys(ts_type(x, indent) for x in schema[k])) or "any"
    t = schema.get("type")
    if isinstance(t, list):
        return " | ".join(dict.fromkeys(ts_type({**schema, "type": x}, indent) for x in t))
    if t in ("integer", "number"):
        return "number"
    if t in ("string", "boolean", "null"):
        return t
    if t == "array":
        inner = ts_type(schema.get("items") or {}, indent)
        return f"({inner})[]" if " | " in inner else inner + "[]"
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        if not props:
            extra = schema.get("additionalProperties")
            return "{[key: string]: %s}" % ts_type(extra, indent) if isinstance(extra, dict) else "{}"
        req, lines, pad = set(schema.get("required") or []), [], indent + "  "
        for k, v in props.items():
            v = v if isinstance(v, dict) else {}
            notes = [str(v.get("description") or "").strip().replace("\n", " ")[:160]] if v.get("description") else []
            if "default" in v:
                notes.append("default: " + json.dumps(v["default"], ensure_ascii=False))
            lines += [f"{pad}// {x}" for x in notes]
            key = k if re.fullmatch(r"[A-Za-z_$][\w$]*", k) else json.dumps(k)
            lines.append(f"{pad}{key}{'' if k in req else '?'}: {ts_type(v, pad)},")
        return "{\n" + "\n".join(lines) + "\n" + indent + "}"
    return "any"


def tool_decl(name: str, desc: str, schema: dict) -> str:
    # The native tool documentation can dwarf the task; retain the call shape and a short purpose.
    head = "// " + " ".join((desc or "").split())[:400] if desc else ""
    ident = name if re.fullmatch(r"[A-Za-z_$][\w$]*", name) else json.dumps(name)
    return (head + "\n" if head else "") + f"type {ident} = (_: {ts_type(schema)}) => any;"


def render_prompt(body: dict) -> tuple[str, dict[str, dict], dict]:
    """An OpenAI chat body -> (the one message to type, {tool name: parameters schema}, rules for the parser:
    {"single": at most one call, "must": a call is required, "only": the one tool that must be called})."""
    tools: dict[str, dict] = {}
    descs: dict[str, str] = {}
    choice = body.get("tool_choice")
    for t in body.get("tools") or []:
        fn = t.get("function") or {}
        if fn.get("name") and choice != "none":
            tools[fn["name"]] = fn.get("parameters") or {"type": "object", "properties": {}}
            descs[fn["name"]] = fn.get("description") or ""
    rules = {
        "single": body.get("parallel_tool_calls") is False or bool(body.get("_single_tool_call")),
        "must": False,
        "only": None,
    }
    if tools and choice == "required":
        rules["must"] = True
    elif tools and isinstance(choice, dict) and (choice.get("function") or {}).get("name") in tools:
        rules["must"], rules["only"] = True, choice["function"]["name"]

    raw = {n: raw_arg(s) for n, s in tools.items() if raw_arg(s)}
    shell = next((n for n in SHELL_TOOLS if n in raw), None)
    system = "\n\n".join(
        _text(m.get("content"))
        for m in body.get("messages") or []
        if m.get("role") in ("system", "developer") and m.get("content")
    )
    parts = [FRAME if tools else FRAME_PLAIN]
    if system.strip():
        patch = ""
        if tools and shell and "apply_patch" in system and "apply_patch" not in tools:
            patch = (
                f" (in particular, there is no separate apply_patch tool here: to edit a file, run `apply_patch` "
                f"as a shell command through {shell}, with the patch in a heredoc)"
            )
        head = INSTRUCTIONS_HEAD % patch if tools else "# Instructions"
        parts.append(head + "\n\n" + system.strip())
    if tools:
        many = (
            "At most one tool block per turn."
            if rules["single"]
            else "One block per call; several blocks call several tools in one turn."
        )
        sec = [TOOLS_HEAD % many]
        if raw:
            ex = shell or next(iter(raw))
            names = ", ".join(f"`{n}` (body = `{k}`)" for n, k in raw.items())
            sample = (
                "apply_patch <<'EOF'\n*** Begin Patch\n*** Update File: src/app.py\n@@\n-old line\n+new line\n"
                "*** End Patch\nEOF"
                if ex == shell and "apply_patch" in system
                else 'rg -n "TODO" src | head -20'
                if ex == shell
                else f"<the {raw[ex]} text>"
            )
            sec.append(RAW_HEAD % (names, "text of that argument", ex, sample))
        if rules["only"]:
            sec.append(f"In this turn you must call the tool `{rules['only']}`.")
        elif rules["must"]:
            sec.append("In this turn you must call a tool.")
        sec.append(
            "The tools, as TypeScript signatures (`_` is the arguments object, `?` marks an optional argument):\n\n"
            + "\n\n".join(tool_decl(n, descs[n], s) for n, s in tools.items())
        )
        parts.append("\n\n".join(sec))
    fmt = body.get("response_format") or {}
    if fmt.get("type") == "json_schema":
        parts.append(
            "# Output format\nReply with one JSON value only (no prose, no code fence) matching this schema: "
            + json.dumps((fmt.get("json_schema") or {}).get("schema") or {}, separators=(",", ":"))
        )
    elif fmt.get("type") == "json_object":
        parts.append("# Output format\nReply with one JSON object only (no prose, no code fence).")

    convo, names = [], {}
    for m in body.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"):
            continue
        if role == "assistant":
            segs = [_text(m.get("content")).strip()]
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                names[tc.get("id")] = fn.get("name", "")
                segs.append(call_block(fn.get("name", ""), fn.get("arguments"), tools))
            convo.append("<<<assistant>>>\n" + "\n".join(s for s in segs if s))
        elif role == "tool":
            name = names.get(m.get("tool_call_id")) or m.get("name") or "tool"
            convo.append(f"<<<tool result: {name}>>>\n" + _text(m.get("content")))
        else:
            convo.append("<<<user>>>\n" + _text(m.get("content")))
    parts.append(
        "# Conversation\nEach turn starts with a <<<role>>> line. <<<user>>> is me (or context the harness adds "
        "for me), <<<assistant>>> is you, <<<tool result>>> is what a tool returned on my computer.\n\n"
        + "\n\n".join(convo)
    )
    if tools:
        how = (
            f", or the ```{shell} shorthand for a command"
            if shell
            else f", or the ```{next(iter(raw))} shorthand"
            if raw
            else ""
        )
        parts.append(REMINDER_TOOLS % how)
    else:
        parts.append(REMINDER_PLAIN)
    return "\n\n".join(parts), tools, rules


LEAN_SYSTEM = """You are a coding agent working in a terminal on my machine, inside the given working directory. Be precise and efficient: look before you change, keep changes minimal and in the style of the code around them. Before you answer: run what you made the way the task says it will be used, including each failure case the task itself names (an interrupt, a missing argument, an extreme input; where it names none, invent none), and say what you ran and what you did not check. Copy any input file before a tool that may change it opens it (sqlite3, an editor, a formatter), and prefer read-only modes. Check your result against every size, count or shape the task states, and do not hard-code what the task says is unknown. Search with `rg`. Edit files with `apply_patch`, run as a shell command with the patch in a heredoc ("*** Begin Patch", then "*** Update File: <path>" with "@@" hunks of " ", "-" and "+" lines, or "*** Add File: <path>" with "+" lines, or "*** Delete File: <path>", then "*** End Patch"). Never ask for approval, never run destructive git commands, and leave files unrelated to the task alone. End with a short final answer in the format the task asks for."""
LEAN_DROP_TOOLS = ("request_user_input", "view_image", "get_goal", "create_goal", "update_goal", "update_plan")


def lean_body(body: dict, system_chars: int = 3000, drop_tools=LEAN_DROP_TOOLS) -> dict:
    """A harness request cut down for a web chat: the harness's long base prompt (Codex sends ~18,000 characters of
    it, written for its own API models) becomes a few lines, its skills list goes, the environment block keeps only
    the working directory, shell and date, and tools an unattended run never needs are not offered. The same
    request always comes out the same, so a continued chat still lines up."""
    out, replaced = [], False
    for m in body.get("messages") or []:
        role, text = m.get("role"), _text(m.get("content"))
        if role in ("system", "developer"):
            if "<skills_instructions>" in text:
                continue
            if len(text) > system_chars:
                if not replaced:
                    out.append({"role": role, "content": LEAN_SYSTEM})
                    replaced = True
                continue
        elif role == "user" and text.lstrip().startswith("<environment_context>"):
            got = {k: re.search(rf"<{k}>(.*?)</{k}>", text, re.S) for k in ("cwd", "shell", "current_date")}
            if got["cwd"]:
                line = ", ".join(f"{k}: {v.group(1).strip()}" for k, v in got.items() if v)
                m = {
                    **m,
                    "content": f"<environment_context>{line}; writes are allowed in cwd and /tmp</environment_context>",
                }
        out.append(m)
    tools = [t for t in body.get("tools") or [] if (t.get("function") or {}).get("name") not in drop_tools]
    return {**body, "messages": out, **({"tools": tools} if body.get("tools") is not None else {})}


def continuation_key(body: dict, model: str) -> str | None:
    """Identify a conversation by everything said before the model's first turn (agents started in the same
    directory share their first, environment message); matching the full prefix still gates reuse."""
    opening = []
    for m in body.get("messages") or []:
        if m.get("role") == "assistant":
            break
        if m.get("role") not in ("system", "developer"):
            opening.append(_text(m.get("content")))
    if not opening:
        return None
    seed = json.dumps([model, opening], sort_keys=True, default=str)
    return hashlib.sha256(seed.encode()).hexdigest()


def _message_shape(m: dict) -> str:
    """Ignore call IDs, which can be regenerated by a Responses client on resume."""
    calls = [
        ((tc.get("function") or {}).get("name"), (tc.get("function") or {}).get("arguments"))
        for tc in m.get("tool_calls") or []
    ]
    return json.dumps([m.get("role"), _text(m.get("content")), calls], sort_keys=True, default=str)


def continuation_prompt(messages: list[dict], tools: dict[str, dict], prior: dict) -> str:
    """Send only new harness events; the earlier instructions and tool calls are already in this chat tab."""
    names = {tc.get("id"): (tc.get("function") or {}).get("name") for tc in prior.get("tool_calls") or []}
    parts = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            blocks = [_text(m.get("content")).strip()]
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                names[tc.get("id")] = fn.get("name")
                blocks.append(call_block(fn.get("name", ""), fn.get("arguments"), tools))
            parts.append("<<<assistant>>>\n" + "\n".join(x for x in blocks if x))
        elif role == "tool":
            name = names.get(m.get("tool_call_id")) or m.get("name") or "tool"
            parts.append(f"<<<tool result: {name}>>>\n" + _text(m.get("content")))
        elif role == "user":
            parts.append("<<<user>>>\n" + _text(m.get("content")))
    return "# New harness events\n" + "\n\n".join(parts) + "\n\n" + (REMINDER_TOOLS % "" if tools else REMINDER_PLAIN)


def short_call_arguments(arguments) -> str:
    """An old tool call's arguments cut down to what it did: a command's first line, a patch's file lines; short
    values stay as they are. The call keeps its name and id, so its result still has a call to belong to."""
    try:
        a = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        return json.dumps({"raw_arguments": str(arguments)[:200]}, ensure_ascii=False)
    if not isinstance(a, dict):
        return json.dumps(a, ensure_ascii=False)[:300]

    def cut(v):
        if isinstance(v, str) and len(v) > 240:
            if "*** Begin Patch" in v:
                files = [
                    x for x in v.split("\n") if x.startswith(("*** Add File", "*** Update File", "*** Delete File"))
                ]
                return "\n".join(["*** Begin Patch", *files, "[... the rest of this earlier patch omitted ...]"])
            first = v.strip().split("\n")[0]
            return first[:200] + " [... the rest of this earlier command omitted ...]"
        if isinstance(v, list) and len(json.dumps(v)) > 300:
            return [cut(x) for x in v]
        return v

    return json.dumps({k: cut(v) for k, v in a.items()}, ensure_ascii=False)


def compact_initial_prompt(body: dict, limit: int) -> tuple[str, dict[str, dict], dict, bool]:
    """Bound an old transcript on a fresh tab, preserving instructions, tools, task and recent evidence. The blunt
    cut (no summary; see compact.py for the one that summarises): old outputs and calls are shortened, then whole
    old turns go. A call and its results are only ever removed together, so every result shown keeps its command."""
    prompt, tools, rules = render_prompt(body)
    if len(prompt) <= limit:
        return prompt, tools, rules, False
    messages = [dict(m) for m in body.get("messages") or []]
    first_asst = next((i for i, m in enumerate(messages) if m.get("role") == "assistant"), len(messages))
    opening = {i for i, m in enumerate(messages[:first_asst]) if m.get("role") == "user"}  # the task: kept as sent
    older = [i for i, m in enumerate(messages) if m.get("role") not in ("system", "developer")][:-8]

    def render():
        return render_prompt({**body, "messages": [m for m in messages if m is not None]})[0]

    for i in older:
        m = messages[i]
        if m.get("role") == "tool":
            s = _text(m.get("content"))
            if len(s) > 800:
                m["content"] = s[:400] + f"\n[... {len(s) - 800} older characters omitted ...]\n" + s[-400:]
        elif m.get("role") == "assistant":
            s = _text(m.get("content"))
            if len(s) > 600:
                m["content"] = s[:300] + "\n[... earlier reasoning omitted ...]\n" + s[-300:]
            if m.get("tool_calls"):
                m["tool_calls"] = [
                    {**tc, "function": {**(tc.get("function") or {}), "arguments": short_call_arguments((tc.get("function") or {}).get("arguments"))}}
                    for tc in m["tool_calls"]
                ]  # fmt: skip
        elif m.get("role") == "user" and i not in opening:
            s = _text(m.get("content"))
            if len(s) > 1200:
                m["content"] = s[:600] + "\n[... earlier user text omitted ...]\n" + s[-600:]
    prompt = render()
    if len(prompt) > limit:
        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        for i in tool_indices[:-2]:
            s = _text(messages[i].get("content"))
            if len(s) > 1200:
                messages[i]["content"] = s[:600] + "\n[... tool output shortened ...]\n" + s[-600:]
        for i in tool_indices[-2:]:
            s = _text(messages[i].get("content"))
            if len(s) > 8000:
                messages[i]["content"] = s[:4000] + "\n[... recent tool output shortened ...]\n" + s[-4000:]
        prompt = render()
    if len(prompt) > limit:
        # Keep the initial task and recent turns verbatim. The harness has already had these older events;
        # the current model needs the latest evidence more than raw output from the start of the run. A turn goes
        # whole: the assistant message with the results of its calls (only when all of them are old).
        old, groups, at = set(older) - opening, [], {}
        for i in sorted(old):
            m = messages[i]
            if m.get("role") == "tool" and m.get("tool_call_id") in at:
                groups[at[m["tool_call_id"]]].append(i)
                continue
            groups.append([i])
            for tc in m.get("tool_calls") or [] if m.get("role") == "assistant" else []:
                at[tc.get("id")] = len(groups) - 1
        kept_results = {
            m.get("tool_call_id") for i, m in enumerate(messages) if m.get("role") == "tool" and i not in old
        }
        removable = [
            g for g in groups if not any(tc.get("id") in kept_results for tc in messages[g[0]].get("tool_calls") or [])
        ]
        for n, g in enumerate(removable):
            for k, i in enumerate(g):
                messages[i] = (
                    {"role": "user", "content": "[earlier harness events omitted to fit the chat page]"}
                    if n == 0 and k == 0
                    else None
                )
            prompt = render()
            if len(prompt) <= limit:
                break
    return prompt, tools, rules, True


def _web_budget(fresh: dict, limit: int) -> int:
    """What the conversation may take of a fresh prompt of `limit` characters: the rest goes to the framing,
    instructions and tool list, rendered here once without any conversation."""
    bare = {**fresh, "messages": [m for m in fresh.get("messages") or [] if m.get("role") in ("system", "developer")]}
    return limit - len(render_prompt(bare)[0]) - 200


def _web_size(m: dict) -> int:
    return 0 if m.get("role") in ("system", "developer") else compact.rendered_size(m)


def summarised_prompt(
    body: dict, fresh: dict, limit: int, opts: dict, compactor: compact.Compactor
) -> tuple[str, dict[str, dict], dict, dict | None]:
    """A fresh chat's first message with the old part of the history as a brief (compact.py) instead of the blunt
    cut: `body` is the request as sent (the brief is made from it), `fresh` the same after pruning (what is kept
    verbatim comes from it). Never waits on a model: a model's narrative is used only when it is already cached
    (Bridge.warm). If the brief cannot be made (too few turns) or the result is still too long, the blunt cut has
    the last word, so the prompt is never over `limit` because of this. -> (prompt, tools, rules, the record)."""
    raw, view = body.get("messages") or [], fresh.get("messages") or []
    budget = _web_budget(fresh, limit)
    rep = None
    for _ in range(3):
        msgs, rep = compactor.compact(raw, view, budget, opts, _web_size, "web")
        if rep is None:
            break
        prompt, tools, rules = render_prompt({**fresh, "messages": msgs})
        if len(prompt) <= limit:
            return prompt, tools, rules, rep
        budget -= len(prompt) - limit + 500  # _web_size was short of the real rendering: try again with less
    prompt, tools, rules, _ = compact_initial_prompt({**fresh, "messages": msgs if rep else view}, limit)
    if rep:
        rep["blunt_cut_after"] = True
    return prompt, tools, rules, rep


# ---- the text protocol: reply -> text + tool calls ------------------------------------------------------------------
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([\w+#.-]*)[^\n]*\n(.*?)\n[ \t]*\1[ \t]*$", re.S | re.M)


_RAW = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*raw[ \t]*\n(.*?)\n[ \t]*\1[ \t]*$", re.S | re.M | re.I)
_FENCE_LINE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([\w+#.:/-]*)[^\n]*$")


def unraw(text: str) -> str:
    """Take off the fences tagged raw, keeping their content. A model sometimes wraps other code blocks (or its
    whole turn) in one: a tagged fence line inside opens a nested block and the next bare fence closes that one,
    so the raw block ends at ITS closing fence and the inner blocks stay whole."""
    out, in_raw, depth = [], False, 0
    for line in text.split("\n"):
        m = _FENCE_LINE.match(line)
        if not in_raw:
            if m and m.group(2).lower() == "raw":
                in_raw, depth = True, 0
                continue
            out.append(line)
            continue
        if m:
            if m.group(2):  # ```bash inside the raw block
                depth += 1
            elif depth:  # the bare fence closing that inner block
                depth -= 1
            else:  # the raw block's own closing fence
                in_raw = False
                continue
        out.append(line)
    return "\n".join(out)


_TURN = re.compile(r"^[ \t]*<<<(?:user|assistant|tool result[^>\n]*)>>>[ \t]*$", re.M)


def _loads(s: str):
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return json.loads(s, strict=False)  # raw newlines / tabs inside strings


def _as_call(obj, tools: dict) -> dict | None:
    """A JSON value as a tool call {name, arguments}, or None when it isn't shaped like one."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):  # {"type": "function", "function": {"name", "arguments"}}
        obj = obj["function"]
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    args = next((obj[k] for k in ("arguments", "args", "parameters", "input") if k in obj), None)
    if not isinstance(name, str) or (args is None and name not in tools):
        return None
    return {"name": name, "arguments": {} if args is None else args}


def _check(call: dict, tools: dict) -> dict:
    name, args = call["name"], call["arguments"]
    if name not in tools:
        raise Malformed(f"unknown tool {name!r}; the tools are: {', '.join(tools)}")
    if isinstance(args, str):  # arguments given as a JSON string
        try:
            args = _loads(args)
        except json.JSONDecodeError:
            props = tools[name].get("properties") or {}
            if len(props) != 1:
                raise Malformed(f"arguments of {name} must be a JSON object") from None
            args = {next(iter(props)): args}  # a single-string tool given its raw string
    if not isinstance(args, dict):
        raise Malformed(f"arguments of {name} must be a JSON object")
    missing = [k for k in tools[name].get("required") or [] if k not in args]
    if missing:
        raise Malformed(f"tool {name} is missing required argument(s): {', '.join(missing)}")
    return {"id": "call_" + secrets.token_hex(12), "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


def parse_reply(md: str, tools: dict, rules: dict | None = None) -> tuple[str, list[dict]]:
    """The reply (as markdown) -> (text, [{id, name, arguments (JSON str)}]). Raises Malformed when a tool call is
    there but unusable, or when one was required and none came."""
    rules = rules or {}
    md = re.sub(r"\A\s*<<<assistant>>>[ \t]*\n", "", md)
    m = _TURN.search(md)
    if m:  # the model kept writing the transcript (the next turn, or a made-up tool result): its turn ends there
        md = md[: m.start()]
    calls, keep, pos = [], [], 0
    shorthand = {n.lower(): (n, raw_arg(sc)) for n, sc in tools.items() if raw_arg(sc)}
    if tools:
        for m in _FENCE.finditer(md):
            lang, inner = m.group(2).lower(), m.group(3)
            short = shorthand.get(lang)
            if short:  # ```exec_command: the body is the raw argument (or, from a careful model, JSON after all)
                name, key = short
                args = None
                if inner.lstrip().startswith("{"):
                    try:
                        obj = _loads(inner)
                        c = _as_call(obj, tools) if isinstance(obj, dict) else None
                        if c and c["name"] == name and isinstance(c["arguments"], dict):
                            args = c["arguments"]
                        elif (
                            isinstance(obj, dict)
                            and key in obj
                            and set(obj) <= set(tools[name].get("properties") or {})
                        ):
                            args = obj
                    except json.JSONDecodeError:
                        pass
                if args is None:
                    if not inner.strip():
                        raise Malformed(f"the {name} block is empty")
                    args = {key: inner}
                calls.append(_check({"name": name, "arguments": args}, tools))
                keep.append(md[pos : m.start()])
                pos = m.end()
                continue
            tagged = lang in TOOL_TAGS
            if not tagged and lang not in ("", "json", "jsonc", "plaintext", "text"):
                continue
            try:
                obj = _loads(inner)
            except json.JSONDecodeError as e:
                if tagged:
                    raise Malformed(
                        f"a tool_call block is not valid JSON ({e.msg} at line {e.lineno} column {e.colno})"
                    ) from None
                continue
            found = [_as_call(o, tools) for o in (obj if isinstance(obj, list) else [obj])]
            if not tagged and not all(c and c["name"] in tools for c in found):
                continue  # an ordinary json block, not a call
            if not found or not all(found):
                raise Malformed('a tool_call block must be {"name": ..., "arguments": {...}}')
            calls += [_check(c, tools) for c in found]
            keep.append(md[pos : m.start()])
            pos = m.end()
    # with a call, the turn is what came before the last block: anything after it is not the model's to write
    text = "".join(keep).strip() if calls else md.strip()
    if tools and not calls:
        # a call written without its fence: the page renders it as prose, so the JSON may be mangled
        m = re.search(r"(?:^|\n)[ \t]*(?:tool_call|tool call)\b[^\n{]*\n?[ \t]*(\{)", md, re.I)
        if m or re.search(r'^\s*\{\s*"name"\s*:\s*"(%s)"' % "|".join(map(re.escape, tools)), md):
            start = m.start(1) if m else md.index("{")
            try:
                obj, end = json.JSONDecoder(strict=False).raw_decode(md[start:])
                c = _as_call(obj, tools)
                if not c:
                    raise ValueError
            except ValueError:
                raise Malformed(
                    "the tool call must be inside a ```tool_call fenced block containing valid JSON"
                ) from None
            calls = [_check(c, tools)]
            text = md[: m.start() if m else start].strip()
    text = unraw(text)  # the fence asked for around literal text (BRACES)
    if rules.get("only") and any(c["name"] != rules["only"] for c in calls):
        raise Malformed(f"this turn must call the tool {rules['only']}")
    if rules.get("must") and not calls:
        raise Malformed(
            "this turn must contain a tool_call block" + (f" for {rules['only']}" if rules.get("only") else "")
        )
    if rules.get("single"):
        calls = calls[:1]
    return text, calls


def repair_message(err: Malformed) -> str:
    return (
        f"The program could not use that reply: {err}.\nSend the whole turn again, corrected. A tool call is a "
        'fenced code block tagged tool_call holding one valid JSON object {"name": ..., "arguments": {...}} '
        "(newlines inside strings written as \\n), or the shorthand block tagged with the tool's name. It runs "
        "on my computer; do not use your built-in tools instead."
    )


# A turn with no call that isn't an answer either. Two kinds seen from the web model: it looked for the tools in its
# own function-calling interface ("the shell isn't available to me"), or it wrote what it would do next and stopped
# ("I'll use find instead. Please retry so I can continue."). Either would end the harness's run with nothing done,
# so it gets one more message in the same chat first.
_STALL = re.compile(
    r"\b(?:is|are)(?: not|n['’]t) (?:currently |yet )?(?:available|enabled|accessible|exposed|provided|connected)\b"
    r"|\b(?:do not|don['’]t|did not|didn['’]t) have (?:access|a way|the ability|any tools?|a tool|tools|a shell|the tools?)\b"
    r"|\b(?:can['’]?t|cannot|unable to|not able to) (?:directly )?(?:run|execute|access|inspect|reach|open|read|call|use|see|modify|edit)\b"
    r"|\b(?:retry|try again|re-?run)\b[^.\n]{0,60}\b(?:enabled|available|tools?)\b"
    r"|\bplease (?:retry|re-?run|run|try again|execute|paste|share|provide|send)\b"
    r"|\bso (?:that )?I can (?:continue|proceed)\b"
    r"|\b(?:would you like|do you want) me to\b|\b(?:should|shall) I (?:re-?run|retry|search|continue|proceed|go ahead|try|look|check|run)\b"
    r"|\b(?:could not|couldn['’]t|did not|didn['’]t|was unable to|wasn['’]t able to) (?:find|locate|see|access)\b"
    r"|\bno longer (?:accessible|available)\b"
    r"|\bI(?:['’]ll| will| am going to|['’]m going to)(?: \w+){0,2} (?:use|run|inspect|check|look|read|open|start|begin|apply|"
    r"patch|create|add|implement|update|fix|search|list|examine|proceed|continue|try|write|edit|verify|test|reproduce)\b"
    r"|\b(?:next|now),? I(?:['’]ll| will)\b|\blet me (?:\w+ )?(?:run|check|inspect|look|read|try|start|open)\b"
    # its own sandbox's tool failed, so it reports that nothing happened ("the local command execution interface
    # returned an error before confirming that either file was written. No successful changes are verified.")
    r"|\bI(?:['’]m| am) ready to (?:create|write|run|make|apply|edit|start|begin|implement|test|proceed)\b"
    # a "blocked" report that blames the tools: it tried its own code container or function calling, not a tool block
    # ("Terminal tool-call interface rejected the command", "returned a container ServerError", "Local terminal
    # access is unavailable, and the available tools cannot write to the specified machine")
    r"|\b(?:tool-?call(?:ing)? interface|terminal|command execution|local execution|execution attempt|container)\b"
    r"[^.\n]{0,60}\b(?:rejected|refused|blocked|unavailable|returned an? (?:\w+ )?(?:Server)?Error|ServerError)\b"
    r"|\b(?:terminal|shell|command|filesystem|file-?editing)(?: \w+){0,3} (?:is|are|was|were) (?:unavailable|inaccessible|blocked)\b"
    r"|\bcannot (?:write|read|reach|connect) (?:to|from) (?:the|your) (?:specified |target |local )?(?:machine|computer|filesystem)\b"
    r"|\bno access to (?:the|your) (?:target |local )?(?:machine|computer|filesystem|terminal)\b"
    r"|\bServerError\b"
    # a step announced as under way, with nothing run ("I’m correcting that and rerunning the checks.")
    r"|\bI(?:['’]m| am)(?: now| just| also)? (?:correcting|fixing|re-?running|running|updating|applying|writing|"
    r"creating|changing|editing|checking|verifying|testing|patching|implementing|adding|removing|replacing|"
    r"inspecting|reading|looking|continuing|proceeding|starting|switching|trying|rewriting|adjusting|investigating)\b"
    r"|\b(?:interface|tool|command|execution|environment|sandbox|shell|session)\b[^.\n]{0,40}\b(?:returned|gave|hit|threw|"
    r"raised|produced|reported|encountered) an? (?:\w+ )?error\b"
    r"|\bno (?:successful |file |code )?(?:changes?|edits?|files?|writes?) (?:are|were|was|have been|has been) "
    r"(?:verified|made|written|applied|confirmed|saved)\b",
    re.I,
)
STALL_MAX = 1500  # longer than this is a real answer that happens to mention a limitation or a next step


def stalled(text: str, tools: dict) -> bool:
    return bool(tools) and len(text) <= STALL_MAX and bool(_STALL.search(text))


def nudge_message(tools: dict) -> str:
    raw = {n: raw_arg(s) for n, s in tools.items() if raw_arg(s)}
    shell = next((n for n in SHELL_TOOLS if n in raw), None)
    how = (
        f"```{shell}\n<the command>\n```" if shell else '```tool_call\n{"name": "<tool name>", "arguments": {...}}\n```'
    )
    return (
        "That reply had no tool block, so nothing ran, and sent as it is it would end the agent's run. Nobody "
        "else runs commands for you here. A tool call is just text in your reply (it does not need your "
        "function-calling interface) and my program runs it on my computer. Anything you looked up with your own "
        "Python, file or search tool came from OpenAI's sandbox, not from my computer, so it does not count. If "
        "there is a next step, write the "
        f"turn again and take it now by ending the turn with a block like this:\n\n{how}\n\n"
        "A command that failed is a reason to try another way, not to stop. If you really are finished, or need "
        "a decision from me, repeat your answer instead."
    )


def chat_completion(model_id: str, text: str, calls: list[dict], prompt: str, extra: dict | None = None) -> dict:
    msg: dict = {"role": "assistant", "content": text or None}
    if calls:
        msg["tool_calls"] = [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
            for c in calls
        ]
    out = len(text) // 4 + sum(len(c["arguments"]) for c in calls) // 4
    return {
        "id": "chatcmpl-web-" + secrets.token_hex(10),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": out, "total_tokens": len(prompt) // 4 + out},
        "pcore": {"web": extra or {}},
    }


class BufferedStream:
    """A finished completion replayed as an OpenAI chat SSE stream (same surface as the httpx response the router
    hands out for real providers). The web reply is read whole before anything is sent on: tool calls have to be
    parsed (and maybe repaired) first, and a failure can still fail over to another model."""

    def __init__(self, data: dict):
        ch = data["choices"][0]
        msg = ch["message"]
        base = {"id": data["id"], "object": "chat.completion.chunk", "created": data["created"], "model": data["model"]}
        chunks = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}]
        if msg.get("content"):
            chunks.append(
                {**base, "choices": [{"index": 0, "delta": {"content": msg["content"]}, "finish_reason": None}]}
            )
        for i, tc in enumerate(msg.get("tool_calls") or []):
            chunks.append(
                {
                    **base,
                    "choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, **tc}]}, "finish_reason": None}],
                }
            )
        chunks.append(
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": ch["finish_reason"]}],
                "usage": data["usage"],
            }
        )
        self.lines = ["data: " + json.dumps(c) for c in chunks] + ["data: [DONE]"]
        self.status_code = 200
        self.headers: dict = {}

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    async def aiter_raw(self):
        for line in self.lines:
            yield (line + "\n\n").encode()

    async def aclose(self):
        return None


class LiveStream:
    """The same surface as BufferedStream, but the reply's prose goes out while the page is still writing it. Only
    whole paragraphs before the first code block are sent early (a tool block has to be complete to be parsed); the
    rest of the text and the tool calls follow when the reply is finished. `first()` waits for the first text or
    the end, so a request that fails before anything was said can still go to another model."""

    def __init__(self, start, model_id: str, on_done=None):
        self.base = {
            "id": "chatcmpl-web-" + secrets.token_hex(10),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_id,
        }
        self.q: asyncio.Queue = asyncio.Queue()
        self.sent = ""
        self.on_done = on_done
        self.status_code = 200
        self.headers: dict = {}
        self._head = None
        self.task = asyncio.create_task(self._run(start))

    async def _run(self, start):
        try:
            self.q.put_nowait(("done", await start(self._on_text)))
        except BaseException as e:  # noqa: BLE001 - handed to the reader
            self.q.put_nowait(("error", e))

    def _on_text(self, piece: str):
        if piece:
            self.sent += piece
            self.q.put_nowait(("text", piece))

    async def first(self):
        self._head = await self.q.get()
        if self._head[0] == "error":
            raise self._head[1]
        return self

    def _chunk(self, delta: dict, finish=None, **more) -> str:
        return "data: " + json.dumps(
            {**self.base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **more}
        )

    async def aiter_lines(self):
        yield self._chunk({"role": "assistant"})
        item, self._head = self._head or await self.q.get(), None
        while True:
            kind, val = item
            if kind == "text":
                yield self._chunk({"content": val})
            elif kind == "error":
                yield "data: " + json.dumps({"error": {"message": str(val)[:500]}})
                return
            else:
                ch = val["choices"][0]
                text = ch["message"].get("content") or ""
                rest = text[len(self.sent) :] if text.startswith(self.sent) else ("" if self.sent else text)
                if rest:
                    yield self._chunk({"content": rest})
                for i, tc in enumerate(ch["message"].get("tool_calls") or []):
                    yield self._chunk({"tool_calls": [{"index": i, **tc}]})
                yield self._chunk({}, ch["finish_reason"], usage=val["usage"])
                if self.on_done:  # before the last line: the reader stops at [DONE] and never comes back
                    self.on_done(val)
                yield "data: [DONE]"
                return
            item = await self.q.get()

    async def aiter_raw(self):
        async for line in self.aiter_lines():
            yield (line + "\n\n").encode()

    async def aclose(self):
        if not self.task.done():
            self.task.cancel()


# ---- the page, as a user sees it ------------------------------------------------------------------------------------
# One probe of the visible page. `fresh` = no conversation on it yet, so the whole page text may be scanned for
# notices; later only alerts / dialogs / toasts outside the messages are read (a reply may itself talk about limits).
PROBE_JS = r"""(fresh) => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                      return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const first = sels => { for (const s of sels) for (const el of document.querySelectorAll(s)) if (vis(el)) return el; return null; };
  const out = {url: location.href, title: document.title};
  out.composer = !!first(['#prompt-textarea', 'div[contenteditable][role="textbox"]', 'textarea[name="prompt"]', 'main textarea']);
  let login = !!first(['[data-testid="login-button"]', '[data-testid="signup-button"]']);
  if (!login) for (const el of document.querySelectorAll('button, a')) {
    const t = (el.innerText || '').trim().toLowerCase();
    if ((t === 'log in' || t === 'sign up' || t === 'sign up for free') && vis(el)) { login = true; break; }
  }
  if (/(^|\.)auth\.openai\.com$|(^|\.)auth0\./.test(location.host) || /^\/(auth\/|log-?in)/.test(location.pathname)) login = true;
  out.login = login;
  out.account = !!first(['[data-testid="accounts-profile-button"]', '[data-testid="profile-button"]',
                         '[data-testid="user-menu-button"]', 'button[aria-label*="profile" i]', 'button[aria-label*="account" i]']);
  const scoped = [...document.querySelectorAll('[role="alert"], [role="alertdialog"], [role="dialog"], [data-testid*="error"], [data-testid*="toast"], [class*="text-token-text-error"]')]
    .filter(vis).filter(el => !el.closest('[data-message-author-role], [data-turn-key]')).map(el => el.innerText || '').join('\n');
  // the whole page is read for notices only while it holds no conversation: a chat still on screen (the tab's last
  // one, before the new chat has loaded) may itself say "try again later"
  const empty = !document.querySelector('[data-turn-key], [data-message-author-role], [data-response-body], [data-dil-response]');
  // ...and never the sidebar: it lists the account's saved chats by title (anything: "Usage limits design") and
  // has its own "Unable to load history / Retry", which says nothing about whether a chat can be sent
  let whole = fresh && empty && document.body ? document.body.innerText : '';
  if (whole) for (const el of document.querySelectorAll('nav, aside, #stage-slideover-sidebar, [data-testid*="sidebar"], [class*="sidebar"]')) {
    const t = (el.innerText || '').trim(); if (t) whole = whole.split(t).join('\n');
  }
  const text = (whole ? whole + '\n' : '') + scoped + '\n' + (document.title.length <= 80 ? document.title : '');  // a chat's title is its first message: never read that for notices
  const m = (re, s) => { const x = (s === undefined ? text : s).match(re); return x ? x[0] : null; };
  out.challenge = m(/just a moment|verify (?:that )?you are (?:a )?human|unusual activity|suspicious activity|confirm you(?:'|’)re (?:not a robot|human)|are you a robot|complete the captcha|checking your browser|you have been blocked|access denied/i);
  if (!out.challenge) for (const f of document.querySelectorAll('iframe')) {
    const src = (f.getAttribute('src') || '') + ' ' + (f.getAttribute('title') || '');
    const r = f.getBoundingClientRect();
    if (/challenges\.cloudflare\.com|arkoselabs|funcaptcha|hcaptcha|recaptcha|challenge/i.test(src) && vis(f) && r.width > 120 && r.height > 40) { out.challenge = 'captcha frame'; break; }
  }
  out.busy = m(/temporar(?:y|ily)[^.\n]{0,80}limit[^.\n]{0,80}|limit[^.\n]{0,60}temporar(?:y|ily)[^.\n]{0,60}|(?:unable|failed|couldn(?:'|’)t) to load[^.\n]{0,60}|try again (?:later|in a (?:few|couple)[^.\n]{0,20})|at capacity|experiencing (?:high|heavy) demand/i);
  if (out.busy) { const at = text.indexOf(out.busy); out.busyContext = text.slice(Math.max(0, at - 220), at + out.busy.length + 120).replace(/\s+/g, ' ').trim(); }
  out.limit = out.busy ? null : m(/you(?:'|’)ve (?:reached|hit) (?:your|the|our)[^.\n]{0,80}limit|you(?:'|’)re out of[^.\n]{0,60}|too many requests|usage cap|limit reached/i);
  out.error = m(/something went wrong|error generating|network error|unable to load|message (?:is )?too long|(?:too long|exceeds)[^.\n]{0,40}(?:message|limit)|conversation not found|an error occurred/i, scoped);
  // the page's own error in place of a reply ("The message you submitted was too long, please edit it and
  // resubmit." / "Something went wrong"): a block with a Retry button inside the turn, outside any message text
  out.turnError = null;
  if (!fresh) for (const b of document.querySelectorAll('main button')) {
    if ((b.innerText || '').trim() !== 'Retry' || !vis(b) || b.closest('[data-message-author-role], .markdown')) continue;
    let box = b; for (let i = 0; i < 4 && box.parentElement; i++) { box = box.parentElement; if ((box.innerText || '').trim().length > 12) break; }
    out.turnError = (box.innerText || '').replace(/\bRetry\b/g, '').replace(/\s+/g, ' ').trim().slice(0, 200) || 'error';
  }
  // "Context window full - Start a new chat with an automatic summary ...": shown beside the composer, not in a reply
  out.full = null;
  if (!fresh && document.body) {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      if (!/context window (?:is )?full|conversation (?:is|has gotten) too long|reached the maximum (?:length|context)/i.test(n.nodeValue)) continue;
      const p = n.parentElement;
      if (p && vis(p) && !p.closest('[data-turn-key], [data-message-author-role], article, textarea, [contenteditable]')) { out.full = n.nodeValue.trim().slice(0, 160); break; }
    }
  }
  let temp = null;
  for (const el of document.querySelectorAll('[aria-label]')) {
    const a = (el.getAttribute('aria-label') || '').toLowerCase();
    if (!a.includes('temporary') || !vis(el)) continue;
    if (a.includes('turn off') || el.getAttribute('aria-pressed') === 'true' || el.getAttribute('aria-checked') === 'true') temp = true;
    else if (temp === null && a.includes('turn on')) temp = false;
  }
  if (temp !== true && document.body) {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      if (!/^\s*temporary chat\s*$/i.test(n.nodeValue)) continue;
      const p = n.parentElement;
      if (p && vis(p) && !p.closest('[data-message-author-role], button, [role="menu"], [role="menuitem"], [role="tooltip"]')) { temp = true; break; }
    }
  }
  out.temp = temp;
  // two generations of markup: [data-message-author-role] turns, and (2026-11) [data-turn-key] turns whose parts are
  // [data-content-search-unit-key="<turn>:<n>:user|assistant"] with the reply in [data-response-body]
  const turnStates = [...document.querySelectorAll('[data-talvt-turn-state]')].map(e => e.getAttribute('data-talvt-turn-state'));
  out.stop = !!first(['[data-testid="stop-button"]', 'button[aria-label="Stop streaming"]', 'button[aria-label="Stop generating"]', 'button[aria-label="Stop"]'])
             || turnStates[turnStates.length - 1] === 'in_progress';
  const send = first(['[data-testid="send-button"]', 'button[aria-label="Send prompt"]', 'button[aria-label="Send message"]', 'button[aria-label="Send"]']);
  out.send = !!send;
  out.sendEnabled = !!send && !send.disabled && send.getAttribute('aria-disabled') !== 'true';
  const msgs = [...document.querySelectorAll('[data-message-author-role]')];
  // a reply is [data-dil-response] while it is being written and gains [data-response-body] when finished
  const replies = () => { const r = [...document.querySelectorAll('[data-response-body], [data-dil-response]')];
                          return r.filter(e => !r.some(o => o !== e && o.contains(e))); };
  out.users = msgs.filter(e => e.getAttribute('data-message-author-role') === 'user').length
              + document.querySelectorAll('[data-content-search-unit-key$=":user"]').length;
  const as = msgs.filter(e => e.getAttribute('data-message-author-role') === 'assistant').concat(replies());
  out.assistants = as.length;
  const last = as[as.length - 1];
  out.lastLen = last ? (last.innerText || '').length : 0;
  out.slug = last ? last.getAttribute('data-message-model-slug') : null;
  // Which message is last, by its own id: a long chat keeps only the recent turns in the page, so counts stop growing.
  const us = [...document.querySelectorAll('[data-message-author-role="user"], [data-content-search-unit-key$=":user"]')];
  const lu = us[us.length - 1];
  out.lastUser = lu ? (lu.getAttribute('data-content-search-unit-key') || lu.getAttribute('data-message-id') || null) : null;
  out.lastAsst = last ? (last.getAttribute('data-response-body') || last.getAttribute('data-dil-message-id') || last.getAttribute('data-message-id') || null) : null;
  out.asstAfterUser = !!(last && lu && (lu.compareDocumentPosition(last) & Node.DOCUMENT_POSITION_FOLLOWING));
  out.lastState = turnStates[turnStates.length - 1] || null;
  out.actions = document.querySelectorAll('[data-testid="copy-turn-action-button"], [data-talvt-turn-state] button[aria-label="Copy"]').length;
  const sw = first(['[data-testid="model-switcher-dropdown-button"]', 'button[aria-label^="Model selector" i]', 'button[aria-haspopup="menu"][aria-label*="model" i]']);
  out.model = sw ? ((sw.innerText || '').trim() || sw.getAttribute('aria-label') || '').replace(/\s+/g, ' ') : null;
  return out;
}"""

# The assistant messages after the last user message, turned back into markdown. Code blocks come back exactly
# (their text content); the rest is rebuilt from the rendered elements.
READ_JS = r"""(cfg) => {
  // both generations of markup (see PROBE_JS): everything the assistant wrote after the last user message
  const role = e => e.getAttribute('data-message-author-role') || (e.hasAttribute('data-response-body') || e.hasAttribute('data-dil-response') ? 'assistant' : 'user');
  const found = [...document.querySelectorAll('[data-message-author-role], [data-content-search-unit-key$=":user"], [data-response-body], [data-dil-response]')];
  const all = found.filter(e => role(e) !== 'assistant' || !found.some(o => o !== e && role(o) === 'assistant' && o.contains(e)));
  let lastUser = -1;
  all.forEach((e, i) => { if (role(e) === 'user') lastUser = i; });
  let as = all.slice(lastUser + 1).filter(e => role(e) === 'assistant');
  if (cfg) {   // another chat site (see SITES): its own selectors for the messages
    const us = [...document.querySelectorAll(cfg.user)], lu = us[us.length - 1];
    as = [...document.querySelectorAll(cfg.assistant)].filter(e => !lu || (lu.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING));
  }
  const boxed = el => {   // a site's code block: a box with a language label, buttons, and the code in pre
    const code = el.querySelector('pre code') || el.querySelector('pre');
    const tagged = el.querySelector('[data-lang]');
    let lang = tagged ? tagged.getAttribute('data-lang') || '' : ((el.querySelector('[class*="language-"]') || el).className.match(/language-([\w+#.-]+)/) || [])[1] || '';
    if (!lang && cfg.codeLang) { const l = el.querySelector(cfg.codeLang); if (l) lang = (l.textContent || '').trim().toLowerCase(); }
    if (/^(code|text|plain|plaintext|code snippet)$/.test(lang) || /\s/.test(lang)) lang = '';
    const lines = code ? code.querySelectorAll(cfg.codeLine || '.never') : [];
    const text = (lines.length ? [...lines].map(l => l.textContent.replace(/\n$/, '')).join('\n') : (code ? code.textContent : el.innerText)).replace(/\n$/, '');
    const f = fence(text, 3);
    return f + lang + '\n' + text + '\n' + f;
  };
  const isWidget = n => n.nodeType === 1 && n.getAttribute('data-client-defined-widget') === 'code_block';
  const isMath = n => n.nodeType === 1 && n.getAttribute('data-d-component') === 'math';
  function widget(el) {   // the 2026-11 code block: an editor widget; the code itself is kept verbatim in an attribute
    const holder = el.querySelector('[data-chatgpt-copy-code]');
    const ed = el.querySelector('[data-language]');
    let text = holder ? holder.getAttribute('data-chatgpt-copy-code') : null;
    const body = ed || el.querySelector('[role="textbox"], [contenteditable="true"]');   // one child per line
    if (text === null && body) text = body.children.length ? [...body.children].map(l => l.textContent).join('\n') : body.textContent;
    if (text === null) { const c = el.cloneNode(true); c.querySelectorAll('[data-markdown-copy="exclude"], button').forEach(x => x.remove()); text = c.textContent; }
    let lang = ed ? (ed.getAttribute('data-language') || '') : '';
    const label = el.querySelector('[data-markdown-copy="exclude"] div');
    const shown = label ? (label.textContent || '').trim() : '';
    if ((!lang || /^(text|plain|plaintext)$/i.test(lang)) && shown.length <= 24 && /^[\w+#.-]+$/.test(shown)) lang = shown.toLowerCase();
    if (/^(code|text|plain|plaintext)$/.test(lang)) lang = '';   // the header's label for a block with no language
    text = text.replace(/\n$/, '');
    const f = fence(text, 3);
    return f + lang + '\n' + text + '\n' + f;
  }
  const fence = (s, min) => { let n = min; for (const x of (s.match(/`+/g) || [])) n = Math.max(n, x.length + 1); return '`'.repeat(n); };
  const BLOCK = new Set(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre', 'ul', 'ol', 'blockquote', 'table', 'hr', 'div', 'section', 'article', 'details', 'li']);
  const SKIP = new Set(['button', 'svg', 'style', 'script', 'noscript']);
  const tex = el => { const a = el.querySelector('annotation'); return a ? a.textContent.trim() : el.textContent; };
  function inl(node) {
    if (node.nodeType === 3) return node.nodeValue;
    if (node.nodeType !== 1) return '';
    const t = node.tagName.toLowerCase();
    if (SKIP.has(t)) return '';
    if (node.classList.contains('katex-display')) return '$$' + tex(node) + '$$';
    if (node.classList.contains('katex')) return '$' + tex(node) + '$';
    if (isMath(node)) return '$' + tex(node) + '$';
    if (isWidget(node)) return '\n' + widget(node) + '\n';
    if (t === 'br') return '\n';
    if (t === 'pre') return '\n' + pre(node) + '\n';
    if (t === 'code') { const s = node.textContent, f = fence(s, 1); return f + (s.startsWith('`') || s.endsWith('`') ? ' ' + s + ' ' : s) + f; }
    const kids = [...node.childNodes].map(inl).join('');
    if (!kids.trim()) return kids;
    if (t === 'strong' || t === 'b') return '**' + kids + '**';
    if (t === 'em' || t === 'i') return '*' + kids + '*';
    if (t === 'del' || t === 's') return '~~' + kids + '~~';
    if (t === 'a') { const h = (node.getAttribute('href') || '').replace(/[?&]utm_source=chatgpt\.com$/, ''); return h && h !== kids ? '[' + kids + '](' + h + ')' : kids; }
    return kids;
  }
  function pre(el) {
    const code = el.querySelector('code');
    let text, lang = '';
    if (code) { text = code.textContent; const m = (code.className || '').match(/language-([\w+#.-]+)/); if (m) lang = m[1]; }
    else { const cm = el.querySelector('.cm-content'); text = cm ? [...cm.querySelectorAll('.cm-line')].map(l => l.textContent).join('\n') : el.innerText; }
    if (!lang) {   // the block's header shows the language as a short label
      for (const e of el.querySelectorAll('*')) {
        if (code && (e === code || code.contains(e))) break;
        if (e.children.length || e.closest('button')) continue;
        const s = (e.textContent || '').trim();
        if (s && s.length <= 24 && /^[\w+#.-]+$/.test(s)) { lang = s.toLowerCase(); break; }
      }
    }
    text = text.replace(/\n$/, '');
    const f = fence(text, 3);
    return f + lang + '\n' + text + '\n' + f;
  }
  function table(el) {
    const rows = [...el.querySelectorAll('tr')].map(r => [...r.children].map(c => inl(c).trim().replace(/\|/g, '\\|').replace(/\n/g, ' ')));
    if (!rows.length) return '';
    const line = r => '| ' + r.join(' | ') + ' |';
    return [line(rows[0]), line(rows[0].map(() => '---')), ...rows.slice(1).map(line)].join('\n');
  }
  function list(el) {
    const ordered = el.tagName.toLowerCase() === 'ol';
    let i = parseInt(el.getAttribute('start') || '1', 10);
    const out = [];
    for (const li of el.children) {
      if (li.tagName.toLowerCase() !== 'li') continue;
      const mark = ordered ? (i++) + '. ' : '- ';
      const lines = blocks(li).join('\n').split('\n');
      out.push(lines.map((l, k) => (k ? ' '.repeat(mark.length) : mark) + l).join('\n'));
    }
    return out.join('\n');
  }
  function blocks(container) {
    const out = []; let buf = '';
    const flush = () => { if (buf.trim()) out.push(buf.trim()); buf = ''; };
    for (const node of container.childNodes) {
      const t = node.nodeType === 1 ? node.tagName.toLowerCase() : '';
      if (node.nodeType === 1 && node.classList.contains('katex-display')) { flush(); out.push('$$' + tex(node) + '$$'); continue; }
      if (isMath(node)) { flush(); out.push('$$' + tex(node) + '$$'); continue; }
      if (isWidget(node)) { flush(); out.push(widget(node)); continue; }
      if (node.nodeType === 1 && node.getAttribute('data-d-component') === 'divider') { flush(); out.push('---'); continue; }
      if (cfg && cfg.code && node.nodeType === 1 && node.matches(cfg.code)) { flush(); out.push(boxed(node)); continue; }
      if (cfg && cfg.drop && node.nodeType === 1 && node.matches(cfg.drop)) continue;
      if (!BLOCK.has(t)) { buf += inl(node); continue; }
      flush();
      if (t === 'p') out.push(inl(node).trim());
      else if (/^h[1-6]$/.test(t)) out.push('#'.repeat(+t[1]) + ' ' + inl(node).trim());
      else if (t === 'pre') out.push(pre(node));
      else if (t === 'ul' || t === 'ol') out.push(list(node));
      else if (t === 'blockquote') out.push(blocks(node).join('\n\n').split('\n').map(l => '> ' + l).join('\n'));
      else if (t === 'table') out.push(table(node));
      else if (t === 'hr') out.push('---');
      else out.push(...blocks(node));
    }
    flush();
    return out.filter(s => s !== '');
  }
  const parts = [];
  for (const a of as) {
    const sel = cfg ? cfg.root : '.markdown';
    const roots = [...a.querySelectorAll(sel)].filter(r => !r.parentElement.closest(sel));
    for (const r of (roots.length ? roots : [a])) parts.push(...blocks(r));
  }
  const last = as[as.length - 1];
  return {md: parts.join('\n\n'), n: as.length, slug: last ? last.getAttribute('data-message-model-slug') : null};
}"""

# Other chat sites, driven the same way (`web: {site: zai}` on a `type: chatgpt_web` provider). Each entry says where
# the page keeps its composer, buttons and messages; SITE_PROBE_JS and READ_JS read the page through it.
SITES = {
    "zai": {  # chat.z.ai: works without an account (guest), so nothing is kept in any history
        "name": "Z.ai",
        "path": "/",
        "composer": "textarea#chat-input",
        "send": "#send-message-button",
        "stop": 'button[aria-label="Stop"]',
        "user": ".user-message",
        "assistant": ".chat-assistant",
        "root": ".chat-markdown:not(.not-prose .chat-markdown)",
        "code": 'div:has(> div[class*="language-"] pre)',
        "actions": 'button[aria-label="Regenerate"]',
        "model": 'button[aria-label="Select a model"]',
        "msg": '[id^="message-"]',
        "login": None,
        "login_url": "/auth",
        "session": {"domains": ["chat.z.ai"], "names": ["token"]},  # not confirmed with a real sign-in
        # chat.z.ai/auth, "Continue with Email" (looked at signed out, 2026-11-23): its own e-mail + password form,
        # but with a human verification ("Click to start verification") in the form itself, every time
        "signin_unsupported": "Z.ai asks for a human verification every time you sign in, which this app never does "
        "for you. Sign in yourself in the window.",
    },
    "qwen": {  # chat.qwen.ai, signed in; temporary chats, like ChatGPT's
        "name": "Qwen",
        "path": "/?temporary-chat=true",
        "temp": 'button[aria-label="Temporary Chat"][aria-pressed="true"]',
        "composer": "textarea.message-input-textarea",
        "send": 'button.send-button, button[aria-label="Send"]',
        "stop": 'button[aria-label="Stop"], button[aria-label="Stop generating"], button.stop-button, .send-button.stop',
        "user": ".qwen-chat-message-user",
        "assistant": ".qwen-chat-message-assistant",
        "root": ".response-message-content.phase-answer",
        "actions": 'button[aria-label="Regenerate"]',
        "model": '[aria-label="Select Model"]',
        "msg": '[data-msg-id], [id^="chat-response-message-"]',
        "login": 'button[aria-label="Log in"], a[href*="/auth"]',
        "login_url": "/auth",
        "session": {"domains": ["chat.qwen.ai"], "names": ["token"]},  # not confirmed with a real sign-in
        # code blocks are an editor widget that only draws the lines in view, so the finished reply is taken from
        # the page's own Copy button (the reply's markdown, as written) instead of from the elements
        "copy": 'button[aria-label="Copy"]',
        # the free plan's daily cap on a model is written under the chat, in place of a reply
        "limit": "You(?:'|’)ve reached today(?:'|’)s [^.\\n]{0,60}limit[^\\n]{0,120}",
        # the sign-in form, for automatic sign-in (web_signin.py). Read off chat.qwen.ai/auth signed out
        # (2026-11-23): the page opens on "Login with verification code"; "Log in with a password" switches to one
        # form with input[name=email], input[type=password][name=password] and a submit button "Log in". Not seen:
        # what the page says to a wrong password (`failure` is web_signin's general wording), or anything after it
        "signin": {
            "url": "/auth",
            "start": {"css": "button", "text": "^log in with (?:a )?password$"},
            "email": 'input[name="email"]',
            "password": 'input[type="password"]',
            "submit": {"css": 'button[type="submit"]', "text": "^(?:log in|sign in|continue)$"},
        },
    },
    "gemini": {  # gemini.google.com, signed in; temporary chats via the page's toggle
        "name": "Gemini",
        "path": "/app",
        "temp": 'button[aria-label="Turn off temporary chat"], chat-window.is-temporary-chat',
        "temp_toggle": True,  # no url for it: the toggle is pressed after the page loads
        "composer": 'rich-textarea [contenteditable="true"], .ql-editor[contenteditable="true"]',
        "send": 'button[aria-label="Send message"]',
        "stop": 'button[aria-label="Stop response"]',
        "user": "user-query",
        "assistant": "model-response",
        "root": "message-content .markdown",
        "code": "code-block",
        "codeLang": ".code-block-decoration > span",
        "actions": 'model-response button[aria-label="Copy"]',
        "model": '[data-test-id="bard-mode-menu-button"]',
        "msg": ".conversation-container[id]",
        "login": 'a[href*="accounts.google.com/ServiceLogin"], a[aria-label="Sign in"]',
        "login_url": "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fgemini.google.com%2Fapp",
        # the Google account's own session cookies (a Google sign-in made in this profile for another site counts)
        "session": {"domains": ["google.com"], "names": ["SID", "__Secure-1PSID", "__Secure-3PSID"]},
        "identity": 'a[aria-label^="Google Account"]',
        # the sign-in is the Google account's: a pool provider's key page on these domains (Google AI Studio) can be
        # opened for the person in this profile, already signed in (POST /web/open, web_setup.py)
        "opens": ["google.com"],
        "copy": 'button[aria-label="Copy"]',
        # deliberately no `signin`: never an automatic sign-in to a Google account
        "signin_unsupported": "Automatic sign-in is not offered for Google accounts: Google's sign-in guards against "
        "automated browsers, and a Google password opens far more than this app. Sign in yourself in the window.",
    },
}

# chatgpt.com itself (no `site:`), for the sign-in flow (web_setup.py): its name, its sign-in page, and the cookie
# that exists once an account is signed in. SITES entries carry the same keys: "login_url" (a path on the site, or a
# full URL) and "session" (cookie names, where a trailing * matches any suffix, on these domains and their
# subdomains). Only the names and dates of those cookies are ever read, never their values.
CHATGPT_SITE = {
    "name": "ChatGPT",
    "login_url": "/auth/login",
    "session": {"domains": ["chatgpt.com"], "names": ["__Secure-next-auth.session-token*"]},
    # the sign-in form, for automatic sign-in (web_signin.py): the e-mail address first, then the password on
    # auth.openai.com. Read off chatgpt.com/auth/login signed out (2026-11-23): input[type=email][name=username] and
    # one submit button "Continue". Not seen (it needs an e-mail address typed): the password page, taken to be a
    # password field and a submit button "Continue" / "Log in", and what it says to a wrong password. Keys: "url";
    # "hosts" (the site's own other hosts the form may be on: any other host is another sign-in provider, a hard
    # stop); "start" (a button that switches the page to the password form, pressed first when it shows); "email";
    # "continue" (sends the e-mail step when the password field is not on the same page); "password"; "submit";
    # "failure" (a regex for the page's wrong-name-or-password words; default: web_signin's general one). A control
    # is a CSS selector, or {"css", "text"} with a regex the control's own text must match
    "signin": {
        "url": "/auth/login",
        "hosts": ["auth.openai.com"],
        "email": 'input[type="email"], input[name="username"], input[name="email"]',
        "continue": {"css": 'button[type="submit"]', "text": "^continue$"},
        "password": 'input[type="password"]',
        "submit": {"css": 'button[type="submit"]', "text": "^(?:continue|log in|sign in)$"},
    },
    "plan": True,  # the account's plan is read from the page (PLAN_JS); a paid one must be acknowledged before use
}

# The plan label of the signed-in account, from the data chatgpt.com serves with the page (#client-bootstrap; nothing
# is clicked or fetched): session.account.planType ("free", "plus", "pro", "team", "enterprise", "edu", ...), else the
# same key a few levels down. Only that one short label is returned; null when the page does not carry it. Not
# confirmed on the real site with a signed-in account (the key is the one its /api/auth/session answer is known for).
PLAN_JS = r"""() => {
  const boot = document.getElementById('client-bootstrap');
  if (!boot) return null;
  let j; try { j = JSON.parse(boot.textContent); } catch (e) { return null; }
  const ok = v => typeof v === 'string' && /^[A-Za-z][A-Za-z0-9 _-]{0,23}$/.test(v) ? v.toLowerCase() : null;
  const from = o => o && typeof o === 'object' ? (ok(o.planType) || ok(o.plan_type)) : null;
  const s = (j && j.session) || {};
  const direct = from(s.account) || from(j.account) || from(s.user) || from(j.user) || from(s);
  if (direct) return direct;
  let budget = 4000;
  const walk = (o, d) => { if (!o || typeof o !== 'object' || d > 5 || --budget < 0) return null;
    const here = from(o); if (here) return here;
    for (const k of Object.keys(o).slice(0, 80)) { const r = walk(o[k], d + 1); if (r) return r; }
    return null; };
  return walk(j, 0);
}"""
FREE_PLANS = ("free", "chatgptfreeplan")  # every other label is a paid plan

# True when the page holds something that must never be written to disk: a filled password or one-time-code field, or
# it is a sign-in page at all. _dump keeps nothing of such a page.
SECRET_PAGE_JS = r"""() => {
  if (/(^|\.)auth\.openai\.com$|(^|\.)auth0\.|(^|\.)accounts\.google\.com$/.test(location.hostname) || /^\/(auth(\/|$)|log-?in|sign-?in)/.test(location.pathname)) return true;
  return [...document.querySelectorAll('input[type="password"], input[autocomplete="current-password"], input[autocomplete="new-password"], input[autocomplete="one-time-code"]')].some(e => !!e.value);
}"""

# Which identity is signed in, read from the page as it is (after a check opened it; nothing is clicked or fetched):
# the e-mail address in the data chatgpt.com serves with the page (#client-bootstrap), else in the site's account
# button (`identity` in SITES: Google's says "Google Account: <name> (<e-mail>)"), else in any account or profile
# button's label. null when the page does not show one. Not confirmed on the real sites with a signed-in account.
IDENTITY_JS = r"""(sel) => {
  const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;
  const boot = document.getElementById('client-bootstrap');
  if (boot) { try { const j = JSON.parse(boot.textContent); const u = (j.session && j.session.user) || j.user;
                    if (u && typeof u.email === 'string' && EMAIL.test(u.email)) return u.email; } catch (e) {} }
  const texts = [];
  const add = el => texts.push(el.getAttribute('aria-label') || '', el.getAttribute('title') || '');
  if (sel) document.querySelectorAll(sel).forEach(el => { add(el); texts.push(el.innerText || ''); });
  document.querySelectorAll('[aria-label*="account" i], [aria-label*="profile" i], [data-testid*="profile" i]').forEach(add);
  for (const t of texts) { const m = t.match(EMAIL); if (m) return m[0]; }
  return null;
}"""

# Runs in the page's own script world: keep what the page's Copy button would put on the clipboard.
CAPTURE_JS = r"""() => {
  if (window.__pcoreCap) { window.__pcoreCap.length = 0; return true; }
  window.__pcoreCap = [];
  try {
    navigator.clipboard.writeText = t => { window.__pcoreCap.push(String(t)); return Promise.resolve(); };
    navigator.clipboard.write = async items => { for (const it of items) if (it.types.includes('text/plain')) window.__pcoreCap.push(await (await it.getType('text/plain')).text()); };
  } catch (e) {}
  const exec = document.execCommand.bind(document);
  document.execCommand = (c, ...a) => { if (c === 'copy') { window.__pcoreCap.push(String(window.getSelection())); return true; } return exec(c, ...a); };
  return true;
}"""
COPY_CLICK_JS = r"""(cfg) => { const as = document.querySelectorAll(cfg.assistant); const a = as[as.length - 1]; if (!a) return false;
  const bs = a.querySelectorAll(cfg.copy); const b = bs[bs.length - 1]; if (!b) return false; b.click(); return true; }"""

# The same facts PROBE_JS reports, for a site described in SITES.
SITE_PROBE_JS = r"""([cfg, fresh]) => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                      return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const first = sel => sel ? [...document.querySelectorAll(sel)].find(vis) || null : null;
  const out = {url: location.href, title: document.title, temp: true, account: true, slug: null, lastState: null};
  if (cfg.temp) out.temp = !!document.querySelector(cfg.temp);
  out.composer = !!first(cfg.composer);
  out.login = !!first(cfg.login);
  const scoped = [...document.querySelectorAll('[role="alert"], [role="alertdialog"], [role="dialog"], [data-sonner-toast], [class*="toast" i], [class*="error" i]')]
    .filter(vis).filter(el => !el.closest(cfg.user + ', ' + cfg.assistant)).map(el => el.innerText || '').join('\n');
  const empty = !document.querySelector(cfg.user + ', ' + cfg.assistant);   // see PROBE_JS
  // ...and never the sidebar: it lists the account's saved chats by title (anything: "Usage limits design") and
  // has its own "Unable to load history / Retry", which says nothing about whether a chat can be sent
  let whole = fresh && empty && document.body ? document.body.innerText : '';
  if (whole) for (const el of document.querySelectorAll('nav, aside, #stage-slideover-sidebar, [data-testid*="sidebar"], [class*="sidebar"]')) {
    const t = (el.innerText || '').trim(); if (t) whole = whole.split(t).join('\n');
  }
  const text = (whole ? whole + '\n' : '') + scoped + '\n' + (document.title.length <= 80 ? document.title : '');  // a chat's title is its first message: never read that for notices
  const m = (re, s) => { const x = (s === undefined ? text : s).match(re); return x ? x[0] : null; };
  out.challenge = m(/just a moment|verify (?:that )?you are (?:a )?human|unusual activity|suspicious activity|confirm you(?:'|’)re (?:not a robot|human)|are you a robot|complete the captcha|checking your browser|you have been blocked|access denied|slide to (?:verify|complete)/i);
  out.busy = m(/try again (?:later|in a (?:few|couple)[^.\n]{0,20})|at capacity|(?:high|heavy) (?:demand|traffic|load)|server (?:is )?busy|too many (?:requests|users)|rate limit/i, scoped);
  out.limit = out.busy ? null : m(/(?:reached|hit|exceeded) (?:your|the|our)[^.\n]{0,80}(?:limit|quota)|(?:daily|usage) (?:limit|quota|cap)[^.\n]{0,60}|(?:log|sign) in to continue[^.\n]{0,60}/i, scoped);
  out.error = m(/something went wrong|network error|message (?:is )?too long|(?:too long|exceeds)[^.\n]{0,40}(?:message|limit)|an error occurred|failed to (?:send|fetch|generate)[^.\n]{0,40}/i, scoped);
  if (!out.limit && !out.busy && cfg.limit && document.body) out.limit = m(new RegExp(cfg.limit, 'i'), document.body.innerText.slice(-700));
  out.full = m(/context (?:window|length)[^.\n]{0,30}(?:full|exceeded)|conversation (?:is|has gotten) too long|maximum (?:length|context)[^.\n]{0,40}/i, scoped);
  out.stop = !!first(cfg.stop);
  const send = first(cfg.send);
  out.send = !!send;
  out.sendEnabled = !!send && !send.disabled && send.getAttribute('aria-disabled') !== 'true';
  const us = [...document.querySelectorAll(cfg.user)], as = [...document.querySelectorAll(cfg.assistant)];
  out.users = us.length; out.assistants = as.length;
  const lu = us[us.length - 1], last = as[as.length - 1];
  const body = last ? last.querySelector(cfg.root) : null;
  out.lastLen = body ? (body.innerText || '').length : 0;
  const id = el => { const h = el && cfg.msg ? (el.closest(cfg.msg) || el.querySelector(cfg.msg)) : null;
                     return h ? h.getAttribute('data-msg-id') || h.id || null : null; };
  out.lastUser = id(lu); out.lastAsst = id(last);
  out.asstAfterUser = !!(last && lu && (lu.compareDocumentPosition(last) & Node.DOCUMENT_POSITION_FOLLOWING));
  out.actions = cfg.actions ? [...document.querySelectorAll(cfg.actions)].length : 0;
  const sw = first(cfg.model);
  out.model = sw ? (sw.innerText || '').trim().replace(/\s+/g, ' ') : null;
  return out;
}"""

COMPOSER = '#prompt-textarea, div[contenteditable][role="textbox"], textarea[name="prompt"], main textarea'
SEND = (
    '[data-testid="send-button"], button[aria-label="Send prompt"], button[aria-label="Send message"], '
    'button[aria-label="Send"]'
)
SWITCHER = (
    '[data-testid="model-switcher-dropdown-button"], button[aria-label^="Model selector" i], '
    'button[aria-haspopup="menu"][aria-label*="model" i]'
)

DEFAULTS = {
    "daily_cap": 200,  # requests per local day; then unavailable until midnight
    # a model with a small allowance of its own on the site (Qwen4-Max): a first guess at replies per account per
    # window, e.g. {qwen4-max: 6}. The real figure is learned each time the site says the limit is reached.
    "model_caps": None,
    "model_window_s": 86400.0,  # a model's count starts again this long after its first reply
    "page_backoff_max_s": 120.0,  # longest wait after repeated page errors (nothing the site said)
    "doubt_tries": 3,  # a missing temporary chat / a sign-in page is believed the third time in a row
    "min_gap_s": 4.0,  # pause between requests: a random time in [min_gap_s, max_gap_s]
    "max_gap_s": 9.0,
    "queue_wait_s": 45.0,  # a request waits this long for the one in flight, then goes to the normal pool
    "named_wait_s": 300.0,  # the same wait for a request that names the model (it has no pool to go to)
    "reply_timeout_s": 600.0,  # longest wait for one reply (thinking models take minutes)
    "idle_close_s": 900.0,  # close the window after this long without a request
    "max_prompt_chars": 120000,
    "fresh_prompt_chars": 60000,  # compact old harness history before sending a new tab's first message
    "strict_model": True,  # a model other than "auto" must be the one selected in the page's model picker
    "allow_auto_fallback": False,  # a named model may use the page's Auto choice when the name disappears
    "reuse_chat": False,  # keep a temporary tab per active agent conversation
    "max_chat_tabs": 6,
    "lean": False,  # replace the harness's long base prompt with a few lines and drop tools a web chat never needs
    "lean_system_chars": 3000,  # a system message longer than this is the harness's base prompt
    # prune.py's pass (true, or a dict of its options) on the history a fresh chat starts with; a continued chat is
    # sent as is. Off unless set here, by `prune:` in the catalog, or by PCORE_PRUNE=1 / web
    "prune": None,
    # compact.py's summarising compaction (true, or a dict of its options) instead of the blunt cut when a fresh
    # chat's first message is over fresh_prompt_chars. Off unless set here, by `compact:` in the catalog, or by
    # PCORE_COMPACT=1 / web
    "compact": None,
    "accounts": None,  # e.g. [main, b, c]: one Chrome profile per signed-in account, requests spread across them
    "backoff_base_s": 60.0,  # a "temporary limit" / failed-to-load page: wait this long, doubling each time in a row
    "backoff_max_s": 900.0,
    "endpoints": ["responses"],  # tier requests from these front ends may use the bridge (responses, messages, chat)
    "cooldown_login_s": 1800,
    "cooldown_challenge_s": 6 * 3600,
    "cooldown_limit_s": 3600,
    "cooldown_closed_s": 600,
    "cooldown_tempchat_s": 6 * 3600,
    "cooldown_model_s": 3600,
    "challenge_wait_s": 20.0,  # an automatic browser check gets this long to pass by itself; nothing is clicked
    "driver": "playwright",  # or "patchright": drive Chrome without the automation markers (docs/TECHNICAL.md)
    "headless": False,  # true: no window at all. chatgpt.com's browser check stops a headless Chrome, so use `quiet`
    "quiet": False,  # macOS: hide the window's Chrome right after it opens, so it never takes the screen or focus
    # which browser runs the window. chrome: the installed Google Chrome (as always). bundled: the bridge's own
    # Chrome for Testing copy under <PCORE_HOME>/browser, a background-only app (no Dock icon, never takes focus) whose
    # window is created off screen; prepared (downloaded once, ~190 MB) on first use, the installed Chrome if that
    # fails. auto: the bundled one when it is ready; while it is prepared in the background, requests skip the
    # bridge (nothing is opened instead). Same profile either way; signing in always opens a normal window
    # (web_setup.py). See browser_host.py
    "browser": "chrome",
    # auto: "chrome" = use the installed Chrome (a window that can show) while the bundled one is missing, as
    # before; unset = never, skip the bridge until the bundled one is ready
    "browser_fallback": None,
    "browser_watch_s": 2.0,  # bundled: how often to check that every window is still off screen (and put it back)
    "login_timeout_s": 900.0,  # a sign-in window left open this long is closed (then the account is checked)
    # automatic sign-in with a saved sign-in (web_signin.py; off unless the person turned it on for the account)
    "autologin_max_per_day": 3,  # attempts per account per local day; then it stays signed out until a person acts
    "autologin_gap_s": 900.0,  # shortest time between two attempts; doubles after each attempt of the day
    "autologin_step_s": 30.0,  # how long each step of the sign-in form may take to appear
    "signin": None,  # the sign-in form's description; default: the site's (`signin` in SITES / CHATGPT_SITE)
    "open_timeout_s": 1800.0,  # a key page opened for the person (POST /web/open) is closed after this long
    "opens": None,  # domains whose pages this provider's sign-in also signs in to; default: the site's
    "plan_refresh_s": 6 * 3600.0,  # the account's plan is read from the page again this long after the last time
    "name": None,  # what the app calls this provider; default: the site's name ("ChatGPT", "Gemini", ...)
    "login_url": None,  # where the sign-in window opens; default: the site's sign-in page
    "session_cookies": None,  # {domains, names}: the cookie that means "signed in"; default: the site's
    "browser_trim": True,  # bundled: also pass the trimmed switch set (browser_host.TRIM_ARGS / TRIM_FEATURES)
    # bundled: cft (Chrome for Testing, what Provenance.app can ship) or chrome (a background-only clone of the
    # installed Google Chrome: the same build, so the same "Google Chrome" brand to sites; this Mac only)
    "browser_source": "cft",
    "notify": True,  # macOS notification when the provider goes unavailable
    # an account no check has ever seen signed in (a new one, nothing signed in yet) is skipped, without penalty,
    # instead of opening its browser to find the sign-in page. Provenance.app's catalog turns it on
    "skip_unchecked": False,
    # keeping the saved sign-ins (cookie_guard.py): backups of the profile's cookie store, a wiped store noticed and
    # a backup put back once a day, no browser started on a wrong clock or a nearly full disk
    "cookie_guard": True,
    "cookie_backup_keep": 5,  # the newest backups kept, plus one per day for cookie_backup_days days
    "cookie_backup_days": 7,
    "cookie_backup_every_s": 3600.0,  # while a browser runs: a snapshot this often (0: only at exit and launch)
    "cookie_settle_s": 3.0,  # after a launch on a profile with sign-ins: watch the store this long before using it
    "cookie_recheck_s": [45.0, 120.0],  # and look again this long after the launch (Chrome writes within 30 s)
    "cookie_collapse_ratio": 0.25,  # a store with under this share of its rows left (or none) has collapsed,
    "cookie_collapse_floor": 10,  # when it had at least this many
    "clock_forward_max_s": 86400.0,  # the clock may not be further ahead of what it should be than this,
    "clock_back_slack_s": 120.0,  # nor further behind than this;
    "clock_settle_s": 600.0,  # a doubted clock that then runs steadily this long is taken as right after all
    "disk_min_free_mb": 500,  # no browser is started with less free space on the home's disk (0: not checked)
    "launch_tries": 3,  # a wrong clock or a full disk: looked at this many times,
    "launch_wait_s": 5.0,  # this far apart, before the launch is refused
    "launch_block_s": 300.0,  # and the account is out for this long (kind clock / disk), then it is tried again
    "profile_wait_s": 20.0,  # a browser still exiting on the profile is waited for this long before a launch
    "profile": None,  # default: <PCORE_HOME>/chatgpt-web-profile
    "site": None,  # another chat site from SITES (e.g. zai) instead of chatgpt.com
}
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LOGIN_HINT = "run `provenance-core weblogin` and sign in to ChatGPT in the window it opens"


def home_dir() -> Path:
    return settings.home_dir()


def enabled(options: dict | None) -> bool:
    """Off unless turned on. In order: PCORE_CHATGPT_WEB=0 forces every web provider off; then a choice made in the
    app (`user_enabled`, from <PCORE_HOME>/settings.json, see provider_settings.py); then PCORE_CHATGPT_WEB=1 turns
    them on; then `enabled: true` on the provider."""
    o = options or {}
    v = (env("CHATGPT_WEB") or "").strip().lower()
    if v and v not in ("1", "true", "on", "yes"):
        return False
    if o.get("user_enabled") is not None:
        return bool(o["user_enabled"])
    return bool(v) or bool(o.get("enabled"))


def enabled_by(options: dict | None) -> str:
    """What decides enabled(): "env-off" (PCORE_CHATGPT_WEB=0, cannot be changed from the app), "app", "env" or
    "catalog"."""
    o = options or {}
    v = (env("CHATGPT_WEB") or "").strip().lower()
    if v and v not in ("1", "true", "on", "yes"):
        return "env-off"
    if o.get("user_enabled") is not None:
        return "app"
    return "env" if v else "catalog"


class Bridge:
    """One Chrome window on the dedicated profile; one request at a time."""

    def __init__(
        self,
        base_url: str = "https://chatgpt.com",
        options: dict | None = None,
        home: Path | None = None,
        sleep=asyncio.sleep,
        clock=time.time,
        rand=random.uniform,
        account: str | None = None,
        host: Bridge | None = None,
    ):
        self.account = account
        self.host = host  # another Bridge whose Chrome this one works in, as extra tabs (`share:` in the catalog)
        self._guests: list[Bridge] = []  # the bridges that work in this one's Chrome
        if host:
            host._guests.append(self)
        self._launch = asyncio.Lock()
        self._tabs = asyncio.Lock()  # one new tab at a time per Chrome: two at once could be handed the same tab
        self.base_url = base_url.rstrip("/")
        self.o = {**DEFAULTS, **(options or {})}
        if env(
            "CHATGPT_WEB_DRIVER"
        ):  # PCORE_CHATGPT_WEB_DRIVER=patchright: a per-machine choice, kept out of the catalog
            self.o["driver"] = env("CHATGPT_WEB_DRIVER").strip()
        self.home = Path(home) if home else home_dir()
        tag = "" if account in (None, "", "main", "default") else "-" + re.sub(r"[^\w.-]", "_", account)
        self.site = SITES[self.o["site"]] if self.o.get("site") else None
        kind = self.o.get("site") or "chatgpt"
        base = Path(self.o["profile"] or self.home / f"{kind}-web-profile").expanduser()
        self.profile = base.with_name(base.name + tag)
        if host:
            self.profile = host.profile
        self.state_path = self.home / f"{kind}_web{tag}.json"
        self.busy = 0  # requests in flight or queued on this account (the pool spreads by it)
        self.sleep, self.clock, self.rand = sleep, clock, rand
        self.lock = asyncio.Lock()
        self._pw = self._ctx = self._page = None
        self._proc = self._watch = None  # bundled: the browser process this bridge started, and its watchdog
        self._bundled: str | None = None  # the bundled browser's version while it is the one running
        self._closing = False
        self._gone = False  # the user closed the window themselves
        self._last_done = 0.0
        self._idle: asyncio.Task | None = None
        self._notified: str | None = None
        self._sessions: dict[str, dict] = {}
        # a sign-in window holds this account's profile (web_setup.py): requests skip the account meanwhile and
        # its background browser is not started
        self.signin: str | None = None
        self._parker: asyncio.Task | None = None  # bundled: keeps every window of the browser off screen
        self._repark: asyncio.Event | None = None
        # automatic sign-in (web_signin.py): the attempt in the background, and what to tell once it worked (the
        # router forgets the cooldown it gave this provider's models)
        self._autologin_task: asyncio.Task | None = None
        self._autologin_running = False
        self.on_revived = None
        # summarising compaction: the router hands over its own (shared cache, and `summarize`, which asks a cheap
        # API model of its pool for the narrative; never this bridge)
        self.compactor = compact.Compactor()
        self.summarize = None
        # the profile's cookie store: backups, a wiped store noticed and repaired, the launch gate (cookie_guard.py)
        self.guard = cookie_guard.Guard(self)
        self._guardian: asyncio.Task | None = None  # looks at the store again after a launch, then hourly
        self._recovering: asyncio.Task | None = None  # closes the browser and puts a backup back

    # ---- what the app shows, and the sign-in flow uses --------------------------------------------------------
    @property
    def site_info(self) -> dict:
        return self.site or CHATGPT_SITE

    @property
    def display_name(self) -> str:
        return str(self.o.get("name") or self.site_info.get("name") or "ChatGPT")

    @property
    def login_url(self) -> str:
        u = self.o.get("login_url") or self.site_info.get("login_url") or "/"
        return u if re.match(r"https?://", u) else self.base_url + "/" + u.lstrip("/")

    @property
    def session_spec(self) -> dict | None:
        return self.o.get("session_cookies") or self.site_info.get("session")

    def signing_in(self) -> bool:
        return bool(self.signin or (self.host and self.host.signin))

    def _held_why(self) -> str:
        who = self.account or "main"
        if cookie_guard.RESTORING in (self.signin, self.host and self.host.signin):
            return f"this account's ({who}) saved sign-ins are being put back"
        return f"a sign-in window is open on this account ({who})"

    # ---- persisted state: today's count, the last login check, why it's blocked -------------------------------
    def load(self) -> dict:
        try:
            s = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            s = {}
        today = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        if s.get("day") != today:
            s["day"], s["count"] = today, 0
        return s

    def save(self, **changes) -> dict:
        s = {**self.load(), **changes}
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=1))
        tmp.replace(self.state_path)
        return s

    def status(self) -> dict:
        s = self.load()
        left = (s.get("blocked_until") or 0) - self.clock()
        return {
            "signed_in": s.get("signed_in"),
            "checked_at": s.get("checked_at"),
            "today": s.get("count", 0),
            "daily_cap": self.o["daily_cap"],
            "blocked": left > 0,
            "blocked_for_s": max(0, int(left)),
            "reason": s.get("reason") if left > 0 else None,
            "kind": s.get("kind") if left > 0 else None,
            "model_seen": s.get("model_seen"),
            "models": {
                m: self.model_usage(m, s) for m in {**(self.o.get("model_caps") or {}), **(s.get("models") or {})}
            },
            "email": s.get("identity"),
            "plan": s.get("plan") if self.site_info.get("plan") else None,
            "plan_label": s.get("plan_label") if self.site_info.get("plan") else None,
            "needs_confirmation": self.needs_confirmation(s),
            "window_open": self._page is not None,
            "browser": self.browser_in_use(),
            "signing_in": self.signing_in() and self._held_why().startswith("a sign-in"),
            # {kind, text, at} for a day after the saved sign-ins were lost and a backup was put back
            "notice": n if (n := s.get("notice")) and self.clock() - (n.get("at") or 0) < 86400 else None,
            "profile": str(self.profile),
            **({"account": self.account} if self.account else {}),
        }

    # ---- the account's plan (sites with `plan`: ChatGPT) --------------------------------------------------------
    def needs_confirmation(self, s: dict | None = None) -> str | None:
        """ "paid_plan" while the account is on a paid plan the person has not said to use anyway: it stays connected
        but no request goes to it. A plan that could not be read ("unknown") does not hold anything back."""
        s = self.load() if s is None else s
        if self.site_info.get("plan") and s.get("plan") == "paid" and not s.get("plan_ack"):
            return "paid_plan"
        return None

    def confirm_plan(self) -> bool:
        """The person's "use it anyway" for the paid plan this account is on. False when there is nothing to confirm."""
        s = self.load()
        if not (self.site_info.get("plan") and s.get("plan") == "paid"):
            return False
        self.save(plan_ack=True)
        return True

    def _note_plan(self, label: str | None):
        """Keep the plan as free / paid / unknown (and the site's own label for a paid one). A page that does not
        show it leaves what was known for the same identity. Seeing the account on the free plan, or another identity,
        withdraws an earlier "use it anyway": a later paid plan is asked about again."""
        s = self.load()
        who = s.get("identity")
        same = not who or not s.get("plan_for") or who == s.get("plan_for")  # not known to be someone else
        ch: dict = {"plan_checked_at": int(self.clock())}
        if label:
            free = label in FREE_PLANS
            ch.update(plan="free" if free else "paid", plan_label=label, plan_for=who or s.get("plan_for"))
            if free or not same:
                ch["plan_ack"] = False
        elif not same or s.get("plan") not in ("free", "paid"):
            ch.update(plan="unknown", plan_label=None, plan_for=who or s.get("plan_for"))
            if not same:
                ch["plan_ack"] = False
        self.save(**ch)

    async def _read_plan(self, page, stale_only: bool = False):
        if not self.site_info.get("plan"):
            return
        if stale_only and self.clock() - (self.load().get("plan_checked_at") or 0) < float(self.o["plan_refresh_s"]):
            return
        try:
            label = await page.evaluate(PLAN_JS)
        except Exception:  # noqa: BLE001
            label = None
        self._note_plan(label if isinstance(label, str) and label.strip() else None)

    def _seen_signed_in(self):
        """A page showed the account signed in. Automatic sign-in that had stopped for a person (a code, a captcha)
        may run again the next time: the person has signed in since."""
        ch: dict = {"signed_in": True, "checked_at": int(self.clock())}
        al = self.load().get("autologin")
        if al and al.get("hold") == "needs_person":
            ch["autologin"] = {**al, "hold": None}
        self.save(**ch)

    def browser_in_use(self) -> str | None:
        """What runs this bridge's window now: "bundled <version>", "chrome", or None when no window is open."""
        owner = self.host or self
        if self._page is None:
            return None
        return f"bundled {owner._bundled}" if owner._bundled else "chrome"

    # ---- a model's own allowance (replies per window), learned from the site's limit notice ------------------
    def model_usage(self, model: str | None, s: dict | None = None) -> dict | None:
        """{cap, used, limited_for_s} for a model with an allowance of its own, else None."""
        if not model:
            return None
        m = dict(((s or self.load()).get("models") or {}).get(model) or {})
        guess = (self.o.get("model_caps") or {}).get(model)
        if not m and not guess:
            return None
        now = self.clock()
        if m.get("since") and now - m["since"] > float(self.o["model_window_s"]) and (m.get("until") or 0) <= now:
            m["used"], m["since"] = 0, None  # a new window
        cap, used = int(m.get("cap") or guess or 0), int(m.get("used") or 0)
        if not cap:
            return None
        limited = max(0.0, (m.get("until") or 0) - now)
        return {"cap": max(cap, used + (0 if limited else 1)), "used": used, "limited_for_s": limited,
                "learned": bool(m.get("cap")), "since": m.get("since")}  # fmt: skip

    def _model_save(self, model: str, **changes):
        s = self.load()
        models = dict(s.get("models") or {})
        models[model] = {**(models.get(model) or {}), **changes}
        self.save(models=models)

    def _model_replied(self, model: str):
        u = self.model_usage(model)
        if u is None:
            return
        self._model_save(model, used=u["used"] + 1, since=u["since"] or self.clock(), until=0)

    def _model_limited(self, model: str, e: WebUnavailable):
        """The site says this model's allowance is used up: what was sent since the window began is the allowance."""
        u = self.model_usage(model) or {"used": 0, "limited_for_s": 0}
        ch = {"until": self.clock() + e.cooldown, "reason": e.reason}
        if u["used"] > 0 and not u["limited_for_s"]:
            ch["cap"] = u["used"]
        self._model_save(model, **ch)

    def _left(self, model: str | None = None) -> tuple[int, int]:
        """(cap, used) on this account for the model: the day's request cap, or the model's own allowance when
        that leaves a smaller share."""
        s = self.load()
        cap = int(self.o["daily_cap"])
        used = min(int(s.get("count", 0)), cap)
        u = self.model_usage(model, s)
        if u:
            if u["limited_for_s"]:
                return u["cap"], u["cap"]
            if not cap or (u["cap"] - u["used"]) / u["cap"] < (cap - used) / cap:
                return u["cap"], u["used"]
        return cap, used

    def capacity(self, accounts=None, model: str | None = None) -> dict:
        s = self.status()
        cap, used = self._left(model)
        unchecked = bool(self.o.get("skip_unchecked")) and s["signed_in"] is None
        out = s["blocked"] or s["signing_in"] or unchecked or bool(s["needs_confirmation"])
        left = 0.0 if out or not cap else max(0, cap - used) / cap
        return {"remaining": left, "busy": self.busy, "accounts": 0 if out else 1, "available": left > 0,
                "signed_in": s["signed_in"] is True}  # fmt: skip

    def reset(self):
        """Forget a block (after `weblogin`, or when you've dealt with a notice by hand)."""
        self._notified = None
        self._gone = False
        self.save(blocked_until=0, reason=None, kind=None, strikes=0)

    def _block(self, e: WebUnavailable):
        if e.kind == "login":
            s = self.load()
            left = (s.get("blocked_until") or 0) - self.clock()
            if s.get("kind") == "cookies_lost" and left > 0:  # already told why, in better words
                e.reason, e.kind, e.cooldown = s.get("reason") or e.reason, "cookies_lost", left
                return
            if (self.host or self)._guard_poke("signed-out page"):
                # not a sign-out: this computer lost the saved sign-ins, and a backup is being put back
                wait = float(self.o["page_backoff_max_s"])
                self.save(blocked_until=self.clock() + wait, kind="page",
                          reason="the browser's saved sign-ins were lost; a backup is being put back")  # fmt: skip
                e.cooldown, e.kind = wait, "page"
                return
        if e.kind in ("tempchat", "login") and e.cooldown > float(self.o["page_backoff_max_s"]):
            # "no temporary chat" and "signed out" are read off the page, and a page that has not finished loading
            # (a busy computer) looks the same. These cost hours, so on an account where they worked before the
            # first sightings are only a short wait; the long one starts when it keeps being seen.
            s = self.load()
            seen = int((s.get("doubts") or {}).get(e.kind, 0)) + 1
            worked = s.get("signed_in") if e.kind == "login" else s.get("temp_ok")
            if worked and seen < int(self.o["doubt_tries"]):
                self.save(
                    doubts={**(s.get("doubts") or {}), e.kind: seen},
                    blocked_until=self.clock() + float(self.o["page_backoff_max_s"]),
                    reason=f"{e.reason} (seen {seen} time{'s' if seen != 1 else ''}; trying again before believing it)",
                    kind="page",
                )
                e.cooldown, e.kind = float(self.o["page_backoff_max_s"]), "page"  # what the caller waits by, too
                return
        ch = {"blocked_until": self.clock() + e.cooldown, "reason": e.reason, "kind": e.kind}
        if e.kind in ("login", "challenge", "tempchat"):
            self._event(e.kind, e.reason)
        if e.kind == "login":
            ch.update(signed_in=False, checked_at=int(self.clock()), identity=None)
        self.save(**ch)
        if e.kind == "login":  # established (not a first sighting): a saved sign-in, if the person agreed to one
            from . import web_signin

            web_signin.kick(self)
        if self.o["notify"] and self._notified != e.kind and e.kind not in ("closed", "busy", "page"):
            self._notified = e.kind
            notify(f"ChatGPT web is out: {e.reason}" + (f" ({LOGIN_HINT})" if e.kind == "login" else ""))

    def _event(self, kind: str, reason: str, **more):
        """One dated line per sign-out / challenge / lost temporary chat / automatic sign-in attempt in
        <home>/web-events.jsonl, with how long this process had been up: what is needed to tell later whether they
        follow restarts, hours or load. Never anything typed or read on a sign-in page."""
        try:
            line = {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "site": self.site["name"] if self.site else "ChatGPT",
                "account": self.account or "main",
                "kind": kind,
                "reason": reason[:200],
                "up_s": int(time.monotonic() - STARTED),
                "today": self.load().get("used"),
                **more,
            }
            self.home.mkdir(parents=True, exist_ok=True)
            with open(self.home / "web-events.jsonl", "a") as f:
                f.write(json.dumps(line) + "\n")
        except OSError:
            pass

    def _precheck(self, model: str | None = None):
        u = self.model_usage(model)
        if u and u["limited_for_s"]:
            raise WebUnavailable(
                f"{model} has reached its limit on this account ({self.account or 'main'})", u["limited_for_s"], "limit"
            )
        if self.signing_in():
            raise WebSkip(self._held_why())
        s = self.load()
        if self.o.get("skip_unchecked") and s.get("signed_in") is None:
            raise WebSkip(f"no one has signed in on this account ({self.account or 'main'}) yet")
        if self.needs_confirmation(s):
            raise WebSkip(
                f"this account ({self.account or 'main'}) is on a paid plan ({s.get('plan_label')}) and has not been "
                "confirmed for use"
            )
        left = (s.get("blocked_until") or 0) - self.clock()
        if left > 0:
            raise WebUnavailable(s.get("reason") or "unavailable", left, s.get("kind") or "blocked")
        if s.get("count", 0) >= int(self.o["daily_cap"]):
            lt = time.localtime(self.clock())
            to_midnight = 86400 - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
            raise WebUnavailable(f"daily cap of {self.o['daily_cap']} requests reached", to_midnight, "cap")

    # ---- the browser ---------------------------------------------------------------------------------------------
    async def _ensure_page(self):
        if self.signing_in():  # the profile is open in the sign-in window: a second browser cannot open it
            raise WebSkip(self._held_why())
        if not self.host:
            async with self._launch:
                return await self._ensure_own_page()
        # a guest in another bridge's Chrome: its own tabs there, reopened whenever that Chrome was restarted
        if (
            self._page is not None
            and not self._page.is_closed()
            and self._ctx is not None
            and self._ctx is self.host._ctx
        ):
            return self._page
        self._sessions.clear()
        self._page, self._gone = None, False
        async with self.host._launch:
            await self.host._ensure_own_page()
        self._ctx, self._pw = self.host._ctx, self.host._pw
        self._page = await self._new_page()
        return self._page

    async def _ensure_own_page(self):
        if self._page is not None and not self._page.is_closed() and not self._gone:
            return self._page
        gone, self._gone = self._gone, False
        was_bundled = self._bundled
        await self._shutdown()
        # closing the window is a "not now": stay out of the way for a while instead of reopening it. The bundled
        # browser's window is never on screen, so it going away is a crash or a quit, not that: just start it again
        if gone and not was_bundled:
            raise WebUnavailable("the ChatGPT window was closed", float(self.o["cooldown_closed_s"]), "closed")
        driver = str(self.o["driver"]).lower()
        if driver not in ("playwright", "patchright"):
            raise WebUnavailable(f"unknown chatgpt-web driver {driver!r} (playwright or patchright)", 3600, "setup")
        try:
            if driver == "patchright":
                from patchright.async_api import async_playwright
            else:
                from playwright.async_api import async_playwright
        except ImportError:
            raise WebUnavailable(f"{driver} is not installed (pip install {driver})", 3600, "setup") from None
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        await self._profile_free()
        if not self.guard.on:
            return await self._start_browser(async_playwright, driver, await self._bundled_browser())
        await self._launch_gate()
        bundled = await self._guard_before(await self._bundled_browser())
        page = await self._start_browser(async_playwright, driver, bundled)
        if await self._guard_after():  # wiped as the browser started: it was closed again, the wipe is on disk now
            bundled = await self._guard_before(await self._bundled_browser())
            page = await self._start_browser(async_playwright, driver, bundled)
            await self._guard_after(last=True)
        self._guardian = asyncio.get_running_loop().create_task(self._guard_watch(self._ctx))
        return page

    async def _start_browser(self, async_playwright, driver: str, bundled: dict | None):
        """Start the browser on the profile (the bundled build `bundled`, else the installed Chrome) -> its page."""
        self._pw = await async_playwright().start()
        if bundled:
            try:
                self._page = await self._launch_bundled(bundled, driver)
                return await self._watch_page()
            except Exception as e:  # noqa: BLE001 - profile in use, the copy broken, ...
                await self._shutdown()
                if str(self.o.get("browser")).lower() != "auto" or self.o.get("browser_fallback") != "chrome":
                    raise WebUnavailable(
                        f"could not open the bundled browser on the profile: {str(e).splitlines()[0][:160]}",
                        120,
                        "browser",
                    ) from None
                self._pw = await async_playwright().start()  # auto: the installed Chrome, as before
        try:
            # playwright: stock defaults, the window says it is automated (navigator.webdriver is true).
            # patchright (the approach CatGPT-Gateway uses): the same real Chrome and profile, driven without the
            # automation markers, so the site's automatic browser check treats it like the hand-opened window.
            # Launched the way patchright documents: real Chrome, persistent profile, no viewport, no extra flags.
            hider = None
            if self.o["quiet"] and not self.o["headless"]:

                async def hide_early():  # Chrome shows its window as it starts: hide it the moment it exists
                    for _ in range(25):
                        await asyncio.sleep(0.15)
                        hide_chrome(self.profile)

                hider = asyncio.get_running_loop().create_task(hide_early())
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(self.profile),
                channel="chrome",
                headless=bool(self.o["headless"]),
                no_viewport=True,
                args=["--window-size=1180,860"]
                + (
                    # a hidden window must keep rendering and running timers, or replies stop arriving
                    [
                        "--disable-backgrounding-occluded-windows",
                        "--disable-renderer-backgrounding",
                        "--disable-background-timer-throttling",
                    ]
                    if self.o["quiet"]
                    else []
                ),
            )
            if self.o["quiet"] and not self.o["headless"]:
                await self._park()
                hide_chrome(self.profile)
                if hider:
                    hider.cancel()
        except Exception as e:  # noqa: BLE001 - profile in use (weblogin still open), Chrome missing, ...
            await self._shutdown()
            raise WebUnavailable(
                f"could not open Chrome on the chatgpt-web profile: {str(e).splitlines()[0][:160]}", 120, "browser"
            ) from None
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        return await self._watch_page()

    async def _watch_page(self):
        try:  # a just-opened window is still loading its blank first tab; navigating over that gets interrupted
            await self._page.wait_for_load_state("load", timeout=5000)
        except Exception:  # noqa: BLE001
            pass

        def closed(*_):
            if not self._closing:
                self._gone = True

        self._page.on("close", closed)
        self._ctx.on("close", closed)
        if self._bundled:
            self._ctx.browser.on("disconnected", closed)
        return self._page

    # ---- the bundled browser (`browser: bundled | auto`) -----------------------------------------------------------
    async def _bundled_browser(self) -> dict | None:
        """The prepared bundled browser to launch, or None for the installed Chrome."""
        mode = str(self.o.get("browser") or "chrome").lower()
        if mode not in ("chrome", "bundled", "auto"):
            raise WebUnavailable(f"unknown chatgpt-web browser {mode!r} (chrome, bundled or auto)", 3600, "setup")
        if mode == "chrome" or self.o["headless"]:  # headless runs the installed Chrome as before
            return None
        from . import browser_host

        source = str(self.o.get("browser_source") or "cft").lower()
        if source == "chrome":  # a background-only clone of the installed Chrome: instant, so always waited for
            return await asyncio.to_thread(browser_host.ensure_chrome_clone, self.profile)
        if source != "cft":
            raise WebUnavailable(f"unknown chatgpt-web browser_source {source!r} (cft or chrome)", 3600, "setup")
        if mode == "bundled":  # wait for it (a download, the first time)
            return await asyncio.to_thread(browser_host.ensure, self.profile)
        rec = browser_host.ensure_soon(self.profile)  # auto: what is ready now; prepared meanwhile otherwise
        if rec or self.o.get("browser_fallback") == "chrome":
            return rec  # None: the installed Chrome meanwhile, as asked
        # never a window that can show instead: skip the bridge (no penalty) until the bundled one is ready
        st = browser_host.prep_status()
        if st["preparing"]:
            pct = f", {st['progress']:.0%}" if st.get("progress") is not None else ""
            raise WebSkip(f"the bridge's browser is being prepared ({st.get('stage') or 'starting'}{pct})")
        raise WebUnavailable(
            f"the bridge's browser could not be prepared: {st.get('error') or 'unknown error'}",
            browser_host.RETRY_S,
            "browser",
        )

    async def _launch_bundled(self, rec: dict, driver: str):
        """Start the bundled browser on the profile with no window, attach over its debugging port, and open the
        one window already off screen and in the background: nothing is ever drawn on screen and focus stays put
        (macOS keeps a 40x41 px corner of it inside the display)."""
        from . import browser_host

        args = browser_host.launch_args(self.profile, driver, trim=bool(self.o.get("browser_trim", True)))
        port_file = self.profile / "DevToolsActivePort"
        started = time.time()
        self._proc = subprocess.Popen(
            [rec["exe"], *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._watch = watchdog(self._proc.pid)
        self._bundled = rec["version"]
        port = None
        for _ in range(300):
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"it exited at start (code {self._proc.returncode}; is the profile open in another browser?)"
                )
            try:
                if port_file.stat().st_mtime >= started - 1:  # not a stale file from an earlier run
                    port = int(port_file.read_text().split()[0])
                    break
            except (OSError, ValueError, IndexError):
                pass
            await asyncio.sleep(0.1)
        if not port:
            raise RuntimeError("it did not open its debugging port within 30 s")
        browser = await self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=30000)
        self._ctx = browser.contexts[0]
        page = await self._parked_page(window=True)
        self._start_parker()
        return page

    def _start_parker(self):
        """bundled: every window of this browser stays off screen. Windows are created there, but macOS moves a
        window onto a display when the displays change (one is unplugged, a lid opens, the Mac wakes with another
        layout, the bridge started with no display at all) and a page may open a popup window of its own: each
        window's position is checked every `browser_watch_s` seconds, and at once when a page opens, and put back."""
        self._repark = asyncio.Event()
        self._ctx.on("page", lambda _p: self._repark and self._repark.set())
        self._parker = asyncio.get_running_loop().create_task(self._keep_parked(self._ctx))

    async def _keep_parked(self, ctx):
        parked: dict[int, tuple] = {}  # windowId -> its bounds right after it was parked
        cdp = None
        while self._ctx is ctx:
            try:
                if cdp is None:
                    cdp = await ctx.browser.new_browser_cdp_session()
                parked = await park_windows(cdp, parked)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the browser is going away, or a window closed between two calls
                cdp = None
            try:
                await asyncio.wait_for(self._repark.wait(), float(self.o.get("browser_watch_s") or 2.0))
                await asyncio.sleep(0.05)  # let the new window be created before looking
            except TimeoutError:
                pass
            self._repark.clear()

    async def _parked_page(self, window: bool):
        """bundled: a new page created in the background, as a tab of the existing window or (window=True) in a
        new window placed off screen as it is made."""
        params = {"url": "about:blank", "background": True}
        if window:
            params.update(newWindow=True, left=20000, top=20000, width=1180, height=860)
        known = set(self._ctx.pages)
        cdp = await self._ctx.browser.new_browser_cdp_session()
        try:
            async with self._ctx.expect_page(timeout=10000) as info:
                await cdp.send("Target.createTarget", params)
            page = await info.value
        finally:
            await cdp.detach()
        if page in known:
            raise RuntimeError("the new tab was an existing one")
        return page

    def _reap(self, proc, watch) -> str:
        """Wait for the bundled browser to exit after Browser.close; end it if it does not (only ever this process,
        which this bridge started). -> how it ended: "closed", "terminated" or "killed"."""
        how = "closed"
        try:
            if proc:
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    how = "terminated"
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        how = "killed"
                        proc.kill()
                        proc.wait(timeout=10)
        finally:
            stop_watchdog(watch)
        return how

    async def _park(self):
        """quiet: push the window past the bottom right corner of the screen (macOS keeps a 40 pixel corner of it
        there), so that nothing shows even in the moments when Chrome un-hides itself."""
        try:
            cdp = await self._ctx.new_cdp_session(self._ctx.pages[0])
            win = await cdp.send("Browser.getWindowForTarget")
            await cdp.send(
                "Browser.setWindowBounds", {"windowId": win["windowId"], "bounds": {"left": 20000, "top": 20000}}
            )
            await cdp.detach()
        except Exception:  # noqa: BLE001 - best effort; hiding still applies
            pass

    async def _new_page(self):
        async with (self.host or self)._tabs:
            return await self._new_tab()

    async def _new_tab(self):
        page = None
        if (self.host or self)._bundled:  # a background tab of the off-screen window; nothing to hide or park
            try:
                page = await self._parked_page(window=not self._ctx.pages)
            except Exception:  # noqa: BLE001
                page = None
        elif self.o["quiet"] and not self.o["headless"]:
            # an ordinary new tab brings the hidden Chrome back on screen and takes the keyboard; a background tab
            # does not take the keyboard, and hiding again right after keeps the window off screen
            try:
                known = set(self._ctx.pages)
                cdp = await self._ctx.new_cdp_session(self._ctx.pages[0])
                async with self._ctx.expect_page(timeout=10000) as info:
                    await cdp.send("Target.createTarget", {"url": "about:blank", "background": True})
                page = await info.value
                await cdp.detach()
                if page in known:  # never hand out a tab that already belongs to a chat
                    page = None
            except Exception:  # noqa: BLE001 - fall back to the ordinary way
                page = None
        if page is None:
            page = await self._ctx.new_page()
        if self.o["quiet"] and not self.o["headless"] and not (self.host or self)._bundled:
            await self._park()
            hide_chrome(self.profile)

        def closed(*_):
            if not self._closing:
                self._gone = True

        page.on("close", closed)
        return page

    async def _shutdown(self):
        self._closing = True
        if self.host:  # only this bridge's tabs: the Chrome belongs to the host
            pages = [x["page"] for x in self._sessions.values()] + [self._page]
            self._sessions.clear()
            self._ctx = self._pw = self._page = None
            for pg in pages:
                try:
                    if pg and not pg.is_closed():
                        await pg.close()
                except Exception:  # noqa: BLE001
                    pass
            self._closing = self._gone = False
            return
        self._sessions.clear()
        ctx, pw, self._ctx, self._pw, self._page = self._ctx, self._pw, None, None, None
        if self._parker:
            self._parker.cancel()
            self._parker = None
        if self._guardian:
            if self._guardian is not asyncio.current_task():
                self._guardian.cancel()
            self._guardian = None
        proc, watch, bundled = self._proc, self._watch, self._bundled
        self._proc = self._watch = self._bundled = None
        try:
            if ctx and bundled:  # a browser this bridge started itself: ask it to quit (it exits, profile saved)
                cdp = await ctx.browser.new_browser_cdp_session()
                await asyncio.wait_for(cdp.send("Browser.close"), 10)
            elif ctx:
                await ctx.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if pw:
                await pw.stop()
        except Exception:  # noqa: BLE001
            pass
        how = "closed"
        if proc or watch:
            how = await asyncio.to_thread(self._reap, proc, watch)
        self._closing = False
        if (ctx or proc) and self.guard.on:  # a browser was running and has exited: note the store, back it up
            await asyncio.to_thread(self._guard_exit, how)

    # ---- the profile's cookie store (cookie_guard.py) ------------------------------------------------------------
    def _others_on_profile(self) -> list[int]:
        """Processes (not this one's own browser) started on this profile: a browser still exiting, its helpers."""
        want = re.escape(f"--user-data-dir={self.profile}") + "( |$)"
        try:
            r = subprocess.run(["pgrep", "-f", want], capture_output=True, text=True, timeout=10)
            return [int(x) for x in r.stdout.split()]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return []

    async def _profile_free(self):
        """Never start a browser on a profile another one still has open (an earlier one on its way out, a window
        somebody opened): Chrome would hand the launch to that process, or end it to take the profile over. It is
        waited for, never ended. A lock left behind by a process that is gone does not count."""
        from . import browser_host

        end = time.monotonic() + float(self.o["profile_wait_s"])
        while True:
            pid = browser_host.window_pid(self.profile)
            if not pid or pid not in await asyncio.to_thread(self._others_on_profile):
                return
            if time.monotonic() >= end:
                raise WebUnavailable("another browser still has this account's profile open", 120, "browser")
            await asyncio.sleep(0.25)

    async def _launch_gate(self):
        """No browser on a clock that looks wrong (Chrome deletes cookies it takes for expired) or a nearly full
        disk: looked at a few times, then the launch is refused for a while with the reason."""
        tries = max(1, int(self.o["launch_tries"]))
        bad = None
        for i in range(tries):
            bad = await asyncio.to_thread(self.guard.gate)
            if not bad:
                return
            if i + 1 < tries:
                await self.sleep(float(self.o["launch_wait_s"]))
        self.guard.event("launch_refused", why=bad[0], reason=bad[1][:200])
        raise WebUnavailable(bad[1], float(self.o["launch_block_s"]), bad[0])

    async def _guard_before(self, bundled: dict | None) -> dict | None:
        """Before a launch, with nothing on the profile: note the store; if it is a wiped one, put the newest good
        backup back (once a day), else back it up when it changed. -> the build to start (it changes when the
        backup needs the previous one)."""
        from . import browser_host

        g = self.guard
        pre = await asyncio.to_thread(g.snap)
        gen = g.newest()
        lost, g.pending = g.pending or g.lost(pre, gen), None
        if lost:
            g.event("cookies_wiped", where="before launch", lost=lost, now=g.brief(pre), backup=g.brief(gen))
            bundled = await self._guard_restore(lost, gen, bundled)
            pre = await asyncio.to_thread(g.snap)
        else:
            await asyncio.to_thread(g.backup, "prelaunch", False, browser_host.profile_version(self.profile))
        g.pre, g.was = pre, browser_host.profile_version(self.profile)
        return bundled

    async def _guard_restore(self, lost: list[str], gen: dict | None, bundled: dict | None) -> dict | None:
        from . import browser_host

        g = self.guard
        why = None
        if not gen:
            why = cookie_guard.LOST_NO_BACKUP
        elif g.restored_today():
            why = cookie_guard.LOST_AGAIN
        else:
            target = bundled["version"] if bundled else browser_host.installed_chrome_version()
            wrote, now = browser_host.major(gen.get("browser")), browser_host.major(target)
            if wrote and now and wrote != now:
                # written by another major version: measured, the newer one deletes it again. Only together with
                # the build that wrote it (kept on disk, or fetched again), which then stays (browser_host.pin)
                rec = None
                if bundled and str(self.o.get("browser_source") or "cft").lower() == "cft":
                    had = browser_host.pinned()
                    browser_host.pin(wrote, now)
                    try:
                        rec = await asyncio.to_thread(browser_host.ensure, self.profile)
                    except Exception:  # noqa: BLE001
                        rec = None
                    if not rec or browser_host.major(rec["version"]) != wrote:
                        rec = None
                        if had:
                            browser_host.pin(had["major"], had.get("refused") or 0)
                        else:
                            browser_host.unpin()
                    g.event("browser_pinned", major=wrote, refused=now, ok=bool(rec))
                if not rec:
                    why = cookie_guard.LOST_VERSION
                bundled = rec or bundled
        if why:
            self._mark_lost(lost, why)
            return bundled
        try:
            r = await asyncio.to_thread(g.put_back, gen)
        except OSError as e:
            g.event("restore_failed", lost=lost, reason=type(e).__name__)
            self._mark_lost(lost, cookie_guard.LOST_NO_BACKUP)
            return bundled
        g.restored = {"lost": lost, "backup": Path(gen["dir"]).name}
        g.event("cookies_restored", lost=lost, backup=Path(gen["dir"]).name, aside=r["aside"],
                wrote=gen.get("browser"), cookies={k: gen.get(k) for k in ("size", "rows", "sites")})  # fmt: skip
        return bundled

    def _mark_lost(self, lost: list[str], why: str):
        """The saved sign-ins are gone and were not put back: say so on every account it concerns."""
        from . import web_signin

        self.guard.event("cookies_lost", lost=lost, reason=why)
        for b in self.guard.bridges():
            if b.display_name in lost:
                b.save(signed_in=False, checked_at=int(b.clock()), identity=None, doubts={}, notice=None,
                       blocked_until=b.clock() + float(b.o["cooldown_login_s"]), reason=why, kind="cookies_lost")  # fmt: skip
                web_signin.kick(b)  # a saved sign-in, if the person agreed to one

    async def _guard_after(self, last: bool = False) -> bool:
        """After a launch: one line for the record, then (on a profile with sign-ins) watch the store for
        `cookie_settle_s`. True when it was wiped as the browser started and the browser was closed again, for the
        backup to be put back; a store wiped again after that is reported and left as it is."""
        from . import browser_host

        g = self.guard
        version = self._bundled or browser_host.installed_chrome_version()
        gen = g.newest()
        now = await asyncio.to_thread(g.snap)
        g.event("browser_launch", browser=version, was=g.was, cookies=g.brief(now))
        restored, g.restored = g.restored, None
        if not g.worth_watching(g.pre, gen):
            return False
        await self._load_cookies()
        end = time.monotonic() + float(self.o["cookie_settle_s"])
        while True:
            now = await asyncio.to_thread(g.snap)
            lost = g.lost(now, g.pre, gen)
            if lost or time.monotonic() >= end:
                break
            await asyncio.sleep(0.25)
        if not lost:
            if restored:
                g.event("cookies_back", lost=restored["lost"], backup=restored["backup"], cookies=g.brief(now))
                for b in g.bridges():
                    if b.display_name in restored["lost"]:
                        ch: dict = {"notice": {"kind": "cookies_lost", "text": cookie_guard.LOST_RESTORED,
                                               "at": int(b.clock())}, "doubts": {}}  # fmt: skip
                        if b.load().get("kind") in ("page", "login", "cookies_lost"):
                            ch.update(blocked_until=0, reason=None, kind=None)
                        b.save(**ch)
            return False
        g.event("cookies_wiped", where="after launch", lost=lost, before=g.brief(g.pre), now=g.brief(now),
                browser=version, was=g.was)  # fmt: skip
        if last or restored or g.restored_today():
            self._mark_lost(lost, cookie_guard.LOST_AGAIN if restored or g.restored_today()
                            else cookie_guard.LOST_NO_BACKUP)  # fmt: skip
            return False
        g.pending = lost
        await self._shutdown()
        return True

    async def _load_cookies(self):
        """Make the browser read its cookie store now instead of at the first page (what it deletes while reading
        shows up then): ask for the cookies of an address that has none. No cookie comes back."""
        try:
            cdp = await self._ctx.new_cdp_session(self._page)
            try:
                await asyncio.wait_for(cdp.send("Network.getCookies", {"urls": ["https://cookie-guard.invalid/"]}), 5)
            finally:
                await cdp.detach()
        except Exception:  # noqa: BLE001 - best effort
            pass

    def _guard_exit(self, how: str):
        """The browser has exited: one line for the record, and a backup once nothing of it is left running."""
        from . import browser_host

        g = self.guard
        end = time.monotonic() + 8.0
        while (left := self._others_on_profile()) and time.monotonic() < end:
            time.sleep(0.25)  # its helper processes (one of them writes the cookies) follow it out
        version = browser_host.profile_version(self.profile)
        g.event("browser_exit", how=how, browser=version, left=len(left) or None, cookies=g.brief(g.snap()))
        if not left and not browser_host.window_pid(self.profile):
            g.backup("exit", False, version)

    def _guard_poke(self, where: str) -> bool:
        """Look at the store of the running browser now. True when it has been wiped: the browser is then closed
        and the backup put back in the background (requests skip the profile's accounts meanwhile)."""
        if self._recovering and not self._recovering.done():
            return True
        g = self.guard
        sib = [*self._guests, self]
        if not g.on or self._ctx is None or any(b.signin for b in sib):  # (a sign-in window has the profile)
            return False
        try:
            now = g.snap()
            lost = g.lost(now, g.pre, g.newest())
            if not lost:
                return False
            loop = asyncio.get_running_loop()
        except (OSError, RuntimeError):
            return False
        g.event("cookies_wiped", where=where, lost=lost, before=g.brief(g.pre), now=g.brief(now), was=g.was)
        g.pending = lost
        for b in sib:  # from now on requests skip the profile's accounts, without penalty
            b.signin = cookie_guard.RESTORING
        self._recovering = loop.create_task(self._guard_recover())
        return True

    async def _guard_watch(self, ctx):
        """While this browser runs: look at the store again after Chrome has written what it changed at start,
        then a snapshot about hourly."""
        from . import browser_host

        started = time.monotonic()
        try:
            for at in sorted(float(x) for x in self.o.get("cookie_recheck_s") or []):
                await asyncio.sleep(max(0.0, started + at - time.monotonic()))
                if self._ctx is not ctx or self._guard_poke("recheck"):
                    return
            every = float(self.o["cookie_backup_every_s"])
            while every > 0:
                await asyncio.sleep(every)
                if self._ctx is not ctx or self._guard_poke("hourly"):
                    return
                await asyncio.to_thread(self.guard.backup, "hourly", True, browser_host.profile_version(self.profile))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never in the way of the bridge
            pass

    async def _guard_recover(self):
        """A wiped store under a running browser: pause the profile's accounts, let a chat in flight finish (30 s),
        close the browser and start it again, which puts the backup back (_guard_before)."""
        sib = [*self._guests, self]
        held, end = [], time.monotonic() + 30.0
        try:
            for b in sib:
                try:
                    await asyncio.wait_for(b.lock.acquire(), max(0.1, end - time.monotonic()))
                    held.append(b)
                except TimeoutError:
                    pass  # it goes anyway: that request is skipped without penalty (_guarded)
            for b in sib:
                await b._shutdown()
            async with self._launch:
                try:
                    await self._ensure_own_page()
                    self._arm_idle()
                except WebError as e:
                    self.guard.event("launch_failed", reason=str(e)[:200])
        finally:
            for b in held:
                b.lock.release()
            for b in sib:
                if b.signin == cookie_guard.RESTORING:
                    b.signin = None

    async def release(self):
        """Close the window (frees the profile for `weblogin`)."""
        async with self.lock:
            await self._shutdown()

    def _arm_idle(self):
        if self._idle:
            self._idle.cancel()

        async def later():
            await asyncio.sleep(float(self.o["idle_close_s"]))
            if not self.lock.locked():
                await self.release()

        try:
            self._idle = asyncio.get_running_loop().create_task(later())
        except RuntimeError:
            self._idle = None

    async def _probe(self, page, fresh: bool) -> dict:
        for _ in range(8):  # the page may navigate under the probe (a browser check passing, a redirect): look again
            try:
                return await self._probe_once(page, fresh)
            except Exception as e:  # noqa: BLE001
                if "context was destroyed" not in str(e) and "navigation" not in str(e):
                    raise
                await asyncio.sleep(0.5)
        return await self._probe_once(page, fresh)

    async def _probe_once(self, page, fresh: bool) -> dict:
        if self.site:
            return await page.evaluate(SITE_PROBE_JS, [self.site, fresh])
        return await page.evaluate(PROBE_JS, fresh)

    async def _main_world(self, page, js: str):
        try:  # patchright runs page scripts in a world of their own unless told otherwise
            return await page.evaluate(js, isolated_context=False)
        except TypeError:
            return await page.evaluate(js)

    async def _read(self, page, final: bool = False) -> dict:
        r = await page.evaluate(READ_JS, self.site)
        if final and self.site and self.site.get("copy"):
            try:
                await self._main_world(page, CAPTURE_JS)
                if await page.evaluate(COPY_CLICK_JS, self.site):
                    for _ in range(10):
                        await asyncio.sleep(0.2)
                        got = await self._main_world(page, "() => window.__pcoreCap || []")
                        if got:
                            if got[-1].strip():
                                r["md"] = got[-1]
                            break
            except Exception:  # noqa: BLE001 - the elements' text is the fallback
                pass
        return r

    def _strike(self) -> float:
        """One more failure in a row -> how long to stay away now (doubles each time, up to backoff_max_s)."""
        n = int(self.load().get("strikes", 0)) + 1
        self.save(strikes=n)
        return min(float(self.o["backoff_base_s"]) * 2 ** (n - 1), float(self.o["backoff_max_s"]))

    def _notices(self, p: dict):
        if p.get("busy") and not p.get("challenge"):
            who = self.site["name"] if self.site else "ChatGPT"
            said = (p.get("busyContext") or p["busy"])[:360]  # the whole notice: it says whether it is a limit
            raise WebUnavailable(f"{who} is turning chats away for now ({said!r})", self._strike(), "busy")
        """Anything on the page that means "stop": never interact with it, just report."""
        if p.get("challenge"):
            raise WebUnavailable(
                f"the page shows a challenge or block notice ({p['challenge']!r}); open the window yourself to see it",
                float(self.o["cooldown_challenge_s"]),
                "challenge",
            )
        if p.get("limit"):
            raise WebUnavailable(
                f"{self.site['name'] if self.site else 'ChatGPT'} reports a usage limit ({p['limit']!r})",
                float(self.o["cooldown_limit_s"]),
                "limit",
            )

    async def _open_chat(self, model: str, page=None) -> dict:
        """A fresh temporary chat, ready for typing. Raises WebUnavailable for anything a user would have to sort
        out by hand."""
        page = page or await self._ensure_page()
        url = f"{self.base_url}{self.site['path']}" if self.site else f"{self.base_url}/?temporary-chat=true"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:  # noqa: BLE001 - a tab that is still loading its blank page drops the first try
            if "interrupted" not in str(e) and "ERR_ABORTED" not in str(e):
                raise
            await asyncio.sleep(1.0)
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        deadline, ok, p, challenged = time.time() + 40, 0, {}, None
        while True:
            p = await self._probe(page, True)
            if p.get("challenge"):  # an automatic "just a moment" check may pass by itself; wait like a user would
                challenged = challenged or time.time()
                if time.time() - challenged > float(self.o["challenge_wait_s"]):
                    self._notices(p)
            elif p.get("login"):
                ok += 1
                if ok >= 3:
                    raise WebUnavailable(
                        "not signed in to ChatGPT in the chatgpt-web profile",
                        float(self.o["cooldown_login_s"]),
                        "login",
                    )
            elif p.get("composer"):
                ok += 1
                # a site whose temporary mode comes on a moment after the page (Qwen): give it time before judging
                slow_temp = bool(
                    self.site
                    and self.site.get("temp")
                    and not self.site.get("temp_toggle")
                    and p.get("temp") is not True
                )
                if ok >= (2 if p.get("account") else 4) and not (slow_temp and time.time() < deadline - 25):
                    break
            else:
                ok = 0
            if time.time() > deadline:
                if p.get("challenge"):
                    self._notices(p)
                raise WebError(f"chatgpt.com did not load a usable page (title {p.get('title')!r})")
            await asyncio.sleep(0.6)
        self._notices(p)
        self._seen_signed_in()
        if p.get("temp") is not True:  # the url flag didn't take: try the page's own toggle once
            toggle = page.locator('button[aria-label*="temporary chat" i]').first
            try:
                for _ in range(3):  # the toggle can be there before it works (Gemini): press again if it did not take
                    try:
                        await toggle.click(timeout=3000)
                    except Exception:  # noqa: BLE001 - a hidden window does not take clicks; the script click does
                        await toggle.evaluate("el => el.click()", timeout=3000)
                    await asyncio.sleep(1.5)
                    p = await self._probe(page, True)
                    if p.get("temp") is True:
                        break
                    toggle = page.locator('button[aria-label^="Turn on temporary chat" i]').first
                    if not await toggle.count():
                        toggle = page.locator('button[aria-label*="temporary chat" i]').first
            except Exception:  # noqa: BLE001
                pass
        if p.get("temp") is not True or p.get("users") or p.get("assistants"):
            raise WebUnavailable(
                "temporary chat is not available on the page, so nothing was sent (a normal chat "
                "would be kept in the account's history)",
                float(self.o["cooldown_tempchat_s"]),
                "tempchat",
            )
        if model and model.lower() != "auto":
            p = await self._pick_model(page, model, p)
        return p

    async def _pick_model(self, page, model: str, p: dict) -> dict:
        want = model.lower()
        if self.site:
            return await self._pick_site_model(page, want, p)

        def auto_ok(shown):
            return bool(self.o["allow_auto_fallback"] and re.search(r"(?:^|\s)auto$", shown or "", re.I))

        if want in (p.get("model") or "").lower() or auto_ok(p.get("model")):
            return p
        if p.get("model") is None:  # no model picker at all (the Free plan): the account has one model, and it answers
            return p
        try:
            await page.locator(self.site["model"] if self.site else SWITCHER).first.click(timeout=4000)
            item = (
                page.locator('[role="menuitem"], [role="menuitemradio"], [role="option"]')
                .filter(has_text=re.compile(re.escape(model), re.I))
                .first
            )
            await item.click(timeout=4000)
            await asyncio.sleep(0.8)
        except Exception:  # noqa: BLE001
            try:
                await page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
        shown, p = p.get("model"), await self._probe(page, True)
        # a picker that is gone on the second look was something else on a half-loaded page: same as no picker
        if (
            p.get("model") is not None
            and want not in p["model"].lower()
            and self.o["strict_model"]
            and not auto_ok(p["model"])
        ):
            raise WebUnavailable(
                f"model {model!r} is not selectable in the page's model picker (it shows "
                f"{p['model']!r}, before the attempt {shown!r})",
                float(self.o["cooldown_model_s"]),
                "model",
            )
        if p.get("temp") is not True:
            raise WebUnavailable(
                "temporary chat was lost while picking the model, so nothing was sent",
                float(self.o["cooldown_tempchat_s"]),
                "tempchat",
            )
        return p

    async def _pick_site_model(self, page, want: str, p: dict) -> dict:
        """SITES: the picker button shows the model's name first; its menu is opened with the keyboard (a hidden
        window does not take clicks) and the entry with exactly that name is chosen."""

        def shown(p):  # the picker button: "GLM-5.3-Flash Max", "Qwen4-Max", "Hydrogen"
            return want in (p.get("model") or "").lower().split()

        if shown(p) or p.get("model") is None:
            return p
        try:
            opener = page.locator(self.site["model"]).first
            items = (
                '[role="menuitem"], [role="menuitemradio"], [role="option"], [data-radix-popper-content-wrapper] button'
            )
            await opener.focus(timeout=4000)
            await page.keyboard.press("Enter")
            await asyncio.sleep(1.0)
            if not await page.locator(items).count():
                await opener.evaluate("el => el.click()")
                await asyncio.sleep(1.0)
            picked = await page.evaluate(
                r"""(want) => { for (const e of document.querySelectorAll('[role="menuitem"], [role="menuitemradio"], [role="option"], [data-radix-popper-content-wrapper] button')) {
                     if ((e.innerText || '').toLowerCase().split(/\s+/).includes(want)) { e.click(); return true; } } return false; }""",
                want,
            )
            if not picked:
                await page.keyboard.press("Escape")
            await asyncio.sleep(0.8)
        except Exception:  # noqa: BLE001
            pass
        p = await self._probe(page, True)
        if not shown(p) and self.o["strict_model"]:
            raise WebUnavailable(
                f"model {want!r} is not selectable on {self.site['name']} (the page shows {p.get('model')!r})",
                float(self.o["cooldown_model_s"]),
                "model",
            )
        return p

    async def _send(self, page, text: str, on_text=None) -> dict:
        """Type one message, send it, wait for the reply to finish. -> {md, slug}"""
        before = await self._probe(page, False)
        if before.get("full"):
            raise WebUnsent(f"this chat is full ({before['full']!r})")
        box = page.locator(self.site["composer"] if self.site else COMPOSER).first
        try:  # a click waits for the element to hold still, which a hidden window may never report: focus is enough
            await box.click(timeout=2500)
        except Exception:  # noqa: BLE001
            await box.focus(timeout=10000)
        want = len(re.sub(r"\s+", "", text))
        for attempt in range(3):  # the editor is sometimes not ready right after the previous reply: type again
            await box.fill(text, timeout=30000)
            typed = await box.evaluate(
                "el => (el.value !== undefined ? el.value : el.innerText || '').replace(/\\s+/g, '').length"
            )
            if typed >= 0.9 * want or attempt == 2:
                break
            await asyncio.sleep(1.5)
            await box.focus(timeout=10000)
        if typed < 0.9 * want:
            # the page's box holds less than this message (free Gemini stops near 25k characters)
            raise WebTooLong(
                f"the composer took {typed} of {len(text)} characters (message too long for the page?)",
                int(len(text) * typed / max(1, want) * 0.9),
            )
        self.save(count=self.load().get("count", 0) + 1)
        send = page.locator(self.site["send"] if self.site else SEND).first
        try:
            await send.click(timeout=3000)
        except Exception:  # noqa: BLE001 - hidden window (see above), or no recognisable send button
            try:
                await send.evaluate("el => el.click()", timeout=5000)
            except Exception:  # noqa: BLE001 - Enter sends too
                await box.press("Enter")
        t0 = time.time()
        timeout = float(self.o["reply_timeout_s"])
        started, stable, last_len, empty, bad = False, 0, -1, 0, 0
        said, tick = "", 0
        while True:
            tick += 1
            await asyncio.sleep(0.5)
            p = await self._probe(page, False)
            self._notices(p)
            if p.get("login"):
                raise WebUnavailable("signed out of ChatGPT mid-request", float(self.o["cooldown_login_s"]), "login")
            # a new message is known by its id as well as by the count: a long chat drops old turns from the page
            new_user = p["users"] > before["users"] or bool(
                p.get("lastUser") and p["lastUser"] != before.get("lastUser")
            )
            new_reply = p["assistants"] > before["assistants"] or bool(
                p.get("lastAsst") and p["lastAsst"] != before.get("lastAsst") and p.get("asstAfterUser")
            )
            if env("CHATGPT_WEB_TRACE"):  # one line per look at the page, for working out timing problems
                print(
                    f"trace {time.time() - t0:5.1f}s stop={p['stop']} users={p['users']} asst={p['assistants']} "
                    f"len={p['lastLen']} id={p.get('lastAsst')} after={p.get('asstAfterUser')} "
                    f"state={p.get('lastState')} new_reply={new_reply}",
                    flush=True,
                )
            if not started:
                started = new_user or new_reply or p["stop"]
                if not started and p.get("full"):
                    raise WebUnsent(f"this chat is full ({p['full']!r})")
                if not started and time.time() - t0 > 30:
                    raise WebUnsent("the message was not sent" + (f": {p['error']}" if p.get("error") else ""))
                if not started:
                    continue
            if p.get("error") and not p["stop"] and not new_reply:
                raise WebError(f"ChatGPT showed an error: {p['error']}")
            if p.get("turnError") and not p["stop"] and not new_reply:
                bad = bad + 1
                if bad >= 4:  # still there after two seconds: the page gave this instead of a reply
                    await self._dump(page, p, "turn-error")
                    if re.search(r"too long", p["turnError"], re.I):
                        raise WebTooLong(f"ChatGPT refused the message: {p['turnError']}", int(len(text) * 0.7))
                    raise WebError(f"ChatGPT showed an error: {p['turnError']}")
            else:
                bad = 0
            done = False
            if not p["stop"] and new_reply and p["lastLen"] > 0:
                stable = stable + 1 if p["lastLen"] == last_len else 0
                finished = p["actions"] > before["actions"] or p.get("lastState") == "complete"
                done = stable >= (2 if finished else 8)
            else:
                stable = 0
            # a reply that is marked finished but has no text never gets any: do not sit out the whole timeout
            empty = (
                empty + 1
                if new_reply and not p["stop"] and not p["lastLen"] and p.get("lastState") == "complete"
                else 0
            )
            if empty >= 40:
                await self._dump(page, p, "empty")
                raise WebError("the reply was empty")
            if on_text and new_reply and p["lastLen"] != last_len and tick % 2 == 0:
                # whole paragraphs before the first code block are final: pass them on while the rest is written
                try:
                    md = (await self._read(page)).get("md") or ""
                except Exception:  # noqa: BLE001 - streaming is a nicety; the full read below is what counts
                    md = ""
                cut = md.find("```")
                safe = md[: cut if cut >= 0 else len(md)]
                safe = safe[: max(safe.rfind("\n\n"), 0)]
                if len(safe) > len(said) and safe.startswith(said):
                    on_text(safe[len(said) :])
                    said = safe
            last_len = p["lastLen"]
            if done:
                break
            if time.time() - t0 > timeout:
                await self._dump(page, p, "timeout")
                raise WebError(
                    f"no complete reply after {int(timeout)} s (page state saved in {self.home / 'chatgpt-web-debug'})"
                )
        r = await self._read(page, final=True)
        if not (r.get("md") or "").strip():
            raise WebError("the reply was empty")
        if r.get("slug"):
            self.save(model_seen=r["slug"])
        return r

    async def _dump(self, page, probe: dict, why: str):
        """Keep what the page looked like when a reply could not be read (the last few, for fixing the markers)."""
        try:
            if self._autologin_running or await page.evaluate(SECRET_PAGE_JS):
                return  # a sign-in page, or a filled password field: nothing of it is ever written down
            d = self.home / "chatgpt-web-debug"
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            for old in sorted(d.glob("*.json"))[:-4]:
                old.unlink()
            html = await page.evaluate("""() => { const t = [...document.querySelectorAll('[data-turn-key], [data-message-author-role], article')];
                const e = t[t.length - 1] || document.querySelector('main') || document.body; return e.outerHTML.slice(0, 400000); }""")
            read = await self._read(page)
            read["page_tail"] = await page.evaluate("() => (document.body.innerText || '').slice(-1500)")
            (d / f"{time.strftime('%Y%m%d-%H%M%S')}-{why}.json").write_text(
                json.dumps({"why": why, "probe": probe, "read": read, "last_turn_html": html}, indent=1)
            )
        except Exception:  # noqa: BLE001 - diagnostics only
            pass

    async def _gap(self):
        wait = self._last_done + self.rand(float(self.o["min_gap_s"]), float(self.o["max_gap_s"])) - self.clock()
        if self._last_done and wait > 0:
            await self.sleep(wait)

    async def _guarded(self, fn):
        """Run a page interaction; map "the window went away" and block-worthy notices to WebUnavailable."""
        try:
            return await fn()
        except WebUnavailable as e:
            model = getattr(self, "_model", None)
            if e.kind == "limit" and self.site and model and self.model_usage(model) is not None:
                self._model_limited(model, e)  # this model's allowance only: the account's other models go on
            elif e.kind in ("login", "challenge", "limit", "tempchat", "model", "closed", "busy", "page", "clock",
                            "disk"):  # fmt: skip
                self._block(e)
            raise
        except WebError:
            raise
        except Exception as e:  # noqa: BLE001
            if self.signing_in():  # the browser was closed under this request to open a sign-in window: no penalty
                self._gone = False
                await self._shutdown()
                raise WebSkip("this account's browser was closed for a sign-in") from None
            if self._gone or self._page is None or self._page.is_closed() or "closed" in str(e).lower():
                self._gone = False
                await self._shutdown()
                err = WebUnavailable("the ChatGPT window was closed", float(self.o["cooldown_closed_s"]), "closed")
                self._block(err)
                raise err from None
            raise WebError(f"page automation failed: {str(e).splitlines()[0][:200]}") from None

    async def _acquire(self, wait: float | None):
        try:
            await asyncio.wait_for(self.lock.acquire(), self.o["queue_wait_s"] if wait is None else wait)
        except TimeoutError:
            raise WebSkip("another request is using the ChatGPT window") from None

    # ---- the first message of a new chat ---------------------------------------------------------------------------
    def _pruned(self, body: dict) -> tuple[dict, dict | None]:
        popts = prune.config("web", self.o.get("prune"))
        return prune.prune_body(body, "fresh", popts) if popts else (body, None)

    def fresh_prompt(self, body: dict) -> tuple[str, dict, dict, bool, dict]:
        """The whole request as one message for a new chat: pruned (`prune`), then, when it is still over
        `fresh_prompt_chars`, summarised (`compact`) or else cut (the blunt cut). -> (prompt, tools, rules, whether
        the history was shortened, {"pruned": prune's record, "compaction": compaction's record})."""
        limit = min(int(self.o["fresh_prompt_chars"]), int(self.load().get("page_chars") or 10**9))
        prompt, tools, rules = render_prompt(body)
        fresh, pruned = self._pruned(body)
        info = {"pruned": pruned, "compaction": None}
        if pruned and pruned["saved"]:
            prompt, tools, rules = render_prompt(fresh)
        if len(prompt) <= limit:
            return prompt, tools, rules, False, info
        copts = compact.config("web", self.o.get("compact"))
        if copts:
            prompt, tools, rules, info["compaction"] = summarised_prompt(body, fresh, limit, copts, self.compactor)
            if info["compaction"]:
                return prompt, tools, rules, True, info
        prompt, tools, rules, compacted = compact_initial_prompt(fresh, limit)
        return prompt, tools, rules, compacted, info

    async def warm(self, body: dict):
        """Before the window is taken: when compaction asks for a model-written brief, have it written (or found in
        the cache) now, so that fresh_prompt, which runs while the window is held, finds it ready. Bounded by the
        compaction's model_timeout_s; on any failure the brief is the rules-only one."""
        copts = compact.config("web", self.o.get("compact"))
        if not (copts and copts.get("model") and self.summarize):
            return
        fresh, _ = self._pruned(body)
        try:
            await self.compactor.acompact(
                body.get("messages") or [],
                fresh.get("messages") or [],
                _web_budget(fresh, int(self.o["fresh_prompt_chars"])),
                copts,
                _web_size,
                "web",
                self.summarize,
            )
        except Exception as e:  # noqa: BLE001 - a brief is an optimisation: never fail the request over it
            print(f"chatgpt-web: compaction warm-up failed: {e}", flush=True)

    # ---- public --------------------------------------------------------------------------------------------------
    async def complete(
        self, model: str, body: dict, model_id: str | None = None, wait: float | None = None, on_text=None
    ) -> dict:
        """One completion request -> a fresh or safely continued temporary chat. `on_text(piece)` gets the reply's
        prose as it is written (see LiveStream)."""
        if self.o["lean"]:
            body = lean_body(body, int(self.o["lean_system_chars"]))
        prompt, tools, rules = render_prompt(body)
        self._precheck(model)
        key = continuation_key(body, model) if self.o["reuse_chat"] else None
        messages = [m for m in body.get("messages") or [] if m.get("role") not in ("system", "developer")]
        setup = json.dumps(
            {
                k: body.get(k)
                for k in ("tools", "tool_choice", "response_format", "parallel_tool_calls", "_single_tool_call")
            },
            sort_keys=True,
            default=str,
        )
        setup += json.dumps(
            [m for m in body.get("messages") or [] if m.get("role") in ("system", "developer")],
            sort_keys=True,
            default=str,
        )

        def continues() -> bool:
            session = self._sessions.get(key) if key else None
            return bool(
                session
                and not session["page"].is_closed()
                and session["setup"] == setup
                and len(messages) > len(session["seen"])
                and all(_message_shape(a) == _message_shape(b) for a, b in zip(messages, session["seen"], strict=False))
            )

        if not continues() and len(prompt) > int(self.o["fresh_prompt_chars"]):
            await self.warm(body)  # a model's brief, if one is wanted, is written now: never while holding the window
        self.busy += 1
        try:
            await self._acquire(wait)
        except BaseException:
            self.busy -= 1
            raise
        try:
            self._precheck(model)
            self._model = model
            await self._gap()

            session = self._sessions.get(key) if key else None
            reuse = continues()
            compacted, fresh_info = False, {"pruned": None, "compaction": None}
            said_live: list[str] = []  # what a streaming client was already sent of the first reply

            def heard(piece: str):
                said_live.append(piece)
                on_text(piece)

            if reuse:
                page = session["page"]
                delta = messages[len(session["seen"]) :]
                prompt = continuation_prompt(delta, tools, session["reply"])
                self._sessions.pop(key)
                self._sessions[key] = session  # most recently used tab goes last
            else:
                if key and session:
                    self._sessions.pop(key)
                if key and session and not session["page"].is_closed():
                    page = session["page"]  # a changed transcript starts a fresh chat in its own tab
                else:
                    await self._guarded(self._ensure_page)
                    page = await self._new_page() if key and self._sessions else self._page
                # Pruning and compaction happen only here, on the text a new chat starts with. Whether a later
                # request continues this chat is decided on the messages as the client sent them (session["seen"]
                # below), so neither can make a chat look changed, and a continued chat is typed exactly as sent.
                prompt, tools, rules, compacted, fresh_info = self.fresh_prompt(body)
            if len(prompt) > int(self.o["max_prompt_chars"]):
                raise WebSkip(f"prompt is {len(prompt)} characters, over the bridge's {self.o['max_prompt_chars']}")
            self._page = page

            async def run():
                if reuse:
                    p = await self._probe(page, False)
                    self._notices(p)
                    if p.get("temp") is not True:
                        raise WebUnavailable(
                            "temporary chat was lost", float(self.o["cooldown_tempchat_s"]), "tempchat"
                        )
                else:
                    await self._open_chat(model, page)
                    await self._read_plan(page, stale_only=True)
                    if self.needs_confirmation():  # it turned out to be on a paid plan: nothing is sent
                        raise WebSkip(f"this account ({self.account or 'main'}) is on a paid plan: not confirmed")
                r = await self._send(page, prompt, heard if on_text else None)
                repaired = False
                try:
                    text, calls = parse_reply(r["md"], tools, rules)
                except Malformed as e:  # one repair round, in the same temporary chat
                    repaired = True
                    await self.sleep(self.rand(1.0, 2.5))
                    r = await self._send(page, repair_message(e))
                    try:
                        text, calls = parse_reply(r["md"], tools, rules)
                    except Malformed as e2:
                        raise WebError(f"unusable tool call after one repair: {e2}") from None
                nudged = False
                if (
                    not calls and not repaired and stalled(text, tools)
                ):  # "the shell isn't available to me": say how, once
                    nudged = True
                    await self.sleep(self.rand(1.0, 2.5))
                    r2 = await self._send(page, nudge_message(tools))
                    try:
                        text, calls = parse_reply(r2["md"], tools, rules)
                        r = r2
                    except Malformed:
                        pass  # keep the first answer
                return (
                    text,
                    calls,
                    {
                        "model_slug": r.get("slug"),
                        "repaired": repaired,
                        "nudged": nudged,
                        "continued": reuse,
                        "sent_chars": len(prompt),
                        "account": self.account,
                        "compacted": False if reuse else compacted,
                        "pruned": None if reuse else fresh_info["pruned"],
                        "compaction": None if reuse else fresh_info["compaction"],
                    },
                )

            try:
                try:
                    try:
                        text, calls, extra = await self._guarded(run)
                    except WebTooLong as e:
                        # the page takes less than this message. A new chat: remember the size it takes (for this
                        # account) and send the history shortened to it, in the same request. A continued chat:
                        # it has outgrown the page, so start a new one from the caller's history
                        if not reuse and e.limit < 4000:
                            raise
                        if not reuse:
                            self.save(page_chars=e.limit)
                        reuse = False
                        prompt, tools, rules, compacted, fresh_info = self.fresh_prompt(body)
                        try:
                            text, calls, extra = await self._guarded(run)
                        except WebTooLong as e2:  # still too long: once more, smaller again
                            if e2.limit < 4000:
                                raise
                            self.save(page_chars=e2.limit)
                            prompt, tools, rules, compacted, fresh_info = self.fresh_prompt(body)
                            text, calls, extra = await self._guarded(run)
                        extra["shortened_to"] = int(self.load().get("page_chars") or 0) or None
                except WebUnsent as e:
                    if not reuse:
                        # a new chat that did not take the message (a page not quite ready just after the browser
                        # started): nothing was sent, so open the chat again and send once more
                        await self.sleep(self.rand(1.0, 2.5))
                        text, calls, extra = await self._guarded(run)
                        extra["resent"] = True
                    else:
                        # the chat is full or stopped taking messages: nothing was sent, so carry on in a fresh
                        # chat with the history the caller sent (shortened), in the same request
                        await self._dump(page, {"reason": str(e)}, "full")
                        # the same first message a new chat gets (pruned, summarised or cut); a model's brief only
                        # if one is already cached, since the window is held here
                        reuse = False
                        prompt, tools, rules, compacted, fresh_info = self.fresh_prompt(body)
                        text, calls, extra = await self._guarded(run)
                        extra["restarted"] = True
            except Exception as e:
                if key:
                    self._sessions.pop(key, None)  # the page may contain a partial reply
                if isinstance(e, WebError) and not isinstance(e, WebUnavailable | WebSkip):
                    # the page failed without saying why (an error banner, nothing sent, no reply): the second
                    # time in a row is treated like a temporary limit
                    if int(self.load().get("strikes", 0)) >= 1:
                        # its own kind: the site said nothing, the page just did not work (often this computer
                        # being too busy to load it in time), so the wait stays short
                        wait = min(self._strike(), float(self.o["page_backoff_max_s"]))
                        self._block(WebUnavailable(f"repeated page errors ({str(e)[:120]})", wait, "page"))
                    else:
                        self.save(strikes=1)
                raise
            st = self.load()
            if st.get("strikes") or st.get("doubts") or not st.get("temp_ok"):
                self.save(strikes=0, doubts={}, temp_ok=True)  # a reply came back, in a temporary chat
            self._model_replied(model)
            fmt = (body.get("response_format") or {}).get("type")
            if fmt in ("json_schema", "json_object") and not calls:
                text = re.sub(r"^```\w*\n|\n```$", "", text.strip())
            data = chat_completion(model_id or f"chatgpt-web/{model}", text, calls, prompt, extra)
            if key:
                kept = data["choices"][0]["message"]
                early = "".join(said_live)
                if early and not (kept.get("content") or "").startswith(early):
                    # a repair or nudge replaced a reply whose first paragraphs had already gone out: the client
                    # holds those as this turn's text (LiveStream sends nothing more), and the next request is
                    # compared with what the client holds, or the chat would look changed and start over
                    kept = {**kept, "content": early}
                self._sessions[key] = {
                    "page": page,
                    "seen": messages + [kept],
                    "reply": data["choices"][0]["message"],
                    "setup": setup,
                }
                while len(self._sessions) > max(1, int(self.o["max_chat_tabs"])):
                    old_key = next(iter(self._sessions))
                    old = self._sessions.pop(old_key)
                    if old["page"] is not page:
                        self._closing = True
                        try:
                            await old["page"].close()
                        finally:
                            self._closing = False
            return data
        finally:
            self._last_done = self.clock()
            (self.host or self)._arm_idle()
            self.busy -= 1
            self.lock.release()

    async def _read_identity(self, page):
        """Who is signed in, as the open page itself shows it (IDENTITY_JS; nothing is clicked, sent or fetched).
        Kept with the account's state; None when the page does not show it."""
        try:
            who = await page.evaluate(IDENTITY_JS, self.site_info.get("identity"))
        except Exception:  # noqa: BLE001
            who = None
        self.save(identity=who if isinstance(who, str) and who.strip() else None)

    async def check(self, model: str = "auto", close: bool = False) -> dict:
        """Open a fresh temporary chat without sending anything: is the login there, is temporary chat there?"""
        await self._acquire(None)
        probe_page = None
        prior_page = self._page
        try:
            try:
                await self._guarded(self._ensure_page)
                if self._sessions:
                    probe_page = await self._new_page()
                p = await self._guarded(lambda: self._open_chat(model, probe_page))
                self.reset()
                await self._read_identity(probe_page or self._page)
                await self._read_plan(probe_page or self._page)
                return {**self.status(), "ok": True, "page_model": p.get("model")}
            except WebUnavailable as e:
                return {**self.status(), "ok": False, "reason": e.reason, "kind": e.kind}
            except WebError as e:
                return {**self.status(), "ok": False, "reason": str(e), "kind": "error"}
        finally:
            if probe_page:
                self._closing = True
                try:
                    await probe_page.close()
                finally:
                    self._closing = False
                self._page = prior_page
            if close:
                await self._shutdown()
            else:
                self._arm_idle()
            self.lock.release()


class BridgePool:
    """Several signed-in accounts, one Bridge (Chrome profile, window, daily count, cooldowns) each. A request goes
    to the account already holding its chat, else to the least busy account that is not cooling down; an account
    that turns it away passes it to the next."""

    def __init__(self, base_url: str = "https://chatgpt.com", options: dict | None = None, hosts=None, **kw):
        names = [str(n) for n in (options or {}).get("accounts") or ["main"]]
        self.options, self.kw = dict(options or {}), kw
        self.bridges = {
            n: Bridge(base_url, options, account=n, host=(hosts or {}).get(n), **kw) for n in dict.fromkeys(names)
        }
        first = next(iter(self.bridges.values()))
        self.o, self.profile, self.base_url = first.o, first.profile, first.base_url
        self._turn = 0

    def add(self, account: str, host: Bridge | None = None) -> Bridge:
        """One more account (POST /web/accounts): its own profile, state and window, made on first use."""
        if account not in self.bridges:
            first = next(iter(self.bridges.values()), None)
            b = self.bridges[account] = Bridge(self.base_url, self.options, account=account, host=host, **self.kw)
            if first:  # what the router gave the others
                b.compactor, b.summarize, b.on_revived = first.compactor, first.summarize, first.on_revived
        return self.bridges[account]

    async def remove(self, account: str):
        """Drop an account: its window is closed; its profile directory and state file stay on disk."""
        b = self.bridges.pop(account, None)
        if b:
            await b.release()
            if b.host and b in b.host._guests:
                b.host._guests.remove(b)

    def pick(self, account: str | None = None) -> Bridge:
        if account and account not in self.bridges:
            raise KeyError(f"no account {account!r} (accounts: {', '.join(self.bridges)})")
        return self.bridges[account] if account else next(iter(self.bridges.values()))

    def capacity(self, accounts=None, model: str | None = None) -> dict:
        """What is left today on these accounts (all of them by default) and how busy they are; the router shares
        requests out between sources by it. With `model`, that model's own allowance counts where it has one."""
        bs = [b for n, b in self.bridges.items() if not accounts or n in accounts]
        open_ = [b for b in bs if b.capacity()["accounts"]]
        left = [b._left(model) for b in open_]
        cap, used = sum(c for c, _ in left), sum(u for _, u in left)
        return {
            "remaining": (cap - used) / cap if cap else 0.0,
            "busy": sum(b.busy for b in bs),
            "accounts": len(open_),
            "available": bool(open_) and used < cap,
            "signed_in": any(b.status()["signed_in"] is True for b in bs),
        }

    def _order(self, model: str, body: dict, accounts=None) -> list[Bridge]:
        lean = lean_body(body, int(self.o["lean_system_chars"])) if self.o["lean"] else body
        key = continuation_key(lean, model)
        self._turn += 1
        bs = [b for n, b in self.bridges.items() if not accounts or n in accounts]
        if accounts is None and not bs:
            bs = list(self.bridges.values())
        rot = {id(b): (i - self._turn) % len(bs) for i, b in enumerate(bs)}  # ties go round-robin
        return sorted(bs, key=lambda b: (key not in b._sessions, b.busy, b.load().get("count", 0) // 10, rot[id(b)]))

    async def complete(
        self,
        model: str,
        body: dict,
        model_id: str | None = None,
        wait: float | None = None,
        on_text=None,
        accounts=None,
    ) -> dict:
        last: Exception | None = None
        said = []

        def heard(piece):
            said.append(piece)
            on_text(piece)

        for b in self._order(model, body, accounts):
            try:
                return await b.complete(model, body, model_id=model_id, wait=wait, on_text=heard if on_text else None)
            except (WebUnavailable, WebSkip) as e:
                last = e
                if said:  # part of a reply already went out: another account would say it twice
                    break
        raise last or WebSkip("none of this provider's accounts has this model")

    def status(self) -> dict:
        per = {n: b.status() for n, b in self.bridges.items()}
        soonest = min(per.values(), key=lambda s: s["blocked_for_s"])
        return {
            "signed_in": True
            if any(s["signed_in"] for s in per.values())
            else False
            if all(s["signed_in"] is False for s in per.values())
            else None,
            "checked_at": max((s["checked_at"] or 0 for s in per.values()), default=0) or None,
            "today": sum(s["today"] for s in per.values()),
            "daily_cap": sum(int(s["daily_cap"]) for s in per.values()),
            "blocked": all(s["blocked"] for s in per.values()),
            "blocked_for_s": soonest["blocked_for_s"],
            "reason": soonest["reason"],
            "kind": soonest["kind"],
            "model_seen": next((s["model_seen"] for s in per.values() if s["model_seen"]), None),
            "window_open": any(s["window_open"] for s in per.values()),
            "profile": str(self.profile),
            "accounts": per,
        }

    def reset(self, account: str | None = None):
        for b in [self.pick(account)] if account else self.bridges.values():
            b.reset()

    async def release(self, account: str | None = None):
        for b in [self.pick(account)] if account else self.bridges.values():
            await b.release()

    async def check(self, model: str = "auto", close: bool = False, account: str | None = None) -> dict:
        results = {}
        for n, b in self.bridges.items():
            if not account or n == account:
                results[n] = await b.check(model, close=close)
        ok = [r for r in results.values() if r["ok"]]
        return {
            **self.status(),
            "ok": bool(ok),
            "page_model": ok[0].get("page_model") if ok else None,
            **({} if ok else {"reason": next(iter(results.values())).get("reason")}),
            "checks": {n: {"ok": r["ok"], "reason": r.get("reason")} for n, r in results.items()},
        }


def make_bridge(base_url: str, options: dict | None = None, host=None):
    """A Bridge, or a BridgePool when the provider lists `accounts`. `host`: the bridge (or pool) of the provider
    named by `share:`; this one then works in the same Chrome windows, account by account, as extra tabs."""
    hosts = getattr(host, "bridges", None) or ({"main": host} if host else {})
    if (options or {}).get("accounts"):
        return BridgePool(base_url, options, hosts=hosts)
    return Bridge(base_url, options, host=next(iter(hosts.values()), None))


PARK = {"left": 20000, "top": 20000}  # past the bottom right corner; macOS keeps a 40x41 px corner on the display


async def park_windows(cdp, parked: dict) -> dict:
    """Put every window of the browser behind `cdp` (a browser-level CDP session) back off screen when it is not
    where it was last parked: moved by macOS, or new. Only the browser's own Browser/Target domains are used:
    nothing is attached to or run in any page. A minimized window is left alone (nothing shows), a maximized or
    full-screen one is made normal first. Returns {windowId: bounds after parking} for the windows that exist."""
    infos = (await cdp.send("Target.getTargets")).get("targetInfos") or []
    out: dict[int, tuple] = {}
    for t in infos:
        if t.get("type") != "page":
            continue
        try:
            w = await cdp.send("Browser.getWindowForTarget", {"targetId": t["targetId"]})
        except Exception:  # noqa: BLE001 - closed meanwhile
            continue
        wid, b = w["windowId"], w["bounds"]
        if wid in out:
            continue
        key = (b.get("left"), b.get("top"), b.get("width"), b.get("height"), b.get("windowState"))
        if b.get("windowState") == "minimized" or parked.get(wid) == key:
            out[wid] = key
            continue
        if b.get("windowState") in ("maximized", "fullscreen"):
            await cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        await cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": dict(PARK)})
        nb = (await cdp.send("Browser.getWindowBounds", {"windowId": wid}))["bounds"]
        out[wid] = (nb.get("left"), nb.get("top"), nb.get("width"), nb.get("height"), nb.get("windowState"))
    return out


def watchdog(pid: int):
    """A tiny shell that ends the bundled browser `pid` when this process goes away, however it goes (even
    SIGKILL): it waits on a pipe only this process holds. Without it a background-only browser, which has no Dock
    icon and no window on screen, could be left running unseen. Its own session, so Ctrl+C here leaves it to act."""
    try:
        return subprocess.Popen(
            ["/bin/sh", "-c", f"read _ignored; kill -TERM {int(pid)} 2>/dev/null"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None


def stop_watchdog(watch):
    """After the browser has exited: end the watchdog without letting it signal (the pid could be reused)."""
    if not watch:
        return
    try:
        watch.kill()
        watch.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        watch.stdin.close()
    except OSError:
        pass


def hide_chrome(profile: Path):
    """Hide (as Cmd+H does) the Chrome that runs on this profile; the user's own Chrome is another process and is
    left alone. Best effort: needs macOS and permission to use System Events."""
    try:
        pids = subprocess.run(
            ["pgrep", "-f", f"user-data-dir={profile}"], capture_output=True, text=True
        ).stdout.split()
        for pid in pids[:40]:
            subprocess.Popen(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to set visible of '
                    f"(every process whose unix id is {int(pid)}) to false",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except (OSError, ValueError):
        pass


def notify(text: str):
    try:
        subprocess.Popen(
            ["osascript", "-e", f'display notification {json.dumps(text)} with title "provenance-core"'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass
