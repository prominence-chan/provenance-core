# tierlm

An OpenAI-compatible router over free LLM tiers, **grouped by strength** so answers don't swing between a frontier
model and a tiny one depending on which free quota happens to be left.

Inspired by [freellmapi](https://github.com/tashfeenahmed/freellmapi), which aggregates dozens of free providers
behind one endpoint. tierlm focuses on stability instead of maximum aggregate tokens:

- **Tiers by measured strength.** Every model gets a 0-100 score from `tierlm calibrate`, an auto-graded probe set
  (arithmetic, combinatorics, code tracing, constrained reasoning; difficulty-weighted). Tiers are score bands
  (`tier-1` >= 80, `tier-2` >= 60, `tier-3` below; configurable). A request asks for a tier, not a model.
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
.venv/bin/python -m tierlm serve          # http://127.0.0.1:8787/v1
```

```bash
curl localhost:8787/v1/chat/completions -H 'content-type: application/json' -d '{
  "model": "tier-1",
  "messages": [{"role": "user", "content": "How many trailing zeros does 100! have?"}],
  "tierlm": {"ensemble": {"k": 3, "strategy": "majority"}}
}'
```

Any OpenAI client works: base URL `http://127.0.0.1:8787/v1`, model `tier-1` / `tier-2` / `tier-3` / `auto` or a
concrete catalog id. Set `TIERLM_TOKEN` to require a bearer token on the proxy.

## Catalog

`catalog.yaml` lists providers (OpenAI-compatible base URL + key env var) and models (provider model name, prior
score, context, free-tier limits). Free-tier lineups change weekly: `python -m tierlm discover <provider>` lists a
provider's current models, then re-run `calibrate`.

## Endpoints

- `POST /v1/chat/completions` (streaming supported for single-model requests)
- `GET /v1/models` - tier aliases plus usable concrete models
- `GET /status` - tiers, scores, calibration, headroom, health, latency

## Tests

`.venv/bin/python -m pytest -q tests` (upstreams are mocked).

## License

MIT
