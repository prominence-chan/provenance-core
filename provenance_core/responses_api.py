"""OpenAI Responses API front end (POST /v1/responses, GET /v1/responses/{id}) for Codex-style clients.

The request is translated to OpenAI chat and served through the Router like /v1/chat/completions (one model, no
ensemble). model = a tier name / alias or a catalog id (optionally "tier:none|down1|any"); an unknown name (e.g. a
client default like "gpt-5-codex") goes to PCORE_MAP_DEFAULT, else the strongest tier. Default fallback:
PCORE_RESPONSES_FALLBACK ("down1").

Input: a string or items: message (input_text / output_text / input_image; developer/system roles fold into the
system prompt with `instructions`), function_call, function_call_output, local_shell_call(_output),
custom_tool_call(_output); reasoning, web_search_call, item_reference and compaction items are dropped.
Tools: function tools pass through; `local_shell` becomes a function tool named local_shell whose calls come back as
local_shell_call items (action.exec); `custom` (freeform, e.g. Codex's apply_patch) becomes a function tool with one
string argument `input` whose calls come back as custom_tool_call items; web_search / file_search / mcp / computer
use / image generation are dropped. parallel_tool_calls=false is enforced by keeping the first call.
store (default true) keeps the request history + output in a small in-memory LRU, so previous_response_id works
(instructions are not carried over, as in the real API)."""
from __future__ import annotations

import json
import secrets
import time
from collections import OrderedDict

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from .adapters import (Target, UpstreamStream, authorized, clamp_max_tokens, clean_schema, complete, now, open_stream,
                       parse_args, resolve_target, safe_id, session_key, text_of)
from .router import NoCapacity, Router
from .settings import env

STORE_MAX = 256
LOCAL_SHELL_SCHEMA = {"type": "object", "properties": {
    "command": {"type": "array", "items": {"type": "string"}, "description": "argv of the command to run"},
    "working_directory": {"type": "string"}, "timeout_ms": {"type": "integer"}}, "required": ["command"]}


def error(status: int, message: str, kind: str = "invalid_request_error", code: str | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind, "param": None, "code": code}}, status_code=status)


def map_model(router: Router, name: str) -> Target:
    fallback = env("RESPONSES_FALLBACK", "down1")
    t = resolve_target(router, name, fallback) if name else None
    if t:
        return t
    mapped = env("MAP_DEFAULT")
    t = resolve_target(router, mapped, fallback) if mapped else None
    return t or Target(router.catalog.tiers[0][0], None, fallback)


# ---- request: Responses -> OpenAI chat ----------------------------------------------------------------------------
def _parts(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    for p in content or []:
        t = p.get("type")
        if t in ("input_text", "output_text", "text", "summary_text"):
            out.append({"type": "text", "text": p.get("text", "")})
        elif t == "refusal":
            out.append({"type": "text", "text": p.get("refusal", "")})
        elif t == "input_image":
            url = p.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            if url:
                out.append({"type": "image_url", "image_url": {"url": url, **({"detail": p["detail"]} if p.get("detail") in ("low", "high") else {})}})
        elif t == "input_file":
            out.append({"type": "text", "text": f"[file omitted: {p.get('filename') or p.get('file_id') or 'file'}]"})
    return out


def _collapse(parts: list[dict]):
    if all(p["type"] == "text" for p in parts):
        return "\n".join(p["text"] for p in parts)
    return parts


def _output_text(out) -> str:
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        return "\n".join(p.get("text", "") for p in out if isinstance(p, dict))
    if isinstance(out, dict):
        return out.get("output") or out.get("text") or json.dumps(out)
    return "" if out is None else str(out)


def items_to_messages(items) -> tuple[list[str], list[dict]]:
    """Responses input items -> (system texts, chat messages)."""
    if isinstance(items, str):
        return [], [{"role": "user", "content": items}]
    system, msgs = [], []

    def add_call(call: dict):
        last = msgs[-1] if msgs else None
        if last is not None and last["role"] == "assistant":
            last.setdefault("tool_calls", []).append(call)
        else:
            msgs.append({"role": "assistant", "content": None, "tool_calls": [call]})

    for it in items or []:
        t = it.get("type") or ("message" if "role" in it else None)
        if t == "message":
            role = it.get("role", "user")
            parts = _parts(it.get("content"))
            if role in ("system", "developer"):
                system.append(text_of(parts))
            elif role == "assistant":
                txt = text_of(parts)
                last = msgs[-1] if msgs else None
                if last is not None and last["role"] == "assistant" and not last.get("tool_calls"):
                    last["content"] = (last["content"] or "") + txt
                else:
                    msgs.append({"role": "assistant", "content": txt})
            elif parts:
                msgs.append({"role": "user", "content": _collapse(parts)})
        elif t == "function_call":
            add_call({"id": safe_id(it.get("call_id") or it.get("id")), "type": "function",
                      "function": {"name": it.get("name", ""), "arguments": it.get("arguments") or "{}"}})
        elif t == "local_shell_call":
            a = it.get("action") or {}
            args = {k: a[k] for k in ("command", "working_directory", "timeout_ms") if a.get(k) is not None}
            add_call({"id": safe_id(it.get("call_id") or it.get("id")), "type": "function",
                      "function": {"name": "local_shell", "arguments": json.dumps(args)}})
        elif t == "custom_tool_call":
            add_call({"id": safe_id(it.get("call_id") or it.get("id")), "type": "function",
                      "function": {"name": it.get("name", ""), "arguments": json.dumps({"input": it.get("input", "")})}})
        elif t in ("function_call_output", "local_shell_call_output", "custom_tool_call_output"):
            msgs.append({"role": "tool", "tool_call_id": safe_id(it.get("call_id") or it.get("id")),
                         "content": _output_text(it.get("output")) or "(no output)"})
        # reasoning, web_search_call, item_reference, compaction, ...: nothing the upstream can use
    return system, msgs


def tools_to_openai(tools) -> tuple[list[dict], set[str], bool]:
    """-> (OpenAI tools, names of custom/freeform tools, whether local_shell is offered)"""
    out, custom, shell = [], set(), False
    flat = []
    for t in tools or []:   # namespace tools (e.g. a multi-agent tool group) carry their function tools nested
        flat += (t.get("tools") or []) if t.get("type") == "namespace" else [t]
    for t in flat:
        kind = t.get("type")
        if kind == "function":
            out.append({"type": "function", "function": {"name": t["name"], "description": t.get("description") or "",
                                                         "parameters": clean_schema(t.get("parameters") or {"type": "object", "properties": {}})}})
        elif kind == "local_shell":
            shell = True
            out.append({"type": "function", "function": {"name": "local_shell", "description":
                        "Run a command on the user's machine (argv list) and return its output.",
                        "parameters": LOCAL_SHELL_SCHEMA}})
        elif kind == "custom":
            custom.add(t["name"])
            fmt = t.get("format") or {}
            desc = t.get("description") or ""
            if fmt.get("type") == "grammar" and fmt.get("definition"):
                desc += f"\n\nThe input must match this {fmt.get('syntax', '')} grammar:\n{fmt['definition']}"
            out.append({"type": "function", "function": {"name": t["name"], "description": desc, "parameters": {
                "type": "object", "properties": {"input": {"type": "string", "description": "the raw tool input"}},
                "required": ["input"]}}})
        # web_search, file_search, mcp, computer_use_preview, image_generation, code_interpreter: dropped
    return out, custom, shell


def to_openai(req: dict, history: list[dict]) -> tuple[dict, list[dict], set[str], bool]:
    """-> (chat body, the conversation without system messages (for the store), custom tool names, local_shell)"""
    system, msgs = items_to_messages(req.get("input"))
    if req.get("instructions"):
        system.insert(0, req["instructions"])
    convo = history + msgs
    body: dict = {"messages": ([{"role": "system", "content": "\n\n".join(s for s in system if s)}] if any(system) else []) + convo,
                  "max_tokens": clamp_max_tokens(req.get("max_output_tokens"))}
    for k in ("temperature", "top_p"):
        if req.get(k) is not None:
            body[k] = req[k]
    tools, custom, shell = tools_to_openai(req.get("tools"))
    if tools:
        body["tools"] = tools
        tc = req.get("tool_choice")
        if tc in ("auto", "none", "required"):
            body["tool_choice"] = tc
        elif isinstance(tc, dict) and tc.get("type") == "function" and tc.get("name"):
            body["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
    fmt = ((req.get("text") or {}).get("format") or {})
    if fmt.get("type") == "json_schema":
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": fmt.get("name", "output"), "schema": fmt.get("schema", {}), "strict": bool(fmt.get("strict"))}}
    elif fmt.get("type") == "json_object":
        body["response_format"] = {"type": "json_object"}
    return body, convo, custom, shell


# ---- response: OpenAI chat -> Responses -----------------------------------------------------------------------------
def call_item(c: dict, custom: set[str], shell: bool) -> dict:
    """An upstream tool call (id, name, arguments str) as a Responses output item."""
    name, args = c["name"], c["arguments"] or "{}"
    if name in custom:
        a = parse_args(args)
        return {"type": "custom_tool_call", "id": "ctc_" + secrets.token_hex(12), "call_id": c["id"], "name": name,
                "input": a.get("input", a.get("raw_arguments", args)), "status": "completed"}
    if shell and name == "local_shell":
        a = parse_args(args)
        cmd = a.get("command") or []
        return {"type": "local_shell_call", "id": "lsh_" + secrets.token_hex(12), "call_id": c["id"],
                "status": "completed", "action": {"type": "exec", "command": cmd if isinstance(cmd, list) else [str(cmd)],
                                                  "working_directory": a.get("working_directory"),
                                                  "timeout_ms": a.get("timeout_ms"), "env": {}, "user": None}}
    return {"type": "function_call", "id": "fc_" + secrets.token_hex(12), "call_id": c["id"], "name": name,
            "arguments": json.dumps(parse_args(args)), "status": "completed"}


def message_item(text: str, item_id: str | None = None) -> dict:
    return {"type": "message", "id": item_id or "msg_" + secrets.token_hex(12), "status": "completed",
            "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}]}


def usage(inp: int, out: int) -> dict:
    return {"input_tokens": inp, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": out,
            "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": inp + out}


def response_obj(rid: str, req: dict, model: str, status: str, output: list, use: dict | None,
                 incomplete: str | None = None) -> dict:
    return {"id": rid, "object": "response", "created_at": now(), "status": status, "error": None,
            "incomplete_details": {"reason": incomplete} if incomplete else None,
            "instructions": req.get("instructions"), "max_output_tokens": req.get("max_output_tokens"),
            "model": model, "output": output, "parallel_tool_calls": req.get("parallel_tool_calls", True),
            "previous_response_id": req.get("previous_response_id"), "reasoning": {"effort": None, "summary": None},
            "store": req.get("store", True), "temperature": req.get("temperature"), "text": req.get("text") or
            {"format": {"type": "text"}}, "tool_choice": req.get("tool_choice", "auto"), "tools": req.get("tools") or [],
            "top_p": req.get("top_p"), "truncation": req.get("truncation", "disabled"), "usage": use,
            "metadata": req.get("metadata") or {}}


def items_from_chat(data: dict, custom: set[str], shell: bool, single: bool) -> tuple[list, str | None]:
    ch = (data.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    output = []
    text = text_of(msg.get("content"))
    if text:
        output.append(message_item(text))
    calls = [{"id": safe_id(tc.get("id")), "name": (tc.get("function") or {}).get("name", ""),
              "arguments": (tc.get("function") or {}).get("arguments") or "{}"} for tc in msg.get("tool_calls") or []]
    for c in calls[:1] if single else calls:
        output.append(call_item(c, custom, shell))
    return output, ch.get("finish_reason")


def estimate_input(body: dict) -> int:
    return max(1, len(json.dumps(body.get("messages", []), default=str)) // 4)


class Store:
    def __init__(self, cap: int = STORE_MAX):
        self.cap = cap
        self.data: OrderedDict[str, dict] = OrderedDict()

    def put(self, rid: str, convo: list[dict], resp: dict):
        _, out_msgs = items_to_messages(resp["output"])
        self.data[rid] = {"messages": convo + out_msgs, "response": resp}
        self.data.move_to_end(rid)
        while len(self.data) > self.cap:
            self.data.popitem(last=False)

    def get(self, rid: str) -> dict | None:
        return self.data.get(rid)


def register(app, router: Router, token: str | None):
    store = Store()
    app.state.responses_store = store

    @app.post("/v1/responses")
    async def responses(request: Request):
        if not authorized(request, token):
            return error(401, "bad token", "invalid_request_error", "invalid_api_key")
        try:
            req = await request.json()
        except Exception:  # noqa: BLE001
            return error(400, "body must be JSON")
        history = []
        if req.get("previous_response_id"):
            prev = store.get(req["previous_response_id"])
            if prev is None:
                return error(400, f"Previous response with id '{req['previous_response_id']}' not found.",
                             code="previous_response_not_found")
            history = prev["messages"]
        model_name = req.get("model") or ""
        target = map_model(router, model_name)
        body, convo, custom, shell = to_openai(req, history)
        session = session_key(request, target, convo)
        single = req.get("parallel_tool_calls") is False
        rid = "resp_" + secrets.token_hex(16)
        try:
            if req.get("stream"):
                resp, m, t0 = await open_stream(router, body, target, session)
                gen = _stream(router, store, resp, m, t0, rid, req, model_name, body, convo, custom, shell, single)
                return StreamingResponse(gen, media_type="text/event-stream",
                                         headers={"X-PCore-Model": m.id, "Cache-Control": "no-cache"})
            data, m = await complete(router, body, target, session)
        except NoCapacity as e:
            return error(503, str(e), "server_error", "no_capacity")
        output, finish = items_from_chat(data, custom, shell, single)
        u = data.get("usage") or {}
        incomplete = "max_output_tokens" if finish == "length" else None
        out = response_obj(rid, req, model_name, "incomplete" if incomplete else "completed", output,
                           usage(int(u.get("prompt_tokens") or estimate_input(body)), int(u.get("completion_tokens") or 0)),
                           incomplete)
        if req.get("store", True) is not False:
            store.put(rid, convo, out)
        return JSONResponse(out, headers={"X-PCore-Model": m.id, "X-PCore-Tier": router.catalog.tier_of(m)})

    @app.get("/v1/responses/{rid}")
    async def get_response(rid: str, request: Request):
        if not authorized(request, token):
            return error(401, "bad token", "invalid_request_error", "invalid_api_key")
        s = store.get(rid)
        if s is None:
            return error(404, f"No response found with id '{rid}'.", code="not_found")
        return s["response"]


async def _stream(router, store, resp, m, t0, rid, req, model, body, convo, custom, shell, single):
    seq = 0

    def ev(kind: str, **data) -> bytes:
        nonlocal seq
        seq += 1
        return f"event: {kind}\ndata: {json.dumps({'type': kind, 'sequence_number': seq, **data})}\n\n".encode()

    up = UpstreamStream(resp)
    base = response_obj(rid, req, model, "in_progress", [], None)
    yield ev("response.created", response=base)
    yield ev("response.in_progress", response=base)
    output: list[dict] = []
    msg_id, text = None, ""
    try:
        async for kind, val in up.events():
            if kind != "text":
                continue
            if msg_id is None:
                msg_id = "msg_" + secrets.token_hex(12)
                yield ev("response.output_item.added", output_index=len(output), item={
                    "type": "message", "id": msg_id, "status": "in_progress", "role": "assistant", "content": []})
                yield ev("response.content_part.added", item_id=msg_id, output_index=len(output), content_index=0,
                         part={"type": "output_text", "text": "", "annotations": [], "logprobs": []})
            text += val
            yield ev("response.output_text.delta", item_id=msg_id, output_index=len(output), content_index=0,
                     delta=val, logprobs=[])
        if msg_id is not None:
            item = message_item(text, msg_id)
            yield ev("response.output_text.done", item_id=msg_id, output_index=len(output), content_index=0,
                     text=text, logprobs=[])
            yield ev("response.content_part.done", item_id=msg_id, output_index=len(output), content_index=0,
                     part=item["content"][0])
            yield ev("response.output_item.done", output_index=len(output), item=item)
            output.append(item)
        calls = up.tool_calls[:1] if single else up.tool_calls
        for c in calls:
            item = call_item(c, custom, shell)
            i = len(output)
            if item["type"] == "function_call":
                yield ev("response.output_item.added", output_index=i, item={**item, "arguments": "", "status": "in_progress"})
                yield ev("response.function_call_arguments.delta", item_id=item["id"], output_index=i, delta=item["arguments"])
                yield ev("response.function_call_arguments.done", item_id=item["id"], output_index=i,
                         name=item["name"], arguments=item["arguments"])
            elif item["type"] == "custom_tool_call":
                yield ev("response.output_item.added", output_index=i, item={**item, "input": "", "status": "in_progress"})
                yield ev("response.custom_tool_call_input.delta", item_id=item["id"], output_index=i, delta=item["input"])
                yield ev("response.custom_tool_call_input.done", item_id=item["id"], output_index=i, input=item["input"])
            else:
                yield ev("response.output_item.added", output_index=i, item={**item, "status": "in_progress"})
            yield ev("response.output_item.done", output_index=i, item=item)
            output.append(item)
        inp = int((up.usage or {}).get("prompt_tokens") or estimate_input(body))
        incomplete = "max_output_tokens" if up.finish_reason == "length" else None
        final = response_obj(rid, req, model, "incomplete" if incomplete else "completed", output,
                             usage(inp, up.output_tokens()), incomplete)
        if req.get("store", True) is not False:
            store.put(rid, convo, final)
        router.state.success(m, (time.time() - t0) * 1000)
        # always response.completed (status says "incomplete" on truncation): Codex treats response.incomplete as a
        # hard error and would drop the partial turn
        yield ev("response.completed", response=final)
    except Exception as e:  # noqa: BLE001
        router.state.failure(m)
        failed = response_obj(rid, req, model, "failed", output, None)
        failed["error"] = {"code": "server_error", "message": f"{m.id}: {e}"[:500]}
        yield ev("response.failed", response=failed)
    finally:
        await resp.aclose()
