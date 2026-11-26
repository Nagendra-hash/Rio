"""
orchestrator/agents/metaquest/metaquest_agent.py — the real Meta Quest
knowledge specialist (Phase 15) on the BaseAgent contract under the
canonical name `metaquest_agent`, enabled with JARVIS_METAQUEST_AGENT=on
(shadow-mode default).

ACTIONS

    ask (default)      rag_service.answer as caller metaquest_agent,
                       collection-pinned to metaquest_docs. The cited
                       answer carries product/version metadata per cited
                       document ("Quest 3 [v67, collected 2026-09-01]");
                       an ask with zero results is an honest refusal, not
                       a guess. metadata.strategy picks the Phase-4 RAG
                       strategy (auto default).
    search (read)      rag_service.search — the chunk-level view with
                       citations + per-chunk product/version tags.
    ingest             ADD/UPDATE a manual/PDF into the dedicated
                       collection: validates the file, extracts product +
                       version (title, current known products, explicit
                       metadata), then parks the EXACT payload behind
                       NEEDS_APPROVAL. Resume ingests through the owner
                       caller and stamps doc metadata after the write —
                       the resume never re-parses the user's words.
    set_doc_meta       attach product/version/collected to an existing
                       doc id (all citations of that doc then carry it).
    add_product        record a known product bump ("quest 3 v69 …").
    corpus             collection status: how many docs/chunks, per
                       product, per version — the honest state of the
                       specialist's corpus.

`handle()` NEVER raises.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from core import rag_service
from core.rag_strategies import heuristics as rag_heuristics
from orchestrator.agents.metaquest import store as mq_store
from orchestrator.agents.metaquest.store import (COLLECTION, MetaQuestStore,
                                                 canon_product,
                                                 extract_version)
from orchestrator.agents.productivity import security as prod_security
from orchestrator.agents.productivity.checkpoint import ProductivityCheckpoint
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)

log = logging.getLogger("jarvis.metaquest")

MAX_ASK_CHARS = 400
# a chunk must cover at least this share of the question's terms to count
# as a citation for it (deterministic, direction-checked)
MIN_QUESTION_COVERAGE = 0.25
MIN_QUERY_COVERAGE = 0.3      # search view: stricter (a lookup, not an answer)
SUPPORTED_SUFFIXES = (".pdf", ".txt", ".md", ".log", ".csv", ".docx")


class MetaQuestCheckpoint(ProductivityCheckpoint):
    """Quest-owned checkpoints (duration-safe, same contract)."""

    def __init__(self, directory=None) -> None:
        super().__init__(directory or (mq_store.metaquest_dir()
                                       / "checkpoints"))

    def save(self, state: dict):
        try:
            import datetime as _dt
            import uuid as _uuid
            stamp = _dt.datetime.now(_dt.timezone.utc) \
                .strftime("%Y%m%dT%H%M%S")
            path = self.dir / f"metaquest-{stamp}-" \
                             f"{_uuid.uuid4().hex[:4]}.json"
            if self._write(path, state):
                self._write(self.dir / "latest.json", state)
                self._prune()
                return path
            return None
        except Exception:
            return None

    def _prune(self) -> None:
        try:
            snaps = sorted(p for p in self.dir.glob("metaquest-*.json"))
            for old in snaps[:-MAX_CHECKPOINTS_]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    def list_ids(self) -> list[str]:
        try:
            return sorted(p.stem for p in self.dir.glob("metaquest-*.json"))
        except Exception:
            return []


MAX_CHECKPOINTS_ = 20


class MetaQuestAgent(BaseAgent):
    name = "metaquest_agent"
    description = (
        "Meta Quest knowledge specialist: searches ONLY the dedicated "
        "metaquest_docs collection (manuals, changelogs, your own Quest "
        "notes), answers with [n] citations carrying product and version "
        "metadata, and ingests new manuals only after your approval.")
    task_types = (TaskType.META_QUEST,)

    def __init__(self, store: MetaQuestStore | None = None,
                 checkpoint: MetaQuestCheckpoint | None = None) -> None:
        self.store = store or MetaQuestStore()
        self.checkpoint = checkpoint or MetaQuestCheckpoint()

    # ── entry ───────────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            meta = request.metadata or {}
            if meta.get("approved") and meta.get("resume"):
                return self._resume_approved(request, str(meta["resume"]),
                                             str(meta.get("by") or "user"))
            action = str(meta.get("quest_action") or "").strip().lower() \
                or self._intent(request.text)
            handler = self._HANDLERS.get(action, MetaQuestAgent._ask)
            return handler(self, request)
        except Exception as exc:                  # never raise outward
            log.warning("metaquest agent error: %s", type(exc).__name__)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error="metaquest action failed",
                               agent=self.name)

    @staticmethod
    def _intent(text: str) -> str:
        t = (text or "").lower()
        import re as _re
        if _re.search(r"\b(?:ingest|remember|add)\b.{0,40}"
                      r"\b(?:manual|pdf|doc|guide|changelog)\b", t):
            return "ingest"
        if _re.search(r"\b(?:corpus|library status|what docs|what manuals"
                      r"\b|coverage)\b", t):
            return "corpus"
        if _re.search(r"\b(?:find|search|list)\b.{0,30}\bpassages?\b", t):
            return "search"
        return "ask"

    # ── helpers ─────────────────────────────────────────────────────────
    def _reply(self, request, text, *, action,
               status: TaskStatus = TaskStatus.COMPLETED,
               extra: dict | None = None) -> AgentResult:
        return AgentResult(
            task_id=request.task_id, status=status,
            text=prod_security.redact(text),
            data={"metaquest": True, "action": action, **(extra or {})},
            agent=self.name)

    def _snapshot(self, kind: str) -> None:
        try:
            self.checkpoint.save({"kind": kind, "state": self.store.summary()})
        except Exception:
            pass

    def _format_cites(self, results: list[dict],
                      meta_by_doc: dict | None = None) -> list[str]:
        """Citation strings WITH product/version metadata: 'Manual title
        [Quest 3 · v67 · collected 2026-09-01] (p.12)'."""
        meta_by_doc = meta_by_doc if isinstance(meta_by_doc, dict) else {}
        cites: list[str] = []
        for r in results or []:
            if not isinstance(r, dict):
                continue
            base = (r.get("source") or "unknown source").strip()
            doc_id = str(r.get("doc_id") or "")
            stored = self.store.doc_meta(doc_id)
            md = r.get("metadata") or {}
            meta_by_doc.setdefault(doc_id, {})
            meta_by_doc[doc_id].update({
                "product": stored.get("product") or md.get("product") or "",
                "version": stored.get("version") or md.get("version") or "",
                "collected": stored.get("collected")
                or md.get("collected") or ""})
            bits: list[str] = []
            if stored.get("product") or md.get("product"):
                bits.append(str(stored.get("product") or md.get("product")))
            if stored.get("version") or md.get("version"):
                v = str(stored.get("version") or md.get("version"))
                bits.append(f"v{v.lstrip('v')}" if v else "")
            if stored.get("collected") or md.get("collected"):
                bits.append(f"collected {stored.get('collected') or md.get('collected')}")
            if r.get("page"):
                cite = f"{base} [{', '.join([b for b in bits if b])}]" \
                    if bits else base
                cite += f" (p.{r['page']})"
            else:
                cite = f"{base} [{', '.join([b for b in bits if b])}]" \
                    if bits else base
            if r.get("collection"):
                cite += f" [{r['collection']}]"
            cites.append(cite)
        meta_by_doc.pop("_order", None)
        return cites

    # ── ask ─────────────────────────────────────────────────────────────
    def _ask(self, request) -> AgentResult:
        question = (request.text or "").strip()[:MAX_ASK_CHARS]
        meta = request.metadata or {}
        if not question:
            return self._reply(
                request, "What about your Quest? (searching the "
                         "metaquest_docs collection)", action="ask",
                status=TaskStatus.NEEDS_INPUT)
        if canon_product(question):
            self.store.upsert_product(canon_product(question))
        result = rag_service.answer(
            caller="metaquest_agent", query=question,
            strategy=str(meta.get("strategy") or "auto"),
            collection=COLLECTION, max_chars=8000)
        if not result.get("ok"):
            return self._reply(
                request, f"Knowledge lookup not permitted: "
                         f"{result.get('error')}. Update "
                         "config/rag_callers.json to grant "
                         "'metaquest_agent' the metaquest_docs "
                         "collection.",
                action="ask")
        cites = self._format_cites(result.get("used_evidence", []))
        answer = result.get("answer") or ""
        used = [e for e in (result.get("used_evidence") or [])
                if isinstance(e, dict)]
        # Weak-match gate: a chunk that barely covers the QUESTION's terms
        # is not a citation for an answer — refuse rather than dress noise
        # up as sourced fact. Direction-checked via the RAG strategies'
        # support_score (question ⊂ chunk).
        best = max((rag_heuristics.support_score(question,
                                                 e.get("text") or "")
                    for e in used), default=0.0)
        if not used or not cites or best < MIN_QUESTION_COVERAGE:
            return self._reply(
                request,
                "I can't answer that from the Quest collection — no cited "
                "material matched. Say 'corpus' to see what's indexed, "
                "or ingest the manual with metadata {quest_action: "
                "'ingest', path: …}.", action="ask",
                extra={"query": question, "groundless": True})
        # the answer must be cited; the extractive floor already is. When
        # the LLM answerer produced an uncited answer we show the
        # evidence-backed extractive note instead of the risky text.
        if "[" not in answer or result.get("hallucination_risk") == "high":
            answer = _evidence_summary(used)
        else:
            answer = _keep_cited(answer, cites)
        citations = cites
        data = {"query": question,
                "citations": citations,
                "strategy": result.get("strategy"),
                "hallucination_risk": result.get("hallucination_risk"),
                "grounded": bool(result.get("grounded")),
                "cited_docs": sorted({c.get("doc_id") for c in
                                      (result.get("used_evidence") or [])
                                      if isinstance(c, dict)})}
        text = (f"{answer}\n\nSources:\n  " + "\n  ".join(citations)
                + ("\nDo not trust hardware claims without their version "
                   "note." if len(citations) <= 1 else ""))
        return self._reply(request, text, action="ask", extra=data)

    # ── search (chunk view) ─────────────────────────────────────────────
    def _search_action(self, request) -> AgentResult:
        meta = request.metadata or {}
        query = (request.text or "").strip()[:MAX_ASK_CHARS]
        if not query:
            return self._reply(
                request, "What should I look for in the Quest manuals?",
                action="search", status=TaskStatus.NEEDS_INPUT)
        result = rag_service.search(
            caller="metaquest_agent", query=query,
            top_k=max(1, min(int(meta.get("top_k") or 5), 10)),
            collection=COLLECTION, max_chars=8000)
        if not result.get("ok"):
            return self._reply(
                request, f"Search not permitted: {result.get('error')}",
                action="search")
        # same weak-match gate as ask(): a passage must cover a real share
        # of the query's terms to be shown at all
        rows = [r for r in (result.get("results") or [])
                if rag_heuristics.support_score(query, r.get("text") or "")
                >= MIN_QUERY_COVERAGE]
        if not rows:
            return self._reply(
                request, "No relevant passages in the metaquest_docs "
                         "collection. 'corpus' shows what's indexed; "
                         "'ingest' adds a manual.",
                action="search", extra={"query": query})
        cites = self._format_cites(rows)
        lines = [f"Found {len(rows)} passage(s) in the Quest "
                 "collection:"]
        for r, c in zip(rows, cites):
            snippet = str(r.get("text") or "").strip()
            snippet = snippet if len(snippet) <= 260 else snippet[:257] + "…"
            lines.append(f"  [{c}]\n    {snippet}")
        return self._reply(request, "\n".join(lines), action="search",
                           extra={"citations": cites,
                                  "query": query})

    # ── ingest (approval-gated) ─────────────────────────────────────────
    def _ingest(self, request) -> AgentResult:
        meta = request.metadata or {}
        path = str(meta.get("path") or "").strip()
        if not path:
            return self._reply(
                request, "Which file? metadata {quest_action: 'ingest', "
                         f"path: '/path/to/manual.pdf', product: 'Quest "
                         "3', version: 'v67', collected: '2026-10-07'}.",
                action="ingest", status=TaskStatus.NEEDS_INPUT)
        fp = Path(path).expanduser()
        if not fp.exists() or not fp.is_file():
            return self._reply(request, f"No file at {path}.",
                               action="ingest")
        if fp.suffix.lower() not in SUPPORTED_SUFFIXES:
            return self._reply(
                request,
                f"Unsupported file type {fp.suffix!r} — ingest PDF, txt, "
                "md, log, csv or docx manuals.", action="ingest")
        product = str(meta.get("product") or "").strip() \
            or canon_product(fp.name) or canon_product(meta.get("title", ""))
        version = str(meta.get("version") or "").strip() \
            or extract_version(fp.name)
        title = str(meta.get("title") or "").strip() or fp.stem[:120]
        collected = str(meta.get("collected") or "").strip()[:10]
        payload = {"path": str(fp), "title": title, "product": product,
                   "version": version, "collected": collected,
                   "collection": COLLECTION,
                   "access": str(meta.get("access") or "private")}
        detail = (f"ingest into the metaquest_docs collection: "
                  f"{fp.name} as '{title}'"
                  + (f" [{product}" + (f" · v{version}" if version else "")
                     + "]" if product else "")
                  + ". Nothing is saved until you approve.")
        cp = {"task_id": request.task_id, "agent": self.name,
              "action": "ingest_doc", "payload": payload,
              "status": "awaiting_approval"}
        self.checkpoint.save_for(request.task_id, cp)
        self.store.audit("parked_ingest")
        return prod_security.approval_result(request, self.name,
                                             "ingest_quest_doc", detail)

    def _resume_approved(self, request, resume_id: str, by: str) -> AgentResult:
        cp = self.checkpoint.load_for(resume_id)
        if not cp or cp.get("task_id") != resume_id \
                or cp.get("agent") != self.name:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED,
                text=f"No pending approval checkpoint for {resume_id!r} "
                     "— nothing changed.", agent=self.name)
        if cp.get("status") != "awaiting_approval":
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.DECLINED,
                text=f"Approval {resume_id!r} is '{cp.get('status')}' — "
                     "already handled.", agent=self.name)
        payload = cp.get("payload") or {}
        result = rag_service.ingest(
            CALLER_OWNER, file_path=str(payload.get("path")),
            collection=COLLECTION, title=str(payload.get("title") or ""),
            source=f"metaquest_agent-approved",
            access=str(payload.get("access") or "private"))
        if not result.get("ok"):
            cp["status"] = "failed"
            self.checkpoint.save_for(resume_id, cp)
            return self._reply(
                request,
                f"Ingest failed (approved by {by}): {result.get('error')}. "
                "Nothing was saved.", action="ingest")
        doc_id = str(result.get("doc_id") or "")
        product = str(payload.get("product") or "")
        version = str(payload.get("version") or "")
        collected = str(payload.get("collected") or "")[:10]
        if product or version or collected:
            self.store.set_doc_meta(doc_id, product=product, version=version,
                                    collected=collected)
        if product:
            self.store.upsert_product(product, version)
        cp["status"] = f"ingested:{doc_id}"
        self.checkpoint.save_for(resume_id, cp)
        self._snapshot("ingest")
        return self._reply(
            request,
            f"Ingested (approved by {by}): '{result.get('title')}' "
            f"[doc {doc_id}] into metaquest_docs with product="
            f"{product or 'unlabeled'}, version="
            f"{version or 'unspecified'}, collected={collected or ''}. "
            "Citations of this doc will carry that metadata.",
            action="ingest", extra={"doc_id": doc_id, "product": product,
                                    "version": version,
                                    "collection": COLLECTION})

    # ── metadata maintenance ────────────────────────────────────────────
    def _set_doc_meta(self, request) -> AgentResult:
        meta = request.metadata or {}
        doc_id = str(meta.get("doc_id") or "").strip()
        if not doc_id:
            return self._reply(request,
                               "metadata {doc_id, product?, version?, "
                               "collected?} labels a doc for citations.",
                               action="set_doc_meta",
                               status=TaskStatus.NEEDS_INPUT)
        row = self.store.set_doc_meta(doc_id, product=str(meta.get("product")
                                                         or ""),
                                      version=str(meta.get("version") or ""),
                                      collected=str(meta.get("collected")
                                                   or ""))
        self._snapshot("doc_meta")
        return self._reply(
            request,
            f"Doc {doc_id} now carries product='{row.get('product')}', "
            f"version='{row.get('version')}', collected="
            f"'{row.get('collected')}'. Every future citation of it "
            "shows that.", action="set_doc_meta", extra={"doc_meta": row})

    def _add_product(self, request) -> AgentResult:
        meta = request.metadata or {}
        text = request.text or ""
        name = str(meta.get("product") or canon_product(text) or "").strip()
        version = str(meta.get("version") or extract_version(text)).strip()
        if not name:
            return self._reply(
                request, "Which product? metadata {product: 'Quest 3', "
                         "version: 'v69'}.", action="add_product",
                status=TaskStatus.NEEDS_INPUT)
        row = self.store.upsert_product(name, version)
        self._snapshot("product")
        return self._reply(
            request, f"Known product: {row['name']} — latest version "
                     f"{'v' + row['latest_version'] if row['latest_version'] else '(none)'}",
            action="add_product", extra={"product": row})

    # ── corpus status ───────────────────────────────────────────────────
    def _corpus(self, request) -> AgentResult:
        listing = rag_service.list_documents(caller="metaquest_agent",
                                             collection=COLLECTION)
        docs = listing.get("documents") or []
        enriched = self.store.enrich(docs)
        lines = [f"Corpus — {COLLECTION}: {len(enriched)} document(s)."]
        if not enriched:
            lines.append("  (empty — 'ingest' a manual to start)")
        by_product: dict[str, list[dict]] = {}
        for d in enriched:
            by_product.setdefault(d.get("product") or "unlabeled",
                                  []).append(d)
        for product, rows in sorted(by_product.items()):
            chunks = sum(int(r.get("chunk_count") or 0) for r in rows)
            lines.append(f"  • {product}: {len(rows)} doc(s), "
                         f"{chunks} chunk(s)")
            for r in rows:
                versions = (r.get("version") or "unspecified")
                extra = (f" — {r.get('original_name')} "
                         f"[v{versions}, collected {r.get('collected')}]"
                         if r.get("original_name") else "")
                lines.append(f"      {r.get('doc_id')} {extra}")
        self.store.audit("corpus_listed", docs=len(enriched))
        return self._reply(request, "\n".join(lines), action="corpus",
                           extra={"docs": [d.get("doc_id")
                                           for d in enriched]})

    _HANDLERS = {
        "ask": _ask,
        "search": _search_action,
        "ingest": _ingest,
        "set_doc_meta": _set_doc_meta,
        "add_product": _add_product,
        "corpus": _corpus,
        "status": _corpus,
    }


CALLER_OWNER = "jarvis"          # the ingest path needs can_write


# ── text helpers ────────────────────────────────────────────────────────

def _evidence_summary(used_evidence: list[dict]) -> str:
    """Extractive fallback that always cites: first sentence of each used
    block, prefixed by its [n]."""
    out: list[str] = []
    for i, e in enumerate(used_evidence[:4], 1):
        if not isinstance(e, dict):
            continue
        text = str(e.get("text") or "").strip()
        first = next((s for s in text.replace("\n", " ").split(". ")
                      if len(s.split()) >= 6), text[:120])
        out.append(f"{first.strip()} [{i}]")
    return "\n".join(out)


def _keep_cited(answer: str, citations: list[str]) -> str:
    """Trim an LLM answer's trailing citation list if it repeats our own
    rich list; return the prose body only."""
    if "\nSources:" in answer:
        body = answer.split("\nSources:", 1)[0]
        return body.strip()
    return answer.strip()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")
