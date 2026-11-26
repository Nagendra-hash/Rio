"""
orchestrator/agents/trading/risk.py — the RISK CHECK stage.

Nothing passes to execution without surviving this module. Three layers:

    1. POSITION SIZING  — fixed-fractional risk: risk_amount / stop distance,
       rounded DOWN to whole units, capped by capital and max_position_usd.
    2. SAFETY LIMITS    — daily loss %, max trades/day, cooldown, whitelist/
       blacklist, min signal score. Read from memory/trading/trading_safety.json
       (JARVIS_HOME-relocated, same file the binance_trader plugin uses).
    3. PROPOSAL AUDIT   — R:R recheck, stop sanity, direction vocabulary. A
       proposal whose numbers disagree with itself is DENIED, not fixed.

The safety file lives beside the plugin's state; enable=false means every
trade request is refused regardless of anything else.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

# ── safety config (file-backed, JARVIS_HOME-aware) ───────────────────────────

def trading_dir() -> Path:
    import os
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        base = Path(home).expanduser().resolve()
    else:
        from memory.config_manager import BASE_DIR
        base = BASE_DIR
    return base / "memory" / "trading"


SAFETY_FILE = "trading_safety.json"
STATE_FILE = "trading_state.json"
LIVE_FILE = "live_execution.json"

# The live switch is deliberately separate from trading_safety.json. That
# file is shared with the Binance plugin, so flipping its mode must never arm
# the Delta broker (or vice versa). This file is owned by the trading agent.
DEFAULT_LIVE: dict = {
    "enabled": False,              # explicit opt-in only
    "environment": "demo",         # demo = Delta India testnet
    "max_order_usd": 100.0,        # hard cap per live order, independent of paper cap
    "allow_auto": False,           # auto-trade may not place live orders
    "enabled_at": "",
    "enabled_by": "",
    "ack": "",
    "last_probe_at": "",
    "last_probe_ok": False,
    "last_probe_detail": "",
    "clock_offset_s": 0,           # learned from expired_signature server_time
}

DEFAULT_SAFETY: dict = {
    "enabled": True,
    "mode": "paper",                 # paper | live — "live" is refused by broker.py
    "max_daily_loss_pct": 2.0,
    "max_position_usd": 1000.0,
    "max_daily_trades": 10,
    "cooldown_seconds": 60,
    "require_confirmation": True,
    "symbols_whitelist": [],
    "symbols_blacklist": [],
    "min_signal_score": 2,
}


def load_safety() -> dict:
    d = trading_dir()
    try:
        data = json.loads((d / SAFETY_FILE).read_text(encoding="utf-8"))
    except Exception:
        data = {}
    out = dict(DEFAULT_SAFETY)
    if isinstance(data, dict):
        for k, v in data.items():
            if k in out:
                out[k] = v
    out.setdefault("mode", "paper")
    return out


def load_state(now: float | None = None) -> dict:
    """Daily counters, rolled over by date. File is the source of truth;
    a missing/corrupt file reads as a fresh day."""
    d = trading_dir()
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        data = json.loads((d / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict) or data.get("date") != today:
        data = {"date": today, "daily_pnl": 0.0, "daily_trades": 0,
                "last_trade_time": 0.0}
    return data


def save_state(state: dict) -> None:
    d = trading_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / STATE_FILE).write_text(json.dumps(state, indent=2),
                                encoding="utf-8")


# ── live execution switch (trading agent only) ───────────────────────────────

def load_live() -> dict:
    """Read the trading agent's live switch. Missing/corrupt → disabled."""
    d = trading_dir()
    try:
        data = json.loads((d / LIVE_FILE).read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return load_live_from(data)


def save_live(patch: dict) -> dict:
    """Merge known live-switch fields and write atomically. Never logs keys."""
    d = trading_dir()
    d.mkdir(parents=True, exist_ok=True)
    out = load_live()
    for k, v in (patch or {}).items():
        if k in DEFAULT_LIVE:
            out[k] = v
    out = load_live_from(out)
    tmp = d / (LIVE_FILE + ".tmp")
    tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
    tmp.replace(d / LIVE_FILE)
    return out


def load_live_from(data: dict) -> dict:
    """Normalise a live-switch dict without touching disk (test/API helper)."""
    out = dict(DEFAULT_LIVE)
    if isinstance(data, dict):
        for k, v in data.items():
            if k in out:
                out[k] = v
    out["enabled"] = bool(out.get("enabled"))
    out["allow_auto"] = bool(out.get("allow_auto"))
    env = str(out.get("environment") or "demo").strip().lower()
    out["environment"] = env if env in ("demo", "prod") else "demo"
    try:
        out["max_order_usd"] = max(0.0, float(out.get("max_order_usd") or 0.0))
    except (TypeError, ValueError):
        out["max_order_usd"] = DEFAULT_LIVE["max_order_usd"]
    try:
        out["clock_offset_s"] = int(float(out.get("clock_offset_s") or 0))
    except (TypeError, ValueError):
        out["clock_offset_s"] = 0
    return out


def live_halted() -> bool:
    """The real filesystem kill switch (shared with vibe-trading)."""
    import os
    return (Path(os.path.expanduser("~")) / ".vibe-trading" / "live"
            / "HALT").exists()


def record_live_order(symbol: str, *, pnl: float = 0.0) -> dict:
    """Count a filled live order against the daily limits."""
    state = load_state()
    state["daily_trades"] = int(state.get("daily_trades", 0) or 0) + 1
    state["last_trade_time"] = time.time()
    try:
        state["daily_pnl"] = round(float(state.get("daily_pnl", 0.0)) +
                                   float(pnl or 0.0), 6)
    except (TypeError, ValueError):
        state["daily_pnl"] = float(pnl or 0.0)
    save_state(state)
    return state


# ── layer 1: position sizing ─────────────────────────────────────────────────

def position_size(capital: float, entry: float, stop: float,
                  risk_pct: float = 1.0, max_position_usd: float = 0.0
                  ) -> dict:
    """Fixed-fractional sizing: risk `risk_pct`% of capital between entry and
    stop. Units round DOWN (never oversize by rounding). Zero/absurd stop
    distance refuses rather than inventing a size."""
    capital = float(capital)
    entry, stop = float(entry), float(stop)
    dist = abs(entry - stop)
    if not math.isfinite(dist) or dist <= 0:
        return {"ok": False, "error": f"invalid stop distance ({dist})"}
    if capital <= 0:
        return {"ok": False, "error": f"invalid capital ({capital})"}
    if entry <= 0:
        return {"ok": False, "error": f"invalid entry ({entry})"}

    risk_amount = capital * (float(risk_pct) / 100.0)
    units = int(risk_amount / dist)          # floor — never round up
    if units < 1:
        return {"ok": False,
                "error": (f"risk budget too small: {risk_amount:.2f} over a "
                          f"{dist:.2f}/unit stop cannot buy 1 unit "
                          f"(need stop closer or more capital)")}

    notional = units * entry
    cap = float(max_position_usd or 0.0)
    capped = False
    if cap > 0 and notional > cap:
        units = int(cap // entry)
        if units < 1:
            return {"ok": False,
                    "error": (f"max_position_usd={cap:.0f} cannot buy 1 unit "
                              f"at {entry:.2f}")}
        notional = units * entry
        capped = True

    actual_risk = units * dist
    return {"ok": True, "units": units, "notional": round(notional, 2),
            "risk_amount": round(actual_risk, 2),
            "risk_pct_of_capital": round(actual_risk / capital * 100.0, 3),
            "capped_by_max_position": capped,
            "stop_distance": round(dist, 4)}


def position_size_contracts(capital: float, entry: float, stop: float,
                            contract_value: float,
                            risk_pct: float = 1.0,
                            max_position_usd: float = 0.0) -> dict:
    """Contract-aware fixed-fractional sizing for Delta derivatives.

    One contract represents ``contract_value`` units of the underlying, so the
    risk per contract is ``|entry-stop| * contract_value`` and the notional is
    ``entry * contract_value``. The returned ``units`` is the integer number
    of contracts the venue expects; ``underlying_units`` is informational.
    """
    capital = float(capital)
    entry, stop = float(entry), float(stop)
    cv = float(contract_value)
    dist = abs(entry - stop)
    if not math.isfinite(dist) or dist <= 0:
        return {"ok": False, "error": f"invalid stop distance ({dist})"}
    if capital <= 0:
        return {"ok": False, "error": f"invalid capital ({capital})"}
    if entry <= 0:
        return {"ok": False, "error": f"invalid entry ({entry})"}
    if not math.isfinite(cv) or cv <= 0:
        return {"ok": False,
                "error": f"invalid contract_value ({contract_value})"}

    risk_amount = capital * (float(risk_pct) / 100.0)
    risk_per_contract = dist * cv
    if risk_per_contract <= 0:
        return {"ok": False, "error": "contract risk is zero"}
    contracts = int(risk_amount / risk_per_contract)   # floor, never round up
    if contracts < 1:
        return {"ok": False,
                "error": (f"risk budget too small: {risk_amount:.2f} over a "
                          f"{dist:.2f} stop at contract_value={cv} cannot buy "
                          f"1 contract")}

    notional_per_contract = entry * cv
    cap = float(max_position_usd or 0.0)
    capped = False
    if cap > 0 and contracts * notional_per_contract > cap:
        contracts = int(cap // notional_per_contract)
        if contracts < 1:
            return {"ok": False,
                    "error": (f"max_position_usd={cap:.0f} cannot buy 1 "
                              f"contract at {entry:.2f} × {cv}")}
        capped = True

    notional = contracts * notional_per_contract
    actual_risk = contracts * risk_per_contract
    return {"ok": True, "units": contracts, "contracts": contracts,
            "contract_value": cv,
            "underlying_units": round(contracts * cv, 10),
            "notional": round(notional, 2),
            "risk_amount": round(actual_risk, 2),
            "risk_pct_of_capital": round(actual_risk / capital * 100.0, 3),
            "capped_by_max_position": capped,
            "stop_distance": round(dist, 4)}


# ── layer 2: safety limits ───────────────────────────────────────────────────

def check_safety(symbol: str, *, min_signal_score: int | None = None,
                 now: float | None = None) -> dict:
    """Gate a trade request against the daily limits. Returns
    {ok, reasons[], warnings[]} — ok=False means the risk stage refuses."""
    cfg = load_safety()
    reasons: list[str] = []
    warnings: list[str] = []
    now = now if now is not None else time.time()

    if not cfg.get("enabled", True):
        reasons.append("trading disabled in trading_safety.json (enabled=false)")
    mode = str(cfg.get("mode", "paper")).lower()
    live = load_live()
    live_on = bool(live.get("enabled"))
    if live_on:
        warnings.append(
            "LIVE execution switch is ENABLED — an approved order can reach "
            f"Delta {str(live.get('environment') or 'demo').upper()} and use "
            "real money (or demo funds).")
    elif mode == "live":
        # The Binance plugin shares trading_safety.json and may run live
        # itself. The trading agent needs the separate live_execution.json
        # switch, so mode=live alone is still paper here.
        warnings.append("safety config says mode=live but the trading agent's "
                        "live execution switch is disabled — PAPER only")

    sym = str(symbol or "").strip().upper()
    wl = [s.upper() for s in (cfg.get("symbols_whitelist") or [])]
    bl = [s.upper() for s in (cfg.get("symbols_blacklist") or [])]
    if bl and sym in bl:
        reasons.append(f"{sym} is blacklisted")
    if wl and sym not in wl:
        reasons.append(f"{sym} not in whitelist {wl[:6]}")

    state = load_state()
    try:
        daily_pnl = float(state.get("daily_pnl", 0.0))
    except (TypeError, ValueError):
        daily_pnl = 0.0
    daily_trades = int(state.get("daily_trades", 0) or 0)

    # daily loss uses a notional reference capital when the file has none —
    # stated in the warning, never silently assumed as real money
    ref_capital = float(cfg.get("capital_usd") or 0.0)
    if ref_capital <= 0:
        ref_capital = 10_000.0
        warnings.append(f"no capital_usd in safety config — daily-loss % "
                        f"checked against a reference 10,000")
    max_loss = ref_capital * float(cfg.get("max_daily_loss_pct", 2.0)) / 100.0
    if daily_pnl < 0 and abs(daily_pnl) >= max_loss:
        reasons.append(f"daily loss {daily_pnl:.2f} hit the "
                       f"{cfg.get('max_daily_loss_pct')}% cap ({max_loss:.2f}) "
                       f"— no new trades today")

    if daily_trades >= int(cfg.get("max_daily_trades", 10)):
        reasons.append(f"daily trade count {daily_trades} ≥ "
                       f"{cfg.get('max_daily_trades')} cap")

    cooldown = int(cfg.get("cooldown_seconds", 60))
    last_t = float(state.get("last_trade_time", 0.0) or 0.0)
    if cooldown > 0 and last_t > 0 and now - last_t < cooldown:
        wait = int(cooldown - (now - last_t))
        reasons.append(f"cooldown active — {wait}s until next trade allowed")

    if min_signal_score is not None:
        floor = int(cfg.get("min_signal_score", 2))
        if min_signal_score < floor:
            reasons.append(f"signal score {min_signal_score} below the "
                           f"configured floor {floor}")

    return {"ok": not reasons, "reasons": reasons, "warnings": warnings,
            "mode": mode, "config": {k: cfg[k] for k in
                                     ("enabled", "mode", "max_daily_loss_pct",
                                      "max_position_usd", "max_daily_trades",
                                      "cooldown_seconds", "min_signal_score")
                                     if k in cfg}}


# ── layer 3: proposal audit ──────────────────────────────────────────────────

def audit_proposal(proposal: dict, *, min_rr: float = 1.5) -> dict:
    """Structural audit of an entry/SL/target proposal. Catches proposals
    whose geometry is wrong BEFORE sizing: stops on the wrong side, targets
    not beyond entry, R:R below floor."""
    issues: list[str] = []
    direction = str(proposal.get("direction", "")).upper()
    entry = _f(proposal.get("entry"))
    sl = _f(proposal.get("stop_loss"))
    targets = [t for t in (_f(x) for x in proposal.get("targets") or [])
               if t is not None]
    if direction not in ("LONG", "SHORT"):
        issues.append(f"direction '{direction}' is not LONG/SHORT")
    if entry is None or entry <= 0:
        issues.append("entry missing or non-positive")
    if sl is None or sl <= 0:
        issues.append("stop_loss missing or non-positive")
    if entry and sl and direction == "LONG" and sl >= entry:
        issues.append("LONG stop is not below entry")
    if entry and sl and direction == "SHORT" and sl <= entry:
        issues.append("SHORT stop is not above entry")
    if entry and targets and direction == "LONG" and \
            any(t <= entry for t in targets):
        issues.append("LONG target at/below entry")
    if entry and targets and direction == "SHORT" and \
            any(t >= entry for t in targets):
        issues.append("SHORT target at/above entry")
    if not targets:
        issues.append("no targets")
    if entry and sl and targets:
        risk = abs(entry - sl)
        rr1 = abs(targets[0] - entry) / risk if risk > 0 else 0.0
        # Compare at 3-decimal resolution: proposal values are rounded to 4dp
        # for display, so a true 1.5R can arrive as 1.499995 and must not
        # fail an exact 1.5 floor. Structural misgeometry (1.2R, 0.9R) still
        # fails loudly.
        if round(rr1, 3) < min_rr - 1e-9:
            issues.append(f"first-target R:R {rr1:.2f} below floor {min_rr}")
    return {"ok": not issues, "issues": issues}


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def full_risk_check(proposal: dict, symbol: str, capital: float,
                    *, risk_pct: float = 1.0, min_rr: float = 1.5,
                    min_signal_score: int | None = None,
                    now: float | None = None,
                    contract_value: float | None = None) -> dict:
    """The RISK stage in one call: audit → size → safety. Any layer failing
    fails the whole check; warnings ride along without blocking.

    ``contract_value`` selects Delta contract sizing (one order unit is an
    integer contract, not one underlying unit). It does not relax any limit:
    the per-order cap is the tighter of max_position_usd and the live switch's
    max_order_usd whenever live execution is armed.
    """
    audit = audit_proposal(proposal, min_rr=min_rr)
    if not audit["ok"]:
        return {"ok": False, "stage": "risk", "denied_by": "audit",
                "issues": audit["issues"]}
    safety_cfg = load_safety()
    live_cfg = load_live()
    cap = float(safety_cfg.get("max_position_usd") or 0)
    live_cap = 0.0
    if live_cfg.get("enabled"):
        live_cap = float(live_cfg.get("max_order_usd") or 0)
        if live_cap > 0:
            cap = live_cap if cap <= 0 else min(cap, live_cap)
    if contract_value:
        size = position_size_contracts(
            capital, float(proposal["entry"]), float(proposal["stop_loss"]),
            contract_value=float(contract_value), risk_pct=risk_pct,
            max_position_usd=cap)
    else:
        size = position_size(capital, float(proposal["entry"]),
                             float(proposal["stop_loss"]),
                             risk_pct=risk_pct, max_position_usd=cap)
    if not size.get("ok"):
        return {"ok": False, "stage": "risk", "denied_by": "sizing",
                "issues": [size.get("error", "sizing failed")]}
    safety = check_safety(symbol, min_signal_score=min_signal_score, now=now)
    if not safety["ok"]:
        return {"ok": False, "stage": "risk", "denied_by": "safety",
                "issues": safety["reasons"], "warnings": safety["warnings"]}
    return {"ok": True, "stage": "risk",
            "sizing": size, "warnings": safety["warnings"],
            "mode": ("live" if live_cfg.get("enabled") else safety["mode"]),
            "live_enabled": bool(live_cfg.get("enabled")),
            "live_environment": live_cfg.get("environment") if live_cfg.get("enabled") else None,
            "max_order_usd": (live_cap or None)}
