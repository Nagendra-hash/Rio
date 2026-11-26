"""
core/rag_strategies/base.py — the shared pipeline all six strategies walk.

PHASE 4 of the RAG roadmap: adaptive, agentic, corrective (CRAG) and
self-RAG retrieval — plus local offline twins — over the Phase-3
service/ACL foundation.

THE 9 BEHAVIOURS (the deliverable spec, mapped to code)

    1. detect query complexity            → heuristics.classify_query
    2. decide whether retrieval is needed → strategy.needs_retrieval
    3. select retrieval depth + sources   → strategy.plan (top_k, collections)
    4. evaluate retrieved evidence        → heuristics.score_evidence (+ LLM
                                            judges where the strategy pays for them)
    5. correct poor retrieval             → strategy.correct (reworded /
                                            expanded / multi-hop queries)
    6. generate an answer                 → answerer (LLM or extractive offline)
    7. evaluate whether more evidence is needed → strategy.more_needed
    8. retrieve again when necessary      → loop back to plan with refinement
    9. return citations / source refs     → every Answer carries citations
                                           (title, page, collection, doc_id,
                                           score) + an evidence string built
                                           ONLY from the retrieved chunks

WHAT A STRATEGY OWNS
    Only the DECISIONS: needs_retrieval / plan / correct / more_needed.
    The mechanics — calling the retriever, scoring, budget caps, audit — live
    here once, so six strategies cannot drift into six security postures.

HARD RULES (every strategy inherits them, none can opt out)
    * ACL: the retriever this pipeline calls is the service's pre-scoped
      closure. A strategy never sees a collection it was not granted.
    * No whole documents: the retriever returns chunks; the evidence string
      passed to any model is bounded by max_chars.
    * Bounded loops: attempts and iterations are capped; the Answer records
      which strategy ran and how the loop ended.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.rag_strategies import heuristics

MAX_ATTEMPTS = 3          # generation attempts across the whole pipeline
MAX_ITERATIONS = 3        # plan → retrieve → evaluate rounds


# ── data contracts ───────────────────────────────────────────────────────────

@dataclass
class Evidence:
    """One retrieved chunk + its evaluation. The unit strategies reason about."""
    text: str
    source: str                    # title of the document it came from
    page: int | None
    doc_id: str
    collection: str
    score: float = 0.0             # retrieval (cosine) score
    relevance: float = 0.0         # evidence score: how much it helps this query
    source_ref: str = ""           # human/LLM citation string


@dataclass
class Answer:
    """The pipeline's return: answer + citations + the reasoning trail."""
    ok: bool
    strategy: str
    answer: str = ""
    citations: list[str] = field(default_factory=list)
    evidence: str = ""             # bounded [n] cite / text blocks for prompts
    used_evidence: list[Evidence] = field(default_factory=list)
    queries_used: list[str] = field(default_factory=list)
    iterations: int = 0
    retrieval_needed: bool = True
    hallucination_risk: str = "unknown"    # low | medium | high | unknown
    grounded: bool = False                 # answer supported by retrieved evidence
    trace: list[str] = field(default_factory=list)   # human-readable steps
    error: str | None = None
    meta: dict = field(default_factory=dict)         # complexity, confidence…

    def to_dict(self) -> dict:
        d = {
            "ok": self.ok, "strategy": self.strategy, "answer": self.answer,
            "citations": self.citations, "evidence": self.evidence,
            "used_evidence": [
                {"text": e.text, "source": e.source, "page": e.page,
                 "doc_id": e.doc_id, "collection": e.collection,
                 "score": e.score, "relevance": round(e.relevance, 4),
                 "source_ref": e.source_ref}
                for e in self.used_evidence],
            "queries_used": self.queries_used, "iterations": self.iterations,
            "retrieval_needed": self.retrieval_needed,
            "hallucination_risk": self.hallucination_risk,
            "grounded": self.grounded, "trace": self.trace,
        }
        if self.error:
            d["error"] = self.error
        d.update(self.meta)
        return d


# ── the pipeline ─────────────────────────────────────────────────────────────

class RetrievalPipeline:
    """Runs one strategy's decisions against one scoped retriever.

    `retrieve(query, top_k, collections)` is supplied by core/rag_service —
    it already enforces the caller's ACL, so a strategy asking for another
    collection simply gets nothing back from it.
    """

    def __init__(self, strategy, retrieve, answerer, *, max_chars: int = 12000,
                 trace=None):
        self.s = strategy
        self.retrieve = retrieve
        self.answerer = answerer
        self.max_chars = max_chars
        self.trace: list[str] = []
        self._trace_fn = trace if trace is not None else (lambda m: None)

    def _trace(self, msg: str) -> None:
        self.trace.append(msg)
        self._trace_fn(msg)

    # ── step 1: complexity ──
    def _classify(self, query: str) -> dict:
        info = heuristics.classify_query(query)
        self._trace(f"complexity={info['complexity']} "
                    f"(type={info['type']}, multi_hop={info['multi_hop']})")
        return info

    # ── steps 2/3: need + plan ──
    def _plan(self, query: str, info: dict, iteration: int) -> dict:
        retrieval_needed = self.s.needs_retrieval(query, info)
        plan = {"retrieval_needed": retrieval_needed, "top_k": 5,
                "collections": [None], "reformulations": ()}
        if retrieval_needed:
            plan.update(self.s.plan(query, info, iteration))
        self._trace(f"plan: retrieve={retrieval_needed} top_k={plan['top_k']} "
                    f"collections={plan['collections']} "
                    f"reformulations={len(plan['reformulations'])}")
        return plan

    # ── steps 3/4: retrieve + evaluate ──
    def _gather(self, query: str, plan: dict) -> tuple[list[Evidence], list[str]]:
        """Run the plan's queries through the scoped retriever and score what
        comes back. Returns (evidence list, queries actually used)."""
        queries: list[str] = [query]
        queries.extend(str(q) for q in (plan.get("reformulations") or ())
                       if str(q).strip() and str(q) != query)
        top_k = max(2, min(int(plan.get("top_k") or 5), 25))
        cols = plan.get("collections") or [None]

        best: dict[tuple, Evidence] = {}
        used: list[str] = []
        for col in cols:
            for q in queries:
                used.append(q)
                rows = self._rows(q, top_k, col)
                for r in rows:
                    key = (r.get("doc_id"), r.get("page"), r.get("text"))
                    if key in best:
                        best[key].score = max(best[key].score,
                                              float(r.get("score") or 0.0))
                        continue
                    best[key] = Evidence(
                        text=r.get("text") or "",
                        source=r.get("source") or "unknown source",
                        page=r.get("page"),
                        doc_id=r.get("doc_id") or "",
                        collection=r.get("collection") or "",
                        score=float(r.get("score") or 0.0))
        evidence = list(best.values())
        for e in evidence:
            e.relevance = heuristics.score_evidence(e.text, queries)
            e.source_ref = (f"{e.source}"
                            + (f", p.{e.page}" if e.page else "")
                            + (f" [{e.collection}]" if e.collection else ""))
        # strongest first: retrieval score first, judged relevance as tiebreak
        evidence.sort(key=lambda e: (e.score, e.relevance), reverse=True)
        self._trace(f"retrieved {len(evidence)} chunk(s) from "
                    f"{len(used)} query run(s)")
        return evidence, used

    def _rows(self, query: str, top_k: int, collection) -> list[dict]:
        """One retriever call, shaped to tolerate the service's dict result."""
        try:
            res = self.retrieve(query, top_k,
                                collection if collection else None)
        except Exception as exc:
            self._trace(f"retriever error: {exc}")
            return []
        if isinstance(res, dict):
            return list(res.get("results") or [])
        return list(res or [])

    # ── steps 5: correction ──
    def _correct(self, query: str, info: dict, evidence: list[Evidence],
                 iteration: int) -> list[str]:
        try:
            fixes = self.s.correct(query, info, evidence, iteration)
        except Exception as exc:
            self._trace(f"correct() failed: {exc}")
            fixes = []
        fixes = [str(f) for f in (fixes or []) if str(f).strip()]
        if fixes:
            self._trace(f"correction: {len(fixes)} new query(ies)")
        return fixes

    # ── steps 6/7: answer + sufficiency ──
    def _answer(self, query: str, evidence: list[Evidence]) -> dict:
        ev_budget = self._mk_budget(evidence, self.max_chars)
        try:
            out = self.answerer(query, ev_budget)
        except Exception as exc:
            return {"text": "", "grounded": False, "confidence": 0.0,
                    "error": str(exc)}
        if not isinstance(out, dict):
            out = {"text": str(out or "")}
        out.setdefault("text", "")
        out.setdefault("grounded", bool(out.get("text")))
        out.setdefault("confidence", 0.5 if out.get("text") else 0.0)
        out.setdefault("error", None)
        return out

    def _more_needed(self, query: str, info: dict, evidence: list[Evidence],
                     ans: dict, iteration: int) -> bool:
        if iteration >= MAX_ITERATIONS:
            return False
        try:
            want = bool(self.s.more_needed(query, info, evidence, ans,
                                           iteration))
        except Exception as exc:
            self._trace(f"more_needed() failed: {exc}")
            want = False
        self._trace(f"more evidence needed: {want} "
                    f"(confidence={ans.get('confidence', 0):.2f})")
        return want

    # ── the loop ──
    def run(self, query: str) -> Answer:
        t0 = time.monotonic()
        info = self._classify(query)
        all_queries: list[str] = []
        evidence: list[Evidence] = []
        ans: dict = {}
        correction_used = False

        pending_fixes: tuple[str, ...] = ()
        for iteration in range(1, MAX_ITERATIONS + 1):
            plan = self._plan(query, info, iteration)
            if pending_fixes:
                # a correction round: force retrieval with the new queries
                plan["reformulations"] = pending_fixes
                plan["retrieval_needed"] = True
            if not plan["retrieval_needed"]:
                ans = self._answer(query, evidence)
                return self._finish(query, evidence, ans, all_queries,
                                    iteration, retrieval_needed=False,
                                    t0=t0, meta={"complexity": info["complexity"],
                                                 "type": info["type"]})
            ev, used = self._gather(query, plan)
            all_queries.extend(used)
            if ev:
                merged = {e.text: e for e in evidence}
                merged.update({e.text: e for e in ev})
                evidence = sorted(merged.values(),
                                  key=lambda e: (e.score, e.relevance),
                                  reverse=True)
            ans = self._answer(query, evidence)

            if self._more_needed(query, info, evidence, ans, iteration):
                fixes = self._correct(query, info, evidence, iteration)
                if fixes:
                    correction_used = True
                    pending_fixes = tuple(fixes)
                    continue
                self._trace("no correction available — stopping the loop")
            break

        return self._finish(query, evidence, ans, all_queries,
                            iteration, retrieval_needed=True,
                            corrected=correction_used, t0=t0,
                            meta={"complexity": info["complexity"],
                                  "type": info["type"]})

    # ── step 9 + telemetry ──
    def _finish(self, query, evidence, ans, queries, iterations, *,
                retrieval_needed, corrected=False, t0, meta) -> Answer:
        evidence = evidence[:8]
        citations = [e.source_ref for e in evidence]
        answer_text = (ans.get("text") or "").strip()
        # pipeline-level ISSUP: the answerer's grounded claim is verified
        # against the evidence itself — claims nearly absent from what we
        # retrieved are not "grounded", whatever the answerer believed
        support = max((heuristics.support_score(answer_text, e.text)
                       for e in evidence), default=0.0)
        grounded = bool(ans.get("grounded")) and bool(evidence) \
            and support >= 0.2
        a = Answer(
            ok=True,
            strategy=getattr(self.s, "name", "unknown"),
            answer=answer_text,
            citations=citations,
            evidence=self._evidence_string(evidence, self.max_chars),
            used_evidence=evidence,
            queries_used=queries,
            iterations=iterations,
            retrieval_needed=retrieval_needed,
            grounded=grounded,
            hallucination_risk=heuristics.hallucination_risk(
                answer_text, evidence, bool(ans.get("grounded"))),
            error=ans.get("error"),
            trace=list(self.trace),
            meta=dict(meta, confidence=round(float(ans.get("confidence") or 0.0), 3),
                      support=round(support, 4),
                      corrected=bool(corrected),
                      elapsed_ms=int((time.monotonic() - t0) * 1000)),
        )
        self._trace(f"done: strategy={a.strategy} grounded={a.grounded} "
                    f"risk={a.hallucination_risk} cites={len(a.citations)}")
        return a

    @staticmethod
    def _mk_budget(evidence: list[Evidence], max_chars: int) -> str:
        out: list[str] = []
        total = 0
        for i, e in enumerate(evidence, 1):
            block = f"[{i}] {e.source_ref}\n{e.text}"
            if total + len(block) > max_chars:
                break
            out.append(block)
            total += len(block) + 2
        return "\n\n".join(out)

    @staticmethod
    def _evidence_string(evidence: list[Evidence], max_chars: int = 12000) -> str:
        out: list[str] = []
        total = 0
        for i, e in enumerate(evidence, 1):
            block = f"[{i}] {e.source_ref}\n{e.text}"
            if total + len(block) > max_chars:
                out.append("[…]")
                break
            out.append(block)
            total += len(block) + 2
        return "\n\n".join(out)
