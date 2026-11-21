"""Anthropic Messages API front end (POST /v1/messages, /v1/messages/count_tokens), so Claude Code can run on tierlm:

  ANTHROPIC_BASE_URL=http://127.0.0.1:4141 ANTHROPIC_AUTH_TOKEN=<TIERLM_TOKEN or anything> claude

Requests are translated to OpenAI chat and served through the Router like /v1/chat/completions (one model, no
ensemble). Claude model ids map to tiers by family: opus -> TIERLM_MAP_OPUS (default the strongest tier, "nova"),
sonnet -> TIERLM_MAP_SONNET ("solstice"), haiku -> TIERLM_MAP_HAIKU ("spark"), anything else -> TIERLM_MAP_DEFAULT
(the sonnet mapping). A tier name, alias or catalog id passes through. Values may end in ":none|down1|any" to set the
fallback; the adapter default is TIERLM_ANTHROPIC_FALLBACK ("any": step down rather than fail a coding agent).

Supported: system (string or text blocks), text / image (base64 or url) / tool_use / tool_result / text documents,
tools, tool_choice, disable_parallel_tool_use (enforced by keeping the first call), max_tokens (capped, see
adapters.clamp_max_tokens), temperature, top_p, stop_sequences, streaming. Ignored: thinking (and thinking blocks in
history), cache_control, top_k, metadata, server tools (web_search etc.), PDF documents."""
from __future__ import annotations

import json
import os
import secrets
import time

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from .adapters import (Target, UpstreamStream, authorized, clamp_max_tokens, clean_schema, complete, open_stream,
                       parse_args, resolve_target, safe_id, session_key, text_of)
from .router import NoCapacity, Router

FAMILY_DEFAULTS = {"opus": ("nova", 0), "sonnet": ("solstice", 1), "haiku": ("spark", 2)}   # (tier, position)


def error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": kind, "message": message}}, status_code=status)


def map_model(router: Router, name: str) -> Target:
    fallback = os.environ.get("TIERLM_ANTHROPIC_FALLBACK", "any")
    name = (name or "").strip()
    base = name.split("[", 1)[0].strip()   # "claude-opus-4-6[1m]" -> "claude-opus-4-6"
    t = resolve_target(router, base, fallback) if base else None
    if t:
        return t
    low = base.lower()
    family = next((f for f in FAMILY_DEFAULTS if f in low), None)
    if family:
        env = os.environ.get(f"TIERLM_MAP_{family.upper()}")
    else:
        env = os.environ.get("TIERLM_MAP_DEFAULT") or os.environ.get("TIERLM_MAP_SONNET")
        family = "sonnet"
    t = resolve_target(router, env, fallback) if env else None
    if t:
        return t
    # default mapping; if the catalog doesn't have that tier name, use the tier at the family's position
    tier_name, pos = FAMILY_DEFAULTS[family]
    t = resolve_target(router, tier_name, fallback)
    if t:
        return t
    names = [n for n, _ in router.catalog.tiers]
    return Target(names[min(pos, len(names) - 1)], None, fallback)


# ---- request: Anthropic -> OpenAI chat ----------------------------------------------------------------------------
def _image(src: dict) -> dict:
    if src.get("type") == "base64":
        url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
    else:
        url = src.get("url", "")
    return {"type": "image_url", "image_url": {"url": url}}


def _blocks_text(content) -> tuple[str, list[dict]]:
    """tool_result content -> (text, image parts)"""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    texts, images = [], []
    for b in content:
        if b.get("type") == "text":
            texts.append(b.get("text", ""))
        elif b.get("type") == "image":
            images.append(_image(b.get("source") or {}))
    return "\n".join(texts), images


def _collapse(parts: list[dict]):
    """Plain string when there are no images (the most widely accepted form), else the parts list."""
    if all(p["type"] == "text" for p in parts):
        return "\n\n".join(p["text"] for p in parts)
    return parts


def to_openai(req: dict) -> dict:
    msgs: list[dict] = []
    system = req.get("system")
    if isinstance(system, list):
        system = "\n\n".join(b.get("text", "") for b in system if b.get("type") == "text")
    if system:
        msgs.append({"role": "system", "content": system})
    for m in req.get("messages", []):
        role, content = m.get("role"), m.get("content")
        if role != "assistant":
            # Claude Code 2.1.x sometimes sends {"role": "system"} entries inside messages (context it sent as a user
            # message on the first turn); several OpenAI-compatible providers reject a system message after the
            # first, so it goes up as user content, as it did on turn one
            role = "user"
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        content = content or []
        if role == "assistant":
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            calls = [{"id": safe_id(b.get("id")), "type": "function",
                      "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input") or {})}}
                     for b in content if b.get("type") == "tool_use"]
            out = {"role": "assistant", "content": "".join(texts) or None}
            if calls:
                out["tool_calls"] = calls
            if out["content"] is not None or calls:
                msgs.append(out)
            continue
        parts: list[dict] = []
        tool_images: list[dict] = []
        for b in content:
            t = b.get("type")
            if t == "tool_result":
                text, imgs = _blocks_text(b.get("content"))
                if b.get("is_error"):
                    text = "[tool error] " + text
                msgs.append({"role": "tool", "tool_call_id": safe_id(b.get("tool_use_id")),
                             "content": text or "(no output)"})
                tool_images += imgs
            elif t == "text":
                parts.append({"type": "text", "text": b.get("text", "")})
            elif t == "image":
                parts.append(_image(b.get("source") or {}))
            elif t == "document":
                src = b.get("source") or {}
                if src.get("type") == "text":
                    parts.append({"type": "text", "text": src.get("data", "")})
                elif src.get("type") == "content":
                    parts.append({"type": "text", "text": _blocks_text(src.get("content"))[0]})
                else:
                    parts.append({"type": "text", "text": f"[document omitted: {b.get('title') or src.get('media_type')}]"})
        if tool_images:   # OpenAI tool messages are text-only: images from tool results follow as a user message
            parts = [{"type": "text", "text": "(images returned by the tool calls above)"}] + tool_images + parts
        if parts:
            msgs.append({"role": "user", "content": _collapse(parts)})

    body: dict = {"messages": msgs, "max_tokens": clamp_max_tokens(req.get("max_tokens"))}
    for k in ("temperature", "top_p"):
        if req.get(k) is not None:
            body[k] = req[k]
    if req.get("stop_sequences"):
        body["stop"] = list(req["stop_sequences"])[:4]
    tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                               "parameters": clean_schema(t.get("input_schema") or {"type": "object"})}}
             for t in req.get("tools") or [] if "input_schema" in t or not t.get("type") or t.get("type") == "custom"]
    if tools:
        body["tools"] = tools
        tc = req.get("tool_choice") or {}
        kind = tc.get("type")
        if kind == "any":
            body["tool_choice"] = "required"
        elif kind == "tool":
            body["tool_choice"] = {"type": "function", "function": {"name": tc.get("name")}}
        elif kind == "none":
            body["tool_choice"] = "none"
        elif kind == "auto":
            body["tool_choice"] = "auto"
    return body


def single_call(req: dict) -> bool:
    return bool((req.get("tool_choice") or {}).get("disable_parallel_tool_use"))


# ---- response: OpenAI chat -> Anthropic -----------------------------------------------------------------------------
def stop_reason(finish: str | None, has_tools: bool) -> str:
    if has_tools or finish in ("tool_calls", "function_call"):
        return "tool_use"
    if finish == "length":
        return "max_tokens"
    return "end_turn"


def from_openai(data: dict, model: str, single: bool = False) -> dict:
    ch = (data.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    blocks = []
    text = text_of(msg.get("content"))
    if text:
        blocks.append({"type": "text", "text": text})
    calls = msg.get("tool_calls") or []
    if single:
        calls = calls[:1]
    for tc in calls:
        fn = tc.get("function") or {}
        blocks.append({"type": "tool_use", "id": safe_id(tc.get("id"), "toolu_"), "name": fn.get("name", ""),
                       "input": parse_args(fn.get("arguments"))})
    u = data.get("usage") or {}
    return {"id": "msg_" + secrets.token_hex(12), "type": "message", "role": "assistant", "model": model,
            "content": blocks, "stop_reason": stop_reason(ch.get("finish_reason"), bool(calls)),
            "stop_sequence": None,
            "usage": {"input_tokens": int(u.get("prompt_tokens") or 0), "output_tokens": int(u.get("completion_tokens") or 0),
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}


def count_tokens(req: dict) -> int:
    """Rough estimate (chars/4) of the prompt: system, messages and tool definitions."""
    parts = [req.get("system") or "", req.get("messages") or [], req.get("tools") or []]
    return max(1, len(json.dumps(parts, default=str)) // 4)


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def register(app, router: Router, token: str | None):
    @app.post("/v1/messages")
    async def messages(request: Request):
        if not authorized(request, token):
            return error(401, "authentication_error", "bad token")
        try:
            req = await request.json()
        except Exception:  # noqa: BLE001
            return error(400, "invalid_request_error", "body must be JSON")
        if not isinstance(req.get("messages"), list):
            return error(400, "invalid_request_error", "messages: field required")
        model_name = req.get("model") or ""
        target = map_model(router, model_name)
        body = to_openai(req)
        session = session_key(request, target, body["messages"])
        single = single_call(req)
        try:
            if req.get("stream"):
                resp, m, t0 = await open_stream(router, body, target, session)
                return StreamingResponse(_stream(router, resp, m, t0, model_name, count_tokens(req), single),
                                         media_type="text/event-stream",
                                         headers={"X-Tierlm-Model": m.id, "Cache-Control": "no-cache"})
            data, m = await complete(router, body, target, session)
        except NoCapacity as e:
            # 529 overloaded: Claude Code retries it with backoff
            return error(529, "overloaded_error", str(e))
        return JSONResponse(from_openai(data, model_name, single),
                            headers={"X-Tierlm-Model": m.id, "X-Tierlm-Tier": router.catalog.tier_of(m)})

    @app.post("/v1/messages/count_tokens")
    async def count(request: Request):
        if not authorized(request, token):
            return error(401, "authentication_error", "bad token")
        return {"input_tokens": count_tokens(await request.json())}


async def _stream(router: Router, resp, m, t0: float, model: str, input_tokens: int, single: bool):
    up = UpstreamStream(resp)
    msg_id = "msg_" + secrets.token_hex(12)
    yield sse("message_start", {"type": "message_start", "message": {
        "id": msg_id, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None,
        "stop_sequence": None, "usage": {"input_tokens": input_tokens, "output_tokens": 1,
                                         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}})
    yield sse("ping", {"type": "ping"})
    idx, text_open, last_ping = -1, False, time.time()
    try:
        async for kind, val in up.events():
            if kind == "text":
                if not text_open:
                    idx += 1
                    text_open = True
                    yield sse("content_block_start", {"type": "content_block_start", "index": idx,
                                                      "content_block": {"type": "text", "text": ""}})
                yield sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                                                  "delta": {"type": "text_delta", "text": val}})
            elif time.time() - last_ping > 5:   # reasoning is dropped; keep the client's idle timer alive
                last_ping = time.time()
                yield sse("ping", {"type": "ping"})
        if text_open:
            yield sse("content_block_stop", {"type": "content_block_stop", "index": idx})
        calls = up.tool_calls[:1] if single else up.tool_calls
        for c in calls:
            idx += 1
            yield sse("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {
                "type": "tool_use", "id": c["id"], "name": c["name"], "input": {}}})
            yield sse("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(parse_args(c["arguments"]))}})
            yield sse("content_block_stop", {"type": "content_block_stop", "index": idx})
        yield sse("message_delta", {"type": "message_delta",
                                    "delta": {"stop_reason": stop_reason(up.finish_reason, bool(calls)),
                                              "stop_sequence": None},
                                    "usage": {"input_tokens": int((up.usage or {}).get("prompt_tokens") or input_tokens),
                                              "output_tokens": up.output_tokens()}})
        yield sse("message_stop", {"type": "message_stop"})
        router.state.success(m, (time.time() - t0) * 1000)
    except Exception as e:  # noqa: BLE001 - upstream died mid-stream: tell the client in Anthropic's terms
        router.state.failure(m)
        yield sse("error", {"type": "error", "error": {"type": "api_error", "message": f"{m.id}: {e}"[:500]}})
    finally:
        await resp.aclose()
