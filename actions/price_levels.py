"""
price_levels.py — voice answers for "what's bitcoin at?" from the journal.

Reads the latest price snapshot the india-market-tracker cron appended to
memory/trading/price_levels.jsonl (BTC-USD, GC=F gold futures, INR=X) and
speaks it back. Deliberately NO fresh fetch: the answer must be instant and
offline-safe — the tracker refreshes the data on its daily 08:30 IST run
(`npm start` in ~/browserbase-demos/india-market-tracker refreshes on demand).

When the queried symbol also has an open paper position, the answer appends
the direction-aware stop-proximity flag from plugins/_trading_journal.py, so
"bitcoin ka kya haal hai?" can surface "stop 1.6% door hai" without the user
asking for paper_status separately.
"""
from __future__ import annotations

from datetime import datetime, timezone

from plugins import _trading_journal as tj

# Spoken-language aliases → tracker symbol.
_KEYWORD_MAP: dict[str, tuple[str, ...]] = {
    "BTC-USD": ("bitcoin", "btc", "crypto", "satoshi"),
    "GC=F": ("gold", "xau", "sona", "bullion"),
    "INR=X": ("dollar", "inr", "rupee", "usdinr", "usd"),
    "RELIANCE.NS": ("reliance", "ril", "ambani"),
    "HDFCBANK.NS": ("hdfc bank", "hdfcbank", "hdfc"),
    "TCS.NS": ("tcs", "tata consultancy", "tata consultancy services"),
    "INFY.NS": ("infosys", "infy"),
    "ICICIBANK.NS": ("icici bank", "icicibank", "icici"),
    "SBIN.NS": ("sbi", "state bank", "sbin"),
}

_LABELS = {
    "BTC-USD": "Bitcoin",
    "GC=F": "Gold futures",
    "INR=X": "Dollar-rupee",
    "RELIANCE.NS": "Reliance",
    "HDFCBANK.NS": "HDFC Bank",
    "TCS.NS": "TCS",
    "INFY.NS": "Infosys",
    "ICICIBANK.NS": "ICICI Bank",
    "SBIN.NS": "SBI",
}


def _match_symbol(query: str) -> str | None:
    q = (query or "").strip().lower()
    if not q:
        return None
    for symbol, keywords in _KEYWORD_MAP.items():
        if any(k in q for k in keywords):
            return symbol
    # direct symbol text (e.g. "gc=f", "btc-usd")
    for symbol in _KEYWORD_MAP:
        if symbol.lower() in q or q in symbol.lower():
            return symbol
    return None


def _age_hours(ts) -> float | None:
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(str(ts).replace("Z", "+00:00"))).total_seconds() / 3600.0
    except Exception:
        return None


def price_levels(parameters: dict, player=None) -> str:
    parameters = parameters or {}
    raw_query = str(parameters.get("symbol", "")).strip()
    want_stops = str(parameters.get("with_stops", "")).strip().lower() in ("true", "yes", "1")

    records = tj.get_price_levels(limit=1)
    if not records:
        return ("Price levels journal khaali hai — market tracker abhi tak nahi chala. "
                "Ise 'npm start' se ~/browserbase-demos/india-market-tracker mein chala sakte hain.")

    rec = records[-1]
    prices = rec.get("prices") or []
    if not prices:
        return "Latest tracker run mein price levels nahi mile."

    age = _age_hours(rec.get("ts"))
    age_note = ""
    if age is not None:
        age_note = " just now" if age < 1 else (
            f" ({age:.0f} hours ago)" if age < 26 else f" — STALE, {age/24:.0f} days old, tracker needs a run"
        )

    wanted = _match_symbol(raw_query) if raw_query else None
    selected = [p for p in prices if (wanted and p.get("symbol") == wanted)] or prices

    lines: list[str] = []
    for p in selected:
        sym = p.get("symbol", "?")
        price = p.get("price")
        chg = p.get("day_change_pct") or 0.0
        if not isinstance(price, (int, float)):
            continue
        label = _LABELS.get(sym, sym)
        arrow = "upar" if chg >= 0 else "neeche"
        sign = "+" if chg >= 0 else ""
        lines.append(f"{label} {price:,.2f} par hai — {sign}{chg:.2f}% {arrow} vs previous close{age_note if len(selected) == 1 else ''}")

        # Stop proximity when an open paper position exists for this symbol.
        if want_stops or wanted:
            level_sym = tj._level_symbol(sym)
            for pos in tj.get_positions().values():
                if tj._level_symbol(pos.get("symbol", "")) != level_sym:
                    continue
                emoji, phrase, _ = tj.stop_proximity_flag(
                    pos.get("direction", "LONG"), float(price), float(pos.get("stop_loss", 0.0))
                )
                lines.append(
                    f"Paper position: {pos['direction']} {pos['symbol']} @ {pos['entry_price']:,.2f} — "
                    f"live {price:,.2f}, {phrase} ({emoji})"
                )

    if not lines:
        return f"No price data matched '{raw_query}'. Logged symbols: {', '.join(p.get('symbol', '?') for p in prices)}."

    if len(selected) > 1:
        lines.append(f"(Source: india-market-tracker snapshot{age_note})")
    return " ".join(lines)


TOOL = {
    "name": "price_levels",
    "description": (
        "Answers 'what's bitcoin at?', 'gold price?', 'reliance ka bhav?' etc. from "
        "the latest india-market-tracker snapshot stored in JARVIS's trading journal "
        "— instant, no live fetch. Covers BTC, gold, USD/INR and NSE names "
        "(RELIANCE, HDFCBANK, TCS, INFY, ICICIBANK, SBIN) when present in the "
        "snapshot. Also reports stop-loss proximity for any open paper position "
        "on that symbol."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "symbol": {
                "type": "STRING",
                "description": "Optional: 'bitcoin'/'btc', 'gold', 'dollar'/'inr', or an NSE name like 'reliance'/'tcs'/'hdfc'. Empty = all logged levels."
            },
            "with_stops": {
                "type": "STRING",
                "description": "Optional 'true' to include stop-loss proximity for open paper positions."
            }
        },
        "required": []
    },
    "handler": price_levels,
}
