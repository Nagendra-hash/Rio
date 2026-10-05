"""
tests/test_orchestrator.py — offline tests for the Phase-1 orchestrator.

What this proves (no network, no LLM, no dashboard):
  1. Task intake + classification across all five lanes, plus explicit
     overrides, neutral chit-chat, and empty input.
  2. Routing: correct agent chosen, unknown intents fall back to the
     existing assistant, low confidence falls back, unregistered lanes fall
     back — the router never dead-ends a user turn.
  3. Agent contract: stubs answer in the standard format; DECLINE re-routes
     to the next candidate; agent crashes are contained and fall back.
  4. Conversation/context handoff: agents receive the session history,
     and agents cannot mutate the canonical session record.
  5. Human approval state: trading tasks park a pending approval; approve
     releases the task, deny refuses it; expired/unknown ids are safe.
  6. Error handling: submit() NEVER raises, even with garbage input.
  7. Logging/audit: every event lands in the JSONL journal.

Run:  .venv/bin/python -m pytest tests/test_orchestrator.py -q
"""
import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.approvals import ApprovalGate
from orchestrator.base import BaseAgent, StubAgent
from orchestrator.classifier import IntentClassifier
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType, new_task_id)
from orchestrator.context import ContextStore
from orchestrator.journal import OrchestrationJournal
from orchestrator.orchestrator import JarvisOrchestrator
from orchestrator.registry import AgentRegistry
from orchestrator.router import TaskRouter


# ── fixtures ──────────────────────────────────────────────────────────────────

def make_orchestrator(journal: OrchestrationJournal | None = None,
                      **kw) -> JarvisOrchestrator:
    from orchestrator.agents import build_default_registry
    return JarvisOrchestrator(
        registry=build_default_registry(),
        journal=journal or OrchestrationJournal(enabled=False),
        **kw)


@pytest.fixture()
def orch() -> JarvisOrchestrator:
    return make_orchestrator()


# ── 1. classification ────────────────────────────────────────────────────────

class TestClassification:
    c = IntentClassifier()

    @pytest.mark.parametrize("text,expected", [
        ("fix the bug in parse_args", TaskType.CODING),
        ("write unit tests for the router", TaskType.CODING),
        ("this traceback appeared when I run my script", TaskType.CODING),
        ("refactor the function to be async", TaskType.CODING),
        ("what's the latest news on the election", TaskType.RESEARCH),
        ("compare iPhone and Pixel", TaskType.RESEARCH),
        ("summarize this article", TaskType.RESEARCH),
        ("search the web for rust vs golang", TaskType.RESEARCH),
        ("BTC analysis for today", TaskType.TRADING),
        ("what is the support and resistance on NIFTY", TaskType.TRADING),
        ("$AAPL setup", TaskType.TRADING),
        ("what's the RSI on SOL right now", TaskType.TRADING),
        ("what do my notes say about the project", TaskType.RAG),
        ("find it in my documents", TaskType.RAG),
        ("remember this pdf", TaskType.RAG),
        ("turn the volume up", TaskType.SYSTEM),
        ("what's my battery level", TaskType.SYSTEM),
        ("take a screenshot", TaskType.SYSTEM),
        ("restart the music app", TaskType.SYSTEM),
        ("route this to the trading agent", TaskType.TRADING),
        ("[coding] please handle this", TaskType.CODING),
    ])
    def test_lanes(self, text, expected):
        got = self.c.classify(text)
        assert got.task_type is expected, f"{text!r} → {got.task_type} ({got.reason})"

    @pytest.mark.parametrize("text", [
        "hello",
        "what's up",
        "thanks",
        "who are you",
        "tell me a joke",
        "how are you doing today my friend",   # conversational, not research
        "",
    ])
    def test_unknown_falls_back(self, text):
        got = self.c.classify(text)
        assert got.task_type is TaskType._FALLBACK, f"{text!r} → {got.reason}"

    def test_confidence_bounds(self):
        for text in ["fix the bug", "BTC chart analysis", "hello"]:
            got = self.c.classify(text)
            assert 0.0 <= got.confidence <= 1.0


# ── 2. routing & fallback ─────────────────────────────────────────────────────

class TestRouting:
    def test_known_intent_routes_to_stub(self, orch):
        result = orch.submit("fix the bug in parse_args")
        assert result.status is TaskStatus.NEEDS_INPUT      # stub admits it
        assert result.agent == "coding_agent"
        assert "not implemented" in result.text

    def test_each_lane_routes_to_its_agent(self, orch):
        probes = {
            "coding_agent": "write unit tests for the router",
            "research_agent": "what's the latest news on AI chips",
            "trading_agent": "BTC analysis for today",
            # (a second trading probe that is unmistakably a chart ask)
            "rag_agent": "what do my notes say about taxes",
            "system_agent": "turn the volume up",
        }
        for agent_name, text in probes.items():
            result = orch.submit(text)
            assert result.agent == agent_name, f"{text!r} → {result.agent}"

    def test_unknown_intent_falls_back_to_existing_assistant(self, orch):
        result = orch.submit("tell me a joke")
        assert result.status is TaskStatus.DECLINED
        assert result.data.get("fallback") is True
        assert result.agent == "gemini_assistant"           # the existing path

    def test_unregistered_lane_falls_back(self):
        # Registry WITHOUT any agents — nothing can serve the task.
        bare = JarvisOrchestrator(
            registry=AgentRegistry(),
            journal=OrchestrationJournal(enabled=False))
        result = bare.submit("fix the bug in parse_args")
        assert result.data.get("fallback") is True

    def test_low_confidence_falls_back(self):
        orch = make_orchestrator(min_confidence=0.99)       # unreachable bar
        result = orch.submit("fix the bug in parse_args")
        assert result.data.get("fallback") is True

    def test_explicit_unknown_agent_override_is_ignored(self, orch):
        result = orch.submit("fix the bug",
                             metadata={"agent_override": "nonexistent"})
        assert result.agent == "coding_agent"               # normal routing

    def test_router_never_raises(self, orch):
        for text in ["", "   ", "fix the bug", "hello", "BTC analysis",
                     None if False else "🚀 to the moon"]:
            result = orch.submit(text)                      # must not raise
            assert result is not None


# ── 3. agent contract ─────────────────────────────────────────────────────────

class ScriptedAgent(BaseAgent):
    """Test double: scripted outcomes, including a raise-on-demand."""

    def __init__(self, name, outcome="ok", task_types=(TaskType.CODING,)):
        self.name, self.outcome = name, outcome
        self.task_types = task_types
        self.calls: list[AgentRequest] = []

    def handle(self, request):
        self.calls.append(request)
        if self.outcome == "raise":
            raise RuntimeError("boom")
        if self.outcome == "decline":
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.DECLINED,
                               text="not my kind of task", agent=self.name)
        if self.outcome == "garbage":
            return "I am not an AgentResult"               # contract violation
        return AgentResult(task_id=request.task_id,
                           status=TaskStatus.COMPLETED,
                           text=f"done by {self.name}", agent=self.name)


class TestAgentContract:
    def _orch_with(self, *agents):
        reg = AgentRegistry()
        for a in agents:
            reg.register(a)
        return JarvisOrchestrator(registry=reg,
                                  journal=OrchestrationJournal(enabled=False)), agents

    def test_decline_re_routes_to_next_candidate(self):
        primary = ScriptedAgent("primary", outcome="decline")
        backup = ScriptedAgent("backup", outcome="ok")
        orch, _ = self._orch_with(primary, backup)
        result = orch.submit("fix the bug")
        assert result.agent == "backup" and result.ok

    def test_agent_crash_is_contained_and_falls_back(self):
        broken = ScriptedAgent("broken", outcome="raise")
        orch, _ = self._orch_with(broken)
        result = orch.submit("fix the bug")
        assert result.data.get("fallback") is True      # user still served

    def test_contract_violation_garbage_result_falls_back(self):
        weird = ScriptedAgent("weird", outcome="garbage")
        orch, _ = self._orch_with(weird)
        result = orch.submit("fix the bug")
        assert result.data.get("fallback") is True

    def test_registry_rejects_duplicate_names(self):
        reg = AgentRegistry()
        reg.register(ScriptedAgent("dupe"))
        with pytest.raises(ValueError):
            reg.register(ScriptedAgent("dupe"))

    def test_registry_rejects_non_agents(self):
        with pytest.raises(TypeError):
            AgentRegistry().register("not an agent")


# ── 4. conversation / context handoff ─────────────────────────────────────────

class TestContextHandoff:
    def test_agent_receives_session_history(self, orch):
        orch.submit("BTC analysis for today", session_id="s1")
        orch.submit("what about ETH", session_id="s1")

        seen: dict = {}

        class Recorder(ScriptedAgent):
            def handle(self, request):
                seen["ctx"] = list(request.context)
                seen["meta"] = dict(request.metadata)
                return AgentResult(task_id=request.task_id,
                                   status=TaskStatus.COMPLETED, text="ok",
                                   agent=self.name)

        recorder = Recorder("recorder")
        orch.register_agent(recorder)
        # Coding ask, explicitly overridden to the recorder so it (not the
        # coding stub) receives the handoff.
        orch.submit("fix the remaining bug", session_id="s1",
                    metadata={"agent_override": "recorder"})
        roles = [r for r, _ in seen["ctx"]]
        # Contract: context carries the PRIOR turns of the session; the
        # current ask itself travels in request.text (no duplication).
        assert roles == ["user", "assistant", "user", "assistant"], roles
        assert seen["ctx"][-2] == ("user", "what about ETH")   # last user turn
        assert seen["meta"]["session_id"] == "s1"

    def test_history_is_bounded(self):
        store = ContextStore(max_turns=2)
        for i in range(10):
            store.record_user("s", f"msg {i}")
            store.record_assistant("s", f"reply {i}")
        snap = store.get_or_create("s").snapshot(2)
        assert len(snap) <= 4 and snap[-1][1] == "reply 9"

    def test_agents_cannot_mutate_canonical_history(self, orch):
        orch.submit("BTC analysis for today", session_id="s2")

        class Vandal(ScriptedAgent):
            def handle(self, request):
                request.context.append(("assistant", "I WAS HERE"))
                return AgentResult(task_id=request.task_id,
                                   status=TaskStatus.COMPLETED, text="ok",
                                   agent=self.name)

        orch.register_agent(Vandal("vandal"))
        orch.submit("fix the bug", session_id="s2")
        snap = orch.context.get_or_create("s2").snapshot(20)
        assert all(t != "I WAS HERE" for _, t in snap)

    def test_separate_sessions_do_not_leak(self, orch):
        orch.submit("BTC analysis", session_id="a")
        orch.submit("fix the bug", session_id="b")
        a = orch.context.get_or_create("a").snapshot(20)
        b = orch.context.get_or_create("b").snapshot(20)
        assert any("BTC" in t for _, t in a)
        assert all("BTC" not in t for _, t in b)


# ── 5. human approval state ───────────────────────────────────────────────────

class TestApprovalState:
    def test_trading_task_parks_pending_approval(self, orch):
        result = orch.submit("BTC analysis for today")
        assert result.status is TaskStatus.NEEDS_APPROVAL
        pending = orch.approvals.get(result.task_id)
        assert pending is not None and not pending.resolved

    def test_approve_releases_task_to_agent(self, orch):
        result = orch.submit("BTC analysis for today")
        approved = orch.resolve_approval(result.task_id, approved=True)
        assert approved.status is TaskStatus.COMPLETED
        assert approved.agent == "trading_agent"

    def test_deny_refuses_task(self, orch):
        result = orch.submit("BTC analysis for today")
        denied = orch.resolve_approval(result.task_id, approved=False)
        assert denied.status is TaskStatus.DENIED
        assert "will not run" in denied.text

    def test_unknown_or_expired_approval_is_safe(self, orch):
        result = orch.resolve_approval("task-does-not-exist", approved=True)
        assert result.status is TaskStatus.DECLINED
        assert "Nothing was run" in result.text

    def test_double_resolution_is_rejected(self, orch):
        result = orch.submit("BTC analysis for today")
        assert orch.resolve_approval(result.task_id, True).ok
        again = orch.resolve_approval(result.task_id, True)
        assert again.status is TaskStatus.DECLINED

    def test_approval_expiry(self):
        t = 1_000_000.0
        gate = ApprovalGate(ttl_s=60.0, clock=lambda: t)
        gate.request("t1", "trading_agent", "sell everything")
        t += 61.0
        assert gate.get("t1") is None            # expired, never auto-runs
        assert gate.resolve("t1", True) is False

    def test_stub_agents_require_approval_only_where_declared(self, orch):
        coding = orch.submit("fix the bug in parse_args")
        assert coding.status is TaskStatus.NEEDS_INPUT          # no approval
        system = orch.submit("take a screenshot")
        assert system.status is TaskStatus.NEEDS_APPROVAL       # gated


# ── 6. error handling ─────────────────────────────────────────────────────────

class TestErrorHandling:
    def test_submit_never_raises_on_garbage(self, orch):
        for bad in (None, "", 12345, ["list"], {"dict": 1}):
            result = orch.submit(bad)                    # type: ignore[arg-type]
            assert result is not None

    def test_failing_classifier_still_falls_back(self, orch):
        class BadClassifier:
            def classify(self, text):
                raise RuntimeError("no")

        orch.classifier = BadClassifier()
        result = orch.submit("fix the bug")
        assert result.data.get("fallback") is True

    def test_failing_journal_never_breaks_intake(self):
        class BadJournal(OrchestrationJournal):
            def record(self, *a, **kw):
                raise RuntimeError("disk on fire")

        orch = JarvisOrchestrator(registry=None, journal=BadJournal(enabled=True),
                                  classifier=None, context=None, approvals=None)
        result = orch.submit("fix the bug")
        assert result is not None

    def test_bad_task_id_is_replaced_not_trusted(self, orch):
        result = orch.submit("fix the bug", task_id="bad id with spaces")
        assert result.task_id and " " not in result.task_id


# ── 7. logging / journal ──────────────────────────────────────────────────────

class TestJournal:
    def test_events_recorded_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "journal.jsonl"
            orch = make_orchestrator(
                journal=OrchestrationJournal(path=path, enabled=True))
            orch.submit("fix the bug in parse_args")

            events = [json.loads(line) for line in
                      path.read_text(encoding="utf-8").splitlines()]
            names = [e["event"] for e in events]
            assert names[0] == "task.received"
            assert "task.classified" in names
            assert "task.routed" not in names or names[-1] == "task.agent_result"
            assert names[-1] == "task.agent_result"
            assert all(e.get("task_id") for e in events)

    def test_fallback_event_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "journal.jsonl"
            orch = make_orchestrator(
                journal=OrchestrationJournal(path=path, enabled=True))
            orch.submit("tell me a joke")

            events = [json.loads(line) for line in
                      path.read_text(encoding="utf-8").splitlines()]
            assert events[-1]["event"] == "task.fallback"
            assert events[-1]["agent"] == "gemini_assistant"


# ── 8. standard contracts sanity ──────────────────────────────────────────────

class TestContracts:
    def test_task_ids_are_collision_safe(self):
        ids = {new_task_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_request_survives_roundtrip_metadata(self):
        req = AgentRequest(task_id="task-1", task_type=TaskType.CODING,
                           text="hi", metadata={"channel": "telegram"})
        req2 = req.with_context([("user", "hello")], {"chat_id": "42"})
        assert req2.metadata["channel"] == "telegram"
        assert req2.metadata["chat_id"] == "42"
        assert req2.context == [("user", "hello")]

    def test_result_serializes(self):
        r = AgentResult(task_id="t", status=TaskStatus.COMPLETED, text="done")
        d = r.to_dict()
        assert d["status"] == "completed" and d["text"] == "done"
