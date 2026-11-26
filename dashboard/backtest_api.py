"""
dashboard/backtest_api.py — REST surface for the backtest research lab.

Endpoints (bearer auth via the dashboard's _auth, like every /api route):

    GET  /api/backtest/status                  jobs + champions + schedule
    GET  /api/backtest/options                 parameter catalog for the room
    GET  /api/backtest/jobs?symbol=&limit=
    GET  /api/backtest/jobs/{job_id}           one job + full result if present
    GET  /api/backtest/result/{job_id}         compact result (room: no trades)
    GET  /api/backtest/artifact/{job_id}/{name} summary/report/result/CSV
    POST /api/backtest/run                     {symbol, market?, timeframes?,
                                                strategies?, lookback_days?,
                                                capital?, risk_pct?,
                                                max_experiments?, notify?}
    POST /api/backtest/cancel                  {job_id}
    GET  /api/backtest/report/{job_id}?kind=summary|detailed
    GET  /api/backtest/champions
    POST /api/backtest/rollback                {key}
    POST /api/backtest/gold-market             {market}
    GET  /api/backtest/schedule
    POST /api/backtest/schedule                {dry_run?: true}

Research only: there is deliberately no order, broker or execution endpoint.
Starting a job returns immediately — the worker runs detached.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

# Intervals the lab can test (same set research.collect_data accepts).
_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d")
_JOB_RE = re.compile(r"^bt-\d{8}-[0-9a-f]{10}$")
_ARTIFACTS = ("summary.md", "report.md", "result.json", "experiments.csv")

# The room never stores a study; these bounds only stop a typo from asking
# the worker for something absurd (a 100-year fetch, 1e9 experiments).
_LIMITS = {
    "lookback_days": (365, 3650),
    "capital": (100.0, 10_000_000.0),
    "risk_pct": (0.1, 100.0),
    "max_experiments": (8, 1000),
    "max_runtime_s": (60, 7200),
}


def _deny() -> JSONResponse:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


def _agent():
    from orchestrator.agents.backtest.backtest_agent import BacktestAgent
    return BacktestAgent()


def _body(payload) -> dict:
    return payload if isinstance(payload, dict) else {}


def _num(payload, key, default, lo: float, hi: float):
    """(value, error) for one numeric parameter. Reject, never clamp: a
    silently-changed history depth would make the result unattributable."""
    raw = payload.get(key, default)
    if raw is None or raw == "":
        raw = default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, f"{key} must be a number"
    if not (lo <= value <= hi):
        return None, f"{key} must be between {lo:g} and {hi:g}"
    return value, ""


def _int_param(payload, key, default, lo: int, hi: int):
    value, err = _num(payload, key, default, lo, hi)
    return (None, err) if err else (int(value), "")


def _compact_result(result):
    """The room's result view needs metrics, not every simulated trade.

    `trades` (and any future equity series) are what make result.json ~1.6 MB;
    the ranking/coverage/champion payload is ~250 KB. Artifacts stay available
    for the full detail."""
    if not isinstance(result, dict):
        return result
    heavy = {"trades", "equity", "equity_curve", "experiments"}
    out = dict(result)
    out["candidates"] = [
        {k: v for k, v in (c or {}).items() if k not in heavy}
        for c in (result.get("candidates") or [])
    ]
    return out


def _parameter_catalog() -> dict:
    from orchestrator.agents.backtest.config import default_config
    from orchestrator.agents.backtest.strategies import library

    cfg = default_config()
    strategies = []
    for spec in library.describe_all():
        if spec["key"] == "buy_hold":
            continue
        strategies.append({
            "key": spec["key"],
            "name": spec["name"],
            "description": spec["description"],
            "markets": list(spec["markets"]),
            "timeframes": list(spec["timeframes"]),
            "intraday_only": bool(spec["intraday_only"]),
        })
    return {
        "ok": True,
        "symbols": [
            {"symbol": "BTC", "name": "Bitcoin", "markets": ["spot", "perp"]},
            {"symbol": "ETH", "name": "Ethereum", "markets": ["spot", "perp"]},
            {"symbol": "SOL", "name": "Solana", "markets": ["spot", "perp"]},
            {"symbol": "GOLD", "name": "Gold",
             "markets": ["xauusd", "futures", "mcx"]},
        ],
        "venues": [
            {"key": "auto", "label": "Auto (Binance / Kraken / Yahoo)"},
            {"key": "delta", "label": "Delta Exchange India"},
        ],
        "delta": {
            "base_url": "https://api.india.delta.exchange",
            "product_endpoint": "/api/backtest/delta-products",
            "read_only": True,
            "mode": "public data + paper only",
            "note": ("Live Delta products are loaded from a separate endpoint "
                     "so this parameter form still renders when the venue is "
                     "unreachable."),
        },
        "timeframes": list(_TIMEFRAMES),
        "strategies": strategies,
        "defaults": {
            "symbol": "BTC",
            "market": "spot",
            "lookback_days": int(cfg.get("lookback_days", 365)),
            "timeframes": list(cfg.get("timeframes") or []),
            "capital": float(cfg.get("capital", 10_000)),
            "risk_pct": float(cfg.get("risk_pct", 1.0)),
            "max_experiments": int(cfg.get("max_experiments", 240)),
            "max_runtime_s": int(cfg.get("max_runtime_s", 1800)),
            "max_candles_per_series": int(cfg.get("max_candles_per_series",
                                                 600_000)),
            "notify": False,
            "force_refresh": False,
        },
        "limits": {key: list(bounds) for key, bounds in _LIMITS.items()},
        "costs": {mc: cfg.costs(mc) for mc in
                  ("crypto_spot", "crypto_perp", "delta_spot", "delta_perp",
                   "gold_xauusd", "gold_futures", "gold_mcx")},
        "allow_short": {mc: bool(cfg.market_limit("allow_short", mc, 0.0))
                        for mc in ("crypto_spot", "crypto_perp", "delta_spot",
                                   "delta_perp", "gold_xauusd",
                                   "gold_futures", "gold_mcx")},
        "gold_market": cfg.gold_market(),
    }


def register(app, auth_check) -> None:
    """Attach /api/backtest/* endpoints to the running FastAPI app."""

    @app.get("/api/backtest/status")
    async def backtest_status(req: Request):
        if not auth_check(req):
            return _deny()
        try:
            payload = _agent().status_payload()
            payload["cache_mb"] = _cache_mb()
            payload["notifications"] = _notification_tail()
            return JSONResponse(payload)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)},
                                status_code=200)

    @app.get("/api/backtest/options")
    async def backtest_options(req: Request):
        """Everything the Backtest room needs to render its parameter form.

        Read-only catalog: symbols/markets, the intervals the lab accepts,
        the reviewed strategy registry (with each strategy's market/timeframe
        suitability) and the current saved defaults. No job is created."""
        if not auth_check(req):
            return _deny()
        try:
            return JSONResponse(_parameter_catalog())
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)},
                                status_code=200)

    @app.get("/api/backtest/delta-products")
    async def backtest_delta_products(req: Request):
        """Live Delta Exchange India perps + spot for the venue selector.

        Read-only public catalog. A venue outage returns ok:false with the
        reason; it never substitutes a cached Binance symbol list.
        """
        if not auth_check(req):
            return _deny()
        try:
            from orchestrator.agents.delta_exchange import products_payload
            return JSONResponse(products_payload(limit=500))
        except Exception as exc:
            return JSONResponse({
                "ok": False, "status": "DISCONNECTED", "products": [],
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            }, status_code=200)

    @app.get("/api/backtest/jobs")
    async def backtest_jobs(req: Request, symbol: str = "", limit: int = 20):
        if not auth_check(req):
            return _deny()
        from orchestrator.agents.backtest.jobs import JobStore
        jobs = JobStore().list(limit=max(1, min(int(limit), 100)),
                               symbol=(symbol or "").upper() or None)
        return JSONResponse({"ok": True, "jobs": jobs})

    @app.get("/api/backtest/jobs/{job_id}")
    async def backtest_job(req: Request, job_id: str):
        if not auth_check(req):
            return _deny()
        from orchestrator.agents.backtest.jobs import JobStore
        from orchestrator.agents.backtest.store import jobs_dir, read_json
        job = JobStore().get(job_id)
        if not job:
            return JSONResponse({"ok": False, "error": "unknown job"},
                                status_code=404)
        result = read_json(jobs_dir() / f"{job_id}.json", None)
        return JSONResponse({"ok": True, "job": job, "result": result})

    @app.get("/api/backtest/result/{job_id}")
    async def backtest_result(req: Request, job_id: str):
        """Result for the room: metrics + ranking + coverage, no trade blobs."""
        if not auth_check(req):
            return _deny()
        if not _JOB_RE.match(job_id or ""):
            return JSONResponse({"ok": False, "error": "invalid job id"},
                                status_code=400)
        from orchestrator.agents.backtest.jobs import JobStore
        from orchestrator.agents.backtest.store import jobs_dir, read_json
        job = JobStore().get(job_id)
        if not job:
            return JSONResponse({"ok": False, "error": "unknown job"},
                                status_code=404)
        result = read_json(jobs_dir() / f"{job_id}.json", None)
        return JSONResponse({"ok": True, "job": job,
                             "result": _compact_result(result)})

    @app.get("/api/backtest/artifact/{job_id}/{name}")
    async def backtest_artifact(req: Request, job_id: str, name: str):
        """Download one whitelisted report artifact (summary/report/JSON/CSV)."""
        if not auth_check(req):
            return _deny()
        if not _JOB_RE.match(job_id or "") or name not in _ARTIFACTS:
            return JSONResponse({"ok": False, "error": "not found"},
                                status_code=404)
        from orchestrator.agents.backtest.store import reports_dir
        path = reports_dir() / job_id / name
        if not path.exists():
            return JSONResponse({"ok": False, "error": "artifact not written"},
                                status_code=404)
        media = ("text/markdown" if name.endswith(".md") else
                 "application/json" if name.endswith(".json") else
                 "text/csv")
        return FileResponse(str(path), media_type=media, filename=name)

    @app.post("/api/backtest/run")
    async def backtest_run(req: Request):
        if not auth_check(req):
            return _deny()
        try:
            payload = _body(await req.json())
        except Exception:
            payload = {}
        from orchestrator.agents.backtest import symbols as _symbols
        from orchestrator.agents.backtest.config import default_config
        from orchestrator.agents.backtest.jobs import start_job
        from orchestrator.agents.backtest.strategies import library as _library

        raw_symbol = str(payload.get("symbol") or "").strip()
        venue = str(payload.get("venue") or "").strip().lower()
        market = str(payload.get("market") or "").strip().lower()
        venue_raw = venue
        cfg = default_config()

        # ── Delta Exchange India: resolve the exact live product first ────
        # The job key/report must name the real instrument (BTCUSD, BTC_INR),
        # not a base-asset guess. Ambiguity comes back as a real choice.
        if venue in ("delta", "delta_india", "delta_perp",
                     "delta_spot") or market in ("delta_perp", "delta_spot"):
            venue = "delta"
            if market in ("", "auto"):
                market = (venue_raw if venue_raw in ("delta_perp", "delta_spot")
                          else "")
            elif market in ("perp", "perpetual"):
                market = "delta_perp"
            elif market == "spot":
                market = "delta_spot"
            elif market not in ("delta_perp", "delta_spot"):
                return JSONResponse({
                    "ok": False,
                    "error": ("Delta market must be delta_perp, delta_spot, "
                              "perp or spot")}, status_code=400)
            resolution = _symbols.resolve(raw_symbol, market=market, cfg=cfg,
                                          venue="delta")
            if not resolution.ok:
                return JSONResponse({
                    "ok": False,
                    "needs_input": bool(resolution.needs_input),
                    "error": resolution.error or resolution.question,
                    "options": list(resolution.options),
                }, status_code=200 if resolution.needs_input else 400)
            symbol = resolution.instrument.symbol
            market = resolution.instrument.market_class
        else:
            symbol = _symbols.extract_symbol(raw_symbol)
            if not symbol:
                return JSONResponse({
                    "ok": False,
                    "error": ("symbol must be BTC, ETH, SOL or GOLD, or a "
                              "Delta product when venue=delta")},
                    status_code=400)
            allowed_markets = {
                "BTC": ("spot", "perp"), "ETH": ("spot", "perp"),
                "SOL": ("spot", "perp"),
                "GOLD": ("xauusd", "futures", "mcx"),
            }[symbol]
            if market and market not in allowed_markets:
                return JSONResponse({
                    "ok": False,
                    "error": f"{symbol} market must be one of "
                             f"{', '.join(allowed_markets)}"}, status_code=400)
            if symbol == "GOLD" and not market and not cfg.gold_market():
                return JSONResponse({
                    "ok": False, "needs_input": True,
                    "error": "choose the gold market once",
                    "options": list(allowed_markets)}, status_code=200)

        # ── timeframes (an explicit empty list is a user error, not "all") ──
        timeframes: list[str] = []
        if "timeframes" in payload:
            raw_tfs = payload.get("timeframes")
            if not isinstance(raw_tfs, list) or not raw_tfs:
                return JSONResponse({"ok": False,
                                     "error": "select at least one timeframe"},
                                    status_code=400)
            for raw in raw_tfs:
                tf = str(raw or "").strip().lower()
                if tf not in _TIMEFRAMES:
                    return JSONResponse({"ok": False,
                                         "error": f"unknown timeframe '{tf}'"},
                                        status_code=400)
                if tf not in timeframes:
                    timeframes.append(tf)

        # ── strategies (keys only; the registry owns the parameter grids) ──
        strategies: list[str] = []
        if "strategies" in payload:
            raw_strats = payload.get("strategies")
            if not isinstance(raw_strats, list) or not raw_strats:
                return JSONResponse({"ok": False,
                                     "error": "select at least one strategy"},
                                    status_code=400)
            known = set(_library.STRATEGIES) - {"buy_hold"}
            for raw in raw_strats:
                key = str(raw or "").strip().lower()
                if key not in known:
                    return JSONResponse({"ok": False,
                                         "error": f"unknown strategy '{key}'"},
                                        status_code=400)
                if key not in strategies:
                    strategies.append(key)

        # ── numeric knobs (reject out-of-range; never silently clamp) ─────
        lookback, err = _int_param(
            payload, "lookback_days", int(cfg.get("lookback_days", 365)),
            *_LIMITS["lookback_days"])
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        capital, err = _num(payload, "capital",
                            float(cfg.get("capital", 10_000)),
                            *_LIMITS["capital"])
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        risk_pct, err = _num(payload, "risk_pct",
                             float(cfg.get("risk_pct", 1.0)),
                             *_LIMITS["risk_pct"])
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        max_experiments, err = _int_param(
            payload, "max_experiments", int(cfg.get("max_experiments", 240)),
            *_LIMITS["max_experiments"])
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        max_runtime_s, err = _int_param(
            payload, "max_runtime_s", int(cfg.get("max_runtime_s", 1800)),
            *_LIMITS["max_runtime_s"])
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)

        # A job is one exact study: the requested depth becomes BOTH the
        # one-year floor and the fetch depth for every interval, so a "1 year"
        # selection cannot silently fetch five years of 1h candles. Only keys
        # the caller actually sent become overrides — existing clients that
        # post just {symbol, market} keep the saved per-interval history.
        overrides: dict = {}
        if "lookback_days" in payload:
            overrides["lookback_days"] = int(lookback)
            overrides["history_days_by_timeframe"] = {
                tf: int(lookback) for tf in _TIMEFRAMES}
        if "capital" in payload:
            overrides["capital"] = float(capital)
        if "risk_pct" in payload:
            overrides["risk_pct"] = float(risk_pct)
        if "max_experiments" in payload:
            overrides["max_experiments"] = int(max_experiments)
        if "max_runtime_s" in payload:
            overrides["max_runtime_s"] = int(max_runtime_s)
        options = {
            "timeframes": timeframes,
            "strategies": strategies,
            "force_refresh": bool(payload.get("force_refresh")),
            "notify": bool(payload.get("notify", True)),
            "config_overrides": overrides,
        }
        if venue == "delta":
            options["venue"] = "delta"
        res = start_job(symbol, market or None, mode="full",
                        source="backtest_room", options=options)
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    @app.post("/api/backtest/cancel")
    async def backtest_cancel(req: Request):
        if not auth_check(req):
            return _deny()
        try:
            payload = _body(await req.json())
        except Exception:
            payload = {}
        job_id = str(payload.get("job_id") or "").strip().lower()
        if not job_id:
            return JSONResponse({"ok": False, "error": "job_id required"},
                                status_code=400)
        from orchestrator.agents.backtest.jobs import JobStore
        return JSONResponse(JobStore().cancel(job_id))

    @app.get("/api/backtest/report/{job_id}")
    async def backtest_report(req: Request, job_id: str, kind: str = "summary"):
        if not auth_check(req):
            return _deny()
        from orchestrator.agents.backtest.reports import read_report
        text = read_report(job_id, "detailed" if kind == "detailed" else "summary")
        if not text:
            return JSONResponse({"ok": False, "error": "no report for that job"},
                                status_code=404)
        return JSONResponse({"ok": True, "job_id": job_id, "kind": kind,
                             "markdown": text})

    @app.get("/api/backtest/champions")
    async def backtest_champions(req: Request):
        if not auth_check(req):
            return _deny()
        from orchestrator.agents.backtest.champions import ChampionStore
        return JSONResponse({"ok": True,
                             "champions": ChampionStore().all()})

    @app.post("/api/backtest/rollback")
    async def backtest_rollback(req: Request):
        if not auth_check(req):
            return _deny()
        try:
            payload = _body(await req.json())
        except Exception:
            payload = {}
        key = str(payload.get("key") or "").strip()
        if not key:
            return JSONResponse({"ok": False, "error": "key required"},
                                status_code=400)
        from orchestrator.agents.backtest.champions import ChampionStore
        res = ChampionStore().rollback(key, reason="dashboard rollback")
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    @app.post("/api/backtest/gold-market")
    async def backtest_gold_market(req: Request):
        if not auth_check(req):
            return _deny()
        try:
            payload = _body(await req.json())
        except Exception:
            payload = {}
        market = str(payload.get("market") or "").strip().lower()
        from orchestrator.agents.backtest.config import default_config
        try:
            saved = default_config().set_gold_market(market)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)},
                                status_code=400)
        return JSONResponse({"ok": True, "gold_market": saved.get("gold_market")})

    @app.get("/api/backtest/schedule")
    async def backtest_schedule(req: Request):
        if not auth_check(req):
            return _deny()
        from orchestrator.agents.backtest import scheduler
        return JSONResponse(scheduler.schedule_status())

    @app.post("/api/backtest/schedule")
    async def backtest_schedule_run(req: Request):
        if not auth_check(req):
            return _deny()
        try:
            payload = _body(await req.json())
        except Exception:
            payload = {}
        from orchestrator.agents.backtest import scheduler
        out = scheduler.run_due(dry_run=bool(payload.get("dry_run", True)))
        return JSONResponse(out)


def _cache_mb() -> float:
    try:
        from orchestrator.agents.backtest.store import cache_size_mb
        return round(cache_size_mb(), 1)
    except Exception:
        return 0.0


def _notification_tail(limit: int = 20) -> list[dict]:
    try:
        from orchestrator.agents.backtest.store import outbox_path
        path = Path(outbox_path())
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception:
        return []


__all__ = ["register"]
