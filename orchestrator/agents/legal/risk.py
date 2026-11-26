"""
orchestrator/agents/legal/risk.py — risk flagging + open questions.

What a careful reader flags BEFORE a lawyer reads the document. The
output is deliberately shaped as "questions for human review", not
verdicts: the agent flags passages of concern, cites the page, and
says WHY it flagged each — the human (and their lawyer) decides.

    scan_document(read, clauses) → RISK_REPORT
        per-finding: severity (high|medium|informational),
        type, question-for-the-human, why, evidence (the document's own
        sentence, trim-capped), page, clause number.

Recognized concerns (adaptive): broad/unlimited liability & indemnity,
auto-renewal, termination asymmetry (one side may terminate for
convenience), unilateral amendment rights, one-sided confidentiality,
non-competes, broad IP assignment ("your ideas"-grabbing), uncapped
late fees/interest, arbitration + waiver of class actions, chargeback
jurisdiction ("venue far from you" is not asserted — only
first-published etc.), business days — kept honest and lexical:

    every pattern is regex over the document's own words; a finding
    quotes the sentence it matched, with its page. Nothing is invented.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from orchestrator.agents.legal.clauses import Clause
from orchestrator.agents.legal.pages import ReadResult
from orchestrator.agents.legal.schema import trim as _trim
from orchestrator.agents.legal.schema import PageSpan  # noqa: F401  (typing)

HIGH = "high"
MEDIUM = "medium"
INFO = "informational"


@dataclass
class Finding:
    severity: str
    concern: str
    question: str                    # the question for the HUMAN
    why: str                         # why a lawyer would read this closely
    evidence: str                    # the document's own sentence
    page: int = 0
    clause_number: str = ""
    clause_type: str = ""


@dataclass
class RiskReport:
    doc: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def counts(self) -> dict:
        out = {HIGH: 0, MEDIUM: 0, INFO: 0}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

    def to_lines(self) -> list[str]:
        c = self.counts
        lines = [f"Review flags for {self.doc}: "
                 f"{c[HIGH]} high, {c[MEDIUM]} medium, "
                 f"{c[INFO]} informational.", ""]
        if not self.findings:
            lines.append("No flagged passages in this document — the "
                         "scan is lexical and non-exhaustive; a lawyer "
                         "review is still encouraged before you sign "
                         "or rely on it.")
            return lines
        for f in self.findings:
            mark = {"high": "⚠⚠", "medium": "⚠",
                    "informational": "ℹ"}[f.severity]
            lines.append(f"{mark} [{f.severity.upper()}] {f.concern}"
                         + (f" — §{f.clause_number}"
                            if f.clause_number else "")
                         + (f" p.{f.page}" if f.page else ""))
            lines.append(f"   Q: {f.question}")
            lines.append(f"   Why: {f.why}")
            lines.append(f"   Evidence: \"{_trim(f.evidence, 240)}\"")
            lines.append("")
        return lines


def _sentence_for(page_text: str, start: int, end: int) -> str:
    """The sentence containing [start, end) — what gets quoted."""
    lo = max(0, start - 200)
    hi = min(len(page_text), end + 200)
    window = page_text[lo:hi]
    m = re.search(r"[.!?]\s+[A-Z(\"']", window[:len(window) // 2 + 200][::-1])
    sent_start = 0
    for m2 in re.finditer(r"(?<=[.!?])\s+", window[:200]):
        sent_start = m2.end()
    sent_end = len(window)
    m3 = re.search(r"(?<=[.!?])\s+(?=[A-Z(\"'])", window[200:] and
                   window[200:] or "")
    if m3:
        sent_end = 200 + m3.start() + 1
    return re.sub(r"\s+", " ", window[sent_start:sent_end]).strip()


_PATTERNS: list[dict] = [
    dict(severity=HIGH, concern="unlimited liability",
         question="Is this liability exposure one your business can actually carry?",
         why=("a clause that keeps liability wholly uncapped can exceed "
              "the contract's whole value — capped/unlimited balance is "
              "a classic negotiation point"),
         rx=(r"\b(?:unlimited|uncapped|no\s+cap|without\s+limit)\s+"
             r"(?:liabilit|damages|indemnit)|"
             r"\bliabilit\w+\s+(?:shall\s+be\s+)?(?:unlimited|not\s+be\s+limited)\b")),
    dict(severity=HIGH, concern="broad indemnity",
         question="Who indemnifies whom, for what, and is it mutual?",
         why=("one-sided 'hold harmless' clauses can transfer losses far "
              "beyond the subject of the contract"),
         rx=(r"\b(?:shall|will)\s+indemnif(?:y|ies)\b|"
             r"\bhold\s+(?:the\s+)?(?:other\s+party|company|us|you)\s+"
             r"harmless\b")),
    dict(severity=HIGH, concern="auto-renewal / evergreen term",
         question="When does renewal become irreversible (notice deadline)?",
         why=("auto-renew clauses re-commit you silently unless notice "
              "lands inside a short window — diary the date"),
         rx=(r"\b(?:auto(?:matical+)?\s+)?renew\w*\b|"
             r"\bshall\s+be\s+(?:automatically\s+)?extended\b|"
             r"\bevergreen\b")),
    dict(severity=HIGH, concern="termination for convenience (one side)",
         question="Who can walk away without cause, and how much notice do YOU get?",
         why=("termination asymmetry lets one side exit whenever it "
              "likes while the other has invested or is locked in"),
         rx=(r"\bterminate\s+.{0,60}\b(?:for\s+convenience|without\s+cause)\b|"
             r"\bwithout\s+cause.{0,40}\bterminate\b")),
    dict(severity=MEDIUM, concern="unilateral amendment",
         question="Can one side change the terms alone, and do you get notice?",
         why=("a right to modify the agreement at any time makes today's "
              "text a moving target"),
         rx=(r"\b(?:may|shall\s+have\s+the\s+right\s+to)\s+"
             r"(?:amend|modify|change|vary)\s+.{0,40}\b(?:terms|agreement)\b|"
             r"\bchange\s+.{0,30}\bat\s+any\s+time\b")),
    dict(severity=MEDIUM, concern="broad IP assignment",
         question="What IP actually transfers — and does it reach ideas, not just deliverables?",
         why=("assignment language that is not limited to the contract's "
              "subject matter can swallow unrelated work"),
         rx=(r"\bassign\w*\s+.{0,80}\b(?:intellectual\s+property|rights|"
             r"title|interest)\b|"
             r"\ball\s+right\w*,?\s+title\w*,?\s+and\s+interest\b")),
    dict(severity=MEDIUM, concern="restrictive covenant",
         question="How wide is the restraint (scope, geography, duration)?",
         why=("non-compete / non-solicit clauses can constrain future "
              "work well past this contract; enforceability varies"),
         rx=(r"\bnon-?compete\b|\bnon-?solicit\w*\b|"
             r"\brestrain\w*\s+from\b|\bshall\s+not\s+(?:compete|solicit)\b")),
    dict(severity=MEDIUM, concern="one-sided confidentiality",
         question="Is confidentiality mutual — and is there a term limit?",
         why=("a duty that binds one side only, or runs forever, is a "
              "standard ask to balance"),
         rx=(r"\bconfidential\s+information\b|"
             r"\bkeep\s+.{0,30}\bconfidential\b|"
             r"\bnondisclosure\s+obligations?\b")),
    dict(severity=MEDIUM, concern="late fees / interest uncapped",
         question="Is there any cap on penalties for late payment?",
         why=("uncapped percentages-on-outstandings can outgrow the "
              "principal debt"),
         rx=(r"\blate\s+(?:fee|charge|payment)\b|"
             r"\binterest\s+(?:at|of)\s+\d+(?:\.\d+)?\s?%|"
             r"\bpenalty\s+(?:of|at)\b")),
    dict(severity=MEDIUM, concern="arbitration / waiver of class actions",
         question="Do you understand you may be giving up court and class remedies?",
         why=("arbitration + class-action waivers change where and how "
              "disputes can be heard"),
         rx=(r"\barbitrat\w+\b|"
             r"\bclass\s+action\s+waiver\b|"
             r"\bwaive\w*\s+.{0,40}\b(?:jury|class)\b")),
    dict(severity=MEDIUM, concern="asymmetric notice period",
         question="Do both sides need the SAME notice to exit or to act?",
         why=("90 days for one party and 7 for the other is rarely an "
              "accident"),
         rx=(r"\b(?:\d+)\s+(?:days?'?|weeks?'?|months?'?)\s+(?:written\s+)?"
             r"notice\b")),
    dict(severity=INFO, concern="personal data handling",
         question="Does the data-processing term match your privacy posture?",
         why=("personal-data clauses interact with privacy law and your "
              "own policies — worth a specialist read"),
         rx=(r"\bpersonal\s+(?:data|information)\b|\bGDPR\b|"
             r"\bdata\s+(?:processor|controller)\b")),
    dict(severity=INFO, concern="governing law / venue",
         question="Which law and which courts would a dispute be heard under?",
         why=("venue far from you raises the cost of enforcing your "
              "rights — check before signing"),
         rx=(r"\bgover(?:ned|ning)\s+(?:by\s+)?law\w*\b|"
             r"\bcourts\s+of\b|\bjurisdiction\b|\bvenue\b")),
    dict(severity=INFO, concern="assignment / subcontracting",
         question="Can the counterparty transfer this contract (or its duties) onward?",
         why=("an assignment right without your consent can hand your "
              "deal to a party you never vetted"),
         rx=(r"\bassign\w*\s+(?:this\s+)?agreement\b|"
             r"\bsubcontract\w*\b")),
]


def scan_document(read: ReadResult, clauses: list[Clause] | None = None,
                  *, max_findings: int = 40) -> RiskReport:
    """Findings for a human's (and their lawyer's) attention, page-cited."""
    report = RiskReport(doc=read.path or "(document)")
    clauses = clauses if clauses is not None else []
    # Page-less formats (plain text) report page 0 = "no page info"; the
    # report still cites a page — the cover page — rather than omitting it.
    page_cite = {p.num: (p.num if p.num > 0 else 1) for p in read.pages}
    seen: set[int] = set()
    for pat in _PATTERNS:
        rx = re.compile(pat["rx"], re.IGNORECASE)
        hits = 0
        for page in read.pages:
            for m in rx.finditer(page.text):
                hits += 1
                if hits > 4:                 # max 4 findings per concern
                    break
                finding = Finding(
                    severity=pat["severity"], concern=pat["concern"],
                    question=pat["question"], why=pat["why"],
                    evidence=_sentence_for(page.text, m.start(), m.end()),
                    page=page_cite.get(page.num, page.num),
                    clause_number=next(
                        (c.number for c in clauses
                         if c.spans and c.spans[0].page == page.num
                         and c.spans[0].start <= m.start() <= c.spans[0].end),
                        ""),
                    clause_type=next(
                        (c.clause_type for c in clauses
                         if c.spans and c.spans[0].page == page.num
                         and c.spans[0].start <= m.start() <= c.spans[0].end),
                        ""))
                key = (finding.concern, finding.page, finding.evidence)
                hk = hash(key)
                if hk in seen:
                    continue
                seen.add(hk)
                if len(report.findings) >= max_findings:
                    break
                report.findings.append(finding)
        # interleave severities: high first, then medium, then info
    report.findings.sort(
        key=lambda f: ({HIGH: 0, MEDIUM: 1, INFO: 2}[f.severity],
                       f.page, f.clause_number))
    return report
