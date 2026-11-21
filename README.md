# tierlm

An OpenAI-compatible router over free LLM tiers, **grouped by strength** so answers don't swing between a frontier
model and a tiny one depending on which free quota happens to be left.

Inspired by [freellmapi](https://github.com/tashfeenahmed/freellmapi), which aggregates dozens of free providers
behind one endpoint. tierlm focuses on stability instead of maximum aggregate tokens:

- **Tiers by measured strength.** Every model gets a 0-100 score from `tierlm calibrate`, an auto-graded probe set
  (arithmetic, combinatorics, code tracing, constrained reasoning; difficulty-weighted). Tiers are score bands
  named from an ember up to a nova: `nova` (hyperfrontier, >= 94), `solstice` (frontier, >= 82),
  `spark` (strong mid, >= 62) and `ember` (fast/small); configurable, `tier-1`..`tier-4` work as aliases. A request asks
  for a tier, not a model, and gets a single model's answer unless it explicitly asks for an ensemble.
- **No silent downgrades.** By default a request is only served inside its tier; it fails over between same-tier
  models (rate limits, errors) and returns 503 rather than quietly answering with a weaker model.
  `fallback: down1 | any` opts into stepping down.
- **Sticky sessions.** `X-Tierlm-Session` keeps a conversation on the model it started with while that model is
  healthy.
- **Ensembles inside a tier.** `ensemble: {k, strategy}`: `majority` (vote on the final line, good for exact
  answers), `judge` (the tier's strongest model merges/picks the best of k answers), `first` (latency hedging).
- **Quota-aware.** Per-model request/token windows (rpm/rpd/tpm/tpd) keep every free tier under its caps; 429s cool a
  model down for its Retry-After, repeated errors back off exponentially.

One API key per provider, from your own account, within each provider's terms.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
export GROQ_API_KEY=... CEREBRAS_API_KEY=... GEMINI_API_KEY=... OPENROUTER_API_KEY=...   # any subset
.venv/bin/python -m tierlm tiers          # what you have and where it sits
.venv/bin/python -m tierlm calibrate      # measure every usable model (writes scores.yaml)
.venv/bin/python -m tierlm serve          # http://127.0.0.1:4141/v1
```

```bash
curl localhost:4141/v1/chat/completions -H 'content-type: application/json' -d '{
  "model": "solstice",
  "messages": [{"role": "user", "content": "How many trailing zeros does 100! have?"}],
  "tierlm": {"ensemble": {"k": 3, "strategy": "majority"}}
}'
```

Any OpenAI client works: base URL `http://127.0.0.1:4141/v1`, model `nova` / `solstice` / `spark` / `ember` / `auto` or a
concrete catalog id. Set `TIERLM_TOKEN` to require a bearer token on the proxy.

## Use with Claude Code / Codex

tierlm also speaks the Anthropic Messages API (`/v1/messages`) and the OpenAI Responses API (`/v1/responses`), so
Claude Code and Codex can fall back to it when their subscriptions run low. Both adapters translate to the same
single-model chat path as `/v1/chat/completions` (no ensembles) and keep one agent conversation on one model
(`X-Tierlm-Session` if sent, else a hash of the conversation's first user message, per tier).

**Claude Code**

```bash
python -m tierlm serve --port 4141
ANTHROPIC_BASE_URL=http://127.0.0.1:4141 ANTHROPIC_AUTH_TOKEN="${TIERLM_TOKEN:-tierlm}" claude
```

Claude model ids map to tiers by family (dates and suffixes like `[1m]` are ignored); a tier name, alias or catalog
id (`ANTHROPIC_MODEL=solstice`, `/model spark`) passes straight through.

| env var | default | |
|---|---|---|
| `TIERLM_MAP_OPUS` | `nova` | opus-family requests |
| `TIERLM_MAP_SONNET` | `solstice` | sonnet-family requests |
| `TIERLM_MAP_HAIKU` | `spark` | haiku-family requests (Claude Code's side calls) |
| `TIERLM_MAP_DEFAULT` | sonnet mapping | any other model name (also unknown models on `/v1/responses`, where the default is the strongest tier) |
| `TIERLM_ANTHROPIC_FALLBACK` | `any` | step down through weaker tiers rather than fail |
| `TIERLM_RESPONSES_FALLBACK` | `down1` | fallback for `/v1/responses` |
| `TIERLM_ADAPTER_MAX_TOKENS` | `16384` | cap on the agents' max_tokens (they ask for 32k-64k, which free tiers reject or count against tokens/minute) |

A mapping value may end in `:none`, `:down1` or `:any` to set that mapping's fallback (`TIERLM_MAP_OPUS=nova:none`).
If a default tier name isn't in your catalog, the tier at the same position (1st/2nd/3rd) is used.
`/v1/messages/count_tokens` returns an estimate. With `TIERLM_TOKEN` set, the proxy accepts it as a bearer token
(`ANTHROPIC_AUTH_TOKEN`) or as `x-api-key` (`ANTHROPIC_API_KEY`). When no model in reach has capacity, the answer is
`529 overloaded_error`, which Claude Code retries with backoff.

**Codex** (`~/.codex/config.toml`, or a profile)

```toml
model = "solstice"                 # a tier name, alias or catalog id
model_provider = "tierlm"

[model_providers.tierlm]
name = "tierlm"
base_url = "http://127.0.0.1:4141/v1"
wire_api = "responses"
env_key = "TIERLM_TOKEN"        # any non-empty value works if the proxy has no TIERLM_TOKEN
```

As a fallback profile instead: put the provider table as above, add `[profiles.free]` with `model = "solstice"` and
`model_provider = "tierlm"`, and run `codex --profile free`. Codex warns that it has no metadata for the model name;
that is harmless.

What the adapters translate: text, images (base64 → data URL), tool definitions and calls (ids kept), tool results
(Anthropic `is_error` is kept as a `[tool error]` prefix), tool_choice, temperature/top_p, stop sequences, streaming
(Anthropic and Responses SSE events, tool-call arguments accumulated from the upstream chat stream), usage.
`/v1/responses` also maps Codex's `local_shell` tool (to a function, answered with `local_shell_call` items) and
freeform `custom` tools such as `apply_patch` (to a function with one `input` string, answered with
`custom_tool_call` items), and keeps a small in-memory store so `previous_response_id` and `GET /v1/responses/{id}`
work. Not translated: extended thinking / reasoning (dropped both ways), prompt caching, server-side tools
(web search, file search, MCP, computer use), PDF documents.

## Catalog

`catalog.yaml` lists providers (OpenAI-compatible base URL + key env var) and models (provider model name, prior
score, context, free-tier limits). Free-tier lineups change weekly: `python -m tierlm discover <provider>` lists a
provider's current models, then re-run `calibrate`.

## Endpoints

- `POST /v1/chat/completions` (streaming supported for single-model requests)
- `POST /v1/messages`, `POST /v1/messages/count_tokens` - Anthropic Messages API (Claude Code)
- `POST /v1/responses`, `GET /v1/responses/{id}` - OpenAI Responses API (Codex)
- `GET /v1/models` - tier aliases plus usable concrete models
- `GET /status` - tiers, scores, calibration, headroom, health, latency

## Tests

`.venv/bin/python -m pytest -q tests` (upstreams are mocked).

## License

MIT

## Subscription fallback

`tierlm usage` reads Claude Code's and Codex's own rate-limit data (Codex session rollouts; Claude Code's status-line
input, recorded by `tierlm statusline-tee '<your status-line command>'`) and `tierlm env claude|codex` prints the
exports that point a tool at tierlm once it's at `TIERLM_LOW_PCT` (95) percent, or nothing if it isn't low or tierlm
has no usable model. Small wrappers can use it to switch automatically:

```sh
eval "$(tierlm env claude)"; exec claude "$@"
```
