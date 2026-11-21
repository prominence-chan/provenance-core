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

## ChatGPT web as a model (optional, off by default)

If your ChatGPT plan gives you a strong model in the web chat, provenance-core can use that chat as one more model:
each request opens a **new temporary chat** in a visible Chrome window on its own profile, types the whole request
as one message, and reads the answer back (tool calls included). Nothing is kept in your ChatGPT history, and the
harness keeps owning context, tools and memory.

```bash
.venv/bin/pip install playwright          # uses your installed Google Chrome
provenance-core weblogin                  # opens Chrome on the dedicated profile: sign in, then quit it (Cmd+Q)
echo 'PCORE_CHATGPT_WEB=1' >> ~/.tierlm/keys.env    # turn it on, then restart the server
provenance-core webstatus --probe         # on? signed in? how many requests today?
```

The model is `chatgpt-web/luna` in `catalog.yaml` (the name is what gets picked in the page's model picker). It
sits in a tier by its score like any other model; Provenance / Codex requests for that tier try it first and fall
back to the normal pool whenever it is busy, signed out, rate limited or showing a challenge.

As of 2026-11-21 chatgpt.com answers the automated window with a Cloudflare check that does not clear by itself, so
in practice the bridge reports itself unavailable and requests use the normal pool; it does not try to get past it.

**Know the risk:** automating the ChatGPT web app is against OpenAI's terms of use. The realistic downside is your
ChatGPT account being limited or banned. The bridge is therefore slow on purpose (one request at a time, a pause
between requests, a daily cap of 200) and never tries to get around a captcha, challenge or "unusual activity"
notice: it stops and tells you. Details: [docs/TECHNICAL.md](docs/TECHNICAL.md#chatgpt-web-provider).

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
