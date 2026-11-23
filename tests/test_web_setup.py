"""web_setup: the /web/* endpoints (accounts, sign-in windows, the bundled browser) and `weblogin`'s use of them.

The sign-in window is the real flow against the mock page (tests/mock_chatgpt.py, login="cookie": signed in means the
browser sends the mock's session cookie) on scratch profiles, with one difference: the window is a headless Chrome
started directly (WebSetup.launch = "exec"), so nothing shows. Its sign-in page signs itself in, as a person would."""

import asyncio
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from mock_chatgpt import SESSION, Mock

from provenance_core import browser_host as bh
from provenance_core import chatgpt_web as cw
from provenance_core import web_setup as ws
from provenance_core.catalog import load
from provenance_core.router import Router
from provenance_core.server import create_app
from provenance_core.state import State

CHROME_APP = Path("/Applications/Google Chrome.app")


def _chrome_ok():
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(cw.CHROME)


browser = pytest.mark.skipif(not _chrome_ok(), reason="needs playwright and Google Chrome")


def catalog(tmp_path, url="http://127.0.0.1:9", accounts=("main", "b"), **web):
    opts = {
        "driver": os.environ.get("PCORE_TEST_DRIVER", "playwright"),
        "headless": True,
        "notify": False,
        "min_gap_s": 0,
        "max_gap_s": 0,
        "challenge_wait_s": 1,
        "profile": str(tmp_path / "profile"),
        "session_cookies": {"domains": ["127.0.0.1"], "names": [SESSION]},
        **({"accounts": list(accounts)} if accounts else {}),
        **web,
    }
    cat = {
        "tiers": [{"name": "nova", "min_score": 90}, {"name": "spark", "min_score": 0}],
        "providers": {
            "chatgpt-web": {
                "type": "chatgpt_web",
                "base_url": url,
                "api_key_env": "none",
                "enabled": True,
                "web": opts,
            },
            # a second site working in the same browsers (`share:`), like Qwen and Gemini do
            "other-web": {
                "type": "chatgpt_web",
                "base_url": url,
                "api_key_env": "none",
                "web": {
                    **{k: v for k, v in opts.items() if k not in ("accounts", "profile")},
                    "share": "chatgpt-web",
                    "name": "Other Chat",
                    **({"accounts": list(accounts)} if accounts else {}),
                },
            },
        },
        "models": [
            {"provider": "chatgpt-web", "model": "luna", "score": 93, "label": "GPT-6 Luna"},
            {"provider": "other-web", "model": "auto", "score": 50, "label": "Other Auto", "accounts": ["b"]},
        ],
    }
    p = tmp_path / "catalog.yaml"
    p.write_text("# hand-written comments stay\n" + yaml.safe_dump(cat))
    return p


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PCORE_HOME", str(tmp_path))
    return tmp_path


def client_for(path):
    router = Router(
        load(path), State(), httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    )
    app = create_app(router=router)
    setup = app.state.web_setup
    # the window: Chrome, started directly and headless (nothing shows); polled fast
    setup.launch, setup.extra_args, setup.app = "exec", ["--headless=new"], CHROME_APP
    setup.poll_s, setup.cookie_poll_s, setup.grace_s = 0.2, 0.5, 0.5
    return router, app, setup


def wait_state(c, want, provider="chatgpt-web", account="b", t=90):
    end, st = time.time() + t, None
    while time.time() < end:
        st = c.get("/web/login", params={"provider": provider, "account": account}).json()
        if st["state"] in want:
            return st
        time.sleep(0.2)
    raise AssertionError(f"still {st} after {t} s")


def running_on(profile) -> list[str]:
    out = subprocess.run(["pgrep", "-f", f"user-data-dir={profile}"], capture_output=True, text=True).stdout
    return out.split()


@pytest.fixture
def mock():
    m = Mock()
    m.state["login"] = "cookie"
    yield m
    m.close()


# ---- the whole flow, against the mock page ---------------------------------------------------------------------------
@browser
def test_sign_in_finishes_by_itself(mock, tmp_path, home):
    """signed out -> POST /web/login -> the window signs in -> the cookie is seen -> window closed -> checked in the
    background browser -> signed_in, with the identity the page shows."""
    mock.state.update(signin_auto=True, flush=600)  # flush: Chrome writes the cookie at once instead of within 30 s
    router, app, setup = client_for(catalog(tmp_path, mock.url))
    prof_b = Path(str(tmp_path / "profile") + "-b")
    with TestClient(app) as c:
        try:
            before = c.get("/web/accounts").json()
            st = c.get("/chatgpt-web/status", params={"probe": 1, "account": "b"}).json()
            assert st["accounts"]["b"]["signed_in"] is False  # the background browser sees the signed-out page
            assert router.bridge("chatgpt-web").bridges["b"]._page is not None
            r = c.post("/web/login", json={"provider": "chatgpt-web", "account": "b"})
            assert r.status_code == 200 and r.json()["state"] == "open"
            b = router.bridge("chatgpt-web").bridges["b"]
            during = dict(
                page=b._page,
                paused=b.signing_in(),
                cap=b.capacity(),
                guest=router.bridge("other-web").bridges["b"].signing_in(),
            )
            done = wait_state(c, ("signed_in", "failed"))
            after = c.get("/web/accounts").json()
            flow = setup.flows[("chatgpt-web", "b")]
        finally:
            c.post("/chatgpt-web/release")
    assert done["state"] == "signed_in" and done["reason"] is None and flow.how == "cookie", done
    assert mock.signin_pages == 1 and mock.signins == 1
    # while the window was open, the account's background browser was closed and every site on it skipped
    assert during["page"] is None and during["paused"] and during["guest"] and not during["cap"]["available"]
    acct = {a["id"]: a for a in after["providers"][0]["accounts"]}
    assert acct["b"]["signed_in"] is True and acct["b"]["email"] == "person@example.com"
    assert acct["b"]["login"] == {"state": "signed_in", "reason": None} and not acct["b"]["signing_in"]
    assert acct["main"]["login"] == {"state": "idle", "reason": None}
    assert before["providers"][0]["accounts"][1]["login"]["state"] == "idle"
    assert not running_on(prof_b)  # neither the window nor the background browser is left running


@browser
def test_window_closed_by_hand(mock, tmp_path, home):
    """No cookie to watch for: closing the window is what finishes it; signed in -> signed_in, not -> failed."""
    path = catalog(tmp_path, mock.url, session_cookies={"names": []})
    router, app, setup = client_for(path)
    with TestClient(app) as c:
        try:
            mock.state.update(signin_auto=True, flush=600)
            assert c.post("/web/login", json={"provider": "other-web", "account": "b"}).json()["state"] == "open"
            # the user quits the window once signed in. A person's Cmd+Q writes the cookies first; a headless window
            # cannot be quit that way and a SIGTERM does not write them, so wait until they are on disk
            spec, end = {"domains": ["127.0.0.1"], "names": [SESSION]}, time.time() + 30
            while not ws.session_cookies(tmp_path / "profile-b", spec) and time.time() < end:
                time.sleep(0.2)
            os.kill(setup.flows[("other-web", "b")].window.pid, signal.SIGTERM)
            ok = wait_state(c, ("signed_in", "failed"), "other-web")
            how = setup.flows[("other-web", "b")].how
            # main: closed without signing in
            mock.state["signin_auto"] = False
            assert c.post("/web/login", json={"provider": "chatgpt-web", "account": "main"}).json()["state"] == "open"
            time.sleep(1)
            os.kill(setup.flows[("chatgpt-web", "main")].window.pid, signal.SIGTERM)
            bad = wait_state(c, ("signed_in", "failed"), "chatgpt-web", "main")
            again = c.get("/web/login", params={"provider": "chatgpt-web", "account": "main"}).json()
        finally:
            c.post("/chatgpt-web/release")
    assert ok["state"] == "signed_in" and how == "closed", ok
    assert bad == {**bad, "state": "failed", "reason": "The window was closed before sign-in finished."}
    assert again["state"] == "failed"  # it stays that way until the next POST or a cancel


@browser
def test_timeout_cancel_and_one_window_per_account(mock, tmp_path, home):
    router, app, setup = client_for(catalog(tmp_path, mock.url, login_timeout_s=2))
    prof = tmp_path / "profile-b"
    with TestClient(app) as c:
        try:
            assert c.post("/web/login", json={"provider": "chatgpt-web", "account": "b"}).json()["state"] == "open"
            # the same account's browser is held: another site on it must wait
            r = c.post("/web/login", json={"provider": "other-web", "account": "b"})
            conflict = (r.status_code, r.json())
            timed = wait_state(c, ("signed_in", "failed"))
            # cancel: the window goes, the account is usable again, the state is idle
            setup.bridges_of("chatgpt-web")["b"].o["login_timeout_s"] = 600
            assert c.post("/web/login", json={"provider": "chatgpt-web", "account": "b"}).json()["state"] == "open"
            pid = setup.flows[("chatgpt-web", "b")].window.pid
            cancelled = c.post("/web/login/cancel", json={"provider": "chatgpt-web", "account": "b"}).json()
            gone = not setup.flows.get(("chatgpt-web", "b")) and subprocess.run(["kill", "-0", str(pid)]).returncode
            state = c.get("/web/login", params={"provider": "chatgpt-web", "account": "b"}).json()
            # not paused any more (it is out for a while only because the timed-out check found it signed out)
            usable = not any(b.signing_in() for b in setup.profile_bridges(prof))
            blocked = setup.bridges_of("chatgpt-web")["b"].status()["kind"]
            unknown = c.get("/web/login", params={"provider": "nope", "account": "b"}).status_code
            unknown_acct = c.post("/web/login", json={"provider": "chatgpt-web", "account": "zz"}).status_code
        finally:
            c.post("/chatgpt-web/release")
    assert conflict[0] == 409 and conflict[1]["state"] == "idle"
    assert (
        conflict[1]["detail"].startswith("A sign-in window is already open") and "chatgpt" not in conflict[1]["detail"]
    )
    assert timed["state"] == "failed" and timed["reason"].startswith("The sign-in window was closed after 2 seconds")
    assert cancelled["state"] == "idle" and gone and state["state"] == "idle" and usable and blocked == "login"
    assert unknown == 404 and unknown_acct == 404
    assert not running_on(prof)


# ---- accounts ------------------------------------------------------------------------------------------------------
def test_accounts_are_global_and_kept(tmp_path, home):
    path = catalog(tmp_path)
    original = path.read_bytes()
    router, app, setup = client_for(path)
    c = TestClient(app)
    v = c.get("/web/accounts").json()
    assert v["accounts"] == ["main", "b"]
    assert [p["id"] for p in v["providers"]] == ["chatgpt-web", "other-web"]
    p0, p1 = v["providers"]
    assert (p0["name"], p0["url"], p1["name"], p1["shares_browser_with"]) == (
        "ChatGPT",
        "http://127.0.0.1:9",
        "Other Chat",
        "chatgpt-web",
    )
    assert p0["models"] == [
        {"id": "chatgpt-web/luna", "label": "GPT-6 Luna", "tier": "nova", "model": "luna", "accounts": None}
    ]
    assert p1["models"][0]["accounts"] == ["b"] and p1["models"][0]["tier"] == "spark"
    a = p0["accounts"][0]
    assert {"id", "signed_in", "checked_at", "blocked", "reason", "today", "daily_cap", "email", "login"} <= set(a)
    assert a["signed_in"] is None and a["login"] == {"state": "idle", "reason": None}
    assert v["browser"]["mode"] == "chrome" and "progress" in v["browser"]

    router.bridge("chatgpt-web")  # built before the change: gains the account at once
    r = c.post("/web/accounts", json={"account": "d"})
    assert r.status_code == 200 and r.json()["accounts"] == ["main", "b", "d"]
    assert r.json()["profile"].endswith("profile-d")
    v = c.get("/web/accounts").json()
    assert all([x["id"] for x in p["accounts"]] == ["main", "b", "d"] for p in v["providers"])
    g = router.bridge("other-web").bridges["d"]
    assert g.host is router.bridge("chatgpt-web").bridges["d"] and g.profile == g.host.profile
    assert c.post("/web/accounts", json={"account": "d"}).status_code == 409
    bad = c.post("/web/accounts", json={"account": "../x"})
    assert bad.status_code == 409 and "letters" in bad.json()["detail"]
    # kept across a restart, without touching catalog.yaml
    assert path.read_bytes() == original
    assert load(path).providers["other-web"].options["accounts"] == ["main", "b", "d"]
    (tmp_path / "profile-b").mkdir()
    r = c.delete("/web/accounts/b")
    assert r.status_code == 200 and r.json()["removed"] and r.json()["profile_kept"].endswith("profile-b")
    assert "not deleted" in r.json()["note"] and (tmp_path / "profile-b").is_dir()
    assert load(path).providers["chatgpt-web"].options["accounts"] == ["main", "d"]
    assert c.get("/web/accounts").json()["accounts"] == ["main", "d"]
    assert c.delete("/web/accounts/b").status_code == 404
    c.delete("/web/accounts/d")
    last = c.delete("/web/accounts/main")
    assert last.status_code == 409 and "only account" in last.json()["detail"]
    assert path.read_bytes() == original
    # the model only `b` had: no account serves it now (not some other account)
    with pytest.raises(cw.WebSkip):
        asyncio.run(
            router.bridge("other-web").complete(
                "auto", {"messages": [{"role": "user", "content": "x"}]}, accounts=["b"]
            )
        )


def test_one_account_becomes_a_pool(tmp_path, home):
    router, app, setup = client_for(catalog(tmp_path, accounts=None))
    single = router.bridge("chatgpt-web")
    assert (
        isinstance(single, cw.Bridge)
        and load(tmp_path / "catalog.yaml").providers["chatgpt-web"].options.get("accounts") is None
    )
    c = TestClient(app)
    assert c.get("/web/accounts").json()["accounts"] == ["main"]
    assert c.post("/web/accounts", json={"account": "b"}).status_code == 200
    pool = router.bridge("chatgpt-web")
    assert isinstance(pool, cw.BridgePool) and pool.bridges["main"] is single and list(pool.bridges) == ["main", "b"]
    assert router.bridge("other-web").bridges["b"].host is pool.bridges["b"]


def test_catalog_lists_are_merged(home):
    ov = {"added": ["x"], "removed": ["c"]}
    assert ws.merge_accounts([["main", "b", "c"], None, ["b", "e"]], ov) == ["main", "b", "e", "x"]
    assert ws.merge_accounts([None], {"added": [], "removed": []}) == ["main"]


# ---- the bundled browser: progress, and what auto does meanwhile ------------------------------------------------------
def test_browser_prepare_and_auto_mode(tmp_path, home, monkeypatch):
    rec = {"exe": "/nonexistent/x", "version": "157.0.1.2", "plain_app": "/nonexistent/x.app", "app": "/x"}
    ready = []

    def fake_ensure(profile=None, root=None, **k):
        bh._progress(preparing=True, stage="download", done=50, total=200, error=None)
        for _ in range(100):
            if ready:
                break
            time.sleep(0.05)
        bh._progress(preparing=False, stage=None, finished_at=time.time())
        if ready == ["fail"]:
            bh._progress(
                error="The browser could not be downloaded. Check your internet connection; it will be tried again."
            )
            return None
        return rec

    monkeypatch.setattr(bh, "ensure", fake_ensure)
    monkeypatch.setattr(bh, "current", lambda root=None, need=0: rec if ready == ["ok"] else None)
    monkeypatch.setattr(bh, "platform_key", lambda: "mac-arm64")
    router, app, setup = client_for(catalog(tmp_path, browser="auto", headless=False))
    c = TestClient(app)
    b = router.bridge("chatgpt-web").bridges["main"]
    first = c.get("/web/accounts").json()["browser"]
    st = c.post("/web/browser/prepare").json()
    with pytest.raises(cw.WebSkip, match="being prepared"):  # never the installed Chrome instead: nothing shows
        asyncio.run(b._bundled_browser())
    mid = c.get("/web/accounts").json()["browser"]
    ready.append("fail")
    bh._bg.join(5)
    failed = c.get("/web/accounts").json()["browser"]
    with pytest.raises(cw.WebUnavailable) as e:
        asyncio.run(b._bundled_browser())
    assert e.value.kind == "browser" and not bh._bg.is_alive()  # not retried on every request
    b.o["browser_fallback"] = "chrome"
    assert asyncio.run(b._bundled_browser()) is None  # the old way, when asked for
    ready[:] = ["ok"]
    done = c.post("/web/browser/prepare").json()
    assert first["mode"] == "auto" and first["ready"] is False
    assert st["preparing"] is True and mid["progress"] == 0.25 and mid["stage"] == "download"
    assert failed["preparing"] is False and failed["progress"] is None and "internet connection" in failed["error"]
    assert done["ready"] is True and done["version"] == "157.0.1.2" and done["error"] is None


def test_signin_app_is_the_background_build(tmp_path, monkeypatch):
    from test_browser_host import fake_app

    plain = fake_app(tmp_path / "plain", "157.0.8103.78", "Google Chrome for Testing")
    chrome = fake_app(tmp_path / "Applications", "157.0.8103.80", "Google Chrome")
    monkeypatch.setattr(bh, "CHROME_APP", chrome)
    monkeypatch.setattr(bh, "current", lambda root=None, need=0: {"plain_app": str(plain), "version": "157.0.8103.78"})
    prof = tmp_path / "p"
    prof.mkdir()
    assert bh.signin_app(prof, "bundled") == plain and bh.signin_app(prof, "auto") == plain
    assert bh.signin_app(prof, "chrome") == chrome and bh.signin_app(prof, "bundled", "chrome") == chrome
    (prof / "Last Version").write_text("158.0.1.0")  # written by a newer browser: neither may open it
    assert bh.signin_app(prof, "bundled") is None and bh.signin_app(prof, "chrome") is None
    monkeypatch.setattr(bh, "current", lambda root=None, need=0: None)
    (prof / "Last Version").write_text("157.0.8103.78")
    assert bh.signin_app(prof, "bundled", fallback=False) is None and bh.signin_app(prof, "bundled") == chrome


def test_browser_app_may_name_a_folder(tmp_path, monkeypatch):
    from test_browser_host import fake_app

    app = fake_app(tmp_path / "Resources/browser", "157.0.8103.78", "Google Chrome for Testing")
    monkeypatch.setenv("PCORE_BROWSER_APP", str(tmp_path / "Resources/browser"))
    assert app in bh.local_sources()


# ---- pieces -----------------------------------------------------------------------------------------------------------
def test_the_window_is_not_automated():
    w = ws.SigninWindow(Path("/x.app"), Path("/p"), "https://chatgpt.com/auth/login", {"left": 5, "width": 700})
    a = w.args()
    assert a[0] == "--user-data-dir=/p" and a[-1] == "https://chatgpt.com/auth/login"
    assert "--window-position=5,60" in a and "--window-size=700,800" in a
    # measured: a debugging port alone makes navigator.webdriver true on every page (docs/TECHNICAL.md)
    assert not [x for x in a if "remote-debugging" in x or "automation" in x.lower() or "headless" in x]
    assert {"--use-mock-keychain", "--password-store=basic"} <= set(a)  # the background browser's cookie key


def test_session_cookies_reads_names_and_dates_only(tmp_path):
    db = tmp_path / "Default/Network/Cookies"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("create table cookies (host_key text, name text, value text, encrypted_value blob, creation_utc int,"
                " last_update_utc int, expires_utc int)")  # fmt: skip
    rows = [(".chatgpt.com", "__Secure-next-auth.session-token.0", 1, 2, 9), ("chatgpt.com", "__cf_bm", 1, 1, 9),
            ("evil-chatgpt.com", "__Secure-next-auth.session-token", 1, 1, 9), (".google.com", "SID", 3, 3, 9)]  # fmt: skip
    con.executemany("insert into cookies values (?, ?, 'secret', x'00', ?, ?, ?)", rows)
    con.commit()
    con.close()
    got = ws.session_cookies(tmp_path, cw.CHATGPT_SITE["session"])
    assert got == {(".chatgpt.com", "__Secure-next-auth.session-token.0"): (1, 2, 9)}
    assert list(ws.session_cookies(tmp_path, cw.SITES["gemini"]["session"])) == [(".google.com", "SID")]
    assert ws.session_cookies(tmp_path / "none", cw.CHATGPT_SITE["session"]) == {}
    assert ws.session_cookies(tmp_path, {"names": []}) == {}


def test_park_windows_puts_moved_and_new_windows_back():
    class CDP:
        def __init__(self):
            self.wins = {1: {"left": 1670, "top": 1071, "width": 1180, "height": 860, "windowState": "normal"},
                         2: {"left": 300, "top": 200, "width": 500, "height": 400, "windowState": "maximized"},
                         3: {"left": 0, "top": 0, "width": 500, "height": 400, "windowState": "minimized"}}  # fmt: skip
            self.sent = []

        async def send(self, m, p=None):
            self.sent.append((m, p))
            if m == "Target.getTargets":
                return {"targetInfos": [{"type": "page", "targetId": f"t{i}"} for i in (1, 1, 2, 3)]
                        + [{"type": "service_worker", "targetId": "sw"}]}  # fmt: skip
            if m == "Browser.getWindowForTarget":
                wid = int(p["targetId"][1:])
                return {"windowId": wid, "bounds": dict(self.wins[wid])}
            if m == "Browser.setWindowBounds":
                b = self.wins[p["windowId"]]
                b.update(p["bounds"])
                if "left" in p["bounds"]:
                    b.update(left=1670, top=1071)  # where macOS constrains it
                return {}
            if m == "Browser.getWindowBounds":
                return {"bounds": dict(self.wins[p["windowId"]])}

    cdp = CDP()
    parked = asyncio.run(cw.park_windows(cdp, {}))
    sets = [p for m, p in cdp.sent if m == "Browser.setWindowBounds"]
    assert {"windowId": 2, "bounds": {"windowState": "normal"}} in sets and {"windowId": 2, "bounds": cw.PARK} in sets
    assert not [p for p in sets if p["windowId"] == 3]  # minimized: nothing shows
    cdp.sent.clear()
    assert asyncio.run(cw.park_windows(cdp, parked)) == parked
    assert not [p for m, p in cdp.sent if m == "Browser.setWindowBounds"]  # nothing moved: nothing done
    cdp.wins[1].update(left=200, top=100)  # macOS moved it onto the display (a display was unplugged, ...)
    asyncio.run(cw.park_windows(cdp, parked))
    assert ("Browser.setWindowBounds", {"windowId": 1, "bounds": cw.PARK}) in cdp.sent


def test_reasons_are_plain_words():
    b = cw.Bridge("https://gemini.google.com", {"site": "gemini", "notify": False})
    for kind in ("login", "challenge", "limit", "busy", "page", "tempchat", "model", "closed", "cap", "browser", "x"):
        r = ws.plain_reason({"kind": kind, "blocked_for_s": 600, "daily_cap": 200}, b)
        assert r and not any(w in r for w in ("chatgpt-web", "profile", "weblogin", "bridge", "_")), r
    assert b.display_name == "Gemini" and b.login_url.startswith("https://accounts.google.com/")
    assert cw.Bridge("https://chatgpt.com", {}).login_url == "https://chatgpt.com/auth/login"
    assert cw.Bridge("https://chat.qwen.ai", {"site": "qwen"}).login_url == "https://chat.qwen.ai/auth"


def test_weblogin_follows_the_server_flow():
    states = iter([(200, {"state": "open", "reason": None}), (200, {"state": "open", "reason": None}),
                   (200, {"state": "verifying", "reason": None}), (200, {"state": "signed_in", "reason": None})])  # fmt: skip
    calls, out = [], []

    def call(method, path, body=None):
        calls.append((method, path, body))
        prep = {"state": "preparing", "reason": "Getting the browser ready (40%). This happens once."}
        return (200, prep) if method == "POST" else next(states)

    assert ws.cli_login(call, "gemini-web", "b", out=out.append, sleep=lambda s: None) == 0
    assert calls[0] == ("POST", "/web/login", {"provider": "gemini-web", "account": "b"})
    assert calls[1] == ("GET", "/web/login?provider=gemini-web&account=b", None)
    assert out[0] == "preparing... Getting the browser ready (40%). This happens once."
    assert out[1].startswith("A sign-in window is open") and out[-1].startswith("Signed in")
    assert sum(o.startswith("A sign-in window") for o in out) == 1  # each state said once
    out.clear()
    fail = iter([(200, {"state": "failed", "reason": "The window was closed before sign-in finished."})])
    assert ws.cli_login(lambda m, p, b=None: (200, {"state": "open"}) if m == "POST" else next(fail), "x", None,
                        out=out.append, sleep=lambda s: None) == 1  # fmt: skip
    assert out[-1] == "Not signed in: The window was closed before sign-in finished."
    assert ws.cli_login(lambda m, p, b=None: (409, {"detail": "busy"}), "x", None, out=out.append) == 1


def test_local_weblogin_reports_unknown_accounts(tmp_path, home):
    call, sleep = ws.local_call(load(catalog(tmp_path)))
    assert call("POST", "/web/login", {"provider": "chatgpt-web", "account": "zz"})[0] == 404
    assert call("GET", "/web/login?provider=chatgpt-web&account=b", None) == (
        200, {"state": "idle", "reason": None, "provider": "chatgpt-web", "account": "b"})  # fmt: skip


# ---- the bundled browser keeps its windows off screen -------------------------------------------------------------------
def _bundled_ready():
    if not _chrome_ok():
        return None
    return bh.current(need=bh.required_major())


@pytest.mark.skipif(not _bundled_ready(), reason="no bundled browser prepared (python -m provenance_core.browser_host)")
def test_bundled_window_moved_on_screen_goes_back(mock, tmp_path, monkeypatch):
    rec = _bundled_ready()
    monkeypatch.setattr(bh, "ensure", lambda profile=None, **k: rec)
    mock.state["login"] = True

    async def go():
        b = cw.Bridge(mock.url, {"driver": os.environ.get("PCORE_TEST_DRIVER", "playwright"), "browser": "bundled",
                                 "notify": False, "min_gap_s": 0, "max_gap_s": 0, "browser_watch_s": 0.5,
                                 "profile": str(tmp_path / "profile")}, home=tmp_path)  # fmt: skip
        try:
            await b.complete("auto", {"messages": [{"role": "user", "content": "hi"}]})
            cdp = await b._ctx.new_cdp_session(b._page)
            win = await cdp.send("Browser.getWindowForTarget")
            parked = win["bounds"]
            # what macOS does to it after a display change: a little more of it on the display (100 px, briefly)
            await cdp.send("Browser.setWindowBounds", {"windowId": win["windowId"],
                                                       "bounds": {"left": parked["left"] - 60, "top": parked["top"] - 60}})  # fmt: skip
            moved = (await cdp.send("Browser.getWindowBounds", {"windowId": win["windowId"]}))["bounds"]
            await asyncio.sleep(1.5)
            back = (await cdp.send("Browser.getWindowBounds", {"windowId": win["windowId"]}))["bounds"]
            await cdp.detach()
            return parked, moved, back, "--hide-crash-restore-bubble" in " ".join(b._proc.args)
        finally:
            await b.release()

    parked, moved, back, flag = asyncio.run(go())
    assert moved["left"] < parked["left"] and (back["left"], back["top"]) == (parked["left"], parked["top"])
    assert flag and not running_on(tmp_path / "profile")
