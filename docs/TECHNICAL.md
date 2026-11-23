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
- **Quota-aware.** Request/token windows (rpm / rph / rpd / tpm / tpd) keep every free tier under its caps. They
  are counted per (model, key): a provider can hold several keys, each another account of the person's with its
  own allowance (see "Several keys per provider"). A request to a model goes out with the key that has the most
  headroom. A 429 (or a 402, out of credit) cools that key down for its Retry-After (or 60 s; an hour for a 402)
  and the request carries on with the model's next key, then the next model; a key the provider rejects as invalid
  or revoked is marked and skipped; other errors are the model's and back off exponentially (30 s up to 1 h).

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
- `GET /v1/sources`: per tier, what it can be served from (each signed-in chat model, the API models as one "Token
  pool"), how much of each is left, whether it can be pinned, and `needs`: `keys` (the tier has API models but none
  with a key: the pool is listed rather than left out), `signin` (no account of that chat site signed in), else
  null. For the pool, `remaining` is the share (0-1) left over all its models and keys, `capacity` the same added
  up (1.0 per model per key with its whole allowance), `keys` how many keys that is
- `GET /v1/pcore/usage?key=<thread>&turn=<turn>[&since&until]`: which models served a conversation's turn (the usage
  ledger, `ledger.py`; an API model's row also has `keys: {<key id>: requests}`, which of the provider's keys
  served, by id), and `context`: per flag, how many of the turn's Responses requests needed it to fit their
  model (`context_signal.py`): `compacted` (an API model got a brief instead of the old history), `trimmed` (even
  then the kept turns' tool output had to be cut), `summarised` / `shortened` (a web chat's fresh first message got a
  brief / a cut history), `restarted` (a web chat was full and the request carried on in a new one), `over` (no
  model of the tier takes the history at all: the request failed). `compacted` and `summarised` are normal for any
  long thread; `trimmed`, `restarted` and `over` mean it no longer fits even compacted, and Provenance.app then
  suggests continuing the thread in Codex or Claude Code
- `/codex-sub/<key>/backend-api/*`: Codex on your own ChatGPT sign-in, forwarded to OpenAI with its history
  pruned (see "Codex on your own sign-in")
- `GET /status`: tiers, scores, calibration, health, latency, and per model `headroom` (the best of its keys, 0-1),
  `capacity` (its keys added up: 1.0 per key with its whole allowance left) and `keys: [{id, available,
  headroom}]`
- `/pool/keys`, `/pool/help`: the pool's API keys and how to get them (see "Several keys per provider" and
  "Provenance.app's router")
- `GET /health`: `{ok, service, version, pid, home, uptime_s, api: {providers, usable, models, keys, slots}, web:
  {providers, enabled}, tiers}` (`keys`: the keys in use over all providers; `slots`: usable models times the keys
  or anonymous access each can be called with); cheap (nothing is opened or fetched), for the app that started the router and for telling
  which router answers on a port

## Catalog

`catalog.yaml` lists:

- **providers**: an OpenAI-compatible `base_url` and the env var holding its first key (`api_key_env`); further
  keys are added from the app (below).
  - `api_key_env: none` (or `auth: none`) marks a provider usable without a key; no Authorization header is sent.
    With `auth: none` plus a real `api_key_env`, the anonymous access and the keys are used side by side (Kilo,
    OVH).
  - `${VAR}` in `base_url` is filled from the key's own `extras`, else the environment (Cloudflare's account id).
    A key without a value for it is unusable.
  - `pool: {rpm, rph, rpd, tpm, tpd}`: limits shared by all of the provider's models, counted per key (OpenRouter's
    50 free requests a day per account); `pool_per: ip` counts them once for the machine, whatever key is sent
    (Kilo's 200 requests/hour per IP).
  - `shared_tpd: true`: each model's `tpd` is its share of one account-wide daily allowance; usage on any model
    spends the allowance proportionally (Cloudflare's 10k neurons/day), per key.
  - `until: <date>`: the last day (UTC) the provider is used. From the next day it is not usable and its models
    are no longer offered (Inception's Mercury API, which shuts down after 2026-12-28); `GET /pool/keys` says so.
  - `check: {path: /models}` or `{url: ...}`: a GET that only a valid key gets a 2xx for and that spends no quota,
    run when a key is added or replaced; `soft: true` reports a refusal without setting the key aside (Cloudflare,
    whose check also depends on the token's permissions). Absent: no check, the first request tells.
  - `headers`: extra headers sent on every request.
- **models**: provider model name, prior score, context, free-tier limits, optional `id` (default
  `<provider>/<model>`).
- **tiers** and **aliases**.
- **prune** (optional, off when absent): history pruning, see "History pruning" below.
- **compact** (optional, off when absent): summarising compaction, see "Context compaction" below.
- **extends** (optional): another catalog file (relative to this one) loaded first, with this one laid over it
  (`catalog.overlay`): mappings merge key by key, an entry with `remove: true` is dropped, `models` entries are
  matched to the base's by id (`id`, else `provider/model`) and merged into them (or dropped with `remove: true`),
  new ones are added at the end, and any other value, lists included, replaces the base's. `catalog.app.yaml` is
  one (see "Provenance.app's router").

A provider without a usable key (or with an unset `${VAR}`) is skipped. Free-tier lineups change weekly:
`python -m provenance_core discover <provider>` lists a provider's current models; re-run `calibrate` afterwards.

### Several keys per provider (`pool_keys.py`)

A person with several accounts at a provider (a personal and a work one, a free and a paid one) can use all of
them: each key is another allowance.

- **A key** has a stable short `id` (`main` for the first, the provider's variable; `k` + 6 hex characters for a
  further one; `anon` is the anonymous slot of a provider that works without a key), an optional `label`, an
  optional `tier` hint (`free` | `paid`; absent = unknown), optional `limits` over the catalog's free-tier numbers
  for that key alone, `extras` (the provider's `${VAR}` values for that key: each Cloudflare token has its own
  account id), `enabled`, and the day it was `added`.
- **Limits per key**: `{"scale": 5}` multiplies every catalog limit of the provider (its models' and its `pool`);
  a field (`rpm`, `rph`, `rpd`, `tpm`, `tpd`) replaces the catalog's number wherever the catalog has that field
  (model or pool), `null` lifting it; `{"models": {"<model>": {...}}}` replaces them for one model. Default: the
  catalog's numbers.
- **Routing** (`state.py`, `router.py`): usage windows and rate-limit cooldowns are kept per (model, key); a
  model's `headroom` is its best key's, its `capacity` the sum over its keys. `Router.call` tries the model's keys
  with headroom, most first (so requests alternate between equal keys), moving on at a 429 / 402 (that key cools
  down alone) or a rejection (HTTP 401, or a 400 / 403 whose text says the key is invalid or revoked: the key is
  marked unusable with the reason, in this process, until it is replaced, turned on again with `PATCH
  {"enabled": true}`, or the router restarts), and gives up on the model only when no key is left. A named model
  with no headroom on any key is still tried on its best one, as before. Which key served a request travels with
  it (a context variable), so streamed usage and the usage ledger's `kid` land on that key.
- **Storage**: the first key stays where it was: the provider's environment variable, from `keys.env` / the shell
  or set from the app into `<PCORE_HOME>/keys.json` as `"<VAR>": "<key>"`. Further keys, and what was said about
  the first (label, tier, limits), are in the same file under `"@keys": {"<provider>": [{id, key, label, tier,
  limits, extras, enabled, added}]}` (mode 0600, the home 0700, written through a temporary file). Files from
  before load unchanged. An older router reads only the top-level strings, so after a rollback the first keys
  still work; if that older router then writes the file (a `PUT /pool/keys`), the `@keys` section is dropped.
- **The credential store instead?** `credstore.py` (the macOS Keychain / `keyring`) already holds the web
  sign-in passwords and could hold API keys too: they would then not sit in a readable file that backups and
  sync tools pick up. Against it today: the first keys of an existing install are in `keys.env` (plain text,
  read by the launch script, outside this code), the router runs unattended under launchd where a locked
  Keychain cannot be asked, and Linux without `keyring` has no store at all. Recommended next step, not done
  here: an opt-in `PCORE_KEY_STORE=keychain` under which keys set from the app go to the store and `keys.json`
  keeps only ids and metadata, with `keys.json` as the fallback where no store is available, and an explicit,
  reversible `provenance-core keys migrate` / `keys export` pair rather than a silent move of existing keys.
- **API** (the names the app is built against; no answer, log line or error ever carries a key, only its id,
  label, tier, `usable`, `reason`, `added` and `last4`):
  - `GET /pool/keys` → `{providers: [...], removed: [...]}`. Per provider, as before: `id`, `usable`,
    `anonymous`, `vars: [{name, set, from: "app" | "env" | null}]`, `models`, `tiers`; and now `keys: [{id, label,
    tier, usable, reason, added, last4, limits, enabled, source: "app" | "env"}]`, `multiple_keys: {allowed:
    "yes" | "check_terms" | "no", note}`, `until`, `expired`, `note`. `removed`: providers taken out of the pool,
    `{id, name, removed, reason, leftover_key, note}`; a key still saved for one is ignored (and `DELETE
    /pool/keys/{id}` forgets it).
  - `PUT /pool/keys/{provider}` `{"values": {"<VAR>": "<key>"}, label?, tier?, limits?}`: set or replace the first
    key (its meaning before). `POST /pool/keys/{provider}` `{key, label?, tier?, extras?, limits?,
    limits_scale?}`: add one (the provider's first if it has none); 409 if that exact key is already there.
    Both answer with the provider's view plus `key_id` and `check: {ok: true | false | null, detail}` (`null`: the
    provider has no check, or it gave no clear answer; `false`: the key was saved but is set aside).
  - `PATCH /pool/keys/{provider}/{key_id}` `{label?, tier?, limits?, limits_scale?, enabled?}`;
    `DELETE /pool/keys/{provider}/{key_id}`; `DELETE /pool/keys/{provider}` removes all of them. A key that comes
    from the environment (`keys.env`) cannot be removed from the app (409, naming the variable); its label, tier
    and limits can be set.
  - Errors: 404 for an unknown provider or key id (for a removed provider, with the reason), 409 with `detail`
    otherwise.
- **Terms**: `multiple_keys` comes from `pool_help.yaml` and says what the provider's own terms say about one
  person using several accounts: `no` where they plainly forbid a second account (OpenRouter, Kilo, Groq, Cohere),
  `check_terms` where they forbid getting round limits in general words or could not be read, `yes` where nothing
  against it was found. It is shown, not enforced: a second key is accepted everywhere, because a person may hold
  a legitimately separate (paid, organization) account. Nothing here creates accounts.

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

## Codex on your own sign-in (`/codex-sub/…`, `codex_sub.py`)

A pass-through for Codex (the provenance binary) on the user's own ChatGPT / Codex sign-in: the binary keeps its own
login (auth.json, token refresh, account id) and is only pointed here, and the router forwards its requests to
OpenAI's Codex backend with the history pruned on the way. Provenance.app's "Run Codex through Provenance" uses it.

```sh
provenance app-server -c model_provider="openai" \
  -c openai_base_url="http://127.0.0.1:4141/codex-sub/<key>/backend-api/codex" \
  -c features.enable_request_compression=false
```

- **Forwarding**: `/codex-sub/<key>/backend-api/<path>` goes to `<upstream>/backend-api/<path>`
  (`PCORE_CODEX_SUB_UPSTREAM`, default `https://chatgpt.com`, the URL the binary itself would use) with the method,
  query, body and the caller's own headers (minus hop-by-hop ones and `X-PCore-Prune`); the answer comes back as it
  came: status, headers (`Retry-After`, `Content-Encoding`, rate-limit headers), body streamed chunk by chunk. A
  websocket upgrade gets `426`, which makes the binary use HTTP for the session at once (over its Responses websocket
  it sends only a turn's new items, which leaves nothing to prune). What the binary sends to its ChatGPT base
  (`chatgpt_base_url`: usage, accounts, workspace routing) and its token refreshes still go to OpenAI directly; leave
  `chatgpt_base_url` alone, or the binary's workspace routing sends model requests to the workspace's backend instead.
- **Treatment**: only `POST …/codex/responses`. Its `input` items are pruned in place (`prune_responses.py`): the text
  of old tool outputs (`function_call_output`, `custom_tool_call_output`, `local_shell_call_output` whose `output` is
  a string or a list of text parts only, which is what code mode returns: a status part, then the script's text) and
  the file text in old whole-file write calls, by the rules above, in mode `stable`. An output with an image or any
  other part is left whole. Current Codex models run every tool through code mode (one `exec` call with a script), so
  the router can't tell a file view from a log there: `long_output` cuts old long outputs of those calls to their head
  and tail, `dup_output` and `repeated_lines` work as usual, and the read/write rules (`stale_read`, `old_write`,
  `write_echo`) only see plain shell, apply_patch and file tool calls. Never touched:
  `instructions`, tools, messages of any role, reasoning items (encrypted content included), ids, call ids, the
  number and order of items; `dup_system` and `filler` are not used; a call carrying encrypted content or arguments
  keeps its arguments. Consecutive requests of a thread share their pruned prefix byte for byte between boundary
  moves (`tests/test_codex_sub.py` checks it), so OpenAI's prompt cache keeps hitting. On by default; `prune:
  {codex: false}` (or options) in the catalog, `PCORE_PRUNE=0`, the header `X-PCore-Prune: off` or the body field
  `"pcore": {"prune": false}` (not forwarded) turn it off. A body that can't be read (zstd without a zstd module,
  unknown items) is forwarded byte for byte. A request whose pruning changed nothing is also forwarded byte for byte.
- **Compaction** is left to Codex: its own remote compaction (`…/responses/compact`) is forwarded untouched; compact.py
  works on chat messages and asks a model for its brief, which has no safe counterpart for encrypted Responses items.
- **Ledger**: each model request is an entry (thread / turn from `client_metadata`, tier `codex`, the token counts and
  `cached` tokens from `response.completed`, `pruned: {before, after}` in characters of `input`); `/v1/pcore/usage`
  sums them as `pruned` and `cached_tokens`. A `codex-sub:` log line names the model and the characters saved.
- **The key**: `Authorization` belongs to OpenAI here, so the caller proves itself in the path instead:
  `<key>` = the first 32 hex digits of sha256(`"provenance-codex-sub\0" + PCORE_TOKEN`) (`codex_sub.path_key`), so a
  key seen in a log or an error message neither gives the token away nor opens any other route. Without
  `PCORE_TOKEN` the router is open and any key is taken. The access log shows the path as `/codex-sub/***/…`.
  `GET /codex-sub/<key>/_health` answers `{ok, upstream, prune}` without forwarding anything. Nothing on this route
  logs, prints or stores a header value, an account id or a token.

## Context compaction

Pruning only removes what the model no longer needs; when a history is still too big for where it is going, something
has to go. `provenance_core/compact.py` decides what: the old part of the conversation becomes a written brief and the
newest turns stay as sent. It is **off by default**.

```yaml
compact: true                    # top level of catalog.yaml: API models and the web bridge
compact: {web: true}             # or one of them; each value may be a dict of options instead of true
compact: {api: {model: spark}}   # with a model-written narrative (see below)
```

`PCORE_COMPACT` overrides it: `1` / `all`, `api`, `web`, `0` (off). A chatgpt_web provider's `web:` block may set
`compact:` for that bridge.

**What the model is given**: the system message(s) and the opening (everything before the agent's first turn: the
environment block and the task) verbatim; one user message holding the brief of turns 1..N; turns N+1.. verbatim. The
cut is always at an assistant turn, so every kept tool call has its result and every kept result its call. The brief
(rules only: no model call, 10-20 ms on a 500,000-character history, deterministic) has, in order of priority:

| section | from |
|---|---|
| what the user asked for | every user message after the opening, verbatim (each clipped only when even that does not fit) |
| files changed | every file a call changed: created / written whole / edited / deleted, how many times, at which turn, failed changes, how often read, and the last change itself (patch lines, the head of a whole write) for as many of the newest as fit |
| what was tried and failed | each failed command or change with its first error line, and whether a later run or change worked |
| the agent's plan | its last `update_plan` |
| commands and outcomes | every command that is not a look-around (tests, builds, git, scripts), latest run of each: exit code, run count, the last output line |
| the agent's notes | its own prose, most recent kept first (answers to the user marked) |
| files read | the names of files read but not changed |
| where it stood | its last words, and the turn the verbatim part starts at |
| with room to spare | the latest text of each file (a whole write, or a read after its last change), then the latest output of searches and listings |

The sections share the brief's size by weight; one that needs less than its share leaves the rest to the others. The
brief is at most `brief_chars` (16,000) and `brief_share` (0.3) of the budget; on the web bridge it may also grow into
whatever the kept turns leave (`use_room`).

**Where the cut goes, and why it is stable.** On the same grid as pruning's stable boundary: every `step`-th assistant
turn (8 for API models, 4 on the web), at least `keep_turns` (4) from the end; the earliest grid turn at which the
opening, a full-size brief and everything after the cut fit in the budget. That is judged on the history as the
client sent it (not the pruned one, which can shrink when pruning's own boundary moves), and the history only grows
at its end, so the cut only ever moves forward, one grid step at a time. The brief is made from the history before
the cut as sent, so it depends on that part alone; it is cached under a hash of it (call ids left out, since a
Responses client may regenerate them on resume), in memory (64 cuts) and, when a model wrote part of it, on disk
under `<PCORE_HOME>/compact/`. Between two moves of the cut every request starts with byte-identical messages up to
and including the brief, followed by the newer turns as the rest of the pipeline sends them. If even the latest cut
leaves too much (a few huge recent outputs), the kept turns' tool output is shortened (`trimmed` in the record); that
part then differs from request to request.

**Where it runs, and how it fits with pruning and chat continuation**

- *Web bridge:* where the blunt cut ran, i.e. only on the first message of a new chat (the first request, after a
  restart, a changed transcript, or when the site says the chat is full) when it is over `fresh_prompt_chars`.
  Order: `lean` → prune (`fresh` mode) → compaction (the brief from the lean history as sent, the kept turns from the
  pruned one) → rendered; if it is still over the limit (or there are too few turns to summarise) the blunt cut has
  the last word. Whether a later request continues the chat is decided on the client's own messages (the session
  remembers those), and a continued chat is typed exactly as sent, so neither pass can make a chat look changed: a
  continued chat is never affected. A model narrative, if configured, is written before the bridge takes its window
  (`Bridge.warm`), never while holding it; the restart path uses one only if it is already cached.
- *API models:* only for a model whose context window is smaller than the (pruned) request, and only one with at
  least `min_context` (32,000) tokens: such a model is then a candidate again, after the models of its tier that take
  the history whole. The budget is `fill` (0.85) of its context minus `max_tokens`. Pruning (stable mode) runs first
  and is what the kept turns are sent as; the brief replaces the oldest part, so pruning's stable prefix and the
  brief's line up: when pruning's boundary moves, the bytes from the cut on change once, as they do with pruning alone.

**Who writes the brief.** By default the rules alone. With `model: <tier or API model id>` a cheap API model of the
router's own pool (via `Router.complete` on that tier, `web: False`, so never a web bridge) writes three short
sections, *decisions made / what was tried and did not work / in progress and the next step*, from the rules' digest
(at most `model_input_chars`, 40,000), never from the raw history, and only when the part summarised is at least
`model_min_chars` (100,000). The rules' sections stay; the model's text replaces most of the notes. It is bounded
(`model_chars`, 4,000), timed out (`model_timeout_s`, 30) and on any failure (timeout, no capacity, an empty reply)
left out; whatever comes out is cached for that cut, so the prefix stays fixed. Measured (below), the model's text was
faithful but short and added little that the rules had not already kept, at several seconds per call and 4 failures
in 24, so rules-only is the default.

**Record.** Each compaction is reported as `{scope, before, after, budget, boundary, summarised: {messages, turns,
chars}, kept: {messages, turns}, brief_chars, writer ("extractive" or "model:<id>"), model_error, cached, trimmed, ms,
key}`: for API models a `compact:` log line and `pcore.compact` in the response; on the bridge `pcore.web.compaction`
and `history summarised (...)` in its log line. (`/v1/responses` does not pass `pcore` through, as with pruning.)

**The blunt cut** (`compact_initial_prompt`, still used when compaction is off) used to drop old tool calls but keep
some of their results, so the model saw outputs without the commands that produced them, and it protected only the
first user message (for Codex, the environment block; the task could be shortened). It now keeps old calls with their
arguments shortened (a command's first line, a patch's file lines), removes old turns only as a whole call-and-results
group, and keeps every message before the first assistant turn as sent.

**Measured** (2026-11-22) on the owner's Provenance sessions: the 3 sessions whose fresh prompt goes over 60,000
characters, plus two chained conversations built from them (all big sessions one after another: 265 messages, 550,000
characters; every session one after another: 417 messages, 585,000 characters, 50 user messages). 71 request points
(up to 20 per conversation, every request whose lean + pruned fresh prompt is over 60,000), each through the web
bridge's fresh-prompt path. "Named with state": the path and a change word on one line. A user message line, a failed
command (one with an error line in its output) or a note "survives" when its text is in the prompt.

| measure | blunt cut (main) | blunt cut (fixed) | compaction |
|---|---|---|---|
| prompt characters (mean, limit 60,000) | 48,454 | 50,785 | 54,000 |
| files touched again later, named | 33/33 | 33/33 | 33/33 |
| every file changed so far, named with its state | 28/97 (29%) | 44/97 (45%) | 97/97 (100%) |
| later user messages at least 90% intact | 709/1019 (70%) | 664/1019 (65%) | 1019/1019 (100%) |
| opening task lines kept | 75% | 100% | 100% |
| failed commands named with their error | 4/111 (4%) | 67/111 (60%) | 111/111 (100%) |
| the agent's notes (80+ characters) kept | 630/993 (63%) | 567/993 (57%) | 775/993 (78%) |
| distinct tool-output lines still shown | 24% | 22% | 23% |
| tool results shown without their call | 2,922 | 0 | 0 |
| time (median / max) | 4 / 107 ms | 6 / 177 ms | 9 / 39 ms |

What compaction loses: raw tool output beyond the newest turns (the same share as the blunt cut keeps, but chosen
differently: the latest text of files and searches instead of the head and tail of every old output), the older
notes when there are many, and anything only visible in an old output that no section picks up (a value printed by a
script, a line in a log). For API models (one 65,536-token model, every request of the chained conversations, prune
on): 133 compacted requests, the cut moved 16 times (only forward), 115 requests reused their cut's brief, median
request 348,000-409,000 → 103,000 characters, 14 ms for a new cut and 6 ms for a cached one. With the same cut, a request's
messages are a byte-identical prefix of the next one's except when pruning's boundary moves or the kept turns had to
be trimmed. The model narratives (24 calls through the running router: `spark` and `ember`, 6 cut points twice): 20
usable, 4 empty replies; `spark` median 4 s (two calls over 30 s), `ember` median 11 s; 600-1,800 characters; 95% of
the file names and commands they quote are in their digest; and they named a file the agent touched in its next three
turns in 2 of 20 cases (the rules' "where it stood": 0 of 6).

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
   `prune` on, the history is pruned first; with `compact` on, the old part becomes a brief instead; see "History
   pruning" and "Context compaction"). If a later
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
names and dates of the site's session cookie in the bridge's own profile, see "Account setup"), handle your password
(unless you save a sign-in for an account yourself, see "Saved sign-in and automatic sign-in"), forge sentinel /
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

**When it is used.** Off unless turned on. In order: `PCORE_CHATGPT_WEB=0` forces every web provider off; a choice
made in the app (`PATCH /web/providers/{id}`, kept in `<PCORE_HOME>/settings.json`) decides next; then
`PCORE_CHATGPT_WEB=1` turns them on; then `enabled: true` on the provider. When on, its model competes inside its tier like any other (the score is a prior until
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
| `compact` | unset | summarising compaction instead of the blunt cut for a new tab's first prompt (see "Context compaction"); unset: the catalog's top-level `compact` |
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
| `skip_unchecked` | false | an account no check has ever seen signed in (`signed_in` still null: a new one, nothing signed in yet) is skipped without penalty and not counted as capacity, instead of having its browser opened to find the sign-in page. `catalog.app.yaml` turns it on |
| `notify` | true | macOS notifications when the provider goes unavailable, and at start when a sign-in is missing |
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
- **Shipping it in Provenance.app** (done: `ui/scripts/fetch-browser.ts`): a build step downloads the Chrome for Testing build for the
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
| `GET /web/accounts` | `browser` (`mode`, `source`, `ready`, `version`, `preparing`, `stage`, `progress` 0-1 while downloading else null, `error`), `accounts` (the global list of ids), `providers`: `id`, `name` ("ChatGPT", "Gemini", ...), `url`, `enabled`, `enabled_by` (`app`, `env`, `catalog`, or `env-off`: PCORE_CHATGPT_WEB=0, not changeable from the app), `daily_cap` (per account), `shares_browser_with`, `models` (`id`, `label`, `tier`, `model`, `accounts`: null = every account), `accounts` (per account: `signed_in` true / false / null = never checked, `checked_at`, `email` (the identity the page showed at the last check, else null), `blocked`, `reason`, `blocked_for_s`, `today`, `daily_cap`, `signing_in`, `window_open`, `login` {`state`, `reason`}, `plan`, `plan_label`, `plan_note`, `needs_confirmation`, `autologin`: see the two sections below) |
| `PUT /web/accounts/{provider}/{account}/autologin` `{agree: true, username, password}` | save a sign-in for this account and turn automatic sign-in on. 400 without `agree: true` (exactly true), a name or a password; 409 with the reason where it is not offered (Gemini, Z.ai) or the system has no credential store. Write-only: the password goes to the system's credential store and no answer carries it. Answers with the account as `GET /web/accounts` lists it, plus `provider` |
| `PATCH …/autologin` `{enabled}` | on or off, keeping what is saved; 409 when nothing is saved, or after a wrong password (save a new one) |
| `DELETE …/autologin` | forgets it: the password leaves the credential store; the name, the setting and the record leave the state |
| `POST …/autologin/try` | one attempt now, under the same rules as an automatic one (409 with the reason when they do not allow it, or the account is signed in). Answers when it is over (up to a minute or two) with the account plus `try` {`at`, `result`, `detail`}; `?wait=0` answers at once (`try`: null) and the attempt goes on: poll the account's `autologin.running`, then `last_try` |
| `POST /web/accounts/{provider}/{account}/confirm` `{confirm: "paid_plan"}` | "use it anyway" for an account on a paid plan; 400 for another `confirm`, 409 when there is nothing to confirm |
| `POST /web/login` `{provider, account, bounds?}` | start a sign-in; answers once the window is open (`{"state": "open"}`), or `preparing` while the browser is downloaded; 409 when a sign-in already holds that account's browser. `bounds` `{left, top, width, height}` places the window (default 80, 60, 1000x800), e.g. beside the app |
| `GET /web/login?provider=&account=` | `{state, reason}` (plus `provider`, `account`, `since`, `how`: cookie / closed / timeout); poll about once a second |
| `POST /web/login/cancel` `{provider, account}` | closes the window, nothing is checked: `{"state": "idle"}` |
| `POST /web/login/show` `{provider, account}` | brings the open sign-in window back to the front (`NSRunningApplication`, as clicking its Dock icon does): the login view plus `shown` (false when macOS refused, or for a headless test window, which is never brought forward); 409 when no window is open |
| `PATCH /web/providers/{id}` `{enabled?, daily_cap?}` | turns the site on or off, sets its daily cap (per account, 1-10000); kept in `<PCORE_HOME>/settings.json` and applied to the running bridges at once; answers with the provider as `GET /web/accounts` lists it. A browser no enabled site uses any more is closed. 409 with a `detail` for anything else (another key, a wrong type, PCORE_CHATGPT_WEB=0) |
| `POST /web/accounts` `{account}` | a new account slot (a new browser profile) on every web provider |
| `DELETE /web/accounts/{id}` | removes it from every provider; its profile directory is never deleted (`profile_kept`, `note` say so) |
| `POST /web/open` `{provider, account, url, bounds?}` | opens a pool provider's key page for the person in a visible window of that account's profile, already signed in (see "A key page in the signed-in window"): `{state: "open", url, …}`; 400 when `url` is not a `keys_url` / `signup_url` of `pool_help.yaml`; 409 when the page is not on a site that provider's sign-in covers, the account is not signed in, or a window is already open on the account |
| `GET /web/open?provider=&account=` | `{state, reason, url}`: idle / preparing / open / closed / failed |
| `POST /web/open/cancel` `{provider, account}` | closes that window: `{"state": "idle"}` |
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

### A key page in the signed-in window (`POST /web/open`, `OpenFlow`)

A person who signed in to Gemini in the app signed in with a Google account, and Google AI Studio, where the free
Gemini API key is made, uses the same sign-in. So the app can open that page in the profile that is already signed
in. `OpenFlow` is the sign-in window's machinery (`LoginFlow`) without the watching and the check: the bridges on
the profile are paused and their background browser closed, one ordinary window opens at the page, and it ends when
the person closes it, on cancel, or after `open_timeout_s` (30 min); then the pause lifts. One window per profile,
sign-in windows included; while it is open the account reports `signing_in` and takes no requests.

- **Only listed pages.** `url` must be exactly a `keys_url` or `signup_url` of `pool_help.yaml`, and on a site the
  provider's sign-in covers: `opens` in `SITES` (Gemini: `google.com` and its subdomains, so today only
  aistudio.google.com). The account must be signed in (`signed_in` true in its state).
- **The window is the person's.** It has no debugging port and nothing is attached to it: the router cannot click,
  type, read or copy anything on the page. The person makes the key and pastes it into the app's key field.
- **`open_with`.** `GET /pool/help` adds `open_with: [{provider, account}, …]` to an entry whose `keys_url` a
  signed-in account can open, worked out at request time; absent otherwise.
- Each Google account's key has its own free allowance, but nothing pools the keys of several Google accounts: the
  pool keeps one Google key, as before.

Verified against the mock (`tests/test_web_signin.py`): refusals (a URL that is not listed, a listed page on another
site, an account that is not signed in), open / status / cancel, closing by hand, the pause while it is open, and
`open_with` only for matching signed-in accounts. Not verified: AI Studio itself in that window.

### The account's plan, and the paid-plan confirmation

For ChatGPT (`"plan": True` in `CHATGPT_SITE`; no other site is checked, a Gemini account on a paid plan included),
the account's plan is read from the page whenever a check finds it signed in, after an automatic sign-in, and again
on the first new chat `plan_refresh_s` (6 h) after the last reading. It comes from the data chatgpt.com serves with
the page (`#client-bootstrap`, where the bridge already reads the e-mail address): `session.account.planType`, else
the same key a few levels down (`PLAN_JS`). Nothing is clicked or fetched, and only that one label is kept:

- `plan`: `free` (the label is `free`), `paid` (any other label; `plan_label` is the site's own word: `plus`, `pro`,
  `team`, `enterprise`, `edu`, ...), or `unknown` (the page did not show it and nothing is known for this identity;
  `plan_note` says the account is used as it is). null for sites that are not checked. A page that does not show the
  plan leaves what was known for the same identity.
- `needs_confirmation: "paid_plan"`: a paid plan the person has not agreed to use. The account stays connected and
  idle: `_precheck` skips it without penalty, it counts as no capacity, and `reason` says in plain words that using
  it still spends that account's own usage, likely faster than Codex would, and that the person can use it anyway
  (`POST …/confirm`) or connect a different account. The sign-in flow ends `signed_in` with the same words as its
  `reason`. `unknown` never holds an account back.
- The agreement is withdrawn when the account is seen on the free plan, or another identity is seen on the slot: a
  later paid plan is asked about again. Moving between paid plans is not.

**Not verified:** the key on the real site with a signed-in account (it is the one the site's session data is known
to use; a paid account whose page carries the plan under another key reads as `unknown` and is used). **On
upgrade:** a ChatGPT account already in use that turns out to be on a paid plan goes idle at its next reading until
it is confirmed in the app.

### Saved sign-in and automatic sign-in (`web_signin.py`, `credstore.py`)

The sites sign an account out from time to time, and then a person has to open the sign-in window again. A person
can instead save the account's sign-in and agree to have it used: off by default, per provider + account, only
through `PUT …/autologin` with `agree: true`.

**Where things are kept.** The password: only in the operating system's credential store, under the service
"provenance-core web sign-in" and the name `<site>/<account>@<8 hex of the router home>`. `credstore.py` is the
interface (`get`, `set`, `delete`): the `keyring` package when installed (Keychain, Windows Credential Manager,
Secret Service), else on macOS the `security` tool, with the secret written on its standard input (its interactive
mode; base64-encoded so no character of a password can be read as a command), never in a command line or the
environment; no store, no feature (`supported: false`). It is read at the moment of an attempt, typed into the
site's password field and dropped. The user name, the setting and the record of attempts are in the account's state
file under `autologin`. The password is never in a state file, the settings, a log line, an event, an exception
(errors from the browser driver are reported by their type only, since their text can quote what was typed), an API
answer or a debug dump: `_dump` keeps nothing of a sign-in page or of a page with a filled password or code field,
and the routine itself never reads or records text from a sign-in page (the page reports flags, see below).

**When it runs.** When the bridge has established that the account is signed out: `Bridge._block` with kind `login`
after its doubt logic, so never on the first sightings on an account that worked. Also on "try now". Never while a
sign-in window is open on the account, and never when the account is signed in. At most `autologin_max_per_day` (3)
attempts per account per local day, the next no sooner than `autologin_gap_s` (15 min) after the first and twice
that after the second; then it stays signed out and `reason` / `autologin.note` say so. Saving the sign-in again
waives the gap once, not the day's count.

**How.** In the background browser and profile the bridge uses, in a tab of its own that is always closed
afterwards: open the site's sign-in page, type the saved name and password into the form its `signin` description
names (in `SITES` / `CHATGPT_SITE`: `url`, `hosts`, `start`, `email`, `continue`, `password`, `submit`, `failure`; a
control is a CSS selector or `{css, text}`), send it, and confirm with the check used for any page
(`Bridge._open_chat`: a fresh chat opens signed in; nothing is sent). One routine (`web_signin._drive`) for every
site; each field is typed into at most once per attempt.

**Hard stops.** The page is looked at before every step (`LOOK_JS` returns flags only) and the attempt ends there,
touching nothing more, on: a captcha or human check (an automatic browser check gets `challenge_wait_s` to pass by
itself, as elsewhere; nothing is clicked), a prompt for a code (two-step, e-mail, SMS), a passkey prompt, a
suspicious-sign-in or locked-account notice, a terms or consent screen, a page on a host that is not the site's own
(single sign-on), the site still asking to sign in after the form was accepted, or an unexpected page after
something was typed. The result is `needs_person` with the reason in `detail`, and nothing automatic happens again
until the person has signed in (the sign-in window) or saved the sign-in again. Nothing solves or works around a
check, reads a code, or loops. When the site says the name or password is wrong the result is `wrong_password` and
automatic sign-in turns off for the account until a new password is saved, so a stale password cannot lock the
account. `error` (the page did not load, the browser could not start) is the only result tried again, within the
limits.

**What the app sees.** `autologin` per account: `supported`, `reason` (why not), `enabled`, `saved`, `username`,
`attempts_today`, `last_try` {`at` (epoch s), `result`: `ok` / `wrong_password` / `needs_person` / `error`,
`detail`}, and also `attempts_per_day`, `history` (the last ten, with `trigger`: auto / manual), `paused` (null,
`wrong_password`, `needs_person`), `note` (one sentence when the person should know something), `next_try_in_s`,
`running`. Each attempt is one line in `<PCORE_HOME>/web-events.jsonl` (`kind: "autologin"`, `result`, `trigger`).
Removing an account forgets its saved sign-ins.

**Which sites.**

| site | automatic sign-in | the form's description |
|---|---|---|
| ChatGPT | yes | e-mail step read off `chatgpt.com/auth/login` signed out (2026-11-23): `input[type=email][name=username]`, one submit button "Continue". The password step (on `auth.openai.com`) could not be seen without typing an address: taken to be a password field and a submit button "Continue" / "Log in". The wrong-password wording is web_signin's general pattern plus `aria-invalid` on the field |
| Qwen | yes | read off `chat.qwen.ai/auth` signed out: opens on "Login with verification code"; the button "Log in with a password" shows one form with `input[name=email]`, `input[type=password][name=password]`, submit "Log in". Wrong-password wording: the general pattern |
| Gemini | no (`supported: false`) | never a Google account: Google's sign-in guards against automated browsers and the password opens far more than this app |
| Z.ai | no (`supported: false`) | its own e-mail + password form (behind "Continue with Email"), but with a human verification in the form every time |

**Verified** against the mock site's sign-in form (`tests/test_web_signin.py`; headless, scratch profiles, a
credential store in memory): off by default; 400 without agreement; write-only; forget; an account found signed out
is signed in again and the next request is served; each hard stop ends the attempt with nothing typed past it; a
wrong password turns it off until a new one is saved; the daily limit and the growing gaps; a slow form; the
two-field form behind a switch; the plan (free, plus, team, missing) and the confirmation; and the test password
searched for, as UTF-8 and UTF-16, in every file the run leaves (state, events, dumps, the browser profile) and in
the output, the API answers and the error texts. The Keychain adapter was tried once on this Mac with a throwaway
item (written, read back, deleted). **Not verified:** a sign-in on any real site. ChatGPT's password page and both
sites' wrong-password wording are assumptions; if a page differs, the attempt ends as `needs_person` (unexpected
page) or `error` rather than typing anywhere else. **Risks:** it is automation of a sign-in form, which the sites'
terms do not allow, like the rest of the bridge; a site may answer repeated automatic sign-ins with a check or a
lock, which is why the limits are low and every stop waits for a person.

## Provenance.app's router

Provenance.app (`ui/` in the Provenance repo) runs this router itself when no router answers at its configured URL:
a relocatable CPython (python-build-standalone) with the wheels its tests pass with and `provenance_core` as plain
files, in `Provenance.app/Contents/Resources/router/` (`ui/scripts/fetch-router.ts`). A router that already answers
(the owner's launchd service on :4141) is used instead and left alone. What the router has for it:

- **`catalog.app.yaml`**: `extends: catalog.yaml` and changes only what a new user's first start needs: one account
  per site, `skip_unchecked`, no notifications, Z.ai left out (no models), OVH needing a key (its anonymous quota
  is one request a year), Gemini's models on every account. With no API keys at all, the chat sites work once
  signed in, Kilo's anonymous models serve the pool where they can, and every tier whose API models all need a key
  lists the pool with `needs: "keys"`.
- **Settings changed from the app** (`provider_settings.py`): `PATCH /web/providers/{id}` (above), kept in
  `<PCORE_HOME>/settings.json` as `{"providers": {"<id>": {"enabled": false, "daily_cap": 120}}}` and laid over the
  catalog at load, like `web_accounts.json`; `catalog.yaml` is never rewritten.
- **API keys from the app** (`pool_keys.py`): `GET /pool/keys`, `PUT` / `POST` / `DELETE /pool/keys/{provider}`,
  `PATCH` / `DELETE /pool/keys/{provider}/{key_id}`: any number of keys per provider, kept in
  `<PCORE_HOME>/keys.json` (mode 0600, the home 0700); see "Several keys per provider" under "Catalog" for the
  shapes, the storage and the routing. The first key is put into the process environment, where a provider reads
  it, so it works at once; a key set from the app wins over the same variable from the shell. Nothing logs a
  value: uvicorn logs request lines only, and errors name variables and key ids, never their contents.
- **How to get the keys** (`pool_help.py`, `pool_help.yaml`): `GET /pool/help` returns `{checked, assistant_notes,
  providers: [...], removed: [...], considered: [...]}`, one entry per API provider in the order worth doing them
  (most free capacity for least effort): `id`, `name`, `free_tier` (`anonymous` | `free` | `trial` | `none`),
  `what_you_get`, `needs`, `time_minutes`, `signup_url`, `keys_url`, `key_var` and `extra_fields[].var` (the names
  `PUT /pool/keys/{id}` takes), `steps`, `key_format`, `key_pattern` (a regex, only where the provider documents
  the shape), `gotchas`, `terms_note`, `multiple_keys` (above), `until` (a provider that is temporary),
  `confidence` (`seen` | `docs-only` | `uncertain`), `checked`, `sources`, and `experience` (below). `removed`:
  `{id, name, removed, reason, key_var, sources}` for providers that are no longer free (Mistral, Cerebras);
  `considered`: `{name, allowance, why_not, source}` for providers looked at and not added.
  `GET /pool/help?provider=<id>` returns `{checked, provider}` (404 for an unknown id). The text is for the app and
  for an assistant walking someone through the sign-ups; [pool-keys.md](pool-keys.md) is the same data, written by
  `python -m provenance_core.pool_help` (a test fails when the two differ).
- **What happened when someone followed the steps** (`pool_help.py`): `POST /pool/help/{provider}/outcome`
  `{result: "saved_working" | "saved_rejected" | "skipped" | "gave_up", minutes?, step_notes?: [{step: <number,
  1 = the first, or text>, ok: bool, note?}], labels_seen?: [str], source: "assistant" | "manual"}` appends a
  dated line to `<PCORE_HOME>/pool-help-outcomes.jsonl` (mode 0600) and answers `{recorded, experience}`. No keys
  and nothing personal: a report in which any string matches a provider's `key_pattern`, starts like a known
  kind of key, holds a long run of mixed letters and digits (a token, an account id) or an email address is
  refused whole with a 400 that does not quote it; so is one with a field other than those. `GET /pool/help`
  then gives each provider `experience: {tries, worked, last_worked, common_trouble: [{step, count, notes}]}`
  from that file (`common_trouble`: the steps reported as not matching the page, most often first).
  `python -m provenance_core.pool_help --review [home]` prints, per provider, those steps beside their written
  text with the notes given, the labels people saw that the steps never mention, and the minutes it took, for a
  person or an agent to correct `pool_help.yaml` by. The instructions are never rewritten automatically.
- **`serve --watch-stdin`**: the app keeps the write end of the router's stdin; when the app goes away, however it
  goes (quit, crash, `kill -9`), stdin reaches EOF and the router shuts down as on Ctrl+C: the sign-in window and
  every background browser are closed on shutdown (they also have watchdogs of their own). If that hangs, it exits
  after 20 s anyway.
- **How the app starts it**: `python3 -I -B -m provenance_core serve --catalog <Resources>/router/catalog.app.yaml
  --scores <home>/scores.yaml --host 127.0.0.1 --port <free port> --watch-stdin`, cwd and `PCORE_HOME` =
  `~/Library/Application Support/Provenance/router`, `PCORE_TOKEN` random per launch (any local web page can POST to
  localhost), `PCORE_CHATGPT_WEB=1`, `PCORE_CHATGPT_WEB_DRIVER=patchright`, `PCORE_BROWSER_APP=<Resources>/browser`,
  and `PLAYWRIGHT_NODEJS_PATH` at a wrapper that runs patchright's driver on the app's own Electron in Node mode, so
  the driver's own 117 MB Node is not shipped. The app hands the same URL and token to `provenance app-server`
  (`TIERLM_BASE_URL`, `TIERLM_TOKEN`, read by its built-in tierlm provider).
- **For the app's end-to-end check**: `tests/serve_mock.py` runs this server with the web chats pointed at the mock
  page (`tests/mock_chatgpt.py`), a scratch home and every browser headless (the sign-in window started directly
  with `--headless=new`), and prints `{url, mock, home, pid}`; `ui/scripts/router-e2e.ts` drives the real UI
  against it.

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
| `PCORE_HOME` | `~/.tierlm` | where the router keeps its state (web accounts and settings, keys.json, browser profiles, the bundled browser, subscription-fallback state) |
| `PCORE_BASE_URL` | `http://127.0.0.1:4141` | where `usage` / `env` look for the proxy |
| `PCORE_LOW_PCT` | `95` | subscription usage that counts as low |
| `PCORE_CHATGPT_WEB` | unset | `1` turns the web providers on (one turned off from the app stays off), `0` forces them all off |
| `PCORE_BROWSER_APP` | unset | the bundled browser to use: a Chrome for Testing `.app`, or the folder that holds one (Provenance.app's `Contents/Resources/browser`) |
| `PCORE_BROWSER_DIR` | `<PCORE_HOME>/browser` | where the bundled browser is prepared |
| `PCORE_PRUNE` | unset | history pruning: `1` / `all`, `api`, `web`, `codex`, or `0` to force it off (overrides the catalog's `prune`) |
| `PCORE_CODEX_SUB_UPSTREAM` | `https://chatgpt.com` | where `/codex-sub/<key>/backend-api/…` forwards to |
| `PCORE_COMPACT` | unset | summarising compaction: `1` / `all`, `api`, `web`, or `0` to force it off (overrides the catalog's `compact`) |

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
headless window on scratch profiles. `tests/test_app_router.py` covers what Provenance.app needs (`catalog.app.yaml`,
settings and keys from the app, `/health`, `needs`, `--watch-stdin`); `tests/test_multikey.py` covers several
keys per provider, removed and temporary providers and the setup outcomes, and `tests/conftest.py` points
`PCORE_HOME` at a scratch directory so no test reads the real home; `tests/serve_mock.py` is not a test but the
server the app's end-to-end check runs (see "Provenance.app's router").
