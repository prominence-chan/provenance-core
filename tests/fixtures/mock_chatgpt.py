"""A local stand-in for the chatgpt.com page, for the chatgpt_web tests: a composer, a send/stop button, messages
with the same markers the bridge reads (data-message-author-role, .markdown, pre/code), a temporary-chat mode, a
model picker, and switchable trouble: logged out, a challenge page, a limit banner, no temporary chat."""

from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>ChatGPT</title></head><body>
<header>
  <button data-testid="model-switcher-dropdown-button" aria-label="Model selector" id="sw"></button>
  <div id="menu" role="menu" style="display:none"></div>
  <span id="acct"></span>
  <span id="tempctl"></span>
</header>
<nav aria-label="Chat history" id="side"></nav>
<div id="banner"></div>
<main>
  <h1 id="temphead"></h1>
  <div id="thread"></div>
  <div id="full"></div>
  <form onsubmit="return false">
    <textarea id="prompt-textarea" role="textbox" rows="4" cols="80"></textarea>
    <button id="composer-submit-button" data-testid="send-button" aria-label="Send prompt">send</button>
  </form>
</main>
<script>
const S = __STATE__;
if (S.title) document.title = S.title;
if (S.sidebar) document.getElementById('side').innerText = S.sidebar;
const q = new URLSearchParams(location.search);
let temp = S.temp && q.get('temporary-chat') === 'true';
let model = S.model;
const $ = id => document.getElementById(id);
function chrome() {
  $('sw').textContent = 'ChatGPT ' + model;
  if (S.picker === false) $('sw').style.display = 'none';   // the Free plan: one model, no picker
  $('acct').innerHTML = S.login ? '<button data-testid="accounts-profile-button" aria-label="Open profile menu">YW</button>'
                                : '<button data-testid="login-button">Log in</button>';
  $('temphead').textContent = temp ? 'Temporary Chat' : 'What can I help with?';
  $('tempctl').innerHTML = !S.login ? '' : temp ? '<button aria-label="Turn off temporary chat">t</button>'
      : (S.temp_toggle ? '<button aria-label="Turn on temporary chat" id="tt">t</button>' : '');
  if ($('tt')) $('tt').onclick = () => { temp = true; chrome(); };
  $('banner').innerHTML = S.limit ? '<div role="alert">You’ve reached your limit for Luna. Responses will use another model until 9 PM.</div>' : S.notice ? '<div role="alert">' + S.notice + '</div>' : '';
}
$('sw').onclick = () => {
  const m = $('menu'); m.style.display = 'block'; m.innerHTML = '';
  for (const name of S.models) { const b = document.createElement('div'); b.setAttribute('role', 'menuitem'); b.textContent = name;
    b.onclick = () => { model = name; m.style.display = 'none'; chrome(); }; m.appendChild(b); }
};
chrome();
if (S.box_max) $('prompt-textarea').addEventListener('input', e => {  // a box that holds only so much
  if (e.target.value.length > S.box_max) e.target.value = e.target.value.slice(0, S.box_max); });
const btn = $('composer-submit-button');
function isFull() {  // S.full_after user messages fill the chat: a notice appears and sending does nothing
  const full = S.full_after && document.querySelectorAll('[data-message-author-role="user"]').length >= S.full_after;
  $('full').innerHTML = full ? '<div><b>Context window full</b><p>Start a new chat with an automatic summary of this conversation, or upgrade to continue here.</p><button>Start a new chat</button></div>' : '';
  return full;
}
function add(role, htmlText, slug) {
  const art = document.createElement('article'); art.setAttribute('data-testid', 'conversation-turn');
  const d = document.createElement('div'); d.setAttribute('data-message-author-role', role);
  if (slug) d.setAttribute('data-message-model-slug', slug);
  d.innerHTML = role === 'assistant' ? '<div class="markdown prose"></div>' : '<div class="whitespace-pre-wrap"></div>';
  d.firstChild[role === 'assistant' ? 'innerHTML' : 'textContent'] = htmlText;
  art.appendChild(d); $('thread').appendChild(art); return art;
}
btn.onclick = async () => {
  if (btn.dataset.testid === 'stop-button') return;
  if (isFull()) return;
  const box = $('prompt-textarea'); const text = box.value; box.value = '';
  add('user', text);
  btn.dataset.testid = 'stop-button'; btn.setAttribute('aria-label', 'Stop streaming'); btn.textContent = 'stop';
  const r = await (await fetch('/__send', {method: 'POST', body: JSON.stringify({prompt: text, temp, model})})).json();
  if (r.error) { const e = document.createElement('div'); e.setAttribute('role', 'alert'); e.textContent = r.error; $('banner').appendChild(e); }
  else if (r.turn_error) {  // the page's own error where the reply would be, with a Retry button
    const d = document.createElement('div'); d.innerHTML = '<div><div>' + r.turn_error + '</div><button>Retry</button></div>'; $('thread').appendChild(d);
  }
  else {
    const art = add('assistant', '', r.slug);
    const md = art.querySelector('.markdown');
    md.innerHTML = '<p>' + 'thinking'.slice(0, 5) + '</p>';
    await new Promise(f => setTimeout(f, S.delay));
    md.innerHTML = r.html;
    const c = document.createElement('button'); c.setAttribute('data-testid', 'copy-turn-action-button'); c.textContent = 'Copy'; art.appendChild(c);
  }
  btn.dataset.testid = 'send-button'; btn.setAttribute('aria-label', 'Send prompt'); btn.textContent = 'send';
  isFull();
};
</script></body></html>"""

# The sign-in page (state login="cookie"): signed in means the browser sends the session cookie. With signin_auto the
# page signs itself in after a moment, the way a person would by typing a password; `flush` more cookies are set as
# well, which makes Chrome write its cookie store at once instead of within 30 s (it writes after 512 changes).
#
# It also carries the site's own sign-in form, for automatic sign-in (web_signin.py): the e-mail address first
# (/auth/login), then the password (/auth/password), as chatgpt.com does; /auth/single is the other shape (Qwen's):
# a page that opens on another way in, with a "Log in with a password" button that shows one form with both fields.
# state["signin_trouble"] switches what the site answers with: "wrong_password", "code" (a verification-code prompt
# after the password), "captcha" (a challenge frame beside the form), "sso" (the e-mail step sends the browser to
# another host's sign-in), "passkey", "terms", "suspicious", "odd" (a page nobody expects after the password);
# state["signin_slow_ms"] delays the form. Every field typed into on any of these pages is reported to the mock
# (Mock.touched: page and field name, never the value) and every form sent is kept (Mock.posted), so a test can say
# exactly what an attempt typed and where it stopped.
SIGNIN = r"""<!doctype html><html><head><meta charset="utf-8"><title>Log in</title></head><body>
<h1>Log in</h1><p id="s">waiting</p><div id="slot"></div><script>
const S = __STATE__; const T = S.signin_trouble || ''; const PAGE = __PAGE__;
const slot = document.getElementById('slot');
const post = async (path, body) => (await fetch(path, {method: 'POST', body: JSON.stringify(body)})).json();
const watch = () => slot.querySelectorAll('input').forEach(i => i.addEventListener('input', () => post('/__typed', {page: PAGE, field: i.name})));
const val = n => slot.querySelector('input[name="' + n + '"]').value;
const pages = {
  login() {
    if (T === 'passkey') { slot.innerHTML = '<p>Use your passkey to sign in to this account</p><button type="button">Try another way</button>'; return; }
    slot.innerHTML = '<button type="button" id="google">Continue with Google</button><form onsubmit="return false">'
      + '<input type="email" name="username" autocomplete="email" placeholder="Email address">'
      + '<button type="submit" id="go">Continue</button></form><a href="/terms">Terms of use</a>';
    if (T === 'captcha') slot.innerHTML += '<iframe title="Widget containing a security challenge" src="/__frame?challenge=1" width="300" height="65"></iframe>';
    document.getElementById('go').onclick = async () => { location.href = (await post('/__auth/email', {email: val('username')})).next; };
    if (S.sso_url) document.getElementById('google').onclick = () => { location.href = S.sso_url; };
  },
  password() {
    slot.innerHTML = '<form onsubmit="return false"><input type="text" name="username" style="display:none">'
      + '<input type="password" name="password" autocomplete="current-password" placeholder="Password">'
      + '<button type="submit" name="intent" value="code">Log in with a one-time code</button>'
      + '<button type="submit" id="go">Continue</button></form><p id="err" role="alert"></p>';
    document.getElementById('go').onclick = async () => {
      const r = await post('/__auth/password', {password: val('password')});
      if (r.error) { document.getElementById('err').textContent = r.error; slot.querySelector('input[type=password]').setAttribute('aria-invalid', 'true'); }
      else location.href = r.next;
    };
  },
  single() {
    slot.innerHTML = '<h2>Login with verification code</h2><p>Enter an email address that can receive verification codes</p>'
      + '<input type="text" name="email" placeholder="Enter Your Email"><button type="button">Continue</button>'
      + '<button type="button" id="pw">Log in with a password</button><p>By continuing, you agree to our Terms of Service and Privacy Policy.</p>';
    document.getElementById('pw').onclick = () => {
      slot.innerHTML = '<form onsubmit="return false"><input type="text" name="email" placeholder="Enter Your Email">'
        + '<input type="password" name="password" placeholder="Enter Your Password">'
        + '<button type="button">Login with verification code</button><button type="submit" id="go">Log in</button></form>'
        + '<p id="err"></p><p>By continuing, you agree to our Terms of Service and Privacy Policy.</p>';
      watch();
      document.getElementById('go').onclick = async () => {
        await post('/__auth/email', {email: val('email')});
        const r = await post('/__auth/password', {password: val('password')});
        if (r.error) document.getElementById('err').textContent = r.error; else location.href = r.next;
      };
    };
  },
  code() { slot.innerHTML = '<h2>Check your inbox</h2><p>Enter the verification code we just sent to your email.</p>'
      + '<input type="text" name="code" autocomplete="one-time-code" inputmode="numeric"><button type="submit">Continue</button>'; },
  terms() { slot.innerHTML = '<h2>We have updated our terms</h2><p>Please review them to continue.</p><button type="button">I agree</button>'; },
  suspicious() { slot.innerHTML = '<h2>Suspicious sign-in blocked</h2><p>Verify it’s you to continue.</p><input type="text" name="answer"><button>Continue</button>'; },
  odd() { slot.innerHTML = '<h2>One more thing</h2><p>Tell us about yourself.</p><input type="text" name="birthday"><button>Next</button>'; },
  sso() { slot.innerHTML = '<h2>Example Identity</h2><p>Sign in with your organisation account.</p><form onsubmit="return false">'
      + '<input type="email" name="username"><input type="password" name="password"><button type="submit">Continue</button></form>'; },
  idp() {
    const mode = S.sso_mode || 'direct';
    const done = S.sso_return || '/';
    if (mode === 'direct') { location.href = done; return; }
    if (mode === 'chooser') {
      slot.innerHTML = '<h2>Choose an account</h2>'
        + '<div data-identifier="other@example.com" role="button" tabindex="0">Other Person<br>other@example.com</div>'
        + '<div data-identifier="person@example.com" role="button" tabindex="0">Test Person<br>person@example.com</div>';
      slot.querySelectorAll('[data-identifier]').forEach(el => {
        el.onclick = () => { location.href = done + '?email=' + encodeURIComponent(el.getAttribute('data-identifier')); };
      });
      return;
    }
    if (mode === 'chooser_one') {
      slot.innerHTML = '<h2>Choose an account</h2>'
        + '<div data-identifier="person@example.com" role="button" tabindex="0">Test Person<br>person@example.com</div>';
      slot.querySelector('[data-identifier]').onclick = () => { location.href = done + '?email=person%40example.com'; };
      return;
    }
    if (mode === 'chooser_ambiguous') {
      slot.innerHTML = '<h2>Choose an account</h2>'
        + '<div data-identifier="alice@example.com" role="button" tabindex="0">Alice<br>alice@example.com</div>'
        + '<div data-identifier="bob@example.com" role="button" tabindex="0">Bob<br>bob@example.com</div>';
      return;
    }
    if (mode === 'idp_password') {
      slot.innerHTML = '<h2>Sign in with Google</h2><form onsubmit="return false">'
        + '<input type="email" name="identifier" placeholder="Email or phone">'
        + '<input type="password" name="password" placeholder="Enter your password">'
        + '<button type="submit">Next</button></form>';
      return;
    }
    if (mode === 'idp_code') {
      slot.innerHTML = '<h2>2-Step Verification</h2><p>Enter the verification code from your authenticator app.</p>'
        + '<input type="text" name="code" autocomplete="one-time-code"><button type="submit">Next</button>';
      return;
    }
    if (mode === 'idp_captcha') {
      slot.innerHTML = '<h2>Sign in</h2><iframe title="Widget containing a security challenge" src="/__frame?challenge=1" width="300" height="65"></iframe>';
      return;
    }
    if (mode === 'idp_consent') {
      slot.innerHTML = '<h2>Sign in to ChatGPT</h2><p>ChatGPT wants to access your Google Account.</p>'
        + '<button type="button">Cancel</button><button type="button">Allow</button>';
      return;
    }
    if (mode === 'idp_passkey') {
      slot.innerHTML = '<h2>Sign in with passkey</h2><p>Use your passkey to sign in to this account.</p>';
      return;
    }
    if (mode === 'idp_suspicious') {
      slot.innerHTML = '<h2>Suspicious activity detected</h2><p>Confirm your identity to continue.</p>';
      return;
    }
  },
};
setTimeout(() => { pages[PAGE](); watch(); }, S.signin_slow_ms || 0);
if (S.signin_auto && PAGE === 'login') setTimeout(async () => {
  await fetch('/__signin', {method: 'POST', body: '{}'});
  for (let i = 0; i < S.flush; i++) document.cookie = 'pad' + (i % 40) + '=' + i + '; path=/';
  document.getElementById('s').textContent = 'signed in';
  location.href = '/';
}, S.signin_after_ms);
</script></body></html>"""
SESSION = "pcore-mock-session"

CHALLENGE = "<!doctype html><html><head><title>Just a moment...</title></head><body><h1>chatgpt.com</h1><p>Verify you are human by completing the action below.</p></body></html>"


def code(lang: str, text: str) -> str:
    """A code block shaped like the page's: a header with the language and a copy button, then pre > code."""
    return (
        f'<pre><div class="hdr"><div>{html.escape(lang)}</div><button>Copy code</button></div>'
        f'<div><code class="whitespace-pre! language-{html.escape(lang)}">{html.escape(text)}\n</code></div></pre>'
    )


def tool_call(name: str, arguments: dict) -> str:
    return code("tool_call", json.dumps({"name": name, "arguments": arguments}))


class Mock:
    """state: login, temp (temporary chat works via the url), temp_toggle (a toggle button exists), challenge,
    limit, model / models (the picker; picker=False hides it), delay (ms before the reply lands). replies: html strings or callables
    (prompt -> html), used in order (the last one repeats); an item may be {"error": "..."}."""

    def __init__(self):
        self.state = {
            "login": True,
            "temp": True,
            "temp_toggle": False,
            "challenge": False,
            "limit": False,
            "notice": "",  # any other banner text (e.g. a temporary-limit message)
            "model": "Luna",
            "models": ["Luna", "Nimbus"],
            "delay": 150,
            "full_after": 0,  # this many user messages fill a chat (0 = never)
            "sidebar": "",  # the saved-chats list beside the chat (titles, its own load errors)
            "box_max": 0,  # the message box holds this many characters (0 = any)
            # login: True / False, or "cookie": signed in when the browser sends SESSION (set by the sign-in page)
            "signin_auto": False,  # the sign-in page signs itself in
            "signin_after_ms": 300,
            "flush": 0,
            "email": "person@example.com",  # shown in the page's bootstrap data when signed in
            "plan": "free",  # the account's plan in the bootstrap data ("plus", "team", ...); None: not shown
            "signin_trouble": "",  # see SIGNIN
            "signin_slow_ms": 0,
            "sso_url": "",  # where "Continue with Google" navigates (e.g. http://localhost:<port>/auth/idp)
            "sso_mode": "",  # direct | chooser | chooser_one | chooser_ambiguous | idp_password | idp_code | ...
            "sso_return": "",  # callback URL on 127.0.0.1 that sets SESSION and redirects to /
        }
        self.accounts = {"person@example.com": "mock-password"}  # who the sign-in form lets in
        self.session_tail = ""  # more characters for the session cookie's value (a value tests can search for)
        self.touched: list[tuple[str, str]] = []  # (page, field) typed into on the sign-in pages
        self.posted: list[tuple[str, dict]] = []  # (step, what was sent)
        self.sso_chosen: list[str] = []  # account emails chosen on the mock IdP
        self._email = None
        self.replies: list = ["<p>ok</p>"]
        self.sent: list[dict] = []
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _out(self, body: str, ctype="text/html; charset=utf-8"):
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/__sso_complete":
                    chosen = parse_qs(urlparse(self.path).query).get("email", [""])[0]
                    if chosen:
                        mock.sso_chosen.append(chosen)
                        mock.state["email"] = chosen
                    mock.signins += 1
                    self.send_response(302)
                    self.send_header("Set-Cookie", f"{SESSION}=1{mock.session_tail}; Max-Age=86400; Path=/; HttpOnly")
                    self.send_header("Location", "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/auth/login":
                    mock.signin_pages += 1
                if path.startswith("/auth/") and path[6:] in (
                    "login", "password", "single", "code", "terms", "suspicious", "odd", "sso", "idp"
                ):  # fmt: skip
                    page = SIGNIN.replace("__STATE__", json.dumps(mock.state)).replace("__PAGE__", json.dumps(path[6:]))
                    return self._out(page)
                if path == "/__frame":
                    return self._out("<!doctype html><title>check</title><p>checking</p>")
                if path != "/":
                    self.send_response(404)
                    self.end_headers()
                    return
                if mock.state["challenge"]:
                    return self._out(CHALLENGE)
                st = dict(mock.state)
                if st["login"] == "cookie":
                    st["login"] = f"{SESSION}=1" in (self.headers.get("Cookie") or "")
                want = parse_qs(urlparse(self.path).query).get("model", [None])[0]
                if want and want in st["models"]:
                    st["model"] = want
                page = PAGE.replace("__STATE__", json.dumps(st))
                if st["login"] and st.get("email"):
                    session = {"user": {"email": st["email"]}}
                    if st.get("plan"):
                        session["account"] = {"id": "acct-1", "planType": st["plan"], "structure": "personal"}
                    boot = json.dumps({"session": session})
                    page = page.replace(
                        "<body>", f'<body><script type="application/json" id="client-bootstrap">{boot}</script>', 1
                    )
                self._out(page)

            def do_POST(self):
                if urlparse(self.path).path == "/__signin":
                    self.rfile.read(int(self.headers.get("Content-Length") or 0))
                    mock.signins += 1
                    self.send_response(200)
                    self.send_header("Set-Cookie", f"{SESSION}=1{mock.session_tail}; Max-Age=86400; Path=/; HttpOnly")
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                path = urlparse(self.path).path
                if path == "/__typed":
                    mock.touched.append((req.get("page"), req.get("field")))
                    return self._out("{}", "application/json")
                if path == "/__auth/email":
                    mock.posted.append(("email", req))
                    mock._email = req.get("email")
                    nxt = "/auth/password"
                    if mock.state["signin_trouble"] == "sso":  # the same server under another host name
                        nxt = f"http://localhost:{mock.httpd.server_address[1]}/auth/sso"
                    return self._out(json.dumps({"next": nxt}), "application/json")
                if path == "/__auth/password":
                    mock.posted.append(("password", req))
                    t = mock.state["signin_trouble"]
                    right = mock.accounts.get(mock._email) == req.get("password")
                    if t == "wrong_password" or not right:
                        return self._out(
                            json.dumps({"error": "Incorrect email address or password"}), "application/json"
                        )
                    if t in ("code", "terms", "suspicious", "odd"):
                        return self._out(json.dumps({"next": "/auth/" + t}), "application/json")
                    mock.signins += 1
                    data = json.dumps({"next": "/"}).encode()
                    self.send_response(200)
                    self.send_header("Set-Cookie", f"{SESSION}=1{mock.session_tail}; Max-Age=86400; Path=/; HttpOnly")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                mock.sent.append(req)
                r = (
                    mock.replies[min(len(mock.sent), len(mock.replies)) - 1]
                    if mock.per_page is None
                    else mock.per_page(req)
                )
                if callable(r):
                    r = r(req["prompt"])
                out = r if isinstance(r, dict) else {"html": r, "slug": "gpt-6-" + req["model"].lower()}
                self._out(json.dumps(out), "application/json")

        self.per_page = None
        self.signin_pages = self.signins = 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
