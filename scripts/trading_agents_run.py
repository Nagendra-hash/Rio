#!/usr/bin/env python3
"""
trading_agents_run.py — run a TradingAgents (TauricResearch) multi-agent
analysis for one ticker and print the final decision.

The framework is installed in its OWN venv (.venv-tradingagents — its
langchain/langgraph stack must not touch JARVIS'), with the repo cloned at
/tmp/TradingAgents only for the install; the installed package is what runs.

LLM brain: the house free-tier ladder, encoded as TradingAgents providers.
The framework speaks OpenAI-compatible Chat Completions, so the primary
provider is 'openai' pointed at the FreeLLMAPI pool (127.0.0.1:3001/v1,
model 'auto', ~253 free endpoints) with the key fed from
config/api_keys.json. Because the pool's vision-capable routes are often
saturated, --brain ollama is the zero-quota local floor: the framework's
'ollama' provider (127.0.0.1:11434/v1, qwen2.5:1.5b) — slower and weaker,
but it cannot be rate-limited. Gemini-direct ('google' provider) is NOT
wired: its key would be double-spent against JARVIS' own quota.

Data vendors: yfinance (keyless) for everything. YFinance serves US tickers
well; Indian .NS tickers generally work for OHLCV/news but are best-effort.

Usage:
  python3 scripts/trading_agents_run.py TICKER [YYYY-MM-DD] [--analysts a,b]
                                        [--brain auto|ollama] [--full]

  TICKER      e.g. NVDA, AAPL, RELIANCE.NS
  date        trade date to analyse (default: today, IST)
  --analysts  comma list from: market,social,news,fundamentals (default market,news)
  --brain     auto = FreeLLMAPI pool (default); ollama = local zero-quota floor
  --full      print the full state dict instead of just the decision

Output: decision summary on stdout, progress on stderr. Exit 0 on success.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent.parent
_CONFIG = BASE / "config" / "api_keys.json"
_FREELLM_URL = "http://127.0.0.1:3001/v1"
_OLLAMA_URL = "http://127.0.0.1:11434/v1"
_OLLAMA_MODEL = "qwen2.5:1.5b"
_VALID_ANALYSTS = ("market", "social", "news", "fundamentals")


def _feed_key_env(require_llm_key: bool = True) -> None:
    """Map the house config onto TradingAgents' expected env vars."""
    try:
        cfg = json.loads(_CONFIG.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[trading-agents] cannot read {_CONFIG}: {e}", file=sys.stderr)
        raise SystemExit(1)
    key = (cfg.get("freellmapi_api_key") or "").strip()
    if key:
        # TradingAgents reads the OpenAI-family key from the canonical env var.
        os.environ.setdefault("OPENAI_API_KEY", key)
    elif require_llm_key:
        print("[trading-agents] freellmapi_api_key missing from config — "
              "FreeLLMAPI is the only cloud brain wired", file=sys.stderr)
        raise SystemExit(1)
    # Macro enrichment (optional): when a FRED key is configured, the macro
    # vendor activates — the news analyst gets rates/CPI/VIX context instead
    # of a DATA_UNAVAILABLE sentinel.
    fred = (cfg.get("fred_api_key") or "").strip()
    if fred:
        os.environ.setdefault("FRED_API_KEY", fred)
        print("[trading-agents] macro enrichment: FRED enabled", file=sys.stderr)
    else:
        print("[trading-agents] macro enrichment: no fred_api_key in config — "
              "macro context degrades to sentinels", file=sys.stderr)
    # SEC EDGAR (keyless): identifies the caller; the working format is
    # 'Name contact@domain' — a bare project name or placeholder domain 403s.
    # Set sec_edgar_user_agent in config to override the default.
    os.environ.setdefault("SEC_EDGAR_USER_AGENT",
                          (cfg.get("sec_edgar_user_agent") or "").strip()
                          or "Mark-LIII Research contact@example.com")
    print(f"[trading-agents] fundamentals: sec_edgar,yfinance (UA: "
          f"{os.environ['SEC_EDGAR_USER_AGENT']})", file=sys.stderr)


def build_graph(selected_analysts, brain: str = "auto", debug: bool = False):
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.default_config import DEFAULT_CONFIG

    config = DEFAULT_CONFIG.copy()
    if brain == "ollama":
        # Zero-quota local floor: the framework's native ollama provider
        # (base_url from OLLAMA_BASE_URL, key optional).
        config["llm_provider"] = "ollama"
        os.environ.setdefault("OLLAMA_BASE_URL", _OLLAMA_URL)
        config["deep_think_llm"] = _OLLAMA_MODEL
        config["quick_think_llm"] = _OLLAMA_MODEL
    else:
        # FreeLLMAPI pool via the OpenAI-compatible family.
        config["llm_provider"] = "openai"
        config["backend_url"] = _FREELLM_URL
        config["deep_think_llm"] = "auto"
        config["quick_think_llm"] = "auto"
    config["max_debate_rounds"] = 1
    config["max_risk_discuss_rounds"] = 1
    # The free pool is bursty (other tenants refill per-minute windows); the
    # framework's own knob for this (#1091): a bigger SDK retry budget rides
    # out 429 waves instead of aborting a 6-minute run at the last node.
    config["llm_max_retries"] = 4
    config["output_language"] = "English"
    # macro_data: 'fred' when a FRED key is configured (fed to the env by
    # _feed_key_env), else 'default' so the router degrades it to a
    # DATA_UNAVAILABLE sentinel instead of aborting. prediction_markets has
    # no keyless-worthy vendor in this network — left 'default' regardless.
    if (os.environ.get("FRED_API_KEY") or "").strip():
        config["data_vendors"]["macro_data"] = "fred"
    else:
        config["data_vendors"]["macro_data"] = "default"
    config["data_vendors"]["prediction_markets"] = "default"
    # Fundamentals chain: SEC EDGAR as-filed statements first (keyless,
    # point-in-time), yfinance as the fallback for non-US filers.
    config["data_vendors"]["fundamental_data"] = "sec_edgar,yfinance"
    return TradingAgentsGraph(debug=debug, config=config,
                              selected_analysts=tuple(selected_analysts))


def main() -> int:
    ap = argparse.ArgumentParser(description="TradingAgents analysis runner")
    ap.add_argument("ticker", help="ticker symbol, e.g. NVDA or RELIANCE.NS")
    ap.add_argument("date", nargs="?", default=None,
                    help="trade date YYYY-MM-DD (default: today IST)")
    ap.add_argument("--analysts", default="market,news",
                    help=f"comma list from {','.join(_VALID_ANALYSTS)}")
    ap.add_argument("--full", action="store_true",
                    help="print the full final state, not just the decision")
    ap.add_argument("--brain", choices=["auto", "ollama"], default="auto",
                    help="auto = FreeLLMAPI pool (default); ollama = local zero-quota floor")
    args = ap.parse_args()

    analysts = tuple(a.strip().lower() for a in args.analysts.split(",") if a.strip())
    bad = [a for a in analysts if a not in _VALID_ANALYSTS]
    if bad:
        print(f"unknown analyst(s): {', '.join(bad)} — valid: {', '.join(_VALID_ANALYSTS)}",
              file=sys.stderr)
        return 2

    trade_date = args.date or datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")

    # Key feeding must happen before build_graph for BOTH brains: the FRED
    # env var matters regardless of which LLM brain is chosen.
    _feed_key_env(require_llm_key=(args.brain != "ollama"))
    if args.brain == "ollama":
        print(f"[trading-agents] brain: ollama {_OLLAMA_MODEL} @ {_OLLAMA_URL} "
              f"(zero-quota local floor — expect a slow run)", file=sys.stderr)
    else:
        print(f"[trading-agents] brain: FreeLLMAPI 'auto' @ {_FREELLM_URL}", file=sys.stderr)
    print(f"[trading-agents] analysts: {', '.join(analysts)} | ticker: {args.ticker} "
          f"| date: {trade_date}", file=sys.stderr)

    try:
        ta = build_graph(analysts, brain=args.brain)
    except Exception as e:
        print(f"[trading-agents] graph build failed: {e}", file=sys.stderr)
        return 1

    try:
        final_state, signal = ta.propagate(args.ticker, trade_date)
    except Exception as e:
        print(f"[trading-agents] run failed: {e}", file=sys.stderr)
        return 1

    if args.full:
        print(json.dumps(final_state, indent=2, default=str))
        return 0

    decision = (final_state or {}).get("final_trade_decision", "")
    if not decision:
        print("[trading-agents] run finished without a decision", file=sys.stderr)
        return 1

    print("=" * 60)
    print(f"TradingAgents decision — {args.ticker} @ {trade_date}")
    print("=" * 60)
    print(f"RATING: {signal}")
    print()
    print(decision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
