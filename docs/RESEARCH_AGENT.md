# Phase 6 — Research Agent

> Companion to `ARCHITECTURE.md`, `docs/CODING_AGENT.md` and `docs/RAG_STRATEGIES.md`.
> Status: **Built** · Agent: `orchestrator/agents/research_agent.py` · Sources: `orchestrator/agents/research_tools.py`

---

## 1. What it is

The research agent is the **real Phase-2-style replacement** of the Phase-1
`research_agent` stub, on the Jarvis Agent interface
(`orchestrator/base.py::BaseAgent`) — same registry name, same contracts,
same router. It implements, as code (not prompt hope):

| Capability | Where |
|---|---|
| Understand the research question | `QUESTION` stage — resolve question, source tiers, resume state |
| Create a research plan | one LLM call → strict JSON (sub-questions, queries, tiers, risks); heuristic no-model floor |
| Search permitted sources | `SourceToolbox.web_search()` — house DDG backend; per-query failures recorded |
| Collect evidence | `_collect()` — normalise, dedupe by URL/title, cap (`MAX_SOURCES=24`), number `[1..n]` |
| Use RAG | `SourceToolbox.local_kb()` — `core/rag_service.search` as caller `research_agent` (same ACL/audit as `rag_agent`; blind caller degrades to zero results) |
| Compare sources | claim clustering + corroboration across hosts; conflicts marked |
| Identify uncertainty | every claim graded **supported / contested / unverified**; uncertainty section in every report |
| Produce a structured report | grounded writer over verified evidence + `data["report"]` record |
| Preserve source references | `[n]` citations inline + full `Sources:` list + every source stored in the checkpoint |

**Disabled by default** (shadow-mode convention): set `JARVIS_RESEARCH_AGENT=on`
to register the real agent instead of the stub.

```python
from orchestrator.agents import build_default_registry
reg = build_default_registry()          # stub unless the env flag is on
agent = reg.get("research_agent")
```

## 2. Workflow

```
QUESTION → PLAN → SEARCH → COLLECT → COMPARE → VERIFY → REPORT
```

1. **QUESTION** — the ask (metadata `allow_web` / `allow_local_kb` override
   the source tiers), or **resume** a checkpoint (`metadata.resume=<task_id>`).
2. **PLAN** — one LLM call (`PLAN_PROMPT` → strict JSON): understanding,
   `sub_questions`, `search_queries` (≤4), `use_local_kb`, `deep_fetch`,
   `risks`. A vague question returns `NEEDS_INPUT`; a missing model degrades
   to the heuristic plan (the question itself becomes the query).
3. **SEARCH** — queries run through the permitted-source toolbox only. Web
   failures and KB failures are **recorded** (`failed_sources`), never
   invented over.
4. **COLLECT** — dedupe, cap, number. Each source keeps
   `{index, title, url, host, snippet, tier, query}`.
5. **COMPARE** — claims are extracted from evidence sentences
   (`_Claims.from_evidence`); hosts counted; corroboration/conflict edges
   computed per claim.
6. **VERIFY** — `_verify_claims()` grades each claim against the pooled
   evidence with the RAG strategies' direction-checked `support_score`
   (claim ⊂ evidence, threshold `0.6`) plus **cross-host corroboration**:
   - `supported` — support ≥ threshold (extractive floor only ever sees these),
   - `contested` — another host carries contradicting markers,
   - `unverified` — everything else.
7. **REPORT** — grounded writer (`REPORT_PROMPT`) must cite `[n]` and put
   unsupported claims ONLY in `unverified`; with no model, the deterministic
   extractive report builds the body from supported claims alone (the
   report **is** the evidence — fabrication is structurally impossible,
   the same philosophy as the offline RAG answerer).

**Unverified claims are never presented as facts**: they go to an explicit
"Unverified" section (text + `data["report"]["unverified_claims"]`), and a
question with zero support returns "No evidence found … I will not guess."

Every run is checkpointed to `memory/research_agent/<task_id>.json`
(`JARVIS_HOME`-relocatable): question, plan, sources, verdicts, report,
failed sources — the full audit trail, and the resume payload.

## 3. Source handling (`research_tools.py`)

**Permitted sources, and only these:**

| Tier | What | Rules |
|---|---|---|
| WEB SEARCH | house DDG backend (`actions/web_search`) | no keys, cached, rate-limit aware; blocked URLs **dropped** from results |
| WEB FETCH | bounded plain-text GET (opt-in via plan `deep_fetch`) | http/https only, text-like content types only, ≤200 KB raw / 8 000 chars text, ≤2 pages per task |
| LOCAL KB | `rag_service.search(caller="research_agent", …)` | grants in `config/rag_callers.json`; audit-logged; read-only |

**Structural URL policy** (`url_allowed`) — enforced before any network:

- schemes: `http`/`https` only (`file:`, `ftp:`, `data:` cannot run)
- credential-carrying URLs (`user:pass@host`) cannot run
- hosts that are literal or resolve to loopback/private/link-local ranges
  cannot run (SSRF guard: `127.0.0.1`, `10/8`, `172.16/12`, `192.168/16`,
  `169.254/16` incl. cloud metadata, `0/8`, `100.64/10`, `fc00::/7`, `fe80::/10`)
- `.local`, `.internal`, `.localhost`, `.home.arpa` and unresolvable hosts
  cannot run

**Secret safety** — every search snippet, fetched page and KB chunk is
masked (`mask_secrets`, the same choke point as the coding agent) before it
can reach a model or a report.

## 4. Report format

Text body (delivered to the user):

```
<grounded summary, every claim cited like [1]>

Unverified (not enough evidence — not facts): <claims without citations>

Sources: [1] <title> — <url>; [2] <title> — <url>; …
```

Structured record (`result.data["report"]`):

```json
{
  "summary": "...",
  "key_findings": ["... [1]", "... [2]"],
  "unverified_claims": ["..."],
  "uncertainties": ["all evidence comes from a single host …", "…"],
  "supported_claims": 3,
  "confidence": "low|medium|high"
}
```

`result.data` also carries `sources` (full references incl. host, tier,
query), `verdicts` (per-claim support scores), `failed_sources`,
`queries_used` and `stage_trace` — the auditable story of the run.

## 5. Model Router integration

LLM calls go through `orchestrator/model_router` with **`ModelTask.RESEARCH`**
(cheapest-capable-first with explained fallbacks; Gemini is the specialist
rung for research). If the router cannot serve, the house `core/gemini`
SMART ladder is the fallback; if that fails too, the run degrades to the
heuristic plan + extractive report — honest and shallower, never fabricated.
Injectable (`llm=...`) for tests and the offline demo.

## 6. Sample workflow (offline)

```bash
# end-to-end happy path on a small technical question (no network, no key):
.venv/bin/python scripts/research_agent_demo.py                # web
.venv/bin/python scripts/research_agent_demo.py --task rag     # local KB only
.venv/bin/python scripts/research_agent_demo.py --task unfounded
#   ↑ nothing findable → zero supported claims, honest refusal, no guesses
```

## 7. Tests

```bash
.venv/bin/python -m pytest tests/test_research_agent.py -q
```

Coverage: the URL policy (11 blocked shapes incl. metadata IPs, credential
URLs, non-http schemes, unresolvable hosts), tier switches, search-backend
failure paths, blind-caller KB degradation, secret masking of snippets,
the verification ladder (supported / contested / unverified), the full
staged workflow with scripted LLM + scripted sources (plan-gates-search,
unfounded-question honesty, single-source uncertainty, KB integration with
a real offline mini-library, deep-fetch permission check, checkpoint +
resume), the extractive no-model floor, and registry integration
(stub default / real agent with the flag).

## 8. Files

| File | Role |
|---|---|
| `orchestrator/agents/research_tools.py` | source permissions, URL policy, guarded toolbox, masking |
| `orchestrator/agents/research_agent.py` | the staged workflow, verification, checkpoints, prompts |
| `orchestrator/agents/__init__.py` | registration behind `JARVIS_RESEARCH_AGENT=on` |
| `tests/test_research_agent.py` | offline test suite |
| `scripts/research_agent_demo.py` | offline sample workflow |

## 9. Deliberate limits

- Verification is lexical (shared with the RAG strategies), not semantic;
  paraphrased contradictions without conflict markers grade unverified, not
  contested.
- Web search is the DDG text backend only — no Google/Bing API tiers yet;
  `deep_fetch` is capped at 2 pages per run.
- One `support_score` threshold (0.6) for all claims; per-claim-type
  thresholds are a Phase-7 candidate.
