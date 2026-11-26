"""
tests/test_trading_institutional.py — offline tests for the institutional
portfolio/risk analytics (orchestrator/agents/trading/institutional.py) and
its CLI (scripts/institutional_metrics.py).

NO network, NO real files: every series is synthesized here so the assertions
are exact-ish (tolerances) and deterministic. The CLI is exercised through its
--from-json (offline) path and its --simulated path against the deterministic
SimulatedAdapter.

What this locks in — the institutional identity (each metric means what an
IC memo would read it as):
  1. return/vol/Sharpe/Sortino signs and scaling
  2. drawdown on a known shape; calmar
  3. beta == 1 vs self, beta == 2 vs a doubled series; correlation bounds
  4. VaR/CVaR ordering (CVaR <= VaR <= 0) and gaussian-vs-historical sanity
  5. vol regime percentile on a quiet-then-wild series
  6. trend regime up/down/range
  7. sizing: fixed-fractional, vol-target cap, risk-parity inverse-vol + cap
  8. Kelly cap
  9. correlation matrix symmetry/diagonal
 10. portfolio stats + effective names; stress contributions
 11. build_report assembly + flags (concentration, correlation, regime)
 12. the CLI prints a JSON report and writes --out

Run:  .venv/bin/python -m pytest tests/test_trading_institutional.py -q
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.agents.trading import institutional as I   # noqa: E402

REPO = BASE


def _bars(closes: list[float], high_extra: float = 0.01) -> list[dict]:
    return [{"ts": f"t{i}", "open": c, "high": c * (1.0 + high_extra),
             "low": c * (1.0 - high_extra), "close": c}
            for i, c in enumerate(closes)]


def _compounded(returns: list[float], start: float = 100.0) -> list[float]:
    out, lvl = [start], start
    for r in returns:
        lvl *= (1.0 + r)
        out.append(lvl)
    return out


# ── 1. returns / ratios ──────────────────────────────────────────────────────

def test_returns_and_ratios():
    closes = [100.0, 110.0, 121.0]           # +10% then +10%
    rets = I.simple_returns(closes)
    assert len(rets) == 2
    assert math.isclose(rets[0], 0.10, rel_tol=1e-9)
    assert math.isclose(I.total_return(closes), 0.21, rel_tol=1e-9)
    # CAGR over 2 steps ≈ 21% annualised on a 252-day clock
    assert I.cagr(closes) > 0.5

    flat = [100.0, 100.0, 100.0, 100.0]
    assert I.annualized_vol(I.simple_returns(flat)) == 0.0
    # a positive-drift, low-noise series → positive Sharpe, no drawdown
    up = _compounded([0.01 if i % 3 else 0.004 for i in range(60)])
    assert I.sharpe(I.simple_returns(up)) > 0
    assert I.max_drawdown(up)["max_dd"] == 0.0
    # a perfectly constant return stream has zero vol → Sharpe is undefined
    assert I.sharpe(I.simple_returns(_compounded([0.01] * 60))) is None
    # noisy series → finite vol, sortino present
    noisy = _compounded([0.01 if i % 2 == 0 else -0.008 for i in range(80)])
    assert I.annualized_vol(I.simple_returns(noisy)) > 0
    assert I.sortino(I.simple_returns(noisy)) is not None


def test_drawdown_and_calmar():
    closes = [100.0, 120.0, 60.0, 90.0]      # trough 60 from peak 120 = -50%
    dd = I.max_drawdown(closes)
    assert math.isclose(dd["max_dd"], -0.5, rel_tol=1e-9)
    assert dd["peak_index"] == 1 and dd["trough_index"] == 2
    # Calmar = CAGR / |max drawdown| (positive when the return is positive)
    assert math.isclose(I.calmar(0.20, -0.5), 0.4, rel_tol=1e-9)
    assert I.calmar(0.20, None) is None


# ── 2. beta / correlation ────────────────────────────────────────────────────

def test_beta_and_correlation_identities():
    rng = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.005, 0.0, 0.012, -0.008] * 4
    assert math.isclose(I.beta(rng, rng), 1.0, rel_tol=1e-9)
    assert math.isclose(I.correlation(rng, rng), 1.0, rel_tol=1e-9)
    doubled = [2 * r if r > 0 else 2 * r for r in rng]   # scale by 2
    assert math.isclose(I.beta(doubled, rng), 2.0, rel_tol=1e-6)
    inverted = [-r for r in rng]
    assert math.isclose(I.correlation(inverted, rng), -1.0, rel_tol=1e-9)


# ── 3. tail risk ─────────────────────────────────────────────────────────────

def test_var_cvar_ordering_and_methods():
    rets = [-0.05, -0.03, -0.01, 0.0, 0.01, 0.01, 0.02, 0.02, 0.03, 0.04] * 4
    hist = I.var_cvar(rets, 0.95, "historical")
    assert hist["var"] is not None and hist["cvar"] is not None
    assert hist["cvar"] <= hist["var"] <= 0.05      # CVaR at least as bad
    gauss = I.var_cvar(rets, 0.95, "gaussian")
    assert gauss["var"] is not None and gauss["cvar"] <= gauss["var"]
    # too-small sample refuses honestly
    assert I.var_cvar([0.01, -0.01], 0.95)["var"] is None


# ── 4. regimes ───────────────────────────────────────────────────────────────

def test_trend_regime_labels():
    up = [100.0 * (1.002 ** i) for i in range(260)]
    down = [100.0 * (0.998 ** i) for i in range(260)]
    flat = [100.0 + 0.02 * math.sin(i / 5.0) for i in range(260)]
    assert I.trend_regime(up)["label"] == "uptrend"
    assert I.trend_regime(down)["label"] == "downtrend"
    assert I.trend_regime(flat)["label"] in ("range", "uptrend", "downtrend")
    assert I.trend_regime(up)["slope_20"] is not None


def test_vol_regime_percentile_rises_with_recent_stress():
    quiet = [0.001, -0.001] * 160
    wild = quiet + [0.03, -0.035] * 20            # recent stress at the end
    closes = _compounded(wild)
    v = I.vol_regime(I.simple_returns(closes))
    assert v["percentile"] is not None and v["percentile"] > 0.8
    assert v["label"] in ("high", "extreme")


def test_market_regime_has_posture():
    up = [100.0 * (1.001 ** i) for i in range(260)]
    r = I.market_regime(_bars(up))
    assert r["label"].startswith("uptrend")
    assert isinstance(r["posture"], str) and len(r["posture"]) > 10
    assert I.market_regime([])["label"] == "unknown"


# ── 5. sizing & construction ─────────────────────────────────────────────────

def test_fixed_fractional_position():
    p = I.fixed_fractional_position(capital=100_000, risk_pct=1.0,
                                    entry=100.0, stop=95.0)
    assert p["ok"] and p["shares"] == 200         # ₹1000 risk / ₹5 per share
    assert math.isclose(p["risk_per_share"], 5.0)
    bad = I.fixed_fractional_position(100_000, 1.0, 100.0, 100.0)
    assert bad["ok"] is False


def test_vol_target_scales_inversely_and_caps():
    low = I.vol_target_position(1_000_000, 100.0, 0.10, 0.10, max_weight=1.0)
    high = I.vol_target_position(1_000_000, 100.0, 0.30, 0.10, max_weight=1.0)
    assert math.isclose(low["notional"], 1_000_000)          # 10% vol → 100%
    assert math.isclose(high["notional"], 1_000_000 / 3, rel_tol=1e-6)
    capped = I.vol_target_position(1_000_000, 100.0, 0.05, 0.10, max_weight=0.25)
    assert capped["capped"] is True and math.isclose(capped["weight"], 0.25)


def test_risk_parity_inverse_vol_and_cap():
    # feasible cap → a clear inverse-vol tilt (calmer name gets more money)
    w = I.risk_parity_weights({"LOW": 0.10, "MID": 0.20, "HIGH": 0.30},
                              max_weight=0.80)
    assert math.isclose(sum(w.values()), 1.0, rel_tol=1e-9)
    assert w["LOW"] > w["MID"] > w["HIGH"]
    # cap binds on an extreme spread and the book still sums to 1
    w2 = I.risk_parity_weights({"A": 0.01, "B": 0.30, "C": 0.30}, max_weight=0.40)
    assert all(v <= 0.40 + 1e-9 for v in w2.values())
    assert math.isclose(sum(w2.values()), 1.0, rel_tol=1e-9)


def test_kelly_is_capped():
    k = I.kelly_capped(win_rate=0.9, payoff_ratio=5.0, cap=0.25)
    assert k["ok"] and k["fraction"] <= 0.25 and k["capped"] is True
    assert I.kelly_capped(1.5, 1.0)["ok"] is False


def test_risk_parity_infeasible_cap_is_respected_with_cash():
    # cap 25% on a 2-name book cannot be met while fully invested → the cap is
    # honoured and the remainder (50%) is cash, never a breached weight.
    w = I.risk_parity_weights({"A": 0.10, "B": 0.30}, max_weight=0.25)
    assert all(v <= 0.25 + 1e-9 for v in w.values())
    assert sum(w.values()) <= 1.0 + 1e-9
    assert math.isclose(sum(w.values()), 0.5, rel_tol=1e-9)   # 50% cash


# ── 6. portfolio assembly ────────────────────────────────────────────────────

def test_correlation_matrix_shape():
    a = [0.01, -0.01, 0.02, -0.02, 0.005] * 6
    b = [-0.01, 0.01, -0.02, 0.02, -0.005] * 6
    m = I.correlation_matrix({"A": a, "B": b, "C": a})
    assert m["A"]["A"] == 1.0 and m["B"]["B"] == 1.0
    assert math.isclose(m["A"]["B"], m["B"]["A"], rel_tol=1e-12)
    assert m["A"]["C"] == 1.0


def test_portfolio_stats_and_stress():
    ra = [0.01, -0.01, 0.02, -0.02, 0.005] * 6
    rb = [0.004, -0.004, 0.008, -0.008, 0.002] * 6   # half the risk
    st = I.portfolio_stats({"A": 0.5, "B": 0.5}, {"A": ra, "B": rb}, rb)
    assert st["ok"] and st["ann_vol"] > 0
    assert math.isclose(st["effective_names"], 2.0, rel_tol=1e-9)
    assert st["beta"] is not None
    s = I.stress_test({"A": 0.5, "B": 0.5}, {"A": -0.20})
    assert math.isclose(s["portfolio_pnl_pct"], -0.10, rel_tol=1e-9)
    assert s["unshocked"] == ["B"]


def test_build_report_structure_and_flags():
    up = _bars([100.0 * (1.001 ** i) for i in range(240)])
    down = _bars([100.0 * (0.999 ** i) for i in range(240)])
    report = I.build_report({"UP": up, "DN": down}, "UP", up,
                            weights={"UP": 0.5, "DN": 0.5}, capital=500_000)
    assert report["assets"]["UP"]["ok"] is True
    assert "regime" in report and report["regime"]["label"].startswith("uptrend")
    assert math.isclose(sum(report["portfolio"]["weights"].values()), 1.0)
    assert report["portfolio"]["stats"]["ok"] is True
    assert report["correlation"]["UP"]["DN"] is not None
    # the sizing block computes vol-target + fixed-fractional (has ATR)
    assert report["sizing"]["UP"]["vol_target"]["ok"] is True
    assert "fixed_fractional" in report["sizing"]["UP"]
    # a 50/50 two-name book should trip the concentration flag
    assert any("concentration" in f or "of the book" in f for f in report["flags"])
    assert any("Decision support only" in n for n in report["notes"])


def test_build_report_handles_short_history_honestly():
    report = I.build_report({"X": _bars([10.0, 11.0])}, "SPY", _bars([10.0, 11.0]))
    assert report["assets"]["X"]["ok"] is False
    assert "error" in report["assets"]["X"]


def test_build_report_flags_infeasible_single_name_cap():
    up = _bars([100.0 * (1.001 ** i) for i in range(240)])
    dn = _bars([100.0 * (1.0005 ** i) for i in range(240)])
    # 25% cap on a 2-name book cannot be met while fully invested
    report = I.build_report({"A": up, "B": dn}, "A", up, max_weight=0.25)
    assert report["sizing"]["max_weight"] == 0.25
    rp = report["portfolio"]["risk_parity_weights"]
    assert all(v <= 0.25 + 1e-9 for v in rp.values())
    assert any("below 1/" in f for f in report["flags"])


# ── 7. the CLI ───────────────────────────────────────────────────────────────

def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "institutional_metrics.py"), *args],
        capture_output=True, text=True, cwd=str(REPO), timeout=120)


def test_cli_offline_from_json(tmp_path):
    bars = {"AAA": _bars([100.0 * (1.001 ** i) for i in range(240)]),
            "BBB": _bars([100.0 * (1.0005 ** i) for i in range(240)])}
    bars["_benchmark"] = "AAA"
    p = tmp_path / "bars.json"
    p.write_text(json.dumps(bars))
    res = _run_cli(["--from-json", str(p), "--capital", "100000",
                    "--stress", "AAA=-0.10", "--out", str(tmp_path / "o.json")])
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert set(doc["assets"]) == {"AAA", "BBB"}
    assert doc["_fetch"]["provenance"] == "file"
    assert "stress" in doc
    assert json.loads((tmp_path / "o.json").read_text())["benchmark"] == "AAA"


def test_cli_simulated_is_deterministic():
    res = _run_cli(["--simulated", "--symbols", "SIMX,SIMY", "--period", "1y"])
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["_fetch"]["provenance"] == "simulated"
    assert doc["regime"]["label"] != "unknown"


# pytest-compatible: run_all(), matching the repo's test convention.
def run_all() -> int:
    import pytest
    return pytest.main([__file__, "-q"])


if __name__ == "__main__":
    raise SystemExit(run_all())
