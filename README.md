<div align="center">

<img src="assets/banner.png" alt="provenance-core" width="100%">
<h1>provenance-core</h1>

<b>free llm capacity, pooled behind one endpoint</b><br>
ask for a strength, not a model<br><br>
<a href="https://github.com/prominence-chan/provenance-core/actions/workflows/ci.yml"><img src="https://github.com/prominence-chan/provenance-core/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
<img src="https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white" alt="python 3.11+">
<img src="https://img.shields.io/badge/API-OpenAI%20·%20Anthropic%20·%20Responses-412991" alt="APIs">
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-3a3a3a" alt="MIT"></a>

<p>a dozen providers' free tiers (google, groq, openrouter, kilo, cloudflare, nvidia, …) served as one openai-, anthropic- and responses-compatible api. every model gets a score and lands in a tier. requests stay in their tier, fail over when a model hits a limit, and keep a conversation on the same model</p>

<table>
<tr>
<th><img src="https://img.shields.io/badge/spark-9cc8ff?style=for-the-badge" alt="spark"></th>
<th><img src="https://img.shields.io/badge/ember-c9663f?style=for-the-badge" alt="ember"></th>
<th><img src="https://img.shields.io/badge/solstice-ff6a3d?style=for-the-badge" alt="solstice"></th>
<th><img src="https://img.shields.io/badge/nova-ff4fa3?style=for-the-badge" alt="nova"></th>
</tr>
<tr>
<td align="center">fast&nbsp;and&nbsp;small</td>
<td align="center">strong&nbsp;mid-size</td>
<td align="center">frontier&nbsp;agentic</td>
<td align="center">the&nbsp;very&nbsp;strongest</td>
</tr>
<tr>
<td align="center"><sub>routing,&nbsp;bulk&nbsp;work</sub></td>
<td align="center"><sub>edits,&nbsp;summaries</sub></td>
<td align="center"><sub>coding&nbsp;(default)</sub></td>
<td align="center"><sub>the&nbsp;hardest&nbsp;problems</sub></td>
</tr>
</table>

</div>

## quick start

```bash
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/provenance-core serve          # http://127.0.0.1:4141/v1
```

```bash
curl localhost:4141/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model": "solstice", "messages": [{"role": "user", "content": "hello"}]}'
```

Kilo answers without a key. Export provider keys (`GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, … see the catalog) for more capacity; providers without one are skipped. `provenance-core tiers` shows what you have, `provenance-core calibrate` measures it.

## use it with

<details>
<summary><b>Claude Code</b></summary>

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:4141 ANTHROPIC_AUTH_TOKEN=pcore claude
```

Opus maps to `nova`, Sonnet to `solstice`, Haiku to `ember`.

</details>

<details>
<summary><b>Codex</b></summary>

```toml
# ~/.codex/config.toml
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

[Provenance](https://github.com/prominence-chan/provenance) (the Codex fork) uses provenance-core out of the box, and Provenance.app carries its own copy for when nothing answers on :4141. Anything else: base URL `http://127.0.0.1:4141/v1`, model = a tier name.

</details>

## docs

Routing, sticky sessions, ensembles, the API adapters, calibration, subscription fallback and every setting: **[docs/TECHNICAL.md](docs/TECHNICAL.md)**.

<div align="center"><sub>use each provider within its own terms · MIT</sub></div>
