"""
orchestrator/agents/health/parser.py — deterministic report/data parsers.

Pure functions, stdlib-only, offline. The parsers EXTRACT and ORGANIZE;
they never interpret clinically (interpretation-by-flag is limited to
comparing a value against the report's own printed reference range).

Privacy rule: patient-identity lines ("Patient: …", "Name: …", MRN, age/
sex/DOB headers) are detected and DROPPED — the store keeps findings, not
identities. Address/phone/ID patterns are scrubbed from stored text.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# ── shared text hygiene ──────────────────────────────────────────────────────

_IDENTITY_RE = re.compile(
    r"^\s*(?:patient(?:\s+name)?|name|mrn|uhid|reg(?:istration)?\s*(?:no|number)"
    r"|case\s*(?:no|number)|age(?:/sex)?|sex|gender|dob|date\s+of\s+birth"
    r"|address|phone|mobile|email|id\s*(?:no|number)?)\s*[:\-#]",
    re.IGNORECASE)

_PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s-]?)?\d{10}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_MRID_RE = re.compile(r"\b(?:MRN|UHID)[:\s#]*[A-Za-z0-9-]{4,}\b", re.IGNORECASE)


def scrub(text: str) -> str:
    """Remove identity lines and obvious contact/ID tokens."""
    out = []
    for line in (text or "").splitlines():
        if _IDENTITY_RE.match(line):
            continue
        out.append(line)
    s = "\n".join(out)
    s = _EMAIL_RE.sub("[contact removed]", s)
    s = _MRID_RE.sub("[id removed]", s)
    s = _PHONE_RE.sub("[contact removed]", s)
    return s.strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── lab reports ──────────────────────────────────────────────────────────────

#  "Hemoglobin: 10.5 g/dL (13.0 - 17.0)"  /  "Hb 10.5 (Ref: 13-17)"
#  "Blood Sugar (Fasting): 180 mg/dL [70 - 110]"  /  "Vitamin D — 12 ng/mL"
_LAB_RE = re.compile(
    r"^\s*[-•*]?\s*(?P<name>[A-Za-z][A-Za-z0-9 ()/%²³·.\-]{1,48}?)"
    r"\s*(?P<sep>[:\-–—])?\s*"
    r"(?P<value>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>[A-Za-zµ%°/²³0-9^.\-]{0,20}?)?\s*"
    r"(?:[(\[]\s*(?:(?:ref(?:erence)?\s*[:\-]?\s*)?)"
    r"(?P<lo>\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(?P<hi>\d+(?:\.\d+)?)"
    r"(?:\s*[A-Za-zµ%/²³.\-]{0,20})?\s*[)\]])?\s*$",
    re.IGNORECASE)

# common panel names → one-line general description (background only)
TEST_GLOSSARY: dict[str, str] = {
    "hemoglobin": "oxygen-carrying protein of red blood cells; low values are called anemia in general terms",
    "wbc": "white blood cells — part of the immune system",
    "platelets": "cells that help blood clot",
    "rbc": "red blood cell count",
    "glucose": "blood sugar",
    "sugar": "blood sugar",
    "hba1c": "average blood sugar over roughly the past 3 months",
    "cholesterol": "blood fat; panels usually split total, LDL, HDL, triglycerides",
    "ldl": "low-density lipoprotein, often called 'bad' cholesterol",
    "hdl": "high-density lipoprotein, often called 'good' cholesterol",
    "triglycerides": "a type of blood fat",
    "tsh": "thyroid-stimulating hormone — a common thyroid-function screen",
    "creatinine": "commonly used to gauge kidney function",
    "urea": "waste product also used to gauge kidney function",
    "alt": "liver enzyme (SGPT)",
    "ast": "liver enzyme (SGOT)",
    "bilirubin": "pigment processed by the liver",
    "vitamin d": "nutrient important for bones and immunity",
    "vitamin b12": "nutrient important for nerves and blood cells",
    "uric acid": "waste product; high levels are associated with gout",
    "esr": "a general inflammation marker",
    "crp": "a general inflammation marker",
    "sodium": "an electrolyte",
    "potassium": "an electrolyte important for heart rhythm",
    "calcium": "mineral important for bones, muscles, nerves",
}


# Real-world lab units: contain a digit/symbol, or are one of these short
# tokens. This keeps prose like "fever since 3 days" (unit would be
# "days") from ever becoming a test row.
_UNIT_WORDS = {"mg", "mcg", "ml", "dl", "gl", "ul", "gm", "kg", "iu",
               "g", "l", "ng", "pg", "mm", "cm", "miu", "ui", "mosm"}


def _unit_ok(unit: str) -> bool:
    if not unit:
        return False
    return bool(re.search(r"[\d/%°^.µ×*]", unit)) \
        or unit.lower() in _UNIT_WORDS


def _flag(value: float, lo: float | None, hi: float | None) -> str:
    if lo is not None and value < lo:
        return "LOW"
    if hi is not None and value > hi:
        return "HIGH"
    if lo is not None or hi is not None:
        return "OK"
    return "CHECK"          # no printed range → clinician reads it


def parse_lab_report(text: str) -> dict:
    """Extract test rows from a lab report's text.

    Returns {tests: [{name, value, unit, low, high, flag, glossary}],
             unparsed: [lines], identity_removed: n}.
    Flags are ONLY 'value vs the report's own printed range' — descriptive,
    not diagnostic.
    """
    tests: list[dict] = []
    unparsed: list[str] = []
    dropped = 0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _IDENTITY_RE.match(line):
            dropped += 1
            continue
        m = _LAB_RE.match(line)
        if not m:
            unparsed.append(line[:120])
            continue
        name = re.sub(r"\s+", " ", m.group("name")).strip(" .:-")
        try:
            value = float(m.group("value").replace(",", "."))
        except ValueError:
            unparsed.append(line[:120])
            continue
        lo = float(m.group("lo")) if m.group("lo") else None
        hi = float(m.group("hi")) if m.group("hi") else None
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        unit = (m.group("unit") or "").strip(" .-")
        # Accept only rows with real lab-line evidence: a separator, a
        # plausible unit, or a printed range. Plain prose ("Room 12",
        # "fever since 3 days") stays unparsed.
        if not (m.group("sep") or lo is not None or _unit_ok(unit)):
            unparsed.append(line[:120])
            continue
        glossary = ""
        lname = name.lower()
        for key, desc in sorted(TEST_GLOSSARY.items(),
                                key=lambda kv: -len(kv[0])):
            # word-boundary match (so 'ast' never fires inside 'fasting');
            # optional trailing digits cover names like 'Vitamin D3'.
            if key and re.search(rf"\b{re.escape(key)}\d*\b", lname):
                glossary = desc
                break
        tests.append({
            "name": name[:60],
            "value": value,
            "unit": unit[:20],
            "low": lo, "high": hi,
            "flag": _flag(value, lo, hi),
            "glossary": glossary,
        })
    return {"tests": tests, "unparsed": unparsed[:20],
            "identity_removed": dropped}


# ── measurements (vitals the user types in) ──────────────────────────────────

_BP_RE = re.compile(
    r"\b(?:bp|blood\s*pressure)\b\D{0,15}?(\d{2,3})\s*/\s*(\d{2,3})", re.IGNORECASE)
_WEIGHT_RE = re.compile(
    r"\bweight\b\D{0,10}?(\d{1,3}(?:\.\d)?)\s*(kg|kgs|kilos?|lb|lbs|pounds?)?",
    re.IGNORECASE)
_GLUCOSE_RE = re.compile(
    r"\b(?:sugar|glucose|bsl|fbs|ppbs)\b(?:[^.\n]{0,12}?\bfasting\b)?\D{0,12}?"
    r"(\d{1,4})(?:\.(\d))?\s*(mg/dl|mmol/l)?", re.IGNORECASE)
_TEMP_RE = re.compile(
    r"\btemp(?:erature)?\b\D{0,10}?(\d{2,3}(?:\.\d)?)\s*°?\s*([fc])?\b",
    re.IGNORECASE)
_SPO2_RE = re.compile(
    r"\b(?:spo2|sp02|o2\s*sat(?:uration)?|oxygen\s+(?:level|sat))\b\D{0,10}?"
    r"(\d{2,3})\s*%?", re.IGNORECASE)
_PULSE_RE = re.compile(
    r"\b(?:pulse|heart\s*rate|hr)\b\D{0,10}?(\d{2,3})\s*(?:bpm)?\b",
    re.IGNORECASE)


def parse_measurements(text: str) -> list[dict]:
    """Extract user-entered measurements: [{kind, value, unit, extra}]."""
    out: list[dict] = []
    for m in _BP_RE.finditer(text or ""):
        out.append({"kind": "bp",
                    "value": {"systolic": int(m.group(1)),
                              "diastolic": int(m.group(2))},
                    "unit": "mmHg", "extra": {}})
    for m in _WEIGHT_RE.finditer(text or ""):
        unit = (m.group(2) or "kg").lower()
        if unit.startswith(("lb", "pound")):
            unit = "lb"
        elif not unit.startswith("kg"):
            unit = "kg"
        out.append({"kind": "weight", "value": {"value": float(m.group(1))},
                    "unit": unit, "extra": {}})
    for m in _GLUCOSE_RE.finditer(text or ""):
        whole = m.group(1)
        unit = (m.group(3) or "mg/dL").lower()
        val: dict = {}
        if unit.startswith("mmol"):
            val["mmol"] = float(f"{whole}.{m.group(2) or '0'}")
            val["mgdl"] = val["mmol"] * 18.0
            unit = "mmol/L"
        else:
            val["mgdl"] = float(whole + (f".{m.group(2)}" if m.group(2) else ""))
            unit = "mg/dL"
        out.append({"kind": "glucose", "value": val, "unit": unit, "extra": {}})
    for m in _TEMP_RE.finditer(text or ""):
        raw = float(m.group(1))
        unit = (m.group(2) or ("c" if raw < 60 else "f")).lower()
        val = {"c": raw} if unit == "c" else {"f": raw}
        out.append({"kind": "temp", "value": val,
                    "unit": "°C" if unit == "c" else "°F", "extra": {}})
    for m in _SPO2_RE.finditer(text or ""):
        out.append({"kind": "spo2", "value": {"value": int(m.group(1))},
                    "unit": "%", "extra": {}})
    for m in _PULSE_RE.finditer(text or ""):
        out.append({"kind": "pulse", "value": {"value": int(m.group(1))},
                    "unit": "bpm", "extra": {}})
    return out


# ── medications (user-provided list only) ────────────────────────────────────

_MED_LINE_RE = re.compile(
    r"^\s*[-•*]?\s*(?P<name>[A-Za-z][A-Za-z0-9/\- ]{1,40}?)"
    r"(?:\s+(?P<dose>\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|iu|units?|unit)\b"
    r"(?:\s*[/+]\s*\d+(?:\.\d+)?\s*(?:mg|mcg|ml)\b)?))?"
    r"(?:[,\s]+(?P<freq>"
    r"(?:\d\s*(?:tabs?|tablets?|caps?|capsules?|drops?|puffs?|sachets?)?\s*)?"
    r"(?:once|twice|thrice|three\s+times|four\s+times|\d+\s*times)\s*"
    r"(?:a\s+|per\s+)?(?:day|daily|week|weekly)"
    r"|(?:once|twice|thrice)\s+daily"
    r"|every\s+\d+\s*hours?"
    r"|\d+\s*(?:h|hr|hours?)\s*(?:interval|gap)?"
    r"|q\d+h|od|bd|tds|sos|daily|nightly|as\s+needed"
    r"|\d\s*-\s*\d\s*-\s*\d"
    r"|at\s+bed\s*time|at\s+night|in\s+the\s+(?:morning|night|evening)"
    r"|mornings?|nights?"
    r")"
    r"(?:\s+(?:before|after|with)\s+(?:food|meals?|milk)"
    r"|\s+empty\s+stomach|\s+at\s+bed\s*time|\s+at\s+night)*"
    r")?"
    r"[.\s]*$", re.IGNORECASE)


def parse_medications(text: str) -> dict:
    """Parse user-provided medication lines (name/dose/frequency/timing).

    Returns {meds: [{name, dose, frequency}], unparsed: [lines]}.
    The agent stores these verbatim-organized; it NEVER adds, changes, or
    advises on them.
    """
    meds: list[dict] = []
    unparsed: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _MED_LINE_RE.match(line)
        if not m or not (m.group("dose") or m.group("freq")):
            unparsed.append(line[:100])
            continue
        meds.append({
            "name": re.sub(r"\s+", " ", m.group("name")).strip(" .,-")[:60],
            "dose": (m.group("dose") or "").strip()[:30],
            "frequency": (m.group("freq") or "").strip()[:60],
        })
    return {"meds": meds, "unparsed": unparsed[:20]}


# ── symptoms (organize, never diagnose) ──────────────────────────────────────

_SYMPTOM_WORDS = (
    r"fever|cough|cold|sore\s+throat|headache|migraine|body\s+ache|"
    r"muscle\s+pain|joint\s+pain|back\s+pain|stomach\s+(?:ache|pain)|"
    r"abdominal\s+pain|nausea|vomit\w*|diarr\w+|loose\s+motions?|"
    r"constipation|acidity|heartburn|bloating|fatigue|tiredness|weakness|"
    r"dizz\w+|light\s*-?headed|faint\w*|rash|itch\w+|swell\w+|"
    r"breathless\w*|wheez\w+|palpitation\w+|insomnia|sleep\w*\s+issues?|"
    r"anxiety|numbness|tingling|blurred\s+vision|ear\s+pain|toothache|"
    r"burning\s+urination|pain\s+urination|chest\s+(?:pain|discomfort)"
)
_SYMPTOM_RE = re.compile(rf"\b({_SYMPTOM_WORDS})\b", re.IGNORECASE)
_ONSET_RE = re.compile(
    r"\b(?:since|from|for)\s+(?:(\d+)\s*(day|days|week|weeks|month|months|"
    r"hour|hours))\b|\b(?:yesterday|today|last\s+night|this\s+morning)\b",
    re.IGNORECASE)
_SEVERITY_RE = re.compile(
    r"\b(mild|moderate|severe|unbearable|(\d{1,2})\s*(?:/|out\s+of)\s*10)\b",
    re.IGNORECASE)


def parse_symptoms(text: str) -> dict:
    """Organize symptom prose: {items: [names], onset: str, severity: str,
    text: scrubbed original (clipped)}."""
    t = scrub(text or "")[:500]
    seen: list[str] = []
    for m in _SYMPTOM_RE.finditer(t):
        w = re.sub(r"\s+", " ", m.group(1)).lower()
        if w not in seen:
            seen.append(w)
    onset_m = _ONSET_RE.search(t)
    sev_m = _SEVERITY_RE.search(t)
    return {
        "items": seen[:12],
        "onset": onset_m.group(0) if onset_m else "",
        "severity": sev_m.group(1) if sev_m else "",
        "text": t,
    }
