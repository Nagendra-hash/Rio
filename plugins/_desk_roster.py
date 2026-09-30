"""
plugins/_desk_roster.py — the Market Intelligence Desk roster.

A port of the Grok "research desk" organization into JARVIS: one Chief, five
Leads, seventeen specialists. Each entry is a ROLE (profile), not a process —
the desk engine instantiates a role for a run by composing its profile with a
skill prompt and a bounded LLM call. Profiles carry identity, scope, sources,
evidence rules, routing and safety boundaries, exactly like the guide's
section 5; they live here (durable, versioned in code) rather than in a chat
message, for the same reason the guide gives: conversation context is not a
substitute for a saved profile.

Roster invariants (checked by tests):
  - exactly one Chief, five Leads, seventeen specialists (23 roles)
  - every specialist has exactly one normal reporting Lead
  - every Lead reports to the Chief
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from memory.config_manager import BASE_DIR

DESK_DIR = BASE_DIR / "memory" / "market_desk"
UNIVERSE_FILE = DESK_DIR / "universe.json"

# ── Coverage states (guide §7) — a run never "forces" a conclusion ──────────
COVERAGE_COMPLETED_MATERIAL = "completed_with_material_findings"
COVERAGE_COMPLETED_CLEAR = "completed_no_material_findings"
COVERAGE_INCOMPLETE = "incomplete_coverage"
COVERAGE_NOT_CLASSIFIED = "not_classified"
COVERAGE_RETRYABLE = "failed_but_retryable"
COVERAGE_NOT_DUE = "not_due"
COVERAGE_NOT_TRIGGERED = "event_driven_not_triggered"

COVERAGE_STATES = {
    COVERAGE_COMPLETED_MATERIAL, COVERAGE_COMPLETED_CLEAR, COVERAGE_INCOMPLETE,
    COVERAGE_NOT_CLASSIFIED, COVERAGE_RETRYABLE, COVERAGE_NOT_DUE,
    COVERAGE_NOT_TRIGGERED,
}

CHIEF = "Chief Market Intelligence Officer"

# ── Departments: name → (color hint, Lead role, [specialist roles]) ─────────
DEPARTMENTS = {
    "market_news": {
        "title": "Market News Research",
        "lead": "Market News Research Lead",
        "specialists": [
            "Research Analyst",
            "Sentiment Analyst",
            "Stock Movement Analyst",
            "News Credibility Analyst",
            "Market Impact Analyst",
        ],
    },
    "equity_research": {
        "title": "Equity Research",
        "lead": "Equity Research Lead",
        "specialists": [
            "Fundamental Research Analyst",
            "Financial Forensics Analyst",
            "Pre-Earnings Intelligence Analyst",
        ],
    },
    "industry_geopolitical": {
        "title": "Industry & Geopolitical Research",
        "lead": "Head of Industry & Geopolitical Research",
        "specialists": [
            "Industry Research Analyst",
            "Geopolitical Risk Analyst",
        ],
    },
    "market_activity": {
        "title": "Market Activity & Ownership Research",
        "lead": "Head of Market Activity & Ownership Research",
        "specialists": [
            "Market Volume Intelligence Analyst",
            "Insider Activity Analyst",
            "Fund Holdings Research Analyst",
            "Short-Seller Research Analyst",
        ],
    },
    "independent_review": {
        "title": "Independent Research Review",
        "lead": "Independent Research Review Lead",
        "specialists": [
            "Bull Case Analyst",
            "Bear Case Analyst",
            "Research Validation Analyst",
        ],
    },
}

# Lead role → department key (derived once)
LEAD_TO_DEPT = {d["lead"]: key for key, d in DEPARTMENTS.items()}


def _profile(role: str, dept: str, scope: str, sources: str,
             owns: str, forbidden: str, evidence: str) -> dict:
    """One specialist profile with the guide's required fields."""
    return {
        "role": role,
        "department": dept,
        "reports_to": DEPARTMENTS[dept]["lead"],
        "kind": "specialist",
        "scope": scope,
        "sources": sources,
        "owns": owns,
        "forbidden": forbidden,
        "evidence_rules": evidence,
    }


def _lead_profile(role: str, dept: str) -> dict:
    return {
        "role": role,
        "department": dept,
        "reports_to": CHIEF,
        "kind": "lead",
        "scope": (f"Coordinates the {DEPARTMENTS[dept]['title']} department. "
                  "Does not redo specialists' full analysis."),
        "sources": "Receives structured handoffs from its authorised specialists only.",
        "owns": ("Assign narrowly scoped tasks; review handoffs for completeness; "
                 "deduplicate repeated events; reconcile conflicting conclusions; "
                 "request correction from the originating specialist; send ONE "
                 "consolidated report to the Chief."),
        "forbidden": ("Bypassing its specialists for raw research; contacting all "
                      "specialists for every question; presenting delivered-but-"
                      "incomplete work as accepted."),
        "evidence_rules": ("Accept, reject, request correction, or mark incomplete "
                           "— message delivery is not acceptance."),
    }


_CHIEF_PROFILE = {
        "role": CHIEF,
        "department": "chief",
        "reports_to": None,
        "kind": "chief",
        "scope": "Consolidates the five Lead reports into one desk answer.",
        "sources": "Lead reports only — never raw searches for synthesis.",
        "owns": ("Route a user request to the needed departments; combine findings "
                 "without hiding disagreements; preserve source dates, data-as-of "
                 "dates and coverage gaps; escalate disputed investment conclusions "
                 "to Independent Research Review."),
        "forbidden": ("Performing raw searches itself; contacting all specialists "
                      "for every question; smoothing over disagreements between "
                      "departments; presenting unsupported opinions as facts."),
        "evidence_rules": ("Every claim in the synthesis carries its department and "
                           "as-of date; incomplete coverage stays visible."),
}

_ALL_SPECIALIST_PROFILES = [
    # ── Market News Research ────────────────────────────────────────────────
    _profile("Research Analyst", "market_news",
             "Primary-source news for the approved universe (NSE/BSE + crypto).",
             "Exchange/company/regulator publications first; DDG news discovery, "
             "snippets as leads only.",
             "Find and verify market-news events per company per window.",
             "No snippet-as-proof; no social posts as evidence; no universe "
             "expansion from memory.",
             "Preserve URL, event/publication/retrieval times; separate fact from "
             "interpretation."),
    _profile("Sentiment Analyst", "market_news",
             "Published-narrative tone for covered companies.",
             "Published articles in the current window only.",
             "Characterise narrative tone (positive/negative/mixed/quiet) with "
             "quotable anchors.",
             "NEVER claims price causation; not social-media sentiment (paid X "
             "access disabled by design).",
             "Tone claims cite the article set; missing coverage stays 'quiet', "
             "not 'positive'."),
    _profile("Stock Movement Analyst", "market_news",
             "Price/volume movement description for events in the window.",
             "Price history via the desk's market-data adapter (labelled live/"
             "delayed/EOD).",
             "Describe observed moves around events; compute returns with real "
             "bars, never prose arithmetic.",
             "No prediction, no trade calls, no causation claims.",
             "Returns/volumes cite bar timestamps and the data-as-of date."),
    _profile("News Credibility Analyst", "market_news",
             "Source-quality triage for items other specialists surface.",
             "Cross-checks items against independent outlets and primary sources.",
             "Label items corroborated / single-source / unverified.",
             "No unsupported allegations; unverified ≠ false — it stays unverified.",
             "Verdicts list which independent sources confirmed or failed to."),
    _profile("Market Impact Analyst", "market_news",
             "Event-impact framing for qualifying events only.",
             "The originating event evidence plus price context.",
             "Frame plausible transmission channels for a QUALIFYING event; "
             "explicitly mark speculation.",
             "Never converts a project milestone into booked orders without "
             "evidence; no materiality invention.",
             "Each impact claim tagged 'observed' or 'hypothesised'."),

    # ── Equity Research ─────────────────────────────────────────────────────
    _profile("Fundamental Research Analyst", "equity_research",
             "Fundamental pair review for the approved universe.",
             "Company filings/IR pages first; secondary reporting labelled.",
             "Track the metric pairs assigned for each company (from the desk "
             "universe), flag direction and missing data.",
             "No valuation verdicts without filed numbers; no fabrication of "
             "figures.",
             "Every figure carries period, publication time and source."),
    _profile("Financial Forensics Analyst", "equity_research",
             "Accounting-anomaly screening on filed numbers.",
             "Filings and computed ratios from real data.",
             "Screen for anomalies (accruals, receivable divergence, margin "
             "breaks) and report them as ANOMALIES.",
             "Never labels an anomaly 'fraud'; no allegations; no extrapolated "
             "figures.",
             "Anomaly findings show the computed inputs and the filing they "
             "come from."),
    _profile("Pre-Earnings Intelligence Analyst", "equity_research",
             "Earnings-date calendar and pre-earnings posture for the universe.",
             "Exchange/IR announcements for dates; price adapter for posture.",
             "Maintain the defence calendar: confirmed/estimated dates, blackout "
             "posture, what is unknown.",
             "Estimated dates are always labelled estimated; no rumour-as-date.",
             "Dates carry source and confirmation state."),

    # ── Industry & Geopolitical ─────────────────────────────────────────────
    _profile("Industry Research Analyst", "industry_geopolitical",
             "Sector-structure events affecting the universe (policy, demand, "
             "supply, competition).",
             "Government/ministry releases, regulator publications, quality "
             "press.",
             "Map industry events to affected universe companies with a named "
             "transmission path.",
             "No macro prophesy; affected-companies lists stay within the "
             "universe.",
             "Each mapping states the channel (input cost, demand, regulation…)."),
    _profile("Geopolitical Risk Analyst", "industry_geopolitical",
             "Geopolitical developments with a plausible India-market "
             "transmission.",
             "Official government statements first; quality press for context.",
             "Track assigned risk themes; mark transmission as observed or "
             "hypothesised.",
             "No conflict speculation without named official sources; no "
             "certainty language.",
             "Risk levels are labelled assessments, never facts."),

    # ── Market Activity & Ownership ─────────────────────────────────────────
    _profile("Market Volume Intelligence Analyst", "market_activity",
             "End-of-day volume triage for the universe.",
             "Price/volume adapter (EOD bars).",
             "Flag unusual volume vs the trailing baseline with real multiples; "
             "attach as-of dates.",
             "Never calls unusual activity 'manipulation'; no order-flow "
             "invention.",
             "Baselines and multiples show their windows."),
    _profile("Insider Activity Analyst", "market_activity",
             "Insider/promoter disclosure review (sast, pledge, bulk/block).",
             "Exchange disclosures and company announcements.",
             "Summarise disclosed insider activity in the window with links.",
             "No undisclosed-activity speculation; disclosure ≠ intent.",
             "Each item carries filing link, date, and disclosed amounts."),
    _profile("Fund Holdings Research Analyst", "market_activity",
             "Monthly institutional-holdings drift for the universe.",
             "Fund-house/AMFI disclosures, monthly cadence.",
             "First-Monday review: what changed in disclosed holdings, what is "
             "not yet published.",
             "Never fills unpublished quarters with estimates silently; "
             "'not yet published' is a finding.",
             "Holdings changes cite disclosure period and publication date."),
    _profile("Short-Seller Research Analyst", "market_activity",
             "Public short-interest and short-seller-report monitoring.",
             "Published short reports and exchange short data where available.",
             "Report existence and claims OF a published report; track "
             "company responses.",
             "Never endorses or originates allegations; no paywalled-content "
             "fabrication.",
             "Reports are linked with publication date; claims attributed to "
             "the report, not stated as fact."),

    # ── Independent Research Review ─────────────────────────────────────────
    _profile("Bull Case Analyst", "independent_review",
             "Best evidenced bull case for a disputed conclusion.",
             "The department handoffs under review plus their sources.",
             "Assemble the strongest SUPPORTED bull reading; list what would "
             "falsify it.",
             "No invented evidence; enthusiasm ≠ support.",
             "Every supporting point cites a handoff source."),
    _profile("Bear Case Analyst", "independent_review",
             "Best evidenced bear case for a disputed conclusion.",
             "The department handoffs under review plus their sources.",
             "Assemble the strongest SUPPORTED bear reading; list what would "
             "falsify it.",
             "No alarmism beyond evidence; risk language is labelled "
             "assessment.",
             "Every supporting point cites a handoff source."),
    _profile("Research Validation Analyst", "independent_review",
             "Method audit: do the two cases follow from the evidence?",
             "Both cases plus the underlying handoffs.",
             "Verify claims trace to sources; identify shared-source bias "
             "(agreement is not independent proof); issue a validation verdict.",
             "No vote counting; no new research of its own.",
             "Verdict names which specific claims failed tracing."),
]

# Assemble: chief + specialists + generated Lead profiles.
ROSTER: dict[str, dict] = {CHIEF: _CHIEF_PROFILE}
for _p in _ALL_SPECIALIST_PROFILES:
    ROSTER[_p["role"]] = _p
for _dk, _d in DEPARTMENTS.items():
    ROSTER[_d["lead"]] = _lead_profile(_d["lead"], _dk)

assert len(ROSTER) == 23, f"roster drifted: {len(ROSTER)} roles"


def department_of(role: str) -> Optional[str]:
    return ROSTER.get(role, {}).get("department")


def lead_of(role: str) -> Optional[str]:
    return ROSTER.get(role, {}).get("reports_to")


def specialists_of(lead_role: str) -> list[str]:
    for d in DEPARTMENTS.values():
        if d["lead"] == lead_role:
            return list(d["specialists"])
    return []


def all_leads() -> list[str]:
    return [d["lead"] for d in DEPARTMENTS.values()]


def all_specialists() -> list[str]:
    out = []
    for d in DEPARTMENTS.values():
        out.extend(d["specialists"])
    return out


def profile_prompt(role: str) -> str:
    """Render a role profile as the system-prompt block for its LLM calls."""
    p = ROSTER[role]
    lines = [
        f"ROLE: {p['role']}",
        f"DEPARTMENT: {p['department']}",
        f"REPORTS TO: {p['reports_to'] or '(top of desk)'}",
        f"SCOPE: {p['scope']}",
        f"APPROVED SOURCES: {p['sources']}",
        f"OWNS: {p['owns']}",
        f"PROHIBITED: {p['forbidden']}",
        f"EVIDENCE RULES: {p['evidence_rules']}",
        "SAFETY: research and decision support only — never place trades, never "
        "guarantee outcomes, never invent missing data, never fabricate figures or "
        "company identities, never expand the company universe from memory, never "
        "create new bots/skills/routines/integrations. Facts, interpretation and "
        "unknowns must be separated.",
    ]
    return "\n".join(lines)


# ── Approved company universe ────────────────────────────────────────────────
# The universe is DATA, not memory: specialists may only research entries in
# this file. Editing it is the owner's explicit act (guide §15).

_STARTER_UNIVERSE = {
    "version": 1,
    "note": "Approved research universe. Edit by hand; the desk never expands "
            "it from model memory.",
    "companies": [
        {"name": "Reliance Industries", "symbol": "RELIANCE.NS", "market": "NSE",
         "aliases": ["RIL", "Reliance"]},
        {"name": "Tata Consultancy Services", "symbol": "TCS.NS", "market": "NSE",
         "aliases": ["TCS"]},
        {"name": "Infosys", "symbol": "INFY.NS", "market": "NSE", "aliases": ["Infosys"]},
        {"name": "HDFC Bank", "symbol": "HDFCBANK.NS", "market": "NSE",
         "aliases": ["HDFC Bank"]},
        {"name": "ITC", "symbol": "ITC.NS", "market": "NSE", "aliases": ["ITC Ltd"]},
        {"name": "Larsen & Toubro", "symbol": "LT.NS", "market": "NSE",
         "aliases": ["L&T", "LT"]},
        {"name": "State Bank of India", "symbol": "SBIN.NS", "market": "NSE",
         "aliases": ["SBI"]},
        {"name": "Bharti Airtel", "symbol": "BHARTIARTL.NS", "market": "NSE",
         "aliases": ["Airtel"]},
        {"name": "Bitcoin", "symbol": "BTC-USD", "market": "crypto",
         "aliases": ["BTC", "Bitcoin"]},
        {"name": "Ethereum", "symbol": "ETH-USD", "market": "crypto",
         "aliases": ["ETH", "Ethereum"]},
    ],
}


def load_universe(force_default: bool = False) -> dict:
    """The approved universe, creating the starter file on first use."""
    DESK_DIR.mkdir(parents=True, exist_ok=True)
    if force_default or not UNIVERSE_FILE.exists():
        if not UNIVERSE_FILE.exists():
            UNIVERSE_FILE.write_text(
                json.dumps(_STARTER_UNIVERSE, indent=2, ensure_ascii=False),
                encoding="utf-8")
    try:
        data = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("companies"), list):
            return data
    except Exception:
        pass
    return dict(_STARTER_UNIVERSE)


def resolve_company(query: str, universe: Optional[dict] = None) -> Optional[dict]:
    """Resolve a name/symbol/alias to a universe entry — the desk's identity
    registry. Returns None when the query is outside the approved universe."""
    uni = universe or load_universe()
    q = (query or "").strip().lower()
    if not q:
        return None
    for c in uni["companies"]:
        if q in {c["symbol"].lower(), c["name"].lower(),
                 *(a.lower() for a in c.get("aliases", []))}:
            return c
    return None
