"""Usage ledger: which concrete model served each request, so a client can show what answered a turn.

One entry per served Responses request: (ts, key, turn, tier, model, input/output tokens). `key` is the client's
conversation: Codex/Provenance send client_metadata.thread_id (and prompt_cache_key, the session id), else the
adapter's session key. `turn` is client_metadata.turn_id when the client sends it (Codex does), which pins a request
to its turn exactly; without it a client asks by time window.

In memory (bounded, PCORE_USAGE_LEDGER_MAX entries, default 20000) plus an append-only jsonl at
PCORE_USAGE_LEDGER (default ~/.tierlm/usage-ledger.jsonl; empty = memory only) that is read back on start, so turns
from before a restart still resolve. The file is compacted to the newest entries when it grows past twice the cap.

GET /v1/pcore/usage?key=...&turn=...&since=...&until=... (since/until in unix seconds) returns per-model counts:
{"key", "turn", "requests", "input_tokens", "output_tokens", "models": [{"model", "tier", "requests",
"input_tokens", "output_tokens"}], "context": {"<flag>": n}} sorted by requests. With `turn`, entries carrying that
turn id match; entries without a turn id match only by the window (when one is given).

An entry may also carry "ctx": what fitting the history into its model took (context_signal.py: compacted, trimmed,
restarted, ...). "context" counts the matching requests per flag (empty when nothing had to be done). A request no
model could take because of its size is an entry with an empty model and ctx ["over"]: counted in "context", not in
"requests" or "models".

An entry may carry "pruned": {"before": chars, "after": chars} of the history as the client sent it and as it went
on, for a request whose history the router pruned (the Codex pass-through, codex_sub.py, records every request it
looked at, saved or not). "pruned" in the answer sums them: {"requests", "before", "after", "saved"}; absent when no
matching entry has one. "cached_tokens" sums the input tokens a provider reported as served from its prompt cache,
for entries that carry it ("cached").

An entry served by an API model carries "kid": the id of the provider's key that served it (pool_keys.py: "main",
"anon" or a further key's id; never the key itself). A model's row in the answer then has "keys": {"<key id>":
requests}."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from ..settings import env, home_dir

DEFAULT_PATH = Path.home() / ".tierlm" / "usage-ledger.jsonl"  # where it is when PCORE_HOME is not set


def request_keys(req: dict, session: str) -> tuple[str, str | None]:
    """(conversation key, turn id) for a Responses request body."""
    meta = req.get("client_metadata") if isinstance(req.get("client_metadata"), dict) else {}
    key = meta.get("thread_id") or req.get("prompt_cache_key") or meta.get("session_id") or session.split("|")[0]
    return str(key), (str(meta["turn_id"]) if meta.get("turn_id") else None)


class Ledger:
    def __init__(self, path: str | Path | None = None, cap: int | None = None):
        self.cap = cap or int(env("USAGE_LEDGER_MAX", "20000"))
        self.entries: deque[dict] = deque(maxlen=self.cap)
        # in the router's own home (PCORE_HOME), so a second router (the app's, a test's) keeps its own ledger
        p = env("USAGE_LEDGER", str(home_dir() / "usage-ledger.jsonl")) if path is None else str(path)
        self.path = Path(p).expanduser() if p else None
        self.lock = threading.Lock()
        self._lines = 0
        self._load()

    def _load(self):
        if not self.path or not self.path.exists():
            return
        try:
            with self.path.open() as f:
                for line in f:
                    self._lines += 1
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and e.get("key") and (e.get("model") or e.get("ctx")):
                        self.entries.append(e)
        except OSError:
            return
        if self._lines > 2 * self.cap:
            self._compact()

    def _compact(self):
        tmp = self.path.with_suffix(".tmp")
        try:
            with tmp.open("w") as f:
                for e in self.entries:
                    f.write(json.dumps(e) + "\n")
            os.replace(tmp, self.path)
            self._lines = len(self.entries)
        except OSError:
            pass

    def record(
        self,
        key: str,
        model: str,
        tier: str | None = None,
        turn: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        ts: float | None = None,
        ctx: list[str] | None = None,
        pruned: dict | None = None,
        cached_tokens: int | None = None,
        kid: str | None = None,
    ) -> dict:
        e = {
            "ts": round(ts if ts is not None else time.time(), 3),
            "key": key,
            "turn": turn,
            "tier": tier,
            "model": model,
        }
        if input_tokens is not None:
            e["in"] = int(input_tokens)
        if output_tokens is not None:
            e["out"] = int(output_tokens)
        if ctx:
            e["ctx"] = list(ctx)
        if cached_tokens is not None:
            e["cached"] = int(cached_tokens)
        if kid:  # which of the provider's keys served (its id from pool_keys.py; never the key)
            e["kid"] = str(kid)
        if pruned:
            e["pruned"] = {"before": int(pruned.get("before") or 0), "after": int(pruned.get("after") or 0)}
        with self.lock:
            self.entries.append(e)
            if self.path:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a") as f:
                        f.write(json.dumps(e) + "\n")
                    self._lines += 1
                    if self._lines > 2 * self.cap:
                        self._compact()
                except OSError:
                    pass
        return e

    def query(self, key: str, turn: str | None = None, since: float | None = None, until: float | None = None) -> dict:
        def in_window(e):
            return (since is None or e["ts"] >= since) and (until is None or e["ts"] <= until)

        with self.lock:
            rows = [e for e in self.entries if e["key"] == key]
        if turn:
            windowed = since is not None or until is not None
            rows = [e for e in rows if e.get("turn") == turn or (not e.get("turn") and windowed and in_window(e))]
        else:
            rows = [e for e in rows if in_window(e)]
        context: dict[str, int] = {}
        for e in rows:
            for f in e.get("ctx") or ():
                context[f] = context.get(f, 0) + 1
        rows = [e for e in rows if e.get("model")]
        cut = [e["pruned"] for e in rows if isinstance(e.get("pruned"), dict)]
        models: dict[str, dict] = {}
        for e in rows:
            m = models.setdefault(
                e["model"],
                {"model": e["model"], "tier": e.get("tier"), "requests": 0, "input_tokens": 0, "output_tokens": 0},
            )
            m["requests"] += 1
            if e.get("kid"):
                ks = m.setdefault("keys", {})
                ks[e["kid"]] = ks.get(e["kid"], 0) + 1
            m["input_tokens"] += e.get("in") or 0
            m["output_tokens"] += e.get("out") or 0
        out = sorted(models.values(), key=lambda m: (-m["requests"], -m["output_tokens"], m["model"]))
        res = {
            "key": key,
            "turn": turn,
            "requests": len(rows),
            "input_tokens": sum(m["input_tokens"] for m in out),
            "output_tokens": sum(m["output_tokens"] for m in out),
            "models": out,
            "context": context,
        }
        if any("cached" in e for e in rows):  # input tokens the provider's prompt cache served (when it says)
            res["cached_tokens"] = sum(e.get("cached") or 0 for e in rows)
        if cut:
            before, after = sum(p.get("before", 0) for p in cut), sum(p.get("after", 0) for p in cut)
            res["pruned"] = {"requests": len(cut), "before": before, "after": after, "saved": before - after}
        return res


def register(app, ledger: Ledger, authorized_fn):
    @app.get("/v1/pcore/usage")
    async def usage(
        request: Request, key: str, turn: str | None = None, since: float | None = None, until: float | None = None
    ):
        if not authorized_fn(request):
            return JSONResponse({"error": {"message": "bad token"}}, status_code=401)
        return ledger.query(key, turn, since, until)
