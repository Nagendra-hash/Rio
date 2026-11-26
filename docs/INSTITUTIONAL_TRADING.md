# Institutional Trading Desk

> Agent: `orchestrator/agents/trading/institutional.py` · CLI:
> `scripts/institutional_metrics.py` · Skill: `institutional-trading`
> (`~/.hermes/skills/institutional-trading/` for Hermes, mirrored at
> `.agents/skills/institutional-trading/`) · Tests:
> `tests/test_trading_institutional.py`

## 1. Why

`orchestrator/agents/trading/analysis.py` answers *"what is this chart doing?"*
— a single-instrument, technical question. An institution asks a different
question **before** risking capital: *how does this position change the
portfolio?* That requires portfolio-level math — realised vol, Sharpe/Sortino,
drawdown, beta and correlation to the benchmark, VaR/CVaR, a trend×volatility
regime read, risk-parity weights and vol-targeted sizes — computed on real
closes, reproducibly, with no language model in the loop.

This module makes the Hermes/JARVIS trading agent **institutional-grade**: it
reasons top-down (macro regime → sector → security → portfolio fit → sizing)
and, crucially, ends every idea with a *number* (how much, at what stop, risking
what %), instead of adjectives.

## 2. What it is

Two pieces plus one discipline layer:

| Piece | File | Role |
|---|---|---|
| Engine | `orchestrator/agents/trading/institutional.py` | Pure, stdlib-only math: returns, vol, Sharpe/Sortino/Calmar, drawdown, beta/alpha/IR, VaR/CVaR, ATR, trend & vol regimes, fixed-fractional / vol-target / risk-parity sizing, Kelly (capped), correlation matrix, stress tests, and `build_report()`. |
| CLI | `scripts/institutional_metrics.py` | Fetches REAL bars via `data.py` (yfinance, provenance-labelled) and prints one JSON report. Offline (`--from-json`) and deterministic (`--simulated`) modes for tests/demos. |
| Skill | `.agents/skills/institutional-trading/` ↔ `~/.hermes/skills/institutional-trading/` | The desk workflow, hard honesty rules, sizing rules and the **IC memo** output contract. Composes `live-prices`, `market-desk`, `trading-agents`, `trading-experience`, `trading-setup`. |

**Nothing is invented.** Every metric is arithmetic over bars that were fetched
elsewhere and labelled with provenance; a metric whose inputs are missing is
`null`, never a guess.

## 3. Use

```bash
# basket risk + sizing for a ₹10L book at a 12% portfolio vol target
.venv/bin/python scripts/institutional_metrics.py \
    --symbols RELIANCE.NS,TCS.NS,INFY.NS --benchmark ^NSEI \
    --period 2y --capital 1000000 --target-vol 0.12

# explicit weights + a scenario ("tech down 15%")
.venv/bin/python scripts/institutional_metrics.py --symbols NVDA,MSFT \
    --weights NVDA=0.6,MSFT=0.4 --stress "NVDA=-0.15,MSFT=-0.10"

# offline / deterministic
.venv/bin/python scripts/institutional_metrics.py --simulated --symbols AAA,BBB
.venv/bin/python scripts/institutional_metrics.py --from-json bars.json
```

Output keys: `regime` (benchmark trend×vol + posture), `assets.<SYM>` (return,
CAGR, vol, Sharpe, Sortino, max DD, Calmar, VaR/CVaR, vol regime, trend, beta,
correlation, ATR), `portfolio` (weights, stats, risk-parity weights,
effective names), `correlation` (matrix), `sizing` (vol-target + fixed
fractional), `flags` (concentration/correlation/vol warnings), `stress`.

From Hermes, the `institutional-trading` skill runs this and turns the numbers
into an Investment Committee memo; `trading-experience` supplies the user's own
measured evidence, `market-desk`/`trading-agents` supply the narrative.

## 4. Design rules

- Every position ends with a **stop ≥ 1.5 × ATR14** and **1–2 % capital risk**
  (JARVIS' own paper record shows tight stops were a top loss driver).
- Size = the **smaller** of the vol-targeted size and the fixed-fractional size:
  the stop protects the trade, the vol target protects the book.
- Single name ≤ 25 % of the book by default; correlation > 0.85 to an existing
  holding means size the pair as **one** position. If the cap can't be met while
  fully invested (`cap × #names < 100 %`), the remainder is **cash** — the cap is
  never breached and the engine flags it.
- Kelly is capped (≤ 25 %) and only used off a real sample.
- Decision support only — no orders, no guarantees; the paper-only rails in
  `risk.py` still gate any execution path.

## 5. Tests

```bash
.venv/bin/python -m pytest tests/test_trading_institutional.py -q
```

Deterministic synthetic series lock in the identities (beta vs self = 1, vs a
doubled series = 2; CVaR ≤ VaR ≤ 0; inverse-vol weights; vol-target inverse
scaling; concentration flags) and exercise the CLI offline and simulated.
