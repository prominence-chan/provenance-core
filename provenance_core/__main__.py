"""provenance-core CLI (`python -m provenance_core ...`; `python -m tierlm ...` still works).

serve [--catalog catalog.yaml] [--scores scores.yaml] [--host 127.0.0.1] [--port 4141]
calibrate [--only id1,id2]      run the probe set, write scores.yaml
tiers                           show tiers, models, scores and which providers are usable
discover <provider>             list the provider's /models (to update the catalog)
usage                           Claude Code / Codex subscription headroom (JSON)
env claude|codex [--force]      shell exports that point the tool at provenance-core, if it's running low
statusline-tee '<cmd>'          Claude Code status-line wrapper: records rate limits, then runs cmd
weblogin                        chatgpt-web: open Chrome on its own profile so you can sign in to ChatGPT once
webstatus [--probe] [--boot]    chatgpt-web: enabled? signed in? today's count; --probe checks the page live,
                                --boot is the quiet form for launchers (prints only when something needs you)"""

import argparse
import asyncio

import httpx

from .catalog import load
from .router import Router
from .settings import env


def main():
    ap = argparse.ArgumentParser(prog="provenance-core")
    ap.add_argument(
        "cmd",
        choices=["serve", "calibrate", "tiers", "discover", "usage", "env", "statusline-tee", "weblogin", "webstatus"],
    )
    ap.add_argument("arg", nargs="?")
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--boot", action="store_true")
    ap.add_argument("--catalog", default=env("CATALOG", "catalog.yaml"))
    ap.add_argument("--scores", default=env("SCORES", "scores.yaml"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(env("PORT", 4141)))
    ap.add_argument("--only", default="")
    a, extra = ap.parse_known_args()
    if a.cmd in ("usage", "env", "statusline-tee"):
        return _usage_cmd(a, extra)
    cat = load(a.catalog, a.scores)
    if a.cmd in ("weblogin", "webstatus"):
        return _web_cmd(a, cat)
    if a.cmd == "serve":
        import uvicorn

        from .server import create_app

        _web_boot(cat)
        uvicorn.run(create_app(router=Router(cat)), host=a.host, port=a.port)
    elif a.cmd == "calibrate":
        from .calibrate import calibrate

        asyncio.run(calibrate(Router(cat), a.scores, [x for x in a.only.split(",") if x] or None))
    elif a.cmd == "tiers":
        for t, floor in cat.tiers:
            print(f"{t} (score >= {floor:g})")
            for m in cat.models_in(t, usable_only=False):
                p = cat.providers[m.provider]
                key = ("key" if p.api_key else "no auth") if p.usable else "no key"
                if p.type == "chatgpt_web":
                    key = "web, on" if p.usable else "web, off"
                print(f"  {m.score:5.1f}{'*' if m.calibrated else ' '} {m.id:45s} {key}")
        print("* = calibrated")
    elif a.cmd == "discover":
        p = cat.providers[a.arg]
        r = httpx.get(f"{p.base_url}/models", headers={**p.auth_headers(), **p.headers}, timeout=30)
        for m in r.json().get("data", []):
            print(m.get("id"))


def _web_boot(cat):
    """Server start: say where the chatgpt-web provider stands (from the last check; the page isn't opened)."""
    from .chatgpt_web import LOGIN_HINT, Bridge, notify

    for p in cat.providers.values():
        if p.type != "chatgpt_web" or not p.usable:
            continue
        st = Bridge(p.base_url, p.options).status()
        if st["signed_in"] and not st["blocked"]:
            print(f"chatgpt-web: on, signed in, {st['today']} of {st['daily_cap']} requests used today", flush=True)
        elif st["signed_in"] is False or st["kind"] == "login" or st["signed_in"] is None:
            print(
                f"chatgpt-web: on but the ChatGPT login is missing or expired; {LOGIN_HINT}. Requests use the "
                "normal pool until then.",
                flush=True,
            )
            notify("ChatGPT web login is missing or expired: run `provenance-core weblogin`")
        else:
            print(
                f"chatgpt-web: on but unavailable for {st['blocked_for_s'] // 60} more min: {st['reason']}", flush=True
            )


def _web_cmd(a, cat):
    """weblogin / webstatus. The server owns the browser while it runs, so the live check goes through it and
    weblogin asks it to let go of the profile first."""
    import sys
    import time

    from .chatgpt_web import LOGIN_HINT, Bridge, login_window, notify

    name = next((n for n, p in cat.providers.items() if p.type == "chatgpt_web"), None)
    if not name:
        sys.exit("no chatgpt_web provider in the catalog")
    p = cat.providers[name]
    base = (env("BASE_URL") or f"http://127.0.0.1:{a.port}").rstrip("/")
    hdr = {"Authorization": f"Bearer {env('TOKEN')}"} if env("TOKEN") else {}

    def server(method, path, timeout=90):
        try:
            r = httpx.request(method, base + path, headers=hdr, timeout=timeout)
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    bridge = Bridge(p.base_url, p.options)
    if a.cmd == "weblogin":
        server("POST", "/chatgpt-web/release")
        code = login_window(bridge)
        if code == 0:
            server("POST", "/chatgpt-web/release?reset=1")
        sys.exit(code)
    on = p.usable
    if a.boot and not on:
        return  # not turned on: nothing to say at boot
    st = bridge.status()
    stale = not st["checked_at"] or time.time() - st["checked_at"] > 12 * 3600
    if on and (a.probe or (a.boot and stale and not st["blocked"])):
        st = server("GET", "/chatgpt-web/status?probe=1") or st
    ok = on and st.get("signed_in") and not st.get("blocked")
    if a.boot:
        if not ok:
            why = st.get("reason") or "not signed in"
            msg = f"chatgpt-web is on but not usable ({why}); requests use the normal pool."
            if not st.get("signed_in") or st.get("kind") == "login":
                msg += f" To sign in: {LOGIN_HINT}."
                notify("ChatGPT web login is missing or expired: run `provenance-core weblogin`")
            print(msg)
        return
    print(
        f"chatgpt-web: {'on' if on else 'off (set PCORE_CHATGPT_WEB=1 in ~/.tierlm/keys.env, or enabled: true in the catalog)'}"
    )
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st["checked_at"])) if st.get("checked_at") else "never"
    print(
        f"  signed in: {'yes' if st.get('signed_in') else 'no' if st.get('signed_in') is False else 'unknown'} (checked {when})"
    )
    print(
        f"  today: {st.get('today', 0)} of {st.get('daily_cap')} requests"
        + (f"; last model seen: {st['model_seen']}" if st.get("model_seen") else "")
    )
    if st.get("blocked"):
        print(f"  unavailable for {st['blocked_for_s'] // 60} more min: {st.get('reason')}")
    if not st.get("signed_in"):
        print(f"  to sign in: {LOGIN_HINT}")
    sys.exit(0 if ok else 1)


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
    else:  # statusline-tee: never let recording break the status bar
        raw = sys.stdin.buffer.read()
        try:
            rl = json.loads(raw or b"{}").get("rate_limits")
            if rl:
                usage.record_claude(rl)
        except Exception:  # noqa: BLE001
            pass
        if a.arg:  # the original status-line command, as one shell string
            sys.exit(subprocess.run(["sh", "-c", a.arg], input=raw).returncode)


if __name__ == "__main__":
    main()
