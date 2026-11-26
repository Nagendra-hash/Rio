"""
orchestrator/agents/health/safety.py — the safety layer for the Health Agent.

Baked into CODE, not prompts (the trading-agent rule table pattern):

  * NOT A DOCTOR      — every user-facing answer ends with the disclaimer;
                        the agent never claims clinical authority.
  * NO DIAGNOSIS      — diagnosis-seeking asks are refused with a safe
                        alternative (organize symptoms, prepare questions).
  * NO PRESCRIBING    — starting/stopping/changing medication or doses is
                        refused; medication data is only organized/summarized.
  * EMERGENCY FIRST   — red-flag symptoms short-circuit EVERYTHING and return
                        an emergency-care directive before any other action.
  * URGENT VALUES     — certain user-entered measurements (BP ≥ 180/120,
                        SpO₂ < 90, extreme glucose/temp) raise the same
                        urgent-care directive.
  * SYSTEMS OF MEDICINE — allopathy / Ayurveda / homeopathy get neutral
                        general information only: no efficacy claims, no
                        comparisons, no treatment recommendations.

Deterministic and offline: no LLM in the safety path, ever.
"""
from __future__ import annotations

import re

# ── constants ────────────────────────────────────────────────────────────────

DISCLAIMER = ("I'm not a doctor and this is not a diagnosis or medical "
              "advice — please consult a qualified clinician.")

EMERGENCY_DIRECTIVE = (
    "⚠️ This may be an EMERGENCY. Stop and get help now:\n"
    "  • Call your local emergency number (112 in India, 999 in the UK, "
    "911 in the US) or go to the nearest emergency department.\n"
    "  • Do not wait for an online assistant, and do not drive yourself "
    "if you feel faint, weak, or unwell.\n"
    "  • Tell the responders exactly what you told me."
)

CRISIS_DIRECTIVE = (
    "⚠️ Please reach out for help right now — you deserve support:\n"
    "  • Call your local emergency number, or a crisis helpline "
    "(India: Tele-MANAS 14416 / iCall 9152987821; US: 988).\n"
    "  • Stay with someone you trust until help is available."
)

REFUSALS = {
    "diagnose": (
        "I can't diagnose conditions — that's a job for a qualified "
        "clinician who can examine you.\n"
        "What I CAN do:\n"
        "  • organize your symptoms and history clearly,\n"
        "  • explain what your lab report says in general terms,\n"
        "  • prepare a list of questions to ask your doctor."),
    "prescribe": (
        "I can't prescribe medication, adjust doses, or advise starting or "
        "stopping any medicine — only your prescriber can do that.\n"
        "What I CAN do:\n"
        "  • keep an organized list of the medicines YOU tell me about,\n"
        "  • prepare questions about them for your clinician or "
        "pharmacist."),
    "treat": (
        "I can't recommend treatments or remedies as a substitute for "
        "professional care. A qualified clinician should decide treatment.\n"
        "I can organize your information and prepare questions for that "
        "conversation."),
}

# ── red-flag emergency triage ────────────────────────────────────────────────

_RED_FLAGS: tuple[tuple[str, str], ...] = (
    # pattern, short label
    (r"\b(?:chest|left\s+arm)\s+(?:pain|pressure|tightness|heaviness)\b"
     r"|\b(?:crushing|severe)\s+(?:chest|arm)\s+(?:pain|pressure)\b"
     r"|\bheart\s+attack\b", "possible cardiac symptoms"),
    (r"\b(?:can(?:'|no)?t\s+breathe|difficulty\s+(?:breathing|breath)"
     r"|short(ness)?\s+of\s+breath|breathless(sness)?|gasping"
     r"|choking)\b", "breathing difficulty"),
    (r"\b(?:face|mouth|eye)\s+(?:is\s+|and\s+)?droop\w*\b"
     r"|\bslurred\s+speech\b|\bspeech\s+(?:is\s+)?slurred\b|\bslurring\b"
     r"|\bsudden(ly)?\s+(?:weak|numb(?:ness)?)\b"
     r"|\b(?:numb|weak)(?:ness)?\s+(?:on\s+)?one\s+side\b"
     r"|\bstroke\b", "possible stroke signs"),
    (r"\b(?:severe|heavy|uncontrolled)\s+bleeding\b"
     r"|\bbleed(?:ing)?\s+(?:won'?t|does\s?n'?t|did\s?n'?t)\s+stop\b"
     r"|\bcough(?:ing)?\s+up\s+blood\b|\bvomit(?:ing)?\s+blood\b", "severe bleeding"),
    (r"\b(?:unconscious|unresponsive|passed\s+out|fainted?|collapse[d]?)\b",
     "loss of consciousness"),
    (r"\b(?:seizu\w+|convulsion|fits?)\b", "seizure / convulsion"),
    (r"\b(?:throat|tongue|face|lips)\s+(?:is\s+)?(?:swell\w*|closing)\b"
     r"|\banaphyla(xis|ctic)\b|\bsevere\s+allergic\s+reaction\b",
     "possible severe allergic reaction"),
    (r"\b(?:poison(?:ing|ed)?|overdose[d]?)\b", "poisoning / overdose"),
    (r"\b(?:suicid\w*|kill(?:ing|s)?\s+(?:myself|me)\b|end\s+my\s+life"
     r"|self[-\s]?harm|hurt\s+myself)\b", "personal safety / crisis"),
    (r"\b(?:baby|infant|newborn)\b[^.\n]{0,40}?\bfever\b"
     r"|\bfever\b[^.\n]{0,40}?\b(?:baby|infant|newborn|under\s+(?:3|three)\s+months)\b",
     "fever in a young infant"),
    (r"\b(?:worst|thunderclap|sudden\s+severe)\s+headache\b", "sudden severe headache"),
    (r"\b(?:severe|burning)\s+(?:burn|scald)\b|\bthird[-\s]degree\s+burn\b", "severe burn"),
    (r"\b(?:broken\s+bone|bone\s+sticking|open\s+fracture|deep\s+wound)\b",
     "serious injury"),
)

_RED_FLAG_RE = re.compile(
    "|".join(f"(?:{pat})" for pat, _ in _RED_FLAGS), re.IGNORECASE)

_EMERGENCY_STYLE_FLAGS = {"personal safety / crisis"}


def triage(text: str) -> dict:
    """Red-flag scan. Returns {emergency, labels, directive}.

    Runs BEFORE every other action: an emergency is never logged, parsed,
    or answered with anything but the directive.
    """
    t = (text or "")
    labels = [label for pat, label in _RED_FLAGS
              if re.search(pat, t, re.IGNORECASE)]
    if not labels:
        return {"emergency": False, "labels": [], "directive": ""}
    crisis = any(lb in _EMERGENCY_STYLE_FLAGS for lb in labels)
    directive = CRISIS_DIRECTIVE if crisis else EMERGENCY_DIRECTIVE
    return {"emergency": True, "labels": labels, "directive": directive}


# ── diagnosis / prescribing asks ─────────────────────────────────────────────

_DIAGNOSE_RE = re.compile(
    r"\b(?:diagnos\w*|what(?:'s|s|)\s+(?:wrong|wrongness)\s+with\s+me"
    r"|what\s+(?:disease|illness|condition|sickness)\s+(?:do\s+i\s+have"
    r"|is\s+this|could\s+(?:this|it)\s+be)\b|do\s+i\s+have\s+(?:a\s+|an\s+|"
    r"any\s+)?(?:(?:serious|dangerous|deadly|chronic|rare)\s+)?"
    r"(?:disease|illness|condition|infection)\b"
    r"|is\s+(?:this|it)\s+(?:cancer|serious|dangerous|deadly)"
    r"|which\s+(?:disease|illness|condition)\b|am\s+i\s+dying)\b",
    re.IGNORECASE)

_PRESCRIBE_RE = re.compile(
    r"\b(?:prescri(?:be|ption)\b(?!\s*(?:list|refill|history|details?|info))"
    r"|what\s+(?:medicine|medication|tablet|drug|dose|dosage)\s+should\s+i"
    r"|which\s+(?:medicine|medication|tablet|drug)\s+(?:should|can)\s+i\s+take"
    r"|how\s+much\s+(?:dose|dosage)\s+(?:of|should)|start\s+taking\b"
    r"|stop\s+taking\b(?!\s*(?:side|medicine\s+list))|increase\s+(?:my\s+)?dose"
    r"|reduce\s+(?:my\s+)?dose|double\s+(?:my\s+)?dose)\b",
    re.IGNORECASE)

_TREAT_RE = re.compile(
    r"\b(?:what\s+(?:treatment|remedy|cure)\s+(?:should|can|do)\s+i\b"
    r"|cure\s+for\s+(?:my|this)\b|best\s+(?:remedy|cure|treatment)\s+for\b"
    r"|treat\s+my\b)\b",
    re.IGNORECASE)


def classify_risk(text: str) -> str:
    """"" -> "diagnose" | "prescribe" | "treat" | "".

    Empty string means the ask needs no refusal.
    """
    t = (text or "")
    if _PRESCRIBE_RE.search(t):
        return "prescribe"
    if _DIAGNOSE_RE.search(t):
        return "diagnose"
    if _TREAT_RE.search(t):
        return "treat"
    return ""


def refusal(kind: str) -> str:
    return REFUSALS.get(kind, REFUSALS["diagnose"])


# ── urgent user-entered measurement values ───────────────────────────────────

def _bp_flag(sys_: int, dia: int) -> str:
    if sys_ >= 180 or dia >= 120:
        return ("hypertensive-crisis range (≥180/120) — seek urgent medical "
                "care now; if it comes with chest pain, breathing trouble, "
                "vision changes, or weakness, treat it as an emergency")
    if sys_ < 90 or dia < 50:
        return ("unusually low for most adults — if you feel dizzy, faint, "
                "or confused, seek medical care promptly")
    return ""


def _spo2_flag(v: int) -> str:
    if v < 90:
        return ("below 90% — seek urgent medical care now (this is a "
                "common urgent-care threshold)")
    if v < 94:
        return ("slightly low — recheck; if it stays under 94% or you feel "
                "breathless, contact a clinician promptly")
    return ""


def _glucose_flag(mgdl: float | None, mmol: float | None) -> str:
    v = mgdl if mgdl is not None else (mmol * 18.0 if mmol else None)
    if v is None:
        return ""
    if v <= 54:
        return ("very low — treat low blood sugar immediately per your "
                "clinician's instructions and seek urgent help")
    if v < 70:
        return ("low — follow your clinician's low-sugar instructions and "
                "recheck; get help if symptoms don't settle")
    if v >= 400:
        return ("very high — contact your clinician today; seek urgent care "
                "with vomiting, confusion, or rapid breathing")
    if v >= 250:
        return ("high — mention to your clinician promptly, especially if "
                "it repeats")
    return ""


def _temp_flag(f: float | None, c: float | None) -> str:
    fc = f if f is not None else (c * 9 / 5 + 32 if c is not None else None)
    if fc is None:
        return ""
    if fc >= 104.0:
        return ("very high fever (≥40 °C / 104 °F) — seek medical care now")
    if fc >= 103.0:
        return ("high fever — contact a clinician today")
    return ""


def _pulse_flag(v: int) -> str:
    if v > 150:
        return ("very fast resting pulse — seek medical care now if it "
                "persists or comes with chest pain or breathlessness")
    if v < 40:
        return ("very low pulse — seek medical care if you feel faint, "
                "dizzy, or weak")
    return ""


def value_flags(kind: str, value: dict) -> str:
    """Urgent-care note for an extreme user-entered measurement, else ''.

    Thresholds are COMMON clinical alert values, not personal targets —
    the caller must attach the standard disclaimer.
    """
    k = (kind or "").lower()
    try:
        if k == "bp":
            return _bp_flag(int(value["systolic"]), int(value["diastolic"]))
        if k == "spo2":
            return _spo2_flag(int(value["value"]))
        if k == "glucose":
            return _glucose_flag(value.get("mgdl"), value.get("mmol"))
        if k == "temp":
            return _temp_flag(value.get("f"), value.get("c"))
        if k == "pulse":
            return _pulse_flag(int(value["value"]))
    except (KeyError, TypeError, ValueError):
        return ""
    return ""


# ── systems of medicine: general info only ───────────────────────────────────

SYSTEMS_INFO: dict[str, str] = {
    "allopathy": (
        "Allopathy (modern/conventional medicine) is the mainstream system "
        "used in hospitals worldwide. It relies on scientifically tested "
        "diagnostics (blood tests, imaging) and treatments (medicines, "
        "surgery, vaccines) that go through clinical trials and regulatory "
        "review. Practitioners: MBBS/MD doctors and specialists."),
    "ayurveda": (
        "Ayurveda is a traditional Indian system, thousands of years old, "
        "built on balance among body, mind, and lifestyle, with concepts "
        "like the three doshas (vata, pitta, kapha). It uses diet, herbal "
        "preparations, massage (panchakarma), yoga, and daily-routine "
        "guidance. In India it is formally regulated (AYUSH ministry; "
        "BAMS practitioners). Tell your doctor about any herbal products "
        "you take — some interact with conventional medicines."),
    "homeopathy": (
        "Homeopathy is a system developed in the 1700s based on "
        "'like cures like' and extreme dilutions of substances. It is "
        "practiced in many countries and regulated to varying degrees. "
        "Its remedies are highly diluted, and its effectiveness beyond a "
        "placebo effect is debated in mainstream research. If you use it, "
        "keep your conventional doctor informed, and don't replace "
        "emergency care or prescribed treatment with it."),
}

_SYSTEMS_RE = re.compile(
    r"\b(allopath\w*|ayurved\w*|homeopath\w*|homoeopath\w*)\b", re.IGNORECASE)


def systems_topic(text: str) -> str:
    """Return the canonical system name mentioned in text, else ''."""
    m = _SYSTEMS_RE.search(text or "")
    if not m:
        return ""
    word = m.group(1).lower()
    if word.startswith("ayurved"):
        return "ayurveda"
    if word.startswith(("homeopath", "homoeopath")):
        return "homeopathy"
    return "allopathy"


def systems_info(topic: str) -> str:
    """General information about a system of medicine. Neutral by design:
    no efficacy claims, no comparisons, no treatment recommendations."""
    info = SYSTEMS_INFO.get((topic or "").lower())
    if not info:
        return ""
    return (info + "\n\nThis is general background, not a recommendation "
            "of any system for your situation — diagnosis and treatment "
            "belong with a qualified practitioner of the system you choose.")
