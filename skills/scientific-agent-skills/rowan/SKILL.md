---
name: rowan
description: Rowan is a cloud-native molecular modeling and medicinal-chemistry workflow platform with a Python API. Use for pKa and macropKa prediction, conformer and tautomer ensembles, docking and analogue docking, protein-ligand cofolding, MSA generation, molecular dynamics, permeability, descriptor workflows, and related small-molecule or protein modeling tasks. Ideal for programmatic batch screening, multi-step chemistry pipelines, and workflows that would otherwise require maintaining local HPC/GPU infrastructure.
license: Proprietary (API key required)
compatibility: Python 3.12+ with rowan-python and its RDKit/stjames dependencies. Requires network access and ROWAN_API_KEY for hosted workflows.
metadata: 
  version: 1.7
  last-reviewed: 2026-09-30
  upstream-version: rowan-python 3.2.0
  skill-author: Rowan Science
  trigger-keywords: pKa prediction, molecular docking, conformer search, chemistry workflow, drug discovery, SMILES, protein structure, batch molecular modeling, cloud chemistry
  openclaw: 
    primaryEnv: ROWAN_API_KEY
    envVars: 
    - name: ROWAN_API_KEY
      required: true
      description: Rowan computational chemistry API key.
---

# Rowan: Cloud-Native Molecular-Modeling and Drug-Design Workflows

## Overview

Rowan is a cloud-native workflow platform for molecular simulation, medicinal chemistry, and structure-based design. Its Python API exposes a unified interface for small-molecule modeling, property prediction, docking, molecular dynamics, and AI structure workflows.

Use Rowan when you want to run medicinal-chemistry or molecular-design workflows programmatically without maintaining local HPC infrastructure, GPU provisioning, or a collection of separate modeling tools. The service manages hosted compute and results; available workflows depend on the account.

## When to use Rowan

**Rowan is a good fit for:**

- Quantum chemistry, semiempirical methods, or neural network potentials
- Batch property prediction (pKa, descriptors, permeability, solubility)
- Conformer and tautomer ensemble generation
- Docking workflows (single-ligand, analogue series, pose refinement)
- Protein-ligand cofolding and MSA generation
- Multi-step chemistry pipelines (e.g., tautomer search → docking → pose analysis)
- Batch medicinal-chemistry campaigns where you need consistent, scalable infrastructure

**Rowan is not the right fit for:**
- Simple molecular I/O (use RDKit directly)
- Methods or element/charge regimes outside the selected engine's documented support

## Quick start

```bash
uv pip install "rowan-python==3.2.0"
```

```python
import rowan
# Reads ROWAN_API_KEY from the environment.

# Descriptors require a 3D Molecule, not a bare SMILES string.
mol = rowan.Molecule.from_smiles("CC(=O)Oc1ccccc1C(=O)O")
wf = rowan.submit_descriptors_workflow(mol, name="aspirin")
result = wf.result()

print(result.descriptors["MW"])       # exact/monoisotopic mass
print(result.descriptors["SLogP"])
print(result.descriptors["TopoPSA"])  # topological PSA
```

This submits a hosted calculation and consumes credits. Examples target
[`rowan-python` 3.2.0](https://pypi.org/project/rowan-python/3.2.0/), reviewed
2026-09-30 against the released SDK and [official reference](https://docs.rowansci.com/api/python/v3/).
Local schema/serialization smoke tests used `stjames` 0.0.279. Hosted examples
are illustrative: no authenticated workflows were run for this refresh. Lock
both package versions for a reproducible campaign.

## Installation

```bash
uv pip install "rowan-python==3.2.0"
# Lock dependencies in your own environment; do not install the unrelated "rowan" package.
```

## Authentication and account access

### Authentication

Set an API key via environment variable (recommended):

```bash
export ROWAN_API_KEY="your_api_key_here"
```

Or set directly in Python:

```python
import rowan
rowan.api_key = "your_api_key_here"
```

Verify authentication:

```python
import rowan
user = rowan.whoami()  # Returns user info if authenticated
print(f"User: {user.email}")
print(f"Credits available: {user.credits_available_string()}")
print(user.enabled_workflows)  # Account-specific backend workflow slugs
```

## Molecule input formats

Use SMILES for topology-based methods and real 3D structures for geometry-based
methods. SMARTS is a substructure-query language, not a general workflow molecule
input; convert InChI with a chemistry toolkit before passing a supported input.
Record stereochemistry, charge, protonation state, and the original identifier.
Canonicalization alone does not resolve these scientific choices.

### SMILES strings versus molecule objects

- pKa: `starling` and `chemprop_nevolianis2025` require a SMILES string;
  `gxtb_wagen2026` (default) and `aimnet2_wagen2024` require coordinates.
- Conformer search: SMILES works with OpenConf (default) or ETKDG; CREST/MCMM
  require a 3D molecule.
- Membrane permeability: `gnn-mtl` requires SMILES; `pypermm` requires a 3D molecule.
- ADMET, LogP, macropKa, and solubility are SMILES-based; pose-analysis MD needs
  ligand SMILES **and a protein complex containing its bound pose**.
- Descriptors, tautomers, docking, analogue docking, BDE, NMR, and Fukui need a
  `rowan.Molecule`, `stjames.Molecule`, or RDKit molecule with a conformer.
  `Chem.MolFromSmiles()` alone has no coordinates. Generate them explicitly with
  `rowan.Molecule.from_smiles()` or import an existing geometry. For analogue
  docking, preserve the reference pose in the receptor's coordinate frame.

**Tip:** Use RDKit to validate SMILES before submission:

```python
from rdkit import Chem
smiles = "CCO"
mol = Chem.MolFromSmiles(smiles)
if mol is None:
    raise ValueError(f"Invalid SMILES: {smiles}")
```

## Core usage pattern

Most Rowan tasks follow the same three-step pattern:

1. **Submit** a workflow
2. **Wait** for completion (with optional streaming)
3. **Retrieve** typed results with convenience properties

```python
import rowan

# 1. Submit — named functions build and validate workflow-specific payloads
workflow = rowan.submit_descriptors_workflow(
    rowan.Molecule.from_smiles("CC(=O)Oc1ccccc1C(=O)O"),
    name="aspirin descriptors",
)

# 2. & 3. Wait and retrieve
result = workflow.result()  # Blocks until done (default: wait=True, poll_interval=5)
print(result.data)              # Raw dict
print(result.descriptors["MW"]) # exact mass; n