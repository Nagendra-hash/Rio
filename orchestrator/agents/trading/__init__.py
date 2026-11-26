"""
orchestrator/agents/trading/ — the trading agent package.

Layout (the pipeline IS the layout):
    data.py        DATA stage — adapters, provenance labels, options/greeks
    analysis.py    ANALYSIS stage — the stock-analysis capability
    signal.py      SIGNAL stage — strategy rules, proposals, R:R
    risk.py        RISK stage — sizing, safety limits, proposal audit
    broker.py      EXECUTION stage — paper broker; LiveBroker raises
    backtest.py    BACKTESTING interface — same code path, bar replay
    rag_bridge.py  owner's trading books via core.rag_service (ACL'd)
    checkpoint.py  crash-safe task checkpoints (approval resume)
    trading_agent.py the BaseAgent that wires the pipeline together
"""
from orchestrator.agents.trading.trading_agent import TradingAgent

__all__ = ["TradingAgent"]
