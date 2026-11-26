# freellmapi — local OpenAI-compatible pool

RIO's `freellm` provider, the Browser Agent's brain chain and the Science Agent
all expect one OpenAI-compatible endpoint at `http://127.0.0.1:3001/v1` that
fans out across free providers.

The upstream project this is driven by — [mnfst/awesome-free-llm-apis](https://github.com/mnfst/awesome-free-llm-apis)
— is a **catalog** of such providers (`data.json`), not a server. This directory
is the missing server.

## What it does

```
GET  /health                 status, per-provider cooldowns (no auth)
GET  /v1/models              "auto" + every pooled model (auth)
POST /v1/chat/completions    proxy with fallback (auth)
```

- `model: "auto"` walks the pool in priority order and returns the first real
  answer.
- Any other model id is routed to the provider(s) that advertise it.
- Key-free endpoints lead the pool (OVHcloud anonymous, LLM7, Kilo Code), so it
  works with **no provider credentials at all**. Key-backed providers (NVIDIA,
  Gemini, OpenRouter) are used when their keys are present.
- Failures park a provider: 429 for its `Retry-After` (or 5 min), 401/403 for
  10 min, 5xx/timeout for 60 s. One dead provider never fails the request while
  another can answer.
- `max_tokens` is raised to at least 64: these are largely reasoning models, and
  a smaller budget is consumed by hidden reasoning, producing an empty answer.

## Auth

Every API request needs `Authorization: Bearer <FREELLMAPI_API_KEY>`.
The key is read from the environment, falling back to `~/.hermes/.env`.
With no key configured the service answers **503** rather than acting as an open
proxy on localhost.

## Files

| File | Purpose |
|---|---|
| `server.py` | the service (stdlib only: `http.server` + `urllib`) |
| `providers.json` | the curated pool + catalog provenance |
| `vendor/data.json`, `vendor/sources.json` | pinned copy of the upstream catalog |

## Run

```bash
# foreground
/usr/bin/python3 services/freellmapi/server.py

# as a user service
systemctl --user enable --now freellmapi
systemctl --user status freellmapi
journalctl --user -u freellmapi -f
```

The unit lives at `~/.config/systemd/user/freellmapi.service` and reads
`~/.hermes/.env` for `FREELLMAPI_API_KEY` plus any provider keys.

## Verify

```bash
KEY=$(grep '^FREELLMAPI_API_KEY=' ~/.hermes/.env | cut -d= -f2-)
curl -s localhost:3001/health | python3 -m json.tool
curl -s -H "Authorization: Bearer $KEY" localhost:3001/v1/models
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Reply: ok"}],"max_tokens":64}' \
  localhost:3001/v1/chat/completions | python3 -m json.tool
```

## Limits and honesty

- Provider quotas belong to the providers (OVHcloud anonymous is 2 RPM per IP
  per model; Kilo is 200 req/hr per IP). The router does not raise them.
- A provider that answers HTTP 200 with empty `content` is treated as a
  failure, not an answer — that is the most common quiet failure of free tiers.
- `openrouter/free` is used, never `openrouter/auto`, because auto can select a
  paid model.
- This is a local development helper on 127.0.0.1. It has no rate limiting of
  its own beyond provider cooldowns; do not expose the port.
