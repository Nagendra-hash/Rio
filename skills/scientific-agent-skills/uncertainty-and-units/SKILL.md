---
name: uncertainty-and-units
description: Tracks physical units and propagates measurement uncertainty in scientific calculations using pint and uncertainties. Use for unit conversion and dimensional checking, GUM uncertainty budgets, Type A and Type B evaluation, coverage factors and expanded uncertainty, Monte Carlo propagation, significant-figure and plus-minus reporting, error propagation through curve fits, CODATA constants, auditing Python code for stripped units or broken uncertainty propagation, and order-of-magnitude plausibility checks using dimensionless groups (Reynolds, Peclet, Damkohler, Knudsen, Biot, Womersley), characteristic scales such as diffusion time or Debye length, and observed magnitude ranges. Trigger on "is this number physically reasonable", "sanity check these units", "what regime is this flow in", or a result that looks off by orders of magnitude.
license: MIT
compatibility: Requires Python 3.12+. The numeric CLIs need pint, uncertainties, NumPy, and SciPy; the static auditor is standard-library only. All bundled tooling runs locally with no network access.
allowed-tools: Read Write Edit Bash
metadata: 
  version: 1.3
  last-reviewed: 2026-10-01
  skill-author: K-Dense Inc.
---

# Uncertainty and units

## Scope

Use this skill whenever a calculation carries physical units or a reported number needs
an uncertainty. Concretely:

- converting between units, including conversions that need a physical context
  (wavelength to photon energy, mass to amount of substance, energy to temperature);
- propagating uncertainty through a measurement model, with or without correlated inputs;
- building a GUM uncertainty budget from calibration certificates, specifications, and
  repeatability data;
- choosing a coverage factor and deciding whether `k = 2` is defensible;
- rounding and writing a result so a reader knows what the `±` means;
- extracting parameter uncertainties from a curve fit without discarding correlations;
- reviewing existing analysis code for silent unit and uncertainty defects;
- checking that a dimensionally consistent answer is also physically possible — the
  order of magnitude, the dimensionless group, and the regime it implies.

This skill covers the metrology and the two libraries that implement it. It does not
cover statistical inference, model selection, or study design — see `statistical-analysis`,
`statistical-power`, and `experimental-design`.

## Current release and installation

Verified 2026-10-01:

- **pint 0.26.1**, released 2026-09-10; requires Python 3.12+.
- **uncertainties 3.2.3**, released 2025-04-21; requires Python 3.8+.
- **NumPy 2.5.3** and **SciPy 1.18.1**; both require Python 3.12+.
- `scipy.constants` in SciPy 1.18.1 serves **CODATA 2022**. SciPy 1.11 and earlier
  served CODATA 2018; the switch to CODATA 2022 occurred in SciPy 1.15.

```bash
uv venv --python 3.13
source .venv/bin/activate
uv pip install "pint==0.26.1" "uncertainties==3.2.3" "numpy==2.5.3" "scipy==1.18.1"
```

`pint-pandas` and `pint-xarray` add unit-aware columns and arrays and are separate
installs.

## Non-negotiable workflow

1. **Attach units at input and strip them only at output.** Convert at function
   boundaries with `ureg.wraps` or `m_as("unit")`, never mid-calculation.
2. **Write the measurement model explicitly** before computing anything, including
   corrections whose estimated value is zero. A correction left out of the model leaves
   its uncertainty out of the budget.
3. **Give every input four things**: an estimate, a standard uncertainty, the
   distribution the uncertainty came from, and its degrees of freedom.
4. **Convert Type B statements with the right divisor.** A certificate's expanded
   uncertainty divides by its stated `k`; rectangular limits divide by `sqrt(3)`.
5. **Identify correlations before combining.** A shared calibration, instrument correction, or fit can induce correlation;
   quantify shared components instead of assuming every pair is correlated.
6. **Compute sensitivity coefficients**, and read the budget from `c_i * u(x_i)` rather
   than from the raw uncertainties.
7. **Check the linearization.** Compare propagated distributions with the GUM interval when in doubt.
   Establish Monte Carlo numerical stability before a JCGM 101 clause 8 claim;
   neither method validates the measurement model or the assigned input PDFs.
8. **Justify coverage.** A Student-t factor from effective degrees of freedom
   is an approximation requiring suitable distributional assumptions.
9. **Round the uncertainty first, then the value to the same decimal place.**
10. **State what the `±` is** — standard or expanded, with `k`, the coverage probability,
    and the method.
11. **Sanity-check the magnitude before reporting.** A dimensionally consistent result can
    still be impossible. Compare it against a known scale or a dimensionless group, and
    confirm every assumption you relied on still holds in that regime.

## The failures this skill exists to prevent

Each of the following runs without error and produces a plausible number.

### A unit stripped at an unknown scale

```python
length = (12.7 * ureg.mm).magnitude          # 12.7 -- of what?
length = (12.7 * ureg.mm).m_as("m")          # 0.0127 metres, stated
```

`.magnitude` returns whatever the quantity happened to be carrying. Name the unit at the
point of extraction, every time.

### Offset temperature arithmetic

```python
Q(20, "degC") + Q(5, "degC")     # OffsetUnitCalculusError -- correctly refused
Q(20, "degC") + Q(5, "delta_degC")   # 25 degree_Celsius
Q(25, "degC") - Q(20, "degC")        # 5 delta_degree_Celsius
```

Celsius and Fahrenheit are interval scales. An uncertainty on a temperature is always a
difference and belongs in a `delta_` unit: converting `20 ± 0.5 degC` to Fahrenheit
gives `68 degF ± 0.9 delta_degF`, two different conversions on one line.

### Logarithmic units that add by multiplying

```python
Q(10, "dBm") + Q(10, "dBm")   # 0.0001 kilogram**2 * meter**4 / second**6
```

That is 10 mW × 10 mW, not 20 mW and not 13 dBm. Nothing raises. Convert to a linear
unit before any arithmetic.

### A correlation destroyed by a round trip

```python
x = ufloat(1.0, 0.1)
x - x                                     # 0.0+/-0
x - ufloat(x.nominal_value, x.std_dev)    # 0.00+/-0.14
```

Rebuilding a variable from its nominal value and standard deviation c