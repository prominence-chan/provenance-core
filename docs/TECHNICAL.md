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

Provider keys use the provider's own variable names (`GEMINI_API_KEY`, `CLOUDFLARE_API_TOKEN`, ...; see
`catalog.yaml`).

## Compatibility with the old name

- `python -m tierlm ...` and `from tierlm import usage` still work through a small `tierlm/` shim package.
- `TIERLM_*` env vars, the `X-Tierlm-Session` header and the `tierlm` request field are still read.
- `env codex` exports both `PCORE_*` and `TIERLM_BASE_URL` / `TIERLM_TOKEN` (Provenance reads the latter).
- Tier entries in `/v1/models` keep `owned_by: "tierlm"`, which existing clients use to find the tiers.

## Tests

`.venv/bin/python -m pytest -q` (upstreams are mocked).
