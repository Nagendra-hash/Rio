"""
orchestrator/agents/legal/schema.py — the legal document shapes.

One value type per field so the agent, the vault and the tests share
exactly one vocabulary:

    DocumentMeta — identity: doc_id, title, kind, version, role in a
                   version chain (supersedes), source file, sha256,
                   page count.
    PageSpan     — where a citation points: page or section number,
                   heading, char offsets in the page text.
    Clause       — an extracted clause: id, doc_id, type, heading,
                   number, text, page, section, spans.
    validate_document_meta / validate_clause — never invent a field:
                   unknown/missing/malformed values are REPORTED.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── document kinds ──────────────────────────────────────────────────────────

_DOC_KINDS = {
    "contract": r"\bcontract|agreement\b",
    "lease": r"\blease|tenancy|rent\b",
    "nda": r"\bnda|non-?disclosure\b",
    "terms": r"\bterms\s+of\s+(service|use)|tos\b",
    "privacy": r"\bprivacy\s+policy\b",
    "eula": r"\beula|licence|license\s+agreement\b",
    "employment": r"\bemployment\s+agreement|offer\s+letter\b",
    "settlement": r"\bsettlement\s+agreement|deed\b",
    "policy": r"\bpolicy\b",
    "other": r"",
}

DEFAULT_KIND = "other"
MAX_TITLE = 200
MAX_TEXT_CHARS = 400_000          # whole-document cap before chunking
MAX_CLAUSES = 300
MAX_CLAUSE_TEXT = 8_000
MAX_PAGES = 2_000                 # absurd reading budget tallies below


def trim(text: str, cap: int = 700) -> str:
    """Whitespace-collapse + cap. The one text cleaner every module uses."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= cap else t[:cap - 1].rstrip() + "…"


def classify_kind(text: str) -> str:
    """A cheap deterministic kind guess from the first pages of text.

    Reports only; the vault accepts any kind the caller names explicitly.
    """
    head = " ".join(str(text or "").lower().split())[:6_000]
    scores: list[tuple[int, str]] = []
    for kind, pat in _DOC_KINDS.items():
        if not pat:
            continue
        rx = re.compile(pat, re.IGNORECASE)
        hits = min(3, len(rx.findall(head)))       # cap the weight
        if hits:
            scores.append((hits, kind))
    if not scores:
        return DEFAULT_KIND
    scores.sort(key=lambda kv: (-kv[0], kv[1]))     # most hits wins
    return scores[0][1]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class DocumentMeta:
    """Identity for one ingested legal document (and its version chain)."""
    doc_id: str
    title: str
    kind: str = DEFAULT_KIND
    version: int = 1                 # starts at 1; ingest bumps supersedes
    supersedes: str = ""             # doc_id of the older version, if any
    source_path: str = ""
    sha256: str = ""
    pages: int = 0
    chars: int = 0
    ingested_at: str = field(default_factory=now_iso)
    notes: str = ""


def validate_document_meta(meta) -> tuple[Optional[DocumentMeta], list[str]]:
    """Validate/normalize a DocumentMeta or dict. Returns (clean, errors).

    Never invents: a missing shape is an error, not a default document.
    """
    errors: list[str] = []
    if isinstance(meta, DocumentMeta):
        data: dict = asdict(meta)
    elif isinstance(meta, dict):
        data = dict(meta)
    else:
        return None, ["document meta must be a DocumentMeta or a dict"]
    doc_id = str(data.get("doc_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_:-]{4,80}", doc_id or ""):
        errors.append("doc_id missing or malformed")
        doc_id = ""
    title = str(data.get("title") or "").strip()[:MAX_TITLE]
    if not title:
        errors.append("title missing")
    kind = str(data.get("kind") or DEFAULT_KIND).strip().lower()
    if kind not in _DOC_KINDS:
        errors.append(f"unknown kind {kind!r} (kept, reading anyway)")
    try:
        version = max(1, int(data.get("version") or 1))
    except (TypeError, ValueError):
        version, errors = 1, errors + ["version not a number"]
    supersedes = str(data.get("supersedes") or "").strip()
    if supersedes and not re.fullmatch(r"[A-Za-z0-9_:-]{4,80}", supersedes):
        errors.append("supersedes doc_id malformed")
        supersedes = ""
    try:
        pages = max(0, int(data.get("pages") or 0))
    except (TypeError, ValueError):
        pages, errors = 0, errors + ["pages not a number"]
    try:
        chars = max(0, int(data.get("chars") or 0))
    except (TypeError, ValueError):
        chars, errors = 0, errors + ["chars not a number"]
    out = DocumentMeta(
        doc_id=doc_id, title=title, kind=kind if kind in _DOC_KINDS
        else DEFAULT_KIND, version=version, supersedes=supersedes,
        source_path=str(data.get("source_path") or "")[:500],
        sha256=str(data.get("sha256") or "")[:64],
        pages=pages, chars=chars,
        ingested_at=str(data.get("ingested_at") or now_iso()),
        notes=str(data.get("notes") or "")[:400],
    )
    return out, errors


@dataclass
class PageSpan:
    """Where a citation points, inside one document."""
    page: int                         # 1-based; 0 = "no page info"
    section: str = ""                 # section number, e.g. "8.2"
    heading: str = ""                 # nearest heading above the text
    start: int = 0                    # char offset into the page text
    end: int = 0


def _clean_span(raw) -> Optional[PageSpan]:
    if not isinstance(raw, dict):
        return None
    try:
        page = int(raw.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    if page < 0 or page > MAX_PAGES:
        page = 0
    return PageSpan(
        page=page,
        section=str(raw.get("section") or "").strip()[:30],
        heading=str(raw.get("heading") or "").strip()[:120],
        start=max(0, int(raw.get("start") or 0)),
        end=max(0, int(raw.get("end") or 0)),
    )


@dataclass
class Clause:
    """One extracted clause with its page/section citation."""
    doc_id: str
    clause_type: str
    n: int                            # 1-based order in the document
    text: str
    heading: str = ""
    number: str = ""                  # "8.2" style explicit numbering
    heading_page: int = 0
    spans: list[PageSpan] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.number}. {self.heading}" if self.number else self.heading

    def citation(self) -> str:
        if not self.spans:
            return f"{self.doc_id} (no page info)"
        s = self.spans[0]
        bits = [f"{self.doc_id}"]
        if s.page:
            bits.append(f"p.{s.page}")
        if s.section or self.number:
            bits.append(f"§{s.section or self.number}")
        return " ".join(bits)

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id, "clause_type": self.clause_type,
            "n": self.n, "text": self.text, "heading": self.heading,
            "number": self.number, "heading_page": self.heading_page,
            "spans": [asdict(s) for s in self.spans],
        }


def validate_clause(raw) -> tuple[Optional[Clause], list[str]]:
    """Validate/normalize a raw clause dict. Never invents content."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["clause must be a dict"]
    doc_id = str(raw.get("doc_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_:-]{4,80}", doc_id or ""):
        errors.append("doc_id missing or malformed")
    text = str(raw.get("text") or "").strip()
    if not text:
        errors.append("clause text is empty")
    if errors:
        return None, errors
    try:
        n = int(raw.get("n") or 0)
    except (TypeError, ValueError):
        n, errors = 0, errors + ["clause n not a number"]
    if n < 1 or n > MAX_CLAUSES:
        errors.append("clause n out of range")
    text = text[:MAX_CLAUSE_TEXT]
    spans = [s for s in (_clean_span(sp) for sp in
                         (raw.get("spans")
                          if isinstance(raw.get("spans"), list) else []))
             if s is not None]
    if errors:
        return None, errors
    return Clause(
        doc_id=doc_id,
        clause_type=str(raw.get("clause_type") or "other")[:40],
        n=n, text=text,
        heading=str(raw.get("heading") or "").strip()[:120],
        number=str(raw.get("number") or "").strip()[:30],
        heading_page=max(0, int(raw.get("heading_page") or 0)),
        spans=spans,
    ), []
