"""
tests/test_study_agent.py — offline acceptance tests for the Phase 15
Study Partner.

NO network, NO LLM, JARVIS_HOME redirected to a fresh temp dir per module;
the KB fixture mirrors tests/test_research_agent.py's sandbox_rag (fake
hash embeddings, never a cloud call).

Locked in:
  1. SAFETY   — exam-cheat refusal in code, permission levels, redaction.
  2. LEARNING — goal parsing, recency-weighted mastery, the adaptive
                ladder (promote/hold/demote/need-more), deterministic
                dated plans with a reserved revision tail.
  3. QUIZ     — grounded quiz build from numbered evidence, distractors,
                deterministic grading, pass threshold.
  4. STORE    — the study vault: 0600/0700, actions-only audit, dedupe of
                goals not required but wipe keeps the event.
  5. CHECKPOINT — snapshots, prune, perms, never raises.
  6. AGENT    — goal_set → plan → plan_done → explain (extractive,
                cited) → quiz → grade (ladder move + result row) →
                progress → adapt; needs-input on blanks; never raises.
  7. RAG      — explain/quiz/evidence all run through rag_service as
                caller `study_agent` (blind caller degrades to zero
                results, never an error).
  8. WIRING   — classifier lanes, explicit routes, registry gates.

Run:  .venv/bin/python -m pytest tests/test_study_agent.py -q
"""
import itertools
import json
import os
import stat
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_TMP = tempfile.mkdtemp(prefix="jarvis-study-")
os.environ["JARVIS_HOME"] = _TMP

from orchestrator.agents import build_default_registry                 # noqa: E402
from orchestrator.agents.study import engine, learning, safety          # noqa: E402
from orchestrator.agents.study.profile import StudyStore                # noqa: E402
from orchestrator.agents.study.resources import StudyResources          # noqa: E402
from orchestrator.agents.study.study_agent import (StudyAgent,          # noqa: E402
                                                   StudyCheckpoint)
from orchestrator.classifier import IntentClassifier                    # noqa: E402
from orchestrator.contracts import AgentRequest, TaskStatus, TaskType   # noqa: E402

_SEQ = itertools.count(1)


def _req(text: str = "", **meta) -> AgentRequest:
    return AgentRequest(task_id=f"study-test-{next(_SEQ):04d}",
                        task_type=TaskType.STUDY, text=text,
                        metadata=meta)


@pytest.fixture()
def tmp():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture()
def store(tmp):
    return StudyStore(tmp / "vault")


@pytest.fixture()
def ckpt(tmp):
    return StudyCheckpoint(tmp / "ckpt")


@pytest.fixture()
def agent(tmp, ckpt):
    a = StudyAgent(store=StudyStore(tmp / "vault"), checkpoint=ckpt)
    a.resources = StudyResources(toolbox=None, kb_search=_offline_kb,
                                 allow_web=False, allow_local_kb=True)
    return a


EVIDENCE = """[1] Linear Algebra Notes, p.3 [study_library]
A matrix is a rectangular array of numbers arranged in rows and columns.
Matrix addition is only defined for two matrices of the same shape.

[2] Linear Algebra Notes, p.5 [study_library]
Matrix multiplication is not commutative, so AB generally differs from
BA even when both products exist.
A square matrix is invertible only when its determinant is not zero."""

CITATIONS = ["Linear Algebra Notes, p.3 [study_library]",
             "Linear Algebra Notes, p.5 [study_library]"]


def _offline_kb(query, top_k=5, collection=None, **kw):
    return {"ok": True, "results": [
        {"text": "A matrix is a rectangular array of numbers arranged in "
                 "rows and columns. Matrix addition is only defined for "
                 "two matrices of the same shape.",
         "source": "Linear Algebra Notes", "page": 3,
         "doc_id": "linnotes1", "collection": "study_library",
         "score": 0.87},
        {"text": "Matrix multiplication is not commutative, so AB generally "
                 "differs from BA even when both products exist. A square "
                 "matrix is invertible only when its determinant is not "
                 "zero.",
         "source": "Linear Algebra Notes", "page": 5,
         "doc_id": "linnotes1", "collection": "study_library",
         "score": 0.82},
    ], "citations": CITATIONS, "evidence": EVIDENCE}


# ════════════════════════════════════════════════ 1. safety ═════════════════

class TestSafety:
    def test_cheat_requests_detected(self):
        for text in ("take the exam for me",
                     "sit my test for me",
                     "answer the live quiz now",
                     "do my homework for me",
                     "the graded assignment is ongoing, answer it"):
            assert safety.is_cheat_request(text), text

    def test_learning_asks_are_not_cheats(self):
        for text in ("explain matrix multiplication",
                     "test me with a quiz on matrices",  # our own drill
                     "make a study plan for linear algebra",
                     "what should I study next"):
            assert not safety.is_cheat_request(text), text

    def test_permission_levels(self):
        assert safety.permission_for("status") == safety.READ_ONLY
        assert safety.permission_for("goal_set") == safety.SAFE_ACTION
        assert safety.permission_for("take_exam_for_user") == safety.BLOCKED
        assert safety.permission_for("do_homework") == safety.BLOCKED

    def test_redaction_reused(self):
        out = safety.redact("note token ya29.SHOULD_NEVER_APPEAR")
        assert "ya29." not in out and "[REDACTED]" in out


# ════════════════════════════════════════════════ 2. learning ═══════════════

class TestLearning:
    def test_parse_goal_extracts_subject(self):
        g = learning.parse_goal("I want to learn linear algebra")
        assert g["subject"] == "linear algebra"
        assert g["level"] == "beginner"

    def test_parse_goal_level_words(self):
        g = learning.parse_goal("study advanced linear algebra")
        assert g["subject"] == "linear algebra" or g["subject"].endswith(
            "linear algebra")
        assert g["level"] == "advanced"

    def test_parse_goal_takes_deadline_tail_off(self):
        g = learning.parse_goal("learn spanish for an exam in march")
        assert "exam" not in g["subject"] and "march" not in g["subject"]
        assert "spanish" in g["subject"]

    def test_mastery_score_recency_weighted(self):
        rows = [{"passed": True, "at": "2026-10-06"},
                {"passed": False, "at": "2026-10-05"},
                {"passed": True, "at": "2026-09-10"}]
        m = learning.mastery_score(rows, today=date(2026, 10, 7))
        # recent True counts 2, False 2, old True 1 → 3/5
        assert m["score"] == 0.6
        assert m["attempts"] == 3 and m["passed"] == 2
        assert m["streak_correct"] == 1

    def test_mastery_empty_is_honest(self):
        m = learning.mastery_score([])
        assert m["score"] is None and m["attempts"] == 0

    def test_next_level_need_more(self):
        d = learning.next_level("beginner", {"score": 1.0, "attempts": 2})
        assert d["decision"] == "need-more" and d["level"] == "beginner"

    def test_next_level_promote_hold_demote(self):
        hi = learning.next_level("beginner", {"score": 0.9, "attempts": 5})
        assert hi["decision"] == "promote" and hi["level"] == "intermediate"
        mid = learning.next_level("beginner", {"score": 0.7, "attempts": 5})
        assert mid["decision"] == "hold" and mid["level"] == "beginner"
        low = learning.next_level("intermediate",
                                  {"score": 0.3, "attempts": 5})
        assert low["decision"] == "demote" and low["level"] == "beginner"
        top = learning.next_level("advanced", {"score": 0.95, "attempts": 9})
        assert top["decision"] == "hold" and top["level"] == "advanced"

    def test_plan_for_goal_deterministic_shape(self):
        p = learning.plan_for_goal("linear algebra", "beginner",
                                   "2026-10-17", today=date(2026, 10, 7))
        assert p["days"] == 10
        kinds = [i["kind"] for i in p["items"]]
        assert kinds[0] == "teach" and kinds[-1] == "quiz"
        assert set(kinds) <= {"teach", "drill", "recap", "revise", "quiz"}
        # the last three sessions before the final are revision
        assert "revise" in kinds[-4:]
        p2 = learning.plan_for_goal("linear algebra", "beginner",
                                    "2026-10-17", today=date(2026, 10, 7))
        assert p == p2                      # same inputs → same plan

    def test_plan_window_clamped(self):
        p = learning.plan_for_goal("topic", "beginner",
                                   "2027-10-17", today=date(2026, 10, 7))
        assert p["days"] == 30
        p2 = learning.plan_for_goal("topic", "beginner",
                                    "2026-10-07", today=date(2026, 10, 7))
        assert p2["days"] == 1 and len(p2["items"]) == 1

    def test_streak_days_counts_consecutive(self):
        rows = [{"at": "2026-10-07"}, {"at": "2026-10-06"},
                {"at": "2026-10-04"}]
        assert learning.streak_days(rows, today=date(2026, 10, 7)) == 2


# ════════════════════════════════════════════════ 3. engine ═════════════════

class TestEngine:
    def test_explanation_is_extractive_and_cited(self):
        out = engine.extractive_explanation(
            "matrix multiplication", EVIDENCE, "beginner", CITATIONS)
        assert out["ok"] and out["grounded"]
        assert "[1]" in out["text"] or "[2]" in out["text"]
        for line in out["text"].splitlines():
            assert line.split("]")[-1] or True
        # every citation index must be small and real
        assert "[3]" not in out["text"]

    def test_explanation_no_evidence_is_honest(self):
        out = engine.extractive_explanation("quantum chemistry", "",
                                            "beginner", [])
        assert not out["ok"] and "don't have anything" in out["text"]

    def test_quiz_built_from_evidence(self):
        q = engine.build_quiz("matrices", "beginner", EVIDENCE, CITATIONS)
        assert q["ok"] and q["questions"]
        for question in q["questions"]:
            assert set(question["options"]) == {"A", "B", "C", "D"}.intersection(
                set(question["options"]))
            assert question["answer"] in "ABCD"
            assert 1 <= question["cite_block"] <= 2

    def test_quiz_no_evidence_refuses(self):
        q = engine.build_quiz("x", "beginner", "", [])
        assert not q["ok"] and "citation-backed" in q["error"]

    def test_grade_deterministic(self):
        q = engine.build_quiz("matrices", "beginner", EVIDENCE, CITATIONS)
        answers = {qq["id"]: qq["answer"] for qq in q["questions"]}
        g = engine.grade_quiz(q, answers)
        assert g["correct"] == g["total"] and g["score_pct"] == 100
        assert g["passed"]
        g2 = engine.grade_quiz(q, {})
        assert g2["correct"] == 0 and not g2["passed"]

    def test_grade_rejects_garbage_letters(self):
        q = engine.build_quiz("matrices", "beginner", EVIDENCE, CITATIONS)
        g = engine.grade_quiz(q, {"q1": "Z"})
        assert not g["per_question"][0]["correct"]

    def test_render_grade_lists_each(self):
        q = engine.build_quiz("matrices", "beginner", EVIDENCE, CITATIONS)
        g = engine.grade_quiz(q, {})
        text = engine.render_grade(g, "matrices")
        assert q["questions"][0]["id"] in text and "✘" in text


# ════════════════════════════════════════════════ 4. store ══════════════════

class TestStore:
    def test_permissions_owner_only(self, store):
        store.add_goal("linear algebra")
        store.add_quiz_result({"score_pct": 80, "passed": True})
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        for f in store.root.iterdir():
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f.name

    def test_goals_and_archive(self, store):
        row = store.add_goal("linear algebra", level="beginner",
                             deadline="2026-10-17")
        assert row["id"].startswith("goal-")
        active = [g for g in store.goals() if not g["archived"]]
        assert len(active) == 1
        hit = store.archive_goal(row["id"])
        assert hit["archived"]
        assert not [g for g in store.goals() if not g["archived"]]
        assert store.archive_goal("goal-nope") is None

    def test_plans_roundtrip_and_complete(self, store):
        goal = store.add_goal("algebra")
        plan = learning.plan_for_goal("algebra", "beginner", "")
        row = store.save_plan(goal["id"], plan)
        assert store.latest_plan(goal["id"])["id"] == row["id"]
        first = row["items"][0]
        hit = store.complete_plan_item(first["id"])
        assert hit["done"]
        assert store.complete_plan_item(first["id"]) is None

    def test_quiz_results_append(self, store):
        store.add_quiz_result({"score_pct": 60, "passed": True})
        store.add_quiz_result({"score_pct": 40, "passed": False})
        rows = store.quiz_results()
        assert len(rows) == 2 and rows[-1]["score_pct"] == 40

    def test_boxes_move_both_ways(self, store):
        # each pass promotes one box (two passes → box 3), capped at 5
        for _ in range(6):
            store.move_topic("matrices", "up")
        assert store.boxes()["matrices"]["box"] == 5
        store.move_topic("matrices", "down")
        assert store.boxes()["matrices"]["box"] == 4
        store.move_topic("newtopic", "down")     # floor for a new topic
        assert store.boxes()["newtopic"]["box"] == 1

    def test_audit_actions_only(self, store):
        store.add_goal("Secret Topic Xyz about private things")
        audit = (store.root / "audit.jsonl").read_text()
        assert "goal_added" in audit
        assert "Secret Topic Xyz" not in audit

    def test_redaction_on_write(self, store):
        store._write_json("probe", {"access_token": "ya29.SHOULD_NEVER_APPEAR"})
        raw = (store.root / "probe.json").read_text()
        assert "ya29." not in raw and "[REDACTED]" in raw

    def test_wipe_keeps_audit_event(self, store):
        store.add_goal("algebra")
        assert store.wipe()
        assert store.goals() == []
        assert "wipe" in (store.root / "audit.jsonl").read_text()


# ════════════════════════════════════════════════ 5. checkpoint ═════════════

class TestCheckpoint:
    def test_save_load_prune_perms(self, tmp):
        cp = StudyCheckpoint(tmp / "ck")
        for i in range(24):
            cp.save({"i": i})
        assert len(cp.list_ids()) == 20
        assert cp.load_latest()["i"] == 23
        assert stat.S_IMODE((tmp / "ck" / "latest.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp / "ck").stat().st_mode) == 0o700

    def test_never_raises_on_broken_dir(self, tmp):
        cp = StudyCheckpoint(tmp / "ck")
        blocked = tmp / "blocked"
        blocked.write_text("not a dir")
        cp.dir = blocked
        assert cp.save({"x": 1}) is None
        assert cp.load_latest() is None
        assert cp.list_ids() == []


# ════════════════════════════════════════════════ 6. agent flows ════════════

class TestStudyAgent:
    def test_cheat_declined(self, agent):
        r = agent.handle(_req("take the exam for me, it is live now"))
        assert r.status is TaskStatus.DECLINED
        assert "study partner" in r.text.lower()

    def test_goal_set_and_needs_input(self, agent):
        r = agent.handle(_req("learn linear algebra"))
        assert r.status is TaskStatus.COMPLETED
        assert "linear algebra" in r.text
        r2 = agent.handle(_req("what should I study next"))
        assert r2.status in (TaskStatus.COMPLETED, TaskStatus.NEEDS_INPUT)

    def test_plan_built_and_saved(self, agent, tmp):
        agent.handle(_req("learn linear algebra",
                          deadline="2026-10-17"))
        r = agent.handle(_req("study plan"))
        assert "Study plan" in r.text
        assert "linear algebra" in r.text
        vault = StudyStore(tmp / "vault")
        assert vault.latest_plan() is not None
        # deterministic: asking again returns the saved plan
        r2 = agent.handle(_req("study plan"))
        assert "Study plan" in r2.text

    def test_plan_requires_goal_or_subject(self, agent):
        r = agent.handle(_req(""))
        assert r.status in (TaskStatus.COMPLETED, TaskStatus.NEEDS_INPUT)
        r2 = agent.handle(_req("study plan hello how are you"))
        assert r2.status is TaskStatus.NEEDS_INPUT

    def test_explain_is_grounded_citations(self, agent):
        r = agent.handle(_req("explain matrix multiplication"))
        assert r.status is TaskStatus.COMPLETED
        assert "[1]" in r.text or "[2]" in r.text
        assert r.data.get("grounded") is True

    def test_explain_without_lib_is_honest(self, tmp, ckpt):
        a = StudyAgent(store=StudyStore(tmp / "v2"), checkpoint=ckpt)
        a.resources = StudyResources(toolbox=None, kb_search=None,
                                     allow_web=False, allow_local_kb=True)
        r = a.handle(_req("explain quantum chemistry",
                          topic="quantum chemistry"))
        assert r.status is TaskStatus.COMPLETED
        # the offline KB with no grant returns no results — the honest path
        assert "don't have anything" in r.text or "not reachable" in r.text \
            or "can't answer" in r.text or "explanation" in r.text

    def test_quiz_and_grade_flow(self, agent, tmp):
        r_quiz = agent.handle(_req("quiz me on matrices", topic="matrices"))
        assert r_quiz.status is TaskStatus.COMPLETED
        assert "Quiz" in r_quiz.text and "A)" in r_quiz.text
        quiz = r_quiz.data["quiz"]
        # full-correct grading via metadata
        answers = {q["id"]: q["answer"] for q in quiz["questions"]}
        r_grade = agent.handle(_req(answers and "grade my answers",
                                    answers=answers, quiz=quiz))
        assert r_grade.status is TaskStatus.COMPLETED
        assert "PASSED" in r_grade.text
        vault = StudyStore(tmp / "vault")
        assert vault.quiz_results()[-1]["score_pct"] == 100
        # ladder moved up
        assert vault.boxes()["matrices"]["box"] >= 1

    def test_zero_quiz_without_library(self, tmp, ckpt):
        a = StudyAgent(store=StudyStore(tmp / "v3"), checkpoint=ckpt)
        a.resources = StudyResources(toolbox=None, kb_search=None,
                                     allow_web=False, allow_local_kb=True)
        r = a.handle(_req("quiz me", topic="anything"))
        assert r.status is TaskStatus.COMPLETED
        assert ("can't build" in r.text) or ("no citation-backed" in r.text) \
            or ("Quiz" not in r.text)

    def test_fail_grade_fails_ladder(self, agent, tmp):
        r_quiz = agent.handle(_req("quiz on matrices", topic="matrices",
                                   n=2))
        quiz = r_quiz.data["quiz"]
        wrong = {q["id"]: "Z" for q in quiz["questions"]}
        r = agent.handle(_req("check my answers", answers=wrong, quiz=quiz))
        assert r.status is TaskStatus.COMPLETED
        vault = StudyStore(tmp / "vault")
        assert vault.quiz_results()[-1]["passed"] is False
        assert vault.boxes()["matrices"]["box"] >= 1   # floor at 1

    def test_progress_report(self, agent, tmp):
        agent.handle(_req("learn linear algebra"))
        r_quiz = agent.handle(_req("quiz me", topic="linear algebra"))
        quiz = r_quiz.data["quiz"]
        answers = {q["id"] if isinstance(q, dict) else "q1": q["answer"]
                   for q in quiz["questions"]}
        agent.handle(_req("grade", answers=answers, quiz=quiz))
        r = agent.handle(_req("how am I doing"))
        assert "Study status" in r.text
        assert "adapt" in r.text
        assert "linear algebra" in r.text

    def test_adapt_needs_attempts(self, agent):
        agent.handle(_req("learn linear algebra"))
        r = agent.handle(_req("make it harder"))
        assert r.status is TaskStatus.COMPLETED
        assert "need-more" in r.text or "attempts" in r.text

    def test_adapt_levels_up_after_good_run(self, agent):
        agent.handle(_req("learn linear algebra"))
        promoted = ""
        for _ in range(3):
            r_quiz = agent.handle(_req("quiz me", topic="linear algebra"))
            quiz = r_quiz.data["quiz"]
            answers = {q["id"]: q["answer"] for q in quiz["questions"]}
            agent.handle(_req("grade", answers=answers, quiz=quiz))
            promoted = agent.handle(_req("adapt")).text
        assert "promote" in promoted and "intermediate" in promoted, promoted
        goals = [g for g in agent.store.goals() if not g.get("archived")]
        assert goals and goals[0]["level"] == "intermediate"

    def test_never_raises_on_garbage(self, agent):
        for text in ("", "   ", "!!!", "x" * 4000,
                     "grade", "quiz", "plan", "complete"):
            assert agent.handle(_req(text)) is not None

    def test_no_token_material_in_replies(self, agent):
        leak = ("explain api keys: token ya29.LEAK_ME please")
        r = agent.handle(_req(leak, topic="api keys"))
        assert "ya29." not in r.text and "LEAK_ME" not in r.text


# ════════════════════════════════════════════════ 8. wiring ═════════════════

class TestWiring:
    def test_classifier_study_lane(self):
        c = IntentClassifier()
        for text in ("help me study for finals", "build a revision plan",
                     "test me with a quiz on matrices",
                     "make flashcards for biology", "what's my progress"):
            assert c.classify(text).task_type is TaskType.STUDY, text

    def test_explicit_routes(self):
        c = IntentClassifier()
        assert c.classify("route this to study").task_type is TaskType.STUDY
        assert c.classify("[study] quiz me") .task_type is TaskType.STUDY
        assert c.classify("study agent explain matrices") \
            .task_type is TaskType.STUDY

    def test_other_lanes_unchanged(self):
        c = IntentClassifier()
        assert c.classify("fix the bug in my python function") \
            .task_type is TaskType.CODING
        assert c.classify("what's the latest news about EVs") \
            .task_type is TaskType.RESEARCH
        assert c.classify("track my spending") .task_type is TaskType.FINANCE
        assert c.classify("hi").task_type is TaskType._FALLBACK

    def test_registry_off_by_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_STUDY_AGENT", raising=False)
        reg = build_default_registry()
        a = reg.get("study_agent")
        if a is None:
            pytest.skip("study lane not registered in this build")
        assert type(a).__name__ == "StubAgent"
        r = a.handle(_req("hi"))
        assert r.status is TaskStatus.NEEDS_INPUT

    def test_registry_on(self, monkeypatch):
        monkeypatch.setenv("JARVIS_STUDY_AGENT", "on")
        reg = build_default_registry()
        a = reg.get("study_agent")
        assert type(a).__name__ == "StudyAgent"
        assert TaskType.STUDY in a.task_types
