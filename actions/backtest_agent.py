"""
actions/backtest_agent.py — Jarvis tool for the bounded backtest research lab.

Thin bridge only: it hands the request to orchestrator/agents/backtest and
returns text. The heavy work always runs in a detached worker
(scripts/backtest_run.py --worker), never inside this call.

RESEARCH ONLY. No broker client is imported here, and the backtest package has
no order endpoint. Nothing in this file can place a trade.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _base_dir() -> Path:
    import os
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def backtest_agent(parameters: dict, player=None, speak=None) -> str:
    """Run a bounded backtest-research action and return a short answer."""
    parameters = parameters or {}
    action = str(parameters.get("action") or "start").strip().lower()
    symbol = str(parameters.get("symbol") or "").strip().upper()
    market = str(parameters.get("market") or "").strip().lower()
    venue = str(parameters.get("venue") or "").strip().lower()
    job_id = str(parameters.get("job_id") or "").strip().lower()

    if player:
        player.write_log(f"[backtest] {action} {symbol} {market} {venue} {job_id}")

    # ── Delta Exchange India: resolve the exact live product first ──────
    # The canonical backtest agent speaks BTC/ETH/SOL/GOLD. Delta products
    # are venue-specific (BTCUSD, BTC_INR, tokenized-equity perps), so a
    # Delta start is resolved here and handed to the same bounded worker.
    if action == "start" and (venue in ("delta", "delta_india", "delta_perp",
                                        "delta_spot")
                              or market in ("delta_perp", "delta_spot")):
        try:
            from orchestrator.agents.backtest import symbols as _symbols
            from orchestrator.agents.backtest.config import default_config
            from orchestrator.agents.backtest.jobs import start_job
            if market in ("", "auto") and venue in ("delta_perp", "delta_spot"):
                market = venue
            resolution = _symbols.resolve(symbol, market=market,
                                          cfg=default_config(), venue="delta")
            if not resolution.ok:
                options = ", ".join(resolution.options[:24])
                return (resolution.question or resolution.error or
                        "Which exact Delta product should I research?") + (
                        f" Choices: {options}." if options else "") + (
                        " Nothing was run and no order exists.")
            inst = resolution.instrument
            result = start_job(inst.symbol, inst.market_class, mode="full",
                               source="jarvis",
                               options={"venue": "delta",
                                        "note": "Delta Exchange India"},
                               cfg=default_config())
            if not result.get("ok"):
                return (f"Could not start the Delta backtest: "
                        f"{result.get('error')}. Nothing ran.")
            if speak:
                speak("Delta backtest research queued, sir. I will report "
                      "when it finishes.")
            return (f"Delta backtest queued: {result.get('job_id')} · "
                    f"{inst.symbol} · {inst.market_class} · "
                    f"{inst.exchange}. It verifies real public candles, runs "
                    "the reviewed strategies out-of-sample and saves the "
                    "report. Research only — no order path exists."
                    + (" (deduplicated an existing identical job)"
                       if result.get("deduplicated") else ""))
        except Exception as exc:
            return (f"The Delta backtest lab is unavailable: "
                    f"{type(exc).__name__}: {str(exc)[:180]}. Nothing was run.")

    try:
        from orchestrator.agents.backtest.backtest_agent import BacktestAgent
        from orchestrator.contracts import AgentRequest, TaskType
        agent = BacktestAgent()
    except Exception as exc:
        return (f"The backtest lab is unavailable: {type(exc).__name__}: "
                f"{str(exc)[:180]}. Nothing was run.")

    parts = ["backtest", action, symbol, market, job_id]
    text = " ".join(p for p in parts if p)
    request = AgentRequest(task_id="task-backtest-action", task_type=TaskType.BACKTEST,
                           text=text, metadata={"source": "action"})
    result = agent.handle(request)
    answer = str(result.text or "").strip()
    if action == "start" and speak and result.status.value in ("completed",
                                                              "needs_input"):
        speak("Backtest research queued, sir. I will report when it finishes.")
    return answer or f"Backtest action '{action}' returned no text."


TOOL = {
    "name": "backtest_agent",
    "description": (
        "Start or inspect a bounded crypto/gold/Delta backtest research job. "
        "Give ONE symbol: BTC, ETH, SOL, GOLD for the default venues, or an "
        "exact Delta Exchange India product (BTCUSD, ETHUSD, BTC_INR) with "
        "venue=delta. The lab resolves the exact live instrument, verifies at "
        "least one year of real candles, tunes reviewed strategies on a "
        "training slice, validates chronologically, tests once out-of-sample, "
        "saves Markdown/CSV/JSON reports and notifies Hermes. Runs detached, "
        "so it never blocks the conversation. Use for 'backtest BTC', 'test "
        "BTCUSD on Delta', 'how is the gold backtest doing', 'show the "
        "backtest report', 'cancel the backtest'. RESEARCH ONLY: it cannot "
        "place, size or route an order and must not be used for live trading. "
        "For a quick Delta quote or product list use delta_exchange."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("start | status | report | cancel | history | "
                                "champion | schedule | rollback (default start)"),
            },
            "symbol": {
                "type": "STRING",
                "description": ("BTC, ETH, SOL or GOLD; with venue=delta use "
                                "an exact product such as BTCUSD, ETHUSD or "
                                "BTC_INR. Optional for status/report."),
            },
            "market": {
                "type": "STRING",
                "description": ("spot | perp for crypto; xauusd | futures | mcx "
                                "for gold; delta_perp | delta_spot for Delta. "
                                "Omit to use the saved/default market."),
            },
            "venue": {
                "type": "STRING",
                "description": ("auto (default Binance/Kraken/Yahoo) or delta "
                                "for Delta Exchange India. Delta is public "
                                "data only and results are paper/research."),
            },
            "job_id": {
                "type": "STRING",
                "description": "Optional bt-… job id when it is unambiguous.",
            },
        },
        "required": ["action"],
    },
    "handler": backtest_agent,
}
