# Phase 2 — Trading Agent

> Companion to `ARCHITECTURE.md` and `docs/CODING_AGENT.md`.
> Status: **Built** · Agent: `orchestrator/agents/trading/` · Tests:
> `tests/test_trading_agent.py` · Demo: `scripts/trading_agent_demo.py`

---

## 1. What it is

The real replacement of the Phase-1 `trading_agent` stub, on the Jarvis agent
interface (`orchestrator/base.py::BaseAgent`) — same registry name, same
contracts, same router. **Stock Analysis is a capability of this agent, not a
separate agent** (`analysis.py`), exactly as the phase brief requires.

Hard rules baked into the code (not prompts):

| Rule | Enforcement |
|---|---|
| Live orders are opt-in only | Paper is default. The Delta live path requires the separate `memory/trading/live_execution.json` switch, credentials, a clear HALT sentinel and a per-order manual phrase. `LiveBroker` remains a hard-fail compatibility symbol. |
| Auto Trade cannot go live silently | `execute_approved` refuses `approved_by=auto-trade` unless `allow_auto` is explicitly true |
| Market data ≠ broker execution | `data.py` never sees orders; `broker.py` never fetches prices; the agent reads the market only through `data.MarketData` |
| Never invent market data | Every answer carries a `Provenance` label; failed fetches return `UNAVAILABLE` + reason with `data=None`; missing fields are listed, not padded |
| Human approval for execution | Proposals park in `NEEDS_APPROVAL` → `orchestrator/approvals.py`; the checkpoint stores the exact proposal; approval re-runs the RISK gate before placing anything |
| Trading books via RAG, when configured | `rag_bridge` calls `core/rag_service` as caller `trading_agent` (ACL in `config/rag_callers.json`); a blind caller degrades visibly, never fails analysis |

**Disabled by default** (shadow-mode convention): set `JARVIS_TRADING_AGENT=on`
to register the real agent instead of the stub.

```python
from orchestrator.agents import build_default_registry
reg = build_default_registry()          # stub unless the env flag is on
agent = reg.get("trading_agent")
```

## 2. Architecture

```
DATA ──▶ ANALYSIS ──▶ SIGNAL ──▶ RISK CHECK ──▶ HUMAN APPROVAL
                                                   │
                                     OPTIONAL BROKER ACTION (paper default;
                                     Delta live only behind the armed switch)
```

| Stage | Module | What happens |
|---|---|---|
| DATA | `data.py` | yfinance (DELAYED default, LIVE only on explicit `request_live`) or `SimulatedAdapter` (SIMULATED, deterministic per symbol). Options chain + IV; Black-Scholes greeks computed only from served IV. News endpoint present and labeled. |
| ANALYSIS | `analysis.py` | EMA 20/50/200 stack, RSI(14), ATR(14), Bollinger %b, ROC, session VWAP ±σ bands, volume profile (POC + 70% value area + effort-vs-result), market structure (swings, HH/HL, break-of-structure), price action (candle shape, wick rejection, engulfing, range position) — all in one analysis card. |
| SIGNAL | `signal.py` | Rule registry (`trend_follow`, `mean_revert`, `breakout`), per-rule verdicts with human reasons, conflict refusal, entry/SL/target proposal: ATR stop tightened by structure, targets at R multiples, R:R per target. Delta-band options proposal from served chains. |
| RISK | `risk.py` | Layer 1 fixed-fractional sizing (rounds DOWN, capped by `max_position_usd`); Layer 2 safety gates from `memory/trading/trading_safety.json` (daily loss %, max trades/day, cooldown, whitelist/blacklist, min signal score); Layer 3 proposal audit (geometry, R:R floor). |
| HUMAN | checkpoint + orchestrator | `NEEDS_APPROVAL` with the full proposal card; checkpoint at `memory/trading_agent/<task_id>.json`. |
| EXECUTION | `broker.py` | On approval + risk re-check pass: `PaperBroker` writes `paper_positions.json` + `paper_journal.jsonl` (same files the existing paper engine uses) with labeled `paper: true`. When the separate live switch is armed, `DeltaLiveBroker` submits a signed limit-IOC Delta order and attaches an SL/TP bracket; if the bracket fails it attempts a reduce-only close and reports the result. |

### Data provenance labels

| Label | When |
|---|---|
| `live` | ONLY on an explicit `request_live=true` from the caller |
| `delayed` | default for every yfinance answer (free endpoints are typically ~15 min behind NSE/BSE) |
| `simulated` | `SimulatedAdapter` (tests, demos, paper drills) |
| `unavailable` | fetch failed — carries the error, carries NO price |

## 3. Using it

Through the orchestrator (after `JARVIS_TRADING_AGENT=on`):

| ask | action taken |
|---|---|
| "analyze AAPL" | full pipeline → analysis card, and a PAPER approval card if rules + risk pass |
| "quote TCS.NS" | labeled quote, missing fields listed |
| "options NVDA" | labeled chain + computed greeks + ATM read |
| "positions" | open paper positions |
| "backtest RELIANCE.NS" | bar-replay of the strategy over history, honest metrics |

Structured callers pass `metadata`:

```python
AgentRequest(
    task_id=..., task_type=TaskType.TRADING, text="analyze AAPL",
    metadata={
        "trading_action": "analyze",   # analyze|quote|options|positions|backtest
        "symbol": "AAPL",              # explicit beats text extraction
        "strategy": "trend_follow",    # trend_follow|mean_revert|breakout
        "capital": 10000, "risk_pct": 1.0,
        "prefer_sim": False,           # True → SimulatedAdapter
        "request_live": False,         # True → yfinance realtime endpoints
    })
```

Approval re-dispatch (the orchestrator does this automatically):
`handle()` with `metadata.approved=True, metadata.resume=<task_id>` →
risk re-check → paper order.

### Enabling RAG for your trading books

`config/rag_callers.json` (create if absent):

```json
{ "trading_agent": {"collections": ["trading_books"], "can_write": false} }
```

Ingest your books into the `trading_books` collection; every analysis that
produces a proposal then attaches a rules-evidence checklist (citations
included) to the approval card. The agent never claims compliance — the human
reads the checklist and decides.

## 4. Offline verification

```bash
.venv/bin/python -m pytest tests/test_trading_agent.py -q     # 75 tests
.venv/bin/python scripts/trading_agent_demo.py                # full workflow, sim data
```

No test touches the network or the real trading files (JARVIS_HOME is
redirected per test); the yfinance adapter is exercised against a fake module.

## 5. Files

| File | Role |
|---|---|
| `orchestrator/agents/trading/data.py` | adapters, provenance, options/IV/greeks, news |
| `orchestrator/agents/trading/analysis.py` | the stock-analysis capability (pure functions) |
| `orchestrator/agents/trading/signal.py` | strategy rules, proposals, R:R, options spreads |
| `orchestrator/agents/trading/risk.py` | sizing, safety limits, proposal audit |
| `orchestrator/agents/trading/broker.py` | paper broker; `DeltaLiveBroker` behind the armed live switch; `LiveBroker` remains a hard-fail symbol |
| `orchestrator/agents/trading/delta_private.py` | signed Delta India private client (wallet/positions/orders/brackets/cancel/close) |
| `orchestrator/agents/trading/backtest.py` | bar-replay engine + walk-forward, no look-ahead |
| `orchestrator/agents/trading/rag_bridge.py` | trading-books evidence via `core/rag_service` |
| `orchestrator/agents/trading/checkpoint.py` | crash-safe task checkpoints (approval resume) |
| `orchestrator/agents/trading/trading_agent.py` | the BaseAgent wiring the pipeline |
| `orchestrator/agents/__init__.py` | registration behind `JARVIS_TRADING_AGENT=on` |
| `tests/test_trading_agent.py` | offline acceptance suite |
| `scripts/trading_agent_demo.py` | offline end-to-end demo |

## 6. What this phase deliberately does NOT do

- No exchange connectivity by default: live execution exists only behind the
  explicitly armed Delta switch and is never enabled by the backend, by Auto
  Trade, or by saving credentials.
- No LLM in the decision path — rules are deterministic and auditable; the
  model router remains available for narrative summaries later.
- No streaming/websocket data; the yfinance/Delta adapters poll REST.
- `mode: "live"` in `trading_safety.json` is respected by the existing
  Binance plugin but is *not* the trading agent's switch. The trading agent
  uses `memory/trading/live_execution.json`; every live order still re-passes
  the risk gate and requires explicit manual approval.
