---
name: analytical-method-validation
description: Plans, executes, and documents validation, verification, and transfer of analytical procedures under the governing framework - ICH Q2(R2) and Q14, USP <1220>/<1225>/<1226>, ICH M10 bioanalytical, CLSI EP, or ISO/IEC 17025. Use for HPLC, LC-MS/MS, GC, CE, ICP-MS, dissolution, qNMR, qPCR, NIR, and ligand binding or cell-based assays whenever the question is whether a procedure is fit for its intended purpose. Triggers include "method validation", "analytical method validation", "AMV", "validation protocol", "acceptance criteria", "linearity", "reportable range", "accuracy and precision", "repeatability", "intermediate precision", "recovery", "LOD", "LOQ", "detection limit", "quantitation limit", "specificity", "robustness", "method transfer", "method comparison", "Deming", "Passing-Bablok", "Bland-Altman", "equivalence testing", "OOS investigation", "ICH Q2", "Q2(R2)", "Q14", "USP 1225", "ICH M10", "incurred sample reanalysis", "ISR", "CLSI EP", and any request to show that an assay works.
license: MIT
compatibility: Requires Python 3.11+. Scripts use only the standard library - no numpy, scipy, or network access. Statistical distributions are computed from first principles so results are reproducible in any conforming interpreter.
allowed-tools: Read Write Edit Bash
metadata: 
  version: 2.0
  skill-author: K-Dense Inc.
  last-reviewed: 2026-09-30
---

# Analytical Method Validation

## When to use

Any time the question is whether an analytical procedure is fit for its intended purpose:
designing a validation study, evaluating validation data, verifying a compendial procedure,
transferring a procedure to another laboratory or instrument, or defending any of these in a
report.

## The two rules

**1. Establish which framework governs before designing anything.** The same assay validates
differently under ICH Q2(R2), USP <1225>, ICH M10, CLSI EP, and ISO/IEC 17025. They differ in
which characteristics are required, how the studies are laid out, and whether numeric acceptance
criteria are supplied at all. Blending them produces a protocol that satisfies none of them.

**2. State acceptance criteria before collecting data.** Criteria chosen after seeing results are
not acceptance criteria, and deciding them post hoc is a standing audit finding. ICH Q2(R2)
deliberately supplies almost no numeric criteria — they have to come from the specification, the
analytical target profile (ICH Q14 section 3), or development data. ICH M10 is the exception: it
supplies explicit numbers, and they differ between chromatographic assays and ligand binding
assays.

## Scope

This skill plans studies, computes the statistics correctly, and structures the documentation. It
does **not** decide that a procedure is validated, release a batch, accept or reject a run, close
an investigation, or substitute for the analyst, the technical reviewer, the quality unit, or the
regulator. Every script reports computations and supported findings; only the responsible reviewers can
make the fitness-for-purpose decision. JSON uses `null` for unavailable statistics.

## Copyright boundary

ICH guidelines are published openly and licensed for reuse with acknowledgement, so their
requirements are encoded directly in this skill. **USP general chapters, CLSI EP documents, and
ISO standards are copyrighted and paywalled.** For those, this skill supplies the designation,
scope, and where to obtain an authorised copy — never the text, never invented thresholds. Do not
ask an agent to retrieve, transcribe, or reconstruct their content. If a number matters and it
lives in a paywalled document, read it from the authorised copy.

## Frameworks

```bash
cd skills/analytical-method-validation/scripts
python3 plan_validation.py --list-frameworks
```

| Key | Governs | Numeric criteria supplied |
| --- | --- | --- |
| `ich-q2r2` | Release and stability testing of drug substances and products | Almost none — you derive them |
| `ich-m10` | Bioanalytical concentration measurement (PK, TK, BE) | Yes, and they differ by modality |
| `usp-1220` | Compendial procedure lifecycle, three stages | Paywalled |
| `usp-1225` / `usp-1226` | Validation / verification of compendial procedures | Paywalled |
| `clsi` | Clinical laboratory measurement procedures (EP series) | Paywalled |
| `iso-17025` | Lab-developed and modified methods under accreditation | No — "to the extent necessary" |

**Q2(R2) replaced Q2(R1) in November 2023 and restructured the characteristics.** Range is now
the parent characteristic (section 3.2), containing *response* (linearity) and *validation of
lower range limits* (DL/QL). Accuracy and precision are section 3.3 and may be evaluated in
combination against a single criterion. Robustness is treated as a development activity and
cross-refers to ICH Q14. Multivariate procedures are addressed explicitly (2.5 and 3.2.2.3), and
Annex 2 adds worked examples for techniques Q2(R1) never covered — quantitative ¹H-NMR, NIR,
quantitative LC/MS, qPCR, biological assays, and particle size. A Q2(R1)-shaped protocol — a flat
list of linearity, range, accuracy, precision, specificity, LOD, LOQ, robustness — is out of date.
Note also the error correction dated 30 November 2023 to Table 5 and Tables 6–11.

## Scripts

```bash
cd skills/analytical-method-validation/scripts
```

| Script | Question answered |
| --- | --- |
| `plan_validation.py` | Which framework, which characteristics, what study layout, what protocol? |
| `check_response.py` | Does the calibration model actually hold across the range? |
| `check_accuracy_precision.py` | What is the recovery, and how much of the variability is between days? |
| `check_detection_limits.py` | What are DL and QL by each allowed approach, and do they serve the reporting threshold? |
| `check_bioanalytical_run.py` | Which supported ICH M10 numerical checks raise findings? |
| `compare_methods.py` | Are two procedures equivalent, at a pre-stated margin? |

Commands below use placeholder data filenames; supply your own controlled data. The printed
numerical examples were re-executed with synthetic repository fixtures under
`tests/analytical-method-validation/fixtures/` on Python 3.13.3. They are computational smoke
tests, not evidence that a physical assay is validated. All scripts take `--format table|tsv|json`. Provenance, guideline citations, and