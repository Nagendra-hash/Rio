"""
orchestrator/agents/legal/legal_agent.py — the real Legal Document
Analysis agent (Phase 16) on the BaseAgent contract under the canonical
name `legal_agent`, enabled with JARVIS_LEGAL_AGENT=on (shadow-mode
default).

ACTIONS

    analyze (default)   full pass over an INGESTED document (or a fresh
                        file given as metadata.path): clause extraction
                        + extractive summary + risk/questions report,
                        all page-cited.
    clauses             the clause list for one document (type, text,
                        page) from the vault cache.
    summarize           the extractive summary alone.
    retrieve            rag_service.search as caller legal_agent,
                        collection-pinned to legal_docs — relevant
                        passages with [n] citations, zero results is an
                        honest refusal, never a guess.
    ingest              approval-gated ADD of a legal document to the
                        vault + the legal_docs collection; resume
                        commits the EXACT parked payload and stamps the
                        supersession chain.
    forget              approval-gated REMOVE of a document.
    compare             structural version diff of two ingested docs
                        (changed / added / removed clauses, cited).
    library             what is in the vault: docs, kinds, chains.
    ask                 rag_service.answer over legal_docs for a
                        question the user wants quoted from their docs.

`handle()` NEVER raises. Advice-shaped asks are DECLINED in code
(safety.is_advice_request). Every substantive reply ends with the
NOT-LEGAL-ADVICE disclaimer and the "invite qualified legal review"
nudge.
"""
from __future__ import annotations

import logging
from pathlib import Path

from core import rag_service
from orchestrator.agents.legal import (clauses as lg_clauses,
                                       diff as lg_diff, pages as lg_pages,
                                       risk as lg_risk, safety)
from orchestrator.agents.legal.checkpoint import LegalCheckpoint
from orchestrator.agents.legal.schema import (Clause, DocumentMeta,
                                              validate_document_meta)
from orchestrator.agents.legal.vault import (COLLECTION, LegalVault,
                                             new_doc_id)
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)

log = logging.getLogger("jarvis.legal")

MAX_TEXT_CHARS = 400_000
MAX_ASK_CHARS = 400
SUPPORTED_SUFFIXES = (".pdf", ".txt", ".md", ".log", ".csv", ".docx")
# a passage must cover at least this share of the query's terms to count
# as a citation (deterministic, direction-checked via support_score)
MIN_QUERY_COVERAGE = 0.25


class LegalAgent(BaseAgent):
    name = "legal_agent"
    description = (
        "Legal document analysis: reads legal PDFs (and text/docx), "
        "extracts clauses with page/section citations, summarizes "
        "sections, compares document versions, retrieves passages, and "
        "flags risks and open questions for human review. Not legal "
        "advice — a qualified legal review is encouraged; ingest and "
        "forget need your approval; documents live in the isolated "
        "legal_docs collection.")
    task_types = (TaskType.LEGAL,)

    def __init__(self, store: LegalVault | None = None,
                 checkpoint: LegalCheckpoint | None = None) -> None:
        self.store = store or LegalVault()
        self.checkpoint = checkpoint or LegalCheckpoint()

    # ── entry ───────────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            meta = request.metadata or {}
            if meta.get("approved") and meta.get("resume"):
                return self._resume_approved(request, str(meta["resume"]),
                                             str(meta.get("by") or "user"))
            if safety.is_advice_request(request.text):
                return self._decline_advice(request)
            if safety.is_blocked_request(request.text):
                return self._decline_blocked(request)
            action = str(meta.get("legal_action") or "").strip().lower() \
                or self._intent(request.text)
            handler = self._HANDLERS.get(action, LegalAgent._analyze)
            return handler(self, request)
        except Exception as exc:                  # never raise outward
            log.warning("legal agent error: %s", type(exc).__name__)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error="legal action failed",
                               agent=self.name)

    @staticmethod
    def _intent(text: str) -> str:
        import re as _re
        t = " ".join((text or "").lower().split())
        if _re.search(r"\b(?:forget|delete|remove)\b.{0,30}\b(?:document|contract|agreement)\b", t):
            return "forget"
        if _re.search(r"\b(?:ingest|add|remember|store|index)\b.{0,40}\b(?:document|pdf|contract|agreement|lease)\b", t) or "ingest" in t:
            return "ingest"
        if _re.search(r"\b(?:compare|diff)\b", t):
            return "compare"
        if _re.search(r"\b(?:library|vault|what docs|what documents|corpus)\b", t):
            return "library"
        if _re.search(r"\b(?:retrieve|find|search|show|where)\b", t):
            return "retrieve"
        if _re.search(r"\bclauses?\b", t):
            return "clauses"
        if _re.search(r"\b(?:summar(?:y|ize|ise)|tl;?dr)\b", t):
            return "summarize"
        return "analyze"

    # ── helpers ─────────────────────────────────────────────────────────
    def _reply(self, request, text, *, action,
               status: TaskStatus = TaskStatus.COMPLETED,
               extra: dict | None = None) -> AgentResult:
        from orchestrator.agents.productivity import security as prod_sec
        body = prod_sec.redact(str(text or ""))
        if status is TaskStatus.COMPLETED:
            if action not in ("library",):
                body = (body.rstrip()
                        + ("\n\n" if body else "")
                        + safety.DISCLAIMER_TEXT)
        return AgentResult(
            task_id=request.task_id, status=status, text=body,
            data={"legal": True, "action": action, **(extra or {})},
            agent=self.name)

    def _decline_advice(self, request) -> AgentResult:
        self.store.audit("advice_declined")
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.DECLINED,
            text=safety.ADVICE_REFUSAL,
            data={"legal": True, "action": "advice_refused",
                  "refusal": True},
            agent=self.name)

    def _decline_blocked(self, request) -> AgentResult:
        self.store.audit("blocked_action_declined")
        return self._reply(
            request,
            "That's outside what I do: I don't draft legal instruments, "
            "sign, notarize, contact counterparties or touch privileged "
            "material. I can read and quote documents you own — but "
            "drafting and filing is your lawyer's job.",
            action="blocked", extra={"refusal": True})

    def _park(self, request, action: str, payload: dict,
              detail: str) -> AgentResult:
        cp = {"task_id": request.task_id, "agent": self.name,
              "action": action, "payload": payload,
              "status": "awaiting_approval"}
        self.checkpoint.save_for(request.task_id, cp)
        self.store.audit(f"parked_{action}")
        return safety.approval_result(request, self.name, action, detail)

    def _find_doc(self, meta: dict, request_text: str) -> tuple[str, str]:
        """The doc_id a read-only action points at (explicit metadata
        beats the most recent ingest)."""
        doc_id = str(meta.get("doc_id") or "").strip()
        if doc_id:
            hit = self.store.get_document(doc_id)
            if hit:
                return doc_id, ""
            return "", f"no document {doc_id!r} in the vault"
        docs = self.store.documents()
        if not docs:
            return "", ("the vault is empty — ingest a document first "
                        "(metadata {legal_action: 'ingest', path: …})")
        newest = max(docs, key=lambda r: str(r.get("ingested_at") or ""))
        return str(newest.get("doc_id") or ""), ""

    def _clauses_of(self, doc_id: str) -> list[Clause]:
        raw = self.store.load_clauses(doc_id)
        out: list[Clause] = []
        for row in raw:
            cl, _errs = None, None
            try:
                from orchestrator.agents.legal.schema import validate_clause
                cl, _errs = validate_clause(row)
            except Exception:
                cl = None
            if cl is not None:
                out.append(cl)
        return out

    # ── ingest (approval-gated) ─────────────────────────────────────────
    def _ingest(self, request) -> AgentResult:
        meta = request.metadata or {}
        path = str(meta.get("path") or "").strip()
        if not path:
            att = [a for a in (request.attachments or []) if a]
            if att:
                path = att[0]
        if not path:
            return self._reply(
                request,
                "Which file? metadata {legal_action: 'ingest', path: "
                "'/path/to/contract.pdf', title?: '…', kind?: '…', "
                "supersedes?: 'lg-…'} — or attach the PDF to the message.",
                action="ingest", status=TaskStatus.NEEDS_INPUT)
        fp = Path(path).expanduser()
        if not fp.exists() or not fp.is_file():
            return self._reply(request, f"No file at {path}.",
                               action="ingest")
        if fp.suffix.lower() not in SUPPORTED_SUFFIXES:
            return self._reply(
                request, f"Unsupported file type {fp.suffix!r} — ingest "
                         "PDF, txt/md/log/csv or docx legal documents.",
                action="ingest")
        if fp.stat().st_size > 40_000_000:
            return self._reply(request,
                               "File over 40 MB — split it first.",
                               action="ingest")
        read = lg_pages.read_pages(fp)
        if not read.ok:
            return self._reply(request, f"Could not read it: "
                                        f"{read.error} Nothing was stored.",
                               action="ingest")
        prev = None
        supersedes = str(meta.get("supersedes") or "").strip()
        if supersedes:
            prev = self.store.get_document(supersedes)
            if not prev:
                return self._reply(
                    request,
                    f"supersedes doc {supersedes!r} is not in the vault "
                    "— ingest it first or drop the supersedes field.",
                    action="ingest")
            version = int(prev.get("version") or 1) + 1
        else:
            version = 1
        title = str(meta.get("title") or "").strip() or fp.stem[:200]
        kind = str(meta.get("kind") or "").strip().lower() or read.kind
        doc_id = new_doc_id(title)
        payload = {"path": str(fp), "title": title, "kind": kind,
                   "doc_id": doc_id, "version": version,
                   "supersedes": supersedes, "sha256": read.sha256,
                   "pages": read.page_count, "collection": COLLECTION}
        detail = (f"add legal document '{title}' ({fp.name}, "
                  f"kind={kind}, pages={read.page_count}"
                  + (f", supersedes {supersedes}" if supersedes else "")
                  + ") to the isolated legal vault and the "
                  f"{COLLECTION} collection. Nothing is stored until "
                  "you approve.")
        return self._park(request, "ingest_document", payload, detail)

    def _forget(self, request) -> AgentResult:
        meta = request.metadata or {}
        doc_id = str(meta.get("doc_id") or "").strip()
        if not doc_id:
            return self._reply(request,
                               "Which doc? metadata {doc_id: 'lg-…'}. "
                               "Say 'library' to list what's in the "
                               "vault.", action="forget",
                               status=TaskStatus.NEEDS_INPUT)
        hit = self.store.get_document(doc_id)
        if not hit:
            return self._reply(request,
                               f"No document {doc_id!r} in the vault.",
                               action="forget")
        detail = (f"DELETE legal document {hit.get('title')!r} "
                  f"[{doc_id}] from the vault")
        return self._park(request, "forget_document",
                          {"doc_id": doc_id}, detail)

    def _resume_approved(self, request, resume_id: str, by: str) -> AgentResult:
        # The checkpoint is keyed by whatever task id the dispatcher used
        # when the action was PARKED (usually the orchestrator-pinned id,
        # not necessarily today's). An approval with no explicit resume
        # id re-dispatches the same task id; a named resume id is resolved
        # to that checkpoint directly. Either way, ONLY the parked
        # payload executes.
        cp = None
        candidates = [resume_id] if resume_id and resume_id != "true" else []
        if request.task_id and request.task_id not in candidates:
            candidates.insert(0, request.task_id)
        for cand in candidates:
            hit = self.checkpoint.load_for(cand)
            if hit and hit.get("agent") == self.name:
                cp = hit
                resume_id = cand
                break
        if not cp:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED,
                text=f"No pending approval checkpoint for {resume_id!r} "
                     "— nothing changed.", agent=self.name)
        if cp.get("status") != "awaiting_approval":
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.DECLINED,
                text=f"Approval {resume_id!r} is '{cp.get('status')}' — "
                     "already handled.", agent=self.name)
        action = cp.get("action")
        payload = cp.get("payload") or {}

        if action == "ingest_document":
            return self._commit_ingest(request, resume_id, payload, by)
        if action == "forget_document":
            return self._commit_forget(request, resume_id, payload, by)
        cp["status"] = "failed"
        self.checkpoint.save_for(resume_id, cp)
        return AgentResult(task_id=request.task_id, status=TaskStatus.FAILED,
                           text=f"Unknown approved action {action!r}.",
                           agent=self.name)

    def _commit_ingest(self, request, resume_id: str,
                       payload: dict, by: str) -> AgentResult:
        fp = Path(str(payload.get("path") or "")).expanduser()
        read = lg_pages.read_pages(fp)
        if not read.ok:
            return self._reply(
                request,
                f"Ingest failed (approved by {by}): {read.error}. "
                "Nothing was stored.", action="ingest")
        supersedes = str(payload.get("supersedes") or "")
        prev = self.store.get_document(supersedes) if supersedes else None
        version = (int(prev.get("version") or 1) + 1) if prev else 1
        meta = DocumentMeta(
            doc_id=str(payload.get("doc_id") or new_doc_id()),
            title=str(payload.get("title") or ""), kind=str(payload.get("kind") or "other"),
            version=version, supersedes=supersedes,
            source_path=str(fp), sha256=read.sha256,
            pages=read.page_count, chars=len(read.full_text))
        meta, _errs = validate_document_meta(meta)
        self.store.register_document(meta)
        self.store.save_page_text(meta.doc_id, [
            {"num": p.num, "text": p.text} for p in read.pages])
        cls = lg_clauses.extract_clauses(read, doc_id=meta.doc_id)
        self.store.save_clauses(meta.doc_id, [c.to_dict() for c in cls])
        ing = self._rag_ingest(read, meta)
        cp = {"task_id": resume_id, "agent": self.name,
              "action": "ingest_document", "payload": payload,
              "status": f"ingested:{meta.doc_id}"}
        self.checkpoint.save_for(resume_id, cp)
        self.store.audit("ingested", doc_id=meta.doc_id, kind=meta.kind,
                         pages=meta.pages, version=meta.version,
                         rag_ingested=bool(ing.get("ok")))
        lines = [f"Stored (approved by {by}): '{meta.title}'",
                 f"  doc id: {meta.doc_id}",
                 f"  kind: {meta.kind} · pages: {meta.pages} · "
                 f"clauses extracted: {len(cls)} · chars: {meta.chars}"]
        if prev:
            lines.append(f"  version {meta.version} — supersedes "
                         f"{prev.get('doc_id')} ('{prev.get('title')}')")
        if ing.get("ok"):
            lines.append(f"  retrieval: indexed into {COLLECTION}"
                         + (" (duplicate already there)" if ing.get("duplicate")
                            else ""))
        else:
            lines.append(f"  retrieval NOT available: {ing.get('error')}"
                         " — the vault copy works, but 'retrieve' needs "
                         "the index; fix rag config and re-ingest.")
        return self._reply(request, "\n".join(lines), action="ingest",
                           extra={"doc_id": meta.doc_id,
                                  "kind": meta.kind,
                                  "clauses": len(cls)})

    def _rag_ingest(self, read, meta: DocumentMeta) -> dict:
        """Index into the legal_docs collection — owner caller (can_write)."""
        text = read.full_text[:MAX_TEXT_CHARS]
        tmp = None
        try:
            import tempfile
            td = tempfile.mkdtemp(prefix="legal-ingest-")
            tmp = Path(td) / f"{meta.doc_id}.txt"
            tmp.write_text(text or "(no extractable text)", encoding="utf-8")
            return rag_service.ingest(
                "jarvis", file_path=str(tmp), collection=COLLECTION,
                title=meta.title, source=f"legal_agent:{meta.doc_id}",
                access="private", metadata={
                    "legal_doc_id": meta.doc_id, "kind": meta.kind,
                    "version": meta.version, "pages": meta.pages})
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                    tmp.parent.rmdir()
                except Exception:
                    pass

    def _commit_forget(self, request, resume_id: str,
                       payload: dict, by: str) -> AgentResult:
        doc_id = str(payload.get("doc_id") or "")
        hit = self.store.get_document(doc_id)
        self.store.forget_document(doc_id)
        # the retrieval copy goes too (owner caller)
        try:
            docs = [d for d in rag_service.list_documents(
                caller="jarvis", collection=COLLECTION)]
            for d in docs:
                if str((d.get("metadata") or {}).get("legal_doc_id")) == doc_id:
                    rag_service.forget("jarvis", str(d.get("doc_id")))
        except Exception:
            pass
        cp = {"task_id": resume_id, "agent": self.name,
              "action": "forget_document", "payload": payload,
              "status": "forgotten"}
        self.checkpoint.save_for(resume_id, cp)
        title = (hit or {}).get("title") or doc_id
        return self._reply(request, f"Deleted (approved by {by}): "
                                    f"'{title}' [{doc_id}] from the legal "
                                    "vault.", action="forget")

    # ── read-only actions ───────────────────────────────────────────────
    def _analyze(self, request) -> AgentResult:
        meta = request.metadata or {}
        doc_id, err = self._find_doc(meta, request.text)
        if err:
            return self._reply(request, err, action="analyze")
        doc = self.store.get_document(doc_id)
        if not doc:
            return self._reply(request,
                               f"No document {doc_id!r}.", action="analyze")
        rows = [dict(p) for p in self.store.load_page_text(doc_id)]
        if not rows:
            return self._reply(request,
                               "This document has stored page text but "
                               "no parsed pages? — re-ingest it.",
                               action="analyze",
                               status=TaskStatus.NEEDS_INPUT)
        read = lg_pages.ReadResult(
            ok=True, path=str(doc.get("source_path") or doc.get("title") or ""),
            pages=[lg_pages.Page(num=int(p.get("num") or 0),
                                 text=str(p.get("text") or ""))
                   for p in rows])
        cls = self._clauses_of(doc_id)
        sumry = lg_clauses.summarize_document(read)
        sumry.title_line = str(doc.get("title") or sumry.title_line)
        risks = lg_risk.scan_document(read, cls)
        self.store.audit("analyzed", doc_id=doc_id)
        lines = [f"Analysis of '{doc.get('title')}' [{doc_id}]"
                 f" — kind {doc.get('kind')}, {doc.get('pages')} pages, "
                 f"version {doc.get('version')}", ""]
        lines += sumry.to_lines()
        lines += ["", "─" * 46, "Questions for human review "
                  "(flagged passages; not legal advice):", ""]
        lines += risks.to_lines()
        extra = {"doc_id": doc_id, "kind": doc.get("kind"),
                 "clauses": len(cls),
                 "risk_counts": risks.counts,
                 "findings": [f.__dict__ for f in risks.findings[:20]]}
        return self._reply(request, "\n".join(lines), action="analyze",
                           extra=extra)

    def _summarize(self, request) -> AgentResult:
        meta = request.metadata or {}
        doc_id, err = self._find_doc(meta, request.text)
        if err:
            return self._reply(request, err, action="summarize")
        doc = self.store.get_document(doc_id)
        rows = [dict(p) for p in self.store.load_page_text(doc_id)]
        read = lg_pages.ReadResult(
            ok=True, path=str(doc.get("source_path") or ""),
            pages=[lg_pages.Page(num=int(p.get("num") or 0),
                                 text=str(p.get("text") or ""))
                   for p in rows])
        sumry = lg_clauses.summarize_document(read)
        sumry.title_line = str(doc.get("title") or sumry.title_line)
        self.store.audit("summarized", doc_id=doc_id)
        return self._reply(request, "\n".join(sumry.to_lines()),
                           action="summarize",
                           extra={"doc_id": doc_id,
                                  "sections": len(sumry.sections)})

    def _clauses_action(self, request) -> AgentResult:
        meta = request.metadata or {}
        doc_id, err = self._find_doc(meta, request.text)
        if err:
            return self._reply(request, err, action="clauses")
        cls = self._clauses_of(doc_id)
        if not cls:
            return self._reply(request,
                               "No clauses extracted for this document "
                               "yet.", action="clauses")
        lines = [f"Clauses extracted from [{doc_id}] — {len(cls)}:"]
        for c in cls[:25]:
            lines.append(f"  {c.n:3d}. [{c.clause_type}] {c.label}"
                         f"  ({c.citation()})")
        if len(cls) > 25:
            lines.append(f"  … and {len(cls) - 25} more")
        self.store.audit("clauses_listed", doc_id=doc_id)
        return self._reply(request, "\n".join(lines), action="clauses",
                           extra={"doc_id": doc_id, "n": len(cls)})

    def _retrieve(self, request) -> AgentResult:
        meta = request.metadata or {}
        query = str(meta.get("query") or request.text or "").strip()[:MAX_ASK_CHARS]
        if not query:
            return self._reply(request,
                               "What should I look for in your legal "
                               "documents?", action="retrieve",
                               status=TaskStatus.NEEDS_INPUT)
        result = rag_service.search(
            caller="legal_agent", query=query,
            top_k=max(1, min(int(meta.get("top_k") or 5), 10)),
            collection=COLLECTION, max_chars=8_000)
        if not result.get("ok"):
            return self._reply(
                request,
                f"Retrieval not permitted: {result.get('error')}. Grant "
                f"'legal_agent' the {COLLECTION} collection in "
                "config/rag_callers.json.", action="retrieve")
        from core.rag_strategies import heuristics as rag_heuristics
        rows = [r for r in (result.get("results") or [])
                if rag_heuristics.support_score(query, r.get("text") or "")
                >= MIN_QUERY_COVERAGE]
        if not rows:
            return self._reply(
                request,
                "No relevant passages in your legal documents for that. "
                "The scan is honest: I will not guess.", action="retrieve",
                extra={"query": query, "groundless": True})
        cites = rag_service.format_citations(rows)
        lines = [f"Relevant passages ({len(rows)}):"]
        for r, c in zip(rows, cites):
            snippet = " ".join(str(r.get("text") or "").split())
            snippet = snippet if len(snippet) <= 260 else snippet[:257] + "…"
            lines.append(f"  [{c}]\n    {snippet}")
        self.store.audit("retrieved", k=len(rows))
        return self._reply(request, "\n".join(lines), action="retrieve",
                           extra={"citations": cites, "query": query})

    def _ask(self, request) -> AgentResult:
        meta = request.metadata or {}
        question = (str(meta.get("query") or request.text or "")
                    .strip()[:MAX_ASK_CHARS])
        if not question:
            return self._reply(request,
                               "What do you want me to read from your "
                               "legal documents?", action="ask",
                               status=TaskStatus.NEEDS_INPUT)
        result = rag_service.answer(
            caller="legal_agent", query=question,
            strategy=str(meta.get("strategy") or "auto"),
            collection=COLLECTION, max_chars=8_000)
        if not result.get("ok"):
            return self._reply(
                request, f"Answering not permitted: {result.get('error')}. "
                         f"Grant 'legal_agent' the {COLLECTION} "
                         "collection in config/rag_callers.json.",
                action="ask")
        used = [e for e in (result.get("used_evidence") or [])
                if isinstance(e, dict)]
        if not used:
            return self._reply(
                request, "I can't answer that from your legal "
                         "documents — nothing cited matched. I will "
                         "not make this up.", action="ask",
                extra={"query": question, "groundless": True})
        answer = str(result.get("answer") or "").strip()
        evidence = "\n".join(f"  [{i+1}] {' '.join(str(e.get('text') or '').split())[:240]}"
                             for i, e in enumerate(used[:5]))
        text = (f"{answer}\n\nQuoted from your documents:\n{evidence}"
                if "sources" not in answer.lower() else answer)
        return self._reply(request, text, action="ask",
                           extra={"grounded": bool(result.get("grounded")),
                                  "risk": result.get("hallucination_risk"),
                                  "strategy": result.get("strategy")})

    def _compare(self, request) -> AgentResult:
        meta = request.metadata or {}
        old_id = str(meta.get("doc_id_old") or meta.get("old_doc") or "").strip()
        new_id = str(meta.get("doc_id_new") or meta.get("new_doc") or "").strip()
        text = request.text or ""
        import re as _re

        def _scan(pattern: str, hit: str = "") -> str:
            m = _re.search(pattern, text, _re.IGNORECASE)
            return (m.group(hit) if hit and m and m.groups() else "") \
                if m else ""
        if not old_id or not new_id:
            ids = _re.findall(r"lg-\d{8}-[a-z0-9]+-[a-z0-9]+",
                              text, _re.IGNORECASE)
            if len(ids) >= 2:
                old_id, new_id = ids[0], ids[1]
        old = self.store.get_document(old_id) if old_id else None
        new = self.store.get_document(new_id) if new_id else None
        if not old or not new:
            hint = (f"{old_id!r} is not in the vault." if old_id and not old
                    else f"{new_id!r} is not in the vault." if new_id and not new
                    else "Give me two doc ids (metadata {doc_id_old, "
                         "doc_id_new} or in your message; 'library' "
                         "lists them).")
            return self._reply(request, hint, action="compare",
                               status=TaskStatus.NEEDS_INPUT)
        old_pages = [dict(p) for p in self.store.load_page_text(old_id)]
        new_pages = [dict(p) for p in self.store.load_page_text(new_id)]
        if not old_pages or not new_pages:
            return self._reply(request,
                               "One of the documents has no stored page "
                               "text — re-ingest it.", action="compare")
        old_read = lg_pages.ReadResult(
            ok=True, path=str(old.get("title") or old_id),
            pages=[lg_pages.Page(num=int(p.get("num") or 0),
                                 text=str(p.get("text") or ""))
                   for p in old_pages])
        new_read = lg_pages.ReadResult(
            ok=True, path=str(new.get("title") or new_id),
            pages=[lg_pages.Page(num=int(p.get("num") or 0),
                                 text=str(p.get("text") or ""))
                   for p in new_pages])
        result = lg_diff.compare_versions(old_read, new_read)
        self.store.audit("compared", old=old_id, new=new_id)
        head = (f"Comparing '{new.get('title')}' [{new_id}] against "
                f"'{old.get('title')}' [{old_id}]"
                + (f" (v{new.get('version')} supersedes v{old.get('version')})"
                   if new.get("supersedes") == old_id else ""))
        return self._reply(request, head + "\n"
                           + "\n".join(result.to_lines()),
                           action="compare",
                           extra={"changed": len(result.changed),
                                  "added": len(result.added),
                                  "removed": len(result.removed)})

    def _library(self, request) -> AgentResult:
        rows = self.store.documents()
        lines = [f"Legal vault: {len(rows)} document(s), "
                 f"collection {COLLECTION} (ACL: legal_agent only)."]
        if not rows:
            lines.append("  (empty — metadata {legal_action: 'ingest', "
                         "path: …} adds one, with your approval)")
        for d in rows:
            sup = f" ← supersedes {d.get('supersedes')}" \
                if d.get("supersedes") else ""
            lines.append(f"  • {d.get('doc_id')}  {d.get('title')} "
                         f"[{d.get('kind')} · v{d.get('version')} · "
                         f"{d.get('pages')}p]{sup}")
        self.store.audit("library_listed", docs=len(rows))
        return self._reply(request, "\n".join(lines), action="library",
                           extra={"docs": len(rows)})

    _HANDLERS = {
        "analyze": _analyze,
        "summarize": _summarize,
        "summary": _summarize,
        "clauses": _clauses_action,
        "retrieve": _retrieve,
        "search": _retrieve,
        "ask": _ask,
        "compare": _compare,
        "ingest": _ingest,
        "forget": _forget,
        "library": _library,
    }
