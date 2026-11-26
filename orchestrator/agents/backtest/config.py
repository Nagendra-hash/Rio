"""
orchestrator/agents/backtest/config.py — one configuration object, one place
to change limits.

Everything the lab may spend is bounded here: history requested, timeframes,
experiments, CPU concurrency, wall-clock budget, cache size and report
retention. The user's saved preferences (gold market, watchlist, schedule)
live in the SAME file, under memory/backtest/config.json, so Jarvis/Hermes/
RIO all read one source of truth.

No secrets are stored here. Optional API keys are read from the environment
(TWELVEDATA_API_KEY, DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN) or from the existing
config/api_keys.json — never written to logs or reports.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    # ── history ──────────────────────────────────────────────────────────
    "lookback_days": 365,            # hard minimum for every tested timeframe
    "history_days_by_timeframe": {   # preferred depth where the provider has it
        "1m": 365, "3m": 365, "5m": 730, "15m": 1095, "30m": 1095,
        "1h": 1825, "2h": 1825, "4h": 1825, "1d": 3650,
    },
    "min_coverage_ratio": 0.95,      # usable bars / expected bars
    "max_gap_seconds": {"1m": 3600, "3m": 7200, "5m": 7200, "15m": 14400,
                        "30m": 21600, "1h": 43200, "2h": 86400,
                        "4h": 172800, "1d": 432000},

    # ── instruments / markets ────────────────────────────────────────────
    "default_crypto_market": "spot",         # spot | perp
    "crypto_markets": ["spot"],              # add "perp" for a separate run
    "gold_market": None,                     # xauusd | futures | mcx (ask once)
    "watchlist": ["BTC", "ETH", "SOL", "GOLD"],
    "provider_order": {
        "crypto_spot": ["binance_spot", "kraken_spot"],
        "crypto_perp": ["binance_futures"],
        "delta_spot": ["delta_india"],
        "delta_perp": ["delta_india"],
        "gold_xauusd": ["twelvedata_xauusd", "yahoo_xauusd"],
        "gold_futures": ["yahoo_gc"],
        "gold_mcx": ["dhan_mcx"],
    },

    # ── timeframes ───────────────────────────────────────────────────────
    # Default sweep: coarse enough that a full one-year-plus verification and
    # tuning run finishes inside max_runtime_s. 1m/3m/5m are fully supported
    # and can be requested explicitly (--timeframes 1m); a year of 1m candles
    # is ~526k bars and is deliberately not fetched on every default run.
    "timeframes": ["15m", "30m", "1h", "2h", "4h", "1d"],
    "max_timeframes": 9,
    "min_bars_per_timeframe": 400,

    # ── research budget ──────────────────────────────────────────────────
    "max_experiments": 240,
    "max_configs_per_combo": 8,
    "max_timeframes_per_strategy": 9,
    "min_segment_bars": 200,
    "min_trades_train": 20,
    "min_trades_validation": 10,
    "min_trades_oos": 10,
    "multiple_testing_flag_at": 50,
    "multiple_testing_min_trades": 30,
    "train_frac": 0.60,
    "validation_frac": 0.20,
    "walk_forward": {"enabled": True, "min_bars": 1200,
                     "train_bars": 500, "test_bars": 125},
    "promotion_margin": 0.05,
    "stability_neighbour_min": 0.5,

    # ── validation thresholds (a candidate must clear ALL) ───────────────
    "min_oos_sharpe": 0.30,
    "min_profit_factor": 1.05,
    "max_drawdown": 0.50,
    "require_positive_oos_return": True,

    # ── execution assumptions (reported, never hidden) ───────────────────
    "capital": 10_000.0,
    "risk_pct": 1.0,
    "costs": {
        "crypto_spot": {"fee_bps": 10.0, "slip_bps": 5.0, "spread_bps": 2.0},
        "crypto_perp": {"fee_bps": 5.0, "slip_bps": 5.0, "spread_bps": 2.0},
        "delta_spot": {"fee_bps": 10.0, "slip_bps": 5.0, "spread_bps": 2.0},
        "delta_perp": {"fee_bps": 5.0, "slip_bps": 5.0, "spread_bps": 2.0},
        "gold_futures": {"fee_bps": 2.0, "slip_bps": 2.0, "spread_bps": 4.0},
        "gold_xauusd": {"fee_bps": 3.0, "slip_bps": 2.0, "spread_bps": 8.0},
        "gold_mcx": {"fee_bps": 3.0, "slip_bps": 3.0, "spread_bps": 6.0},
    },
    "max_leverage": {"crypto_spot": 1.0, "crypto_perp": 3.0,
                     "delta_spot": 1.0, "delta_perp": 3.0,
                     "gold_futures": 2.0, "gold_xauusd": 1.0, "gold_mcx": 2.0},
    "allow_short": {"crypto_spot": False, "crypto_perp": True,
                    "delta_spot": False, "delta_perp": True,
                    "gold_futures": True, "gold_xauusd": False,
                    "gold_mcx": True},
    "contract_multiplier": {"crypto_spot": 1.0, "crypto_perp": 1.0,
                            "delta_spot": 1.0, "delta_perp": 1.0,
                            "gold_futures": 1.0, "gold_xauusd": 1.0,
                            "gold_mcx": 1.0},
    "funding_enabled": True,          # perp only, real venue funding history

    # ── resources ────────────────────────────────────────────────────────
    "max_workers": 1,                 # one research job at a time
    "max_fetch_workers": 2,
    "max_runtime_s": 1800,
    "max_candles_per_series": 600_000,
    "max_cache_mb": 1500,
    "max_ram_mb": 3500,
    "keep_reports": 40,
    "http_timeout_s": 25,
    "http_retries": 3,
    "request_pause_s": 0.15,

    # ── scheduling / notification ────────────────────────────────────────
    "schedule_interval_hours": 168,   # weekly per symbol
    "schedule_enabled": True,
    "notify": {"enabled": True, "channel": "telegram", "dry_run": False,
               "dedup_hours": 12},
}


def base_dir() -> Path:
    """<JARVIS_HOME or repo>/memory/backtest."""
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        base = Path(home).expanduser().resolve()
    else:
        try:
            from memory.config_manager import BASE_DIR
            base = Path(BASE_DIR)
        except Exception:
            base = Path(__file__).resolve().parents[3]
    return base / "memory" / "backtest"


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """Thread-safe config reader/writer with an atomic save."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else base_dir() / "config.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] | None = None

    # ── load / save ──────────────────────────────────────────────────────
    def load(self) -> dict[str, Any]:
        with self._lock:
            if self._data is not None:
                return self._data
            raw: dict[str, Any] = {}
            try:
                if self.path.exists():
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        raw = loaded
            except Exception:
                raw = {}
            self._data = _deep_merge(DEFAULT_CONFIG, raw)
            return self._data

    def reload(self) -> dict[str, Any]:
        with self._lock:
            self._data = None
        return self.load()

    def save(self, updates: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            data = _deep_merge(self.load(), updates or {})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.path)
            self._data = data
            return data

    def apply_overrides(self, updates: dict[str, Any] | None = None
                        ) -> dict[str, Any]:
        """Merge per-run overrides into THIS instance, in memory only.

        Used by a single research job (the Backtest room's parameter form:
        history depth, capital, experiment budget) so one run can differ from
        the saved preferences without rewriting memory/backtest/config.json.
        Deliberately never touches disk — a UI experiment must not silently
        become the user's new default.
        """
        with self._lock:
            self._data = _deep_merge(self.load(), updates or {})
            return self._data

    # ── typed access ─────────────────────────────────────────────────────
    def get(self, key: str, default: Any = None) -> Any:
        data = self.load()
        return data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.load()[key]

    # ── preferences ──────────────────────────────────────────────────────
    def gold_market(self) -> str | None:
        value = self.get("gold_market")
        return str(value).strip().lower() if value else None

    def set_gold_market(self, market: str) -> dict[str, Any]:
        value = str(market or "").strip().lower()
        if value not in {"xauusd", "futures", "mcx"}:
            raise ValueError("gold_market must be xauusd, futures or mcx")
        return self.save({"gold_market": value})

    def costs(self, market_class: str) -> dict[str, float]:
        table = self.get("costs", {}) or {}
        base = {"fee_bps": 5.0, "slip_bps": 3.0, "spread_bps": 2.0}
        base.update(table.get(market_class) or {})
        return {k: float(v) for k, v in base.items()}

    def market_limit(self, table: str, market_class: str, fallback: float) -> float:
        return float((self.get(table, {}) or {}).get(market_class, fallback))

    def history_days(self, timeframe: str) -> int:
        table = self.get("history_days_by_timeframe", {}) or {}
        requested = int(table.get(timeframe) or 0)
        return max(int(self.get("lookback_days", 365)), requested)


def api_key(name: str) -> str:
    """Environment first, then the existing api_keys.json. Never logged."""
    env = os.environ.get(name, "").strip()
    if env:
        return env
    try:
        cfg = json.loads((base_dir().parents[1] / "config" / "api_keys.json")
                         .read_text(encoding="utf-8"))
        return str(cfg.get(name.lower()) or cfg.get(name) or "").strip()
    except Exception:
        return ""


# Process-wide default config (tests inject their own Config instance).
_DEFAULT: Config | None = None
_LOCK = threading.Lock()


def default_config() -> Config:
    global _DEFAULT
    with _LOCK:
        if _DEFAULT is None:
            _DEFAULT = Config()
        return _DEFAULT


def reset_default_config() -> None:
    global _DEFAULT
    with _LOCK:
        _DEFAULT = None
