"""
trading_agents.py — bridge action: JARVIS runs a TradingAgents analysis.

TradingAgents (github.com/TauricResearch/TradingAgents) is a multi-agent LLM
trading framework: analyst agents (market technicals, news, sentiment,
fundamentals) research an instrument, bull/bear researchers debate, a trader
drafts a plan, and a risk team argues before a portfolio manager issues a
5-tier rating (Buy / Overweight / Hold / Underweight / Sell).

This action shells out to scripts/trading_agents_run.py, which runs the
framework in its own venv (.venv-tradingagents) with the LLM brain routed to
the FreeLLMAPI pool — the house free-tier router, no direct provider quota.

RESEARCH ONLY. This is analysis, not financial advice, and nothing here
executes a trade — live orders remain behind the trading_strategy/binance
plugins' own confirmations.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PYTHON = str(_ROOT / ".venv-tradingagents" / "bin" / "python")
_SCRIPT = _ROOT / "scripts" / "trading_agents_run.py"
_TIMEOUT = 540  # seconds: a trimmed run is 5-9 min through the free pool


def trading_agents(parameters: dict, player=None, speak=None) -> str:
    """Run one multi-agent analysis and speak the decision."""
    parameters = parameters or {}
    ticker = str(parameters.get("ticker", "")).strip().upper()
    date = str(parameters.get("date", "")).strip()
    analysts = str(parameters.get("analysts", "market,news")).strip()

    if not ticker:
        return "Which ticker should the analyst team evaluate, sir?"
    if not _SCRIPT.exists():
        return "The TradingAgents runner is missing, sir — check scripts/trading_agents_run.py."
    if not Path(_PYTHON).exists():
        return (
            "The TradingAgents venv is missing, sir — rebuild .venv-tradingagents "
            "with the framework's requirements installed."
        )

    if player:
        player.write_log(f"[trading-agents] {ticker} {date} analysts={analysts}")
    if speak:
        speak(
            f"Convening the analyst team for {ticker}, sir. This will take several minutes."
        )

    cmd = [_PYTHON, str(_SCRIPT), ticker]
    if date:
        cmd.append(date)
    cmd += ["--analysts", analysts]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            env={"HOME": str(Path.home()), "PATH": "/usr/local/bin:/usr/bin:/bin"},
        )
    except subprocess.TimeoutExpired:
        return (
            f"The analyst team ran out of time on {ticker}, sir. "
            "The free-tier brain was likely throttled — try again shortly, "
            "or with fewer analysts."
        )

    out = (proc.stdout or "").strip()
    if proc.returncode == 0 and out:
        # Keep the spoken answer compact: rating line + first chunk of the rationale.
        return out[:2500]

    err = (proc.stderr or "").strip().splitlines()
    reason = err[-1] if err else f"exit {proc.returncode}"
    if "429" in reason or "rate" in reason.lower():
        return (
            "The analyst team was rate-limited by the free LLM pool, sir. "
            "The quota resets within minutes — ask me to retry shortly."
        )
    return f"The TradingAgents run for {ticker} failed: {reason[:250]}"


TOOL = {
    "name": "trading_agents",
    "description": (
        "Run a TradingAgents multi-agent equity analysis for one ticker: "
        "technical/market and news analysts research the instrument, bull/bear "
        "researchers debate, and a risk-adjusted portfolio decision is issued "
        "as a 5-tier rating (Buy/Overweight/Hold/Underweight/Sell). Takes "
        "several minutes. Use when the user asks for a deep AI analysis of a "
        "stock ('what does the analyst team think of NVDA?'). Do NOT use for "
        "quick quotes (use market tools), paper trading actions, or live "
        "orders — this is research only, not financial advice."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "ticker": {
                "type": "STRING",
                "description": "Ticker symbol, e.g. NVDA, AAPL, RELIANCE.NS.",
            },
            "date": {
                "type": "STRING",
                "description": "Optional trade date YYYY-MM-DD (default: today IST).",
            },
            "analysts": {
                "type": "STRING",
                "description": "Comma list from market,social,news,fundamentals (default: market,news — fewer analysts means a faster, more reliable run).",
            },
        },
        "required": ["ticker"],
    },
    "handler": trading_agents,
}
