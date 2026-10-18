"""run_agent.py — dispatch a task to ANY orchestrator agent, from chat.

Why: the 21-agent registry was reachable only through the dashboard HTTP API
(POST /api/rio/agents/run). Voice and Hermes-inbound conversations (the
/api/message → command-queue path) could not execute a single one — the
orchestrator classified those turns in shadow mode and nothing dispatched.
This action closes that gap: one action, every agent, same dispatch semantics
as the Agent Center (direct agent.handle, journal result, honest statuses).

Mapping to the Hermes bridge: inbound Telegram/WhatsApp text that reaches
JARVIS can now run any agent by name ("ask the coding agent to fix X",
"what's in my pantry" → meal_agent); outbound results return as text or ride
hermes_task-style delivery. The agent fleet gates default to on in main.py,
so a registered REAL implementation answers; a stub answers honestly.

House rules honoured:
- stdlib only, no new dependencies.
- Never hang the caller: handle() runs in a worker thread and this waits at
  most _TIMEOUT_S (the thread itself finishes in the background and journals
  the real result either way — a chat timeout must not lie about the run).
- Approvals stay approvals: NEEDS_APPROVAL / DENIED / DECLINED are relayed
  verbatim, never converted into fake success.
- Autonomy pause is respected: a paused agent is skipped with a reason, the
  same way the dashboard skips it.
"""
from __future__ import annotations

import threading
import time

_TIMEOUT_S = 180          # chat-visible wait; coding/backtests can run long
_LIST_TIMEOUT_S = 30

# Friendly keys → registry names (the keys the RIO Agent Center uses).
_ALIASES = {
    "coding": "coding_agent", "research": "research_agent",
    "trading": "trading_agent", "rag": "rag_agent",
    "system": "system_agent", "email": "email_agent", "inbox": "email_agent",
    "calendar": "calendar_agent", "health": "health_agent",
    "briefing": "briefing_agent", "meal": "meal_agent",
    "travel": "travel_agent", "finance": "finance_agent",
    "study": "study_agent", "metaquest": "metaquest_agent",
    "quest": "metaquest_agent", "legal": "legal_agent", "sql": "sql_agent",
    "selfeval": "self_eval_agent", "self-eval": "self_eval_agent",
    "lead": "lead_agent", "marketing": "marketing_agent",
    "news": "news_agent", "plan": "plan_agent",
    "replica": "replica_agent", "clone": "replica_agent",
    "youtube": "youtube_agent", "yt": "youtube_agent",
    "channel": "youtube_agent",
}


def _registry():
    from orchestrator import integration as oi
    orch = oi.shared_orchestrator()
    return orch, orch.registry


def _paused_agents() -> set[str]:
    """Agents disabled in the autonomy engine (best effort; empty on doubt)."""
    try:
        from dashboard.autonomy_api import shared_engine
        bundle = shared_engine()
        if bundle:
            eng = bundle["engine"]
            return {str(k) for k, v in eng._agent_enabled.items() if not v}
    except Exception:
        pass
    return set()


def _resolve(registry, name: str):
    """'coding' / 'coding_agent' → registry entry, or (None, reason)."""
    key = (name or "").strip().lower().replace(" ", "_")
    key = _ALIASES.get(key, key)
    if not key.endswith("_agent"):
        key = f"{key}_agent"
    agent = registry.get(key)
    if agent is None:
        known = ", ".join(sorted(
            a.name.replace("_agent", "") for a in registry))
        return None, (f"no such agent: '{name}'. Known agents: {known}")
    return agent, key


def _inventory(registry) -> str:
    lines = []
    for a in registry:
        real = type(a).__name__ != "StubAgent"
        lanes = "/".join(t.value for t in a.task_types) or "—"
        lines.append(f"• {a.name.replace('_agent', '')}"
                     f"{'' if real else ' (stub — not implemented)'}: "
                     f"{a.description or lanes}")
    return ("Registered agents — call run_agent with agent=<name> and task="
            "<what to do>:\n" + "\n".join(lines))


def run_agent(parameters: dict, player=None) -> str:
    """Dispatch one task to a named orchestrator agent; return its reply."""
    parameters = parameters or {}
    name = str(parameters.get("agent", "")).strip()
    task = str(parameters.get("task", "")).strip()
    attachments = parameters.get("attachments") or []
    # Optional per-agent metadata pass-through (e.g. trading: symbol, period,
    # interval, trading_action; any agent: task-type hints). Unknown keys are
    # harmless — agents ignore metadata they do not read.
    extra_meta = {
        k: parameters[k] for k in
        ("symbol", "period", "interval", "trading_action", "strategy",
         "capital", "risk_pct", "min_score", "repo_root", "repo")
        if parameters.get(k) not in (None, "")
    }

    try:
        orch, registry = _registry()
    except Exception as exc:
        return f"The agent registry is unavailable ({type(exc).__name__}: {exc})."

    if not name or name.lower() in ("list", "inventory", "all"):
        return _inventory(registry)

    agent, reg_name = _resolve(registry, name)
    if agent is None:
        return reg_name  # the resolution reason text (no such agent: …)

    if type(agent).__name__ == "StubAgent":
        return (f"The {reg_name.replace('_agent', '')} agent is only a "
                f"registered placeholder — nothing executable is behind it.")

    if reg_name in _paused_agents():
        return (f"The {reg_name.replace('_agent', '')} agent is paused in the "
                f"autonomy engine. Resume it in the dashboard (Agents page) "
                f"and try again.")

    if not task:
        return (f"What should the {reg_name.replace('_agent', '')} agent do? "
                f"Give me the task and I'll dispatch it.")

    from orchestrator.contracts import AgentRequest, TaskType, new_task_id
    request = AgentRequest(
        task_id=new_task_id("agentrun"),
        task_type=agent.task_types[0] if agent.task_types else TaskType._FALLBACK,
        text=task,
        attachments=[str(a) for a in attachments if str(a).strip()],
        metadata={"agent_override": reg_name, "channel": "chat_dispatch",
                  "session_id": "run_agent_action", **extra_meta},
    )

    outcome: dict = {"done": False, "status": "failed", "text": "", "error": ""}

    def _work():
        try:
            result = agent.handle(request)
            try:
                agent.last_request = request
                agent.last_result = result
            except Exception:
                pass
            outcome["status"] = str(
                result.status.value if result.status else "failed")
            outcome["text"] = str(result.text or "")
            if result.error:
                outcome["error"] = str(result.error)
        except Exception as exc:                    # never silent
            outcome["status"] = "failed"
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            outcome["done"] = True
            try:
                ok = outcome["status"] in ("completed", "needs_input")
                orch.journal.agent_result(
                    request.task_id, reg_name,
                    "completed" if ok else "failed",
                    (outcome["error"] or outcome["text"])[:200])
            except Exception:
                pass

    threading.Thread(target=_work, daemon=True, name="run-agent-action").start()
    deadline = time.monotonic() + _TIMEOUT_S
    while not outcome["done"] and time.monotonic() < deadline:
        time.sleep(0.25)

    label = reg_name.replace("_agent", "")
    if not outcome["done"]:
        return (f"The {label} agent is still working — it has exceeded the "
                f"{_TIMEOUT_S}s chat budget. Its result will land in the "
                f"journal; check the Agent Center history shortly.")

    status = outcome["status"]
    body = outcome["text"] or outcome["error"] or "(no output)"
    if status == "completed":
        return f"[{label} agent] {body}"
    if status == "needs_input":
        return f"[{label} agent needs input] {body}"
    if status == "needs_approval":
        return (f"[{label} agent needs your approval] {body} — approve it in "
                f"the dashboard if you want it executed.")
    if status == "denied":
        return f"[{label} agent refused] {body}"
    if status == "declined":
        return f"[{label} agent declined this task] {body}"
    return f"[{label} agent failed] {body}"


TOOL = {
    "name": "run_agent",
    "description": (
        "Run a task on one of JARVIS's specialist agents and return its "
        "answer. Agents: coding, research, trading (paper-only), rag, system, "
        "email/inbox, calendar, health, briefing, meal, travel, finance, "
        "study, metaquest, legal, sql, self_eval, lead, marketing, news, "
        "plan. Use when the user names an agent or asks for a specialist's "
        "kind of work ('run the coding agent on this traceback', 'ask the "
        "meal agent what to cook', 'briefing agent, morning summary'). "
        "Call with agent='list' to enumerate the fleet. The result may be an "
        "approval request — relay it, never pretend it succeeded."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "agent": {
                "type": "STRING",
                "description": "Agent name or key, e.g. 'coding' or "
                               "'coding_agent'. 'list' enumerates the fleet."
            },
            "task": {
                "type": "STRING",
                "description": "The complete instruction for that agent."
            },
            "attachments": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Optional local file paths the agent may read "
                               "(e.g. a received PDF)."
            }
        },
        "required": ["agent"]
    },
    "handler": run_agent,
}
