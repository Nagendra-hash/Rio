---
name: 13c-metabolic-flux
description: Estimates intracellular metabolic fluxes from steady-state carbon-13 isotope-tracing measurements using validated atom maps, mfapy isotope simulation, constrained multistart fitting, and flux-profile diagnostics. Use for 13C-MFA, carbon tracing, mass isotopomer distributions (MDVs/MIDs), positional isotopomers, parallel tracer experiments, and determining whether labeling data constrain a pathway flux. Distinguishes measured-label inference from COBRA flux balance analysis and flags experiments requiring nonstationary MFA.
license: MIT
compatibility: Python 3.12 with uv and Git for installation. Tested with mfapy 0.6.3 at a10433af16682386548b360297e2476152d46ede, NumPy 2.5.3, SciPy 1.18.1, and NLopt 2.11.0. Network access is needed only to install public dependencies. Inference runs locally without credentials; inputs are JSON.
metadata: 
  version: 1.2
  skill-author: K-Dense Inc.
  last-reviewed: 2026-09-30
---

# Carbon-13 metabolic flux inference

Turn reviewed carbon maps, explicit tracer mixtures, and corrected labeling measurements
into feasible flux estimates and evidence about which fluxes the experiment constrains.
Use the bundled solver rather than reconstructing isotope balances or fitting each
reaction independently. It runs mfapy's EMU forward simulator and fits fluxes in the
mass-balanced feasible space with SciPy. It does not use an FBA objective.

## Scope and required evidence

This implementation supports **metabolic and isotopic steady state**, a single shared
flux state across one or more tracer experiments, nonnegative one-way reaction fluxes,
and carbon-subset mass distributions. Reversible reactions are two separately mapped
directions. Measurement error is Gaussian with a supplied covariance or a disclosed
diagonal approximation.

Before fitting, obtain:

- The carbon network and the source of each atom assignment. Stoichiometry alone does
  not specify where labeled atoms go. Record compartments as separate metabolite IDs.
- Evidence for both steady-state assumptions. Stable metabolite abundance does not
  establish isotopic steady state. Time-course labeling requires INST-MFA with pool
  sizes and initial labeling; do not average it into this solver.
- Every carbon input's positional isotopomer distribution, including unlabeled
  supplements, bicarbonate/CO2 when assimilated, and tracer impurity.
- Fragment carbon assignments, natural-abundance correction history, and uncertainty
  of the **reported mean**. Raw peak intensities, derivatized spectra, and MS/MS
  transitions require validated preprocessing before these inputs can be constructed.
- Flux units, extracellular rate measurements or a stated relative-flux reference,
  and biologically justified bounds. Label fractions alone cannot set an absolute rate.

If necessary information is missing, name it and prepare the input template; do not
invent a fragment assignment, atom map, isotope correction, or measurement error.
Read [references/input-contract.md](references/input-contract.md) when preparing inputs.
Read [references/inference.md](references/inference.md) before interpreting an actual fit.

## Install the tested engine

Run in the user's analysis directory. Set `SKILL_DIR` to this skill's installed directory,
using the actual resolved path. Keep environments and generated results outside the skill.

```bash
uv venv --python 3.12 .venv-mfa
uv pip install --python .venv-mfa/bin/python -r "$SKILL_DIR/assets/requirements.txt"
```

The following commands use `.venv-mfa/bin/python`; on Windows use the environment's
`Scripts/python.exe`. mfapy is installed from an immutable Git revision because it is
not distributed on PyPI. Installation executes dependency build code; model inputs
are data, not user-supplied Python. The adapter restricts identifiers and atom-map
syntax before they reach mfapy's internally generated numerical functions.

The pinned commit matched upstream `master` on 2026-09-30. Its README labels the
latest change "064", but its installed distribution still reports `0.6.3`; retain
the Git commit alongside the package version in an analysis record. The refreshed
NumPy/SciPy pins require Python 3.12 or later; the commands above use the tested 3.12
environment. See the reviewed forward-model contract in
[references/inference.md](references/inference.md).

## Workflow

1. **Prepare explicit inputs.** Copy a relevant model asset into the analysis directory,
   then replace its scientific content only from reviewed evidence. The bundled models
   are demonstrations, not validated organism-specific reconstructions. Use a separate
   dataset for each biological condition; jointly fit tracer replicates only when their
   biological flux state is defensibly shared.
2. **Check the contract and feasibility.**

   ```bash
   .venv-mfa/bin/python "$SKILL_DIR/scripts/mfa.py" check \
     --model model.json --data measurements.json --output input-check.json
   ```

   This checks atom counts and conservation, fragments, tracer sums, uncertainty
   matrices, bounds, and steady-state mass-balance feasibility. It cannot verify that a
   chemically consistent atom map is biologically correct or that a sample reached steady state.
3. **Exercise the forward model.** Supply one mass-balanced flux vector in the declared
   units. Compare predicted labeling with a reference or independently derived limits.

   ```bash
   .venv-mfa/bin/python "$SKILL_DIR/scripts/mfa.py" simulate \
     --model model.json --data measurements.json --fluxes fluxes.json \
     --output simulated-mdvs.json
   ```

4. **Fit and profile the fluxes relevant to the question.**

   ```bash
   .venv-mfa/bin/python "$SKILL_DIR/scripts/mfa.py" fit \
     --model model.json --data measurements.json --starts 12 --seed 2026 \
     --profile v3 --profile v7 --profile-points 31 --profile-starts 6 \
     --output fit.json
   ```

   Replace `v3` and `v7` with actual reaction IDs. Each profile point fixes that reaction
   and reoptimizes nuisance fluxes. For nonlinear networks, repeat with a different seed
   and more starts before interpreting a profile. A small residual is not an
   identifiability result.
5. **Inspect the evidence.** Check failed starts, residual patterns, mass balance,
   active bounds, local sensitivity rank, and profile status. Report threshold-crossing
   brackets at their actua