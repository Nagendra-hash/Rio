"""
core/scientific/safety.py — deterministic guardrails for research output.

Same philosophy as `orchestrator/agents/health/safety.py`: the checks that must
never be talked out of are code, not prompt. A prompt can be argued with; a
regular expression that refuses "what dose should I give my patient" cannot.

Three separate concerns:

    clinical   research/education only. No diagnosis, no prescribing, no
               patient-specific treatment or triage. A qualified professional
               must review anything clinical.
    financial  analysis only. Never an order. Trading goes to the trading agent
               with its existing approval gate; AlgoTrade's risk controls are
               untouched.
    integrity  citations come from retrieved records, and hypotheses are never
               presented as established findings. The report builder enforces
               this structurally (see report.py), and these rules describe it
               to the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CLINICAL_DISCLAIMER = (
    "Research and educational information only — not medical advice, diagnosis "
    "or treatment. A qualified clinician must review any clinical decision."
)
FINANCIAL_DISCLAIMER = (
    "Research and statistical analysis only — not financial advice. No order was "
    "placed; trading is handled by the trading agent's own approval process."
)
INTEGRITY_NOTE = (
    "Citations refer only to records retrieved in this run. Claims without a "
    "retrieved source are labelled unverified, and hypotheses are labelled as "
    "hypotheses — never as findings."
)

# ── clinical ────────────────────────────────────────────────────────────────

_CLINICAL_REFUSE = re.compile(
    r"\b(?:diagnose|diagnosis for|what(?:'s| is) wrong with (?:me|my patient)|"
    r"do i have|does my patient have|"
    r"prescribe|prescription for|what (?:dose|dosage)|how much (?:should|do) i "
    r"(?:take|give)|should i (?:take|give|stop taking)|"
    r"treat(?:ment)? for my|is it (?:cancer|safe to stop)|"
    r"interpret my (?:scan|mri|ct|blood|lab)|"
    r"which (?:drug|medication) should (?:i|my patient) (?:take|use))\b",
    re.IGNORECASE,
)

_CLINICAL_TOPIC = re.compile(
    r"\b(?:patient|clinical|diagnos|prognos|therap|treatment|dose|dosage|drug|"
    r"medication|symptom|disease|syndrome|cancer|tumou?r|infection|vaccine|"
    r"trial|cohort|mortality|comorbid|adverse event|side effect|fda|ema|"
    r"prescription|oncology|cardiology|psychiatr|obesity|diabet|agonist|"
    r"placebo|randomi[sz]ed|contraindicat)\w*\b",
    re.IGNORECASE,
)

# ── financial ───────────────────────────────────────────────────────────────

_FINANCIAL_EXECUTE = re.compile(
    r"\b(?:buy|sell|short|long|place (?:a )?(?:trade|order)|execute (?:a )?(?:trade|order)|"
    r"open (?:a )?position|close (?:a )?position|transfer (?:funds|money)|"
    r"move (?:my )?(?:money|funds)|withdraw|deposit)\b"
    r"[^.!?\n]{0,40}\b(?:btc|eth|sol|bitcoin|ethereum|crypto|stock|shares?|"
    r"option|futures?|forex|gold|silver|portfolio|account)\b",
    re.IGNORECASE,
)

_FINANCIAL_TOPIC = re.compile(
    r"\b(?:market|trading|trader|portfolio|asset|equity|bond|yield|crypto|btc|"
    r"bitcoin|stock|option|futures?|forex|volatility|risk-adjusted|alpha|beta|"
    r"sharpe|drawdown|backtest|valuation)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SafetyDecision:
    verdict: str                    # allow | allow_with_disclaimer | refuse
    reasons: tuple[str, ...] = ()
    disclaimers: tuple[str, ...] = ()
    scope: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict != "refuse"

    def public(self) -> dict:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "disclaimers": list(self.disclaimers),
            "scope": self.scope,
        }


def screen(task: str, *, dataset: bool = False) -> SafetyDecision:
    """Decide what a scientific request may and may not produce.

    `dataset=True` means the user supplied data to analyse. Statistical
    analysis of real data is allowed; clinical interpretation of it is not.
    """
    text = str(task or "")
    reasons: list[str] = []
    disclaimers: list[str] = []

    if _CLINICAL_REFUSE.search(text):
        return SafetyDecision(
            "refuse",
            reasons=(
                "patient-specific diagnosis, prescribing or treatment is out of scope "
                "for a research assistant",
            ),
            disclaimers=(CLINICAL_DISCLAIMER,),
            scope="clinical",
        )

    if _FINANCIAL_EXECUTE.search(text):
        return SafetyDecision(
            "refuse",
            reasons=(
                "this asks for an order or a funds movement; the science agent never "
                "trades — the trading agent owns that path and keeps its approval gate",
            ),
            disclaimers=(FINANCIAL_DISCLAIMER,),
            scope="financial",
        )

    scope = "general"
    if _CLINICAL_TOPIC.search(text):
        scope = "clinical"
        disclaimers.append(CLINICAL_DISCLAIMER)
        reasons.append("clinical topic detected: output stays research/education only")
    if _FINANCIAL_TOPIC.search(text):
        scope = "financial" if scope == "general" else "clinical+financial"
        disclaimers.append(FINANCIAL_DISCLAIMER)
        reasons.append("financial topic detected: analysis only, no order path")

    if disclaimers:
        return SafetyDecision("allow_with_disclaimer", tuple(reasons),
                              tuple(dict.fromkeys(disclaimers)), scope)
    return SafetyDecision("allow", scope="general")


def disclaimers_for(scope: str) -> list[str]:
    if scope == "clinical":
        return [CLINICAL_DISCLAIMER]
    if scope == "financial":
        return [FINANCIAL_DISCLAIMER]
    if scope == "clinical+financial":
        return [CLINICAL_DISCLAIMER, FINANCIAL_DISCLAIMER]
    return []


INTEGRITY_RULES = """Research integrity rules for this run:
1. Every citation you write must be a numbered reference from the retrieved
   records supplied to you. If a claim has no record behind it, omit the claim
   or label it unverified — never invent a paper, author, DOI or number.
2. Separate what is established from what is hypothesised. A single study, a
   preprint, or a model's inference is not an established finding.
3. Report conflicting results as conflicting; do not average them into a
   consensus that the sources do not support.
4. State the limits of the evidence: sample sizes, populations studied, date of
   retrieval, and what the search did not cover.
5. Distinguish statistical significance from practical importance, and report
   effect sizes with uncertainty where they exist.
"""


def integrity_rules() -> str:
    return INTEGRITY_RULES
