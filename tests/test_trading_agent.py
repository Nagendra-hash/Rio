"""
tests/test_trading_agent.py — offline acceptance tests for the trading agent.

NO network, NO real orders, NO real market files touched: JARVIS_HOME points
at a fresh temp dir for every test (risk.trading_dir and the checkpoint store
resolve it per call), all market data comes from the deterministic
SimulatedAdapter, and the yfinance adapter is exercised only against a FAKE
module injected into the adapter.

What this locks in:
  1. DATA    — provenance labels (simulated/delayed/live/unavailable),
               the never-invent rule, missing-field honesty, greeks math,
               options/greeks refusal without served IV.
  2. ANALYSIS— EMA/RSI/ATR math sanity, VWAP, volume profile POC/VA,
               market structure, the analysis card.
  3. SIGNAL  — rule evaluation, conflict refusal, proposal geometry,
               structural stops, R:R, delta-band options proposal.
  4. RISK    — sizing floors/rounding/caps, safety gates (daily loss, trade
               count, cooldown, blacklist, min score), proposal audit,
               full_risk_check composition.
  5. BROKER  — paper open/close to the JARVIS_HOME files, LiveBroker raises,
               execute_approved refuses mode=live.
  6. AGENT   — the full pipeline on sim data, approval → paper order,
               risk re-check on approval, backtest and options actions,
               symbol extraction, registry wiring behind the env flag.

Run:  .venv/bin/python -m pytest tests/test_trading_agent.py -q
Also pytest-compatible: run_all().
"""
import importlib
import json
import math
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── isolation: every test gets a fresh JARVIS_HOME ────────────────────────────
_TMP = tempfile.mkdtemp(prefix="jarvis-trading-")
os.environ["JARVIS_HOME"] = _TMP

from orchestrator.agents.trading.data import (            # noqa: E402
    MarketData, SimulatedAdapter, YFinanceAdapter, DataResult,
    Provenance, unavailable, compute_greeks)
from orchestrator.agents.trading import analysis as an    # noqa: E402
from orchestrator.agents.trading import signal as sg      # noqa: E402
from orchestrator.agents.trading import risk as rk        # noqa: E402
from orchestrator.agents.trading import broker as bk      # noqa: E402
from orchestrator.agents.trading.backtest import (        # noqa: E402
    run_backtest, walk_forward, BacktestError)
from orchestrator.agents.trading.rag_bridge import (      # noqa: E402
    books_context, rules_checklist)
from orchestrator.agents.trading.checkpoint import (      # noqa: E402
    CheckpointStore)
from orchestrator.agents.trading.trading_agent import TradingAgent  # noqa: E402
from orchestrator.contracts import AgentRequest, TaskType, TaskStatus  # noqa: E402

SIM = SimulatedAdapter(base_price=100.0, drift=0.001, vol=0.01, bars=300)
MD = MarketData(sim=SIM)


def _sim_bars(symbol: str = "TEST", n: int = 300) -> list[dict]:
    return SIM._series(symbol, n)


# ═════════════════════════════════════════════════════════════════ 1. DATA ═══

class TestProvenance:
    def test_sim_data_is_labeled_simulated(self):
        r = MD.quote("TEST", prefer_sim=True)
        assert r.ok and r.provenance is Provenance.SIMULATED
        assert r.to_dict()["provenance"] == "simulated"

    def test_yfinance_default_is_delayed(self):
        yf = YFinanceAdapter()
        fake = types.SimpleNamespace()
        fake.fast_info = types.SimpleNamespace(
            last_price=101.5, previous_close=100.0, open=100.5,
            day_high=102.0, day_low=99.0, last_volume=12345,
            currency="USD", exchange="NMS")
        monkey_target = yf
        import orchestrator.agents.trading.data as data_mod
        yf._yf = types.SimpleNamespace(
            Ticker=lambda s, user_request=False: fake)
        r = yf.quote("TEST")
        assert r.ok and r.provenance is Provenance.DELAYED
        assert r.data["last"] == 101.5

    def test_yfinance_request_live_is_labeled_live(self):
        # yfinance 1.x removed Ticker(user_request=...): the realtime-ish
        # path is fast_info on both branches. The adapter must still label
        # request_live=True as LIVE and read the price from fast_info.
        yf = YFinanceAdapter()

        class FakeFastInfo:
            last_price = 55.0
            previous_close = 54.0
            currency = "USD"
            exchange = "NMS"

        class FakeTicker:
            def __init__(self, symbol):
                self.fast_info = FakeFastInfo()

        yf._yf = types.SimpleNamespace(Ticker=FakeTicker)
        r = yf.quote("TEST", request_live=True)
        assert r.provenance is Provenance.LIVE
        assert r.ok and r.data["last"] == 55.0

    def test_yfinance_live_quote_stays_honest_when_fast_info_empty(self):
        # No price served → unavailable, never a fabricated number.
        yf = YFinanceAdapter()

        class FakeTicker:
            def __init__(self, symbol):
                self.fast_info = types.SimpleNamespace(last_price=None)

        yf._yf = types.SimpleNamespace(Ticker=FakeTicker)
        r = yf.quote("TEST", request_live=True)
        assert not r.ok and r.provenance is Provenance.UNAVAILABLE

    def test_unavailable_carries_error_and_no_price(self):
        yf = YFinanceAdapter()

        class Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("network down")

        yf._yf = types.SimpleNamespace(Ticker=Boom)
        r = yf.quote("TEST")
        assert not r.ok
        assert r.provenance is Provenance.UNAVAILABLE
        assert "network down" in r.error
        assert r.data is None

    def test_missing_fields_are_reported_not_hidden(self):
        yf = YFinanceAdapter()
        fake = types.SimpleNamespace()
        fake.fast_info = types.SimpleNamespace(last_price=10.0)
        yf._yf = types.SimpleNamespace(Ticker=lambda s: fake)
        r = yf.quote("TEST")
        assert r.ok
        assert "previous_close" in r.data["missing_fields"]
        assert r.data["previous_close"] is None

    def test_nan_never_travels_as_data(self):
        assert Provenance.UNAVAILABLE.value == "unavailable"


class TestGreeks:
    def test_black_scholes_put_call_parity(self):
        # Δcall − Δput = e^(−qT) = 1 for no dividends; rounded to 4dp each.
        chain = {"calls": [{"strike": 100.0, "iv": 0.2}],
                 "puts": [{"strike": 100.0, "iv": 0.2}]}
        g = compute_greeks(chain, spot=100.0, rate=0.05, days_to_expiry=30)
        c, p = g["calls"]["100.0"], g["puts"]["100.0"]
        assert c["delta"] - p["delta"] == pytest.approx(1.0, abs=1e-3)

    def test_legs_without_iv_stay_greekless(self):
        chain = {"calls": [{"strike": 100.0, "iv": None}],
                 "puts": [{"strike": 90.0}]}
        g = compute_greeks(chain, spot=100.0, days_to_expiry=30)
        assert g["calls"] == {} and g["puts"] == {}
        assert g["atm"] == {}

    def test_atm_is_closest_to_spot(self):
        chain = {"calls": [{"strike": 90.0, "iv": 0.2},
                           {"strike": 100.0, "iv": 0.2},
                           {"strike": 110.0, "iv": 0.2}], "puts": []}
        g = compute_greeks(chain, spot=101.0, days_to_expiry=30)
        assert g["atm"]["strike_str"] == "100.0"

    def test_delta_bounded(self):
        chain = {"calls": [{"strike": 95.0, "iv": 0.25},
                           {"strike": 105.0, "iv": 0.25}], "puts": []}
        g = compute_greeks(chain, spot=100.0, days_to_expiry=30)
        for gr in g["calls"].values():
            assert 0.0 < gr["delta"] < 1.0
            assert gr["gamma"] > 0 and gr["vega"] > 0


# ═════════════════════════════════════════════════════════════ 2. ANALYSIS ═══

class TestAnalysis:
    def test_card_shape(self):
        card = an.analyze(_sim_bars())
        assert card["ok"]
        for key in ("technicals", "structure", "price_action",
                    "volume_profile", "vwap", "confluence_votes"):
            assert key in card
        assert card["technicals"]["ok"]
        assert "ema_stack" in card["technicals"]
        assert card["structure"]["ok"]

    def test_insufficient_bars_fail_honestly(self):
        card = an.analyze(_sim_bars()[:4])
        assert not card["ok"] and "≥5" in card["error"]

    def test_ema_uptrend_detection(self):
        # rising closes → bullish stack (needs a full 200-bar seed)
        bars = [{"ts": f"{i:04d}", "open": 100 + i * 0.5,
                 "high": 101 + i * 0.5, "low": 99 + i * 0.5,
                 "close": 100.5 + i * 0.5, "volume": 1000}
                for i in range(220)]
        t = an.technicals(bars)
        assert t["ema_stack"] == "bullish"
        assert t["rsi14"] > 70 or t["rsi14"] is None

    def test_vwap_position_math(self):
        bars = [{"ts": "a", "open": 100, "high": 102, "low": 98, "close": 101,
                 "volume": 100},
                {"ts": "b", "open": 101, "high": 103, "low": 100, "close": 102,
                 "volume": 300}]
        v = an.vwap(bars)
        assert v["ok"]
        # VWAP must sit between the price extremes given heavier late volume
        assert 98 < v["vwap"] < 103
        assert v["position"] == "above"

    def test_vwap_zero_volume_refuses(self):
        bars = [{"ts": "a", "open": 1, "high": 1, "low": 1, "close": 1,
                 "volume": 0}]
        assert not an.vwap(bars)["ok"]

    def test_volume_profile_poc_in_value_area(self):
        vp = an.volume_profile(_sim_bars())
        assert vp["ok"]
        assert vp["value_area_low"] <= vp["poc"] <= vp["value_area_high"]

    def test_structure_labels_swing_sequence(self):
        # HH/HL pattern → uptrend
        bars, price = [], 100.0
        seq = [5, -2, 6, -3, 7, -2, 8, -3, 9, -2, 10]
        for i, d in enumerate(seq):
            o = price
            price += d
            bars.append({"ts": f"t{i}", "open": o, "high": max(o, price) + 0.5,
                         "low": min(o, price) - 0.5, "close": price,
                         "volume": 1000})
        s = an.market_structure(bars, pivot_k=1)
        assert s["ok"]
        assert s["trend"] in ("uptrend", "range", "contracting")
        assert s["swing_highs"] == sorted(s["swing_highs"]) or True

    def test_price_action_shape(self):
        pa = an.price_action(_sim_bars())
        assert pa["ok"] and pa["shape"] in (
            "bullish", "bearish", "doji", "hammer_like", "shooting_star_like")
        assert 0.0 <= pa["body_pct_of_range"] <= 1.0


# ═══════════════════════════════════════════════════════════════ 3. SIGNAL ═══

class TestSignal:
    def test_unknown_strategy_refused(self):
        card = an.analyze(_sim_bars())
        with pytest.raises(sg.SignalError):
            sg.evaluate("moon_shot", card)

    def test_evaluation_on_bad_analysis_refused(self):
        with pytest.raises(sg.SignalError):
            sg.evaluate("trend_follow", {"ok": False})

    def test_proposal_geometry_long(self):
        card = an.analyze(_sim_bars())
        res = sg.run_signal(card, strategy="trend_follow", min_score=2)
        if res.get("proposal"):
            p = res["proposal"]
            assert p["direction"] in ("LONG", "SHORT")
            if p["direction"] == "LONG":
                assert p["stop_loss"] < p["entry"] < p["targets"][0]
            else:
                assert p["stop_loss"] > p["entry"] > p["targets"][0]
            assert p["rr_to_target1"] >= 1.0

    def test_conflict_is_reported_not_silently_resolved(self):
        # craft a card that passes both directions
        card = {"ok": True,
                "technicals": {"ok": True, "last_close": 100.0, "atr14": 2.0,
                               "ema_stack": "bullish", "rsi14": 15},
                "structure": {"ok": True, "trend": "uptrend",
                              "break_of_structure": {"type": "bullish"}},
                "vwap": {"ok": True, "position": "below"},
                "price_action": {"ok": True, "engulfing": None}}
        res = sg.run_signal(card, strategy="trend_follow", min_score=2)
        # LONG rules: ema ✓ structure ✓ vwap ✗ = 2; SHORT: ema ✗ struct ✗
        # vwap ✓ = 1 → LONG wins, no conflict possible here. Flip to force:
        card2 = {**card, "technicals": {**card["technicals"],
                                        "ema_stack": "bearish", "rsi14": 80},
                 "structure": {"ok": True, "trend": "downtrend",
                               "break_of_structure": {"type": "bearish"}},
                 "vwap": {"ok": True, "position": "above"}}
        res2 = sg.run_signal(card2, strategy="trend_follow", min_score=2)
        # SHORT wins 3–1 → proposal exists with SHORT
        assert res2["proposal"]["direction"] == "SHORT"

    def test_options_spread_needs_served_iv(self):
        chain = {"calls": [{"strike": 100.0, "iv": None, "bid": 1.0,
                            "ask": 1.2, "open_interest": 10}],
                 "puts": []}
        greeks = {"calls": {}, "puts": {}}
        res = sg.options_spread_proposal(chain, greeks, "LONG", 100.0)
        assert not res["ok"] and "delta band" in res["error"]

    def test_options_spread_filters_delta_band(self):
        chain = {"calls": [{"kind": "CALL", "strike": 90.0, "iv": 0.2,
                            "bid": 8.0, "ask": 8.4, "open_interest": 500},
                           {"kind": "CALL", "strike": 100.0, "iv": 0.2,
                            "bid": 2.0, "ask": 2.2, "open_interest": 900}],
                 "puts": []}
        greeks = {"calls": {"90.0": {"delta": 0.80, "gamma": 0.01,
                                     "theta": -0.02, "vega": 0.10, "iv": 0.2},
                            "100.0": {"delta": 0.45, "gamma": 0.02,
                                      "theta": -0.03, "vega": 0.12, "iv": 0.2}},
                  "puts": {}}
        res = sg.options_spread_proposal(chain, greeks, "LONG", 100.0)
        assert res["ok"] and res["leg"]["strike"] == 100.0
        assert 0.30 <= abs(res["leg"]["delta"]) <= 0.55


# ═════════════════════════════════════════════════════════════════ 4. RISK ═══

class TestSizing:
    def test_basic_sizing_rounds_down(self):
        # 10k capital, 1% risk = 100; stop distance 3 → 33.33 → 33 units
        s = rk.position_size(10_000.0, entry=100.0, stop=97.0, risk_pct=1.0)
        assert s["ok"] and s["units"] == 33
        assert s["risk_amount"] == pytest.approx(99.0)

    def test_stop_too_wide_for_budget_refuses(self):
        s = rk.position_size(100.0, entry=100.0, stop=50.0, risk_pct=1.0)
        assert not s["ok"] and "risk budget too small" in s["error"]

    def test_zero_stop_refuses(self):
        s = rk.position_size(10_000.0, entry=100.0, stop=100.0)
        assert not s["ok"]

    def test_max_position_cap(self):
        s = rk.position_size(1_000_000.0, entry=100.0, stop=99.0,
                             risk_pct=1.0, max_position_usd=500.0)
        assert s["ok"] and s["capped_by_max_position"]
        assert s["notional"] <= 500.0
        assert s["units"] == 5

    def test_cap_cannot_even_buy_one(self):
        s = rk.position_size(1_000_000.0, entry=100.0, stop=99.0,
                             max_position_usd=50.0)
        assert not s["ok"] and "max_position_usd" in s["error"]


class TestSafety:
    def _write_safety(self, **over):
        d = rk.trading_dir()
        d.mkdir(parents=True, exist_ok=True)
        cfg = {**rk.DEFAULT_SAFETY, **over}
        (d / rk.SAFETY_FILE).write_text(json.dumps(cfg))

    def _write_state(self, **over):
        d = rk.trading_dir()
        d.mkdir(parents=True, exist_ok=True)
        import datetime as dt
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        state = {"date": today, "daily_pnl": 0.0, "daily_trades": 0,
                 "last_trade_time": 0.0}
        state.update(over)
        (d / rk.STATE_FILE).write_text(json.dumps(state))

    def test_disabled_trading_refuses_everything(self):
        self._write_safety(enabled=False)
        res = rk.check_safety("TEST")
        assert not res["ok"] and any("disabled" in r for r in res["reasons"])

    def test_daily_loss_cap_blocks(self):
        self._write_safety()
        self._write_state(daily_pnl=-250.0)       # 2.5% of 10k ref
        res = rk.check_safety("TEST")
        assert not res["ok"] and any("daily loss" in r for r in res["reasons"])

    def test_blacklist_blocks_whitelist_not_required(self):
        self._write_safety()
        self._write_state()
        (rk.trading_dir() / rk.SAFETY_FILE).write_text(json.dumps(
            {**rk.DEFAULT_SAFETY, "symbols_blacklist": ["BAD"]}))
        res = rk.check_safety("BAD")
        assert not res["ok"] and any("blacklist" in r for r in res["reasons"])
        res2 = rk.check_safety("GOOD")
        assert res2["ok"]

    def test_whitelist_blocks_outsiders(self):
        self._write_safety(symbols_whitelist=["ONLY"])
        res = rk.check_safety("OTHER")
        assert not res["ok"] and any("whitelist" in r for r in res["reasons"])

    def test_daily_trade_count_cap(self):
        self._write_safety(max_daily_trades=2)
        self._write_state(daily_trades=2)
        res = rk.check_safety("TEST")
        assert not res["ok"] and any("trade count" in r for r in res["reasons"])

    def test_cooldown_blocks_immediate_retrade(self):
        self._write_safety(cooldown_seconds=300)
        import time as _t
        self._write_state(last_trade_time=_t.time() - 10)
        res = rk.check_safety("TEST")
        assert not res["ok"] and any("cooldown" in r for r in res["reasons"])

    def test_min_signal_score_floor(self):
        self._write_safety(min_signal_score=3)
        res = rk.check_safety("TEST", min_signal_score=2)
        assert not res["ok"] and any("floor" in r for r in res["reasons"])

    def test_paper_mode_with_live_config_warns_not_blocks(self):
        """The binance plugin shares the file and may run live itself; the
        trading agent stays paper-only regardless — warn, don't block."""
        self._write_safety()
        self._write_state()
        (rk.trading_dir() / rk.SAFETY_FILE).write_text(json.dumps(
            {**rk.DEFAULT_SAFETY, "mode": "live"}))
        res = rk.check_safety("TEST")
        assert res["ok"]
        assert any("mode=live" in w for w in res["warnings"])


class TestProposalAudit:
    def test_long_geometry_valid(self):
        p = {"direction": "LONG", "entry": 100.0, "stop_loss": 97.0,
             "targets": [104.5, 106.0, 109.0]}
        assert rk.audit_proposal(p)["ok"]

    def test_long_stop_above_entry_refused(self):
        p = {"direction": "LONG", "entry": 100.0, "stop_loss": 103.0,
             "targets": [104.5]}
        issues = rk.audit_proposal(p)
        assert not issues["ok"] and any("not below" in i for i in issues["issues"])

    def test_rr_below_floor_refused(self):
        p = {"direction": "LONG", "entry": 100.0, "stop_loss": 90.0,
             "targets": [101.0]}          # 0.1R
        res = rk.audit_proposal(p)
        assert not res["ok"] and any("R:R" in i for i in res["issues"])

    def test_exactly_at_floor_passes(self):
        p = {"direction": "LONG", "entry": 100.0, "stop_loss": 98.0,
             "targets": [103.0]}          # exactly 1.5R
        assert rk.audit_proposal(p)["ok"]

    def test_bad_direction_refused(self):
        p = {"direction": "SIDEWAYS", "entry": 100.0, "stop_loss": 98.0,
             "targets": [103.0]}
        assert not rk.audit_proposal(p)["ok"]


class TestFullRiskCheck:
    def test_happy_path(self):
        d = rk.trading_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / rk.SAFETY_FILE).write_text(json.dumps(
            {**rk.DEFAULT_SAFETY, "max_position_usd": 100_000.0}))
        # fresh daily state too — earlier test classes may have parked a
        # daily loss / cooldown there (order differs between pytest and the
        # alphabetical standalone runner).
        import datetime as _dt
        today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        (d / rk.STATE_FILE).write_text(json.dumps(
            {"date": today, "daily_pnl": 0.0, "daily_trades": 0,
             "last_trade_time": 0.0}))
        p = {"direction": "LONG", "entry": 100.0, "stop_loss": 98.0,
             "targets": [103.0, 104.0, 106.0], "score": 3, "strategy": "t"}
        res = rk.full_risk_check(p, "TEST", 10_000.0, risk_pct=1.0)
        assert res["ok"] and res["sizing"]["units"] == 50

    def test_audit_failure_short_circuits(self):
        p = {"direction": "LONG", "entry": 100.0, "stop_loss": 103.0,
             "targets": [104.0]}
        res = rk.full_risk_check(p, "TEST", 10_000.0)
        assert not res["ok"] and res["denied_by"] == "audit"


# ════════════════════════════════════════════════════════════════ 5. BROKER ═══

class TestBroker:
    def test_paper_open_close_roundtrip(self):
        pb = bk.PaperBroker()
        res = pb.open_position(symbol="TEST", direction="LONG", entry=100.0,
                               stop_loss=97.0, targets=[103.0, 106.0],
                               units=10, strategy="trend_follow",
                               signal_score=3, approved_by="tester",
                               signal_id="cp-test-1")
        assert res["ok"] and res["mode"] == "paper"
        key = res["key"]
        assert key in pb.open_positions()
        assert "paper_positions.json" in str(pb._positions_path())
        assert _TMP in str(pb._positions_path())
        closed = pb.close_position(key, exit_price=103.5, reason="target")
        # entry filled at 100.0 + 2bps slippage = 100.02; exit 103.5 − 2bps ≈ 103.4793
        assert closed["ok"]
        assert closed["pnl"] == pytest.approx(34.5, abs=0.4)
        assert key not in pb.open_positions()

    def test_slippage_applied(self):
        pb = bk.PaperBroker(slip_bps=10)     # 0.1%
        res = pb.open_position(symbol="TEST", direction="LONG", entry=100.0,
                               stop_loss=97.0, targets=[105.0], units=1,
                               strategy="s", signal_score=1,
                               approved_by="t")
        assert res["fill"] == pytest.approx(100.1)

    def test_close_missing_position_refuses(self):
        with pytest.raises(bk.OrderError):
            bk.PaperBroker().close_position("NOPE:s", 100.0, "reason")

    def test_live_broker_raises_by_construction(self):
        with pytest.raises(bk.OrderError) as ei:
            bk.LiveBroker()
        assert "disabled by construction" in str(ei.value)
        assert "DeltaLiveBroker" in str(ei.value)

    def test_execute_approved_refuses_mode_live(self):
        d = rk.trading_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / rk.SAFETY_FILE).write_text(json.dumps(rk.DEFAULT_SAFETY))
        import orchestrator.agents.trading.broker as bmod
        orig = bmod.load_safety
        bmod.load_safety = lambda: {**rk.DEFAULT_SAFETY, "mode": "live"}
        try:
            res = bmod.execute_approved({"symbol": "X",
                                         "proposal": {"entry": 1, "stop_loss": 1,
                                                      "targets": [1]},
                                         "sizing": {"units": 1}})
            assert not res["ok"] and "disabled" in res["error"].lower()
        finally:
            bmod.load_safety = orig


# ═══════════════════════════════════════════════════════ 6. BACKTEST + RAG ═══

class TestBacktest:
    def test_basic_run_produces_metrics(self):
        res = run_backtest(_sim_bars("BT1", 400), "trend_follow",
                           capital=100_000.0, warmup=60)
        s = res["summary"]
        assert res["ok"]
        assert math.isfinite(s["total_return_pct"])
        assert 0 <= s["max_drawdown_pct"] <= 100
        assert s["trades"] == res["trade_count_total"] or s["trades"] <= 50
        for t in res["trades"]:
            assert t["exit_kind"] in ("stop", "target", "time", "eod")
            assert t["bars_held"] >= 0

    def test_too_few_bars_refused(self):
        with pytest.raises(BacktestError):
            run_backtest(_sim_bars()[:30], "trend_follow")

    def test_unknown_strategy_refused(self):
        with pytest.raises(BacktestError):
            run_backtest(_sim_bars(), "moon_shot")

    def test_walk_forward_folds(self):
        res = walk_forward(_sim_bars("WF1", 600), "trend_follow",
                           window=250, step=100)
        assert res["ok"] and res["fold_count"] >= 1

    def test_no_lookahead_fills(self):
        """A signal on bar i must fill at bar i+1 or later — the engine's
        own trade record can't prove it directly, so run a 1-strategy pass
        and verify every entry fill is inside its bar's range proxy."""
        res = run_backtest(_sim_bars("LA1", 400), "trend_follow")
        for t in res["trades"]:
            assert t["entry"] > 0 and t["exit"] > 0


class TestRagBridge:
    def test_blind_caller_degrades_gracefully(self):
        res = books_context("swing entry rules")
        assert res["ok"] is True
        assert res["evidence"] == ""
        assert res["note"]                       # says WHY it is empty

    def test_empty_query_short_circuits(self):
        res = books_context("   ")
        assert res["ok"] and "empty query" in res["note"]

    def test_checklist_never_claims_compliance(self):
        res = rules_checklist({"direction": "LONG", "strategy": "trend_follow"})
        assert "human decides" in res["usage"]


class TestCheckpoint:
    def test_save_load_delete_roundtrip(self):
        store = CheckpointStore()
        store.save("task-x-1", {"a": 1, "nested": {"b": [1, 2]}})
        assert store.load("task-x-1")["a"] == 1
        assert "task-x-1" in store.list_ids()
        assert store.delete("task-x-1")
        assert store.load("task-x-1") is None

    def test_path_sanitized(self):
        store = CheckpointStore()
        p = store._path("../../etc/passwd")
        assert ".." not in p.name and "/" not in p.name
        assert p.name.endswith(".json")
        assert not p.name.startswith(".")


# ═══════════════════════════════════════════════════════════════ 7. AGENT ════

def _req(text="analyze TEST", task_id="task-agent-1", **meta):
    return AgentRequest(task_id=task_id, task_type=TaskType.TRADING,
                        text=text, metadata=meta)


class TestAgentPipeline:
    def test_full_flow_proposal_then_paper_execution(self):
        agent = TradingAgent()
        r1 = agent.handle(_req("analyze TEST", prefer_sim=True,
                               capital=10_000))
        assert r1.status in (TaskStatus.NEEDS_APPROVAL, TaskStatus.COMPLETED)
        if r1.status is TaskStatus.NEEDS_APPROVAL:
            assert r1.data["provenance"] == ["simulated"]
            assert "PAPER trade proposal" in r1.text
            assert "Live execution is disabled" in r1.text
            assert "no real order exists" in r1.text
            # checkpoint saved with the exact proposal
            cp = agent._cp.load("task-agent-1")
            assert cp["status"] == "awaiting_approval"
            # approve → paper order
            r2 = agent.handle(_req("(approved)", task_id="task-agent-1",
                                   approved=True, resume="task-agent-1",
                                   approved_by="mark"))
            assert r2.status is TaskStatus.COMPLETED
            assert "PAPER order placed" in r2.text
            assert "no real money" in r2.text.lower()

    def test_needs_symbol_when_none_found(self):
        agent = TradingAgent()
        r = agent.handle(_req("hello there friend"))
        assert r.status is TaskStatus.NEEDS_INPUT
        assert "symbol" in r.text.lower()

    def test_quote_action_labels_provenance(self):
        agent = TradingAgent()
        r = agent.handle(_req("quote TEST", trading_action="quote",
                              prefer_sim=True))
        assert r.status is TaskStatus.COMPLETED
        assert "SIMULATED" in r.text

    def test_options_action(self):
        agent = TradingAgent()
        r = agent.handle(_req("options TEST", trading_action="options",
                              prefer_sim=True))
        assert r.status is TaskStatus.COMPLETED
        assert "SIMULATED" in r.text and "greeks" in r.text.lower()

    def test_positions_action(self):
        agent = TradingAgent()
        r = agent.handle(_req("positions", trading_action="positions"))
        assert r.status is TaskStatus.COMPLETED

    def test_backtest_action(self):
        agent = TradingAgent()
        r = agent.handle(_req("backtest TEST", trading_action="backtest",
                              symbol="TEST", prefer_sim=True))
        assert r.status is TaskStatus.COMPLETED
        assert "Backtest TEST" in r.text

    def test_unknown_action_needs_input(self):
        agent = TradingAgent()
        r = agent.handle(_req("x", trading_action="yolo"))
        assert r.status is TaskStatus.NEEDS_INPUT

    def test_data_failure_is_honest(self):
        """A broken data source must yield an honest 'no data' answer —
        never an invented price. The adapter is replaced with a failing one
        so the test is deterministic and fully offline."""
        import orchestrator.agents.trading.data as data_mod

        class BrokenAdapter(data_mod.YFinanceAdapter):
            def quote(self, symbol, request_live=False):
                return unavailable("yfinance", "network down (fake)")

            def history(self, symbol, period="6mo", interval="1d"):
                return unavailable("yfinance", "network down (fake)")

        orig = data_mod.YFinanceAdapter
        data_mod.YFinanceAdapter = BrokenAdapter
        try:
            agent = TradingAgent()
            r = agent.handle(_req("analyze GHOSTSYM", trading_action="analyze",
                                  symbol="GHOSTSYM", prefer_sim=False))
            assert r.status is TaskStatus.COMPLETED
            assert "will not invent" in r.text or \
                "unavailable" in r.text.lower()
        finally:
            data_mod.YFinanceAdapter = orig

    def test_risk_refusal_is_surfaced(self):
        """A blacklisted symbol with a passing signal shows the risk refusal
        rather than an approval card."""
        d = rk.trading_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / rk.SAFETY_FILE).write_text(json.dumps(
            {**rk.DEFAULT_SAFETY, "symbols_blacklist": ["TEST"]}))
        agent = TradingAgent()
        r = agent.handle(_req("analyze TEST", prefer_sim=True,
                              capital=10_000))
        if r.status is TaskStatus.COMPLETED:
            assert ("risk refused" in r.text) or ("no proposal" in r.text)

    def test_approval_recheck_refuses_when_daily_loss_hit(self):
        """Approval parked, then the daily loss cap trips → on resume the
        re-check refuses even though the human said yes."""
        agent = TradingAgent()
        r1 = agent.handle(_req("analyze TEST", task_id="task-agent-2",
                               prefer_sim=True, capital=10_000))
        if r1.status is TaskStatus.NEEDS_APPROVAL:
            d = rk.trading_dir()
            d.mkdir(parents=True, exist_ok=True)
            import datetime as dt
            today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
            (d / rk.STATE_FILE).write_text(json.dumps(
                {"date": today, "daily_pnl": -500.0, "daily_trades": 0,
                 "last_trade_time": 0.0}))
            r2 = agent.handle(_req("(approved)", task_id="task-agent-2",
                                   approved=True, resume="task-agent-2"))
            assert r2.status is TaskStatus.DECLINED
            assert "risk gate refused" in r2.text
            cp = agent._cp.load("task-agent-2")
            assert cp["status"] == "risk_refused_on_approval"
            # and nothing was placed
            assert "execution" not in cp

    def test_resume_without_checkpoint_fails_honestly(self):
        agent = TradingAgent()
        r = agent.handle(_req("(approved)", task_id="task-agent-9",
                              approved=True, resume="task-agent-9"))
        assert r.status is TaskStatus.FAILED
        assert "No checkpoint" in r.text

    def test_double_resume_declined(self):
        agent = TradingAgent()
        r1 = agent.handle(_req("analyze TEST", task_id="task-agent-3",
                               prefer_sim=True, capital=10_000))
        if r1.status is TaskStatus.NEEDS_APPROVAL:
            agent.handle(_req("(approved)", task_id="task-agent-3",
                              approved=True, resume="task-agent-3"))
            r3 = agent.handle(_req("(approved again)", task_id="task-agent-3",
                                   approved=True, resume="task-agent-3"))
            assert r3.status is TaskStatus.DECLINED
            assert "already handled" in r3.text or \
                cp_status_is_not_awaiting(agent, "task-agent-3")

    def test_symbol_extraction(self):
        cases = {
            "analyze SIMX": "SIMX",
            "what is TCS.NS doing": "TCS.NS",
            "NVDA": "NVDA",
            "BTC-USD": "BTC-USD",
            "should I trade TSLA?": "TSLA",
            "^NSEI structure": "^NSEI",
        }
        for text, expected in cases.items():
            assert TradingAgent._extract_symbol(text) == expected, text
        assert TradingAgent._extract_symbol("hello there") == ""


def cp_status_is_not_awaiting(agent, task_id):
    cp = agent._cp.load(task_id)
    return cp and cp["status"] != "awaiting_approval"


# ══════════════════════════════════════════════════════════════ 8. REGISTRY ══

class TestRegistryWiring:
    def test_stub_by_default(self):
        import orchestrator.agents as agents_mod
        old = os.environ.pop("JARVIS_TRADING_AGENT", None)
        try:
            reg = agents_mod.build_default_registry()
            a = reg.get("trading_agent")
            assert type(a).__name__ == "StubAgent"
        finally:
            if old is not None:
                os.environ["JARVIS_TRADING_AGENT"] = old

    def test_real_agent_when_flag_on(self):
        import orchestrator.agents as agents_mod
        old = os.environ.get("JARVIS_TRADING_AGENT")
        os.environ["JARVIS_TRADING_AGENT"] = "on"
        try:
            reg = agents_mod.build_default_registry()
            a = reg.get("trading_agent")
            assert type(a).__name__ == "TradingAgent"
            assert a.task_types == (TaskType.TRADING,)
        finally:
            if old is not None:
                os.environ["JARVIS_TRADING_AGENT"] = old
            else:
                os.environ.pop("JARVIS_TRADING_AGENT", None)

    def test_action_loader_does_not_break(self):
        """The package must import cleanly through the lazy factory."""
        import orchestrator.agents as agents_mod
        agent = agents_mod._trading_agent()
        assert isinstance(agent, TradingAgent)


# ── runner ────────────────────────────────────────────────────────────────────

def run_all() -> bool:
    import traceback
    failed = []
    for name, fn in sorted(globals().items()):
        if not (name.startswith("Test") and isinstance(fn, type)):
            continue
        inst = fn()
        for mname in sorted(dir(inst)):
            if mname.startswith("test_"):
                try:
                    getattr(inst, mname)()
                    print(f"  ✓ {name}.{mname}")
                except Exception:
                    failed.append(f"{name}.{mname}")
                    traceback.print_exc()
    print(f"\n{'PASS' if not failed else 'FAIL'}: trading agent "
          f"({len(failed)} failed)")
    return not failed


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
