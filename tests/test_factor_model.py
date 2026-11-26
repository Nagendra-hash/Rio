"""
tests/test_factor_model.py — tests for factor_model.py
"""
import pytest
import math
import statistics
import random

from orchestrator.agents.trading.factor_model import (
    factor_regression,
    factor_attribution,
    scenario_stress_test,
    monte_carlo_stress_test,
    factor_risk_decomposition,
    run_all_scenarios,
    build_factor_report,
    HISTORICAL_SCENARIOS,
    FACTOR_NAMES,
)


def make_factor_returns(n: int = 252, seed: int = 42) -> dict[str, list[float]]:
    """Generate synthetic factor returns for testing."""
    rng = random.Random(seed)
    factors = {}
    for f in FACTOR_NAMES:
        # Small positive drift for mkt_rf, zero for others
        mu = 0.0004 if f == "mkt_rf" else 0.0
        vol = 0.01 if f == "mkt_rf" else 0.005
        factors[f] = [rng.gauss(mu, vol) for _ in range(n)]
    return factors


def make_asset_returns(
    factor_returns: dict[str, list[float]],
    betas: dict[str, float],
    alpha: float = 0.0,
    idio_vol: float = 0.005,
    seed: int = 123,
) -> list[float]:
    """Generate asset returns = alpha + sum(beta_i * factor_i) + idiosyncratic."""
    rng = random.Random(seed)
    n = len(factor_returns["mkt_rf"])
    rets = []
    for i in range(n):
        r = alpha
        for f, b in betas.items():
            r += b * factor_returns[f][i]
        r += rng.gauss(0, idio_vol)
        rets.append(r)
    return rets


class TestFactorRegression:
    def test_basic_regression(self):
        factor_rets = make_factor_returns(252)
        betas = {"mkt_rf": 1.2, "smb": 0.3, "hml": -0.2}
        asset_rets = make_asset_returns(factor_rets, betas, alpha=0.0001)

        result = factor_regression(asset_rets, factor_rets)

        assert result["ok"] is True
        assert result["alpha"] is not None
        assert abs(result["alpha"] - 0.0001) < 0.0005  # close to true alpha
        assert result["betas"]["mkt_rf"] is not None
        assert abs(result["betas"]["mkt_rf"] - 1.2) < 0.15
        assert result["r2"] is not None
        assert result["r2"] > 0.7  # should explain most variance

    def test_insufficient_data(self):
        factor_rets = make_factor_returns(10)
        asset_rets = make_asset_returns(factor_rets, {"mkt_rf": 1.0})
        result = factor_regression(asset_rets, factor_rets)
        assert result["ok"] is False
        assert "insufficient" in result["error"].lower()

    def test_empty_returns(self):
        result = factor_regression([], {})
        assert result["ok"] is False


class TestFactorAttribution:
    def test_attribution(self):
        factor_rets = make_factor_returns(252)
        betas_a = {"mkt_rf": 1.2, "smb": 0.3}
        betas_b = {"mkt_rf": 0.8, "hml": 0.2}

        rets_a = make_asset_returns(factor_rets, betas_a, alpha=0.0002)
        rets_b = make_asset_returns(factor_rets, betas_b, alpha=-0.0001)

        weights = {"A": 0.6, "B": 0.4}
        # Portfolio returns = weighted sum
        n = len(rets_a)
        port_rets = [weights["A"] * rets_a[i] + weights["B"] * rets_b[i] for i in range(n)]

        asset_betas = {
            "A": betas_a,
            "B": betas_b,
        }

        result = factor_attribution(port_rets, factor_rets, weights, asset_betas)

        assert result["ok"] is True
        assert "factor_contributions" in result
        assert "alpha_contribution" in result
        assert result["r2"] is not None


class TestScenarioStressTest:
    def test_historical_scenarios_exist(self):
        assert "2008_financial_crisis" in HISTORICAL_SCENARIOS
        assert "2020_covid_crash" in HISTORICAL_SCENARIOS
        assert "2022_bear_market" in HISTORICAL_SCENARIOS

    def test_scenario_stress_test(self):
        factor_rets = make_factor_returns(252)
        betas_a = {"mkt_rf": 1.2, "smb": 0.3, "hml": -0.2}
        betas_b = {"mkt_rf": 0.8, "hml": 0.2}

        asset_betas = {"A": betas_a, "B": betas_b}
        weights = {"A": 0.6, "B": 0.4}

        result = scenario_stress_test(weights, asset_betas, "2008_financial_crisis")

        assert result["ok"] is True
        assert result["scenario"] == "2008_financial_crisis"
        assert "portfolio_pnl_pct" in result
        assert "per_asset_pnl" in result
        assert "factor_contributions" in result

    def test_custom_scenario(self):
        asset_betas = {"A": {"mkt_rf": 1.0}}
        weights = {"A": 1.0}
        custom = {"mkt_rf": -0.10}

        result = scenario_stress_test(weights, asset_betas, "2008_financial_crisis", custom_shocks=custom)

        assert result["ok"] is True
        assert result["shocks_applied"] == custom
        # Portfolio should lose ~10%
        assert abs(result["portfolio_pnl_pct"] - (-0.10)) < 0.001

    def test_unknown_scenario(self):
        result = scenario_stress_test({}, {}, "unknown_scenario")
        assert result["ok"] is False


class TestMonteCarloStressTest:
    def test_monte_carlo_basic(self):
        factor_rets = make_factor_returns(252)
        betas_a = {"mkt_rf": 1.2, "smb": 0.3}
        betas_b = {"mkt_rf": 0.8, "hml": 0.2}

        asset_betas = {"A": betas_a, "B": betas_b}
        weights = {"A": 0.6, "B": 0.4}

        factor_mean = {f: statistics.fmean(r) for f, r in factor_rets.items()}
        # Build covariance
        factor_cov = {}
        for fi in factor_rets:
            factor_cov[fi] = {}
            for fj in factor_rets:
                factor_cov[fi][fj] = statistics.covariance(factor_rets[fi], factor_rets[fj])

        result = monte_carlo_stress_test(weights, asset_betas, factor_mean, factor_cov, n_sims=1000)

        assert result["ok"] is True
        assert "var_95" in result
        assert "cvar_95" in result
        assert "mean_pnl" in result
        # For losses (negative): CVaR is avg of worst 5%, so more negative than VaR
        # e.g., VaR=-0.04, CVaR=-0.06 => CVaR <= VaR <= 0
        assert result["cvar_95"] <= result["var_95"] <= 0
        assert result["n_sims"] == 1000


class TestFactorRiskDecomposition:
    def test_decomposition(self):
        factor_rets = make_factor_returns(252)
        betas_a = {"mkt_rf": 1.2, "smb": 0.3, "hml": -0.2}
        betas_b = {"mkt_rf": 0.8, "smb": -0.1, "hml": 0.2}

        asset_betas = {"A": betas_a, "B": betas_b}
        weights = {"A": 0.6, "B": 0.4}

        factor_cov = {}
        for fi in factor_rets:
            factor_cov[fi] = {}
            for fj in factor_rets:
                factor_cov[fi][fj] = statistics.covariance(factor_rets[fi], factor_rets[fj])

        result = factor_risk_decomposition(weights, asset_betas, factor_cov)

        assert result["ok"] is True
        assert "portfolio_variance" in result
        assert "component_contributions" in result
        assert "pct_explained" in result
        # Market should dominate
        assert "mkt_rf" in result["component_contributions"]
        assert result["component_contributions"]["mkt_rf"] > 0


class TestRunAllScenarios:
    def test_all_scenarios(self):
        asset_betas = {"A": {"mkt_rf": 1.0, "smb": 0.2}}
        weights = {"A": 1.0}

        results = run_all_scenarios(weights, asset_betas)

        assert len(results) == len(HISTORICAL_SCENARIOS)
        for r in results:
            assert r["ok"] is True
            assert "portfolio_pnl_pct" in r


class TestBuildFactorReport:
    def test_build_report(self):
        factor_rets = make_factor_returns(252)
        betas_a = {"mkt_rf": 1.2, "smb": 0.3}
        betas_b = {"mkt_rf": 0.8, "hml": 0.2}

        rets_a = make_asset_returns(factor_rets, betas_a, alpha=0.0002)
        rets_b = make_asset_returns(factor_rets, betas_b, alpha=-0.0001)

        asset_rets = {"A": rets_a, "B": rets_b}
        weights = {"A": 0.6, "B": 0.4}

        result = build_factor_report(asset_rets, factor_rets, weights)

        assert "as_of" in result
        assert "assets" in result
        assert "portfolio" in result
        assert "scenarios" in result
        assert "A" in result["assets"]
        assert "B" in result["assets"]
        assert len(result["scenarios"]) == len(HISTORICAL_SCENARIOS)

    def test_report_with_covariance(self):
        factor_rets = make_factor_returns(252)
        betas_a = {"mkt_rf": 1.2, "smb": 0.3}
        betas_b = {"mkt_rf": 0.8, "hml": 0.2}

        rets_a = make_asset_returns(factor_rets, betas_a)
        rets_b = make_asset_returns(factor_rets, betas_b)

        asset_rets = {"A": rets_a, "B": rets_b}
        weights = {"A": 0.6, "B": 0.4}

        factor_cov = {}
        for fi in factor_rets:
            factor_cov[fi] = {}
            for fj in factor_rets:
                factor_cov[fi][fj] = statistics.covariance(factor_rets[fi], factor_rets[fj])

        result = build_factor_report(asset_rets, factor_rets, weights, factor_cov=factor_cov)

        assert "monte_carlo" in result
        assert result["monte_carlo"].get("ok") is True
        assert "risk_decomposition" in result
        assert result["risk_decomposition"].get("ok") is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])