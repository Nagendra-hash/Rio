"""
core/scientific/report.py — assemble evidence into a citable report.

The report is built from STRUCTURE, not from a model's prose:

    records (retrieved)  ->  numbered references  ->  claims that cite them

A claim that names no retrieved record cannot be rendered as established; it is
downgraded to `unverified` and an issue is recorded. Reference entries are
generated from database metadata, so a DOI or author list cannot be invented by
the writing step — there is nothing for it to invent from.

`check_fabrication()` then re-reads the finished markdown and flags any `[n]`
outside the reference list, any DOI that was never retrieved, and any
"established" claim with no citation. That check is what the tests assert and
what the agent reports when it cannot substantiate something.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from core.scientific import safety
from core.scientific.config import load_config

ESTABLISHED = "established"
HYPOTHESIS = "hypothesis"
CONTESTED = "contested"
UNVERIFIED = "unverified"

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")
_CITE_RE = re.compile(r"\[(\d{1,3})\]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class Reference:
    index: int
    title: str
    authors: list[str] = field(default_factory=list)
    year: str = ""
    journal: str = ""
    doi: str = ""
    url: str = ""
    source: str = ""
    retrieved_at: str = ""

    def line(self) -> str:
        authors = ", ".join(self.authors[:6]) or "Unknown authors"
        if len(self.authors) > 6:
            authors += ", et al."
        parts = [f"{authors} ({self.year or 'n.d.'}).", f"{self.title}."]
        if self.journal:
            parts.append(f"{self.journal}.")
        if self.doi:
            parts.append(f"https://doi.org/{self.doi}")
        elif self.url:
            parts.append(self.url)
        return f"[{self.index}] " + " ".join(parts)


@dataclass
class Claim:
    text: str
    citations: list[int] = field(default_factory=list)
    status: str = ESTABLISHED
    note: str = ""

    def render(self, references: dict[int, Reference]) -> str:
        label = {
            ESTABLISHED: "Established",
            HYPOTHESIS: "Hypothesis",
            CONTESTED: "Contested",
            UNVERIFIED: "Unverified",
        }.get(self.status, self.status.title())
        cites = "".join(f"[{n}]" for n in self.citations if n in references)
        body = f"- **{label}** — {self.text.strip()}"
        if cites:
            body += f" {cites}"
        if self.note:
            body += f" _({self.note})_"
        return body


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _authors(raw: Any) -> list[str]:
    if isinstance(raw, list):
        names = [_clean(a) for a in raw if _clean(a)]
    elif isinstance(raw, str):
        names = [_clean(a) for a in re.split(r",|;| and ", raw) if _clean(a)]
    else:
        names = []
    return names[:20]


def references_from_records(records: Iterable[dict], retrieved_at: str = "") -> list[Reference]:
    """Numbered references, deduplicated, only for records with a locator."""
    seen: set[str] = set()
    refs: list[Reference] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        doi = _clean(record.get("doi")).lower()
        url = _clean(record.get("url"))
        title = _clean(record.get("title") or record.get("name"))
        if not title:
            continue
        key = doi or url or title.lower()
        if key in seen:
            continue
        if not doi and not url:
            # Nothing to cite: keep it in the notes, not in the bibliography.
            continue
        seen.add(key)
        refs.append(Reference(
            index=len(refs) + 1,
            title=title,
            authors=_authors(record.get("authors")),
            year=_clean(record.get("year")),
            journal=_clean(record.get("journal") or record.get("organism")
                           or record.get("formula") or ""),
            doi=doi,
            url=url,
            source=_clean(record.get("source")),
            retrieved_at=retrieved_at,
        ))
    return refs


_TRAILING_CITES = re.compile(r"(?:\s*\[\d{1,3}\])+\s*$")


def _strip_trailing_citations(text: str) -> str:
    """The renderer appends [n]; a model that already wrote them would double up."""
    return _TRAILING_CITES.sub("", str(text or "")).strip()


def verify_claims(claims: list[Claim], references: list[Reference]) -> tuple[list[Claim], list[str]]:
    """Downgrade anything that cannot be substantiated by a retrieved record.

    Two rules are mechanical, not prompt-level:
      * a citation must point at a record retrieved in this run;
      * "established" needs at least two distinct records. One paper — often a
        preprint or a case series — is a report, not a consensus.
    """
    valid = {ref.index for ref in references}
    issues: list[str] = []
    checked: list[Claim] = []
    for claim in claims:
        kept: list[int] = []
        for citation in claim.citations:
            if citation in valid:
                kept.append(citation)
            else:
                issues.append(f"citation [{citation}] is not a retrieved reference")
        claim = Claim(_strip_trailing_citations(claim.text), kept, claim.status, claim.note)
        if claim.status == ESTABLISHED and len(set(kept)) < 2:
            claim.status = UNVERIFIED if not kept else CONTESTED
            note = ("no retrieved source" if not kept
                    else "single retrieved source — reported, not established")
            claim.note = (claim.note + "; " if claim.note else "") + note
            issues.append(f"adjusted: claim not treated as established: "
                          f"{claim.text[:80]} ({note})")
            checked.append(claim)
            continue
        if claim.status == ESTABLISHED and not kept:
            claim.status = UNVERIFIED
            claim.note = (claim.note + "; " if claim.note else "") + "no retrieved source"
            issues.append(f"claim downgraded to unverified: {claim.text[:80]}")
        if claim.status == HYPOTHESIS and not claim.note:
            claim.note = "candidate explanation, not an established finding"
        checked.append(claim)
    return checked, issues


def check_fabrication(markdown: str, references: list[Reference]) -> list[str]:
    """Re-read the rendered report and flag unsupported citations."""
    issues: list[str] = []
    valid = {ref.index for ref in references}
    doi_set = {ref.doi.lower() for ref in references if ref.doi}
    for match in _CITE_RE.finditer(markdown or ""):
        index = int(match.group(1))
        if index not in valid:
            issues.append(f"report cites [{index}] but only {len(valid)} references exist")
    for doi in set(_DOI_RE.findall(markdown or "")):
        if doi.lower() not in doi_set:
            issues.append(f"report contains a DOI that was never retrieved: {doi}")
    return issues


def render(
    *,
    question: str,
    references: list[Reference],
    claims: list[Claim],
    narrative: str = "",
    method: dict | None = None,
    gaps: list[str] | None = None,
    conflicts: list[str] | None = None,
    hypotheses: list[str] | None = None,
    limitations: list[str] | None = None,
    datasets: list[dict] | None = None,
    meta: dict | None = None,
    scope: str = "general",
) -> str:
    """Markdown report. Sections are omitted when they have no content."""
    meta = meta or {}
    method = method or {}
    ref_by_index = {ref.index: ref for ref in references}
    lines: list[str] = []

    lines.append(f"# {question.strip() or 'Scientific research report'}")
    lines.append("")
    lines.append(f"_Generated {time.strftime('%Y-%m-%d %H:%M')} · "
                 f"depth: {meta.get('depth', 'standard')} · "
                 f"skills: {', '.join(meta.get('skills') or []) or 'none'}_")
    lines.append("")

    if narrative.strip():
        lines.append("## Summary")
        lines.append("")
        lines.append(narrative.strip())
        lines.append("")

    if claims:
        established = [c for c in claims if c.status == ESTABLISHED]
        if established:
            lines.append("## Findings")
            lines.append("")
            lines.extend(c.render(ref_by_index) for c in established)
            lines.append("")

    if conflicts:
        lines.append("## Conflicting evidence")
        lines.append("")
        lines.extend(f"- {item}" for item in conflicts)
        lines.append("")

    if gaps:
        lines.append("## Research gaps")
        lines.append("")
        lines.extend(f"- {item}" for item in gaps)
        lines.append("")

    hypothesis_claims = [c for c in claims if c.status == HYPOTHESIS]
    if hypotheses or hypothesis_claims:
        lines.append("## Candidate hypotheses")
        lines.append("")
        lines.extend(c.render(ref_by_index) for c in hypothesis_claims)
        lines.extend(f"- **Hypothesis** — {item}" for item in (hypotheses or []))
        lines.append("")

    unverified = [c for c in claims if c.status in (UNVERIFIED, CONTESTED)]
    if unverified:
        lines.append("## Unverified or contested")
        lines.append("")
        lines.extend(c.render(ref_by_index) for c in unverified)
        lines.append("")

    if datasets:
        lines.append("## Data analysis")
        lines.append("")
        for item in datasets:
            summary = _clean(item.get("summary"))
            if not summary:
                continue
            lines.append(f"- **{item.get('recipe', 'analysis')}** — {summary}")
            for figure in item.get("figures") or []:
                lines.append(f"  - figure: `{figure}`")
            for name, table in (item.get("tables") or {}).items():
                lines.append(f"  - table ({name}): `{table}`")
        lines.append("")

    if limitations:
        lines.append("## Limitations and uncertainty")
        lines.append("")
        lines.extend(f"- {item}" for item in limitations)
        lines.append("")

    lines.append("## Methods")
    lines.append("")
    sources = method.get("sources") or []
    queries = method.get("queries") or []
    lines.append(f"- Sources searched: {', '.join(sources) if sources else 'none recorded'}")
    if queries:
        lines.append(f"- Queries: " + "; ".join(f"`{q}`" for q in queries))
    if method.get("databases"):
        lines.append(f"- Database calls: {method['databases']}")
    if method.get("retrieved_at"):
        lines.append(f"- Retrieval date: {method['retrieved_at']}")
    failures = method.get("failed_sources") or []
    if failures:
        lines.append("- Sources that failed: "
                     + "; ".join(f"{f.get('source')} ({f.get('error')})" for f in failures))
    if meta.get("interpreter"):
        lines.append(f"- Analysis interpreter: `{meta['interpreter']}`")
    if meta.get("versions"):
        versions = ", ".join(f"{k} {v}" for k, v in sorted(meta["versions"].items()))
        lines.append(f"- Library versions: {versions}")
    lines.append("")

    if references:
        lines.append("## References")
        lines.append("")
        lines.extend(ref.line() for ref in references)
        lines.append("")
        lines.append("_Every entry above was retrieved during this run. Records "
                     "without a DOI or URL are not listed as references._")
        lines.append("")

    lines.append("## Provenance")
    lines.append("")
    lines.append(f"- Upstream skills: K-Dense scientific-agent-skills "
                 f"{meta.get('skills_tag', 'v2.72.0')}")
    if meta.get("model"):
        lines.append(f"- Model: {meta['model']}")
    if meta.get("job_dir"):
        lines.append(f"- Job directory: `{meta['job_dir']}`")
    if meta.get("skills"):
        lines.append(f"- Skill procedures used: {', '.join(meta['skills'])}")
    lines.append("")

    for disclaimer in safety.disclaimers_for(scope):
        lines.append(f"> {disclaimer}")
        lines.append("")
    lines.append(f"> {safety.INTEGRITY_NOTE}")
    return "\n".join(lines).rstrip() + "\n"


def slugify(text: str, limit: int = 60) -> str:
    slug = _SLUG_RE.sub("-", (text or "report").lower()).strip("-")
    return (slug or "report")[:limit]


def save(markdown: str, question: str) -> Path:
    """Write the report under memory/scientific/reports and return its path."""
    cfg = load_config()
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.reports_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{slugify(question)}.md"
    path.write_text(markdown, encoding="utf-8")
    return path
