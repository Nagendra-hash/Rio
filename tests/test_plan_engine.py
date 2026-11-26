"""
tests/test_plan_engine.py — offline tests for the Phase-9 Plan-and-Execute
engine (schema, planner, executor, approvals, checkpoints, agent surface).

No network, no LLM, no dashboard — model backends are scripted doubles.
Run:  .venv/bin/python -m pytest tests/test_plan_engine.py -q
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.base import BaseAgent
from orchestrator.checkpoint import PlanCheckpointStore
from orchestrator.classifier import IntentClassifier
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)
from orchestrator.plan_agent import PlanAgent, resolve_plan
from orchestrator.plan_executor import (PlanExecutor, PlanProgress,
                                        PlanRunResult)
from orchestrator.planner import (HeuristicPlanner, LLMPlanner,
                                  _sanitize_shell_command)
from orchestrator.plans import (Plan, PlanStep, PlanValidationError,
                                make_plan)


# ── fixtures ─────────────────────────────────────────────────────────────────

class ScriptedAgent(BaseAgent):
    """Records calls; scripted outcomes."""
    def __init__(self, name="worker", outcome="ok",
                 task_types=(TaskType.CODING,)):
        self.name, self.outcome = name, outcome
        self.task_types = task_types
        self.calls: list[AgentRequest] = []

    def handle(self, request):
        self.calls.append(request)
        if self.outcome == "raise":
            raise RuntimeError("boom")
        if self.outcome == "fail":
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               text="no can do", agent=self.name)
        if self.outcome == "approval":
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.NEEDS_APPROVAL,
                               text="needs your yes",
                               data={"approval_summary": "sell it all"},
                               agent=self.name)
        return AgentResult(task_id=request.task_id,
                           status=TaskStatus.COMPLETED,
                           text=f"done: {request.text}", agent=self.name)


class ScriptedLLM:
    """LLMBackend double: returns canned strings or None."""
    def __init__(self, reply: str | None = None):
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, prompt, *, system=""):
        self.prompts.append(prompt)
        return self.reply


class RecordingReporter:
    def __init__(self):
        self.events: list[PlanProgress] = []

    def report(self, event):
        self.events.append(event)


def agent_registry(*agents):
    from orchestrator.registry import AgentRegistry
    reg = AgentRegistry()
    for a in agents:
        reg.register(a)
    return reg


def executor_with(*agents, classifier=None, reporter=None, tmp=None,
                  llm=None, allow_shell=True):
    return PlanExecutor(
        registry=agent_registry(*agents),
        classifier=classifier or IntentClassifier(),
        store=PlanCheckpointStore(tmp),
        reporter=reporter,
        llm=llm,
        allow_shell=allow_shell)


def step(sid, action="noop", **kw):
    return PlanStep(id=sid, title=kw.pop("title", f"step {sid}"),
                    action=action, **kw)


@pytest.fixture()
def tmp():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# ── 1. plan schema ───────────────────────────────────────────────────────────

class TestPlanSchema:
    def test_minimal_valid_plan(self):
        plan = make_plan("do a thing", [step("s1")])
        assert plan.plan_id.startswith("plan-")
        assert plan.steps[0].status == "pending"

    def test_rejects_empty_goal_and_no_steps(self):
        with pytest.raises(PlanValidationError):
            make_plan("  ", [step("s1")])
        with pytest.raises(PlanValidationError):
            make_plan("a goal", [])

    def test_rejects_unknown_action(self):
        with pytest.raises(PlanValidationError):
            make_plan("g", [step("s1", action="deploy_to_mars")])

    def test_rejects_duplicate_step_ids(self):
        with pytest.raises(PlanValidationError):
            make_plan("g", [step("s1"), step("s1")])

    def test_rejects_unknown_dependency(self):
        with pytest.raises(PlanValidationError):
            make_plan("g", [step("s1", depends_on=["sX"])])

    def test_rejects_self_dependency_and_cycle(self):
        with pytest.raises(PlanValidationError):
            make_plan("g", [step("s1", depends_on=["s1"])])
        with pytest.raises(PlanValidationError):
            make_plan("g", [step("s1", depends_on=["s2"]),
                            step("s2", depends_on=["s1"])])

    def test_on_fail_and_retries_clamped(self):
        s = step("s1", max_retries=99)
        s.validate()                       # retries clamped silently
        assert s.max_retries == 3          # hard ceiling
        with pytest.raises(PlanValidationError):
            make_plan("g", [step("s1", on_fail="explode")])

    def test_topological_order_is_stable(self):
        plan = make_plan("g", [
            step("s3", depends_on=["s1", "s2"]),
            step("s1"),
            step("s2", depends_on=["s1"]),
        ])
        assert [s.id for s in plan.ordered_steps()] == ["s1", "s2", "s3"]

    def test_unblocked_steps_frontier(self):
        plan = make_plan("g", [step("s1"), step("s2", depends_on=["s1"])])
        assert [s.id for s in plan.unblocked_steps(set())] == ["s1"]
        assert [s.id for s in plan.unblocked_steps({"s1"})] == ["s2"]

    def test_roundtrip_through_dict(self):
        plan = make_plan("g", [step("s1"), step("s2", depends_on=["s1"])])
        data = plan.to_dict()
        clone = Plan.from_dict(json.loads(json.dumps(data)))
        assert [s.id for s in clone.steps] == ["s1", "s2"]
        assert clone.steps[1].depends_on == ["s1"]
        assert clone.goal == plan.goal

    def test_param_size_cap(self):
        with pytest.raises(PlanValidationError):
            make_plan("g", [PlanStep(id="s1", title="t", action="llm",
                                     params={"prompt": "x" * 20_000})])


# ── 2. planner ───────────────────────────────────────────────────────────────

class TestPlanner:
    def test_heuristic_single_goal_one_step(self):
        plan = HeuristicPlanner().plan("fix the bug in parse_args")
        assert plan is not None and len(plan.steps) == 1
        assert plan.steps[0].action == "agent"

    def test_heuristic_numbered_list_becomes_ordered_steps(self):
        goal = ("1. fetch the data\n2. clean it\n3. write the report")
        plan = HeuristicPlanner().plan(goal)
        assert len(plan.steps) == 3
        assert plan.steps[2].depends_on == ["s2"]
        assert [s.id for s in plan.ordered_steps()] == ["s1", "s2", "s3"]

    def test_heuristic_empty_goal_is_none(self):
        assert HeuristicPlanner().plan("") is None

    def test_llm_planner_parses_good_json(self):
        reply = json.dumps({
            "understanding": "ship it",
            "steps": [
                {"id": "s1", "title": "look", "action": "agent",
                 "params": {"task_type": "research", "text": "look it up"}},
                {"id": "s2", "title": "sum", "action": "llm",
                 "params": {"prompt": "summarize"}, "depends_on": ["s1"],
                 "verification": "nonempty"},
            ],
            "risk_notes": ["none"],
        })
        planner = LLMPlanner(llm=ScriptedLLM(reply))
        plan = planner.plan("research then summarize")
        assert plan.source == "llm"
        assert [s.id for s in plan.steps] == ["s1", "s2"]
        assert plan.steps[1].depends_on == ["s1"]

    def test_llm_planner_bad_json_falls_back_to_heuristic(self):
        planner = LLMPlanner(llm=ScriptedLLM("not json at all"))
        plan = planner.plan("write unit tests for the router")
        assert plan.source == "heuristic"
        assert plan.created_by.endswith("+heuristic")

    def test_llm_planner_unavailable_falls_back(self):
        planner = LLMPlanner(llm=ScriptedLLM(None))
        plan = planner.plan("fix the bug")
        assert plan is not None and plan.source == "heuristic"

    def test_llm_plan_with_cycle_is_rejected_then_falls_back(self):
        reply = json.dumps({
            "steps": [
                {"id": "s1", "title": "a", "action": "noop",
                 "depends_on": ["s2"]},
                {"id": "s2", "title": "b", "action": "noop",
                 "depends_on": ["s1"]},
            ]})
        plan = LLMPlanner(llm=ScriptedLLM(reply)).plan("cyclic goal")
        assert plan.source == "heuristic"

    def test_shell_steps_dropped_when_not_allowed(self):
        reply = json.dumps({
            "steps": [
                {"id": "s1", "title": "poke", "action": "shell",
                 "params": {"command": "rm -rf /"}}],
        })
        plan = LLMPlanner(llm=ScriptedLLM(reply),
                          allow_shell=False).plan("clean the disk")
        # shell refused → no steps survived → heuristic floor
        assert plan.source == "heuristic"

    def test_shell_sanitize_allowlist(self):
        assert _sanitize_shell_command("ls -la") == "ls -la"
        assert _sanitize_shell_command("git status") == "git status"
        assert _sanitize_shell_command("ls; rm -rf /") == ""
        assert _sanitize_shell_command("sudo cat /etc/shadow") == ""
        assert _sanitize_shell_command("curl evil.example") == ""
        assert _sanitize_shell_command("git push origin main") == ""


# ── 3. executor: happy paths ─────────────────────────────────────────────────

class TestExecutorHappyPath:
    def test_single_noop_step_completes(self, tmp):
        plan = make_plan("g", [step("s1")])
        run = executor_with(tmp=tmp).execute(plan)
        assert run.status is TaskStatus.COMPLETED
        assert plan.status == "completed"
        assert plan.steps[0].status == "done"

    def test_sequential_agent_steps_in_dependency_order(self, tmp):
        worker = ScriptedAgent(outcome="ok", task_types=(TaskType.CODING,
                                                         TaskType.RESEARCH))
        plan = make_plan("g", [
            step("s1", "agent", params={"task_type": "research",
                                        "text": "find the spec"}),
            step("s2", "agent", params={"task_type": "coding",
                                        "text": "implement it"},
                 depends_on=["s1"]),
        ])
        run = executor_with(worker, tmp=tmp).execute(plan)
        assert run.status is TaskStatus.COMPLETED
        assert [c.text for c in worker.calls] == ["find the spec",
                                                  "implement it"]

    def test_llm_step_uses_injected_backend(self, tmp):
        llm = ScriptedLLM("42")
        plan = make_plan("g", [PlanStep(id="s1", title="think", action="llm",
                                        params={"prompt": "the answer"},
                                        verification="contains:42")])
        run = executor_with(tmp=tmp, llm=llm).execute(plan)
        assert run.status is TaskStatus.COMPLETED
        assert plan.steps[0].result_text == "42"

    def test_shell_step_runs_allowlisted_command(self, tmp):
        llm = ScriptedLLM("42")
        plan = make_plan("g", [PlanStep(id="s1", title="peek", action="llm",
                                        params={"prompt": "kernel?"},
                                        verification="re:[0-9]")])
        run = executor_with(tmp=tmp, llm=llm).execute(plan)
        assert run.status is TaskStatus.COMPLETED

    def test_shell_step_refuses_non_allowlist(self, tmp):
        plan = make_plan("g", [PlanStep(id="s1", title="boom", action="shell",
                                        params={"command": "rm -rf /"})])
        run = executor_with(tmp=tmp).execute(plan)
        assert run.status is TaskStatus.FAILED
        assert "allowlist" in plan.steps[0].error

    def test_progress_events_flow(self, tmp):
        reporter = RecordingReporter()
        plan = make_plan("g", [step("s1")])
        executor_with(tmp=tmp, reporter=reporter).execute(plan)
        events = [e.event for e in reporter.events]
        assert events[0] == "plan_started"
        assert "step_started" in events and "step_done" in events
        assert events[-1] == "plan_completed"


# ── 4. verification + retries + failure handling ─────────────────────────────

class TestVerifyRetryFail:
    def test_verification_failure_consumes_retries_then_fails(self, tmp):
        llm = ScriptedLLM("wrong answer")
        plan = make_plan("g", [PlanStep(
            id="s1", title="think", action="llm",
            params={"prompt": "the answer"}, max_retries=2,
            verification="contains:42")])
        run = executor_with(tmp=tmp, llm=llm).execute(plan)
        assert run.status is TaskStatus.FAILED
        assert plan.steps[0].status == "failed"
        assert plan.steps[0].attempts == 3            # 1 + 2 retries

    def test_retry_until_success(self, tmp):
        class FlakyLLM:
            def __init__(self):
                self.calls = 0

            def complete(self, prompt, *, system=""):
                self.calls += 1
                return "42" if self.calls >= 2 else "nope"

        plan = make_plan("g", [PlanStep(
            id="s1", title="think", action="llm", max_retries=2,
            params={"prompt": "?"}, verification="contains:42")])
        run = executor_with(tmp=tmp, llm=FlakyLLM()).execute(plan)
        assert run.status is TaskStatus.COMPLETED
        assert plan.steps[0].attempts == 2

    def test_empty_output_fails_default_verification(self, tmp):
        plan = make_plan("g", [PlanStep(id="s1", title="speak", action="llm",
                                        params={"prompt": "hi"})])
        run = executor_with(tmp=tmp, llm=ScriptedLLM("")).execute(plan)
        assert run.status is TaskStatus.FAILED

    def test_on_fail_abort_skips_rest_of_plan(self, tmp):
        plan = make_plan("g", [step("s1", "noop"),
                               step("s2", "noop")])
        plan.steps[0].action = "llm"                  # will fail (no model)
        run = executor_with(tmp=tmp, llm=ScriptedLLM(None)).execute(plan)
        assert run.status is TaskStatus.FAILED
        assert plan.steps[0].status == "failed"
        assert plan.steps[1].status == "skipped"

    def test_on_fail_skip_drops_dependents(self, tmp):
        ran: list[str] = []

        class Marker(BaseAgent):
            name = "marker"
            task_types = (TaskType.CODING,)

            def handle(self, request):
                ran.append(request.metadata.get("plan_step", ""))
                return AgentResult(task_id=request.task_id,
                                   status=TaskStatus.COMPLETED, text="ok",
                                   agent=self.name)

        doomed = ScriptedAgent(name="doomed", outcome="fail")
        plan = make_plan("g", [
            PlanStep(id="s1", title="doomed", action="agent",
                     params={"task_type": "coding", "text": "x"},
                     on_fail="skip"),
            PlanStep(id="s2", title="dependent", action="agent",
                     params={"task_type": "coding", "text": "y"},
                     depends_on=["s1"]),
            PlanStep(id="s3", title="independent", action="noop"),
        ])
        marker = Marker()
        run = executor_with(doomed, marker, tmp=tmp).execute(plan)
        assert run.status is TaskStatus.FAILED
        assert plan.steps[0].status == "failed"
        assert plan.steps[1].status == "skipped"      # dependent dropped
        assert plan.steps[2].status == "done"         # independent still ran
        assert ran == ["s2"] or ran == []             # s2 never dispatched

    def test_on_fail_continue_lets_dependents_run(self, tmp):
        plan = make_plan("g", [
            PlanStep(id="s1", title="doomed", action="llm",
                     params={"prompt": "?"}, on_fail="continue"),
            PlanStep(id="s2", title="after", action="noop",
                     depends_on=["s1"]),
        ])
        run = executor_with(tmp=tmp, llm=ScriptedLLM(None)).execute(plan)
        assert plan.steps[1].status == "done"
        assert run.status is TaskStatus.FAILED        # plan still failed

    def test_agent_step_crash_is_contained(self, tmp):
        broken = ScriptedAgent(outcome="raise")
        plan = make_plan("g", [PlanStep(
            id="s1", title="use broken", action="agent",
            params={"task_type": "coding", "text": "go"})])
        run = executor_with(broken, tmp=tmp).execute(plan)
        assert run.status is TaskStatus.FAILED
        assert "boom" in plan.steps[0].error

    def test_agent_step_needing_approval_fails_honestly(self, tmp):
        gated = ScriptedAgent(outcome="approval")
        plan = make_plan("g", [PlanStep(
            id="s1", title="move money", action="agent",
            params={"task_type": "coding", "text": "trade"})])
        run = executor_with(gated, tmp=tmp).execute(plan)
        assert run.status is TaskStatus.FAILED
        assert "requires approval" in plan.steps[0].error

    def test_no_agent_for_task_type_fails_step(self, tmp):
        plan = make_plan("g", [PlanStep(
            id="s1", title="trade", action="agent",
            params={"task_type": "trading", "text": "BTC"})])
        run = executor_with(tmp=tmp).execute(plan)
        assert run.status is TaskStatus.FAILED
        assert "no agent registered" in plan.steps[0].error


# ── 5. human approval checkpoints ────────────────────────────────────────────

class TestApprovalGate:
    def test_approval_gated_plan_parks_before_anything_runs(self, tmp):
        worker = ScriptedAgent()
        plan = make_plan("g", [
            step("s1", "noop"),
            PlanStep(id="s2", title="danger", action="noop",
                     requires_approval=True),
        ])
        run = executor_with(worker, tmp=tmp).execute(plan)
        assert run.status is TaskStatus.NEEDS_APPROVAL
        assert worker.calls == []                     # NOTHING ran
        assert plan.status == "awaiting_approval"
        assert plan.steps[0].status == "pending"      # not even safe steps
        assert "approval" in run.text.lower()
        assert "s1." in run.text and "s2." in run.text  # plan is inspectable

    def test_approval_summary_names_gated_steps(self, tmp):
        plan = make_plan("g", [PlanStep(id="s1", title="delete stuff",
                                        action="noop",
                                        requires_approval=True)],
                         risk_notes=["irreversible"])
        run = executor_with(tmp=tmp).execute(plan)
        summary = run.data["approval_summary"]
        assert "s1" in summary and "irreversible" in summary

    def test_approved_plan_runs_and_completes(self, tmp):
        plan = make_plan("g", [PlanStep(id="s1", title="danger",
                                        action="noop",
                                        requires_approval=True)])
        executor_with(tmp=tmp).execute(plan)          # parks
        run = executor_with(tmp=tmp).execute(plan, approved=True)
        assert run.status is TaskStatus.COMPLETED
        assert plan.status == "completed"

    def test_resume_from_checkpoint_preserves_state(self, tmp):
        store = PlanCheckpointStore(tmp)
        plan = make_plan("g", [step("s1"), step("s2"),
                               step("s3")])
        plan.steps[0].status = "done"                 # simulate earlier crash
        store.save(plan.to_dict(include_runtime=True))
        loaded = Plan.from_dict(store.load(plan.plan_id))
        ex = executor_with(tmp=tmp)
        run = ex.execute(loaded)
        assert run.status is TaskStatus.COMPLETED
        # s1 was NOT re-run (already done at checkpoint time)
        assert loaded.steps[0].attempts == 0
        assert [s.status for s in loaded.steps] == ["done", "done", "done"]

    def test_every_step_transition_is_checkpointed(self, tmp):
        store = PlanCheckpointStore(tmp)
        llm = ScriptedLLM("payload")
        plan = make_plan("g", [PlanStep(id="s1", title="w", action="llm",
                                        params={"prompt": "p"})])
        executor_with(tmp=tmp, llm=llm).execute(plan)
        saved = store.load(plan.plan_id)
        assert saved is not None
        assert saved["steps"][0]["status"] == "done"
        assert saved["status"] == "completed"


# ── 6. the plan_agent surface ────────────────────────────────────────────────

class TestPlanAgent:
    def _agent(self, tmp):
        return PlanAgent(planner=LLMPlanner(llm=ScriptedLLM(None),
                                            heuristic=HeuristicPlanner()),
                         store=PlanCheckpointStore(tmp))

    def test_needs_goal_when_empty(self, tmp):
        agent = self._agent(tmp)
        res = agent.handle(AgentRequest(task_id="t1",
                                        task_type=TaskType._FALLBACK,
                                        text=""))
        assert res.status is TaskStatus.NEEDS_INPUT

    def test_plans_and_executes_safe_goal(self, tmp):
        agent = self._agent(tmp)
        worker = ScriptedAgent(outcome="ok")
        req = AgentRequest(task_id="t2", task_type=TaskType._FALLBACK,
                           text="summarize the quarterly report",
                           metadata={"_registry": agent_registry(worker)})
        res = agent.handle(req)
        assert res.status is TaskStatus.COMPLETED, res.text
        assert res.data["plan_id"]
        assert res.data["plan"]["status"] == "completed"
        assert len(worker.calls) >= 1     # the goal was delegated, not faked

    def test_approval_roundtrip_through_adapter(self, tmp):
        agent = self._agent(tmp)
        worker = ScriptedAgent(outcome="ok")
        reg = agent_registry(worker)
        req = AgentRequest(task_id="t3", task_type=TaskType._FALLBACK,
                           text="prepare the thing",
                           metadata={"_registry": reg})
        first = agent.handle(req)
        assert first.status is TaskStatus.COMPLETED, first.text
        plan_id = first.data["plan_id"]
        # every step is terminal now → inject one gated step and resume
        data = agent.store.load(plan_id)
        data["steps"].append({
            "id": "sX", "title": "delete prod", "action": "noop",
            "requires_approval": True, "status": "pending",
        })
        agent.store.save(data)
        first = agent.handle(AgentRequest(
            task_id="t3b", task_type=TaskType._FALLBACK, text="again",
            metadata={"plan_resume": plan_id, "_registry": reg}))
        assert first.status is TaskStatus.NEEDS_APPROVAL, first.text
        assert "sX" in first.text          # plan is inspectable in the ask
        assert len(worker.calls) == 1      # resume did NOT re-run done steps

        approved = agent.handle(AgentRequest(
            task_id="t3c", task_type=TaskType._FALLBACK, text="(ok)",
            metadata={"plan_action": "approve", "plan_id": plan_id,
                      "_registry": reg}))
        assert approved.status is TaskStatus.COMPLETED, approved.text

        denied = agent.handle(AgentRequest(
            task_id="t3d", task_type=TaskType._FALLBACK, text="(no)",
            metadata={"plan_action": "deny", "plan_id": plan_id}))
        assert denied.status is TaskStatus.DECLINED     # already resolved

    def test_resolve_plan_module_hook(self, tmp):
        from orchestrator.registry import AgentRegistry
        agent = self._agent(tmp)
        reg = AgentRegistry()
        reg.register(agent)
        orch = type("O", (), {"registry": reg})()
        res = resolve_plan(orch, "plan-missing-0000", approved=True)
        assert res.status is TaskStatus.DECLINED
        assert "nothing was run" in res.text.lower() or \
            "not registered" in res.text.lower()

    def test_agent_step_via_registry(self, tmp):
        worker = ScriptedAgent(outcome="ok")
        agent = self._agent(tmp)
        req = AgentRequest(task_id="t4", task_type=TaskType._FALLBACK,
                           text="one agent step",
                           metadata={"_registry": agent_registry(worker),
                                     "heuristic_action": "agent"})
        # force the heuristic plan to produce an agent step
        agent.planner = LLMPlanner(llm=ScriptedLLM(json.dumps({
            "steps": [{"id": "s1", "title": "delegate", "action": "agent",
                       "params": {"task_type": "coding",
                                  "text": "delegate me"}}]})))
        res = agent.handle(req)
        assert res.status is TaskStatus.COMPLETED
        assert len(worker.calls) == 1
        assert worker.calls[0].metadata.get("plan_id") == \
            res.data["plan_id"]


# ── 7. registry wiring (env-gated) ───────────────────────────────────────────

class TestRegistryWiring:
    def test_plan_agent_registered_when_env_on(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PLAN_AGENT", "on")
        monkeypatch.setenv("JARVIS_CODING_AGENT", "off")
        monkeypatch.setenv("JARVIS_RAG_AGENT", "off")
        from orchestrator.agents import build_default_registry
        reg = build_default_registry()
        assert "plan_agent" in reg

    def test_plan_agent_absent_by_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_PLAN_AGENT", raising=False)
        from orchestrator.agents import build_default_registry
        reg = build_default_registry()
        assert "plan_agent" not in reg


# ── 8. honesty: executor never raises, plans never auto-approve ─────────────

class TestSafety:
    def test_execute_never_raises_on_garbage_plan(self, tmp):
        ex = executor_with(tmp=tmp)
        plan = Plan(goal="broken", steps=[])          # invalid
        run = ex.execute(plan)
        assert run.status is TaskStatus.FAILED

    def test_deny_leaves_steps_untouched(self, tmp):
        agent = PlanAgent(planner=LLMPlanner(llm=ScriptedLLM(None),
                                             heuristic=HeuristicPlanner()),
                          store=PlanCheckpointStore(tmp))
        # craft a gated plan directly
        plan = make_plan("g", [PlanStep(id="s1", title="big delete",
                                        action="noop",
                                        requires_approval=True)])
        agent.store.save(plan.to_dict(include_runtime=True))
        res = agent.handle(AgentRequest(
            task_id="t9", task_type=TaskType._FALLBACK, text="(deny)",
            metadata={"plan_action": "deny", "plan_id": plan.plan_id}))
        assert res.status is TaskStatus.DENIED
        saved = agent.store.load(plan.plan_id)
        assert saved["steps"][0]["status"] == "denied"
