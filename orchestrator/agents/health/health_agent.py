"""
orchestrator/agents/health/health_agent.py — the Phase 11 Health Assistant.

A safety-first BaseAgent on the standard contract (BaseAgent, AgentRequest,
AgentResult — same registry convention as every phase). It MAY:

    organize symptoms and patient-provided info · extract and explain
    medical reports · track measurements and history · summarize
    user-provided medications · prepare clinician questions · give general
    information about allopathy / Ayurveda / homeopathy · monitor
    user-entered data.

It must NOT (enforced in safety.py, in code, before any action):

    present itself as a doctor · diagnose disease · prescribe or adjust
    treatment · substitute for a qualified clinician.

Urgent/dangerous symptoms short-circuit EVERYTHING (including storage) and
return an emergency-care directive. All health data stays inside the
protected memory/health/ domain (see store.py) — never in general Jarvis
memory, and log lines carry action names, never content.

Deterministic and offline by default; no LLM in the loop this phase.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.agents.health import parser, safety
from orchestrator.agents.health.checkpoint import HealthCheckpoint
from orchestrator.agents.health.store import HealthStore
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)

log = logging.getLogger("jarvis.health")

MAX_ATTACHMENT_BYTES = 200_000
TEXT_SUFFIXES = {".txt", ".text", ".md", ".csv"}


class HealthAgent(BaseAgent):
    """The Health Assistant capability (Phase 11)."""

    name = "health_agent"
    description = ("Safety-first health assistant: organizes symptoms, "
                   "explains reports, tracks measurements and medications, "
                   "prepares clinician questions. Never diagnoses or "
                   "prescribes.")
    task_types = (TaskType.HEALTH,)

    def __init__(self, store: HealthStore | None = None,
                 checkpoint: HealthCheckpoint | None = None) -> None:
        self.store = store or HealthStore()
        self.checkpoint = checkpoint or HealthCheckpoint()

    # ── entry point ──────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        text = request.text or ""
        try:
            # 1) EMERGENCY FIRST — before parsing, storage, everything.
            t = safety.triage(text)
            if t["emergency"]:
                self.store.audit("emergency_directive",
                                 labels=len(t["labels"]))
                log.info("health: emergency directive shown (%d flags)",
                         len(t["labels"]))
                return self._reply(request, t["directive"],
                                   action="emergency",
                                   extra={"emergency": True,
                                          "labels": t["labels"]})

            # 2) Diagnosis / prescribing / treatment asks are refused.
            risk = safety.classify_risk(text)
            if risk:
                self.store.audit(f"refused_{risk}")
                return self._reply(request, safety.refusal(risk),
                                   action=f"refused_{risk}",
                                   extra={"refused": risk})

            # 3) Dispatch.
            action = str(request.metadata.get("health_action") or "") \
                .strip().lower() or self._intent(request)
            handler = self._HANDLERS.get(action)
            if handler is None:
                return self._menu(request)
            return handler(self, request)
        except Exception as exc:                      # never raise outward
            log.warning("health agent error: %s", type(exc).__name__)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error="health action failed", agent=self.name)

    # ── intent detection (metadata.health_action overrides) ──────────────
    def _intent(self, request: AgentRequest) -> str:
        text = (request.text or "").lower()
        if safety.systems_topic(text):
            return "systems"
        if request.attachments:
            return "report"
        if parser.parse_measurements(text):
            return "track"
        if re.search(r"\bwipe\b|\bdelete\s+(?:my\s+)?(?:health|medical)"
                     r"\s+data\b", text):
            return "wipe"
        if re.search(r"\badd\s+questions?\b", text):
            return "questions"
        if parser.parse_medications(request.text or "")["meds"]:
            return "meds"
        if parser.parse_lab_report(request.text or "")["tests"]:
            return "report"
        if re.search(r"\b(?:medic\w*|tablet\w*|capsule\w*|dose|dosage|"
                     r"prescription\s+list)\b", text):
            return "meds"
        if re.search(r"\breport\w*\b|\blab\b|\bblood\s+test\b|"
                     r"\btest\s+results?\b", text):
            return "report"
        if re.search(r"\bquestion\w*\b|\bask\s+(?:my\s+)?(?:doctor|"
                     r"clinician)\b", text):
            return "questions"
        if re.search(r"\bhistor\w*\b|\btrend\w*\b", text):
            return "history"
        if re.search(r"\bmonitor\w*\b|\bhow\s+am\s+i\s+doing\b", text):
            return "monitor"
        if re.search(r"\bprofile\b|\bmy\s+(?:age|blood\s+group|allergies|"
                     r"conditions)\b", text):
            return "profile"
        if re.search(r"\bsummary\b|\bwhat\s+have\s+you\s+got\b", text):
            return "summary"
        if parser.parse_symptoms(text)["items"] or \
                re.search(r"\bsymptom\w*\b", text):
            return "symptoms"
        return ""

    _HANDLERS: dict = {}          # filled below

    # ── result helpers ───────────────────────────────────────────────────
    def _reply(self, request: AgentRequest, text: str, *, action: str,
               extra: dict | None = None) -> AgentResult:
        body = (text or "").rstrip() + "\n\n" + safety.DISCLAIMER
        data = {"health": True, "sensitive": True, "action": action}
        data.update(extra or {})
        return AgentResult(task_id=request.task_id,
                           status=TaskStatus.COMPLETED,
                           text=body, data=data, agent=self.name)

    def _menu(self, request: AgentRequest) -> AgentResult:
        text = ("I can help with:\n"
                "  • organizing symptoms you describe,\n"
                "  • explaining a medical report you paste (or attach as "
                "text),\n"
                "  • tracking measurements like 'bp 130/85' or "
                "'weight 72 kg',\n"
                "  • summarizing medicines you tell me about,\n"
                "  • preparing questions for your clinician,\n"
                "  • general information about allopathy, Ayurveda, and "
                "homeopathy.\n"
                "Tell me what you'd like to start with.")
        return AgentResult(task_id=request.task_id,
                           status=TaskStatus.NEEDS_INPUT,
                           text=text + "\n\n" + safety.DISCLAIMER,
                           data={"health": True, "sensitive": True,
                                 "action": "menu"},
                           agent=self.name)

    # ── checkpointing ────────────────────────────────────────────────────
    def _save_checkpoint(self) -> None:
        try:
            self.checkpoint.save(self._state())
        except Exception:
            pass

    def _state(self) -> dict:
        return {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "summary": self.store.summary(),
            "profile": self.store.get_profile(),
            "medications": self.store.get_medications(),
            "symptoms": self.store.symptoms(limit=50),
            "measurements": self.store.measurements(limit=200),
            "reports": self.store.reports(limit=10),
            "questions": self.store.questions(limit=100),
        }

    # ── formatting helpers ───────────────────────────────────────────────
    @staticmethod
    def _fmt_measurement(m: dict) -> str:
        v, k = m.get("value") or {}, m.get("kind", "?")
        if k == "bp":
            return f"BP {v.get('systolic')}/{v.get('diastolic')} mmHg"
        if k == "weight":
            return f"weight {v.get('value'):g} {m.get('unit', 'kg')}"
        if k == "glucose":
            if "mmol" in v:
                return f"glucose {v['mmol']:g} mmol/L"
            return f"glucose {v.get('mgdl', 0):g} mg/dL"
        if k == "temp":
            if "c" in v:
                return f"temperature {v['c']:g} °C"
            return f"temperature {v.get('f', 0):g} °F"
        if k == "spo2":
            return f"SpO2 {v.get('value')}%"
        if k == "pulse":
            return f"pulse {v.get('value')} bpm"
        return f"{k} {v}"

    @staticmethod
    def _num(row: dict) -> float | None:
        v, k = row.get("value") or {}, row.get("kind")
        try:
            if k == "bp":
                return float(v["systolic"])
            if k == "glucose":
                if "mgdl" in v:
                    return float(v["mgdl"])
                return float(v["mmol"]) * 18.0
            if k == "temp":
                return float(v.get("c") or v.get("f") or 0)
            if k in ("spo2", "pulse", "weight"):
                return float(v["value"])
        except (KeyError, TypeError, ValueError):
            return None
        return None    # ── action handlers ──────────────────────────────────────────────
    def _do_symptoms(self, request: AgentRequest) -> AgentResult:
        parsed = parser.parse_symptoms(request.text or "")
        if not parsed["items"] and not parsed["text"]:
            return self._menu(request)
        entry = self.store.add_symptom(parsed["items"], parsed["text"],
                                       parsed["onset"], parsed["severity"])
        lines = ["Here's your symptom note, organized:"]
        if parsed["items"]:
            lines.append("  • symptoms: " + ", ".join(parsed["items"]))
        if parsed["onset"]:
            lines.append("  • onset: " + parsed["onset"])
        if parsed["severity"]:
            lines.append("  • severity: " + parsed["severity"])
        lines.append("  • recorded " + entry["at"][:19].replace("T", " ")
                     + " UTC (private health domain)")
        lines.append("\nWant me to turn this into questions for your "
                     "clinician? Just ask.")
        self._save_checkpoint()
        return self._reply(request, "\n".join(lines), action="symptoms")

    def _do_report(self, request: AgentRequest) -> AgentResult:
        text = request.text or ""
        source = "pasted text"
        for path in request.attachments[:3]:
            try:
                p = Path(str(path))
                if p.suffix.lower() not in TEXT_SUFFIXES:
                    continue          # pdf/images not supported this phase
                if p.stat().st_size > MAX_ATTACHMENT_BYTES:
                    continue
                text += "\n" + p.read_text(encoding="utf-8",
                                            errors="replace")
                source = p.name
            except Exception:
                continue
        parsed = parser.parse_lab_report(text)
        if not parsed["tests"]:
            return self._reply(
                request,
                "I couldn't find any test rows. Paste lines like\n"
                "    Hemoglobin: 10.5 g/dL (13.0-17.0)\n"
                "— one test per line — or attach the report as a .txt/.csv "
                "file, and I'll organize it.",
                action="report")
        self.store.add_report(parsed, source)
        lines = [f"Organized {len(parsed['tests'])} test row(s) from "
                 f"{source} (saved privately):"]
        for t in parsed["tests"][:40]:
            rng = ""
            if t["low"] is not None and t["high"] is not None:
                rng = f" (ref {t['low']:g}–{t['high']:g})"
            lines.append(f"  • {t['name']}: {t['value']:g} {t['unit']}"
                         f"{rng} → {t['flag']}")
        flagged = [t for t in parsed["tests"]
                   if t["flag"] in ("LOW", "HIGH")]
        if flagged:
            lines.append("\nOutside the report's printed reference range "
                         "— a finding to discuss with your clinician, "
                         "not a diagnosis:")
            for t in flagged[:15]:
                direction = "below" if t["flag"] == "LOW" else "above"
                lines.append(f"  • {t['name']}: {t['value']:g} {t['unit']} "
                             f"is {direction} the printed range")
        gloss = [t for t in parsed["tests"] if t["glossary"]]
        if gloss:
            lines.append("\nWhat these tests generally measure "
                         "(background only):")
            for t in gloss[:10]:
                lines.append(f"  • {t['name']}: {t['glossary']}")
        if parsed["identity_removed"]:
            lines.append(f"\nPatient identity field(s) removed before "
                         f"storing: {parsed['identity_removed']}.")
        if parsed["unparsed"]:
            lines.append(f"Couldn't parse {len(parsed['unparsed'])} "
                         "line(s) — send them one at a time if they "
                         "matter.")
        lines.append("\nPlease show the original report to your clinician "
                     "for the actual interpretation.")
        self._save_checkpoint()
        return self._reply(request, "\n".join(lines), action="report")

    def _do_track(self, request: AgentRequest) -> AgentResult:
        found = parser.parse_measurements(request.text or "")
        if not found:
            return self._reply(
                request,
                "I didn't spot a measurement. Examples I understand:\n"
                "  'bp 138/86' · 'weight 72.5 kg' · 'sugar 180 mg/dL' · "
                "'temp 101.2 F' · 'spo2 96%' · 'pulse 88'",
                action="track")
        urgent: list[tuple[str, str]] = []
        for m in found[:20]:
            self.store.add_measurement(m["kind"], m["value"], m["unit"],
                                       str(request.metadata.get("when", "")))
            note = safety.value_flags(m["kind"], m["value"])
            if note:
                urgent.append((self._fmt_measurement(m), note))
        lines = [f"Recorded {min(len(found), 20)} measurement(s):"]
        for m in found[:20]:
            lines.append("  • " + self._fmt_measurement(m))
        if urgent:
            lines.append("\n⚠️ One or more of these need attention:")
            for shown, note in urgent:
                lines.append(f"  • {shown} — {note}.")
        self._save_checkpoint()
        return self._reply(request, "\n".join(lines), action="track")

    def _do_history(self, request: AgentRequest) -> AgentResult:
        rows = self.store.measurements(limit=500)
        if not rows:
            return self._reply(
                request,
                "No measurements recorded yet. Send one like 'bp 130/85' "
                "and I'll start tracking.",
                action="history")
        by_kind: dict[str, list[dict]] = {}
        for r in rows:
            by_kind.setdefault(r["kind"], []).append(r)
        lines = ["Your measurement history (from your own entries):"]
        for kind, entries in by_kind.items():
            nums = [n for n in (self._num(r) for r in entries)
                    if n is not None]
            if not nums:
                continue
            trend = ""
            if len(nums) >= 2:
                delta = nums[-1] - nums[0]
                trend = ("rising" if delta > 0
                         else "falling" if delta < 0 else "stable")
            lines.append(
                f"  • {kind}: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"
                f" · latest {self._fmt_measurement(entries[-1])}"
                f" · min {min(nums):g} / max {max(nums):g}"
                f" / avg {sum(nums) / len(nums):.1f}"
                + (f" · trend {trend}" if trend else ""))
        lines.append("\nThese are simple statistics on what you entered — "
                     "not a clinical assessment.")
        return self._reply(request, "\n".join(lines), action="history")

    def _do_meds(self, request: AgentRequest) -> AgentResult:
        parsed = parser.parse_medications(request.text or "")
        if parsed["meds"]:
            self.store.add_medications(parsed["meds"])
            self._save_checkpoint()
        current = self.store.get_medications()
        if not current:
            return self._reply(
                request,
                "I don't have any medicines on file yet. Send them like\n"
                "    Metformin 500mg twice daily after food\n"
                "— one per line — and I'll keep an organized list.",
                action="meds")
        lines = [f"Your medication list ({len(current)}), exactly as you "
                 "provided it:"]
        for m in current[:60]:
            bits = [m["name"]]
            if m.get("dose"):
                bits.append(m["dose"])
            if m.get("frequency"):
                bits.append(m["frequency"])
            lines.append("  • " + " · ".join(bits))
        lines.append("\nI only organize this list — I don't check "
                     "interactions or advise on doses. Share it with your "
                     "clinician or pharmacist.")
        return self._reply(request, "\n".join(lines), action="meds")

    def _generate_questions(self) -> list[str]:
        qs: list[str] = []
        symptoms = self.store.symptoms(limit=10)
        if symptoms:
            latest = symptoms[-1]
            what = ", ".join(latest.get("items") or []) or "my symptoms"
            since = f" (onset: {latest['onset']})" if latest.get("onset") \
                else ""
            qs.append(f"I've had {what}{since}. What should we look into, "
                      "and what warning signs should bring me back "
                      "sooner?")
        flagged_names: set[str] = set()
        for rep in self.store.reports(limit=5):
            for t in rep.get("tests", []):
                if t.get("flag") in ("LOW", "HIGH"):
                    flagged_names.add(t.get("name", ""))
        if flagged_names:
            qs.append("Some of my lab values were outside the printed "
                      f"ranges ({', '.join(sorted(flagged_names)[:6])}) — "
                      "do we need to repeat or follow up on any of "
                      "them?")
        if self.store.get_medications():
            qs.append("Do any of my current medicines interact with each "
                      "other or with something new you might prescribe?")
        if symptoms or flagged_names or self.store.get_medications():
            qs.append("What monitoring or lifestyle steps do you suggest "
                      "between now and my next visit?")
        return qs

    def _do_questions(self, request: AgentRequest) -> AgentResult:
        m = re.search(r"\badd\s+question\b[:\-]?\s*(.+)$",
                      request.text or "", re.IGNORECASE)
        if m:
            self.store.add_question(m.group(1))
        existing = {q["text"].lower()
                    for q in self.store.questions(limit=100)}
        for q in self._generate_questions():
            if q.lower() not in existing:
                self.store.add_question(q)
                existing.add(q.lower())
        questions = self.store.questions(limit=30)
        if not questions:
            return self._reply(
                request,
                "No questions stored yet. Describe symptoms or share a "
                "report/medicines, or send 'add question: <text>' and I'll "
                "build the list.",
                action="questions")
        lines = ["Questions to bring to your clinician:"]
        for i, q in enumerate(questions, 1):
            lines.append(f"  {i}. {q['text']}")
        lines.append("\nI can add more any time ('add question: …') — "
                     "the list lives in your private health domain.")
        self._save_checkpoint()
        return self._reply(request, "\n".join(lines), action="questions")

    def _do_systems(self, request: AgentRequest) -> AgentResult:
        topic = safety.systems_topic(request.text or "")
        if not topic:
            return self._reply(
                request,
                "I can give general information about allopathy (modern "
                "medicine), Ayurveda, and homeopathy — which would you "
                "like to hear about?",
                action="systems")
        return self._reply(request, safety.systems_info(topic),
                           action="systems")

    def _do_monitor(self, request: AgentRequest) -> AgentResult:
        rows = self.store.measurements(limit=10)
        if not rows:
            return self._reply(
                request,
                "Nothing to monitor yet. Send measurements like 'bp 130/85' "
                "or 'spo2 96%' and I'll keep watch on the values you "
                "enter.",
                action="monitor")
        lines = ["Your latest entries:"]
        urgent = 0
        for r in rows:
            shown = self._fmt_measurement(r)
            note = safety.value_flags(r["kind"], r.get("value") or {})
            lines.append("  • " + shown
                         + (f"  — ⚠️ {note}." if note else ""))
            urgent += 1 if note else 0
        if urgent:
            lines.append(f"\n⚠️ {urgent} recent value(s) were in a range "
                         "that deserves prompt attention — follow the "
                         "guidance shown, and contact a clinician.")
        s = self.store.summary()
        lines.append(f"\nStored privately: {s['measurements']} "
                     f"measurement(s), {s['symptoms']} symptom note(s), "
                     f"{s['medications']} medicine(s), {s['reports']} "
                     f"report(s), {s['questions']} question(s).")
        return self._reply(request, "\n".join(lines), action="monitor")

    def _do_profile(self, request: AgentRequest) -> AgentResult:
        text = request.text or ""
        updates: dict = {}
        m = re.search(r"\bage\b[:\s]*(\d{1,3})", text, re.IGNORECASE)
        if m:
            updates["age"] = m.group(1)
        m = re.search(r"\bblood\s*group\b[:\s]*"
                      r"([ABOab]{1,2}\s*(?:pos|neg|positive|negative|\+|-)?)",
                      text, re.IGNORECASE)
        if m:
            updates["blood_group"] = m.group(1).strip()
        m = re.search(r"\ballerg(?:y|ies)\b[:\s]*(.+)$", text,
                      re.IGNORECASE | re.DOTALL)
        if m:
            updates["allergies"] = m.group(1).strip()[:200]
        m = re.search(r"\bconditions?\b[:\s]*(.+)$", text,
                      re.IGNORECASE | re.DOTALL)
        if m:
            updates["conditions"] = m.group(1).strip()[:200]
        if updates:
            profile = self.store.get_profile()
            profile.update(updates)
            self.store.save_profile(profile)
            self._save_checkpoint()
        profile = self.store.get_profile()
        if not profile:
            return self._reply(
                request,
                "No profile stored. Tell me things like 'age: 34', "
                "'blood group: O+', 'allergies: penicillin' — all kept "
                "private.",
                action="profile")
        lines = ["Your health profile (private):"]
        for k in ("name", "age", "sex", "blood_group", "allergies",
                  "conditions", "contact", "notes"):
            if profile.get(k):
                lines.append(f"  • {k.replace('_', ' ')}: {profile[k]}")
        return self._reply(request, "\n".join(lines), action="profile")

    def _do_summary(self, request: AgentRequest) -> AgentResult:
        s = self.store.summary()
        lines = ["Your private health domain currently holds:"]
        for label, key in (("symptom note(s)", "symptoms"),
                           ("measurement(s)", "measurements"),
                           ("medicine(s)", "medications"),
                           ("report(s)", "reports"),
                           ("clinician question(s)", "questions")):
            lines.append(f"  • {s[key]} {label}")
        lines.append("  • profile: " + ("yes" if s["profile"] else "no"))
        lines.append("\nEverything lives in memory/health/ with "
                     "owner-only permissions, never in general Jarvis "
                     "memory. Say 'wipe health data' (and confirm) to "
                     "erase it all.")
        return self._reply(request, "\n".join(lines), action="summary")

    def _do_wipe(self, request: AgentRequest) -> AgentResult:
        if not request.metadata.get("confirm_wipe"):
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.NEEDS_INPUT,
                text=("This would permanently erase ALL stored health data "
                      "(profile, symptoms, measurements, medicines, "
                      "reports, questions). Confirm by sending the same "
                      "request with metadata confirm_wipe=true."),
                data={"health": True, "sensitive": True, "action": "wipe",
                      "needs_confirmation": True},
                agent=self.name)
        self.store.wipe()
        self._save_checkpoint()
        return self._reply(request,
                           "All stored health data has been erased. The "
                           "audit log records only that a wipe happened.",
                           action="wipe")

    # ── dispatch table ───────────────────────────────────────────────────
    _HANDLERS = {
        "symptoms": _do_symptoms,
        "report": _do_report,
        "track": _do_track,
        "history": _do_history,
        "meds": _do_meds,
        "questions": _do_questions,
        "systems": _do_systems,
        "monitor": _do_monitor,
        "profile": _do_profile,
        "summary": _do_summary,
        "wipe": _do_wipe,
    }
