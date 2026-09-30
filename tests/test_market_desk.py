"""
tests/test_market_desk.py — acceptance tests for the Market Intelligence Desk.

Offline acceptance for the desk's structural rules (the guide's checklists):
  roster invariants · handoff contract fields · §10 duplicate/delivery
  controls · §11 paused-by-default scheduling · timezone & due logic.
LLM calls are mocked — no network, no quota burn. (Guide §13: acceptance
testing must not consume production slots.)
"""
import json
import sys
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IST = ZoneInfo("Asia/Kolkata")
from plugins import _desk_engine as eng                     # noqa: E402
from plugins import _desk_roster as roster                  # noqa: E402
from plugins._desk_engine import HandoffStore               # noqa: E402


class RosterTests(unittest.TestCase):
    """§18 checklist: organization invariants."""

    def test_roster_shape(self):
        self.assertEqual(len(roster.ROSTER), 23)
        self.assertEqual(len(roster.all_leads()), 5)
        self.assertEqual(len(roster.all_specialists()), 17)

    def test_every_specialist_has_exactly_one_lead(self):
        for role in roster.all_specialists():
            lead = roster.lead_of(role)
            self.assertIn(lead, roster.all_leads(), role)

    def test_every_lead_reports_to_chief(self):
        for lead in roster.all_leads():
            self.assertEqual(roster.lead_of(lead), roster.CHIEF)

    def test_profile_prompt_carries_boundaries(self):
        p = roster.profile_prompt("Sentiment Analyst")
        for frag in ("PROHIBITED", "SAFETY", "never place trades",
                     "EVIDENCE RULES", "REPORTS TO"):
            self.assertIn(frag, p)

    def test_universe_resolution_rejects_outsiders(self):
        uni = roster.load_universe()
        self.assertIsNotNone(roster.resolve_company("TCS", uni))
        self.assertIsNotNone(roster.resolve_company("BTC", uni))
        self.assertIsNone(roster.resolve_company("PivotBin Industries", uni))


class FakeStore(HandoffStore):
    """In-memory store — same contract, no filesystem."""

    def __init__(self):
        self.records = []

    def append(self, record):
        self.records.append(record)

    def read_all(self, limit=400):
        return list(self.records)[-limit:]


def _fake_llm(specialist_json: dict, lead_json: dict | None = None,
              chief_json: dict | None = None):
    """Patch _ask_llm by record type."""
    def fake(role, task, timeout_ms=90_000):
        if role == roster.CHIEF:
            return json.dumps(chief_json or {
                "answer": "consolidated answer",
                "departments_consulted": ["market_news"],
                "disagreements": [], "as_of_notes": ["EOD 2026-09-21"],
                "coverage_gaps": [], "escalated_to_review": False,
            })
        if "consolidated_report" in task and "STRICT JSON" in task:
            return json.dumps(lead_json or {
                "accepted": [], "needs_correction": [], "incomplete": [],
                "dedup_note": "", "conflicts": [], "consolidated_report": "lead says",
                "coverage_gaps": ["no filings indexed"],
                "escalate_to_independent_review": False,
            })
        return json.dumps(specialist_json)
    return fake


GOOD_SPEC = {
    "sources": [{"title": "Filing", "url": "https://nseindia.com/x",
                 "publication_time": "2026-09-21T10:00:00+05:30",
                 "retrieval_time": "2026-09-21T12:00:00+05:30",
                 "data_as_of": "2026-09-20"}],
    "verified_facts": ["company filed X on 2026-09-21"],
    "interpretation": "routine disclosure",
    "contradictions": [],
    "unknowns": [],
    "coverage_status": "completed_with_material_findings",
    "confidence": "high",
    "recommended_lead_action": "accept",
}


class HandoffTests(unittest.TestCase):
    """§9 contract + §10 duplicate & delivery controls."""

    def setUp(self):
        self.store = FakeStore()

    def test_handoff_contract_fields(self):
        with mock.patch.object(eng, "_ask_llm",
                               _fake_llm(dict(GOOD_SPEC))):
            h = eng.run_specialist("Research Analyst",
                                   roster.load_universe()["companies"][0],
                                   window="day", store=self.store,
                                   run_id="r1", request_id="req1")
        for field in ("handoff_id", "run_id", "event_id", "reporting_period",
                      "bot", "company", "created_at", "sources",
                      "verified_facts", "interpretation", "contradictions",
                      "unknowns", "coverage_status", "confidence",
                      "recommended_lead_action", "delivery_status"):
            self.assertIn(field, h)
        self.assertEqual(h["delivery_status"], "delivered")
        self.assertEqual(h["coverage_status"], "completed_with_material_findings")

    def test_duplicate_suppression_after_delivery(self):
        comp = roster.load_universe()["companies"][0]
        with mock.patch.object(eng, "_ask_llm", _fake_llm(dict(GOOD_SPEC))):
            first = eng.run_specialist("Research Analyst", comp, window="day",
                                       store=self.store, run_id="r1",
                                       request_id="req1")
            dup = eng.run_specialist("Research Analyst", comp, window="day",
                                     store=self.store, run_id="r2",
                                     request_id="req1")
        self.assertFalse(first.get("duplicate"))
        self.assertTrue(dup.get("duplicate"))
        self.assertEqual(len(self.store.records), 1)

    def test_failed_llm_is_retryable_not_invented(self):
        with mock.patch.object(eng, "_ask_llm", return_value=None):
            h = eng.run_specialist("Research Analyst",
                                   roster.load_universe()["companies"][0],
                                   window="day", store=self.store,
                                   run_id="r1", request_id="req9")
        self.assertEqual(h["coverage_status"], "failed_but_retryable")
        self.assertIn("unknowns", h)
        self.assertEqual(h["delivery_status"], "delivered")   # record kept

    def test_garbage_json_becomes_retryable(self):
        with mock.patch.object(eng, "_ask_llm", return_value="not json at all {"):
            h = eng.run_specialist("Sentiment Analyst",
                                   roster.load_universe()["companies"][1],
                                   window="day", store=self.store,
                                   run_id="r1", request_id="reqX")
        self.assertEqual(h["coverage_status"], "failed_but_retryable")

    def test_unknown_coverage_downgraded_to_incomplete(self):
        spec = dict(GOOD_SPEC, coverage_status="i_made_this_up")
        with mock.patch.object(eng, "_ask_llm", _fake_llm(spec)):
            h = eng.run_specialist("Sentiment Analyst",
                                   roster.load_universe()["companies"][1],
                                   window="day", store=self.store,
                                   run_id="r1", request_id="reqY")
        self.assertEqual(h["coverage_status"], "incomplete_coverage")

    def test_lead_acceptance_recorded_on_handoff(self):
        comp = roster.load_universe()["companies"][0]
        with mock.patch.object(eng, "_ask_llm", _fake_llm(
                dict(GOOD_SPEC),
                lead_json={"accepted": ["Research Analyst"],
                           "needs_correction": [], "incomplete": [],
                           "dedup_note": "", "conflicts": [],
                           "consolidated_report": "ok",
                           "coverage_gaps": [],
                           "escalate_to_independent_review": False})):
            h = eng.run_specialist("Research Analyst", comp, window="day",
                                   store=self.store, run_id="r1",
                                   request_id="reqA")
            lr = eng.run_lead("Market News Research Lead", [h],
                              store=self.store, run_id="r2",
                              request_id="reqA")
        self.assertEqual(lr["record_type"], "lead_report")
        self.assertIn("Market News Research Lead", lr["bot"])
        reviews = [r for r in self.store.records
                   if r.get("record_type") == "handoff_review"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["lead_review"]["status"], "accepted")


class HermesBridgeTests(unittest.TestCase):
    """Desk → Hermes research fallback: provenance-tagged, contract-identical,
    and switchable — the Lead must always see which channel produced work."""

    def setUp(self):
        self.store = FakeStore()

    def test_fallback_tags_channel_and_delivers(self):
        hermes_reply = ('Some prose the agent wrote.\nDESK_JSON: '
                        + json.dumps(GOOD_SPEC))
        with mock.patch.object(eng, "_gemini_call", return_value=None), \
             mock.patch.object(eng, "_hermes_research",
                               return_value=f"(research channel: hermes)\n{hermes_reply}"), \
             mock.patch.object(eng, "_setting_val", return_value=True):
            h = eng.run_specialist("Research Analyst",
                                   roster.load_universe()["companies"][1],
                                   window="day", store=self.store,
                                   run_id="r1", request_id="reqH1")
        self.assertEqual(h.get("research_channel"), "hermes")
        self.assertEqual(h["delivery_status"], "delivered")
        self.assertEqual(h["coverage_status"], "completed_with_material_findings")

    def test_fallback_respects_kill_switch(self):
        with mock.patch.object(eng, "_gemini_call", return_value=None), \
             mock.patch.object(eng, "_hermes_research") as hr, \
             mock.patch.object(eng, "_setting_val", return_value=False):
            h = eng.run_specialist("Research Analyst",
                                   roster.load_universe()["companies"][1],
                                   window="day", store=self.store,
                                   run_id="r1", request_id="reqH2")
        hr.assert_not_called()
        self.assertEqual(h["coverage_status"], "failed_but_retryable")
        self.assertNotIn("research_channel", h)

    def test_gemini_primary_does_not_touch_hermes(self):
        with mock.patch.object(eng, "_gemini_call",
                               return_value=json.dumps(GOOD_SPEC)), \
             mock.patch.object(eng, "_hermes_research") as hr, \
             mock.patch.object(eng, "_setting_val", return_value=True):
            h = eng.run_specialist("Research Analyst",
                                   roster.load_universe()["companies"][0],
                                   window="day", store=self.store,
                                   run_id="r1", request_id="reqH3")
        hr.assert_not_called()
        self.assertEqual(h.get("research_channel"), "gemini")


class ScheduleTests(unittest.TestCase):
    """§11/§12: paused by default, timezone correct, cursors idempotent."""

    def setUp(self):
        self.state = {"routines": {}, "cursors": {}, "budget": 99}

    def test_nothing_runs_while_all_paused(self):
        monday_0810 = datetime(2026, 9, 21, 8, 10, tzinfo=IST)
        with mock.patch.object(eng, "_load_state", return_value=dict(self.state)), \
             mock.patch.object(eng, "routine_active", return_value=False):
            self.assertEqual(eng.due_routines(monday_0810, dict(self.state)), [])

    def test_due_only_after_scheduled_time_weekday_gated(self):
        self.state["routines"] = {"news_primary": {"active": True}}
        monday_1159 = datetime(2026, 9, 21, 11, 59, tzinfo=IST)
        monday_1201 = datetime(2026, 9, 21, 12, 1, tzinfo=IST)
        saturday_1201 = datetime(2026, 9, 26, 12, 1, tzinfo=IST)
        with mock.patch.object(eng, "_load_state", return_value=dict(self.state)):
            self.assertEqual(eng.due_routines(monday_1159, dict(self.state)), [])
            due = eng.due_routines(monday_1201, dict(self.state))
            self.assertEqual([r["id"] for r in due], ["news_primary"])
            self.assertEqual(eng.due_routines(saturday_1201, dict(self.state)), [])

    def test_first_monday_gate(self):
        self.state["routines"] = {"fund_holdings": {"active": True}}
        first_monday = datetime(2026, 9, 7, 18, 30, tzinfo=IST)   # day ≤7
        second_monday = datetime(2026, 9, 14, 18, 30, tzinfo=IST)
        with mock.patch.object(eng, "_load_state", return_value=dict(self.state)):
            self.assertEqual([r["id"] for r in
                              eng.due_routines(first_monday, dict(self.state))],
                             ["fund_holdings"])
            self.assertEqual(eng.due_routines(second_monday, dict(self.state)), [])

    def test_cursor_prevents_double_run_same_day(self):
        self.state["routines"] = {"news_primary": {"active": True}}
        self.state["cursors"] = {"news_primary": "2026-09-21"}
        monday_1201 = datetime(2026, 9, 21, 12, 1, tzinfo=IST)
        with mock.patch.object(eng, "_load_state", return_value=dict(self.state)):
            self.assertEqual(eng.due_routines(monday_1201, dict(self.state)), [])

    def test_run_schedule_persists_cursors(self):
        monday_1201 = datetime(2026, 9, 21, 12, 1, tzinfo=IST)
        saved = {}

        def fake_save(st):
            saved.update(st)

        comp = roster.load_universe()["companies"][0]
        # §10: acceptance tests never touch production records — DESK_DIR is
        # redirected to a throwaway dir so run_schedule's internal
        # HandoffStore(test=False) cannot write the real handoffs.jsonl.
        import tempfile
        prod_file = eng.DESK_DIR / "handoffs.jsonl"
        _prod_lines_before = (prod_file.read_text().count("\n")
                              if prod_file.exists() else 0)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(eng, "DESK_DIR", Path(td)), \
                 mock.patch.object(eng, "STATE_FILE", Path(td) / "state.json"), \
                 mock.patch.object(eng, "_load_state", return_value={
                        "routines": {"news_primary": {"active": True}},
                        "cursors": {}, "budget": 99}), \
                 mock.patch.object(eng, "_save_state", side_effect=fake_save), \
                 mock.patch.object(eng, "_now_ist", return_value=monday_1201), \
                 mock.patch.object(eng, "_ask_llm", _fake_llm(dict(GOOD_SPEC))), \
                 mock.patch.object(eng, "load_universe", return_value={
                        "companies": [comp]}):
                msg = eng.run_schedule()
        self.assertIn("news_primary", msg)
        self.assertEqual(saved["cursors"].get("news_primary"), "2026-09-21")
        # Production store untouched by the test run (§10).
        prod = eng.DESK_DIR / "handoffs.jsonl"
        self.assertEqual(prod.read_text().count("\n"), _prod_lines_before,
                         "unit test leaked records into production handoffs")

    def test_all_routines_defined_for_known_roles(self):
        known = set(roster.ROSTER)
        for r in eng.ROUTINES:
            self.assertIn(r["role"], known)
            self.assertRegex(r["time"], r"^\d{2}:\d{2}$")
            self.assertLess(r["time"], "23:59")


class ThreadSafetyTests(unittest.TestCase):
    def test_fake_store_append_threadsafe_enough(self):
        store = FakeStore()
        def writer(i):
            store.append({"i": i})
        ts = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(store.records), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
