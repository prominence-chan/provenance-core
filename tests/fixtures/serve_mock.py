"""A real provenance-core for Provenance.app's end-to-end check (ui/scripts/router-e2e.ts): the server `serve` runs,
with its web chats pointed at the mock page (tests/fixtures/mock_chatgpt.py) instead of the real sites, all state in a
scratch PCORE_HOME, and every browser headless, so nothing shows on screen and nothing reaches a real site:

- the background browser: `headless: true` (the installed Google Chrome on a scratch profile);
- the sign-in window: started directly with --headless=new instead of `open -n -a` (WebSetup.launch = "exec", the
  one thing the router's own tests change too). The mock's sign-in page signs itself in when `signin_auto` is on, as
  a person would; the router then sees the session cookie, closes the window and checks the page as always.

    PYTHONPATH=. python tests/fixtures/serve_mock.py [--port 0] [--home DIR]

prints one JSON line, {"url", "mock", "home", "pid"}, once the server answers, and shuts down when stdin closes.
Set PCORE_BROWSER_APP to a Chrome for Testing app (or the folder holding one) to let "prepare the browser" copy it
instead of downloading ~190 MB.

Routes only this harness has: POST /mock/state {..} (the mock page's state: signin_auto, login, limit, email, ...),
GET /mock/sent (what the page was sent)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_chatgpt import SESSION, Mock  # noqa: E402

CHROME_APP = Path("/Applications/Google Chrome.app")


def write_catalog(home: Path, mock_url: str) -> Path:
    web = {
        "headless": True,
        "notify": False,
        "min_gap_s": 0,
        "max_gap_s": 0,
        "challenge_wait_s": 1,
        "skip_unchecked": True,
        "browser": "bundled",  # what GET /web/accounts reports (headless itself runs the installed Chrome)
        "login_timeout_s": 120,
        "session_cookies": {"domains": ["127.0.0.1"], "names": [SESSION]},
        "accounts": ["main"],
    }
    cat = {
        "tiers": [
            {"name": "nova", "min_score": 94},
            {"name": "solstice", "min_score": 82},
            {"name": "ember", "min_score": 62},
            {"name": "spark", "min_score": 0},
        ],
        "providers": {
            # an API provider without its key: the token pool says it needs keys
            "example": {"base_url": "https://api.example.invalid/v1", "api_key_env": "PCORE_MOCK_EXAMPLE_KEY"},
            "chatgpt-web": {
                "type": "chatgpt_web",
                "base_url": mock_url,
                "api_key_env": "none",
                "web": {**web, "daily_cap": 50, "profile": str(home / "chatgpt-web-profile")},
            },
            # a second site in the same browsers (`share:`), the way Qwen and Gemini work in the real catalog
            "other-web": {
                "type": "chatgpt_web",
                "base_url": mock_url,
                "api_key_env": "none",
                "web": {**web, "share": "chatgpt-web", "name": "Other Chat", "daily_cap": 30},
            },
        },
        "models": [
            {"provider": "chatgpt-web", "model": "luna", "score": 93, "context": 64000, "label": "GPT-6 Luna"},
            {"provider": "other-web", "model": "auto", "score": 50, "context": 64000, "label": "Other Auto"},
            {"provider": "example", "model": "big", "score": 86},
            {"provider": "example", "model": "small", "score": 40},
        ],
    }
    p = home / "catalog.yaml"
    p.write_text(yaml.safe_dump(cat))
    return p


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--home", default=None)
    a = ap.parse_args()
    made = not a.home  # a scratch home of its own is removed again on the way out
    home = Path(a.home or tempfile.mkdtemp(prefix="pcore-mock-")).resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["PCORE_HOME"] = str(home)
    os.environ["PCORE_CHATGPT_WEB"] = "1"  # as Provenance.app starts its own router
    os.environ.pop("PCORE_TOKEN", None)

    import uvicorn

    from provenance_core.__main__ import _exit_with_parent
    from provenance_core.routing.catalog import load
    from provenance_core.routing.router import Router
    from provenance_core.api.server import create_app

    mock = Mock()
    mock.state.update(login="cookie", signin_auto=False, signin_after_ms=800, flush=600)
    app = create_app(router=Router(load(write_catalog(home, mock.url))))
    setup = app.state.web_setup
    setup.launch, setup.extra_args, setup.app = "exec", ["--headless=new"], CHROME_APP
    setup.poll_s, setup.cookie_poll_s, setup.grace_s = 0.3, 0.5, 0.5

    @app.post("/mock/state")
    async def mock_state(body: dict):
        mock.state.update(body)
        return mock.state

    @app.get("/mock/sent")
    async def mock_sent():
        return {"sent": len(mock.sent), "signin_pages": mock.signin_pages, "signins": mock.signins}

    app.router.routes[:0] = [app.router.routes.pop(), app.router.routes.pop()]  # before the catch-all 404
    port = a.port or free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))

    async def run():
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        info = {"url": f"http://127.0.0.1:{port}", "mock": mock.url, "home": str(home), "pid": os.getpid()}
        print(json.dumps(info), flush=True)
        await task

    _exit_with_parent()
    try:
        asyncio.run(run())
    finally:
        mock.close()
        if made:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
