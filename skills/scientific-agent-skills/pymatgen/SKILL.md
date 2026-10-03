---
name: pymatgen
description: Analyzes, validates, converts, and transforms materials structures and computed materials data with pymatgen. Use for local phase diagrams, symmetry sensitivity, electronic-structure I/O, and bounded Materials Project queries.
license: MIT
compatibility: Python 3.11+ with uv. The verified snapshot uses pymatgen 2026.9.24, pymatgen-core 2026.9.23, and mp-api 0.46.5. Bundled help and planning CLIs use only the standard library; local scientific execution lazily requires the pinned pymatgen packages. Materials Project access additionally requires explicit network approval and the single named secret MP_API_KEY.
allowed-tools: Read Write Bash Glob Python
metadata: 
  version: 1.5
  skill-author: K-Dense Inc.
  last-reviewed: 2026-09-30
---

# pymatgen

Use pymatgen for explicit, provenance-preserving work with compositions,
molecules, periodic structures, computed entries, symmetry, phase diagrams,
electronic structures, and electronic-structure-code files. Treat every parse,
conversion, symmetry assignment, transformation, and database result as
method- and parameter-dependent.

The MIT frontmatter license covers this skill. `pymatgen` and
`pymatgen-core` are MIT; `mp-api` declares BSD-3-Clause-LBNL. Materials Project
data is generally CC BY 4.0, while contributed data remains owned by its
contributors. Check the exact artifact and data terms before redistribution.

## Verified snapshot (2026-09-30)

- `pymatgen==2026.9.24` is the latest stable wrapper release (uploaded 2026-09-23).
  Package metadata requires Python 3.11+ and directly requires
  `pymatgen-core>=2026.9.23`.
- `pymatgen-core==2026.9.23` is the latest stable core release (2026-09-23).
  It now contains core objects, symmetry/lattice operations, and the I/O layer,
  all under the existing `pymatgen.*` namespace.
- `mp-api==0.46.5` is the latest stable Materials Project client
  (2026-08-18), requires Python 3.11+, and depends on
  `pymatgen>2024.2.20`.
- The current API site is built from 2026.9.23 core documentation. Pinning both
  distributions prevents `pymatgen==2026.9.24` from silently resolving to a
  different future core.
- Pymatgen uses date-based versions. PyPI renders the date with dots; do not
  infer semantic-version compatibility from the numbers.

The local CLI suite and synthetic examples were executed on this snapshot.
File-dependent VASP/Q-Chem parses and authenticated API examples below are
illustrative: their signatures and current SDK source were checked, but no
licensed calculation or authenticated service retrieval was performed.

Create a project lock for reproducibility:

```bash
uv init --python 3.11
uv add "pymatgen==2026.9.24" "pymatgen-core==2026.9.23" "mp-api==0.46.5"
uv lock
uv sync --frozen
```

For a disposable reviewed environment:

```bash
uv venv --python 3.11 .venv-pymatgen
uv pip install --python .venv-pymatgen/bin/python \
  "pymatgen==2026.9.24" "pymatgen-core==2026.9.23" "mp-api==0.46.5"
```

Direct pins do not freeze all transitive wheels. Preserve `uv.lock`, platform,
Python version, package versions, and artifact hashes.

## Required workflow

1. State whether the object is a non-periodic `Molecule` or periodic
   `Structure`; record lattice and periodic boundary conditions.
2. State units. Pymatgen commonly uses Å, degrees, eV, eV/atom, amu, and
   g/cm³, but each API's documented contract is authoritative.
3. State coordinate mode. `Structure` coordinates are fractional unless
   `coords_are_cartesian=True`; `Molecule` coordinates are Cartesian.
4. Inspect every parser warning. For CIF, preserve occupancy, site-merging,
   stoichiometry, and correction warnings; do not silently accept fixes.
5. Report disorder/partial occupancies and oxidation-state decoration. Never
   guess oxidation states implicitly.
6. Run validation before symmetry, neighbor, transformation, conversion, or
   thermodynamic analysis.
7. Sweep symmetry tolerances and report `symprec` in Å and
   `angle_tolerance` in degrees with every assignment.
8. Treat transformations as new artifacts. Preserve the input, parameters,
   software versions, warnings, and parent/child checksums.
9. Before conversion, identify representation loss. Write only to a new path
   and round-trip-check scientifically relevant properties.
10. Build phase diagrams only from compatible total energies and correction
    schemes. A computed hull is conditional on the supplied entry set.
11. Keep all database access off by default. Disclose endpoint, filters,
    fields, result limit, cache behavior, output, license, and citation before
    an explicit execution step.
12. Preserve an artifact manifest. Never use pickle or load an untrusted
    general object graph; use schema-validated JSON and explicit constructors.

## Core objects

Use the public convenience imports:

```python
from pymatgen.core import Composition, Element, Lattice, Molecule, Structure

composition = Composition("LiFePO4", strict=True)
iron = Element("Fe")

lattice = Lattice.cubic(5.64)  # Å
structure = Structure(
    lattice,
    ["Na", "Cl"],
    [[0, 0, 0], [0.5, 0.5, 0.5]],
    coords_are_cartesian=False,
    validate_proximity=True,
)

molecule = Molecule(
    ["O", "H", "H"],
    [[0.0, 0.0, 0.0], [0.758, 0.0, 0.504], [-0.758, 0.0, 0.504]],
    charge=0,
    spin_multiplicity=1,
)
```

`Structure` and `Molecule` are mutable; use `IStructure`/`IMolecule` or an
explicit copy when mutation would compromise provenance. See
[core classes](references/core_classes.md).

## Safe local structure intake

Prefer the bundled validator, which captures CIF and Python warnings and
reports units, occupancy, disorder, oxidation states, periodicity, coordinate
mode, and minimum distances:

```bash
python scripts/composition_structure_validator.py composition "Fe2O3"
python scripts/composition_structure_validator.py structure structure.cif
python scripts/structure_analyzer.py structure.cif --symmetry
```

The distance report compares distinct sites under PBC; it excludes images of
the same site and returns null for a one-site cell. It is not a complete check
for contacts across very short lattice vectors. Plain Structure JSON is read
strictly; nested MSON objects, YAML, and compressed JSON are refused.

For direct CIF work, use the current parser method and inspect both warning
channels:

```python
import warnings
from pymatgen.io.cif import Ci