"""
orchestrator/agents/study/resources.py — resource discovery for the Study
Partner.

All outside-world reads go through the permitted-source toolbox
(`research_tools.SourceToolbox`) exactly like the research agent and the
daily-life agents — the URL policy, SSRF guard and secret masking apply
without duplication. The local KB is searched under the `study_agent`
caller identity (grants in config/rag_callers.json), so the study_library /
personal_knowledge / research collections are the user's own materials.

Design rules (the dailylife honesty rule, restated here):
  * every method returns {"ok": bool, ...} and never raises outward;
  * a failed source is REPORTED ("unavailable — no live source queried"),
    never faked over and never silently retried;
  * results carry their source label — the explanation and the resources
    reply always say WHERE a resource/claim came from;
  * nothing here ever invents a link or a fact: with no results the reply
    says none were found and points at the local library instead.

The toolbox and its permissions are injectable for offline tests and demos.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from orchestrator.agents.research_tools import SourceToolbox

MAX_WEB_RESULTS = 5
MAX_KB_TOP_K = 5

kb_search_t = Callable[..., dict]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class StudyResources:
    """Resource boundary for the study partner."""

    def __init__(self, toolbox: SourceToolbox | None = None,
                 kb_search: kb_search_t | None = None,
                 allow_web: bool = True,
                 allow_local_kb: bool = True) -> None:
        from orchestrator.agents.research_tools import SourcePermissions
        self.sources = toolbox or SourceToolbox(
            permissions=_Perms(allow_web, allow_local_kb))
        self._kb_search = kb_search
        self._allow_web = allow_web
        self._allow_local_kb = allow_local_kb

    # ── web search (permitted sources only) ─────────────────────────────
    def web_search(self, query: str, max_results: int = MAX_WEB_RESULTS) -> dict:
        if not self._allow_web:
            return {"ok": False, "results": [], "queried_at": "",
                    "error": "web tier disabled for this task"}
        try:
            results = self.sources.web_search(query, max_results=max_results)
            return {"ok": True, "results": results, "queried_at": _now_iso(),
                    "error": ""}
        except Exception as exc:
            return {"ok": False, "results": [], "queried_at": "",
                    "error": f"web search unavailable: {exc}"[:200]}

    # ── local KB (the user's own library; RAG-integration point) ─────────
    def local_kb(self, query: str, top_k: int = MAX_KB_TOP_K,
                 collection: str | None = None) -> dict:
        """rag_service.search as caller `study_agent` — the same ACL/audit
        envelope the rag_agent uses. An injected `kb_search` (offline
        tests/demos) is honoured first; a blind live caller degrades to
        zero results with a visible note; never an error."""
        if not self._allow_local_kb:
            return {"ok": True, "results": [], "citations": [],
                    "note": "local KB tier disabled for this task"}
        if self._kb_search is not None:
            try:
                return self._kb_search(query, top_k, collection)
            except Exception as exc:
                return {"ok": False, "results": [], "citations": [],
                        "error": f"local KB failed: "
                                 f"{type(exc).__name__}: {exc}"[:200]}
        try:
            from core import rag_service
            return rag_service.search(caller="study_agent", query=query,
                                      top_k=max(1, min(int(top_k), 10)),
                                      collection=collection, max_chars=8000)
        except Exception as exc:
            return {"ok": False, "results": [], "citations": [],
                    "error": f"local KB failed: "
                             f"{type(exc).__name__}: {exc}"[:200]}

    # ── the study-resources ask ─────────────────────────────────────────
    def find(self, subject: str, level: str = "beginner") -> dict:
        """Resources for one subject: local library first, then permitted
        web. Each hit keeps {title, url, source-label}; nothing is padded
        with a guess when a tier is unavailable."""
        subject = (subject or "").strip()
        if not subject:
            return {"ok": True, "subject": "", "kb": [],
                    "web": [], "kb_note": "no subject",
                    "web_error": "", "kb_error": ""}
        kb = self.local_kb(
            f"{subject} {level} study notes explanation fundamentals")
        kb_hits = _kb_hits(kb)
        web = self.web_search(
            f"{subject} {level} tutorial course study guide")
        return {"ok": True, "subject": subject, "level": level,
                "kb": kb_hits, "kb_note": kb.get("note", "")
                or (kb.get("error") if not kb.get("ok") else ""),
                "kb_error": kb.get("error", "") if not kb.get("ok") else "",
                "web": web.get("results", []) if web.get("ok") else [],
                "web_error": web.get("error", "")}

    # ── RAG evidence for explanations / quizzes ─────────────────────────
    def explain_evidence(self, question: str, top_k: int = MAX_KB_TOP_K) -> dict:
        """The cited retrieval pool used by engine.explain and engine.quiz:
        rag_service.search-shaped dict straight from the KB (evidence
        string plus per-chunk citations)."""
        kb = self.local_kb(question, top_k=top_k)
        if not kb.get("ok"):
            return {"ok": False, "evidence": "", "citations": [],
                    "error": kb.get("error", "local KB failed")}
        return {"ok": True, "evidence": kb.get("evidence", ""),
                "citations": kb.get("citations", []),
                "results": kb.get("results", []),
                "note": kb.get("note", "")}


class _Perms:
    """SourceToolbox expects a SourcePermissions-shaped object; same tiny
    adapter the dailylife toolbox uses."""

    def __init__(self, allow_web: bool, allow_local_kb: bool) -> None:
        from orchestrator.agents.research_tools import SourcePermissions
        self._inner = SourcePermissions(allow_web=allow_web,
                                        allow_local_kb=allow_local_kb)
        self.allow_web = allow_web
        self.allow_local_kb = allow_local_kb

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _kb_hits(kb: dict) -> list[dict]:
    """Normalise rag_service.search results into resource rows."""
    if not kb.get("ok"):
        return []
    citations = kb.get("citations") or []
    hits: list[dict] = []
    for i, r in enumerate(kb.get("results") or []):
        if not isinstance(r, dict):
            continue
        cite = citations[i] if i < len(citations) else \
            str(r.get("source") or "local document")
        hits.append({
            "title": str(r.get("source") or cite)[:200],
            "detail": str(r.get("text") or "")[:400],
            "url": f"kb://{r.get('doc_id') or 'local'}",
            "source": f"your library — {cite}",
            "score": r.get("score"),
        })
    return hits
