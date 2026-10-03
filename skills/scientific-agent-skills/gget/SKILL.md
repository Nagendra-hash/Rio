---
name: gget
description: Queries 20+ bioinformatics resources through CLI/Python. Supports quick lookups of gene info, BLAST/BLAT, viral sequence downloads, PDB/mmCIF structures, G2P residue annotations, enrichment analysis, OpenTargets, COSMIC, CELLxGENE, and 8cube mouse specificity/expression data. Best for interactive exploration and simple queries. For batch processing or advanced BLAST use biopython; for multi-database Python workflows use bioservices.
license: BSD-2-Clause license
allowed-tools: Read Write Edit Bash
compatibility: Requires Python >=3.12, gget 0.30.8, and network access for remote queries. Use a separate Python 3.12/3.13 environment for optional Census dependencies. Local MUSCLE/DIAMOND need compatible binaries and OpenMP libraries; COSMIC downloads require an account.
metadata: 
  version: 1.7
  last-reviewed: 2026-09-30
  upstream-version: 0.30.8
  skill-author: K-Dense Inc.
---

# gget

## Overview

gget is a command-line bioinformatics tool and Python package providing unified access to 20+ genomic databases and analysis methods. Query gene information, sequence analysis, protein structures, viral sequences, expression data, disease associations, and mouse tissue/cell specificity metrics through a consistent interface. Most gget modules work both as command-line tools and as Python functions.

**Important**: The databases queried by gget are continuously updated, which sometimes changes their structure. Guidance here targets gget 0.30.8 (reviewed 2026-09-30). For reproducible work, pin `gget==0.30.8`; for broken upstream database adapters, update gget after checking release notes.

## Installation

Install gget in a clean virtual environment to avoid conflicts:

```bash
# Reproducible install targeting this skill
uv venv --python 3.13 .venv
source .venv/bin/activate
uv pip install "gget==0.30.8"
```

```python
import gget
```

## Quick Start

Inspect module-specific syntax before querying:

```bash
gget info --help
gget pdb --help
```

Most modules return:
- **Command-line**: JSON (default) or CSV with `-csv` flag
- **Python**: DataFrame or dictionary

Common flags across modules:
- `-o/--out`: Save results to file
- `-q/--quiet`: Suppress progress information
- `-csv`: Return CSV format (command-line only)

Python argument names generally match long CLI options without leading dashes. For example, `--census_version` becomes `census_version=...`. Use `gget <module> --help` for CLI syntax and `inspect.signature(gget.<module>)` for Python; CLI flags such as `--download` may have no Python equivalent.

## Module Categories

gget exposes 24 modules in six categories. Parameters, CLI and Python examples, and
return shapes for every one are in
[references/module_catalog.md](references/module_catalog.md); fuller per-parameter
documentation is in [references/module_reference.md](references/module_reference.md).

| Category | Modules |
| --- | --- |
| 1. Reference & gene information | `ref` (Ensembl reference downloads), `search` (gene search), `info` (gene/transcript detail), `seq` (nucleotide and protein sequences) |
| 2. Sequence analysis & alignment | `blast`, `blat`, `muscle` (multiple alignment), `diamond` (local alignment) |
| 3. Structural & protein analysis | `pdb` (PDB/mmCIF structures and metadata), `g2p` (residue annotations and isoform maps), `elm` (linear motifs), `alphafold` (deprecated prediction wrapper) |
| 4. Expression & disease data | `archs4` (correlation, tissue expression), `cellxgene` (single-cell), `enrichr` (enrichment), `bgee` (orthology and expression), `opentargets` (disease and drug), `cbio` (cancer genomics), `cosmic` (mutations) |
| 5. Viral & mouse specificity | `virus` (viral sequences), `8cube` (mouse specificity and expression) |
| 6. Additional tools | `mutate` (mutated sequences), `gpt` (deprecated text generation), `setup` (install module dependencies) |

Several modules need a one-time `gget setup` before first use (`elm`, `cellxgene`, `cbio`; legacy `alphafold`/`gpt`), and `cosmic` prompts for COSMIC credentials to download its database.

## Common Workflows

Worked multi-module pipelines — gene characterization, structural comparison, expression
and enrichment analysis, disease and drug association, orthology comparison, and
reference-file preparation for kallisto or alignment — are in
[references/common_workflows.md](references/common_workflows.md), with longer versions in
[references/workflows.md](references/workflows.md).

## Best Practices

### Data Retrieval
- Use `--limit` where supported; it is often a local cap, not a complete pagination mechanism
- Save results with `-o/--out` for reproducibility
- Check database versions/releases for consistency across analyses
- Use `--quiet` in production scripts to reduce output

### Sequence Analysis
- For BLAST/BLAT, start with default parameters, then adjust sensitivity
- Use `gget diamond` with `--threads` for faster local alignment
- Save DIAMOND databases with `--diamond_db`; gget still requires the reference input
- For multiple sequence alignment, use `-s5/--super5` for large datasets

### Expression and Disease Data
- Gene symbols are case-sensitive in cellxgene (e.g., 'PAX7' vs 'Pax7')
- Install optional dependencies for cellxgene, elm, and cbio with `gget setup <module>`
- For enrichment, record the full library name and release, not only a shortcut:
  [the adapter](https://github.com/scverse/gget/blob/main/gget/gget_enrichr.py)
  maps shortcuts to specific dated libraries, and non-human/mouse species need
  full species-specific names. For human/mouse, supply the tested-gene universe
  through `background_list` when appropriate; custom backgrounds are not
  supported for the other species. Report mapped/unmapped query and background
  counts and adjusted p-values, so identifier loss and selection bias are visible.
- Cache cBioPortal data with `-dd` to avoid repeated downloads
- Open Targets filters are local and applied after `limit`; expression fetches only the first page (at most 3000 rows). Other paginated resources use the server default page. Do not treat results as exhaustive.
- `gget.info` returns IDs in its index; preserve that index when saving CSV. ARCHS4 tissue labels use `id`, correlations use `pearson_correlation`; Enrichr uses `path_name` and `adj_p_val`.
- `gget.search` matches descriptions and synonyms; select an exact `gene_name` and reject am