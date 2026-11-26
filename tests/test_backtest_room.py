"""
tests/test_backtest_room.py — parameter plumbing + page contract for the
Backtest room (the research-lab form in RIO).

What this locks in:
  1. GET /api/backtest/options requires bearer auth and returns the real
     catalog (symbols, accepted intervals, reviewed strategies, limits).
  2. POST /api/backtest/run rejects bad parameters instead of clamping them,
     and forwards the selected set as job options + in-memory config
     overrides (history depth, capital, risk, budget).
  3. Existing clients that post only {symbol, market} keep the old defaults.
  4. Compact results used by the room strip per-candidate trade blobs; the
     existing full job endpoint is untouched.
  5. Artifacts are whitelisted and require the same auth.
  6. /backtest serves the shell with backtest.js, the asset is whitelisted,
     and /backtest is in the RIO route table.
  7. Config.apply_overrides never rewrites memory/backtest/config.json.
  8. Different parameters are different jobs (a changed selector must rerun).
  9. The detached worker applies overrides in memory only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard.backtest_api as bt_api  # noqa: E402
import orchestrator.agents.backtest.jobs as jobs_mod  # noqa: E402
import scripts.backtest_run as runner  # noqa: E402
from orchestrator.agents.backtest.config import (  # noqa: E402
    Config, default_config, reset_default_config,
)
from orchestrator.agents.backtest.jobs import JobStore  # noqa: E402
from orchestrator.agents.backtest.store import (  # noqa: E402
    jobs_dir, reports_dir, write_json,
)

AUTH = {"Authorization": "Bearer test"}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A fresh JARVIS_HOME + config singleton for every test."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    reset_default_config()
    yield tmp_path
    reset_default_config()


def _app(auth: bool = True) -> TestClient:
    app = FastAPI()
    bt_api.register(app, lambda request: auth)
    return TestClient(app)


def _run_payload(**updates):
    payload = {
        "symbol": "BTC", "market": "spot",
        "timeframes": ["15m", "1h"], "strategies": ["ema_cross", "rsi_meanrev"],
        "lookback_days": 730, "capital": 25_000, "risk_pct": 1.5,
        "max_experiments": 120, "max_runtime_s": 900,
        "force_refresh": True, "notify": False,
    }
    payload.update(updates)
    return payload


def _patch_start(monkeypatch):
    calls = []

    def fake_start(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True, "deduplicated": False,
                "job_id": "bt-20261021-0000000000"}

    monkeypatch.setattr(jobs_mod, "start_job", fake_start)
    return calls


# ── 1. catalog ───────────────────────────────────────────────────────────────

def test_options_requires_auth(home):
    c = _app(auth=False)
    assert c.get("/api/backtest/options").status_code == 401
    assert c.get("/api/backtest/options", headers=AUTH).status_code == 401


def test_options_catalog_shape(home):
    c = _app()
    r = c.get("/api/backtest/options", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert [s["symbol"] for s in body["symbols"]] == ["BTC", "ETH", "SOL", "GOLD"]
    assert body["symbols"][0]["markets"] == ["spot", "perp"]
    assert body["symbols"][3]["markets"] == ["xauusd", "futures", "mcx"]
    assert "15m" in body["timeframes"] and "1d" in body["timeframes"]
    keys = [s["key"] for s in body["strategies"]]
    assert "ema_cross" in keys and "buy_hold" not in keys
    assert all(s["markets"] for s in body["strategies"])
    d = body["defaults"]
    assert d["lookback_days"] >= 365
    assert d["notify"] is False
    assert d["max_candles_per_series"] == 600_000
    assert body["limits"]["lookback_days"] == [365, 3650]
    assert "crypto_spot" in body["costs"]
    assert body["costs"]["crypto_spot"]["fee_bps"] > 0


# ── 2-3. run validation + forwarding ────────────────────────────────────────

@pytest.mark.parametrize("patch,fragment", [
    ({"symbol": "DOGE"}, "symbol"),
    ({"market": "perp"}, "GOLD"),          # only meaningful with symbol=GOLD
    ({"timeframes": []}, "timeframe"),
    ({"timeframes": ["7m"]}, "unknown timeframe"),
    ({"strategies": []}, "strategy"),
    ({"strategies": ["not_a_strategy"]}, "unknown strategy"),
    ({"lookback_days": 10}, "lookback_days"),
    ({"lookback_days": 9999}, "lookback_days"),
    ({"capital": 0}, "capital"),
    ({"risk_pct": 0}, "risk_pct"),
    ({"max_experiments": 1}, "max_experiments"),
    ({"max_runtime_s": 5}, "max_runtime_s"),
])
def test_run_rejects_bad_parameters(home, monkeypatch, patch, fragment):
    payload = _run_payload(**patch)
    if payload.get("symbol") == "DOGE":
        fragment = "symbol"
    if "market" in patch:
        payload["symbol"] = "GOLD"       # market=perp is invalid for gold
        fragment = "market"
    calls = _patch_start(monkeypatch)
    c = _app()
    r = c.post("/api/backtest/run", json=payload, headers=AUTH)
    assert r.status_code == 400
    assert fragment.lower() in r.json()["error"].lower()
    assert calls == []


def test_run_forwards_options_and_overrides(home, monkeypatch):
    calls = _patch_start(monkeypatch)
    c = _app()
    r = c.post("/api/backtest/run", json=_run_payload(), headers=AUTH)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("BTC", "spot")
    assert kwargs["mode"] == "full"
    assert kwargs["source"] == "backtest_room"
    options = kwargs["options"]
    assert options["timeframes"] == ["15m", "1h"]
    assert options["strategies"] == ["ema_cross", "rsi_meanrev"]
    assert options["force_refresh"] is True
    assert options["notify"] is False
    over = options["config_overrides"]
    assert over["lookback_days"] == 730
    assert over["capital"] == 25_000
    assert over["risk_pct"] == 1.5
    assert over["max_experiments"] == 120
    assert over["max_runtime_s"] == 900
    # A "2 year" selection fetches 2 years for every interval, so a deeper
    # saved preference cannot leak into this job.
    assert set(over["history_days_by_timeframe"]) == {
        "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"}
    assert set(over["history_days_by_timeframe"].values()) == {730}


def test_run_defaults_keep_existing_clients(home, monkeypatch):
    calls = _patch_start(monkeypatch)
    c = _app()
    r = c.post("/api/backtest/run", json={"symbol": "BTC", "market": "spot"},
               headers=AUTH)
    assert r.status_code == 200
    options = calls[0][1]["options"]
    assert options["timeframes"] == []
    assert options["strategies"] == []
    assert options["notify"] is True          # old behavior preserved
    # No explicit execution knobs -> no overrides; the saved config (including
    # its per-interval history depths) is used exactly as before.
    assert options["config_overrides"] == {}


def test_gold_requires_market_choice(home, monkeypatch):
    calls = _patch_start(monkeypatch)
    c = _app()
    r = c.post("/api/backtest/run", json={"symbol": "GOLD"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["needs_input"] is True
    assert calls == []


# ── 4-5. result + artifacts ─────────────────────────────────────────────────

def test_compact_result_strips_trade_blobs(home):
    c = _app()
    created = JobStore().create("BTC", "spot",
                                options={"timeframes": ["15m"]})
    job_id = created["job_id"]
    full = {
        "ok": True, "status": "COMPLETED", "job_id": job_id,
        "ranking": [{"strategy": "ema_cross", "timeframe": "15m"}],
        "candidates": [{
            "strategy": "ema_cross", "timeframe": "15m",
            "trades": [{"pnl": 1}], "equity": [1, 2], "equity_curve": [1],
            "experiments": [{"params": {}}],
            "oos": {"net_return": 0.1}, "keep": "yes",
        }],
    }
    write_json(jobs_dir() / f"{job_id}.json", full)

    r = c.get(f"/api/backtest/result/{job_id}", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["job"]["job_id"] == job_id
    cand = body["result"]["candidates"][0]
    for heavy in ("trades", "equity", "equity_curve", "experiments"):
        assert heavy not in cand
    assert cand["oos"] == {"net_return": 0.1}
    assert cand["keep"] == "yes"

    # The existing full-result endpoint keeps its contract.
    full_res = c.get(f"/api/backtest/jobs/{job_id}", headers=AUTH)
    assert full_res.status_code == 200
    assert full_res.json()["result"]["candidates"][0]["trades"] == [{"pnl": 1}]


def test_result_endpoint_validation(home):
    c = _app(auth=False)
    assert c.get("/api/backtest/result/bt-20261021-0000000000").status_code == 401
    c = _app()
    assert c.get("/api/backtest/result/nope", headers=AUTH).status_code == 400
    assert c.get(
        "/api/backtest/result/bt-20261021-0000000000",
        headers=AUTH).status_code == 404


def test_artifacts_are_whitelisted(home):
    c = _app(auth=False)
    assert c.get(
        "/api/backtest/artifact/bt-20261021-0000000000/summary.md"
    ).status_code == 401
    c = _app()
    created = JobStore().create("BTC", "spot")
    job_id = created["job_id"]
    folder = reports_dir() / job_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "summary.md").write_text("# summary\n", encoding="utf-8")
    (folder / "secret.txt").write_text("nope", encoding="utf-8")

    ok = c.get(f"/api/backtest/artifact/{job_id}/summary.md", headers=AUTH)
    assert ok.status_code == 200
    assert "# summary" in ok.text
    assert c.get(
        f"/api/backtest/artifact/{job_id}/secret.txt",
        headers=AUTH).status_code == 404
    assert c.get(
        f"/api/backtest/artifact/{job_id}/missing.md",
        headers=AUTH).status_code == 404
    assert c.get(
        "/api/backtest/artifact/../../config.json/summary.md",
        headers=AUTH).status_code == 404


# ── 6. shell + assets — trading/backtest PAGES removed 2026-10-23; the
#       /api/backtest/* surface above stays mounted for the agents. ──────────

# ── 7. config overrides stay in memory ─────────────────────────────────────

def test_apply_overrides_does_not_touch_disk(home):
    path = home / "configs" / "cfg.json"
    cfg = Config(path)
    cfg.save({"lookback_days": 365, "capital": 10_000})
    before = json.loads(path.read_text(encoding="utf-8"))

    cfg.apply_overrides({
        "lookback_days": 730, "capital": 25_000,
        "history_days_by_timeframe": {"1h": 730},
    })
    assert cfg.get("lookback_days") == 730
    assert cfg.get("capital") == 25_000
    assert cfg.history_days("1h") == 730
    assert json.loads(path.read_text(encoding="utf-8")) == before


# ── 8. parameters are part of job identity ─────────────────────────────────

def test_different_parameters_are_different_jobs(home):
    store = JobStore()
    a = store.create("BTC", "spot", options={
        "timeframes": ["1h"], "config_overrides": {"lookback_days": 365}})
    b = store.create("BTC", "spot", options={
        "timeframes": ["1h"], "config_overrides": {"lookback_days": 730}})
    assert a["job_id"] != b["job_id"]
    same = store.create("BTC", "spot", options={
        "timeframes": ["1h"], "config_overrides": {"lookback_days": 365}})
    assert same["deduplicated"] is True
    assert same["job_id"] == a["job_id"]


# ── 9. worker applies overrides in memory ──────────────────────────────────

def test_worker_applies_overrides_without_saving(home, monkeypatch):
    cfg = default_config()
    before = cfg.path.read_text(encoding="utf-8") if cfg.path.exists() else None
    created = JobStore().create(
        "BTC", "spot",
        options={"notify": False,
                 "config_overrides": {
                     "lookback_days": 730, "capital": 25_000,
                     "history_days_by_timeframe": {"1h": 730}}})
    seen = {}

    def fake_run(job_id, symbol, market=None, *, options, cfg,
                 progress, cancel):
        seen["lookback"] = cfg.get("lookback_days")
        seen["capital"] = cfg.get("capital")
        seen["hist_1h"] = cfg.history_days("1h")
        seen["overrides"] = options.config_overrides
        return {"status": "COMPLETED", "headline": "ok", "experiments_total": 1}

    monkeypatch.setattr(runner, "run_research", fake_run)
    rc = runner.worker(created["job_id"])
    assert rc == 0
    assert seen["lookback"] == 730
    assert seen["capital"] == 25_000
    assert seen["hist_1h"] == 730
    assert seen["overrides"]["lookback_days"] == 730
    after = cfg.path.read_text(encoding="utf-8") if cfg.path.exists() else None
    assert after == before


# ── 10. Delta Exchange India venue ─────────────────────────────────────────

def test_delta_products_endpoint_is_separate_and_auth_guarded(home, monkeypatch):
    import orchestrator.agents.delta_exchange as dx

    monkeypatch.setattr(dx, "products_payload", lambda limit=500: {
        "ok": True, "status": "CONNECTED", "count": 2,
        "products": [{"symbol": "BTCUSD", "market_class": "delta_perp"},
                     {"symbol": "BTC_INR", "market_class": "delta_spot"}],
        "read_only": True, "mode": "public data + paper only"})
    c = _app()
    r = c.get("/api/backtest/delta-products", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["count"] == 2
    assert body["products"][0]["symbol"] == "BTCUSD"
    assert _app(auth=False).get(
        "/api/backtest/delta-products").status_code == 401


def test_delta_run_resolves_exact_product_before_start(home, monkeypatch):
    from orchestrator.agents.backtest.symbols import Instrument, Resolution

    calls = _patch_start(monkeypatch)
    inst = Instrument(
        symbol="BTCUSD", name="Bitcoin", asset_class="crypto", market="perp",
        market_class="delta_perp", exchange="Delta Exchange India",
        instrument_type="BTCUSD perpetual", quote_currency="USD",
        provider_symbols={"delta_india": "BTCUSD"})
    monkeypatch.setattr(
        "orchestrator.agents.backtest.symbols.resolve",
        lambda *a, **k: Resolution(ok=True, instrument=inst))
    c = _app()
    r = c.post("/api/backtest/run", headers=AUTH, json={
        "venue": "delta", "symbol": "BTCUSD", "market": "delta_perp",
        "timeframes": ["1h"], "strategies": ["ema_cross"],
        "lookback_days": 365, "notify": False})
    assert r.status_code == 200, r.text
    assert calls, "the job must start after exact Delta resolution"
    args, kwargs = calls[0]
    assert args[0] == "BTCUSD"
    assert args[1] == "delta_perp"
    assert kwargs["options"]["venue"] == "delta"


def test_delta_ambiguous_product_returns_real_choices(home, monkeypatch):
    from orchestrator.agents.backtest.symbols import Resolution

    calls = _patch_start(monkeypatch)
    monkeypatch.setattr(
        "orchestrator.agents.backtest.symbols.resolve",
        lambda *a, **k: Resolution(
            ok=False, needs_input=True,
            question="Which exact Delta product?",
            options=("BTCUSD", "BTC_INR"),
            error="'BTC' matches more than one Delta product"))
    c = _app()
    r = c.post("/api/backtest/run", headers=AUTH, json={
        "venue": "delta", "symbol": "BTC", "market": "",
        "timeframes": ["1h"], "strategies": ["ema_cross"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["needs_input"] is True
    assert body["options"] == ["BTCUSD", "BTC_INR"]
    assert not calls, "ambiguity must never start a job"
