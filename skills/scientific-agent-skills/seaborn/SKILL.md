---
name: seaborn
description: Creates Seaborn statistical visualizations with pandas integration for distributions, relationships, categorical comparisons, regression displays, pair plots, and heatmaps. Supports function and objects interfaces with explicit aggregation, uncertainty, and missing-data handling. Best suited to static exploratory plots; plotly covers interactive figures and scientific-visualization covers publication styling.
license: BSD-3-Clause license
allowed-tools: Read Write Edit Bash
compatibility: Requires Python 3.8+ with seaborn 0.13.2, NumPy, pandas, and Matplotlib; the tested current dependency stack requires Python 3.12+. Optional scipy/statsmodels for advanced regression or clustering, ipywidgets for notebook controls. Network only for installation or uncached example datasets.
metadata: 
  version: 1.5
  last-reviewed: 2026-10-01
  upstream-version: 0.13.2
  skill-author: K-Dense Inc.
---

# Seaborn Statistical Visualization

## Overview

Seaborn is a Python visualization library for creating publication-quality statistical graphics. Use this skill for dataset-oriented plotting, multivariate analysis, automatic statistical estimation, and complex multi-panel figures with minimal code.

## Environment and Installation

Reviewed 2026-10-01 against the current stable [Seaborn 0.13.2 documentation](https://seaborn.pydata.org/api.html) and released source. Native synthetic checks used Python 3.13, Seaborn 0.13.2, Matplotlib 3.11.2, pandas 3.0.6, NumPy 2.5.3, SciPy 1.18.1, and statsmodels 0.15.0. Official docs support Python 3.8+ with mandatory NumPy, pandas, and matplotlib dependencies; scipy, statsmodels, and fastcluster are optional for some advanced statistics and clustering workflows. The tested stack emits upstream pandas Copy-on-Write and Matplotlib deprecation warnings; successful current plots do not guarantee compatibility with future pandas 4 or Matplotlib 3.13.

```bash
# Reproducible install for examples in this skill
uv pip install "seaborn==0.13.2"

# Include optional statistical dependencies when needed
uv pip install "seaborn[stats]==0.13.2"
```

Recommended imports:

```python
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import seaborn.objects as so
```

`sns.load_dataset()` downloads public CSV example data from the moving `mwaskom/seaborn-data` repository when it is not cached; it returns a DataFrame and applies some dataset-specific preprocessing. No credentials are required. Cache presence does not establish dataset version: record the source revision or file hash for reproducibility. For private, regulated, or offline work, load local files explicitly with pandas and pass the resulting DataFrame to seaborn.

## Design Philosophy

Seaborn follows these core principles:

1. **Dataset-oriented**: Work directly with DataFrames and named variables rather than abstract coordinates
2. **Semantic mapping**: Automatically translate data values into visual properties (colors, sizes, styles)
3. **Statistical awareness**: Built-in aggregation, error estimation, and confidence intervals
4. **Aesthetic defaults**: Publication-ready themes and color palettes out of the box
5. **Matplotlib integration**: Matplotlib axes and artists support further customization

## Quick Start

```python
import seaborn as sns
import matplotlib.pyplot as plt
import pandas as pd

# Load example dataset
df = sns.load_dataset('tips')

# Create a simple visualization
sns.scatterplot(data=df, x='total_bill', y='tip', hue='day')
plt.show()
```

## Core Plotting Interfaces

### Function Interface (Traditional)

The function interface provides specialized plotting functions organized by visualization type. Each category has **axes-level** functions (plot to single axes) and **figure-level** functions (manage entire figure with faceting).

**When to use:**
- Quick exploratory analysis
- Single-purpose visualizations
- When you need a specific plot type

### Objects Interface (Modern)

The `seaborn.objects` interface provides a declarative, composable API similar to ggplot2. Build visualizations by chaining methods to specify data mappings, marks, transformations, and scales. Upstream still describes this interface as experimental and incomplete in 0.13.2, although stable enough for serious use; prefer the function interface for conservative production code unless the compositional API materially simplifies the plot.

**When to use:**
- Complex layered visualizations
- When you need fine-grained control over transformations
- Building custom plot types
- Programmatic plot generation

```python
from seaborn import objects as so

# Declarative syntax
(
    so.Plot(data=df, x='total_bill', y='tip')
    .add(so.Dot(), color='day')
    .add(so.Line(), so.PolyFit(order=1))
)
```

## Current API Notes

Seaborn 0.12 and 0.13 changed several common plotting patterns:

- Most plotting functions now require keyword arguments for variables. Prefer `sns.scatterplot(data=df, x="x", y="y")` over positional `sns.scatterplot(df["x"], df["y"])`.
- `errorbar` replaces the old `ci` parameter in `lineplot()`, `barplot()`, and `pointplot()`. Regression functions such as `regplot()` and `lmplot()` still use `ci`.
- Categorical plots were rewritten in 0.13. Use `native_scale=True` when numeric or datetime categories should keep their original scale instead of ordinal positions.
- Passing `palette` without assigning `hue` is deprecated for categorical functions. If each category should get its own color, assign a redundant hue such as `hue="day"` and set `legend=False`.
- Prefer renamed parameters: `violinplot(density_norm=..., common_norm=...)` instead of `scale`/`scale_hue`, `boxenplot(width_method=...)` instead of `scale`, and `barplot(err_kws=...)` instead of `errcolor`/`errwidth`.

## Data Structure Requirements

### Long-Form Data (Preferred)

Each variable is a column, each observation is a row. Retain subject/sample IDs when reshaping; rows from the same subject are not independent replicates. This "tidy" format provides maximum flexibility:

```python
# Long-form structure
   subject  condition  measurement
0        1    control         10.5
1        1  treatment         12.3
2        2    control          9.8
3        2  treatment         13.1
```

**Advantages:**
- Works with all seaborn functions
- Easy to remap variables to visual properties
