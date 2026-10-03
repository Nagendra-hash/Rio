---
name: scvelo
description: Performs RNA velocity analysis with scVelo from spliced and unspliced single-cell RNA counts. Fits deterministic or dynamical models, examines gene phase portraits, builds velocity graphs, estimates relative latent time, and ranks velocity-associated genes. Use for directional trajectory hypotheses and kinetic-model diagnostics alongside Scanpy; velocity alone does not establish cell fate or causal drivers.
license: BSD-3-Clause
compatibility: Requires Python 3.13 for the tested stack with scvelo 0.3.4, scanpy 1.12.4, anndata 0.13.4, numpy 2.5.3 and pandas 2.3.3. Loom import requires loompy. Local H5AD analysis needs no network or credentials. The default stochastic solver is incompatible with NumPy 2.
metadata: 
  version: 2.0
  skill-author: Kuan-lin Huang
  last-reviewed: 2026-10-01
---

# scVelo RNA Velocity

## When to use

Use for kinetic analysis of aligned spliced/unspliced RNA counts, directional
trajectory hypotheses, gene-level phase portraits, and dynamical latent time.
Use the [model reference](references/velocity_models.md) for assumptions,
transition probabilities, and an optional CellRank handoff.

RNA velocity estimates an expression derivative under a model. Smooth arrows
are not observed cell movement, lineage tracing, causal drivers, or proof of
future fate. The dynamical estimator is not automatically more accurate than
a steady-state estimator on every dataset.

## Tested environment and compatibility

The maintained workflow targets scVelo **0.3.4**, Scanpy **1.12.4**, AnnData
**0.13.4**, NumPy **2.5.3**, pandas **2.3.3**, SciPy **1.18.1**, Matplotlib
**3.11.2**, and loompy **3.0.8** on Python 3.13. Use an isolated environment:

```bash
uv venv --python 3.13 .venv-velocity
uv pip install --python .venv-velocity/bin/python \
  scvelo==0.3.4 scanpy==1.12.4 anndata==0.13.4 numpy==2.5.3 \
  pandas==2.3.3 scipy==1.18.1 matplotlib==3.11.2 loompy==3.0.8
```

The packages above were resolved from cache and tested in an isolated `uv`
environment; shell paths above use POSIX syntax. Installation requires network
access unless packages are cached. No API key is required.

- **Deterministic and dynamical models:** native synthetic fitting, graphs,
  relative times, plots and H5AD round trips are tested on this stack.
- **Stochastic model:** scVelo 0.3.4's default GLS scalar assignment fails with
  NumPy 2. Scanpy 1.12 requires NumPy 2, so installing `numpy<2` beside current
  Scanpy is not a solution. A separately validated legacy Scanpy/NumPy environment
  is required; that legacy stack was not executed in this review. The helper
  rejects this combination before modifying data, without changing the model.
- **pandas 3:** scVelo 0.3.4 dynamical fitting and some plots use operations no
  longer supported by pandas 3. Keep the explicit pandas pin.
- The upstream stable tutorial still contains removed `scv.read` and older
  preprocessing calls. Check the [0.3.4 source](https://github.com/theislab/scvelo/tree/v0.3.4)
  when a tutorial disagrees with the installed API.

## Input contract

1. Obtain spliced and unspliced counts from a velocity-aware quantifier. Keep
   quantifier/version, reference annotation, counting mode, sample IDs and cell
   barcode mapping. Upstream quantification is outside the bundled script.
2. Supply **cells by genes** matrices in `adata.layers['spliced']` and
   `adata.layers['unspliced']`, with identical cell/gene ordering and unique IDs.
   Counts must be finite and nonnegative; fractional count estimates are allowed.
   Never use logged, scaled, residualized, or batch-corrected values as counts.
   Numerical inspection alone cannot establish that data are raw.
3. Inspect per-library/cluster coverage, doublets, ambient RNA, zero-count cells,
   and annotation compatibility before fitting. Preserve the original full-gene
   count file. Retained-gene backups do not preserve filtered-out genes.
4. Resolve barcode prefixes and gene identifiers explicitly when combining
   files. Do not silently strip library IDs, intersect away most cells, or make
   duplicated biological IDs unique without understanding why they repeat.

Read H5AD through AnnData. scVelo 0.3.4 has no `scv.read` or `scv.DataFrame`:

```python
import anndata as ad
adata = ad.read_h5ad("velocity_counts.h5ad")
```

For legacy loom input use `ad.io.read_loom('counts.loom', X_name='spliced',
sparse=True)`. AnnData 0.13 deprecates loom; convert to H5AD for further work.
Its `layers[None]` aliases `X`: do not delete it or iterate all layers as if every
key were a string. The helper accesses only named velocity layers. Native loom
reading and exact, reordered metadata alignment are covered by the tests;
AnnData 0.13 `write_loom` is not used.

## Run the maintained workflow

The [bundled script](scripts/rna_velocity_workflow.py) accepts local files and
never downloads a demonstration dataset on startup:

```bash
MPLBACKEND=Agg .venv-velocity/bin/python scripts/rna_velocity_workflow.py \
  velocity_counts.h5ad --mode dynamical --groupby clusters \
  --n-top-genes 2000 --n-neighbors 30 --n-jobs 1 --output-dir velocity_results

# Import a loom and align its raw counts to an existing annotation file:
MPLBACKEND=Agg .venv-velocity/bin/python scripts/rna_velocity_workflow.py \
  counts.loom --processed-h5ad annotated.h5ad --groupby clusters \
  --mode deterministic --output-dir velocity_check
```

Run these from the skill directory. Input filenames and biological annotations
are illustrative; the same CLI and functions are tested on small generated
kinetic fixtures. Omit `--groupby` when annotations are unavailable. Use
`--no-plots` for analysis without computing UMAP.

The helper performs these steps:

1. Validate layers, IDs, model compatibility and grouping; refuse previously
   generated moments/velocity so preprocessing cannot silently run twice.
2. Back up retained raw layers as `spliced_counts` and `unspliced_counts` and
   rebuild `X` from raw spliced counts. Filter genes and normalize `X` and the
   two count layers on a linear scale.
3. Apply `sc.pp.log1p` **only to X**, select highly variable genes with Scanpy,
   and rebuild PCA and neighbors after subsetting. Reusing stale PCA/neighbors
   from an unrelated feature set can silently change the velocity model.
4. Compute `Ms` and `Mu` with `scv.pp.moments(adata, n_neighbors=None)` from the
   explicit Sca