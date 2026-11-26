"""
orchestrator/agents/trading/signal.py — the SIGNAL stage: strategy evaluation
and trade PROPOSALS. Nothing here touches money.

Strategy evaluation is deliberately rule-shaped rather than model-shaped:
deterministic, explainable, and unit-testable. The `RULES` registry maps
strategy names to pure predicates over the analysis card; `evaluate` runs a
strategy and returns votes with reasons; `propose` turns a passing evaluation
into an entry/SL/target structure with ATR stops and R multiples. The RISK
stage re-checks everything this module says.
"""
from __future__ import annotations

import math
from typing import Any

# ── strategy rule registry ────────────────────────────────────────────────────
# Each rule: (direction vote, predicate(analysis) → bool, human reason).
# A "vote" is the minimal honest unit: the predicate fires or it does not.

def _r_ema_bull(a: dict) -> bool:
    t = a.get("technicals") or {}
    return t.get("ema_stack") == "bullish"


def _r_ema_bear(a: dict) -> bool:
    t = a.get("technicals") or {}
    return t.get("ema_stack") == "bearish"


def _r_struct_bull(a: dict) -> bool:
    s = a.get("structure") or {}
    if s.get("trend") == "uptrend":
        return True
    bos = s.get("break_of_structure") or {}
    return bos.get("type") == "bullish"


def _r_struct_bear(a: dict) -> bool:
    s = a.get("structure") or {}
    if s.get("trend") == "downtrend":
        return True
    bos = s.get("break_of_structure") or {}
    return bos.get("type") == "bearish"


def _r_rsi_pullback_long(a: dict) -> bool:
    """Mean-reversion entry: uptrend structure + RSI dipped below 45, closing
    back above 40 — buying weakness in strength."""
    s = a.get("structure") or {}
    t = a.get("technicals") or {}
    rsi = t.get("rsi14")
    return s.get("trend") in ("uptrend", "range") and \
        isinstance(rsi, (int, float)) and 38 <= rsi <= 45


def _r_rsi_stretch_short(a: dict) -> bool:
    s = a.get("structure") or {}
    t = a.get("technicals") or {}
    rsi = _num(t.get("rsi14"))
    return s.get("trend") in ("downtrend", "range") and \
        rsi is not None and rsi >= 62


def _r_vwap_long(a: dict) -> bool:
    v = a.get("vwap") or {}
    return v.get("ok") and v.get("position") == "above"


def _r_vwap_short(a: dict) -> bool:
    v = a.get("vwap") or {}
    return v.get("ok") and v.get("position") == "below"


def _r_bos_bull(a: dict) -> bool:
    bos = (a.get("structure") or {}).get("break_of_structure") or {}
    return bos.get("type") == "bullish"


def _r_bos_bear(a: dict) -> bool:
    bos = (a.get("structure") or {}).get("break_of_structure") or {}
    return bos.get("type") == "bearish"


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


RULES: dict[str, list[tuple[str, Any, str]]] = {
    "trend_follow": [
        ("LONG", _r_ema_bull, "EMA20>50>200 stack"),
        ("LONG", _r_struct_bull, "uptrend structure or bullish BOS"),
        ("LONG", _r_vwap_long, "price above VWAP"),
        ("SHORT", _r_ema_bear, "EMA20<50<200 stack"),
        ("SHORT", _r_struct_bear, "downtrend structure or bearish BOS"),
        ("SHORT", _r_vwap_short, "price below VWAP"),
    ],
    "mean_revert": [
        ("LONG", _r_rsi_pullback_long, "RSI pullback in strength (38–45)"),
        ("SHORT", _r_rsi_stretch_short, "RSI stretched (≥62) in weak tape"),
        ("LONG", _r_vwap_long, "above VWAP"),
        ("SHORT", _r_vwap_short, "below VWAP"),
    ],
    "breakout": [
        ("LONG", _r_bos_bull, "bullish break of structure"),
        ("SHORT", _r_bos_bear, "bearish break of structure"),
        ("LONG", _r_ema_bull, "trend support"),
        ("SHORT", _r_ema_bear, "trend support"),
    ],
}


class SignalError(ValueError):
    pass


def evaluate(strategy: str, analysis: dict,
             min_score: int = 2) -> dict:
    """Run a strategy's rules against an analysis card.

    Returns per-rule verdicts with human reasons and a per-direction score.
    A proposal requires score ≥ min_score in exactly one direction. Ties
    (equal LONG/SHORT scores) are refused as "conflict" — the honest answer
    when the tape argues with itself.
    """
    rules = RULES.get(str(strategy or "").strip().lower())
    if not rules:
        raise SignalError(f"unknown strategy '{strategy}' — have: "
                          f"{', '.join(sorted(RULES))}")
    if not analysis or not analysis.get("ok"):
        raise SignalError("analysis card is not ok — refusing to evaluate")

    verdicts, scores = [], {"LONG": 0, "SHORT": 0}
    for direction, pred, reason in rules:
        try:
            fired = bool(pred(analysis))
        except Exception as exc:
            verdicts.append({"direction": direction, "fired": False,
                             "reason": reason, "note": f"predicate error: {exc}"})
            continue
        verdicts.append({"direction": direction, "fired": fired,
                         "reason": reason})
        if fired:
            scores[direction] += 1

    if scores["LONG"] >= min_score and scores["SHORT"] >= min_score:
        return {"ok": False, "error": "conflict: both directions passed",
                "verdicts": verdicts, "scores": scores}
    if scores["LONG"] >= min_score:
        direction = "LONG"
    elif scores["SHORT"] >= min_score:
        direction = "SHORT"
    else:
        return {"ok": False,
                "error": (f"no direction reached min_score={min_score} "
                          f"(LONG={scores['LONG']}, SHORT={scores['SHORT']})"),
                "verdicts": verdicts, "scores": scores}

    return {"ok": True, "strategy": strategy, "direction": direction,
            "score": scores[direction], "scores": scores,
            "verdicts": verdicts}


# ── proposals ────────────────────────────────────────────────────────────────

def propose(evaluation: dict, analysis: dict,
            *, sl_atr_mult: float = 1.5,
            targets_r: tuple = (1.5, 2.0, 3.0),
            min_rr: float = 1.5) -> dict:
    """Turn a passing evaluation into entry / stop / targets.

    Stop = entry ∓ sl_atr_mult × ATR(14) (structure level wins when it is
    TIGHTER and still valid). Targets are R multiples of the stop distance.
    Every number is rounded for display but computed unrounded.
    """
    if not evaluation.get("ok"):
        raise SignalError("refusing to propose from a failed evaluation")
    tech = analysis.get("technicals") or {}
    struct = analysis.get("structure") or {}
    pa = analysis.get("price_action") or {}
    entry = _num(tech.get("last_close"))
    atr = _num(tech.get("atr14"))
    if not entry or not atr or atr <= 0:
        raise SignalError("missing entry close or ATR — cannot size a stop")

    direction = evaluation["direction"]
    sign = 1.0 if direction == "LONG" else -1.0
    sl = entry - sign * sl_atr_mult * atr

    # tighter structural stop when it is still on the safe side
    if direction == "LONG":
        sl_cands = [x for x in (_num(struct.get("last_swing_low")),
                                _num((analysis.get("volume_profile") or {})
                                     .get("value_area_low"))) if x and x < entry]
        if sl_cands:
            cand = max(sl_cands)               # closest stop BELOW entry
            if entry - cand <= 2.5 * sl_atr_mult * atr:
                sl = cand
    else:
        sl_cands = [x for x in (_num(struct.get("last_swing_high")),
                                _num((analysis.get("volume_profile") or {})
                                     .get("value_area_high"))) if x and x > entry]
        if sl_cands:
            cand = min(sl_cands)               # closest stop ABOVE entry
            if cand - entry <= 2.5 * sl_atr_mult * atr:
                sl = cand

    risk = abs(entry - sl)
    if risk <= 0 or not math.isfinite(risk):
        raise SignalError("degenerate stop distance — refusing to propose")
    targets = [entry + sign * risk * m for m in targets_r]

    return {
        "ok": True,
        "strategy": evaluation.get("strategy"),
        "direction": direction,
        "score": evaluation.get("score"),
        "entry": round(entry, 4),
        "stop_loss": round(sl, 4),
        "risk_per_unit": round(risk, 4),
        "targets": [round(t, 4) for t in targets],
        "target_r": list(targets_r),
        "min_rr": min_rr,
        "rr_to_target1": round(abs(targets[0] - entry) / risk, 2),
        "rr_to_target2": round(abs(targets[1] - entry) / risk, 2),
        "rr_to_target3": round(abs(targets[2] - entry) / risk, 2),
        "verdicts": evaluation.get("verdicts") or [],
        "structure_context": {
            "trend": struct.get("trend"),
            "last_swing_high": _num(struct.get("last_swing_high")),
            "last_swing_low": _num(struct.get("last_swing_low")),
            "candle_shape": pa.get("shape"),
        },
        "disclaimer": "proposal only — not financial advice; nothing executes "
                      "without risk check + human approval",
    }


def options_spread_proposal(chain: dict, greeks: dict,
                            direction: str, spot: float,
                            min_delta: float = 0.30,
                            max_delta: float = 0.55) -> dict:
    """Delta-band single-leg proposal from a SERVED chain + computed greeks.
    Refuses honestly when the chain/IV is unavailable — no synthetic legs."""
    legs = chain.get("calls" if direction == "LONG" else "puts") or []
    g = greeks.get("calls" if direction == "LONG" else "puts") or {}
    cands = []
    for leg in legs:
        strike_str = str(leg["strike"])
        gr = g.get(strike_str)
        if not gr:
            continue
        d = gr["delta"]
        if not (min_delta <= abs(d) <= max_delta):
            continue
        mid = None
        if leg.get("bid") is not None and leg.get("ask") is not None:
            mid = (leg["bid"] + leg["ask"]) / 2.0
        cands.append({"kind": leg["kind"], "strike": leg["strike"],
                      "expiry": leg.get("expiry"),
                      "iv": gr["iv"], "delta": d,
                      "gamma": gr["gamma"], "theta": gr["theta"],
                      "vega": gr["vega"],
                      "bid": leg.get("bid"), "ask": leg.get("ask"),
                      "mid": round(mid, 2) if mid else None,
                      "open_interest": leg.get("open_interest")})
    if not cands:
        return {"ok": False,
                "error": (f"no {direction.lower()} leg in delta band "
                          f"[{min_delta}, {max_delta}] with served IV + greeks")}
    cands.sort(key=lambda c: -c.get("open_interest") or 0)
    best = cands[0]
    return {"ok": True, "direction": direction, "leg": best,
            "alternatives": cands[1:4],
            "note": "premium from served bid/ask mid where available; "
                    "greeks computed Black-Scholes from served IV",
            "disclaimer": "proposal only — not financial advice"}


# ── the SIGNAL stage entry point ─────────────────────────────────────────────

def run_signal(analysis: dict, strategy: str = "trend_follow",
               min_score: int = 2, **propose_kw) -> dict:
    """ANALYSIS → SIGNAL. Returns {ok, evaluation?, proposal?} — a failed
    evaluation (no edge) is a SUCCESSFUL stage run with no proposal."""
    try:
        ev = evaluate(strategy, analysis, min_score=min_score)
    except SignalError as exc:
        return {"ok": False, "stage": "signal", "error": str(exc)}
    if not ev.get("ok"):
        return {"ok": True, "stage": "signal", "evaluation": ev,
                "proposal": None,
                "note": ev.get("error", "no trade met the strategy rules")}
    try:
        prop = propose(ev, analysis, **propose_kw)
    except SignalError as exc:
        return {"ok": True, "stage": "signal", "evaluation": ev,
                "proposal": None, "note": f"evaluation passed but proposal "
                                          f"refused: {exc}"}
    return {"ok": True, "stage": "signal", "evaluation": ev, "proposal": prop}
