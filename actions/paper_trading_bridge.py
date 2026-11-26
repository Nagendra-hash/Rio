"""
actions/paper_trading_bridge.py — Paper Trading Bridge from Backtest Champions.

Connects backtest research champions to the paper trading execution pipeline:
  Champion → Signal Proposal → Risk Check → Human Approval → PaperBroker

This action mirrors the Delta Exchange read-only interface pattern but for
paper trading: it discovers champions, evaluates them against current market
data, and executes paper positions through the same approval gates as live
trading (minus the real order). No real money moves.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path for orchestrator imports
import sys
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from orchestrator.agents.backtest.champions import ChampionStore
from orchestrator.agents.backtest.config import default_config
from orchestrator.agents.trading import signal as sg
from orchestrator.agents.trading import risk as rk
from orchestrator.agents.trading import broker as bk
from orchestrator.agents.trading.data import MarketData
from orchestrator.agents.trading.checkpoint import CheckpointStore, new_stage_trace
from orchestrator.base import BaseAgent
from orchestrator.contracts import AgentRequest, AgentResult, TaskStatus, TaskType


# Map backtest engine strategies to trading agent strategies
_BACKTEST_TO_TRADING_STRATEGY = {
    # Breakout-type strategies
    "atr_breakout": "breakout",
    "donchian_breakout": "breakout",
    "macd_momentum": "breakout",
    # Trend-following strategies
    "ema_cross": "trend_follow",
    "ema_adx_trend": "trend_follow",
    "vol_trend": "trend_follow",
    "vwap_intraday": "trend_follow",
    # Mean reversion strategies
    "rsi_meanrev": "mean_revert",
    "bb_meanrev": "mean_revert",
    # Benchmark (no signal)
    "buy_hold": None,
}


def _map_strategy(backtest_strategy: str) -> str | None:
    """Map a backtest strategy name to a trading agent strategy."""
    return _BACKTEST_TO_TRADING_STRATEGY.get(backtest_strategy)


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:+.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_rr(value: Any) -> str:
    try:
        return f"1 : {float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _champion_line(key: str, entry: dict) -> str:
    champ = entry.get("champion") or {}
    if not champ:
        return f"{key}: no active champion"
    retired_at = entry.get("retired_at")
    retired_reason = entry.get("retired_reason")
    oos = champ.get("oos") or {}
    status = " (RETIRED)" if retired_at else ""
    line = (f"{key}: {champ.get('strategy')} v{champ.get('version')} on "
            f"{champ.get('timeframe')} · promoted {champ.get('promoted_at')} "
            f"· OOS {_fmt_pct(oos.get('net_return'))}, Sharpe "
            f"{oos.get('sharpe')} · score {champ.get('oos_score')}{status}")
    if retired_at:
        line += f" · retired {retired_at}: {retired_reason}"
    return line


def paper_trading_bridge(parameters: dict, player=None, speak=None,
                          response=None, session_memory=None,
                          dashboard=None) -> str:
    """
    Paper Trading Bridge: connect backtest champions to paper execution.

    Actions:
      - list: show all available champions
      - evaluate: evaluate a champion against current market data (signal → risk)
      - approve: approve a pending paper trade (resumes checkpoint)
      - positions: show open paper positions
      - status: show bridge status and recent activity
    """
    parameters = parameters or {}
    action = str(parameters.get("action") or "list").strip().lower()
    symbol = str(parameters.get("symbol") or "").strip().upper()
    market = str(parameters.get("market") or "").strip().lower()
    champion_key = str(parameters.get("champion_key") or "").strip()
    task_id = str(parameters.get("task_id") or "").strip()
    capital = float(parameters.get("capital") or 10_000.0)
    risk_pct = float(parameters.get("risk_pct") or 1.0)
    strategy_override = str(parameters.get("strategy") or "").strip().lower()
    timeframe_override = str(parameters.get("timeframe") or "").strip().lower()

    if player:
        player.write_log(f"[paper_bridge] {action} {symbol} {market} {champion_key}")

    cfg = default_config()
    store = ChampionStore(cfg=cfg)
    md = MarketData(prefer_sim=False)
    paper_broker = bk.PaperBroker()

    # ── LIST ────────────────────────────────────────────────────────────────
    if action in ("list", "champions"):
        ledger = store.all()
        if symbol:
            ledger = {k: v for k, v in ledger.items() if k.startswith(symbol.upper() + ":")}
        if market:
            ledger = {k: v for k, v in ledger.items() if k.endswith(":" + market.lower())}
        if not ledger:
            return "No champions recorded yet. Run a backtest first (e.g., 'backtest BTC')."
        lines = [f"{len(ledger)} champion(s) available for paper trading:"]
        for key, entry in sorted(ledger.items()):
            lines.append("  " + _champion_line(key, entry))
        lines.append("\nUse 'evaluate' with a champion_key to test against current market.")
        return "\n".join(lines)

    # ── STATUS ──────────────────────────────────────────────────────────────
    if action in ("status", "health"):
        ledger = store.all()
        open_positions = paper_broker.open_positions()
        lines = [
            f"Paper Trading Bridge — {len(ledger)} champion(s) in ledger, "
            f"{len(open_positions)} open paper position(s)",
            f"Capital: {capital:,.0f} · Risk per trade: {risk_pct}%",
        ]
        if open_positions:
            lines.append("Open positions:")
            for key, pos in open_positions.items():
                lines.append(
                    f"  {key}: {pos['symbol']} {pos['direction']} "
                    f"{pos['position_size']}u @ {pos['entry_price']} SL {pos['stop_loss']}"
                )
        return "\n".join(lines)

    # ── POSITIONS ──────────────────────────────────────────────────────────
    if action in ("positions", "open"):
        opens = paper_broker.open_positions()
        if not opens:
            return "No open paper positions."
        lines = [f"{len(opens)} open paper position(s):"]
        for k, v in opens.items():
            lines.append(
                f"  {k}: {v['symbol']} {v['direction']} "
                f"{v['position_size']}u @ {v['entry_price']} SL {v['stop_loss']} "
                f"T1 {v['targets'][0] if v['targets'] else '—'}"
            )
        return "\n".join(lines)

    # ── EVALUATE ────────────────────────────────────────────────────────────
    if action in ("evaluate", "test", "signal"):
        if not champion_key:
            return ("Provide a champion_key (e.g., 'BTC:spot'). "
                    "Use 'list' to see available champions.")
        entry = store.get(champion_key)
        if not entry or not entry.get("champion"):
            return f"No champion found for key '{champion_key}'."
        champ = entry["champion"]

        # Resolve symbol/market from champion key if not provided
        if not symbol:
            # champion_key format is "SYMBOL:market"
            if ":" in champion_key:
                symbol = champion_key.split(":")[0].upper()
            else:
                symbol = str(champ.get("instrument") or "").upper()
        if not market:
            if ":" in champion_key:
                market = champion_key.split(":")[1].lower()
            else:
                market = str(champ.get("market") or "").lower()

        # Map backtest strategy to trading agent strategy
        backtest_strategy = champ.get("strategy")
        mapped_strategy = _map_strategy(backtest_strategy)
        strategy = strategy_override or mapped_strategy or backtest_strategy
        timeframe = timeframe_override or champ.get("timeframe")
        params = champ.get("params") or {}

        if mapped_strategy is None and backtest_strategy != "buy_hold":
            return (f"Champion {champion_key} uses backtest strategy "
                    f"'{backtest_strategy}' which has no trading agent mapping. "
                    f"Use 'strategy' parameter to override (trend_follow, "
                    f"mean_revert, breakout).")

        # Fetch current market data
        venue = str(champ.get("provider") or "").strip().lower()
        hist = md.history(symbol, period="6mo", interval="1d", venue=venue or None)
        if not hist.ok:
            return f"Market data unavailable for {symbol}: {hist.error}"

        bars = hist.data["bars"]
        quote_res = md.quote(symbol, venue=venue or None)
        quote = quote_res.data if quote_res.ok else None

        # Run analysis (same as trading_agent)
        from orchestrator.agents.trading import analysis as an
        card = an.analyze(bars, quote=quote)
        if not card.get("ok"):
            return f"Analysis failed for {symbol}: {card.get('error')}"

        # Run signal evaluation using champion's strategy
        try:
            sig_result = sg.run_signal(card, strategy=strategy, min_score=2)
        except Exception as exc:
            return f"Signal evaluation failed: {type(exc).__name__}: {exc}"

        if not sig_result.get("ok") or not sig_result.get("proposal"):
            note = sig_result.get("note") or sig_result.get("error") or "no edge"
            ev = sig_result.get("evaluation") or {}
            scores = ev.get("scores") or {"LONG": 0, "SHORT": 0}
            return (f"Champion {champion_key} ({strategy}) produces no trade signal "
                    f"on current data: {note} · scores LONG={scores.get('LONG')} "
                    f"SHORT={scores.get('SHORT')}")

        proposal = sig_result["proposal"]

        # Risk check
        venue_meta = {}
        if quote:
            venue_meta = {k: quote.get(k) for k in (
                "venue", "market", "market_class", "product_id",
                "product_symbol", "contract_type", "quote_currency",
                "contract_value", "tick_size") if quote.get(k) not in (None, "")}

        risk_result = rk.full_risk_check(
            proposal, symbol, capital,
            risk_pct=risk_pct,
            min_signal_score=int(proposal.get("score") or 0),
            contract_value=venue_meta.get("contract_value"))

        # Format output
        s = risk_result.get("sizing") or {}
        unit_label = "contracts" if s.get("contracts") else "units"
        lines = [
            f"Paper trade evaluation — {champion_key}",
            f"  Champion: {champ.get('strategy')} v{champ.get('version')} on {champ.get('timeframe')}",
            f"  Current signal: {proposal['direction']} score {proposal['score']}",
            f"  Entry {proposal['entry']} · SL {proposal['stop_loss']} · "
            f"Targets {proposal['targets']}",
            f"  R:R to T1 {proposal['rr_to_target1']}",
        ]
        if risk_result.get("ok"):
            lines.append(
                f"  ✓ Risk CHECK PASSED: {s['units']} {unit_label} "
                f"(~{s['notional']:,.0f} notional) · risk {s['risk_amount']:,.2f} "
                f"({s['risk_pct_of_capital']}% of capital)"
            )
            lines.append(
                f"  To place this paper trade, run: "
                f"paper_trading_bridge approve task_id=<checkpoint_id>"
            )
            # Save checkpoint for approval
            cp_store = CheckpointStore()
            cp_id = f"paper_bridge_{champion_key.replace(':', '_')}_{int(__import__('time').time())}"
            cp_data = {
                "task_id": cp_id, "symbol": symbol, "strategy": strategy,
                "proposal": proposal, "risk": risk_result,
                "venue": venue, "venue_meta": venue_meta,
                "champion_key": champion_key, "status": "awaiting_approval",
            }
            cp_store.save(cp_id, cp_data)
            lines.append(f"  Checkpoint saved: {cp_id}")
        else:
            lines.append(f"  ✗ Risk CHECK FAILED: {risk_result.get('denied_by')}")
            issues = risk_result.get("issues", [])
            if issues:
                lines.append("  Issues: " + "; ".join(issues[:3]))
        return "\n".join(lines)

    # ── APPROVE ─────────────────────────────────────────────────────────────
    if action in ("approve", "execute", "place"):
        if not task_id:
            return "Provide the task_id from the evaluate step (checkpoint ID)."
        cp_store = CheckpointStore()
        cp = cp_store.load(task_id)
        if not cp:
            return f"No checkpoint found for task_id '{task_id}'."
        if cp.get("status") != "awaiting_approval":
            return f"Checkpoint {task_id} is '{cp.get('status')}' — already handled."

        # Re-check risk on approval
        proposal = cp["proposal"]
        symbol = cp["symbol"]
        sizing = (cp.get("risk") or {}).get("sizing") or {}
        recheck = rk.check_safety(symbol, min_signal_score=int(proposal.get("score") or 0))
        if not recheck.get("ok"):
            cp["status"] = "risk_refused_on_approval"
            cp["risk_refusal"] = recheck.get("reasons")
            cp_store.save(task_id, cp)
            return ("Approved, but risk gate refused on re-check: "
                    + "; ".join(recheck.get("reasons", []))
                    + " — nothing was placed.")

        # Execute paper trade directly via PaperBroker (bypass live config checks)
        paper_broker = bk.PaperBroker()
        result = paper_broker.open_position(
            symbol=symbol,
            direction=str(proposal["direction"]),
            entry=float(proposal["entry"]),
            stop_loss=float(proposal["stop_loss"]),
            targets=[float(t) for t in proposal.get("targets") or []],
            units=int(sizing["units"]),
            strategy=str(proposal.get("strategy") or "manual"),
            signal_score=int(proposal.get("score") or 0),
            approved_by="paper_bridge_user",
            signal_id=task_id,
            atr=float((proposal.get("structure_context") or {}).get("atr") or 0.0),
            venue=str(cp.get("venue") or ""),
            product_id=cp.get("venue_meta", {}).get("product_id"),
            contract_type=str(cp.get("venue_meta", {}).get("contract_type") or ""),
            quote_currency=str(cp.get("venue_meta", {}).get("quote_currency") or ""),
            contract_value=cp.get("venue_meta", {}).get("contract_value"),
            market_class=str(cp.get("venue_meta", {}).get("market_class") or ""),
        )

        cp["status"] = "executed_paper" if result.get("ok") else "execution_failed"
        cp["execution"] = result
        cp_store.save(task_id, cp)

        if not result.get("ok"):
            return f"Paper execution failed: {result.get('error')}"

        pos = result.get("position") or {}
        unit_label = "contracts" if sizing.get("contracts") else "units"
        return (f"PAPER order placed (no real money moved): "
                f"{symbol} {pos.get('direction')} "
                f"{sizing.get('units')} {unit_label} @ {pos.get('entry_price')} "
                f"SL {pos.get('stop_loss')} — Position key: {result.get('key')}")

    # ── CLOSE ───────────────────────────────────────────────────────────────
    if action in ("close", "exit"):
        position_key = str(parameters.get("position_key") or "").strip()
        exit_price = parameters.get("exit_price")
        reason = str(parameters.get("reason") or "manual close")
        if not position_key:
            return "Provide position_key (from 'positions' action)."
        if exit_price is None:
            return "Provide exit_price to close the position."
        try:
            result = paper_broker.close_position(position_key, float(exit_price), reason)
            return (f"Paper position closed: {position_key} @ {exit_price} "
                    f"({reason}) · P&L: {result.get('pnl'):,.2f}")
        except Exception as exc:
            return f"Failed to close position: {type(exc).__name__}: {exc}"

    # ── RETIRE CHAMPION ─────────────────────────────────────────────────────
    if action in ("retire", "deactivate"):
        if not champion_key:
            return "Provide champion_key to retire."
        reason = str(parameters.get("reason") or "manual retire via paper bridge")
        result = store.retire(champion_key, reason, job_id="paper_bridge")
        if result.get("ok"):
            return f"Champion {champion_key} retired: {reason}"
        return f"Failed to retire: {result.get('error')}"

    return ("Unknown action. Use: list, status, positions, evaluate, approve, "
            "close, retire")


TOOL = {
    "name": "paper_trading_bridge",
    "description": (
        "Paper Trading Bridge: connect backtest champions to paper execution. "
        "Actions: list (show champions), evaluate (test champion on current market), "
        "approve (place paper trade from checkpoint), positions (show open), "
        "status (bridge health), close (exit position), retire (deactivate champion). "
        "No real orders are placed; all fills are simulated and journaled."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("list | evaluate | approve | positions | "
                                "status | close | retire (default: list)"),
            },
            "symbol": {
                "type": "STRING",
                "description": "Symbol to evaluate (e.g., BTCUSD, AAPL, TCS.NS).",
            },
            "market": {
                "type": "STRING",
                "description": "Market: spot | perp | futures | xauusd | mcx.",
            },
            "champion_key": {
                "type": "STRING",
                "description": "Champion key from 'list' (e.g., 'BTC:spot', 'GOLD:xauusd').",
            },
            "task_id": {
                "type": "STRING",
                "description": "Checkpoint ID from evaluate step for approve action.",
            },
            "capital": {
                "type": "NUMBER",
                "description": "Capital for sizing (default 10000).",
            },
            "risk_pct": {
                "type": "NUMBER",
                "description": "Risk percent per trade (default 1.0).",
            },
            "strategy": {
                "type": "STRING",
                "description": "Override champion strategy (trend_follow, mean_revert, breakout).",
            },
            "timeframe": {
                "type": "STRING",
                "description": "Override champion timeframe (1d, 4h, 1h, etc.).",
            },
            "position_key": {
                "type": "STRING",
                "description": "Position key from 'positions' for close action.",
            },
            "exit_price": {
                "type": "NUMBER",
                "description": "Exit price for close action.",
            },
            "reason": {
                "type": "STRING",
                "description": "Reason for close or retire action.",
            },
        },
        "required": [],
    },
    "handler": paper_trading_bridge,
    "behavior": "BLOCKING",
    "scheduling": "WHEN_IDLE",
}