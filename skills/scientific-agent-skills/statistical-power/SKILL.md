---
name: statistical-power
description: Calculates sample sizes and statistical power for study planning. Applies when someone asks "how many subjects/samples/replicates do I need", wants an a priori power analysis, a minimum detectable effect (MDE), a power curve, or needs to justify a sample size for a grant, IRB protocol, or pre-registration. Covers closed-form power for t-tests, ANOVA, proportions, correlations, chi-square, and regression, plus simulation-based (Monte Carlo) power for complex designs — logistic/Poisson regression, mixed models, cluster-randomized trials, survival, and interactions. Also handles requests that only mention an effect size, alpha, or "80% power" without saying "power analysis" explicitly. For laying out the study (randomization, blocking, factorial/DOE, crossover, sequential designs) use experimental-design; for analyzing data already collected and reporting it use statistical-analysis.
allowed-tools: Read Write Edit Bash
compatibility: Requires Python >=3.12 with statsmodels, scipy, numpy, pandas, and matplotlib. Optional comparison uses pingouin; survival extensions use lifelines (requires pandas<3). Installation needs network access unless packages are cached. Calculations run locally without credentials.
license: MIT license
metadata: 
  version: 1.3
  last-reviewed: 2026-10-01
  skill-author: K-Dense Inc.
---

# Statistical Power & Sample Size

## Overview

Power analysis plans the probability of rejecting a specified null under an assumed alternative. It answers **how many independent experimental units are needed to detect a scientifically important effect**, or what effects a feasible sample could detect. Choose the inferential goal first: precision, equivalence, noninferiority, or sequential monitoring need their own calculations; the bundled superiority-test helpers do not cover them.

Four quantities are locked together for any given test: **sample size (n)**, **effect size**, **significance level (α)**, and **power (1 − β)**. For a fixed design, analysis, and nuisance parameters, fixing three permits solving for the fourth when a solution exists. Every calculation in this skill is some rearrangement of that relationship.

This skill covers the two ways to do power analysis:
- **Analytical** power calculations (exact under some standard-test assumptions; asymptotic approximations for others) — see `references/closed_form_recipes.md`.
- **Simulation / Monte Carlo** (requires a credible data-generating process and a calibrated planned analysis) — see `references/simulation_based_power.md`.

For choosing and converting effect sizes — usually the hardest part — see `references/effect_sizes.md`.

## When to Use This Skill

- Determining required sample size before collecting data (a priori power analysis)
- Finding the minimum detectable effect (MDE) for a fixed, already-determined sample size
- Producing power curves (power vs. n, or power vs. effect size) for a grant or protocol
- Justifying a sample size for an IRB submission, grant, or pre-registration
- Powering designs with unequal group sizes or non-1:1 allocation
- Planning complex mixed models, GLMs, clustering, survival, mediation, and interactions through simulation when simpler approximations do not fit
- Accounting for multiple comparisons, attrition/dropout, or clustering in the sample-size estimate

## Installation

The local examples were checked on Python 3.13 with statsmodels 0.15.0, SciPy
1.18.1, NumPy 2.5.3, pandas 2.3.3, and Matplotlib 3.11.2. Use an environment
separate from the repository's development environment:

```bash
uv venv --python 3.13 .venv-power
uv pip install --python .venv-power/bin/python "statsmodels==0.15.0" "scipy==1.18.1" "numpy==2.5.3" "pandas==2.3.3" "matplotlib==3.11.2"
# Optional comparison / survival methods (also checked for current API use):
uv pip install --python .venv-power/bin/python "pingouin==0.7.0" "lifelines==0.30.3"
```

On Windows use `.venv-power/Scripts/python.exe`. Lifelines 0.30.3 requires
`pandas<3`; the tested pin above accommodates it. Mixed models and GLMs are
included in statsmodels. Record versions and seeds with the output; numerical
smoke tests do not establish a study's effect assumptions or Type I error control.

---

## The one decision that drives everything: the effect size

Power calculations are only as trustworthy as the effect size you feed them. **Do not invent a number.** Use, in rough order of preference:

1. A **minimally important effect** — the smallest effect that would actually change a decision or matter scientifically/clinically (the "smallest effect size of interest", SESOI). This is the most defensible basis: you power to detect what matters, not what you hope to see.
2. A **pilot or prior-study estimate**, with uncertainty and selection bias considered. Small pilots are imprecise; publication or significance-based selection can inflate effects. Use a justified uncertainty model or sensitivity range instead of an arbitrary shrinkage factor.
3. A **convention** (Cohen's small/medium/large) only as a last resort, and say so explicitly.

Whatever you pick, run a **sensitivity analysis**: report how required n changes across a plausible range of effect sizes, not a single point. A power analysis presented as one number hides its biggest source of uncertainty. See `references/effect_sizes.md` for benchmarks and conversions between d, f, r, η², odds ratios, and Cohen's h/w.

> **Avoid post-hoc ("observed") power.** Computing power from the effect size you just estimated is circular: for standard tests it largely restates the test statistic/p-value and adds no independent evidence of adequacy. If a study is already done and you want to know what it could have detected, report a **sensitivity analysis** (MDE at the achieved n) or, better, the confidence interval around the observed effect. This is a common reviewer complaint — do not produce observed power even if asked without flagging the issue.

---

## Quick recipes (closed-form)

The bundled `scripts/power.py` wraps statsmodels and SciPy into one consistent interface so you don't have to remember which solver belongs to which test. Run from `skills/statistical-power/scripts/` or add that directory to `sys.path`.

```python
from power import sample_size, power, mde, power_curve

# 1. How many per group to detect Cohen's d = 0.5, two-sided, 80% power?
sa