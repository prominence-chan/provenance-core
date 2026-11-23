"""provenance-core CLI (`python -m provenance_core ...`; `python -m tierlm ...` still works).

serve [--catalog catalog.yaml] [--scores scores.yaml] [--host 127.0.0.1] [--port 4141] [--watch-stdin]
                                --watch-stdin: shut down when stdin closes (the app that started it went away)
calibrate [--only id1,id2]      run the probe set, write scores.yaml
tiers                           show tiers, models, scores and which providers are usable
discover <provider>             list the provider's /models (to update the catalog)
usage                           Claude Code / Codex subscription headroom (JSON)
env claude|codex [--force]      shell exports that point the tool at provenance-core, if it's running low
statusline-tee '<cmd>'          Claude Code status-line wrapper: records rate limits, then runs cmd
weblogin [provider] [--account b]  sign in to a web chat (default: the first one, ChatGPT) in a window that
                                closes by itself once you are in, then check it; through the server when it runs
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
    ap.add_argument("--account", default=None, help="weblogin: which of the provider's `accounts` to sign in")
    ap.add_argument("--catalog", default=env("CATALOG", "catalog.yaml"))
    ap.add_argument("--scores", default=env("SCORES", "scores.yaml"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(env("PORT", 4141)))
    ap.add_argument("--only", default="")
    ap.add_argument("--watch-stdin", action="store_true", help="serve: shut down when stdin reaches EOF")
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
        if a.watch_stdin:
            _exit_with_parent()
        # a stop must reach the shutdown hooks (which close the browsers cleanly, so their cookies are written) before
        # launchd gives up on the process: do not wait on open streams for longer than this
        uvicorn.run(create_app(router=Router(cat)), host=a.host, port=a.port, timeout_graceful_shutdown=4)
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


def _exit_with_parent():
    """`serve --watch-stdin`: the process that started the router (Provenance.app) holds the other end of its stdin.
    When that process goes away, however it goes (quit, crash, kill -9), stdin reaches EOF and the router shuts
    down as it does on Ctrl+C, closing its browsers and any sign-in window; if that hangs, it exits anyway."""
    import os
    import signal
    import sys
    import threading

    def wait():
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        print("stdin closed: the app that started this router is gone; shutting down", flush=True)
        os.kill(os.getpid(), signal.SIGTERM)
        t = threading.Timer(20.0, lambda: os._exit(1))
        t.daemon = True
        t.start()

    threading.Thread(target=wait, daemon=True, name="watch-stdin").start()


def _web_boot(cat):
    """Server start: say where the chatgpt-web provider stands (from the last check; the page isn't opened)."""
    from . import browser_host
    from .chatgpt_web import LOGIN_HINT, make_bridge, notify

    # bundled / auto: have the bridge's own browser ready before the first request (a ~200 MB download the first
    # time, in the background; its progress is in GET /web/accounts)
    for p in cat.providers.values():
        o = p.options
        if (
            p.type == "chatgpt_web"
            and not o.get("share")
            and str(o.get("browser") or "").lower() in ("bundled", "auto")
        ):
            if str(o.get("browser_source") or "cft").lower() == "cft":
                browser_host.ensure_soon(make_bridge(p.base_url, o).profile)
            break
    for p in cat.providers.values():
        if p.type != "chatgpt_web" or not p.usable:
            continue
        st = make_bridge(p.base_url, p.options).status()
        for n, s in (st.get("accounts") or {}).items():
            state = "signed in" if s["signed_in"] else "NOT signed in (provenance-core weblogin --account " + n + ")"
            print(
                f"chatgpt-web account {n}: {state}, {s['today']} of {s['daily_cap']} today"
                + (f", out for {s['blocked_for_s'] // 60} more min: {s['reason']}" if s["blocked"] else ""),
                flush=True,
            )
        if st["signed_in"] and not st["blocked"]:
            print(f"chatgpt-web: on, signed in, {st['today']} of {st['daily_cap']} requests used today", flush=True)
        elif st["signed_in"] is False or st["kind"] == "login" or st["signed_in"] is None:
            print(
                f"chatgpt-web: on but the ChatGPT login is missing or expired; {LOGIN_HINT}. Requests use the "
                "normal pool until then.",
                flush=True,
            )
            if p.options.get("notify", True):  # Provenance.app's catalog turns these off: the app shows it itself
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

    from .chatgpt_web import LOGIN_HINT, make_bridge, notify

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

    bridge = make_bridge(p.base_url, p.options)
    if a.cmd == "weblogin":  # the same sign-in flow as the app's (web_setup.py): through the server when it runs
        from .web_setup import cli_login, local_call

        prov = a.arg or name
        if prov not in cat.providers or cat.providers[prov].type != "chatgpt_web":
            sys.exit(
                f"no web provider {prov!r} (one of: {', '.join(n for n, q in cat.providers.items() if q.type == 'chatgpt_web')})"
            )

        def call(method, path, body=None):
            try:
                r = httpx.request(method, base + path, headers=hdr, json=body, timeout=180)
                return r.status_code, r.json()
            except (httpx.HTTPError, ValueError):
                return None, None

        status, _ = call("GET", "/web/accounts")
        sleep = time.sleep
        if status != 200:  # no server (or an older one): the flow runs here
            call, sleep = local_call(cat)
        sys.exit(cli_login(call, prov, a.account, sleep=sleep))
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
    for n, s in (st.get("accounts") or {}).items():
        print(
            f"  account {n}: {'signed in' if s['signed_in'] else 'not signed in'}, {s['today']} of {s['daily_cap']}"
            + (f", out for {s['blocked_for_s'] // 60} more min: {s['reason']}" if s["blocked"] else "")
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
