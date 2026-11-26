"""
plugins/_trading_journal.py — JARVIS Paper Trading Engine.

NOTE: the leading underscore keeps the plugin loader from treating this
library as a plugin (files without a PLUGIN dict get rejected at discovery).
Drop-in extension of the trading_strategy plugin. Adds paper trading to the
existing signal framework: position tracking, trade journal with outcomes,
mistake detection (why a trade lost), and performance stats.

State lives in memory/trading/:

    paper_positions.json      — active paper positions (one per symbol+method)
    paper_journal.jsonl      — completed paper trades with outcome + lessons
    paper_performance.json    — aggregated paper P&L stats
    paper_mistakes.jsonl     — mistakes detected during/after trades

NEVER touches real money or real order systems. Pure simulation on top of the
signal engine already in plugins/trading_strategy.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BASE = Path(__file__).resolve().parent.parent
_TRADING_DIR = _BASE / "memory" / "trading"

_POSITIONS = _TRADING_DIR / "paper_positions.json"
_JOURNAL = _TRADING_DIR / "paper_journal.jsonl"
_PERF = _TRADING_DIR / "paper_performance.json"
_MISTAKES = _TRADING_DIR / "paper_mistakes.jsonl"
_LEARNINGS = _TRADING_DIR / "paper_learnings.json"

# ── Types ──────────────────────────────────────────────────────────────────────

PaperPosition = dict[str, Any]
PaperTrade = dict[str, Any]
MistakeRecord = dict[str, Any]

# ── Helpers ───────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path: Path, default: Any = None) -> Any:
    try:
        data = json.loads(path.read_text())
        return data if data is not None else default
    except Exception:
        return default


def _save(path: Path, data: Any) -> None:
    _TRADING_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def _append_jsonl(path: Path, record: dict) -> None:
    _TRADING_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records


# ── Paper Positions ───────────────────────────────────────────────────────────


def get_positions() -> dict[str, PaperPosition]:
    """Return all active paper positions keyed by 'SYMBOL:METHOD'."""
    return _load(_POSITIONS, {})


def save_positions(positions: dict[str, PaperPosition]) -> None:
    _save(_POSITIONS, positions)


def clear_positions() -> None:
    save_positions({})


# ── Paper Journal ─────────────────────────────────────────────────────────────


def get_journal() -> list[PaperTrade]:
    return _load_jsonl(_JOURNAL)


def journal_trade(trade: PaperTrade) -> None:
    _append_jsonl(_JOURNAL, trade)


def get_price_levels(limit: int = 5) -> list[dict[str, Any]]:
    """Latest BTC/gold/USDINR price-level records logged by the
    india-market-tracker cron (Browserbase news scanner). Kept separate from
    the trade journal: these are market snapshots, not trades, so the learning
    pipeline never sees them. Newest last; returns up to `limit` most recent."""
    path = _TRADING_DIR / "price_levels.jsonl"
    records = _load_jsonl(path)
    return records[-limit:] if limit else records


# Position symbols that refer to the same instrument under a different
# notation than price_levels.jsonl logs (levels use yfinance symbols).
_LEVEL_ALIASES = {
    "BTCUSD": "BTC-USD",
    "BTCUSDT": "BTC-USD",
    "XBTUSD": "BTC-USD",
    "XAUUSD": "GC=F",
    "XAU-USD": "GC=F",
    "GOLD": "GC=F",
    # TV watchlist rows use bare NSE symbols; the journal logs yfinance
    # notation (RELIANCE.NS) — accept both when matching positions to levels.
    "RELIANCE": "RELIANCE.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "TCS": "TCS.NS",
    "INFY": "INFY.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "SBIN": "SBIN.NS",
}


def _level_symbol(symbol: str) -> str:
    """Map a position symbol onto the tracker's level symbol when needed."""
    return _LEVEL_ALIASES.get(symbol, symbol)


def _latest_level_map() -> tuple[dict[str, Any], str | None]:
    """({symbol: price}, ts) from the newest price_levels.jsonl record."""
    records = get_price_levels(limit=1)
    if not records:
        return {}, None
    rec = records[-1]
    return (
        {
            p.get("symbol", ""): p.get("price")
            for p in rec.get("prices", [])
            if isinstance(p.get("price"), (int, float))
        },
        rec.get("ts"),
    )


def _stop_distance_pct(direction: str, price: float, stop: float) -> float:
    """Direction-aware % the live price sits from the stop.
    Positive = stop not yet hit; <= 0 = price at/beyond the stop."""
    if str(direction).upper() == "SHORT":
        return (stop - price) / price * 100.0
    return (price - stop) / price * 100.0


def _stop_flag(dist_pct: float) -> tuple[str, str]:
    """Escalating stop-proximity flag: breached → <1% → <3% → far."""
    if dist_pct <= 0:
        return "🔴", "AT/BEYOND STOP — review now"
    if dist_pct < 1.0:
        return "🟠", f"stop only {dist_pct:.2f}% away"
    if dist_pct < 3.0:
        return "🟡", f"approaching stop ({dist_pct:.2f}% away)"
    return "⚪", f"stop {dist_pct:.2f}% away"


def stop_proximity_flag(
    direction: str, live_price: float, stop: float
) -> tuple[str, str, float]:
    """Public helper: direction-aware stop proximity for one position.
    Returns (emoji, human phrase, distance_pct). Used by paper_status and the
    price_levels voice action."""
    dist = _stop_distance_pct(direction, live_price, stop)
    emoji, phrase = _stop_flag(dist)
    return emoji, phrase, dist


def _level_stale_tag(ts: str | None) -> str:
    """Warn when the level predates the daily tracker run (>26h old)."""
    if not ts:
        return ""
    try:
        age_h = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        ).total_seconds() / 3600.0
    except Exception:
        return ""
    return f" [level stale: {age_h:.0f}h old]" if age_h > 26 else ""


def get_mistakes() -> list[MistakeRecord]:
    return _load_jsonl(_MISTAKES)


def record_mistake(mistake: MistakeRecord) -> None:
    _append_jsonl(_MISTAKES, mistake)


# ── Paper Performance ─────────────────────────────────────────────────────────


def get_performance() -> dict[str, Any]:
    return _load(
        _PERF,
        {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "max_drawdown": 0.0,
            "started": None,
            "last_updated": None,
        },
    )


def save_performance(pf: dict[str, Any]) -> None:
    pf["last_updated"] = _now_iso()
    _save(_PERF, pf)


# ── Learnings / Lessons ───────────────────────────────────────────────────────


def get_learnings() -> dict[str, Any]:
    return _load(_LEARNINGS, {"lessons": [], "updated": None})


def save_learnings(data: dict[str, Any]) -> None:
    data["updated"] = _now_iso()
    _save(_LEARNINGS, data)


def add_learning(lesson: str, category: str = "general") -> None:
    data = get_learnings()
    data.setdefault("lessons", [])
    data["lessons"].append(
        {
            "lesson": lesson,
            "category": category,
            "when": _now_iso(),
        }
    )
    # keep last 50 lessons
    data["lessons"] = data["lessons"][-50:]
    save_learnings(data)


# ── Position Management ───────────────────────────────────────────────────────


def open_position(
    symbol: str,
    method: str,
    direction: str,  # "LONG" or "SHORT"
    entry_price: float,
    stop_loss: float,
    targets: list[float],
    position_size: float,  # number of units/shares
    capital: float,
    atr: float,
    rules_votes: dict[str, int] | None = None,
) -> PaperPosition:
    """
    Open a paper position. Returns the position dict.
    """
    positions = get_positions()
    key = f"{symbol}:{method}"

    if key in positions:
        # close existing before reopening
        close_position(key)

    now = _now_iso()
    position: PaperPosition = {
        "symbol": symbol,
        "method": method,
        "direction": direction,
        "entry_price": round(entry_price, 6),
        "stop_loss": round(stop_loss, 6),
        "targets": [round(t, 6) for t in targets],
        "position_size": position_size,
        "capital_at_entry": capital,
        "atr": round(atr, 6),
        "open_time": now,
        "last_update": now,
        "status": "open",
        "hits": [False, False, False],  # which targets hit
        "max_favorable_excursion": 0.0,  # peak unrealized P&L
        "max_unfavorable_excursion": 0.0,  # worst unrealized drawdown
        "rules_votes": rules_votes or {},
        "closed_reason": None,
        "closed_time": None,
        "exit_price": None,
        "realized_pnl_pct": None,
        "realized_pnl": None,
        "slip_bps": 0,
    }

    positions[key] = position
    save_positions(positions)
    return position


def close_position(
    key: str | None = None,
    symbol: str | None = None,
    method: str | None = None,
    exit_price: float | None = None,
    reason: str = "manual",
    slip_bps: float = 0.0,
) -> PaperTrade | None:
    """
    Close a paper position and journal the trade.
    Returns the completed trade record.
    """
    positions = get_positions()

    if key is None and symbol and method:
        key = f"{symbol}:{method}"
    if key not in positions:
        return None

    pos = positions[key]
    direction = 1 if pos["direction"] == "LONG" else -1

    # Determine exit price
    if exit_price is None:
        exit_price = pos.get("last_price") or pos["entry_price"]

    entry = pos["entry_price"]
    risk = abs(entry - pos["stop_loss"]) or 1e-9
    realized_pnl_pct = (exit_price - entry) / entry * direction * 100
    realized_pnl = realized_pnl_pct / 100 * pos["capital_at_entry"]

    # Apply slippage
    slip_factor = slip_bps / 10000
    realized_pnl *= 1 - slip_factor

    now = _now_iso()
    trade: PaperTrade = {
        "symbol": pos["symbol"],
        "method": pos["method"],
        "direction": pos["direction"],
        "entry_price": entry,
        "stop_loss": pos["stop_loss"],
        "targets": pos["targets"],
        "position_size": pos["position_size"],
        "capital_at_entry": pos["capital_at_entry"],
        "open_time": pos["open_time"],
        "exit_time": now,
        "exit_price": round(exit_price, 6),
        "exit_reason": reason,
        "realized_pnl_pct": round(realized_pnl_pct, 4),
        "realized_pnl": round(realized_pnl, 2),
        "slip_bps": slip_bps,
        "targets_hit": sum(pos["hits"]),
        "max_favorable_excursion": round(pos["max_favorable_excursion"], 4),
        "max_unfavorable_excursion": round(pos["max_unfavorable_excursion"], 4),
        "holding_bars": pos.get("holding_bars", 0),
        "atr": pos["atr"],
        "rules_votes": pos.get("rules_votes", {}),
        "lessons": [],  # filled by mistake detection
    }

    # Detect mistakes
    lessons = detect_mistakes(pos, trade)
    trade["lessons"] = lessons
    for lesson in lessons:
        record_mistake(
            {
                "symbol": pos["symbol"],
                "method": pos["method"],
                "direction": pos["direction"],
                "entry_price": entry,
                "exit_price": exit_price,
                "realized_pnl_pct": round(realized_pnl_pct, 4),
                "mistake_type": lesson.get("type", "unknown"),
                "mistake_description": lesson.get("description", ""),
                "suggestion": lesson.get("suggestion", ""),
                "time": now,
            }
        )
        add_learning(
            lesson.get("suggestion", lesson.get("description", "")),
            category=lesson.get("type", "general"),
        )

    # Update performance
    pf = get_performance()
    if pf["started"] is None:
        pf["started"] = now
    pf["total_trades"] = pf.get("total_trades", 0) + 1
    pf["total_pnl"] = pf.get("total_pnl", 0.0) + realized_pnl
    if realized_pnl > 0:
        pf["wins"] = pf.get("wins", 0) + 1
    else:
        pf["losses"] = pf.get("losses", 0) + 1

    # Track max drawdown
    equity = pf.get("equity_curve", [])
    equity.append(realized_pnl)
    peak = 0.0
    dd = 0.0
    running = 0.0
    for pnl in equity:
        running += pnl
        peak = max(peak, running)
        dd = max(dd, peak - running)
    pf["max_drawdown"] = round(dd, 4)
    pf["equity_curve"] = equity

    save_performance(pf)

    # Remove from active positions
    del positions[key]
    save_positions(positions)

    return trade


def update_position(
    key: str,
    current_price: float,
    bar_count: int | None = None,
) -> PaperPosition | None:
    """
    Update an open position with the latest price.
    Tracks MFE, MAE, and drift from entry.
    """
    positions = get_positions()
    if key not in positions:
        return None

    pos = positions[key]
    direction = 1 if pos["direction"] == "LONG" else -1
    entry = pos["entry_price"]

    # Unrealized P&L %
    unrealized_pct = (current_price - entry) / entry * direction * 100

    # Track MFE / MAE
    mfe = max(pos.get("max_favorable_excursion", 0), unrealized_pct)
    mae = min(pos.get("max_unfavorable_excursion", 0), unrealized_pct)
    pos["max_favorable_excursion"] = round(mfe, 4)
    pos["max_unfavorable_excursion"] = round(mae, 4)

    if bar_count is not None:
        pos["holding_bars"] = bar_count

    pos["last_price"] = round(current_price, 6)
    pos["last_unrealized_pct"] = round(unrealized_pct, 4)
    pos["last_update"] = _now_iso()

    positions[key] = pos
    save_positions(positions)
    return pos


def check_stop_loss(key: str, current_high: float, current_low: float) -> bool:
    """Check if stop loss is hit. Returns True if stopped out."""
    positions = get_positions()
    if key not in positions:
        return False

    pos = positions[key]
    direction = 1 if pos["direction"] == "LONG" else -1
    stop = pos["stop_loss"]

    if direction == 1:
        hit = current_low <= stop
    else:
        hit = current_high >= stop

    if hit:
        close_position(
            key, exit_price=stop, reason="stop_loss", slip_bps=pos.get("slip_bps", 5)
        )
        return True
    return False


def check_targets(key: str, current_high: float, current_low: float) -> list[int]:
    """
    Check if any targets are hit. Returns list of target indices hit.
    Also moves stop to break-even after first target.
    """
    positions = get_positions()
    if key not in positions:
        return []

    pos = positions[key]
    direction = 1 if pos["direction"] == "LONG" else -1
    entry = pos["entry_price"]
    hits_new = []

    for i, target in enumerate(pos["targets"]):
        if pos["hits"][i]:
            continue
        if direction == 1:
            if current_high >= target:
                hits_new.append(i)
        else:
            if current_low <= target:
                hits_new.append(i)

    for i in hits_new:
        pos["hits"][i] = True
        positions[key] = pos

        # Move stop to break-even after first target
        if i == 0:
            pos["stop_loss"] = round(entry, 6)

        positions[key] = pos
        save_positions(positions)

        # Check if all targets hit -> close
    if all(pos["hits"]):
        last_target = pos["targets"][-1]
        close_position(key, exit_price=last_target, reason="all_targets_hit")

    return hits_new


def check_time_stop(key: str, max_bars: int) -> bool:
    """Time-based exit. Returns True if time stop triggered."""
    positions = get_positions()
    if key not in positions:
        return False

    pos = positions[key]
    bars_held = pos.get("holding_bars", 0)
    if bars_held >= max_bars:
        close_position(
            key,
            exit_price=pos.get("last_price") or pos["entry_price"],
            reason="time_stop",
        )
        return True
    return False


# ── Mistake Detection ─────────────────────────────────────────────────────────


def detect_mistakes(pos: PaperPosition, trade: PaperTrade) -> list[dict]:
    """
    Analyze a closed trade for common mistakes.
    Returns list of detected issues with descriptions and suggestions.
    """
    lessons = []
    pnl = trade["realized_pnl_pct"]
    direction = 1 if trade["direction"] == "LONG" else -1
    entry = trade["entry_price"]
    stop = trade["stop_loss"]
    mfe = trade.get("max_favorable_excursion", 0)
    mae = trade.get("max_unfavorable_excursion", 0)
    holding = trade.get("holding_bars", 0)

    # Mistake 1: Stopped out immediately (within a few bars)
    if pnl <= -0.5 and mae <= 0.3:
        lessons.append(
            {
                "type": "tight_stop",
                "description": f"Trade stopped out within 0.3% despite small MFE ({mfe:+.2f}%). "
                f"Stop may have been too tight for normal volatility.",
                "suggestion": f"Consider wider stops based on ATR. Current ATR was {trade.get('atr', 0):.2f}. "
                f"A stop of 1.5-2x ATR would give the trade more room.",
            }
        )

    # Mistake 2: Winner turned to loser (high MFE then gave back)
    if mfe > 2.0 and pnl < 0:
        lessons.append(
            {
                "type": "winner_to_loser",
                "description": f"Trade reached MFE of {mfe:+.2f}% but closed at {pnl:+.2f}%. "
                f"Good moves were given back.",
                "suggestion": "Use trailing stops to capture more of big moves. "
                "Consider partial takes at each target with trailing remainder.",
            }
        )

    # Mistake 3: Held too long (time stop candidate)
    if holding > 15 and pnl < 1.0:
        lessons.append(
            {
                "type": "held_too_long",
                "description": f"Trade held for {holding} bars with only {pnl:+.2f}% result. "
                f"Dead capital.",
                "suggestion": "Set explicit time stops. If price isn't moving in your favor "
                "within a few bars, the thesis may be wrong.",
            }
        )

    # Mistake 4: No targets hit, stopped out
    if trade["targets_hit"] == 0 and pnl < 0:
        lessons.append(
            {
                "type": "no_target_hit",
                "description": "Trade hit no targets and was stopped out. "
                "Entry timing or pattern recognition may need review.",
                "suggestion": "Review the entry setup — was the signal strength sufficient? "
                "Higher score thresholds reduce low-conviction trades.",
            }
        )

    # Mistake 5: Counter-trend trade without confirmation
    if trade.get("rules_votes", {}).get("ema_trend", 0) != direction:
        lessons.append(
            {
                "type": "counter_trend",
                "description": f"Trade went {trade['direction']} but EMA trend was opposite. "
                "Fighting the trend is lower-probability.",
                "suggestion": "Consider only taking signals that align with the EMA trend, "
                "or reduce position size when counter-trend.",
            }
        )

    # Mistake 6: Big loser (any single trade > 2% loss)
    if pnl < -2.0:
        lessons.append(
            {
                "type": "large_loss",
                "description": f"Trade lost {pnl:+.2f}%. Large single-trade loss.",
                "suggestion": "Review risk management. Consider capping position size so no single "
                "trade can lose more than 1-2% of capital. Review stop placement.",
            }
        )

    # Mistake 7: Won but small (could have been bigger with trailing)
    if 0.5 < pnl < 1.5 and mfe > pnl + 1.0:
        lessons.append(
            {
                "type": "left_money_on_table",
                "description": f"Trade won {pnl:+.2f}% but MFE was {mfe:+.2f}%. "
                f"Left {mfe - pnl:.2f}% on the table.",
                "suggestion": "Use trailing stops or scale out partially to capture more of big moves.",
            }
        )

    # Mistake 8: Chasing / late entry (large gap from signal to entry)
    if trade.get("rules_votes", {}):
        votes = trade["rules_votes"]
        score_signal = sum(abs(v) for v in votes.values())
        if score_signal < 3.0 and pnl < 0:
            lessons.append(
                {
                    "type": "low_conviction",
                    "description": f"Trade taken with low signal score ({score_signal:.1f} votes). "
                    "Low-conviction trades underperform.",
                    "suggestion": "Raise the minimum score threshold for entry. Only trade A+ setups.",
                }
            )

    return lessons


# ── Paper Trading Simulation ──────────────────────────────────────────────────


def run_paper_tick(
    symbol: str,
    method: str,
    current_price: float,
    current_high: float,
    current_low: float,
    bar_index: int,
    rules_votes: dict[str, int] | None = None,
) -> dict[str, Any]:
    """
    Process one bar of paper trading for a symbol+method.

    Checks:
    - Existing position: stop loss, targets, time stop
    - New signal: open position if signal strength thresholds met

    Returns dict with action taken and position state.
    """
    result = {
        "symbol": symbol,
        "method": method,
        "bar_index": bar_index,
        "price": round(current_price, 6),
        "action": "none",
        "detail": "",
        "position": None,
    }

    key = f"{symbol}:{method}"
    positions = get_positions()

    # ── Update existing position ──
    if key in positions:
        pos = positions[key]
        direction = 1 if pos["direction"] == "LONG" else -1
        entry = pos["entry_price"]
        prof = get_method_profile(method)

        update_position(
            key, current_price, bar_index - pos.get("bar_index_open", bar_index)
        )

        # Check stop loss
        if check_stop_loss(key, current_high, current_low):
            result["action"] = "stop_loss"
            result["detail"] = (
                f"Stop loss hit at {current_low if direction==1 else current_high:.2f}"
            )
            result["position"] = None
            return result

        # Check targets
        hit_targets = check_targets(key, current_high, current_low)
        if hit_targets:
            result["action"] = "target_hit"
            result["detail"] = f"Target(s) {hit_targets} hit"
            result["position"] = get_positions().get(key)

        # Check time stop
        if check_time_stop(key, prof["max_hold_bars"]):
            result["action"] = "time_stop"
            result["detail"] = f"Time stop after {prof['max_hold_bars']} bars"
            result["position"] = None
            return result

        result["position"] = positions.get(key)
        return result

    # ── Open new position if signal ──
    if rules_votes:
        score = sum(abs(v) for v in rules_votes.values())
        if score >= 3.0:  # minimum signal strength
            direction_str = (
                "LONG"
                if rules_votes.get("ema_trend", 0) > 0
                or any(v > 0 for v in rules_votes.values())
                else "SHORT"
            )

            # Determine direction from votes
            net_vote = sum(
                (1 if v > 0 else -1 if v < 0 else 0) * 1.0 for v in rules_votes.values()
            )
            direction_str = "LONG" if net_vote > 0 else "SHORT"

            atr = (
                pos.get("atr", current_price * 0.02)
                if key in positions
                else current_price * 0.02
            )
            prof = get_method_profile(method)

            entry = current_price
            stop = (
                entry - (1 if direction_str == "LONG" else -1) * prof["atr_stop"] * atr
            )
            targets = [
                entry + (1 if direction_str == "LONG" else -1) * rr * abs(entry - stop)
                for rr in prof["targets_r"]
            ]

            capital = get_performance().get("capital", 10000)
            risk_pct = 1.0
            risk_amount = capital * risk_pct / 100
            risk_per_unit = abs(entry - stop) or 1e-9
            position_size = max(1, int(risk_amount / risk_per_unit))

            open_position(
                symbol=symbol,
                method=method,
                direction=direction_str,
                entry_price=entry,
                stop_loss=stop,
                targets=targets,
                position_size=position_size,
                capital=capital,
                atr=atr,
                rules_votes=rules_votes,
            )

            result["action"] = "position_opened"
            result["detail"] = f"{direction_str} opened at {entry:.2f}, stop {stop:.2f}"
            result["position"] = get_positions().get(key)

    return result


# ── Method Profile Access ─────────────────────────────────────────────────────


def get_method_profile(method: str) -> dict:
    """Access method profiles from trading_strategy without importing it."""
    try:
        from plugins.trading_strategy import METHODS

        return METHODS.get(method, METHODS["swing"])
    except ImportError:
        return {
            "label": method,
            "interval": "1d",
            "atr_stop": 1.5,
            "targets_r": [1.0, 2.0, 3.0],
            "max_hold_bars": 15,
        }


# ── Paper Trading Commands ────────────────────────────────────────────────────


def paper_status(symbol: str | None = None, method: str | None = None) -> str:
    """
    Get paper trading status as a readable string.
    Includes TradingView chart links for open positions.
    """
    positions = get_positions()
    journal = get_journal()
    pf = get_performance()

    lines = ["=== PAPER TRADING STATUS ==="]

    # Active positions
    level_map, level_ts = _latest_level_map()
    if positions:
        lines.append("\n--- OPEN POSITIONS ---")
        for key, pos in positions.items():
            s, m = key.split(":")
            if symbol and s != symbol:
                continue
            if method and m != method:
                continue
            direction_arrow = "▲" if pos["direction"] == "LONG" else "▼"
            unrel = pos.get("last_unrealized_pct", 0)
            unrel_str = f" ({unrel:+.2f}%)" if unrel else ""
            lines.append(
                f"  {direction_arrow} {pos['symbol']} ({pos['method']}) "
                f"@ {pos['entry_price']:.2f} → "
                f"stop {pos['stop_loss']:.2f}, "
                f"targets: {', '.join(f'{t:.2f}' for t in pos['targets'])}"
                f"{unrel_str}"
            )
            lines.append(
                f"    Size: {pos['position_size']} units, "
                f"opened: {pos['open_time']}"
            )
            if pos.get("hits"):
                lines.append(f"    Targets hit: {sum(pos['hits'])}/3")
            # TradingView chart link
            tv_symbol = s.replace(".NS", "")
            if s.endswith(".NS"):
                tv_link = f"https://www.tradingview.com/chart/?symbol=NSE:{tv_symbol}&interval=D"
            elif s.endswith(".BO"):
                tv_link = f"https://www.tradingview.com/chart/?symbol=BSE:{tv_symbol}&interval=D"
            elif "-USD" in s:
                tv_link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{s.replace('-USD', 'USDT').upper()}&interval=D"
            else:
                tv_link = f"https://www.tradingview.com/chart/?symbol={s}&interval=D"
            lines.append(f"    TradingView: {tv_link}")
            # Stop-proximity vs the latest tracker price level, when available.
            live = level_map.get(_level_symbol(pos["symbol"]))
            if live is not None:
                emoji, phrase, _ = stop_proximity_flag(
                    pos.get("direction", "LONG"),
                    float(live),
                    float(pos.get("stop_loss", 0.0)),
                )
                lines.append(
                    f"    Stop check: {emoji} live {float(live):,.2f} — {phrase}{_level_stale_tag(level_ts)}"
                )
    else:
        lines.append("\nNo open paper positions.")

    # Performance summary
    lines.append("\n--- PERFORMANCE ---")
    lines.append(f"  Total trades: {pf.get('total_trades', 0)}")
    lines.append(f"  Wins: {pf.get('wins', 0)} | Losses: {pf.get('losses', 0)}")
    if pf.get("total_trades", 0) > 0:
        lines.append(f"  Win rate: {pf['wins']/pf['total_trades']*100:.1f}%")
    lines.append(f"  Total P&L: {pf.get('total_pnl', 0):+.2f}")
    lines.append(f"  Max drawdown: {pf.get('max_drawdown', 0):.2f}%")

    # Recent trades
    if journal:
        lines.append("\n--- RECENT TRADES ---")
        recent = sorted(journal, key=lambda x: x.get("exit_time", ""), reverse=True)[:5]
        for t in recent:
            if symbol and t["symbol"] != symbol:
                continue
            pnl_str = f"{t['realized_pnl_pct']:+.2f}%"
            pnl_money = (
                f" (${t['realized_pnl']:+,.2f})" if t.get("realized_pnl") else ""
            )
            lines.append(
                f"  {t['direction']} {t['symbol']} ({t['method']}) → "
                f"entry {t['entry_price']:.2f}, exit {t['exit_price']:.2f} = "
                f"{pnl_str}{pnl_money} [{t['exit_reason']}]"
            )

    # Learnings / mistakes summary
    mistakes = get_mistakes()
    if mistakes:
        lines.append("\n--- MISTAKES LEARNED FROM ---")
        mistake_types = {}
        for m in mistakes:
            t = m.get("mistake_type", "unknown")
            mistake_types[t] = mistake_types.get(t, 0) + 1
        for mt, count in sorted(mistake_types.items(), key=lambda x: -x[1]):
            lines.append(f"  {mt}: {count} occurrences")

    # Live price levels logged by the india-market-tracker (Browserbase cron).
    levels = get_price_levels()
    if levels:
        lines.append("\n--- LIVE PRICE LEVELS (india-market-tracker) ---")
        for rec in levels:
            ts = str(rec.get("ts", ""))[:16].replace("T", " ")
            parts = []
            for p in rec.get("prices", []):
                chg = p.get("day_change_pct") or 0.0
                parts.append(f"{p['symbol']} {p['price']:,} ({chg:+.2f}%)")
            lines.append(f"  {ts} UTC | " + " | ".join(parts))

    return "\n".join(lines)


def paper_reset() -> str:
    """Reset all paper trading state."""
    clear_positions()
    # Don't delete journal - keep history
    _save(
        _PERF,
        {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "max_drawdown": 0.0,
            "started": None,
            "last_updated": None,
        },
    )
    return "Paper trading reset. All positions closed, performance cleared."


def paper_learnings_summary() -> str:
    """Return accumulated learnings as readable text."""
    data = get_learnings()
    lessons = data.get("lessons", [])
    if not lessons:
        return "No trading lessons learned yet. Make some paper trades first."

    # Group by category
    by_cat: dict[str, list[str]] = {}
    for l in lessons:
        cat = l.get("category", "general")
        by_cat.setdefault(cat, []).append(l["lesson"])

    lines = ["=== TRADING LESSONS LEARNED ==="]
    for cat, cat_lessons in by_cat.items():
        lines.append(f"\n--- {cat.upper()} ---")
        for lesson in cat_lessons[-5:]:  # last 5 per category
            lines.append(f"  • {lesson}")

    lines.append(f"\nTotal lessons: {len(lessons)}")
    return "\n".join(lines)


# ── Paper Trading Settings ────────────────────────────────────────────────────


def is_paper_trading_enabled() -> bool:
    """Check if paper trading is enabled via plugin settings."""
    try:
        from memory.config_manager import get_plugin_config

        cfg = get_plugin_config("trading")
        val = cfg.get("paper_trading_enabled", True)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)
    except Exception:
        return True


def get_paper_capital() -> float:
    """Get paper trading capital from settings."""
    try:
        from memory.config_manager import get_plugin_config

        cfg = get_plugin_config("trading")
        val = cfg.get("paper_capital", 10000)
        try:
            return float(val)
        except (TypeError, ValueError):
            return 10000.0
    except Exception:
        return 10000.0


# ── Integration with trading_strategy signals ────────────────────────────────


def paper_trade_signal(
    symbol: str,
    method: str,
    signal_direction: str,
    score: float,
    votes: dict[str, int],
    current_price: float,
    atr: float,
    capital: float | None = None,
) -> str:
    """
    Main entry point: given a trading signal, manage paper position.
    Called after _signal() returns in the trading flow.

    Returns action description.
    """
    # Check if paper trading is enabled
    if not is_paper_trading_enabled():
        return "Paper trading is disabled in settings. Enable it to auto-trade."

    if capital is None:
        capital = get_paper_capital()

    key = f"{symbol}:{method}"
    positions = get_positions()

    prof = get_method_profile(method)

    # Update existing position with new price
    if key in positions:
        pos = positions[key]
        update_position(key, current_price)

        # Simulate stop/target checks
        direction = 1 if pos["direction"] == "LONG" else -1

        # Check stop (simplified - using last price vs stop)
        if (direction == 1 and current_price <= pos["stop_loss"]) or (
            direction == -1 and current_price >= pos["stop_loss"]
        ):
            trade = close_position(key, exit_price=pos["stop_loss"], reason="stop_loss")
            if trade:
                return f"Position stopped out at {pos['stop_loss']:.2f}. P&L: {trade['realized_pnl_pct']:+.2f}%"

        # Check targets
        for i, target in enumerate(pos["targets"]):
            if not pos["hits"][i]:
                if (direction == 1 and current_price >= target) or (
                    direction == -1 and current_price <= target
                ):
                    pos["hits"][i] = True
                    # Move stop to BE after first target
                    if i == 0:
                        pos["stop_loss"] = pos["entry_price"]
                    save_positions(positions)

                    if all(pos["hits"]):
                        trade = close_position(
                            key, exit_price=target, reason="all_targets_hit"
                        )
                        if trade:
                            return f"All targets hit. Exit @ {target:.2f}. P&L: {trade['realized_pnl_pct']:+.2f}%"

        unrel = (
            (current_price - pos["entry_price"]) / pos["entry_price"] * direction * 100
        )
        return f"Position still open. Unrealized: {unrel:+.2f}%. Stop: {pos['stop_loss']:.2f}"

    # New signal - open position
    if signal_direction != "FLAT" and score >= 2.0:
        direction_val = 1 if signal_direction == "LONG" else -1

        stop = current_price - direction_val * prof["atr_stop"] * atr
        targets = [
            current_price + direction_val * rr * abs(current_price - stop)
            for rr in prof["targets_r"]
        ]

        risk_amount = capital * 1.0 / 100  # 1% risk
        risk_per_unit = abs(current_price - stop) or 1e-9
        position_size = max(1, int(risk_amount / risk_per_unit))

        open_position(
            symbol=symbol,
            method=method,
            direction=signal_direction,
            entry_price=current_price,
            stop_loss=stop,
            targets=targets,
            position_size=position_size,
            capital=capital,
            atr=atr,
            rules_votes=votes,
        )

        return (
            f"Paper position opened: {signal_direction} {symbol} @ {current_price:.2f}, "
            f"stop {stop:.2f}, targets {', '.join(f'{t:.2f}' for t in targets)}, "
            f"size {position_size} units"
        )

    return "No action — signal below threshold or no clear direction."


# ── Backfill Paper Trades from Journal ────────────────────────────────────────


def backfill_from_journal() -> str:
    """
    Analyze the existing performance journal from trading_strategy
    and convert it to paper trading learnings.
    """
    try:
        from plugins.trading_strategy import _journal_load, _stats
    except ImportError:
        return "Cannot import trading_strategy journal."

    entries = _journal_load()
    if not entries:
        return "No journal entries to backfill."

    lessons = []
    for e in entries:
        if e.get("correct", True):
            continue  # only learn from mistakes

        symbol = e.get("symbol", "?")
        method = e.get("method", "?")
        ret = e.get("ret_pct", 0)
        votes = e.get("votes", {})

        # Detect mistake type
        if abs(ret) < 0.5:
            mistake = "Small move — lack of conviction or bad timing"
        elif ret < -1.0:
            mistake = f"Large loss of {ret:+.2f}% — review entry and stop placement"
        else:
            mistake = f"Lost {ret:+.2f}% — strategy needs adjustment for this pattern"

        lessons.append(
            {
                "symbol": symbol,
                "method": method,
                "outcome": f"{ret:+.2f}%",
                "mistake": mistake,
                "votes": votes,
                "time": e.get("bar_date", ""),
            }
        )

    # Save learnings
    data = get_learnings()
    data.setdefault("backfilled_mistakes", [])
    data["backfilled_mistakes"].extend(lessons[-100:])  # keep last 100
    save_learnings(data)

    loss_count = sum(1 for e in entries if not e.get("correct", True))
    return (
        f"Backfilled {len(lessons)} losing trades from journal. "
        f"Total journal: {len(entries)} signals, "
        f"{loss_count} losses to learn from."
    )


# ── Init ──────────────────────────────────────────────────────────────────────


def ensure_files() -> None:
    """Ensure all paper trading files exist."""
    _TRADING_DIR.mkdir(parents=True, exist_ok=True)
    for path in (_POSITIONS, _JOURNAL, _PERF, _MISTAKES, _LEARNINGS):
        if not path.exists():
            if path.suffix == ".jsonl":
                path.write_text("")
            else:
                _save(
                    path,
                    {}
                    if path != _PERF
                    else {
                        "total_trades": 0,
                        "wins": 0,
                        "losses": 0,
                        "total_pnl": 0.0,
                        "max_drawdown": 0.0,
                        "started": None,
                        "last_updated": None,
                    },
                )
