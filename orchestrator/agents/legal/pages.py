"""
orchestrator/agents/legal/pages.py — read legal PDFs (and plain text).

The reading plumbing the agent and the pipeline share:

    read_pages(path)     → list[Page]: one entry per page with the
                           page's clean text (pdfplumber → PyPDF2 →
                           binary-sniff floor, exactly the core/rag
                           extraction ladder — no new dependency).
    Page.num             1-based page number; page 0 = whole-text file.
    build_sections(pages)→ sections: numbered/heading-bounded chunks,
                           each carrying the page it starts on — the
                           p.N §M attribution citations point at.
    doc_hash(path)       → sha256 (ingest dedupe / chain of custody).
    guess_kind(path)     → kind sniff from the first bytes/pages.

NO network, NO LLM. Never raises: a PDF that cannot be read yields an
honest empty/failed report the caller can show.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.agents.legal.schema import MAX_PAGES, classify_kind

MAX_CHARS_PER_PAGE = 60_000        # a page bomb does not eat memory

_PAGE_STRUCT = re.compile(
    r"^(?P<num>\d{1,3}(?:\.\d{1,3}){0,3})\.?\s+(?P<txt>\S.{2,120})$")


@dataclass
class Page:
    num: int                        # 1-based; 0 = a page-less text file
    text: str


@dataclass
class Section:
    """A heading/number-bounded run of pages with page attribution."""
    num: str                        # "8" / "8.2" / "" for an untitled run
    heading: str
    start_page: int                 # page the section starts on (1-based)
    start_char: int                 # offset in that page
    text: str                       # full section text (pages concatenated)


@dataclass
class ReadResult:
    ok: bool
    error: str = ""
    path: str = ""
    pages: list[Page] = field(default_factory=list)
    sha256: str = ""
    kind: str = "other"

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)


_SECTION_NOISE = re.compile(
    r"^(?:page\s+\d+(?:\s+of\s+\d+)?|\d+\s*\|\s*|\f+)$", re.IGNORECASE)


def _clean_page_text(raw: str) -> str:
    """Strip page furniture (footers, form feeds) a citation never needs."""
    lines = []
    for line in str(raw or "").replace("\x0c", "\n").splitlines():
        t = line.strip()
        if not t or _SECTION_NOISE.match(t):
            continue
        lines.append(t)
    return "\n".join(lines)[:MAX_CHARS_PER_PAGE]


def extract_pages(path: str | Path) -> list[str]:
    """Per-page raw text. pdfplumber → PyPDF2 → [] (the core/rag ladder).
    Own copy on purpose: the legal agent must not depend on rag internals
    and must report a failed extraction, not fall into a KB ingest."""
    p = Path(path)
    pages: list[str] = []
    try:
        import pdfplumber
        with pdfplumber.open(str(p)) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
        if any(x.strip() for x in pages):
            return pages[:MAX_PAGES]
    except Exception:
        pages = []
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(str(p))
        pages = [(pg.extract_text() or "") for pg in reader.pages]
        if any(x.strip() for x in pages):
            return pages[:MAX_PAGES]
    except Exception:
        pages = []
    return []


_PLAIN_SUFFIXES = {".txt", ".md", ".log", ".csv"}
_DOCX_SUFFIXES = {".docx"}


def read_pages(path: str | Path) -> ReadResult:
    """Page-wise text of a legal document. Never raises."""
    p = Path(path).expanduser()
    res = ReadResult(ok=False, path=str(p))
    if not p.exists() or not p.is_file():
        res.error = f"no file at {p}"
        return res
    try:
        res.sha256 = _sha256(p)
    except Exception:
        res.sha256 = ""
    suffix = p.suffix.lower()
    if suffix == ".pdf" or _sniffs_pdf(p):
        raw_pages = extract_pages(p)
        texts = [t for t in (_clean_page_text(x) for x in raw_pages)]
        if not any(t.strip() for t in texts):
            res.error = ("no extractable text — the PDF may be scanned; "
                         "OCR it first and feed the text file")
            return res
        res.pages = [Page(num=i, text=t) for i, t in
                     enumerate([t for t in texts if t.strip()], 1)]
        res.ok = True
        res.kind = classify_kind(res.full_text[:6_000])
        return res
    if suffix in _PLAIN_SUFFIXES:
        try:
            text = _clean_page_text(p.read_text(encoding="utf-8",
                                                errors="replace"))
        except Exception as exc:
            res.error = f"unreadable text file: {exc}"
            return res
        if not text.strip():
            res.error = "file is empty"
            return res
        res.pages = [Page(num=0, text=text)]    # a text file has no pages
        res.ok = True
        res.kind = classify_kind(text[:6_000])
        return res
    if suffix in _DOCX_SUFFIXES:
        try:
            from docx import Document
            doc = Document(str(p))
        except Exception:
            res.error = "python-docx could not read this file"
            return res
        parags = [(pa.text or "").strip() for pa in doc.paragraphs]
        page, buf, size = [], [], 0
        for para in parags:
            if not para:
                continue
            buf.append(para)
            size += len(para)
            if size >= 6_000:
                page.append(_clean_page_text("\n".join(buf)))
                buf, size = [], 0
        if buf:
            page.append(_clean_page_text("\n".join(buf)))
        if not any(x.strip() for x in page):
            res.error = "no readable paragraphs in the docx"
            return res
        res.pages = [Page(num=i, text=t) for i, t in enumerate(page, 1)]
        res.ok = True
        res.kind = classify_kind(" ".join(parags)[:6_000])
        return res
    res.error = f"unsupported file type {suffix!r} — use PDF, txt/md/log/csv or docx"
    return res


def _sniffs_pdf(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == b"%PDF-"
    except Exception:
        return False


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def guess_kind(path: str | Path, read: ReadResult | None = None) -> str:
    if read is not None and read.ok:
        return read.kind
    return classify_kind(Path(path).stem)


# ── sections ─────────────────────────────────────────────────────────────────

def build_sections(pages: list[Page]) -> list[Section]:
    """Heading-bounded runs with the page each starts on.

    Recognition is deliberately conservative — a heading is:
      * an explicit number line ("8.2 Termination") starting a numbered
        section, or
      * a SHORT (≤ 80 chars) standalone title line directly followed by
        a long body line (≥ 120 chars).
    Anything else is body text. Sections are only an aid to citation;
    clause extraction does not depend on them.
    """
    sections: list[Section] = []
    cur_num, cur_heading, cur_start_page, cur_start_char = "", "", 0, 0
    cur_lines: list[str] = []

    def _flush(page_no: int, char_at: int) -> None:
        nonlocal cur_lines
        body = "\n".join(cur_lines).strip()
        if cur_lines and (cur_heading or cur_num or body):
            sections.append(Section(
                num=cur_num, heading=cur_heading,
                start_page=cur_start_page or page_no,
                start_char=cur_start_char or char_at,
                text=body[:100_000]))
        cur_lines = []

    for page in pages:
        offset = 0
        lines = page.text.splitlines()
        for li, line in enumerate(lines):
            t = line.strip()
            nxt = lines[li + 1].strip() if li + 1 < len(lines) else ""
            m = _PAGE_STRUCT.match(t) if t else None
            is_numbered = bool(m and m.group("txt"))
            is_titled = (not is_numbered and t and len(t) <= 80
                         and len(nxt) >= 120
                         and not t.endswith((".", ",", ";", ":")))
            if is_numbered or is_titled:
                _flush(page.num, offset)
                cur_num = (m.group("num") if is_numbered else
                           (m.group("num") if is_numbered else ""))
                cur_heading = (m.group("txt") if is_numbered else t)
                cur_start_page, cur_start_char = page.num, offset
                cur_lines = [t]
            else:
                if t:
                    cur_lines.append(t)
            offset += len(line) + 1
        _flush(page.num, offset)
        cur_num, cur_heading = "", ""
        cur_start_page, cur_start_char = 0, 0
    if cur_lines:
        _flush(pages[-1].num if pages else 0, 0)
    return [s for s in sections if s.text]
