"""A saved sign-in for a web-chat account, and signing in again with it when the site has signed the account out.

Off unless the person turned it on for that provider + account, with their agreement (PUT .../autologin, see
web_setup.py). What is kept where:

- the password: only in the operating system's credential store (credstore.py). It is read from there at the moment
  of an attempt, typed into the site's own password field, and dropped. It is never written to a state file, the
  settings, a log line, an event, an exception, a debug dump or an API answer;
- the user name / e-mail address, whether it is on, and the record of attempts: in the account's state file, under
  "autologin".

An attempt (`attempt`) happens only when the bridge has established that the account is signed out (Bridge._block
with kind "login", after its doubt logic), or when the person presses "try now"; never while a sign-in window is
open; at most `autologin_max_per_day` times a day per account, with a gap that doubles after each try
(`autologin_gap_s`). It runs in the background browser and profile the bridge already uses, in a tab of its own that
is closed afterwards: the site's sign-in page is opened, the saved name and password are typed into the form the
site's `signin` description names (chatgpt_web.SITES / CHATGPT_SITE), the form is sent, and the result is confirmed
with the same check the bridge uses for any page (Bridge._open_chat).

Hard stops. The page is looked at before every step, and the attempt ends at once, touching nothing more, when it
shows: a captcha or other human check, a prompt for a code (two-step, e-mail, SMS), a passkey prompt, a "suspicious
sign-in" / locked-account notice, a terms or consent screen, a page on a host that is not the site's own (single
sign-on with another identity provider), or anything the description does not expect after something was typed. The
result is then `needs_person`, and no further automatic attempt is made until the person has signed in themselves
(the sign-in window) or saved the sign-in again. Nothing here solves, clicks or works around a check, reads a code
from anywhere, or tries again in a loop. A site that says the name or password is wrong turns automatic sign-in off
for the account until a new password is saved, so that a stale password can never lock the account.

Results: `ok`, `wrong_password`, `needs_person` (with `detail` in plain words), `error` (the page did not load or
could not be driven; only this one is tried again later, within the limits above). Each attempt is one dated line in
<home>/web-events.jsonl (kind "autologin") and is shown by GET /web/accounts."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import time
from urllib.parse import urlparse

from . import credstore

MAX_USERNAME = 254

# What the person reads for each hard stop.
STOPS = {
    "captcha": "The site asked for a human check (a captcha).",
    "code": "The site asked for a verification code.",
    "passkey": "The site asked for a passkey or security key.",
    "suspicious": "The site showed a security notice about this sign-in or account.",
    "terms": "The site showed terms or a consent screen to accept.",
    "sso": "The site sent the sign-in to another sign-in provider.",
    "unexpected": "The site showed a page this app did not expect.",
    "still_out": "The form was accepted, but the site still asks to sign in.",
}

# Looks at a sign-in page: which of the described controls are showing, and whether the page shows something that
# must be left to a person. Returns flags only: no text of the page and no field value ever leaves the page.
LOOK_JS = r"""(cfg) => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                      return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const all = spec => { if (!spec) return []; const css = typeof spec === 'string' ? spec : spec.css;
    const re = spec.text ? new RegExp(spec.text, 'i') : null;
    return [...document.querySelectorAll(css)].filter(el => vis(el) && (!re || re.test((el.innerText || el.value || '').trim()))); };
  const out = {host: location.hostname, path: location.pathname};
  for (const k of ['start', 'email', 'continue', 'password', 'submit']) out[k] = all(cfg[k]).length > 0;
  const text = document.body ? (document.body.innerText || '') + '\n' + document.title : document.title;
  let captcha = /just a moment|verify (?:that )?you are (?:a )?human|confirm you(?:'|’)re (?:not a robot|human)|are you a robot|i(?:'|’)m not a robot|complete the captcha|checking your browser|you have been blocked|access denied|slide to (?:verify|complete)|click to start verification|security check/i.test(text);
  if (!captcha) for (const f of document.querySelectorAll('iframe')) {
    const src = (f.getAttribute('src') || '') + ' ' + (f.getAttribute('title') || '');
    const r = f.getBoundingClientRect();
    if (/challenges\.cloudflare\.com|arkoselabs|funcaptcha|hcaptcha|recaptcha|captcha|challenge/i.test(src) && vis(f) && r.width > 120 && r.height > 40) { captcha = true; break; }
  }
  if (!captcha) captcha = [...document.querySelectorAll('.cf-turnstile, [data-sitekey], [class*="captcha" i], [id*="captcha" i]')].some(vis);
  out.captcha = captcha;
  out.codeInput = [...document.querySelectorAll('input[autocomplete="one-time-code"], input[name*="otp" i], input[id*="otp" i], input[name*="code" i]:not([type="hidden"])')].some(vis);
  out.codeText = /enter (?:the |your )?(?:\w+[- ]digit |verification |security |one[- ]time |login |sign[- ]in )?(?:code|passcode)|check your (?:e-?mail|inbox|phone)|we(?:(?:'|’)ve| have)? (?:just )?(?:sent|e-?mailed|texted)|two[- ](?:step|factor)|2[- ](?:step|factor)|multi[- ]factor|authenticator app|verification code (?:was |has been )?sent|verify your (?:e-?mail|phone)/i.test(text);
  out.passkey = /passkey|security key|touch id|face id|windows hello|use your (?:fingerprint|face)/i.test(text);
  out.suspicious = /suspicious (?:activity|sign[- ]?in|log[- ]?in)|unusual (?:activity|sign[- ]?in|log[- ]?in)|verify (?:that )?it(?:'|’)s (?:really )?you|verify your identity|confirm your identity|account (?:has been |is |was )?(?:locked|disabled|suspended|deactivated)|too many (?:failed )?(?:attempts|tries|requests)/i.test(text);
  out.terms = /(?:we(?:(?:'|’)ve| have)?|has|have) updated (?:our|the) (?:terms|privacy)|(?:review|accept) (?:and accept )?(?:our |the )?(?:updated |new )?terms/i.test(text)
    || [...document.querySelectorAll('button, [role="button"], input[type="submit"]')].some(el => vis(el) && /^(?:i )?(?:agree|accept)(?: and continue| all)?$/i.test((el.innerText || el.value || '').trim()));
  const bad = new RegExp(cfg.failure || "(?:incorrect|wrong|invalid) (?:e-?mail(?: address)?|user ?name|password|credentials|login)|password is (?:incorrect|wrong|invalid)|(?:e-?mail|password)[^.\\n]{0,40}(?:incorrect|not match|wrong|invalid)|(?:doesn|don|didn)(?:'|’)t match|(?:couldn(?:'|’)t|could not|can(?:'|’)t|cannot) find (?:your|an) account|no account (?:found|exists)|account (?:does not|doesn(?:'|’)t) exist|not registered", 'i');
  out.failure = bad.test(text) || [...all(cfg.email), ...all(cfg.password)].some(el => el.getAttribute('aria-invalid') === 'true');
  return out;
}"""

# The first showing control a description names (the same rule LOOK_JS reports by), for typing into or pressing.
FIND_JS = r"""(spec) => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                      return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const css = typeof spec === 'string' ? spec : spec.css; const re = spec.text ? new RegExp(spec.text, 'i') : null;
  return [...document.querySelectorAll(css)].find(el => vis(el) && (!re || re.test((el.innerText || el.value || '').trim()))) || null;
}"""


class Refused(Exception):
    """An attempt (or a change) that the rules do not allow now; the text is written for the person."""


class Invalid(Exception):
    """A request that is not acceptable as sent (no agreement, no name, no password)."""


# ---- what is kept ----------------------------------------------------------------------------------------------------
def config(b) -> dict | None:
    """The sign-in form's description for the bridge's site, or None when automatic sign-in is not offered there."""
    return b.o.get("signin") or b.site_info.get("signin")


def support(b) -> tuple[bool, str | None]:
    if not config(b):
        why = b.site_info.get("signin_unsupported")
        return False, why or f"Automatic sign-in is not available for {b.display_name}."
    if credstore.default() is None:
        return False, "This computer has no system password store this app can use, so a password cannot be saved."
    return True, None


def key(b) -> str:
    """The credential store's name for this account's password: site, account, and which router home it belongs to."""
    home = hashlib.sha256(str(b.home.resolve()).encode()).hexdigest()[:8]
    site = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(b.o.get("site") or "chatgpt"))
    acct = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(b.account or "main"))
    return f"{site}/{acct}@{home}"


def state(b) -> dict:
    return dict(b.load().get("autologin") or {})


def _save(b, **changes) -> dict:
    st = {**state(b), **changes}
    b.save(autologin=st)
    return st


def _day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def tries_today(b, st: dict | None = None) -> list[float]:
    st = state(b) if st is None else st
    today = _day(b.clock())
    return [float(t) for t in st.get("tries") or [] if _day(float(t)) == today]


def _gap(b, n: int) -> float:
    """How long after the n-th try of the day the next one may start: the base gap, doubled each time."""
    return float(b.o["autologin_gap_s"]) * 2 ** max(0, n - 1)


def _mins(s: float) -> str:
    m = max(1, int((s + 59) // 60))
    return f"{m} minute{'s' if m != 1 else ''}"


def blocked_by(b, manual: bool = False) -> str | None:
    """None when an attempt may start now, else why not, in words for the person."""
    ok, why = support(b)
    if not ok:
        return why
    st = state(b)
    name = b.display_name
    if not st.get("saved"):
        return "No sign-in is saved for this account."
    if st.get("hold") == "wrong_password":
        return (f"{name} did not accept the saved password, so automatic sign-in is off for this account. "
                "Save a new password to turn it back on.")  # fmt: skip
    if not st.get("enabled"):
        return "Automatic sign-in is turned off for this account."
    if st.get("hold") == "needs_person" and not manual:
        return f"Automatic sign-in stopped and waits for you. {(st.get('last_try') or {}).get('detail') or ''}".strip()
    if b.signing_in():
        return "A sign-in window is open for this account."
    tries = tries_today(b, st)
    cap = int(b.o["autologin_max_per_day"])
    if len(tries) >= cap:
        return (f"Automatic sign-in was tried {len(tries)} time{'s' if len(tries) != 1 else ''} today, which is the "
                "limit. It can be tried again tomorrow; you can sign in yourself at any time.")  # fmt: skip
    if tries and not st.get("gap_waived"):
        wait = tries[-1] + _gap(b, len(tries)) - b.clock()
        if wait > 0:
            return f"Automatic sign-in was tried a short while ago. The next try is possible in about {_mins(wait)}."
    return None


def refusal(b, manual: bool = False) -> str | None:
    """blocked_by, and before the limits: an account that is signed in needs no attempt."""
    ok, _ = support(b)
    s = b.load()
    if ok and (s.get("autologin") or {}).get("saved") and s.get("signed_in") is True:
        if (s.get("blocked_until") or 0) <= b.clock() or s.get("kind") != "login":
            return "This account is signed in, so there is nothing to do."
    return blocked_by(b, manual)


def note(b) -> str | None:
    """One sentence about where automatic sign-in stands, when there is something the person should know."""
    st = state(b)
    if not st.get("saved"):
        return None
    if st.get("hold") == "wrong_password" or (st.get("enabled") and st.get("hold") == "needs_person"):
        return blocked_by(b)
    if st.get("enabled") and len(tries_today(b, st)) >= int(b.o["autologin_max_per_day"]):
        return blocked_by(b)
    return None


def running(b) -> bool:
    return bool(getattr(b, "_autologin_running", False))


def view(b) -> dict:
    """The account's `autologin` in GET /web/accounts. Never the password: only whether one is saved."""
    ok, why = support(b)
    st = state(b)
    wait = None
    tries = tries_today(b, st)
    if tries and not st.get("gap_waived"):
        wait = max(0, int(tries[-1] + _gap(b, len(tries)) - b.clock())) or None
    return {
        "supported": ok,
        "reason": why,
        "enabled": bool(ok and st.get("enabled") and st.get("saved")),
        "saved": bool(st.get("saved")),
        "username": st.get("username"),
        "attempts_today": len(tries),
        "attempts_per_day": int(b.o["autologin_max_per_day"]),
        "last_try": st.get("last_try"),
        "history": list(st.get("history") or []),
        "paused": st.get("hold"),  # null, "wrong_password" or "needs_person"
        "note": note(b),
        "next_try_in_s": wait,
        "running": running(b),
    }


# ---- the person's choices ----------------------------------------------------------------------------------------------
def save_signin(b, agree, username, password) -> dict:
    """PUT: keep this sign-in and turn automatic sign-in on. Needs the person's agreement, said in the request."""
    if agree is not True:
        raise Invalid("Automatic sign-in needs your agreement: send agree: true.")
    ok, why = support(b)
    if not ok:
        raise Refused(why)
    if not isinstance(username, str) or not username.strip() or len(username) > MAX_USERNAME:
        raise Invalid("Send the e-mail address or user name of the account.")
    if not isinstance(password, str) or not password or len(password) > credstore.MAX_SECRET:
        raise Invalid("Send the password of the account.")
    try:
        credstore.default().set(key(b), password)
    except credstore.CredentialError as e:
        raise Refused(f"The password could not be saved: {e}.") from None
    _save(b, enabled=True, saved=True, username=username.strip(), agreed_at=int(b.clock()), hold=None,
          gap_waived=True)  # fmt: skip
    return view(b)


def set_enabled(b, enabled) -> dict:
    """PATCH: on or off, keeping what is saved."""
    if not isinstance(enabled, bool):
        raise Invalid("Send enabled: true or false.")
    st = state(b)
    if enabled:
        ok, why = support(b)
        if not ok:
            raise Refused(why)
        if not st.get("saved"):
            raise Refused("No sign-in is saved for this account. Save one first.")
        if st.get("hold") == "wrong_password":
            raise Refused(blocked_by(b))
    _save(b, enabled=enabled)
    return view(b)


def forget(b) -> dict:
    """DELETE: the password leaves the credential store, and the setting, the name and the record leave the state."""
    store = credstore.default()
    if store is not None:
        try:
            store.delete(key(b))
        except credstore.CredentialError:
            pass
    if b.load().get("autologin") is not None:
        b.save(autologin=None)
    return view(b)


# ---- one attempt ---------------------------------------------------------------------------------------------------------
def kick(b):
    """The bridge has just established that this account is signed out: start one attempt in the background, when
    the rules allow one."""
    task = getattr(b, "_autologin_task", None)
    if (task and not task.done()) or running(b) or blocked_by(b) is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def go():
        try:
            await attempt(b, "auto")
        except Refused:
            pass
        except Exception as e:  # noqa: BLE001 - never the error's text: it could quote what was typed
            print(f"web sign-in: the automatic attempt ended with {type(e).__name__}", file=sys.stderr)

    b._autologin_task = loop.create_task(go())


async def attempt(b, trigger: str = "auto") -> dict:
    """One sign-in attempt for the bridge's account -> {at, result, detail}. Raises Refused when the rules do not
    allow one now. `trigger`: "auto" (the bridge found the account signed out) or "manual" (the person asked)."""
    manual = trigger == "manual"
    why = refusal(b, manual)
    if why:
        raise Refused(why)
    if running(b):
        raise Refused("A sign-in attempt is already running for this account.")
    b._autologin_running = True
    try:
        try:  # after the request that found the account signed out (it holds the window until it has failed)
            await asyncio.wait_for(b.lock.acquire(), None if not manual else float(b.o["queue_wait_s"]))
        except TimeoutError:
            raise Refused("This account is busy with a chat. Try again in a moment.") from None
        try:
            why = refusal(b, manual)  # again, now that the window is ours: a sign-in window may have opened
            if why:
                raise Refused(why)
            st = state(b)
            _save(b, tries=tries_today(b, st) + [b.clock()], gap_waived=False)  # counted before anything is opened
            result, detail = await _signin(b, st.get("username") or "")
            return _record(b, trigger, result, detail)
        finally:
            b._arm_idle()
            b.lock.release()
    finally:
        b._autologin_running = False


def _record(b, trigger: str, result: str, detail: str | None) -> dict:
    last = {"at": int(b.clock()), "result": result, "detail": detail}
    changes: dict = {"last_try": last}
    changes["history"] = (list(state(b).get("history") or []) + [{**last, "trigger": trigger}])[-10:]
    if result == "ok":
        changes["hold"] = None
    elif result == "wrong_password":
        changes.update(hold="wrong_password", enabled=False)
    elif result == "needs_person":
        changes["hold"] = "needs_person"
    _save(b, **changes)
    b._event("autologin", f"{result}: {detail}" if detail else result, result=result, trigger=trigger)
    if result == "ok" and b.on_revived:
        try:
            b.on_revived()
        except Exception:  # noqa: BLE001 - the sign-in itself is done
            pass
    return last


async def _signin(b, username: str) -> tuple[str, str | None]:
    """Open the sign-in page in a tab of its own, type the saved sign-in, confirm. The tab is always closed."""
    from .chatgpt_web import WebError, WebSkip, WebUnavailable

    cfg = config(b)
    store = credstore.default()
    try:
        password = store.get(key(b)) if store is not None else None
    except credstore.CredentialError:
        password = None
    if not password or not username:
        _save(b, enabled=False, saved=False)
        return "error", "The saved sign-in could not be read from the system password store. Save it again."
    page = None
    try:
        try:
            await b._ensure_page()
            page = await b._new_page()
        except WebSkip:
            return "error", "The background browser is not available right now."
        except WebError:
            return "error", "The background browser could not start."
        try:
            result, detail = await _drive(b, page, cfg, username, password)
        except Exception as e:  # noqa: BLE001 - only the kind of error: its text could quote what was typed
            return "error", f"The sign-in page could not be used ({type(e).__name__})."
        if result != "ok":
            return result, detail
        # the same check as for any page: a fresh chat is opened (nothing is sent) and must show a signed-in page
        try:
            await b._open_chat("auto", page)
        except WebUnavailable as e:
            if e.kind == "login":
                return "needs_person", STOPS["still_out"]
            if e.kind == "challenge":
                return "needs_person", STOPS["captcha"]
            b.reset()
            b._block(e)  # signed in; the site's own limit / missing temporary chat is the bridge's usual business
            await b._read_identity(page)
            await b._read_plan(page)
            return "ok", detail
        except Exception as e:  # noqa: BLE001
            return "error", f"The chat page did not load after signing in ({type(e).__name__})."
        b.reset()
        await b._read_identity(page)
        await b._read_plan(page)
        return "ok", detail
    finally:
        password = None  # noqa: F841 - dropped as soon as the attempt is over
        if page is not None:
            b._closing = True
            try:
                await page.close()  # with it goes whatever the form's fields held
            except Exception:  # noqa: BLE001
                pass
            finally:
                b._closing = False


def _host_ok(host: str, allowed: set[str]) -> bool:
    return not host or any(host == h or host.endswith("." + h) for h in allowed)


async def _look(page, cfg: dict) -> dict:
    for _ in range(8):  # the page navigates between the steps: look again
        try:
            return await page.evaluate(LOOK_JS, cfg)
        except Exception as e:  # noqa: BLE001
            if "context was destroyed" not in str(e) and "navigation" not in str(e):
                raise
            await asyncio.sleep(0.4)
    return await page.evaluate(LOOK_JS, cfg)


async def _press(page, spec) -> bool:
    el = (await page.evaluate_handle(FIND_JS, spec)).as_element()
    if el is None:
        return False
    try:
        await el.click(timeout=3000)
    except Exception:  # noqa: BLE001 - a window that is not on screen may not take clicks; the script click does
        await el.evaluate("el => el.click()")
    return True


async def _type(page, spec, value: str) -> bool:
    el = (await page.evaluate_handle(FIND_JS, spec)).as_element()
    if el is None:
        return False
    await el.fill(value, timeout=10000)
    return True


async def _drive(b, page, cfg: dict, username: str, password: str) -> tuple[str, str | None]:
    """The form, step by step. Every field is typed into at most once, and the page is looked at (LOOK_JS) before
    each step: a hard stop ends it there."""
    url = cfg.get("url") or b.login_url
    if not url.startswith(("http://", "https://")):
        url = b.base_url + "/" + url.lstrip("/")
    allowed = {h for h in (urlparse(b.base_url).hostname, urlparse(url).hostname, *(cfg.get("hosts") or [])) if h}
    step_s = float(b.o["autologin_step_s"])
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:  # noqa: BLE001
        if "interrupted" not in str(e) and "ERR_ABORTED" not in str(e):
            return "error", "The sign-in page did not load."
        await asyncio.sleep(1.0)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    started = typed_name = sent_name = typed_password = sent = False
    deadline, challenged = time.time() + step_s, None
    while True:
        s = await _look(page, cfg)
        if not _host_ok(s["host"], allowed):
            return "needs_person", STOPS["sso"]
        if s["captcha"]:
            # an automatic browser check may pass by itself, as it does for a person; nothing is clicked meanwhile
            challenged = challenged or time.time()
            if time.time() - challenged > float(b.o["challenge_wait_s"]):
                return "needs_person", STOPS["captcha"]
            await asyncio.sleep(0.5)
            continue
        challenged = None
        if s["suspicious"]:
            return "needs_person", STOPS["suspicious"]
        if s["codeInput"]:
            return "needs_person", STOPS["code"]
        expecting = (s["email"] and not typed_name) or (s["password"] and not typed_password)
        if not expecting:  # words that also appear as side options on a sign-in form count only when it is not there
            for flag, stop in (("codeText", "code"), ("passkey", "passkey"), ("terms", "terms")):
                if s[flag]:
                    return "needs_person", STOPS[stop]
        if (typed_name or typed_password) and s["failure"]:
            return "wrong_password", f"{b.display_name} did not accept the saved e-mail address or password."
        # one action per look, taken right after it: whatever the page shows next is seen before the next action
        acted = False
        if s["start"] and not started and not s["password"] and not typed_name:
            started = acted = True  # "sign in with a password", on a page that opens on another way in
            await _press(page, cfg["start"])
        elif s["email"] and not typed_name:
            typed_name = acted = await _type(page, cfg["email"], username)
        elif typed_name and not sent_name and not typed_password and not s["password"] and s["continue"]:
            sent_name = acted = await _press(page, cfg["continue"])  # the e-mail step of a form that asks for it first
        elif s["password"] and not typed_password and (typed_name or not s["email"]):
            typed_password = acted = await _type(page, cfg["password"], password)
        elif typed_password and not sent and s["submit"]:
            sent = acted = await _press(page, cfg["submit"])
        elif not s["email"] and not s["password"]:
            p = await b._probe(page, True)
            if p.get("composer") and not p.get("login"):
                return "ok", None if typed_password else "The account was already signed in."
        if acted:
            deadline = time.time() + step_s
        elif time.time() > deadline:
            if typed_name or typed_password:
                return "needs_person", STOPS["unexpected"]
            return "error", "The sign-in page did not show its form in time."
        await asyncio.sleep(b.rand(0.4, 1.0) if acted else 0.4)  # a person's pace between steps
