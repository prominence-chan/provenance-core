# provenance-core: technical reference

## How requests are routed

- **Tiers by measured strength.** Every model has a 0-100 score: a prior from `catalog.yaml`, replaced by a measured
  one once `python -m provenance_core calibrate` runs its auto-graded probe set (arithmetic, combinatorics, code
  tracing, constrained reasoning; difficulty-weighted). Tiers are score bands: `nova` (>= 94), `solstice` (>= 82),
  `spark` (>= 62), `ember` (the rest). Bands and names are configurable in `catalog.yaml`; `tier-1`..`tier-4`,
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
id (`ANTHROPIC_MODEL=solstice`, `/model spark`) passes straight through. A mapping value may end in `:none`,
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

## ChatGPT web provider

`type: chatgpt_web` in `catalog.yaml` (`provenance_core/chatgpt_web.py`): your own signed-in chatgpt.com chat as a
stateless completion engine.

**Risk first.** Driving the web app with a program is against OpenAI's terms of use for ChatGPT; the account can be
rate limited, flagged or banned. Everything below is built to stay small and visible rather than to hide: it is
opt-in, serial, paced, capped, and stops at the first sign that the site wants a human.

**Status on 2026-11-21.** Verified against the local mock page and a capture of the real page's markup
(`tests/real_page_2026_11.html`); the text tool protocol with real Provenance requests answered by a pool model;
failover when the login is missing and when the page is challenged. On the real site: with the stock `playwright`
driver every load after the first was answered with a Cloudflare "Just a moment..." page that never clears (6 h
block, requests go to the normal pool). With `driver: patchright` the check passes and real replies come back
(seven requests, 9 to 50 s each; headings, lists, tables, code blocks, math and a ```` ```tool_call ```` block read
back intact). Two things found there: the page's markup had changed (turns are `[data-turn-key]` /
`[data-response-body]`, code blocks are an editor widget; both generations are read now), and the test account was on
the Free plan, which has **no model picker**: `chatgpt-web/luna` is correctly reported as not selectable, and the
Free default model (`auto`, served by `gpt-6`) ignored the text tool protocol in 2 of 2 tool requests and used
ChatGPT's own sandbox instead (it "ran" `pytest` and read `/etc/hosts` in OpenAI's container, not on this machine).
So `auto` on a Free account is not usable for agent work; prompt fidelity for long requests, the model picker on a
paid plan and the model's score are still unmeasured.

**Drivers.** `playwright` (default) is a stock Playwright-driven Chrome and says so (`navigator.webdriver` is true).
`patchright` (`driver: patchright` under the provider's `web:`, or `PCORE_CHATGPT_WEB_DRIVER=patchright`; needs
`pip install patchright`) is the approach CatGPT-Gateway uses, reduced to its core: the same installed Chrome, the
same hand-signed-in profile, launched the way patchright documents (persistent profile, no viewport override, no
extra flags), driven without the automation markers. Not taken from CatGPT-Gateway: the playwright-stealth
fingerprint patches, viewport jitter and fake mouse movement (not needed to pass the check, and each is one more
thing to be wrong about). This driver hides the automation from the site on purpose. Everything else stays: one
request at a time, the pauses, the daily cap, and stopping at any challenge that does not pass by itself.

**What it does per request**

1. Renders the whole chat request (system prompt, tool definitions, every message, earlier tool calls and their
   results) into one message.
2. Opens `https://chatgpt.com/?temporary-chat=true` in its Chrome window and checks what a user would see: signed
   in, no challenge or limit notice, temporary chat actually on (a "Temporary Chat" heading or a "Turn off temporary
   chat" control; if the URL flag didn't take, the page's own toggle is tried once), and the wanted model selected
   in the model picker.
3. Types the message, presses send, waits until the reply has finished, and turns the rendered reply back into
   markdown (code blocks verbatim).
4. Parses tool calls out of the reply and returns an ordinary chat completion (`finish_reason: tool_calls`), which
   the Responses / Anthropic / chat front ends translate as for any provider.

Nothing carries over between requests: every request is a new temporary chat, so ChatGPT's memory and history are
not involved and nothing is saved in the account. If temporary chat can't be confirmed, nothing is sent and the
provider goes unavailable.

**What it never does:** read cookies or tokens from any browser profile, handle your password, forge sentinel /
proof-of-work / Turnstile tokens, solve or click through captchas or challenges, or call ChatGPT's private backend
API. With the default driver it also uses no anti-detection measures at all; `driver: patchright` is the one
exception, described above.

**Tool-call protocol.** The prompt tells the model to call a tool with a fenced block:

````
```tool_call
{"name": "shell", "arguments": {"command": ["ls", "-la"]}}
```
````

One block per call (one call per turn when the client sent `parallel_tool_calls: false`). The parser also accepts
an untagged or `json` block that is exactly a call to a known tool, arguments given as a JSON string, raw newlines
inside JSON strings, and a call written without its fence. A call that is there but unusable (invalid JSON, unknown
tool, missing required argument, or no call when `tool_choice` required one) gets **one repair round**: the error
is sent as a second message in the same temporary chat and the corrected reply is parsed; a second failure fails
the request (the router moves on to the next model). Freeform tools such as `apply_patch` are a function with one
`input` string, as for every other provider. Images are not passed through: such requests skip the bridge.

**Streaming.** The reply is read whole before anything is sent to the client (tool calls must be parsed and maybe
repaired first, and a failure can still fail over), then replayed as a normal stream. Clients see no tokens until
the reply is complete.

**Sign-in.** `provenance-core weblogin` opens an ordinary Chrome window (not driven by anything) on the dedicated
profile `~/.tierlm/chatgpt-web-profile`; you sign in by hand and quit that Chrome, then the profile is checked once
through the bridge. The server says at start whether the login is there (from the last check), `provenance-ui` runs
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
| the model isn't in the picker | 1 h |
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
| `reply_timeout_s` | 600 | longest wait for one reply |
| `idle_close_s` | 900 | close the window after this long without a request |
| `max_prompt_chars` | 120000 | larger requests skip the bridge |
| `strict_model` | true | fail rather than answer with a different model than the one named |
| `endpoints` | `[responses]` | front ends whose tier requests may use it |

A model's `model` is the name to pick in the page's model picker (`auto` = leave the page's choice). The page
markers the bridge reads live in `PROBE_JS` / `READ_JS`; when chatgpt.com changes its markup, that is the place to
update.

**Commands and endpoints:** `weblogin`, `webstatus [--probe] [--boot]`; `GET /chatgpt-web/status[?probe=1]`,
`POST /chatgpt-web/release[?reset=1]` (close the window; `reset` also forgets a block). State (today's count, last
check, block reason) is in `~/.tierlm/chatgpt_web.json`.

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
| `PCORE_MAP_HAIKU` | `spark` | haiku-family requests (Claude Code's side calls) |
| `PCORE_MAP_DEFAULT` | sonnet mapping | any other model name (on `/v1/responses` the default is the strongest tier) |
| `PCORE_ANTHROPIC_FALLBACK` | `any` | fallback for `/v1/messages` |
| `PCORE_RESPONSES_FALLBACK` | `down1` | fallback for `/v1/responses` |
| `PCORE_ADAPTER_MAX_TOKENS` | `16384` | cap on agents' max_tokens (they ask for 32k-64k, which free tiers reject or count against tokens/minute) |
| `PCORE_HOME` | `~/.tierlm` | subscription-fallback state |
| `PCORE_BASE_URL` | `http://127.0.0.1:4141` | where `usage` / `env` look for the proxy |
| `PCORE_LOW_PCT` | `95` | subscription usage that counts as low |
| `PCORE_CHATGPT_WEB` | unset | `1` turns the chatgpt-web provider on, `0` forces it off |

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
