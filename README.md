<div align="center">

<img src="assets/banner.png" alt="provenance-core" width="100%">
<h1>provenance-core</h1>

<b>free llm capacity, pooled behind one endpoint</b><br>
ask for a strength, not a model<br><br>
<a href="https://github.com/prominence-chan/provenance-core/actions/workflows/ci.yml"><img src="https://img.shields.io/badge/CI-pytest%20%C2%B7%20ruff-2ea44f?style=for-the-badge&labelColor=1f1f1f&logo=githubactions&logoColor=white" alt="CI"></a>
<img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=for-the-badge&labelColor=1f1f1f&logo=python&logoColor=white" alt="python 3.11+">
<img src="https://img.shields.io/badge/APIs-OpenAI%20%C2%B7%20Anthropic%20%C2%B7%20Responses-412991?style=for-the-badge&labelColor=1f1f1f" alt="APIs">
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-3a3a3a?style=for-the-badge&labelColor=1f1f1f" alt="MIT"></a>

<p>a dozen providers' free tiers (google, groq, openrouter, kilo, cloudflare, nvidia, …) served as one openai-, anthropic- and responses-compatible api. every model gets a score and lands in a tier. requests stay in their tier, fail over when a model hits a limit, and keep a conversation on the same model</p>

<table>
<tr>
<th width="200" align="center"><img src="https://img.shields.io/badge/spark-9cc8ff?style=for-the-badge&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNiIgaGVpZ2h0PSIxNiIgdmlld0JveD0iMCAwIDE2IDE2Ij48cGF0aCBkPSJNOCAybDEuMyA0LjcgNC43IDEuMy00LjcgMS4zLTEuMyA0LjctMS4zLTQuNy00LjctMS4zIDQuNy0xLjN6IiBmaWxsPSIjMzMzMzMzIi8%2BPC9zdmc%2B" alt="spark"></th>
<th width="200" align="center"><img src="https://img.shields.io/badge/ember-c9663f?style=for-the-badge&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNiIgaGVpZ2h0PSIxNiIgdmlld0JveD0iMCAwIDE2IDE2Ij48Y2lyY2xlIGN4PSI4IiBjeT0iOCIgcj0iNCIgZmlsbD0iI2ZmZmZmZiIvPjxjaXJjbGUgY3g9IjgiIGN5PSI4IiByPSI2LjIiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2ZmZmZmZiIgc3Ryb2tlLXdpZHRoPSIxLjMiIG9wYWNpdHk9IjAuNDUiLz48L3N2Zz4%3D" alt="ember"></th>
<th width="200" align="center"><img src="https://img.shields.io/badge/solstice-ff6a3d?style=for-the-badge&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNiIgaGVpZ2h0PSIxNiIgdmlld0JveD0iMCAwIDE2IDE2Ij48Y2lyY2xlIGN4PSI4IiBjeT0iOCIgcj0iMy40IiBmaWxsPSIjZmZmZmZmIi8%2BPGcgc3Ryb2tlPSIjZmZmZmZmIiBzdHJva2Utd2lkdGg9IjEuNCIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIj48cGF0aCBkPSJNOCAxLjJ2MS42TTggMTMuMnYxLjZNMS4yIDhoMS42TTEzLjIgOGgxLjZNMy4yIDMuMmwxLjEgMS4xTTExLjcgMTEuN2wxLjEgMS4xTTMuMiAxMi44bDEuMS0xLjFNMTEuNyA0LjNsMS4xLTEuMSIvPjwvZz48L3N2Zz4%3D" alt="solstice"></th>
<th width="200" align="center"><img src="https://img.shields.io/badge/nova-ff4fa3?style=for-the-badge&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNiIgaGVpZ2h0PSIxNiIgdmlld0JveD0iMCAwIDE2IDE2Ij48cGF0aCBkPSJNOCAuNWwxLjMgNC42IDQuMS0yLjUtMi41IDQuMUwxNS41IDhsLTQuNiAxLjMgMi41IDQuMS00LjEtMi41TDggMTUuNWwtMS4zLTQuNi00LjEgMi41IDIuNS00LjFMLjUgOGw0LjYtMS4zLTIuNS00LjEgNC4xIDIuNXoiIGZpbGw9IiNmZmZmZmYiLz48Y2lyY2xlIGN4PSI4IiBjeT0iOCIgcj0iMS45IiBmaWxsPSIjZmY0ZmEzIi8%2BPC9zdmc%2B" alt="nova"></th>
</tr>
<tr>
<td width="200" align="center">fast and small<br><sub>routing, bulk work</sub></td>
<td width="200" align="center">strong mid-size<br><sub>edits, summaries</sub></td>
<td width="200" align="center">frontier agentic<br><sub>coding (default)</sub></td>
<td width="200" align="center">the very strongest<br><sub>the hardest problems</sub></td>
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

<details open>
<summary><b>Provenance</b> (recommended)</summary>

[Provenance](https://github.com/prominence-chan/provenance) uses provenance-core out of the box, and Provenance.app carries its own copy for when nothing answers on :4141.

</details>

<details>
<summary><b>Claude Code</b></summary>

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:4141 ANTHROPIC_AUTH_TOKEN=pcore claude
```

Fable maps to `nova`, Opus to `solstice`, Sonnet to `ember`, Haiku to `spark`.

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
<summary><b>Any other OpenAI client</b></summary>

Base URL `http://127.0.0.1:4141/v1`, model = a tier name.

</details>

## docs

Routing, sticky sessions, ensembles, the API adapters, calibration, subscription fallback and every setting: **[docs/TECHNICAL.md](docs/TECHNICAL.md)**.

<div align="center"><sub>use each provider within its own terms · MIT</sub></div>
