"""
core/rag.py — Jarvis's long-term document knowledge layer (RAG).

ARCHITECTURE POSITION (deliberate)
    Hermes stays the messaging bridge, Jarvis stays the head, Gemini stays the
    reasoning engine. This module is the searchable LIBRARY behind Jarvis:
    Jarvis decides when to consult it, and this file never talks to Hermes and
    never talks to the model conversation.

DESIGN
    Local-first, zero heavy dependencies. A personal library is thousands of
    chunks, not billions — brute-force cosine over numpy is milliseconds at
    that scale, so the "vector database" is a directory of files:

        knowledge/
          library/            original PDFs, kept verbatim for traceability
            <doc_id>.pdf
          vectors.npz         one matrix (n_chunks x EMBED_DIM), row i = chunk i
          chunks.jsonl        one line per chunk: text + metadata + doc_id
          registry.json       one entry per document (hash, version, status…)
          audit.log           who added / searched / deleted what, and when

    Replacing this with ChromaDB later means rewriting search()/ingest() in
    ONE file — the action tool and Jarvis's prompt never change.

TWO PDF ACTIONS (the contract with the intent layer)
    read-once : process_pdf / search happen WITHOUT registering the document;
                temporary files never pollute long-term memory.
    remember  : ingest() stores the original, chunks, embeds, indexes, and
                registers the document so it is searchable forever after.

CITATIONS
    Every chunk carries (source, page, title, collection, doc_id) — answers
    can say WHERE a claim came from, and never confuse a 2023 book with
    today's market.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from memory.config_manager import BASE_DIR

KB_DIR      = BASE_DIR / "knowledge"
LIBRARY_DIR = KB_DIR / "library"
CHUNKS_FILE = KB_DIR / "chunks.jsonl"
VECTORS_FILE = KB_DIR / "vectors.npz"
REGISTRY_FILE = KB_DIR / "registry.json"
AUDIT_FILE  = KB_DIR / "audit.log"

# Gemini's embedding dimensionality — must match core.gemini.EMBED_DIM. Stored
# in the registry so a future model change can detect stale vectors and force
# a re-embed instead of silently mixing spaces.
EMBED_DIM = 3072
EMBED_MODEL_TAG = "gemini-embedding-001@3072"

_kb_lock = threading.RLock()

COLLECTIONS = (
    "trading_books",      # books, PDFs, educational material
    "strategies",         # your strategy rules and documented setups
    "research",           # research reports and notes
    "personal_knowledge", # your own notes and instructions
    "market_rules",       # definitions and methodology
    "temporary",          # used once, not remembered
)

DEFAULT_COLLECTION = "personal_knowledge"


# ── audit ────────────────────────────────────────────────────────────────────

def _audit(action: str, detail: str) -> None:
    """Append-only audit trail: who did what to the library, and when."""
    try:
        KB_DIR.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t"
                    f"{action}\t{detail}\n")
    except Exception:
        pass   # an audit write must never break the operation it audits


# ── registry ─────────────────────────────────────────────────────────────────

def _load_registry() -> dict:
    try:
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_registry(reg: dict) -> None:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(reg, indent=1, ensure_ascii=False),
                             encoding="utf-8")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ── extraction ───────────────────────────────────────────────────────────────

def extract_pages(pdf_path: str) -> list[str]:
    """Per-page text of a PDF. Tries pdfplumber (layout-aware), falls back to
    PyPDF2, and finally to whatever binary text the PDF leaks — so a scanned
    page yields little instead of raising. Scanned PDFs are handled by
    ocr_pdf_pages(), which ingest() calls automatically."""
    path = Path(pdf_path)
    pages: list[str] = []
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
        if any(p.strip() for p in pages):
            return pages
    except Exception:
        pass
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(str(path))
        pages = [(p.extract_text() or "") for p in reader.pages]
        if any(p.strip() for p in pages):
            return pages
    except Exception:
        pass
    return []


# ── other document types (txt / md / docx) ─────────────────────────────────
# The library is not only PDFs: notes and exports arrive as plain text and
# Word documents. Each becomes "pages" so chunking and page attribution stay
# uniform downstream — for page-less formats a page is a window of text cut
# at paragraph boundaries, not a lie about paper.

_PLAIN_SUFFIXES = frozenset({"txt", "md", "markdown", "log", "csv"})
_PAGE_WINDOW_CHARS = 6000          # synthetic page size for page-less formats


def _sniffs_pdf(path: Path) -> bool:
    """Content sniff: a mis-suffixed PDF is still a PDF."""
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False


def _extract_docx(path: Path) -> list[str]:
    """docx → synthetic pages of concatenated paragraphs. python-docx is
    already a dependency (file_processor), so this adds no install weight."""
    try:
        from docx import Document
        doc = Document(str(path))
    except Exception:
        return []
    pages: list[str] = []
    buf: list[str] = []
    size = 0
    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        buf.append(text)
        size += len(text) + 2
        if size >= _PAGE_WINDOW_CHARS:
            pages.append("\n\n".join(buf))
            buf, size = [], 0
    if buf:
        pages.append("\n\n".join(buf))
    return pages


def _extract_plain(path: Path) -> list[str]:
    """txt/md/log/csv → synthetic pages cut on blank lines when possible,
    so a paragraph is never split across pages mid-thought."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pages: list[str] = []
    buf: list[str] = []
    size = 0
    for para in paragraphs:
        if len(para) > _PAGE_WINDOW_CHARS:
            if buf:
                pages.append("\n\n".join(buf))
                buf, size = [], 0
            pages.append(para)
            continue
        if size + len(para) > _PAGE_WINDOW_CHARS and buf:
            pages.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(para)
        size += len(para) + 2
    if buf:
        pages.append("\n\n".join(buf))
    return pages


def extract_document(path: str) -> tuple[list[str], str]:
    """Text of any supported document type as pages, plus its filetype tag.

    Dispatch: PDF magic/extension → extract_pages (with OCR downstream),
    .docx → python-docx, plain-text suffixes → windowed pages. Returns
    ([...], filetype) with filetype in {pdf, docx, txt, md, ...} — "unknown"
    when nothing matches, which ingest() reports as an unsupported file.
    """
    p = Path(path)
    suffix = p.suffix.lower().lstrip(".")
    if suffix == "pdf" or _sniffs_pdf(p):
        return extract_pages(str(p)), "pdf"
    if suffix == "docx":
        return _extract_docx(p), "docx"
    if suffix in _PLAIN_SUFFIXES:
        return _extract_plain(p), suffix
    return [], "unknown"


# ── OCR (scanned pages) ──────────────────────────────────────────────────────
# System tools, deliberately: pdftoppm rasterises, tesseract recognises. Both
# run locally, cost no API quota and keep private documents on this machine —
# a cloud-vision OCR path can be added later for charts and handwriting.
#   apt: poppler-utils (pdftoppm) + tesseract-ocr [-hin for Hindi]

OCR_MAX_PAGES = 40          # safety cap: a 500-page scan is a decision, not an accident
OCR_DPI = 200               # 150 is fast but blurs small chart labels; 300 doubles runtime
OCR_PAGE_TIMEOUT = 30       # seconds per page before that page is abandoned
OCR_LANGS = "eng+hin"       # Hindi documents are common in this library

# ── Vision (chart pages) ─────────────────────────────────────────────────
# A chart screenshot survives OCR as number soup. Those pages are rasterised
# and DESCRIBED by a Gemini vision model — the description is what gets
# embedded, so "double top on the 15m" finds the page. Each described page's
# PNG is stored next to the original for traceability. Costs one vision call
# per thin page, hence the cap.
VISION_MAX_PAGES = 12       # per document; a 40-page scan of charts is a decision
VISION_DPI = 150            # description needs shape, not small print


_COMMON_WORDS = frozenset(
    "the and of to in is are it that this for with on as at by from or an be "
    "price chart level levels stop stoploss entry buy sell trend support "
    "resistance pattern candle candlestick market time frame when if not "
    "which what how their there then than more most also only can will".split())


def _is_meaningful_text(text: str, min_words: int = 25) -> bool:
    """False for the number-soup OCR that chart pages produce.

    Three gates, cheapest first: enough letter-words at all; letters visibly
    outnumbering digits (a price ladder is 90 % digits); and — the decisive
    one — real prose repeats common words, while rotated-text OCR noise
    produces pseudo-words ('ung', 'Kew', 'maIABuIpDIL') that never do."""
    text = text or ""
    words = re.findall(r"[A-Za-z]{2,}", text)
    if len(words) < min_words:
        return False
    letters = sum(len(w) for w in words)
    digits = len(re.findall(r"\d", text))
    if letters / max(1, letters + digits) < 0.45:
        return False
    lowered = {w.lower() for w in words}
    return len(lowered & _COMMON_WORDS) >= 3


def _raster_pages(pdf_path: str, page_numbers: list[int]) -> list[str]:
    """Render selected 1-based pages to PNG files; returns file paths in the
    same order ("" for a page that failed). Files are written to a fresh temp
    dir that the CALLER removes (shutil.rmtree) after describing them."""
    if shutil.which("pdftoppm") is None:
        return [""] * len(page_numbers)
    final_dir = Path(tempfile.mkdtemp(prefix="jarvis-vis-keep-"))
    out: list[str] = []
    with tempfile.TemporaryDirectory(prefix="jarvis-vis-") as tmp:
        tmpdir = Path(tmp)
        for n in page_numbers:
            dst = tmpdir / f"vis-{n}"
            try:
                subprocess.run(
                    ["pdftoppm", "-r", str(VISION_DPI), "-png",
                     "-f", str(n), "-l", str(n), pdf_path, str(dst)],
                    capture_output=True, timeout=60)
                made = sorted(tmpdir.glob(f"vis-{n}-*.png"))
                if made:
                    keep = final_dir / f"p{n}.png"
                    shutil.copy(made[0], keep)
                    out.append(str(keep))
                else:
                    out.append("")
            except Exception:
                out.append("")
    return out


def _describe_chart_page(png_path: str) -> str:
    """One vision call: what is factually ON this trading page. The text goes
    into the vector store; the annotation rules keep Gemini from inventing
    levels or giving advice."""
    try:
        from google.genai import types as gtypes
        from core import gemini
        data = Path(png_path).read_bytes()
        prompt = (
            "You are annotating a page from a trading PDF for a searchable "
            "knowledge base. Describe ONLY what is visibly on the page: "
            "chart type, instrument and timeframe if readable, trend and "
            "direction, marked or obvious levels, candlestick or indicator "
            "patterns BY NAME, and any text you can actually read (prices, "
            "labels, headings). 5-8 factual sentences. No trading advice, no "
            "interpretation beyond naming what is there, never invent numbers "
            "you cannot read."
        )
        resp = gemini.call(
            [gtypes.Part.from_bytes(data=data, mime_type="image/png"), prompt],
            tier=gemini.SMART, timeout_ms=45_000,
        )
        if resp is None:
            return ""
        return (getattr(resp, "text", "") or "").strip()
    except Exception as e:
        print(f"[RAG] ⚠️ vision description failed: {e}")
        return ""

_tess_langs_cache: str | None = None


def _tesseract_langs() -> str | None:
    """'eng+hin' when both are installed, 'eng' alone otherwise, None when
    tesseract is missing. Probed once per process."""
    global _tess_langs_cache
    if _tess_langs_cache is not None:
        return _tess_langs_cache or None
    tess = shutil.which("tesseract")
    if not tess:
        _tess_langs_cache = ""
        return None
    try:
        out = subprocess.run([tess, "--list-langs"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        _tess_langs_cache = ""
        return None
    have = {line.strip() for line in out.splitlines()}
    lang = "eng" if "eng" in have else (next(iter(have - {"List of available languages", ""}), "eng"))
    if "hin" in have:
        lang += "+hin"
    _tess_langs_cache = lang
    return lang


def ocr_pdf_pages(pdf_path: str, max_pages: int = OCR_MAX_PAGES,
                  dpi: int = OCR_DPI) -> list[str]:
    """Per-page text of a scanned PDF via pdftoppm + tesseract.

    Returns [] when either tool is missing or nothing is recognised — the
    caller treats that exactly like an unreadable PDF. Each page is bounded
    by its own timeout, so one pathological page cannot stall an ingest.
    """
    if shutil.which("pdftoppm") is None or _tesseract_langs() is None:
        print("[RAG] ⚠️ OCR unavailable — install poppler-utils and tesseract-ocr")
        return []

    tess_lang = _tesseract_langs()
    tess = shutil.which("tesseract")
    pages: list[str] = []
    with tempfile.TemporaryDirectory(prefix="jarvis-ocr-") as tmp:
        tmpdir = Path(tmp)
        try:
            subprocess.run(
                ["pdftoppm", "-r", str(dpi), "-png",
                 "-l", str(max_pages), pdf_path, str(tmpdir / "pg")],
                capture_output=True, timeout=max_pages * 10,
            )
        except Exception as e:
            print(f"[RAG] ⚠️ pdftoppm failed: {e}")
            return []

        pngs = sorted(tmpdir.glob("pg-*.png"),
                      key=lambda p: int(p.stem.split("-")[-1]))
        for png in pngs:
            try:
                r = subprocess.run(
                    [tess, str(png), "stdout", "-l", tess_lang],
                    capture_output=True, text=True, timeout=OCR_PAGE_TIMEOUT,
                )
                pages.append(r.stdout or "")
            except subprocess.TimeoutExpired:
                print(f"[RAG] ⚠️ OCR timed out on {png.name} — page skipped")
                pages.append("")
            except Exception as e:
                print(f"[RAG] ⚠️ OCR failed on {png.name}: {e}")
                pages.append("")
    return pages


_NOISE_LINE = re.compile(
    r"^(page \d+( of \d+)?|\d{1,3}\s*$|©|copyright|all rights reserved)",
    re.IGNORECASE)


def _clean_page(text: str) -> str:
    """Remove repeated-header/footer noise and broken spacing per page."""
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]{2,}", "  ", line).rstrip()
        if line and _NOISE_LINE.match(line.strip()):
            continue
        lines.append(line)
    out = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", out)


_HEADING = re.compile(r"^(chapter|section|part|appendix|[ivx]+\.\s|\d+\.\d*\s+[A-Z])",
                      re.IGNORECASE)


def _split_block(block: str, max_chars: int) -> list[str]:
    """Paragraph-first split of an oversize block, then hard-cut as a last
    resort so a pathological single paragraph still becomes chunks."""
    if len(block) <= max_chars:
        return [block]
    paras = [p for p in block.split("\n\n") if p.strip()]
    out, cur = [], ""
    for para in paras:
        if len(para) > max_chars:              # single giant paragraph
            for i in range(0, len(para), max_chars):
                out.append(para[i:i + max_chars])
            continue
        if cur and len(cur) + len(para) + 2 > max_chars:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def _overlap_tail(text: str, n: int) -> str:
    """The last n characters, cut at a word boundary — the bridge that keeps a
    sentence spanning two chunks readable in both."""
    if len(text) <= n:
        return text
    cut = text[-n:]
    sp = cut.find(" ")
    return cut[sp + 1:] if sp != -1 else cut


def chunk_pages(pages: list[str], max_chars: int = 1800,
                overlap: int = 200) -> list[dict]:
    """Page-aware chunking: chunks NEVER span pages (a page number must be
    trustworthy), headings start a new chunk, tables/formulas stay attached to
    their section because only paragraph boundaries are cut. Slightly oversize
    sections (≤ max_chars * 1.25) stay whole rather than being shredded."""
    chunks: list[dict] = []
    for page_no, raw_page in enumerate(pages, start=1):
        page = _clean_page(raw_page)
        if not page:
            continue
        blocks: list[str] = []
        cur = ""
        for line in page.split("\n"):
            if _HEADING.match(line.strip()) and cur.strip():
                blocks.append(cur)
                cur = line
            else:
                cur = f"{cur}\n{line}" if cur else line
        if cur.strip():
            blocks.append(cur)

        for block in blocks:
            limit = int(max_chars * 1.25)
            if len(block) > limit:
                pieces = _split_block(block, max_chars)
            else:
                pieces = [block]
            for i, piece in enumerate(pieces):
                if chunks and chunks[-1]["page"] == page_no \
                        and len(piece) < overlap * 2 and i == 0 \
                        and len(chunks[-1]["text"]) + len(piece) + 2 <= max_chars:
                    # Tiny orphan after a split: glue it to the previous
                    # same-page chunk so the store is not full of stubs.
                    chunks[-1]["text"] += "\n\n" + piece
                    continue
                text = piece
                if i > 0 and overlap:
                    text = _overlap_tail(pieces[i - 1], overlap) + " " + text
                chunks.append({"page": page_no, "text": text.strip()})
    return chunks


# ── vector store ─────────────────────────────────────────────────────────────

def _load_vectors() -> tuple[np.ndarray, int]:
    """(matrix, next_row). Row i of the matrix corresponds to line i+1 of
    chunks.jsonl — the two files are written under the same lock, read-only
    after that."""
    try:
        data = np.load(VECTORS_FILE)
        return data["vectors"], int(data["count"])
    except Exception:
        return np.zeros((0, EMBED_DIM), dtype=np.float32), 0


def _normalise(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _search_vectors(query_vec: np.ndarray, top_k: int,
                    allowed_docs: set[str] | None = None) -> list[dict]:
    """Cosine similarity over every chunk whose doc_id is allowed. Returns
    [{index, score}] sorted best-first."""
    with _kb_lock:
        mat, count = _load_vectors()
        if count == 0:
            return []
        rows = _read_chunks(count)
        if allowed_docs is not None:
            keep = [i for i, r in enumerate(rows)
                    if r["doc_id"] in allowed_docs]
            if not keep:
                return []
            mat = mat[keep]
            idx_map = keep
        else:
            idx_map = list(range(count))
        q = _normalise(query_vec)
        scores = (mat @ q) / (np.linalg.norm(mat, axis=1) + 1e-9)
        order = np.argsort(-scores)[:top_k]
        return [{"index": int(idx_map[i]), "score": float(scores[i])}
                for i in order]


def _read_chunks(count: int | None = None) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(CHUNKS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
                if count is not None and len(rows) >= count:
                        break
    except FileNotFoundError:
        pass
    return rows


# ── lexical fallback ──────────────────────────────────────────────────────────

def _tokenise(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", text.lower())}


def _search_lexical(query: str, top_k: int,
                    allowed_docs: set[str] | None = None) -> list[dict]:
    """Overlap scoring — the honest degraded mode when the embedding call
    itself fails (quota, network). No vectors touched."""
    q_tokens = _tokenise(query)
    if not q_tokens:
        return []
    scored = []
    for i, row in enumerate(_read_chunks()):
        if allowed_docs is not None and row["doc_id"] not in allowed_docs:
            continue
        score = len(q_tokens & _tokenise(row["text"])) / len(q_tokens)
        if score > 0:
            scored.append({"index": i, "score": score})
    scored.sort(key=lambda x: -x["score"])
    return scored[:top_k]


def _replace_doc_rows(doc_id: str, rows: list[dict], vectors: list) -> int:
    """Atomically swap one document's rows in the index (update / reindex).

    Drops every existing row of doc_id, appends the new rows + vectors,
    rewrites both files under the lock, and returns the row base. `rows` must
    be complete chunk rows (doc_id, text, page, …) — only "index" is set here.
    Callers hold _kb_lock. Shared by update() and deep_reingest() so the two
    can never drift apart.
    """
    _, count = _load_vectors()
    existing = _read_chunks(count)
    keep_rows = [r for r in existing if r["doc_id"] != doc_id]
    keep_idx = [r["index"] for r in existing if r["doc_id"] != doc_id]
    mat, _ = _load_vectors()
    if keep_idx:
        mat = mat[keep_idx]
    else:
        mat = np.zeros((0, mat.shape[1] if mat.size else len(vectors[0])),
                       dtype=np.float32)
    base = len(keep_rows)
    new_rows: list[dict] = []
    for i, row in enumerate(rows):
        r = dict(row)
        r["index"] = base + i
        new_rows.append(r)
    mat = np.vstack([mat, np.asarray(vectors, dtype=np.float32)]) \
        if mat.size else np.asarray(vectors, dtype=np.float32)
    np.savez_compressed(VECTORS_FILE, vectors=mat,
                        count=len(keep_rows) + len(new_rows))
    with open(CHUNKS_FILE, "w", encoding="utf-8") as f:
        for r_ in keep_rows + new_rows:
            f.write(json.dumps(r_, ensure_ascii=False) + "\n")
    return base


# ── ingest ───────────────────────────────────────────────────────────────────

def _next_version(reg: dict, doc_hash: str, collection: str, title: str) -> int:
    """Version 1 — or previous version + 1 when the same logical document
    (same title + collection) is re-ingested with new content."""
    for entry in reg.values():
        if entry.get("title") == title and entry.get("collection") == collection:
            return int(entry.get("version", 1)) + 1
    return 1


def ingest(file_path: str, collection: str = DEFAULT_COLLECTION,
           title: str | None = None, source: str = "local",
           access: str = "private", metadata: dict | None = None) -> dict:
    """RECEIVE → EXTRACT → CLEAN → CHUNK → METADATA → EMBED → INDEX → REGISTER.

    The original file is stored verbatim FIRST (traceability before anything
    else). A document whose embeddings fail is registered with
    status="failed" and nothing is indexed — no half-remembered books.

    PDFs, .docx and plain-text files (txt/md/log/csv) are supported; the
    filetype is stored in the registry and the original keeps its real suffix.
    """
    path = Path(file_path).expanduser()
    if not path.exists():
        return {"ok": False, "error": f"file not found: {path}"}
    if collection not in COLLECTIONS:
        collection = DEFAULT_COLLECTION
    access = access if access in ("private", "shared") else "private"
    extra_meta = dict(metadata) if isinstance(metadata, dict) else {}

    doc_hash = _sha256(path)
    reg = _load_registry()

    # Duplicate detection is exact-content: the same bytes were already
    # remembered, re-indexing would only double every future citation.
    for doc_id, entry in reg.items():
        if entry.get("hash") == doc_hash and entry.get("status") == "indexed":
            _audit("ingest_dedup", f"{doc_id} ({path.name})")
            return {"ok": True, "doc_id": doc_id, "duplicate": True,
                    "pages": entry.get("page_count"),
                    "title": entry.get("title")}

    doc_id = doc_hash[:16]
    title = (title or path.stem).strip()[:120]
    version = _next_version(reg, doc_hash, collection, title)

    pages, filetype = extract_document(str(path))
    if not any(p.strip() for p in pages) and filetype == "pdf":
        # No text layer → try OCR before giving up. Scanned trading PDFs are
        # common in this library; this is what makes them rememberable.
        print(f"[RAG] 🔍 No text layer on '{path.name}' — running OCR "
              f"(max {OCR_MAX_PAGES} pages, {OCR_DPI} dpi)…")
        pages = ocr_pdf_pages(str(path))
        ocr_used = bool(pages) and any(p.strip() for p in pages)
        if ocr_used:
            source = f"{source}+ocr"
    clean = [_clean_page(p) for p in pages]
    page_count = len([p for p in clean if p.strip()])
    if page_count == 0:
        if filetype == "unknown":
            return {"ok": False,
                    "error": ("unsupported file type — ingest PDF, docx, "
                              "txt, md, log or csv")}
        return {"ok": False,
                "error": ("no extractable text and OCR found nothing "
                          "(empty, encrypted, or unrecognisable scan)")}

    # ── Vision pass: chart pages ─────────────────────────────────────────
    # Pages whose text is number-soup (chart screenshots) are described by a
    # Gemini vision model. The description REPLACES the soup as that page's
    # text, so it is chunked and embedded like prose and "double top on the
    # 15m" finds the page. The PNG is archived for traceability.
    thin = [i for i, p in enumerate(clean)
            if p.strip() and not _is_meaningful_text(p)]
    vision_pages: dict[int, str] = {}
    if thin and len(thin) <= VISION_MAX_PAGES:
        print(f"[RAG] 🖼️  {len(thin)} chart-like page(s) — describing them "
              f"with Gemini vision…")
        pngs = _raster_pages(str(path), [i + 1 for i in thin])
        pages_dir = LIBRARY_DIR / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        _tmp_parents: set[Path] = set()
        for i, png in zip(thin, pngs):
            if not png:
                continue
            _tmp_parents.add(Path(png).parent)
            desc = _describe_chart_page(png)
            if not desc:
                continue
            shutil.copy(png, pages_dir / f"{doc_id}-p{i + 1}.png")
            clean[i] = desc
            vision_pages[i + 1] = desc
        for d in _tmp_parents:
            shutil.rmtree(d, ignore_errors=True)
    elif thin:
        print(f"[RAG] ⚠️ {len(thin)} chart-like pages exceed the vision cap "
              f"({VISION_MAX_PAGES}) — they stay OCR-only")

    chunks = chunk_pages(clean)
    if not chunks:
        return {"ok": False, "error": "chunking produced nothing"}

    # 1. the original, verbatim (real suffix — PDFs stay PDFs, notes stay notes)
    KB_DIR.mkdir(parents=True, exist_ok=True)
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    stored = LIBRARY_DIR / f"{doc_id}.{filetype}" if filetype != "unknown" \
        else LIBRARY_DIR / f"{doc_id}.bin"
    if not stored.exists():
        stored.write_bytes(path.read_bytes())

    # 2. embeddings, before any index mutation — a failure here must not
    #    leave the store half-written.
    from core import gemini
    vectors = gemini.embed([c["text"] for c in chunks], is_query=False)
    vectors = [_normalise(v) for v in vectors]

    # 3. index + register under one lock
    with _kb_lock:
        mat, count = _load_vectors()
        rows = _read_chunks(count)
        base = count
        new_rows = []
        for i, c in enumerate(chunks):
            rows.append({
                "doc_id": doc_id,
                "index": base + i,
                "text": c["text"],
                "page": c["page"],
                "title": title,
                "collection": collection,
                "filetype": filetype,
                "source": source,
                "version": version,
                "access": access,
                "metadata": dict(extra_meta),
                "vision": c["page"] in vision_pages,
            })
            new_rows.append(rows[-1])
        mat = np.vstack([mat, np.asarray(vectors, dtype=np.float32)]) \
            if mat.size else np.asarray(vectors, dtype=np.float32)
        np.savez_compressed(VECTORS_FILE, vectors=mat, count=len(rows))
        with open(CHUNKS_FILE, "a", encoding="utf-8") as f:
            for row in new_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

        reg[doc_id] = {
            "doc_id": doc_id,
            "original_name": path.name,
            "title": title,
            "hash": doc_hash,
            "collection": collection,
            "version": version,
            "filetype": filetype,
            "page_count": page_count,
            "chunk_count": len(chunks),
            "source_channel": source,   # records +ocr when OCR rescued it
            "access": access,           # private | shared — read gate for callers
            "metadata": extra_meta,     # free-form (channel, chat_id, tags…)
            "ingested_at": datetime.now().isoformat(timespec="seconds"),
            "status": "indexed",
            "embed_model": EMBED_MODEL_TAG,
            "vectors_row_base": base,
            "vision_pages": sorted(vision_pages),
        }
        _save_registry(reg)

    _audit("ingest", f"{doc_id} v{version} '{title}' "
                     f"({page_count}p/{len(chunks)} chunks, "
                     f"{len(vision_pages)} vision) [{collection}:{access}]")
    return {"ok": True, "doc_id": doc_id, "title": title, "pages": page_count,
            "chunks": len(chunks), "version": version, "duplicate": False,
            "collection": collection, "filetype": filetype, "access": access,
            "vision_pages": sorted(vision_pages)}


def _resolve_original(doc_id: str) -> Path | None:
    """The stored original of a document, whatever suffix it was saved with."""
    for p in sorted(LIBRARY_DIR.glob(f"{doc_id}.*")):
        if p.is_file() and not p.name.endswith(".png"):
            return p
    return None


# ── search ───────────────────────────────────────────────────────────────────

def deep_reingest(doc_id: str, ocr_max_pages: int = None,
                  vision_max_pages: int = None) -> dict:
    """Deep pass over an already-ingested document: rescue thin/image pages.

    The normal ingest only OCRs when the WHOLE PDF has no text layer; a book
    that mixes a text layer with scanned/chart pages ends up with near-empty
    page chunks. This pass re-extracts, then:
      1. OCRs every page whose text is missing/thin (per-page, bounded),
      2. sends still-thin pages (chart screenshots) to Gemini vision with a
         raised page cap,
      3. replaces the document's vectors+chunks in place (same doc_id, the
         stored original PDF is untouched) and bumps the version.
    Returns the same shape as ingest().
    """
    reg = _load_registry()
    entry = reg.get(doc_id)
    if not entry:
        return {"ok": False, "error": f"unknown document: {doc_id}"}
    original = _resolve_original(doc_id)
    if original is None:
        return {"ok": False, "error": f"stored original missing: {doc_id}"}

    ocr_cap = ocr_max_pages if ocr_max_pages is not None else OCR_MAX_PAGES
    vision_cap = vision_max_pages if vision_max_pages is not None else VISION_MAX_PAGES * 4

    title = entry.get("title") or doc_id
    collection = entry.get("collection", DEFAULT_COLLECTION)
    version = int(entry.get("version", 1)) + 1
    source = f"deep-v{version}"

    pages = extract_pages(str(original))
    clean = [_clean_page(p) for p in pages]

    # 1) per-page OCR rescue for empty/thin pages
    thin = [i for i, p in enumerate(clean)
            if not p.strip() or not _is_meaningful_text(p)]
    ocr_rescued = []
    if thin:
        print(f"[RAG] 🔎 deep pass '{title}': OCR on {len(thin)} thin page(s) "
              f"(cap {ocr_cap})…")
        ocr_pages = ocr_pdf_pages(str(original), max_pages=min(ocr_cap,
                                  max(len(pages), 1)))
        for i in thin[:len(ocr_pages)]:
            rescued = _clean_page(ocr_pages[i]) if i < len(ocr_pages) else ""
            if rescued.strip():
                clean[i] = rescued
                ocr_rescued.append(i + 1)

    # 2) vision pass on pages that are STILL thin (chart screenshots)
    still_thin = [i for i, p in enumerate(clean)
                  if p.strip() and not _is_meaningful_text(p)]
    # include pages that stayed empty after OCR too
    still_thin += [i for i, p in enumerate(clean) if not p.strip()]
    still_thin = sorted(set(still_thin))
    vision_pages: dict[int, str] = {}
    if still_thin and len(still_thin) <= vision_cap:
        print(f"[RAG] 🖼️  deep pass: {len(still_thin)} page(s) to Gemini vision…")
        pngs = _raster_pages(str(original), [i + 1 for i in still_thin])
        pages_dir = LIBRARY_DIR / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        tmp_parents: set[Path] = set()
        for i, png in zip(still_thin, pngs):
            if not png:
                continue
            tmp_parents.add(Path(png).parent)
            desc = _describe_chart_page(png)
            if not desc:
                continue
            shutil.copy(png, pages_dir / f"{doc_id}-p{i + 1}.png")
            clean[i] = desc
            vision_pages[i + 1] = desc
        for d in tmp_parents:
            shutil.rmtree(d, ignore_errors=True)
    elif still_thin:
        print(f"[RAG] ⚠️ {len(still_thin)} pages exceed the deep vision cap "
              f"({vision_cap}) — left as-is")

    chunks = chunk_pages(clean)
    if not chunks:
        return {"ok": False, "error": "deep pass produced no chunks"}

    from core import gemini
    vectors = gemini.embed([c["text"] for c in chunks], is_query=False)
    vectors = [_normalise(v) for v in vectors]

    page_count = len([p for p in clean if p.strip()])
    with _kb_lock:
        reg = _load_registry()          # re-read under lock
        full_rows = [{
            "doc_id": doc_id,
            "text": c["text"],
            "page": c["page"],
            "title": title,
            "collection": collection,
            "filetype": entry.get("filetype", "pdf"),
            "source": source,
            "version": version,
            "access": entry.get("access", "private"),
            "metadata": entry.get("metadata") or {},
            "vision": c["page"] in vision_pages,
        } for c in chunks]
        base = _replace_doc_rows(doc_id, full_rows, vectors)

        entry = dict(entry)
        entry.update({
            "version": version,
            "page_count": page_count,
            "chunk_count": len(chunks),
            "source_channel": source,
            "ingested_at": datetime.now().isoformat(timespec="seconds"),
            "status": "indexed",
            "embed_model": EMBED_MODEL_TAG,
            "vectors_row_base": base,
            "vision_pages": sorted(vision_pages),
            "deep_pass": {"ocr_rescued": ocr_rescued,
                          "vision_pages": sorted(vision_pages)},
        })
        reg[doc_id] = entry
        _save_registry(reg)

    _audit("deep_reingest", f"{doc_id} v{version} '{title}' "
                            f"({page_count}p/{len(chunks)} chunks, "
                            f"ocr+{len(ocr_rescued)}, vision {len(vision_pages)})")
    return {"ok": True, "doc_id": doc_id, "title": title, "pages": page_count,
            "chunks": len(chunks), "version": version, "ocr_rescued": ocr_rescued,
            "vision_pages": sorted(vision_pages), "collection": collection,
            "duplicate": False}


def _patch_doc_rows(doc_id: str, **fields) -> int:
    """Rewrite selected metadata fields on every index row of doc_id — the
    metadata-only update path: citations must never serve stale access/title
    after a patch. Vectors are untouched. Callers hold _kb_lock. Returns the
    number of rows patched."""
    rows = _read_chunks()
    if not rows:
        return 0
    n = 0
    for r in rows:
        if r["doc_id"] == doc_id:
            r.update(fields)
            n += 1
    with open(CHUNKS_FILE, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return n


def _build_rows(doc_id: str, chunks: list[dict], *, title: str,
                collection: str, filetype: str, source: str, version: int,
                access: str = "private", metadata: dict | None = None,
                vision_pages: dict[int, str] | set[int] | None = None) -> list[dict]:
    """Complete index rows for one document — the single metadata schema shared
    by ingest, update and deep_reingest so a citation never depends on which
    path produced it."""
    vp = vision_pages or {}
    vp_set = set(vp) if not isinstance(vp, set) else vp
    return [{
        "doc_id": doc_id,
        "text": c["text"],
        "page": c["page"],
        "title": title,
        "collection": collection,
        "filetype": filetype,
        "source": source,
        "version": version,
        "access": access,
        "metadata": dict(metadata or {}),
        "vision": c["page"] in vp_set,
    } for c in chunks]


def update(doc_id: str, new_path: str | None = None,
           text: str | None = None, title: str | None = None,
           collection: str | None = None, access: str | None = None,
           metadata: dict | None = None,
           reindex: bool = True) -> dict:
    """Update a document — edit metadata in place, or replace its content.

    Content replacement (new_path or text given): re-extract, re-chunk, embed
    the NEW chunks, then atomically swap this doc's rows. The old vectors are
    never mixed with the new — half-old answers are worse than a slow update.
    The version bumps when content actually changed.

    Metadata-only (no content): title/collection/access/metadata are patched
    with zero re-embedding cost.
    """
    reg = _load_registry()
    entry = reg.get(doc_id)
    if not entry:
        return {"ok": False, "error": f"unknown document: {doc_id}"}

    meta_only = new_path is None and text is None
    if meta_only:
        with _kb_lock:
            reg = _load_registry()
            e = reg.get(doc_id)
            if not e:
                return {"ok": False, "error": f"document vanished: {doc_id}"}
            row_fields: dict = {}
            if title is not None and str(title).strip():
                e["title"] = str(title).strip()[:120]
                row_fields["title"] = e["title"]
            if collection is not None and collection in COLLECTIONS:
                e["collection"] = collection
                row_fields["collection"] = collection
            if access is not None and access in ("private", "shared"):
                e["access"] = access
                row_fields["access"] = access
            if isinstance(metadata, dict):
                merged = dict(e.get("metadata") or {})
                merged.update(metadata)
                e["metadata"] = merged
                row_fields["metadata"] = merged
            if row_fields:
                _patch_doc_rows(doc_id, **row_fields)
            _save_registry(reg)
        _audit("update_meta", f"{doc_id} '{e.get('title')}'")
        return {"ok": True, "doc_id": doc_id, "updated_fields": True,
                "title": e.get("title")}

    if not reindex:
        return {"ok": True, "doc_id": doc_id, "reindexed": False}

    # ── content replacement ─────────────────────────────────────────────
    if text is not None:
        pages = [text]
        filetype = entry.get("filetype", "txt")
        new_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    else:
        p = Path(new_path).expanduser()
        if not p.exists():
            return {"ok": False, "error": f"file not found: {new_path}"}
        pages, filetype = extract_document(str(p))
        new_hash = _sha256(p)
    if not any(pg.strip() for pg in pages) and filetype == "pdf" and text is None:
        pages = ocr_pdf_pages(str(p))
    if not any(pg.strip() for pg in pages):
        return {"ok": False, "error": "new content has no extractable text"}

    clean = [_clean_page(pg) for pg in pages]
    page_count = len([pg for pg in clean if pg.strip()])
    chunks = chunk_pages(clean)
    if not chunks:
        return {"ok": False, "error": "chunking produced nothing"}

    version = int(entry.get("version", 1)) + (1 if new_hash != entry.get("hash") else 0)
    source = f"update-v{version}"
    new_title = (title or entry.get("title") or doc_id)
    new_collection = collection if collection in COLLECTIONS \
        else entry.get("collection", DEFAULT_COLLECTION)
    new_access = access if access in ("private", "shared") \
        else entry.get("access", "private")
    new_meta = dict(entry.get("metadata") or {})
    if isinstance(metadata, dict):
        new_meta.update(metadata)

    from core import gemini
    vectors = gemini.embed([c["text"] for c in chunks], is_query=False)
    vectors = [_normalise(v) for v in vectors]

    with _kb_lock:
        reg = _load_registry()
        e = reg.get(doc_id)
        if not e:
            return {"ok": False, "error": f"document vanished: {doc_id}"}
        # store the new original when the content came from a file; drop a
        # stale original whose suffix changed (pdf → txt update)
        if text is None:
            stored = LIBRARY_DIR / f"{doc_id}.{filetype}" if filetype != "unknown" \
                else LIBRARY_DIR / f"{doc_id}.bin"
            for old in LIBRARY_DIR.glob(f"{doc_id}.*"):
                if old != stored and old.is_file() \
                        and not old.name.endswith(".png"):
                    try:
                        old.unlink()
                    except OSError:
                        pass
            stored.write_bytes(p.read_bytes())
        base = _replace_doc_rows(doc_id, _build_rows(
            doc_id, chunks, title=new_title, collection=new_collection,
            filetype=filetype, source=source, version=version,
            access=new_access, metadata=new_meta), vectors)
        e.update({
            "version": version,
            "hash": new_hash,
            "filetype": filetype,
            "title": new_title,
            "collection": new_collection,
            "access": new_access,
            "metadata": new_meta,
            "page_count": page_count,
            "chunk_count": len(chunks),
            "source_channel": source,
            "status": "indexed",
            "vectors_row_base": base,
        })
        reg[doc_id] = e
        _save_registry(reg)

    _audit("update", f"{doc_id} v{version} '{new_title}' "
                     f"({page_count}p/{len(chunks)} chunks) [{new_collection}]")
    return {"ok": True, "doc_id": doc_id, "title": new_title,
            "pages": page_count, "chunks": len(chunks), "version": version,
            "collection": new_collection, "duplicate": False}


def search(query: str, top_k: int = 5, collection: str | None = None,
           doc_id: str | None = None,
           allowed_docs: set[str] | None = None) -> dict:
    """Embed the query, return the strongest chunks with full citations.

    Falls back to lexical scoring when the embedding call fails — degraded
    but honest, and never empty without saying why.

    allowed_docs is the caller's read whitelist (service-level ACL); when
    given, it INTERSECTS with any doc_id/collection filter — a caller can
    never widen its scope past what it is allowed to see.
    """
    if not query.strip():
        return {"ok": False, "error": "empty query"}

    allowed: set[str] | None = None
    reg = _load_registry()
    if doc_id:
        if doc_id not in reg:
            return {"ok": False, "error": f"unknown document: {doc_id}"}
        allowed = {doc_id}
    elif collection:
        if collection not in COLLECTIONS:
            return {"ok": False, "error": f"unknown collection: {collection}"}
        allowed = {d for d, e in reg.items()
                   if e.get("collection") == collection
                   and e.get("status") == "indexed"}
        if not allowed:
            return {"ok": True, "results": [], "mode": "none",
                    "note": f"collection '{collection}' is empty"}
    if allowed_docs is not None:
        allowed = (allowed & allowed_docs) if allowed is not None \
            else set(allowed_docs)
        if not allowed:
            return {"ok": True, "results": [], "mode": "none",
                    "note": "no documents in scope for this caller"}

    with _kb_lock:
        _, count = _load_vectors()
    if count == 0:
        return {"ok": True, "results": [], "mode": "none",
                "note": "the knowledge base is empty — nothing ingested yet"}

    mode = "semantic"
    try:
        from core import gemini
        qvec = gemini.embed(query, is_query=True)   # single string → one vector
        hits = _search_vectors(qvec, top_k, allowed)
    except Exception as e:
        print(f"[RAG] ⚠️ embedding search failed ({e}) — lexical fallback")
        mode = "lexical"
        hits = _search_lexical(query, top_k, allowed)

    rows = _read_chunks(count)
    results = []
    for h in hits:
        r = rows[h["index"]]
        results.append({
            "text": r["text"],
            "source": r.get("title") or r.get("source"),
            "page": r.get("page"),
            "doc_id": r["doc_id"],
            "collection": r.get("collection"),
            "filetype": r.get("filetype"),
            "version": r.get("version"),
            "access": r.get("access", "private"),
            "metadata": r.get("metadata") or {},
            "vision": bool(r.get("vision")),
            "score": round(h["score"], 4),
        })
    _audit("search", f"mode={mode} k={len(results)} q={query[:60]!r}")
    return {"ok": True, "mode": mode, "results": results}


# ── management ───────────────────────────────────────────────────────────────

def list_documents() -> list[dict]:
    reg = _load_registry()
    return sorted(reg.values(),
                  key=lambda e: e.get("ingested_at", ""), reverse=True)


def forget(doc_id: str) -> dict:
    """Remove one document: its vectors, its chunks, its stored original, its
    registry entry. The audit line stays — deletions are remembered."""
    with _kb_lock:
        reg = _load_registry()
        entry = reg.get(doc_id)
        if not entry:
            return {"ok": False, "error": f"unknown document: {doc_id}"}

        _, count = _load_vectors()
        rows = _read_chunks(count)
        keep_rows = [r for r in rows if r["doc_id"] != doc_id]
        keep_idx = [r["index"] for r in rows if r["doc_id"] != doc_id]
        if keep_idx:
            mat, _ = _load_vectors()
            mat = mat[keep_idx]
            np.savez_compressed(VECTORS_FILE, vectors=mat, count=len(keep_rows))
        else:
            try:
                VECTORS_FILE.unlink()
            except FileNotFoundError:
                pass
        try:
            CHUNKS_FILE.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n"
                        for r in keep_rows), encoding="utf-8")
        except FileNotFoundError:
            pass
        try:
            (LIBRARY_DIR / f"{doc_id}.pdf").unlink()
        except FileNotFoundError:
            pass
        for png in (LIBRARY_DIR / "pages").glob(f"{doc_id}-p*.png"):
            try:
                png.unlink()
            except FileNotFoundError:
                pass
        del reg[doc_id]
        _save_registry(reg)

    _audit("forget", f"{doc_id} '{entry.get('title')}'")
    return {"ok": True, "forgotten": entry.get("title")}


def stats() -> dict:
    reg = _load_registry()
    by_collection: dict[str, int] = {}
    for e in reg.values():
        c = e.get("collection", "?")
        by_collection[c] = by_collection.get(c, 0) + 1
    with _kb_lock:
        _, count = _load_vectors()
    return {"documents": len(reg), "chunks": count,
            "collections": by_collection, "embed_dim": EMBED_DIM,
            "embed_model": EMBED_MODEL_TAG}
