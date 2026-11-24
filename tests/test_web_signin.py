"""A saved sign-in and automatic sign-in (web_signin.py, credstore.py), and the account's plan with the paid-plan
confirmation: the rules without a browser, then the real routine against the mock site's sign-in form
(tests/fixtures/mock_chatgpt.py) in headless Chrome on scratch profiles. The credential store is one in memory (conftest.py):
no test touches the system's. The test password is searched for in everything the run leaves behind."""

import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fixtures.mock_chatgpt import Mock
from test_web_setup import catalog, client_for

from provenance_core.web import chatgpt_web as cw
from provenance_core.web import credstore
from provenance_core.web import web_setup as ws
from provenance_core.web import web_signin as si

SECRET = "Zx9-leakcheck/Pa55 w0rd\"'$(x)"
WHO = "person@example.com"
BODY = {"messages": [{"role": "user", "content": "hi"}]}


def _chrome_ok():
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(cw.CHROME)


browser = pytest.mark.skipif(not _chrome_ok(), reason="needs playwright and Google Chrome")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PCORE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def mock():
    m = Mock()
    m.state["login"] = "cookie"
    m.accounts = {WHO: SECRET}
    yield m
    m.close()


def bridge(tmp_path, url="http://127.0.0.1:9", now=None, **opts):
    async def sleep(_s):
        pass

    return cw.Bridge(
        url,
        {
            "driver": os.environ.get("PCORE_TEST_DRIVER", "playwright"),
            "headless": True,
            "notify": False,
            "min_gap_s": 0,
            "max_gap_s": 0,
            "challenge_wait_s": 1,
            "autologin_step_s": 8,
            "profile": str(tmp_path / "profile"),
            **opts,
        },
        home=tmp_path,
        sleep=sleep,
        **({"clock": lambda: now[0]} if now else {}),
    )


def leaks(root: Path, secret: str = SECRET) -> list[str]:
    """Every file under `root` that holds the secret, as text in either of the encodings a browser profile uses."""
    needles = [secret.encode(), secret.encode("utf-16-le")]
    found = []
    for p in root.rglob("*"):
        if p.is_file():
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if any(n in data for n in needles):
                found.append(str(p.relative_to(root)))
    return found


# ---- the rules, without a browser ----------------------------------------------------------------------------------
def test_off_by_default_and_which_sites(tmp_path, monkeypatch):
    b = bridge(tmp_path)
    v = si.view(b)
    assert v["supported"] and not v["enabled"] and not v["saved"] and v["username"] is None and v["last_try"] is None
    assert v["attempts_today"] == 0 and si.blocked_by(b) == "No sign-in is saved for this account."
    kicked = []

    async def go():  # found signed out with nothing saved: nothing starts
        b._block(cw.WebUnavailable("not signed in", 1800, "login"))
        kicked.append(b._autologin_task)

    asyncio.run(go())
    assert kicked == [None]
    google = si.view(cw.Bridge("https://gemini.google.com", {"site": "gemini", "notify": False}, home=tmp_path))
    assert google["supported"] is False and "Google" in google["reason"] and not google["enabled"]
    zai = si.view(cw.Bridge("https://chat.z.ai", {"site": "zai", "notify": False}, home=tmp_path))
    assert zai["supported"] is False and "human verification" in zai["reason"]
    assert si.view(cw.Bridge("https://chat.qwen.ai", {"site": "qwen", "notify": False}, home=tmp_path))["supported"]
    assert "signin" not in cw.SITES["gemini"] and "signin" not in cw.SITES["zai"]
    with pytest.raises(si.Refused, match="Google"):  # and it cannot be turned on for one
        si.save_signin(cw.Bridge("https://gemini.google.com", {"site": "gemini"}, home=tmp_path), True, WHO, SECRET)
    # a system with no credential store this app can use: said so, and nothing can be saved
    credstore.use(None)
    monkeypatch.setattr(credstore.sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "keyring", None)
    assert si.view(b)["supported"] is False and "password store" in si.view(b)["reason"]
    with pytest.raises(si.Refused, match="password store"):
        si.save_signin(b, True, WHO, SECRET)


def test_api_is_write_only_and_needs_agreement(tmp_path, home, _no_real_credential_store):
    store = _no_real_credential_store
    router, app, setup = client_for(catalog(tmp_path))
    base = "/web/accounts/chatgpt-web/b"
    with TestClient(app) as c:
        first = c.get("/web/accounts").json()
        for p in first["providers"]:
            for a in p["accounts"]:
                assert a["plan"] is None and a["plan_label"] is None and a["needs_confirmation"] is None
                assert set(a["autologin"]) >= {"supported", "reason", "enabled", "saved", "username",
                                               "attempts_today", "last_try"}  # fmt: skip
                assert a["autologin"]["supported"] and not a["autologin"]["enabled"] and not a["autologin"]["saved"]
        no = c.put(base + "/autologin", json={"username": WHO, "password": SECRET})
        no2 = c.put(base + "/autologin", json={"agree": "yes", "username": WHO, "password": SECRET})
        assert no.status_code == 400 and no2.status_code == 400 and "agree" in no.json()["detail"]
        assert not store.items and c.get("/web/accounts").json() == first
        assert c.put(base + "/autologin", json={"agree": True, "username": WHO}).status_code == 400
        assert c.put(base + "/autologin", json={"agree": True, "password": SECRET}).status_code == 400
        assert c.patch(base + "/autologin", json={"enabled": True}).status_code == 409  # nothing saved to turn on
        r = c.put(base + "/autologin", json={"agree": True, "username": WHO, "password": SECRET})
        assert r.status_code == 200 and r.json()["id"] == "b" and r.json()["provider"] == "chatgpt-web"
        al = r.json()["autologin"]
        assert al["enabled"] and al["saved"] and al["username"] == WHO and al["attempts_today"] == 0
        assert list(store.items.values()) == [SECRET]
        seen = [r.text, c.get("/web/accounts").text]
        off = c.patch(base + "/autologin", json={"enabled": False})
        assert off.status_code == 200 and not off.json()["autologin"]["enabled"] and off.json()["autologin"]["saved"]
        assert c.patch(base + "/autologin", json={"enabled": "no"}).status_code == 400
        on = c.patch(base + "/autologin", json={"enabled": True})
        assert on.json()["autologin"]["enabled"]
        main = [a for a in c.get("/web/accounts").json()["providers"][0]["accounts"] if a["id"] == "main"][0]
        assert not main["autologin"]["saved"]  # per account
        seen += [off.text, on.text, c.get("/chatgpt-web/status").text]
        assert c.put("/web/accounts/nope/b/autologin", json={"agree": True}).status_code == 404
        assert c.put("/web/accounts/chatgpt-web/zz/autologin", json={"agree": True}).status_code == 404
        assert c.post(base + "/confirm", json={"confirm": "paid_plan"}).status_code == 409  # nothing to confirm
        assert c.post(base + "/confirm", json={}).status_code == 400
        gone = c.delete(base + "/autologin")
        assert gone.status_code == 200 and not gone.json()["autologin"]["saved"]
        assert not gone.json()["autologin"]["enabled"] and gone.json()["autologin"]["username"] is None
        assert not store.items and c.get("/web/accounts").json() == first
        assert c.post(base + "/autologin/try").status_code == 409
        c.put(base + "/autologin", json={"agree": True, "username": WHO, "password": SECRET})
        assert store.items and c.delete("/web/accounts/b").status_code == 200  # removing the account forgets it too
        assert not store.items
    assert not any(SECRET in t for t in seen) and not leaks(tmp_path)
    (tmp_path / "planted.bin").write_bytes(b"x" + SECRET.encode("utf-16-le"))  # the search itself finds one
    assert leaks(tmp_path) == ["planted.bin"]
    assert WHO not in (tmp_path / "chatgpt_web-b.json").read_text()  # forgotten: the name too


def test_keychain_adapter_keeps_the_secret_off_the_command_line(monkeypatch):
    calls, kept = [], {}

    class Done:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(cmd, input=None, **kw):  # noqa: A002
        calls.append((cmd, input, kw.get("env")))
        if cmd[1] == "-i":
            assert input.startswith("add-generic-password -U -s ")
            kept["v"] = input.strip().rsplit(" ", 1)[1]
            return Done()
        if cmd[1] == "find-generic-password":
            return Done(0, kept["v"] + "\n") if "v" in kept else Done(44)
        kept.pop("v", None)
        return Done()

    monkeypatch.setattr(credstore.subprocess, "run", fake_run)
    s = credstore.MacKeychainStore("test service")
    s.set("chatgpt/b@1234", SECRET)
    assert s.get("chatgpt/b@1234") == SECRET
    s.delete("chatgpt/b@1234")
    assert s.get("chatgpt/b@1234") is None
    for cmd, stdin, env in calls:
        assert not any(SECRET in part for part in cmd) and env is None  # never in argv, never in the environment
        assert SECRET not in (stdin or "")  # and what goes down stdin cannot be read as a command
    assert sum(1 for cmd, stdin, _ in calls if stdin) == 1
    with pytest.raises(credstore.CredentialError) as e:
        s.set('x"; delete-keychain', SECRET)
    assert SECRET not in str(e.value)


def test_runs_only_when_really_signed_out_and_within_limits(tmp_path, monkeypatch):
    now = [time.mktime((2026, 11, 23, 9, 0, 0, 0, 0, -1))]
    b = bridge(tmp_path, now=now, doubt_tries=3)
    results, calls = [("error", "The sign-in page did not load.")], []

    async def fake_signin(bridge_, username):
        calls.append((now[0], username))
        return results[0]

    monkeypatch.setattr(si, "_signin", fake_signin)
    out = cw.WebUnavailable("not signed in", 1800, "login")

    async def found_signed_out():
        b._block(cw.WebUnavailable(out.reason, 1800, "login"))
        if b._autologin_task:
            await b._autologin_task
            b._autologin_task = None

    async def go():
        si.save_signin(b, True, WHO, SECRET)
        b.save(signed_in=True)  # an account that worked: the first sightings of a sign-in page are doubted
        await found_signed_out()
        await found_signed_out()
        assert calls == [] and b.status()["kind"] == "page"
        await found_signed_out()  # the third in a row is believed: now, and only now, one attempt
        assert len(calls) == 1 and calls[0][1] == WHO and b.status()["kind"] == "login"
        await found_signed_out()  # at once again: not before the gap
        assert len(calls) == 1 and "about 15 minutes" in si.blocked_by(b)
        with pytest.raises(si.Refused, match="15 minutes"):  # "try now" keeps to the same rules
            await si.attempt(b, "manual")
        now[0] += 15 * 60 + 1
        await found_signed_out()
        assert len(calls) == 2
        now[0] += 15 * 60 + 1  # the gap has doubled
        await found_signed_out()
        assert len(calls) == 2 and "about 15 minutes" in si.blocked_by(b)
        now[0] += 15 * 60
        await found_signed_out()
        assert len(calls) == 3
        now[0] += 6 * 3600  # three today: no more, however long it waits
        await found_signed_out()
        assert len(calls) == 3 and "3 times today" in si.blocked_by(b)
        with pytest.raises(si.Refused, match="3 times today"):
            await si.attempt(b, "manual")
        v = si.view(b)
        assert v["attempts_today"] == 3 and v["last_try"]["result"] == "error" and v["enabled"]
        assert "3 times today" in ws.plain_reason(b.status(), b)  # and the person is told
        now[0] += 24 * 3600  # the next day it may try again
        assert si.view(b)["attempts_today"] == 0
        results[0] = ("needs_person", si.STOPS["code"])
        await found_signed_out()
        assert len(calls) == 4 and si.view(b)["paused"] == "needs_person"
        now[0] += 3600  # it asked for a person: nothing automatic until one has been
        await found_signed_out()
        assert len(calls) == 4 and "verification code" in ws.plain_reason(b.status(), b)
        b._seen_signed_in()  # the person signed in (the sign-in window); later the site signs the account out again
        b.save(signed_in=False)
        assert si.view(b)["paused"] is None
        results[0] = ("wrong_password", "nope")
        await found_signed_out()
        assert len(calls) == 5
        v = si.view(b)
        assert not v["enabled"] and v["saved"] and v["paused"] == "wrong_password"
        now[0] += 24 * 3600
        await found_signed_out()
        assert len(calls) == 5  # off until a new password is saved
        with pytest.raises(si.Refused, match="Save a new password"):
            si.set_enabled(b, True)
        with pytest.raises(si.Refused, match="Save a new password"):
            await si.attempt(b, "manual")
        si.save_signin(b, True, WHO, SECRET + "2")
        results[0] = ("ok", None)
        await found_signed_out()
        assert len(calls) == 6 and si.view(b)["enabled"] and si.view(b)["last_try"]["result"] == "ok"
        b.signin = "chatgpt-web"  # a sign-in window is open on the account: never at the same time
        now[0] += 24 * 3600
        await found_signed_out()
        assert len(calls) == 6 and "window is open" in si.blocked_by(b)

    asyncio.run(go())
    lines = [json.loads(x) for x in (tmp_path / "web-events.jsonl").read_text().splitlines()]
    tries = [x for x in lines if x["kind"] == "autologin"]
    assert [t["result"] for t in tries] == ["error"] * 3 + ["needs_person", "wrong_password", "ok"]
    assert all(t["trigger"] == "auto" and t["at"] and t["account"] == "main" for t in tries)
    assert not leaks(tmp_path) and not leaks(tmp_path, SECRET + "2")


def test_plan_and_the_paid_plan_confirmation(tmp_path):
    b = bridge(tmp_path)
    b.save(signed_in=True, identity=WHO)
    assert b.status()["plan"] is None and b.needs_confirmation() is None
    b._note_plan("free")
    assert (b.status()["plan"], b.status()["plan_label"], b.needs_confirmation()) == ("free", "free", None)
    assert b.capacity()["available"]
    b._note_plan("plus")
    s = b.status()
    assert (s["plan"], s["plan_label"], s["needs_confirmation"]) == ("paid", "plus", "paid_plan")
    assert not b.capacity()["available"] and not s["blocked"]  # connected, idle
    with pytest.raises(cw.WebSkip, match="paid plan"):
        b._precheck("auto")
    said = ws.plain_reason({**s, "kind": "paid_plan"}, b)
    for part in ("paid plan (plus)", "still spends that account's own usage", "faster than Codex", "use it anyway",
                 "connect a different account"):  # fmt: skip
        assert part in said, said
    assert b.confirm_plan() and b.needs_confirmation() is None and b.capacity()["available"]
    b._precheck("auto")
    b._note_plan(None)  # a page that does not show it: what was known stays
    assert b.status()["plan"] == "paid" and b.needs_confirmation() is None
    b._note_plan("team")  # still paid, already agreed to
    assert b.status()["plan_label"] == "team" and b.needs_confirmation() is None
    b._note_plan("free")
    b._note_plan("pro")  # free, later paid: asked again
    assert b.needs_confirmation() == "paid_plan"
    b.confirm_plan()
    b.save(identity="other@example.com")  # another account signed in on this slot: asked again
    b._note_plan("plus")
    assert b.needs_confirmation() == "paid_plan"
    b.save(identity="third@example.com")
    b._note_plan(None)  # never read for this one: unknown, and that does not hold anything back
    assert b.status()["plan"] == "unknown" and b.needs_confirmation() is None and b.capacity()["available"]
    assert not b.confirm_plan()
    g = cw.Bridge("https://gemini.google.com", {"site": "gemini", "notify": False}, home=tmp_path / "g")
    g.save(plan="paid", plan_label="pro")  # only ChatGPT is checked
    assert g.status()["plan"] is None and g.needs_confirmation() is None and g.capacity()["available"]


# ---- the routine, against the mock site ------------------------------------------------------------------------------
async def signed_out_for_sure(b, now):
    """An account that worked, then the site signs it out: requests until the bridge believes it (doubt_tries)."""
    kinds = []
    for _ in range(int(b.o["doubt_tries"])):
        with pytest.raises(cw.WebUnavailable) as e:
            await b.complete("auto", BODY)
        kinds.append(e.value.kind)
        now[0] += 200
    return kinds


@browser
def test_signed_out_account_is_revived_and_serves_the_next_request(mock, tmp_path, capfd):
    now = [time.time()]
    b = bridge(tmp_path, mock.url, now=now, doubt_tries=2)
    revived = []
    b.on_revived = lambda: revived.append(True)

    async def go():
        try:
            si.save_signin(b, True, WHO, SECRET)
            b.save(signed_in=True, temp_ok=True)
            with pytest.raises(cw.WebUnavailable) as e:
                await b.complete("auto", BODY)
            assert e.value.kind == "page" and b._autologin_task is None and mock.signin_pages == 0  # first sighting
            now[0] += 200
            with pytest.raises(cw.WebUnavailable) as e:
                await b.complete("auto", BODY)
            assert e.value.kind == "login" and b._autologin_task is not None
            await b._autologin_task
            st = b.status()
            data = await b.complete("auto", BODY)
            pages = len(b._ctx.pages)
            return st, data, pages
        finally:
            await b.release()

    st, data, pages = asyncio.run(go())
    v = si.view(b)
    assert v["last_try"]["result"] == "ok" and v["attempts_today"] == 1 and v["enabled"] and v["paused"] is None
    assert st["signed_in"] is True and not st["blocked"] and st["email"] == WHO and st["plan"] == "free"
    assert data["choices"][0]["message"]["content"] == "ok" and revived == [True] and pages == 1  # its tab is closed
    assert mock.posted == [("email", {"email": WHO}), ("password", {"password": SECRET})] and mock.signins == 1
    assert set(mock.touched) == {("login", "username"), ("password", "password")}
    ev = [json.loads(x) for x in (tmp_path / "web-events.jsonl").read_text().splitlines()]
    ev = [x for x in ev if not x["kind"].startswith(("browser_", "cookie"))]  # the cookie guard's own lines
    assert [(x["kind"], x.get("result")) for x in ev] == [("login", None), ("autologin", "ok")]
    out = capfd.readouterr()
    assert SECRET not in out.out + out.err and not leaks(tmp_path)


@browser
def test_hard_stops_wrong_password_and_slow_pages(mock, tmp_path, capfd):
    now = [time.time()]
    b = bridge(tmp_path, mock.url, now=now, doubt_tries=1, autologin_max_per_day=50, autologin_gap_s=0)
    got, texts = {}, []

    async def one(trouble, password=SECRET, **state):
        mock.state.update({"signin_trouble": trouble, "signin_slow_ms": 0, **state})
        mock.touched.clear()
        mock.posted.clear()
        si.save_signin(b, True, WHO, password)
        try:
            last = await si.attempt(b, "manual")
        except Exception as e:  # noqa: BLE001
            texts.append(repr(e) + str(e))
            raise
        texts.append(json.dumps([last, si.view(b), b.status()]))
        return last, list(mock.touched), [p[0] for p in mock.posted], si.view(b)

    async def go():
        try:
            b.save(signed_in=False)
            for t in ("captcha", "passkey", "sso", "code", "terms", "suspicious", "odd"):
                got[t] = await one(t)
            # a stop holds automatic sign-in back (a person is needed), and a request finding the account signed
            # out starts nothing
            with pytest.raises(cw.WebUnavailable):
                await b.complete("auto", BODY)
            got["held"] = (b._autologin_task, si.blocked_by(b))
            got["wrong"] = await one("", password="not-the-password")
            with pytest.raises(si.Refused) as e:
                await si.attempt(b, "manual")
            got["after_wrong"] = str(e.value)
            got["site_says_wrong"] = await one("wrong_password")
            b.o["autologin_step_s"] = 2
            got["too_slow"] = await one("", signin_slow_ms=6000)
            b.o["autologin_step_s"] = 8
            got["slow"] = await one("", signin_slow_ms=2500)
            # the bridge's debug dump keeps nothing of a page that holds a typed password, or of a sign-in page
            page = await b._ensure_page()
            await page.goto(mock.url + "/auth/password")
            await page.locator('input[type="password"]').fill(SECRET)
            await b._dump(page, {}, "test")
            await page.goto(mock.url + "/?temporary-chat=true")
            await page.evaluate("s => { const i = document.createElement('input'); i.type = 'password'; "
                                "document.body.appendChild(i); i.value = s; i.setAttribute('value', s); }", SECRET)  # fmt: skip
            await b._dump(page, {}, "test")
            got["dumps"] = (
                list((tmp_path / "chatgpt-web-debug").glob("*")) if (tmp_path / "chatgpt-web-debug").exists() else []
            )
            await page.evaluate("document.querySelector('input[type=password]').remove()")
            await b._dump(page, {}, "test")  # an ordinary page is still kept
            got["dumps_after"] = len(list((tmp_path / "chatgpt-web-debug").glob("*")))
        finally:
            await b.release()

    asyncio.run(go())
    for t, detail, touched, posted in (
        ("captcha", "human check", [], []),  # beside the form: nothing is typed at all
        ("passkey", "passkey", [], []),
        ("sso", "another sign-in provider", [("login", "username")], ["email"]),  # nothing typed on the other host
        ("code", "verification code", [("login", "username"), ("password", "password")], ["email", "password"]),
        ("terms", "terms", [("login", "username"), ("password", "password")], ["email", "password"]),
        ("suspicious", "security notice", [("login", "username"), ("password", "password")], ["email", "password"]),
        ("odd", "did not expect", [("login", "username"), ("password", "password")], ["email", "password"]),
    ):
        last, was_touched, was_posted, v = got[t]
        assert last["result"] == "needs_person" and detail in last["detail"], (t, last)
        assert was_touched == touched and was_posted == posted, (t, was_touched, was_posted)
        assert v["paused"] == "needs_person" and v["enabled"] and v["last_try"] == last
    assert got["held"][0] is None and "waits for you" in got["held"][1]
    for k in ("wrong", "site_says_wrong"):
        last, _, was_posted, v = got[k]
        assert last["result"] == "wrong_password" and was_posted == ["email", "password"]  # one try, no second
        assert not v["enabled"] and v["saved"] and v["paused"] == "wrong_password"
    assert "Save a new password" in got["after_wrong"]
    last, was_touched, was_posted, v = got["too_slow"]
    assert last["result"] == "error" and "in time" in last["detail"] and not was_touched and v["paused"] is None
    last, _, was_posted, v = got["slow"]
    assert last["result"] == "ok" and was_posted == ["email", "password"] and v["enabled"]
    assert got["dumps"] == [] and got["dumps_after"] == 1
    assert mock.signins == 1 and si.view(b)["attempts_today"] == 11
    out = capfd.readouterr()
    everything = out.out + out.err + "".join(texts) + (tmp_path / "web-events.jsonl").read_text()
    assert SECRET not in everything and "not-the-password" not in everything
    assert not leaks(tmp_path) and not leaks(tmp_path, "not-the-password")


@browser
def test_a_form_with_both_fields_behind_a_switch(mock, tmp_path):
    """Qwen's shape: the page opens on a code sign-in; "Log in with a password" shows one form with both fields."""
    signin = {**cw.SITES["qwen"]["signin"], "url": "/auth/single"}
    b = bridge(tmp_path, mock.url, signin=signin)

    async def go():
        try:
            b.save(signed_in=False)
            si.save_signin(b, True, WHO, SECRET)
            last = await si.attempt(b, "manual")
            with pytest.raises(si.Refused, match="signed in"):  # nothing to do now
                await si.attempt(b, "manual")
            return last, (await b.complete("auto", BODY))["choices"][0]["message"]["content"]
        finally:
            await b.release()

    last, reply = asyncio.run(go())
    assert last["result"] == "ok" and reply == "ok"
    assert mock.posted == [("email", {"email": WHO}), ("password", {"password": SECRET})]
    assert set(mock.touched) == {("single", "email"), ("single", "password")} and not leaks(tmp_path)


@browser
def test_try_now_and_the_paid_plan_through_the_api(mock, tmp_path, home):
    router, app, setup = client_for(catalog(tmp_path, mock.url, accounts=("b",)))
    base = "/web/accounts/chatgpt-web/b"
    chat = {"model": "chatgpt-web/luna", "messages": BODY["messages"]}
    texts = []

    def acct(c):
        r = c.get("/web/accounts")
        texts.append(r.text)
        return r.json()["providers"][0]["accounts"][0]

    with TestClient(app) as c:
        try:
            st = c.get("/chatgpt-web/status", params={"probe": 1, "account": "b"}).json()
            assert st["accounts"]["b"]["signed_in"] is False
            c.put(base + "/autologin", json={"agree": True, "username": WHO, "password": SECRET})
            before = acct(c)
            mock.state["plan"] = None  # the page does not show the plan
            r = c.post(base + "/autologin/try")
            texts.append(r.text)
            after = acct(c)
            unknown = c.post("/v1/chat/completions", json=chat)
            again = c.post(base + "/autologin/try")
            mock.state["plan"] = "plus"
            c.get("/chatgpt-web/status", params={"probe": 1, "account": "b"})
            paid = acct(c)
            sent = len(mock.sent)
            idle = c.post("/v1/chat/completions", json=chat)
            idle_sent = len(mock.sent) - sent
            ok = c.post(base + "/confirm", json={"confirm": "paid_plan"})
            served = c.post("/v1/chat/completions", json=chat)
            plans = {}
            for plan in ("team", None, "free"):
                mock.state["plan"] = plan
                c.get("/chatgpt-web/status", params={"probe": 1, "account": "b"})
                a = acct(c)
                plans[plan] = (a["plan"], a["plan_label"], a["needs_confirmation"])
            mock.state["plan"] = "plus"  # free, and later paid again: asked again
            c.get("/chatgpt-web/status", params={"probe": 1, "account": "b"})
            asked_again = acct(c)["needs_confirmation"]
            texts.append(c.get("/chatgpt-web/status").text)
        finally:
            c.post("/chatgpt-web/release")
    assert "Automatic sign-in is on" in before["reason"] and before["signed_in"] is False
    assert r.status_code == 200 and r.json()["try"]["result"] == "ok" and r.json()["id"] == "b"
    assert after["signed_in"] is True and after["email"] == WHO and not after["blocked"]
    assert after["autologin"]["last_try"]["result"] == "ok" and after["autologin"]["attempts_today"] == 1
    # a plan that cannot be read holds nothing back, and the status says so
    assert (after["plan"], after["plan_label"], after["needs_confirmation"]) == ("unknown", None, None)
    assert "could not be read" in after["plan_note"] and after["reason"] is None
    assert unknown.status_code == 200 and unknown.json()["choices"][0]["message"]["content"] == "ok"
    assert again.status_code == 409 and "signed in" in again.json()["detail"]
    assert (paid["plan"], paid["plan_label"], paid["needs_confirmation"]) == ("paid", "plus", "paid_plan")
    assert "paid plan (plus)" in paid["reason"] and "faster than Codex" in paid["reason"] and not paid["blocked"]
    assert paid["signed_in"] is True and paid["plan_note"] is None
    assert idle.status_code != 200 and idle_sent == 0  # connected, but nothing is sent on it
    assert ok.status_code == 200 and ok.json()["needs_confirmation"] is None and ok.json()["reason"] is None
    assert served.status_code == 200 and served.json()["choices"][0]["message"]["content"] == "ok"
    assert plans["team"] == ("paid", "team", None)  # still paid: already agreed to
    assert plans[None] == ("paid", "team", None)  # not shown this time: what was known stays
    assert plans["free"] == ("free", "free", None)
    assert asked_again == "paid_plan"
    assert not any(SECRET in t for t in texts) and not leaks(tmp_path)


# ---- a pool provider's key page, opened in a signed-in profile (POST /web/open) ---------------------------------------
def key_help(monkeypatch, url):
    from provenance_core.keys import pool_help

    data = {
        "checked": "2026-11-23",
        "providers": [
            {"id": "google", "name": "G", "keys_url": url + "/", "signup_url": url + "/auth/login"},
            {"id": "other", "name": "O", "keys_url": "https://example.com/keys", "signup_url": "https://example.com"},
        ],
    }
    monkeypatch.setattr(pool_help, "load", lambda: data)
    return data


def test_only_key_pages_of_the_signed_in_site_can_be_opened(tmp_path, home, monkeypatch):
    url = "http://127.0.0.1:9"
    data = key_help(monkeypatch, url)
    router, app, setup = client_for(catalog(tmp_path, url, opens=["127.0.0.1"]))
    ask = {"provider": "chatgpt-web", "account": "b"}
    with TestClient(app) as c:
        got = c.get("/pool/help").json()
        for p in got["providers"]:  # each entry also carries how its steps have gone so far (pool_help.experience)
            assert p.pop("experience")["tries"] == 0
        assert got == data  # nobody signed in: no open_with anywhere
        anywhere = c.post("/web/open", json={**ask, "url": "https://evil.example/steal"})
        near = c.post("/web/open", json={**ask, "url": url + "/other"})  # the right site, not a listed page
        none = c.post("/web/open", json=ask)
        signed_out = c.post("/web/open", json={**ask, "url": url + "/"})
        setup.bridge_for("chatgpt-web", "b").save(signed_in=True)
        wrong_site = c.post("/web/open", json={**ask, "url": "https://example.com/keys"})
        all_ = c.get("/pool/help").json()["providers"]
        one = c.get("/pool/help", params={"provider": "google"}).json()["provider"]
        unknown = c.post("/web/open", json={"provider": "nope", "account": "b", "url": url + "/"})
        idle = c.get("/web/open", params=ask).json()
    for r in (anywhere, near, none):
        assert r.status_code == 400 and "key page" in r.json()["detail"]
    assert signed_out.status_code == 409 and "not signed in" in signed_out.json()["detail"]
    assert wrong_site.status_code == 409 and "does not use" in wrong_site.json()["detail"]
    # the two test providers keep one state per account, so both list b; main is not signed in
    who = [{"provider": "chatgpt-web", "account": "b"}, {"provider": "other-web", "account": "b"}]
    assert all_[0]["open_with"] == who and one["open_with"] == who and "open_with" not in all_[1]
    assert unknown.status_code == 404 and idle["state"] == "idle" and not setup.opens
    assert cw.SITES["gemini"]["opens"] == ["google.com"] and "opens" not in cw.CHATGPT_SITE
    real = cw.Bridge("https://gemini.google.com", {"site": "gemini"}, home=tmp_path)
    assert setup._covers(real, "https://aistudio.google.com/api-keys")
    assert not setup._covers(real, "https://console.groq.com/keys")
    assert not setup._covers(real, "https://google.com.evil.example/api-keys")


@browser
def test_key_page_window_opens_is_left_alone_and_closes(mock, tmp_path, home, monkeypatch):
    import signal

    key_help(monkeypatch, mock.url)
    router, app, setup = client_for(catalog(tmp_path, mock.url, opens=["127.0.0.1"]))
    ask = {"provider": "chatgpt-web", "account": "b"}
    with TestClient(app) as c:
        try:
            b = setup.bridge_for("chatgpt-web", "b")
            b.save(signed_in=True)
            r = c.post("/web/open", json={**ask, "url": mock.url + "/"})
            st = c.get("/web/open", params=ask).json()
            flow = setup.opens[("chatgpt-web", "b")]
            pid = flow.window.pid
            during = (b.signing_in(), setup.bridge_for("other-web", "b").signing_in(), b.capacity()["available"])
            second = c.post("/web/open", json={"provider": "other-web", "account": "b", "url": mock.url + "/"})
            signin = c.post("/web/login", json=ask)
            login_view = c.get("/web/login", params=ask).json()["state"]
            cancelled = c.post("/web/open/cancel", json=ask).json()
            gone = os.system(f"kill -0 {pid} 2>/dev/null") != 0
            after = (b.signing_in(), c.get("/web/open", params=ask).json()["state"])
            # again; this time the person closes the window
            assert c.post("/web/open", json={**ask, "url": mock.url + "/"}).json()["state"] == "open"
            os.kill(setup.opens[("chatgpt-web", "b")].window.pid, signal.SIGTERM)
            end = time.time() + 30
            while c.get("/web/open", params=ask).json()["state"] == "open" and time.time() < end:
                time.sleep(0.2)
            closed = c.get("/web/open", params=ask).json()
            free = not b.signing_in()
        finally:
            c.post("/web/open/cancel", json=ask)
            c.post("/chatgpt-web/release")
    assert r.status_code == 200 and r.json()["state"] == "open" and r.json()["url"] == mock.url + "/"
    assert st["state"] == "open" and "--remote-debugging" not in " ".join(flow.window.args())  # nothing drives it
    assert during == (True, True, False)  # the account's bridges keep out of the profile meanwhile
    assert second.status_code == 409 and signin.status_code == 409 and "already open" in signin.json()["detail"]
    assert login_view == "idle"  # it is not a sign-in
    assert cancelled["state"] == "idle" and gone and after == (False, "idle")
    assert closed["state"] == "closed" and closed["reason"] is None and free
    assert not mock.sent and not mock.touched and not mock.posted  # the page was only shown


# ---- remembered login methods, SSO auto-relogin, and automatic requeuing ---------------------------------------------
def _write_history(profile: Path, visits: list[tuple[str, float]]):
    """Create a Chrome-shaped Default/History SQLite DB with (url, unix_ts) rows."""
    db = profile / "Default" / "History"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT NOT NULL, last_visit_time INTEGER NOT NULL)")
    con.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER NOT NULL, visit_time INTEGER NOT NULL)")
    for idx, (u, ts) in enumerate(visits, 1):
        ct = si.chrome_time_us(ts)
        con.execute("INSERT INTO urls (id, url, last_visit_time) VALUES (?, ?, ?)", (idx, u, ct))
        con.execute("INSERT INTO visits (id, url, visit_time) VALUES (?, ?, ?)", (idx, idx, ct))
    con.commit()
    con.close()


def test_detect_login_method_from_history_and_fallbacks(tmp_path, _no_real_credential_store):
    store = _no_real_credential_store
    t0 = 1_800_000_000.0
    tok = "SUPER-SECRET-OAUTH-TOKEN-999"
    b = bridge(tmp_path, "https://chatgpt.com")
    prof = b.profile

    # 1. Google IdP (with secret token in query — must never leak)
    _write_history(
        prof,
        [
            ("https://chatgpt.com/auth/login", t0 + 1),
            ("https://auth.openai.com/log-in", t0 + 2),
            (f"https://accounts.google.com/o/oauth2/v2/auth?code={tok}&state=xyz", t0 + 3),
            ("https://chatgpt.com/", t0 + 5),
        ],
    )
    assert si.detect_login_method(prof, b, t0) == ("google", None)

    # 2. Microsoft IdP
    _write_history(
        prof,
        [
            ("https://chatgpt.com/auth/login", t0 + 1),
            (f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize?token={tok}", t0 + 2),
        ],
    )
    assert si.detect_login_method(prof, b, t0) == ("microsoft", None)
    _write_history(prof, [("https://login.live.com/oauth20_authorize.srf", t0 + 2)])
    assert si.detect_login_method(prof, b, t0) == ("microsoft", None)

    # 3. Apple IdP
    _write_history(prof, [("https://appleid.apple.com/auth/authorize?state=abc", t0 + 2)])
    assert si.detect_login_method(prof, b, t0) == ("apple", None)

    # 4. GitHub auth vs non-auth repo visit
    _write_history(prof, [(f"https://github.com/login/oauth/authorize?code={tok}", t0 + 2)])
    assert si.detect_login_method(prof, b, t0) == ("github", None)
    _write_history(
        prof,
        [
            ("https://chatgpt.com/auth/login", t0 + 1),
            ("https://auth.openai.com/log-in/password", t0 + 2),
            ("https://github.com/octocat/hello-world", t0 + 3),
        ],
    )
    assert si.detect_login_method(prof, b, t0) == ("password", None)

    # 5. Other external SSO (hostname ONLY; path/query/tokens discarded)
    _write_history(
        prof,
        [
            ("https://chatgpt.com/auth/login", t0 + 1),
            (f"https://acme.okta.com/app/openai/sso/saml?SAMLRequest={tok}", t0 + 2),
        ],
    )
    assert si.detect_login_method(prof, b, t0) == ("other", "acme.okta.com")

    # 6. Normal password login with Cloudflare challenge in between (Cloudflare ignored)
    _write_history(
        prof,
        [
            ("https://chatgpt.com/auth/login", t0 + 1),
            ("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g", t0 + 2),
            ("https://auth.openai.com/log-in/password", t0 + 3),
            ("https://chatgpt.com/", t0 + 4),
        ],
    )
    assert si.detect_login_method(prof, b, t0) == ("password", None)

    # 7. Old visit before window_started_at - 5s is ignored
    _write_history(
        prof,
        [
            ("https://accounts.google.com/o/oauth2/auth", t0 - 3600),
            ("https://auth.openai.com/log-in", t0 + 2),
        ],
    )
    assert si.detect_login_method(prof, b, t0) == ("password", None)

    # 8. Missing / corrupt History fallbacks
    (prof / "Default" / "History").write_bytes(b"not-a-sqlite-db")
    assert si.detect_login_method(prof, b, t0) == (None, None)
    assert si.detect_login_method(prof, b, t0, google_cookies_changed=True) == ("google", None)
    assert si.detect_login_method(prof, b, t0, hint="microsoft") == ("microsoft", None)
    gem = cw.Bridge("https://gemini.google.com", {"site": "gemini", "notify": False}, home=tmp_path / "gem")
    assert si.detect_login_method(prof, gem, t0) == ("google", None)

    # 9. Persisting method rules + never storing passwords/tokens silently
    v_other = si.note_login_method(b, "other", "acme.okta.com")
    assert v_other["method"] == "other" and v_other["method_label"] == "acme.okta.com"
    assert not v_other["enabled"] and not v_other["needs_credentials"] and not store.items
    assert tok not in (tmp_path / "chatgpt_web.json").read_text()

    # Password remembered without consent: needs_credentials=True, enabled=False, nothing auto-runs
    b_pw = bridge(tmp_path / "pw", "https://chatgpt.com", doubt_tries=1)
    v_pw = si.note_login_method(b_pw, "password")
    assert v_pw["method"] == "password" and v_pw["method_label"] is None
    assert v_pw["needs_credentials"] is True and v_pw["enabled"] is False and v_pw["saved"] is False
    assert not store.items

    async def check_no_auto():
        b_pw._block(cw.WebUnavailable("not signed in", 1800, "login"))
        assert b_pw._autologin_task is None and not si.view(b_pw)["queued"]

    asyncio.run(check_no_auto())

    # Google remembered on ChatGPT: auto-enables SSO without storing a password; respects user_disabled
    b_sso = bridge(tmp_path / "sso", "https://chatgpt.com")
    v_sso = si.note_login_method(b_sso, "google")
    assert v_sso["method"] == "google" and v_sso["enabled"] is True and v_sso["saved"] is False
    assert v_sso["needs_credentials"] is False and not store.items
    v_off = si.set_enabled(b_sso, False)
    assert v_off["enabled"] is False and v_off["method"] == "google"
    v_again = si.note_login_method(b_sso, "google")
    assert v_again["enabled"] is False  # respected user_disabled!
    v_on = si.set_enabled(b_sso, True)
    assert v_on["enabled"] is True

    # Gemini remembers method="google" while staying unsupported for autologin
    v_gem = si.note_login_method(gem, "google")
    assert v_gem["supported"] is False and v_gem["enabled"] is False and v_gem["method"] == "google"
    assert v_gem["needs_credentials"] is False


def test_sso_api_opt_in_patch_and_delete(tmp_path, home, monkeypatch, _no_real_credential_store):
    store = _no_real_credential_store
    router, app, setup = client_for(catalog(tmp_path))
    base = "/web/accounts/chatgpt-web/b"
    with TestClient(app) as c:
        # Bad requests / unsupported SSO method
        assert c.put(base + "/autologin", json={"agree": False, "method": "google"}).status_code == 400
        assert c.put(base + "/autologin", json={"agree": True, "method": "bogus"}).status_code == 400
        assert c.put(base + "/autologin", json={"agree": True, "method": "github"}).status_code == 409

        # Opt into Google SSO without password
        r = c.put(base + "/autologin", json={"agree": True, "method": "google"})
        assert r.status_code == 200
        al = r.json()["autologin"]
        assert al["method"] == "google" and al["method_label"] is None
        assert al["enabled"] is True and al["saved"] is False and al["needs_credentials"] is False
        assert not store.items

        # Toggle off and back on via PATCH
        off = c.patch(base + "/autologin", json={"enabled": False})
        assert off.status_code == 200 and off.json()["autologin"]["enabled"] is False
        on = c.patch(base + "/autologin", json={"enabled": True})
        assert on.status_code == 200 and on.json()["autologin"]["enabled"] is True

        # Switch to password then back to SSO: old password deleted from credential store
        pw = c.put(base + "/autologin", json={"agree": True, "username": WHO, "password": SECRET})
        assert pw.status_code == 200 and pw.json()["autologin"]["method"] == "password"
        assert pw.json()["autologin"]["saved"] is True and pw.json()["autologin"]["needs_credentials"] is False
        assert list(store.items.values()) == [SECRET]
        back = c.put(base + "/autologin", json={"agree": True, "method": "microsoft"})
        assert back.status_code == 200 and back.json()["autologin"]["method"] == "microsoft"
        assert back.json()["autologin"]["saved"] is False and back.json()["autologin"]["enabled"] is True
        assert not store.items

        # SSO remains supported even with no system password store, while password save still refuses
        credstore.use(None)
        monkeypatch.setattr(credstore.sys, "platform", "linux")
        monkeypatch.setitem(sys.modules, "keyring", None)
        accts = c.get("/web/accounts").json()
        acct_b = [a for a in accts["providers"][0]["accounts"] if a["id"] == "b"][0]
        assert acct_b["autologin"]["supported"] is True and acct_b["autologin"]["enabled"] is True
        assert c.put(base + "/autologin", json={"agree": True, "username": WHO, "password": SECRET}).status_code == 409

        # Restore in-memory store and DELETE clears everything
        credstore.use(store)
        gone = c.delete(base + "/autologin")
        assert gone.status_code == 200
        gal = gone.json()["autologin"]
        assert gal["method"] is None and gal["enabled"] is False and gal["saved"] is False and gal["queued"] is False
        assert c.patch(base + "/autologin", json={"enabled": True}).status_code == 409


def test_automatic_requeue_lifecycle(tmp_path, monkeypatch):
    # 1. Transient error -> automatic requeue -> succeeds on retry
    b1 = bridge(tmp_path / "r1", doubt_tries=1, autologin_gap_s=0.05, autologin_max_per_day=3)
    seq1 = iter([("error", "temporary blip"), ("ok", None)])
    calls1 = []

    async def fake1(bridge_, username):
        calls1.append(username)
        res = next(seq1)
        if res[0] == "ok":
            bridge_.reset()
            bridge_._seen_signed_in()
        return res

    monkeypatch.setattr(si, "_signin", fake1)

    async def run1():
        si.save_signin(b1, True, WHO, SECRET)
        b1._block(cw.WebUnavailable("not signed in", 1800, "login"))
        assert b1._autologin_task is not None
        await b1._autologin_task
        assert len(calls1) == 1 and si.view(b1)["queued"] is True
        # Wait for requeue timer to fire and retry
        end = time.time() + 3.0
        while (len(calls1) < 2 or si.running(b1) or si.view(b1)["queued"]) and time.time() < end:
            await asyncio.sleep(0.02)
        if b1._autologin_task and not b1._autologin_task.done():
            await b1._autologin_task
        assert len(calls1) == 2 and si.view(b1)["last_try"]["result"] == "ok" and si.view(b1)["queued"] is False
        await b1._shutdown()

    asyncio.run(run1())

    # 2. Bounded by autologin_max_per_day (never loops past cap)
    b2 = bridge(tmp_path / "r2", doubt_tries=1, autologin_gap_s=0.04, autologin_max_per_day=3)
    calls2 = []

    async def fake2(bridge_, username):
        calls2.append(username)
        return "error", "down"

    monkeypatch.setattr(si, "_signin", fake2)

    async def run2():
        si.save_signin(b2, True, WHO, SECRET)
        b2._block(cw.WebUnavailable("not signed in", 1800, "login"))
        end = time.time() + 3.0
        while (len(calls2) < 3 or si.running(b2)) and time.time() < end:
            await asyncio.sleep(0.02)
        if b2._autologin_task and not b2._autologin_task.done():
            await b2._autologin_task
        await asyncio.sleep(0.2)
        assert len(calls2) == 3 and si.view(b2)["attempts_today"] == 3 and si.view(b2)["queued"] is False
        await b2._shutdown()

    asyncio.run(run2())

    # 3. Gap-wait requeue & busy requeue & hard stops never requeue
    b3 = bridge(tmp_path / "r3", doubt_tries=1, autologin_gap_s=0.08, autologin_max_per_day=5)
    calls3 = []
    outcomes3 = [("needs_person", si.STOPS["code"])]

    async def fake3(bridge_, username):
        calls3.append(username)
        return outcomes3[0]

    monkeypatch.setattr(si, "_signin", fake3)

    async def run3():
        si.save_signin(b3, True, WHO, SECRET)
        # Simulate recent try inside the backoff gap -> kick() queues instead of running now
        si._save(b3, tries=[b3.clock()], gap_waived=False)
        si.kick(b3)
        assert b3._autologin_task is None and si.view(b3)["queued"] is True and len(calls3) == 0
        # Wait for gap-wait timer to fire -> runs once, gets needs_person -> stops and clears queue
        end = time.time() + 3.0
        while (len(calls3) < 1 or si.running(b3)) and time.time() < end:
            await asyncio.sleep(0.02)
        if b3._autologin_task and not b3._autologin_task.done():
            await b3._autologin_task
        await asyncio.sleep(0.15)
        assert len(calls3) == 1 and si.view(b3)["paused"] == "needs_person" and si.view(b3)["queued"] is False

        # wrong_password also never requeues
        si.save_signin(b3, True, WHO, SECRET)
        outcomes3[0] = ("wrong_password", "nope")
        b3._block(cw.WebUnavailable("not signed in", 1800, "login"))
        await b3._autologin_task
        assert len(calls3) == 2 and si.view(b3)["paused"] == "wrong_password" and si.view(b3)["queued"] is False

        # Busy/running kick schedules requeue, and cancel paths clear it cleanly
        si.save_signin(b3, True, WHO, SECRET)
        b3._autologin_running = True
        si.kick(b3)
        assert si.view(b3)["queued"] is True
        b3._autologin_running = False
        si.set_enabled(b3, False)
        assert si.view(b3)["queued"] is False

        si.set_enabled(b3, True)
        si._schedule_requeue(b3, 60.0)
        assert si.view(b3)["queued"] is True
        b3._seen_signed_in()  # manual login cancels requeue
        assert si.view(b3)["queued"] is False

        si._schedule_requeue(b3, 60.0)
        assert si.view(b3)["queued"] is True
        si.forget(b3)
        assert si.view(b3)["queued"] is False

        si.save_signin(b3, True, WHO, SECRET)
        si._schedule_requeue(b3, 60.0)
        assert si.view(b3)["queued"] is True
        await b3.release()  # _shutdown cancels cleanly without warnings
        assert si.view(b3)["queued"] is False

    asyncio.run(run3())


@browser
def test_sso_auto_relogin_succeeds_and_obeys_hard_stops(mock, tmp_path, capfd, _no_real_credential_store):
    store = _no_real_credential_store
    port = mock.httpd.server_address[1]
    sso_signin = {
        **cw.CHATGPT_SITE["signin"],
        "sso": {
            "google": {
                "button": cw.CHATGPT_SITE["signin"]["sso"]["google"]["button"],
                "hosts": ["localhost"],
            }
        },
    }
    now = [time.time()]
    b = bridge(
        tmp_path,
        mock.url,
        now=now,
        doubt_tries=1,
        autologin_max_per_day=50,
        autologin_gap_s=0,
        signin=sso_signin,
    )
    revived = []
    b.on_revived = lambda: revived.append(True)
    mock.state["sso_url"] = f"http://localhost:{port}/auth/idp"
    mock.state["sso_return"] = f"{mock.url}/__sso_complete"

    async def clear_cookies():
        if b._ctx is not None:
            await b._ctx.clear_cookies()

    async def go():
        results = {}
        try:
            # 1. Remembered Google SSO auto-relogin with multi-account chooser matching expected email
            si.note_login_method(b, "google")
            b.save(signed_in=True, temp_ok=True, identity=WHO)
            mock.state["sso_mode"] = "chooser"
            with pytest.raises(cw.WebUnavailable) as e:
                await b.complete("auto", BODY)
            assert e.value.kind == "login" and b._autologin_task is not None
            await b._autologin_task
            results["chooser_st"] = b.status()
            results["chooser_reply"] = (await b.complete("auto", BODY))["choices"][0]["message"]["content"]
            results["chooser_chosen"] = list(mock.sso_chosen)

            # 2. Direct SSO redirect (no chooser)
            await clear_cookies()
            b.save(signed_in=False)
            mock.state["sso_mode"] = "direct"
            results["direct"] = await si.attempt(b, "manual")

            # 3. Sole-account chooser with no expected_email
            await clear_cookies()
            b.save(signed_in=False, identity=None, last_identity=None)
            mock.state["sso_mode"] = "chooser_one"
            results["sole"] = await si.attempt(b, "manual")

            # 4. Hard stops on site and IdP (all stop as needs_person without typing anything)
            for name, mode, trouble in (
                ("site_captcha", "direct", "captcha"),
                ("idp_captcha", "idp_captcha", ""),
                ("idp_code", "idp_code", ""),
                ("idp_password", "idp_password", ""),
                ("idp_consent", "idp_consent", ""),
                ("idp_passkey", "idp_passkey", ""),
                ("idp_suspicious", "idp_suspicious", ""),
                ("chooser_ambiguous", "chooser_ambiguous", ""),
            ):
                await clear_cookies()
                b.save(signed_in=False, identity=None, last_identity=WHO)
                mock.state["sso_mode"] = mode
                mock.state["signin_trouble"] = trouble
                results[name] = await si.attempt(b, "manual")

            # 5. Unexpected host (localhost removed from allowed IdP hosts)
            await clear_cookies()
            b.save(signed_in=False)
            mock.state["sso_mode"] = "chooser"
            mock.state["signin_trouble"] = ""
            b.o["signin"] = {
                **sso_signin,
                "sso": {"google": {"button": sso_signin["sso"]["google"]["button"], "hosts": ["accounts.google.com"]}},
            }
            results["unexpected_host"] = await si.attempt(b, "manual")
            return results
        finally:
            await b.release()

    res = asyncio.run(go())
    assert res["chooser_st"]["signed_in"] is True and res["chooser_st"]["email"] == WHO
    assert res["chooser_reply"] == "ok" and res["chooser_chosen"] == [WHO]
    assert revived == [True, True, True]
    assert res["direct"]["result"] == "ok" and res["sole"]["result"] == "ok"
    for key_name, expected_fragment in (
        ("site_captcha", "human check"),
        ("idp_captcha", "human check"),
        ("idp_code", "verification code"),
        ("idp_password", "password or e-mail"),
        ("idp_consent", "terms or a consent"),
        ("idp_passkey", "passkey"),
        ("idp_suspicious", "security notice"),
        ("chooser_ambiguous", "which account"),
        ("unexpected_host", "another sign-in provider"),
    ):
        got = res[key_name]
        assert got["result"] == "needs_person" and expected_fragment in got["detail"], (key_name, got)
    assert not store.items  # no password ever stored for SSO
    assert mock.touched == []  # never typed into any field on site or IdP
    assert si.view(b)["saved"] is False and si.view(b)["method"] == "google" and si.view(b)["queued"] is False


def test_login_flow_detects_method_and_cancels_requeue(tmp_path, home, monkeypatch, _no_real_credential_store):
    store = _no_real_credential_store
    router, app, setup = client_for(catalog(tmp_path))
    setup.poll_s, setup.cookie_poll_s, setup.grace_s = 0.01, 0.01, 0.01

    history_plan: list[tuple[str, float]] = []

    class FakeWindow:
        def __init__(self, app_, profile, url, bounds=None, launch="open", extra_args=()):
            self.profile = Path(profile)
            self.pid = 42424
            self._alive = False

        async def open(self, wait_s=30.0):
            self._alive = True
            if history_plan:
                _write_history(self.profile, list(history_plan))

        def alive(self):
            return False  # closes immediately after open -> triggers verify

        def closed_by_hand(self):
            return False

        async def close(self, timeout=20.0):
            self.pid = None
            self._alive = False

    async def fake_check(self, model="auto", close=False):
        self.reset()
        self._seen_signed_in()
        return {**self.status(), "ok": True, "page_model": None}

    monkeypatch.setattr(ws, "SigninWindow", FakeWindow)
    monkeypatch.setattr(cw.Bridge, "check", fake_check)

    b = setup.bridge_for("chatgpt-web", "b")
    with TestClient(app) as c:
        # Invalid method hint is rejected with 400
        assert c.post("/web/login", json={"provider": "chatgpt-web", "account": "b", "method": "nope"}).status_code == 400

        # 1. Google login detected from History -> method="google", auto-enables SSO, cancels pending requeue
        si.save_signin(b, True, WHO, SECRET)
        si._save(b, tries=[b.clock()], gap_waived=False)
        # Wait for flow via API
        history_plan[:] = [
            ("https://chatgpt.com/auth/login", time.time()),
            ("https://accounts.google.com/o/oauth2/auth?token=SHOULD_NOT_BE_SAVED", time.time() + 1),
        ]
        r = c.post("/web/login", json={"provider": "chatgpt-web", "account": "b"})
        assert r.status_code == 200
        end = time.time() + 5
        st = r.json()
        while st["state"] not in ("signed_in", "failed") and time.time() < end:
            time.sleep(0.02)
            st = c.get("/web/login", params={"provider": "chatgpt-web", "account": "b"}).json()
        assert st["state"] == "signed_in" and st["method"] == "google"
        acct_b = [a for a in c.get("/web/accounts").json()["providers"][0]["accounts"] if a["id"] == "b"][0]
        assert acct_b["autologin"]["method"] == "google" and acct_b["autologin"]["enabled"] is True
        assert acct_b["autologin"]["queued"] is False
        assert "SHOULD_NOT_BE_SAVED" not in (tmp_path / "chatgpt_web-b.json").read_text()

        # 2. Password login on main (no password saved) -> method="password", needs_credentials=True, enabled=False
        history_plan[:] = [
            ("https://chatgpt.com/auth/login", time.time()),
            ("https://auth.openai.com/log-in/password", time.time() + 1),
        ]
        r2 = c.post("/web/login", json={"provider": "chatgpt-web", "account": "main"})
        assert r2.status_code == 200
        end = time.time() + 5
        st2 = r2.json()
        while st2["state"] not in ("signed_in", "failed") and time.time() < end:
            time.sleep(0.02)
            st2 = c.get("/web/login", params={"provider": "chatgpt-web", "account": "main"}).json()
        assert st2["state"] == "signed_in" and st2["method"] == "password"
        acct_main = [a for a in c.get("/web/accounts").json()["providers"][0]["accounts"] if a["id"] == "main"][0]
        assert acct_main["autologin"]["method"] == "password"
        assert acct_main["autologin"]["needs_credentials"] is True
        assert acct_main["autologin"]["enabled"] is False
        assert acct_main["autologin"]["saved"] is False
        assert not [k for k in store.items if "main@" in k]

        # 3. Empty History falls back to optional method hint on POST /web/login
        history_plan.clear()
        hist_file = setup.bridge_for("chatgpt-web", "main").profile / "Default" / "History"
        if hist_file.exists():
            hist_file.unlink()
        r3 = c.post("/web/login", json={"provider": "chatgpt-web", "account": "main", "method": "apple"})
        assert r3.status_code == 200
        end = time.time() + 5
        st3 = r3.json()
        while st3["state"] not in ("signed_in", "failed") and time.time() < end:
            time.sleep(0.02)
            st3 = c.get("/web/login", params={"provider": "chatgpt-web", "account": "main"}).json()
        assert st3["state"] == "signed_in" and st3["method"] == "apple"
        acct_main = [a for a in c.get("/web/accounts").json()["providers"][0]["accounts"] if a["id"] == "main"][0]
        assert acct_main["autologin"]["method"] == "apple" and acct_main["autologin"]["enabled"] is True

