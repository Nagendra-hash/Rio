"""
tests/test_paper_integration.py — real strategy hook -> paper execution.

No network: deterministic candles are fed to the REAL liquidity engine. This
proves the LONG_SIGNAL_READY event from core/liquidity_sweep.py reaches the
paper engine and opens exactly one virtual position.
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tests"))

import core.liquidity_sweep as ls  # noqa: E402
import core.paper_trading as pt  # noqa: E402
from test_liquidity_sweep import (  # noqa: E402
    BASE_TS, candle as liq_candle, make_config as liq_config,
)
from test_paper_trading import make_config as paper_config  # noqa: E402


def test_liquidity_long_signal_opens_exactly_one_paper_trade(tmp_path):
    liq_cfg_path = tmp_path / "liquidity.json"
    liq_cfg_path.write_text(__import__("json").dumps(liq_config()),
                            encoding="utf-8")
    clock = {"now": BASE_TS}
    liq_feed = {"live": True, "transport": "ws", "last_tick_age_s": 1.0}
    liq = ls.LiquiditySweepEngine(
        liq_cfg_path, tmp_path / "market.db",
        alert_sender=lambda text: False,
        live_provider=lambda: dict(liq_feed),
        data_provider=None,
        price_provider=lambda s: None,
        clock=lambda: clock["now"] / 1000,
    )

    paper_cfg_path = tmp_path / "paper.json"
    paper_cfg_path.write_text(__import__("json").dumps(paper_config()),
                              encoding="utf-8")
    quote = {"price": 100.0, "ts": BASE_TS, "source": "test_ticker"}
    paper = pt.PaperTradingEngine(
        paper_cfg_path, tmp_path / "paper.db",
        alert_sender=lambda text: True,
        price_provider=lambda s: dict(quote),
        feed_provider=lambda: {"live": True, "transport": "ws",
                               "last_tick_age_s": 1.0},
        strategy_provider=lambda s: {"ok": True, "symbol": s, "state": "IDLE"},
        clock=lambda: clock["now"] / 1000,
    )
    liq.subscribe_signals(paper.on_signal)
    try:
        ts = BASE_TS
        for _ in range(6):
            clock["now"] = ts + 180_000 + 1000
            liq.process_closed_candle(liq_candle(
                "BTCUSDT", ts, 100.8, 101.0, 100.5, 100.7, 100, 40, "3m"))
            ts += 180_000

        clock["now"] = ts + 180_000 + 1000
        liq.process_closed_candle(liq_candle(
            "BTCUSDT", ts, 100.6, 100.7, 98.0, 99.6, 120, 30, "3m"))
        ts += 180_000

        clock["now"] = ts + 180_000 + 1000
        liq.process_closed_candle(liq_candle(
            "BTCUSDT", ts, 99.2, 101.5, 98.0, 101.2, 250, 220, "3m"))
        ts += 180_000

        # The liquidity engine emits LONG_SIGNAL_READY from this candle.
        confirm_close_ts = ts + 180_000
        clock["now"] = confirm_close_ts + 1000
        quote["ts"] = confirm_close_ts + 1000
        liq.process_closed_candle(liq_candle(
            "BTCUSDT", ts, 101.2, 102.0, 101.0, 101.8, 100, 60, "3m"))

        status = liq.get_status("BTC")
        assert status["state"] == ls.LONG
        positions = paper.positions()
        assert len(positions) == 1, positions
        pos = positions[0]
        assert pos["symbol"] == "BTCUSDT"
        assert pos["entry_price"] > 100.0       # slippage applied to ticker
        assert pos["stop_loss"] < pos["entry_price"]
        assert pos["take_profit"] > pos["entry_price"]
        assert paper.status()["paper_only"] is True

        # A second identical confirmation must not duplicate the trade.
        liq.process_closed_candle(liq_candle(
            "BTCUSDT", ts, 101.2, 102.0, 101.0, 101.8, 100, 60, "3m"))
        assert len(paper.positions()) == 1

        # Position management still runs on real 1m candle data.
        clock["now"] = confirm_close_ts + 60_000
        paper.on_closed_candle({
            "symbol": "BTCUSDT", "interval": "1m", "closed": True,
            "ts": confirm_close_ts, "close_ts": confirm_close_ts + 60_000,
            "open": 101.5, "high": 101.8, "low": 90.0, "close": 91.0,
            "volume": 10.0, "taker_buy_volume": 5.0,
        })
        assert paper.positions() == []
        assert paper.history()[0]["exit_reason"] == "STOP_LOSS"
    finally:
        liq.unsubscribe_signals(paper.on_signal)
        liq.close()
        paper.close()
