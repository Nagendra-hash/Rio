"""
tests/test_health_agent.py — offline acceptance tests for the Phase 11
Health Assistant.

NO network, NO LLM, NO real health files touched: JARVIS_HOME points at a
fresh temp dir for this module, and stores/checkpoints are usually built on
explicit temp directories.

What this locks in:
  1. SAFETY     — emergency triage short-circuits everything (nothing is
                  stored); diagnosis/prescribing/treatment asks are refused
                  with alternatives; the disclaimer is on every reply;
                  urgent user-entered values raise care directives;
                  systems-of-medicine info stays general and neutral.
  2. PARSER     — lab rows (ranges, flags, glossary, identity dropped,
                  prose never mis-parsed), measurement kinds, medication
                  lines, symptom organization.
  3. STORE      — the health-data boundary: 0o600 files in a 0o700 dir,
                  actions-only audit log, path containment, caps, wipe.
  4. CHECKPOINT — crash-safe snapshots, pruning, perms, never raises.
  5. AGENT      — full flows: emergency/refusal, track/monitor/history,
                  report explain (text + attachment), meds, questions,
                  systems, profile, summary, wipe confirmation, menu,
                  never raises on garbage, checkpoints after mutations.
  6. BOUNDARY   — after a full session with the default store, no health
                  token appears in ANY file outside memory/health/, and
                  general-memory files are never created.
  7. WIRING     — classifier HEALTH lane + explicit routes, other lanes
                  unchanged, registry stub off / real on behind
                  JARVIS_HEALTH_AGENT=on.

Run:  .venv/bin/python -m pytest tests/test_health_agent.py -q
"""
import itertools
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── isolation: this module gets a fresh JARVIS_HOME ──────────────────────────
_TMP = tempfile.mkdtemp(prefix="jarvis-health-")
os.environ["JARVIS_HOME"] = _TMP

from orchestrator.agents.health import parser, safety          # noqa: E402
from orchestrator.agents.health.checkpoint import (            # noqa: E402
    HealthCheckpoint)
from orchestrator.agents.health.store import HealthStore       # noqa: E402
from orchestrator.agents.health.health_agent import HealthAgent  # noqa: E402
from orchestrator.classifier import IntentClassifier           # noqa: E402
from orchestrator.contracts import (                           # noqa: E402
    AgentRequest, TaskStatus, TaskType)
from orchestrator.agents import build_default_registry         # noqa: E402

_SEQ = itertools.count(1)


def _req(text: str, attachments=None, **meta) -> AgentRequest:
    return AgentRequest(task_id=f"health-test-{next(_SEQ)}",
                        task_type=TaskType.HEALTH, text=text,
                        attachments=list(attachments or []), metadata=meta)


@pytest.fixture()
def tmp():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture()
def agent(tmp):
    return HealthAgent(store=HealthStore(tmp / "health"),
                       checkpoint=HealthCheckpoint(tmp / "ckpt"))


# ── 1. safety ────────────────────────────────────────────────────────────────

class TestSafety:
    def test_emergency_chest_pain(self):
        t = safety.triage("I have crushing chest pain going to my left arm")
        assert t["emergency"]
        assert any("cardiac" in l for l in t["labels"])
        assert "emergency number" in t["directive"]

    def test_emergency_stroke_signs(self):
        assert safety.triage("his face is drooping and speech is slurred")[
            "emergency"]

    def test_emergency_breathing_and_bleeding(self):
        assert safety.triage("she can't breathe at all")["emergency"]
        assert safety.triage("the bleeding won't stop")["emergency"]

    def test_crisis_path_gets_helpline_not_hospital_only(self):
        t = safety.triage("sometimes i think about killing myself")
        assert t["emergency"]
        assert "helpline" in t["directive"].lower()

    def test_benign_asks_are_not_emergencies(self):
        assert not safety.triage("my knee aches after running")["emergency"]
        assert not safety.triage("sugar 140 mg/dL this morning")["emergency"]
        assert not safety.triage("summarize my medicines")["emergency"]

    def test_risk_classification(self):
        assert safety.classify_risk("diagnose my rash") == "diagnose"
        assert safety.classify_risk("what's wrong with me") == "diagnose"
        assert safety.classify_risk("do i have a serious condition") == "diagnose"
        assert safety.classify_risk(
            "what medicine should i take for fever") == "prescribe"
        assert safety.classify_risk("increase my dose of metformin") == "prescribe"
        assert safety.classify_risk("best remedy for my cough") == "treat"
        assert safety.classify_risk("summarize my medications list") == ""
        assert safety.classify_risk("track bp 130/85") == ""

    def test_refusal_texts_offer_alternatives(self):
        assert "can't diagnose" in safety.refusal("diagnose")
        assert "can't prescribe" in safety.refusal("prescribe")
        assert "questions" in safety.refusal("diagnose").lower()

    def test_value_flags_bp(self):
        assert "urgent" in safety.value_flags(
            "bp", {"systolic": 190, "diastolic": 125}).lower()
        assert "seek medical care" in safety.value_flags(
            "bp", {"systolic": 85, "diastolic": 48}).lower()
        assert safety.value_flags("bp", {"systolic": 124, "diastolic": 82}) == ""

    def test_value_flags_other_kinds(self):
        assert safety.value_flags("spo2", {"value": 88})
        assert safety.value_flags("spo2", {"value": 98}) == ""
        assert safety.value_flags("glucose", {"mgdl": 420})
        assert safety.value_flags("glucose", {"mgdl": 40})
        assert safety.value_flags("glucose", {"mmol": 22.0})
        assert safety.value_flags("temp", {"f": 104.6})
        assert safety.value_flags("temp", {"c": 40.2})
        assert safety.value_flags("pulse", {"value": 165})
        assert safety.value_flags("pulse", {"value": 72}) == ""
        assert safety.value_flags("weight", {"value": 70}) == ""
        assert safety.value_flags("bp", {"systolic": "garbage"}) == ""

    def test_systems_info_stays_general(self):
        for topic in ("allopathy", "ayurveda", "homeopathy"):
            info = safety.systems_info(topic)
            assert len(info) > 80
            assert "qualified practitioner" in info
        assert safety.systems_info("reiki") == ""

    def test_systems_topic_detection(self):
        assert safety.systems_topic("tell me about Ayurveda") == "ayurveda"
        assert safety.systems_topic("what is homeopathy?") == "homeopathy"
        assert safety.systems_topic("allopathic medicine") == "allopathy"
        assert safety.systems_topic("what is quantum physics") == ""

    def test_disclaimer_constant(self):
        assert "not a doctor" in safety.DISCLAIMER
        assert "consult" in safety.DISCLAIMER.lower()


# ── 2. parsers ───────────────────────────────────────────────────────────────

class TestParser:
    REPORT = ("Patient: Ramesh Kumar\n"
              "MRN: 88421\n"
              "Hemoglobin: 10.5 g/dL (13.0 - 17.0)\n"
              "Blood Sugar (Fasting): 180 mg/dL [70 - 110]\n"
              "Platelets 250 x10^9/L\n"
              "Room 12")

    def test_lab_rows_ranges_and_flags(self):
        out = parser.parse_lab_report(self.REPORT)
        tests = {t["name"]: t for t in out["tests"]}
        hb = tests["Hemoglobin"]
        assert hb["value"] == 10.5 and hb["unit"] == "g/dL"
        assert hb["low"] == 13.0 and hb["high"] == 17.0
        assert hb["flag"] == "LOW"
        assert tests["Blood Sugar (Fasting)"]["flag"] == "HIGH"
        assert tests["Platelets"]["flag"] == "CHECK"

    def test_identity_lines_dropped(self):
        out = parser.parse_lab_report(self.REPORT)
        assert out["identity_removed"] == 2
        blob = json.dumps(out)
        assert "Ramesh" not in blob and "88421" not in blob

    def test_prose_is_never_a_test_row(self):
        assert parser.parse_lab_report(self.REPORT)["unparsed"] == ["Room 12"]

    def test_glossary_word_boundaries(self):
        out = parser.parse_lab_report(
            "ALT: 80 U/L (5 - 40)\nAST: 90 U/L (5 - 40)")
        g = {t["name"]: t["glossary"] for t in out["tests"]}
        assert "liver enzyme" in g["ALT"]
        assert "liver enzyme" in g["AST"]

    def test_glossary_sugar_not_liver_enzyme(self):
        out = parser.parse_lab_report(
            "Blood Sugar (Fasting): 180 mg/dL [70 - 110]")
        assert out["tests"][0]["glossary"] == "blood sugar"

    def test_measurements_all_kinds(self):
        ms = parser.parse_measurements(
            "bp 138/86 and weight 72.5 kg, sugar 180 mg/dL, bsl 8.2 mmol/l, "
            "temp 101.2 F, temp 38.5 c, spo2 96%, pulse 88")
        kinds = [m["kind"] for m in ms]
        assert kinds.count("bp") == 1 and kinds.count("weight") == 1
        bp = ms[0]
        assert bp["value"] == {"systolic": 138, "diastolic": 86}
        glucose = [m for m in ms if m["kind"] == "glucose"]
        assert glucose[0]["value"]["mgdl"] == 180.0
        assert glucose[1]["value"]["mmol"] == 8.2
        assert glucose[1]["value"]["mgdl"] == pytest.approx(147.6)
        temps = [m for m in ms if m["kind"] == "temp"]
        assert temps[0]["unit"] == "°F" and temps[1]["unit"] == "°C"
        assert ms[-1]["kind"] == "pulse" and ms[-1]["value"]["value"] == 88

    def test_plain_prose_yields_no_measurements(self):
        assert parser.parse_measurements("hello how are you today") == []

    def test_medication_lines(self):
        out = parser.parse_medications(
            "Metformin 500mg twice daily after food\n"
            "Amlodipine 5 mg once a day at night\n"
            "Vitamin D3 60000 IU once weekly\n"
            "Crocin 500 mg 1-0-1\n"
            "Take rest and drink water")
        meds = out["meds"]
        assert meds[0] == {"name": "Metformin", "dose": "500mg",
                           "frequency": "twice daily"}
        assert meds[1]["dose"] == "5 mg"
        assert meds[1]["frequency"] == "once a day"
        assert meds[2]["dose"] == "60000 IU"
        assert meds[2]["frequency"] == "once weekly"
        assert meds[3]["frequency"] == "1-0-1"
        assert out["unparsed"] == ["Take rest and drink water"]

    def test_symptoms_organized_and_scrubbed(self):
        s = parser.parse_symptoms(
            "fever and headache since 3 days, mild; call 9876543210")
        assert s["items"] == ["fever", "headache"]
        assert s["onset"] == "since 3 days"
        assert s["severity"] == "mild"
        assert "9876543210" not in s["text"]

    def test_symptom_scrub_emails_and_ids(self):
        s = parser.parse_symptoms(
            "rash since yesterday. mail me at a@b.com MRN: 12345")
        assert "@b.com" not in s["text"]
        assert "12345" not in s["text"]

# ── 3. store: the health-data boundary ───────────────────────────────────

class TestStore:
    def test_permissions_owner_only(self, tmp):
        store = HealthStore(tmp / "h")
        store.add_symptom(["fever"], "fever since 2 days")
        store.save_profile({"age": "34"})
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        for f in store.root.iterdir():
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f.name

    def test_roundtrips(self, tmp):
        store = HealthStore(tmp / "h")
        e = store.add_symptom(["fever", "cough"], "fever 100F",
                              onset="2 days", severity="mild")
        assert e["items"] == ["fever", "cough"]
        assert store.symptoms()[0]["text"] == "fever 100F"
        store.add_measurement("bp", {"systolic": 130, "diastolic": 85}, "mmHg")
        store.add_measurement("spo2", {"value": 97}, "%")
        assert store.measurements(kind="bp")[0]["value"]["systolic"] == 130
        assert store.summary()["measurements"] == 2
        store.set_medications([{ "name": "Metformin", "dose": "500mg",
                                 "frequency": "twice daily"}])
        store.add_medications([
            {"name": "Metformin", "dose": "500mg", "frequency": "twice daily"},
            {"name": "Amlodipine", "dose": "5mg", "frequency": "od"}])
        assert len(store.get_medications()) == 2      # deduped by name
        assert store.remove_medication("metformin")
        assert len(store.get_medications()) == 1

    def test_audit_records_actions_only(self, tmp):
        store = HealthStore(tmp / "h")
        marker = "super-secret-symptom-xyzzy"
        store.add_symptom(["fever"], f"very {marker} high fever")
        store.add_measurement("bp", {"systolic": 200, "diastolic": 130}, "mmHg")
        audit = (tmp / "h" / "audit.jsonl").read_text()
        assert "symptom_added" in audit and "measurement_added" in audit
        assert marker not in audit
        assert "200" not in audit and "130" not in audit

    def test_path_containment(self, tmp):
        store = HealthStore(tmp / "h")
        assert store._safe_name("../../etc/passwd") == "etcpasswd"
        assert not (tmp / "etc").exists()
        assert store._safe_name("") == "unnamed"
        assert store._safe_name("../..") in ("", "unnamed")  # never traversal

    def test_wipe_erases_data_keeps_audit(self, tmp):
        store = HealthStore(tmp / "h")
        store.add_symptom(["fever"], "fever")
        store.save_profile({"age": "40"})
        assert store.summary()["symptoms"] == 1
        assert store.wipe()
        assert store.summary()["symptoms"] == 0
        assert store.get_profile() == {}
        assert "wipe" in (tmp / "h" / "audit.jsonl").read_text()

    def test_entry_caps(self, tmp):
        store = HealthStore(tmp / "h")
        e = store.add_symptom(["fever"], "x" * 10_000)
        assert len(e["text"]) <= 500


# ── 4. checkpoint ──────────────────────────────────────────────────────────

class TestCheckpoint:
    def test_save_load_prune_and_perms(self, tmp):
        cp = HealthCheckpoint(tmp / "ck")
        for i in range(25):
            assert cp.save({"i": i})
        assert len(cp.list_ids()) == 20
        assert cp.load_latest()["i"] == 24
        assert stat.S_IMODE(
            (tmp / "ck" / "latest.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp / "ck").stat().st_mode) == 0o700

    def test_never_raises_on_broken_dir(self, tmp):
        cp = HealthCheckpoint(tmp / "ck")
        blocked = tmp / "blocked"
        blocked.write_text("not a dir")
        cp.dir = blocked
        assert cp.save({"x": 1}) is None
        assert cp.load_latest() is None
        assert cp.list_ids() == []


# ── 5. agent flows ───────────────────────────────────────────────────────

class TestHealthAgent:
    def test_emergency_first_and_nothing_stored(self, agent):
        r = agent.handle(_req(
            "I have crushing chest pain and left arm pressure"))
        assert r.status is TaskStatus.COMPLETED
        assert r.data["emergency"] is True
        assert "emergency number" in r.text
        assert agent.store.summary()["symptoms"] == 0
        assert agent.store.summary()["measurements"] == 0

    def test_diagnosis_refused_with_alternatives(self, agent):
        r = agent.handle(_req("what disease do i have?"))
        assert r.status is TaskStatus.COMPLETED
        assert r.data.get("refused") == "diagnose"
        assert "questions" in r.text.lower()
        assert "not a doctor" in r.text.lower()

    def test_prescription_refused(self, agent):
        r = agent.handle(_req("what medicine should i take for my fever?"))
        assert r.data.get("refused") == "prescribe"

    def test_disclaimer_on_every_completed_reply(self, agent):
        for text in ("fever since 2 days", "bp 130/85",
                     "tell me about ayurveda", "Metformin 500mg twice daily",
                     "Hemoglobin: 10.5 g/dL (13.0-17.0)"):
            r = agent.handle(_req(text))
            assert r.status is TaskStatus.COMPLETED, text
            assert "not a doctor" in r.text.lower(), text

    def test_track_and_urgent_flag(self, agent):
        r = agent.handle(_req("track bp 190/125"))
        assert "Recorded 1" in r.text
        assert "urgent" in r.text.lower()
        assert agent.store.measurements(kind="bp")

    def test_report_flow_explains_and_flags(self, agent):
        r = agent.handle(_req(
            "explain my report:\nHemoglobin: 10.5 g/dL (13.0-17.0)\n"
            "TSH: 6.5 mIU/L (0.4 - 4.0)"))
        assert "LOW" in r.text and "HIGH" in r.text
        assert "not a diagnosis" in r.text.lower()
        assert agent.store.summary()["reports"] == 1

    def test_report_from_text_attachment(self, agent, tmp):
        f = tmp / "report.txt"
        f.write_text("Vitamin D: 12 ng/mL (30 - 100)\n")
        r = agent.handle(_req("explain this report", attachments=[str(f)]))
        assert "LOW" in r.text and "Vitamin D" in r.text

    def test_unsupported_attachment_is_honest(self, agent, tmp):
        f = tmp / "scan.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        r = agent.handle(_req("explain my report", attachments=[str(f)]))
        assert "couldn't find any test rows" in r.text

    def test_meds_flow(self, agent):
        agent.handle(_req("Metformin 500mg twice daily after food"))
        r = agent.handle(_req("summarize my medicines"))
        assert "Metformin" in r.text and "500mg" in r.text
        assert "don't check interactions" in r.text

    def test_questions_generated_from_data(self, agent):
        agent.handle(_req("fever since 3 days"))
        agent.handle(_req("Hemoglobin: 10.5 g/dL (13.0-17.0)"))
        r = agent.handle(_req("prepare questions for my doctor"))
        assert "Questions to bring" in r.text
        assert "fever" in r.text and "Hemoglobin" in r.text
        r2 = agent.handle(_req("questions for my clinician"))
        assert r2.text.count("fever") == r.text.count("fever")  # no dupes

    def test_add_question_explicit(self, agent):
        r = agent.handle(_req("add question: can I fast while on this medication?"))
        assert "can I fast" in r.text
        assert any("can I fast" in q["text"]
                   for q in agent.store.questions())

    def test_systems_info(self, agent):
        r = agent.handle(_req("tell me about homeopathy"))
        assert "Homeopathy" in r.text
        r2 = agent.handle(_req("what is ayurveda"))
        assert "doshas" in r2.text

    def test_history_and_monitor(self, agent):
        agent.handle(_req("bp 130/85"))
        agent.handle(_req("bp 128/82"))
        h = agent.handle(_req("show my history"))
        assert "2 entries" in h.text and "avg" in h.text
        m = agent.handle(_req("monitor my health data"))
        assert "latest" in m.text.lower()

    def test_profile_flow(self, agent):
        agent.handle(_req("age: 34, blood group: O+, allergies: penicillin"))
        r = agent.handle(_req("show my profile"))
        assert "34" in r.text and "O+" in r.text and "penicillin" in r.text

    def test_summary_and_wipe_confirmation(self, agent):
        agent.handle(_req("bp 130/85"))
        r = agent.handle(_req("health summary"))
        assert "measurement(s)" in r.text
        w = agent.handle(_req("wipe my health data"))
        assert w.status is TaskStatus.NEEDS_INPUT
        assert agent.store.summary()["measurements"] == 1
        w2 = agent.handle(_req("wipe my health data", confirm_wipe=True))
        assert "erased" in w2.text.lower()
        assert agent.store.summary()["measurements"] == 0

    def test_menu_on_unknown_ask(self, agent):
        r = agent.handle(_req("what's the weather like"))
        assert r.status is TaskStatus.NEEDS_INPUT
        assert "I can help with" in r.text

    def test_never_raises_on_garbage(self, agent):
        for text in ("", "   ", "\n\n\n", "999999", "!!!", "x" * 5000,
                     "bp abc/def", "—–—"):
            r = agent.handle(_req(text))
            assert r is not None

    def test_checkpoint_after_mutation(self, agent):
        agent.handle(_req("bp 130/85"))
        latest = agent.checkpoint.load_latest()
        assert latest and latest["summary"]["measurements"] == 1

# ── 6. boundary: nothing outside memory/health/ ever holds health data ──

class TestBoundary:
    def test_no_health_data_outside_the_domain(self):
        home = Path(tempfile.mkdtemp(prefix="jarvis-boundary-"))
        old = os.environ.get("JARVIS_HOME")
        os.environ["JARVIS_HOME"] = str(home)
        try:
            agent = HealthAgent()          # default store under JARVIS_HOME
            for text in ("fever and cough since 4 days, severe",
                         "bp 150/95 and sugar 210 mg/dL",
                         "Metformin 1000mg after food",
                         "Hemoglobin: 9.8 g/dL (13.0-17.0)"):
                agent.handle(_req(text))

            tokens = ["fever and cough", "150/95", "210", "metformin",
                      "hemoglobin", "9.8"]
            health_root = home / "memory" / "health"
            assert health_root.is_dir()
            leaked = []
            for p in home.rglob("*"):
                if not p.is_file() or health_root in p.parents:
                    continue
                try:
                    low = p.read_text(encoding="utf-8",
                                      errors="ignore").lower()
                except Exception:
                    continue
                for tok in tokens:
                    if tok.lower() in low:
                        leaked.append((str(p.relative_to(home)), tok))
            assert leaked == []

            # general-memory files are never created by the health agent
            assert not (home / "memory" / "long_term.json").exists()
            assert not (home / "memory" / "orchestration" /
                        "journal.jsonl").exists()
            assert not (home / "memory" / "conversations").exists()

            # every health file is owner-only
            for p in health_root.rglob("*"):
                mode = stat.S_IMODE(p.stat().st_mode)
                assert mode == (0o700 if p.is_dir() else 0o600), p.name
        finally:
            if old is None:
                os.environ.pop("JARVIS_HOME", None)
            else:
                os.environ["JARVIS_HOME"] = old
            shutil.rmtree(home, ignore_errors=True)


# ── 7. wiring: classifier lane + registry ─────────────────────────────

class TestWiring:
    def test_classifier_health_lane(self):
        c = IntentClassifier()
        for text in ("I have fever and headache since 2 days",
                     "summarize my medications",
                     "my lab report shows high sugar",
                     "what is homeopathy",
                     "track my blood pressure"):
            got = c.classify(text)
            assert got.task_type is TaskType.HEALTH, (text, got.reason)

    def test_explicit_routes(self):
        c = IntentClassifier()
        assert c.classify("route this to health").task_type is TaskType.HEALTH
        assert c.classify("[health] organize my symptoms") \
            .task_type is TaskType.HEALTH
        assert c.classify("health agent please help") \
            .task_type is TaskType.HEALTH

    def test_other_lanes_unchanged(self):
        c = IntentClassifier()
        assert c.classify("fix the bug in my python function") \
            .task_type is TaskType.CODING
        assert c.classify("analyze RELIANCE chart setup") \
            .task_type is TaskType.TRADING
        assert c.classify("what's the latest news about EVs") \
            .task_type is TaskType.RESEARCH
        assert c.classify("hi").task_type is TaskType._FALLBACK

    def test_registry_off_by_default(self):
        os.environ.pop("JARVIS_HEALTH_AGENT", None)
        reg = build_default_registry()
        stub = reg.get("health_agent")
        assert type(stub).__name__ == "StubAgent"
        r = stub.handle(_req("fever"))
        assert r.status is TaskStatus.NEEDS_INPUT

    def test_registry_on(self):
        os.environ["JARVIS_HEALTH_AGENT"] = "on"
        try:
            reg = build_default_registry()
            assert type(reg.get("health_agent")).__name__ == "HealthAgent"
        finally:
            os.environ.pop("JARVIS_HEALTH_AGENT", None)
