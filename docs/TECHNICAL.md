# provenance-core: technical reference

## How requests are routed

- **Tiers by measured strength.** Every model has a 0-100 score: a prior from `catalog.yaml`, replaced by a measured
  one once `python -m provenance_core calibrate` runs its auto-graded probe set (arithmetic, combinatorics, code
  tracing, constrained reasoning; difficulty-weighted). Tiers are score bands: `nova` (>= 94), `solstice` (>= 82),
  `ember` (>= 62), `spark` (the rest). Bands and names are configurable in `catalog.yaml`; `tier-1`..`tier-4`,
  `auto` / `default` (= `solstice`) and the older names (`supernova`, `blaze`, `cinder`, ...) are aliases.
- **No silent downgrades.** A request is only served inside its tier. It fails over between same-tier models (rate
  limits, errors) and returns 503 rather than quietly answering with a weaker model. `fallback: down1 | any` opts
  into stepping down one tier or all the way.
- **Ranking inside a tier:** score first (stability), then quota headroom (spreads load before a model hits its cap),
  then latency.
- **Sticky sessions.** `X-PCore-Session` (the older `X-Tierlm-Session` also works) keeps a conversation on the model
  it started with while that model is healthy. The Anthropic and Responses adapters derive a session from a hash of
  the conversation's first user message when no header is sent, scoped per tier.
- **Ensembles inside a tier.** `ensemble: {k, strategy}`: `majority` (vote on the final line, good for exact
  answers), `judge` (the tier's strongest model merges/picks the best of k answers), `first` (latency hedging).
- **Quota-aware.** Per-model request/token windows (rpm / rph / rpd / tpm / tpd) keep every free tier under its caps.
  429s cool a model down for its Retry-After (or 60 s); repeated errors back off exponentially (30 s up to 1 h).

Request options go in a `pcore` body field (the older `tierlm` field is still read):

```bash
curl localhost:4141/v1/chat/completions -H 'content-type: application/json' -d '{
  "model": "solstice",
  "messages": [{"role": "user", "content": "How many trailing zeros does 100! have?"}],
  "pcore": {"fallback": "down1", "ensemble": {"k": 3, "strategy": "majority"}}
}'
```

Responses carry `X-PCore-Model` (the concrete model that answered) and `X-PCore-Tier`.

## Endpoints

- `POST /v1/chat/completions`: model = tier name, alias, `auto`, or a concrete catalog id
  (`<provider>/<model>`, e.g. `ovh/gpt-oss-120b`). Streaming is supported for single-model requests.
- `POST /v1/messages`, `POST /v1/messages/count_tokens`: Anthropic Messages API (Claude Code)
- `POST /v1/responses`, `GET /v1/responses/{id}`: OpenAI Responses API (Codex, Provenance)
- `GET /v1/models`: the tiers plus every usable concrete model (OpenAI and Anthropic list shapes)
- `GET /status`: tiers, scores, calibration, headroom, health, latency

## Catalog

`catalog.yaml` lists:

- **providers**: an OpenAI-compatible `base_url` and the env var holding the key (`api_key_env`).
  - `api_key_env: none` (or `auth: none`) marks a provider usable without a key; no Authorization header is sent.
    With `auth: none` plus a real `api_key_env`, the key is sent when it's set.
  - `${VAR}` in `base_url` is filled from the environment (Cloudflare's account id). A provider whose variables
    aren't set is unusable.
  - `pool: {rpm, rph, rpd, tpm, tpd}`: limits shared by all of the provider's models (Kilo's 200 requests/hour per
    IP).
  - `shared_tpd: true`: each model's `tpd` is its share of one provider-wide daily allowance; usage on any model
    spends the allowance proportionally (Cloudflare's 10k neurons/day).
  - `headers`: extra headers sent on every request.
- **models**: provider model name, prior score, context, free-tier limits, optional `id` (default
  `<provider>/<model>`).
- **tiers** and **aliases**.
- **prune** (optional, off when absent): history pruning, see "History pruning" below.

A provider without its key (or with an unset `${VAR}`) is skipped. Free-tier lineups change weekly:
`python -m provenance_core discover <provider>` lists a provider's current models; re-run `calibrate` afterwards.

## Calibration

`python -m provenance_core calibrate [--only id1,id2]` runs the probe set against every usable model and writes
scores to `--scores` (default `scores.yaml`). Those override the catalog priors on load. `tiers` shows the result
(`*` = calibrated).

## API adapters

Both adapters translate to the same single-model chat path as `/v1/chat/completions` (no ensembles).

**Claude Code** (`/v1/messages`)

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:4141 ANTHROPIC_AUTH_TOKEN="${PCORE_TOKEN:-pcore}" claude
```

Claude model ids map to tiers by family (dates and suffixes like `[1m]` are ignored); a tier name, alias or catalog
id (`ANTHROPIC_MODEL=solstice`, `/model ember`) passes straight through. A mapping value may end in `:none`,
`:down1` or `:any` to set its fallback (`PCORE_MAP_OPUS=nova:none`). If a default tier name isn't in your catalog,
the tier at the same position is used. `/v1/messages/count_tokens` returns an estimate. With `PCORE_TOKEN` set the
proxy accepts it as a bearer token (`ANTHROPIC_AUTH_TOKEN`) or as `x-api-key` (`ANTHROPIC_API_KEY`). When nothing in
reach has capacity the answer is `529 overloaded_error`, which Claude Code retries with backoff.

**Codex** (`/v1/responses`): see the README for the provider table. Add `env_key = "PCORE_TOKEN"` if the proxy has a
token. As a fallback profile: add `[profiles.free]` with `model = "solstice"` and `model_provider = "pcore"`, then
`codex --profile free`. Codex warns that it has no metadata for the model name; that's harmless. Unknown model names
(Codex defaults like `gpt-5-codex`) go to `PCORE_MAP_DEFAULT`, else the strongest tier.

What the adapters translate: text, images (base64 to data URL), tool definitions and calls (ids kept), tool results
(Anthropic `is_error` becomes a `[tool error]` prefix), tool_choice, temperature/top_p, stop sequences, streaming
(Anthropic and Responses SSE events, tool-call arguments accumulated from the upstream chat stream), usage, and
inline `<thought>` blocks (returned as reasoning). `/v1/responses` also maps Codex's `local_shell` tool (to a
function, answered with `local_shell_call` items) and freeform `custom` tools such as `apply_patch` (to a function
with one `input` string, answered with `custom_tool_call` items), and keeps a small in-memory store so
`previous_response_id` and `GET /v1/responses/{id}` work. Not translated: extended thinking requests, prompt caching,
server-side tools (web search, file search, MCP, computer use), PDF documents.

## History pruning

Agents resend their whole conversation on every request, and much of it is dead weight by then: a file read that
was read again later, the same `git status` twice, a file written whole and then rewritten, the bookkeeping lines on
every command result. `provenance_core/prune.py` is a rule-based pass (no model calls, a few milliseconds on a
500,000-character history) that shrinks that without changing what the model needs to know. It is **off by
default**.

```yaml
prune: true                      # top level of catalog.yaml: on for API models and for the web bridge
prune: {api: true}               # or one of them; each value may be a dict of options instead of true
prune: {web: {keep_turns: 6}}
```

`PCORE_PRUNE` overrides it: `1` / `all` (both), `api`, `web`, `0` (off). A chatgpt_web provider's own `web:` block
may also set `prune:` for that bridge.

**Rules**, applied only to messages older than the last `keep_turns` (4) assistant turns; user messages are never
changed, nor are tool call ids, names, or the number, order and roles of messages (every call keeps its result,
only contents shrink):

| rule | what goes |
|---|---|
| `dup_system` | a block of instructions repeated in the system/developer text (Codex can send the same developer message every turn); the first copy stays |
| `exec_header` | `Chunk ID` / `Wall time` / `Original token count` / `Output:` on old command results, down to `Exit code: N` (a running process keeps its session id) |
| `stale_read` | an old read of one file, when the file was changed afterwards by a call that succeeded and its new text is shown later (read whole again, or written whole) |
| `dup_output` | an old result identical to a later one: a stub that says so |
| `repeated_lines` | runs of lines (matched in 4-line windows, at least 200 characters) that appear again in a later result: the same file or slice read twice, a superset read later |
| `old_write` | the file text in an old whole-file write (`cat > f <<EOF`, apply_patch "Add File", a Write tool) when that file was written or read whole again later; the call and its path stay |
| `write_echo` | bulky success output of an old write/patch: the summary lines stay |
| `long_output` | an old result over `long_chars` (6000) of a command that shows no file (logs, test runs, listings, search, curl): the first `long_head` (2500) and last `long_tail` (1500) characters stay |
| `filler` | old one-line narration on a tool-calling turn ("Let me look at the tests."), only without any reason, finding or question in it |

"Appears again later" is only judged against content that is itself kept, so every stub points at something the
model can still read. Options: `keep_turns`, `step`, `min_dup_chars`, `run_lines`, `min_run_chars`, `long_chars`,
`long_head`, `long_tail`, `rules` (the list of rules to run).

**Where it runs, and why that is safe for continued chats and prompt caches**

- *Web bridge:* only on the prompt that starts a new chat, before the `fresh_prompt_chars` cut (which then has less
  to cut). Whether a later request continues that chat is still decided on the messages exactly as the client sent
  them (the session remembers those, not the pruned ones), and a continued chat is typed exactly as sent, so pruning
  can never make a chat look changed or lose its place. Mode `fresh`: the whole history is evidence.
- *API models:* mode `stable`. Only the history before a boundary is pruned, judged on that part alone; the boundary
  sits on every `step`-th (8) assistant turn and moves only after 8 more turns. Between moves, every request starts
  with byte-identical pruned history followed by the raw newer turns, so a provider's prefix cache keeps matching;
  when the boundary moves, the prefix changes once, from the first message the new evidence touches. The router also
  uses the pruned size to decide which models' context windows the request fits.

Each request records what was removed (per rule: count and characters saved): the bridge's log line (`history saved
N of M chars (rule count/chars, ...)`), `pcore.web.pruned` in its completion, and for API models a `prune:` log line
and `pcore.prune` in the response.

## ChatGPT web provider

`type: chatgpt_web` in `catalog.yaml` (`provenance_core/chatgpt_web.py`): your own signed-in chatgpt.com chat as a
completion engine. The local catalog enables bounded temporary-chat reuse for successive turns of one agent.

**Risk first.** Driving the web app with a program is against OpenAI's terms of use for ChatGPT; the account can be
rate limited, flagged or banned. Everything below is built to stay small and visible rather than to hide: it is
opt-in, serial, paced, capped, and stops at the first sign that the site wants a human.

**Status on 2026-11-21.** Verified against the local mock page and a capture of the real page's markup
(`tests/real_page_2026_11.html`), and on the real site with `driver: patchright` (the stock `playwright` driver is
answered with a Cloudflare "Just a moment..." page that never clears). The test account is on the Free plan, which
has **no model picker**: there is one model and it answers, so `chatgpt-web/luna` is accepted as it is (the page
does not say which model replied; a picker that exists but lacks the named model still fails the request).

With the first prompt that model ignored the text tool protocol in 2 of 2 tool requests and used ChatGPT's own
sandbox. With the prompt described under "Prompt design": real Provenance sessions (`provenance exec -m
chatgpt-web/luna -s workspace-write`, the unmodified 25,000-character Codex request) found a failing test, patched
it with `apply_patch`, added a function with tests, wrote a small CLI tool with its own tests, and reported the
results: 4 requests of 15 to 50 s per task.
Seen on the way and handled: a first turn that answered "the shell interface isn't available to me", a turn that
stopped after a failed command to ask for a retry (the nudge got the next command out of it), and a model-picker
check that failed on a half-loaded page. Seen once and not explained: a reply the page never
showed as finished (the request failed after `reply_timeout_s`; the page state is now saved to
`~/.tierlm/chatgpt-web-debug/` when that happens). The model's score is still a prior, not measured.

**Drivers.** `playwright` (default) is a stock Playwright-driven Chrome and says so (`navigator.webdriver` is true).
`patchright` (`driver: patchright` under the provider's `web:`, or `PCORE_CHATGPT_WEB_DRIVER=patchright`; needs
`pip install patchright`) is the approach CatGPT-Gateway uses, reduced to its core: the same installed Chrome, the
same hand-signed-in profile, launched the way patchright documents (persistent profile, no viewport override, no
extra flags), driven without the automation markers. Not taken from CatGPT-Gateway: the playwright-stealth
fingerprint patches, viewport jitter and fake mouse movement (not needed to pass the check, and each is one more
thing to be wrong about). This driver hides the automation from the site on purpose. Everything else stays: one
request at a time, the pauses, the daily cap, and stopping at any challenge that does not pass by itself.

**What it does per request**

1. Renders the initial chat request (system prompt, tools, and conversation) into one message. Long first messages
   keep the task and recent evidence while shortening old tool output and actions to `fresh_prompt_chars` (with
   `prune` on, the history is pruned first; see "History pruning"). If a later
   request extends the same transcript, it sends only the new harness events in that chat's tab. A changed transcript
   or tool setup starts fresh with the whole request. At most `max_chat_tabs` tabs stay open.
2. Opens `https://chatgpt.com/?temporary-chat=true` in its Chrome window for a new conversation and checks what a user would see: signed
   in, no challenge or limit notice, temporary chat actually on (a "Temporary Chat" heading or a "Turn off temporary
   chat" control; if the URL flag didn't take, the page's own toggle is tried once), and the wanted model selected
   in the model picker.
3. Types the message, presses send, waits until the reply has finished, and turns the rendered reply back into
   markdown (code blocks verbatim).
4. Parses tool calls out of the reply and returns an ordinary chat completion (`finish_reason: tool_calls`), which
   the Responses / Anthropic / chat front ends translate as for any provider.

Successive turns in a matching agent transcript can carry context in the same temporary chat. Different agents use
different tabs, and a server restart starts a fresh chat. Temporary chats stay out of account history. If temporary
chat can't be confirmed, nothing is sent and the provider goes unavailable.

**What it never does:** read cookie values or tokens from any browser profile (the sign-in flow reads only the
names and dates of the site's session cookie in the bridge's own profile, see "Account setup"), handle your password, forge sentinel /
proof-of-work / Turnstile tokens, solve or click through captchas or challenges, or call ChatGPT's private backend
API. With the default driver it also uses no anti-detection measures at all; `driver: patchright` is the one
exception, described above.

**Prompt design.** The request is written for the reader it actually has: a chat model inside a consumer chat
app, not the API model a harness prompt assumes. What the first version got wrong, and what the prompt does now
(`FRAME`, `INSTRUCTIONS_HEAD`, `TOOLS_HEAD`, `REMINDER_TOOLS` in `chatgpt_web.py`):

- *It has tools of its own and reaches for them.* The first prompt only said "do not use your built-in tools" and
  the model ran `pytest` in OpenAI's sandbox anyway. The prompt now says where those tools run and that the user's
  files are not there, so their output is wrong for the task, and that a tool block is the only way to touch the
  user's machine.
- *It looks for the harness tools in its function-calling interface and doesn't find them* ("the shell interface
  isn't available to me in this turn"). The prompt says a tool call is plain text in the reply and always works.
- *It treats the run as a chat.* After a failed command it wrote what it would do next and asked for a retry, with
  no tool block, which ends a Codex run. The prompt says what a reply without a block means to the harness: the run
  is over, so keep going block after block until the task is done, and a failed command is a reason to try another
  way.
- For both of those, a short reply with no call that says it cannot act, or that it is about to act, gets **one
  nudge** in the same temporary chat (`stalled` / `nudge_message`) before the harness sees it. If the model answers
  the same way again, that answer stands. The log line for the request says `nudged`.
- *The harness prompt is about another setup.* Codex's instructions speak of "function calls" and name tools the
  session may not have (`apply_patch`, `update_plan`). The prompt says how to read them, and when the request has a
  shell tool but no `apply_patch` tool, that edits go through `apply_patch` run as a shell command (Codex's
  `exec_command` intercepts it).
- *Tools are shown as TypeScript signatures*, the way OpenAI renders function tools into its models' prompts
  (`type exec_command = (_: { cmd: string, workdir?: string, ... }) => any;` with descriptions as comments), not as
  JSON schema. Shorter, and a form the model has seen.
- *The short rules are repeated*: once at the top, and again after the conversation or new events. The harness still
  supplies its own system instructions; the bridge's framing is shorter and no longer tells the model to work one
  action at a time. A client that sets `parallel_tool_calls: false` still permits only one tool call per response.

**Tool-call protocol.** Two ways to call a tool. Any tool, as JSON:

````
```tool_call
{"name": "write_stdin", "arguments": {"session_id": 3, "chars": "y\n"}}
```
````

A shell tool or a freeform tool (one required free-text argument: `cmd`, `command`, `input`, `script`), as a block
named after the tool whose body is that argument, raw. This is what makes patches and heredocs reliable, since
nothing has to be escaped into a JSON string:

````
```exec_command
apply_patch <<'EOF'
*** Begin Patch
*** Update File: calc.py
@@
-    return sum(xs) // len(xs)
+    return sum(xs) / len(xs)
*** End Patch
EOF
```
````

The model's own earlier calls are shown back to it in the same two forms. One block per call (one call per turn
when the client sent `parallel_tool_calls: false`); text after the last block is dropped. The parser also accepts
an untagged or `json` block that is exactly a call to a known tool, arguments given as a JSON string, raw newlines
inside JSON strings, JSON inside a shorthand block, and a call written without its fence. A call that is there but
unusable (invalid JSON, unknown tool, missing required argument, or no call when `tool_choice` required one) gets
**one repair round**: the error is sent as a second message in the same temporary chat and the corrected reply is
parsed; a second failure fails the request (the router moves on to the next model). Freeform tools such as
`apply_patch` are a function with one `input` string, as for every other provider. Images are not passed through:
such requests skip the bridge.

**Streaming.** The reply is read whole before anything is sent to the client (tool calls must be parsed and maybe
repaired first, and a failure can still fail over), then replayed as a normal stream. Clients see no tokens until
the reply is complete.

**Sign-in.** From the app (`POST /web/login`, see "Account setup from the app" below) or with `provenance-core
weblogin [provider] [--account b]`, which uses the same flow through the running server (or in its own process when
none runs): an ordinary window, not driven by anything, opens on the account's profile at the site's sign-in page;
you sign in by hand and it closes by itself (or you close it), then the site is checked in the background browser.
The server says at start whether the login is there (from the last check), `provenance-ui` runs
`provenance-core webstatus --boot` when it starts (a live check if the last one is older than 12 h; silent when
all is well), and a macOS notification appears when the login turns out to be missing or expired.

**When it is used.** Off unless `PCORE_CHATGPT_WEB=1` (or `enabled: true` on the provider; `PCORE_CHATGPT_WEB=0`
forces it off). When on, its model competes inside its tier like any other (the score is a prior until
`calibrate --only chatgpt-web/<model>` measures it; calibration never touches it unless named), but only for:

- tier requests on the front ends listed in `web.endpoints` (default `[responses]`: Provenance and Codex; add
  `messages` for Claude Code or `chat` for `/v1/chat/completions`), and
- requests that name it (`model: "chatgpt-web/luna"`), on any endpoint.

Ensembles never include it.

**When it steps aside.** Each of these makes the model unavailable for a while, with the reason in
`webstatus` / `GET /chatgpt-web/status`, and requests fail over inside the tier (and down a tier if the request's
`fallback` allows):

| what the page shows | unavailable for |
|---|---|
| login wall / signed out | 30 min, or until `weblogin` |
| captcha, challenge, "unusual activity", block page (after 20 s for an automatic check to pass by itself) | 6 h |
| usage-limit notice | 1 h |
| temporary chat not available | 6 h |
| the page has a model picker and the model isn't in it (unless `allow_auto_fallback` accepts Auto) | 1 h |
| you closed the window | 10 min |
| daily cap reached | until local midnight |

A request that finds the window busy for more than `queue_wait_s`, or whose prompt is over `max_prompt_chars`, goes
to the normal pool without any penalty. `provenance-core webstatus --probe` (or `weblogin`) clears a block when the
page is fine again.

**Settings** (the provider's `web:` block in `catalog.yaml`):

| key | default | |
|---|---|---|
| `daily_cap` | 200 | requests per local day (repair messages count) |
| `min_gap_s`, `max_gap_s` | 4, 9 | pause between requests, a random time in that range |
| `queue_wait_s` | 45 | how long a request waits for the one in flight |
| `named_wait_s` | 300 | the same wait for a request that names the model (no pool to fail over to; lets a few agents share the window) |
| `reply_timeout_s` | 600 | longest wait for one reply |
| `idle_close_s` | 900 | close the window after this long without a request |
| `max_prompt_chars` | 120000 | larger requests skip the bridge |
| `fresh_prompt_chars` | 60000 | target size for a new tab's first prompt; older history is shortened if needed |
| `prune` | unset | history pruning for a new tab's first prompt (true or options; see "History pruning"); unset: the catalog's top-level `prune` |
| `strict_model` | true | fail rather than answer with a different model than the one named |
| `allow_auto_fallback` | false | allow a named model request to use the page's Auto selection when that name disappears |
| `reuse_chat`, `max_chat_tabs` | false, 6 | reuse a matching agent's temporary chat and cap open tabs |
| `lean`, `lean_system_chars` | false, 3000 | replace a harness base prompt longer than `lean_system_chars` (Codex sends ~18,000 characters) with a few lines, drop its skills list and the tools an unattended run never uses, keep only cwd / shell / date of the environment block |
| `accounts` | none | e.g. `[main, b, c]`: one Chrome profile, window, daily count and cooldown per signed-in account (`provenance-core weblogin --account b`). A request goes to the account already holding its chat, else the least busy one that is not cooling down; `daily_cap` is per account |
| `backoff_base_s`, `backoff_max_s` | 60, 900 | a "temporary limits" / failed-to-load notice, or two page errors in a row: stay away this long, doubling each time in a row |
| `quiet` | false | macOS: hide the bridge's Chrome as soon as it opens (your own Chrome is untouched). `headless: true` is stopped by the site's browser check, so this is the way to keep it off the screen |
| `browser` | `chrome` | which browser runs the window: `chrome` (the installed Google Chrome), `bundled` (the bridge's own background-only Chrome for Testing, see "Bundled browser" below; a request waits while it is downloaded the first time), `auto` (bundled when it is ready; while it is prepared in the background, requests skip the bridge with no penalty and nothing else is opened; the choice for a shipped app). `share:` guests use their host's browser |
| `browser_fallback` | unset | `auto` only: `chrome` = use the installed Chrome (a window that can show) while the bundled one is not ready, the old `auto` |
| `browser_trim` | true | bundled only: also pass the trimmed switch set |
| `browser_source` | `cft` | bundled only: `cft` (Chrome for Testing) or `chrome` (a background-only clone of the installed Google Chrome, this Mac only) |
| `browser_watch_s` | 2 | bundled only: how often every window's position is checked and put back off screen |
| `login_timeout_s` | 900 | a sign-in window left open this long is closed (then the account is checked) |
| `name`, `login_url`, `session_cookies` | the site's | what the app calls the provider, where the sign-in window opens, and `{domains, names}` of the cookie that means signed in (`CHATGPT_SITE` / `SITES`) |
| `endpoints` | `[responses]` | front ends whose tier requests may use it |

A model's `model` is the name to pick in the page's model picker (`auto` = leave the page's choice; on a page with no picker, as on the Free plan, the account's one model answers). The page
markers the bridge reads live in `PROBE_JS` / `READ_JS`; when chatgpt.com changes its markup, that is the place to
update.

**Commands and endpoints:** `weblogin`, `webstatus [--probe] [--boot]`; `GET /chatgpt-web/status[?probe=1]`,
`POST /chatgpt-web/release[?reset=1]` (close the window; `reset` also forgets a block). State (today's count, last
check, block reason) is in `~/.tierlm/chatgpt_web.json`.

### Bundled browser

`browser: bundled` (or `auto`) runs the window in the bridge's own copy of Chrome for Testing instead of the
installed Google Chrome (`provenance_core/browser_host.py`). The copy's Info.plist says `LSBackgroundOnly`: no Dock
icon, no app-switcher entry, and it can never become the active app, so it cannot take focus. It starts with no
window (`--no-startup-window`) and the bridge creates its one window already off screen and in the background
(`Target.createTarget {newWindow, background, left: 20000, top: 20000}`): no window is ever drawn on screen; macOS
keeps a 40x41 px corner of it inside the display. Tabs are background tabs of that window. `quiet`'s hiding and
parking are not needed and are skipped. The same profile directory is used, so the sign-in carries over (both
launches use `--use-mock-keychain --password-store=basic`, the same cookie-encryption key). Signing in always opens a
normal window (the bundled build's plain copy): a background-only app cannot take the keyboard.

- **Getting it.** `python -m provenance_core.browser_host` prepares it ahead of time; otherwise `serve` starts
  preparing it in the background (bundled or auto mode), or the first launch in `bundled` mode does, or
  `POST /web/browser/prepare`. Progress and errors are in `GET /web/accounts` → `browser` (`preparing`, `stage`
  look / download / unpack / copy, `progress` 0-1 while downloading, `error` in plain words). A failed attempt is
  retried by requests at most every 5 min (`RETRY_S`), by `POST /web/browser/prepare` at once. Sources, in
  order: the app, or the folder holding it, named by `PCORE_BROWSER_APP` (this is how Provenance.app, run from
  anywhere, should point at its own `Contents/Resources/browser`: set it in the router's environment when the app
  starts it), an app under `Provenance.app/Contents/Resources/browser/` in /Applications or ~/Applications, the copy
  already prepared, Playwright's cache, a download from Google's Chrome for Testing bucket (~190 MB zip, md5-checked). It
  must be at least as new as the installed Chrome's major version and as the profile's `Last Version` (an older
  browser must not open a newer profile); the closest major from above is chosen, so the installed Chrome (used by
  `weblogin`) can still open the profile. When Chrome updates to a new major version, a matching build is prepared
  on the next launch. If nothing suitable can be had, the installed Chrome is used as before.
- **Shipping it in Provenance.app** (not done yet): a build step downloads the Chrome for Testing build for the
  current Stable milestone into `ui/vendor/browser/` and electron-builder copies it unmodified with
  `"extraResources": [{"from": "vendor/browser", "to": "browser"}]` (outside the asar) to
  `Provenance.app/Contents/Resources/browser/Google Chrome for Testing.app`. browser_host finds it there and makes the
  background-only clone under `<PCORE_HOME>/browser` on first use (same APFS volume: no copy). Chrome for Testing
  is only ad hoc (linker) signed, so the app's own ad-hoc `codesign --deep` pass (`scripts/adhoc-sign.cjs`) has no
  Google signature to break; that pass's `--verify --deep --strict` must still be checked once with the browser
  inside. Shipping it unmodified also leaves a normal-window copy for `weblogin`. It adds ~370 MB to the app (~190 MB to the DMG); a newer one is still
  downloaded when the installed Chrome or the profile moves past the shipped major version.
- **On disk.** `<PCORE_HOME>/browser/<version>/plain/` (the build as downloaded, ~370 MB) and `.../background/` (an
  APFS clone with the edited Info.plist, re-signed ad hoc: an edited plist breaks the signature and the kernel kills
  the process; the clone costs ~0.1 MB, though `du` counts it again), `current.json` (the version in use). Older
  versions are removed once nothing runs from them.
- **Lifetime.** The bridge starts the browser itself and attaches over a local debugging port (`--remote-debugging-
  port=0`, 127.0.0.1). A watchdog (a shell waiting on a pipe only the bridge holds) ends the browser if the bridge
  dies, so a browser nobody can see is never left running. `release` / idle close sends `Browser.close` and waits
  for the process. A bundled window that vanished is restarted on the next request without the "closed" cooldown.
- **Nothing ever shows, including after a crash or with the screen locked.** What could still draw something, and
  what stops it: (1) macOS moves windows onto a display when the display layout changes (an external display
  unplugged at sleep, a lid opened, the bridge started with no display at all); and (2) a page can open a popup
  window of its own after one of the bridge's clicks. A watcher on the browser's own CDP session (`park_windows`:
  `Target.getTargets` / `Browser.getWindowForTarget` / `Browser.setWindowBounds`, nothing attached to any page)
  checks every window every `browser_watch_s` and at once when a page opens, and puts any window that is not where
  it was parked back off screen (a minimized one is left alone; a maximized or full-screen one is made normal
  first). Tested by moving the window 60 px further onto the display: back within 1.5 s. (3) After a crash or the
  watchdog's kill, Chrome offers to restore the session in a bubble: `--hide-crash-restore-bubble`, and with
  `--no-startup-window` no session is restored into a window. (4) The app cannot be activated (LSBackgroundOnly),
  so nothing can take focus at any point. Not tested: an actual display change, a launch with the screen locked
  (both reduce to (1)), and whether macOS shows its "quit unexpectedly" dialog when a background-only app crashes
  (it is not expected to; the trimmed set turns off Chrome's own crash reporter). The window server lists the live
  bundled windows at x 1670, y 1071 of a 1710x1112 display (the 40x41 px corner) and none as on screen otherwise.
- **Trimmed switches** (`browser_trim`, `TRIM_ARGS` / `TRIM_FEATURES`): `--process-per-site` (tabs of one site share
  a renderer), `--disable-back-forward-cache`, no spare renderer, no phishing classifier, domain reliability, pings,
  metrics upload, crash reporter, download protection, optimization-guide fetches/models, autofill server, CT
  updater, What's New; `--autoplay-policy=user-gesture-required`, `--mute-audio`. The renderer sandbox stays on
  (Playwright's launch passes `--no-sandbox`). Left out on purpose because a page can tell: GPU / software GL /
  device-scale-factor switches, `--disable-extensions`, `--disable-default-apps`, `--disable-component-update` and
  `MediaRouter` (patchright removes these from Playwright's defaults). A fingerprint of 38 page-visible groups
  (globals, navigator, userAgentData, WebGL, canvas, audio, EME, permissions, codecs, AI APIs) is identical with and
  without the trim, and chatgpt.com's logged-out page shows its input box with no challenge.
  Measured (Chrome for Testing 157, M-series Mac, scratch profile, sum of RSS over the browser's processes; 4 tabs
  of a heavy news page, each loaded twice): idle with one blank tab ~650 MB / 1-1.5 % CPU either way; 4 heavy tabs
  2,305-2,383 MB / 150-155 % CPU / 19 processes with today's switches, 1,648-1,702 MB / 121-129 % CPU / 14 processes
  trimmed (about -29 % memory, -18 % CPU).
- **Known differences from the installed Chrome.** The same fingerprint, taken from a background-only clone of the
  installed Chrome 157.0.8103.80, differs from Chrome for Testing 157.0.8103.78 in one thing only:
  `navigator.userAgentData.brands` / `fullVersionList` have no `Google Chrome` entry (`Chromium` + `Not-A.Brand`;
  the user-agent string, Widevine, WebGL, plugins and globals are the same). `browser_source: chrome` avoids even
  that: the bundled browser is then a background-only APFS clone of the installed Google Chrome (`<PCORE_HOME>/
  browser/chrome/`, re-cloned whenever Chrome updates, never older than the profile); it is for this Mac only, as
  Google Chrome cannot be shipped inside Provenance.app. Device-bound Google sessions (DBSC) keep their keys per
  browser app, so a Google sign-in made in the installed Chrome may have to be renewed in the bundled one.

### Account setup from the app (`/web/*`, `provenance-core/web_setup.py`)

Everything a desktop app needs to set up the web chats without a terminal: pick a model, sign in in a window the
router opens, see it verified. Same auth as the other routes. Every `reason` and 409 `detail` is written to be shown
to the person as it is.

| endpoint | |
|---|---|
| `GET /web/accounts` | `browser` (`mode`, `source`, `ready`, `version`, `preparing`, `stage`, `progress` 0-1 while downloading else null, `error`), `accounts` (the global list of ids), `providers`: `id`, `name` ("ChatGPT", "Gemini", ...), `url`, `enabled`, `shares_browser_with`, `models` (`id`, `label`, `tier`, `model`, `accounts`: null = every account), `accounts` (per account: `signed_in` true / false / null = never checked, `checked_at`, `email` (the identity the page showed at the last check, else null), `blocked`, `reason`, `blocked_for_s`, `today`, `daily_cap`, `signing_in`, `window_open`, `login` {`state`, `reason`}) |
| `POST /web/login` `{provider, account, bounds?}` | start a sign-in; answers once the window is open (`{"state": "open"}`), or `preparing` while the browser is downloaded; 409 when a sign-in already holds that account's browser. `bounds` `{left, top, width, height}` places the window (default 80, 60, 1000x800), e.g. beside the app |
| `GET /web/login?provider=&account=` | `{state, reason}` (plus `provider`, `account`, `since`, `how`: cookie / closed / timeout); poll about once a second |
| `POST /web/login/cancel` `{provider, account}` | closes the window, nothing is checked: `{"state": "idle"}` |
| `POST /web/accounts` `{account}` | a new account slot (a new browser profile) on every web provider |
| `DELETE /web/accounts/{id}` | removes it from every provider; its profile directory is never deleted (`profile_kept`, `note` say so) |
| `POST /web/browser/prepare` | start preparing the bundled browser if it is not ready; answers with `browser` |

**Accounts are global.** One account is one browser profile, shared by every site (`share:`), so the same ids are
listed under every provider. The catalog's `accounts:` lists are merged, and what was added or removed from the app is
kept in `<PCORE_HOME>/web_accounts.json` and applied when the catalog is loaded: catalog.yaml is never rewritten. An
account added at run time joins the running pools at once (a provider that had one account becomes a pool, keeping
its open browser).

**The sign-in, step by step** (one provider + account; `LoginFlow`):

```
idle ──POST──> preparing ──> open ──(session cookie seen | window closed | login_timeout_s)──> verifying ──> signed_in
                                                                                                     └──> failed
preparing / open / verifying ──POST /web/login/cancel──> idle      (window closed, nothing checked)
signed_in / failed stay until the next POST /web/login or cancel for that provider + account
anything that cannot be done (no browser to sign in with, the profile held by another window) ──> failed
```

1. *preparing*: waits while the bundled browser is downloaded (`reason`: "Getting the browser ready (45%)...").
   Pauses every bridge on the account's profile (all sites: their requests skip the account with no penalty and
   their background browser is not started), waits up to 120 s for a chat in progress there, closes the background
   browser (a request still running after that is skipped, not penalized).
2. *open*: one ordinary window on the profile at the site's sign-in page (`login_url`), with the same build that
   runs in the background: the bundled build's plain (normal) copy, the installed Chrome only when there is no
   bundled build (or in `chrome` mode). Started with `open -n -a` so macOS brings it to the front like an app the
   user opened (measured: the frontmost app became it); always with an explicit position and size, because Chrome
   would otherwise reopen the profile's last window where the background browser left it, off screen. No debugging
   port, no automation switch, nothing attached.
3. Ends on the first of: the site's session cookie is new or changed in the profile's cookie database (then 3 s
   for the site's redirect); the window was closed (the app quit, or its last window closed while the app runs, as
   the window server's list shows); `login_timeout_s`.
4. *verifying*: the window is quit as Cmd+Q does (`NSRunningApplication.terminate`, no permission needed), the pause
   is lifted, and `Bridge.check` opens a fresh chat in the background browser without sending anything; it also
   reads who is signed in from the page (`IDENTITY_JS`: ChatGPT's page data, Google's account button). Then
   `signed_in` (with a `reason` when the site reports a usage limit), or `failed`: "The window was closed before
   sign-in finished.", "The sign-in window was closed after 15 minutes because sign-in did not finish. Try
   again.", "<site> still asks to sign in...", a human check, no temporary chats, the site not loading.

A failed or cancelled sign-in lifts the pause too: an account is never left with neither a sign-in window nor a
background browser that can start. If the router dies, a watchdog closes the window.

**Why sign-in is watched from outside the window.** Google refuses sign-in in browsers it considers automated
("This browser or app may not be secure"), and `navigator.webdriver` is the documented tell. Measured on Chrome for
Testing 157 with a page reporting what it sees: no debugging port → `webdriver: false`; `--remote-debugging-port`
with nothing attached → `webdriver: true` (as with Playwright or patchright attached). Watching the window over a
debugging port would therefore mark it automated to every site, Google's sign-in included; hiding that flag would be
defeating the check. So the window runs with no automation at all and is watched through the profile: Chrome keeps
its cookie database (`Default/Network/Cookies`, SQLite) readable while it runs (measured), and writes new cookies
to it within about 30 s (measured 30.8 s; it also writes after 512 changes). Only `host_key`, `name` and the date
columns are read, never `value` / `encrypted_value`. Two more measurements decided how the window is closed: a
SIGTERM makes Chrome exit at once without writing cookies set in the last 30 s (a just-finished sign-in would be
lost), while a Cmd+Q style quit writes them. Session cookies per site (`session` in `CHATGPT_SITE` / `SITES`, or
`session_cookies` in `web:`): ChatGPT `__Secure-next-auth.session-token*` on chatgpt.com, Google `SID` /
`__Secure-1PSID` / `__Secure-3PSID` on google.com, Qwen and Z.ai `token` (not confirmed). With no names, only closing
the window or the timeout ends a sign-in.

**Verified** against the mock page (`tests/test_web_setup.py`): signed out → window → the page signs itself in →
cookie seen → window closed → checked → `signed_in` with the e-mail; the background browser closed and every site on
the account skipping while the window is open; closed by hand after and before signing in; timeout; cancel; 409; the
accounts endpoints and their persistence; the browser's progress and `auto` waiting for it. Those tests start the
window headless and directly (`WebSetup.launch = "exec"`) so nothing shows. Once with the real thing: the bundled
plain copy opened with `open -n -a`, small in a corner, came to the front, signed in on the mock page, was detected,
quit and verified in 10 s. **Not verified:** a real sign-in on any site (the cookie names above, Google's reaction to
this window, the identity selectors, and how each site's sign-in page behaves when opened directly).

## Subscription fallback

`python -m provenance_core usage` reads Claude Code's and Codex's own rate-limit data (Codex session rollouts;
Claude Code's status-line input, recorded by `python -m provenance_core statusline-tee '<your status-line
command>'`). `python -m provenance_core env claude|codex [--force]` prints the exports that point a tool at
provenance-core once it's at `PCORE_LOW_PCT` percent, or nothing (exit 1) if it isn't low or the proxy has no usable
model. A wrapper can switch automatically:

```sh
eval "$(provenance-core env claude)"; exec claude "$@"
```

The `usage` module is stdlib-only, so other programs can import it (`from provenance_core import usage`).
State (`claude_limits.json`) lives in `~/.tierlm` by default (the pre-rename location; override with `PCORE_HOME`).

## Environment variables

Every setting is read as `PCORE_<NAME>`, falling back to the pre-rename `TIERLM_<NAME>`.

| variable | default | |
|---|---|---|
| `PCORE_TOKEN` | unset | bearer token required by the proxy |
| `PCORE_CATALOG` | `catalog.yaml` | catalog path |
| `PCORE_SCORES` | `scores.yaml` | calibrated scores path |
| `PCORE_PORT` | `4141` | `serve` port |
| `PCORE_MAP_OPUS` | `nova` | tier for opus-family requests |
| `PCORE_MAP_SONNET` | `solstice` | sonnet-family requests |
| `PCORE_MAP_HAIKU` | `ember` | haiku-family requests (Claude Code's side calls) |
| `PCORE_MAP_DEFAULT` | sonnet mapping | any other model name (on `/v1/responses` the default is the strongest tier) |
| `PCORE_ANTHROPIC_FALLBACK` | `any` | fallback for `/v1/messages` |
| `PCORE_RESPONSES_FALLBACK` | `down1` | fallback for `/v1/responses` |
| `PCORE_ADAPTER_MAX_TOKENS` | `16384` | cap on agents' max_tokens (they ask for 32k-64k, which free tiers reject or count against tokens/minute) |
| `PCORE_HOME` | `~/.tierlm` | subscription-fallback state |
| `PCORE_BASE_URL` | `http://127.0.0.1:4141` | where `usage` / `env` look for the proxy |
| `PCORE_LOW_PCT` | `95` | subscription usage that counts as low |
| `PCORE_CHATGPT_WEB` | unset | `1` turns the chatgpt-web provider on, `0` forces it off |
| `PCORE_BROWSER_APP` | unset | the bundled browser to use: a Chrome for Testing `.app`, or the folder that holds one (Provenance.app's `Contents/Resources/browser`) |
| `PCORE_BROWSER_DIR` | `<PCORE_HOME>/browser` | where the bundled browser is prepared |
| `PCORE_PRUNE` | unset | history pruning: `1` / `all`, `api`, `web`, or `0` to force it off (overrides the catalog's `prune`) |

Provider keys use the provider's own variable names (`GEMINI_API_KEY`, `CLOUDFLARE_API_TOKEN`, ...; see
`catalog.yaml`).

## Compatibility with the old name

- `python -m tierlm ...` and `from tierlm import usage` still work through a small `tierlm/` shim package.
- `TIERLM_*` env vars, the `X-Tierlm-Session` header and the `tierlm` request field are still read.
- `env codex` exports both `PCORE_*` and `TIERLM_BASE_URL` / `TIERLM_TOKEN` (Provenance reads the latter).
- Tier entries in `/v1/models` keep `owned_by: "tierlm"`, which existing clients use to find the tiers.

## Tests

`.venv/bin/python -m pytest -q` (upstreams are mocked). The chatgpt-web page driver is tested against a local mock
page (`tests/mock_chatgpt.py`) in headless Chrome; those tests are skipped without Playwright and Google Chrome.
The bundled-browser tests run the same mock page in the background-only browser (its window off screen); the
Chrome for Testing one is skipped unless a copy is already prepared (`python -m provenance_core.browser_host`).
`tests/test_web_setup.py` runs the sign-in flow against the mock page's sign-in page (`login: "cookie"`) with a
headless window on scratch profiles.
