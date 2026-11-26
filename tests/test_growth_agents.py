"""
tests/test_growth_agents.py — offline acceptance tests for the Phase 18
growth capability (self-evaluation / lead score / marketing).

NO network, NO LLM keys, NO real embeddings: JARVIS_HOME points at a
fresh temp dir, the marketing agent runs with evidence CARRIED IN the
request (no research-agent web call), the reflection loop runs its
deterministic floor, and the vault is sandboxed.

What this locks in:

  SHARED VAULT
    1. perms + containment  — 0600 files in a 0700 dir, redaction on
       write, actions-only audit, wipe.
  LEAD SCORING
    2. engine              — user-defined rules only (no rules → honest
       refusal, never default points), where/min/max/has matching,
       per-rule breakdown, deterministic tie order, cold leads named.
    3. gating              — scoring is read-only; ingest/rules-writes/
       forget/mark park the EXACT payload; resume commits it; re-resume
       declined; outreach execution DECLINED in code.
    4. human review        — disclaimer on every score; review sheet
       carries the breakdown.
  SELF-EVALUATION
    5. evidence honesty    — results computed from records; goals with
       no activity are reported as such, not padded; improvement plan
       derived from flagged gaps only.
    6. reflection          — the orchestrator Reflector runs (score,
       rounds, never raises); deletion refused in code.
  MARKETING
    7. no invented facts   — carried-in evidence is the only base;
       uncited findings produce NO options; every option cites sources;
       unverified claims are labeled and excluded.
    8. campaign plans      — plans validate against orchestrator/plans
       (Plan-and-Execute vocabulary), carry citations in risk_notes, and
       mark the publish step human-executed; execution asks DECLINED.
  WIRING
    9. classifier lanes + explicit routes; registry stubs off by
       default, real agents behind JARVIS_*_AGENT=on.

Run:  .venv/bin/python -m pytest tests/test_growth_agents.py -q
"""
from __future__ import annotations

import itertools
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── isolation: this module gets a fresh JARVIS_HOME ──────────────────────
_TMP = tempfile.mkdtemp(prefix="jarvis-growth-")
os.environ["JARVIS_HOME"] = _TMP

from orchestrator.agents import build_default_registry                 # noqa: E402
from orchestrator.agents.growth import lead_scoring, safety, shared    # noqa: E402
from orchestrator.agents.growth.lead_agent import LeadAgent            # noqa: E402
from orchestrator.agents.growth.marketing_agent import MarketingAgent  # noqa: E402
from orchestrator.agents.growth.self_eval_agent import SelfEvalAgent   # noqa: E402
from orchestrator.agents.growth.shared import (GrowthCheckpoint,       # noqa: E402
                                               GrowthVault)
from orchestrator.classifier import IntentClassifier                   # noqa: E402
from orchestrator.contracts import (AgentRequest, TaskStatus,          # noqa: E402
                                    TaskType)
from orchestrator.plans import Plan                                    # noqa: E402

_SEQ = itertools.count(1)


def _req(text: str = "", ttype: TaskType = TaskType.LEAD_SCORE,
         **meta) -> AgentRequest:
    return AgentRequest(task_id=f"growth-test-{next(_SEQ):04d}",
                        task_type=ttype, text=text, metadata=meta)


@pytest.fixture()
def tmp():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture()
def vault(tmp):
    return GrowthVault(tmp / "growth")


@pytest.fixture()
def ckpt(tmp):
    return GrowthCheckpoint(tmp / "ckpt")


@pytest.fixture()
def lead_agent(tmp, vault, ckpt):
    return LeadAgent(store=vault, checkpoint=ckpt)


@pytest.fixture()
def eval_agent(tmp, vault):
    return SelfEvalAgent(store=vault)


@pytest.fixture()
def mkt_agent(tmp, vault):
    return MarketingAgent(store=vault, researcher=None)


# ── carried-in research evidence (the offline marketing path) ────────────
CARRIED_REPORT = {
    "summary": ("SaaS onboarding costs dominate. The market shows high "
                "churn in week one [1], and pricing pressure in the "
                "mid-tier [2]."),
    "key_findings": [
        "Week-one churn in self-serve SaaS averages 40% [1]",
        "Mid-tier buyers compare three vendors before purchase [2]",
    ],
    "unverified_claims": ["AI adoption will triple by 2028"],
    "uncertainties": ["both findings come from vendor blogs"],
    "supported_claims": 2,
    "confidence": "medium",
    "sources": [
        {"index": 1, "title": "Churn Report 2026",
         "url": "https://example.com/churn", "host": "example.com",
         "tier": "web"},
        {"index": 2, "title": "Vendor Benchmarks",
         "url": "https://other.example.org/bench",
         "host": "other.example.org", "tier": "web"},
    ],
}


# ═════════════════════════════════════════════════════════════════════════
# shared vault
# ═════════════════════════════════════════════════════════════════════════

def test_vault_permissions_and_containment(tmp, vault):
    vault.save_goals([{"name": "g", "target": "", "metric": ""}])
    f = vault.root / "goals.json"
    assert f.exists()
    assert stat.S_IMODE(f.stat().st_mode) & 0o777 == 0o600
    assert stat.S_IMODE(vault.root.stat().st_mode) & 0o777 == 0o700
    # containment: a crafted name is mangled INSIDE the vault root —
    # it can never write outside it (no ../escape.json appears anywhere)
    assert vault._write_json("../escape", {"x": 1}) is True
    assert not (tmp / "escape.json").exists()
    assert (vault.root / "escape.json").exists()


def test_vault_redaction_and_actions_only_audit(vault):
    vault.save_goals([{"name": "goal with token sk-abcdef1234567890",
                       "target": "", "metric": ""}])
    blob = (vault.root / "goals.json").read_text(encoding="utf-8")
    assert "sk-abcdef1234567890" not in blob
    vault.audit("goal_added", name="secret goal text should clip")
    audit = (vault.root / "audit.jsonl").read_text(encoding="utf-8")
    assert "secret goal text should clip" not in audit  # clipped to 60
    assert "goal_added" in audit


def test_vault_dedupe_and_summary(vault):
    r1 = vault.add_leads([{"name": "A", "company": "Co",
                           "email": "a@x.com"}])
    r2 = vault.add_leads([{"name": "A", "company": "Co",
                           "email": "a@x.com"}])
    assert r1["added"] == 1 and r2["duplicates"] == 1
    assert vault.summary()["leads"] == 1
    assert vault.wipe() is True
    assert vault.summary()["leads"] == 0


def test_clean_lead_rejects_nameless_companyless_rows():
    assert shared.clean_lead({"email": "only@x.com"}) is None
    cl = shared.clean_lead({"name": "A", "fields": {"team_size": 50}})
    assert cl is not None and cl["fields"]["team_size"] == 50


# ═════════════════════════════════════════════════════════════════════════
# lead scoring engine
# ═════════════════════════════════════════════════════════════════════════

RULES = [
    {"name": "saas-fit", "points": 30, "where": {"industry": "saas"}},
    {"name": "size", "points": 25, "min": {"team_size": 50}},
    {"name": "fresh", "points": 15, "max": {"poke_days": 7}},
    {"name": "decision-maker", "points": 20, "has": ["decision_maker"]},
]


def test_rule_matching_where_min_max_has():
    lead = {"lead_id": "l1", "name": "A", "company": "Co",
            "fields": {"industry": "saas", "team_size": 80,
                       "poke_days": 3, "decision_maker": "yes"}}
    hit, why = lead_scoring.rule_matches(RULES[0], lead)
    assert hit is not None and hit.points == 30
    hit, _ = lead_scoring.rule_matches(RULES[1], lead)
    assert hit is not None and hit.points == 25
    hit, _ = lead_scoring.rule_matches(RULES[2], lead)
    assert hit is not None
    hit, _ = lead_scoring.rule_matches(RULES[3], lead)
    assert hit is not None
    # misses, honestly reported
    hit, why = lead_scoring.rule_matches(RULES[0],
                                         {"fields": {"industry": "retail"}})
    assert hit is None and "saas" in why


def test_no_rules_no_points_no_invention():
    s = lead_scoring.score_lead({"name": "A", "lead_id": "x"}, [])
    assert s.total == 0 and s.grade == "cold"
    assert s.hits == []


def test_score_breakdown_and_deterministic_order():
    leads = [
        {"lead_id": "a", "name": "A",
         "fields": {"industry": "saas", "team_size": 80}},
        {"lead_id": "b", "name": "B",
         "fields": {"industry": "saas", "team_size": 10}},
    ]
    rules = [
        {"name": "fit", "points": 30, "where": {"industry": "saas"}},
        {"name": "big", "points": 20, "min": {"team_size": 50}},
    ]
    scores = lead_scoring.score_leads(leads, rules)
    assert [s.lead_id for s in scores] == ["a", "b"]
    assert scores[0].total == 50 and scores[0].grade == "hot"
    assert [h.rule for h in scores[0].hits] == ["fit", "big"]
    assert scores[1].total == 30
    # reproducible: same input, same order
    again = lead_scoring.score_leads(leads, rules)
    assert [s.lead_id for s in again] == [s.lead_id for s in scores]


def test_rule_validation_rejects_matching_nothing():
    rule, errors = shared.clean_rule({"name": "empty", "points": 10})
    assert rule is None and errors
    rule, _ = shared.clean_rule({"name": "ok", "points": 10,
                                 "where": {"industry": "saas"}})
    assert rule is not None


# ═════════════════════════════════════════════════════════════════════════
# lead agent
# ═════════════════════════════════════════════════════════════════════════

def test_score_without_rules_is_honest(lead_agent):
    res = lead_agent.handle(_req("score my leads"))
    assert res.status is TaskStatus.NEEDS_INPUT
    assert "won't invent" in res.text


def test_score_requires_leads(lead_agent):
    lead_agent.store.save_rules(RULES)
    res = lead_agent.handle(_req("score", lead_action="score"))
    assert res.status is TaskStatus.NEEDS_INPUT
    assert "ingest" in res.text.lower()


def _seed_leads(lead_agent):
    lead_agent.store.save_rules(RULES)
    lead_agent.store.add_leads([
        {"lead_id": "lead-a", "name": "Alpha", "company": "AlphaCo",
         "email": "a@alphaco.io", "fields": {"industry": "saas",
                                             "team_size": 80,
                                             "poke_days": 3}},
        {"lead_id": "lead-b", "name": "Beta", "company": "BetaLtd",
         "email": "b@betaltd.com", "fields": {"industry": "retail",
                                              "team_size": 5}},
    ])


def test_score_prioritizes_and_explains(lead_agent):
    _seed_leads(lead_agent)
    res = lead_agent.handle(_req("score my leads", lead_action="score"))
    assert res.status is TaskStatus.COMPLETED
    assert "lead-a" in res.text and "lead-b" in res.text
    assert "Outreach priority" in res.text
    # per-rule breakdown is IN the reply (explain factors)
    assert "saas-fit" in res.text and "+30" in res.text
    # cold lead named, not padded
    assert "cold" in res.text
    # human-review disclaimer on every score
    assert "human reviews" in res.text or "advisory" in res.text


def test_ingest_is_gated_and_resume_commits_exact_payload(lead_agent, tmp):
    f = tmp / "leads.json"
    f.write_text(json.dumps([
        {"name": "Gamma", "company": "GammaCo", "email": "g@g.io",
         "fields": {"team_size": 120}},
        {"email": "nameless@x.io"},          # invalid: no name/company
    ]), encoding="utf-8")
    req = _req("ingest these leads", lead_action="ingest", path=str(f))
    res = lead_agent.handle(req)
    assert res.status is TaskStatus.NEEDS_APPROVAL
    assert lead_agent.store.leads() == []          # nothing stored yet
    # resume with the parked payload — same task id
    ok = lead_agent.handle(AgentRequest(
        task_id=req.task_id, task_type=TaskType.LEAD_SCORE, text="approve",
        metadata={"approved": True, "resume": req.task_id, "by": "user"}))
    assert ok.status is TaskStatus.COMPLETED, ok.text
    rows = lead_agent.store.leads()
    assert len(rows) == 1 and rows[0]["name"] == "Gamma"
    # re-resume is declined
    again = lead_agent.handle(AgentRequest(
        task_id=req.task_id, task_type=TaskType.LEAD_SCORE, text="approve",
        metadata={"approved": True, "resume": req.task_id, "by": "user"}))
    assert again.status is TaskStatus.DECLINED


def test_csv_ingest_maps_fields(lead_agent, tmp):
    f = tmp / "leads.csv"
    f.write_text("name,company,email,team_size,industry\n"
                 "Delta,DeltaInc,d@d.io,90,saas\n", encoding="utf-8")
    req = _req("import", lead_action="ingest", path=str(f))
    assert lead_agent.handle(req).status is TaskStatus.NEEDS_APPROVAL
    lead_agent.handle(AgentRequest(
        task_id=req.task_id, task_type=TaskType.LEAD_SCORE, text="ok",
        metadata={"approved": True, "resume": req.task_id}))
    rows = lead_agent.store.leads()
    assert rows and rows[0]["fields"]["team_size"] == 90.0
    assert rows[0]["fields"]["industry"] == "saas"


def test_add_rule_is_gated_then_changes_scores(lead_agent):
    lead_agent.store.add_leads([{"lead_id": "l1", "name": "A",
                                 "fields": {"industry": "saas"}}])
    req = _req("add rule", lead_action="add_rule", rule={
        "name": "saas-only", "points": 40, "where": {"industry": "saas"}})
    res = lead_agent.handle(req)
    assert res.status is TaskStatus.NEEDS_APPROVAL
    assert lead_agent.store.rules() == []          # not saved yet
    ok = lead_agent.handle(AgentRequest(
        task_id=req.task_id, task_type=TaskType.LEAD_SCORE, text="approve",
        metadata={"approved": True, "resume": req.task_id}))
    assert ok.status is TaskStatus.COMPLETED
    assert len(lead_agent.store.rules()) == 1
    scored = lead_agent.handle(_req("score", lead_action="score"))
    assert "+40" in scored.text


def test_invalid_rule_is_refused_not_saved(lead_agent):
    res = lead_agent.handle(_req("add", lead_action="add_rule", rule={
        "name": "does-nothing", "points": 10}))
    assert res.status is TaskStatus.NEEDS_INPUT
    assert lead_agent.store.rules() == []


def test_outreach_execution_is_declined_in_code(lead_agent):
    _seed_leads(lead_agent)
    res = lead_agent.handle(_req("send an outreach blast to my leads"))
    assert res.status is TaskStatus.DECLINED
    assert "don't execute outreach" in res.text or "execute" in res.text


def test_mark_contacted_and_forget_lead_are_gated(lead_agent):
    _seed_leads(lead_agent)
    for action, meta in (("mark_contacted", {"lead_id": "lead-a"}),
                         ("forget_lead", {"lead_id": "lead-b"})):
        req = _req(f"{action}", lead_action=action, **meta)
        res = lead_agent.handle(req)
        assert res.status is TaskStatus.NEEDS_APPROVAL, res.text
        ok = lead_agent.handle(AgentRequest(
            task_id=req.task_id, task_type=TaskType.LEAD_SCORE,
            text="approve", metadata={"approved": True,
                                      "resume": req.task_id}))
        assert ok.status is TaskStatus.COMPLETED, ok.text
    ids = [l["lead_id"] for l in lead_agent.store.leads()]
    assert "lead-b" not in ids and "lead-a" in ids
    acts = [a for a in lead_agent.store.activities()
            if a.get("event") == "lead_contacted"]
    assert acts and acts[0]["lead_id"] == "lead-a"


def test_review_sheet_carries_breakdown(lead_agent):
    _seed_leads(lead_agent)
    res = lead_agent.handle(_req("review", lead_action="review",
                                 lead_id="lead-a"))
    assert res.status is TaskStatus.COMPLETED
    assert "Review sheet" in res.text and "saas-fit" in res.text
    assert "evidence, not a queue" in res.text


def test_lead_agent_never_raises_on_garbage(lead_agent):
    res = lead_agent.handle(_req("", lead_action=None))
    assert res.status is TaskStatus.COMPLETED or \
        res.status is TaskStatus.NEEDS_INPUT


# ═════════════════════════════════════════════════════════════════════════
# self-evaluation agent
# ═════════════════════════════════════════════════════════════════════════

def test_review_without_goals_is_honest(eval_agent):
    res = eval_agent.handle(_req("review my week",
                                 ttype=TaskType.SELF_EVAL,
                                 eval_action="review"))
    assert res.status is TaskStatus.COMPLETED
    assert "no goals recorded" in res.text.lower()


def test_results_computed_from_records_no_padding(eval_agent):
    eval_agent.store.save_goals([
        {"name": "ship v2", "target": "2027-06-01", "metric": "signups"},
        {"name": "grow list", "target": "", "metric": ""},
    ])
    eval_agent.store.log_activity({"event": "activity",
                                   "what": "shipped the parser",
                                   "goal_name": "ship v2",
                                   "at": "2026-10-01T10:00:00"})
    res = eval_agent.handle(_req("review", ttype=TaskType.SELF_EVAL,
                                 eval_action="review"))
    assert res.status is TaskStatus.COMPLETED
    assert "no_activity" in res.text          # grow list: plainly reported
    assert "ship v2" in res.text
    assert "1 logged activit" in res.text
    # reflection ran through the Reflector
    assert "Reflection loop" in res.text and "critic=" in res.text
    # review persisted as a vault record
    assert len(eval_agent.store.reviews()) == 1


def test_overdue_and_stalled_flags(eval_agent):
    eval_agent.store.save_goals([
        {"name": "late goal", "target": "2020-01-01", "metric": ""},
        # with days_left < 30 the thin goal reads "stalled": the target is
        # computed so the test never expires into "overdue"
        {"name": "thin goal",
         "target": (datetime.now(timezone.utc).replace(tzinfo=None)
                    + timedelta(days=25)).strftime("%Y-%m-%d"),
         "metric": ""},
    ])
    for i in range(2):
        eval_agent.store.log_activity({"event": "activity",
                                       "what": f"step {i}",
                                       "goal_name": "thin goal",
                                       "at": "2026-10-01T10:00:00"})
    res = eval_agent.handle(_req("review", ttype=TaskType.SELF_EVAL,
                                 eval_action="review"))
    assert "overdue" in res.text
    assert "stalled" in res.text
    # improvement plan derives from flagged gaps only
    assert "Improvement plan" in res.text
    assert "re-scope" in res.text or "re-plan" in res.text
    assert "increase cadence" in res.text


def test_improvement_plan_action_and_no_pad(eval_agent):
    eval_agent.store.save_goals([
        {"name": "idle", "target": "", "metric": ""},
    ])
    eval_agent.handle(_req("review", ttype=TaskType.SELF_EVAL,
                           eval_action="review"))
    res = eval_agent.handle(_req("improvement plan",
                                 ttype=TaskType.SELF_EVAL,
                                 eval_action="plan"))
    assert "restart with one concrete activity" in res.text
    # a clean review produces NO invented plan
    eval_agent.store.save_goals([
        {"name": "active", "target": "2027-06-01", "metric": ""}])
    for i in range(4):
        eval_agent.store.log_activity({"event": "activity",
                                       "what": f"work {i}",
                                       "goal_name": "active",
                                       "at": "2026-10-02T10:00:00"})
    eval_agent.handle(_req("review", ttype=TaskType.SELF_EVAL,
                           eval_action="review"))
    res2 = eval_agent.handle(_req("plan", ttype=TaskType.SELF_EVAL,
                                  eval_action="plan"))
    assert "no complaints" in res2.text or "nothing flagged" in res2.text


def test_goal_deletion_is_refused_in_code(eval_agent):
    eval_agent.store.save_goals([{"name": "keep me", "target": "",
                                  "metric": ""}])
    res = eval_agent.handle(_req("delete my goals",
                                 ttype=TaskType.SELF_EVAL))
    assert res.status is TaskStatus.DECLINED
    assert eval_agent.store.goals()          # still there


def test_add_goal_and_log_activity(eval_agent):
    res = eval_agent.handle(_req("set goal", ttype=TaskType.SELF_EVAL,
                                 eval_action="add_goal",
                                 goal={"name": "ship v3",
                                       "target": "2027-01-01"}))
    assert res.status is TaskStatus.COMPLETED
    assert len(eval_agent.store.goals()) == 1
    res2 = eval_agent.handle(_req("log", ttype=TaskType.SELF_EVAL,
                                  eval_action="log_activity",
                                  what="wrote the spec",
                                  goal_name="ship v3"))
    assert res2.status is TaskStatus.COMPLETED
    assert len(eval_agent.store.activities()) == 1


def test_reflection_never_raises_with_injected_reflector(eval_agent):
    from orchestrator.reflection import Reflector
    eval_agent.reflector = Reflector()       # deterministic floor
    eval_agent.store.save_goals([{"name": "g", "target": "", "metric": ""}])
    res = eval_agent.handle(_req("review", ttype=TaskType.SELF_EVAL,
                                 eval_action="review"))
    assert res.status is TaskStatus.COMPLETED
    assert "critic=deterministic" in res.text


# ═════════════════════════════════════════════════════════════════════════
# marketing agent
# ═════════════════════════════════════════════════════════════════════════

def test_marketing_without_evidence_never_invents(mkt_agent):
    res = mkt_agent.handle(_req("build a marketing strategy",
                                ttype=TaskType.MARKETING))
    # no researcher, nothing carried in → honest refusal, no invented facts
    assert "no" in res.text.lower() and ("invent" in res.text.lower()
                                         or "evidence" in res.text.lower())
    assert "Option" not in res.text


def test_strategy_options_cite_evidence(mkt_agent):
    res = mkt_agent.handle(_req("strategy", ttype=TaskType.MARKETING,
                                mkt_action="strategy",
                                report=CARRIED_REPORT))
    assert res.status is TaskStatus.COMPLETED, res.text
    assert "Option A" in res.text
    assert "[1]" in res.text                  # citations present
    assert "nothing invented" in res.text.lower() or \
        "citation-backed" in res.text.lower()


def test_uncited_findings_produce_no_options(mkt_agent):
    bad = dict(CARRIED_REPORT)
    bad["key_findings"] = ["AI adoption will triple by 2028"]  # no [n]
    bad["supported_claims"] = 0
    res = mkt_agent.handle(_req("strategy", ttype=TaskType.MARKETING,
                                mkt_action="strategy", report=bad))
    assert res.status is TaskStatus.COMPLETED
    assert "No strategy options" in res.text
    assert "invented market facts" in res.text


def test_unverified_claims_are_labeled_excluded(mkt_agent):
    res = mkt_agent.handle(_req("brief", ttype=TaskType.MARKETING,
                                mkt_action="full",
                                report=CARRIED_REPORT))
    assert "Unverified (not facts)" in res.text or "GUESS" in res.text
    assert "AI adoption will triple" in res.text   # shown, labeled
    assert "Option" not in res.text.split("Unverified")[0] or \
        "citation-backed" in res.text


def test_campaign_plan_is_valid_plan_artifact(mkt_agent):
    res = mkt_agent.handle(_req("campaign", ttype=TaskType.MARKETING,
                                mkt_action="campaign", option=0,
                                report=CARRIED_REPORT))
    assert res.status is TaskStatus.COMPLETED, res.text
    data = res.data
    assert data.get("plan_id")
    plan = Plan.from_dict(data["plan"])
    assert plan.goal.startswith("campaign:")
    assert [s.id for s in plan.ordered_steps()] == ["s1", "s2", "s3", "s4"]
    # citations travel on the plan (risk notes)
    assert any("[1]" in r for r in plan.risk_notes)
    # publish step is human-executed
    publish = plan.steps_for("s4")
    assert "HUMAN" in publish.title
    # review step requires approval
    assert plan.steps_for("s3").requires_approval is True


def test_campaign_without_premise_is_refused(mkt_agent):
    bad = dict(CARRIED_REPORT)
    bad["key_findings"] = []
    bad["supported_claims"] = 0
    res = mkt_agent.handle(_req("campaign", ttype=TaskType.MARKETING,
                                mkt_action="campaign", report=bad))
    assert "no campaign" in res.text.lower() or "No citation" in res.text


def test_marketing_execution_is_declined(mkt_agent):
    res = mkt_agent.handle(_req("send the campaign to my list",
                                ttype=TaskType.MARKETING))
    assert res.status is TaskStatus.DECLINED
    assert "don't execute outreach" in res.text


def test_research_failure_is_reported_not_faked(mkt_agent):
    class _DeadResearcher:
        def handle(self, request):
            from orchestrator.contracts import AgentResult
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               text="no network", agent="research_agent")
    mkt_agent._researcher = _DeadResearcher()
    res = mkt_agent.handle(_req("research the market",
                                ttype=TaskType.MARKETING,
                                mkt_action="research", query="market"))
    assert "could not run" in res.text.lower()
    assert "assumptions" in res.text.lower() or \
        "not fill" in res.text.lower()


def test_sources_listed_with_urls(mkt_agent):
    res = mkt_agent.handle(_req("full brief", ttype=TaskType.MARKETING,
                                mkt_action="full", report=CARRIED_REPORT))
    assert "example.com/churn" in res.text
    assert "other.example.org" in res.text


# ═════════════════════════════════════════════════════════════════════════
# wiring
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,expected", [
    ("score my leads", TaskType.LEAD_SCORE),
    ("prioritize outreach for this week", TaskType.LEAD_SCORE),
    ("self evaluation of this week", TaskType.SELF_EVAL),
    ("how am I doing against my goals", TaskType.SELF_EVAL),
    ("marketing strategy for our saas", TaskType.MARKETING),
    ("competitor analysis for the board deck", TaskType.MARKETING),
    ("hi there", TaskType._FALLBACK),
])
def test_classifier_lanes(text, expected):
    assert IntentClassifier().classify(text).task_type is expected


@pytest.mark.parametrize("text,expected", [
    ("route this to lead agent", TaskType.LEAD_SCORE),
    ("[marketing] build the campaign", TaskType.MARKETING),
    ("self eval agent please", TaskType.SELF_EVAL),
])
def test_explicit_routes(text, expected):
    assert IntentClassifier().classify(text).task_type is expected


def test_registry_gates(monkeypatch):
    # off by default: stubs
    for var in ("JARVIS_SELF_EVAL_AGENT", "JARVIS_LEAD_AGENT",
                "JARVIS_MARKETING_AGENT"):
        monkeypatch.delenv(var, raising=False)
    reg = build_default_registry()
    assert reg.get("lead_agent").__class__.__name__ == "StubAgent"
    assert reg.get("self_eval_agent").__class__.__name__ == "StubAgent"
    assert reg.get("marketing_agent").__class__.__name__ == "StubAgent"
    # on: the real agents
    for var in ("JARVIS_SELF_EVAL_AGENT", "JARVIS_LEAD_AGENT",
                "JARVIS_MARKETING_AGENT"):
        monkeypatch.setenv(var, "on")
    reg2 = build_default_registry()
    assert reg2.get("lead_agent").__class__.__name__ == "LeadAgent"
    assert reg2.get("self_eval_agent").__class__.__name__ == "SelfEvalAgent"
    assert reg2.get("marketing_agent").__class__.__name__ == "MarketingAgent"


def test_safety_levels():
    assert safety.is_blocked(safety.BLOCKED)
    assert safety.needs_approval(safety.APPROVAL_REQUIRED)
    assert not safety.needs_approval(safety.READ_ONLY)
