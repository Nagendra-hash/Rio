---
name: anndata
description: Handles annotated matrices in single-cell analysis, .h5ad and Zarr files, and integration with the scverse ecosystem. This is the data format skill—for analysis workflows use scanpy; for probabilistic models use scvi-tools; for population-scale queries use cellxgene-census.
license: BSD-3-Clause license
allowed-tools: Read Write Edit Bash
compatibility: Requires Python 3.12+ and anndata; uv for installation. Optional dask/lazy extras for lazy I/O; openpyxl for Excel, loompy for legacy Loom, and provider-specific fsspec adapters for remote stores. Network required for installation and remote data only.
metadata: 
  version: 1.4
  last-reviewed: 2026-09-30
  upstream-version: 0.13.4
  skill-author: K-Dense Inc.
---

# AnnData

## Overview

AnnData is a Python package for handling annotated data matrices, storing experimental measurements (X) alongside observation metadata (obs), variable metadata (var), and multi-dimensional annotations (obsm, varm, obsp, varp, uns). Originally designed for single-cell genomics through Scanpy, it now serves as a general-purpose framework for any annotated data requiring efficient storage, manipulation, and analysis.

## When to Use This Skill

Use this skill when:
- Creating, reading, or writing AnnData objects
- Working with h5ad, zarr, or other genomics data formats
- Performing single-cell RNA-seq analysis
- Managing large datasets with sparse matrices or backed mode
- Concatenating multiple datasets or experimental batches
- Subsetting, filtering, or transforming annotated data
- Integrating with scanpy, scvi-tools, or other scverse ecosystem tools

## Installation

Targets AnnData 0.13.4 (current PyPI release reviewed 2026-09-30), requiring Python
3.12+. Small synthetic checks cover dense/sparse matrices, native I/O, metadata,
concatenation, and lazy reads. File paths, biological analysis, remote stores, and
third-party integration examples are illustrative unless stated otherwise.

```bash
uv pip install "anndata==0.13.4"

# Lazy I/O and dask-backed operations
uv pip install "anndata[dask,lazy]==0.13.4"
```

Use unpinned installs only when intentionally tracking the latest compatible release.

Current API notes:
- Use `anndata.io` for non-native `read_*` and `write_*` helpers. Top-level `anndata.read_h5ad` and `anndata.read_zarr` remain supported.
- Use `ad.concat`; `AnnData.concatenate()` was removed in 0.13. Avoid old `ad.read`, deprecated `AnnData.*_keys()` helpers and `anndata.__version__`; prefer explicit readers, mapping `.keys()`, and `importlib.metadata.version("anndata")`.
- In 0.13, `.X` is also `layers[None]`; layer iteration includes `None`. Use `key is not None` when selecting named layers. View `.X` writes now use copy-on-write.
- Zarr v3 and automatic sharding are the defaults; the Python dependency is Zarr >=3. Dense H5AD `X` remains writable with `backed="r+"`, but backed sparse item assignment is unsupported in 0.13.
- `AnnLoader` and Loom reading/writing are deprecated. `AnnCollection` and other experimental APIs need the caveats in the references.

These changes are documented in the [official release notes](https://anndata.readthedocs.io/en/stable/release-notes/). The live docs header still displayed 0.13.3.post0 at review; behavior below was also checked against installed 0.13.4 source.

## Quick Start

### Creating an AnnData object
```python
import anndata as ad
import numpy as np
import pandas as pd

# Minimal creation
X = np.random.rand(100, 2000)  # 100 cells × 2000 genes
adata = ad.AnnData(X)

# With metadata
obs = pd.DataFrame({
    'cell_type': ['T cell', 'B cell'] * 50,
    'sample': ['A', 'B'] * 50
}, index=[f'cell_{i}' for i in range(100)])

var = pd.DataFrame({
    'gene_name': [f'Gene_{i}' for i in range(2000)]
}, index=[f'ENSG{i:05d}' for i in range(2000)])

adata = ad.AnnData(X=X, obs=obs, var=var)
```

### Reading data
```python
# Native formats (read_h5ad/read_zarr remain at top-level)
adata = ad.read_h5ad('data.h5ad')
source = ad.read_h5ad('large_data.h5ad', backed='r')  # X backed; metadata/layers can load
try:
    subset = source[:100, :].to_memory()
finally:
    source.file.close()
adata = ad.read_zarr('data.zarr')

# Other formats: prefer anndata.io (top-level imports are deprecated)
from anndata.io import read_csv, read_loom, read_mtx

adata = read_csv('data.csv')
adata = read_loom('data.loom')

# 10X Genomics: use scanpy (not anndata) — see scanpy skill
import scanpy as sc
adata = sc.read_10x_h5('filtered_feature_bc_matrix.h5')
adata = sc.read_10x_mtx('filtered_feature_bc_matrix/')
```

### Writing data
```python
# Write h5ad file
adata.write_h5ad('output.h5ad')

# Write with compression
adata.write_h5ad('output.h5ad', compression='gzip')

# Write other formats
adata.write_zarr('output.zarr')
adata.write_csvs('output_dir/', skip_data=False)  # Lossy; may densify X
```

### Basic operations
```python
# Subset by conditions
t_cells = adata[adata.obs['cell_type'] == 'T cell']

# Subset by indices
subset = adata[0:50, 0:100]

# Add metadata
adata.obs['quality_score'] = np.random.rand(adata.n_obs)
adata.var['highly_variable'] = np.random.rand(adata.n_vars) > 0.8

# Access dimensions
print(f"{adata.n_obs} observations × {adata.n_vars} variables")
```

## Core Capabilities

### 1. Data Structure

Understand the AnnData object structure including X, obs, var, layers, obsm, varm, obsp, varp, uns, and raw components.

**See**: `references/data_structure.md` for comprehensive information on:
- Core components (X, obs, var, layers, obsm, varm, obsp, varp, uns, raw)
- Creating AnnData objects from various sources
- Accessing and manipulating data components
- Memory-efficient practices

### 2. Input/Output Operations

Read and write data in various formats with support for compression, backed mode, and cloud storage.

**See**: `references/io_operations.md` for details on:
- Native formats (h5ad, zarr)
- Alternative formats (CSV, MTX, Loom, 10X, Excel)
- Backed mode for large datasets
- Remote data access
- Format conversion
- Performance optimization

Common commands:
```python
from anndata.io import read_mtx

# Read/write h5ad
source = ad.read_h5ad('data.h5ad', backed='r')
try:
    source.write_h5ad('output.h5ad', compression='gzip')
finally:
    source.file.close()

# 10X Genomics (via scanpy)
import scanpy as sc
adata = sc.read_10x_h5('filtered_feature_bc_matrix.h5')

# Read M