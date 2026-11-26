"""
orchestrator/agents/browser/browser_agent.py — RIO's browser specialist.

Registered as `browser_agent` behind the `JARVIS_BROWSER_AGENT` gate and
routed by `TaskType.BROWSER`. It owns no browser code: everything goes through
`core/browser_agent/`, the same runtime, policy and job log the voice tool
(`actions/browser_agent.py`) uses. One implementation, two entry points.

WHY EXECUTION IS GATED
    `orchestrator.integration.observe_user_turn()` calls `submit()` even in
    shadow mode, which is the default, and `dashboard/server.py` then enqueues
    the same turn for the conversation path anyway. If this agent executed
    every browser turn it saw there, one Telegram message would run the task
    twice — once in the HTTP request thread and once through the model's tool
    call. So the agent executes only when the caller asks it to:

        channel="rio_agent_center"  (Agent Center)
        channel="chat_dispatch"     (actions/run_agent.py)
        metadata["browser_execute"] = True

    Anything else returns DECLINED with a reason. The router then falls back to
    the existing assistant, which reaches the same runtime through the
    `browser_agent` tool — the documented Voice/Text → RIO → agent → browser
    flow, with no duplicate launch.

STATUS FIDELITY
    COMPLETED only when the browser run actually finished and produced text.
    NEEDS_APPROVAL / DENIED / FAILED / NEEDS_INPUT carry the real reason; a
    started-but-unfinished job is never reported as done.
"""

from __future__ import annotations

import re

from orchestrator.base import BaseAgent
from orchestrator.contracts import AgentRequest, AgentResult, TaskStatus

_EXECUTING_CHANNELS = frozenset({"rio_agent_center", "chat_dispatch"})
_DEFAULT_WAIT_S = 150.0

_BROWSER_TEXT_RE = re.compile(
    r"\b(?:browser|web\s?page|website|click|fill\s+(?:in\s+)?(?:the\s+)?form|"
    r"download|log\s?in|sign\s?in|tab|navigate|open\s+\S+\s+and)\b",
    re.IGNORECASE,
)


def _wants_execution(request: AgentRequest) -> bool:
    meta = request.metadata or {}
    if meta.get("browser_execute") is True:
        return True
    return str(meta.get("channel") or "").strip().lower() in _EXECUTING_CHANNELS


def _job_action(request: AgentRequest, text: str) -> str:
    meta = request.metadata or {}
    explicit = str(meta.get("action") or "").strip().lower()
    if explicit in ("run", "status", "cancel", "list"):
        return explicit
    lowered = text.lower().strip()
    if re.match(r"^(?:list|show)\b.*\b(?:browser\s+)?(?:jobs?|tasks?)\b", lowered):
        return "list"
    if re.search(r"\bcancel\b.*\b(?:browser|job|task)\b", lowered):
        return "cancel"
    if re.search(r"\b(?:status|progress|how\s+far|still\s+running)\b", lowered):
        return "status"
    return "run"


class BrowserAgent(BaseAgent):
    name = "browser_agent"
    description = (
        "Drives a real browser for multi-step work: navigate, click, fill forms, "
        "log into saved sessions, read JavaScript-rendered pages, compare sites, "
        "download and summarise files. Local Chrome, free/local models only; risky "
        "actions (send, publish, buy, trade, transfer, delete, security changes) "
        "require human approval."
    )
    task_types: tuple = ()
    priority = 10

    def __init__(self, cfg=None, runtime=None) -> None:
        # TaskType.BROWSER is imported lazily so this module still imports if an
        # older contracts.py is on the path (the gate then simply never fires).
        from orchestrator.contracts import TaskType

        self.task_types = (TaskType.BROWSER,)
        self._cfg = cfg
        self._runtime = runtime
        self.last_request = None
        self.last_result = None

    # ── dependencies (lazily, so a missing venv never breaks the registry) ──
    def _config(self):
        if self._cfg is None:
            from core.browser_agent import load_config

            self._cfg = load_config()
        return self._cfg

    def _rt(self):
        if self._runtime is None:
            from core.browser_agent import shared_runtime

            self._runtime = shared_runtime()
        return self._runtime

    # ── routing ───────────────────────────────────────────────────────────
    def can_handle(self, request: AgentRequest) -> bool:
        try:
            if request.task_type in self.task_types:
                return True
            # An explicit override still needs a browser-shaped ask; the router
            # only reaches this agent for TaskType.BROWSER otherwise.
            return bool(
                str((request.metadata or {}).get("agent_override") or "") == self.name
                and _BROWSER_TEXT_RE.search(str(request.text or ""))
            )
        except Exception:
            return False

    # ── entry point ───────────────────────────────────────────────────────
    def _handle_impl(self, request: AgentRequest) -> AgentResult:
        try:
            return self._dispatch(request)
        except Exception as exc:  # never break the router
            return self._fail(request, f"{type(exc).__name__}: {str(exc)[:200]}")

    def _dispatch(self, request: AgentRequest) -> AgentResult:
        text = str(request.text or "").strip()
        meta = request.metadata or {}
        action = _job_action(request, text)

        if action in ("status", "cancel", "list"):
            return self._job_control(request, action, str(meta.get("job_id") or "").strip())

        if not text:
            return AgentResult(
                task_id=request.task_id,
                status=TaskStatus.NEEDS_INPUT,
                text="What should the browser do? Give me the task and I'll run it.",
                agent=self.name,
            )

        from core.browser_agent import policy

        decision = policy.classify(text)
        if decision.verdict is policy.Verdict.DENY:
            return AgentResult(
                task_id=request.task_id,
                status=TaskStatus.DENIED,
                text=f"Refused: {decision.reason}.",
                error=decision.reason,
                data={"policy": decision.public()},
                agent=self.name,
            )

        approved = bool(meta.get("approved"))
        if decision.verdict is policy.Verdict.NEEDS_APPROVAL and not approved:
            return AgentResult(
                task_id=request.task_id,
                status=TaskStatus.NEEDS_APPROVAL,
                text=(
                    f"This needs your approval before I run it: {text[:200]}\n"
                    f"{decision.reason}."
                ),
                data={
                    "approval_summary": f"{decision.category}: {text[:160]}",
                    "policy": decision.public(),
                },
                agent=self.name,
            )

        if not _wants_execution(request):
            return AgentResult(
                task_id=request.task_id,
                status=TaskStatus.DECLINED,
                text=(
                    "Browser work is handled by the conversation path for this "
                    "channel; nothing was started here."
                ),
                data={"reason": "not_an_execution_channel"},
                agent=self.name,
            )

        from core.browser_agent import runtime as rt_mod

        ok, version, reason = rt_mod.preflight()
        if not ok:
            return self._fail(
                request,
                f"the browser runtime is not installed — {reason}. "
                "Install it with: ~/.jarvis-browser-venv/bin/pip install browser-use==1.0.5",
            )

        rt = self._rt()
        started = rt.start(
            text,
            confirmed=approved,
            mode=str(meta.get("mode") or "auto"),
            max_steps=meta.get("max_steps"),
            visible=bool(meta.get("visible", False)),
            auth=bool(meta.get("auth", False)),
            site=(str(meta["site"]) if meta.get("site") else None),
            domains=str(meta.get("domains") or ""),
            wait=False,
        )

        if started.status == "delegated":
            return AgentResult(
                task_id=request.task_id,
                status=TaskStatus.DECLINED,
                text=started.error,
                data={"plan": started.plan, "reason": "delegated"},
                agent=self.name,
            )
        if started.status != "running":
            return self._from_result(request, started)

        budget = meta.get("wait_s")
        try:
            budget_s = float(budget) if budget is not None else _DEFAULT_WAIT_S
        except (TypeError, ValueError):
            budget_s = _DEFAULT_WAIT_S

        result = rt.wait(started.job_id, timeout=max(10.0, budget_s))
        if result.status == "timeout":
            return AgentResult(
                task_id=request.task_id,
                status=TaskStatus.NEEDS_INPUT,
                text=(
                    f"The browser task is still running (job {started.job_id}). "
                    f"Ask for 'browser status {started.job_id}' to collect the "
                    "result, or cancel it."
                ),
                data={"job_id": started.job_id, "status": "running"},
                agent=self.name,
            )
        return self._from_result(request, result)

    # ── helpers ───────────────────────────────────────────────────────────
    def _job_control(self, request: AgentRequest, action: str, job_id: str) -> AgentResult:
        rt = self._rt()
        if action == "list":
            active = rt.active()
            text = (
                "No browser tasks are running."
                if not active
                else "Running browser tasks:\n"
                + "\n".join(f"• {j['job_id']} ({j['elapsed_s']}s): {j['task']}" for j in active)
            )
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.COMPLETED,
                text=text, data={"active": active}, agent=self.name,
            )

        if not job_id:
            active = rt.active()
            if len(active) == 1:
                job_id = active[0]["job_id"]
            else:
                return AgentResult(
                    task_id=request.task_id,
                    status=TaskStatus.NEEDS_INPUT,
                    text="Which job? Pass job_id. Running: "
                    + (", ".join(j["job_id"] for j in active) or "none"),
                    agent=self.name,
                )

        if action == "cancel":
            result = rt.cancel(job_id)
            status = (
                TaskStatus.COMPLETED if result.status == "cancelled" else TaskStatus.FAILED
            )
            return AgentResult(
                task_id=request.task_id, status=status,
                text=result.text or result.render(self._config()),
                data={"job_id": job_id, "status": result.status},
                agent=self.name,
            )

        result = rt.status(job_id)
        if result.status == "running":
            return AgentResult(
                task_id=request.task_id,
                status=TaskStatus.NEEDS_INPUT,
                text=f"{job_id} is still running ({result.duration_s:.0f}s elapsed).",
                data={"job_id": job_id, "status": "running"},
                agent=self.name,
            )
        return self._from_result(request, result)

    def _from_result(self, request: AgentRequest, result) -> AgentResult:
        cfg = self._config()
        data = result.public()
        if result.plan:
            data["plan"] = result.plan

        if result.ok:
            text = result.render(cfg)
            if result.downloads:
                text += "\nDownloads: " + ", ".join(result.downloads)
            return AgentResult(
                task_id=request.task_id,
                status=TaskStatus.COMPLETED,
                text=text,
                data=data,
                agent=self.name,
            )

        status_map = {
            "denied": TaskStatus.DENIED,
            "needs_approval": TaskStatus.NEEDS_APPROVAL,
            "cancelled": TaskStatus.DENIED,
            "unavailable": TaskStatus.FAILED,
            "duplicate": TaskStatus.NEEDS_INPUT,
            "busy": TaskStatus.NEEDS_INPUT,
            "delegated": TaskStatus.DECLINED,
        }
        status = status_map.get(result.status, TaskStatus.FAILED)
        payload = {
            "approval_summary": (
                f"browser task: {result.task[:160]}" if status is TaskStatus.NEEDS_APPROVAL else ""
            )
        }
        payload.update(data)
        return AgentResult(
            task_id=request.task_id,
            status=status,
            text=result.text or result.render(cfg),
            error=result.error,
            data=payload,
            agent=self.name,
        )

    def _fail(self, request: AgentRequest, reason: str) -> AgentResult:
        return AgentResult(
            task_id=request.task_id,
            status=TaskStatus.FAILED,
            text=f"The browser agent hit an error: {reason}. Nothing was run.",
            error=reason,
            agent=self.name,
        )
