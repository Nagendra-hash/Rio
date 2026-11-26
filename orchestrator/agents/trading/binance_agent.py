"""
orchestrator/agents/trading/binance_agent.py — Crypto market data agent.

Routes natural-language requests about live crypto prices, alerts, and
market summaries into the binance_ws daemon + binance_analyzer action.
"""
from __future__ import annotations

import re

from orchestrator.base import BaseAgent
from orchestrator.contracts import AgentRequest, AgentResult, TaskStatus, TaskType

_RE_SYM = re.compile(
    r"\b(btc|bitcoin|xbt|eth|ethereum|sol|solana|paxg|xaut)\b", re.IGNORECASE)
_SYM_ALIASES = {
    "btc": "BTC", "bitcoin": "BTC", "xbt": "BTC",
    "eth": "ETH", "ethereum": "ETH",
    "sol": "SOL", "solana": "SOL",
    "paxg": "PAXG", "xaut": "XAUT",
}
_RE_ALERT = re.compile(
    r"\b(alert|watch|notify|below|above|cross|break\s*(above|below)|spike)",
    re.IGNORECASE,
)
_RE_QUERY = re.compile(
    r"\b(price|how\s*much|what's?\s*(the\s*)?price|current|quote|last|value)",
    re.IGNORECASE,
)
_RE_STATS = re.compile(
    r"\b(1h|hour|hourly|stats|range|high|low|change|trend|performance)",
    re.IGNORECASE,
)
_RE_CRYPTO = re.compile(
    r"\b(?:btc|bitcoin|xbt|eth|ethereum|sol|solana|paxg|xaut|"
    r"binance|crypto|cryptocurrency)\b",
    re.IGNORECASE,
)
# Market-DATA asks only. "BTC analysis / setup / should I buy" stays with the
# generic TradingAgent; this agent reports live prices, candles and alerts.
_RE_DATA_INTENT = re.compile(
    r"\b(?:price|prices|quote|current|last|value|how\s+much|"
    r"alert|alerts|notify|watch|below|above|"
    r"candles?|bars?|chart|stats?|hourly|range|summary|status)\b"
    r"|\b\d+\s*(?:h|hr|hrs|hour|hours)\b",
    re.IGNORECASE,
)
# Strategy asks that belong to the liquidity engine, not to a trade order.
# Deliberately does NOT match a bare "buy BTC" (TradingAgent owns trade
# requests, always behind its approval gate).
_RE_LIQUIDITY = re.compile(
    r"\b(?:liquidity|sweeps?|reversal|long\s+signal|buy\s+signal|"
    r"should\s+(?:i|we)\s+buy|is\s+it\s+a\s+buy|"
    r"reversal\s+confirmation)\b",
    re.IGNORECASE,
)


class BinanceAgent(BaseAgent):
    name = "binance_agent"
    description = (
        "Live cryptocurrency market data from Binance: current prices for "
        "BTC, ETH, SOL, PAXG, XAU; 1-hour stats; candle history; price-level "
        "alerts; periodic market summaries pushed to Telegram via Hermes."
    )
    task_types = (TaskType.TRADING,)
    # The generic TradingAgent shares this lane; a higher priority plus a
    # crypto-only can_handle() means crypto market asks land here while
    # NIFTY/portfolio/order asks still reach TradingAgent.
    priority = 10

    def can_handle(self, request: AgentRequest) -> bool:
        text = request.text or ""
        return (
            request.task_type is TaskType.TRADING
            and bool(_RE_CRYPTO.search(text))
            and bool(_RE_DATA_INTENT.search(text) or _RE_LIQUIDITY.search(text))
        )

    def handle(self, request: AgentRequest) -> AgentResult:
        # AgentResult.ok is a read-only property, not a constructor helper —
        # every previous return here called it and raised TypeError.
        def _ok(message: str) -> AgentResult:
            return AgentResult(
                task_id=request.task_id,
                status=TaskStatus.COMPLETED,
                text=message,
                agent=self.name,
            )

        def _err(message: str) -> AgentResult:
            return AgentResult(
                task_id=request.task_id,
                status=TaskStatus.FAILED,
                error=message,
                agent=self.name,
            )

        text = (request.text or "").strip().lower()
        if not text:
            return _err("empty input")
        try:
            from actions.binance_analyzer import (
                binance_status, binance_query, binance_set_alert,
                binance_rm_alert, binance_summary,
            )

            m_sym = _RE_SYM.search(text)
            sym = _SYM_ALIASES.get(m_sym.group(1).lower(), "BTC") if m_sym else "BTC"

            if _RE_LIQUIDITY.search(text):
                from core.binance_ws import normalize_symbol
                from core.liquidity_sweep import get_engine

                pair = normalize_symbol(sym).upper()
                snapshot = get_engine().get_status(pair)
                if not snapshot.get("ok"):
                    return _ok(
                        f"No liquidity/reversal profile is configured for "
                        f"{pair}. Configured: BTCUSDT and ETHUSDT.")
                return _ok(snapshot.get("advice") or "No signal.")

            # Remove first: _RE_ALERT matches the word "alert", so doing set
            # first meant "remove btc alert" ADDED/updated one instead.
            if re.search(r"\b(?:remove|delete|cancel|clear|stop)\b[^.?!\n]{0,24}\balert", text):
                key_m = re.search(r"\b(below|above|pct_1m|pct_5m)\b", text)
                key = key_m.group(1) if key_m else "below"
                result_text = binance_rm_alert({"symbol": sym, "key": key})
                return _ok(result_text)

            # --- alert setup ---
            if _RE_ALERT.search(text):
                m_below = re.search(r"below\s*\$?([\d,]+\.?\d*)", text)
                m_above = re.search(r"above\s*\$?([\d,]+\.?\d*)", text)
                params: dict = {"symbol": sym}
                if m_below:
                    params["below"] = float(m_below.group(1).replace(",", ""))
                if m_above:
                    params["above"] = float(m_above.group(1).replace(",", ""))
                m_pct = re.search(
                    r"(\d+(?:\.\d+)?)\s*%\s*(?:move\s+)?(?:on|in|over)?\s*"
                    r"(1\s*m(?:in)?|5\s*m(?:in)?)",
                    text,
                )
                if m_pct:
                    interval = "1m" if m_pct.group(2).startswith("1") else "5m"
                    params[f"pct_{interval}"] = float(m_pct.group(1))
                if len(params) == 1:
                    return _ok(
                        f"Tell me the level or move for {sym}: e.g. "
                        f"'alert me if BTC drops below 60000' or "
                        f"'alert on ETH 2% move in 1m'.",
                    )
                result_text = binance_set_alert(params)
                return _ok(result_text)

            # --- summary ---
            if "summary" in text or "push" in text:
                result_text = binance_summary({})
                return _ok(result_text)

            # --- query / stats ---
            if (_RE_QUERY.search(text) or _RE_STATS.search(text)
                    or re.search(r"\b(?:candles?|bars?|chart)\b", text)):
                params = {"symbol": sym}
                m_hours = re.search(
                    r"(\d+)\s*(?:h|hr|hrs|hour|hours)\b", text)
                if m_hours:
                    params["hours"] = int(m_hours.group(1))
                elif "1h" in text or "hour" in text or "stats" in text:
                    params["hours"] = 1
                if "candle" in text or "chart" in text:
                    # `int(re.search(...) or 20).group()` raised on text with
                    # no number ("show btc candles"), turning a valid ask into
                    # an error result.
                    m_bars = re.search(r"\b(\d+)\s*(?:candles?|bars?)\b", text)
                    params["bars"] = min(int(m_bars.group(1)), 100) if m_bars else 20
                m_int = re.search(r"\b(1m|5m|15m|1\s*min|5\s*min|15\s*min)\b", text)
                if m_int:
                    params["interval"] = (
                        m_int.group(1).replace(" ", "").replace("min", "m"))
                result_text = binance_query(params)
                return _ok(result_text)

            # --- default: status + latest prices ---
            result_text = binance_status({})
            return _ok(result_text)

        except Exception as exc:
            return _err(str(exc)[:300])
