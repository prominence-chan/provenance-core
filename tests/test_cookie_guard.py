"""cookie_guard: backups of a profile's cookie store, a wiped store noticed and put back once, the launch gate.

The store tests use a hand-made SQLite file shaped like Chrome's; the browser tests run the real flow against the mock
page (login="cookie": signed in means the browser sends the mock's session cookie) on scratch profiles, with the
headless installed Chrome, and once with the bundled browser when one is prepared (PCORE_BROWSER_DIR names where)."""

import asyncio
import json
import os
import secrets
import sqlite3
import stat
import subprocess
import time
from pathlib import Path

import pytest
from mock_chatgpt import SESSION, Mock

from provenance_core import browser_host as bh
from provenance_core import chatgpt_web as cw
from provenance_core import cookie_guard as cg
from provenance_core import web_setup as ws

SPECS = {"Site": {"domains": ["site.test"], "names": ["sid*"]}, "Other": {"domains": ["other.test"], "names": ["SID"]}}
SECRET = "c00kie-value-" + secrets.token_hex(8)
USER = {"messages": [{"role": "user", "content": "hi"}]}


def _chrome_ok():
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(cw.CHROME)


browser = pytest.mark.skipif(not _chrome_ok(), reason="needs playwright and Google Chrome")


def make_store(profile: Path, pads: int = 60, sessions=(("site.test", "sid.0"), (".other.test", "SID"))) -> Path:
    db = profile / "Default" / "Network" / "Cookies"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("create table if not exists cookies (host_key text, name text, value text, encrypted_value blob,"
                " creation_utc int, last_update_utc int, expires_utc int)")  # fmt: skip
    con.execute("delete from cookies")
    rows = [(h, n) for h, n in sessions] + [("pad.test", f"pad{i}") for i in range(pads)]
    con.executemany(
        "insert into cookies values (?, ?, ?, ?, 1, 2, 9)", [(h, n, SECRET, SECRET.encode()) for h, n in rows]
    )
    con.commit()
    con.close()
    return db


def events(home: Path, *kinds) -> list[dict]:
    try:
        lines = [json.loads(x) for x in (home / "web-events.jsonl").read_text().splitlines()]
    except OSError:
        return []
    return [x for x in lines if not kinds or x["kind"] in kinds]


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def leaks(home: Path, value: str, also: str = "") -> list[str]:
    """Where `value` shows outside the cookie stores and their backups (which hold it by nature)."""
    found = ["(text)"] if value in also else []
    for p in home.rglob("*"):
        if not p.is_file() or p.name.startswith("Cookies") or "profile" in p.relative_to(home).parts[0]:
            continue
        try:
            if value.encode() in p.read_bytes():
                found.append(str(p.relative_to(home)))
        except OSError:
            pass
    return found


# ---- the store, backups, rotation -------------------------------------------------------------------------------------
def test_backups_are_checked_rotated_and_private(tmp_path):
    home, prof = tmp_path / "home", tmp_path / "home" / "profile-x"
    db = make_store(prof)
    snap = cg.snapshot(prof, SPECS)
    assert snap["rows"] == 62 and snap["sites"] == {"Site": 1, "Other": 1} and snap["size"] > 0
    assert cg.snapshot(tmp_path / "none", SPECS)["rows"] == 0
    t0 = time.mktime((2027, 1, 15, 12, 0, 0, 0, 0, -1))  # noon: the copies of one day below stay within it
    r = cg.take(home, prof, SPECS, "exit", expect={"Site"}, browser="157.0.1.2", wall=t0)
    assert r["kept"] and r["rows"] == 62 and r["sites"]["Site"] == 1
    root = home / "cookie-backups" / "profile-x"
    gen = cg.generations(home, prof)[0]
    assert gen["browser"] == "157.0.1.2" and gen["rows"] == 62 and gen["kind"] == "exit"
    assert mode(home / "cookie-backups") == 0o700 and mode(root) == 0o700 and mode(gen["dir"]) == 0o700
    assert {mode(f) for f in Path(gen["dir"]).iterdir()} == {0o600}
    assert SECRET not in (Path(gen["dir"]) / "meta.json").read_text() and SECRET not in json.dumps(r)
    assert SECRET.encode() in (Path(gen["dir"]) / "Cookies").read_bytes()  # the copy is the store, byte for byte
    # nothing changed: no new copy
    assert cg.take(home, prof, SPECS, "exit", wall=t0 + 60)["why"] == "unchanged"
    # an emptied store is never kept, nor one that collapsed and lost a signed-in site's session cookies
    make_store(prof, pads=0, sessions=())
    assert cg.take(home, prof, SPECS, "exit", expect={"Site"}, wall=t0 + 120) == {
        "kept": False, "why": "empty", "rows": 0, "sites": {"Site": 0, "Other": 0}}  # fmt: skip
    make_store(prof, pads=9, sessions=())
    assert cg.take(home, prof, SPECS, "exit", expect={"Site"}, wall=t0 + 180)["why"] == "collapsed"
    db.write_bytes(b"\0" * 8192)  # zero-filled pages, as found on 2026-11-23
    assert cg.take(home, prof, SPECS, "exit", expect={"Site"}, wall=t0 + 200)["why"] == "unreadable"
    assert [g["rows"] for g in cg.generations(home, prof)] == [62] and not list(root.glob(".tmp-*"))
    # fewer rows with the session cookies still there (the site tidied up), or on an account that is signed out
    # anyway: an ordinary store
    db.unlink()
    make_store(prof, pads=9)
    assert cg.take(home, prof, SPECS, "exit", expect={"Site"}, wall=t0 + 240)["kept"]
    make_store(prof, pads=1, sessions=())
    assert cg.take(home, prof, SPECS, "exit", expect=set(), wall=t0 + 300)["kept"]
    # rotation: the newest 5, and the newest of each of the last 7 days
    for day in range(1, 10):
        for n in range(3):
            make_store(prof, pads=60 + day * 3 + n)
            assert cg.take(home, prof, SPECS, "hourly", wall=t0 + day * 86400 + n * 3600)["kept"]
    gens = cg.generations(home, prof)
    days = [time.strftime("%Y-%m-%d", time.localtime(g["ts"])) for g in gens]
    assert len(gens) == 5 + 6 and gens[0]["rows"] == 62 + 9 * 3 + 2  # days 9 and 8 are among the newest 5 already
    assert len(set(days)) == 8 and days.count(days[0]) == 3 and min(g["ts"] for g in gens) >= t0 + 2 * 86400
    # put one back: what was there is moved aside and kept, the copy is private
    make_store(prof, pads=2, sessions=())
    out = cg.restore(home, prof, gens[0], wall=t0 + 10 * 86400)
    assert cg.snapshot(prof, SPECS)["rows"] == gens[0]["rows"] and mode(db) == 0o600
    assert cg.read_db(root / out["aside"] / "Cookies", SPECS)["rows"] == 2 and mode(root / out["aside"]) == 0o700
    assert len(cg.generations(home, prof)) == len(gens)  # the folder set aside is not a backup


def test_a_running_browsers_store_is_copied_without_waiting(tmp_path):
    home, prof = tmp_path / "home", tmp_path / "home" / "profile-x"
    db = make_store(prof)
    busy = sqlite3.connect(db, isolation_level=None)
    busy.execute("begin exclusive")  # the browser is in the middle of a write
    t = time.monotonic()
    r = cg.take(home, prof, SPECS, "hourly", running=True, wall=1_800_000_000.0)
    assert r == {"kept": False, "why": "busy"} and time.monotonic() - t < 5
    busy.execute("rollback")
    r = cg.take(home, prof, SPECS, "hourly", running=True, wall=1_800_000_000.0)
    busy.close()
    gen = cg.generations(home, prof)[0]
    assert r["kept"] and gen["files"] == ["Cookies"] and cg.read_db(Path(gen["dir"]) / "Cookies", SPECS)["rows"] == 62


def test_wiped_means_collapsed_and_signed_in_sites_without_their_cookies():
    base = {"rows": 300, "sites": {"Site": 2, "Other": 1}}
    few = {"rows": 9, "sites": {"Site": 0, "Other": 0}}
    assert cg.lost_sites(few, base, {"Site", "Other"}) == ["Other", "Site"] and cg.lost_sites(few, base, {"Site"}) == [
        "Site"
    ]
    assert cg.lost_sites(few, base, set()) == []  # nobody was signed in: nothing was lost
    assert cg.lost_sites({"rows": 290, "sites": {"Site": 0, "Other": 1}}, base, {"Site"}) == []  # a sign-out
    assert cg.lost_sites({"rows": 9, "sites": {"Site": 1, "Other": 0}}, base, {"Site"}) == []  # the cookie is there
    assert cg.lost_sites({"rows": None, "sites": {}}, base, {"Site"}) == []  # could not be read: not judged
    assert cg.collapsed(0, 3) and cg.collapsed(9, 300) and not cg.collapsed(2, 3) and not cg.collapsed(80, 300)


# ---- the clock ---------------------------------------------------------------------------------------------------------
def test_the_clock_is_checked_against_a_counting_clock_and_what_was_written():
    t = {"wall": 1_800_000_000.0, "mono": 5000.0}
    c = cg.Clock(lambda: t["wall"], lambda: t["mono"])
    start = t["wall"]

    def go(wall=0.0, mono=0.0):
        t["wall"] += wall
        t["mono"] += mono

    assert c.check([start - 30]) is None
    go(3 * 86400, 3 * 86400)  # asleep for a long weekend: both clocks moved, nothing was written meanwhile
    assert c.check([start - 30]) is None
    go(2 * 86400 + 100, 2)  # the clock jumps two days ahead in two seconds
    assert "jumped ahead by 2 days" in c.check([start])
    go(-2 * 86400 - 100, 5)  # and back: still not trusted,
    assert "jumped back" in c.check([start])
    go(300, 300)
    assert c.check([start]) is not None
    go(400, 400)  # until it has run steadily for clock_settle_s
    assert c.check([start]) is None
    # a new process (it did not see the time pass) against what the router wrote
    now = t["wall"]
    fresh = cg.Clock(lambda: t["wall"], lambda: t["mono"])
    assert fresh.check([now - 3600]) is None
    assert "behind the last thing the router wrote" in fresh.check([now + 3600])
    assert "2 days ahead of the last thing the router wrote" in fresh.check([now - 2 * 86400])
    # the note an earlier process of this boot left explains a long gap, and unmasks a stepped clock
    note = {"wall": now - 2 * 86400, "mono": t["mono"] - 2 * 86400}
    assert cg.Clock(lambda: t["wall"], lambda: t["mono"]).check([now - 2 * 86400], note) is None
    assert "ahead of where it was" in cg.Clock(lambda: t["wall"], lambda: t["mono"]).check(
        [], {**note, "mono": t["mono"] - 60}
    )
    assert (
        cg.Clock(lambda: t["wall"], lambda: t["mono"]).check([], {**note, "mono": t["mono"] + 9e6}) is None
    )  # a reboot
    # a doubted clock that stays put is the time after all (and what was written is not)
    go(0, 601)
    go(601, 0)
    assert fresh.check([now + 3600]) is None and fresh.check([now + 3600]) is None


def test_a_wrong_clock_or_a_full_disk_stops_the_launch(tmp_path):
    pytest.importorskip("playwright")
    t = {"wall": time.time(), "mono": 100.0, "free": 50e9}
    launches, slept = [], []

    async def sleep(s):
        slept.append(s)

    async def go():
        b = cw.Bridge("http://127.0.0.1:9", {"notify": False, "headless": True, "profile": str(tmp_path / "profile")},
                      home=tmp_path, sleep=sleep, clock=lambda: t["wall"])  # fmt: skip
        b.guard.clock = cg.Clock(lambda: t["wall"], lambda: t["mono"])
        b.guard.free = lambda _p: t["free"]

        async def start(*a):
            launches.append(a)
            raise cw.WebUnavailable("no browser in this test", 1, "browser")

        b._start_browser = start
        b.save(signed_in=True, checked_at=int(t["wall"]))
        t["wall"] += 2 * 86400  # two days ahead in no time
        clock = await b.check()
        st = b.status()
        again = b.capacity()
        t["wall"] -= 2 * 86400  # and back again: seen as well,
        back = await b.check()
        t["mono"] += 700
        t["wall"] += 700  # then steady for a while: the launch goes ahead (and fails for the test's own reason)
        b.reset()
        fine = await b.check()
        t["free"] = 200e6
        b.reset()
        disk = await b.check()
        return b, clock, st, again, back, fine, disk, b.status()

    b, clock, st, again, back, fine, disk, st2 = asyncio.run(go())
    assert clock["ok"] is False and clock["kind"] == "clock" and "jumped ahead by 2 days" in clock["reason"]
    assert st["blocked"] and st["kind"] == "clock" and 0 < st["blocked_for_s"] <= 300 and not again["available"]
    assert back["kind"] == "clock" and "jumped back by 2 days" in back["reason"]
    assert slept == [5.0] * 6 and len(launches) == 1  # looked at three times, each of the three times
    assert fine["kind"] == "browser" and "no browser in this test" in fine["reason"]
    assert disk["kind"] == "disk" and "200 MB" in disk["reason"] and st2["kind"] == "disk"
    refused = events(tmp_path, "launch_refused")
    assert [e["why"] for e in refused] == ["clock", "clock", "disk"] and refused[2]["free_mb"] == 200
    assert all(e["wall"] and e["mono"] and e["at"] and e["profile"] == "profile" for e in refused)
    for kind in ("clock", "disk", "cookies_lost"):
        text = ws.plain_reason({"kind": kind, "blocked_for_s": 300}, b)
        assert "sign-ins" in text and not any(w in text for w in ("chatgpt-web", "profile", "bridge", "_")), text
    assert ws.plain_reason({"kind": "cookies_lost", "reason": cg.LOST_AGAIN}, b) == cg.LOST_AGAIN


# ---- the whole thing, against the mock page ---------------------------------------------------------------------------
def bridge(mock, home: Path, **opts):
    return cw.Bridge(
        mock.url,
        {"driver": os.environ.get("PCORE_TEST_DRIVER", "playwright"), "headless": True, "notify": False,
         "min_gap_s": 0, "max_gap_s": 0, "challenge_wait_s": 1, "profile": str(home / "profile"),
         "session_cookies": {"domains": ["127.0.0.1"], "names": [SESSION]}, "cookie_settle_s": 1.5,
         "cookie_recheck_s": [], **opts},
        home=home,
    )  # fmt: skip


async def sign_in(b, mock):
    """What a person does once in the sign-in window: the site sets its session cookie (and others)."""
    page = await b._ensure_page()
    await page.goto(mock.url + "/auth/login")
    await page.evaluate("fetch('/__signin', {method: 'POST', body: '{}'}).then(r => r.text())")
    await page.evaluate(
        "for (let i = 0; i < 60; i++) document.cookie = 'pad' + i + '=' + i + '; path=/; max-age=86400'"
    )
    r = await b.check("auto")
    assert r["ok"] and r["signed_in"] is True, r
    await b.release()


def empty_store(profile: Path) -> int:
    """What happened on 2026-11-23, done by hand: every row gone, the file still there."""
    db = cg.cookie_db(profile)
    con = sqlite3.connect(db)
    n = con.execute("select count(*) from cookies").fetchone()[0]
    con.execute("delete from cookies")
    con.commit()
    con.close()
    return n


def running_on(profile) -> str:
    return subprocess.run(["pgrep", "-f", f"user-data-dir={profile}"], capture_output=True, text=True).stdout.strip()


@pytest.fixture
def mock():
    m = Mock()
    m.state["login"] = "cookie"
    m.session_tail = SECRET
    yield m
    m.close()


class _NoFlows:
    @staticmethod
    def login_state(provider, account):
        return {"state": "idle", "reason": None}


def wipe_and_restore(mock, home: Path, capfd, **opts):
    """Sign in, close (a backup is taken), empty the store while the browser is down, ask again (the backup is put
    back, the request is served signed in), empty it again (reported, nothing put back)."""

    async def go():
        b = bridge(mock, home, **opts)
        out = {}
        try:
            await sign_in(b, mock)
            out["first"] = cg.generations(home, b.profile)
            out["rows"] = empty_store(b.profile)
            out["reply"] = await b.complete("auto", dict(USER))
            out["status"] = b.status()
            out["browser"] = b.browser_in_use()
            out["account"] = ws.WebSetup._account(_NoFlows(), "chatgpt-web", "main", b)  # as GET /web/accounts lists it
            await b.release()
            out["kept"] = [g["rows"] for g in cg.generations(home, b.profile)]
            empty_store(b.profile)
            with pytest.raises(cw.WebUnavailable) as e:
                await b.complete("auto", dict(USER))
            out["second"], out["status2"] = e.value, b.status()
        finally:
            await b.release()
        out["b"] = b
        return out

    r = asyncio.run(go())
    b, prof, root = r["b"], home / "profile", home / "cookie-backups" / "profile"
    # a backup after the clean exit, private
    assert r["rows"] >= 61 and r["first"] and r["first"][0]["rows"] == r["rows"] and r["first"][0]["kind"] == "exit"
    assert r["first"][0]["sites"] == {"ChatGPT": 1} and r["first"][0]["browser"]
    assert mode(home / "cookie-backups") == 0o700 and mode(root) == 0o700
    assert all(mode(d) == 0o700 and {mode(f) for f in d.iterdir()} == {0o600} for d in root.iterdir() if d.is_dir())
    # the wiped store was noticed, the backup put back, and the request served signed in
    assert r["reply"]["choices"][0]["message"]["content"] == "ok"
    st = r["status"]
    assert st["signed_in"] is True and not st["blocked"] and st["notice"]["text"] == cg.LOST_RESTORED
    acct = r["account"]
    assert acct["notice"] == "cookies_lost" and acct["reason"] == cg.LOST_RESTORED and acct["signed_in"] is True
    ev = events(home)
    kinds = [e["kind"] for e in ev]
    wiped = [e for e in ev if e["kind"] == "cookies_wiped"]
    assert wiped[0]["where"] == "before launch" and wiped[0]["lost"] == ["ChatGPT"] and wiped[0]["now"]["rows"] == 0
    back = [e for e in ev if e["kind"] == "cookies_back"]
    assert len(back) == 1 and back[0]["cookies"]["rows"] >= 61 and back[0]["cookies"]["sites"] == {"ChatGPT": 1}
    assert kinds.count("cookies_restored") == 1 and kinds.count("browser_launch") == kinds.count("browser_exit") >= 3
    assert len(list(root.glob("wiped-*"))) == 1  # what was there is kept
    assert min(r["kept"]) >= r["rows"]  # the emptied store never became a backup
    # every launch and exit left a line: size, rows, session cookies per site, both clocks, free disk
    for e in ev:
        if e["kind"] in ("browser_launch", "browser_exit"):
            assert set(e["cookies"]) == {"size", "rows", "sites"} and e["wall"] and e["mono"] and e["free_mb"] > 0
            assert e["browser"] and e["at"] and e["up_s"] >= 0 and e["profile"] == "profile"
    assert {e["how"] for e in ev if e["kind"] == "browser_exit"} == {"closed"}
    # the second wipe of the day: reported in plain words, nothing put back, everything kept
    assert r["second"].kind == "cookies_lost" and r["second"].reason == cg.LOST_AGAIN
    st2 = r["status2"]
    assert st2["blocked"] and st2["kind"] == "cookies_lost" and st2["signed_in"] is False
    assert ws.plain_reason(st2, b) == cg.LOST_AGAIN and kinds.count("cookies_restored") == 1
    lost = [e for e in ev if e["kind"] == "cookies_lost"]
    assert len(lost) == 1 and lost[0]["reason"] == cg.LOST_AGAIN and len(wiped) == 2
    assert min(g["rows"] for g in cg.generations(home, prof)) >= r["rows"] and len(list(root.glob("wiped-*"))) == 1
    assert cg.snapshot(prof, b.guard.specs())["sites"] == {"ChatGPT": 0}  # left as it was found
    assert not running_on(prof)
    # no cookie value anywhere but in the stores and their backups
    cap = capfd.readouterr()
    said = cap.out + cap.err + json.dumps([st, st2, acct, r["reply"], str(r["second"]), r["first"]])
    assert not leaks(home, SECRET, said)
    return r


@browser
def test_a_wiped_store_is_put_back_once_a_day(mock, tmp_path, capfd):
    wipe_and_restore(mock, tmp_path, capfd)


@browser
def test_a_store_wiped_as_the_browser_starts_is_noticed_after_the_launch(mock, tmp_path):
    """The store is fine on disk before the launch and empty a moment after it (as when the browser itself deletes
    what it cannot read): noticed while it settles, the browser closed again, the backup put back, then served."""

    async def go():
        b = bridge(mock, tmp_path)
        try:
            await sign_in(b, mock)
            start, n = b._start_browser, []

            async def start_then_wipe(*a):
                page = await start(*a)
                if not n:
                    n.append(empty_store(b.profile))
                return page

            b._start_browser = start_then_wipe
            reply = await b.complete("auto", dict(USER))
            return reply, b.status(), n
        finally:
            await b.release()

    reply, st, n = asyncio.run(go())
    assert n and n[0] >= 61 and reply["choices"][0]["message"]["content"] == "ok"
    assert st["signed_in"] is True and st["notice"]["kind"] == "cookies_lost" and not st["blocked"]
    ev = events(tmp_path, "cookies_wiped", "cookies_restored", "cookies_back", "cookies_lost")
    assert [e["kind"] for e in ev] == ["cookies_wiped", "cookies_wiped", "cookies_restored", "cookies_back"]
    assert ev[0]["where"] == "after launch" and ev[0]["before"]["rows"] == n[0] and ev[0]["now"]["rows"] < 10
    assert not running_on(tmp_path / "profile")


@browser
def test_a_store_wiped_under_a_running_browser_is_put_back(mock, tmp_path):
    """Seen at a later look (or when a page says "signed out"): the account is paused, the browser closed, the
    backup put back and the browser started again; the next request is served signed in."""

    async def go():
        b = bridge(mock, tmp_path)
        try:
            await sign_in(b, mock)
            await b.complete("auto", dict(USER))
            fine = b._guard_poke("recheck")
            n = empty_store(b.profile)
            hit = b._guard_poke("recheck")
            with pytest.raises(cw.WebSkip, match="saved sign-ins are being put back"):
                await b.complete("auto", dict(USER))
            during = b.status()
            await b._recovering
            reply = await b.complete("auto", dict(USER))
            return fine, n, hit, during, reply, b.status()
        finally:
            await b.release()

    fine, n, hit, during, reply, st = asyncio.run(go())
    assert fine is False and n >= 61 and hit is True and not during["signing_in"]
    assert reply["choices"][0]["message"]["content"] == "ok" and st["signed_in"] is True and not st["blocked"]
    assert st["notice"]["text"] == cg.LOST_RESTORED and not st["signing_in"]
    ev = events(tmp_path, "cookies_wiped", "cookies_restored", "cookies_back", "cookies_lost")
    assert [(e["kind"], e.get("where")) for e in ev] == [
        ("cookies_wiped", "recheck"), ("cookies_wiped", "before launch"), ("cookies_restored", None),
        ("cookies_back", None)]  # fmt: skip
    assert not running_on(tmp_path / "profile")


def test_a_backup_from_another_browser_version_needs_that_version(tmp_path, monkeypatch):
    """Measured: a newer major build deletes the cookies an older one wrote, again and again. So such a backup is
    put back only together with the build that wrote it, which is then held (browser_host.pin); when that build
    cannot be had, nothing is put back and the loss is reported."""
    root = tmp_path / "browser"
    monkeypatch.setattr(bh, "root_dir", lambda: root)
    old, new = {"exe": "/x/157", "version": "157.0.8103.78"}, {"exe": "/x/158", "version": "158.0.8127.88"}
    have = {"old": True}

    def ensure(profile=None, **k):
        pin = bh.pinned()
        return (old if have["old"] else None) if pin and pin["major"] == 157 else new

    monkeypatch.setattr(bh, "ensure", ensure)

    async def go(home):
        b = cw.Bridge("http://127.0.0.1:9", {"notify": False, "browser": "bundled", "profile": str(home / "profile"),
                                             "session_cookies": SPECS["Site"]}, home=home)  # fmt: skip
        make_store(b.profile)
        (b.profile / "Last Version").write_text("157.0.8103.78")
        b.save(signed_in=True, checked_at=int(time.time()))
        assert b.guard.backup("exit", False, "157.0.8103.78")["kept"]
        make_store(b.profile, pads=9, sessions=())  # what 158 left of it
        (b.profile / "Last Version").write_text("158.0.8127.88")
        rec = await b._guard_before(new)
        return b, rec

    b, rec = asyncio.run(go(tmp_path / "a"))
    assert rec == old and (bh.pinned()["major"], bh.pinned()["refused"]) == (157, 158)
    assert b.guard.snap()["rows"] == 62 and b.load().get("signed_in") is True
    assert [e["kind"] for e in events(tmp_path / "a")][-3:] == ["cookies_wiped", "browser_pinned", "cookies_restored"]
    bh.unpin()
    have["old"] = False
    b, rec = asyncio.run(go(tmp_path / "b"))
    st = b.status()
    assert rec == new and bh.pinned() is None and b.guard.snap()["rows"] == 9  # nothing put back, nothing held
    assert st["kind"] == "cookies_lost" and st["reason"] == cg.LOST_VERSION and st["signed_in"] is False
    assert len(cg.generations(tmp_path / "b", b.profile)) == 1  # the backup is still there


# ---- the bundled browser, once ------------------------------------------------------------------------------------------
def _bundled_ready():
    """The bundled browser already prepared under <PCORE_HOME>/browser (or PCORE_BROWSER_DIR); never prepared here."""
    if not _chrome_ok():
        return None
    return bh.current(need=bh.required_major())


@pytest.mark.skipif(not _bundled_ready(), reason="no bundled browser prepared (python -m provenance_core.browser_host)")
def test_bundled_browser_wiped_store_is_put_back(mock, tmp_path, monkeypatch, capfd):
    rec = _bundled_ready()
    monkeypatch.setattr(bh, "ensure", lambda profile=None, **k: rec)
    monkeypatch.setattr(bh, "pin", lambda *a, **k: pytest.fail("the prepared browser's folder is not written to"))
    monkeypatch.setattr(bh, "unpin", lambda *a, **k: None)
    r = wipe_and_restore(mock, tmp_path, capfd, headless=False, browser="bundled")
    assert r["browser"].startswith("bundled ") and r["first"][0]["browser"] == rec["version"]
