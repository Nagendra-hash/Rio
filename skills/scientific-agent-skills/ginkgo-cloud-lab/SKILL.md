---
name: ginkgo-cloud-lab
description: Guides protocol selection, input preparation, pricing checks, and browser ordering on Ginkgo Bioworks Cloud Lab (cloud.ginkgo.bio). Applies to cell-free, E. coli, and Pichia protein expression; HiBiT, A280, and LabChip readouts; IVT mRNA/circRNA synthesis; thermal shift assays; Echo-MS methods; SPR target onboarding; plate-reader assay onboarding; and fluorescent pixel art.
license: MIT license
compatibility: Requires network access and a browser for Ginkgo Cloud Lab; account access may be needed for ordering and results.
metadata: 
  version: 2.3
  last-reviewed: 2026-09-30
  skill-author: K-Dense Inc.
---

# Ginkgo Cloud Lab

## Overview

Ginkgo Cloud Lab (https://cloud.ginkgo.bio) provides remote access to Ginkgo Bioworks' autonomous lab infrastructure. Protocols use Reconfigurable Automation Carts (RACs), modular units with robotic arms and plate transport, across a catalog-advertised fleet of 70+ integrated instruments.

The platform also includes **EstiMate**, a compatibility and pricing assistant that accepts protocol descriptions, files, or links and returns preliminary estimates for custom workflows.

The catalog is organized into **Expression & Purification** (in vitro / cell-free / E. coli / Pichia), **Characterization & Assay**, **Method & Target Onboarding**, and **Specialty**. Pick a protocol below, then read its reference file for inputs, outputs, the automated workflow, and ordering details.

## Available Protocols

The following prices, availability labels, and turnaround text are a **2026-09-30
catalog snapshot**, not a configured quote. References link to the corresponding
service terms. Catalog and terms sometimes disagree on days versus business days,
input format, or readout scope; resolve those differences in the service order.

### Expression & Purification - In vitro

| Protocol | Readout | Price | Turnaround | Status |
|---|---|---|---|---|
| [IVT mRNA/circRNA Synthesis](references/ivt-rna-synthesis-qpcr.md) | qPCR (mRNA or circRNA, 384-well) | $99/sample | up to 12 business days | Certified |

### Expression & Purification - Cell-free (E. coli CFPS)

| Protocol | Readout | Price | Turnaround | Status |
|---|---|---|---|---|
| [Validate sequence expression](references/cell-free-protein-expression-validation.md) | Go/no-go titer + purity (up to 1800 bp) | $39/sample | up to 10 days | Certified |
| [Optimize expression conditions](references/cell-free-protein-expression-optimization.md) | DoE across 24 conditions | $199/sample | up to 11 days | Certified |
| [Express + quantify (HiBiT)](references/cell-free-protein-expression-hibit.md) | Luminescence, no purification | $39/sample | up to 11 days | Certified |
| [Express + purify (A280)](references/cfps-strep-tag-purification-a280.md) | Strep-tag, A280 yield | $149/sample | up to 11 days | Certified |
| [Express + purify minibinder](references/minibinder-strep-tag-a280.md) | Strep-tag, A280; confirm LabChip separately | $149/sample | up to 11 days | Certified |
| [Express + purify (A280 + LabChip)](references/cfps-expression-purification-quantification.md) | Strep-tag, A280 + purity/size | $159/sample | up to 12 days | Certified |

### Expression & Purification - E. coli

| Protocol | Readout | Price | Turnaround | Status |
|---|---|---|---|---|
| [Express + quantify (HiBiT)](references/ecoli-protein-expression-hibit.md) | Luminescence (up to 384 constructs) | $79/sample | up to 3 weeks | Certified |
| [Express + purify (A280)](references/ecoli-protein-expression-histag-a280.md) | His-tag, A280 yield | $199/sample | up to 3 weeks | Certified |
| [Express + purify minibinder](references/ecoli-minibinder-expression-histag-a280.md) | His-tag, A280 yield | $199/sample | up to 3 weeks | Certified |
| [Express + purify (A280 + LabChip)](references/ecoli-expression-purification-quantification.md) | His-tag, A280 + purity/size | $209/sample | up to 3 weeks | Certified |

### Expression & Purification - Pichia

| Protocol | Readout | Price | Turnaround | Status |
|---|---|---|---|---|
| [Express + quantify (LabChip)](references/pichia-protein-expression-labchip.md) | Secreted protein, size/purity (up to 96) | $89/sample | up to 4 weeks | Certified |

### Characterization & Assay

| Protocol | Readout | Price | Turnaround | Status |
|---|---|---|---|---|
| [Express + thermal shift](references/cfps-strep-purification-thermal-shift.md) | SYPRO Orange Tm (Tonset, TM1-3) | $159/sample | up to 12 days | Certified |
| [Detect enzymatic products (Echo-MS)](references/echo-ms-cfps-detection.md) | Substrate/product by Echo-MS | $44/sample | up to 13 days | Beta |

### Method & Target Onboarding

| Protocol | Readout | Price | Turnaround | Status |
|---|---|---|---|---|
| [Onboard Echo-MS method](references/echo-ms-method-onboarding.md) | Calibration curve, LOD/LOQ | $799/molecule | up to 3 weeks | Certified |
| [Onboard SPR target](references/spr-target-onboarding.md) | Validated SPR capture method | $1,399/target | up to 4 weeks | Beta |
| [Onboard plate-reader assay](references/plate-reader-assay-onboarding.md) | Qualification data; customer assesses performance | $399/assay | up to 4 weeks | Certified |

### Specialty

| Protocol | Readout | Price | Turnaround | Status |
|---|---|---|---|---|
| [Generate fluorescent pixel art](references/fluorescent-pixel-art-generation.md) | UV photo, 7-color E. coli palette | $25/plate | up to 7 days | Beta |

**Coming soon:** Protein Expression and Binding Affinity Characterization (express + purify, then screen binding affinity against a target).

## Choosing a Protocol

- **Quick expressibility screen?** Cell-free HiBiT ($39) or Validate sequence expression ($39).
- **Need purified protein + yield?** A280 tiers (cell-free or E. coli); add LabChip for purity/size.
- **Difficult / membrane / disulfide / cofactor targets?** Cell-free Optimize (24-condition DoE).
- **Secreted or eukaryotic targets?** Pichia expression.
- **Screening de novo binders/minibinders?** Expression tiers screen yield; SPR onboarding qualifies the target. Confirm availability of the separate downstream binding service before planning kinetics.
- **Enzyme activity / biocatalysis?** Echo-MS enzymatic detection (onboard the analyte method first).
- **Stability / developability ranking?** Thermal shift assay.
- **RNA (mRNA/circRNA)?** IVT synthesis + qPCR.
- **Transfer an existi