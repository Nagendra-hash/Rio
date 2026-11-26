"""
orchestrator/agents/growth/marketing_agent.py — MARKETING (Phase 18).

The business-capability workflow, mapped onto the EXISTING research
infrastructure (never a parallel one):

    RESEARCH      → the real ResearchAgent (Phase 6) answers the market /
                    customer / competitor questions with its own pipeline:
                    PLAN → SEARCH → COLLECT → COMPARE → VERIFY → REPORT.
                    Its result carries `sources` (full references incl.
                    url/host/tier), `verdicts` (per-claim support scores)
                    and `report.unverified_claims`. This agent consumes
                    that — it does not re-implement search.
    ANALYZE       → deterministic reading of the research record: market
                    facts (supported claims with citations), open
                    questions (unverified ones), competitor mentions.
    STRATEGY      → 2-4 options derived from the SUPPORTED facts only,
                    each labeled with the citations it stands on and its
                    evidence count. An option whose premise no supported
                    claim backs is refused — no invented market facts.
    CAMPAIGNS     → an execution PLAN in orchestrator/plans.py format
                    (Plan-and-Execute vocabulary: agent/llm/noop steps,
                    verification, approval flags): research → draft →
                    review → (human-executed publish step, marked as such
                    — the agent does not execute outreach).
    EVIDENCE      → every strategy and campaign cites [n] sources from
                    the research report; claims with zero support are
                    labeled GUESS and never enter strategy text.

SAFETY (code, not prompt)
    * send/publish/buy-ad asks are BLOCKED (safety.is_execution_request);
    * no live web call happens unless a research agent is reachable —
      with none configured, the agent answers from data carried IN the
      request (metadata.report / metadata.facts) or says "no evidence";
    * the campaign plan's publish step is a noop marked human_executed
      — Plan-and-Execute would need an approval even if it ran it.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from orchestrator.agents.growth import safety, shared
from orchestrator.agents.growth.shared import GrowthVault
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)
from orchestrator.plans import Plan, PlanStep, make_plan

log = logging.getLogger("jarvis.marketing")

MAX_FACTS = 12
MAX_STRATEGIES = 4
MIN_OPTION_CITATIONS = 1
GUESS_LABEL = "GUESS (no supporting evidence — not a fact)"


class MarketingAgent(BaseAgent):
    name = "marketing_agent"
    description = (
        "Marketing: reuses the Research Agent over permitted sources to "
        "gather market/customer/competitor evidence, analyzes it into "
        "cited strategy options, and produces campaign ideas as "
        "inspectable Plan-and-Execute artifacts. Claims are cited or "
        "labeled guesses; execution of outreach is refused in code.")
    task_types = (TaskType.MARKETING,)

    def __init__(self, store: GrowthVault | None = None,
                 researcher=None) -> None:
        self.store = store or GrowthVault()
        self._researcher = researcher   # a ResearchAgent, or None

    def _researcher_agent(self):
        """The research agent to reuse — injected, else None (an offline
        machine answers from carried-in evidence or says there is none).
        NEVER a self-spawned live agent: a MarketingAgent constructed
        offline (researcher=None) must not silently open network
        connections — it only consumes evidence the caller carried in."""
        if self._researcher is not None \
                and hasattr(self._researcher, "handle"):
            return self._researcher
        return None

    # ── entry ────────────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            meta = request.metadata or {}
            if safety.is_execution_request(request.text) and \
                    str(meta.get("mkt_action") or "") not in (
                        "research", "analyze"):
                self.store.audit("execution_refused")
                return AgentResult(
                    task_id=request.task_id, status=TaskStatus.DECLINED,
                    text=safety.EXECUTION_REFUSAL,
                    data={"marketing": True, "refusal": True},
                    agent=self.name)
            action = str(meta.get("mkt_action") or "").strip().lower() \
                or self._intent(request.text)
            handler = self._HANDLERS.get(action, MarketingAgent._full)
            return handler(self, request)
        except Exception as exc:                  # never raise outward
            log.warning("marketing error: %s", type(exc).__name__)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error="marketing action failed",
                               agent=self.name)

    @staticmethod
    def _intent(text: str) -> str:
        t = " ".join(str(text or "").lower().split())
        if re.search(r"\b(?:campaign|plan)s?\b", t) and \
                "research" not in t.split()[0:2]:
            return "campaign"
        if re.search(r"\bstrateg(?:y|ies|ic options?)\b", t):
            return "strategy"
        if re.search(r"\b(?:research|market|competitor|customer)\b", t):
            return "research"
        return "full"

    # ── helpers ──────────────────────────────────────────────────────────
    def _reply(self, request, text, *, action: str,
               status: TaskStatus = TaskStatus.COMPLETED,
               extra: dict | None = None) -> AgentResult:
        from orchestrator.agents.productivity import security as prod_sec
        body = prod_sec.redact(str(text or ""))
        return AgentResult(
            task_id=request.task_id, status=status, text=body,
            data={"marketing": True, "action": action, **(extra or {})},
            agent=self.name)

    # ── RESEARCH stage (reuse, not re-implementation) ────────────────────
    def _run_research(self, request, question: str) -> dict:
        """Returns a normalized research record: summary, key_findings,
        unverified, sources, failed_sources, support counts. Honest on
        every failure path."""
        meta = request.metadata or {}
        carried = meta.get("report")
        if isinstance(carried, dict) and (carried.get("summary") or
                                          carried.get("key_findings")):
            # evidence supplied by the caller (offline path / tests):
            # trust shape, renumber citations from the sources block
            return self._normalize_report(carried)
        agent = self._researcher_agent()
        if agent is None:
            return {"ok": False, "error": "no research agent available",
                    "summary": "", "key_findings": [],
                    "unverified": [], "sources": [], "supported": 0}
        from orchestrator.contracts import AgentRequest as AR, TaskType as TT
        req = AR(task_id=request.task_id, task_type=TT.RESEARCH,
                 text=question, metadata={
                     "allow_web": bool(meta.get("allow_web", True)),
                     "allow_local_kb": bool(meta.get("allow_local_kb",
                                                     True))})
        res = agent.handle(req)
        if res.status.value not in ("completed",):
            return {"ok": False, "error": f"research {res.status.value}: "
                                          f"{(res.text or '')[:120]}",
                    "summary": "", "key_findings": [], "unverified": [],
                    "sources": [], "supported": 0}
        return self._normalize_report((res.data or {}).get("report") or {},
                                      sources=(res.data or {}).get("sources")
                                      or [],
                                      failed=(res.data or {}).get(
                                          "failed_sources") or [])

    @staticmethod
    def _normalize_report(report: dict, sources=None, failed=None) -> dict:
        """One research-record shape, whatever came in."""
        srcs = sources if sources is not None else report.get("sources") or []
        norm_srcs = []
        for i, s in enumerate(srcs[:MAX_FACTS], 1):
            norm_srcs.append({
                "index": int(s.get("index") or i),
                "title": str(s.get("title") or "")[:160],
                "url": str(s.get("url") or "")[:300],
                "host": str(s.get("host") or "")[:80],
                "tier": str(s.get("tier") or "web"),
            })
        return {
            "ok": True,
            "summary": str(report.get("summary") or "")[:1500],
            "key_findings": [str(f)[:300]
                             for f in (report.get("key_findings") or [])
                             if str(f).strip()][:MAX_FACTS],
            "unverified": [str(u)[:300]
                           for u in (report.get("unverified_claims")
                                     or report.get("unverified") or [])
                           if str(u).strip()][:MAX_FACTS],
            "uncertainties": [str(u)[:200]
                              for u in (report.get("uncertainties") or [])
                              if str(u).strip()][:6],
            "supported": int(report.get("supported_claims") or 0),
            "confidence": str(report.get("confidence") or "low"),
            "sources": norm_srcs,
            "failed_sources": [str(f)[:120] for f in (failed or [])],
        }

    # ── ANALYZE stage ────────────────────────────────────────────────────
    @staticmethod
    def _analyze_record(rec: dict) -> dict:
        """Split the research record into analysis buckets. Deterministic:
        supported findings are FACTS WITH CITATIONS; unverified ones are
        OPEN QUESTIONS; empty means empty."""
        facts: list[dict] = []
        for f in rec.get("key_findings") or []:
            cites = re.findall(r"\[(\d+)\]", f)
            facts.append({"text": f, "citations": cites})
        return {
            "facts": facts,
            "open_questions": rec.get("unverified") or [],
            "uncertainties": rec.get("uncertainties") or [],
            "n_sources": len(rec.get("sources") or []),
            "hosts": sorted({s.get("host") for s in
                             (rec.get("sources") or []) if s.get("host")}),
        }

    # ── STRATEGY stage (cited options, no invented facts) ───────────────
    @staticmethod
    def _strategy_options(question: str, rec: dict, analysis: dict) -> list[dict]:
        """Derive strategy options from the SUPPORTED findings only.

        Each option carries its premise (a finding), its citations, and
        its evidence count — an option with zero citations cannot exist
        (the phase rule: no invented market facts)."""
        options: list[dict] = []
        facts = analysis["facts"]
        for i, fact in enumerate(facts[:MAX_STRATEGIES]):
            cites = fact.get("citations") or []
            if len(cites) < MIN_OPTION_CITATIONS:
                continue                     # unsupported → not a premise
            angle = ("lead with the strongest finding" if i == 0
                     else "differentiate on the gap it names"
                     if i == 1 else "counter-position against it"
                     if i == 2 else "niche down on its audience")
            options.append({
                "name": f"Option {chr(65 + i)} — {angle}",
                "premise": fact["text"],
                "citations": [f"[{c}]" for c in cites],
                "evidence": len(cites),
                "first_moves": [
                    "validate the premise with 5 customer conversations",
                    "draft the message around the cited finding",
                    "pilot on one channel before spending",
                ]})
        if not options and facts:
            # findings existed but none were citation-backed — the honest
            # answer is "nothing here supports a strategy yet"
            options = []
        return options

    # ── CAMPAIGN stage (a Plan artifact, not an execution) ───────────────
    @staticmethod
    def _campaign_plan(question: str, option: dict,
                       rec: dict) -> Optional[Plan]:
        """Campaign idea as an orchestrator Plan (Plan-and-Execute): the
        human can inspect, price and approve each step. The final
        publish step is a noop marked human_executed — the agent never
        executes outreach."""
        if not option.get("citations"):
            return None
        q = question[:120]
        steps = [
            PlanStep(id="s1", title="re-verify the premise",
                     action="agent",
                     params={"task_type": "research",
                             "text": f"verify with sources: "
                                     f"{option['premise'][:160]}"},
                     verification="contains:evidence", max_retries=1),
            PlanStep(id="s2", title="draft the campaign message",
                     action="llm",
                     params={"prompt": f"Draft a short campaign message "
                                       f"for: {q}. Premise (cited): "
                                       f"{option['premise'][:200]}. Use "
                                       f"only the premise above; do not "
                                       f"invent market facts."},
                     depends_on=["s1"], max_retries=1),
            PlanStep(id="s3", title="human review of the draft",
                     action="noop", depends_on=["s2"],
                     verification="contains:reviewed",
                     requires_approval=True),
            PlanStep(id="s4",
                     title="publish (HUMAN-executed — outside the agent)",
                     action="noop", depends_on=["s3"],
                     on_fail="skip"),
        ]
        try:
            return make_plan(
                goal=f"campaign: {q} — premise {option['premise'][:100]}",
                steps=steps, source="manual", created_by="marketing_agent",
                risk_notes=[f"premise stands on {option['evidence']} "
                            f"source(s): {', '.join(option['citations'])}",
                            "final publish is a human step, not an agent "
                            "action"])
        except Exception as exc:
            log.warning("campaign plan rejected: %s", exc)
            return None

    # ── actions ──────────────────────────────────────────────────────────
    def _research(self, request) -> AgentResult:
        meta = request.metadata or {}
        question = str(meta.get("query") or request.text or "").strip()[:400]
        if not question:
            return self._reply(request,
                               "What market, customer or competitor "
                               "question should the research agent run? "
                               "metadata {mkt_action: 'research', query: "
                               "'…'}", action="research",
                               status=TaskStatus.NEEDS_INPUT)
        rec = self._run_research(request, question)
        if not rec.get("ok"):
            return self._reply(request,
                               f"Research could not run: "
                               f"{rec.get('error')}. I will not fill the "
                               "gap with assumptions.", action="research")
        analysis = self._analyze_record(rec)
        lines = [f"Research: {question}", ""]
        if rec.get("summary"):
            lines.append(rec["summary"])
        lines.append("")
        lines.append(f"Supported findings: {rec.get('supported', 0)} · "
                     f"sources: {analysis['n_sources']} · "
                     f"hosts: {', '.join(analysis['hosts'][:4]) or '—'}")
        if rec.get("failed_sources"):
            lines.append(f"failed sources (recorded, not faked): "
                         f"{len(rec['failed_sources'])}")
        if analysis["open_questions"]:
            lines.append("")
            lines.append("Open questions (unverified — not facts):")
            for u in analysis["open_questions"][:6]:
                lines.append(f"  ? {u}")
        self.store.audit("researched", q=question[:60],
                         n=analysis["n_sources"])
        return self._reply(request, "\n".join(lines), action="research",
                           extra={"research": rec, "analysis": analysis})

    def _strategy(self, request) -> AgentResult:
        meta = request.metadata or {}
        question = str(meta.get("query") or request.text or "").strip()[:400]
        rec = self._run_research(request, question or "market analysis")
        if not rec.get("ok"):
            return self._reply(request,
                               f"No evidence base: {rec.get('error')} — "
                               "no strategy options can be derived from "
                               "nothing.", action="strategy")
        analysis = self._analyze_record(rec)
        options = self._strategy_options(question or "market analysis",
                                         rec, analysis)
        if not options:
            return self._reply(
                request,
                "No strategy options: the research returned no "
                "citation-backed findings. Options derived from "
                "uncited claims would be invented market facts — run "
                "sharper research first (metadata {mkt_action: "
                "'research', query: '…'}).",
                action="strategy")
        lines = [f"Strategy options for: {question or 'market analysis'}",
                 "(each stands only on cited findings — nothing invented)",
                 ""]
        for o in options:
            lines.append(f"{o['name']}  [{o['evidence']} source(s): "
                         f"{', '.join(o['citations'])}]")
            lines.append(f"  premise: {o['premise'][:220]}")
            lines.append("  first moves: " + "; ".join(o["first_moves"]))
            lines.append("")
        if analysis["open_questions"]:
            lines.append("Open questions the options do NOT rest on: "
                         + "; ".join(analysis["open_questions"][:3]))
        if rec.get("unverified"):
            lines.append(GUESS_LABEL + " — anything below never entered "
                         "the options above.")
        self.store.audit("strategy_built", options=len(options))
        return self._reply(request, "\n".join(lines), action="strategy",
                           extra={"options": options, "research": rec})

    def _campaign(self, request) -> AgentResult:
        meta = request.metadata or {}
        question = str(meta.get("query") or request.text or "").strip()[:400]
        option_idx = int(meta.get("option") or 0)
        rec = self._run_research(request, question or "campaign analysis")
        if not rec.get("ok"):
            return self._reply(request,
                               f"No evidence base: {rec.get('error')}.",
                               action="campaign")
        analysis = self._analyze_record(rec)
        options = self._strategy_options(question or "campaign analysis",
                                         rec, analysis)
        if not options:
            return self._reply(request,
                               "No citation-backed premise → no campaign "
                               "plan (it would execute an invented "
                               "premise).", action="campaign")
        option = options[min(option_idx, len(options) - 1)]
        plan = self._campaign_plan(question or "campaign", option, rec)
        if plan is None:
            return self._reply(request,
                               "Campaign plan rejected by the plan "
                               "validator — see the strategy options "
                               "first.", action="campaign")
        self.store.save_strategies([{"campaign": plan.goal,
                                     "citations": option["citations"],
                                     "plan_id": plan.plan_id}])
        self.store.audit("campaign_planned", plan=plan.plan_id[:60])
        body = (f"Campaign plan (inspectable Plan-and-Execute artifact, "
                f"id {plan.plan_id}):\n\n{plan.to_json(indent=1)}"
                "\n\nSteps run through the existing engine: research → "
                "draft → human review → human-executed publish. The "
                "agent does not execute outreach; each step is "
                "inspectable and the publish step requires a person.")
        return self._reply(request, body, action="campaign",
                           extra={"plan": plan.to_dict(),
                                  "plan_id": plan.plan_id,
                                  "citations": option["citations"]})

    def _full(self, request) -> AgentResult:
        """The complete workflow: research → analyze → strategy →
        campaign, one reply, every claim cited or excluded."""
        meta = request.metadata or {}
        question = str(meta.get("query") or request.text or "").strip()[:400]
        if not question:
            return self._reply(request,
                               "Give me the market/customer/competitor "
                               "question (or metadata {query: '…'}) and "
                               "I'll run the full chain.",
                               action="full", status=TaskStatus.NEEDS_INPUT)
        rec = self._run_research(request, question)
        if not rec.get("ok"):
            return self._reply(request,
                               f"Research could not run: "
                               f"{rec.get('error')}. Without evidence I "
                               "produce no strategy — no invented market "
                               "facts.", action="full")
        analysis = self._analyze_record(rec)
        options = self._strategy_options(question, rec, analysis)
        cite_list = "; ".join(
            f"[{s['index']}] {s['title'] or s['host'] or s['tier']} — "
            f"{s['url'] or 'local KB'}"
            for s in rec.get("sources") or [])
        lines = [f"Market brief: {question}", ""]
        if rec.get("summary"):
            lines.append(rec["summary"])
            lines.append("")
        lines.append(f"Sources: {cite_list or '— none —'}")
        if rec.get("failed_sources"):
            lines.append(f"Unreachable sources (recorded): "
                         f"{len(rec['failed_sources'])}")
        if analysis["open_questions"]:
            lines.append("Unverified (not facts): "
                         + "; ".join(analysis["open_questions"][:3]))
        lines.append("")
        if options:
            lines.append("Strategy options (citation-backed only):")
            for o in options:
                lines.append(f"  {o['name']} — {', '.join(o['citations'])}")
                lines.append(f"      {o['premise'][:180]}")
        else:
            lines.append("Strategy options: none — no finding reached "
                         "citation support; sharper research first.")
        opt = options[0] if options else None
        plan = self._campaign_plan(question, opt, rec) if opt else None
        if plan is not None:
            lines.append("")
            lines.append(f"Campaign plan {plan.plan_id}: "
                         f"{plan.summary_line()} — research → draft → "
                         "human review → human-executed publish "
                         "(inspectable via metadata {mkt_action: "
                         "'campaign', option: 0}).")
        self.store.audit("full_brief", q=question[:60],
                         options=len(options))
        return self._reply(request, "\n".join(lines), action="full",
                           extra={"research": rec, "options": options,
                                  "plan_id": plan.plan_id
                                  if plan else ""})

    _HANDLERS = {
        "research": _research,
        "analyze": _research,
        "strategy": _strategy,
        "strategies": _strategy,
        "campaign": _campaign,
        "plan": _campaign,
        "full": _full,
        "brief": _full,
    }
