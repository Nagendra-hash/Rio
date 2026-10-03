---
name: genomic-intelligence
description: Predicts regulatory features, gene structure, and expression directly from DNA sequence using Genomic Intelligence's hosted transformer DNA language models — no local GPU or model weights. Six tasks over a REST API and a hosted MCP server (keyless public demo): promoter regions, splice donor/acceptor sites, enhancer activity, chromatin state, sequence-to-expression (log TPM), and de-novo gene annotation, plus a composite find-genes-then-predict-expression workflow. Use when the user has a gene symbol, a genomic region, or a DNA/FASTA sequence and wants any of these predictions, mentions Genomic Intelligence, genomicintelligence.ai, api.genomicintelligence.ai, or mcp.genomicintelligence.ai.
license: MIT
compatibility: Python 3.10+ with the `requests` library for the REST examples. Network access required. The REST `/v1` API needs a `GI_API_KEY` (a `gi_` bearer); the hosted MCP server at mcp.genomicintelligence.ai/mcp works keyless against a rate- and concurrency-limited public demo tier, key optional.
metadata: 
  version: 1.4
  last-reviewed: 2026-10-01
  skill-author: Genomic Intelligence
  trigger-keywords: DNA sequence prediction, regulatory genomics, promoter prediction, splice site prediction, enhancer activity, chromatin state, gene expression prediction, sequence to expression, log TPM, gene annotation, transcript prediction, DNA language model, genomic intelligence, hosted inference, Ensembl sequence, FASTA prediction, cis-regulatory, TSS window, DeepSEA, DeepSTARR, BigBird splice, MCP genomics
  openclaw: 
    primaryEnv: GI_API_KEY
    envVars: 
    - name: GI_API_KEY
      required: false
      description: Optional gi_ bearer key for the REST /v1 API and higher MCP rate and concurrency limits. The hosted MCP demo runs keyless; request a key at contact@genomicintelligence.ai.
---

# Genomic Intelligence — DNA Sequence Models

Genomic Intelligence (GI) serves transformer DNA language models over six
sequence-analysis tasks on managed GPUs. Give it a **gene symbol**, a **genomic
region**, or a **DNA/FASTA sequence**; it returns structured predictions —
promoter regions, splice sites, enhancer activity, chromatin state, expression
(log TPM), and de-novo gene annotation. Nothing runs locally: no model weights,
no GPU, no heavy Python stack. It is a thin client over a hosted, versioned
inference API.

**Official docs:** [docs.genomicintelligence.ai](https://docs.genomicintelligence.ai) ·
REST contract at [api.genomicintelligence.ai/v1/openapi.json](https://api.genomicintelligence.ai/v1/openapi.json) ·
hosted MCP server at `https://mcp.genomicintelligence.ai/mcp`

## When to use this skill

Use GI when the user has DNA and wants a model prediction:

- **Find promoters** in a genomic region (`promoter`)
- **Predict splice** donor/acceptor sites (`splice`)
- **Score enhancer activity** — developmental & housekeeping (`enhancer`)
- **Annotate chromatin state** across hundreds of tracks (`chromatin`)
- **Predict expression** as log(TPM+1) from a sequence + cell-type context (`expression`)
- **Annotate genes/transcripts** de novo, no reference needed (`annotation`)
- **Find the genes in a region and predict each one's expression** (composite)

Not for local alignment, variant calling, or file I/O — use a local tool
(BioPython, bcftools) for those. GI is for **model inference from sequence**.

> Research and development use. Not for clinical or diagnostic decisions.

## Two ways to call GI

### Hosted MCP server (keyless; preferred on MCP hosts)

GI hosts an MCP server at `https://mcp.genomicintelligence.ai/mcp` (Streamable
HTTP). When your agent host supports MCP, prefer it: it works **keyless** against
a rate- and concurrency-limited public demo tier, and an optional `gi_` bearer
key raises those limits. It exposes acquisition tools that return a **sequence handle**
(`sequence_ref`) and `predict_*` tools that take that handle, so large sequences
stay out of the context. See [MCP workflow](#mcp-workflow-handle-based) below and
`references/mcp.md`.

### REST API (universal)

Plain HTTP with `requests` against `https://api.genomicintelligence.ai/v1`. The
REST path **requires** a `GI_API_KEY` (a `gi_` bearer). Use it on any host, in
scripts, or when you need the raw envelope. See [Core REST workflow](#core-rest-workflow).

## Access and authentication

1. The **hosted MCP demo is keyless** — try it with nothing set.
2. REST prediction and job operations need a key, sent as `Authorization: Bearer <key>`.
   Public `GET /v1/tasks/{task}/models` discovery needs no key and is rate-limited
   by source IP; inspect model windows and bounds before requesting access.
   See the [current authentication contract](https://docs.genomicintelligence.ai/).
   Request a prediction key at [contact@genomicintelligence.ai](mailto:contact@genomicintelligence.ai).
3. **Never hardcode the key.** Read it from the `GI_API_KEY` environment variable
   (or a `.env` via `python-dotenv`). Never commit keys.

```bash
export GI_API_KEY="gi_yourkeyhere"     # optional for MCP; required for REST
export GI_BASE_URL="https://api.genomicintelligence.ai"   # override for staging
```

Keys are scoped to a partner tier with concurrency and per-minute caps. A `429`
means you hit a cap — back off and retry, or ask GI to raise your tier.

## The six tasks

Each task is **its own published operation** with its own request schema, its own
minimum length, and its own closed `options` object — `POST
/v1/tasks/promoter/predict`, `/v1/tasks/splice/predict`,
`/v1/tasks/enhancer/predict`, `/v1/tasks/chromatin/predict`,
`/v1/tasks/annotation/predict`, `/v1/tasks/expression/predict`. Each path is a
literal string, so nothing needs to be constructed, and there is no shared
`PredictRequest` schema. Body is `{sequence, sequence_name?, model?,
options?}`, returning a `{data, meta}` envelope. What differs per task:

| Task | Recommended mode | Accepted length | `context_window_bp` | Notes |
|---|---|---|---|---|
| `promoter` | sync | 300–500,000 bp | 2,000 bp | sliding-window promoter regions |
| `splice` | sync | 100–500,000 bp | 15,000 bp | donor/acceptor sites (long-context BigBird); strand-specific — feed transcript orientation |
| `enhancer` | sync | 50–500,000 bp | 249 bp | dev + housekeeping scores (DeepSTARR, *Drosophila*) |
| `chromatin` | sync | 200–500,000 bp | 1,000 bp | hundreds of