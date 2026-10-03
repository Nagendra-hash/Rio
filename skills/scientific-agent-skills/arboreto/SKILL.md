---
name: arboreto
description: Infers candidate gene regulatory networks from bulk or single-cell expression data using AertsLab Arboreto GRNBoost2 and GENIE3. Use for transcription factor-target association ranking, compatible Dask execution, sparse expression inputs, and network stability checks.
license: BSD-3-Clause license
compatibility: Requires the isolated Python 3.11 compatibility stack below, including Arboreto, Dask/distributed, NumPy, pandas, scikit-learn and SciPy. Network access is needed for installation, not local inference. No credentials required.
metadata: 
  version: 1.3
  skill-author: K-Dense Inc.
  last-reviewed: 2026-09-30
---

# Arboreto

## When to use

Use Arboreto to rank candidate regulator-target associations from expression
measurements. GRNBoost2 fits stochastic gradient boosting regressions; GENIE3
fits random forests. Each target is predicted from candidate regulators, excluding
itself. These are observational predictive associations, not proof of direct
binding, activation/repression, or causal regulation.

The latest PyPI release checked is **0.1.6** (2021-02-09). Read the Docs still
labels its documentation 0.1.5; the current GitHub source contains fixes that are
**not in the PyPI wheel**. Do not assume a successful unpinned installation can
run inference. See [compatibility and distribution details](references/distributed_computing.md).

## Installation and compatibility

The following exact stack passed dense and CSC-sparse GRNBoost2, dense GENIE3,
custom GBM/RF, and wrapper smoke tests on macOS arm64 with Python 3.11.11:

```bash
uv venv --python 3.11 .venv-arboreto
uv pip install --python .venv-arboreto/bin/python \
  'arboreto==0.1.6' 'dask[complete]==2024.7.1' 'distributed==2024.7.1' \
  'numpy==1.26.4' 'pandas==2.2.3' 'scikit-learn==1.5.2' 'scipy==1.13.1'
```

This is a bounded compatibility recipe, not a claim that current releases of all
dependencies work. PyPI Arboreto builds an empty metadata graph that the newer
Dask dataframe implementation rejects. For this pinned Dask version, select its
legacy dataframe backend **before importing Arboreto or `dask.dataframe`**:

```python
import dask
dask.config.set({"dataframe.query-planning": False})
from arboreto.algo import grnboost2, genie3
```

The bundled wrapper does this for Dask 2024.7.1. Restart an existing notebook
kernel if it has already imported the newer dataframe backend. Sparse targets
also use `.A` inside Arboreto 0.1.6; this attribute was removed in SciPy 1.14.
Keep the tested SciPy pin for sparse inference. No monkeypatch to site-packages
is required by this recipe.

## Workflow

1. Select biologically comparable cells/samples; document normalization, filtering,
   batch handling, organism, identifier namespace, and expression layer.
2. Prepare **rows = observations, columns = genes**. Exclude sample IDs from
   expression values. Require unique gene names, numeric finite values, and a TF
   list with a nonempty overlap. All-zero/constant genes provide no useful targets.
3. Choose GRNBoost2 for an efficient starting analysis, GENIE3 for method comparison,
   or `diy` for explicit regressor settings. See [algorithms](references/algorithms.md).
4. Run a small subset first in the pinned environment, then scale worker counts to
   available memory. Keep the `if __name__ == "__main__":` guard in process-based scripts.
5. Inspect worker warnings and target coverage, save the full ranked network, and
   assess stability across seeds and resampled observations before prioritizing edges.

## Run the bundled wrapper

From this skill directory, with a TSV containing gene headers and numeric rows:

```bash
.venv-arboreto/bin/python scripts/basic_grn_inference.py expression_data.tsv network.tsv \
  --tf-file tfs.txt --seed 777 --workers 2 --limit 5000
```

Add `--index-col 0` only if the first column contains cell/sample identifiers.
The wrapper rejects duplicate headers before pandas can rename them, nonnumeric
or nonfinite values, empty TF overlap, invalid limits, and wholly empty results.
It reports TF overlap and uses a fresh bounded Dask client that closes on error.
The default is one worker; increase it after a successful pilot. Without a TF
file, **all genes** are candidate regulators, even though the output column is
named `TF`.

Output is a headerless TSV in `TF`, `target`, `importance` order. For downstream
consumers that require column headers (including pySCENIC adjacency loading),
write a separate copy with `header=True` rather than assuming every tool accepts
the headerless upstream example format.

## Minimal Python example

This synthetic example checks execution and output structure; it is not a
biological benchmark. The same calls were tested with a 32-observation,
four-gene fixture.

```python
import dask
dask.config.set({"dataframe.query-planning": False})
import numpy as np
import pandas as pd
from arboreto.algo import grnboost2
from distributed import Client, LocalCluster

if __name__ == "__main__":
    rng = np.random.default_rng(123)
    values = rng.normal(size=(32, 4))
    values[:, 2] = 3 * values[:, 0] + rng.normal(scale=0.1, size=32)
    matrix = pd.DataFrame(values, columns=["TF1", "TF2", "G1", "G2"])
    with LocalCluster(n_workers=1, threads_per_worker=1,
                      dashboard_address=None) as cluster, Client(cluster) as client:
        network = grnboost2(expression_data=matrix, tf_names=["TF1", "TF2"],
                            seed=777, client_or_address=client)
    assert not network.empty
    assert not (network["TF"] == network["target"]).any()
    network.to_csv("network.tsv", sep="\t", index=False, header=False)
```

For real DataFrame, ndarray, CSC, and AnnData input conventions, read
[basic inference](references/basic_inference.md).

## Interpret and validate output

| Column | Meaning |
| --- | --- |
| `TF` | Candidate predictor gene, restricted only if a TF list was supplied |
| `target` | Gene whose expression was predicted |
| `importance` | Nonnegative feature importance used to rank candidate links |

Results are sorted by decreasing importance; zero-importance links are omitted.
GRNBoost2 rescales feature importance by the fitted number of trees, so its
scores can exceed 1 and are not on the same scale as GENIE3. There is no universal
`importance > 0.5` confidence cutoff. `limit=N` keeps the top N links globally;
it does not 