"""
orchestrator/agents/backtest/scheduler.py — bounded periodic research.

The scheduler is intentionally small. It answers three questions and stops:

    1. which watchlist instruments are DUE (interval elapsed)?
    2. is there NEW data since the last research run?
    3. is a champion still behaving on the most recent window?

It never starts more than `max_workers` research jobs, never queues the same
instrument twice, never researches gold without a saved market preference, and
never executes a trade. A systemd timer or the autonomy scheduler may call
`run_due()`; the function is also safe to call by hand or from the dashboard.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from orchestrator.agents.backtest import symbols
from orchestrator.agents.backtest.cache import CandleCache
from orchestrator.agents.backtest.champions import ChampionStore
from orchestrator.agents.backtest.config import Config, default_config
from orchestrator.agents.backtest.jobs import JobStore, start_job
from orchestrator.agents.backtest.providers import build_providers, providers_for
from orchestrator.agents.backtest.providers.base import tf_seconds
from orchestrator.agents.backtest.store import now_iso

KV_LAST = "schedule:last:"
KV_CHECK = "schedule:check:"


def _last_run(store: JobStore, key: str) -> float:
    value = store.kv_get(KV_LAST + key, 0)
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _new_data_since(instrument, cfg: Config, since_ts: float,
                    registry=None) -> dict[str, Any]:
    """Compare the newest *cached* completed candle with the last run time.

    The check is offline by design: it reads the cache sidecars, so a scheduler
    tick cannot hammer providers. A missing cache means "unknown → due", which
    is the safe direction (the worker re-verifies coverage anyway).
    """
    cache = CandleCache(cfg)
    chosen = ""
    last_ts = 0.0
    for tf in ("1d", "4h", "1h", "15m", "5m", "1m"):
        for provider in providers_for(instrument, cfg, registry
                                      or build_providers(cfg)):
            meta = cache.latest_cached(provider.name, instrument, tf)
            if not meta:
                continue
            ts = float(meta.get("last_ts") or 0)
            if ts > last_ts:
                last_ts, chosen = ts, f"{provider.name}:{tf}"
    if not last_ts:
        return {"known": False, "new_data": True, "detail": "no local cache"}
    age_h = (time.time() - last_ts) / 3600.0
    step_h = tf_seconds(chosen.rsplit(":", 1)[-1]) / 3600.0 if chosen else 24.0
    fresh = age_h <= max(step_h * 2.0, 48.0)
    return {
        "known": True,
        "new_data": bool(last_ts > since_ts) or fresh,
        "last_candle": now_iso(last_ts),
        "source": chosen,
        "age_hours": round(age_h, 2),
    }


def due_jobs(cfg: Config | None = None, *, now: float | None = None,
             store: JobStore | None = None) -> list[dict[str, Any]]:
    """The bounded plan: which instrument should run next, and why."""
    cfg = cfg or default_config()
    store = store or JobStore(cfg=cfg)
    interval_h = float(cfg.get("schedule_interval_hours", 168) or 168)
    now = time.time() if now is None else float(now)
    registry = build_providers(cfg)
    plan: list[dict[str, Any]] = []

    for symbol in (cfg.get("watchlist") or []):
        raw = str(symbol or "").strip().upper()
        if not raw:
            continue
        resolution = symbols.resolve(raw, cfg=cfg, allow_gold_default=True)
        if not resolution.ok or not resolution.instrument:
            plan.append({
                "symbol": raw, "due": False, "reason":
                resolution.error or resolution.question or "unresolved",
                "needs_input": bool(resolution.needs_input)})
            continue
        instrument = resolution.instrument
        last = _last_run(store, instrument.key)
        elapsed_h = (now - last) / 3600.0 if last else None
        data = _new_data_since(instrument, cfg, last, registry)
        due = last == 0 or elapsed_h >= interval_h
        reason = ("never researched" if last == 0 else
                  f"last run {elapsed_h:.1f}h ago (interval {interval_h:.0f}h)")
        if due and last and not data.get("new_data"):
            due = False
            reason = "interval elapsed but no new completed candle"
        plan.append({
            "symbol": instrument.symbol, "market": instrument.market,
            "key": instrument.key, "due": bool(due), "reason": reason,
            "last_run": now_iso(last) if last else "",
            "hours_since": round(elapsed_h, 2) if elapsed_h is not None else None,
            "data": data,
        })
    return plan


def run_due(cfg: Config | None = None, *, dry_run: bool = False,
            limit: int | None = None, source: str = "scheduler") -> dict[str, Any]:
    """Start the due jobs (bounded) and re-check champions. No orders."""
    cfg = cfg or default_config()
    store = JobStore(cfg=cfg)
    recovered = store.recover_stale()
    plan = due_jobs(cfg, store=store)

    active = store.list(limit=50, status="QUEUED") + \
        store.list(limit=50, status="RUNNING")
    slots = max(0, int(cfg.get("max_workers", 1)) - len(active))
    cap = int(limit if limit is not None else slots)
    cap = min(cap, slots)

    started: list[dict[str, Any]] = []
    if not dry_run:
        for item in plan:
            if not item.get("due") or cap <= 0:
                continue
            result = start_job(item["symbol"], item.get("market"), mode="scheduled",
                               source=source,
                               options={"scheduled": True, "notify": True},
                               cfg=cfg)
            if result.get("ok") and not result.get("deduplicated"):
                cap -= 1
                store.kv_set(KV_LAST + str(item["key"]), time.time())
            started.append({"symbol": item["symbol"],
                            "job_id": result.get("job_id"),
                            "ok": bool(result.get("ok")),
                            "deduplicated": bool(result.get("deduplicated")),
                            "error": result.get("error", "")})

    champions = champion_checks(cfg, dry_run=dry_run)
    return {
        "ok": True,
        "at": now_iso(),
        "dry_run": bool(dry_run),
        "recovered": recovered,
        "plan": plan,
        "started": started,
        "champions": champions,
        "limits": {"max_workers": cfg.get("max_workers", 1),
                   "active_before": len(active), "slots": slots,
                   "started": len([s for s in started if s.get("ok")])},
    }


def champion_checks(cfg: Config | None = None, *, dry_run: bool = False
                    ) -> list[dict[str, Any]]:
    """Re-evaluate every stored champion on the most recent window (bounded)."""
    cfg = cfg or default_config()
    store = JobStore(cfg=cfg)
    ledger = ChampionStore(cfg).all()
    out: list[dict[str, Any]] = []
    for key, entry in list(ledger.items())[:12]:
        champ = (entry or {}).get("champion")
        if not champ:
            continue
        symbol, _, market = str(key).partition(":")
        if dry_run:
            out.append({"key": key, "checked": False, "reason": "dry run"})
            continue
        try:
            from orchestrator.agents.backtest.research import run_champion_check
            res = run_champion_check(symbol, market or None, cfg)
        except Exception as exc:                    # never break the tick
            res = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        res["key"] = key
        out.append(res)
        store.kv_set(KV_CHECK + key, now_iso())
    return out


def schedule_status(cfg: Config | None = None) -> dict[str, Any]:
    cfg = cfg or default_config()
    store = JobStore(cfg=cfg)
    return {
        "enabled": bool(cfg.get("schedule_enabled", True)),
        "interval_hours": cfg.get("schedule_interval_hours", 168),
        "watchlist": list(cfg.get("watchlist") or []),
        "plan": due_jobs(cfg, store=store),
        "active_jobs": [j["job_id"] for j in
                        store.list(limit=50, status="QUEUED") +
                        store.list(limit=50, status="RUNNING")],
        "champions": sorted(ChampionStore(cfg).all().keys()),
    }
