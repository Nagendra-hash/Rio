# Jarvis RAG — architecture checkpoint (Phase 1)

*Branch `upgrade-20260930`, October 2026. Status: implemented, 128 relevant tests green.*

This document is the checkpoint for the first production-style RAG foundation:
document ingestion → text extraction → chunking → metadata → embeddings →
vector storage → retrieval with citations → update/delete — with collection
separation and access control, entirely local-first.

---

## 1. The layer map

```
   ┌─────────────────────────────────────────────────────────────────┐
   │ callers                                                         │
   │                                                                 │
   │  Jarvis intent layer        Hermes gateway       dashboard UI   │
   │  (core/prompt.txt)          (jarvis_inbox.py     (browser)      │
   │        │                      --rag flag)            │          │
   │        │                            │                │          │
   │  actions/knowledge_base.py   POST /api/rag/       fetch /api/rag │
   │        │                     ingest-path           │           │
   │        │                            └───────┬──────┘            │
   │        ▼                                    ▼                   │
   │  ┌──────────────────────────────────────────────────┐           │
   │  │ orchestrator rag_agent (JARVIS_RAG_AGENT=on)     │           │
   │  └────────────────────┬─────────────────────────────┘           │
   └───────────────────────┼─────────────────────────────────────────┘
                           ▼
            ┌──────────────────────────────┐
            │  core/rag_service.py         │   ← THE ONE API other agents call
            │  identity · ACL · audit      │
            └──────────────┬───────────────┘
                           ▼
            ┌──────────────────────────────┐
            │  core/rag.py                 │   ← the LIBRARY (no callers
            │  extract · chunk · embed ·   │      of its own)
            │  index · search · forget     │
            └──────────────┬───────────────┘
                           ▼
   knowledge/  (gitignored, relocatable via JARVIS_HOME)
     vectors.npz   chunks.jsonl   registry.json   audit.log
     library/<doc_id>.<pdf|docx|txt|…>  library/pages/<doc_id>-p<N>.png
```

**The rule:** nothing except `core/rag_service.py` calls `core/rag.py` directly.
Permissions, collection grants and evidence budgets are therefore enforced in
exactly one place. The Jarvis action tool, the REST API, the orchestrator agent
and Hermes-driven scripts all go through the service.

## 2. Ingestion pipeline (core/rag.py `ingest`)

```
RECEIVE → EXTRACT → CLEAN → CHUNK → METADATA → EMBED → INDEX → REGISTER
```

1. **Receive** — the original file is stored **verbatim first** (`knowledge/library/{doc_id}.{filetype}`): traceability before anything else. Dedup is exact-content (sha256): re-ingesting the same bytes returns the existing `doc_id` with `duplicate: true` instead of double-indexing.
2. **Extract** (`extract_document`) — dispatch on sniffed/declared type:
   - **PDF**: `pdfplumber` page-by-page text; if a page has no text layer, OCR (`pdftoppm` + tesseract, capped at `OCR_MAX_PAGES`) rescues scanned trading PDFs; source is tagged `+ocr`.
   - **Chart pages**: pages whose text is number-soup (chart screenshots) are described by a Gemini **vision** pass; the description *replaces* the soup so it is chunked like prose ("double top on the 15m" finds the page). The rendered PNG is archived in `library/pages/` (capped at `VISION_MAX_PAGES`).
   - **docx** (python-docx), **txt/md/log/csv** (utf-8, split into `~6000`-char page windows), via the same `extract_document` contract.
   - Unsupported types are refused with a clear error — never silently indexed.
3. **Clean → chunk** — per-page normalisation, then page-aware chunking (`chunk_pages`): chunk boundaries respect page edges, so a citation can always name a page.
4. **Embed** — Gemini `text-embedding` (3072-dim), batches of 32, L2-normalised. Embedding happens **before any index mutation**: a failure registers the doc `status="failed"` and indexes nothing — no half-remembered books.
5. **Index + register** — under one RLock: vectors appended to `vectors.npz`, rows appended to `chunks.jsonl` (text, page, title, collection, filetype, source, version, access, metadata, vision), registry entry written. All mutations are audit-logged to `audit.log`.

**Cost control:** whole documents are **never** sent to any model. Only chunk texts (~300–600 chars each) go to the embedder, and only the query string goes to the embedder at search time. The test suite proves this with a spy on the embedder.

## 3. Retrieval (search with citations)

`core/rag.search(query, top_k, collection, doc_id, allowed_docs)`:

- Embeds the **query only**, cosine-scores against the store, returns top-k chunks sorted by score, each with full citation fields: `text, source(title), page, doc_id, collection, filetype, version, access, metadata, vision, score`.
- Embedding failure → **lexical fallback** (degraded but honest, mode tagged).
- `doc_id` scopes to one document; `collection` scopes to one collection; `allowed_docs` (service-supplied) **intersects** — a caller can never widen scope past its grant.

`core/rag_service.search(caller, query, top_k, collection, doc_id, max_chars=12000)` wraps it with:

- **citations** — human/LLM-readable strings: `"Options Playbook, p.2 [strategies] (v2)"`, plus a chart-page marker when the chunk came from a vision-described page.
- **evidence** — one bounded string (`[1] cite\ntext\n\n[2] cite\ntext…`), hard-capped at `max_chars` (default 12 000). This is what a caller may drop into a prompt — **the document never rides along**.

## 4. Update / delete / versions

- **Metadata patch** (`update(doc_id, title=…, collection=…, access=…, metadata=…)`): patches registry **and index rows** (`_patch_doc_rows`) so citations never serve a stale title/access. Zero re-embedding.
- **Content replacement** (`update(doc_id, new_path=… | text=…)`): re-extract → re-chunk → re-embed the **new** chunks, then an atomic row swap (`_replace_doc_rows`). The old vectors are never mixed with the new. Version bumps **only when content actually changed** (hash compare).
- **Delete** (`forget(doc_id)`): removes vectors, chunk rows, stored original and registry entry. The audit line stays — deletions are remembered.
- `deep_reingest(doc_id)` re-runs the full pipeline (OCR + vision passes) onto the same shared helpers.

## 5. Collections + access control (core/rag_service.py)

Two axes, checked on **every** call:

| axis | meaning |
|---|---|
| `collections` | the collections a caller may read. Empty = blind. `"__all__"` = everything (owner only). |
| `can_write` | ingest/update/forget additionally require write. Research/summariser agents get `False`. |

Collections (fixed tuple in `core/rag.py`): `trading_books, strategies, research, personal_knowledge, market_rules, temporary`. Default: `personal_knowledge`.

Callers are minted in **`config/rag_callers.json`**, re-read on each use so access can be revoked without a restart:

```json
{
  "trading_desk": { "collections": ["trading_books", "market_rules"], "can_write": false },
  "ingest_bot":   { "collections": ["__all__"], "can_write": true }
}
```

Built-in **owner** caller `"jarvis"` (always present, not in the file, cannot be revoked): all collections, full write. Unknown callers are **blind by default** (they can log in; they see nothing). Denial semantics:

- reading an ungranted collection → `access denied: caller 'X' may not read collection 'Y'`
- writing without `can_write` → `access denied: caller 'X' cannot write to the knowledge base`
- doc-scoped read outside the grant → `access denied: document … is outside caller 'X''s collections`

Every denial, ingest, update, forget and successful search is audit-logged with the caller name (`svc:denied`, `svc:ingest`, `svc:search`, …), so "who asked the library for this" always has an answer. Blind-caller probes are logged too — they are a security signal.

## 6. Surfaces

### REST (dashboard/rag_api.py — `register(app, auth_check)`)

All bearer-auth via the dashboard's existing `_auth`. Uploaded by token ⇒ the token grants the keyhole, not the library.

| endpoint | body | notes |
|---|---|---|
| `POST /api/rag/query` | `{query, collection?, doc_id?, top_k?, caller?}` | top_k clamped 1–25; default caller `dashboard` (blind unless granted) |
| `POST /api/rag/ingest` | multipart `file` + `collection/title/access/caller` | streams to temp, service moves bytes |
| `POST /api/rag/ingest-path` | JSON `{path, collection?, title?, source?, metadata?}` | the **Hermes flow**: file already on this machine |
| `POST /api/rag/ingest-text` | JSON `{text, title, collection?, suffix?, access?}` | notes/transcripts without a file |
| `POST /api/rag/update` | multipart `file` + `doc_id` | content replacement, version bump |
| `POST /api/rag/update-meta` | JSON `{doc_id, title?, collection?, access?, metadata?}` | no reindex |
| `POST /api/rag/forget` | JSON `{doc_id}` | |
| `GET /api/rag/documents?collection=&caller=` | | scoped list |
| `GET /api/rag/stats?caller=` | | scoped stats |

Status mapping: ok → 200; `access denied…` → **403**; anything else → 400. Upload support degrades to a clean error when `python-multipart` is missing (mirrors `/api/upload`).

Wired in `dashboard/server.py::_build_app()` after the `/api/orchestrator` block (failure to import logs and continues — the dashboard must not die with RAG).

### Jarvis action tool (actions/knowledge_base.py)

`knowledge_base(action=search|ingest|forget|list)` — unchanged shape, now routed through the service as caller `jarvis`. Format-vs-decide stays split: the tool formats results with source+page; the decision tree stays in `core/prompt.txt`.

### Orchestrator agent (orchestrator/agents/rag_agent.py)

The real `rag_agent` replaces the Phase-1 stub under the **same registry name** (Phase-2 convention — router/contracts untouched). `read` → service search (evidence + citations, never calls the LLM); `ingest`/`forget` via `metadata.rag_action`, gated by ACL. Registered only when `JARVIS_RAG_AGENT=on`; default off until the orchestrator goes live.

### Hermes (scripts/jarvis_inbox.py)

`--rag [collection] --title "…"` on the inbox forwarder resolves the collection from the caption (or `personal_knowledge`) and POSTs `/api/rag/ingest-path` — the file never leaves the machine, no re-upload. Default dashboard URL `https://127.0.0.1:8000`, same token source as messages.

## 7. Test evidence

All offline: temp knowledge dirs, temp callers file, deterministic fake embedder, raw-syntax PDF fixtures with a real text layer (no OCR detours, no committed binaries). Run: `.venv/bin/python -m pytest tests/test_rag_service.py tests/test_rag_api.py -q` → **56 passed**.

- `tests/test_rag_service.py` (~50 tests)
  - **Ingestion**: real PDF → verbatim original, registry (filetype/access/metadata/version), page-aware chunks, vectors present; dedup by sha256; unknown collection refused.
  - **Retrieval**: question → sorted hits with all citation fields; citations block (`"Options Playbook, p.N [strategies]"`); bounded evidence (`[1] …`, ≤ 12 010 chars); doc-scoped search; unknown doc error; **no-LLM-send** — a spy proves search embeds only the query string.
  - **ACL**: owner sees all; scoped reader only granted collection; unknown caller blind; scope cannot be widened; out-of-scope doc_id refused; read-only caller blocked from write/delete/modify; granted writer can ingest into its collection but not others; every denial audited (`svc:denied`, `[mallory]`).
  - **Update**: metadata patch without reindex (+ access propagates to rows); content replacement swaps rows, bumps version, old text gone.
  - **Delete**: forget removes original + registry + rows, audit kept.
  - **Formats**: md/docx/txt through the same pipeline; unsupported suffix refused; page-less formats never touch OCR.
  - **Text ingest**: roundtrip + metadata; empty refused.
- `tests/test_rag_api.py`
  - **REST flow**: upload → 200 (`filetype pdf, pages 2, chunks ≥ 2`) → documents/stats → query → hits + citations + evidence → doc-scoped query; ingest-text + forget; ingest-path with metadata channel; update-meta; content update → version 2; empty query → 400; no file → 400.
  - **Auth**: missing/bad token → 401 on every endpoint (parametrised).
  - **ACL over HTTP**: scoped caller cross-collection → 403; read-only ingest → 403; unknown caller gets empty 200s; list filtered.
  - **rag_agent**: read → COMPLETED with evidence + citations; scoped caller; empty library → COMPLETED with empty citations; write gated → FAILED; env-on replaces the stub under the same name; default off → stub.
  - **Hermes inbox**: `--rag` parses to a correct `/api/rag/ingest-path` POST; missing file → rc=1 with message; missing token → rc=1.

### Regression status

- `tests/test_rag_offline.py`, `tests/test_orchestrator.py`, `tests/test_pdf_ocr.py` — **all green** (existing behaviour intact; `core/rag.py` is backward compatible).
- `tests/test_dashboard_offline.py::test_offline_flow` — **fails, pre-existing, unrelated**: it calls `DashboardServer._load_devices()` (line 95), a method that does not exist anywhere in `dashboard/` (device-token persistence was never implemented; the reference exists in the committed test at HEAD, commit 3643e76). Track separately from the RAG work.

## 8. Deliberate limits (Phase-2 candidates)

- **Storage**: single local `vectors.npz` + `chunks.jsonl` (append + per-doc row rewrite). Scales to thousands of chunks; a real vector DB (sqlite-vec/qdrant) is a drop-in behind `core/rag.py` when needed.
- **Retrieval**: pure semantic (+ lexical fallback). No hybrid ranking/rerank yet.
- **Access model**: collection-level grants + write flag; no row-level or per-document sharing.
- **rag_agent** ships env-off; flip `JARVIS_RAG_AGENT=on` when the orchestrator goes live.
- Pre-existing dashboard gap: device-token persistence (`_load_devices`) needed by `test_dashboard_offline.py`.
