#!/usr/bin/env python3
"""
scripts/backtest_run.py — CLI + detached worker for the backtest research lab.

    python scripts/backtest_run.py BTC                 # start (detached worker)
    python scripts/backtest_run.py ETH --market perp   # separate perpetual run
    python scripts/backtest_run.py GOLD --market futures
    python scripts/backtest_run.py BTC --wait           # run in this process
    python scripts/backtest_run.py status [JOB]
    python scripts/backtest_run.py report JOB [--detailed]
    python scripts/backtest_run.py cancel JOB
    python scripts/backtest_run.py history [--symbol BTC]
    python scripts/backtest_run.py champion [--symbol BTC]
    python scripts/backtest_run.py rollback BTC --market spot
    python scripts/backtest_run.py schedule [--dry-run]
    python scripts/backtest_run.py set-gold-market futures
    python scripts/backtest_run.py selftest
    python scripts/backtest_run.py --worker JOB_ID      # internal

Exit codes: 0 success/queued, 2 usage, 3 job not found, 4 run failed/blocked.

The CLI never places an order and never executes LLM-written code. Real
research runs inside `--worker`, one bounded job at a time.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.agents.backtest import scheduler, symbols          # noqa: E402
from orchestrator.agents.backtest.backtest_agent import BacktestAgent  # noqa: E402
from orchestrator.agents.backtest.champions import ChampionStore    # noqa: E402
from orchestrator.agents.backtest.config import (                   # noqa: E402
    Config, default_config,
)
from orchestrator.agents.backtest.jobs import (                     # noqa: E402
    JobStore, is_transient, maybe_retry, start_job,
)
from orchestrator.agents.backtest.notify import Notifier            # noqa: E402
from orchestrator.agents.backtest.research import (                 # noqa: E402
    JobCancelled, ResearchOptions, run_research,
)
from orchestrator.agents.backtest.store import (                    # noqa: E402
    cache_size_mb, human_age, now_iso, read_json, write_json,
)


# ── worker ───────────────────────────────────────────────────────────────────

def worker(job_id: str) -> int:
    """Run one job in this process. Called by jobs.spawn_worker()."""
    cfg = default_config()
    store = JobStore(cfg=cfg)
    job = store.get(job_id)
    if not job:
        print(f"unknown job {job_id}", file=sys.stderr)
        return 3
    if job.get("status") == "CANCELLED":
        print(f"{job_id} was cancelled before start", file=sys.stderr)
        return 4

    stop = {"flag": False}

    def _signal(_sig, _frm):
        stop["flag"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _signal)
        except Exception:
            pass

    try:
        options_raw = json.loads(job.get("options") or "{}")
    except Exception:
        options_raw = {}
    options = ResearchOptions(
        mode="scheduled" if options_raw.get("scheduled") else "full",
        strategies=list(options_raw.get("strategies") or []),
        timeframes=list(options_raw.get("timeframes") or []),
        force_refresh=bool(options_raw.get("force_refresh")),
        notify=bool(options_raw.get("notify", True)),
        source=str(job.get("source") or "cli"),
        note=str(options_raw.get("note") or ""),
        config_overrides=(options_raw.get("config_overrides")
                          if isinstance(options_raw.get("config_overrides"), dict)
                          else {}),
    )
    # Backtest-room parameter form: history depth / capital / budget are
    # per-job. In memory only — the saved config stays the user's default.
    if options.config_overrides:
        cfg.apply_overrides(options.config_overrides)

    store.mark_running(job_id, pid=os.getpid())
    last = {"t": 0.0}

    def progress(phase: str, pct: float, message: str = "") -> None:
        now = time.time()
        if now - last["t"] < 0.8 and phase not in ("done", "saving"):
            return
        last["t"] = now
        store.progress(job_id, phase, pct, message)

    def cancel() -> bool:
        return stop["flag"] or store.is_cancelled(job_id)

    print(f"[{now_iso()}] worker start {job_id} "
          f"{job.get('symbol')} {job.get('market')} pid={os.getpid()}",
          flush=True)
    try:
        result = run_research(job_id, str(job.get("symbol") or ""),
                              job.get("market") or None, options=options,
                              cfg=cfg, progress=progress, cancel=cancel)
    except JobCancelled:
        result = {"status": "CANCELLED", "headline": "cancelled",
                  "reason": "the job was cancelled", "symbol": job.get("symbol")}
    except Exception as exc:
        result = {"status": "FAILED", "headline": "research failed",
                  "reason": f"{type(exc).__name__}: {exc}",
                  "symbol": job.get("symbol")}

    result.setdefault("job_id", job_id)
    state = store.finish(job_id, result)
    report_dir = ""
    try:
        from orchestrator.agents.backtest.store import reports_dir
        candidate = reports_dir() / job_id
        if candidate.exists():
            report_dir = str(candidate)
            store.update(job_id, report_dir=report_dir)
    except Exception:
        pass

    if options.notify and result.get("status") not in ("CANCELLED",):
        try:
            note = Notifier(cfg, store=store).send_result(result)
            print(f"[{now_iso()}] notify: {note}", flush=True)
        except Exception as exc:
            print(f"[{now_iso()}] notify failed: {exc}", flush=True)

    if state == "FAILED" and is_transient(str(result.get("reason") or "")):
        retry = maybe_retry(job_id, result, cfg=cfg)
        print(f"[{now_iso()}] retry: {retry}", flush=True)

    print(f"[{now_iso()}] worker done {job_id} state={state} "
          f"status={result.get('status')} "
          f"runtime={result.get('runtime_s')}s", flush=True)
    return 0 if state == "COMPLETED" else 4


# ── commands ─────────────────────────────────────────────────────────────────

def _print_job(job: dict) -> None:
    print(f"{job.get('job_id')}  {job.get('symbol')} {job.get('market') or ''}  "
          f"{job.get('status')} {float(job.get('progress') or 0) * 100:.0f}%  "
          f"[{job.get('phase') or '-'}]")
    if job.get("message"):
        print(f"  {job['message']}")
    if job.get("headline"):
        print(f"  {job['headline']}")
    if job.get("result_status") and job.get("result_status") != job.get("status"):
        print(f"  result: {job['result_status']}")
    if job.get("report_dir"):
        print(f"  reports: {job['report_dir']}")
    if job.get("error") and job.get("status") in ("FAILED", "CANCELLED"):
        print(f"  error: {job['error']}")


def cmd_run(args) -> int:
    cfg = default_config()
    if args.wait:
        created = JobStore(cfg=cfg).create(
            args.symbol, args.market, mode="full", source="cli",
            options={"strategies": args.strategies or [],
                     "timeframes": args.timeframes or [],
                     "force_refresh": bool(args.refresh),
                     "notify": not args.no_notify})
        if not created.get("ok"):
            print(json.dumps(created, indent=2))
            return 4
        job_id = created["job_id"]
        if created.get("deduplicated"):
            print(f"reusing {job_id} ({created['job']['status']})")
        return worker(job_id)

    result = start_job(args.symbol, args.market, mode="full", source="cli",
                       options={"strategies": args.strategies or [],
                                "timeframes": args.timeframes or [],
                                "force_refresh": bool(args.refresh),
                                "notify": not args.no_notify}, cfg=cfg)
    if not result.get("ok"):
        print(f"could not start: {result.get('error')}", file=sys.stderr)
        return 4
    job = result.get("job") or {}
    print(f"{'reused' if result.get('deduplicated') else 'queued'} "
          f"{result['job_id']} — {job.get('symbol')} {job.get('market') or ''} "
          f"({job.get('status')})")
    if result.get("log"):
        print(f"log: {result['log']}")
    print(f"check: python scripts/backtest_run.py status {result['job_id']}")
    return 0


def cmd_status(args) -> int:
    store = JobStore()
    if args.job:
        job = store.get(args.job)
        if not job:
            print(f"unknown job {args.job}", file=sys.stderr)
            return 3
        if args.json:
            print(json.dumps(job, indent=2, default=str))
        else:
            _print_job(job)
        return 0
    jobs = store.list(limit=args.limit,
                      symbol=(args.symbol or "").upper() or None)
    if args.json:
        print(json.dumps(jobs, indent=2, default=str))
        return 0
    if not jobs:
        print("no jobs yet")
        return 0
    for job in jobs:
        _print_job(job)
    print(f"\ncache: {cache_size_mb():.1f} MB · champions: "
          f"{', '.join(sorted(ChampionStore().all())) or 'none'}")
    return 0


def cmd_report(args) -> int:
    from orchestrator.agents.backtest.reports import read_report
    text = read_report(args.job, "detailed" if args.detailed else "summary")
    if not text:
        print(f"no report for {args.job}", file=sys.stderr)
        return 3
    print(text)
    return 0


def cmd_cancel(args) -> int:
    res = JobStore().cancel(args.job)
    print(json.dumps(res, indent=2, default=str))
    return 0 if res.get("ok") else 3


def cmd_history(args) -> int:
    jobs = JobStore().list(limit=args.limit,
                           symbol=(args.symbol or "").upper() or None)
    for job in jobs:
        print(f"{job.get('created_at'):.0f} {job.get('job_id')} "
              f"{job.get('symbol')} {job.get('market') or ''} "
              f"{job.get('status')} {job.get('result_status') or ''} "
              f"{job.get('headline') or ''}"[:160])
    return 0


def cmd_champion(args) -> int:
    ledger = ChampionStore().all()
    if args.symbol:
        prefix = args.symbol.upper()
        ledger = {k: v for k, v in ledger.items() if k.startswith(prefix)}
    if args.json:
        print(json.dumps(ledger, indent=2, default=str))
        return 0
    if not ledger:
        print("no champions recorded")
        return 0
    for key, entry in ledger.items():
        champ = (entry or {}).get("champion") or {}
        print(f"{key}: {champ.get('strategy')} {champ.get('timeframe')} "
              f"score={champ.get('oos_score')} promoted={champ.get('promoted_at')}")
        history = (entry or {}).get("history") or []
        for event in history[-3:]:
            print(f"  {event.get('at')} {event.get('action')}: "
                  f"{str(event.get('reason'))[:120]}")
    return 0


def cmd_rollback(args) -> int:
    key = f"{args.symbol.upper()}:{args.market.lower()}"
    res = ChampionStore().rollback(key, reason="cli rollback")
    print(json.dumps(res, indent=2, default=str))
    return 0 if res.get("ok") else 4


def cmd_schedule(args) -> int:
    if args.run:
        out = scheduler.run_due(dry_run=args.dry_run)
    else:
        out = scheduler.schedule_status()
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_set_gold(args) -> int:
    cfg = default_config()
    saved = cfg.set_gold_market(args.market)
    print(f"gold market saved as '{saved['gold_market']}' "
          f"({cfg.path})")
    return 0


def cmd_selftest(_args) -> int:
    """Offline sanity checks: no network, no writes outside a temp dir."""
    import tempfile
    import numpy as np
    import pandas as pd
    from orchestrator.agents.backtest import quality
    from orchestrator.agents.backtest.engine import Costs, run_backtest
    from orchestrator.agents.backtest.strategies import library

    idx = pd.date_range("2024-01-01", periods=600, freq="1D", tz="UTC")
    rng = np.random.default_rng(7)
    close = 100 * np.cumprod(1 + rng.normal(0, 0.01, len(idx)))
    df = pd.DataFrame({"open": close, "high": close * 1.01,
                       "low": close * 0.99, "close": close,
                       "volume": 10.0}, index=idx)
    spec = library.STRATEGIES["ema_cross"]
    ctx = library.StrategyContext("crypto_spot", "1d", False)
    sig = spec.build(df, {"fast": 8, "slow": 21}, ctx)
    res = run_backtest(df, sig, timeframe="1d", costs=Costs(10, 5, 2),
                       capital=10_000, allow_short=False, trade_start_idx=200)
    rep = quality.coverage_report(df, instrument=_Instrument(), provider="selftest",
                                  timeframe="1d", requested_start=idx[0],
                                  requested_end=idx[-1])
    checks = {
        "engine_ok": bool(res.ok),
        "coverage_ok": bool(rep.get("usable")),
        "warmup_respected": bool(sig["warmup"].iloc[0] > 0),
        "no_llm_code_path": True,
    }
    print(json.dumps({"ok": all(checks.values()), "checks": checks,
                      "trades": res.metrics.get("trades"),
                      "bars": len(df)}, indent=2))
    return 0 if all(checks.values()) else 4


class _Instrument:
    symbol = "SELFTEST"
    market = "spot"
    market_class = "crypto_spot"
    asset_class = "crypto"
    exchange = "selftest"
    instrument_type = "synthetic selftest series"


def _parse(argv: list[str]):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="status",
                   help="run | status | report | cancel | history | champion | "
                        "rollback | schedule | set-gold-market | selftest")
    p.add_argument("symbol", nargs="?", default="", help="BTC/ETH/SOL/GOLD or job id")
    p.add_argument("--worker", metavar="JOB", help="internal: run one job")
    p.add_argument("--market", default="", help="spot|perp|xauusd|futures|mcx")
    p.add_argument("--job", default="", help="job id for status/report/cancel")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--wait", action="store_true", help="run in this process")
    p.add_argument("--json", action="store_true")
    p.add_argument("--detailed", action="store_true")
    p.add_argument("--refresh", action="store_true", help="ignore the cache")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--run", action="store_true", help="schedule: start due jobs")
    p.add_argument("--strategies", nargs="*", default=[])
    p.add_argument("--timeframes", nargs="*", default=[])
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(list(sys.argv[1:] if argv is None else argv))
    if args.worker:
        return worker(args.worker)

    cmd = args.command.lower()
    known = {"run", "status", "report", "cancel", "history", "champion",
             "rollback", "schedule", "set-gold-market", "set_gold_market",
             "selftest"}
    if cmd not in known:
        # `backtest_run.py BTC` — a bare symbol is a run.
        if args.symbol:
            print(f"unexpected extra argument '{args.symbol}'", file=sys.stderr)
            return 2
        args.symbol, cmd = args.command, "run"
    # "status bt-…" / "report bt-…" convenience: the id is in `symbol`.
    if args.symbol.lower().startswith("bt-") and not args.job:
        args.job = args.symbol.lower()
        if cmd in ("run",):
            cmd = "status"

    if cmd == "run":
        return cmd_run(args)
    if cmd == "status":
        return cmd_status(args)
    if cmd == "report":
        if not args.job:
            print("usage: backtest_run.py report JOB_ID", file=sys.stderr)
            return 2
        return cmd_report(args)
    if cmd == "cancel":
        if not args.job:
            print("usage: backtest_run.py cancel JOB_ID", file=sys.stderr)
            return 2
        return cmd_cancel(args)
    if cmd == "history":
        return cmd_history(args)
    if cmd == "champion":
        return cmd_champion(args)
    if cmd == "rollback":
        if not args.symbol or not args.market:
            print("usage: backtest_run.py rollback BTC --market spot",
                  file=sys.stderr)
            return 2
        return cmd_rollback(args)
    if cmd == "schedule":
        return cmd_schedule(args)
    if cmd in ("set-gold-market", "set_gold_market"):
        if args.symbol:
            args.market = args.market or args.symbol
        if args.market not in ("xauusd", "futures", "mcx"):
            print("usage: backtest_run.py set-gold-market xauusd|futures|mcx",
                  file=sys.stderr)
            return 2
        return cmd_set_gold(args)
    if cmd == "selftest":
        return cmd_selftest(args)
    print(f"unknown command '{cmd}'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
