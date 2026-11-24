"""Codex on the user's own ChatGPT / Codex sign-in, through the router: a pass-through to OpenAI's Codex backend that
applies the router's history pruning to the model request on the way (prune_responses.py).

    /codex-sub/<key>/backend-api/<path>   ->   <upstream>/backend-api/<path>     (upstream: https://chatgpt.com)

The provenance binary keeps its own sign-in (auth.json, token refresh, account id); it is only pointed here with

    -c openai_base_url="http://127.0.0.1:<port>/codex-sub/<key>/backend-api/codex"

so its model requests (POST …/codex/responses) and the other calls it makes on that base (…/codex/models,
…/codex/responses/compact, …) come here instead of to chatgpt.com. Everything it sends on its ChatGPT base
(chatgpt_base_url: usage and rate limits, accounts, workspace routing) and its token refreshes still go to OpenAI
directly: none of them carry a conversation, and leaving chatgpt_base_url alone keeps the binary from re-routing its
requests to a workspace backend (which would skip this route).

Forwarding: method, path, query, body and the caller's own headers (Authorization, ChatGPT-Account-ID, session and
originator headers, Accept-Encoding, ...) go on as they came, minus hop-by-hop headers and X-PCore-Prune. The answer
comes back as it came: status, headers (Retry-After, Content-Encoding, ...), body streamed chunk by chunk, compressed
or not. A websocket upgrade gets 426, so the binary switches to HTTP for the session at once (its Responses websocket
sends only the new items of a turn, which leaves nothing to prune).

Treatment: only POST …/codex/responses with a JSON body (plain, gzip or deflate; zstd when the Python has it). Its
`input` is pruned in stable mode, so a thread's consecutive requests start with the same bytes between boundary moves
and OpenAI's prompt cache keeps hitting. Turned off per request by the header `X-PCore-Prune: off` or the body field
`"pcore": {"prune": false}` (removed before forwarding), for the whole route by `prune: {codex: false}` in the catalog
or PCORE_PRUNE=0. A body that can't be read or pruned is forwarded unchanged. Compaction is left to Codex: its own
remote compaction (…/responses/compact) is forwarded untouched, and the router's compact.py (chat messages, a model
call for the brief) has no safe counterpart for encrypted Responses items. No model calls are made here.

Each model request lands in the usage ledger (thread id + turn id from client_metadata, as for /v1/responses) with
the model's token counts from response.completed and "pruned": {before, after} in characters of `input`.

The key: the router's PCORE_TOKEN guards its other routes as a bearer token, but Authorization here belongs to OpenAI.
So the caller proves itself in the path instead, with a key derived from the token (path_key(): a hash, so a key seen
in a log line or an error message never gives the token away, and only opens this route). Without PCORE_TOKEN the
router is open and any key is taken. The key is masked in the access log. Nothing here logs, prints or stores a header
value, an account id or a token."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
import zlib

import httpx
from fastapi import Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..accounting import ledger as usage_ledger
from ..context import prune_responses
from ..settings import env

PREFIX = "/codex-sub"
DEFAULT_UPSTREAM = "https://chatgpt.com"
MODEL_PATH = "backend-api/codex/responses"
CONTROL_HEADER = "x-pcore-prune"
# hop-by-hop and connection-specific headers: each side of the proxy has its own
HOP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}
RESPONSE_HOP = HOP - {"content-length", "proxy-authenticate"}
MAX_TAP_LINE = 16 << 20  # an SSE line longer than this is not looked at for usage

log = logging.getLogger("provenance_core.codex_sub")


def path_key(token: str | None) -> str:
    """The path segment a caller shows: derived from the router's token ("open" when it has none)."""
    if not token:
        return "open"
    return hashlib.sha256(b"provenance-codex-sub\0" + token.encode()).hexdigest()[:32]


def upstream() -> str:
    return (env("CODEX_SUB_UPSTREAM") or DEFAULT_UPSTREAM).rstrip("/")


_KEY_IN_PATH = re.compile(r"(/codex-sub/)[^/\s?]+")


class _MaskKey(logging.Filter):
    """uvicorn's access log prints the path: the pass-through key in it becomes "***"."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_KEY_IN_PATH.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = _KEY_IN_PATH.sub(r"\1***", record.msg)
        return True


def _mask_access_log():
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _MaskKey) for f in access.filters):
        access.addFilter(_MaskKey())


# ---- the request body -------------------------------------------------------------------------------------------
def _decode(raw: bytes, encoding: str) -> bytes:
    enc = (encoding or "identity").strip().lower()
    if enc in ("", "identity"):
        return raw
    if enc in ("gzip", "x-gzip"):
        return zlib.decompress(raw, 16 + zlib.MAX_WBITS)
    if enc == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    if enc == "zstd":
        try:
            from compression import zstd  # Python 3.14+

            return zstd.decompress(raw)
        except ImportError:
            pass
        try:
            import zstandard  # type: ignore[import-not-found]

            return zstandard.ZstdDecompressor().decompress(raw, max_output_size=256 << 20)
        except ImportError as e:
            raise prune_responses.Unsupported("zstd body and no zstd module") from e
    raise prune_responses.Unsupported(f"content-encoding {enc}")


def _off(value: str | None) -> bool:
    return (value or "").strip().lower() in ("0", "off", "false", "no")


def treat(raw: bytes, headers: dict, options: dict | None) -> tuple[bytes | None, dict]:
    """(the body to send instead of `raw`, or None to send `raw` unchanged; what happened: {"model", "key", "turn",
    "pruned": report or None, "skipped": why or None})."""
    info: dict = {"model": None, "key": None, "turn": None, "pruned": None, "skipped": None}
    try:
        body = json.loads(_decode(raw, headers.get("content-encoding", "")))
    except (ValueError, zlib.error, prune_responses.Unsupported) as e:
        info["skipped"] = f"unreadable body ({type(e).__name__})"
        return None, info
    if not isinstance(body, dict):
        info["skipped"] = "not a JSON object"
        return None, info
    info["model"] = body.get("model") if isinstance(body.get("model"), str) else None
    info["key"], info["turn"] = usage_ledger.request_keys(body, "codex-sub")
    ours = body.pop("pcore", None)
    off = _off(headers.get(CONTROL_HEADER)) or (isinstance(ours, dict) and ours.get("prune") is False)
    out = body
    if off or not options:
        info["skipped"] = "off"
    else:
        try:
            out, info["pruned"] = prune_responses.prune_request(body, options)
        except Exception as e:  # noqa: BLE001 - whatever the adapter can't read goes on as it came
            info["skipped"] = f"not understood ({type(e).__name__})"
            out = body
    if out is body and ours is None:
        return None, info  # nothing changed: the caller's bytes go on exactly as they were
    return json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode(), info


# ---- usage from the response stream --------------------------------------------------------------------------------
class UsageTap:
    """Watches an SSE stream for response.completed and keeps its usage. Sees the bytes, never changes them."""

    def __init__(self):
        self.buf = b""
        self.usage: dict | None = None
        self.gave_up = False

    def feed(self, chunk: bytes):
        if self.gave_up or self.usage is not None:
            return
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if line.startswith(b"data:") and b'"response.completed"' in line[:200]:
                try:
                    ev = json.loads(line[5:].strip())
                    u = (ev.get("response") or {}).get("usage")
                    if isinstance(u, dict):
                        self.usage = u
                        self.buf = b""
                        return
                except ValueError:
                    pass
        if len(self.buf) > MAX_TAP_LINE:
            self.gave_up, self.buf = True, b""

    def tokens(self) -> tuple[int | None, int | None, int | None]:
        u = self.usage or {}
        details = u.get("input_tokens_details") if isinstance(u.get("input_tokens_details"), dict) else {}
        return u.get("input_tokens"), u.get("output_tokens"), details.get("cached_tokens")


# ---- the routes -----------------------------------------------------------------------------------------------------
def register(app, router, token: str | None, client: httpx.AsyncClient | None = None):
    key = path_key(token)
    _mask_access_log()
    ledger = getattr(app.state, "usage_ledger", None) or usage_ledger.Ledger()
    app.state.usage_ledger = ledger
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30, read=900, write=120, pool=30), follow_redirects=False
        )
        app.router.on_shutdown.append(client.aclose)
    app.state.codex_sub_client = client  # tests swap in a fake upstream here

    def allowed(k: str) -> bool:
        return not token or hmac.compare_digest(k.encode(), key.encode())

    def options() -> dict | None:
        return prune_responses.config(router.catalog.prune)

    def denied() -> JSONResponse:
        return JSONResponse({"error": {"message": "bad pass-through key", "type": "invalid_request_error"}}, 401)

    @app.get(PREFIX + "/{k}/_health")
    async def health(k: str):
        """For the app: the route is there, the key is right, where it forwards to. Nothing is forwarded."""
        if not allowed(k):
            return denied()
        return {"ok": True, "upstream": upstream(), "prune": options() is not None}

    @app.websocket(PREFIX + "/{k}/{path:path}")
    async def websocket(ws: WebSocket, k: str, path: str):
        # HTTP only (see above): 426 makes the binary fall back to HTTP for the session without retrying
        await ws.send_denial_response(Response(status_code=426 if allowed(k) else 401))

    @app.api_route(
        PREFIX + "/{k}/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    async def forward(k: str, path: str, request: Request):
        if not allowed(k):
            return denied()
        if not path.startswith("backend-api/"):
            return JSONResponse({"error": {"message": f"/{path} is not forwarded"}}, 404)
        if request.headers.get("upgrade", "").lower() == "websocket":  # a server without a websocket library
            return Response(status_code=426)
        method = request.method
        raw = await request.body()
        headers = [(n, v) for n, v in request.headers.raw if n.decode("latin-1").lower() not in HOP | {CONTROL_HEADER}]
        info = None
        if method == "POST" and path.rstrip("/") == MODEL_PATH:
            body, info = treat(raw, request.headers, options())
            if body is not None:
                raw = body
                headers = [(n, v) for n, v in headers if n.decode("latin-1").lower() != "content-encoding"]
            if info["pruned"] and info["pruned"]["saved"] > 0:
                p = info["pruned"]
                rules = ", ".join(f"{r} {v['count']}/{v['saved']}" for r, v in p["rules"].items())
                print(f"codex-sub: {info['model']} input {p['before']} -> {p['after']} chars ({rules})", flush=True)
            elif info["skipped"] and info["skipped"] != "off":
                print(f"codex-sub: {info['model']} forwarded as sent: {info['skipped']}", flush=True)
        url = f"{upstream()}/{path}"
        if request.url.query:
            url += "?" + request.url.query
        req = client.build_request(method, url, headers=headers, content=raw if raw or method != "GET" else None)
        sent = {n.decode("latin-1").lower() for n, _ in headers}
        for h in ("accept", "accept-encoding", "user-agent"):  # httpx's own defaults: only what the caller sent
            if h not in sent and h in req.headers:
                del req.headers[h]
        t0 = time.time()
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            print(f"codex-sub: upstream unreachable ({type(e).__name__})", flush=True)
            return JSONResponse(
                {"error": {"message": f"Provenance couldn't reach {upstream()} ({type(e).__name__})"}}, 502
            )
        tap = None
        if info is not None and resp.status_code < 400 and not resp.headers.get("content-encoding"):
            tap = UsageTap()

        async def body_stream():
            try:
                async for chunk in resp.aiter_raw():
                    if tap:
                        tap.feed(chunk)
                    yield chunk
            finally:
                await resp.aclose()
                if info is not None and resp.status_code < 400 and info["model"]:
                    inp, out, cached = tap.tokens() if tap else (None, None, None)
                    p = info["pruned"]
                    ledger.record(
                        info["key"],
                        info["model"],
                        "codex",
                        info["turn"],
                        inp,
                        out,
                        ts=t0,
                        pruned={"before": p["before"], "after": p["after"]} if p else None,
                        cached_tokens=cached,
                    )

        out = StreamingResponse(body_stream(), status_code=resp.status_code)
        out.raw_headers = [
            (n.lower(), v) for n, v in resp.headers.raw if n.decode("latin-1").lower() not in RESPONSE_HOP
        ]
        return out
