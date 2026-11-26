"""
scripts/trading_agent_demo.py — offline demo of the trading agent.

Runs the COMPLETE pipeline on the deterministic simulated market (no network,
no keys, no real orders, real files only under a disposable JARVIS_HOME):

    1. analyze SIMX        → full analysis card + PAPER proposal → approval
    2. approve             → paper order placed into the demo sandbox
    3. quote / options     → provenance-labeled data + computed greeks
    4. backtest SIMX       → bar-replay metrics through the same strategy code
    5. positions           → the open paper position

Run:  .venv/bin/python scripts/trading_agent_demo.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# Disposable JARVIS_HOME: demo state never touches the real trading files.
os.environ["JARVIS_HOME"] = tempfile.mkdtemp(prefix="jarvis-trading-demo-")
# A permissive-but-labeled safety profile for the demo (daily caps still on).
from orchestrator.agents.trading import risk as rk          # noqa: E402
_d = rk.trading_dir()
_d.mkdir(parents=True, exist_ok=True)
(_d / rk.SAFETY_FILE).write_text(__import__("json").dumps(
    {**rk.DEFAULT_SAFETY, "mode": "paper", "max_position_usd": 5000.0,
     "cooldown_seconds": 0, "max_daily_trades": 20}))

from orchestrator.agents.trading.trading_agent import TradingAgent    # noqa: E402
from orchestrator.agents.trading.checkpoint import CheckpointStore    # noqa: E402
from orchestrator.contracts import AgentRequest, TaskType, TaskStatus  # noqa: E402


def banner(t: str) -> None:
    print(f"\n{'=' * 72}\n  {t}\n{'=' * 72}")


def main() -> int:
    agent = TradingAgent(checkpoint_store=CheckpointStore())

    banner("1 · ANALYZE — DATA → ANALYSIS → SIGNAL → RISK → HUMAN APPROVAL")
    r = agent.handle(AgentRequest(
        task_id="demo-task-1", task_type=TaskType.TRADING,
        text="analyze SIMX", metadata={"prefer_sim": True, "capital": 10_000}))
    print(r.text)
    print(f"\n  → status: {r.status.value}")

    if r.status is not TaskStatus.NEEDS_APPROVAL:
        print("\n(demo symbol produced no proposal this run — pipeline still "
              "valid; re-run to vary, the sim is deterministic per symbol)")
        return 0

    banner("2 · APPROVED — the human said yes → PAPER order (no real money)")
    r2 = agent.handle(AgentRequest(
        task_id="demo-task-1", task_type=TaskType.TRADING,
        text="(approved)", metadata={"approved": True,
                                     "resume": "demo-task-1",
                                     "approved_by": "mark"}))
    print(r2.text)

    banner("3 · QUOTE + OPTIONS — provenance labels on every answer")
    r3 = agent.handle(AgentRequest(
        task_id="demo-task-2", task_type=TaskType.TRADING, text="quote SIMX",
        metadata={"trading_action": "quote", "prefer_sim": True}))
    print(r3.text)
    r4 = agent.handle(AgentRequest(
        task_id="demo-task-3", task_type=TaskType.TRADING, text="options SIMX",
        metadata={"trading_action": "options", "prefer_sim": True}))
    print(r4.text)

    banner("4 · BACKTEST — the same strategy code over history")
    r5 = agent.handle(AgentRequest(
        task_id="demo-task-4", task_type=TaskType.TRADING,
        text="backtest SIMX",
        metadata={"trading_action": "backtest", "symbol": "SIMX",
                  "prefer_sim": True}))
    print(r5.text)

    banner("5 · POSITIONS — the paper book")
    r6 = agent.handle(AgentRequest(
        task_id="demo-task-5", task_type=TaskType.TRADING, text="positions",
        metadata={"trading_action": "positions"}))
    print(r6.text)

    banner("DONE — nothing touched a real exchange; demo state is in a "
           "temp JARVIS_HOME")
    return 0


if __name__ == "__main__":
    sys.exit(main())
