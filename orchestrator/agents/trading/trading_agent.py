"""
orchestrator/agents/trading/trading_agent.py — the real trading_agent.

Replaces the Phase-1 stub under the SAME registry name. Stock Analysis is a
CAPABILITY of this agent (analysis.py), never a separate agent.

PIPELINE (the phase brief's architecture):

    DATA ──▶ ANALYSIS ──▶ SIGNAL ──▶ RISK CHECK ──▶ HUMAN APPROVAL
                                                      │
                                        OPTIONAL BROKER ACTION (paper only)

    * DATA      data.MarketData — yfinance (DELAYED default) or SIMULATED;
                  every result carries a Provenance label. Never invents data.
    * ANALYSIS  analysis.analyze() — technicals, VWAP, volume, structure,
                  price action. Stock analysis capability of THIS agent.
    * SIGNAL    signal.run_signal() — strategy evaluation → entry/SL/target
                  proposal with R:R. Proposals only; never an order.
    * RISK      risk.full_risk_check() — proposal audit, position sizing,
                  daily limits. Any layer failing refuses the trade.
    * HUMAN     the orchestrator's ApprovalGate parks the task (NEEDS_APPROVAL);
                  the checkpoint stores the exact proposal for the resume.
    * EXECUTION broker.execute_approved() — paper by default; a real Delta
                  order only when the separate live switch is armed, a human
                  approved this exact proposal, credentials exist and HALT is
                  clear. mode=live alone is not sufficient.

APPROVAL RE-DISPATCH (the same contract the coding agent uses):
    the orchestrator re-calls handle() with metadata.approved=true and
    metadata.resume=<task_id>; the agent loads the checkpoint and — only if
    the stored risk check still passes — places the order. Paper remains the
    default; live requires the separate armed switch + manual approval.
"""
from __future__ import annotations

from typing import Any

from orchestrator.base import BaseAgent
from orchestrator.contracts import AgentRequest, AgentResult, TaskType, TaskStatus
from orchestrator.agents.trading import analysis as an
from orchestrator.agents.trading import signal as sg
from orchestrator.agents.trading import risk as rk
from orchestrator.agents.trading import broker as bk
from orchestrator.agents.trading import rag_bridge
from orchestrator.agents.trading.checkpoint import CheckpointStore, new_stage_trace


class TradingAgent(BaseAgent):
    name = "trading_agent"
    description = ("Market analysis with a stock-analysis capability: data "
                   "with provenance, technicals/VWAP/volume/structure, "
                   "strategy evaluation, entry/SL/target proposals, risk "
                   "checks, paper execution behind human approval. No live "
                   "orders in this phase.")
    task_types = (TaskType.TRADING,)

    def __init__(self, checkpoint_store: CheckpointStore | None = None) -> None:
        self._cp = checkpoint_store or CheckpointStore()

    # ── dispatch ───────────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        text = (request.text or "").strip()
        try:
            if meta.get("approved") and meta.get("resume"):
                return self._resume_approved(request, str(meta["resume"]),
                                             str(meta.get("approved_by") or "user"))
            action = str(meta.get("trading_action") or "").strip().lower()
            if action in ("", "analyze", "trade", "signal"):
                return self._pipeline(request, text, meta)
            if action == "quote":
                return self._quote_only(request, text, meta)
            if action == "options":
                return self._options(request, text, meta)
            if action == "positions":
                return self._positions(request)
            if action == "backtest":
                return self._backtest(request, text, meta)
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.NEEDS_INPUT,
                text=(f"Unknown trading_action '{action}'. Use analyze / "
                      f"quote / options / positions / backtest."),
                agent=self.name)
        except Exception as exc:                     # never break the router
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               text=f"trading_agent error: "
                                    f"{type(exc).__name__}: {exc}",
                               agent=self.name)

    # ── the main pipeline ──────────────────────────────────────────────────
    def _pipeline(self, request: AgentRequest, text: str,
                  meta: dict) -> AgentResult:
        symbol = str(meta.get("symbol") or self._extract_symbol(text) or "").upper()
        if not symbol:
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.NEEDS_INPUT,
                               text="Which symbol? (e.g. AAPL, TCS.NS, "
                                    "BTC-USD — NSE symbols need the .NS "
                                    "suffix)",
                               agent=self.name)
        strategy = str(meta.get("strategy") or "trend_follow").strip().lower()
        prefer_sim = bool(meta.get("prefer_sim"))
        venue = str(meta.get("venue") or "").strip().lower()
        capital = float(meta.get("capital") or 10_000.0)
        risk_pct = float(meta.get("risk_pct") or 1.0)
        trace: list[dict] = []

        # ── DATA ───────────────────────────────────────────────────────────
        from orchestrator.agents.trading.data import MarketData
        md = MarketData(prefer_sim=prefer_sim)
        # interval is plumbed so callers can request true intraday analysis
        # (e.g. period='5d', interval='15m'); default stays the daily chart.
        hist = md.history(symbol, period=str(meta.get("period") or "6mo"),
                          interval=str(meta.get("interval") or "1d"),
                          venue=venue or None)
        trace.append(new_stage_trace("data", hist.ok,
                                     hist.error or
                                     f"provenance={hist.provenance.value}"))
        if not hist.ok:
            return self._completed(request, self._fmt_data_fail(symbol, hist),
                                   {"symbol": symbol, "stage": "data",
                                    "trace": trace})
        bars = hist.data["bars"]
        quote_res = md.quote(symbol, prefer_sim=prefer_sim,
                             request_live=bool(meta.get("request_live")),
                             venue=venue or None)
        quote = quote_res.data if quote_res.ok else None
        prov_labels = sorted({hist.provenance.value,
                              quote_res.provenance.value})
        venue_meta = {}
        if quote:
            venue_meta = {k: quote.get(k) for k in (
                "venue", "market", "market_class", "product_id",
                "product_symbol", "contract_type", "quote_currency",
                "contract_value", "tick_size",
            ) if quote.get(k) not in (None, "")}
            if not venue and quote.get("venue"):
                venue = str(quote.get("venue") or "").strip().lower()

        # ── ANALYSIS (stock-analysis capability) ───────────────────────────
        card = an.analyze(bars, quote=quote)
        trace.append(new_stage_trace("analysis", bool(card.get("ok")),
                                     str(card.get("error", "ok"))))
        if not card.get("ok"):
            return self._completed(request,
                                   f"Analysis unavailable for {symbol}: "
                                   f"{card.get('error')}",
                                   {"symbol": symbol, "stage": "analysis",
                                    "trace": trace})

        # ── SIGNAL ─────────────────────────────────────────────────────────
        sig = sg.run_signal(card, strategy=strategy,
                            min_score=int(meta.get("min_score") or 2))
        proposal = sig.get("proposal")
        trace.append(new_stage_trace(
            "signal", bool(proposal),
            (proposal and f"{proposal['direction']} score "
                          f"{proposal['score']}") or sig.get("note",
                                                             sig.get("error", ""))))

        # ── RISK ───────────────────────────────────────────────────────────
        risk_result = None
        if proposal:
            risk_result = rk.full_risk_check(
                proposal, symbol, capital,
                risk_pct=risk_pct,
                min_signal_score=int(proposal.get("score") or 0),
                contract_value=venue_meta.get("contract_value"))
            trace.append(new_stage_trace(
                "risk", bool(risk_result.get("ok")),
                risk_result.get("denied_by") or
                f"units={risk_result['sizing']['units']}, "
                f"risk={risk_result['sizing']['risk_amount']}"))

        # ── RAG books/rules (when configured) ──────────────────────────────
        books = rag_bridge.rules_checklist(proposal) if proposal \
            else {"ok": True, "evidence": "", "citations": [],
                  "note": "no proposal → no rule checklist pulled"}

        # ── HUMAN APPROVAL gate ────────────────────────────────────────────
        if proposal and risk_result and risk_result.get("ok"):
            cp = {"task_id": request.task_id, "symbol": symbol,
                  "strategy": strategy, "proposal": proposal,
                  "risk": {"ok": True,
                           "sizing": risk_result["sizing"],
                           "mode": risk_result.get("mode"),
                           "warnings": risk_result.get("warnings") or []},
                 "books": books, "trace": trace, "status": "awaiting_approval",
                 "analysis": card, "data_provenance": prov_labels,
                 "venue": venue, "venue_meta": venue_meta}
            self._cp.save(request.task_id, cp)
            live_cfg = rk.load_live()
            live_on = bool(live_cfg.get("enabled"))
            unit_label = ("contracts" if (risk_result["sizing"].get("contracts")
                                          or venue_meta.get("contract_value"))
                          else "units")
            summary = (f"{'LIVE' if live_on else 'PAPER'} trade proposal {symbol} "
                       f"{proposal['direction']} @ {proposal['entry']} "
                       f"SL {proposal['stop_loss']} T1 "
                       f"{proposal['targets'][0]} "
                       f"({risk_result['sizing']['units']} {unit_label}, "
                       f"risk {risk_result['sizing']['risk_amount']})")
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.NEEDS_APPROVAL,
                text=self._fmt_approval(symbol, proposal, risk_result,
                                        prov_labels, books, live=live_on,
                                        environment=live_cfg.get("environment")),
                data={"approval_summary": summary,
                      "checkpoint": request.task_id,
                      "proposal": proposal,
                      "sizing": risk_result["sizing"],
                      "live_enabled": live_on,
                      "live_environment": live_cfg.get("environment"),
                      "provenance": prov_labels},
                agent=self.name)

        # No proposal / risk-refused: an honest completed answer.
        return self._completed(request,
                               self._fmt_no_trade(symbol, prov_labels, card,
                                                  sig, risk_result, strategy),
                               {"symbol": symbol, "stage": "signal",
                                "analysis": card, "signal": sig,
                                "risk": risk_result, "trace": trace,
                                "provenance": prov_labels})

    # ── approval resume → paper execution ──────────────────────────────────
    def _resume_approved(self, request: AgentRequest, resume_id: str,
                         approved_by: str) -> AgentResult:
        cp = self._cp.load(resume_id)
        if not cp:
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               text=f"No checkpoint for approval "
                                    f"{resume_id!r} — nothing was placed.",
                               agent=self.name)
        if cp.get("status") != "awaiting_approval":
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.DECLINED,
                               text=f"Checkpoint {resume_id!r} is "
                                    f"'{cp.get('status')}' — already handled.",
                               agent=self.name)

        # RE-CHECK the risk gate on approval: limits may have changed while
        # the approval sat parked (daily loss, cooldown, blacklist…).
        proposal = cp["proposal"]
        symbol = cp["symbol"]
        sizing = (cp.get("risk") or {}).get("sizing") or {}
        recheck = rk.check_safety(symbol,
                                  min_signal_score=int(proposal.get("score") or 0))
        if not recheck.get("ok"):
            cp["status"] = "risk_refused_on_approval"
            cp["risk_refusal"] = recheck.get("reasons")
            self._cp.save(resume_id, cp)
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.DECLINED,
                text="Approved, but the risk gate refused on re-check: "
                     + "; ".join(recheck.get("reasons", []))
                     + " — nothing was placed.",
                data={"checkpoint": resume_id,
                      "risk_refusal": recheck.get("reasons")},
                agent=self.name)

        execution_ctx = {
            "symbol": symbol, "proposal": proposal, "sizing": sizing,
            "signal_score": int(proposal.get("score") or 0),
            "approved_by": approved_by, "signal_id": resume_id,
            "venue": str(cp.get("venue") or ""),
        }
        execution_ctx.update(cp.get("venue_meta") or {})
        result = bk.execute_approved(execution_ctx)
        mode = str(result.get("mode") or "paper").lower()
        cp["status"] = (f"executed_{mode}" if result.get("ok")
                        else "execution_failed")
        cp["execution"] = result
        self._cp.save(resume_id, cp)
        if not result.get("ok"):
            label = "LIVE" if mode == "live" else "Paper"
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               text=f"{label} execution failed: "
                                    f"{result.get('error')}",
                               data={"checkpoint": resume_id,
                                     "execution": result, "mode": mode},
                               agent=self.name)
        if mode == "live":
            text = (f"LIVE order placed on Delta "
                    f"{str(result.get('environment') or 'demo').upper()}: "
                    f"{symbol} {proposal.get('direction')} "
                    f"{result.get('filled_size')} contracts @ "
                    f"{result.get('entry_price')} SL "
                    f"{result.get('stop_loss')} bracket="
                    f"{'yes' if result.get('bracket_ok') else 'NO'} "
                    f"(order {result.get('order_id')}) — approved by "
                    f"{approved_by}.")
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.COMPLETED, text=text,
                               data={"checkpoint": resume_id,
                                     "execution": result, "mode": "live"},
                               agent=self.name)
        pos = result.get("position") or {}
        unit_label = ("contracts" if sizing.get("contracts") else "units")
        text = (f"PAPER order placed (no real money moved): "
                f"{symbol} {pos.get('direction')} "
                f"{sizing.get('units')} {unit_label} @ {pos.get('entry_price')} "
                f"SL {pos.get('stop_loss')} — approved by {approved_by}. "
                f"Position key: {result.get('key')}")
        return AgentResult(task_id=request.task_id,
                           status=TaskStatus.COMPLETED, text=text,
                           data={"checkpoint": resume_id,
                                 "execution": result, "mode": "paper"},
                           agent=self.name)

    # ── smaller actions ────────────────────────────────────────────────────
    def _quote_only(self, request: AgentRequest, text: str,
                    meta: dict) -> AgentResult:
        from orchestrator.agents.trading.data import MarketData
        symbol = str(meta.get("symbol") or self._extract_symbol(text) or "").upper()
        if not symbol:
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.NEEDS_INPUT,
                               text="Which symbol?", agent=self.name)
        venue = str(meta.get("venue") or "").strip().lower()
        md = MarketData(prefer_sim=bool(meta.get("prefer_sim")))
        res = md.quote(symbol, request_live=bool(meta.get("request_live")),
                       venue=venue or None)
        trace = [new_stage_trace("data", res.ok, res.error or
                                 f"provenance={res.provenance.value}")]
        if not res.ok:
            return self._completed(request,
                                   f"Quote unavailable for {symbol}: "
                                   f"{res.error}",
                                   {"symbol": symbol, "trace": trace})
        d = res.data
        miss = f" (missing: {', '.join(d['missing_fields'])})" \
            if d.get("missing_fields") else ""
        label = res.provenance.value.upper()
        text = (f"{symbol} [{label} · {res.source}] last {d['last']} "
                f"prev {d.get('previous_close')} "
                f"({d.get('currency') or '—'}){miss}")
        return self._completed(request, text,
                               {"quote": res.to_dict(), "trace": trace})

    def _options(self, request: AgentRequest, text: str,
                 meta: dict) -> AgentResult:
        from orchestrator.agents.trading.data import MarketData, compute_greeks
        symbol = str(meta.get("symbol") or self._extract_symbol(text) or "").upper()
        if not symbol:
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.NEEDS_INPUT,
                               text="Which symbol?", agent=self.name)
        venue = str(meta.get("venue") or "").strip().lower()
        md = MarketData(prefer_sim=bool(meta.get("prefer_sim")))
        chain = md.options_chain(symbol, venue=venue or None)
        if not chain.ok:
            return self._completed(request,
                                   f"Options chain unavailable for {symbol}: "
                                   f"{chain.error}",
                                   {"symbol": symbol,
                                    "trace": [new_stage_trace(
                                        "data", False, chain.error)]})
        spot = float((chain.data or {}).get("spot_hint") or 0.0) \
            or float((md.quote(symbol).data or {}).get("last") or 0.0)
        dte = float(meta.get("days_to_expiry") or 30.0)
        greeks = compute_greeks(chain.data or {}, spot=spot,
                                days_to_expiry=dte) if spot > 0 else {}
        atm = greeks.get("atm") or {}
        label = chain.provenance.value.upper()
        text = (f"{symbol} options [{label} · {chain.source}] expiry "
                f"{chain.data.get('expiry')}: {len(chain.data.get('calls') or [])} "
                f"calls / {len(chain.data.get('puts') or [])} puts served; "
                f"greeks computed for {len(greeks.get('calls', {})) + len(greeks.get('puts', {}))} "
                f"legs with served IV; ATM ≈ "
                f"{atm.get('kind', '—')} {atm.get('strike', '—')} "
                f"delta {atm.get('delta', '—')}")
        return self._completed(request, text,
                               {"chain": chain.to_dict(), "greeks": greeks,
                                "provenance": chain.provenance.value})

    def _positions(self, request: AgentRequest) -> AgentResult:
        pb = bk.PaperBroker()
        opens = pb.open_positions()
        if not opens:
            return self._completed(request, "No open paper positions.",
                                   {"positions": {}})
        lines = [f"  {k}: {v['symbol']} {v['direction']} "
                 f"{v['position_size']}u @ {v['entry_price']} "
                 f"SL {v['stop_loss']}" for k, v in opens.items()]
        return self._completed(request,
                               f"{len(opens)} open paper position(s):\n"
                               + "\n".join(lines),
                               {"positions": opens})

    def _backtest(self, request: AgentRequest, text: str,
                  meta: dict) -> AgentResult:
        from orchestrator.agents.trading.backtest import (run_backtest,
                                                          BacktestError)
        from orchestrator.agents.trading.data import MarketData
        symbol = str(meta.get("symbol") or self._extract_symbol(text) or "").upper()
        strategy = str(meta.get("strategy") or "trend_follow").strip().lower()
        if not symbol:
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.NEEDS_INPUT,
                               text="Which symbol?", agent=self.name)
        venue = str(meta.get("venue") or "").strip().lower()
        md = MarketData(prefer_sim=bool(meta.get("prefer_sim")))
        hist = md.history(symbol, period=str(meta.get("period") or "1y"),
                          venue=venue or None)
        if not hist.ok:
            return self._completed(request,
                                   f"Backtest data unavailable for {symbol}: "
                                   f"{hist.error}",
                                   {"symbol": symbol})
        try:
            res = run_backtest(hist.data["bars"], strategy,
                               capital=float(meta.get("capital") or 100_000.0),
                               risk_pct=float(meta.get("risk_pct") or 1.0))
        except BacktestError as exc:
            return self._completed(request,
                                   f"Backtest refused for {symbol}: {exc}",
                                   {"symbol": symbol})
        s = res["summary"]
        label = hist.provenance.value.upper()
        text = (f"Backtest {symbol} [{label}] · {strategy} · "
                f"{s['trades']} trades · return {s['total_return_pct']}% · "
                f"maxDD {s['max_drawdown_pct']}% · win rate "
                f"{s['win_rate_pct']}% · PF {s['profit_factor']} · "
                f"avg R {s['avg_r_multiple']}")
        return self._completed(request, text, {"backtest": res,
                                               "provenance": label})

    # ── formatting helpers ─────────────────────────────────────────────────
    def _completed(self, request: AgentRequest, text: str,
                   data: dict) -> AgentResult:
        return AgentResult(task_id=request.task_id,
                           status=TaskStatus.COMPLETED, text=text,
                           data=data, agent=self.name)

    @staticmethod
    def _fmt_data_fail(symbol: str, res) -> str:
        return (f"No {res.provenance.value} data for {symbol} "
                f"({res.source}): {res.error}. I will not invent a price — "
                f"try another symbol or approve simulated mode.")

    @staticmethod
    def _extract_symbol(text: str) -> str:
        """Best-effort ticker extraction from chat text. Convenience only —
        structured callers should pass metadata.symbol (always wins).
        Order of preference: the token right after a verb (analyze AAPL),
        a suffixed symbol anywhere (TCS.NS, BTC-USD, ^NSEI), then the first
        short capitalized token that is not a stopword. Returns '' when
        nothing credible is found — the agent then ASKS, never guesses."""
        import re
        if not text:
            return ""
        up = text.upper()
        tokens = re.findall(
            r"\^?[A-Z][A-Z0-9]*(?:\.[A-Z]{1,4})?(?:-[A-Z]{2,4})?", up)
        noise = {"THE", "A", "AN", "IS", "AT", "ON", "IN", "TO", "OF",
                 "AND", "FOR", "ME", "MY", "SHOW", "GET", "WHAT", "WHATS",
                 "PRICE", "ANALYZE", "ANALYSIS", "ANALYSE", "TRADE",
                 "SIGNAL", "USD", "DOING", "SHOULD", "I", "YOU", "IT",
                 "THIS", "THAT", "TODAY", "NOW", "PLEASE", "CAN", "COULD",
                 "WILL", "STOCK", "SHARES", "MARKET", "BUY", "SELL",
                 "QUOTE", "CHART", "BACKTEST", "CHECK", "GIVE", "TELL",
                 "ABOUT", "US", "SETUP", "LOOK", "INTO"}
        verbs = {"ANALYZE", "ANALYSE", "TRADE", "QUOTE", "PRICE", "CHART",
                 "BACKTEST", "SIGNAL", "BUY", "SELL", "CHECK", "ON",
                 "FOR", "IN"}
        # 1) verb-adjacent: "analyze AAPL", "buy TSLA", "in NVDA"
        for i, tok in enumerate(tokens[:-1]):
            if tok in verbs and tokens[i + 1] not in noise:
                return tokens[i + 1]
        # 2) market-suffixed symbol anywhere: TCS.NS, BTC-USD, ^NSEI, BRK.B
        for tok in tokens:
            if ("." in tok or "-" in tok or tok.startswith("^")) \
                    and tok not in noise:
                return tok
        # 3) first short plain token that could be a ticker
        for tok in tokens:
            if 2 <= len(tok) <= 10 and tok not in noise:
                # ALL-CAPS in the ORIGINAL text → likely a ticker (AAPL,
                # RELIANCE); mixed case → a common word (Hello, News).
                if tok in up.split() and tok not in up.split():
                    continue
                orig = text.upper().split()
                idx = None
                for j, t2 in enumerate(orig):
                    if t2.strip("?.,!;:") == tok:
                        idx = j
                        break
                if idx is None or text.split()[idx].isupper():
                    return tok
        return ""

    @staticmethod
    def _fmt_approval(symbol: str, proposal: dict, risk: dict,
                      prov_labels: list, books: dict, *,
                      live: bool = False, environment: str | None = None) -> str:
        s = risk["sizing"]
        unit_label = "contracts" if s.get("contracts") else "units"
        lines = [
            f"{'LIVE' if live else 'PAPER'} trade proposal — {symbol} "
            f"{proposal['direction']} (data: {', '.join(prov_labels)})",
            f"  strategy: {proposal.get('strategy')} "
            f"(signal score {proposal.get('score')})",
            f"  entry {proposal['entry']} · SL {proposal['stop_loss']} · "
            f"targets {proposal['targets']}",
            f"  R:R to T1 {proposal['rr_to_target1']} · "
            f"sizing {s['units']} {unit_label} ≈ {s['notional']} notional · "
            f"risk {s['risk_amount']} ({s['risk_pct_of_capital']}% of capital)",
        ]
        if books.get("evidence"):
            lines.append("  your books say (evidence):")
            for c in books.get("citations", [])[:3]:
                lines.append(f"    {c}")
        if live:
            env = str(environment or "demo").upper()
            lines.append(
                f"  LIVE execution is ARMED for Delta {env}. Approving this "
                f"proposal submits a real order (demo funds on DEMO). The "
                f"broker attaches a stop-loss/take-profit bracket; if the "
                f"bracket fails it attempts a reduce-only close.")
        else:
            lines.append("  Approve to place a PAPER order. Live execution is "
                         "disabled; no real order exists on this path.")
        return "\n".join(lines)

    @staticmethod
    def _fmt_no_trade(symbol: str, prov_labels: list, card: dict,
                      sig: dict, risk_result: dict | None,
                      strategy: str) -> str:
        tech = card.get("technicals") or {}
        struct = card.get("structure") or {}
        bits = [f"{symbol} [{', '.join(prov_labels)}]"]
        if tech.get("ok"):
            bits.append(f"close {tech.get('last_close')}, "
                        f"EMA stack {tech.get('ema_stack')}, "
                        f"RSI {tech.get('rsi14')}")
        if struct.get("ok"):
            bits.append(f"structure {struct.get('trend')}")
        if sig.get("proposal") is None:
            why = sig.get("note") or sig.get("error") or "no edge met the rules"
            bits.append(f"no proposal: {why}")
        if risk_result and not risk_result.get("ok"):
            bits.append(f"risk refused ({risk_result.get('denied_by')}): "
                        + "; ".join(risk_result.get("issues", [])[:3]))
        return " · ".join(bits) + f" · strategy={strategy}"
