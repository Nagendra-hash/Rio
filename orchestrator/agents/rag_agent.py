"""
orchestrator/agents/rag_agent.py — the real rag_agent (Phase 2).

Replaces the Phase-1 stub under the SAME registry name (orchestrator/agents/
__init__.py: "Phase 2 replaces each stub by registering a real BaseAgent
under the SAME name — the router, contracts, and orchestrator are untouched").

WHAT IT DOES
    read  → search(caller="rag_agent", …) — scoped to its granted collections
            (config/rag_callers.json), evidence returned with citations.
    write → ingest/update/forget, gated by the caller's can_write.
    learn → run the self-directed web learner (core/rag_learner): research a
            few topics, merge findings into cited dossiers in the
            "web_knowledge" collection. rag_action="learning_status" reports
            progress without touching the network.

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
    description = (
        "Private document / knowledge base Q&A: retrieval with "
        "source citations over the caller's granted collections."
    )
    task_types = (TaskType.RAG,)

    def _handle_impl(self, request: AgentRequest) -> AgentResult:
        text = (request.text or "").strip()
        mode = str(request.metadata.get("rag_action") or "").strip().lower()

        try:
            if mode == "answer":
                # full strategy pipeline: complexity → retrieval → evaluation
                # → correction → cited answer. metadata.rag_strategy picks
                # the strategy (auto default); rag_action='read' stays the
                # plain chunk search for Phase-3 compatibility.
                result = rag_service.answer(
                    caller="rag_agent",
                    query=text,
                    strategy=str(request.metadata.get("rag_strategy") or "auto"),
                    collection=request.metadata.get("collection"),
                )
                if not result.get("ok"):
                    return self._fail(request, result.get("error", "answer failed"))
                data = {
                    k: result.get(k)
                    for k in (
                        "citations",
                        "evidence",
                        "used_evidence",
                        "queries_used",
                        "iterations",
                        "hallucination_risk",
                        "grounded",
                        "trace",
                    )
                }
                text_out = (
                    result.get("answer") or "The knowledge base does not cover this."
                )
                return AgentResult(
                    task_id=request.task_id,
                    status=TaskStatus.COMPLETED,
                    text=text_out,
                    data=data,
                    agent=self.name,
                )

            if mode == "read" or not mode:
                result = rag_service.search(
                    caller="rag_agent",
                    query=text,
                    top_k=int(request.metadata.get("top_k") or 5),
                    collection=request.metadata.get("collection"),
                )
                if not result.get("ok"):
                    return self._fail(request, result.get("error", "search failed"))
                if not result.get("results"):
                    return AgentResult(
                        task_id=request.task_id,
                        status=TaskStatus.COMPLETED,
                        text="No relevant documents found for this question.",
                        data={"citations": [], "mode": result.get("mode")},
                        agent=self.name,
                    )
                citations = result.get("citations") or []
                top = result["results"][0]
                answer = (
                    f"Found {len(result['results'])} relevant passage(s). "
                    f"Top match — [{citations[0]}] "
                    f"(score {top['score']}):"
                )
                return AgentResult(
                    task_id=request.task_id,
                    status=TaskStatus.COMPLETED,
                    text=answer,
                    data={
                        "results": result["results"][:5],
                        "citations": citations,
                        "evidence": result.get("evidence", ""),
                        "mode": result.get("mode"),
                    },
                    agent=self.name,
                )

            if mode in ("learn", "learning", "harvest"):
                # Self-directed web learning: research topics and merge the
                # findings into the "web_knowledge" collection. Safe to call
                # from the router/voice: the cycle is lock-guarded and every
                # failure is reported per topic, never raised.
                from core import rag_learner

                topics = request.metadata.get("topics") or None
                if isinstance(topics, str):
                    topics = [t.strip() for t in topics.split(",") if t.strip()]
                result = rag_learner.run_cycle(force=True, topics=topics)
                if not result.get("ok"):
                    return self._fail(
                        request, result.get("error", "learning cycle failed")
                    )
                learned = int(result.get("learned") or 0)
                text_out = (
                    f"Learning cycle finished: {learned} topic(s) learned, "
                    f"{result.get('no_sources', 0)} with no new sources, "
                    f"{result.get('failed', 0)} failed, "
                    f"{result.get('skipped', 0)} unchanged."
                )
                return AgentResult(
                    task_id=request.task_id,
                    status=TaskStatus.COMPLETED,
                    text=text_out,
                    data=result,
                    agent=self.name,
                )

            if mode in ("learning_status", "learn_status", "status"):
                from core import rag_learner

                st = rag_learner.status()
                text_out = (
                    f"Web knowledge learner: {st.get('topics', 0)} topics, "
                    f"{st.get('with_knowledge', 0)} with a dossier, "
                    f"{st.get('ingested', 0)} created / {st.get('updated', 0)} "
                    f"updated; last run {st.get('last_run_date') or 'never'}."
                )
                return AgentResult(
                    task_id=request.task_id,
                    status=TaskStatus.COMPLETED,
                    text=text_out,
                    data=st,
                    agent=self.name,
                )

            if mode == "ingest":
                path = request.metadata.get("path") or ""
                result = rag_service.ingest(
                    caller="rag_agent",
                    file_path=str(path),
                    collection=request.metadata.get("collection")
                    or rag_service.rag.DEFAULT_COLLECTION,
                )
                return self._mutation_result(request, result)

            if mode == "forget":
                doc_id = str(request.metadata.get("doc_id") or "")
                result = rag_service.forget(caller="rag_agent", doc_id=doc_id)
                return self._mutation_result(request, result)

            return self._fail(request, f"unknown rag_action '{mode}'")
        except Exception as exc:  # never break the router
            return self._fail(request, f"rag_agent error: {exc}")

    @staticmethod
    def _mutation_result(request: AgentRequest, result: dict) -> AgentResult:
        ok = bool(result.get("ok"))
        return AgentResult(
            task_id=request.task_id,
            status=TaskStatus.COMPLETED if ok else TaskStatus.FAILED,
            text=(
                str(
                    result.get("title")
                    or result.get("forgotten")
                    or result.get("error")
                    or "done"
                )
            ),
            data=result,
            agent="rag_agent",
        )

    @staticmethod
    def _fail(request: AgentRequest, msg: str) -> AgentResult:
        return AgentResult(
            task_id=request.task_id,
            status=TaskStatus.FAILED,
            text=msg,
            agent="rag_agent",
        )
