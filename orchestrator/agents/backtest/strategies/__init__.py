"""
orchestrator/agents/backtest/strategies — the reviewed strategy registry.

Only strategies registered in `library.STRATEGIES` can run. A research job
selects a key and a numeric parameter set from that strategy's declared grid;
it can never submit code.
"""
from orchestrator.agents.backtest.strategies.library import (
    STRATEGIES, STRATEGY_VERSION, StrategyContext, StrategySpec, describe_all,
    for_market, get, register, sample_grid,
)

__all__ = ["STRATEGIES", "STRATEGY_VERSION", "StrategyContext", "StrategySpec",
           "describe_all", "for_market", "get", "register", "sample_grid"]
