"""
orchestrator/agents/legal/clauses.py — clause extraction + summarization.

Deterministic, offline, in code (no model in the decision path):

    CLAUSE_TYPES     the legal clause families this phase recognizes —
                     term/renewal, termination, liability, indemnity,
                     confidentiality, IP, payment, data protection,
                     dispute resolution (arbitration / governing law /
                     jurisdiction), assignment, non-compete/non-solicit,
                     notices, severability, force majeure, warranties,
                     insurance, amendment, dispute — 'other' if none.
    extract_clauses(doc) → list[Clause]: heading/number lines followed by
                     their rooflines; every clause keeps its PageSpan
                     (page + section + char offsets) so citations are
                     structural, not guessed.
    summarize_document(read) → SUMMARY: extractive section summary —
                     the two densest sentences per section, with a
                     one-line "what is this document" opener.
    summarize_clause(clause) → short extractive digest.

No LLM anywhere. Unsupported legal readings are structurally impossible:
the summaries ARE the document's own sentences, with page citations.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from orchestrator.agents.legal.pages import (Page, ReadResult, Section,
                                             build_sections)
from orchestrator.agents.legal.schema import (Clause, PageSpan, trim as _trim)

# ── clause families ──────────────────────────────────────────────────────────

CLAUSE_TYPES = (
    "term_renewal", "termination", "liability", "indemnity",
    "confidentiality", "ip", "payment", "data_protection",
    "dispute", "assignment", "restrictive_covenant", "notices",
    "severability", "force_majeure", "warranty", "insurance",
    "amendment", "compliance", "other",
)

_TYPE_WORDS: dict[str, tuple[str, ...]] = {
    "term_renewal": ("term of this agreement", "shall commence", "renew",
                     "auto-renew", "auto renew", "expiry", "initial term"),
    "termination": ("terminat", "breach of this agreement", "cease",
                    "wind up", "days' notice", "without cause",
                    "for convenience"),
    "liability": ("liabilit", "liable", "damages", "consequential",
                  "indirect losses", "limitation of liability",
                  " cap on ", "aggregate liability"),
    "indemnity": ("indemnif", "indemnit", "hold harmless",
                  "defend the", "make good the loss"),
    "confidentiality": ("confidential", "non-disclosure", "trade secret",
                        "keep secret", "disclose the"),
    "ip": ("intellectual property", "copyright", "trademark", "patent",
           "moral rights", "derivative works", "license grant",
           "licence grant", "ownership of"),
    "payment": ("payment", "fees", "invoice", "invoice", "charges",
                "price", "arrears", "late payment", "consideration"),
    "data_protection": ("personal data", "personal information",
                        "gdpr", "data protection", "privacy",
                        "data processing", "data breach"),
    "dispute": ("arbitrat", "dispute", "governing law", "jurisdiction",
                "venue", "mediation", "courts of", "laws of"),
    "assignment": ("assign", "novat", "transfer of this agreement",
                   "subcontract", "successors"),
    "restrictive_covenant": ("non-compete", "noncompete", "non-solicit",
                             "nonsolicit", "non-compet", "restraint of trade"),
    "notices": ("notice", "in writing", "registered post",
                "email address", "delivery of notice"),
    "severability": ("severab", "invalid", "unenforceable",
                     "provisions shall be severed"),
    "force_majeure": ("force majeure", "act of god", "acts of god",
                      "events beyond", "circumstances beyond"),
    "warranty": ("warrant", "warranties", "as is", "merchantability",
                 "fitness for a particular purpose", "disclaim"),
    "insurance": ("insurance", "insure", "policy of insurance",
                  "cover of", "insurer"),
    "amendment": ("amend", "variation", "supersede", "modify this",
                  "change this agreement"),
    "compliance": ("comply with", "compliance with", "applicable law",
                   "anti-bribery", "sanctions", "regulation"),
}


@dataclass
class Summary:
    """The extractive summary of one document."""
    kind: str
    overview: str                     # one-line "what is this"
    title_line: str = ""
    sections: list[dict] = field(default_factory=list)
    clauses_found: int = 0
    pages: int = 0

    def to_lines(self) -> list[str]:
        lines = [f"Document: {self.title_line or '(untitled)'}",
                 f"Kind: {self.kind} · pages: {self.pages} · "
                 f"clauses extracted: {self.clauses_found}",
                 "", self.overview]
        if self.sections:
            lines.append("")
            lines.append("Section summaries (each sentence from the "
                         "document, page cited):")
            for s in self.sections:
                head = f"§ {s.get('label')}" if s.get("label") else "§ —"
                p = s.get("page")
                lines.append(f"\n  {head} (starts p.{p})")
                for sent in s.get("sentences", []):
                    lines.append(f"    • {sent['text']} (p.{sent['page']})")
        return lines


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")
_HEADING_LINE = re.compile(
    r"^(?P<num>(\d{1,2})(?:\.(\d{1,2}))?(?:\.(\d{1,2}))?)\.?\s+"
    r"(?P<title>[A-Z][A-Za-z0-9 ,&()/-]{2,90})$")
_TITLE_LINE = re.compile(r"^[A-Z][A-Za-z0-9 ,&()/-]{2,80}$")


def _sentences(page: Page) -> list[tuple[int, str]]:
    """(offset, sentence) pairs for one page; offsets back-map to spans."""
    out: list[tuple[int, str]] = []
    start = 0
    text = page.text
    for m in _SENT_SPLIT.finditer(text):
        end = m.start() + 1
        chunk = text[start:end].strip()
        if len(chunk.split()) >= 6:
            out.append((start, chunk))
        start = end
    if start < len(text):
        chunk = text[start:].strip()
        if len(chunk.split()) >= 6:
            out.append((start, chunk))
    return out


def _densest(sents: list[tuple[int, str]], page_num: int,
             max_sents: int = 2) -> list[dict]:
    """The droitest extractive sentences of a section (with page cite)."""
    ranked = sorted(range(len(sents)),
                    key=lambda i: -len(sents[i][1].split()))
    out: list[dict] = []
    for i in ranked[:max_sents]:
        off, text = sents[i]
        out.append({"page": page_num, "start": off, "end": off + len(text),
                    "text": _trim(text, 300)})
    out.sort(key=lambda d: d["start"])          # document order
    return out


# ── structure/type detection ─────────────────────────────────────────────────

def _find_heading(lines: list[str], idx: int) -> tuple[str, str, int]:
    """The heading that governs line `idx`: nearest numbered heading above
    it, else the nearest short title line. Returns (number, title, delta)."""
    for back in range(idx, max(-1, idx - 40), -1):
        t = lines[back].strip()
        m = _HEADING_LINE.match(t)
        if m:
            return m.group("num"), m.group("title").strip(), idx - back
        if (_TITLE_LINE.match(t) and len(t.split()) <= 10
                and not t.endswith((".", ",", ";", ":"))):
            return "", t, idx - back
    return "", "", idx


def _clause_type(heading: str, body: str) -> str:
    hay = f"{heading} {heading.lower()} {body.lower()}"[:4_000]
    scores: list[tuple[int, str]] = []
    for ct, words in _TYPE_WORDS.items():
        hits = 0
        for w in words:
            if w in hay:
                hits += hay.count(w)
        if hits:
            scores.append((hits, ct))
    if not scores:
        return "other"
    scores.sort(key=lambda kv: (-kv[0], kv[1]))
    return scores[0][1]


def extract_clauses(read: ReadResult, *,
                    max_clauses: int = 120,
                    doc_id: str = "") -> list[Clause]:
    """Clause extraction with page + section spans on every clause.

    doc_id stamps the found clauses (optional — the vault cache fills it
    at ingest time so cached rows validate on the way back out)."""
    clauses: list[Clause] = []
    n = 0
    for page in read.pages:
        # Page-less formats (txt) report page 0 = "no page info"; clause
        # spans still need a page pointer for citations, so a page-less
        # document's clause bodies cite cover page 1 (the number line
        # "§3" stays the primary citation key regardless).
        span_page = page.num if page.num > 0 else 1
        offsets: list[int] = [0]
        lines = page.text.splitlines()
        for line in lines:
            offsets.append(offsets[-1] + len(line) + 1)
        idx_of_numbering = 0
        while idx_of_numbering < len(lines):
            line = lines[idx_of_numbering].strip()
            m = _HEADING_LINE.match(line)
            if not m:
                idx_of_numbering += 1
                continue
            body_lines: list[str] = []
            j = idx_of_numbering + 1
            while j < len(lines):
                t = lines[j].strip()
                if _HEADING_LINE.match(t):
                    break
                if t and len(t) > 40:
                    body_lines.append(t)
                elif t and len(body_lines) >= 1:
                    body_lines.append(t)
                else:
                    break
                j += 1
            body = " ".join(body_lines)
            if len(body.split()) < 12:
                idx_of_numbering = j
                continue
            n += 1
            if n > max_clauses:
                break
            number, title = m.group("num"), m.group("title").strip()
            start_line = idx_of_numbering + 1
            end_line = j
            start_off = offsets[start_line] if start_line < len(offsets) \
                else offsets[-1]
            end_off = offsets[end_line] if end_line < len(offsets) \
                else offsets[-1]
            span = PageSpan(page=span_page, section="" if "." not in number
                            else number.rsplit(".", 1)[0] or number,
                            heading=title, start=start_off, end=end_off)
            clause = Clause(
                doc_id=str(doc_id or ""),
                clause_type=_clause_type(title, body),
                n=n, text=_trim(body, 6_000), heading=title[:120],
                number=number, heading_page=page.num, spans=[span])
            clauses.append(clause)
            idx_of_numbering = j
        if n > max_clauses:
            break
    ordered = [c for c in clauses if len(c.text.split()) >= 12]
    for i, c in enumerate(ordered, 1):
        c.n = i
    return ordered


def summarize_document(read: ReadResult, *, per_section: int = 2,
                       max_sections: int = 40) -> Summary:
    """Extractive summary: opener + the two densest sentences of EACH
    section's own text (a one-pager summarizes differently per section,
    not by re-quoting the globally densest page sentences)."""
    sumry = Summary(kind=read.kind, overview="", pages=read.page_count,
                    title_line=_first_title(read))
    # opener: the meatiest sentence of the document's opening run
    if read.pages:
        opening = _sentences(read.pages[0])[:8]
        best = max(opening, key=lambda s: len(s[1].split()), default=None)
        if best:
            sumry.overview = _trim(best[1], 260)
    sections = build_sections(read.pages)
    seen_sents: set[str] = set()
    for sec in sections[:max_sections]:
        if not sec.text.strip():
            continue
        # summarize THE SECTION'S own text (page = where it starts;
        # a page-less txt cites cover page 1, same convention as spans)
        page_cite = sec.start_page if sec.start_page > 0 else 1
        sec_page = Page(num=page_cite, text=sec.text)
        sents = _densest(_sentences(sec_page), sec_page.num, per_section)
        # drop sentences a previous section already quoted verbatim
        sents = [s for s in sents
                 if s["text"][:80] not in seen_sents]
        if not sents:
            continue
        for s in sents:
            seen_sents.add(s["text"][:80])
        label = (f"{sec.num}. {sec.heading}" if sec.num else sec.heading)
        sumry.sections.append({
            "label": _trim(label, 120), "page": page_cite,
            "sentences": sents})
    sumry.clauses_found = len(extract_clauses(read))
    return sumry


def _first_title(read: ReadResult) -> str:
    if not read.pages:
        return ""
    for line in read.pages[0].text.splitlines():
        t = line.strip()
        if _TITLE_LINE.match(t) and len(t.split()) >= 2:
            return _trim(t, 120)
    return _trim(read.pages[0].text.splitlines()[0] if
                 read.pages[0].text else "", 120)


def summarize_clause(clause: Clause) -> str:
    """Short extractive digest of ONE clause (2 densest sentences)."""
    page = Page(num=clause.spans[0].page if clause.spans else 0,
                text=clause.text)
    sents = _densest(_sentences(page), clause.heading_page, max_sents=2)
    bits = []
    label = clause.citation()
    bits.append(label)
    for s in sents:
        bits.append(f"  • {s['text']} (p.{s['page']})")
    return "\n".join(bits)
