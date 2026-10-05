"""
orchestrator/agents/rag_agent.py — the real rag_agent (Phase 2).

Replaces the Phase-1 stub under the SAME registry name (orchestrator/agents/
__init__.py: "Phase 2 replaces each stub by registering a real BaseAgent
under the SAME name — the router, contracts, and orchestrator are untouched").

WHAT IT DOES
    read  → search(caller="rag_agent", …) — scoped to its granted collections
            (config/rag_callers.json), evidence returned with citations.
    write → ingest/update/forget, gated by the caller's can_write.

The agent NEVER calls the LLM: it returns structured evidence the orchestrator
(or the main assistant) can answer from. Sending the evidence to a model is
the caller's decision, bounded by the evidence budget in rag_service.search.
"""
from __future__ import annotations

from orchestrator.base import BaseAgent
from orchestrator.contracts import AgentRequest, AgentResult, TaskType, TaskStatus
from core import rag_service


class RagAgent(BaseAgent):
    name = "rag_agent"
    description = ("Private document / knowledge base Q&A: retrieval with "
                   "source citations over the caller's granted collections.")
    task_types = (TaskType.RAG,)

    def handle(self, request: AgentRequest) -> AgentResult:
        text = (request.text or "").strip()
        mode = str(request.metadata.get("rag_action") or "").strip().lower()

        try:
            if mode == "read" or not mode:
                result = rag_service.search(
                    caller="rag_agent", query=text,
                    top_k=int(request.metadata.get("top_k") or 5),
                    collection=request.metadata.get("collection"))
                if not result.get("ok"):
                    return self._fail(request, result.get("error", "search failed"))
                if not result.get("results"):
                    return AgentResult(
                        task_id=request.task_id, status=TaskStatus.COMPLETED,
                        text="No relevant documents found for this question.",
                        data={"citations": [], "mode": result.get("mode")},
                        agent=self.name)
                citations = result.get("citations") or []
                top = result["results"][0]
                answer = (f"Found {len(result['results'])} relevant passage(s). "
                          f"Top match — [{citations[0]}] "
                          f"(score {top['score']}):")
                return AgentResult(
                    task_id=request.task_id, status=TaskStatus.COMPLETED,
                    text=answer, data={"results": result["results"][:5],
                                       "citations": citations,
                                       "evidence": result.get("evidence", ""),
                                       "mode": result.get("mode")},
                    agent=self.name)

            if mode == "ingest":
                path = request.metadata.get("path") or ""
                result = rag_service.ingest(caller="rag_agent",
                                            file_path=str(path),
                                            collection=request.metadata.get(
                                                "collection")
                                            or rag_service.rag.DEFAULT_COLLECTION)
                return self._mutation_result(request, result)

            if mode == "forget":
                doc_id = str(request.metadata.get("doc_id") or "")
                result = rag_service.forget(caller="rag_agent", doc_id=doc_id)
                return self._mutation_result(request, result)

            return self._fail(request, f"unknown rag_action '{mode}'")
        except Exception as exc:                      # never break the router
            return self._fail(request, f"rag_agent error: {exc}")

    @staticmethod
    def _mutation_result(request: AgentRequest, result: dict) -> AgentResult:
        ok = bool(result.get("ok"))
        return AgentResult(
            task_id=request.task_id,
            status=TaskStatus.COMPLETED if ok else TaskStatus.FAILED,
            text=(str(result.get("title") or result.get("forgotten")
                      or result.get("error") or "done")),
            data=result, agent="rag_agent")

    @staticmethod
    def _fail(request: AgentRequest, msg: str) -> AgentResult:
        return AgentResult(task_id=request.task_id, status=TaskStatus.FAILED,
                           text=msg, agent="rag_agent")
