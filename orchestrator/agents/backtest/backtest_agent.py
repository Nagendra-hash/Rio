"""
orchestrator/agents/backtest/backtest_agent.py — the conversation surface.

One symbol in, one bounded research job out. This agent never performs the
research itself: it starts a detached worker, remembers the job id, and answers
status/report/cancel questions from durable state. That is what keeps a
10–30 minute backtest off the Jarvis conversation loop.

Supported asks (all optional):
    "backtest BTC"                  start spot-crypto research
    "backtest ETH perp"             separate perpetual run
    "backtest GOLD"                 ask once, then remember the market
    "GOLD futures"                  save the answer + run
    "backtest status [BTC|bt-…]"    progress or last result
    "backtest report [bt-…]"        the saved summary
    "cancel backtest [bt-…]"        stop the worker
    "backtest history [BTC]"        recent jobs
    "backtest champion [BTC]"       current research champion + ledger
    "backtest schedule"             due-instruments plan
    "rollback champion BTC"         restore the previous champion

There is no order path, no broker client and no LLM-generated strategy code in
this module or anything it imports.
"""
from __future__ import annotations

import re
from typing import Any

from orchestrator.agents.backtest import symbols
from orchestrator.agents.backtest.champions import ChampionStore
from orchestrator.agents.backtest.config import Config, default_config
from orchestrator.agents.backtest.jobs import JobStore, start_job
from orchestrator.agents.backtest.store import now_iso, read_json
from orchestrator.base import BaseAgent
from orchestrator.contracts import AgentRequest, AgentResult, TaskStatus, TaskType

_JOB_RE = re.compile(r"\b(bt-\d{8}-[0-9a-f]{10})\b", re.IGNORECASE)
_STATUS_RE = re.compile(r"\b(status|progress|how\s+far|running|finished|queued)\b",
                        re.IGNORECASE)
_REPORT_RE = re.compile(r"\b(report|results?|summary|show\s+me|details?)\b",
                        re.IGNORECASE)
_CANCEL_RE = re.compile(r"\b(cancel|stop|abort|kill)\b", re.IGNORECASE)
_HISTORY_RE = re.compile(r"\b(history|recent|jobs|list)\b", re.IGNORECASE)
_CHAMPION_RE = re.compile(r"\b(champion|best\s+strategy|current\s+model)\b",
                          re.IGNORECASE)
_ROLLBACK_RE = re.compile(r"\brollback\b", re.IGNORECASE)
_SCHEDULE_RE = re.compile(r"\b(schedule|scheduled|due|watchlist)\b", re.IGNORECASE)
_RUN_RE = re.compile(r"\b(backtest|back-test|test|research|run|optimis|optimiz)\b",
                     re.IGNORECASE)
_STATUSES = {"queued": "QUEUED", "running": "RUNNING", "completed": "COMPLETED",
             "failed": "FAILED", "cancelled": "CANCELLED"}


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:+.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_rr(value: Any) -> str:
    """Risk/reward as 1 : x (reward per unit risked)."""
    try:
        return f"1 : {float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _job_line(job: dict[str, Any]) -> str:
    progress = float(job.get("progress") or 0.0)
    return (f"{job.get('job_id')} · {job.get('symbol')}"
            f"{' ' + str(job.get('market')) if job.get('market') else ''} · "
            f"{job.get('status')} {progress * 100:.0f}%"
            f"{' · ' + str(job.get('phase')) if job.get('phase') else ''}")


class BacktestAgent(BaseAgent):
    name = "backtest_agent"
    description = (
        "Bounded crypto/gold backtest research: resolves one symbol to an "
        "explicit instrument, verifies ≥1 year of real candles, tunes and "
        "validates reviewed strategies out-of-sample, saves reports and "
        "notifies Hermes. Research only — it cannot place an order.")
    task_types = (TaskType.BACKTEST,)

    def __init__(self, cfg: Config | None = None,
                 store: JobStore | None = None) -> None:
        self.cfg = cfg or default_config()
        self.store = store or JobStore(cfg=self.cfg)

    # ── contract ─────────────────────────────────────────────────────────
    def can_handle(self, request: AgentRequest) -> bool:
        if request.task_type is TaskType.BACKTEST:
            return True
        text = str(request.text or "")
        return bool(symbols.extract_symbol(text) and
                    (_RUN_RE.search(text) or _STATUS_RE.search(text) or
                     _REPORT_RE.search(text) or _CANCEL_RE.search(text) or
                     _CHAMPION_RE.search(text) or _SCHEDULE_RE.search(text)))

    def _handle_impl(self, request: AgentRequest) -> AgentResult:
        text = str(request.text or "").strip()
        try:
            return self._dispatch(request, text)
        except Exception as exc:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED,
                text=f"The backtest lab hit an error: {type(exc).__name__}: "
                     f"{str(exc)[:200]}. Nothing was run and no order exists.",
                error=str(exc), agent=self.name)

    # ── dispatch ─────────────────────────────────────────────────────────
    def _dispatch(self, request: AgentRequest, text: str) -> AgentResult:
        job_id = self._job_id(text)
        explicit_gold = self._gold_answer(text)

        if _SCHEDULE_RE.search(text):
            return self._schedule(request)

        if _ROLLBACK_RE.search(text):
            return self._rollback(request, text)

        if _CHAMPION_RE.search(text):
            return self._champion(request, text)

        stop_loss = bool(re.search(r"stop\s*loss|\bstop\s+loss\b", text,
                                   re.IGNORECASE))
        if _CANCEL_RE.search(text) and not stop_loss and (
                job_id or symbols.extract_symbol(text) or
                not _RUN_RE.search(text)):
            return self._cancel(request, text, job_id)

        if _HISTORY_RE.search(text) and not _REPORT_RE.search(text):
            return self._history(request, text)

        # Reports and status win over "start": "backtest report" and
        # "backtest status" are questions about existing work.
        if _REPORT_RE.search(text):
            return self._report(request, text, job_id)

        if _STATUS_RE.search(text):
            return self._status(request, text, job_id)

        # ── a follow-up that only answers the gold question ──────────────
        if explicit_gold and not symbols.extract_symbol(text):
            self.cfg.set_gold_market(explicit_gold)
            return self._start(request, "GOLD", explicit_gold, note="gold saved")

        return self._start(request, symbols.extract_symbol(text),
                           symbols.market_hint(text), note="")

    # ── start a job ──────────────────────────────────────────────────────
    def _start(self, request: AgentRequest, symbol: str, market: str,
               *, note: str = "") -> AgentResult:
        if not symbol:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.NEEDS_INPUT,
                text="Name one instrument to research: BTC, ETH, SOL or GOLD. "
                     "Optionally add a market (spot / perp for crypto; xauusd / "
                     "futures / mcx for gold).",
                agent=self.name)

        # Gold: explicit request wins and is remembered; otherwise the saved
        # preference is used; otherwise ask exactly once.
        if symbol == "GOLD":
            market = (market or "").strip().lower()
            if market in ("futures", "xauusd", "mcx"):
                self.cfg.set_gold_market(market)
            elif not self.cfg.gold_market():
                resolution = symbols.resolve("GOLD", cfg=self.cfg)
                return AgentResult(
                    task_id=request.task_id, status=TaskStatus.NEEDS_INPUT,
                    text=resolution.question or
                         "Which gold market should I research: XAUUSD spot, "
                         "COMEX futures, or MCX?",
                    data={"options": list(resolution.options),
                          "question": "gold_market"},
                    agent=self.name)

        if symbol != "GOLD" and market not in ("", "spot", "perp", "auto"):
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.NEEDS_INPUT,
                text=f"{symbol} is crypto; '{market}' is a gold market. Use "
                     "spot or perp.",
                agent=self.name)

        result = start_job(symbol, market or None, mode="full", source="jarvis",
                           options={"note": note}, cfg=self.cfg)
        if not result.get("ok"):
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED,
                text=f"Could not start the backtest: {result.get('error')}. "
                     "Nothing ran.",
                error=str(result.get("error") or ""), agent=self.name)

        job = self.store.get(result["job_id"]) or result.get("job") or {}
        if result.get("deduplicated"):
            head = ("That research is already queued/running — I reused it "
                    "instead of starting a duplicate.")
        else:
            head = ("Started a bounded research job. It runs in a separate "
                    "worker, so this conversation stays responsive.")
        text_out = "\n".join([
            head,
            _job_line(job),
            "It will verify at least one year of real candles per timeframe, "
            "reject anything shorter, then tune on train, choose on validation "
            "and test once out-of-sample.",
            f"Ask 'backtest status {result['job_id']}' or 'backtest report "
            f"{result['job_id']}' when it finishes.",
            "Research only — no order is placed.",
        ])
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED, text=text_out,
            data={"job_id": result["job_id"], "job": job,
                  "deduplicated": bool(result.get("deduplicated"))},
            agent=self.name)

    # ── queries ──────────────────────────────────────────────────────────
    def _job_id(self, text: str) -> str:
        m = _JOB_RE.search(text or "")
        return m.group(1).lower() if m else ""

    def _gold_answer(self, text: str) -> str:
        low = str(text or "").strip().lower()
        if low in ("xauusd", "xau/usd", "spot gold"):
            return "xauusd"
        if low in ("futures", "comex", "gc", "gc=f"):
            return "futures"
        if low == "mcx":
            return "mcx"
        return ""

    def _pick(self, text: str, job_id: str) -> dict | None:
        if job_id:
            return self.store.get(job_id)
        symbol = symbols.extract_symbol(text)
        if symbol:
            jobs = self.store.list(limit=1, symbol=symbol)
            return jobs[0] if jobs else None
        jobs = self.store.list(limit=1)
        return jobs[0] if jobs else None

    def _status(self, request: AgentRequest, text: str,
                job_id: str) -> AgentResult:
        job = self._pick(text, job_id)
        if not job:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.COMPLETED,
                text="No backtest job found yet. Say 'backtest BTC' to start one.",
                agent=self.name)
        lines = [_job_line(job)]
        if job.get("message"):
            lines.append(str(job["message"]))
        if job.get("headline"):
            lines.append(str(job["headline"]))
        if job.get("status") == "COMPLETED":
            lines.append(self._result_line(job))
            lines.append(f"Ask 'backtest report {job['job_id']}' for the summary.")
        elif job.get("status") in ("FAILED", "CANCELLED"):
            lines.append(str(job.get("error") or "")[:300])
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED,
            text="\n".join([l for l in lines if l]),
            data={"job": job}, agent=self.name)

    def _result_line(self, job: dict) -> str:
        champ = job.get("champion")
        if isinstance(champ, str):
            champ = read_json_from_text(champ) or {}
        if not champ:
            return (f"Verdict: {job.get('result_status') or job.get('status')} — "
                    "no strategy cleared every out-of-sample gate.")
        oos = champ.get("oos") or {}
        if isinstance(oos, str):
            oos = read_json_from_text(oos) or {}
        return ("Champion: {s} on {tf} · OOS net {r} · Sharpe {sh} · "
                "max DD {dd} · R:R {rr} · {n} trades").format(
                    s=champ.get("strategy"), tf=champ.get("timeframe"),
                    r=_fmt_pct(oos.get("net_return")), sh=oos.get("sharpe"),
                    dd=_fmt_pct(oos.get("max_drawdown")),
                    rr=_fmt_rr(oos.get("risk_reward")),
                    n=oos.get("trades", 0))

    def _report(self, request: AgentRequest, text: str,
                job_id: str) -> AgentResult:
        job = self._pick(text, job_id)
        if not job:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.COMPLETED,
                text="No report yet — start a backtest first.",
                agent=self.name)
        if job.get("status") in ("QUEUED", "RUNNING"):
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.COMPLETED,
                text=f"{_job_line(job)}\nThe report is written when the job "
                     "finishes.",
                data={"job": job}, agent=self.name)
        path = job.get("report_dir") or ""
        if not path:
            from orchestrator.agents.backtest.store import reports_dir
            path = str(reports_dir() / str(job["job_id"]))
        summary = ""
        try:
            from pathlib import Path
            summary = (Path(path) / "summary.md").read_text(
                encoding="utf-8")
        except Exception:
            summary = str(job.get("headline") or job.get("error") or "")
        if len(summary) > 3500:
            summary = summary[:3500] + "\n… (truncated — see summary.md)"
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED,
            text=summary or "The report file is empty.",
            data={"job": job, "path": path}, agent=self.name)

    def _cancel(self, request: AgentRequest, text: str,
                job_id: str) -> AgentResult:
        job = self._pick(text, job_id)
        if not job:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.COMPLETED,
                text="No job to cancel.", agent=self.name)
        res = self.store.cancel(job["job_id"])
        return AgentResult(
            task_id=request.task_id,
            status=TaskStatus.COMPLETED if res.get("ok") else TaskStatus.FAILED,
            text=(f"Cancelled {job['job_id']}." if res.get("ok") else
                  f"Could not cancel: {res.get('error')}"),
            data=res, agent=self.name)

    def _history(self, request: AgentRequest, text: str) -> AgentResult:
        symbol = symbols.extract_symbol(text) or None
        jobs = self.store.list(limit=10, symbol=symbol)
        if not jobs:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.COMPLETED,
                text="No backtest jobs in history yet.", agent=self.name)
        lines = ["Recent backtest jobs:"]
        for job in jobs:
            lines.append("· " + _job_line(job) +
                         (f" · {job.get('result_status')}"
                          if job.get("result_status") else ""))
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED,
            text="\n".join(lines), data={"jobs": jobs}, agent=self.name)

    def _champion(self, request: AgentRequest, text: str) -> AgentResult:
        store = ChampionStore(self.cfg)
        ledger = store.all()
        symbol = symbols.extract_symbol(text)
        if symbol == "GOLD" and not self.cfg.gold_market():
            keys = [k for k in ledger if k.startswith("GOLD:")]
        elif symbol:
            market = symbols.market_hint(text)
            if market:
                keys = [f"{symbol}:{market}"]
            else:
                keys = [k for k in ledger if k.startswith(f"{symbol}:")]
        else:
            keys = sorted(ledger)
        if not keys:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.COMPLETED,
                text="No champion has been promoted yet. A champion is only "
                     "recorded after a strategy clears every out-of-sample gate.",
                agent=self.name)
        lines = []
        for key in keys:
            entry = ledger.get(key) or {}
            champ = entry.get("champion")
            if not champ:
                lines.append(f"{key}: no active champion")
                continue
            oos = champ.get("oos") or {}
            lines.append(
                f"{key}: {champ.get('strategy')} v{champ.get('version')} on "
                f"{champ.get('timeframe')} · promoted {champ.get('promoted_at')} "
                f"· OOS {_fmt_pct(oos.get('net_return'))}, Sharpe "
                f"{oos.get('sharpe')} · score {champ.get('oos_score')}")
            if entry.get("retired_at"):
                lines.append(f"  ⚠ retired {entry.get('retired_at')}: "
                             f"{entry.get('retired_reason')}")
        lines.append("A champion is a research record, not a live position.")
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED,
            text="\n".join(lines), data={"champions": ledger}, agent=self.name)

    def _rollback(self, request: AgentRequest, text: str) -> AgentResult:
        store = ChampionStore(self.cfg)
        key = ""
        for candidate in store.all():
            symbol, _, market = candidate.partition(":")
            if symbol == (symbols.extract_symbol(text) or "") and (
                    not symbols.market_hint(text) or
                    market == symbols.market_hint(text)):
                key = candidate
                break
        if not key:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.COMPLETED,
                text="Which champion should I roll back? Use e.g. "
                     "'rollback champion BTC'.",
                agent=self.name)
        res = store.rollback(key, reason="manual rollback via Jarvis")
        return AgentResult(
            task_id=request.task_id,
            status=TaskStatus.COMPLETED if res.get("ok") else TaskStatus.FAILED,
            text=(f"Restored the previous champion for {key}: "
                  f"{(res.get('champion') or {}).get('strategy')}"
                  if res.get("ok") else f"Rollback failed: {res.get('error')}"),
            data=res, agent=self.name)

    def _schedule(self, request: AgentRequest) -> AgentResult:
        from orchestrator.agents.backtest import scheduler
        status = scheduler.schedule_status(self.cfg)
        lines = [
            f"Weekly research is {'enabled' if status['enabled'] else 'disabled'} "
            f"(every {status['interval_hours']}h) for "
            f"{', '.join(status['watchlist']) or 'no symbols'}.",
            f"Active jobs: {len(status['active_jobs'])} · champions: "
            f"{', '.join(status['champions']) or 'none'}",
        ]
        for item in status["plan"]:
            state = "DUE" if item.get("due") else "not due"
            lines.append(f"· {item.get('symbol')} {item.get('market') or ''}: "
                         f"{state} — {item.get('reason')}")
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED,
            text="\n".join(lines), data=status, agent=self.name)

    # ── used by the RIO panel and the CLI ────────────────────────────────
    def status_payload(self) -> dict[str, Any]:
        from orchestrator.agents.backtest import scheduler
        return {
            "ok": True,
            "at": now_iso(),
            "version": __import__("orchestrator.agents.backtest",
                                  fromlist=["__version__"]).__version__,
            "jobs": self.store.list(limit=20),
            "champions": ChampionStore(self.cfg).all(),
            "gold_market": self.cfg.gold_market(),
            "schedule": scheduler.schedule_status(self.cfg),
        }


def read_json_from_text(value: str) -> dict:
    import json
    try:
        out = json.loads(value)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}
