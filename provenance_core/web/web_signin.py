"""A saved sign-in (or remembered SSO sign-in) for a web-chat account, and signing in again when the site has signed
the account out.

For password sign-in, off unless the person turned it on for that provider + account, with their agreement
(PUT .../autologin, see web_setup.py). What is kept where:

- the password: only in the operating system's credential store (credstore.py). It is read from there at the moment
  of an attempt, typed into the site's own password field, and dropped. It is never written to a state file, the
  settings, a log line, an event, an exception, a debug dump or an API answer;
- the user name / e-mail address, remembered login method, whether it is on, and the record of attempts: in the
  account's state file, under "autologin" (and "login_method" / "login_method_label").

For supported SSO sign-ins (e.g. Google / Microsoft / Apple on ChatGPT, Google / GitHub on Qwen), automatic sign-in
re-triggers the site's SSO button using the browser profile's existing signed-in SSO session — storing no password
and never typing credentials into an identity provider.

An attempt (`attempt`) happens only when the bridge has established that the account is signed out (Bridge._block
with kind "login", after its doubt logic), or when the person presses "try now"; never while a sign-in window is
open; at most `autologin_max_per_day` times a day per account, with a gap that doubles after each try
(`autologin_gap_s`), automatically requeuing transient errors and gap waits within those bounds. It runs in the
background browser and profile the bridge already uses, in a tab of its own that is closed afterwards, and confirms
the result with the same check the bridge uses for any page (Bridge._open_chat).

Hard stops. The page is looked at before every step, and the attempt ends at once, touching nothing more, when it
shows: a captcha or other human check, a prompt for a code (two-step, e-mail, SMS), a passkey prompt, a "suspicious
sign-in" / locked-account notice, a terms or consent screen, an identity-provider password/e-mail prompt, an
ambiguous account chooser, a page on an unexpected host, or anything the description does not expect after an action.
The result is then `needs_person`, and no further automatic attempt is made until the person has signed in themselves
(the sign-in window) or saved the sign-in again. Nothing here solves, clicks or works around a check, reads a code
from anywhere, or tries again in a loop. A site that says the name or password is wrong turns automatic sign-in off
for the account until a new password is saved, so that a stale password can never lock the account.

Results: `ok`, `wrong_password`, `needs_person` (with `detail` in plain words), `error` (the page did not load or
could not be driven; only this one is tried again later, within the limits above). Each attempt is one dated line in
<home>/web-events.jsonl (kind "autologin") and is shown by GET /web/accounts."""

from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from . import credstore

MAX_USERNAME = 254
METHODS = ("google", "password", "microsoft", "apple", "github", "other")
CHROME_EPOCH_OFFSET = 11644473600

_GITHUB_AUTH_PATH = re.compile(r"^/(?:login|session|sessions|oauth)(?:/|$)", re.I)
_AUTH_PATH = re.compile(r"(?:^|/)(?:auth|log-?in|sign-?in|oauth2?|oidc|saml|sso|authorize|password)(?:/|$)", re.I)
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")

_NON_SSO_HOSTS = frozenset(
    {
        "challenges.cloudflare.com",
        "turnstile.cloudflare.com",
        "hcaptcha.com",
        "recaptcha.net",
        "arkoselabs.com",
        "funcaptcha.com",
        "google.com",
        "www.google.com",
        "gstatic.com",
        "googleapis.com",
        "cloudflare.com",
        "github.com",
        "www.github.com",
    }
)
_OTHER_SSO_DOMAINS = frozenset(
    {
        "okta.com",
        "oktapreview.com",
        "auth0.com",
        "onelogin.com",
        "pingidentity.com",
        "pingone.com",
        "duosecurity.com",
        "jumpcloud.com",
        "b2clogin.com",
        "amazoncognito.com",
        "workos.com",
        "clerk.accounts.dev",
        "stytch.com",
        "descope.com",
    }
)
_OTHER_SSO_PREFIXES = (
    "sso.",
    "login.",
    "signin.",
    "sign-in.",
    "auth.",
    "id.",
    "idp.",
    "identity.",
    "accounts.",
    "oauth.",
)
_KNOWN_IDP_HOSTS = frozenset(
    {
        "accounts.google.com",
        "login.microsoftonline.com",
        "login.live.com",
        "account.live.com",
        "appleid.apple.com",
        "idmsa.apple.com",
        "github.com",
    }
)

# What the person reads for each hard stop.
STOPS = {
    "captcha": "The site asked for a human check (a captcha).",
    "code": "The site asked for a verification code.",
    "passkey": "The site asked for a passkey or security key.",
    "suspicious": "The site showed a security notice about this sign-in or account.",
    "terms": "The site showed terms or a consent screen to accept.",
    "sso": "The site sent the sign-in to another sign-in provider.",
    "idp_signin": "The sign-in provider asked for a password or e-mail address.",
    "chooser": "The sign-in provider asked which account to choose.",
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
  if (cfg.ssoButton) out.ssoButton = all(cfg.ssoButton).length > 0;
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

# Looks at an identity-provider page during SSO auto-relogin: whether it asks for credentials or consent (hard stop),
# or displays an already-signed-in account chooser. Never returns field values or page prose.
SSO_PAGE_JS = r"""() => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                      return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const credInput = [...document.querySelectorAll(
    'input[type="password"], input[type="email"], input[name="identifier"], input[name="loginfmt"], ' +
    'input[name="username"], input[name="email"], input[autocomplete="username"], ' +
    'input[autocomplete="email"], input[autocomplete="current-password"]'
  )].some(vis);
  const text = document.body ? (document.body.innerText || '') + '\n' + document.title : document.title;
  const consent = /wants to access your|will share your (?:name|email|profile)|grant(?:ing)? access|authorize .{0,60} to access|confirm you want to sign in to|review .{0,40} permissions|allow .{0,40} to access/i.test(text)
    || [...document.querySelectorAll('button, [role="button"], input[type="submit"]')].some(
      el => vis(el) && /^(?:allow|authorize|grant access|i agree|accept(?: all)?)$/i.test((el.innerText || el.value || '').trim())
    );
  const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;
  const accounts = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('[data-identifier], [data-email], [data-account-email], [data-test-id]')) {
    if (!vis(el)) continue;
    const attr = (el.getAttribute('data-identifier') || el.getAttribute('data-email') || el.getAttribute('data-account-email') || el.getAttribute('data-test-id') || '').trim();
    const m = attr.match(EMAIL);
    if (!m) continue;
    const em = m[0].toLowerCase();
    if (seen.has(em)) continue;
    seen.add(em);
    const txt = (el.innerText || '').trim();
    accounts.push({email: em, signedOut: /\bsigned out\b/i.test(txt)});
  }
  if (!accounts.length) {
    const els = [...document.querySelectorAll('button, [role="button"], [role="option"], [role="link"], a, li[tabindex], div[tabindex]')].filter(vis);
    for (const el of els) {
      const txt = (el.innerText || '').trim();
      const matches = txt.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g) || [];
      const uniq = [...new Set(matches.map(x => x.toLowerCase()))];
      if (uniq.length !== 1) continue;
      if (/\b(?:use another|add account|remove an account|sign out)\b/i.test(txt)) continue;
      const em = uniq[0];
      if (seen.has(em)) continue;
      seen.add(em);
      accounts.push({email: em, signedOut: /\bsigned out\b/i.test(txt)});
    }
  }
  return {credInput, consent, accounts};
}"""

# Find the visible signed-in account-chooser item matching `target` e-mail on an IdP page.
CLICK_ACCOUNT_JS = r"""(target) => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                      return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const want = String(target || '').toLowerCase();
  const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;
  for (const el of document.querySelectorAll('[data-identifier], [data-email], [data-account-email], [data-test-id]')) {
    if (!vis(el)) continue;
    const attr = (el.getAttribute('data-identifier') || el.getAttribute('data-email') || el.getAttribute('data-account-email') || el.getAttribute('data-test-id') || '').trim();
    const m = attr.match(EMAIL);
    if (m && m[0].toLowerCase() === want && !/\bsigned out\b/i.test(el.innerText || '')) return el;
  }
  const els = [...document.querySelectorAll('button, [role="button"], [role="option"], [role="link"], a, li[tabindex], div[tabindex]')].filter(vis);
  for (const el of els) {
    const txt = (el.innerText || '').trim();
    const matches = txt.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g) || [];
    const uniq = [...new Set(matches.map(x => x.toLowerCase()))];
    if (uniq.length === 1 && uniq[0] === want && !/\b(?:use another|add account|remove an account|sign out|signed out)\b/i.test(txt)) {
      return el;
    }
  }
  return null;
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


# ---- login-method detection from Chrome's History DB (strictly read-only; hostnames only) ---------------------------
def chrome_time_us(unix_ts: float) -> int:
    return int((float(unix_ts) + CHROME_EPOCH_OFFSET) * 1_000_000)


def _clean_host(host: str | None) -> str | None:
    if not isinstance(host, str):
        return None
    h = host.strip().lower().lstrip(".")
    if not h or not _HOST_RE.fullmatch(h):
        return None
    return h


def _history_db(profile: Path) -> Path | None:
    for rel in ("Default/History", "History"):
        p = Path(profile) / rel
        if p.exists():
            return p
    return None


def _history_visits(profile: Path, since_ts: float) -> list[tuple[str, bool, bool]]:
    """Read only (hostname, is_github_auth_path, is_auth_path) for visits since `since_ts - 5s`.
    Full URLs, paths, query strings and fragments are discarded immediately and never logged or stored."""
    db = _history_db(profile)
    if not db:
        return []
    cutoff = chrome_time_us(since_ts - 5.0)
    raw_urls: list[str] = []
    try:
        con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=1.0)
        try:
            try:
                rows = con.execute(
                    "SELECT u.url FROM visits v JOIN urls u ON v.url = u.id "
                    "WHERE v.visit_time >= ? ORDER BY v.visit_time ASC",
                    (cutoff,),
                ).fetchall()
                raw_urls = [str(r[0]) for r in rows if r and r[0]]
            except sqlite3.Error:
                raw_urls = []
            if not raw_urls:
                rows = con.execute(
                    "SELECT url FROM urls WHERE last_visit_time >= ? ORDER BY last_visit_time ASC",
                    (cutoff,),
                ).fetchall()
                raw_urls = [str(r[0]) for r in rows if r and r[0]]
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return []
    out: list[tuple[str, bool, bool]] = []
    for raw in raw_urls:
        try:
            parsed = urlparse(raw)
        except ValueError:
            continue
        host = _clean_host(parsed.hostname)
        if not host:
            continue
        path = parsed.path or ""
        is_gh = host == "github.com" and bool(_GITHUB_AUTH_PATH.match(path))
        is_auth = bool(_AUTH_PATH.search(path))
        out.append((host, is_gh, is_auth))
    return out


def _own_domains(b) -> set[str]:
    own: set[str] = set()
    for u in (b.base_url, b.login_url):
        h = _clean_host(urlparse(u).hostname)
        if h and h not in _KNOWN_IDP_HOSTS:
            own.add(h)
    for d in (b.session_spec or {}).get("domains") or []:
        h = _clean_host(str(d))
        if h and h not in _KNOWN_IDP_HOSTS:
            own.add(h)
    for h_raw in (config(b) or {}).get("hosts") or []:
        h = _clean_host(str(h_raw))
        if h and h not in _KNOWN_IDP_HOSTS:
            own.add(h)
    if not b.site:
        own.update({"chatgpt.com", "openai.com", "auth.openai.com"})
    elif b.o.get("site") == "qwen":
        own.update({"chat.qwen.ai", "qwen.ai"})
    return own


def detect_login_method(
    profile: Path,
    b,
    since_ts: float,
    *,
    google_cookies_changed: bool = False,
    hint: str | None = None,
) -> tuple[str | None, str | None]:
    """Classify the login method used during a LoginFlow window -> (canonical_method, optional_host_label)."""
    if b.o.get("site") == "gemini":
        return "google", None
    own = _own_domains(b)
    visits = _history_visits(profile, since_ts)
    known_idp: str | None = None
    other_host: str | None = None
    saw_own_auth = False
    for host, is_gh_auth, is_auth_path in visits:
        if host == "accounts.google.com":
            known_idp = "google"
            continue
        if host in ("login.microsoftonline.com", "login.live.com", "account.live.com") or host.endswith(
            ".microsoftonline.com"
        ):
            known_idp = "microsoft"
            continue
        if host in ("appleid.apple.com", "idmsa.apple.com"):
            known_idp = "apple"
            continue
        if host == "github.com":
            if is_gh_auth:
                known_idp = "github"
            continue
        if _host_ok(host, own):
            if is_auth_path or host.startswith("auth."):
                saw_own_auth = True
            continue
        if _host_ok(host, _NON_SSO_HOSTS):
            continue
        if _host_ok(host, _OTHER_SSO_DOMAINS) or host.startswith(_OTHER_SSO_PREFIXES) or is_auth_path:
            other_host = host
    if known_idp:
        return known_idp, None
    if other_host:
        return "other", other_host
    if google_cookies_changed:
        return "google", None
    if saw_own_auth:
        return "password", None
    if hint in METHODS:
        return hint, None
    return None, None


# ---- what is kept ----------------------------------------------------------------------------------------------------
def config(b) -> dict | None:
    """The sign-in form's description for the bridge's site, or None when automatic sign-in is not offered there."""
    return b.o.get("signin") or b.site_info.get("signin")


def sso_config(b) -> dict:
    cfg = config(b) or {}
    sso = cfg.get("sso")
    return sso if isinstance(sso, dict) else {}


def sso_supported(b, method: str | None) -> bool:
    return bool(method and method in sso_config(b))


def method_of(b, s: dict | None = None) -> tuple[str | None, str | None]:
    s = b.load() if s is None else s
    st = s.get("autologin") or {}
    method = st.get("method") or s.get("login_method")
    if method not in METHODS:
        method = None
    label = None
    if method == "other":
        raw_label = st.get("method_label") if st.get("method") == "other" else s.get("login_method_label")
        label = _clean_host(raw_label)
    return method, label


def _active_mode(b, s: dict | None = None) -> tuple[str | None, str | None]:
    """('sso', method) | ('password', 'password') | (None, None)."""
    s = b.load() if s is None else s
    st = s.get("autologin") or {}
    method, _ = method_of(b, s)
    if sso_supported(b, method):
        return "sso", method
    if st.get("saved") and method in (None, "password"):
        return "password", "password"
    return None, None


def support(b) -> tuple[bool, str | None]:
    if not config(b):
        why = b.site_info.get("signin_unsupported")
        return False, why or f"Automatic sign-in is not available for {b.display_name}."
    method, _ = method_of(b)
    if sso_supported(b, method):
        return True, None
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
    s = b.load()
    st = dict(s.get("autologin") or {})
    mode, _ = _active_mode(b, s)
    name = b.display_name
    if mode is None:
        return "No sign-in is saved for this account."
    if mode == "password" and st.get("hold") == "wrong_password":
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
    mode, _ = _active_mode(b, s)
    if ok and mode is not None and s.get("signed_in") is True:
        if (s.get("blocked_until") or 0) <= b.clock() or s.get("kind") != "login":
            return "This account is signed in, so there is nothing to do."
    return blocked_by(b, manual)


def note(b) -> str | None:
    """One sentence about where automatic sign-in stands, when there is something the person should know."""
    s = b.load()
    st = dict(s.get("autologin") or {})
    mode, _ = _active_mode(b, s)
    if mode is None:
        return None
    if (mode == "password" and st.get("hold") == "wrong_password") or (
        st.get("enabled") and st.get("hold") == "needs_person"
    ):
        return blocked_by(b)
    if st.get("enabled") and len(tries_today(b, st)) >= int(b.o["autologin_max_per_day"]):
        return blocked_by(b)
    return None


def running(b) -> bool:
    return bool(getattr(b, "_autologin_running", False))


def queued(b) -> bool:
    task = getattr(b, "_autologin_requeue_task", None)
    return bool(task and not task.done())


def view(b) -> dict:
    """The account's `autologin` in GET /web/accounts. Never the password: only whether one is saved."""
    ok, why = support(b)
    s = b.load()
    st = dict(s.get("autologin") or {})
    method, method_label = method_of(b, s)
    mode, _ = _active_mode(b, s)
    saved = bool(st.get("saved"))
    wait = None
    tries = tries_today(b, st)
    if tries and not st.get("gap_waived"):
        wait = max(0, int(tries[-1] + _gap(b, len(tries)) - b.clock())) or None
    return {
        "supported": ok,
        "reason": why,
        "enabled": bool(ok and st.get("enabled") and mode is not None),
        "saved": saved,
        "username": st.get("username"),
        "attempts_today": len(tries),
        "attempts_per_day": int(b.o["autologin_max_per_day"]),
        "last_try": st.get("last_try"),
        "history": list(st.get("history") or []),
        "paused": st.get("hold"),  # null, "wrong_password" or "needs_person"
        "note": note(b),
        "next_try_in_s": wait,
        "running": running(b),
        "method": method,
        "method_label": method_label if method == "other" else None,
        "needs_credentials": bool(method == "password" and not saved),
        "queued": queued(b),
    }


# ---- remembering a verified login method ----------------------------------------------------------------------------
def note_login_method(b, method: str | None, method_label: str | None = None) -> dict:
    """Called when LoginFlow._verify() confirms signed_in: persist the detected login method on bridge state,
    auto-enabling supported SSO auto-relogin unless the user previously disabled it, and never storing passwords."""
    if method not in METHODS:
        return view(b)
    label = _clean_host(method_label) if method == "other" else None
    b.save(login_method=method, login_method_label=label)
    st = state(b)
    if sso_supported(b, method):
        if st.get("user_disabled"):
            _save(b, method=method, method_label=None, enabled=False, hold=None)
        else:
            _save(b, method=method, method_label=None, enabled=True, hold=None, gap_waived=True)
    elif method == "password":
        if st.get("saved"):
            hold = None if st.get("hold") == "needs_person" else st.get("hold")
            _save(b, method="password", method_label=None, hold=hold)
        else:
            _save(b, method="password", method_label=None, enabled=False, saved=False)
    elif config(b):
        _save(b, method=method, method_label=label, enabled=False)
    return view(b)


# ---- the person's choices ----------------------------------------------------------------------------------------------
def save_signin(b, agree, username=None, password=None, method=None) -> dict:
    """PUT: keep this sign-in (password or supported SSO) and turn automatic sign-in on. Needs agree: true."""
    if agree is not True:
        raise Invalid("Automatic sign-in needs your agreement: send agree: true.")
    if method is not None and (not isinstance(method, str) or method not in METHODS):
        raise Invalid("Send a recognised sign-in method.")
    if not config(b):
        why = b.site_info.get("signin_unsupported")
        raise Refused(why or f"Automatic sign-in is not available for {b.display_name}.")
    if method is not None and method != "password":
        if not sso_supported(b, method):
            raise Refused(f"Automatic sign-in with {method} is not available for {b.display_name}.")
        store = credstore.default()
        if store is not None:
            try:
                store.delete(key(b))
            except credstore.CredentialError:
                pass
        cancel_requeue(b)
        b.save(login_method=method, login_method_label=None)
        _save(
            b,
            method=method,
            method_label=None,
            enabled=True,
            saved=False,
            username=None,
            agreed_at=int(b.clock()),
            hold=None,
            gap_waived=True,
            user_disabled=False,
        )
        return view(b)
    if credstore.default() is None:
        raise Refused("This computer has no system password store this app can use, so a password cannot be saved.")
    if not isinstance(username, str) or not username.strip() or len(username) > MAX_USERNAME:
        raise Invalid("Send the e-mail address or user name of the account.")
    if not isinstance(password, str) or not password or len(password) > credstore.MAX_SECRET:
        raise Invalid("Send the password of the account.")
    try:
        credstore.default().set(key(b), password)
    except credstore.CredentialError as e:
        raise Refused(f"The password could not be saved: {e}.") from None
    cancel_requeue(b)
    b.save(login_method="password", login_method_label=None)
    _save(
        b,
        method="password",
        method_label=None,
        enabled=True,
        saved=True,
        username=username.strip(),
        agreed_at=int(b.clock()),
        hold=None,
        gap_waived=True,
        user_disabled=False,
    )
    return view(b)


def set_enabled(b, enabled) -> dict:
    """PATCH: on or off, keeping what is saved or remembered."""
    if not isinstance(enabled, bool):
        raise Invalid("Send enabled: true or false.")
    s = b.load()
    st = dict(s.get("autologin") or {})
    mode, method = _active_mode(b, s)
    if enabled:
        ok, why = support(b)
        if not ok:
            raise Refused(why)
        if mode is None:
            raise Refused("No sign-in is saved for this account. Save one first.")
        if mode == "password" and st.get("hold") == "wrong_password":
            raise Refused(blocked_by(b))
        changes: dict = {"enabled": True, "user_disabled": False}
        if mode == "sso":
            changes["method"] = method
            changes["method_label"] = None
        _save(b, **changes)
    else:
        cancel_requeue(b)
        _save(b, enabled=False, user_disabled=True)
    return view(b)


def forget(b) -> dict:
    """DELETE: the password leaves the credential store, the autologin state clears, and any queued retry stops."""
    cancel_requeue(b)
    store = credstore.default()
    if store is not None:
        try:
            store.delete(key(b))
        except credstore.CredentialError:
            pass
    s = b.load()
    changes: dict = {}
    if s.get("autologin") is not None:
        changes["autologin"] = None
    if s.get("login_method") is not None:
        changes["login_method"] = None
    if s.get("login_method_label") is not None:
        changes["login_method_label"] = None
    if changes:
        b.save(**changes)
    return view(b)


# ---- automatic requeuing --------------------------------------------------------------------------------------------
def cancel_requeue(b):
    """Cancel any pending background retry timer for this bridge."""
    task = getattr(b, "_autologin_requeue_task", None)
    b._autologin_requeue_task = None
    if task is not None and not task.done():
        try:
            cur = asyncio.current_task()
        except RuntimeError:
            cur = None
        if task is not cur:
            task.cancel()


def _can_requeue(b) -> bool:
    """True when this account is eligible for automatic retries (enabled, supported, not hard-stopped, under cap)."""
    ok, _ = support(b)
    if not ok:
        return False
    s = b.load()
    st = dict(s.get("autologin") or {})
    mode, _ = _active_mode(b, s)
    if mode is None or not st.get("enabled"):
        return False
    if st.get("hold") in ("wrong_password", "needs_person"):
        return False
    if b.signing_in():
        return False
    tries = tries_today(b, st)
    return len(tries) < int(b.o["autologin_max_per_day"])


def _requeue_delay(b) -> float | None:
    """Delay in seconds until the next allowed try when blocked only by the backoff gap, else None."""
    if not _can_requeue(b):
        return None
    st = state(b)
    tries = tries_today(b, st)
    if tries and not st.get("gap_waived"):
        wait = tries[-1] + _gap(b, len(tries)) - b.clock()
        if wait > 0:
            return wait
    return None


def _schedule_requeue(b, delay_s: float) -> bool:
    """Schedule one background retry after `delay_s` seconds, replacing any previous timer."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    cancel_requeue(b)
    wait = max(0.02, float(delay_s)) + (0.01 if delay_s > 0 else 0.0)

    async def later():
        me = asyncio.current_task()
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        finally:
            if getattr(b, "_autologin_requeue_task", None) is me:
                b._autologin_requeue_task = None
        s = b.load()
        if s.get("signed_in") is True and ((s.get("blocked_until") or 0) <= b.clock() or s.get("kind") != "login"):
            return
        kick(b)

    b._autologin_requeue_task = loop.create_task(later())
    return True


# ---- one attempt ---------------------------------------------------------------------------------------------------------
def kick(b):
    """The bridge has just established that this account is signed out: start one attempt in the background when
    the rules allow one, or schedule a requeue when blocked only by the backoff gap or an in-flight attempt."""
    task = getattr(b, "_autologin_task", None)
    if (task and not task.done()) or running(b):
        if _can_requeue(b) and not queued(b):
            delay = _requeue_delay(b)
            if delay is None:
                gap = float(b.o["autologin_gap_s"])
                delay = min(5.0, gap) if gap > 0 else 0.05
            _schedule_requeue(b, delay)
        return
    if blocked_by(b) is not None:
        delay = _requeue_delay(b)
        if delay is not None and not queued(b):
            _schedule_requeue(b, delay)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    cancel_requeue(b)

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
    cancel_requeue(b)
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
    if result in ("ok", "wrong_password", "needs_person"):
        cancel_requeue(b)
    elif result == "error" and _can_requeue(b):
        tries = tries_today(b)
        gap = _gap(b, len(tries)) if tries else float(b.o["autologin_gap_s"])
        wait = max(0.0, tries[-1] + gap - b.clock()) if tries else gap
        _schedule_requeue(b, wait)
    if result == "ok" and b.on_revived:
        try:
            b.on_revived()
        except Exception:  # noqa: BLE001 - the sign-in itself is done
            pass
    return last


async def _signin(b, username: str) -> tuple[str, str | None]:
    """Open the sign-in page in a tab of its own, perform password or SSO sign-in, confirm. The tab is always closed."""
    from .chatgpt_web import WebError, WebSkip, WebUnavailable

    cfg = config(b)
    s = b.load()
    mode, method = _active_mode(b, s)
    if mode == "sso":
        expected_email = s.get("identity") or s.get("last_identity") or username or ""
        password = None
    else:
        store = credstore.default()
        try:
            password = store.get(key(b)) if store is not None else None
        except credstore.CredentialError:
            password = None
        if not password or not username:
            _save(b, enabled=False, saved=False)
            return "error", "The saved sign-in could not be read from the system password store. Save it again."
        expected_email = ""
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
            if mode == "sso":
                result, detail = await _drive_sso(b, page, cfg, method, expected_email)
            else:
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


def _host_ok(host: str, allowed: set[str] | frozenset[str]) -> bool:
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


async def _look_sso(page) -> dict:
    for _ in range(8):
        try:
            return await page.evaluate(SSO_PAGE_JS)
        except Exception as e:  # noqa: BLE001
            if "context was destroyed" not in str(e) and "navigation" not in str(e):
                raise
            await asyncio.sleep(0.4)
    return await page.evaluate(SSO_PAGE_JS)


async def _press(page, spec) -> bool:
    el = (await page.evaluate_handle(FIND_JS, spec)).as_element()
    if el is None:
        return False
    try:
        await el.click(timeout=3000)
    except Exception:  # noqa: BLE001 - a window that is not on screen may not take clicks; the script click does
        await el.evaluate("el => el.click()")
    return True


async def _press_account(page, email: str) -> bool:
    el = (await page.evaluate_handle(CLICK_ACCOUNT_JS, email)).as_element()
    if el is None:
        return False
    try:
        await el.click(timeout=3000)
    except Exception:  # noqa: BLE001
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


async def _drive_sso(b, page, cfg: dict, method: str, expected_email: str = "") -> tuple[str, str | None]:
    """Re-trigger the remembered SSO sign-in button using the profile's existing signed-in IdP session.
    Never types credentials into an IdP; hard-stops immediately on any captcha, 2FA, passkey, consent,
    IdP credential prompt, suspicious notice, unexpected host, or ambiguous account chooser."""
    sso_entry = (cfg.get("sso") or {}).get(method)
    if not sso_entry:
        return "needs_person", STOPS["sso"]
    url = cfg.get("url") or b.login_url
    if not url.startswith(("http://", "https://")):
        url = b.base_url + "/" + url.lstrip("/")
    site_hosts = {
        h.lower() for h in (urlparse(b.base_url).hostname, urlparse(url).hostname, *(cfg.get("hosts") or [])) if h
    }
    idp_hosts = {str(h).lower().lstrip(".") for h in (sso_entry.get("hosts") or []) if h}
    allowed = site_hosts | idp_hosts
    step_s = float(b.o["autologin_step_s"])
    look_cfg = {**cfg, "ssoButton": sso_entry["button"]}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:  # noqa: BLE001
        if "interrupted" not in str(e) and "ERR_ABORTED" not in str(e):
            return "error", "The sign-in page did not load."
        await asyncio.sleep(1.0)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

    clicked_sso = False
    picked_account = False
    want_email = (expected_email or "").strip().lower()
    deadline, challenged = time.time() + step_s, None
    while True:
        s = await _look(page, look_cfg)
        host = (s.get("host") or "").lower()
        if not _host_ok(host, allowed):
            return "needs_person", STOPS["sso"]
        if s["captcha"]:
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

        on_idp = bool(host and _host_ok(host, idp_hosts) and not _host_ok(host, site_hosts))
        if on_idp:
            for flag, stop in (("codeText", "code"), ("passkey", "passkey"), ("terms", "terms")):
                if s[flag]:
                    return "needs_person", STOPS[stop]
            idp = await _look_sso(page)
            if idp["credInput"]:
                return "needs_person", STOPS["idp_signin"]
            if idp["consent"]:
                return "needs_person", STOPS["terms"]
            acted = False
            accounts = idp.get("accounts") or []
            if accounts and not picked_account:
                active = [a["email"] for a in accounts if not a.get("signedOut")]
                if want_email:
                    if want_email in active:
                        picked_account = acted = await _press_account(page, want_email)
                    else:
                        return "needs_person", STOPS["chooser"]
                elif len(active) == 1 and len(accounts) == 1:
                    picked_account = acted = await _press_account(page, active[0])
                else:
                    return "needs_person", STOPS["chooser"]
            if acted:
                deadline = time.time() + step_s
            elif time.time() > deadline:
                return "needs_person", STOPS["unexpected"]
            await asyncio.sleep(b.rand(0.4, 1.0) if acted else 0.4)
            continue

        expecting_sso = bool(s.get("ssoButton") and not clicked_sso)
        if not expecting_sso:
            for flag, stop in (("codeText", "code"), ("passkey", "passkey"), ("terms", "terms")):
                if s[flag]:
                    return "needs_person", STOPS[stop]
        acted = False
        if s.get("ssoButton") and not clicked_sso:
            clicked_sso = acted = await _press(page, sso_entry["button"])
        elif not s.get("ssoButton"):
            p = await b._probe(page, True)
            if p.get("composer") and not p.get("login"):
                return "ok", None if clicked_sso else "The account was already signed in."
        if acted:
            deadline = time.time() + step_s
        elif time.time() > deadline:
            if clicked_sso:
                return "needs_person", STOPS["unexpected"]
            return "error", "The sign-in page did not show its sign-in option in time."
        await asyncio.sleep(b.rand(0.4, 1.0) if acted else 0.4)
