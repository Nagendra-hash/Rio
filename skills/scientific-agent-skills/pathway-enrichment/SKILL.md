---
name: pathway-enrichment
description: Performs pathway and gene-set enrichment analysis on gene lists or ranked gene data and interprets the results. Used when the user has a set of genes (differentially expressed genes from PyDESeq2/Scanpy, CRISPR-screen hits, cluster marker genes, proteomics hits) and wants to know which biological pathways, GO terms, or gene sets are over-represented or enriched. Covers over-representation analysis (ORA / Enrichr / Fisher / hypergeometric), ranked Gene Set Enrichment Analysis (GSEA / preranked), single-sample scoring (ssGSEA/GSVA), and functional profiling via gseapy, g:Profiler, Enrichr libraries, MSigDB, GO, KEGG, Reactome, and WikiPathways — plus gene-ID mapping, choosing the right background universe, multiple-testing correction, redundancy reduction, dotplots/enrichment maps, and publication-ready tables. Use this for "pathway analysis", "enrichment analysis", "GO enrichment", "KEGG/Reactome pathways", "GSEA", "over-representation", "functional annotation", or "what pathways are my genes in".
license: MIT
compatibility: Requires Python 3.12+ with gseapy 1.3.1, numpy, pandas and matplotlib. Optional gprofiler-official, mygene and lxml for online mapping/catalogs. Online queries need network access; local GMT workflows run offline.
metadata: 
  version: 1.3
  last-reviewed: 2026-10-01
  skill-author: K-Dense Inc.
---

# Pathway Enrichment

## Overview

Enrichment analysis answers "what biology is over-represented in my genes?" It is the standard last step after differential expression, a screen, or clustering. There are two core methods, and choosing correctly is the single most important decision:

- **ORA (over-representation analysis)** — take a *thresholded* gene list (e.g., padj < 0.05) and test which gene sets it overlaps more than chance, using Fisher's exact / hypergeometric tests. Tools: Enrichr, g:Profiler.
- **GSEA (gene set enrichment analysis)** — take the *whole ranked list* of genes (no threshold) and test whether each gene set is concentrated toward the top or bottom. Preranked GSEA uses a per-gene score (e.g., the DESeq2 `stat`). Better when effects are broad and subtle.

This skill orchestrates these analyses, the gene-set databases behind them, and the interpretation pitfalls that make results wrong or unpublishable.

## When to Use This Skill

Use this skill when the user wants to:
- Find enriched GO terms / KEGG / Reactome / WikiPathways / MSigDB Hallmark sets in a gene list.
- Run GSEA / preranked GSEA on DESeq2, edgeR, limma, or Scanpy `rank_genes_groups` output.
- Score gene-set expression per sample/cell (ssGSEA, GSVA).
- Interpret, deduplicate, and visualize enrichment results, or build a publication table/figure.
- Decide between ORA and GSEA, pick gene-set libraries, choose a background, or fix gene-ID problems.

For quick one-off Enrichr lookups the `gget` skill (`gget enrichr`) is lighter weight; for raw pathway/interaction APIs (Reactome, KEGG, STRING) see the `database-lookup` skill. Use **this** skill for full, defensible enrichment workflows.

## Choosing the Right Method

| Situation | Method | Tool / entry point |
|-----------|--------|--------------------|
| You have a discrete hit list (DE genes, screen hits, cluster markers) | **ORA** | `gp.enrichr(...)` or g:Profiler |
| You have a full ranked list (every tested gene + a score) | **Preranked GSEA** | `gp.prerank(...)` |
| You have an expression matrix + class labels | **GSEA** | `gp.gsea(...)` |
| You want a gene-set score per sample/cell | **ssGSEA / GSVA** | `gp.ssgsea(...)`, `gp.gsva(...)` |
| You need a custom background or additional organisms | **ORA with custom domain** | g:Profiler (`domain_scope='custom'`) |
| You want TF / signaling *activity* (PROGENy, DoRothEA) | activity inference | see `references/databases-and-gene-sets.md` (decoupler) |

When in doubt: a thresholded list → ORA; a ranked table with scores → GSEA. Never threshold a list and then feed it to GSEA — that discards the ranking GSEA depends on.

## Setup

```bash
uv pip install gseapy==1.3.1 gprofiler-official==1.0.0 mygene==3.2.2 lxml
# gseapy pulls pandas, numpy, scipy, matplotlib. Network access is needed for
# Enrichr, g:Profiler, and MSigDB downloads. For fully offline ORA, use a local
# GMT file with gp.enrich() (see references/gseapy.md).
```

The examples target GSEApy 1.3.1 (released stable); the helper runs classic
permutation GSEA. Current MSigDB is 2026.1.Hs/2026.1.Mm. Local synthetic workflows
and small public mapping/catalog queries were executed; Enrichr submission
routes were verified from released source with mocked transport. BioMart returned
service-unavailable HTML during review; validate its output schema. See
[verified API contracts](references/verified-api.md) for evidence and boundaries.

Verify and list available gene-set libraries (names change over time — never hardcode blindly):

```python
import gseapy as gp
names = gp.get_library_name(organism="human")   # discover current Enrichr libraries
print([n for n in names if "Reactome" in n or "KEGG" in n or "Hallmark" in n])
```

## Quick Start

### ORA on a hit list (gseapy + Enrichr)

```python
import gseapy as gp

# Match the library organism and identifier namespace; map aliases explicitly.
# Keep original spelling: capitalization is not gene-ID or orthology mapping.
genes = [g.strip() for g in open("deg_symbols.txt") if g.strip()]
tested = [g.strip() for g in open("tested_symbols.txt") if g.strip()]
assert set(genes) <= set(tested)

enr = gp.enrichr(
    gene_list=genes,
    gene_sets=["MSigDB_Hallmark_2020", "GO_Biological_Process_2026",
               "KEGG_2026", "Reactome_Pathways_2024"],
    organism="human", background=tested,
    outdir=None,            # in-memory; set a path to also write tables/plots
)
res = enr.results
sig = res[res["Adjusted P-value"] < 0.05].sort_values("Adjusted P-value")
print(sig[["Gene_set", "Term", "Adjusted P-value", "Combined Score", "Genes"]].head(20))
# Speedrichr results omit Overlap; retain its actual response schema.
```

### Preranked GSEA from DESeq2 results

```python
import gseapy as gp
import pandas as pd
import numpy as np

res = pd.read_csv("deseq2_results.csv", index_col=0)   # index = gene symbols
# Rank by the test statistic (sign = direction, magnitude = evidence). This is
# more stable than ranking by log2FoldChange, which is noisy for low-co