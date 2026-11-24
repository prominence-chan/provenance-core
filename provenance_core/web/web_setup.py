"""Account setup for the web-chat bridge, done from the app (the /web/* endpoints) or `provenance-core weblogin`.

An account is a browser profile, shared by every site (`share:` in the catalog): signing in to Gemini on account b
and to ChatGPT on account b both write the profile chatgpt-web-profile-b. A profile can only be open in one browser
at a time, so a sign-in:

1. pauses every bridge on that profile (their requests skip the account, with no penalty), waits for one in flight
   to finish, and closes the account's background browser;
2. opens ONE ordinary, visible window on the profile at the site's sign-in page, with the same build the bridge
   runs in the background (the bundled build's normal copy; the installed Chrome only when there is none), and
   brings it to the front (`open -n -a`: macOS activates an app the user asked for);
3. watches it from outside: the window has no debugging port and nothing drives it. Measured on Chrome 157: a
   browser started with --remote-debugging-port reports navigator.webdriver = true to every page even with nothing
   attached, which is the signal Google's "This browser or app may not be secure" refusal is known to key on; with
   no port it reports false. So sign-in is noticed by the site's session cookie appearing in the profile's cookie
   database (only the cookie's name and dates are read, never its value; Chrome writes new cookies to disk within
   about 30 s), or by the user closing the window, or by `login_timeout_s` passing;
4. closes the window (SIGTERM: Chrome quits cleanly and saves the profile), lifts the pause, and checks the site in
   the background browser with Bridge.check (a fresh chat is opened, nothing is sent): `signed_in`, or `failed` with
   a reason written for the person using the app.

States of one provider + account (GET /web/login), and the only ways between them:

    idle ──POST──> preparing ──> open ──(cookie | window closed | timeout)──> verifying ──> signed_in | failed
                   (waiting for the browser download or a request in flight)
    preparing / open / verifying ──POST /web/login/cancel──> idle   (window closed, nothing checked)
    signed_in / failed stay until the next POST /web/login or cancel for that provider + account.
    Any step that cannot be done (no browser, profile held by another window) ──> failed.

A failed or cancelled sign-in still lifts the pause: the account is never left with neither a sign-in window nor a
background browser that can start. If the router itself dies, a watchdog closes the window.

Accounts are global: POST /web/accounts adds one slot (a new profile) to every web provider, DELETE removes it from
all of them; both are kept in <PCORE_HOME>/web_accounts.json and merged over the catalog's `accounts:` lists when the
catalog is loaded, so catalog.yaml is never rewritten.

Per provider + account there is also: a saved sign-in with automatic sign-in (web_signin.py; PUT / PATCH / DELETE
/web/accounts/{provider}/{account}/autologin and POST .../autologin/try), and for ChatGPT the account's plan with a
confirmation before an account on a paid plan is used (POST /web/accounts/{provider}/{account}/confirm)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from . import browser_host, cookie_guard, web_signin
from ..keys import pool_help
from ..routing import provider_settings
from .chatgpt_web import Bridge, BridgePool, enabled_by, home_dir, stop_watchdog, watchdog

ACCOUNT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
ACTIVE = ("preparing", "open", "verifying")
DEFAULT_BOUNDS = {"left": 80, "top": 60, "width": 1000, "height": 800}


class SetupError(Exception):
    """Something the person using the app has to know; `str(e)` is written for them."""


class BadRequest(SetupError):
    """The request itself is not acceptable (answered 400): no agreement, a missing field."""


# ---- accounts: the catalog's lists plus what was added or removed from the app ------------------------------------
def accounts_file(home: Path | None = None) -> Path:
    return Path(home or home_dir()) / "web_accounts.json"


def load_overrides(home: Path | None = None) -> dict:
    try:
        d = json.loads(accounts_file(home).read_text())
    except (OSError, ValueError):
        d = {}
    return {"added": [str(a) for a in d.get("added") or []], "removed": [str(a) for a in d.get("removed") or []]}


def save_overrides(d: dict, home: Path | None = None):
    p = accounts_file(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"added": d["added"], "removed": d["removed"]}, indent=1))
    tmp.replace(p)


def merge_accounts(lists: list, overrides: dict) -> list[str]:
    """The one account list every web provider gets: the catalog's lists in order (a provider without one has
    "main"), minus the removed ones, plus the added ones."""
    base = list(dict.fromkeys(str(a) for lst in lists for a in (lst or ["main"])))
    out = [a for a in base if a not in overrides["removed"]]
    return out + [a for a in overrides["added"] if a not in out and a not in overrides["removed"]]


def apply_accounts(providers: dict, home: Path | None = None):
    """catalog.load: give every chatgpt_web provider the same, merged `accounts`. Left alone when nothing says more
    than the one default account (no `accounts:` anywhere and nothing added)."""
    web = [p for p in providers.values() if p.type == "chatgpt_web"]
    if not web:
        return
    lists = [p.options.get("accounts") for p in web]
    merged = merge_accounts(lists, load_overrides(home))
    if merged == ["main"] and not any(lists):
        return
    for p in web:
        p.options["accounts"] = list(merged)


# ---- reading the profile from outside -------------------------------------------------------------------------------
_cookie_db, _matches, session_cookies = cookie_guard.cookie_db, cookie_guard._matches, cookie_guard.session_cookies


# ---- the visible window --------------------------------------------------------------------------------------------
# How many real windows (layer 0, larger than Chrome's 1x1 helper windows, minimized ones included) a process has,
# from the window server's list. Needs no permission (only window titles would). On macOS, closing a browser's last
# window leaves the app running, so this is how a window closed by hand is told from one still open.
WINDOWS_JXA = r"""ObjC.import('CoreGraphics');
function run(argv) {
  const pid = parseInt(argv[0]); let n = 0;
  const list = ObjC.castRefToObject($.CGWindowListCopyWindowInfo(0, 0));
  for (let i = 0; i < list.count; i++) {
    const w = list.objectAtIndex(i);
    if (w.objectForKey('kCGWindowOwnerPID').js !== pid || w.objectForKey('kCGWindowLayer').js !== 0) continue;
    const b = w.objectForKey('kCGWindowBounds');
    if (b.objectForKey('Width').js > 100 && b.objectForKey('Height').js > 100) n++;
  }
  return String(n);
}"""


def real_windows(pid: int) -> int | None:
    """None when it cannot be told (not macOS, osascript missing or failing)."""
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", WINDOWS_JXA, str(int(pid))],
                           capture_output=True, text=True, timeout=10)  # fmt: skip
        return int(r.stdout.strip()) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


QUIT_JXA = r"""ObjC.import('AppKit');
function run(argv) { const a = $.NSRunningApplication.runningApplicationWithProcessIdentifier(parseInt(argv[0]));
  return a.isNil() ? 'none' : String(a.terminate); }"""


def quit_app(pid: int) -> bool:
    """Ask an app to quit the way Cmd+Q does (NSRunningApplication.terminate; no permission needed). This matters:
    measured on Chrome 157, a SIGTERM exits at once without writing cookies set in the last 30 s, so a sign-in
    finished just before would be lost, while this quit writes them. False when it could not be asked."""
    if sys.platform != "darwin":
        return False
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", QUIT_JXA, str(int(pid))],
                           capture_output=True, text=True, timeout=10)  # fmt: skip
        return r.stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


ACTIVATE_JXA = r"""ObjC.import('AppKit');
function run(argv) { const a = $.NSRunningApplication.runningApplicationWithProcessIdentifier(parseInt(argv[0]));
  return a.isNil() ? 'none' : String(a.unhide && a.activateWithOptions($.NSApplicationActivateAllWindows)); }"""


def activate_app(pid: int) -> bool:
    """Bring an app's windows to the front, as clicking its Dock icon does (NSRunningApplication; no permission
    needed). macOS may refuse when the asking process is not the active app; False then, or when it cannot ask."""
    if sys.platform != "darwin":
        return False
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", ACTIVATE_JXA, str(int(pid))],
                           capture_output=True, text=True, timeout=10)  # fmt: skip
        return r.stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


class SigninWindow:
    """One ordinary browser window on a profile, not driven by anything: no debugging port, no automation switch.
    `launch`: "open" (macOS LaunchServices: a new instance, brought to the front like any app the user opens) or
    "exec" (started directly; tests use it with a headless switch so that nothing shows)."""

    def __init__(self, app: Path, profile: Path, url: str, bounds: dict | None = None, launch: str = "open",
                 extra_args=()):  # fmt: skip
        self.app, self.profile, self.url, self.launch = Path(app), Path(profile), url, launch
        self.bounds = {**DEFAULT_BOUNDS, **{k: int(v) for k, v in (bounds or {}).items() if k in DEFAULT_BOUNDS}}
        self.extra_args = list(extra_args)
        self.pid: int | None = None
        self._proc = self._watch = None
        self._had_window = False
        self._none_seen = 0

    def closed_by_hand(self) -> bool:
        """The app is still running but its windows are gone (Cmd+W / the red button on the last window). Only
        after a window was seen, and twice in a row; headless windows (tests) never count."""
        if not self.pid or self.headless:
            return False
        n = real_windows(self.pid)
        if n is None:
            return False
        if n > 0:
            self._had_window, self._none_seen = True, 0
            return False
        self._none_seen += 1
        return self._had_window and self._none_seen >= 2

    def args(self) -> list[str]:
        b = self.bounds
        return [
            f"--user-data-dir={self.profile}",
            "--use-mock-keychain",  # the same cookie-encryption key as the background browser: one cookie jar
            "--password-store=basic",
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-crash-restore-bubble",
            # always given: otherwise Chrome reopens the profile's last window where it was, which for a profile the
            # background browser used is off screen
            f"--window-position={b['left']},{b['top']}",
            f"--window-size={b['width']},{b['height']}",
            *self.extra_args,
            self.url,
        ]

    async def open(self, wait_s: float = 30.0):
        exe = browser_host.app_exe(self.app)
        if not exe or not exe.exists():
            raise SetupError("The browser used for signing in could not be found. Try again in a minute.")
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.launch == "open":
            cmd = ["open", "-n", "-W", "-a", str(self.app), "--args", *self.args()]
        else:
            cmd = [str(exe), *self.args()]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)  # fmt: skip
        end = time.time() + wait_s
        while time.time() < end:
            pid = browser_host.window_pid(self.profile)
            if pid and (self.launch == "open" or pid == self._proc.pid):
                self.pid = pid
                self._watch = watchdog(pid)  # the router going away closes the window too
                return
            if self._proc.poll() is not None:
                break
            await asyncio.sleep(0.2)
        await self.close()
        raise SetupError("The sign-in window did not open. Try again.")

    @property
    def headless(self) -> bool:  # tests: a window that cannot be seen, so it is never brought forward either
        return "--headless" in " ".join(self.extra_args)

    async def show(self) -> bool:
        """Bring the window back to the front (it went behind the app, or another window). False when it is not
        open or macOS did not bring it forward."""
        if not self.alive() or self.headless:
            return False
        return await asyncio.to_thread(activate_app, self.pid)

    def alive(self) -> bool:
        if not self.pid:
            return False
        if self.launch == "exec" and self._proc and self._proc.pid == self.pid:
            return self._proc.poll() is None  # our own child: poll, or a finished one would look alive
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return browser_host.window_pid(self.profile) == self.pid

    async def close(self, timeout: float = 20.0):
        """Quit it as Cmd+Q does, so the profile (cookies included) is written; SIGTERM, then SIGKILL, only if that
        cannot be done or does not end it."""
        pid, self.pid = self.pid, None
        if pid:
            asked = self._pid_alive(pid) and await asyncio.to_thread(quit_app, pid)
            for sig in ([None] if asked else []) + [signal.SIGTERM, signal.SIGKILL]:
                if sig is not None:
                    try:
                        os.kill(pid, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
                end = time.time() + (5 if sig == signal.SIGKILL else timeout)
                while time.time() < end and self._pid_alive(pid):
                    await asyncio.sleep(0.2)
                if not self._pid_alive(pid):
                    break
        if self._proc:
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        stop_watchdog(self._watch)
        self._watch = None

    def _pid_alive(self, pid: int) -> bool:
        if self._proc and self._proc.pid == pid:
            return self._proc.poll() is None
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True


# ---- one sign-in ---------------------------------------------------------------------------------------------------
class LoginFlow:
    def __init__(self, setup: WebSetup, provider: str, account: str, bounds: dict | None = None):
        self.setup, self.provider, self.account, self.bounds = setup, provider, account, bounds
        self.bridge = setup.bridge_for(provider, account)
        self.owner = self.bridge.host or self.bridge  # the bridge whose browser holds the profile
        self.state, self.reason, self.how = "idle", None, None
        self.started_at = self.updated_at = time.time()
        self.window: SigninWindow | None = None
        self.task: asyncio.Task | None = None
        self._paused: list[Bridge] = []

    @property
    def name(self) -> str:
        return self.bridge.display_name

    def view(self) -> dict:
        return {"state": self.state, "reason": self.reason, "provider": self.provider, "account": self.account,
                "since": int(self.updated_at), "how": self.how}  # fmt: skip

    def _set(self, state: str, reason: str | None = None):
        self.state, self.reason, self.updated_at = state, reason, time.time()

    async def run(self):
        try:
            self._set("preparing")
            app = await self._app()
            await self._release()
            if browser_host.window_pid(self.owner.profile):
                raise SetupError(
                    "This account's browser is still open in another window. Close that window and try again."
                )
            spec = self.bridge.session_spec
            before = await asyncio.to_thread(session_cookies, self.owner.profile, spec)
            self.window = SigninWindow(app, self.owner.profile, self.bridge.login_url, self.bounds,
                                       self.setup.launch, self.setup.extra_args)  # fmt: skip
            await self.window.open()
            self._set("open")
            self.how = await self._watch(spec, before)
            self._set("verifying")
            await self.window.close()
            self._resume()
            await self._verify()
        except asyncio.CancelledError:
            raise
        except SetupError as e:
            self._set("failed", str(e))
        except Exception as e:  # noqa: BLE001 - never leave a flow hanging in a working state
            print(f"web setup: sign-in for {self.provider}/{self.account} failed: {e!r}", file=sys.stderr)
            self._set("failed", "Something went wrong while signing in. Try again.")
        finally:
            if self.window and self.window.pid:
                await self.window.close()
            self._resume()

    async def _app(self) -> Path:
        """The app for the window, waiting while the bundled build is prepared."""
        o = self.owner.o
        mode = str(o.get("browser") or "chrome").lower()
        source = str(o.get("browser_source") or "cft").lower()
        if self.setup.app:
            return Path(self.setup.app)
        if mode not in ("bundled", "auto") or source == "chrome":
            app = browser_host.signin_app(self.owner.profile, mode, source)
            if not app:
                raise SetupError("Google Chrome is needed to sign in and was not found on this Mac.")
            return app
        tried = False
        while True:
            app = browser_host.signin_app(self.owner.profile, mode, source, fallback=False)
            if app:
                return app
            if browser_host.preparing():
                st = browser_host.prep_status()
                pct = f" ({st['progress']:.0%})" if st.get("progress") is not None else ""
                self._set("preparing", f"Getting the browser ready{pct}. This happens once.")
            elif not tried:
                tried = True
                browser_host.prepare(self.owner.profile)
                await asyncio.sleep(0.2)
                continue
            else:  # it could not be prepared: the installed Chrome, if there is one that can open the profile
                app = browser_host.signin_app(self.owner.profile, mode, source, fallback=True)
                if app:
                    return app
                raise SetupError(
                    "The browser needed for signing in could not be downloaded. Check your internet connection "
                    "and try again."
                )
            await asyncio.sleep(1.0)

    async def _release(self):
        """Pause every bridge on the profile, let a request in flight finish (up to `release_wait_s`), close them."""
        sib = self.setup.profile_bridges(self.owner.profile)
        for b in sib:
            b.signin = self.provider
        self._paused = sib
        guests = [b for b in sib if b is not self.owner]
        held, end = [], time.time() + self.setup.release_wait_s
        try:
            for b in guests + [self.owner]:
                if b.lock.locked():
                    self._set("preparing", "Waiting for a chat in progress on this account to finish.")
                try:
                    await asyncio.wait_for(b.lock.acquire(), max(0.1, end - time.time()))
                    held.append(b)
                except TimeoutError:
                    pass  # it goes anyway: that request is skipped without penalty (Bridge._guarded)
            for b in guests + [self.owner]:
                await b._shutdown()
        finally:
            for b in held:
                b.lock.release()

    def _resume(self):
        for b in self._paused:
            if b.signin == self.provider:
                b.signin = None
        self._paused = []

    async def _watch(self, spec, before: dict) -> str:
        """Until the session cookie is new or changed ("cookie"), the window is closed ("closed"), or the time is up
        ("timeout")."""
        deadline = time.time() + float(self.bridge.o.get("login_timeout_s") or 900)
        seen_at, next_read = None, 0.0
        while True:
            if not self.window.alive() or await asyncio.to_thread(self.window.closed_by_hand):
                return "closed"
            now = time.time()
            if now >= deadline:
                return "timeout"
            if spec and spec.get("names") and seen_at is None and now >= next_read:
                next_read = now + self.setup.cookie_poll_s
                got = await asyncio.to_thread(session_cookies, self.owner.profile, spec)
                if any(before.get(k) != v for k, v in got.items()):
                    seen_at = now
            if seen_at is not None and now - seen_at >= self.setup.grace_s:  # let the site's redirect land
                return "cookie"
            await asyncio.sleep(self.setup.poll_s)

    async def _verify(self):
        r = await self.bridge.check("auto", close=self.setup.close_after)
        if self.setup.close_after and self.owner is not self.bridge:
            await self.owner.release()
        name, kind = self.name, r.get("kind")
        if r.get("ok"):
            self.setup.cleared(self.provider)
            if r.get("needs_confirmation"):  # connected, and not used until the person has chosen
                return self._set("signed_in", plain_reason({**r, "kind": r["needs_confirmation"]}, self.bridge))
            return self._set("signed_in")
        if kind in ("limit", "busy", "page"):
            return self._set(
                "signed_in",
                f"Signed in. {name} says this account is limited right now; it will be used when that clears.",
            )
        if kind == "login":
            why = {
                "closed": "The window was closed before sign-in finished.",
                "timeout": f"The sign-in window was closed after {_duration(self.bridge.o.get('login_timeout_s'))} "
                "because sign-in did not finish. Try again.",
            }.get(self.how, f"{name} still asks to sign in. Try again, and wait until the chat page appears.")
            return self._set("failed", why)
        why = {
            "challenge": f"{name} asked for a human check (such as a captcha). Sign in again, complete the check in "
            "the window, then close it.",
            "tempchat": f"You are signed in, but {name} does not offer temporary chats on this account, so it cannot "
            "be used here.",
            "browser": "The background browser could not start for this account. Try again in a minute.",
            "setup": "The background browser could not start for this account. Try again in a minute.",
        }.get(kind, f"{name} did not finish loading. Check your internet connection and try again.")
        self._set("failed", why)


# ---- a page opened for the person in an account's signed-in profile ------------------------------------------------
class OpenFlow(LoginFlow):
    """POST /web/open: one ordinary, visible window on an account's profile at a page the person asked for (a pool
    provider's key page, where the profile's sign-in already counts: Google AI Studio with the Gemini account). The
    same window as a sign-in's, with the same rules: every bridge on the profile is paused and its background
    browser closed while it is open, one window per profile, and nothing drives it. It has no debugging port: the
    router cannot click, type, read or copy anything in it. It ends when the person closes it, on cancel, or after
    `open_timeout_s`; nothing is checked afterwards.

        idle ──POST──> preparing ──> open ──(window closed | timeout)──> closed        any failure ──> failed"""

    def __init__(self, setup: WebSetup, provider: str, account: str, url: str, bounds: dict | None = None):
        super().__init__(setup, provider, account, bounds)
        self.url = url

    def view(self) -> dict:
        return {**super().view(), "url": self.url}

    async def run(self):
        try:
            self._set("preparing")
            app = await self._app()
            await self._release()
            if browser_host.window_pid(self.owner.profile):
                raise SetupError(
                    "This account's browser is still open in another window. Close that window and try again."
                )
            self.window = SigninWindow(app, self.owner.profile, self.url, self.bounds, self.setup.launch,
                                       self.setup.extra_args)  # fmt: skip
            await self.window.open()
            self._set("open")
            limit = float(self.bridge.o.get("open_timeout_s") or 1800)
            deadline = time.time() + limit
            while self.window.alive() and not await asyncio.to_thread(self.window.closed_by_hand):
                if time.time() >= deadline:
                    self.how = "timeout"
                    break
                await asyncio.sleep(self.setup.poll_s)
            else:
                self.how = "closed"
            await self.window.close()
            self._set("closed", f"The window was closed after {_duration(limit)}." if self.how == "timeout" else None)
        except asyncio.CancelledError:
            raise
        except SetupError as e:
            self._set("failed", str(e))
        except Exception as e:  # noqa: BLE001 - never leave a flow hanging in a working state
            print(f"web setup: opening a page for {self.provider}/{self.account} failed: {e!r}", file=sys.stderr)
            self._set("failed", "Something went wrong while opening the window. Try again.")
        finally:
            if self.window and self.window.pid:
                await self.window.close()
            self._resume()


# ---- everything the endpoints need --------------------------------------------------------------------------------
class WebSetup:
    """The /web/* endpoints' state: sign-in flows per provider + account, and account slots. `launch`,
    `extra_args` and `app` are for tests (a headless window started directly); `close_after` is for the command
    line, which must not leave a background browser running when it exits."""

    def __init__(self, router, launch: str = "open", extra_args=(), app=None, close_after: bool = False):
        self.router = router
        self.launch, self.extra_args, self.app, self.close_after = launch, list(extra_args), app, close_after
        self.poll_s, self.cookie_poll_s, self.grace_s, self.release_wait_s = 1.0, 2.0, 3.0, 120.0
        self.flows: dict[tuple[str, str], LoginFlow] = {}
        self.opens: dict[tuple[str, str], OpenFlow] = {}  # pages opened for the person (POST /web/open)

    # -- providers and bridges
    def providers(self) -> list[str]:
        return [n for n, p in self.router.catalog.providers.items() if p.type == "chatgpt_web"]

    def _hosts_first(self) -> list[str]:
        names = self.providers()
        share = {n: self.router.catalog.providers[n].options.get("share") for n in names}
        return [n for n in names if not share[n]] + [n for n in names if share[n]]

    def pool(self, provider: str):
        if provider not in self.providers():
            raise KeyError(f"There is no web provider called {provider}.")
        return self.router.bridge(provider)

    def accounts(self) -> list[str]:
        names = self.providers()
        if not names:
            return []
        return list(self.router.catalog.providers[names[0]].options.get("accounts") or ["main"])

    def bridges_of(self, provider: str) -> dict[str, Bridge]:
        b = self.pool(provider)
        return dict(b.bridges) if isinstance(b, BridgePool) else {"main": b}

    def bridge_for(self, provider: str, account: str | None) -> Bridge:
        bs = self.bridges_of(provider)
        account = account or "main"
        if account not in bs:
            raise KeyError(f"There is no account called {account}.")
        return bs[account]

    def profile_bridges(self, profile: Path) -> list[Bridge]:
        return [b for n in self._hosts_first() for b in self.bridges_of(n).values() if b.profile == profile]

    def cleared(self, provider: str):
        """After a sign-in: the router forgets the cooldown it gave this provider's models."""
        for m in self.router.catalog.models.values():
            if m.provider == provider:
                self.router.state.health.pop(m.id, None)

    # -- sign-in
    def _active_on(self, profile: Path) -> LoginFlow | None:
        both = [*self.flows.values(), *self.opens.values()]
        return next((f for f in both if f.state in ACTIVE and f.owner.profile == profile), None)

    async def start(self, provider: str, account: str | None, bounds: dict | None = None) -> dict:
        account = account or "main"
        b = self.bridge_for(provider, account)
        busy = self._active_on((b.host or b).profile)
        if isinstance(busy, OpenFlow):
            raise SetupError("A window is already open on this account. Close it first.")
        if busy:
            what = "this" if busy.provider == provider else f"the {busy.name}"
            raise SetupError(f"A sign-in window is already open for {what} sign-in on this account. Finish or "
                             "cancel it first.")  # fmt: skip
        flow = LoginFlow(self, provider, account, bounds)
        self.flows[(provider, account)] = flow
        flow.task = asyncio.get_running_loop().create_task(flow.run())
        for _ in range(100):  # answer once the window is up (or it waits for the browser, or it failed)
            if flow.state not in ("idle", "preparing") or flow.task.done():
                break
            if flow.state == "preparing" and flow.reason and "browser ready" in flow.reason:
                break
            await asyncio.sleep(0.1)
        return flow.view()

    def login_state(self, provider: str, account: str | None) -> dict:
        account = account or "main"
        self.bridge_for(provider, account)  # KeyError for an unknown one
        f = self.flows.get((provider, account))
        return f.view() if f else {"state": "idle", "reason": None, "provider": provider, "account": account}

    async def cancel(self, provider: str, account: str | None) -> dict:
        account = account or "main"
        self.bridge_for(provider, account)
        f = self.flows.pop((provider, account), None)
        if f and f.task and not f.task.done():
            f.task.cancel()
            try:
                await f.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        return {"state": "idle", "reason": None, "provider": provider, "account": account}

    async def show(self, provider: str, account: str | None) -> dict:
        """POST /web/login/show: the open sign-in window back to the front."""
        account = account or "main"
        self.bridge_for(provider, account)
        f = self.flows.get((provider, account))
        if not f or f.state != "open" or not f.window:
            raise SetupError("There is no sign-in window open for this account.")
        shown = await f.window.show()
        return {**f.view(), "shown": shown}

    async def close(self):
        for key in list(self.flows):
            await self.cancel(*key)
        for key in list(self.opens):
            await self.open_cancel(*key)

    # -- a pool provider's key page, opened for the person in a profile that is already signed in to that site
    @staticmethod
    def key_pages() -> set[str]:
        """The only pages that may be opened: the `keys_url` / `signup_url` values of pool_help.yaml."""
        return {p[k] for p in pool_help.load()["providers"] for k in ("keys_url", "signup_url") if p.get(k)}

    def _covers(self, b: Bridge, url: str) -> bool:
        """Is the page on a site this provider's sign-in is also the sign-in of (`opens` in SITES: Google's pages
        for Gemini)?"""
        host = (urlparse(url).hostname or "").lower()
        domains = [str(d).lower().lstrip(".") for d in b.o.get("opens") or b.site_info.get("opens") or []]
        return bool(host) and any(host == d or host.endswith("." + d) for d in domains)

    def open_with(self, url: str | None) -> list[dict]:
        """The signed-in accounts whose profile can open this page: [{provider, account}], from the accounts' state
        as it is now."""
        out = []
        if not url or url not in self.key_pages():
            return out
        for n in self.providers():
            for a in self.accounts():
                b = self.bridges_of(n).get(a)
                if b and self._covers(b, url) and b.load().get("signed_in") is True:
                    out.append({"provider": n, "account": a})
        return out

    def help_view(self, entry: dict) -> dict:
        """One /pool/help provider entry, with `open_with` when a signed-in account can open its key page."""
        who = self.open_with(entry.get("keys_url"))
        return {**entry, "open_with": who} if who else entry

    async def open_page(self, provider: str, account: str | None, url, bounds: dict | None = None) -> dict:
        account = account or "main"
        b = self.bridge_for(provider, account)
        if not isinstance(url, str) or url not in self.key_pages():
            raise BadRequest("Only a provider's own key page can be opened here.")
        if not self._covers(b, url):
            raise SetupError(f"That page does not use the {b.display_name} sign-in, so it cannot be opened with it.")
        if b.load().get("signed_in") is not True:
            raise SetupError(f"This account is not signed in to {b.display_name}. Sign in first.")
        busy = self._active_on((b.host or b).profile)
        if busy:
            raise SetupError("A window is already open on this account. Close it first.")
        flow = OpenFlow(self, provider, account, url, bounds)
        self.opens[(provider, account)] = flow
        flow.task = asyncio.get_running_loop().create_task(flow.run())
        for _ in range(100):  # answer once the window is up (or it waits for the browser, or it failed)
            if flow.state not in ("idle", "preparing") or flow.task.done():
                break
            if flow.state == "preparing" and flow.reason and "browser ready" in flow.reason:
                break
            await asyncio.sleep(0.1)
        return flow.view()

    def open_state(self, provider: str, account: str | None) -> dict:
        account = account or "main"
        self.bridge_for(provider, account)
        f = self.opens.get((provider, account))
        if f:
            return f.view()
        return {"state": "idle", "reason": None, "provider": provider, "account": account, "url": None}

    async def open_cancel(self, provider: str, account: str | None) -> dict:
        account = account or "main"
        self.bridge_for(provider, account)
        f = self.opens.pop((provider, account), None)
        if f and f.task and not f.task.done():
            f.task.cancel()
            try:
                await f.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        return {"state": "idle", "reason": None, "provider": provider, "account": account, "url": None}

    # -- a saved sign-in and automatic sign-in (web_signin.py), and the paid-plan confirmation
    def account_view(self, provider: str, account: str | None) -> dict:
        """One account of one provider, as GET /web/accounts lists it."""
        account = account or "main"
        return {"provider": provider, **self._account(provider, account, self.bridge_for(provider, account))}

    def _signin_call(self, fn, *a):
        try:
            return fn(*a)
        except web_signin.Invalid as e:
            raise BadRequest(str(e)) from None
        except web_signin.Refused as e:
            raise SetupError(str(e)) from None

    def autologin_save(self, provider: str, account: str | None, body: dict) -> dict:
        """PUT /web/accounts/{provider}/{account}/autologin {agree: true, username, password}: write-only for the
        password, which goes straight to the system's credential store."""
        b = self.bridge_for(provider, account)
        self._signin_call(web_signin.save_signin, b, body.get("agree"), body.get("username"), body.get("password"))
        return self.account_view(provider, account)

    def autologin_enable(self, provider: str, account: str | None, body: dict) -> dict:
        b = self.bridge_for(provider, account)
        self._signin_call(web_signin.set_enabled, b, body.get("enabled"))
        return self.account_view(provider, account)

    def autologin_forget(self, provider: str, account: str | None) -> dict:
        web_signin.forget(self.bridge_for(provider, account))
        return self.account_view(provider, account)

    async def autologin_try(self, provider: str, account: str | None, wait: bool = True) -> dict:
        """POST .../autologin/try: one attempt now, under the same rules as an automatic one (409 with the reason
        when they do not allow it). Answers when it is over, with the account and `try` {at, result, detail}; with
        wait=false it answers at once and the attempt goes on (the account's autologin.running, then last_try)."""
        b = self.bridge_for(provider, account)
        why = web_signin.refusal(b, manual=True)
        if why:
            raise SetupError(why)
        if web_signin.running(b):
            raise SetupError("A sign-in attempt is already running for this account.")
        if not wait:

            async def go():
                try:
                    await web_signin.attempt(b, "manual")
                except web_signin.Refused:
                    pass
                except Exception as e:  # noqa: BLE001 - never the error's text
                    print(f"web setup: the sign-in attempt ended with {type(e).__name__}", file=sys.stderr)

            b._autologin_task = asyncio.get_running_loop().create_task(go())
            await asyncio.sleep(0)
            return {**self.account_view(provider, account), "try": None}
        try:
            last = await web_signin.attempt(b, "manual")
        except web_signin.Refused as e:
            raise SetupError(str(e)) from None
        return {**self.account_view(provider, account), "try": last}

    def confirm(self, provider: str, account: str | None, body: dict) -> dict:
        """POST /web/accounts/{provider}/{account}/confirm {confirm: "paid_plan"}: use this account although it is
        on a paid plan."""
        b = self.bridge_for(provider, account)
        if body.get("confirm") != "paid_plan":
            raise BadRequest('Send confirm: "paid_plan".')
        if not b.confirm_plan():
            raise SetupError("There is nothing to confirm for this account.")
        self.cleared(provider)
        return self.account_view(provider, account)

    # -- per-provider settings (provider_settings.py): on / off, daily cap
    async def update_provider(self, provider: str, changes: dict) -> dict:
        """PATCH /web/providers/{provider}: change and keep its settings; answers with the provider as
        GET /web/accounts lists it. A browser no enabled site needs any more is closed."""
        self.pool(provider)  # KeyError for one that is not a web provider
        if not isinstance(changes, dict):
            raise SetupError("Send the changes as a JSON object.")
        if "enabled" in changes and enabled_by(self.router.catalog.providers[provider].options) == "env-off":
            raise SetupError("The web chats are turned off on this router (PCORE_CHATGPT_WEB=0), so they cannot be "
                             "turned on from the app.")  # fmt: skip
        try:
            provider_settings.update(self.router, provider, changes)
        except provider_settings.SettingError as e:
            raise SetupError(str(e)) from None
        for host in provider_settings.unused_hosts(self.router):
            await self.router.bridges[host].release()
        return self.provider_view(provider)

    # -- accounts
    async def add_account(self, account: str) -> dict:
        account = str(account or "").strip()
        if not ACCOUNT_ID.fullmatch(account) or account.lower() == "default":
            raise SetupError("An account name can use letters, digits, dot, dash and underscore (up to 32).")
        if account in self.accounts():
            raise SetupError(f"There is already an account called {account}.")
        if not self.providers():
            raise SetupError("No web chat providers are set up.")
        ov = load_overrides()
        ov["removed"] = [a for a in ov["removed"] if a != account]
        if account not in ov["added"]:
            ov["added"].append(account)
        save_overrides(ov)
        new = self.accounts() + [account]
        self._set_accounts(new)
        for name in self._hosts_first():
            b = self.router.bridges.get(name)
            if b is None:
                continue  # made with the new list on first use
            share = self.router.catalog.providers[name].options.get("share")
            host = self.bridges_of(share).get(account) if share else None
            if isinstance(b, BridgePool):
                b.add(account, host)
            else:  # one account so far: becomes a pool, keeping the bridge (and any open browser) it had
                opts = {**b.o, "accounts": new}
                hosts = self.bridges_of(share) if share else {}
                pool = BridgePool(b.base_url, opts, hosts=hosts)
                pool.bridges["main"] = b
                self.router.bridges[name] = pool
        return {"account": account, "added": True, "accounts": self.accounts(),
                "profile": str(self.bridge_for(self.providers()[0], account).profile)}  # fmt: skip

    async def remove_account(self, account: str) -> dict:
        if account not in self.accounts():
            raise KeyError(f"There is no account called {account}.")
        left = [a for a in self.accounts() if a != account]
        if not left:
            raise SetupError("This is the only account, so it cannot be removed.")
        profile = self.bridge_for(self.providers()[0], account).profile
        for p in self.providers():
            if (p, account) in self.flows:
                await self.cancel(p, account)
            if (p, account) in self.opens:
                await self.open_cancel(p, account)
            web_signin.forget(self.bridge_for(p, account))  # a saved sign-in goes with the account
        ov = load_overrides()
        if account in ov["added"]:
            ov["added"].remove(account)
        else:
            ov["removed"].append(account)
        save_overrides(ov)
        for name in reversed(self._hosts_first()):  # guests' tabs first, then the browser that holds them
            b = self.router.bridges.get(name)
            if isinstance(b, BridgePool):
                await b.remove(account)
        self._set_accounts(left)
        return {
            "account": account,
            "removed": True,
            "accounts": self.accounts(),
            "profile_kept": str(profile),
            "note": "The account was removed from the app. Its browser data (sign-ins, cookies) was not deleted "
            f"and stays in {profile}; adding an account with the same name uses it again.",
        }

    def _set_accounts(self, accounts: list[str]):
        for n in self.providers():
            self.router.catalog.providers[n].options["accounts"] = list(accounts)

    # -- what GET /web/accounts shows
    def browser_view(self) -> dict:
        hosts = [n for n in self._hosts_first() if not self.router.catalog.providers[n].options.get("share")]
        o = self.pool(hosts[0]).o if hosts else {}
        mode = str(o.get("browser") or "chrome").lower()
        source = str(o.get("browser_source") or "cft").lower()
        st = browser_host.prep_status()
        out = {"mode": mode, "source": source, "ready": False, "version": None, "preparing": False, "error": None,
               "progress": None, "stage": None}  # fmt: skip
        if mode == "chrome" or source == "chrome":
            v = browser_host.installed_chrome_version()
            out.update(ready=bool(v), version=v)
            if not v:
                out["error"] = "Google Chrome was not found on this Mac."
            return out
        profiles = [b.profile for n in hosts for b in self.bridges_of(n).values()]
        need = max([browser_host.required_major(p) for p in profiles] or [browser_host.required_major()])
        rec = browser_host.current(need=need)
        out.update(
            ready=bool(rec),
            version=rec["version"] if rec else None,
            preparing=st["preparing"],
            progress=st["progress"],
            stage=st["stage"] if st["preparing"] else None,
            error=None if rec or st["preparing"] else st.get("error"),
        )
        return out

    def prepare_browser(self) -> dict:
        hosts = [n for n in self._hosts_first() if not self.router.catalog.providers[n].options.get("share")]
        profiles = [b.profile for n in hosts for b in self.bridges_of(n).values()]
        prof = max(profiles, key=browser_host.required_major) if profiles else None
        if not browser_host.current(need=browser_host.required_major(prof)):
            if not browser_host.prepare(prof):
                raise SetupError("The app's own browser is only available on macOS.")
        return self.browser_view()

    def view(self) -> dict:
        return {
            "browser": self.browser_view(),
            "accounts": self.accounts(),
            "providers": [self.provider_view(n) for n in self.providers()],
        }

    def provider_view(self, n: str) -> dict:
        """One provider as GET /web/accounts lists it."""
        cat = self.router.catalog
        p = cat.providers[n]
        bs = self.bridges_of(n)
        first = next(iter(bs.values()))
        models = [
            {
                "id": m.id,
                "label": m.label or m.model,
                "tier": cat.tier_of(m),
                "model": m.model,
                "accounts": m.accounts,
            }  # fmt: skip
            for m in cat.models.values()
            if m.provider == n
        ]
        accts = [self._account(n, a, bs[a]) for a in self.accounts() if bs.get(a)]
        return {
            "id": n,
            "name": first.display_name,
            "url": first.base_url,
            "enabled": p.usable,
            # what decides `enabled`: "app" (turned on or off here), "env" (PCORE_CHATGPT_WEB=1), "catalog", or
            # "env-off" (PCORE_CHATGPT_WEB=0: off, and cannot be turned on from the app)
            "enabled_by": enabled_by(p.options),
            "daily_cap": int(first.o["daily_cap"]),  # per account
            "shares_browser_with": p.options.get("share"),
            "models": models,
            "accounts": accts,
        }

    def _account(self, n: str, a: str, b: Bridge) -> dict:
        s = b.status()
        reason = None
        if s["blocked"]:
            reason = plain_reason(s, b)
        elif s["needs_confirmation"]:  # connected, idle until the person has chosen
            reason = plain_reason({**s, "kind": s["needs_confirmation"]}, b)
        elif s.get("notice"):  # in use; the saved sign-ins were lost and a backup was put back
            reason = s["notice"].get("text")
        return {
            "id": a,
            "signed_in": s["signed_in"],
            "checked_at": s["checked_at"],
            "blocked": s["blocked"],
            "reason": reason,
            "blocked_for_s": s["blocked_for_s"],
            "today": s["today"],
            "daily_cap": s["daily_cap"],
            "email": s.get("email"),
            # the account's plan where the site is checked for it (ChatGPT): "free", "paid" (plan_label: the site's
            # own word, e.g. "plus") or "unknown" (it could not be read; the account is used as it is); null elsewhere
            "plan": s["plan"],
            "plan_label": s["plan_label"],
            "plan_note": "The plan of this account could not be read, so it is used as it is."
            if s["plan"] == "unknown" and s["signed_in"]
            else None,
            "needs_confirmation": s["needs_confirmation"],  # null or "paid_plan" (POST .../confirm)
            "notice": (s.get("notice") or {}).get("kind"),  # null or "cookies_lost" (a backup was put back; `reason`)
            "autologin": web_signin.view(b),
            "signing_in": s["signing_in"],
            "window_open": s["window_open"],
            "login": {k: v for k, v in self.login_state(n, a).items() if k in ("state", "reason")},
        }


def _duration(s) -> str:
    s = int(float(s or 900))
    return f"{s // 60} minute{'s' if s // 60 != 1 else ''}" if s >= 60 else f"{s} second{'s' if s != 1 else ''}"


def plain_reason(s: dict, b: Bridge) -> str:
    """Why an account is out for now, in words for the person using the app (the bridge's own reason names
    internals)."""
    name = b.display_name
    mins = max(1, (s.get("blocked_for_s") or 0) // 60)
    if s.get("kind") == "paid_plan":
        label = f" ({s['plan_label']})" if s.get("plan_label") else ""
        return (
            f"This {name} account is on a paid plan{label}. Using it here still spends that account's own usage, "
            "likely faster than Codex would. It is connected but not used until you choose: use it anyway, or "
            "connect a different account."
        )
    if s.get("kind") == "cookies_lost":  # written for the person already (cookie_guard.py)
        return s.get("reason") or cookie_guard.LOST_NO_BACKUP
    if s.get("kind") == "login":
        auto = web_signin.note(b)  # where automatic sign-in stands, when it was tried or has stopped
        if auto:
            return f"Not signed in to {name}. {auto}"
        if web_signin.view(b)["enabled"]:
            return f"Not signed in to {name}. Automatic sign-in is on and will try; you can also sign in yourself."
    return {
        "login": f"Not signed in to {name}. Sign in again.",
        "challenge": f"{name} asked for a human check (such as a captcha). It will be tried again in about "
        f"{mins} minutes, or sign in again to deal with it now.",
        "limit": f"{name} reports a usage limit on this account. It will be used again in about {mins} minutes.",
        "busy": f"{name} is turning chats away for the moment. It will be tried again in about {mins} minutes.",
        "page": f"The {name} page did not load or respond in time on this computer (the site did not refuse "
        f"anything). It will be tried again in about {_duration(s.get('blocked_for_s'))}.",
        "tempchat": f"Temporary chats are not available on this {name} account, so it is not used.",
        "model": f"The chosen model is not available on this {name} account right now.",
        "closed": "The browser window was closed. It will reopen when needed.",
        "cap": f"Today's limit of {s.get('daily_cap')} chats on this account is used up. It resets at midnight.",
        "browser": "The browser could not start. It will be tried again shortly.",
        "clock": "This computer's clock looks wrong, and starting the browser on a wrong clock can erase the saved "
        f"sign-ins, so it was not started. It will be tried again in about {mins} minutes.",
        "disk": "This computer's disk is almost full, and starting the browser on a full disk can erase the saved "
        f"sign-ins, so it was not started. Free some space; it will be tried again in about {mins} minutes.",
        "setup": "The browser could not start. It will be tried again shortly.",
    }.get(s.get("kind"), f"{name} cannot be used on this account for the moment. It will be tried again later.")


# ---- the command line: `provenance-core weblogin [provider] [--account b]` ------------------------------------------
def cli_login(call, provider: str, account: str | None, out=print, sleep=time.sleep, poll_s: float = 1.0) -> int:
    """Drive the server's sign-in flow: start it, report each state change, stop it on Ctrl+C. `call(method,
    path, body=None)` -> (status, json)."""
    q = f"provider={provider}&account={account or 'main'}"
    body = {"provider": provider, "account": account or "main"}
    status, r = call("POST", "/web/login", body)
    if status != 200:
        out(f"could not start the sign-in: {(r or {}).get('detail') or (r or {}).get('reason') or status}")
        return 1
    last = None
    try:
        while True:
            state, reason = r.get("state"), r.get("reason")
            if (state, reason) != last:
                last = (state, reason)
                if state == "open":
                    out("A sign-in window is open: sign in there. It closes by itself once you are in (or close it "
                        "yourself when you are done).")  # fmt: skip
                elif state == "signed_in":
                    out("Signed in and checked: ready." + (f" ({reason})" if reason else ""))
                    return 0
                elif state == "failed":
                    out(f"Not signed in: {reason}")
                    return 1
                elif state == "idle":
                    out("The sign-in was cancelled.")
                    return 1
                else:
                    out(f"{state}..." + (f" {reason}" if reason else ""))
            sleep(poll_s)
            status, r = call("GET", "/web/login?" + q, None)
            if status != 200:
                out(f"lost track of the sign-in ({status})")
                return 1
    except KeyboardInterrupt:
        call("POST", "/web/login/cancel", body)
        out("cancelled")
        return 130


def local_call(cat):
    """`call` for cli_login when no server is running: the same flow in this process (the window, then a check in
    a background browser that is closed again before the command ends)."""
    from ..routing.router import Router

    loop = asyncio.new_event_loop()
    setup = WebSetup(Router(cat), close_after=True)

    def call(method, path, body):
        try:
            if method == "POST" and path == "/web/login":
                return 200, loop.run_until_complete(setup.start(body["provider"], body["account"]))
            if method == "POST" and path == "/web/login/cancel":
                return 200, loop.run_until_complete(setup.cancel(body["provider"], body["account"]))
            q = dict(x.split("=", 1) for x in path.split("?", 1)[1].split("&"))
            loop.run_until_complete(asyncio.sleep(0))  # let the flow run between polls
            return 200, setup.login_state(q["provider"], q["account"])
        except SetupError as e:
            return 409, {"detail": str(e)}
        except KeyError as e:
            return 404, {"detail": str(e).strip("'\"")}

    def sleep(s):
        loop.run_until_complete(asyncio.sleep(s))

    return call, sleep
