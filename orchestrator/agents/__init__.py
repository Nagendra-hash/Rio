"""
orchestrator/agents/__init__.py — routing targets.

Five capabilities are registered so the router has real destinations. Most
are still Phase-1 stubs (NEEDS_INPUT → the orchestrator falls back to the
existing assistant); the rag_agent is the first REAL Phase-2 agent, enabled
with JARVIS_RAG_AGENT=on and scoped to its granted collections through
core/rag_service (see config/rag_callers.json).

Phase 2 replaces each remaining stub by registering a real BaseAgent under
the SAME name — the router, contracts, and orchestrator are untouched.
"""
from __future__ import annotations

import os

from orchestrator.agents import stubs
from orchestrator.base import StubAgent
from orchestrator.contracts import TaskType
from orchestrator.registry import AgentRegistry

# Canonical registry names — Phase 2 agents must reuse these names.
CODING_AGENT = "coding_agent"
RESEARCH_AGENT = "research_agent"
TRADING_AGENT = "trading_agent"
RAG_AGENT = "rag_agent"
SYSTEM_AGENT = "system_agent"
# Phase 11-13 specialists.
EMAIL_AGENT = "email_agent"
CALENDAR_AGENT = "calendar_agent"
HEALTH_AGENT = "health_agent"
BRIEFING_AGENT = "briefing_agent"
MEAL_AGENT = "meal_agent"
TRAVEL_AGENT = "travel_agent"
FINANCE_AGENT = "finance_agent"
# Phase 15 specialists.
STUDY_AGENT = "study_agent"
METAQUEST_AGENT = "metaquest_agent"
# Phase 16 specialist.
LEGAL_AGENT = "legal_agent"
# Phase 17 specialist.
SQL_AGENT = "sql_agent"
# Phase 18 growth specialists.
SELF_EVAL_AGENT = "self_eval_agent"
LEAD_AGENT = "lead_agent"
MARKETING_AGENT = "marketing_agent"
# Phase 19 news curator.
NEWS_AGENT = "news_agent"
# Phase 20 clean-room app rebuilds (replica pack).
REPLICA_AGENT = "replica_agent"


def _rag_agent():
    """The real rag_agent (Phase 2), imported lazily so a broken core.rag
    dependency cannot take the whole registry down."""
    from orchestrator.agents.rag_agent import RagAgent
    return RagAgent()


def _trading_agent():
    """The real TradingAgent, imported lazily so the package (and the
    TradingAgents action loader) can always import this module cleanly."""
    from orchestrator.agents.trading.trading_agent import TradingAgent
    return TradingAgent()


def _real_agent(import_path: str, class_name: str):
    """Lazy importer for the Phase 9-13 real agents: a broken dependency
    must never take the whole registry down — the caller falls back to the
    stub."""
    import importlib
    module = importlib.import_module(import_path)
    return getattr(module, class_name)()


def build_default_registry() -> AgentRegistry:
    """The registry: the real implementation under each canonical name when
    its JARVIS_*_AGENT gate is "on", a stub otherwise.

    (2026-10-14 fix: the coding-agent block used to `return registry` the
    moment the real CodingAgent registered — the factory then handed back a
    registry containing ONLY coding_agent and every other agent vanished
    from the Agent Center. It also could not fall through, because the stub
    below would collide with the registered real agent's name. The coding
    block now follows the same register-real-else-stub pattern as every
    other agent.)"""
    registry = AgentRegistry()
    stub = StubAgent(
        CODING_AGENT,
        "Code understanding, bug fixes, refactors, tests (Phase 2).",
        task_types=(TaskType.CODING,))
    if (os.environ.get("JARVIS_CODING_AGENT", "off").strip().lower() == "on"):
        try:
            registry.register(_real_agent("orchestrator.agents.coding_agent",
                                          "CodingAgent"))
            stub = None                    # real agent owns the name now
        except Exception as exc:
            print(f"[orchestrator] coding_agent unavailable ({exc}) — "
                  "stub stays registered")
    if stub is not None:
        registry.register(stub)
    stub = StubAgent(
        RESEARCH_AGENT,
        "Web research, news, comparisons, summaries (Phase 2).",
        task_types=(TaskType.RESEARCH,))
    if (os.environ.get("JARVIS_RESEARCH_AGENT", "off").strip().lower() == "on"):
        try:
            registry.register(_real_agent(
                "orchestrator.agents.research_agent", "ResearchAgent"))
            stub = None
        except Exception as exc:
            print(f"[orchestrator] research_agent unavailable ({exc}) — "
                  "stub stays registered")
    if stub is not None:
        registry.register(stub)
    stub = StubAgent(
        TRADING_AGENT,
        "Market analysis and paper trading. Human approval required "
        "for anything that moves money.",
        task_types=(TaskType.TRADING,),
        require_approval=True)
    if (os.environ.get("JARVIS_TRADING_AGENT", "off").strip().lower() == "on"):
        try:
            registry.register(_real_agent(
                "orchestrator.agents.trading.trading_agent", "TradingAgent"))
            stub = None
        except Exception as exc:
            print(f"[orchestrator] trading_agent unavailable ({exc}) — "
                  "stub stays registered")
    if stub is not None:
        registry.register(stub)
    if (os.environ.get("JARVIS_RAG_AGENT", "off").strip().lower() == "on"):
        try:
            registry.register(_rag_agent())
        except Exception as exc:
            print(f"[orchestrator] rag_agent unavailable ({exc}) — "
                  "stub stays registered")
            registry.register(StubAgent(
                RAG_AGENT,
                "Private documents / knowledge base Q&A (Phase 2).",
                task_types=(TaskType.RAG,)))
    else:
        registry.register(StubAgent(
            RAG_AGENT,
            "Private documents / knowledge base Q&A (Phase 2).",
            task_types=(TaskType.RAG,)))
    stub = StubAgent(
        SYSTEM_AGENT,
        "Device control, system status, app/process actions (Phase 2).",
        task_types=(TaskType.SYSTEM,),
        require_approval=True)
    if (os.environ.get("JARVIS_SYSTEM_AGENT", "off").strip().lower() == "on"):
        try:
            registry.register(_real_agent(
                "orchestrator.agents.system_agent", "SystemAgent"))
            stub = None
        except Exception as exc:
            print(f"[orchestrator] system_agent unavailable ({exc}) — "
                  "stub stays registered")
    if stub is not None:
        registry.register(stub)

    # ── Phase 9 Plan-and-Execute: env-gated like every other agent. ─────────
    # plan_agent reaches TaskType._FALLBACK by design — the router hands it a
    # plan request explicitly (metadata plan=on), not via a task-type match.
    if (os.environ.get("JARVIS_PLAN_AGENT", "off").strip().lower() == "on"):
        try:
            from orchestrator.plan_agent import PlanAgent
            registry.register(PlanAgent())
        except Exception as exc:
            print(f"[orchestrator] plan_agent unavailable ({exc}) — "
                  "not registered")

    # ── Phase 11-13 specialists: shadow-mode convention, off by default. ────
    # Each real agent is imported lazily behind its own env gate; a broken
    # dependency leaves the stub registered instead of killing the router.
    _gated = (
        ("JARVIS_EMAIL_AGENT", EMAIL_AGENT,
         "Inbox specialist: triage, summaries, newsletters, reply drafts "
         "(Phase 12). Sending always needs approval.",
         (TaskType.EMAIL,),
         "orchestrator.agents.productivity.email_agent", "EmailAgent"),
        ("JARVIS_CALENDAR_AGENT", CALENDAR_AGENT,
         "Calendar specialist: availability, conflicts, slots, invites "
         "(Phase 12). External invites always need approval.",
         (TaskType.CALENDAR,),
         "orchestrator.agents.productivity.calendar_agent",
         "CalendarAgent"),
        ("JARVIS_HEALTH_AGENT", HEALTH_AGENT,
         "Health assistant: measurements, medications, lab reports — "
         "safety-first (Phase 11).",
         (TaskType.HEALTH,),
         "orchestrator.agents.health.health_agent", "HealthAgent"),
        ("JARVIS_BRIEFING_AGENT", BRIEFING_AGENT,
         "Daily briefing: calendar, important email, tasks, weather, "
         "news, optional market summary; audio-ready (Phase 13).",
         (TaskType.BRIEFING,),
         "orchestrator.agents.dailylife.briefing_agent", "BriefingAgent"),
        ("JARVIS_MEAL_AGENT", MEAL_AGENT,
         "Meals & groceries: pantry/expiry, weekly menu, grocery list, "
         "budget from your own price hints (Phase 13).",
         (TaskType.MEAL,),
         "orchestrator.agents.dailylife.meal_agent", "MealAgent"),
        ("JARVIS_TRAVEL_AGENT", TRAVEL_AGENT,
         "Travel: flight/train/bus/road/hotel search, route and price "
         "comparison over supplied data, itineraries, watches (Phase 13).",
         (TaskType.TRAVEL,),
         "orchestrator.agents.dailylife.travel_agent", "TravelAgent"),
        ("JARVIS_FINANCE_AGENT", FINANCE_AGENT,
         "Personal finance: receipt parsing, expense tracking, budgets, "
         "recurring expenses, overspending, summaries. Data stays in the "
         "finance vault; imports and edits need approval; never gives "
         "investment advice (Phase 14).",
         (TaskType.FINANCE,),
         "orchestrator.agents.finance.finance_agent", "FinanceAgent"),
        ("JARVIS_STUDY_AGENT", STUDY_AGENT,
         "Study partner: learning goals, study plans, explanations, "
         "quizzes, progress tracking, adaptive difficulty (Phase 15). "
         "Reads your study library via RAG.",
         (TaskType.STUDY,),
         "orchestrator.agents.study.study_agent", "StudyAgent"),
        ("JARVIS_METAQUEST_AGENT", METAQUEST_AGENT,
         "Meta Quest knowledge specialist: searches the dedicated Quest "
         "manual/docs collection and answers with citations + product and "
         "version metadata (Phase 15).",
         (TaskType.META_QUEST,),
         "orchestrator.agents.metaquest.metaquest_agent", "MetaQuestAgent"),
        ("JARVIS_LEGAL_AGENT", LEGAL_AGENT,
         "Legal document analysis: reads legal PDFs, extracts clauses with "
         "page citations, summarizes sections, compares versions, retrieves "
         "passages, flags risks and questions for human review. Not legal "
         "advice; ingest/forget need approval; documents live in the "
         "isolated legal_docs collection (Phase 16).",
         (TaskType.LEGAL,),
         "orchestrator.agents.legal.legal_agent", "LegalAgent"),
        ("JARVIS_SQL_AGENT", SQL_AGENT,
         "SQL agent: inspects permitted databases, converts natural "
         "language to SQL, validates every statement, runs read-only "
         "SELECTs by default, and parks anything that writes, deletes or "
         "drops behind approval. Never exposes credentials; blocked SQL "
         "never runs even approved (Phase 17).",
         (TaskType.SQL,),
         "orchestrator.agents.sql.sql_agent", "SqlAgent"),
        ("JARVIS_SELF_EVAL_AGENT", SELF_EVAL_AGENT,
         "Self-evaluation: goals, activities, evidence-computed results, "
         "a reflection loop with hard ceilings, and an improvement plan "
         "derived from flagged gaps only. Never deletes data and never "
         "pads a stalled goal (Phase 18).",
         (TaskType.SELF_EVAL,),
         "orchestrator.agents.growth.self_eval_agent", "SelfEvalAgent"),
        ("JARVIS_LEAD_AGENT", LEAD_AGENT,
         "Lead scoring: ingests permitted lead data behind approval, "
         "applies user-defined rules deterministically, explains every "
         "score with a per-rule breakdown, and prioritizes outreach. "
         "Scores are advisory — a human decides anything consequential; "
         "executing outreach is refused (Phase 18).",
         (TaskType.LEAD_SCORE,),
         "orchestrator.agents.growth.lead_agent", "LeadAgent"),
        ("JARVIS_MARKETING_AGENT", MARKETING_AGENT,
         "Marketing: reuses the research agent over permitted sources, "
         "derives citation-backed strategy options, and produces "
         "campaign ideas as inspectable Plan-and-Execute artifacts. "
         "Cites research evidence or labels a guess; never executes "
         "outreach (Phase 18).",
         (TaskType.MARKETING,),
         "orchestrator.agents.growth.marketing_agent", "MarketingAgent"),
        ("JARVIS_NEWS_AGENT", NEWS_AGENT,
         "News Curator: top headlines or topic news through the existing "
         "web_search news pipeline (CurrentsAPI → Gemini/DDG), formatted "
         "as a sourced digest. Read-only; reports honestly when every "
         "backend is unavailable (Phase 19).",
         (TaskType.NEWS,),
         "orchestrator.agents.news_agent", "NewsCuratorAgent"),
        ("JARVIS_REPLICA_AGENT", REPLICA_AGENT,
         "Clean-room app rebuilds from a Google Play listing: recon the app, "
         "plan an Android build, read its reviews to find what to improve, "
         "rebuild it as your own app, then verify parity and rebrand before "
         "it ships. Never decompiles the original or copies its code, assets "
         "or branding (Phase 20).",
         (TaskType.REPLICA,),
         "orchestrator.agents.replica.replica_agent", "ReplicaAgent"),
    )
    for env_gate, agent_name, blurb, types, module, cls in _gated:
        stub = StubAgent(agent_name, blurb, task_types=types)
        if os.environ.get(env_gate, "off").strip().lower() == "on":
            try:
                registry.register(_real_agent(module, cls))
            except Exception as exc:
                print(f"[orchestrator] {agent_name} unavailable ({exc}) — "
                      "stub stays registered")
                registry.register(stub)
        else:
            registry.register(stub)
    return registry


__all__ = [
    "CODING_AGENT", "RESEARCH_AGENT", "TRADING_AGENT", "RAG_AGENT",
    "SYSTEM_AGENT", "EMAIL_AGENT", "CALENDAR_AGENT", "HEALTH_AGENT",
    "BRIEFING_AGENT", "MEAL_AGENT", "TRAVEL_AGENT", "FINANCE_AGENT",
    "STUDY_AGENT", "METAQUEST_AGENT", "LEGAL_AGENT", "SQL_AGENT",
    "SELF_EVAL_AGENT", "LEAD_AGENT", "MARKETING_AGENT", "NEWS_AGENT",
    "REPLICA_AGENT",
    "StubAgent", "stubs", "build_default_registry",
]
