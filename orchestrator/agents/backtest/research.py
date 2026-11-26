"""
orchestrator/agents/backtest/research.py — the research job itself.

Flow (each step bounded by config):

    resolve instrument
      → fetch + verify coverage per timeframe (provider order, cache,
        derived-only-from-finer aggregation)
      → reject timeframes without one year of usable completed candles
      → build market-appropriate strategies
      → tune on train, choose on validation, test once out-of-sample
      → rank by robust out-of-sample score
      → save artifacts, update champion ledger

A job that cannot meet the data floor returns INSUFFICIENT_DATA / BLOCKED with
the exact reason. It never stretches a window, merges venues or invents a bar.
"""
from __future__ import annotations

import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pandas as pd

from orchestrator.agents.backtest import aggregate, quality, reports, symbols
from orchestrator.agents.backtest.cache import CandleCache
from orchestrator.agents.backtest.champions import ChampionStore
from orchestrator.agents.backtest.config import Config, default_config
from orchestrator.agents.backtest.engine import Costs, bars_per_year_for
from orchestrator.agents.backtest.providers import (
    build_providers, provider_health, providers_for,
)
from orchestrator.agents.backtest.providers.base import tf_seconds
from orchestrator.agents.backtest.store import (
    now_iso, prune_cache, prune_reports, write_json,
)
from orchestrator.agents.backtest.strategies import library
from orchestrator.agents.backtest.validation import evaluate, research_strategy


class JobCancelled(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class ResearchOptions:
    mode: str = "full"                 # full | scheduled
    strategies: list[str] = field(default_factory=list)
    timeframes: list[str] = field(default_factory=list)
    force_refresh: bool = False
    notify: bool = True
    source: str = "cli"
    note: str = ""
    # Per-run config overrides (backtest room parameter form). Applied to the
    # worker's Config in memory only — never saved to config.json.
    config_overrides: dict[str, Any] = field(default_factory=dict)


def _peak_rss_mb() -> float:
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        return 0.0


def _base_result(job_id: str, symbol: str, market: str | None,
                 status: str) -> dict[str, Any]:
    return {
        "ok": status == "COMPLETED",
        "status": status,
        "job_id": job_id,
        "symbol": symbol,
        "market": market or "",
        "created_at": now_iso(),
        "instrument": {},
        "coverage": {},
        "timeframes_tested": [],
        "timeframes_rejected": [],
        "strategies": [],
        "candidates": [],
        "champion": None,
        "ranking": [],
        "experiments_total": 0,
        "failures_total": 0,
        "providers_used": [],
        "coverage_notes": [],
        "warnings": [],
        "limitations": [],
        "headline": "",
        "reason": "",
        "paths": {},
        "disclaimer": reports.DISCLAIMER,
    }


def _progress(cb: Callable | None, phase: str, pct: float, message: str = "") -> None:
    if cb:
        try:
            cb(phase, max(0.0, min(1.0, pct)), message)
        except Exception:
            pass


def _check(cancel: Callable | None, deadline: float) -> None:
    if cancel:
        try:
            if cancel():
                raise JobCancelled("cancelled by user")
        except JobCancelled:
            raise
        except Exception:
            pass
    if time.time() > deadline:
        raise BudgetExceeded("runtime budget exhausted")


def _max_leverage(cfg: Config, market_class: str) -> float:
    return max(1.0, cfg.market_limit("max_leverage", market_class, 1.0))


def _multiplier(cfg: Config, market_class: str) -> float:
    return float((cfg.get("contract_multiplier", {}) or {}).get(market_class, 1.0))


def _funding_for(registry, instrument, df: pd.DataFrame, cfg: Config) -> pd.Series | None:
    """Real funding history from the venue that served this instrument.

    Binance and Delta both expose funding history; the instrument's
    provider_symbols decides which adapter is asked. A venue with no history
    returns None, and the result is marked as funding-excluded rather than
    silently assuming zero funding.
    """
    if instrument.market != "perp" or not bool(cfg.get("funding_enabled", True)):
        return None
    if not len(df):
        return None
    for name in ("binance_futures", "delta_india"):
        provider = registry.get(name)
        venue = str((instrument.provider_symbols or {}).get(name) or "")
        if not provider or not venue or not hasattr(provider, "funding_history"):
            continue
        try:
            series, err = provider.funding_history(
                venue, df.index[0].to_pydatetime(), df.index[-1].to_pydatetime())
        except Exception:
            continue
        if err and not len(series):
            continue
        if len(series):
            return series
    return None


def _derive(frame: pd.DataFrame, source_tf: str, target_tf: str
            ) -> tuple[pd.DataFrame, dict]:
    return aggregate.resample_ohlcv(frame, source_tf, target_tf)


def collect_data(instrument, cfg: Config, *, options: ResearchOptions,
                 progress=None, cancel=None, deadline: float) -> dict[str, Any]:
    """Fetch every advertised timeframe, verify coverage, aggregate honestly."""
    registry = build_providers(cfg)
    providers = providers_for(instrument, cfg, registry)
    cache = CandleCache(cfg)
    end = datetime.now(timezone.utc)
    wanted = list(options.timeframes or cfg.get("timeframes", []))
    wanted = [tf for tf in wanted if tf in
              ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d")]
    wanted.sort(key=tf_seconds)
    wanted = wanted[:int(cfg.get("max_timeframes", 9))]

    frames: dict[str, pd.DataFrame] = {}
    coverage: dict[str, dict] = {}
    attempts: dict[str, list[dict]] = {}
    rejected: list[dict] = []
    notes: list[str] = []
    providers_used: set[str] = set()

    if not providers:
        return {"ok": False, "reason": "no provider serves this instrument",
                "frames": {}, "coverage": {}, "rejected": rejected,
                "providers_used": [], "registry": registry,
                "providers": providers, "notes": notes}

    for idx, tf in enumerate(wanted):
        _check(cancel, deadline)
        _progress(progress, "coverage", 0.05 + 0.25 * idx / max(len(wanted), 1),
                  f"checking {tf} coverage")
        start = end - timedelta(days=cfg.history_days(tf))
        attempts[tf] = []
        accepted = False

        for provider in providers:
            if tf not in provider.timeframes(instrument):
                continue
            result = cache.get_or_fetch(
                provider, instrument, tf, start, end,
                max_candles=int(cfg.get("max_candles_per_series", 600_000)),
                force=options.force_refresh)
            if not result.ok:
                attempts[tf].append({"provider": provider.name,
                                     "error": result.error})
                continue
            report = quality.coverage_report(
                result.frame, instrument=instrument, provider=provider.name,
                timeframe=tf, requested_start=start, requested_end=end,
                duplicates_dropped=(result.fetch.duplicates_dropped
                                    if result.fetch else 0),
                fetch_notes=(result.meta.get("limitations") or []),
                min_days=int(cfg.get("lookback_days", 365)),
                min_ratio=float(cfg.get("min_coverage_ratio", 0.95)),
                max_gap_s=float((cfg.get("max_gap_seconds", {}) or {}).get(tf)
                                or 0) or None,
                complete=not bool(result.fetch.incomplete_dropped
                                  if result.fetch else False))
            attempts[tf].append({"provider": provider.name,
                                 "status": report["status"],
                                 "reason": report["reason"],
                                 "from_cache": result.from_cache})
            coverage[tf] = report
            if report["usable"]:
                frames[tf] = result.frame
                providers_used.add(provider.name)
                accepted = True
                break

        if accepted:
            continue

        # Derive from a finer timeframe that was actually fetched — never from
        # coarser data and never by repeating a bar.
        source = next((src for src in sorted(frames, key=tf_seconds,
                                             reverse=True)
                       if aggregate.can_aggregate(src, tf)), None)
        if source:
            agg, info = _derive(frames[source], source, tf)
            report = quality.coverage_report(
                agg, instrument=instrument,
                provider=f"derived:{source}",
                timeframe=tf, requested_start=start, requested_end=end,
                fetch_notes=[f"Aggregated from {source} ({info['rule']})"],
                min_days=int(cfg.get("lookback_days", 365)),
                min_ratio=float(cfg.get("min_coverage_ratio", 0.95)),
                max_gap_s=float((cfg.get("max_gap_seconds", {}) or {}).get(tf)
                                or 0) or None,
                derived_from=source)
            coverage[tf] = report
            if report["usable"]:
                frames[tf] = agg
                providers_used.add(f"derived:{source}")
                continue
            rejected.append({"timeframe": tf,
                             "reason": f"derived from {source}: {report['reason']}"})
            continue

        reason = "no configured provider serves this interval"
        if coverage.get(tf):
            reason = coverage[tf].get("reason") or reason
        elif attempts.get(tf):
            reason = "; ".join(f"{a['provider']}: {a.get('error') or a.get('reason')}"
                               for a in attempts[tf])[:400]
        rejected.append({"timeframe": tf, "reason": reason,
                         "attempts": attempts.get(tf, [])})

    return {
        "ok": bool(frames), "frames": frames, "coverage": coverage,
        "rejected": rejected, "providers_used": sorted(providers_used),
        "registry": registry, "providers": providers,
        "attempts": attempts, "notes": notes,
        "reason": "" if frames else "no timeframe met the coverage floor",
    }


def run_research(job_id: str, symbol: str, market: str | None = None,
                 options: ResearchOptions | None = None,
                 cfg: Config | None = None,
                 progress: Callable | None = None,
                 cancel: Callable | None = None) -> dict[str, Any]:
    cfg = cfg or default_config()
    options = options or ResearchOptions()
    started = time.time()
    deadline = started + float(cfg.get("max_runtime_s", 1800))
    result = _base_result(job_id, symbol, market, "RUNNING")
    peak_ram = 0.0

    try:
        _progress(progress, "resolve", 0.02, "resolving instrument")
        resolution = symbols.resolve(symbol, market=market, cfg=cfg)
        result["resolution"] = resolution.to_dict()
        if not resolution.ok:
            result.update({
                "status": "BLOCKED" if not resolution.needs_input else "NEEDS_INPUT",
                "headline": resolution.question or "instrument resolution blocked",
                "reason": resolution.error or resolution.question,
                "needs_input": resolution.needs_input,
                "question": resolution.question,
                "options": list(resolution.options),
            })
            result["runtime_s"] = round(time.time() - started, 2)
            return result

        instrument = resolution.instrument
        result["instrument"] = instrument.describe()
        result["market"] = instrument.market
        result["limitations"].extend(instrument.notes)

        _progress(progress, "coverage", 0.05, "verifying historical coverage")
        data = collect_data(instrument, cfg, options=options,
                            progress=progress, cancel=cancel, deadline=deadline)
        result["coverage"] = data["coverage"]
        result["timeframes_rejected"] = data["rejected"]
        result["providers_used"] = data["providers_used"]
        result["provider_attempts"] = data.get("attempts", {})
        result["provider_health"] = [
            p for p in provider_health(cfg, data.get("registry"),
                                       instrument=instrument)]

        if not data["ok"]:
            result.update({
                "status": "INSUFFICIENT_DATA",
                "headline": "INSUFFICIENT DATA",
                "reason": ("No timeframe produced at least "
                           f"{cfg.get('lookback_days', 365)} days of usable, "
                           "completed candles from an authoritative provider."),
            })
            result["warnings"].append(
                "Nothing was backtested: the data floor is a hard gate.")
            _finalise(result, job_id, cfg, started, peak_ram)
            return result

        frames: dict[str, pd.DataFrame] = data["frames"]
        eligible = sorted(frames, key=tf_seconds)
        result["timeframes_tested"] = eligible
        for tf, frame in frames.items():
            result["limitations"].extend(
                (data["coverage"].get(tf, {}).get("limitations") or []))
        if data["rejected"]:
            result["warnings"].append(
                f"{len(data['rejected'])} timeframe(s) rejected before testing: "
                + ", ".join(r["timeframe"] for r in data["rejected"]))

        # ── strategy selection ───────────────────────────────────────────
        keys = set(options.strategies or [])
        scheduled_keys: list[str] = []
        if options.mode == "scheduled":
            champion = ChampionStore(cfg).get(instrument.key) or {}
            if champion.get("champion"):
                scheduled_keys.append(champion["champion"]["strategy"])
            prev = (ChampionStore(cfg).history(instrument.key) or [])[-8:]
            for event in reversed(prev):
                if event.get("strategy") and event["strategy"] not in scheduled_keys:
                    scheduled_keys.append(event["strategy"])
                if len(scheduled_keys) >= 4:
                    break
            keys = set(scheduled_keys) if scheduled_keys else keys

        allow_short = bool(cfg.market_limit("allow_short",
                                            instrument.market_class, 0.0))
        ctx_base = {"allow_short": allow_short,
                    "session_market": instrument.asset_class == "gold"}

        budget_total = int(cfg.get("max_experiments", 240))
        budget_per_tf = max(8, budget_total // max(len(eligible), 1))
        experiments_total = 0
        failures_total = 0
        candidates: list[dict[str, Any]] = []
        strategies_seen: dict[str, dict] = {}
        funding_cache: dict[str, pd.Series | None] = {}

        for t_i, tf in enumerate(eligible):
            _check(cancel, deadline)
            df = frames[tf]
            spec_list = [s for s in library.STRATEGIES.values()
                         if s.key != "buy_hold"
                         and s.allows(instrument.market_class, tf)
                         and (not keys or s.key in keys)]
            if not spec_list and keys:
                spec_list = [s for s in library.STRATEGIES.values()
                             if s.key != "buy_hold"
                             and s.allows(instrument.market_class, tf)]
            if not spec_list:
                continue
            costs = Costs(**{k: v for k, v in
                             cfg.costs(instrument.market_class).items()})
            multiplier = _multiplier(cfg, instrument.market_class)
            ctx = library.StrategyContext(
                market_class=instrument.market_class, timeframe=tf,
                allow_short=allow_short,
                session_market=instrument.asset_class == "gold")
            if tf not in funding_cache:
                funding_cache[tf] = _funding_for(data["registry"], instrument,
                                                 df, cfg)

            for s_i, spec in enumerate(spec_list):
                _check(cancel, deadline)
                strategies_seen[spec.key] = {"key": spec.key, "name": spec.name,
                                             "version": spec.version}
                pct = 0.30 + 0.60 * ((t_i + s_i / max(len(spec_list), 1))
                                     / max(len(eligible), 1))
                _progress(progress, "research", pct,
                          f"{tf} · {spec.key} tuning")
                remaining = budget_total - experiments_total
                if remaining <= 0:
                    result["warnings"].append(
                        "experiment budget exhausted before every combination "
                        "was tested; results are marked truncated.")
                    break
                try:
                    cand = research_strategy(
                        df, spec, timeframe=tf, ctx=ctx, costs=costs, cfg=cfg,
                        multiplier=multiplier, funding=funding_cache[tf],
                        budget={"remaining": min(budget_per_tf, remaining)})
                except (JobCancelled, BudgetExceeded):
                    raise
                except Exception as exc:
                    failures_total += 1
                    cand = {"strategy": spec.key, "name": spec.name,
                            "version": spec.version, "timeframe": tf,
                            "status": "REJECTED",
                            "reason": f"{type(exc).__name__}: {exc}",
                            "experiments": [], "tested": 0}
                experiments_total += int(cand.get("tested") or 0)
                failures_total += int(cand.get("failures") or 0)
                cand.update({
                    "provider": (data["coverage"].get(tf) or {}).get("provider", ""),
                    "instrument": instrument.instrument_type,
                    "market": instrument.market,
                    "market_class": instrument.market_class,
                    "costs": costs.describe(),
                    "multiplier": multiplier,
                    "funding_used": bool(
                        funding_cache[tf] is not None
                        and len(funding_cache[tf]) > 0),
                })
                candidates.append(cand)
                peak_ram = max(peak_ram, _peak_rss_mb())
            if time.time() > deadline:
                result["warnings"].append(
                    "runtime budget exhausted mid-run — later timeframes were "
                    "not tested.")
                break

        result["strategies"] = list(strategies_seen.values())
        result["candidates"] = candidates
        result["experiments_total"] = experiments_total
        result["failures_total"] = failures_total

        # ── ranking + champion ───────────────────────────────────────────
        passing = [c for c in candidates if c.get("status") == "CANDIDATE"]
        passing.sort(key=lambda c: float(c.get("oos_score") or -1e9), reverse=True)
        result["ranking"] = [{
            "strategy": c["strategy"], "timeframe": c["timeframe"],
            "oos_score": c.get("oos_score"),
            "oos_net_return": (c.get("oos") or {}).get("net_return"),
            "oos_sharpe": (c.get("oos") or {}).get("sharpe"),
            "oos_max_drawdown": (c.get("oos") or {}).get("max_drawdown"),
            "oos_trades": (c.get("oos") or {}).get("trades"),
            "oos_risk_reward": (c.get("oos") or {}).get("risk_reward"),
            "stability": c.get("stability"),
            "status": c.get("status"),
        } for c in sorted(candidates,
                          key=lambda c: float(c.get("oos_score") or -1e9),
                          reverse=True)]

        champion = passing[0] if passing else None
        if champion:
            result["champion"] = champion
            result["headline"] = (
                f"{champion['name']} v{champion['version']} on "
                f"{champion['timeframe']} passed all out-of-sample gates")
        else:
            result["status"] = "NO_ROBUST_STRATEGY_FOUND"
            result["headline"] = "NO ROBUST STRATEGY FOUND"
            result["reason"] = (
                "Every strategy/timeframe combination failed at least one "
                "out-of-sample gate (return, Sharpe, drawdown, profit factor, "
                "trade count or parameter stability). This is a valid result.")

        # Cost / execution disclosure
        result.update({
            "capital": float(cfg.get("capital", 10_000)),
            "risk_pct": float(cfg.get("risk_pct", 1.0)),
            "max_leverage": _max_leverage(cfg, instrument.market_class),
            "costs": cfg.costs(instrument.market_class),
            "execution": {
                "fill": "next bar open ± (slippage + half spread)",
                "stops": "intrabar, stop-first pessimism, gap fills at open",
                "fees": "charged on entry and exit notional",
                "funding": ("real venue funding history where available "
                            "(Binance or Delta)"
                            if instrument.market == "perp" else "not applicable"),
                "leverage_cap": _max_leverage(cfg, instrument.market_class),
                "capital_constraint": "notional ≤ equity × max leverage",
            },
        })
        if instrument.market_class == "gold_futures":
            result["limitations"].append(
                "GC=F is a continuous series and the COMEX contract multiplier "
                "(100 oz) is not applied by default: position sizes are "
                "fractional ounces of notional exposure, not contract P&L.")
        if instrument.market_class == "gold_mcx":
            result["limitations"].append(
                "MCX results cover one configured contract, not a rolled "
                "continuous series.")
        if instrument.market_class in ("delta_perp", "delta_spot"):
            result["limitations"].append(
                "Delta Exchange India data is a separate venue from "
                "Binance/Kraken; results are not comparable across venues "
                "without accounting for fees, funding and liquidity.")
            if instrument.market == "perp":
                result["limitations"].append(
                    "Delta contract value is reported but the engine sizes "
                    "underlying units; exchange lot rounding is not applied.")
        if instrument.market == "perp" and champion and not champion.get("funding_used"):
            result["warnings"].append(
                "Perpetual funding history was unavailable; costs exclude "
                "funding, so net results are optimistic for held positions.")

        if champion:
            bench = champion.get("benchmark_oos") or {}
            if float((champion.get("oos") or {}).get("net_return") or 0) < \
                    float(bench.get("net_return") or 0):
                result["warnings"].append(
                    "The champion's out-of-sample net return is below buy & hold "
                    "on the same window; the ranking is risk-adjusted, not "
                    "return-maximising.")

        _progress(progress, "saving", 0.92, "saving reports")
        result["status"] = ("COMPLETED" if champion else
                            "NO_ROBUST_STRATEGY_FOUND")
        _finalise(result, job_id, cfg, started, peak_ram, instrument=instrument)

        # Champion ledger: promotion/rejection is decided from the evidence.
        if options.mode != "dry":
            store = ChampionStore(cfg)
            top = champion or (passing[0] if passing else
                               (sorted(candidates,
                                       key=lambda c: float(c.get("oos_score") or -1e9),
                                       reverse=True) or [{}])[0])
            if top:
                decision = store.decide(instrument.key, top, job_id)
                result["champion_decision"] = decision
        _progress(progress, "done", 1.0, "complete")
        _write_result(result, job_id)
        prune_reports(cfg=cfg)
        prune_cache(cfg=cfg)
        return result

    except JobCancelled:
        result.update({"status": "CANCELLED", "headline": "cancelled",
                       "reason": "the job was cancelled"})
        _finalise(result, job_id, cfg, started, peak_ram)
        return result
    except BudgetExceeded as exc:
        result.update({"status": "FAILED", "headline": "runtime budget exceeded",
                       "reason": str(exc)})
        _finalise(result, job_id, cfg, started, peak_ram)
        return result
    except Exception as exc:
        result.update({"status": "FAILED", "headline": "research failed",
                       "reason": f"{type(exc).__name__}: {exc}",
                       "traceback": traceback.format_exc()[-4000:]})
        _finalise(result, job_id, cfg, started, peak_ram)
        return result


def _finalise(result: dict[str, Any], job_id: str, cfg: Config,
              started: float, peak_ram: float, instrument=None) -> None:
    result["runtime_s"] = round(time.time() - started, 2)
    # The same warning can be emitted by several loop iterations (e.g. the
    # experiment budget); report each distinct caveat once.
    result["warnings"] = list(dict.fromkeys(result.get("warnings") or []))
    result["limitations"] = list(dict.fromkeys(result.get("limitations") or []))
    result["ram_peak_mb"] = round(peak_ram or _peak_rss_mb(), 1)
    result["limits"] = {
        "max_experiments": cfg.get("max_experiments"),
        "max_runtime_s": cfg.get("max_runtime_s"),
        "max_workers": cfg.get("max_workers"),
        "max_candles_per_series": cfg.get("max_candles_per_series"),
        "lookback_days": cfg.get("lookback_days"),
    }
    if instrument is not None:
        result["instrument"] = instrument.describe()
    _write_result(result, job_id)


def _write_result(result: dict[str, Any], job_id: str) -> None:
    try:
        paths = reports.write_artifacts(result)
        result["paths"] = paths
    except Exception as exc:
        result.setdefault("warnings", []).append(
            f"report writing failed: {type(exc).__name__}: {exc}")
    try:
        from orchestrator.agents.backtest.store import jobs_dir
        write_json(jobs_dir() / f"{job_id}.json", _json_ready(result))
    except Exception:
        pass


def _json_ready(value: Any) -> Any:
    """Strip pandas objects and cap huge lists before JSON serialisation."""
    if isinstance(value, pd.Series):
        return {"points": int(len(value)),
                "first": value.index[0].isoformat() if len(value) else "",
                "last": value.index[-1].isoformat() if len(value) else "",
                "final": float(value.iloc[-1]) if len(value) else None}
    if isinstance(value, pd.DataFrame):
        return {"rows": int(len(value))}
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        capped = list(value)[:1000]
        return [_json_ready(v) for v in capped]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def run_champion_check(symbol: str, market: str | None, cfg: Config,
                       *, recent_days: int = 90) -> dict[str, Any]:
    """Re-evaluate the saved champion on the most recent window."""
    resolution = symbols.resolve(symbol, market=market, cfg=cfg,
                                 allow_gold_default=True)
    if not resolution.ok:
        return {"ok": False, "reason": resolution.error or resolution.question}
    instrument = resolution.instrument
    store = ChampionStore(cfg)
    entry = store.get(instrument.key) or {}
    champ = entry.get("champion")
    if not champ:
        return {"ok": False, "reason": f"no champion for {instrument.key}"}

    spec = library.STRATEGIES.get(champ.get("strategy") or "")
    if not spec:
        return {"ok": False, "reason": "champion strategy is no longer registered"}
    tf = champ.get("timeframe") or "1d"
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(int(recent_days * 3), 400))
    registry = build_providers(cfg)
    providers = providers_for(instrument, cfg, registry)
    cache = CandleCache(cfg)
    frame = None
    used = ""
    for provider in providers:
        if tf not in provider.timeframes(instrument):
            continue
        res = cache.get_or_fetch(provider, instrument, tf, start, end,
                                 max_candles=int(cfg.get("max_candles_per_series",
                                                         600_000)))
        if res.ok and len(res.frame) > 100:
            frame, used = res.frame, provider.name
            break
    if frame is None:
        return {"ok": False, "reason": "could not refresh champion data"}

    ctx = library.StrategyContext(
        market_class=instrument.market_class, timeframe=tf,
        allow_short=bool(cfg.market_limit("allow_short",
                                         instrument.market_class, 0.0)),
        session_market=instrument.asset_class == "gold")
    costs = Costs(**cfg.costs(instrument.market_class))
    funding = _funding_for(registry, instrument, frame, cfg)
    recent_bars = int(recent_days * 86400 / tf_seconds(tf))
    recent_start = max(0, len(frame) - recent_bars)
    res = evaluate(frame, spec, champ.get("params") or {}, timeframe=tf,
                   ctx=ctx, costs=costs, capital=float(cfg.get("capital", 10_000)),
                   risk_pct=float(cfg.get("risk_pct", 1.0)),
                   max_leverage=_max_leverage(cfg, instrument.market_class),
                   trade_start_idx=recent_start,
                   multiplier=_multiplier(cfg, instrument.market_class),
                   funding=funding)
    metrics = res.get("metrics", {}) if res.get("ok") else {}
    stored = champ.get("oos") or {}
    degraded = False
    reasons: list[str] = []
    if metrics:
        if float(metrics.get("max_drawdown") or 0) > \
                max(0.25, 1.5 * float(stored.get("max_drawdown") or 0)):
            degraded = True
            reasons.append(
                f"recent drawdown {float(metrics.get('max_drawdown') or 0):.1%} "
                f"exceeds 1.5× the promoted drawdown")
        if float(metrics.get("net_return") or 0) < -0.10:
            degraded = True
            reasons.append(
                f"recent net return {float(metrics.get('net_return') or 0):.1%} "
                "is worse than -10%")
        if float(metrics.get("sharpe") or 0) < 0:
            degraded = True
            reasons.append("recent Sharpe is negative")
    else:
        degraded = True
        reasons.append("champion could not be evaluated on fresh data")

    out = {"ok": True, "instrument": instrument.describe(),
           "champion": {k: champ.get(k) for k in
                        ("strategy", "version", "timeframe", "params",
                         "oos", "oos_score", "promoted_at")},
           "provider": used, "recent_metrics": metrics,
           "stored_oos": stored, "degraded": degraded,
           "reasons": reasons, "recent_bars": int(len(frame)),
           "recent_window_start": frame.index[recent_start].isoformat()
           if recent_start < len(frame) else ""}
    if degraded:
        store.retire(instrument.key, "; ".join(reasons), job_id="champion-check")
        out["retired"] = True
    return out
