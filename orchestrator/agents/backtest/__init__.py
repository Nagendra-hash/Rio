"""
orchestrator/agents/backtest — the autonomous crypto + gold backtest lab.

WHAT THIS PACKAGE IS
    A bounded, read-only research agent. A user supplies ONE symbol (BTC, ETH,
    SOL, GOLD); the package resolves the instrument, fetches real historical
    candles from a public market-data provider, verifies coverage, discovers
    usable timeframes, tunes reviewed strategies on a training slice, validates
    them chronologically, and ranks what survives on untouched out-of-sample
    data. Results are saved as Markdown + CSV + JSON and reported through
    Jarvis and Hermes.

WHAT IT IS NOT
    * No broker, no account, no order endpoint, no API key with trade rights.
    * No LLM-generated strategy code. Strategy logic lives in
      strategies/library.py and is reviewed and tested.
    * No synthetic candles outside the test suite. Every real result names the
      provider, instrument and market type it came from.
    * A backtest is historical evidence, never a promise of future profit.

ENTRY POINTS
    BacktestAgent                 — orchestrator agent (registry: backtest_agent)
    actions/backtest_agent.py     — Jarvis voice / chat tool
    scripts/backtest_run.py       — CLI (run, status, report, cancel, schedule)
    dashboard/backtest_api.py     — RIO / dashboard REST surface
"""
from __future__ import annotations

__version__ = "1.0.0"

AGENT_NAME = "backtest_agent"


def agent():
    """Lazy constructor so importing the package stays dependency-free."""
    from orchestrator.agents.backtest.backtest_agent import BacktestAgent
    return BacktestAgent()


__all__ = ["AGENT_NAME", "__version__", "agent"]
