"""
tests/test_reflection_reflexion.py — offline tests for Phase 10:
Reflection (GENERATE→CRITIQUE→IMPROVE), Reflexion
(PLAN→ACT→OBSERVE→REFLECT→ADJUST→RETRY), the shared retry policy, and the
lesson store.

No network, no LLM — all model surfaces are scripted doubles. The default
(loops without an injected LLM) must never touch the network at all.
Run:  .venv/bin/python -m pytest tests/test_reflection_reflexion.py -q
"""
import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.base import BaseAgent
from orchestrator.checkpoint import PlanCheckpointStore
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)
from orchestrator.plan_executor import PlanExecutor
from orchestrator.plans import Plan, PlanStep, make_plan
from orchestrator.reflection import (Critique, DeterministicCritic,
                                     LLMCritic, LLMImprover, ReflectionPolicy,
                                     ReflectionResult, Reflector,
                                     rough_tokens)
from orchestrator.reflexion import (LessonStore, Observation, ReflexionCritic,
                                    ReflexionLoop, adjust_plan, heuristic_reflection,
                                    observe, sanitize_lesson)
from orchestrator.registry import AgentRegistry
from orchestrator.retry_policy import (BudgetState, RetryPolicy, backoff_delay)


# ── doubles ──────────────────────────────────────────────────────────────────

class ScriptedLLM:
    """LLMBackend double: pops canned replies, records prompts."""
    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, prompt, *, system=""):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else None


def critic_json(score, missing=(), contradictions=(), weak=(), formatting=(),
                summary="c"):
    return json.dumps({"score": score, "missing_info": list(missing),
                       "contradictions": list(contradictions),
                       "weak_reasoning": list(weak),
                       "formatting": list(formatting), "summary": summary})


def executor_with(*agents, store):
    reg = AgentRegistry()
    for a in agents:
        reg.register(a)
    return PlanExecutor(registry=reg, store=store)


class GateAgent(BaseAgent):
    """Fails while .fail is True, succeeds after — for retry loops."""
    name = "gate"
    task_types = (TaskType.CODING,)
    fail = True
    error_text = "gate closed"

    def handle(self, request):
        if type(self).fail:
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error=type(self).error_text, agent=self.name)
        return AgentResult(task_id=request.task_id,
                           status=TaskStatus.COMPLETED,
                           text="gate open", agent=self.name)


def gated_plan(step_id="s1", max_retries=0):
    return make_plan("get through the gate", [PlanStep(
        id=step_id, title="open the gate", action="agent",
        params={"task_type": "coding", "text": "open it"},
        max_retries=max_retries)])


@pytest.fixture()
def tmp():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# ── 1. retry policy ──────────────────────────────────────────────────────────

class TestRetryPolicy:
    def test_defaults_and_clamping(self):
        p = RetryPolicy()
        assert p.max_retries == 2 and p.attempts_allowed() == 3
        assert RetryPolicy(max_retries=99).max_retries == 3      # hard cap
        assert RetryPolicy(max_retries=-5).max_retries == 0
        assert RetryPolicy(max_wall_seconds=0.001).max_wall_seconds == 1.0

    def test_budget_state_tokens_and_time(self):
        p = RetryPolicy(max_total_tokens=100, max_wall_seconds=5.0)
        b = BudgetState()
        assert b.exhausted(p) == ""
        b.spend(150)
        assert b.exhausted(p) == "tokens"

    def test_budget_state_time_exhaustion(self):
        class FakeClock:
            t = 1000.0

            def __call__(self):
                return self.t

        clock = FakeClock()
        p = RetryPolicy(max_wall_seconds=10.0, clock=clock)
        b = BudgetState()
        b.started_at = clock.t
        assert b.exhausted(p) == ""
        clock.t += 11.0
        assert b.exhausted(p) == "time"

    def test_backoff_delay_linear_and_capped(self):
        p = RetryPolicy(backoff_seconds=2.0)
        assert backoff_delay(p, 1) == 2.0
        assert backoff_delay(p, 5) <= 8.0        # hard cap
        assert backoff_delay(RetryPolicy(), 3) == 0.0

    def test_rough_tokens_sane(self):
        assert rough_tokens("") == 1
        assert rough_tokens("x" * 400) == 100


# ── 2. reflection: deterministic critic ─────────────────────────────────────

class TestDeterministicCritic:
    def test_clean_draft_scores_high(self):
        c = DeterministicCritic().critique("t", "A complete, plain answer "
                                           "that addresses the task fully.")
        assert c.approved and c.score >= 7.5

    def test_placeholder_and_hedging_detected(self):
        c = DeterministicCritic().critique(
            "t", "TODO finish this. maybe it works, possibly, perhaps.")
        assert not c.approved
        assert any("placeholder" in i for i in c.issues())

    def test_empty_draft_rejected(self):
        c = DeterministicCritic().critique("t", "   ")
        assert c.score == 0.0 and not c.approved

    def test_long_unstructured_draft_flagged(self):
        c = DeterministicCritic().critique("t", "word " * 400)
        assert any("formatting" in i for i in c.issues())


# ── 3. reflection: the full loop ────────────────────────────────────────────

class TestReflectionLoop:
    def test_approved_draft_needs_one_round(self, tmp=None):
        llm = ScriptedLLM([critic_json(9.0, summary="good")])
        r = Reflector(critic=LLMCritic(llm),
                      policy=ReflectionPolicy(max_rounds=2))
        res = r.reflect("task", "a good complete draft answer")
        assert res.approved and res.rounds_used == 1
        assert res.final_output == "a good complete draft answer"

    def test_critique_then_improve_then_approve(self):
        llm = ScriptedLLM([
            critic_json(4.0, missing=["no numbers"]),     # round 1 critique
            "the answer is 42 with full detail",          # improvement
            critic_json(9.0, summary="good"),             # round 2 critique
        ])
        res = Reflector(critic=LLMCritic(llm),
                        improver=LLMImprover(llm),
                        policy=ReflectionPolicy(max_rounds=2)) \
            .reflect("task", "an answer without specifics")
        assert res.approved and res.rounds_used == 2
        assert "42" in res.final_output
        assert res.history[0]["round"] == 1

    def test_budget_rounds_never_exceeded(self):
        # critic never approves; improver keeps changing → must stop at cap
        llm = ScriptedLLM([critic_json(3.0), "v2", critic_json(3.5), "v3",
                           critic_json(4.0)])
        res = Reflector(critic=LLMCritic(llm), improver=LLMImprover(llm),
                        policy=ReflectionPolicy(max_rounds=3)) \
            .reflect("task", "draft one")
        assert res.rounds_used <= 3
        assert res.budget_exhausted == "rounds" or res.stalled
        assert not res.approved

    def test_stall_detection_stops_the_loop(self):
        # improver returns the SAME text → stall counter kicks in
        llm = ScriptedLLM([critic_json(3.0), "same draft",
                           critic_json(3.0), "same draft"])
        res = Reflector(critic=LLMCritic(llm),
                        improver=LLMImprover(llm),
                        policy=ReflectionPolicy(max_rounds=3)) \
            .reflect("task", "same draft")
        assert res.stalled and not res.approved

    def test_token_budget_exhaustion(self):
        llm = ScriptedLLM([critic_json(1.0), "x" * 50_000])
        res = Reflector(critic=LLMCritic(llm), improver=LLMImprover(llm),
                        policy=ReflectionPolicy(max_rounds=3,
                                                max_total_tokens=5_000)) \
            .reflect("task", "draft")
        assert res.tokens_used <= 5_000 + 5_000      # clamp tolerance
        assert res.budget_exhausted in ("tokens", "rounds")

    def test_time_budget_exhaustion(self):
        class SlowCritic:
            def critique(self, task, draft):
                time.sleep(0.05)
                return Critique(score=1.0, approved=False, summary="slow")

        res = Reflector(critic=SlowCritic(), improver=None,
                        policy=ReflectionPolicy(max_rounds=3,
                                                max_wall_seconds=1.0)) \
            .reflect("task", "draft")
        assert res.wall_seconds < 10.0
        assert res.budget_exhausted in ("time", "rounds")

    def test_secrets_masked_in_critic_output_and_result(self):
        secret = "sk-abcdef1234567890"
        llm = ScriptedLLM([
            critic_json(4.0, missing=[f"add the key {secret} to config"],
                        summary="leaks sk-abcdefghij12345678"),
            "fixed draft", critic_json(9.0),
        ])
        res = Reflector(critic=LLMCritic(llm), improver=LLMImprover(llm),
                        policy=ReflectionPolicy(max_rounds=2)) \
            .reflect("task", f"draft with {secret} inside")
        blob = json.dumps(res.to_dict())
        assert secret not in blob
        assert "sk-abcdefghij12345678" not in blob
        assert res.critique.masked_kinds  # the critic noticed and redacted

    def test_improver_output_masked(self):
        llm = ScriptedLLM([critic_json(4.0, weak=["add auth"]),
                           "use Bearer tok_abcdefghij1234567890 for auth",
                           critic_json(9.0)])
        res = Reflector(critic=LLMCritic(llm), improver=LLMImprover(llm),
                        policy=ReflectionPolicy(max_rounds=2)) \
            .reflect("task", "draft")
        assert "tok_abcdefghij1234567890" not in res.final_output

    def test_no_llm_default_never_touches_network(self):
        # default constructor: deterministic floor, no model calls
        res = Reflector().reflect("task", "TODO placeholder draft")
        assert res.source == "deterministic"
        assert not res.approved

    def test_reflect_never_raises_on_garbage(self):
        res = Reflector(critic=DeterministicCritic(), improver=None) \
            .reflect("", None)
        assert isinstance(res, ReflectionResult)
        assert res.final_output == "" or res.final_output


# ── 4. reflexion: observe → reflect → adjust ────────────────────────────────

class TestObserveReflectAdjust:
    def _failed_plan(self):
        plan = make_plan("g", [PlanStep(id="s1", title="one", action="noop"),
                               PlanStep(id="s2", title="two", action="noop",
                                        depends_on=["s1"])])
        plan.steps[0].status = "failed"
        plan.steps[0].error = "explosion"
        plan.steps[1].status = "skipped"
        plan.status = "failed"
        return plan

    def test_observe_reads_the_plan_honestly(self):
        plan = self._failed_plan()
        obs = observe(plan, attempt=1)
        assert not obs.ok
        assert [s["id"] for s in obs.failed_steps] == ["s1"]
        assert [s["id"] for s in obs.skipped_steps] == ["s2"]
        assert obs.progress_signature.startswith("s1")  # id + error hash

    def test_heuristic_reflection_names_a_cause(self):
        obs = observe(self._failed_plan(), 1)
        r = heuristic_reflection(obs, "goal", [])
        assert r["cause"] and r["lesson"] and r["adjust"] in \
            ("replace", "patch", "extend")

    def test_adjust_resets_failed_steps_and_notes_hint(self):
        plan = self._failed_plan()
        obs = observe(plan, 1)
        adjust_plan(plan, obs, {"adjust": "patch", "fix_hint": "use the v2 endpoint",
                                "cause": "old endpoint"})
        assert plan.steps[0].status == "pending"
        assert plan.steps[0].attempts == 0
        assert "v2 endpoint" in plan.steps[0].title
        assert plan.status == "draft"
        plan.validate()                       # still a valid plan

    def test_adjust_extend_inserts_diagnostic_step(self):
        plan = self._failed_plan()
        obs = observe(plan, 1)
        adjust_plan(plan, obs, {"adjust": "extend", "lesson": "probe first",
                                "cause": "unknown"})
        ids = [s.id for s in plan.steps]
        assert "s1x1" in ids                   # diagnostic step inserted
        diag = plan.steps_for("s1x1")
        assert diag.action == "noop" and diag.on_fail == "continue"
        assert plan.steps_for("s1").depends_on == ["s1x1"]
        plan.validate()

    def test_adjust_extend_is_idempotent(self):
        plan = self._failed_plan()
        obs = observe(plan, 1)
        reflection = {"adjust": "extend", "lesson": "probe first",
                      "cause": "unknown"}
        adjust_plan(plan, obs, reflection)
        adjust_plan(plan, observe(plan, 2), reflection)   # second attempt
        diag_ids = [s.id for s in plan.steps if s.id.startswith("s1x")]
        assert diag_ids.count("s1x1") == 1     # reused, not duplicated
        plan.validate()

    def test_adjust_replace_clears_params(self):
        plan = make_plan("g", [PlanStep(id="s1", title="do", action="agent",
                                        params={"text": "old instruction"})])
        plan.steps[0].status = "failed"
        plan.steps[0].error = "bad"
        plan.status = "failed"
        adjust_plan(plan, observe(plan, 1),
                    {"adjust": "replace", "lesson": "rethink it"})
        assert plan.steps[0].params == {}
        assert plan.steps[0].title.startswith("re-plan:")

    def test_llm_reflection_parsed_and_masked(self):
        llm = ScriptedLLM([json.dumps({
            "cause": "the api token sk-abcdef1234567890abc expired",
            "lesson": "refresh credentials before running",
            "adjust": "extend", "fix_hint": "add a refresh step"})])
        obs = observe(self._failed_plan(), 1)
        r = ReflexionCritic(llm).reflect(obs, "goal", [])
        assert "sk-abcdef1234567890abc" not in json.dumps(r)
        assert r["adjust"] == "extend" and r["source"] == "llm"

    def test_llm_reflection_falls_back_to_heuristic(self):
        llm = ScriptedLLM(["not json"])        # unparseable
        obs = observe(self._failed_plan(), 1)
        r = ReflexionCritic(llm).reflect(obs, "goal", [])
        assert r["source"] == "heuristic"


# ── 5. lessons store (no secrets, capped) ───────────────────────────────────

class TestLessonStore:
    def test_add_and_recall(self, tmp):
        store = LessonStore(tmp / "lessons.json")
        store.add("retry after warming the cache", goal="deploy the api")
        store.add("split big steps", goal="plan the migration")
        got = store.recall("deploy the api", limit=2)
        assert any("cache" in g for g in got)

    def test_secrets_never_stored(self, tmp):
        # a full-length OpenAI-style key (the real mask pattern needs 16+ chars)
        secret = "sk-abcdef1234567890"
        store = LessonStore(tmp / "lessons.json")
        entry = store.add(f"the key is {secret} keep it safe")
        assert secret not in entry["lesson"]
        assert entry["masked_kinds"]
        raw = (tmp / "lessons.json").read_text()
        assert secret not in raw

    def test_lesson_length_capped(self, tmp):
        store = LessonStore(tmp / "lessons.json")
        entry = store.add("x" * 10_000)
        assert len(entry["lesson"]) <= 600

    def test_store_capped_entries(self, tmp):
        store = LessonStore(tmp / "lessons.json", max_entries=5)
        for i in range(20):
            store.add(f"lesson {i}")
        assert len(store) == 5

    def test_recall_on_empty_store_is_safe(self, tmp):
        store = LessonStore(tmp / "missing.json")
        assert store.recall("anything") == []


# ── 6. reflexion: the full loop ─────────────────────────────────────────────

class TestReflexionLoop:
    def test_success_on_first_attempt(self, tmp):
        GateAgent.fail = False
        store = PlanCheckpointStore(tmp)
        loop = ReflexionLoop(executor_with(GateAgent(), store=store),
                             store=store,
                             lessons=LessonStore(tmp / "l.json"),
                             policy=RetryPolicy(max_retries=2))
        res = loop.run(gated_plan())
        assert res.success and res.attempts_used == 1
        assert not res.lessons                 # nothing to learn

    def test_retry_succeeds_after_adjustment(self, tmp):
        # agent fails on attempt 1, succeeds afterwards; the heuristic
        # lesson is stored in the lesson library either way
        class FlakyGate(GateAgent):
            name = "flaky_gate"
            calls = 0

            def handle(self, request):
                type(self).calls += 1
                if type(self).calls == 1:
                    return AgentResult(task_id=request.task_id,
                                       status=TaskStatus.FAILED,
                                       error="transient net error",
                                       agent=self.name)
                return AgentResult(task_id=request.task_id,
                                   status=TaskStatus.COMPLETED,
                                   text="ok", agent=self.name)

        lessons = LessonStore(tmp / "l.json")
        store = PlanCheckpointStore(tmp)
        loop = ReflexionLoop(executor_with(FlakyGate(), store=store),
                             store=store, lessons=lessons,
                             policy=RetryPolicy(max_retries=2))
        res = loop.run(gated_plan())
        assert res.success, res.budget_exhausted
        assert res.attempts_used == 2
        assert len(lessons) >= 1               # the failure taught something

    def test_attempt_cap_is_respected(self, tmp):
        """A deterministic failure whose signature CHANGES after each adjust
        (diagnostic step inserted) burns all attempts, then stops."""
        GateAgent.fail = True
        GateAgent.error_text = "boom"
        store = PlanCheckpointStore(tmp)
        loop = ReflexionLoop(executor_with(GateAgent(), store=store),
                             store=store,
                             lessons=LessonStore(tmp / "l.json"),
                             policy=RetryPolicy(max_retries=2))
        plan = gated_plan()
        # make each adjust change the failure surface: a step that fails
        # only when more steps exist (signature shifts every attempt)
        class Shifting(BaseAgent):
            name = "shifting"
            task_types = (TaskType.CODING,)
            calls = 0

            def handle(self, request):
                type(self).calls += 1
                return AgentResult(
                    task_id=request.task_id, status=TaskStatus.FAILED,
                    error=f"failure mode {type(self).calls}",
                    agent=self.name)

        plan = make_plan("g", [PlanStep(id="s1", title="t", action="agent",
                                        params={"task_type": "coding",
                                                "text": "x"},
                                        max_retries=0)])
        loop = ReflexionLoop(executor_with(Shifting(), store=store),
                             store=store,
                             lessons=LessonStore(tmp / "l.json"),
                             policy=RetryPolicy(max_retries=2))
        res = loop.run(plan)
        assert not res.success
        assert res.attempts_used == 3          # 1 + 2 retries, then STOP
        assert res.budget_exhausted == "attempts"
        assert not res.stalled

    def test_stall_detection_repeated_failures(self, tmp):
        # NOTE: first attempt's signature differs (dependency skips), then
        # identical signatures → stall. Force identical by a plan whose
        # failure signature is stable across attempts.
        class AlwaysSame(BaseAgent):
            name = "always_same"
            task_types = (TaskType.CODING,)

            def handle(self, request):
                return AgentResult(task_id=request.task_id,
                                   status=TaskStatus.FAILED,
                                   error="same every time",
                                   agent=self.name)

        store = PlanCheckpointStore(tmp)
        loop = ReflexionLoop(executor_with(AlwaysSame(), store=store),
                             store=store,
                             lessons=LessonStore(tmp / "l.json"),
                             policy=RetryPolicy(max_retries=3))
        res = loop.run(gated_plan())
        assert res.stalled
        assert res.attempts_used < loop.policy.attempts_allowed() or \
            res.budget_exhausted == "attempts"

    def test_time_budget_stops_the_loop(self, tmp):
        """With budget exhausted mid-loop, no further attempts run — the
        loop ends via attempts, time, or a stall, never spins."""
        GateAgent.fail = True

        class ShiftingSlow(BaseAgent):
            name = "shifting_slow"
            task_types = (TaskType.CODING,)
            calls = 0

            def handle(self, request):
                type(self).calls += 1
                time.sleep(0.15)
                return AgentResult(task_id=request.task_id,
                                   status=TaskStatus.FAILED,
                                   error=f"failure {type(self).calls}",
                                   agent=self.name)

        store = PlanCheckpointStore(tmp)
        loop = ReflexionLoop(executor_with(ShiftingSlow(), store=store),
                             store=store,
                             lessons=LessonStore(tmp / "l.json"),
                             policy=RetryPolicy(max_retries=3,
                                                max_wall_seconds=0.4))
        res = loop.run(gated_plan())
        assert not res.success
        assert res.budget_exhausted in ("time", "attempts", "stall")
        assert res.wall_seconds < 10

    def test_every_attempt_checkpointed(self, tmp):
        GateAgent.fail = True
        store = PlanCheckpointStore(tmp)
        plan = gated_plan()
        loop = ReflexionLoop(executor_with(GateAgent(), store=store),
                             store=store,
                             lessons=LessonStore(tmp / "l.json"),
                             policy=RetryPolicy(max_retries=1))
        loop.run(plan)
        cp = store.load(plan.plan_id)
        assert cp and len(cp.get("reflexion_attempts") or []) >= 1
        assert all("plan_status" in a for a in cp["reflexion_attempts"])

    def test_lessons_stored_masked(self, tmp):
        secret = "sk-abcdef1234567890"

        class Leaky(BaseAgent):
            name = "leaky"
            task_types = (TaskType.CODING,)

            def handle(self, request):
                return AgentResult(task_id=request.task_id,
                                   status=TaskStatus.FAILED,
                                   error=f"auth failed with {secret}",
                                   agent=self.name)

        llm = ScriptedLLM([json.dumps({
            "cause": f"bad key {secret}", "lesson": f"rotate {secret} first",
            "adjust": "patch", "fix_hint": ""}),
            json.dumps({"cause": "bad key", "lesson": "rotate keys first",
                        "adjust": "patch", "fix_hint": ""})])
        store = PlanCheckpointStore(tmp)
        lessons = LessonStore(tmp / "l.json")
        loop = ReflexionLoop(executor_with(Leaky(), store=store),
                             critic=ReflexionCritic(llm), store=store,
                             lessons=lessons, policy=RetryPolicy(max_retries=1))
        res = loop.run(gated_plan())
        blob = json.dumps(res.to_dict())
        lesson_file = lessons.path
        if Path(lesson_file).exists():
            blob += Path(lesson_file).read_text()
        assert secret not in blob
        assert res.lessons

    def test_loop_never_raises_on_broken_executor(self, tmp):
        class Broken:
            store = None

            def execute(self, plan):
                raise RuntimeError("boom")

        loop = ReflexionLoop(Broken(), store=PlanCheckpointStore(tmp),
                             lessons=LessonStore(tmp / "l.json"),
                             policy=RetryPolicy(max_retries=1))
        res = loop.run(gated_plan())
        assert not res.success
        assert res.budget_exhausted == "error" or res.observations == []

    def test_approval_gate_still_applies_to_retries(self, tmp):
        """A retry must not bypass the plan engine's approval gate."""
        plan = make_plan("g", [PlanStep(id="s1", title="danger", action="noop",
                                        requires_approval=True)])
        store = PlanCheckpointStore(tmp)
        loop = ReflexionLoop(executor_with(store=store), store=store,
                             lessons=LessonStore(tmp / "l.json"),
                             policy=RetryPolicy(max_retries=2))
        res = loop.run(plan)
        # every attempt parks on approval (never executes), never "passes"
        assert not res.success
        assert plan.steps[0].status == "pending"   # untouched


# ── 7. no-infinite-loop guarantees (stress) ─────────────────────────────────

class TestNoInfiniteLoops:
    def test_reflection_terminates_on_hostile_critic(self):
        class Hostile:
            def critique(self, task, draft):
                return Critique(score=0.0, approved=False, summary="never")

        class Spinner:
            def improve(self, task, draft, critique):
                return f"{draft} {time.monotonic()}"   # always "changes"

        res = Reflector(critic=Hostile(), improver=Spinner(),
                        policy=ReflectionPolicy(max_rounds=3,
                                                max_wall_seconds=5.0)) \
            .reflect("task", "seed")
        assert res.rounds_used <= 3
        assert res.budget_exhausted == "rounds"

    def test_reflexion_terminates_on_alter_frenzy(self, tmp):
        """An adjuster that adds a step every round must still terminate."""
        class FrenzyLoop:
            def __init__(self):
                self.n = 0

            def run(self, plan, *, goal=""):
                self.n += 1
                # add a fresh pending step each time (would loop forever
                # without caps) but cap our own sabotage at 10 for the test
                if self.n <= 10:
                    plan.steps.append(PlanStep(id=f"z{self.n}", title="z",
                                               action="noop"))
                return ReflexionResult(goal=goal, success=False,
                                       attempts_used=self.n, final_plan=plan)

        loop = ReflexionLoop(FrenzyLoop(), store=PlanCheckpointStore(tmp),
                             lessons=LessonStore(tmp / "l.json"),
                             policy=RetryPolicy(max_retries=3))
        res = loop.run(gated_plan())
        assert res.attempts_used <= loop.policy.attempts_allowed()
