"""tierlm CLI.

  python -m tierlm serve [--catalog catalog.yaml] [--scores scores.yaml] [--host 127.0.0.1] [--port 4141]
  python -m tierlm calibrate [--only id1,id2]      run the probe set, write scores.yaml
  python -m tierlm tiers                           show tiers, models, scores and which providers have keys
  python -m tierlm discover <provider>             list the provider's /models (to update the catalog)
  python -m tierlm usage                           Claude Code / Codex subscription headroom (JSON)
  python -m tierlm env claude|codex [--force]      shell exports that point the tool at tierlm, if it's running low
  python -m tierlm statusline-tee '<cmd>'          Claude Code status-line wrapper: records rate limits, then runs cmd"""
import argparse
import asyncio
import os

import httpx

from .catalog import load
from .router import Router


def main():
    ap = argparse.ArgumentParser(prog="tierlm")
    ap.add_argument("cmd", choices=["serve", "calibrate", "tiers", "discover", "usage", "env", "statusline-tee"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--catalog", default=os.environ.get("TIERLM_CATALOG", "catalog.yaml"))
    ap.add_argument("--scores", default=os.environ.get("TIERLM_SCORES", "scores.yaml"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("TIERLM_PORT", 4141)))
    ap.add_argument("--only", default="")
    a, extra = ap.parse_known_args()
    if a.cmd in ("usage", "env", "statusline-tee"):
        return _usage_cmd(a, extra)
    cat = load(a.catalog, a.scores)
    if a.cmd == "serve":
        import uvicorn
        from .server import create_app
        uvicorn.run(create_app(router=Router(cat)), host=a.host, port=a.port)
    elif a.cmd == "calibrate":
        from .calibrate import calibrate
        asyncio.run(calibrate(Router(cat), a.scores, [x for x in a.only.split(",") if x] or None))
    elif a.cmd == "tiers":
        for t, floor in cat.tiers:
            print(f"{t} (score >= {floor:g})")
            for m in cat.models_in(t, usable_only=False):
                key = "key" if cat.providers[m.provider].api_key else "no key"
                print(f"  {m.score:5.1f}{'*' if m.calibrated else ' '} {m.id:45s} {key}")
        print("* = calibrated")
    elif a.cmd == "discover":
        p = cat.providers[a.arg]
        r = httpx.get(f"{p.base_url}/models", headers={"Authorization": f"Bearer {p.api_key}", **p.headers}, timeout=30)
        for m in r.json().get("data", []):
            print(m.get("id"))


def _usage_cmd(a, extra):
    import json
    import shlex
    import subprocess
    import sys
    from . import usage
    if a.cmd == "usage":
        print(json.dumps(usage.status(), indent=2))
    elif a.cmd == "env":
        env = usage.fallback_env(a.arg or "claude", force=a.force)
        for k, v in (env or {}).items():
            print(f"export {k}={shlex.quote(v)}")
        sys.exit(0 if env else 1)
    else:   # statusline-tee: never let recording break the status bar
        raw = sys.stdin.buffer.read()
        try:
            rl = json.loads(raw or b"{}").get("rate_limits")
            if rl:
                usage.record_claude(rl)
        except Exception:  # noqa: BLE001
            pass
        if a.arg:   # the original status-line command, as one shell string
            sys.exit(subprocess.run(["sh", "-c", a.arg], input=raw).returncode)


if __name__ == "__main__":
    main()
