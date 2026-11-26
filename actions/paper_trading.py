"""
actions/paper_trading.py — Jarvis voice surface for the paper trading engine.

PAPER ONLY. No broker, no real order endpoint, no live switch. The engine
consumes the existing liquidity strategy; this action only reports and toggles
paper entries.
"""
from __future__ import annotations

from core.paper_commands import format_config_help, format_settings
from core.paper_trading import (
    format_balance, format_history, format_performance, format_positions,
    format_report, format_status, get_engine,
)


def paper_trading(parameters: dict | None = None, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").strip().lower()
    try:
        engine = get_engine()
        if action in ("status", "health"):
            return format_status(engine)
        if action in ("settings", "config", "configuration"):
            if action == "settings":
                return format_settings(engine)
            return format_config_help()
        if action in ("set", "set_setting", "update_setting"):
            key = str(params.get("setting") or "").strip()
            value = params.get("value")
            if not key or value is None:
                return ("error: set needs 'setting' and 'value' "
                        "(e.g. setting=risk_percent, value=2). PAPER ONLY.")
            try:
                result = engine.update_setting(key, value, actor="voice",
                                               source="action")
            except ValueError as exc:
                return f"⚠ Rejected: {exc}. No setting was changed."
            if not result.get("changed"):
                return f"{result['label']} is already {value}. Nothing changed."
            return (f"{result['label']} updated: {result['old']} → "
                    f"{result['new']}. Existing positions are unchanged. "
                    "PAPER TRADING ONLY.")
        if action in ("start", "enable"):
            engine.start_trading()
            return ("✅ Paper trading entries enabled. Open positions continue "
                    "to be managed.\n\n" + format_status(engine))
        if action in ("stop", "disable"):
            engine.stop_trading()
            return ("🛑 New paper entries stopped. Existing open positions are "
                    "still managed until SL/TP/invalidation.\n\n"
                    + format_status(engine))
        if action in ("positions", "open"):
            return format_positions(engine)
        if action in ("history", "trades"):
            return format_history(engine, limit=int(params.get("limit") or 20))
        if action in ("balance", "equity"):
            return format_balance(engine)
        if action in ("performance", "stats"):
            return format_performance(engine)
        if action in ("report", "daily_report"):
            return format_report(engine)
        if action == "reset":
            if params.get("confirm") is not True:
                return ("Paper reset requires explicit confirmation. Say "
                        "'reset paper account confirm' or send "
                        "/paper_reset confirm. The current paper account will "
                        "be archived; Binance/liquidity history is untouched.")
            result = engine.reset(confirm=True)
            return ("✅ Paper account reset. Previous account archived: "
                    f"{result['archived_account']}\nNew account: "
                    f"{result['account_id']}\n\n" + format_status(engine))
        return ("Unknown paper action. Use status / start / stop / positions / "
                "history / balance / performance / report / reset. "
                "PAPER ONLY.")
    except Exception as exc:
        return (f"Paper trading error: {type(exc).__name__}: "
                f"{str(exc)[:240]}. No order was placed.")


TOOL = {
    "name": "paper_trading",
    "description": (
        "Autonomous PAPER (virtual) trading account for the liquidity-sweep "
        "reversal strategy. Reports account/equity/P&L, open positions, trade "
        "history, performance and the daily report; can enable/disable new "
        "paper entries, read/change paper settings (capital, quantity mode, "
        "leverage, risk, risk/reward, daily limits, fees, slippage) and reset "
        "the paper account with explicit confirmation. "
        "PAPER ONLY: it never places a real order, never touches a broker and "
        "cannot switch to live. Open positions are managed automatically by "
        "the background paper engine."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": [
                    "status", "start", "stop", "positions", "history",
                    "balance", "performance", "report", "reset",
                    "settings", "config", "set",
                ],
                "description": "Which paper-trading operation to perform.",
            },
            "setting": {
                "type": "STRING",
                "description": ("For action=set: risk_percent, reward_risk, "
                                "leverage, starting_capital, fixed_quantity, "
                                "quantity_mode, max_daily_trades, "
                                "max_open_positions, max_daily_loss_percent, "
                                "max_exposure_percent, fee_percent, "
                                "slippage_percent."),
            },
            "value": {
                "type": "STRING",
                "description": "For action=set: the new value (e.g. 2, 1:3, AUTO).",
            },
            "limit": {
                "type": "INTEGER",
                "description": "History rows to return (default 20).",
            },
            "confirm": {
                "type": "BOOLEAN",
                "description": "Required true for reset.",
            },
        },
        "required": ["action"],
    },
    "handler": paper_trading,
}
