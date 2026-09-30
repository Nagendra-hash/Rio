---
name: trading-agents
description: >-
  Operate the TradingAgents (TauricResearch) integration: run multi-agent
  equity analyses from JARVIS voice, Hermes, or the CLI, route the framework's
  LLM brain through the house free-tier ladder, and debug failed runs. Use
  when an analysis should be delegated, when a run fails, or when changing
  brain routing or analyst sets. Triggers: "TradingAgents", "analyst team",
  "multi-agent stock analysis", "trading agents run failed".
---

# TradingAgents integration — JARVIS + Hermes

The TauricResearch multi-agent trading-research framework, wired to the
house free-tier ladder (added 2026-09-26).

```
.venv-tradingagents/                ← isolated install (its langchain stack must not touch JARVIS')
scripts/trading_agents_run.py       ← the only entry point; brain routing + vendors + output
├── actions/trading_agents.py       ← JARVIS action (TOOL dict, voice path)
└── ~/.hermes/skills/trading-agents/ ← Hermes skill (CLI path)
```

## Quick start

```bash
# Direct:
.venv-tradingagents/bin/python scripts/trading_agents_run.py NVDA
# Local zero-quota floor (slow, never rate-limited):
.venv-tradingagents/bin/python scripts/trading_agents_run.py NVDA --brain ollama
# From Hermes:
hermes -z "Use the trading-agents skill: run the analyst team on AAPL"
```

## Brain routing

| Brain | Provider | Notes |
|---|---|---|
| `auto` (default) | `openai` + `backend_url=127.0.0.1:3001/v1`, model `auto` | FreeLLMAPI pool; key fed from `config/api_keys.json` → `OPENAI_API_KEY` |
| `ollama` | framework's native `ollama` provider, `qwen2.5:1.5b` | zero-quota floor; too slow for the full loop on this box (CPU-only) — usable for single-node tests |
| ~~gemini direct~~ | deliberately NOT wired | would double-spend JARVIS' own per-day quota |

## Verified status (2026-09-26)

- Graph compiles for both brains; single-node brain check returns correctly
  through the pool.
- Analysts → researchers → trader → risk chain RUNS end-to-end; a full
  unattended decision needs the pool's vision-capable routes to be free —
  expect 429s on busy days (the runner surfaces them; retry in minutes).
- **Full decision delivered**: NVDA → `RATING: Overweight` with a data-grounded
  rationale (SMA/RSI/ATR/Bollinger levels, risk team overruling the trader's
  stop-loss).
- Vendors: yfinance (keyless) for prices/technicals/news; **fundamentals =
  `sec_edgar,yfinance`** — SEC as-filed statements first (verified live:
  NVDA quarterly income statement back to 2008, cached under
  `~/.tradingagents/cache/sec_edgar/`), yfinance fallback for non-US
  filers; `macro_data` = **`fred`** when `fred_api_key` is present in
  `config/api_keys.json` (verified live: VIX + 10Y treasury series), else
  `"default"` → graceful `DATA_UNAVAILABLE` sentinels; `prediction_markets`
  stays `"default"`.
- SEC EDGAR UA gotcha: SEC 403s automated callers whose User-Agent lacks a
  real contact address. The runner sends `Mark-LIII Research
  contact@example.com` by default — set `sec_edgar_user_agent` in
  `config/api_keys.json` to your own name and email so SEC can reach you.
  Formats that got 403 in testing: `noreply@` addresses, `localhost`, and
  bare project names.

## Debug drill

1. Read the last stderr line: `run failed: …` is authoritative.
2. `429` / `rate_limit` → pool saturation; retry in minutes. Nothing to fix.
3. Graph build error → `pip show tradingagents` in `.venv-tradingagents`;
   re-install from a fresh clone if the venv is broken.
4. `Configured vendor(s) … not available` → a data_vendors entry names a
   vendor the method doesn't support. `macro_data` may only be `fred` (needs
   `fred_api_key` in config — the runner feeds it to `FRED_API_KEY`) or
   `default` (graceful sentinels); `prediction_markets` stays `default`.
5. Empty decision → check `~/.tradingagents/logs/<TICKER>/` state JSONs to
   see which node last wrote.

## House rules

- Research only — never wire this into order execution; the disclaimer must
  accompany every spoken rating.
- Prefer `market,news` analysts; more analysts multiply pool calls.
- Never hardcode keys; the runner feeds `OPENAI_API_KEY` from house config.
