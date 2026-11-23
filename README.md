<div align="center">

# provenance-core

**Free LLM capacity, pooled behind one endpoint.**

Ask for a strength, not a model.

[![CI](https://github.com/prominence-chan/provenance-core/actions/workflows/ci.yml/badge.svg)](https://github.com/prominence-chan/provenance-core/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![API](https://img.shields.io/badge/API-OpenAI%20%C2%B7%20Anthropic%20%C2%B7%20Responses-412991)
![Ruff](https://img.shields.io/badge/code%20style-ruff-D7FF64?logo=ruff&logoColor=black)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

</div>

provenance-core takes the free tiers of a dozen providers (Google, Groq, OpenRouter, Kilo, Cloudflare, Requesty,
NVIDIA, ...) and serves them as one OpenAI-, Anthropic- and Responses-compatible API. Every model gets a
score and lands in one of four tiers:

```
spark  ─▶  ember  ─▶  solstice  ─▶  nova
fast       strong      frontier      the very
& small    mid-size    agentic       strongest
```

| tier       | good for                                  |
| ---------- | ----------------------------------------- |
| `nova`     | the hardest problems                      |
| `solstice` | agentic coding and reasoning (default)    |
| `ember`    | edits, summaries, tool plumbing           |
| `spark`    | classification, routing, bulk work        |

A request stays inside its tier, fails over to another model when one hits a limit, and keeps a conversation on the
same model.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/provenance-core serve          # http://127.0.0.1:4141/v1
```

```bash
curl localhost:4141/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model": "solstice", "messages": [{"role": "user", "content": "hello"}]}'
```

Kilo answers without a key. Export provider keys (`GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, ... see
`catalog.yaml`) for more capacity; providers without a key are skipped. `provenance-core tiers` shows what you have,
`provenance-core calibrate` measures it.

## Use it with

<details>
<summary><b>Claude Code</b></summary>

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:4141 ANTHROPIC_AUTH_TOKEN=pcore claude
```

Opus maps to `nova`, Sonnet to `solstice`, Haiku to `ember`.

</details>

<details>
<summary><b>Codex</b> (<code>~/.codex/config.toml</code>)</summary>

```toml
model = "solstice"
model_provider = "pcore"

[model_providers.pcore]
name = "provenance-core"
base_url = "http://127.0.0.1:4141/v1"
wire_api = "responses"
```

</details>

<details>
<summary><b>Provenance</b> and any other OpenAI client</summary>

[Provenance](https://github.com/prominence-chan/provenance) (the Codex fork) uses provenance-core out of the box.
Provenance.app carries its own copy and runs it (with `catalog.app.yaml`) when no router answers on :4141; sign-ins,
per-site settings and API keys are then set up from the app (docs/TECHNICAL.md, "Provenance.app's router").
Anything else: base URL `http://127.0.0.1:4141/v1`, model = a tier name.

</details>

## Docs

Routing, sticky sessions, ensembles, the API adapters, calibration, subscription fallback and every setting live in
**[docs/TECHNICAL.md](docs/TECHNICAL.md)**.

Use each provider within its own terms. MIT licensed.
