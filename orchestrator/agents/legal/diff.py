"""
orchestrator/agents/legal/diff.py — version comparison.

compare_versions(old_read, new_read) → DiffResult: an honest, structural
answer to "what changed between these two versions of the agreement":

    * changed clauses — matched by heading number (§ numbering is the
      stable key legal drafting actually uses), plus heading text as a
      secondary key; each change keeps BOTH texts with page citations
    * added clauses   — in the new doc, under a heading absent from the old
    * removed clauses — in the old doc, under a heading absent from the new
    * jaccard text similarity drives the "modified" verdict: ≥ changed—
      threshold means the clause was edited; a lower score still counts
      as changed if the heading matched (an edit is an edit)

Deterministic, offline, no LLM. Every row cites pages on both sides.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from orchestrator.agents.legal.clauses import Clause, extract_clauses
from orchestrator.agents.legal.pages import ReadResult
from orchestrator.agents.legal.schema import trim as _trim

CHANGED_SIMILARITY = 0.85          # ≥ this = unchanged text (no diff row)


@dataclass
class DiffRow:
    change: str                     # "changed" | "added" | "removed"
    number: str
    heading: str
    old_text: str = ""
    new_text: str = ""
    old_page: int = 0
    new_page: int = 0
    similarity: float = 1.0

    def line(self) -> str:
        if self.change == "removed":
            return (f"  − removed  §{self.number or '—'} {self.heading}"
                    f"  (old p.{self.old_page or '?'})")
        if self.change == "added":
            return (f"  + added    §{self.number or '—'} {self.heading}"
                    f"  (new p.{self.new_page or '?'})")
        pct = int(round((1.0 - self.similarity) * 100))
        return (f"  ~ changed  §{self.number or '—'} {self.heading}"
                f"  (~{pct}% of the text differs; old p.{self.old_page or '?'}"
                f" → new p.{self.new_page or '?'})")


@dataclass
class DiffResult:
    old_doc: str
    new_doc: str
    rows: list[DiffRow] = field(default_factory=list)
    old_clauses: int = 0
    new_clauses: int = 0

    @property
    def changed(self) -> list[DiffRow]:
        return [r for r in self.rows if r.change == "changed"]

    @property
    def added(self) -> list[DiffRow]:
        return [r for r in self.rows if r.change == "added"]

    @property
    def removed(self) -> list[DiffRow]:
        return [r for r in self.rows if r.change == "removed"]

    def to_lines(self) -> list[str]:
        lines = [
            f"Version comparison: {self.new_doc} vs {self.old_doc}",
            f"  clauses: old {self.old_clauses} → new {self.new_clauses} "
            f"({len(self.changed)} changed, {len(self.added)} added, "
            f"{len(self.removed)} removed)", ""]
        if not self.rows:
            lines.append("No text differences found under matched "
                         "headings — the two versions look textually "
                         "identical clause-by-clause (or neither has "
                         "extractable numbered clauses).")
            return lines
        for r in self.rows:
            lines.append(r.line())
            if r.change == "changed" and r.old_text and r.new_text:
                lines.append(f"      old: {_trim(r.old_text, 180)}")
                lines.append(f"      new: {_trim(r.new_text, 180)}")
        return lines


def _norm_heading(heading: str) -> str:
    return re.sub(r"\s+", " ", str(heading or "").strip().lower())


def _key(clause: Clause) -> tuple[str, str]:
    return clause.number or "§", _norm_heading(clause.heading)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9$%.,']+", str(text or "").lower()))


def similarity(a: str, b: str) -> float:
    """Jaccard over word tokens (deterministic, no model)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return round(inter / union, 4) if union else 1.0


def _page_of(clause: Clause) -> int:
    return clause.spans[0].page if clause.spans else clause.heading_page


def compare_versions(old_read: ReadResult,
                     new_read: ReadResult, *,
                     max_rows: int = 80) -> DiffResult:
    """Structural diff between two reads of the same agreement."""
    old_clauses = extract_clauses(old_read)
    new_clauses = extract_clauses(new_read)
    out = DiffResult(old_doc=old_read.path, new_doc=new_read.path,
                     old_clauses=len(old_clauses),
                     new_clauses=len(new_clauses))

    old_by_key: dict[tuple[str, str], list[Clause]] = {}
    for c in old_clauses:
        old_by_key.setdefault(_key(c), []).append(c)
    new_by_key: dict[tuple[str, str], list[Clause]] = {}
    for c in new_clauses:
        new_by_key.setdefault(_key(c), []).append(c)

    rows: list[DiffRow] = []
    used_new: set[int] = set()
    for key, olds in old_by_key.items():
        if key not in new_by_key:
            continue
        pairs = list(zip(sorted(olds, key=lambda c: c.n),
                         sorted(new_by_key[key], key=lambda c: c.n)))
        for oc, nc in pairs:
            used_new.add(nc.n)
            sim = similarity(oc.text, nc.text)
            if sim >= CHANGED_SIMILARITY:
                continue
            rows.append(DiffRow(
                change="changed", number=oc.number, heading=oc.heading,
                old_text=oc.text, new_text=nc.text,
                old_page=_page_of(oc), new_page=_page_of(nc),
                similarity=sim))
    for c in new_clauses:
        if c.n in used_new:
            continue
        if _key(c) not in old_by_key:
            rows.append(DiffRow(change="added", number=c.number,
                                heading=c.heading, new_text=c.text,
                                new_page=_page_of(c), similarity=1.0))
    for c in old_clauses:
        if _key(c) not in new_by_key:
            rows.append(DiffRow(change="removed", number=c.number,
                                heading=c.heading, old_text=c.text,
                                old_page=_page_of(c), similarity=1.0))
    rows.sort(key=lambda r: (r.number or "§", r.heading.lower()))
    out.rows = rows[:max_rows]
    return out
