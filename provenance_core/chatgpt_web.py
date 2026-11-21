"""ChatGPT web as a provider (`type: chatgpt_web` in the catalog): your own signed-in chatgpt.com chat, used as a
stateless completion engine. The harness (Codex / Provenance) owns context, tools and memory; every request is
rendered into one message, sent in a NEW temporary chat, and the reply is parsed back into text and tool calls.

How it stays honest and gentle (read docs/TECHNICAL.md before turning it on):
- a real, visible Chrome window on its own profile (~/.tierlm/chatgpt-web-profile) that you sign into by hand once
  (`provenance-core weblogin`); nothing is read from any other browser profile and no password is ever handled
- the page is driven like a user would: open, type, press send, read the answer. No forged sentinel /
  proof-of-work / Turnstile tokens, no captcha solving or clicking, no calls to the private backend API
- two drivers. `playwright` (default) is stock: the window says it is automated, and chatgpt.com answers it with a
  browser check that never clears. `patchright` (opt-in: `driver: patchright` or PCORE_CHATGPT_WEB_DRIVER=patchright;
  the approach CatGPT-Gateway uses) drives the same Chrome and profile without the automation markers, so the
  automatic check passes as it does for you. That is hiding the automation from the site: your call, your account
- a challenge, captcha, "unusual activity" notice, login wall, usage-limit banner or a missing temporary-chat mode
  marks the provider unavailable (with a reason) and the router fails over to its normal pool
- one request at a time, a human-scale pause between requests, a daily cap, off unless enabled

Automating the web app is against OpenAI's terms of use; the realistic downside is the ChatGPT account being
limited or banned. That is why this is opt-in and deliberately slow."""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import secrets
import subprocess
import time
from pathlib import Path

from .settings import env


# ---- errors ---------------------------------------------------------------------------------------------------------
class WebError(RuntimeError):
    """This request failed (counts as a model failure; the router tries the next model)."""


class WebSkip(WebError):
    """The bridge can't take this request (busy, prompt too long, images): no health penalty."""


class WebUnavailable(WebError):
    """The provider is out until `cooldown` seconds pass (or the login is redone): login, challenge, limit, ..."""

    def __init__(self, reason: str, cooldown: float, kind: str):
        super().__init__(reason)
        self.reason, self.cooldown, self.kind = reason, cooldown, kind


class Malformed(ValueError):
    """The reply contains a tool call that can't be used as written."""


# ---- the text protocol: request -> one prompt -----------------------------------------------------------------------
PREAMBLE = """You are the model behind an API endpoint that a coding agent (the "harness") calls. This one message \
holds the whole request: system instructions, tool definitions and the conversation so far. Write only the \
assistant's next turn.
- The harness owns context, memory and tools. Do not use your own built-in tools (web search, code execution, \
canvas, image generation, memory). The only tools are the ones listed under "Tools", called with the text protocol \
described there.
- Do not mention this framing or these rules in your answer."""

TOOLS_HEAD = """# Tools
To call a tool, write a fenced code block with the language tag tool_call that contains exactly one JSON object:

```tool_call
{"name": "<tool name>", "arguments": {<arguments matching the tool's JSON schema>}}
```

- The JSON must be valid: double-quoted keys and strings, newlines inside strings written as \\n, no comments, no \
trailing commas.
- %s
- You may write a short note before the block. After the last block, stop: never write or guess a tool's output. \
The harness runs the tool and sends the result in the next request.
- When no tool is needed, answer normally with no tool_call block."""

TOOL_TAGS = ("tool_call", "tool-call", "toolcall", "tool_calls", "tool")


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


def call_block(name: str, arguments) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            arguments = {"raw_arguments": arguments}
    return "```tool_call\n" + json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False) + "\n```"


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
    rules = {"single": body.get("parallel_tool_calls") is False or bool(body.get("_single_tool_call")), "must": False,
             "only": None}
    if tools and choice == "required":
        rules["must"] = True
    elif tools and isinstance(choice, dict) and (choice.get("function") or {}).get("name") in tools:
        rules["must"], rules["only"] = True, choice["function"]["name"]

    system = "\n\n".join(_text(m.get("content")) for m in body.get("messages") or []
                         if m.get("role") in ("system", "developer") and m.get("content"))
    parts = [PREAMBLE]
    if system.strip():
        parts.append("# System instructions\n" + system.strip())
    if tools:
        many = ("At most one tool_call block per turn." if rules["single"]
                else "One block per call; several blocks call several tools in one turn.")
        sec = [TOOLS_HEAD % many]
        if rules["only"]:
            sec.append(f"In this turn you must call the tool `{rules['only']}`.")
        elif rules["must"]:
            sec.append("In this turn you must call a tool.")
        for name, schema in tools.items():
            sec.append(f"## {name}\n{descs[name].strip()}\nArguments schema: "
                       + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
        parts.append("\n\n".join(sec))
    fmt = body.get("response_format") or {}
    if fmt.get("type") == "json_schema":
        parts.append("# Output format\nReply with one JSON value only (no prose, no code fence) matching this schema: "
                     + json.dumps((fmt.get("json_schema") or {}).get("schema") or {}, separators=(",", ":")))
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
                segs.append(call_block(fn.get("name", ""), fn.get("arguments")))
            convo.append("<<<assistant>>>\n" + "\n".join(s for s in segs if s))
        elif role == "tool":
            name = names.get(m.get("tool_call_id")) or m.get("name") or "tool"
            convo.append(f"<<<tool result: {name}>>>\n" + _text(m.get("content")))
        else:
            convo.append("<<<user>>>\n" + _text(m.get("content")))
    parts.append("# Conversation\nEach turn starts with a <<<role>>> line.\n\n" + "\n\n".join(convo))
    parts.append("Write the assistant's next turn now (the turn's content only, no <<<assistant>>> line).")
    return "\n\n".join(parts), tools, rules


# ---- the text protocol: reply -> text + tool calls ------------------------------------------------------------------
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([\w+#.-]*)[^\n]*\n(.*?)\n[ \t]*\1[ \t]*$", re.S | re.M)


_TURN = re.compile(r"^[ \t]*<<<(?:user|assistant|tool result[^>\n]*)>>>[ \t]*$", re.M)


def _loads(s: str):
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return json.loads(s, strict=False)   # raw newlines / tabs inside strings


def _as_call(obj, tools: dict) -> dict | None:
    """A JSON value as a tool call {name, arguments}, or None when it isn't shaped like one."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):   # {"type": "function", "function": {"name", "arguments"}}
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
    if isinstance(args, str):   # arguments given as a JSON string
        try:
            args = _loads(args)
        except json.JSONDecodeError:
            props = (tools[name].get("properties") or {})
            if len(props) != 1:
                raise Malformed(f"arguments of {name} must be a JSON object") from None
            args = {next(iter(props)): args}   # a single-string tool given its raw string
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
    if m:   # the model kept writing the transcript (the next turn, or a made-up tool result): its turn ends there
        md = md[:m.start()]
    calls, keep, pos = [], [], 0
    if tools:
        for m in _FENCE.finditer(md):
            lang, inner = m.group(2).lower(), m.group(3)
            tagged = lang in TOOL_TAGS
            if not tagged and lang not in ("", "json", "jsonc", "plaintext", "text"):
                continue
            try:
                obj = _loads(inner)
            except json.JSONDecodeError as e:
                if tagged:
                    raise Malformed(f"a tool_call block is not valid JSON ({e.msg} at line {e.lineno} column {e.colno})") from None
                continue
            found = [_as_call(o, tools) for o in (obj if isinstance(obj, list) else [obj])]
            if not tagged and not all(c and c["name"] in tools for c in found):
                continue   # an ordinary json block, not a call
            if not found or not all(found):
                raise Malformed('a tool_call block must be {"name": ..., "arguments": {...}}')
            calls += [_check(c, tools) for c in found]
            keep.append(md[pos:m.start()])
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
                raise Malformed("the tool call must be inside a ```tool_call fenced block containing valid JSON") from None
            calls = [_check(c, tools)]
            text = md[:m.start() if m else start].strip()
    if rules.get("only") and any(c["name"] != rules["only"] for c in calls):
        raise Malformed(f"this turn must call the tool {rules['only']}")
    if rules.get("must") and not calls:
        raise Malformed("this turn must contain a tool_call block" + (f" for {rules['only']}" if rules.get("only") else ""))
    if rules.get("single"):
        calls = calls[:1]
    return text, calls


def repair_message(err: Malformed) -> str:
    return (f"The harness could not use that reply: {err}.\nSend the whole turn again, corrected. A tool call is a "
            'fenced code block tagged tool_call holding one valid JSON object {"name": ..., "arguments": {...}} '
            "(newlines inside strings written as \\n).")


def chat_completion(model_id: str, text: str, calls: list[dict], prompt: str, extra: dict | None = None) -> dict:
    msg: dict = {"role": "assistant", "content": text or None}
    if calls:
        msg["tool_calls"] = [{"id": c["id"], "type": "function",
                              "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]
    out = len(text) // 4 + sum(len(c["arguments"]) for c in calls) // 4
    return {"id": "chatcmpl-web-" + secrets.token_hex(10), "object": "chat.completion", "created": int(time.time()),
            "model": model_id, "choices": [{"index": 0, "message": msg,
                                            "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": out,
                      "total_tokens": len(prompt) // 4 + out}, "pcore": {"web": extra or {}}}


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
            chunks.append({**base, "choices": [{"index": 0, "delta": {"content": msg["content"]}, "finish_reason": None}]})
        for i, tc in enumerate(msg.get("tool_calls") or []):
            chunks.append({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, **tc}]}, "finish_reason": None}]})
        chunks.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": ch["finish_reason"]}],
                       "usage": data["usage"]})
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
    .filter(vis).filter(el => !el.closest('[data-message-author-role]')).map(el => el.innerText || '').join('\n');
  const text = (fresh && document.body ? document.body.innerText + '\n' : '') + scoped + '\n' + document.title;
  const m = (re, s) => { const x = (s === undefined ? text : s).match(re); return x ? x[0] : null; };
  out.challenge = m(/just a moment|verify (?:that )?you are (?:a )?human|unusual activity|suspicious activity|confirm you(?:'|’)re (?:not a robot|human)|are you a robot|complete the captcha|checking your browser|you have been blocked|access denied/i);
  if (!out.challenge) for (const f of document.querySelectorAll('iframe')) {
    const src = (f.getAttribute('src') || '') + ' ' + (f.getAttribute('title') || '');
    const r = f.getBoundingClientRect();
    if (/challenges\.cloudflare\.com|arkoselabs|funcaptcha|hcaptcha|recaptcha|challenge/i.test(src) && vis(f) && r.width > 120 && r.height > 40) { out.challenge = 'captcha frame'; break; }
  }
  out.limit = m(/you(?:'|’)ve (?:reached|hit) (?:your|the|our)[^.\n]{0,80}limit|you(?:'|’)re out of[^.\n]{0,60}|too many requests|usage cap|limit reached/i);
  out.error = m(/something went wrong|error generating|network error|unable to load|message (?:is )?too long|(?:too long|exceeds)[^.\n]{0,40}(?:message|limit)|conversation not found|an error occurred/i, scoped);
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
  out.users = msgs.filter(e => e.getAttribute('data-message-author-role') === 'user').length
              + document.querySelectorAll('[data-content-search-unit-key$=":user"]').length;
  const as = msgs.filter(e => e.getAttribute('data-message-author-role') === 'assistant').concat([...document.querySelectorAll('[data-response-body]')]);
  out.assistants = as.length;
  const last = as[as.length - 1];
  out.lastLen = last ? (last.innerText || '').length : 0;
  out.slug = last ? last.getAttribute('data-message-model-slug') : null;
  out.actions = document.querySelectorAll('[data-testid="copy-turn-action-button"], [data-talvt-turn-state] button[aria-label="Copy"]').length;
  const sw = first(['[data-testid="model-switcher-dropdown-button"]', 'button[aria-label^="Model selector" i]', 'button[aria-haspopup="menu"][aria-label*="model" i]']);
  out.model = sw ? ((sw.innerText || '').trim() || sw.getAttribute('aria-label') || '').replace(/\s+/g, ' ') : null;
  return out;
}"""

# The assistant messages after the last user message, turned back into markdown. Code blocks come back exactly
# (their text content); the rest is rebuilt from the rendered elements.
READ_JS = r"""() => {
  // both generations of markup (see PROBE_JS): everything the assistant wrote after the last user message
  const role = e => e.getAttribute('data-message-author-role') || (e.hasAttribute('data-response-body') ? 'assistant' : 'user');
  const all = [...document.querySelectorAll('[data-message-author-role], [data-content-search-unit-key$=":user"], [data-response-body]')];
  let lastUser = -1;
  all.forEach((e, i) => { if (role(e) === 'user') lastUser = i; });
  const as = all.slice(lastUser + 1).filter(e => role(e) === 'assistant');
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
    const roots = [...a.querySelectorAll('.markdown')].filter(r => !r.parentElement.closest('.markdown'));
    for (const r of (roots.length ? roots : [a])) parts.push(...blocks(r));
  }
  const last = as[as.length - 1];
  return {md: parts.join('\n\n'), n: as.length, slug: last ? last.getAttribute('data-message-model-slug') : null};
}"""

COMPOSER = '#prompt-textarea, div[contenteditable][role="textbox"], textarea[name="prompt"], main textarea'
SEND = ('[data-testid="send-button"], button[aria-label="Send prompt"], button[aria-label="Send message"], '
        'button[aria-label="Send"]')
SWITCHER = ('[data-testid="model-switcher-dropdown-button"], button[aria-label^="Model selector" i], '
            'button[aria-haspopup="menu"][aria-label*="model" i]')

DEFAULTS = {
    "daily_cap": 200,          # requests per local day; then unavailable until midnight
    "min_gap_s": 4.0,          # pause between requests: a random time in [min_gap_s, max_gap_s]
    "max_gap_s": 9.0,
    "queue_wait_s": 45.0,      # a request waits this long for the one in flight, then goes to the normal pool
    "reply_timeout_s": 600.0,  # longest wait for one reply (thinking models take minutes)
    "idle_close_s": 900.0,     # close the window after this long without a request
    "max_prompt_chars": 120000,
    "strict_model": True,      # a model other than "auto" must be the one selected in the page's model picker
    "endpoints": ["responses"],   # tier requests from these front ends may use the bridge (responses, messages, chat)
    "cooldown_login_s": 1800, "cooldown_challenge_s": 6 * 3600, "cooldown_limit_s": 3600,
    "cooldown_closed_s": 600, "cooldown_tempchat_s": 6 * 3600, "cooldown_model_s": 3600,
    "challenge_wait_s": 20.0,  # an automatic browser check gets this long to pass by itself; nothing is clicked
    "driver": "playwright",    # or "patchright": drive Chrome without the automation markers (docs/TECHNICAL.md)
    "headless": False,         # tests only: the real thing is always a visible window
    "notify": True,            # macOS notification when the provider goes unavailable
    "profile": None,           # default: <PCORE_HOME>/chatgpt-web-profile
}
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LOGIN_HINT = "run `provenance-core weblogin` and sign in to ChatGPT in the window it opens"


def home_dir() -> Path:
    return Path(env("HOME") or Path.home() / ".tierlm")


def enabled(options: dict | None) -> bool:
    """Off unless turned on: `enabled: true` on the provider, or PCORE_CHATGPT_WEB=1 (=0 forces it off)."""
    v = env("CHATGPT_WEB")
    if v is not None and v != "":
        return v.strip().lower() in ("1", "true", "on", "yes")
    return bool((options or {}).get("enabled"))


class Bridge:
    """One Chrome window on the dedicated profile; one request at a time."""

    def __init__(self, base_url: str = "https://chatgpt.com", options: dict | None = None, home: Path | None = None,
                 sleep=asyncio.sleep, clock=time.time, rand=random.uniform):
        self.base_url = base_url.rstrip("/")
        self.o = {**DEFAULTS, **(options or {})}
        if env("CHATGPT_WEB_DRIVER"):   # PCORE_CHATGPT_WEB_DRIVER=patchright: a per-machine choice, kept out of the catalog
            self.o["driver"] = env("CHATGPT_WEB_DRIVER").strip()
        self.home = Path(home) if home else home_dir()
        self.profile = Path(self.o["profile"] or self.home / "chatgpt-web-profile").expanduser()
        self.state_path = self.home / "chatgpt_web.json"
        self.sleep, self.clock, self.rand = sleep, clock, rand
        self.lock = asyncio.Lock()
        self._pw = self._ctx = self._page = None
        self._closing = False
        self._gone = False        # the user closed the window themselves
        self._last_done = 0.0
        self._idle: asyncio.Task | None = None
        self._notified: str | None = None

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
        return {"signed_in": s.get("signed_in"), "checked_at": s.get("checked_at"), "today": s.get("count", 0),
                "daily_cap": self.o["daily_cap"], "blocked": left > 0, "blocked_for_s": max(0, int(left)),
                "reason": s.get("reason") if left > 0 else None, "kind": s.get("kind") if left > 0 else None,
                "model_seen": s.get("model_seen"), "window_open": self._page is not None,
                "profile": str(self.profile)}

    def reset(self):
        """Forget a block (after `weblogin`, or when you've dealt with a notice by hand)."""
        self._notified = None
        self._gone = False
        self.save(blocked_until=0, reason=None, kind=None)

    def _block(self, e: WebUnavailable):
        ch = {"blocked_until": self.clock() + e.cooldown, "reason": e.reason, "kind": e.kind}
        if e.kind == "login":
            ch.update(signed_in=False, checked_at=int(self.clock()))
        self.save(**ch)
        if self.o["notify"] and self._notified != e.kind and e.kind != "closed":
            self._notified = e.kind
            notify(f"ChatGPT web is out: {e.reason}" + (f" ({LOGIN_HINT})" if e.kind == "login" else ""))

    def _precheck(self):
        s = self.load()
        left = (s.get("blocked_until") or 0) - self.clock()
        if left > 0:
            raise WebUnavailable(s.get("reason") or "unavailable", left, s.get("kind") or "blocked")
        if s.get("count", 0) >= int(self.o["daily_cap"]):
            lt = time.localtime(self.clock())
            to_midnight = 86400 - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
            raise WebUnavailable(f"daily cap of {self.o['daily_cap']} requests reached", to_midnight, "cap")

    # ---- the browser ---------------------------------------------------------------------------------------------
    async def _ensure_page(self):
        if self._page is not None and not self._page.is_closed() and not self._gone:
            return self._page
        gone, self._gone = self._gone, False
        await self._shutdown()
        if gone:   # closing the window is a "not now": stay out of the way for a while instead of reopening it
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
        self._pw = await async_playwright().start()
        try:
            # playwright: stock defaults, the window says it is automated (navigator.webdriver is true).
            # patchright (the approach CatGPT-Gateway uses): the same real Chrome and profile, driven without the
            # automation markers, so the site's automatic browser check treats it like the hand-opened window.
            # Launched the way patchright documents: real Chrome, persistent profile, no viewport, no extra flags.
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(self.profile), channel="chrome", headless=bool(self.o["headless"]), no_viewport=True,
                args=["--window-size=1180,860"])
        except Exception as e:  # noqa: BLE001 - profile in use (weblogin still open), Chrome missing, ...
            await self._shutdown()
            raise WebUnavailable(f"could not open Chrome on the chatgpt-web profile: {str(e).splitlines()[0][:160]}",
                                 120, "browser") from None
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        try:   # a just-opened window is still loading its blank first tab; navigating over that gets interrupted
            await self._page.wait_for_load_state("load", timeout=5000)
        except Exception:  # noqa: BLE001
            pass

        def closed(*_):
            if not self._closing:
                self._gone = True
        self._page.on("close", closed)
        self._ctx.on("close", closed)
        return self._page

    async def _shutdown(self):
        self._closing = True
        ctx, pw, self._ctx, self._pw, self._page = self._ctx, self._pw, None, None, None
        try:
            if ctx:
                await ctx.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if pw:
                await pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._closing = False

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
        for _ in range(8):   # the page may navigate under the probe (a browser check passing, a redirect): look again
            try:
                return await page.evaluate(PROBE_JS, fresh)
            except Exception as e:  # noqa: BLE001
                if "context was destroyed" not in str(e) and "navigation" not in str(e):
                    raise
                await asyncio.sleep(0.5)
        return await page.evaluate(PROBE_JS, fresh)

    def _notices(self, p: dict):
        """Anything on the page that means "stop": never interact with it, just report."""
        if p.get("challenge"):
            raise WebUnavailable(f"the page shows a challenge or block notice ({p['challenge']!r}); open the window "
                                 "yourself to see it", float(self.o["cooldown_challenge_s"]), "challenge")
        if p.get("limit"):
            raise WebUnavailable(f"ChatGPT reports a usage limit ({p['limit']!r})",
                                 float(self.o["cooldown_limit_s"]), "limit")

    async def _open_chat(self, model: str) -> dict:
        """A fresh temporary chat, ready for typing. Raises WebUnavailable for anything a user would have to sort
        out by hand."""
        page = await self._ensure_page()
        url = f"{self.base_url}/?temporary-chat=true"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:  # noqa: BLE001
            if "interrupted" not in str(e):
                raise
            await asyncio.sleep(1.0)
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        deadline, ok, p, challenged = time.time() + 40, 0, {}, None
        while True:
            p = await self._probe(page, True)
            if p.get("challenge"):   # an automatic "just a moment" check may pass by itself; wait like a user would
                challenged = challenged or time.time()
                if time.time() - challenged > float(self.o["challenge_wait_s"]):
                    self._notices(p)
            elif p.get("login"):
                ok += 1
                if ok >= 3:
                    raise WebUnavailable("not signed in to ChatGPT in the chatgpt-web profile",
                                         float(self.o["cooldown_login_s"]), "login")
            elif p.get("composer"):
                ok += 1
                if ok >= (2 if p.get("account") else 4):
                    break
            else:
                ok = 0
            if time.time() > deadline:
                if p.get("challenge"):
                    self._notices(p)
                raise WebError(f"chatgpt.com did not load a usable page (title {p.get('title')!r})")
            await asyncio.sleep(0.6)
        self._notices(p)
        self.save(signed_in=True, checked_at=int(self.clock()))
        if p.get("temp") is not True:   # the url flag didn't take: try the page's own toggle once
            toggle = page.locator('button[aria-label*="temporary chat" i]').first
            try:
                await toggle.click(timeout=3000)
                await asyncio.sleep(1.0)
                p = await self._probe(page, True)
            except Exception:  # noqa: BLE001
                pass
        if p.get("temp") is not True or p.get("users") or p.get("assistants"):
            raise WebUnavailable("temporary chat is not available on the page, so nothing was sent (a normal chat "
                                 "would be kept in the account's history)", float(self.o["cooldown_tempchat_s"]), "tempchat")
        if model and model.lower() != "auto":
            p = await self._pick_model(page, model, p)
        return p

    async def _pick_model(self, page, model: str, p: dict) -> dict:
        want = model.lower()
        if want in (p.get("model") or "").lower():
            return p
        try:
            await page.locator(SWITCHER).first.click(timeout=4000)
            item = page.locator('[role="menuitem"], [role="menuitemradio"], [role="option"]').filter(
                has_text=re.compile(re.escape(model), re.I)).first
            await item.click(timeout=4000)
            await asyncio.sleep(0.8)
        except Exception:  # noqa: BLE001
            try:
                await page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
        p = await self._probe(page, True)
        if want not in (p.get("model") or "").lower() and self.o["strict_model"]:
            raise WebUnavailable(f"model {model!r} is not selectable in the page's model picker (it shows "
                                 f"{p.get('model')!r})", float(self.o["cooldown_model_s"]), "model")
        if p.get("temp") is not True:
            raise WebUnavailable("temporary chat was lost while picking the model, so nothing was sent",
                                 float(self.o["cooldown_tempchat_s"]), "tempchat")
        return p

    async def _send(self, page, text: str) -> dict:
        """Type one message, send it, wait for the reply to finish. -> {md, slug}"""
        before = await self._probe(page, False)
        box = page.locator(COMPOSER).first
        await box.click(timeout=10000)
        await box.fill(text, timeout=30000)
        typed = await box.evaluate("el => (el.value !== undefined ? el.value : el.innerText || '').replace(/\\s+/g, '').length")
        if typed < 0.9 * len(re.sub(r"\s+", "", text)):
            raise WebError(f"the composer took {typed} of {len(text)} characters (message too long for the page?)")
        self.save(count=self.load().get("count", 0) + 1)
        try:
            await page.locator(SEND).first.click(timeout=15000)
        except Exception:  # noqa: BLE001 - no recognisable send button: Enter sends too
            await box.press("Enter")
        t0 = time.time()
        timeout = float(self.o["reply_timeout_s"])
        started, stable, last_len = False, 0, -1
        while True:
            await asyncio.sleep(0.5)
            p = await self._probe(page, False)
            self._notices(p)
            if p.get("login"):
                raise WebUnavailable("signed out of ChatGPT mid-request", float(self.o["cooldown_login_s"]), "login")
            if not started:
                started = p["users"] > before["users"] or p["assistants"] > before["assistants"] or p["stop"]
                if not started and time.time() - t0 > 30:
                    raise WebError("the message was not sent" + (f": {p['error']}" if p.get("error") else ""))
                if not started:
                    continue
            if p.get("error") and not p["stop"] and p["assistants"] <= before["assistants"]:
                raise WebError(f"ChatGPT showed an error: {p['error']}")
            done = False
            if not p["stop"] and p["assistants"] > before["assistants"] and p["lastLen"] > 0:
                stable = stable + 1 if p["lastLen"] == last_len else 0
                done = stable >= (2 if p["actions"] > before["actions"] else 8)
            else:
                stable = 0
            last_len = p["lastLen"]
            if done:
                break
            if time.time() - t0 > timeout:
                raise WebError(f"no complete reply after {int(timeout)} s")
        r = await page.evaluate(READ_JS)
        if not (r.get("md") or "").strip():
            raise WebError("the reply was empty")
        if r.get("slug"):
            self.save(model_seen=r["slug"])
        return r

    async def _gap(self):
        wait = self._last_done + self.rand(float(self.o["min_gap_s"]), float(self.o["max_gap_s"])) - self.clock()
        if self._last_done and wait > 0:
            await self.sleep(wait)

    async def _guarded(self, fn):
        """Run a page interaction; map "the window went away" and block-worthy notices to WebUnavailable."""
        try:
            return await fn()
        except WebUnavailable as e:
            if e.kind in ("login", "challenge", "limit", "tempchat", "model", "closed"):
                self._block(e)
            raise
        except WebError:
            raise
        except Exception as e:  # noqa: BLE001
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
        except asyncio.TimeoutError:
            raise WebSkip("another request is using the ChatGPT window") from None

    # ---- public --------------------------------------------------------------------------------------------------
    async def complete(self, model: str, body: dict, model_id: str | None = None, wait: float | None = None) -> dict:
        """One chat-completions request -> one temporary chat -> an OpenAI chat completion dict."""
        prompt, tools, rules = render_prompt(body)
        if len(prompt) > int(self.o["max_prompt_chars"]):
            raise WebSkip(f"prompt is {len(prompt)} characters, over the bridge's {self.o['max_prompt_chars']}")
        self._precheck()
        await self._acquire(wait)
        try:
            self._precheck()
            await self._gap()

            async def run():
                await self._open_chat(model)
                r = await self._send(self._page, prompt)
                repaired = False
                try:
                    text, calls = parse_reply(r["md"], tools, rules)
                except Malformed as e:   # one repair round, in the same temporary chat
                    repaired = True
                    await self.sleep(self.rand(1.0, 2.5))
                    r = await self._send(self._page, repair_message(e))
                    try:
                        text, calls = parse_reply(r["md"], tools, rules)
                    except Malformed as e2:
                        raise WebError(f"unusable tool call after one repair: {e2}") from None
                return text, calls, {"model_slug": r.get("slug"), "repaired": repaired}
            text, calls, extra = await self._guarded(run)
            fmt = (body.get("response_format") or {}).get("type")
            if fmt in ("json_schema", "json_object") and not calls:
                text = re.sub(r"^```\w*\n|\n```$", "", text.strip())
            return chat_completion(model_id or f"chatgpt-web/{model}", text, calls, prompt, extra)
        finally:
            self._last_done = self.clock()
            self._arm_idle()
            self.lock.release()

    async def check(self, model: str = "auto", close: bool = False) -> dict:
        """Open a fresh temporary chat without sending anything: is the login there, is temporary chat there?"""
        await self._acquire(None)
        try:
            try:
                p = await self._guarded(lambda: self._open_chat(model))
                self.reset()
                return {**self.status(), "ok": True, "page_model": p.get("model")}
            except WebUnavailable as e:
                return {**self.status(), "ok": False, "reason": e.reason, "kind": e.kind}
            except WebError as e:
                return {**self.status(), "ok": False, "reason": str(e), "kind": "error"}
        finally:
            if close:
                await self._shutdown()
            else:
                self._arm_idle()
            self.lock.release()


def notify(text: str):
    try:
        subprocess.Popen(["osascript", "-e", f'display notification {json.dumps(text)} with title "provenance-core"'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ---- the one-time sign-in ---------------------------------------------------------------------------------------------
def login_window(bridge: Bridge, wait_min: float = 45) -> int:
    """`provenance-core weblogin`: an ordinary Chrome window on the dedicated profile, not driven by anything. You
    sign in by hand and quit that Chrome (Cmd+Q); then the profile is checked once."""
    if not os.path.exists(CHROME):
        print(f"Google Chrome not found at {CHROME}")
        return 2
    bridge.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    # the same key-storage switches Playwright's Chrome uses, so both read the same cookie jar
    cmd = [CHROME, f"--user-data-dir={bridge.profile}", "--use-mock-keychain", "--password-store=basic",
           "--no-first-run", "--no-default-browser-check", "--window-size=1180,860", bridge.base_url + "/"]
    print("Opening Chrome on the chatgpt-web profile.\n"
          "  1. sign in to ChatGPT in that window (your password goes to OpenAI only)\n"
          "  2. quit that Chrome when you're in (Cmd+Q on the window that just opened)", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=wait_min * 60)
    except subprocess.TimeoutExpired:
        print(f"still open after {wait_min:g} min; checking anyway once you quit it")
        proc.wait()
    print("checking the sign-in...", flush=True)
    r = asyncio.run(bridge.check(close=True))
    if r["ok"]:
        print("signed in, temporary chat works. chatgpt-web is ready"
              + ("" if enabled(bridge.o) else " (still off: set PCORE_CHATGPT_WEB=1 to use it)"))
        return 0
    print(f"not ready: {r.get('reason')}")
    return 1
