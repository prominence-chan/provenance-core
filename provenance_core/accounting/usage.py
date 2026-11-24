"""Subscription headroom for Claude Code and Codex, and the env that points them at provenance-core when they
run low.

Where the numbers come from (all local, nothing is fetched):
- Codex writes its rate limits into every session rollout (~/.codex/sessions/**/rollout-*.jsonl, "rate_limits":
  primary/secondary windows with used_percent and resets_at).
- Claude Code passes rate_limits (five_hour / seven_day used_percentage + resets_at) to the status-line command;
  `provenance_core statusline-tee <original command>` records them and passes stdin through. `claude -p --output-format
  stream-json` also emits rate_limit_event lines, which a caller can hand to record_claude_event. And when the CLI
  prints its usage-limit notice, record_claude_blocked marks it out until the reset.

A tool counts as low when any window is at or above the threshold (PCORE_LOW_PCT, default 95) and hasn't reset yet.
Fallback is only offered when provenance-core is actually up and has a usable model, so a dead proxy never makes things worse."""

from __future__ import annotations

import glob
import json
import os
import re
import time
import urllib.request
from pathlib import Path

from ..settings import env

HOME = Path(env("HOME") or Path.home() / ".tierlm")  # state dir; keeps the pre-rename location
CLAUDE_FILE = HOME / "claude_limits.json"
BASE = env("BASE_URL", "http://127.0.0.1:4141").rstrip("/").removesuffix("/v1")
LOW_PCT = float(env("LOW_PCT", 95))


def _save(d: dict):
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = CLAUDE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(CLAUDE_FILE)


def _load() -> dict:
    try:
        return json.loads(CLAUDE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _ts(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) / (1000 if v > 1e11 else 1)
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---- recording (Claude) ------------------------------------------------------------------------------------------
def record_claude(rate_limits: dict):
    """From the status-line JSON: {"five_hour": {"used_percentage", "resets_at"}, "seven_day": {...}}."""
    d = _load()
    windows = d.get("windows", {})
    for name, w in (rate_limits or {}).items():
        if isinstance(w, dict) and w.get("used_percentage") is not None:
            windows[name] = {"pct": float(w["used_percentage"]), "resets_at": _ts(w.get("resets_at"))}
    d.update(windows=windows, at=time.time())
    _save(d)


def record_claude_event(evt: dict):
    """A stream-json rate_limit_event: {"rate_limit_info": {"status", "resetsAt", "rateLimitType", "utilization"}}."""
    info = evt.get("rate_limit_info") or evt
    name = info.get("rateLimitType") or "five_hour"
    util = info.get("utilization")
    d = _load()
    windows = d.get("windows", {})
    if util is not None:
        windows[name] = {"pct": float(util) * (100 if float(util) <= 1 else 1), "resets_at": _ts(info.get("resetsAt"))}
    if info.get("status") == "rejected":
        d["blocked_until"] = _ts(info.get("resetsAt")) or time.time() + 3600
    d.update(windows=windows, at=time.time())
    _save(d)


def record_claude_blocked(message: str = ""):
    """The CLI said the limit is hit ("You've hit your weekly limit · resets 8pm (America/New_York)"). The reset time
    in the text is parsed when possible; otherwise it's treated as out for an hour and checked again."""
    until = time.time() + 3600
    m = re.search(r"resets\s+(?:at\s+)?(\w+\s+\d+,?\s+)?(\d{1,2})(?::(\d\d))?\s*(am|pm)", message or "", re.I)
    if m:
        import datetime as dt

        h = int(m.group(2)) % 12 + (12 if m.group(4).lower() == "pm" else 0)
        now = dt.datetime.now()
        t = now.replace(hour=h, minute=int(m.group(3) or 0), second=0, microsecond=0)
        if t <= now:
            t += dt.timedelta(days=1)
        until = t.timestamp()
    d = _load()
    d.update(blocked_until=until, at=time.time())
    _save(d)


# ---- reading -----------------------------------------------------------------------------------------------------
def claude_status() -> dict:
    d, now = _load(), time.time()
    worst, resets = 0.0, None
    for w in (d.get("windows") or {}).values():
        if w.get("resets_at") and w["resets_at"] < now:
            continue  # that window already reset
        if w["pct"] >= worst:
            worst, resets = w["pct"], w.get("resets_at")
    blocked = (d.get("blocked_until") or 0) > now
    return {
        "pct": worst,
        "resets_at": d.get("blocked_until") if blocked else resets,
        "blocked": blocked,
        "low": blocked or worst >= LOW_PCT,
        "seen": d.get("at"),
    }


def codex_status(root: str | None = None) -> dict:
    root = root or os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))
    files = sorted(glob.glob(f"{root}/sessions/*/*/*/rollout-*.jsonl"), key=os.path.getmtime, reverse=True)[:5]
    now = time.time()
    for f in files:
        try:
            with open(f, "rb") as fh:
                fh.seek(max(0, os.path.getsize(f) - 400_000))
                tail = fh.read().decode(errors="replace")
        except OSError:
            continue
        hits = re.findall(r'"rate_limits":(\{.*?"plan_type":[^}]*\})', tail)
        for raw in reversed(hits):
            try:
                rl = json.loads(raw)
            except ValueError:
                continue
            worst, resets = 0.0, None
            for k in ("primary", "secondary"):
                w = rl.get(k)
                if not w or (w.get("resets_at") and w["resets_at"] < now):
                    continue
                if w["used_percent"] >= worst:
                    worst, resets = float(w["used_percent"]), w.get("resets_at")
            credits = rl.get("credits") or {}
            return {
                "pct": worst,
                "resets_at": resets,
                "low": worst >= LOW_PCT,
                "credits": credits.get("balance") if credits.get("has_credits") else None,
                "seen": os.path.getmtime(f),
            }
    return {"pct": 0.0, "resets_at": None, "low": False, "seen": None}


def pcore_up(timeout: float = 2.0) -> bool:
    req = urllib.request.Request(f"{BASE}/v1/models")
    tok = env("TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r).get("data", [])
        return any("/" in m.get("id", "") for m in data)  # at least one concrete model has a key
    except Exception:  # noqa: BLE001
        return False


def status() -> dict:
    return {"claude": claude_status(), "codex": codex_status(), "pcore_up": pcore_up()}


tierlm_up = pcore_up  # pre-rename name


def fallback_env(tool: str, force: bool = False) -> dict | None:
    """Env vars that send `tool` ("claude" or "codex") to provenance-core, or None if it isn't low (or the
    proxy is down)."""
    st = claude_status() if tool == "claude" else codex_status()
    if not (force or st["low"]) or not pcore_up():
        return None
    tok = env("TOKEN") or "pcore"
    if tool == "claude":
        return {
            "ANTHROPIC_BASE_URL": BASE,
            "ANTHROPIC_AUTH_TOKEN": tok,
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    # Provenance (the codex fork) still reads the TIERLM_ names
    return {"PCORE_BASE_URL": f"{BASE}/v1", "PCORE_TOKEN": tok, "TIERLM_BASE_URL": f"{BASE}/v1", "TIERLM_TOKEN": tok}
