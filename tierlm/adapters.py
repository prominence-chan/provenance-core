"""Shared plumbing for the non-OpenAI-chat front ends (Anthropic Messages, OpenAI Responses): resolving a client's
model name to a tier or a concrete model, sticky-session keys, auth, and reading the upstream OpenAI chat stream.

Every adapter translates its request into an OpenAI chat body and goes through the same Router path as
/v1/chat/completions (single request, no ensemble), then translates the answer back."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass

from .router import NoCapacity, Router, estimate_tokens


@dataclass
class Target:
    tier: str | None        # serve from this tier (with fallback) ...
    concrete: str | None    # ... or from exactly this catalog model
    fallback: str = "none"

    @property
    def label(self) -> str:
        return self.tier or self.concrete or "?"


def resolve_target(router: Router, name: str, fallback: str) -> Target | None:
    """A tier name / alias (via the catalog) or a concrete catalog id; None if neither. `name` may carry a
    ":fallback" suffix (e.g. "blaze:none") to override the adapter's default fallback."""
    if ":" in name and name.rsplit(":", 1)[1] in ("none", "down1", "any"):
        name, fallback = name.rsplit(":", 1)
    tier = router.catalog.resolve_tier(name)
    if tier:
        return Target(tier, None, fallback)
    if name in router.catalog.models:
        return Target(None, name, "none")
    return None


def authorized(request, token: str | None) -> bool:
    """TIERLM_TOKEN as a bearer token, or as x-api-key (what Anthropic SDKs send for ANTHROPIC_API_KEY)."""
    if not token:
        return True
    return (request.headers.get("authorization") == f"Bearer {token}"
            or request.headers.get("x-api-key") == token)


def session_key(request, target: Target, messages: list[dict]) -> str:
    """X-Tierlm-Session if the client sends it, else a hash of the conversation's first user message, so an agent
    conversation stays on one model. Scoped per tier: a client mixing tiers in one conversation (Claude Code's
    haiku side calls) keeps one sticky model per tier instead of overwriting the session each call."""
    base = request.headers.get("x-tierlm-session")
    if not base:
        first = next((m.get("content") for m in messages if m.get("role") == "user"), None)
        base = "h:" + hashlib.sha256(json.dumps(first, sort_keys=True, default=str).encode()).hexdigest()[:24]
    return f"{base}|{target.label}"


def clamp_max_tokens(n) -> int:
    """Agent CLIs ask for 32k-64k output tokens; most free tiers cap output lower and count max_tokens against
    their tokens-per-minute budget, so a huge max_tokens would exclude every model. TIERLM_ADAPTER_MAX_TOKENS caps it."""
    cap = int(os.environ.get("TIERLM_ADAPTER_MAX_TOKENS", "16384"))
    try:
        n = int(n)
    except (TypeError, ValueError):
        return cap
    return max(1, min(n, cap))


def clean_schema(s):
    """Drop JSON-schema keys some OpenAI-compatible providers reject ($schema)."""
    if isinstance(s, dict):
        return {k: clean_schema(v) for k, v in s.items() if k != "$schema"}
    if isinstance(s, list):
        return [clean_schema(v) for v in s]
    return s


def safe_id(i: str | None, prefix: str = "call_") -> str:
    if not i:
        return prefix + secrets.token_hex(12)
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(i))


def parse_args(s) -> dict:
    if isinstance(s, dict):
        return s
    if not s or not str(s).strip():
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {"value": v}
    except json.JSONDecodeError:
        return {"raw_arguments": s}


def text_of(content) -> str:
    """OpenAI message content (str or a parts list) as plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content if isinstance(p, dict))


async def complete(router: Router, body: dict, target: Target, session: str):
    if target.concrete:
        m = router.catalog.models[target.concrete]
        try:
            return await router.call(m, body), m
        except Exception as e:  # noqa: BLE001
            raise NoCapacity(str(e)) from e
    return await router.complete(body, target.tier, session, target.fallback)


async def open_stream(router: Router, body: dict, target: Target, session: str):
    if target.concrete:
        m = router.catalog.models[target.concrete]
        try:
            resp, t0 = await router.call(m, body, stream=True)
        except Exception as e:  # noqa: BLE001
            raise NoCapacity(str(e)) from e
        router.state.record(m, estimate_tokens(body))
        return resp, m, t0
    return await router.stream(body, target.tier, session, target.fallback)


class UpstreamStream:
    """Reads an OpenAI chat-completions SSE stream. Yields ("text", str) and ("reasoning", str) as they arrive and
    accumulates tool calls by index (providers split a call's id/name/arguments over many chunks, and some send
    every call whole in one chunk without an index). After the stream: .tool_calls, .finish_reason, .usage."""

    def __init__(self, resp):
        self.resp = resp
        self.calls: dict[int, dict] = {}
        self.finish_reason: str | None = None
        self.usage: dict | None = None
        self.text_chars = 0
        self.model: str | None = None

    async def events(self):
        async for line in self.resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                err = chunk["error"]
                raise RuntimeError(err.get("message") if isinstance(err, dict) else str(err))
            if chunk.get("usage"):
                self.usage = chunk["usage"]
            for ch in chunk.get("choices") or []:
                d = ch.get("delta") or ch.get("message") or {}
                r = d.get("reasoning_content") or d.get("reasoning")
                if isinstance(r, str) and r:
                    yield "reasoning", r
                c = d.get("content")
                if isinstance(c, list):
                    c = text_of(c)
                if c:
                    self.text_chars += len(c)
                    yield "text", c
                for tc in d.get("tool_calls") or []:
                    idx = tc.get("index")
                    if idx is None:   # no index: same id continues a call, anything else starts a new one
                        idx = next((k for k, x in self.calls.items() if tc.get("id") and x["id"] == tc["id"]),
                                   max(self.calls, default=-1) + 1)
                    cur = self.calls.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                    if tc.get("id"):
                        cur["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name") and not cur["name"]:
                        cur["name"] = fn["name"]
                    if fn.get("arguments"):
                        a = fn["arguments"]
                        cur["arguments"] += a if isinstance(a, str) else json.dumps(a)
                if ch.get("finish_reason"):
                    self.finish_reason = ch["finish_reason"]

    @property
    def tool_calls(self) -> list[dict]:
        for c in self.calls.values():
            c["id"] = safe_id(c["id"])   # stable: assigned once (generated if the provider sent none)
        return [c for _, c in sorted(self.calls.items()) if c["name"]]

    def output_tokens(self) -> int:
        if self.usage and self.usage.get("completion_tokens") is not None:
            return int(self.usage["completion_tokens"])
        return (self.text_chars + sum(len(c["arguments"]) for c in self.calls.values())) // 4


def now() -> int:
    return int(time.time())
