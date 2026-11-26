"""
orchestrator/agents/trading/factor_model.py — factor models & scenario stress testing.

WHY THIS EXISTS
    institutional.py gives portfolio risk from *own* returns (VaR, beta, correlation).
    A factor model asks: *what drives these returns?* — decomposing P&L into market,
    size, value, momentum, quality, etc. exposures. That lets you:
      • Attribute performance to factor tilts vs. alpha
      • Stress test against factor shocks (not just single-name shocks)
      • Build factor-neutral / factor-tilted portfolios
      • Run historical scenario analysis (2008, 2020 COVID, etc.) via factor returns

CONTRACT
    All functions are pure arithmetic over return series (no network, no LLM).
    Factor data must be supplied by the caller (fetched elsewhere, e.g. Ken French
    library, Bloomberg, or synthetic). Missing data → None, never a guess.
"""

from __future__ import annotations

import math
import random
import statistics
from datetime import datetime, timezone
from typing import Optional

import numpy as np

# ── OLS helpers (stdlib only) ──────────────────────────────────────────────────

def _ols_simple(y: list[float], X: list[list[float]]) -> dict:
    """
    OLS: y = X @ beta + eps
    Returns: {"beta": [...], "alpha": ..., "r2": ..., "t_stats": [...], "residuals": [...]}
    """
    if not y or not X or len(y) != len(X[0]):
        return {"beta": [], "alpha": None, "r2": None, "t_stats": [], "residuals": []}

    n = len(y)
    k = len(X)
    if n <= k + 1:
        return {"beta": [], "alpha": None, "r2": None, "t_stats": [], "residuals": []}

    # Add intercept column
    X_mat = np.column_stack([np.ones(n)] + X)
    y_vec = np.array(y)

    try:
        # (X'X)^-1 X'y
        beta_full = np.linalg.lstsq(X_mat, y_vec, rcond=None)[0]
        alpha = float(beta_full[0])
        betas = beta_full[1:].tolist()

        y_hat = X_mat @ beta_full
        residuals = (y_vec - y_hat).tolist()

        ss_res = sum(r * r for r in residuals)
        ss_tot = sum((yi - statistics.fmean(y)) ** 2 for yi in y)
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None

        # Standard errors for t-stats
        mse = ss_res / (n - k - 1) if n > k + 1 else None
        if mse and mse > 0:
            XtX_inv = np.linalg.inv(X_mat.T @ X_mat)
            se = [math.sqrt(max(0.0, mse * XtX_inv[i + 1, i + 1])) for i in range(k)]
            t_stats = [b / s if s > 0 else None for b, s in zip(betas, se)]
        else:
            t_stats = [None] * k

        return {
            "alpha": alpha,
            "beta": betas,
            "r2": r2,
            "t_stats": t_stats,
            "residuals": residuals,
        }
    except Exception:
        return {"beta": [], "alpha": None, "r2": None, "t_stats": [], "residuals": []}


# ── Factor data structures ─────────────────────────────────────────────────────

FACTOR_NAMES = [
    "mkt_rf",   # Market excess return (MKT - RF)
    "smb",      # Small minus Big (size factor)
    "hml",      # High minus Low (value factor)
    "rmw",      # Robust minus Weak (profitability/quality)
    "cma",      # Conservative minus Aggressive (investment)
    "mom",      # Momentum (optional, Carhart 4-factor)
]

# Historical scenario factor returns (approximate, for stress testing)
# Values are 1-period (daily) factor shocks in decimal
HISTORICAL_SCENARIOS = {
    "2008_financial_crisis": {
        "description": "Global Financial Crisis (Sep-Oct 2008)",
        "period_days": 40,
        "factor_shocks": {
            "mkt_rf": -0.25,   # Market down ~25% over period
            "smb": -0.05,      # Small caps underperformed
            "hml": 0.08,       # Value outperformed growth
            "rmw": 0.03,
            "cma": 0.02,
            "mom": -0.15,      # Momentum crash
        },
    },
    "2020_covid_crash": {
        "description": "COVID-19 Crash (Feb-Mar 2020)",
        "period_days": 22,
        "factor_shocks": {
            "mkt_rf": -0.34,   # S&P 500 down ~34% in a month
            "smb": -0.12,      # Small caps hit harder
            "hml": -0.05,      # Growth (tech) outperformed
            "rmw": 0.05,       # Quality/profitability helped
            "cma": -0.03,
            "mom": -0.10,
        },
    },
    "2022_bear_market": {
        "description": "2022 Rate Hike Bear Market",
        "period_days": 252,
        "factor_shocks": {
            "mkt_rf": -0.18,
            "smb": -0.02,
            "hml": 0.12,       # Value strongly outperformed
            "rmw": 0.08,
            "cma": 0.05,
            "mom": -0.05,
        },
    },
    "2010_flash_crash": {
        "description": "Flash Crash (May 6, 2010) — single day",
        "period_days": 1,
        "factor_shocks": {
            "mkt_rf": -0.09,
            "smb": -0.05,
            "hml": 0.01,
            "rmw": 0.00,
            "cma": 0.00,
            "mom": -0.08,
        },
    },
    "2011_european_debt": {
        "description": "European Debt Crisis (Aug 2011)",
        "period_days": 20,
        "factor_shocks": {
            "mkt_rf": -0.17,
            "smb": -0.08,
            "hml": 0.03,
            "rmw": 0.02,
            "cma": 0.01,
            "mom": -0.12,
        },
    },
    "2024_tech_rotation": {
        "description": "Tech Rotation / AI Boom (2024)",
        "period_days": 60,
        "factor_shocks": {
            "mkt_rf": 0.12,
            "smb": -0.05,      # Mega-cap tech led
            "hml": -0.10,      # Growth >> Value
            "rmw": 0.08,       # Quality/Profitability rewarded
            "cma": -0.02,
            "mom": 0.15,       # Strong momentum in tech
        },
    },
}


# ── Factor regression ──────────────────────────────────────────────────────────

def factor_regression(
    asset_returns: list[float],
    factor_returns: dict[str, list[float]],
    factors: Optional[list[str]] = None,
) -> dict:
    """
    Run multi-factor regression: asset ~ alpha + sum(beta_i * factor_i)

    Args:
        asset_returns: Asset return series (decimal)
        factor_returns: Dict of factor name -> return series (same length)
        factors: Which factors to use (default: all available in factor_returns)

    Returns:
        dict with alpha, betas, r2, t_stats, residuals, factor_names
    """
    if not asset_returns:
        return {"ok": False, "error": "empty asset returns"}

    factors = factors or [f for f in FACTOR_NAMES if f in factor_returns]
    available = [f for f in factors if f in factor_returns]
    if not available:
        return {"ok": False, "error": "no factor data available"}

    # Align lengths (use common tail)
    n = len(asset_returns)
    for f in available:
        n = min(n, len(factor_returns[f]))
    if n < 20:
        return {"ok": False, "error": f"insufficient aligned data: {n} periods"}

    y = asset_returns[-n:]
    X = [factor_returns[f][-n:] for f in available]

    result = _ols_simple(y, X)

    return {
        "ok": True,
        "alpha": result["alpha"],
        "betas": dict(zip(available, result["beta"])),
        "r2": result["r2"],
        "t_stats": dict(zip(available, result["t_stats"])),
        "residuals": result["residuals"],
        "factor_names": available,
        "n_obs": n,
    }


def factor_attribution(
    portfolio_returns: list[float],
    factor_returns: dict[str, list[float]],
    weights: dict[str, float],
    asset_factor_betas: dict[str, dict[str, float]],
) -> dict:
    """
    Decompose portfolio returns into factor contributions + alpha.

    Args:
        portfolio_returns: Portfolio return series
        factor_returns: Factor return series
        weights: Portfolio weights per asset
        asset_factor_betas: {asset: {factor: beta}} from factor_regression

    Returns:
        dict with factor_contributions, alpha_contribution, r2
    """
    if not portfolio_returns or not weights or not asset_factor_betas:
        return {"ok": False, "error": "missing inputs"}

    # Portfolio-level factor betas = weighted average of asset betas
    port_betas = {}
    for factor in FACTOR_NAMES:
        wb = 0.0
        total_w = 0.0
        for asset, w in weights.items():
            b = (asset_factor_betas.get(asset) or {}).get(factor)
            if b is not None:
                wb += w * b
                total_w += abs(w)
        if total_w > 0:
            port_betas[factor] = wb / total_w

    # Run portfolio-level regression
    factors_present = [f for f in port_betas if f in factor_returns]
    if not factors_present:
        return {"ok": False, "error": "no common factors"}

    n = len(portfolio_returns)
    for f in factors_present:
        n = min(n, len(factor_returns[f]))

    reg = factor_regression(portfolio_returns[-n:], {f: factor_returns[f][-n:] for f in factors_present})
    if not reg.get("ok"):
        return reg

    # Factor contributions to return
    contributions = {}
    for f in factors_present:
        beta = reg["betas"].get(f, 0.0)
        factor_mean = statistics.fmean(factor_returns[f][-n:]) if factor_returns[f] else 0.0
        contributions[f] = beta * factor_mean

    alpha_contrib = reg["alpha"] or 0.0
    total_pred = alpha_contrib + sum(contributions.values())
    actual_mean = statistics.fmean(portfolio_returns[-n:])

    return {
        "ok": True,
        "factor_contributions": contributions,
        "alpha_contribution": alpha_contrib,
        "predicted_mean": total_pred,
        "actual_mean": actual_mean,
        "r2": reg["r2"],
        "portfolio_betas": port_betas,
    }


# ── Scenario stress testing ────────────────────────────────────────────────────

def scenario_stress_test(
    weights: dict[str, float],
    asset_factor_betas: dict[str, dict[str, float]],
    scenario_name: str,
    custom_shocks: Optional[dict[str, float]] = None,
) -> dict:
    """
    Stress test portfolio against a historical factor scenario.

    Args:
        weights: Portfolio weights per asset
        asset_factor_betas: {asset: {factor: beta}}
        scenario_name: Key from HISTORICAL_SCENARIOS
        custom_shocks: Optional override {factor: shock}

    Returns:
        dict with portfolio_pnl_pct, per_asset_pnl, factor_contributions
    """
    if scenario_name not in HISTORICAL_SCENARIOS and not custom_shocks:
        return {"ok": False, "error": f"unknown scenario: {scenario_name}"}

    shocks = custom_shocks or HISTORICAL_SCENARIOS[scenario_name]["factor_shocks"]

    per_asset = {}
    total_pnl = 0.0
    for asset, w in weights.items():
        betas = asset_factor_betas.get(asset) or {}
        asset_pnl = 0.0
        for factor, beta in betas.items():
            shock = shocks.get(factor, 0.0)
            asset_pnl += beta * shock
        per_asset[asset] = {"pnl_pct": asset_pnl, "weight": w, "betas": betas}
        total_pnl += w * asset_pnl

    # Factor-level contributions
    factor_contrib = {}
    for factor, shock in shocks.items():
        contrib = 0.0
        for asset, w in weights.items():
            beta = (asset_factor_betas.get(asset) or {}).get(factor, 0.0)
            contrib += w * beta * shock
        factor_contrib[factor] = contrib

    return {
        "ok": True,
        "scenario": scenario_name,
        "description": HISTORICAL_SCENARIOS.get(scenario_name, {}).get("description", "Custom"),
        "period_days": HISTORICAL_SCENARIOS.get(scenario_name, {}).get("period_days"),
        "portfolio_pnl_pct": total_pnl,
        "per_asset_pnl": per_asset,
        "factor_contributions": factor_contrib,
        "shocks_applied": shocks,
    }


def monte_carlo_stress_test(
    weights: dict[str, float],
    asset_factor_betas: dict[str, dict[str, float]],
    factor_mean: dict[str, float],
    factor_cov: dict[str, dict[str, float]],
    n_sims: int = 10_000,
    horizon_days: int = 21,
    confidence_levels: tuple = (0.95, 0.99, 0.999),
) -> dict:
    """
    Monte Carlo stress test using factor model.

    Generates correlated factor paths, maps to asset returns via betas,
    then computes portfolio P&L distribution.

    Args:
        weights: Portfolio weights
        asset_factor_betas: {asset: {factor: beta}}
        factor_mean: {factor: daily_mean_return}
        factor_cov: {factor: {factor: covariance}} — daily covariance matrix
        n_sims: Number of simulations
        horizon_days: Stress horizon
        confidence_levels: VaR levels to report

    Returns:
        dict with VaR/CVaR at confidence levels, full distribution stats
    """
    if not weights or not asset_factor_betas:
        return {"ok": False, "error": "missing inputs"}

    factors = sorted(set(factor_mean.keys()) & set(factor_cov.keys()))
    if not factors:
        return {"ok": False, "error": "no common factors in mean/cov"}

    # Build covariance matrix
    k = len(factors)
    cov_mat = np.zeros((k, k))
    for i, fi in enumerate(factors):
        for j, fj in enumerate(factors):
            cov_mat[i, j] = factor_cov[fi].get(fj, 0.0)
    mean_vec = np.array([factor_mean[f] for f in factors])

    # Portfolio factor betas
    port_betas = np.zeros(k)
    for i, f in enumerate(factors):
        wb = 0.0
        total_w = 0.0
        for asset, w in weights.items():
            beta = (asset_factor_betas.get(asset) or {}).get(f)
            if beta is not None:
                wb += w * beta
                total_w += abs(w)
        if total_w > 0:
            port_betas[i] = wb / total_w

    # Monte Carlo
    rng = np.random.default_rng()
    pnls = []
    for _ in range(n_sims):
        # Generate correlated factor shocks over horizon
        # Scale daily mean/cov by horizon
        z = rng.multivariate_normal(mean_vec * horizon_days, cov_mat * horizon_days)
        # Portfolio P&L = sum(beta_i * factor_shock_i)
        pnl = float(port_betas @ z)
        pnls.append(pnl)

    pnls = np.array(pnls)
    pnls.sort()

    result = {
        "ok": True,
        "n_sims": n_sims,
        "horizon_days": horizon_days,
        "mean_pnl": float(np.mean(pnls)),
        "std_pnl": float(np.std(pnls)),
        "min_pnl": float(pnls[0]),
        "max_pnl": float(pnls[-1]),
    }

    for cl in confidence_levels:
        idx = int((1.0 - cl) * n_sims)
        var = float(pnls[idx])
        cvar = float(np.mean(pnls[:idx + 1])) if idx >= 0 else var
        result[f"var_{int(cl*100)}"] = var
        result[f"cvar_{int(cl*100)}"] = cvar

    return result


def factor_risk_decomposition(
    weights: dict[str, float],
    asset_factor_betas: dict[str, dict[str, float]],
    factor_cov: dict[str, dict[str, float]],
) -> dict:
    """
    Decompose portfolio factor risk (variance) into factor contributions.

    Returns variance explained by each factor + cross-terms.
    """
    factors = sorted(set(factor_cov.keys()))
    k = len(factors)
    if k == 0:
        return {"ok": False, "error": "no factor covariance data"}

    # Portfolio factor betas
    port_betas = np.zeros(k)
    for i, f in enumerate(factors):
        wb = 0.0
        total_w = 0.0
        for asset, w in weights.items():
            beta = (asset_factor_betas.get(asset) or {}).get(f)
            if beta is not None:
                wb += w * beta
                total_w += abs(w)
        if total_w > 0:
            port_betas[i] = wb / total_w

    # Covariance matrix
    cov_mat = np.zeros((k, k))
    for i, fi in enumerate(factors):
        for j, fj in enumerate(factors):
            cov_mat[i, j] = factor_cov[fi].get(fj, 0.0)

    # Portfolio variance = beta' @ Cov @ beta
    port_var = float(port_betas @ cov_mat @ port_betas)

    # Marginal contribution of each factor
    marg_contrib = (cov_mat @ port_betas).tolist()

    # Component contributions (beta_i * marg_i)
    comp_contrib = {f: port_betas[i] * marg_contrib[i] for i, f in enumerate(factors)}

    # Cross-terms
    cross_terms = {}
    for i, fi in enumerate(factors):
        for j, fj in enumerate(factors):
            if i < j:
                cross_terms[f"{fi}x{fj}"] = 2 * port_betas[i] * port_betas[j] * cov_mat[i, j]

    return {
        "ok": True,
        "portfolio_variance": port_var,
        "portfolio_vol": math.sqrt(port_var) if port_var > 0 else 0.0,
        "factor_betas": dict(zip(factors, port_betas.tolist())),
        "marginal_contributions": dict(zip(factors, marg_contrib)),
        "component_contributions": comp_contrib,
        "cross_terms": cross_terms,
        "pct_explained": {f: c / port_var if port_var > 0 else 0.0
                          for f, c in comp_contrib.items()},
    }


# ── Scenario runner for CLI/agent ──────────────────────────────────────────────

def run_all_scenarios(
    weights: dict[str, float],
    asset_factor_betas: dict[str, dict[str, float]],
) -> list[dict]:
    """Run portfolio against all historical scenarios."""
    results = []
    for scenario in HISTORICAL_SCENARIOS:
        results.append(scenario_stress_test(weights, asset_factor_betas, scenario))
    return results


# ── Report builder (agent-facing) ──────────────────────────────────────────────

def build_factor_report(
    asset_returns: dict[str, list[float]],
    factor_returns: dict[str, list[float]],
    weights: dict[str, float],
    benchmark_returns: Optional[list[float]] = None,
    capital: Optional[float] = None,
    factor_cov: Optional[dict[str, dict[str, float]]] = None,
) -> dict:
    """
    Build a comprehensive factor model report for the agent/skill.

    Inputs:
        asset_returns: {symbol: [returns...]}
        factor_returns: {factor: [returns...]}
        weights: {symbol: weight}
        benchmark_returns: optional benchmark series
        capital: optional capital for sizing
        factor_cov: optional factor covariance matrix (for Monte Carlo)

    Returns:
        JSON-safe dict with regressions, attribution, stress tests, risk decomp
    """
    report = {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "assets": {},
        "portfolio": {},
        "scenarios": [],
        "monte_carlo": {},
        "risk_decomposition": {},
        "notes": [],
    }

    # Align all series to common length
    n_asset = min(len(r) for r in asset_returns.values()) if asset_returns else 0
    n_factor = min(len(r) for r in factor_returns.values()) if factor_returns else 0
    n = min(n_asset, n_factor)
    if n < 30:
        report["notes"].append(f"insufficient data: {n} periods (need >=30)")
        return report

    # Trim to common tail
    asset_returns_aligned = {s: r[-n:] for s, r in asset_returns.items()}
    factor_returns_aligned = {f: r[-n:] for f, r in factor_returns.items()}

    # Per-asset factor regressions
    asset_betas = {}
    for symbol, rets in asset_returns_aligned.items():
        reg = factor_regression(rets, factor_returns_aligned)
        report["assets"][symbol] = reg
        if reg.get("ok"):
            asset_betas[symbol] = reg["betas"]

    # Portfolio-level attribution
    port_rets = []
    for i in range(n):
        r = 0.0
        for s, w in weights.items():
            if s in asset_returns_aligned and i < len(asset_returns_aligned[s]):
                r += w * asset_returns_aligned[s][i]
        port_rets.append(r)

    attr = factor_attribution(port_rets, factor_returns_aligned, weights, asset_betas)
    report["portfolio"]["attribution"] = attr

    # Portfolio factor regression
    port_reg = factor_regression(port_rets, factor_returns_aligned)
    report["portfolio"]["factor_regression"] = port_reg

    # Historical scenarios
    report["scenarios"] = run_all_scenarios(weights, asset_betas)

    # Monte Carlo (if covariance provided)
    if factor_cov:
        factor_mean = {f: statistics.fmean(r) for f, r in factor_returns_aligned.items() if r}
        mc = monte_carlo_stress_test(weights, asset_betas, factor_mean, factor_cov)
        report["monte_carlo"] = mc

    # Risk decomposition
    if factor_cov:
        rd = factor_risk_decomposition(weights, asset_betas, factor_cov)
        report["risk_decomposition"] = rd

    # Benchmark-relative
    if benchmark_returns and len(benchmark_returns) >= n:
        bench = benchmark_returns[-n:]
        from .institutional import beta, correlation, alpha_annualized, information_ratio
        report["portfolio"]["benchmark_relative"] = {
            "beta": beta(port_rets, bench),
            "correlation": correlation(port_rets, bench),
            "alpha_ann": alpha_annualized(port_rets, bench),
            "information_ratio": information_ratio(port_rets, bench),
        }

    report["notes"].append(
        "Factor model estimates — not forecasts. Factor data quality and "
        "stationarity assumptions materially affect results. Use for "
        "risk awareness, not position sizing alone."
    )
    return report