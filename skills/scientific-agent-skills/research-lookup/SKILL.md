---
name: research-lookup
description: Compiles current scholarly evidence for a scientific manuscript or research brief when the user explicitly asks to gather literature, references, background evidence, competing findings, or a manuscript research packet. Uses Parallel Search by default, Parallel Extract for source retrieval, Parallel Research for explicitly deep/exhaustive work, optional explicit Parallel Chat, and optional Perplexity only when requested or allowed as a failure fallback.
license: MIT license
compatibility: Requires Python 3.10+ and network access; targets parallel-web-tools CLI 0.9.3 for Search, Extract, and Research. Explicit Chat requires requests and PARALLEL_API_KEY; optional Perplexity through openrouter.ai requires requests and OPENROUTER_API_KEY.
metadata: 
  version: 1.7
  last-reviewed: 2026-09-30
  skill-author: K-Dense Inc.
  openclaw: 
    primaryEnv: PARALLEL_API_KEY
    envVars: 
    - name: OPENROUTER_API_KEY
      required: false
      description: Optional OpenRouter key for explicit Perplexity use.
---

# Research Lookup

Compile the external evidence needed to plan and write a high-quality scientific
manuscript. The default academic workflow targets **60 verified, unique references**
and produces a manuscript-ready research packet rather than a loose list of links.
Here `verified_references` counts successful retrieval of nonempty Extract excerpts,
excluding flagged retractions. It does not establish bibliographic identity, claim
support, peer review, or absence of corrections; those require source review.

## Scope and boundaries

Use this skill when the user explicitly wants:

- literature and background research for a manuscript
- many high-quality academic references
- evidence supporting or contradicting a scientific claim
- a structured evidence matrix or claim-to-source map
- current studies, methods precedent, mechanisms, limitations, or research gaps

Do not activate it for casual factual questions that do not need research, private
or unpublished material, or a claim that can be answered from user-provided files.
Query text is sent to Parallel. It is sent to OpenRouter only when Perplexity is
explicitly selected or the user enables that fallback.

This skill compiles **external evidence**. It cannot supply the user's unpublished
study data, decide what their Results show, or guarantee systematic-review
completeness. For a PRISMA-style systematic review, use `literature-review` for
protocols, database-specific searching, screening, exclusion reasons, and risk of
bias.

## Parallel-first routing

| Need | Backend | Selection |
|---|---|---|
| Manuscript literature and references | Parallel Search + Extract | Default; use `--academic` |
| Fast bounded web lookup | Parallel Search | Use `--no-academic` |
| Deep/exhaustive multi-source report | Parallel Research | Explicit `--force-backend research` |
| OpenAI-compatible synthesis with research basis | Parallel Chat | Explicit `--force-backend chat` |
| Optional alternative academic search | Perplexity via OpenRouter | Explicit or enabled failure fallback |

Important compatibility behavior:

- A bare script query uses **Parallel Search**. Chat Completions remains available
  only through explicit backend selection.
- `--force-backend parallel` remains an alias for explicit Parallel Research.
- Academic keywords select the multi-pass Parallel academic strategy; they do not
  silently switch the provider to Perplexity.
- `--batch`, `--json`, `-o/--output`, the `ResearchLookup` class, progress output,
  and the existing result envelope remain supported.

## Recommended manuscript workflow

### 1. Capture manuscript context

Use the user's available context to constrain retrieval:

- research question or hypothesis
- study type
- population or biological/technical system
- intervention or exposure
- comparator
- outcomes
- field and date range
- target journal, if known

The script accepts a JSON object through `--context-file`. Do not invent missing
study details. A bare topic is supported, but the packet will flag its section briefs
as broad.

Example:

```json
{
  "research_question": "How does intervention X affect outcome Y?",
  "study_type": "prospective cohort",
  "population": "adults with condition Z",
  "exposure": "intervention X",
  "comparator": "standard care",
  "outcomes": ["primary outcome Y", "adverse events"],
  "field": "clinical epidemiology",
  "target_journal": "Journal Name"
}
```

### 2. Run the academic evidence pipeline

From the repository root (illustrative paid-service run; review its scope first):

```bash
python skills/research-lookup/scripts/research_lookup.py \
  "Evidence relevant to the manuscript's research question" \
  --academic \
  --target-references 60 \
  --context-file manuscript-context.json \
  --packet-dir sources/manuscript-research \
  --json
```

The academic pipeline runs bounded `advanced` Search passes for:

1. recent peer-reviewed primary studies
2. systematic reviews, meta-analyses, and consensus evidence
3. seminal and foundational publications
4. methods, protocols, validation, benchmarks, and mechanisms
5. contradictory, null, negative, replication, and limitation evidence
6. an unrestricted companion search when filtered passes do not reach the target

It prioritizes PubMed/PMC, Europe PMC, Crossref, OpenAlex, Semantic Scholar,
arXiv/bioRxiv/medRxiv, major journals, and authoritative institutional sources.
Domain filters are not treated as exhaustive; the companion pass reduces blind spots.

### 3. Verify promising sources with Parallel Extract

Search candidates are deduplicated and ranked before batched extraction. Extraction
requests source-supported:

- authors, year, venue, DOI, and PMID
- publication and study design
- population/system and sample size
- methods, intervention/exposure, comparator, and outcomes
- quantitative findings, uncertainty, and statistical values
- limitations and conclusions
- preprint, correction, retraction, or withdrawal status

The default extraction limit equals `--target-references`. Use `--extract-limit N`
to reduce cost or `--no-extract` only when unverified search results are acceptable.
The coverage report will not count search-only records as verified, even when they
contain a DOI or PMID. Successful extraction is only a retrieval check: inspect each
excerpt against the proposed claim, co