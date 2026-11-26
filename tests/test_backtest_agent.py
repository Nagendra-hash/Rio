"""
tests/test_backtest_agent.py — offline acceptance tests for the bounded
crypto/gold backtest research lab (Phase 22).

Test labelling
--------------
Every test whose name starts with ``test_mocked_`` uses ONLY local fake
providers / synthetic candles: no network, no exchange, no Hermes delivery.
The single ``test_real_provider_binance_daily`` test is a REAL-provider check
and is skipped unless ``JARVIS_BACKTEST_LIVE=1`` is exported, so a normal test
run never depends on the internet. (The measured live validation batch lives
in BACKTEST_VALIDATION_AND_RISK_REPORT.md.)

What this locks in
------------------
  1. SYMBOLS     — one symbol in, one explicit instrument out; gold asks once
                   and the answer is remembered; no silent spot/perp or
                   XAUUSD/COMEX/MCX substitution.
  2. DATA        — UTC normalisation, duplicate/garbage handling, no filling,
                   honest aggregation (upsample only), the one-year gate and
                   session-market gap reporting.
  3. ENGINE      — next-bar-open fills (no look-ahead), stop-first pessimism,
                   no target on the entry bar, gap stops at the open, fees and
                   funding charged, leverage cap, long-only spot.
  4. STRATEGIES  — exactly the reviewed registry, deterministic bounded grids,
                   market/timeframe suitability, causal signals.
  5. VALIDATION  — chronological split with a bounded floor, train-only
                   tuning, single untouched OOS run, walk-forward inside
                   train+validation, NO ROBUST STRATEGY FOUND is a valid result.
  6. JOBS        — QUEUED/RUNNING/COMPLETED/FAILED/CANCELLED, dedup, bounded
                   retries, cancellation, restart recovery.
  7. PROVIDERS   — pagination, cache key isolation, incremental cache, honest
                   BLOCKED responses when credentials/contracts are absent.
  8. SURFACES    — classifier lane, agent replies, notifications (dry-run),
                   reports, champion ledger + rollback.
  9. SAFETY      — no order endpoint, no eval/exec, research-only proof.

Run:  .venv/bin/python -m pytest tests/test_backtest_agent.py -q
"""
import itertools
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── isolation: this module gets a fresh JARVIS_HOME ──────────────────────────
_TMP = tempfile.mkdtemp(prefix="jarvis-backtest-")
os.environ["JARVIS_HOME"] = _TMP

from orchestrator.agents import BACKTEST_AGENT, build_default_registry  # noqa: E402
from orchestrator.agents.backtest import (  # noqa: E402
    aggregate, jobs, quality, reports, research, symbols,
)
from orchestrator.agents.backtest import scheduler as bt_scheduler  # noqa: E402
from orchestrator.agents.backtest.backtest_agent import BacktestAgent  # noqa: E402
from orchestrator.agents.backtest.cache import CandleCache  # noqa: E402
from orchestrator.agents.backtest.champions import ChampionStore  # noqa: E402
from orchestrator.agents.backtest.config import Config  # noqa: E402
from orchestrator.agents.backtest.engine import (  # noqa: E402
    Costs, bars_per_year_for, buy_and_hold, run_backtest,
)
from orchestrator.agents.backtest.notify import (  # noqa: E402
    Notifier, format_result, hermes_binary, redact,
)
from orchestrator.agents.backtest.providers import (  # noqa: E402
    build_providers, provider_health, providers_for,
)
from orchestrator.agents.backtest.providers.base import (  # noqa: E402
    FetchResult, Provider, drop_incomplete, normalise_frame,
)
from orchestrator.agents.backtest.providers.binance import (  # noqa: E402
    BinanceSpot, SPOT_BASE,
)
from orchestrator.agents.backtest.providers.dhan import DhanMcxGold  # noqa: E402
from orchestrator.agents.backtest.providers.kraken import (  # noqa: E402
    KrakenProvider, REST_MAX_CANDLES,
)
from orchestrator.agents.backtest.providers.twelvedata import (  # noqa: E402
    TwelveDataXauUsd,
)
from orchestrator.agents.backtest.providers.yahoo import (  # noqa: E402
    YahooGoldProvider,
)
from orchestrator.agents.backtest.strategies import library  # noqa: E402
from orchestrator.agents.backtest.validation import (  # noqa: E402
    evaluate, make_split, research_strategy,
)
from orchestrator.classifier import IntentClassifier  # noqa: E402
from orchestrator.contracts import AgentRequest, TaskStatus, TaskType  # noqa: E402

_SEQ = itertools.count(1)


# ── helpers ──────────────────────────────────────────────────────────────────

def _cfg(**updates) -> Config:
    """A fresh config file per test (never the live memory/backtest/config)."""
    cfg = Config(Path(_TMP) / "configs" / f"config-{next(_SEQ)}.json")
    if updates:
        cfg.save(updates)
    return cfg


def _store(cfg: Config | None = None) -> jobs.JobStore:
    return jobs.JobStore(Path(_TMP) / "dbs" /
                         f"jobs-{next(_SEQ)}.db", cfg=cfg or _cfg())


def _request(text: str, task: TaskType = TaskType.BACKTEST) -> AgentRequest:
    return AgentRequest(task_id=f"bt-test-{next(_SEQ)}", task_type=task,
                        text=text)


def _ohlc(index, opens, spread: float = 1.0) -> pd.DataFrame:
    opens = np.asarray(opens, dtype="float64")
    return pd.DataFrame({
        "open": opens,
        "high": opens + spread,
        "low": opens - spread,
        "close": opens + spread / 2,
        "volume": np.full(len(opens), 10.0),
    }, index=index)


def _signals(df: pd.DataFrame, *, stop: float = 1.0, target_r: float = 0.0,
             warmup: int = 0, long_entry=(), long_exit=(),
             short_entry=(), short_exit=()) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    for col in ("long_entry", "short_entry", "long_exit", "short_exit"):
        out[col] = False
    for i in long_entry:
        out.iloc[i, out.columns.get_loc("long_entry")] = True
    for i in long_exit:
        out.iloc[i, out.columns.get_loc("long_exit")] = True
    for i in short_entry:
        out.iloc[i, out.columns.get_loc("short_entry")] = True
    for i in short_exit:
        out.iloc[i, out.columns.get_loc("short_exit")] = True
    out["stop_distance"] = float(stop)
    out["target_r"] = float(target_r)
    out["warmup"] = int(warmup)
    return out


def _daily_frame(periods: int = 600, *, start: str = "2022-01-01",
                 drift: float = 0.0008, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=periods, freq="1D", tz="UTC")
    rets = drift + rng.normal(0.0, 0.018, periods)
    close = 100.0 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.006
    low = np.minimum(open_, close) * 0.994
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": 1000.0}, index=idx)


class _FakeProvider(Provider):
    """Synthetic candles that obey the requested window. No network."""

    name = "fake_provider"
    venue = "Fake Venue"
    native_timeframes = ("1d", "1h")
    supported_markets = ("crypto_spot", "crypto_perp")

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def fetch(self, instrument, timeframe, start, end, *,
              max_candles=None) -> FetchResult:
        start = pd.Timestamp(start).tz_convert("UTC")
        end = pd.Timestamp(end).tz_convert("UTC")
        idx = pd.date_range(start=start, end=end, freq="1D", tz="UTC")
        if len(idx) < 1:
            return FetchResult(ok=False, provider=self.name,
                               timeframe=timeframe, error="empty window")
        self.calls.append((instrument.market_class, timeframe,
                           idx[0], idx[-1]))
        n = len(idx)
        rng = np.random.default_rng(11)
        close = 100.0 * np.exp(np.cumsum(0.0005 + rng.normal(0, 0.01, n)))
        open_ = np.concatenate([[close[0]], close[:-1]])
        frame = pd.DataFrame({
            "open": open_, "high": np.maximum(open_, close) * 1.005,
            "low": np.minimum(open_, close) * 0.995, "close": close,
            "volume": 100.0}, index=idx)
        return FetchResult(ok=True, provider=self.name, timeframe=timeframe,
                           frame=frame, requests_made=1)

    def funding_history(self, venue_symbol, start, end):
        idx = pd.date_range(start=start, end=end, freq="8h", tz="UTC")
        return pd.Series(0.0001, index=idx), ""


class _FakeFuturesProvider(_FakeProvider):
    name = "binance_futures"
    venue = "Fake USDⓈ-M Futures"
    supported_markets = ("crypto_perp",)


# ═══════════════════════════════════════════════════════════════════════════
# 1. SYMBOLS + CONFIG  [MOCKED / offline]
# ═══════════════════════════════════════════════════════════════════════════

def test_mocked_symbol_resolution_is_explicit():
    cfg = _cfg()
    btc = symbols.resolve("BTC", cfg=cfg)
    assert btc.ok and btc.instrument.market == "spot"
    assert btc.instrument.key == "BTC:spot"
    assert btc.instrument.quote_currency == "USDT"
    assert set(btc.instrument.provider_symbols) == {"binance_spot",
                                                    "kraken_spot"}

    perp = symbols.resolve("eth perp", cfg=cfg)
    assert perp.ok and perp.instrument.market == "perp"
    assert perp.instrument.market_class == "crypto_perp"

    # Crypto never accepts a gold market.
    bad = symbols.resolve("BTC futures", cfg=cfg)
    assert not bad.ok and "gold market" in bad.error

    # Nothing named → an honest question, not a guess.
    missing = symbols.resolve("please research something", cfg=cfg)
    assert not missing.ok and "BTC" in missing.error


def test_mocked_gold_asks_once_then_remembers(tmp_path):
    cfg = Config(tmp_path / "config.json")
    first = symbols.resolve("GOLD", cfg=cfg)
    assert first.needs_input and not first.ok
    assert set(first.options) == {"GOLD xauusd", "GOLD futures", "GOLD mcx"}
    assert cfg.gold_market() is None, "nothing must be saved before the answer"

    cfg.set_gold_market("futures")
    second = symbols.resolve("GOLD", cfg=cfg)
    assert second.ok and second.instrument.market == "futures"
    assert second.instrument.provider_symbols == {"yahoo_gc": "GC=F"}

    # Explicit request wins over the saved preference, without losing it.
    spot = symbols.resolve("GOLD xauusd", cfg=cfg)
    assert spot.ok and spot.instrument.market == "xauusd"
    reloaded = Config(tmp_path / "config.json")
    assert reloaded.gold_market() == "futures"

    # MCX cannot be resolved without an explicit contract.
    mcx = symbols.resolve("GOLD mcx", cfg=cfg)
    assert not mcx.ok and "MCX" in mcx.question and mcx.needs_input

    with pytest.raises(ValueError):
        cfg.set_gold_market("bitcoin")


def test_mocked_mcx_contract_must_be_explicit(tmp_path):
    cfg = Config(tmp_path / "config.json")
    cfg.save({
        "mcx_gold_contract": {"security_id": "123456", "expiry": "2027-02-05",
                              "lot_size": 1},
        "mcx_timezone_confirmed": True,
    })
    inst = symbols.resolve("GOLD mcx", cfg=cfg)
    assert inst.ok and inst.instrument.market == "mcx"
    assert inst.instrument.quote_currency == "INR"
    assert inst.instrument.provider_symbols == {"dhan_mcx": "123456"}

    cfg.save({"mcx_gold_contract": {"security_id": "123456",
                                    "expiry": "2020-01-01"}})
    expired = symbols.resolve("GOLD mcx", cfg=cfg)
    assert not expired.ok and "expired" in expired.error


# ═══════════════════════════════════════════════════════════════════════════
# 2. DATA NORMALISATION, AGGREGATION AND THE COVERAGE GATE  [MOCKED]
# ═══════════════════════════════════════════════════════════════════════════

def test_mocked_normalise_frame_utc_duplicates_and_garbage():
    idx = pd.to_datetime(["2020-01-01 02:00", "2020-01-01 00:00",
                          "2020-01-01 00:00", "2020-01-01 01:00"])
    raw = pd.DataFrame({
        "open": [3, 1, 1.5, 2], "high": [4, 2, 2, 3],
        "low": [2, 0, 0, 1], "close": [3, 1, 1.5, 2],
        "volume": [1, 1, 1, 1]}, index=idx)
    frame, dupes = normalise_frame(raw)
    assert dupes == 1
    assert str(frame.index.tz) == "UTC"
    assert list(frame.index) == sorted(frame.index)
    # Last row wins for a duplicated timestamp.
    assert frame.loc[pd.Timestamp("2020-01-01 00:00", tz="UTC"), "close"] == 1.5

    dirty = raw.copy()
    dirty.iloc[0, dirty.columns.get_loc("close")] = np.inf
    cleaned, _ = normalise_frame(dirty)
    assert np.isfinite(cleaned.to_numpy()).all()
    assert len(cleaned) == 2          # inf row dropped, duplicate collapsed


def test_mocked_drop_incomplete_drops_forming_candle():
    now = datetime(2024, 1, 1, 12, 30, tzinfo=timezone.utc)
    idx = pd.date_range("2024-01-01 10:00", periods=3, freq="1h", tz="UTC")
    frame = _ohlc(idx, [100, 101, 102])
    out, dropped = drop_incomplete(frame, "1h", now=now)
    assert dropped and len(out) == 2
    assert out.index[-1] == pd.Timestamp("2024-01-01 11:00", tz="UTC")


def test_mocked_aggregation_is_upsample_only_and_gap_aware():
    idx = pd.date_range("2024-01-01 00:00", periods=8, freq="1h", tz="UTC")
    frame = pd.DataFrame({
        "open": np.arange(100.0, 108.0),
        "high": np.arange(101.0, 109.0),
        "low": np.arange(99.0, 107.0),
        "close": np.arange(100.5, 108.5),
        "volume": np.arange(1.0, 9.0)}, index=idx)
    out, info = aggregate.resample_ohlcv(frame, "1h", "4h")
    assert len(out) == 2 and info["empty_buckets"] == 0
    assert out.iloc[0]["open"] == 100 and out.iloc[0]["close"] == 103.5
    assert out.iloc[0]["high"] == 104 and out.iloc[0]["low"] == 99
    assert out.iloc[0]["volume"] == 1 + 2 + 3 + 4
    assert out.index[0] == pd.Timestamp("2024-01-01 00:00", tz="UTC")

    with pytest.raises(ValueError):
        aggregate.resample_ohlcv(frame, "4h", "1h")
    assert not aggregate.can_aggregate("4h", "1h")

    wide_idx = pd.date_range("2024-01-01", periods=16, freq="1h", tz="UTC")
    wide = pd.DataFrame({
        "open": np.arange(100.0, 116.0),
        "high": np.arange(101.0, 117.0),
        "low": np.arange(99.0, 115.0),
        "close": np.arange(100.5, 116.5),
        "volume": 1.0}, index=wide_idx)
    # Remove 04:00–11:00 entirely: two whole 4h buckets are absent and must be
    # counted as empty, never forward-filled.
    holey = wide.drop(wide.index[4:12])
    out2, info2 = aggregate.resample_ohlcv(holey, "1h", "4h")
    assert len(out2) == 2 and info2["empty_buckets"] == 2


def test_mocked_coverage_gate_accepts_one_year_and_rejects_short_history():
    cfg = _cfg()
    inst = symbols.resolve("BTC", cfg=cfg).instrument
    start = datetime(2022, 1, 1, tzinfo=timezone.utc)
    end = datetime(2023, 5, 16, tzinfo=timezone.utc)
    frame = _ohlc(pd.date_range("2022-01-01", periods=500, freq="1D",
                                tz="UTC"), np.linspace(100, 200, 500))
    ok = quality.coverage_report(
        frame, instrument=inst, provider="fake", timeframe="1d",
        requested_start=start, requested_end=end, min_days=365,
        min_ratio=0.95, max_gap_s=432000)
    assert ok["usable"] and ok["status"] == "OK"
    assert ok["one_year_ok"] and ok["missing_candles"] == 0
    assert ok["coverage_days"] >= 364

    short_frame = frame.iloc[:120]
    short = quality.coverage_report(
        short_frame, instrument=inst, provider="fake", timeframe="1d",
        requested_start=start, requested_end=end, min_days=365)
    assert not short["usable"] and short["status"] == "INSUFFICIENT_DATA"
    assert "calendar days" in short["reason"]

    empty = quality.coverage_report(
        frame.iloc[:0], instrument=inst, provider="fake", timeframe="1d",
        requested_start=start, requested_end=end)
    assert empty["status"] == "BLOCKED" and not empty["usable"]


def test_mocked_session_market_gaps_are_reported_not_fatal():
    cfg = _cfg(gold_market="futures")
    inst = symbols.resolve("GOLD futures", cfg=cfg).instrument
    # Weekdays only: weekends are ~3-day gaps, which must be reported.
    idx = pd.bdate_range("2022-01-03", periods=520, tz="UTC")
    frame = _ohlc(idx, np.linspace(1800, 2000, len(idx)), spread=2.0)
    rep = quality.coverage_report(
        frame, instrument=inst, provider="yahoo_gc", timeframe="1d",
        requested_start=datetime(2021, 1, 1, tzinfo=timezone.utc),
        requested_end=datetime(2023, 12, 31, tzinfo=timezone.utc),
        min_days=365)
    assert rep["usable"] and rep["status"] == "OK"
    assert rep["missing_candles"] is None, "session markets do not assert bars"
    assert rep["gaps"]["count"] > 0, "weekend gaps must be visible"
    assert any("Session market" in n for n in rep["limitations"])


# ═══════════════════════════════════════════════════════════════════════════
# 3. EXECUTION ENGINE  [MOCKED]
# ═══════════════════════════════════════════════════════════════════════════

def test_mocked_engine_fills_next_bar_open_never_the_signal_bar():
    idx = pd.date_range("2024-01-01", periods=8, freq="1h", tz="UTC")
    df = _ohlc(idx, [100, 100, 100, 110, 120, 130, 140, 150], spread=2.0)
    sig = _signals(df, stop=10.0, target_r=0.0, long_entry=[2],
                   long_exit=[4])
    res = run_backtest(df, sig, timeframe="1h", costs=Costs(0, 0, 0),
                       capital=10_000, max_leverage=1.0)
    assert res.ok and len(res.trades) == 1
    trade = res.trades[0]
    # Signal happened at bar 2 (close 101); the fill is bar 3's open (110).
    assert trade["entry"] == 110.0
    assert trade["entry_time"] == df.index[3].isoformat()
    assert trade["exit"] == 130.0 and trade["exit_time"] == df.index[5].isoformat()

    # A signal on the final bar cannot be filled — there is no next open.
    last_bar = _signals(df, stop=10.0, long_entry=[7])
    assert run_backtest(df, last_bar, timeframe="1h",
                        costs=Costs(0, 0, 0)).trades == []


def test_mocked_engine_target_cannot_fill_on_entry_bar():
    idx = pd.date_range("2024-02-01", periods=6, freq="1h", tz="UTC")
    df = _ohlc(idx, [100, 100, 100, 100, 100, 100], spread=1.0)
    df.loc[idx[2], "high"] = 110.0          # target 105 is inside the entry bar
    sig = _signals(df, stop=10.0, target_r=0.5, long_entry=[1],
                   long_exit=[3])
    res = run_backtest(df, sig, timeframe="1h", costs=Costs(0, 0, 0))
    assert len(res.trades) == 1
    assert res.trades[0]["exit_kind"] == "signal"
    assert res.trades[0]["exit"] == 100.0


def test_mocked_engine_stop_first_when_bar_touches_both_levels():
    idx = pd.date_range("2024-03-01", periods=6, freq="1h", tz="UTC")
    df = _ohlc(idx, [100, 100, 100, 100, 100, 100], spread=1.0)
    df.loc[idx[2], "low"] = 97.0            # stop 98
    df.loc[idx[2], "high"] = 105.0          # target 102
    sig = _signals(df, stop=2.0, target_r=1.0, long_entry=[1])
    res = run_backtest(df, sig, timeframe="1h", costs=Costs(0, 0, 0))
    assert res.trades[0]["exit_kind"] == "stop"
    assert res.trades[0]["exit"] == 98.0
    assert res.trades[0]["pnl"] < 0


def test_mocked_engine_gap_through_stop_fills_at_open():
    idx = pd.date_range("2024-04-01", periods=6, freq="1h", tz="UTC")
    df = _ohlc(idx, [100, 100, 100, 90, 90, 90], spread=1.0)
    df.loc[idx[2], "low"] = 99.0            # not stopped on entry bar
    sig = _signals(df, stop=2.0, target_r=1.0, long_entry=[1])
    res = run_backtest(df, sig, timeframe="1h", costs=Costs(0, 0, 0))
    trade = res.trades[0]
    assert trade["exit_kind"] == "gap_stop"
    assert trade["exit"] == 90.0, "a gap must fill at the open, not the stop"


def test_mocked_engine_costs_shorting_and_leverage_constraints():
    idx = pd.date_range("2024-05-01", periods=40, freq="1h", tz="UTC")
    opens = 100 + np.sin(np.arange(40) / 2.0) * 5
    df = _ohlc(idx, opens, spread=1.0)
    sig = _signals(df, stop=1.0, target_r=0.0,
                   long_entry=list(range(1, 35, 6)),
                   long_exit=list(range(4, 38, 6)))

    free = run_backtest(df, sig, timeframe="1h", costs=Costs(0, 0, 0))
    paid = run_backtest(df, sig, timeframe="1h",
                        costs=Costs(10.0, 5.0, 2.0))
    assert free.metrics["trades"] == paid.metrics["trades"] > 0
    assert paid.metrics["net_return"] < free.metrics["net_return"]
    assert paid.metrics["max_drawdown"] >= 0

    # Leverage cap: notional can never exceed equity × cap.
    for trade in paid.trades:
        assert trade["notional"] <= 10_000.0 * 1.0001

    # Spot is long-only: a short signal must not open a position.
    short_sig = _signals(df, stop=1.0, short_entry=[1],
                         short_exit=[3])
    no_short = run_backtest(df, short_sig, timeframe="1h",
                            costs=Costs(0, 0, 0), allow_short=False)
    assert no_short.trades == []
    short_ok = run_backtest(df, short_sig, timeframe="1h",
                            costs=Costs(0, 0, 0), allow_short=True,
                            max_leverage=3.0)
    assert short_ok.trades and short_ok.trades[0]["direction"] == "SHORT"


def test_mocked_engine_reports_realised_risk_reward():
    idx = pd.date_range("2024-07-01", periods=10, freq="1h", tz="UTC")
    df = _ohlc(idx, [100, 100, 102, 104, 106, 102, 100, 99, 98, 98],
               spread=0.2)
    # One +4 winner and one -2 loser, with a wide stop so neither is stopped
    # out: realised R is +0.8 / -0.4, so risk/reward must be exactly 2.0.
    sig = _signals(df, stop=5.0, target_r=0.0,
                   long_entry=[1, 5], long_exit=[3, 7])
    res = run_backtest(df, sig, timeframe="1h", costs=Costs(0, 0, 0))
    m = res.metrics
    assert m["trades"] == 2
    assert m["avg_win_r"] == pytest.approx(0.8, abs=1e-3)
    assert m["avg_loss_r"] == pytest.approx(0.4, abs=1e-3)
    assert m["risk_reward"] == pytest.approx(2.0, abs=1e-3)
    assert m["expectancy_r"] == pytest.approx(0.2, abs=1e-3)

    # No losing trade means no ratio to report — None, never infinity.
    only_wins = _signals(df, stop=5.0, target_r=0.0,
                         long_entry=[1], long_exit=[3])
    m2 = run_backtest(df, only_wins, timeframe="1h",
                      costs=Costs(0, 0, 0)).metrics
    assert m2["trades"] == 1
    assert m2["avg_loss_r"] == 0.0
    assert m2["risk_reward"] is None


def test_mocked_engine_funding_is_charged_on_real_timestamps():
    idx = pd.date_range("2024-06-01", periods=30, freq="1h", tz="UTC")
    df = _ohlc(idx, np.full(30, 100.0), spread=0.2)
    sig = _signals(df, stop=1.0, long_entry=[1])
    funding = pd.Series([0.001] * 5,
                        index=pd.date_range("2024-06-01 02:00", periods=5,
                                            freq="8h", tz="UTC"))
    plain = run_backtest(df, sig, timeframe="1h", costs=Costs(0, 0, 0))
    charged = run_backtest(df, sig, timeframe="1h", costs=Costs(0, 0, 0),
                           funding=funding)
    assert charged.metrics["final_equity"] < plain.metrics["final_equity"]


def test_mocked_engine_buy_and_hold_benchmark_and_reproducibility():
    df = _daily_frame(400, drift=0.001)
    bench = buy_and_hold(df, trade_start_idx=1, costs=Costs(0, 0, 0))
    expected = df["close"].iloc[-1] / df["open"].iloc[1] - 1
    assert bench["trades"] == 1
    assert bench["net_return"] == pytest.approx(expected, rel=1e-4)

    spec = library.get("ema_cross")
    ctx = library.StrategyContext("crypto_spot", "1d", allow_short=False)
    one = evaluate(df, spec, {"fast": 8, "slow": 21}, timeframe="1d", ctx=ctx,
                   costs=Costs(5, 2, 1), capital=10_000, risk_pct=1.0,
                   max_leverage=1.0)
    two = evaluate(df, spec, {"fast": 8, "slow": 21}, timeframe="1d", ctx=ctx,
                   costs=Costs(5, 2, 1), capital=10_000, risk_pct=1.0,
                   max_leverage=1.0)
    assert one["ok"] and one["metrics"] == two["metrics"]
    assert one["trades"] == two["trades"]
    assert bars_per_year_for(df, "1d") > 300


# ═══════════════════════════════════════════════════════════════════════════
# 4. STRATEGY LIBRARY  [MOCKED]
# ═══════════════════════════════════════════════════════════════════════════

def test_mocked_registry_is_bounded_and_reviewed():
    keys = set(library.STRATEGIES)
    assert len(keys) == 10
    assert {"buy_hold", "ema_cross", "rsi_meanrev", "macd_momentum",
            "donchian_breakout", "atr_breakout", "vol_trend",
            "vwap_intraday"} <= keys
    for spec in library.STRATEGIES.values():
        assert spec.version == "1.0.0"
        assert callable(spec.build)
        assert isinstance(spec.param_grid, dict)
    # VWAP is intraday-only; buy & hold is the benchmark.
    assert not library.get("vwap_intraday").allows("crypto_spot", "1d")
    assert library.get("vwap_intraday").allows("crypto_spot", "1h")
    assert library.get("buy_hold").allows("gold_futures", "1d")


def test_mocked_parameter_grids_are_deterministic_and_bounded():
    spec = library.get("ema_cross")
    a = spec.param_sets(limit=5)
    b = spec.param_sets(limit=5)
    assert a == b and len(a) == 5
    assert all(p["fast"] < p["slow"] for p in a)

    full = 1
    for values in spec.param_grid.values():
        full *= len(values)
    assert len(spec.param_sets(limit=1000)) <= full
    # Deterministic sampling is reproducible for the same limit.
    assert library.sample_grid(spec.param_grid, 3) == \
        library.sample_grid(spec.param_grid, 3)


def test_mocked_strategy_signals_are_causal():
    df = _daily_frame(700, drift=0.0004, seed=3)
    ctx = library.StrategyContext("crypto_spot", "1d", allow_short=False)
    spec = library.get("atr_breakout")
    params = {"lookback": 20, "atr_buffer": 0.0, "stop_atr": 2.0}
    full = spec.build(df, params, ctx)
    cut = 500
    part = spec.build(df.iloc[:cut], params, ctx)
    cols = ["long_entry", "short_entry", "long_exit", "short_exit"]
    pd.testing.assert_frame_equal(full[cols].iloc[:cut], part[cols])
    assert np.allclose(full["stop_distance"].iloc[:cut],
                       part["stop_distance"], equal_nan=True)


# ═══════════════════════════════════════════════════════════════════════════
# 5. VALIDATION PROTOCOL  [MOCKED]
# ═══════════════════════════════════════════════════════════════════════════

def test_mocked_split_is_chronological_with_a_bounded_floor():
    split = make_split(365)
    assert split is not None
    assert split.train == (0, 219)
    assert split.validation == (219, 292)
    assert split.test == (292, 365)
    assert all(size >= 30 for size in split.sizes().values())
    assert make_split(50) is None, "too few bars for three real segments"

    big = make_split(1000, min_segment_bars=200)
    assert big is not None and big.sizes()["test"] == 200


def test_mocked_research_uses_train_only_and_one_untouched_oos_run():
    df = _daily_frame(900, drift=0.0009, seed=5)
    cfg = _cfg(
        min_trades_train=1, min_trades_validation=1, min_trades_oos=1,
        min_oos_sharpe=-99.0, min_profit_factor=0.0, max_drawdown=1.0,
        require_positive_oos_return=False, stability_neighbour_min=0.0,
        max_configs_per_combo=3, min_segment_bars=100,
        walk_forward={"enabled": False},
    )
    spec = library.get("atr_breakout")
    ctx = library.StrategyContext("crypto_spot", "1d", allow_short=False)
    out = research_strategy(df, spec, timeframe="1d", ctx=ctx,
                            costs=Costs(5, 2, 1), cfg=cfg)
    assert out["status"] in {"CANDIDATE", "REJECTED"}
    assert out["tested"] > 0 and out["experiments"]
    assert out["train"] and out["validation"] and out["oos"]
    assert out["selection"] == "train → shortlist → validation → single OOS test"
    assert out["walk_forward"]["enabled"] is False
    # The reported OOS window starts inside the test slice, never before it.
    split = make_split(len(df), 0.60, 0.20, 100)
    assert out["oos"]["start"] >= df.index[split.test[0]].isoformat()
    # Train and validation must stop at their own boundaries; running them over
    # the full frame would leak the untouched test slice into selection.
    assert out["train"]["end"] <= df.index[split.train_end - 1].isoformat()
    assert out["validation"]["end"] <= df.index[split.val_end - 1].isoformat()
    assert out["benchmark_oos"]["trades"] == 1


def test_mocked_no_robust_strategy_is_a_valid_explicit_verdict():
    result = {
        "job_id": "bt-20260101-deadbeef01", "symbol": "BTC",
        "status": "NO_ROBUST_STRATEGY_FOUND",
        "headline": "NO ROBUST STRATEGY FOUND",
        "reason": "Every candidate failed an out-of-sample gate.",
        "candidates": [], "instrument": {},
    }
    text = reports.render_summary(result)
    assert "NO ROBUST STRATEGY FOUND" in text
    assert "not advice" in text.lower()


def test_mocked_summary_reports_risk_reward():
    result = {
        "job_id": "bt-20260101-feedface01", "symbol": "BTC",
        "status": "COMPLETED", "candidates": [], "instrument": {},
        "champion": {
            "name": "EMA cross", "version": 1, "timeframe": "1h",
            "strategy": "ema_cross", "params": {"fast": 8, "slow": 21},
            "oos": {"net_return": 0.12, "risk_reward": 2.1, "trades": 14},
            "benchmark_oos": {}, "costs": {},
        },
    }
    text = reports.render_summary(result)
    assert "Risk/reward (realised)" in text
    assert "1 : 2.10" in text
    # A missing ratio is a dash, never a fabricated number.
    result["champion"]["oos"].pop("risk_reward")
    assert "| Risk/reward (realised) | n/a |" in reports.render_summary(result)


# ═══════════════════════════════════════════════════════════════════════════
# 6. PROVIDERS + CACHE  [MOCKED]
# ═══════════════════════════════════════════════════════════════════════════

def test_mocked_provider_order_and_health_probe():
    cfg = _cfg()
    reg = build_providers(cfg)
    spot = symbols.resolve("BTC", cfg=cfg).instrument
    names = [p.name for p in providers_for(spot, cfg, reg)]
    assert names[:2] == ["binance_spot", "kraken_spot"]
    assert "binance_futures" not in names

    perp = symbols.resolve("BTC perp", cfg=cfg).instrument
    assert [p.name for p in providers_for(perp, cfg, reg)] == \
        ["binance_futures"]

    class _Avail(Provider):
        name, venue = "binance_spot", "Mock"
        supported_markets = ("crypto_spot",)
        native_timeframes = ("1d",)

        def available(self, symbol):
            return True, symbol

    rows = provider_health(cfg, {"binance_spot": _Avail()}, instrument=spot)
    assert rows[0]["available"] is True and rows[0]["detail"] == "BTCUSDT"
    # Without an instrument the probe must not invent a venue symbol.
    unprobed = provider_health(cfg, {"binance_spot": _Avail()})
    assert unprobed[0]["available"] is None
    assert "not probed" in unprobed[0]["detail"]


def test_mocked_binance_pagination_and_url(monkeypatch):
    cfg = _cfg()
    inst = symbols.resolve("BTC", cfg=cfg).instrument
    provider = BinanceSpot(cfg)
    provider.page_limit = 3
    monkeypatch.setattr(provider, "available", lambda symbol: (True, "TRADING"))

    step = 3600_000
    base = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    all_rows = [[base + i * step, f"{100 + i}", f"{101 + i}", f"{99 + i}",
                 f"{100.5 + i}", "10", 0, "0", 1, "0", "0", "0"]
                for i in range(8)]
    seen_urls = []

    def fake_http(url, params=None, **kwargs):
        seen_urls.append(url)
        start = int(params["startTime"])
        limit = int(params["limit"])
        return [r for r in all_rows if r[0] >= start][:limit], 1, 0, ""

    monkeypatch.setattr(provider, "_http_json", fake_http)
    res = provider.fetch(inst, "1h", datetime(2020, 1, 1, tzinfo=timezone.utc),
                         datetime(2020, 1, 1, 8, tzinfo=timezone.utc))
    assert res.ok and res.rows == 8
    assert res.requests_made == 3               # 3 + 3 + 2 rows
    assert res.frame.index.is_monotonic_increasing
    assert not res.frame.index.duplicated().any()
    assert seen_urls and all(u == f"{SPOT_BASE}/api/v3/klines"
                             for u in seen_urls)


def test_mocked_cache_is_incremental_and_market_isolated(tmp_path):
    cfg = _cfg()
    provider = _FakeProvider()
    cache = CandleCache(cfg, root=tmp_path / "cache")
    spot = symbols.resolve("BTC", cfg=cfg).instrument
    perp = symbols.resolve("BTC perp", cfg=cfg).instrument
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    end = datetime(2020, 4, 10, tzinfo=timezone.utc)

    first = cache.get_or_fetch(provider, spot, "1d", start, end)
    assert first.ok and not first.from_cache and len(provider.calls) == 1
    second = cache.get_or_fetch(provider, spot, "1d", start, end)
    assert second.ok and second.from_cache and len(provider.calls) == 1

    # Same symbol, different market class → different cache file, new fetch.
    cache.get_or_fetch(provider, perp, "1d", start, end)
    assert len(provider.calls) == 2
    assert provider.calls[0][0] == "crypto_spot"
    assert provider.calls[1][0] == "crypto_perp"

    # Extending the window fetches only the missing tail and merges it.
    cache.get_or_fetch(provider, spot, "1d", start,
                       datetime(2020, 5, 1, tzinfo=timezone.utc))
    assert len(provider.calls) == 3
    merged, meta = cache.load(provider.name, spot, "1d")
    assert len(merged) == len(pd.date_range(
        start, pd.Timestamp("2020-05-01", tz="UTC"), freq="1D"))
    assert meta["rows"] == len(merged) and meta["provider"] == provider.name

    # A corrupt cache is deleted and refetched, never repaired.
    csv_path, _ = cache.paths(provider.name, spot, "1d")
    csv_path.write_text("not,a,valid,candle\n", encoding="utf-8")
    repaired = cache.get_or_fetch(provider, spot, "1d", start, end, force=True)
    assert repaired.ok and len(provider.calls) == 4


def test_mocked_blocked_providers_say_exactly_what_is_missing(monkeypatch,
                                                              tmp_path):
    for key in ("TWELVEDATA_API_KEY", "DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    cfg = Config(tmp_path / "config.json")

    td = TwelveDataXauUsd(cfg)
    ok, detail = td.available()
    assert not ok and "TWELVEDATA_API_KEY" in detail

    dhan = DhanMcxGold(cfg)
    ok, detail = dhan.available()
    assert not ok and "Dhan credentials" in detail

    monkeypatch.setenv("DHAN_CLIENT_ID", "client")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "token")
    ok, detail = DhanMcxGold(cfg).available()
    assert not ok and "contract is not configured" in detail

    cfg.save({"mcx_gold_contract": {"security_id": "1", "expiry": "2027-02-05"},
              "mcx_timezone_confirmed": False})
    ok, detail = DhanMcxGold(cfg).available()
    assert not ok and "timezone" in detail.lower()

    cfg.save({"mcx_timezone_confirmed": True})
    ok, _ = DhanMcxGold(cfg).available()
    assert ok

    # Documented hard limits are not hidden.
    assert REST_MAX_CANDLES == 720
    yahoo = YahooGoldProvider(cfg)
    assert yahoo.name == "yahoo_gc" and yahoo.supports(
        symbols.resolve("GOLD futures", cfg=cfg).instrument)


# ═══════════════════════════════════════════════════════════════════════════
# 7. JOBS: STATE MACHINE, DEDUP, RETRIES, RECOVERY  [MOCKED]
# ═══════════════════════════════════════════════════════════════════════════

def test_mocked_job_states_and_dedup():
    store = _store()
    first = store.create("BTC", "spot", source="test")
    assert first["ok"] and not first["deduplicated"]
    job_id = first["job_id"]
    assert store.get(job_id)["status"] == "QUEUED"

    again = store.create("BTC", "spot", source="test")
    assert again["deduplicated"] and again["job_id"] == job_id

    store.mark_running(job_id, pid=os.getpid())
    assert store.get(job_id)["status"] == "RUNNING"
    assert store.get(job_id)["attempts"] == 1

    state = store.finish(job_id, {"status": "COMPLETED",
                                  "headline": "done",
                                  "experiments_total": 12})
    assert state == "COMPLETED"
    job = store.get(job_id)
    assert job["status"] == "COMPLETED" and job["experiments"] == 12
    assert job["progress"] == 1.0

    # A recent completion is reused instead of recomputing.
    recent = store.create("BTC", "spot", source="test")
    assert recent["deduplicated"] and recent.get("recent") is True
    assert recent["job_id"] == job_id

    # NO ROBUST STRATEGY FOUND is a successful research outcome.
    nrs = store.create("ETH", "spot")
    assert store.finish(nrs["job_id"],
                        {"status": "NO_ROBUST_STRATEGY_FOUND"}) == "COMPLETED"
    assert store.get(nrs["job_id"])["result_status"] == \
        "NO_ROBUST_STRATEGY_FOUND"


def test_mocked_job_cancel_recover_and_bounded_retry():
    cfg = _cfg(max_attempts=1)
    store = _store(cfg)
    job = store.create("BTC", "spot")
    assert store.cancel(job["job_id"])["ok"]
    assert store.get(job["job_id"])["status"] == "CANCELLED"
    # A late worker result must not overwrite the cancellation.
    assert store.finish(job["job_id"],
                        {"status": "COMPLETED"}) == "CANCELLED"

    stale = store.create("ETH", "spot")
    store.update(stale["job_id"], status="RUNNING", pid=4_000_000)
    recovered = store.recover_stale()
    assert any(r["job_id"] == stale["job_id"] for r in recovered)
    assert store.get(stale["job_id"])["status"] == "FAILED"

    # Retry budget is honoured; a non-transient error is not retried.
    retry = store.create("SOL", "spot")
    store.finish(retry["job_id"], {"status": "FAILED",
                                   "reason": "HTTP 500 upstream"})
    assert store.requeue(retry["job_id"])["ok"]
    store.mark_running(retry["job_id"], pid=os.getpid())
    store.finish(retry["job_id"], {"status": "FAILED", "reason": "boom"})
    assert not store.requeue(retry["job_id"])["ok"]
    assert not jobs.maybe_retry(retry["job_id"],
                                {"status": "FAILED",
                                 "reason": "permanent schema error"})["retried"]


def test_mocked_notification_dedup_claim():
    store = _store()
    key = "job:bt-test:COMPLETED"
    assert store.claim_notification(key, event="job", job_id="bt-test")
    assert not store.claim_notification(key, event="job", job_id="bt-test")
    store.mark_notified(key, True, "ok")
    with sqlite_connect(store.path) as conn:
        row = conn.execute("SELECT delivered, detail FROM notifications "
                           "WHERE dedup_key=?", (key,)).fetchone()
    assert row["delivered"] == 1 and row["detail"] == "ok"


def sqlite_connect(path):
    import sqlite3
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return _Closing(conn)


class _Closing:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        self.conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# 8. RESEARCH FLOW, SCHEDULER, NOTIFY, CHAMPIONS, AGENT  [MOCKED]
# ═══════════════════════════════════════════════════════════════════════════

def test_mocked_full_research_job_with_fake_provider(monkeypatch):
    cfg = _cfg(max_experiments=40, max_configs_per_combo=2,
               min_trades_train=1, min_trades_validation=1, min_trades_oos=1,
               min_oos_sharpe=-99.0, min_profit_factor=0.0, max_drawdown=1.0,
               require_positive_oos_return=False, stability_neighbour_min=0.0,
               min_segment_bars=60, walk_forward={"enabled": False},
               timeframes=["1d"])
    provider = _FakeProvider()
    monkeypatch.setattr(research, "build_providers", lambda c=None: {
        provider.name: provider})
    monkeypatch.setattr(research, "providers_for",
                        lambda inst, c=None, reg=None: [provider])

    result = research.run_research(
        "bt-20260101-abcdef0123", "BTC",
        options=research.ResearchOptions(strategies=["atr_breakout"],
                                         timeframes=["1d"], notify=False),
        cfg=cfg)
    assert result["status"] in {"COMPLETED", "NO_ROBUST_STRATEGY_FOUND"}
    assert result["timeframes_tested"] == ["1d"]
    assert result["coverage"]["1d"]["status"] == "OK"
    assert result["coverage"]["1d"]["provider"] == provider.name
    assert result["candidates"], "candidates must be assigned to the result"
    assert result["experiments_total"] > 0
    assert result["provider_attempts"]["1d"]
    assert result["execution"]["fill"].startswith("next bar open")
    assert result["paths"]["summary_md"]
    assert Path(result["paths"]["summary_md"]).exists()
    if result["champion"]:
        assert result["champion"]["oos"]["start"] >= \
            result["coverage"]["1d"]["earliest"]
    else:
        assert result["headline"] == "NO ROBUST STRATEGY FOUND"


def test_mocked_perp_research_applies_funding_without_crashing(monkeypatch):
    """Regression: a perp funding Series must never be used as a boolean."""
    cfg = _cfg(max_experiments=20, max_configs_per_combo=1,
               min_trades_train=1, min_trades_validation=1, min_trades_oos=1,
               min_oos_sharpe=-99.0, min_profit_factor=0.0, max_drawdown=1.0,
               require_positive_oos_return=False, stability_neighbour_min=0.0,
               min_segment_bars=60, walk_forward={"enabled": False},
               funding_enabled=True, timeframes=["1d"])
    provider = _FakeFuturesProvider()
    monkeypatch.setattr(research, "build_providers",
                        lambda c=None: {provider.name: provider})
    monkeypatch.setattr(research, "providers_for",
                        lambda inst, c=None, reg=None: [provider])

    result = research.run_research(
        "bt-20260101-abcdef0456", "BTC", market="perp",
        options=research.ResearchOptions(strategies=["atr_breakout"],
                                         timeframes=["1d"], notify=False),
        cfg=cfg)
    assert result["status"] != "FAILED", result.get("reason")
    assert result["market"] == "perp"
    assert result["candidates"], result.get("traceback")
    assert result["candidates"][0]["funding_used"] is True
    assert any("funding" in lim.lower()
               for lim in result["candidates"][0].get("limitations", [])
               ) or result["execution"]["funding"].startswith("real Binance") \
        or "Binance or Delta" in result["execution"]["funding"]


def test_mocked_research_blocks_before_fetching_when_unresolved():
    cfg = _cfg()
    blocked = research.run_research("bt-20260101-0000000001", "DOGE",
                                    cfg=cfg)
    assert blocked["status"] == "BLOCKED" and "BTC" in blocked["reason"]

    cfg2 = _cfg()
    needs = research.run_research("bt-20260101-0000000002", "GOLD", cfg=cfg2)
    assert needs["status"] == "NEEDS_INPUT"
    assert "GOLD futures" in needs["options"]


def test_mocked_scheduler_is_bounded_and_never_guesses_gold():
    cfg = _cfg(watchlist=["BTC", "GOLD"], max_workers=1)
    store = _store(cfg)
    plan = bt_scheduler.due_jobs(cfg, store=store)
    btc = next(p for p in plan if p["symbol"] == "BTC")
    gold = next(p for p in plan if p["symbol"] == "GOLD")
    assert btc["due"] is True and btc["reason"] == "never researched"
    assert gold["due"] is False and gold["needs_input"] is True

    dry = bt_scheduler.run_due(cfg, dry_run=True)
    assert dry["ok"] and dry["dry_run"] is True
    assert dry["started"] == []           # dry run never spawns a worker
    assert dry["limits"]["max_workers"] == 1

    store.kv_set(bt_scheduler.KV_LAST + "BTC:spot", __import__("time").time())
    after = bt_scheduler.due_jobs(cfg, store=store)
    assert next(p for p in after if p["symbol"] == "BTC")["due"] is False


def test_mocked_notifier_dry_run_redaction_and_dedup():
    cfg = _cfg(notify={"enabled": True, "channel": "telegram",
                       "dry_run": True, "dedup_hours": 12})
    notifier = Notifier(cfg, store=_store(cfg), dry_run=True)
    assert notifier.dry_run is True

    secret = "api_key=SUPERSECRETVALUE1234567890"
    assert "SUPERSECRET" not in redact(secret)
    assert "***" in redact(secret)

    result = {"job_id": "bt-20260101-aaaaaaaaaa", "symbol": "BTC",
              "market": "spot", "status": "COMPLETED",
              "instrument": {"instrument_type": "BTCUSDT spot pair"},
              "timeframes_tested": ["1d"], "experiments_total": 10,
              "champion": {"name": "MACD momentum", "version": "1.0.0",
                           "strategy": "macd_momentum", "timeframe": "1d",
                           "stability": 0.8,
                           "oos": {"net_return": 0.032, "sharpe": 0.48,
                                   "max_drawdown": 0.047, "trades": 27},
                           "benchmark_oos": {"net_return": 0.01}},
              "warnings": []}
    first = notifier.send_result(result, event="job")
    assert first["ok"] and first["dry_run"]
    second = notifier.send_result(result, event="job")
    assert second.get("deduplicated") is True

    body = format_result(result)
    assert "Research only" in body and "No order was placed" in body
    assert "MACD momentum" in body

    outbox = Path(_TMP) / "memory" / "backtest" / "outbox.jsonl"
    assert outbox.exists()
    assert any(json.loads(line)["job_id"] == result["job_id"]
               for line in outbox.read_text(encoding="utf-8").splitlines())


def test_mocked_champion_ledger_promote_keep_retire_rollback(tmp_path):
    cfg = Config(tmp_path / "cfg.json")
    store = ChampionStore(cfg, path=tmp_path / "champions.json")

    def cand(key, score, status="CANDIDATE"):
        return {"strategy": key, "name": key, "version": "1.0.0",
                "timeframe": "1d", "params": {}, "status": status,
                "oos_score": score, "oos": {"net_return": 0.1},
                "stability": 0.8}

    first = store.decide("BTC:spot", cand("ema_cross", 1.0), "job-1")
    assert first["action"] == "promoted"
    kept = store.decide("BTC:spot", cand("rsi_meanrev", 1.01), "job-2")
    assert kept["action"] == "kept", "the margin must be respected"
    second = store.decide("BTC:spot", cand("atr_breakout", 1.5), "job-3")
    assert second["action"] == "promoted"

    entry = store.get("BTC:spot")
    assert entry["champion"]["strategy"] == "atr_breakout"
    assert entry["stack"][0]["strategy"] == "ema_cross"

    rolled = store.rollback("BTC:spot")
    assert rolled["ok"] and rolled["champion"]["strategy"] == "ema_cross"
    assert store.get("BTC:spot")["stack"] == []

    store.retire("BTC:spot", "degraded")
    assert store.get("BTC:spot")["retired_reason"] == "degraded"
    again = store.decide("BTC:spot", cand("vol_trend", 0.9), "job-4",
                         retired=True)
    assert again["action"] == "promoted"
    assert store.get("BTC:spot").get("retired_at") is None
    assert len(store.history("BTC:spot")) >= 5


def test_mocked_agent_asks_gold_once_then_starts_bounded_job(tmp_path,
                                                             monkeypatch):
    cfg = Config(tmp_path / "config.json")
    store = jobs.JobStore(tmp_path / "db.sqlite", cfg=cfg)
    agent = BacktestAgent(cfg, store)

    asked = agent.handle(_request("backtest GOLD"))
    assert asked.status == TaskStatus.NEEDS_INPUT
    assert "XAUUSD" in asked.text and "MCX" in asked.text
    assert cfg.gold_market() is None

    def fake_start(symbol, market=None, **kwargs):
        created = store.create(symbol, market, mode="full", source="test")
        return {"ok": True, "job_id": created["job_id"],
                "deduplicated": created["deduplicated"],
                "job": created["job"]}

    monkeypatch.setattr("orchestrator.agents.backtest.backtest_agent.start_job",
                        fake_start)

    saved = agent.handle(_request("futures"))
    assert saved.status == TaskStatus.COMPLETED
    assert cfg.gold_market() == "futures"
    gold_job = store.get(saved.data["job_id"])
    assert gold_job["symbol"] == "GOLD" and gold_job["market"] == "futures"

    started = agent.handle(_request("backtest BTC"))
    assert started.status == TaskStatus.COMPLETED
    assert "separate worker" in started.text
    assert "no order" in started.text.lower()
    btc_job = store.get(started.data["job_id"])
    assert btc_job["symbol"] == "BTC"

    status = agent.handle(_request(f"backtest status {btc_job['job_id']}"))
    assert btc_job["job_id"] in status.text
    history = agent.handle(_request("backtest history BTC"))
    assert btc_job["job_id"] in history.text

    # A response that answers the gold question saves it, once.
    other = BacktestAgent(Config(tmp_path / "config2.json"),
                          jobs.JobStore(tmp_path / "db2.sqlite",
                                        cfg=Config(tmp_path / "config2.json")))
    assert other.cfg.gold_market() is None


def test_mocked_agent_can_handle_and_classifier_routing():
    cfg = _cfg()
    agent = BacktestAgent(cfg, _store(cfg))
    assert agent.can_handle(_request("BTC", TaskType.BACKTEST))
    assert agent.can_handle(_request("backtest BTC"))
    assert not agent.can_handle(_request("what is the weather",
                                         TaskType.RESEARCH))

    classifier = IntentClassifier()
    assert classifier.classify("backtest BTC").task_type is TaskType.BACKTEST
    assert classifier.classify("BTC").task_type is TaskType.BACKTEST
    assert classifier.classify("[backtest] ETH perp").task_type is \
        TaskType.BACKTEST
    assert classifier.classify("BTC price").task_type is TaskType.TRADING
    assert classifier.classify("cancel backtest").task_type is TaskType.BACKTEST


def test_mocked_registry_gate_serves_real_agent():
    os.environ["JARVIS_BACKTEST_AGENT"] = "on"
    try:
        registry = build_default_registry()
        found = registry.by_task_type(TaskType.BACKTEST)
        assert found and any(a.name == BACKTEST_AGENT for a in found)
        assert not isinstance(
            registry.get(BACKTEST_AGENT), type(None))
    finally:
        os.environ.pop("JARVIS_BACKTEST_AGENT", None)


# ═══════════════════════════════════════════════════════════════════════════
# 9. SAFETY + RESOURCES  [STATIC / MOCKED]
# ═══════════════════════════════════════════════════════════════════════════

def test_mocked_package_has_no_order_or_code_execution_path():
    package = BASE / "orchestrator" / "agents" / "backtest"
    forbidden = (
        "api/v3/order", "fapi/v1/order", "create_order", "place_order",
        "submit_order", "cancel_order", "eval(", "exec(", "privateKey",
        "apiSecret",
    )
    offenders = []
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, offenders

    # No live-trading module is imported anywhere in the research package.
    assert "actions.trading" not in "\n".join(
        p.read_text(encoding="utf-8") for p in package.rglob("*.py"))


def test_mocked_rio_agent_center_and_workflows_include_backtest_and_youtube():
    """The registry alone is not enough: RIO renders cards and workflow pages
    from hard-coded specs, so each new agent must be present in both."""
    from dashboard import rio_api

    registered = {r for _, _, r in rio_api._AGENTS_SPEC if r}
    expected = {
        "backtest_agent": ("JARVIS_BACKTEST_AGENT", "backtest"),
        "youtube_agent": ("JARVIS_YOUTUBE_AGENT", "youtube"),
    }
    for registry_name, (gate, workflow_key) in expected.items():
        assert registry_name in registered
        assert rio_api._GATES[registry_name][0] == gate
        wf = rio_api._WORKFLOWS.get(workflow_key)
        assert wf is not None, workflow_key
        assert wf["group"] == "orchestrator"
        assert wf["stages"]


def test_mocked_low_ram_bounds_and_a_moderate_run():
    cfg = _cfg()
    assert cfg.get("max_workers") == 1
    assert cfg.get("max_ram_mb") <= 3500
    assert cfg.get("max_candles_per_series") <= 600_000
    assert cfg.get("max_runtime_s") <= 3600
    assert cfg.get("keep_reports") <= 100

    import psutil
    proc = psutil.Process(os.getpid())
    before = proc.memory_info().rss / (1024 * 1024)
    idx = pd.date_range("2021-01-01", periods=20_000, freq="1h", tz="UTC")
    rng = np.random.default_rng(2)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, len(idx))))
    df = pd.DataFrame({
        "open": close, "high": close * 1.002, "low": close * 0.998,
        "close": close, "volume": 1.0}, index=idx)
    ctx = library.StrategyContext("crypto_spot", "1h", allow_short=False)
    out = evaluate(df, library.get("ema_cross"), {"fast": 8, "slow": 21},
                   timeframe="1h", ctx=ctx, costs=Costs(5, 2, 1),
                   capital=10_000, risk_pct=1.0, max_leverage=1.0)
    peak = proc.memory_info().rss / (1024 * 1024)
    assert out["ok"] and out["metrics"]["trades"] > 0
    assert peak - before < 1500, f"unexpected RAM growth: {peak - before:.0f} MB"


# ═══════════════════════════════════════════════════════════════════════════
# 10. REAL PROVIDER (opt-in only)  [REAL — skipped by default]
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(os.environ.get("JARVIS_BACKTEST_LIVE") != "1",
                    reason="set JARVIS_BACKTEST_LIVE=1 to run real-provider checks")
def test_real_provider_binance_daily():
    """REAL network call: Binance spot BTCUSDT, last ~30 daily candles."""
    cfg = _cfg()
    inst = symbols.resolve("BTC", cfg=cfg).instrument
    provider = BinanceSpot(cfg)
    end = datetime.now(timezone.utc) - timedelta(days=1)
    start = end - timedelta(days=35)
    res = provider.fetch(inst, "1d", start, end)
    assert res.ok, res.error
    assert res.rows >= 25
    assert res.frame.index.tz is not None
    assert res.frame.index.is_monotonic_increasing
