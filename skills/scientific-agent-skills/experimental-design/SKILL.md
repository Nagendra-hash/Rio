---
name: experimental-design
description: Designs experiments and studies BEFORE data is collected — choosing a design, randomizing, blocking, and laying out treatment combinations so results are interpretable. Use whenever someone is planning a study, asks how to assign subjects/samples to groups, mentions randomization, blocking, stratification, controls, factorial or fractional-factorial designs, design of experiments (DOE), screening many factors, response-surface optimization, crossover or repeated-measures or split-plot designs, cluster/group randomization, Latin squares, plate layouts, batch/run-order effects, replication vs. pseudoreplication, or sequential/adaptive/group-sequential designs. Trigger even for informal phrasings like "how should I set up this experiment", "how do I avoid confounding", "what's the best way to test these 6 factors", or "assign these mice to conditions". For computing the sample size or power once the design is chosen, use statistical-power; for analyzing data already collected, use statistical-analysis.
allowed-tools: Read Write Edit Bash
compatibility: Requires Python >=3.12 with numpy, pandas, and pydoe 1.5.0 (DOE matrices). Network access is needed only to install packages; no credentials are required.
license: MIT license
metadata: 
  version: 1.5
  last-reviewed: 2026-09-30
  skill-author: K-Dense Inc.
---

# Experimental Design

## Overview

The design of a study — how units are assigned to conditions, what is held constant, what is varied, and in what structure — determines what questions the data can answer. No analysis can rescue a confounded or pseudoreplicated design after the fact. This skill is about the decisions made *before* data collection: picking a design that isolates the effect of interest, randomizing to license causal claims, blocking to remove known nuisance variation, and structuring multi-factor experiments so effects are estimable rather than tangled together.

The three ideas behind almost every good design (Fisher's principles):
- **Randomization** — assign treatments at random so that confounders, known and unknown, are balanced in expectation. This is what turns a comparison into a causal claim.
- **Replication** — independent repetition at the right level, so you can estimate variability and your effects aren't artifacts of a single unit. The most common fatal error is **pseudoreplication**: counting repeated measurements on the same unit as independent replicates.
- **Blocking / local control** — group similar units (by batch, day, site, litter) and randomize within blocks, removing that nuisance variation from the error term instead of letting it inflate noise.

This skill helps you choose among design types, generate the actual randomization or DOE layout (with reproducible scripts), and avoid the structural mistakes that make data uninterpretable.

## When to Use This Skill

- Planning any comparative experiment or trial and deciding how to assign units
- Randomizing subjects/samples to arms (simple, blocked, stratified, or cluster)
- Removing nuisance variation by blocking or stratification
- Designing multi-factor experiments: full or fractional factorial, screening designs
- Optimizing a response over continuous factors (response-surface designs)
- Within-subject / repeated-measures, crossover, split-plot, or Latin-square designs
- Cluster- or group-randomized designs (sites, clinics, classrooms, litters)
- Deciding the number and level of replicates and avoiding pseudoreplication
- Sequential, group-sequential, or adaptive designs with interim analyses
- Laying out plates/batches and randomizing run order to defeat drift

## Installation

```bash
uv venv --python 3.13 .venv-design
uv pip install --python .venv-design/bin/python "numpy==2.5.3" "pandas==3.0.6" "pydoe==1.5.0"
```

`pyDOE3` was [archived in May 2026](https://github.com/relf/pyDOE3); active
development returned to [pydoe](https://pydoe.github.io/pydoe/). The bundled
wrappers target `pydoe==1.5.0`, imported as lowercase `pydoe`, and return
designs in real factor units. All seven DOE wrappers and both script demos were
executed with Python 3.13, NumPy 2.5.3, pandas 3.0.6, and SciPy 1.18.1. On
Windows, the environment interpreter is `.venv-design/Scripts/python.exe`.
Archive the environment versions and exported schedules: a seed alone does not
promise identical output across package or script upgrades.

---

## Choosing a design

Start from the question and the structure of your units, not from a favorite design.

```
What are you trying to learn?
│
├─ Compare a few predefined conditions (A vs B vs C)?
│   ├─ Units independent, possibly with a known nuisance factor (day, batch, site)?
│   │     → Completely randomized (no nuisance) or RANDOMIZED BLOCK design.
│   ├─ Each unit can receive every condition in sequence (washout possible)?
│   │     → CROSSOVER / repeated-measures design (watch carry-over and correlation).
│   └─ You can only randomize groups, not individuals (schools, clinics)?
│         → CLUSTER-randomized design (account for clustering; see pseudoreplication).
│
├─ Screen MANY factors (5+) to find the few that matter?
│     → FRACTIONAL FACTORIAL or PLACKETT-BURMAN screening design.
│
├─ Quantify main effects AND interactions among a handful of factors?
│     → FULL 2^k FACTORIAL design.
│
├─ Find the settings that OPTIMIZE a response (curvature matters)?
│     → RESPONSE-SURFACE design: central composite or Box-Behnken.
│
└─ Explore a simulation/computer model over a continuous space?
      → SPACE-FILLING design: Latin hypercube.
```

Detailed guidance per branch:
- **Randomization, blocking, stratification, controls** → `references/randomization_and_blocking.md`
- **Factorial, fractional-factorial, screening, response-surface, DOE concepts (aliasing, resolution)** → `references/factorial_and_doe.md`
- **Crossover, repeated-measures, split-plot, Latin-square, cluster, nested designs** → `references/design_types.md`
- **Sequential, group-sequential, and adaptive designs (interim analyses)** → `references/sequential_and_adaptive.md`

---

## Generating the design

Two scripts produce ready-to-use, reproducible layouts. Run them from the skill's
`scripts/` directory or add it to `sys.path`. Seeds support reproducible layouts
within the recorded software environment.
The default seed is for demonstrations; choose and securely re