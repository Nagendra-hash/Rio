---
name: relsa-severity-assessment
description: Supports multivariate severity assessment and exploratory endpoint-time score forecasting for laboratory animal studies using the RELSA (RELative Severity Assessment) score and ARIMA-based foRcast forecasting. Use when combining welfare readouts — body weight or weight loss, body temperature, clinical or nesting scores, biomarkers, activity, heart rate, burrowing, wheel running — into one severity score per animal per day, when asking which animals are at risk of reaching a humane endpoint at a specified future observation time, when defining attention/danger zones or thresholds on a severity scale by kernel density estimation, or when reporting severity for a 3Rs, refinement, animal-welfare, or EU Directive 2010/63/EU severity-assessment context. Covers directionality ("turned" variables), baseline normalization, reference sets, RELSA weights, ARIMA prediction intervals, and RMSE/PICP/MPIW evaluation.
license: MIT
allowed-tools: Read Write Edit Bash
compatibility: Requires Python >=3.12 with numpy, pandas, scipy, statsmodels and matplotlib. Local analysis needs no network or credentials; installation and upstream review need network access.
metadata: 
  version: 1.3
  last-reviewed: 2026-10-01
  skill-author: K-Dense Inc.
---

# RELSA severity assessment and exploratory score forecasting

## Overview

This skill computes reference-relative multivariate scores and exploratory ARIMA forecasts.
It targets the equations and printed example of [RELSA 0.0.1.9000](https://github.com/mytalbot/RELSA)
(commit `e68e8451e8719ccc3600179f55900cd7254ede9d`, current upstream at review), plus the
published [foRcast methodology](https://doi.org/10.3389/fphys.2026.1869563).
The Python forecast helper is an independent nonseasonal approximation, not an exact R port.

RELSA is the RMS of directional deviations divided by each variable's reference maximum
**deviation**. Zero means no measured worsening in those directions; it does not establish
normal welfare. One is a reference scale unit, not a universal endpoint or an upper bound.
Different variables may attain their reference extrema in different animals or at different
times, so the reference cohort need not contain a score of exactly one.

Forecasts estimate a **score at a specified time**, not time-to-endpoint or probability of death.
Keep the approved study's observation schedule and humane endpoint criteria separate from
these exploratory outputs. See [Boundaries](#boundaries-state-these-when-you-report).

## When to use this skill

- Combining weight loss, temperature, clinical scoring, biomarkers, or telemetry into a single
  per-animal severity score
- Asking which animals in a cohort are at risk of reaching a humane endpoint, or predicting
  the severity score at a coming time point
- Comparing severity between treatment groups, interventions, or animal models on a common
  relative scale
- Defining thresholds or zones on a severity scale from the data
- Writing the severity-assessment section of an animal welfare report, a 3Rs/refinement
  analysis, or an application under EU Directive 2010/63/EU

For general forecasting of a time series that is not a severity score, use
**timesfm-forecasting** or **statsmodels**. For study design and sample size, use
**experimental-design** and **statistical-power**.

## Installation

```bash
uv venv --python 3.13 .venv-relsa
uv pip install --python .venv-relsa/bin/python numpy==2.5.3 pandas==3.0.6 scipy==1.18.1 statsmodels==0.15.0 matplotlib==3.11.2
source .venv-relsa/bin/activate
```

Run the examples from this skill directory. `relsa_score.py` needs numpy/pandas;
`kde_thresholds.py` additionally needs scipy; statsmodels is required
for forecasting and matplotlib only for figures.

## Data format

One row per animal per time point, in a CSV:

| id | treatment | condition | day | temp | weight | score | il6 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| M01 | treated | endpoint | -1 | 37.15 | 25.17 | 0 | 35.1 |
| M01 | treated | endpoint | 0 | 37.26 | 25.25 | 0 | 39.5 |
| M01 | treated | endpoint | 1 | 35.83 | 23.12 | 4 | 162.0 |

- `id` and a time column (`day`, `time`, `hour`, …) are required; `treatment` and `condition`
  are optional labels used for grouping and for selecting the reference set.
- Time must be finite, with one consistent unit and origin across cohorts. Forecast training and
  target times must align with the chosen regular grid; disable interpolation only on regular observations. The RELSA
  convention codes the baseline time point as `-1`.
- **One row per animal per time point.** Average hourly telemetry to one value per interval
  first (the published models average heart rate, HRV, and temperature, and sum activity).
- Preserve lexical animal IDs (including leading zeros). Duplicate IDs/times, nonnumeric
  measurements and infinities are rejected. Leave missing measurements empty. They are dropped from the score, never imputed — a
  missing value treated as "no deviation" biases severity downward.

`assets/example_cohort.csv` is a small synthetic cohort (6 mice, 9 days, temperature, body
weight, an 0–8 clinical score, and an IL-6-like biomarker) used by every command below, so
each one is runnable as written.

## The four decisions that determine the result

Make these explicitly and write them into the methods. Nothing else about the procedure
matters as much.

**1. Directionality — which variables rise under worsening?** Falling is the default (body
weight, activity, food intake, burrowing, wheel running). Variables that *rise* must be
declared as `--turned`: clinical scores, inflammatory biomarkers, fever, tachycardia. Get
this wrong and the variable contributes nothing at all, silently, because deviations in the
"wrong" direction are floored at zero. Body temperature is model-dependent — it *falls* in
sepsis and endotoxaemia, *rises* in fever models. Nothing in the data can settle this for you:
in the published sepsis model activity legitimately swings further above baseline than below,
so only a variable that *never once* moves the declared way is detectable, and
`build_reference()` warns about exactly that case.

**2. The reference set — relative to what?** RELSA scores mean nothing without it. Choose a scientifically characterized reference and document its burden; the 2026
forecasting study uses the group assumed to