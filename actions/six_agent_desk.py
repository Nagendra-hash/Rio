"""
six_agent_desk.py — bridge action: JARVIS convenes the Six Agent Trading Desk.

Runs scripts/six_agent_desk.py (the six-agent paper-trading desk seated inside
the TradingAgents graph): ATLAS scans a candidate list and picks one market,
VEGA reads the chart, ORION checks dated news, LUNA reads the crowd, bull/bear
researchers challenge the idea, NOVA drafts a proposal, TITAN runs hard risk
checks with veto power, and NOVA issues the final PAPER decision
(TRADE / WAIT / REJECT) plus a markdown journal entry.

PAPER TRADING ONLY. Nothing here touches a broker, keys, or real money. The
desk's own rules force TITAN's veto to be final, and a paper win proves
nothing about real-money performance.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT    = Path(__file__).resolve().parent.parent
_PYTHON  = str(_ROOT / ".venv-tradingagents" / "bin" / "python")
_SCRIPT  = _ROOT / "scripts" / "six_agent_desk.py"
_TIMEOUT = 1200   # seconds: a full desk run is ~10-15 min through the free pool


def six_agent_desk(parameters: dict, player=None, speak=None) -> str:
    """Run the six-agent desk for one ticker and speak the paper decision."""
    parameters = parameters or {}
    ticker     = str(parameters.get("ticker", "")).strip().upper()
    candidates = str(parameters.get("candidates", "")).strip()
    skip_scout = bool(parameters.get("skip_scout", False))

    if not ticker:
        return "Which ticker should the trading desk evaluate, sir?"
    if not _SCRIPT.exists():
        return "The desk runner is missing, sir — check scripts/six_agent_desk.py."
    if not Path(_PYTHON).exists():
        return ("The desk venv is missing, sir — rebuild .venv-tradingagents "
                "with the framework's requirements installed.")

    if player:
        player.write_log(f"[six-agent-desk] {ticker} candidates={candidates or 'default'}")
    if speak:
        speak(f"Convening the six-agent trading desk for {ticker}, sir. "
              "Paper trading only — this will take ten to fifteen minutes.")

    cmd = [_PYTHON, str(_SCRIPT), ticker]
    if candidates:
        cmd += ["--candidates", candidates]
    if skip_scout:
        cmd.append("--skip-scout")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_TIMEOUT,
                              env={"HOME": str(Path.home()),
                                   "PATH": "/usr/local/bin:/usr/bin:/bin"})
    except subprocess.TimeoutExpired:
        return (f"The desk ran out of time on {ticker}, sir. The free-tier "
                "brain was likely throttled — try again shortly.")

    out = (proc.stdout or "").strip()
    if proc.returncode == 0 and out:
        return out[:2500]

    err = (proc.stderr or "").strip().splitlines()
    reason = err[-1] if err else f"exit {proc.returncode}"
    if "429" in reason or "rate" in reason.lower():
        return ("The desk was rate-limited by the free LLM pool, sir. "
                "The quota resets within minutes — ask me to retry shortly.")
    return f"The six-agent desk run for {ticker} failed: {reason[:250]}"


TOOL = {
    "name": "six_agent_desk",
    "description": (
        "Convene the Six Agent AI Trading Desk for one ticker — PAPER TRADING "
        "ONLY. ATLAS scans a candidate list and picks the focus market, VEGA "
        "reads the chart, ORION verifies dated news, LUNA measures crowd "
        "sentiment and hype, bull/bear researchers challenge the idea, TITAN "
        "runs hard risk checks (max loss, reward-to-risk, missing inputs) "
        "with veto power, and NOVA issues the final decision: TRADE, WAIT or "
        "REJECT, plus a saved journal entry. Takes ten to fifteen minutes. "
        "Use when the user asks to 'run the trading desk' or for the "
        "six-agent paper-trading workflow. Not for quick quotes, and never "
        "for real orders — this is a paper-trading exercise, not financial "
        "advice."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "ticker": {
                "type": "STRING",
                "description": "Intended focus ticker, e.g. NVDA, AAPL."
            },
            "candidates": {
                "type": "STRING",
                "description": ("Optional comma list of up to five candidates "
                                "for ATLAS to rank (default: AAPL,MSFT,NVDA,AMD,META "
                                "plus the focus ticker).")
            },
            "skip_scout": {
                "type": "BOOLEAN",
                "description": ("Skip ATLAS and run the desk directly on the "
                                "given ticker (default false).")
            }
        },
        "required": ["ticker"]
    },
    "handler": six_agent_desk,
}
