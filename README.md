# provenance-core

**Free LLM capacity, pooled behind one endpoint.** provenance-core collects the free tiers of a dozen providers
(Google, Groq, Cerebras, OpenRouter, Kilo, OVHcloud, Cloudflare, Mistral, NVIDIA, ...) and serves them as one
OpenAI- and Anthropic-compatible API. Every model is scored and sorted into four tiers, so you ask for a strength,
not a model:

| tier | what you get |
|---|---|
| `nova` | the very strongest models |
| `solstice` | frontier: solid agentic coding and reasoning |
| `spark` | strong mid-size: edits, summaries, tool plumbing |
| `ember` | fast and small: classification, routing, bulk work |

A request stays inside its tier, fails over between models when one hits a limit, and keeps a conversation on one
model.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m provenance_core serve        # http://127.0.0.1:4141/v1
```

Kilo and OVHcloud work with no key at all, so that already answers. Add keys for more capacity (below), then:

```bash
curl localhost:4141/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model": "solstice", "messages": [{"role": "user", "content": "hello"}]}'
```

`python -m provenance_core tiers` shows what you have; `python -m provenance_core calibrate` measures it.

## Use it with

**Claude Code**

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:4141 ANTHROPIC_AUTH_TOKEN=pcore claude
```

Opus maps to `nova`, Sonnet to `solstice`, Haiku to `spark`.

**Codex** (`~/.codex/config.toml`)

```toml
model = "solstice"
model_provider = "pcore"

[model_providers.pcore]
name = "provenance-core"
base_url = "http://127.0.0.1:4141/v1"
wire_api = "responses"
```

**Provenance** (the Codex fork) uses provenance-core out of the box at `http://127.0.0.1:4141`.

Any other OpenAI client: base URL `http://127.0.0.1:4141/v1`, model = a tier name.

## Keys

Put provider keys in your environment (or an env file your service sources), any subset:

```bash
GEMINI_API_KEY=...  GROQ_API_KEY=...  OPENROUTER_API_KEY=...  CEREBRAS_API_KEY=...
CLOUDFLARE_ACCOUNT_ID=...  CLOUDFLARE_API_TOKEN=...  ZAI_API_KEY=...  SAMBANOVA_API_KEY=...
```

Providers without a key are simply skipped. The full list is in `catalog.yaml`. Use each provider within its own
terms.

## More

Routing, sticky sessions, ensembles, the API adapters, calibration, subscription fallback and every setting:
[docs/TECHNICAL.md](docs/TECHNICAL.md).

MIT licensed.
