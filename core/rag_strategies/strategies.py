"""
core/rag_strategies/strategies.py — the six strategies + the registry.

One CLASS per strategy, each owning only the DECISIONS of the pipeline:

    needs_retrieval(query, info) → bool                       (step 2)
    plan(query, info, iteration) → {top_k, collections,
                                    reformulations}           (step 3)
    correct(query, info, evidence, iteration) → [new queries] (step 5)
    more_needed(query, info, evidence, answer, iteration)→bool(step 7)

The six (LLM-judged where noted, L = LLM judge; otherwise heuristic-only):

    adaptive        L: query router — grades complexity with a model, skips
                       retrieval for chit-chat/commands, widens for hard asks
    agentic         L: full agent loop — plans, multi-hop queries, grades
                       evidence, rewrites, asks for more until confident
    corrective      L: CRAG — retrieval-quality gate; on weak evidence
                       rewrites the query (web/search would slot here)
    self_rag        L: SELF-RAG — reflection tokens as JSON: ISREL/ISSUP/
                       ISUSE style grading of every chunk + the answer
    local_adaptive  offline: heuristic router — same shape as adaptive, all
                       decisions from heuristics.py, zero model calls
    local_self_rag  offline: heuristic evidence gate + extractive answers —
                       answers are verbatim spans, so hallucination is
                       structurally impossible

REGISTRY
    strategies() → {name: class}; get(name) resolves "auto" to the best
    available (LLM reachable → adaptive; else local_adaptive).
"""
from __future__ import annotations

from core.rag_strategies import heuristics

# ── shared bases ─────────────────────────────────────────────────────────────


class Strategy:
    """Decision points only. Base defaults = the honest minimum."""
    name = "base"

    def __init__(self, llm=None):
        # every strategy accepts the same wiring; LLM strategies use it,
        # local ones ignore it (their judge calls are heuristic-only)
        self.llm = llm

    def needs_retrieval(self, query: str, info: dict) -> bool:
        return True

    def plan(self, query: str, info: dict, iteration: int) -> dict:
        return {"top_k": 5, "collections": [None], "reformulations": ()}

    def correct(self, query: str, info: dict, evidence, iteration) -> list:
        return []

    def more_needed(self, query: str, info: dict, evidence, ans: dict,
                    iteration: int) -> bool:
        return False


class _LocalGate(Strategy):
    """Heuristic-only decisions shared by the local strategies: the router
    (step 2) and the evidence gate (step 7)."""

    FLOOR = 0.25          # below this a chunk is treated as noise
    TOP_RELEVANCE = 0.45  # best-chunk relevance needed to answer at all

    def needs_retrieval(self, query, info) -> bool:
        # the offline router: chit-chat / arithmetic / device commands do
        # not belong in a document library
        return info.get("complexity") != "simple"

    @staticmethod
    def _usable(evidence) -> list:
        return [e for e in evidence if e.relevance >= _LocalGate.FLOOR]

    def more_needed(self, query, info, evidence, ans, iteration) -> bool:
        usable = self._usable(evidence)
        if not usable:
            return iteration < 2          # one correction round, then stop
        # enough strong chunks? if the top chunk is far above the floor the
        # library has answered; more retrieval adds noise, not signal
        best = max(e.relevance for e in usable)
        return best < self.TOP_RELEVANCE and iteration < 2


# ── 1. Adaptive RAG ──────────────────────────────────────────────────────────


class AdaptiveRAG(Strategy):
    """Query router: grade the query with one LLM call, then route.

    simple → no retrieval (model answers from itself or declines)
    moderate → single retrieval pass, standard depth
    complex → deeper retrieval across granted collections + correction
    """
    name = "adaptive"

    def __init__(self, llm=None):
        super().__init__(llm)
        self.llm = llm                    # callable(prompt) → str (JSON)

    def needs_retrieval(self, query, info):
        if self.llm is None:
            return info["complexity"] != "simple"
        verdict = self.llm(
            'You route questions for a private knowledge base. Reply ONLY '
            'JSON: {"retrieve": true/false, "complexity": '
            '"simple"|"moderate"|"complex", "reason": "..."}\n'
            f'Question: {query!r}\n'
            'Rules: greetings/small-talk/OS commands/arithmetic → retrieve '
            'false. Anything needing facts from documents → true.')
        if not isinstance(verdict, dict):
            return info["complexity"] != "simple"
        if verdict.get("complexity") in ("simple", "moderate", "complex"):
            info["complexity"] = verdict["complexity"]      # refines step 1
        return bool(verdict.get("retrieve", True))

    def plan(self, query, info, iteration):
        depth = {"simple": 4, "moderate": 6, "complex": 10}.get(
            info.get("complexity"), 6)
        plan = {"top_k": depth if iteration == 1 else depth + 4,
                "collections": [None], "reformulations": ()}
        if info.get("multi_hop"):
            # one sub-question per hop is the agentic strategy's job; here we
            # just widen with two angle queries so the merge has material
            plan["reformulations"] = (
                f"background: {query}",
                f"details: {query}",
            ) if iteration == 1 else ()
        return plan

    def correct(self, query, info, evidence, iteration):
        if self.llm is None or iteration >= 3:
            return []
        verdict = self.llm(
            'The retrieval below may be irrelevant to the question. Reply '
            'ONLY JSON: {"rephrase": [ "...better query...", ... ]} — empty '
            'list if retrieval looks fine.\n'
            f'Question: {query!r}\n'
            'Retrieved passages (titles + first lines):\n'
            + "\n".join(f"- {e.source_ref}: {e.text[:120]}"
                        for e in evidence[:4]))
        if not isinstance(verdict, dict):
            return []
        return [q for q in verdict.get("rephrase", []) if isinstance(q, str)]


# ── 2. Agentic RAG ───────────────────────────────────────────────────────────


class AgenticRAG(Strategy):
    """The full loop: plan → retrieve → grade → decompose → rewrite → repeat.

    Distinguishing marks vs adaptive: sub-question decomposition for
    multi-hop asks, per-chunk relevance grading by the LLM, and a
    confidence-driven sufficiency check each round.
    """
    name = "agentic"

    GOOD_ENOUGH = 0.55   # best-chunk LLM relevance to stop the loop

    def __init__(self, llm=None):
        super().__init__(llm)
        self.llm = llm

    def needs_retrieval(self, query, info):
        return info["complexity"] != "simple" or info.get("multi_hop", False)

    def plan(self, query, info, iteration):
        if iteration == 1:
            subs = ()
            if self.llm is not None and info.get("multi_hop"):
                verdict = self.llm(
                    'Decompose the question into independent sub-questions '
                    'that together answer it. Reply ONLY JSON: '
                    '{"sub_questions": ["...", ...]} (max 3).\n'
                    f'Question: {query!r}')
                if isinstance(verdict, dict):
                    subs = tuple(str(s) for s in
                                 verdict.get("sub_questions", [])[:3]
                                 if str(s).strip())
            return {"top_k": 8, "collections": [None],
                    "reformulations": subs or ()}
        return {"top_k": 8 + 4 * (iteration - 1), "collections": [None],
                "reformulations": ()}

    def correct(self, query, info, evidence, iteration):
        if self.llm is None or iteration >= 3:
            return []
        verdict = self.llm(
            'Sub-questions were answered poorly. Propose ONE rewritten '
            'query, narrower than the original. Reply ONLY JSON: '
            '{"rewrite": "..."} or {"rewrite": null}.\n'
            f'Original: {query!r}\n'
            'What came back (titles + snippets):\n'
            + "\n".join(f"- {e.source_ref}: {e.text[:100]}"
                        for e in evidence[:4]))
        if isinstance(verdict, dict) and isinstance(verdict.get("rewrite"), str):
            return [verdict["rewrite"]]
        return []

    def more_needed(self, query, info, evidence, ans, iteration) -> bool:
        if iteration >= 3:
            return False
        if self.llm is None:
            return super().more_needed(query, info, evidence, ans, iteration)
        verdict = self.llm(
            'Is the evidence below sufficient to answer the question '
            'confidently? Reply ONLY JSON: {"sufficient": true/false, '
            '"confidence": 0.0-1.0}.\n'
            f'Question: {query!r}\n'
            'Evidence:\n' + "\n".join(f"- {e.source_ref}: {e.text[:160]}"
                                      for e in evidence[:5]))
        if isinstance(verdict, dict):
            conf = verdict.get("confidence")
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                conf = 0.5
            ans["confidence"] = conf
            return bool(not verdict.get("sufficient", True)) and conf < 0.8
        return True          # judge unavailable → one more honest attempt


# ── 3. Corrective RAG (CRAG) ─────────────────────────────────────────────────


class CorrectiveRAG(Strategy):
    """Retrieval-quality gate: grade what came back; on weak evidence,
    correct (rewrite) and pull again. Correction-accuracy over reflection.
    """
    name = "corrective"

    WEAK = 0.35       # mean relevance below this → correction
    PARTIAL = 0.6     # mean above this → good enough

    def __init__(self, llm=None):
        super().__init__(llm)
        self.llm = llm

    def plan(self, query, info, iteration):
        return {"top_k": 8 if iteration == 1 else 12, "collections": [None],
                "reformulations": ()}

    def _grade(self, query, evidence):
        """LLM grades relevance per chunk when available; else heuristic."""
        if self.llm is None or not evidence:
            return
        verdict = self.llm(
            'Grade each passage STRICTLY against the question. Reply ONLY '
            'JSON: {"grades": [0.0-1.0, ...]} — one per passage, in order.\n'
            f'Question: {query!r}\n'
            + "\n".join(f"{i + 1}. {e.text[:220]}"
                        for i, e in enumerate(evidence[:6])))
        if isinstance(verdict, dict):
            grades = verdict.get("grades")
            if isinstance(grades, list):
                for e, g in zip(evidence, grades):
                    try:
                        g = max(0.0, min(1.0, float(g)))
                    except (TypeError, ValueError):
                        continue
                    # LLM grade replaces the heuristic (blended a little)
                    e.relevance = 0.8 * g + 0.2 * e.relevance

    def more_needed(self, query, info, evidence, ans, iteration) -> bool:
        self._grade(query, evidence)
        mean_rel = (sum(e.relevance for e in evidence) / len(evidence)) \
            if evidence else 0.0
        ans["crag_mean_relevance"] = round(mean_rel, 4)
        return mean_rel < self.PARTIAL and iteration < 2

    def correct(self, query, info, evidence, iteration):
        if iteration >= 3:
            return []
        if self.llm is None:
            # heuristic rewrite: strip fluff, front-load content words
            core = " ".join(heuristics._terms(query)) or query
            return [f"{core} details", f"{core} explained"] if core else []
        verdict = self.llm(
            'Retrieval quality was poor. Rewrite the query to fix it. Reply '
            'ONLY JSON: {"rewrites": ["...", ...]} (max 2).\n'
            f'Original: {query!r}\n'
            'What came back:\n'
            + "\n".join(f"- {e.source_ref}: {e.text[:100]}"
                        for e in evidence[:4]))
        if isinstance(verdict, dict):
            return [r for r in verdict.get("rewrites", [])
                    if isinstance(r, str)]
        return []


# ── 4. Self-RAG ──────────────────────────────────────────────────────────────


class SelfRAG(Strategy):
    """Reflection tokens as JSON, per chunk and per answer.

    ISREL  — is this chunk relevant?          (evidence gate)
    ISSUP  — is the answer supported by it?   (grounding check)
    ISUSE  — is the answer useful for this ask? (final gate)
    Answers that fail support are re-generated once with stricter grounding.
    """
    name = "self_rag"

    REL_FLOOR = 0.5

    def __init__(self, llm=None):
        super().__init__(llm)
        self.llm = llm

    def plan(self, query, info, iteration):
        return {"top_k": 8, "collections": [None], "reformulations": ()}

    def needs_retrieval(self, query, info):
        return info["complexity"] != "simple"

    def _isrel(self, query, evidence):
        if self.llm is None or not evidence:
            return
        verdict = self.llm(
            'For each passage reply ONLY JSON: {"relevant": '
            '[true/false, ...]} — one per passage, in order.\n'
            f'Question: {query!r}\n'
            + "\n".join(f"{i + 1}. {e.text[:200]}"
                        for i, e in enumerate(evidence[:6])))
        if isinstance(verdict, dict):
            flags = verdict.get("relevant")
            if isinstance(flags, list):
                for e, ok in zip(evidence, flags):
                    if not ok:
                        e.relevance = 0.0
                    elif e.relevance < self.REL_FLOOR:
                        e.relevance = self.REL_FLOOR

    def more_needed(self, query, info, evidence, ans, iteration) -> bool:
        self._isrel(query, evidence)
        return any(e.relevance >= self.REL_FLOOR for e in evidence) is False \
            and iteration < 2

    def correct(self, query, info, evidence, iteration):
        if self.llm is None or iteration >= 3:
            return []
        verdict = self.llm(
            'No passage was relevant. Rewrite the question for retrieval. '
            'Reply ONLY JSON: {"rewrite": "..."} or {"rewrite": null}.\n'
            f'Question: {query!r}')
        if isinstance(verdict, dict) and isinstance(verdict.get("rewrite"), str):
            return [verdict["rewrite"]]
        return []

    # answer-side reflection: wired via the answerer wrapper in __init__ of
    # the service — see core/rag_service.answer for the ISSUP/ISUSE pass.


# ── 5. Local Adaptive RAG ────────────────────────────────────────────────────


class LocalAdaptiveRAG(_LocalGate):
    """Adaptive's shape, zero model calls: heuristic router + heuristic gate.
    The always-available floor for every machine this runs on."""
    name = "local_adaptive"

    def plan(self, query, info, iteration):
        depth = {"simple": 4, "moderate": 6, "complex": 9}.get(
            info.get("complexity"), 6)
        plan = {"top_k": depth if iteration == 1 else depth + 3,
                "collections": [None], "reformulations": ()}
        if info.get("multi_hop") and iteration == 1:
            plan["reformulations"] = (f"background: {query}",
                                      f"details: {query}")
        return plan

    def correct(self, query, info, evidence, iteration):
        if iteration >= 3 or not evidence:
            return []
        # lexical correction: query terms the best chunks do NOT cover are
        # likely noise; drop them and retry with the core
        best = max(evidence, key=lambda e: e.relevance)
        ev_terms = heuristics._terms(best.text)
        q_terms = heuristics._terms(query)
        uncovered = q_terms - ev_terms
        if len(uncovered) >= 2 and len(q_terms) - len(uncovered) >= 2:
            return [" ".join(q_terms - uncovered)]
        return []


# ── 6. Local Self-RAG ────────────────────────────────────────────────────────


class LocalSelfRAG(_LocalGate):
    """Self-RAG's shape, zero model calls: heuristic evidence gate + a
    stricter floor. The answerer does the rest (extractive spans only), so
    unsupported claims are structurally impossible."""
    name = "local_self_rag"

    TOP_RELEVANCE = 0.5     # stricter than the shared gate

    def plan(self, query, info, iteration):
        return {"top_k": 10 if iteration == 1 else 14,
                "collections": [None], "reformulations": ()}

    def correct(self, query, info, evidence, iteration):
        if iteration >= 3 or not evidence:
            return []
        core = " ".join(sorted(heuristics._terms(query))) or query
        return [f"{core} summary", f"{core} definition"]


# ── registry ─────────────────────────────────────────────────────────────────

REGISTRY: dict[str, type[Strategy]] = {
    AdaptiveRAG.name: AdaptiveRAG,
    AgenticRAG.name: AgenticRAG,
    CorrectiveRAG.name: CorrectiveRAG,
    SelfRAG.name: SelfRAG,
    LocalAdaptiveRAG.name: LocalAdaptiveRAG,
    LocalSelfRAG.name: LocalSelfRAG,
}

LLM_STRATEGIES = ("adaptive", "agentic", "corrective", "self_rag")
LOCAL_STRATEGIES = ("local_adaptive", "local_self_rag")
DEFAULT_STRATEGY = "adaptive"


def strategies() -> dict[str, type[Strategy]]:
    """The six, by name. Import-time cheap; classes carry no state."""
    return dict(REGISTRY)


def get(name: str, llm=None) -> Strategy:
    """Resolve a strategy by name; 'auto' picks LLM vs local by llm presence."""
    key = (name or DEFAULT_STRATEGY).strip().lower()
    if key == "auto":
        key = DEFAULT_STRATEGY if llm is not None else "local_adaptive"
    cls = REGISTRY.get(key)
    if cls is None:
        raise KeyError(f"unknown strategy '{name}'. Valid: "
                       f"{', '.join(sorted(REGISTRY))}, auto")
    return cls(llm=llm)
