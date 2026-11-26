# PHASE 4 — ADAPTIVE, AGENTIC, CRAG AND SELF-RAG

## RAG_STRATEGIES.md — adaptive retrieval strategies over the Jarvis library

*Branch `upgrade-20260930`, October 2026. Status: implemented, evaluation-tested (39 strategy tests; 166 green across all RAG suites). Builds on the Phase-3 foundation (`docs/rag_architecture.md`: ingestion pipeline, service API, ACL, REST surface, rag_agent).*

Six retrieval strategies — **Adaptive RAG, Agentic RAG, Corrective RAG (CRAG), Self-RAG, Local Adaptive RAG, Local Self-RAG** — implemented as **strategy modules over one shared pipeline**, not six agents. There is exactly one entry point (`rag_service.answer`), one ACL envelope, one audit trail.

## 1. Design in one paragraph

The Phase-3 RAG answered "find me chunks about X". This upgrade answers "answer this question, with citations, and don't make things up". A single `RetrievalPipeline` (core/rag_strategies/base.py) owns the mechanics — retriever calls, evidence scoring, budgets, the loop, the audit — and each strategy class owns only the **decisions**: is retrieval needed at all, how deep, what sources, when the evidence is bad, when more is needed. Six decision-sets, one pipeline: nothing can drift into six security postures.

```
                    query
                      │
        ┌─────────────▼──────────────┐
        │ 1. classify_query          │  heuristics (deterministic)
        └─────────────┬──────────────┘
                      ▼
        ┌─ strategy.needs_retrieval ──┐   no → answer without retrieval
        │ 2. retrieval needed?        │        (chit-chat / arithmetic /
        └─────────────┬───────────────┘         device commands)
                      │ yes
        ┌─ strategy.plan ─────────────┐
        │ 3. depth + sources          │  top_k, collections, sub-queries
        └─────────────┬───────────────┘
                      ▼
        ┌─ scoped retriever ──────────┐   ← ACL enforced HERE, in
        │  (rag_service closure)      │     rag_service, before the
        └─────────────┬───────────────┘     strategy ever sees results
                      ▼
        ┌─ 4. evaluate evidence ──────┐   heuristics.score_evidence
        │  (+ strategy LLM judges)    │   + LLM grades where the strategy pays
        └─────────────┬───────────────┘
              weak ▼
        ┌─ strategy.correct ──────────┐   reworded / narrowed / decomposed
        │ 5. correct + 8. go again    │   queries; loop bounded (≤3 rounds)
        └─────────────┬───────────────┘
                      ▼
        ┌─ 6. answer (answerer) ──────┐   LLM-grounded w/ citations [n]
        │  7. more needed?            │   or extractive spans (offline)
        └─────────────┬───────────────┘
                      ▼
        ┌─ 9. cited answer ───────────┐   citations, evidence, trace,
        └─────────────────────────────┘   grounded flag, hallucination_risk
```

## 2. The six strategies (core/rag_strategies/strategies.py)

| strategy | judges | router (step 2) | depth (step 3) | correction (step 5) | sufficiency (step 7) |
|---|---|---|---|---|---|
| `adaptive` | LLM | one router call: retrieve? complexity? | complexity-scaled top_k | LLM rewrites on weak retrieval | heuristic gate |
| `agentic` | LLM | retrieve unless trivially simple | deep (8+) + LLM **sub-question decomposition** for multi-hop | LLM narrowing rewrite | LLM sufficiency verdict + confidence each round |
| `corrective` (CRAG) | LLM | always retrieve | deep (8→12) | **retrieval-quality gate**: grades every chunk, rewrites on weak mean relevance | mean-relevance threshold |
| `self_rag` | LLM | skip for simple | deep (8) | rewrite when *no* chunk is relevant (ISREL) | ISREL per-chunk gate |
| `local_adaptive` | none | heuristic router (skips chit-chat/commands/arithmetic) | heuristic depth + angle queries for multi-hop | lexical: drops uncovered noise terms | heuristic relevance gate |
| `local_self_rag` | none | heuristic router | deep (10→14) | keyword rewrites | strict heuristic gate (0.5) |

**Auto-resolution:** `strategy="auto"` runs an LLM strategy when a generation rung is reachable and a local strategy otherwise. An explicitly-requested LLM strategy on an offline machine **degrades to its local twin** (adaptive/agentic/corrective → `local_adaptive`, self_rag → `local_self_rag`) instead of failing.

## 3. Offline mode: available when practical, honest when not

Three rungs, walked automatically:

1. **Full online** — LLM strategies with real model judges, grounded LLM answers through the ladder (which itself walks Live → REST → LOCAL, so a quota-dead API key still answers via Ollama/GGUF).
2. **LLM rung down mid-call** — the answerer wrapper in `rag_service.answer` falls through to the extractive answerer on any failure; the answer degrades, it never disappears.
3. **Fully offline** — local strategies: every decision from deterministic heuristics (zero model calls), and answers come from the **extractive answerer**: verbatim sentence spans of the retrieved evidence, cited by block number. Unsupported claims are **structurally impossible** — the answer IS the evidence. When nothing matches, it says "The knowledge base does not cover this."

`llm_ready()` probes cheaply (API key configured, or local rung configured with enough RAM) — no network call, no load.

## 4. Hallucination reduction (the layered defense)

1. **Grounded prompting** — the LLM answerer's prompt: answer only from numbered evidence, cite like [1], refuse if insufficient. A conversational answer without evidence (chit-chat path) is marked `grounded: false`.
2. **Extraction when offline** — local answers are evidence spans, not generations.
3. **Pipeline ISSUP check** — before the answer is returned, `heuristics.support_score` measures how much of the answer's terms the evidence covers (direction matters: answer-covered-by-evidence). An answerer's own `grounded` claim is *verified*, not trusted (`support >= 0.2` required).
4. **`hallucination_risk` grading** — low / medium / high from support level, hedging language, ungrounded markers ("as we all know"), absolutes, and **fabricated numbers** (numbers in the answer that appear nowhere in the evidence — checked after stripping the [n] citation markers themselves). An empty or ungrounded answer grades `high`.
5. **Bounded evidence** — the budget any model sees is capped by `max_chars` (default 12 000); whole documents never reach a model (test-proven with an embedder spy across a full `answer()` run).
6. **Everything ships with citations** — `citations` (`"Condor Book, p.1 [strategies]"`), per-chunk `used_evidence` (doc_id/page/collection/score/relevance), and the `evidence` string with `[n]` blocks. `queries_used`, `iterations`, `corrected` and the human-readable `trace` make every answer auditable after the fact.

## 5. Security model — unchanged from Phase 3

Strategies never call `core/rag.py` and never choose their own scope. `rag_service.answer` builds the **pre-scoped retriever closure**: the caller's collection grants are intersected with whatever the strategy asks for, a caller-pinned `collection` binds the strategy too, out-of-scope asks return empty (and are audit-logged `svc:denied`), doc-scoped reads check the grant. `search()`, the REST query endpoint, the action tool, and the Phase-3 agent contracts are byte-identical — `answer` is purely additive.

## 6. Surfaces

- **Service**: `rag_service.answer(caller, query, *, strategy="auto", collection=None, doc_id=None, max_chars=12000) -> dict` — keys: `ok, strategy, answer, citations, evidence, used_evidence, queries_used, iterations, retrieval_needed, grounded, hallucination_risk, trace, error, support, confidence, complexity, elapsed_ms, caller`.
- **REST**: `POST /api/rag/answer` `{query, strategy?, collection?, doc_id?, caller?}` — bearer-authed; 200 / 403 (access denied) / 400 (empty query, unknown strategy). Known-bad strategy names are validated early → clean 400.
- **Agent**: `rag_agent` with `metadata.rag_action="answer"` (+ optional `rag_strategy`); `rag_action="read"` remains the Phase-3 chunk search.
- **No new agents**: the orchestrator registry still holds one `rag_agent`.

## 7. Evaluation evidence (tests/test_rag_strategies.py, 39 tests, all offline)

**Retrieval quality**
- top chunk comes from the right document for every one of the six strategies (parametrised — no cross-topic bleed);
- a messy natural-language question still retrieves the right document;
- a condor-vs-straddle comparison retrieves *both* sides;
- the correction loop: a strategy demanding a second round gets its rewritten query actually used (`queries_used`, `iterations >= 2`, second-round evidence present);
- scope discipline: an ungranted caller's answer contains zero out-of-scope chunks (ACL holds under the pipeline); a caller-pinned collection binds the strategy; `max_chars` bounds the evidence.

**Hallucination reduction**
- grounded answers always carry citations;
- extractive answers are verbatim evidence spans (every answer sentence found inside a retrieved chunk);
- unanswerable questions refuse or grade non-low — never a confident low-risk guess with nothing behind it;
- risk-grading ladder verified: supported-cited → low; half-fabricated → medium; fabricated-numbers → ≥ medium; fully-fabricated / ungrounded / empty → high;
- `support_score` direction test (answer⊂evidence scores high, the reverse doesn't);
- no-LLM-send: an embedder spy across a full `answer()` run shows only query-sized texts (nothing over 1 200 chars).

**Behaviours + offline**
- registry = exactly the six; every strategy answers with citations, trace, iterations and a risk grade;
- offline degradation (agentic→local_adaptive, self_rag→local_self_rag), auto→local offline;
- chit-chat / arithmetic / device commands skip retrieval entirely (step 2);
- LLM strategies' judge calls verified with a scripted fake (router refinement, sub-question decomposition, sufficiency verdicts).

**Surfaces + Phase-3 safety**
- agent `answer` mode COMPLETED with citations; `read` mode byte-compatible;
- REST answer endpoint: 200 flow, 401, 400 (empty), 400 (unknown strategy), scoped-caller isolation;
- Phase-3 regression suites all green (166 total).

## 8. Deliberate limits (Phase-5 candidates)

- Evaluation is behavioural + heuristic, not a benchmark suite (no labelled QA corpus yet); the risk grader is lexical, not semantic.
- CRAG's "search the web" branch is stubbed to query rewrites only — the library has no web retriever by design.
- One loop budget (`MAX_ITERATIONS=3`) serves all strategies; per-strategy budgets would let agentic go deeper at higher cost.
- The LLM answerer's ISSUP check is deterministic-lexical; a judge-call variant could catch paraphrased fabrication at one extra call per answer.
