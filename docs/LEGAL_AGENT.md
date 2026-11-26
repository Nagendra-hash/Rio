# Legal Agent — Phase 16

> Companion to `ARCHITECTURE.md` and the agent docs (`FINANCE_AGENT.md`,
> `RESEARCH_AGENT.md`). Status: **Built** · Agent:
> `orchestrator/agents/legal/` · Tests: `tests/test_legal_agent.py` ·
> Demo: `scripts/legal_agent_demo.py`

## 1. What it is

The Phase 16 specialist on the standard BaseAgent contract
(`orchestrator/base.py`), registered under the canonical name
`legal_agent`; enabled with `JARVIS_LEGAL_AGENT=on` (shadow-mode
convention — off by default like every other phase).

Capabilities, as code (not prompt hope):

| Capability | Where | Enforcement |
|---|---|---|
| Read legal PDFs | `pages.py` — pdfplumber → PyPDF2 ladder (the same one `core/rag.py` uses; no new dependency), plus txt/md/log/csv and docx | unreadable/scanned PDFs report honestly, never raise |
| Extract clauses | `clauses.py` — numbered-heading rooflines (§8.2-style), 19 clause families (termination, liability, indemnity, confidentiality, IP, payment, data, dispute, …) | every clause keeps a `PageSpan` (page + section + char offsets) |
| Summarize sections | `clauses.py::summarize_document` — EXTRACTIVE: each section's own two densest sentences, page-cited | unsupported summary text is structurally impossible: the summary IS the document's sentences |
| Compare versions | `diff.py` — structural diff keyed by § number + heading; jaccard text similarity | changed/added/removed clauses each cite both pages |
| Retrieve relevant passages | `rag_service.search/answer` as caller `legal_agent`, collection-pinned to `legal_docs` | zero results = honest refusal; weakly-matching passages are dropped via `support_score` |
| Highlight questions/risks | `risk.py` — 14 regex risk patterns (auto-renewal, broad indemnity, unlimited liability, termination asymmetry, unilateral amendment, IP grabs, restrictive covenants, arbitration/class waivers, …) | every finding = severity + a QUESTION for the human + why + the quoted sentence + page |
| Cite source pages/sections | `schema.py::Clause.citation()` — `lg-… p.4 §8.2` | spans are structural, not guessed |

**Do not present output as legal representation or definitive legal
advice** — enforced in `safety.py::is_advice_request` (advice-shaped asks
are DECLINED with a fixed refusal, in code), a BLOCKED list (drafting
instruments, contacting counterparties, privileged access — never run,
even approved), and `safety.DISCLAIMER_TEXT` appended to every
substantive reply: *Not legal advice… Have a qualified lawyer review
anything consequential.*

## 2. Files

| File | Role |
|---|---|
| `orchestrator/agents/legal/schema.py` | `DocumentMeta`, `PageSpan`, `Clause` + validators, `trim`, kind sniffing |
| `orchestrator/agents/legal/pages.py` | the PDF/text reader: page-wise text, sha256, sections with page attribution |
| `orchestrator/agents/legal/clauses.py` | clause extraction (19 families), extractive summarization |
| `orchestrator/agents/legal/diff.py` | version comparison (changed/added/removed, both pages cited) |
| `orchestrator/agents/legal/risk.py` | risk patterns → severity + question + why + quoted evidence + page |
| `orchestrator/agents/legal/safety.py` | advice guardrail, BLOCKED list, permission levels, disclaimer |
| `orchestrator/agents/legal/vault.py` | the isolated vault (below) |
| `orchestrator/agents/legal/checkpoint.py` | crash-safe approval checkpoints (productivity contract) |
| `orchestrator/agents/legal/legal_agent.py` | the BaseAgent: 11 actions, approval parking, citation formatting |
| `tests/test_legal_agent.py` | 53 offline tests |
| `scripts/legal_agent_demo.py` | offline end-to-end demo (sandboxed KB, no network/key) |

## 3. Isolated legal-document storage and access control

Three layers, all baked in:

1. **The vault** — `memory/legal/` (relocated by `JARVIS_HOME`): 0600
   files in a 0700 dir, thread-safe, stdlib-only, `redact_obj()` before
   every disk write, actions-only audit (doc ids and counts — never
   contract text). Registries, page text, clause caches, version chains
   and approval checkpoints live nowhere else.
2. **The retrieval collection** — indexed documents live ONLY in the
   `legal_docs` RAG collection (added to `core.rag.COLLECTIONS`), and
   `config/rag_callers.json` grants `legal_agent` EXACTLY that
   collection, read-only. A foreign caller is blind by construction
   (the `rag_service` ACL, Phase 4- tested).
3. **The approval gate** — ingest (vault + collection write) and forget
   (vault + collection delete) park the EXACT payload behind
   `NEEDS_APPROVAL` (checkpointed at `memory/legal/checkpoints/`);
   the approved re-dispatch executes the parked payload — never a fresh
   parse of the user's words; a second resume is DECLINED.

## 4. Actions

| ask | action |
|---|---|
| "ingest this contract" (+ path/attach) | preview → approval → vault + `legal_docs` index; title/kind/version/supersedes from metadata |
| "analyze it" | clauses (§-cited) + extractive section summary + flagged questions |
| "summarize" | the summary alone |
| "extract the clauses" | numbered clause list with type + page |
| "where is arbitration handled?" | passage retrieval with `[n]` citations |
| "compare v2 vs v1" | structural diff: `~ changed (n% differs; old p.x → new p.y)` / `+ added` / `− removed` |
| "forget that document" | approval → vault + index deletion |
| "should I sign this?" | DECLINED in code — the advice guardrail |
| "show the library" | the vault's docs, kinds, versions, chains |

Explicit control: `metadata={legal_action: 'ingest'|'analyze'|'clauses'|
'summarize'|'retrieve'|'ask'|'compare'|'forget'|'library', …}`.

## 5. Offline verification

```bash
.venv/bin/python -m pytest tests/test_legal_agent.py -q     # 53 passed
.venv/bin/python scripts/legal_agent_demo.py                # full walkthrough
```

No test touches the network or a real PDF corpus: `JARVIS_HOME` points at
a temp dir, embeddings run on a deterministic hash-vector fake `core`
gemini module, and the PDF path exercises a real minimal PDF via the
extraction ladder (skips honestly if no writer/extractor pair exists).

Coverage: the reader (txt + PDF + broken/missing files + sha256 + kind +
sections), clause extraction with spans and type families, extractive
summaries (verbatim-in-document proof), the guardrails (6 advice shapes
declined; blocked refusal; disclaimer present), the vault (location,
0700/0600 perms, actions-only audit), the full gated pipeline (park →
exact payload → resume → re-resume declined → unknown resume fails),
retrieval (citations, weak-passage drop, honest zero-result, foreign
caller blind), version compare (changed/added/removed/identical),
risk scan (questions+pages, severity interleaving, never fabricates),
and wiring (classifier lane, explicit routes, registry gate).

## 6. Hard rules summary

1. **Not a lawyer.** Advice-shaped asks are refused in code; the
   disclaimer ships on every substantive reply; blocked actions never
   run, even approved.
2. **Isolated storage.** `memory/legal/` + the `legal_docs` collection,
   both exclusively reachable through this agent's ACL identity.
3. **Confirmation before writes.** Ingest and forget park the exact
   payload and execute only that.
4. **Citations or nothing.** Analysis, retrieval and diffs all point at
   pages/sections; a passage that barely covers the query terms is not
   shown; an empty result is an honest "I will not guess".
