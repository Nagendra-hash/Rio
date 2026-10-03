---
name: bulk-rnaseq
description: Prepares bulk RNA-seq FASTQ, Salmon, STAR or featureCounts output for gene-level differential expression. Covers nf-core/rnaseq and standalone quantification, biological replication, strandedness, reference provenance, validated count assembly and a PyDESeq2 handoff. Use for FASTQ-to-counts analysis, nf-core/rnaseq configuration, STAR/Salmon quantification, or building a counts matrix for DESeq2. For single-cell data use scanpy; for statistical fitting alone use pydeseq2.
license: MIT
compatibility: Requires Python 3.11+ with pandas and numpy; Salmon import also needs pytximport. Read processing needs Nextflow with containers or standalone bioinformatics tools. Network access is needed for installation and reference downloads.
metadata: 
  version: 2.0
  last-reviewed: 2026-09-30
  skill-author: K-Dense Inc.
---

# Bulk RNA-seq

## Overview

This skill prepares bulk RNA-seq reads and quantification output for a reproducible gene-level comparison. It owns sample validation, reads-to-counts recipes, and the count/metadata handoff; downstream statistical fitting and enrichment remain in their specialist skills.

"Defensible" means three things, applied throughout:
- **Reproducible** — pinned pipeline/tool versions, containers where possible, recorded parameters, fixed random seeds.
- **Quality-gated** — QC is inspected and acted on before, during, and after quantification, not skipped.
- **Statistically sound** — adequate replication, a design that matches the biology, counts handled correctly, and FDR-controlled testing.

The pipeline is: **FastQC/trim → align/quant (STAR/Salmon) → counts → DE (pydeseq2) → enrichment (pathway-enrichment) → figures**.

## When to Use This Skill

Use this skill when the user wants to:
- Go from FASTQ files (or a sequencing run) to differentially expressed genes and pathways.
- Run or configure `nf-core/rnaseq`, or align/quantify with STAR, Salmon, or featureCounts.
- Turn Salmon/STAR/featureCounts output into a counts matrix ready for DESeq2/PyDESeq2.
- Design or sanity-check a bulk RNA-seq experiment (replicates, batch, strandedness) before committing compute.
- Scope an end-to-end RNA-seq analysis and decide which tools and skills to chain.

This is **bulk** RNA-seq (samples = biological specimens). For single-cell/nuclei data use `scanpy`; for the DE statistics alone use `pydeseq2`; for enrichment alone use `pathway-enrichment`.

## The Pipeline at a Glance

```mermaid
flowchart TD
    fastq["Raw FASTQ + samplesheet"] --> qc["FastQC + MultiQC"]
    qc --> trim["Trim: fastp / Trim Galore"]
    trim --> align["Align + quant: STAR and/or Salmon"]
    align --> counts["Gene-level counts matrix"]
    counts --> de["Differential expression"]
    de --> enrich["Pathway / GSEA enrichment"]
    de --> fig["Figures"]
    enrich --> fig
    nfcore["nf-core/rnaseq via nextflow skill"] -.->|"path A"| align
    manual["Standalone recipes (this skill)"] -.->|"path B"| align
    bridge["build_counts_matrix.py (this skill)"] -.-> counts
    pydeseq2skill["pydeseq2 skill"] -.-> de
    pwskill["pathway-enrichment skill"] -.-> enrich
    vizskill["scientific-visualization skill"] -.-> fig
```

## Two Upstream Paths — Pick One

The reads → counts stage can be run two ways. Both produce gene-level counts,
but STAR/featureCounts and Salmon/tximport do not generally give identical or
interchangeable values: assignment rules, multimapping, and effective-length
corrections differ. Choose one quantification route for the entire comparison.

| Use **Path A — `nf-core/rnaseq`** when… | Use **Path B — standalone tools** when… |
|------------------------------------------|------------------------------------------|
| You want the field-standard, audited, citable pipeline with one command | You have a few samples and want to learn/inspect each step |
| Many samples, or you'll scale to HPC/cloud | No Nextflow/containers available, or a constrained environment |
| Reproducibility and a full MultiQC report matter most | You need a non-standard step the pipeline doesn't expose |
| → Drive it through the **`nextflow`** skill | → Follow `references/upstream-manual.md` |

When unsure, prefer **Path A**: `nf-core/rnaseq` already wires together FastQC → trimming → STAR/Salmon → quantification → tximport → MultiQC with sensible, reviewed defaults, which is the most defensible option. Path B exists for transparency and constrained setups.

Both paths converge on a **gene-level counts matrix**. Preserve whether counts are
raw or length-scaled, the transcript-to-gene mapping release, and any offsets.
Length-scaled counts must not receive a second transcript-length correction.

## Setup

```bash
# This skill's glue (bridge + handoffs) — Python
uv pip install "pytximport==0.13.0" pandas numpy

# Downstream skills install their own deps:
#   pydeseq2 skill           -> uv pip install pydeseq2
#   pathway-enrichment skill -> uv pip install gseapy gprofiler-official

# Path A (nf-core): only Nextflow + a container engine are needed — see the `nextflow` skill.

# Path B (standalone tools): install via bioconda. Pin versions for reproducibility.
conda create -n rnaseq -c conda-forge -c bioconda --strict-channel-priority \
  fastqc fastp trim-galore "star=2.7.11b" "salmon=2.8.0" subread multiqc
```

Reviewed against nf-core/rnaseq 3.27.0, STAR 2.7.11b documentation, Salmon 2.8.0,
pytximport 0.13.0 and PyDESeq2 0.5.4. The bundled Python bridge and a tiny Salmon
index/quant run were executed; the full Nextflow/STAR/trimming recipes are illustrative,
not an end-to-end validation. Salmon 2.x cannot read older C++ indexes: rebuild them.
The bridge's length-scaled Salmon route is for full-length bulk RNA-seq; 3′ counting
assays need original counts without transcript-length correction.

Record the exact versions you use (pipeline revision, tool versions, reference genome + annotation release) — they belong in the methods section and make the analysis reproducible.

Version 2.0 makes STAR strandedness explicit and rejects ambiguous samples, gene sets,
transcript mappings and fractional featureCounts data that older bridge versions accepted.

## Quick Start

### Path A — nf-core/rnaseq (recommended)

```bash
# 0. Validate the samplesheet first (catches the most common failures early)
python scripts/validate_samplesheet.py --samplesheet samplesheet.csv --nfcore

# 1. Smoke-