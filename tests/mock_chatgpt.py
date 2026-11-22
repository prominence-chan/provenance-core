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
        }
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
                if urlparse(self.path).path != "/":
                    self.send_response(404)
                    self.end_headers()
                    return
                if mock.state["challenge"]:
                    return self._out(CHALLENGE)
                st = dict(mock.state)
                want = parse_qs(urlparse(self.path).query).get("model", [None])[0]
                if want and want in st["models"]:
                    st["model"] = want
                self._out(PAGE.replace("__STATE__", json.dumps(st)))

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
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
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
