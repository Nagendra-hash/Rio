---
name: glycoengineering
description: Analyzes and engineers protein glycosylation by scanning canonical N-glycosylation sequons, describing S/T-rich regions, checking curated glycan evidence, and preparing NetNGlyc, NetOGlyc and GlycoSHIELD workflows. Use for glycoprotein engineering, antibody Fc glycosylation, glycan shielding, and site-specific glycoproteomics interpretation.
license: Unknown
compatibility: Local sequence helpers require Python 3.10+. Public GlyTouCan lookup requires requests and network access. DTU predictors use a browser; optional GlycoSHIELD needs a separate source installation, glycan libraries, and GROMACS for SASA analysis.
metadata: 
  version: 1.4
  skill-author: Kuan-lin Huang
  last-reviewed: 2026-10-01
---

# Glycoengineering

## When to use

Use for canonical N-glycosylation candidate analysis, O-GalNAc candidate triage,
antibody glycoform comparison, or structural glycan shielding. Keep four kinds of
result separate: sequence motif, prediction, experimentally supported occupancy,
and glycan structure/composition. None alone establishes all the others.

This workflow focuses on eukaryotic secretory-pathway N-glycosylation and mucin-type
O-GalNAc. O-GlcNAc, O-mannose, O-fucose and other O-linked modifications require
appropriate evidence and predictors; S/T enrichment does not identify the type.

## Workflow

1. Record the protein accession, isoform/version, exact sequence, expression host,
   signal peptide/transmembrane topology and construct boundaries. Preserve a
   mapping from submitted sequence coordinates to mature protein, PDB chain/residue
   identifiers and antibody numbering where relevant.
2. Scan canonical N-X-[S/T] candidates with X not Pro. Retain overlaps: `NNST`
   has candidates starting at 1 and 2. A missing canonical motif does not rule out
   unusual N-glycosylation; a present motif does not establish occupancy.
3. Prioritize secreted/luminal/extracellular regions using topology and curated
   evidence. Sequence-only results in a cytoplasmic region are not evidence of
   secretory-pathway glycosylation.
4. For O-GalNAc, use S/T density only as a descriptive feature, then inspect a
   suitable predictor and cell/transferase context. **Do not exclude SP/TP motifs**:
   adjacent Pro can be favorable, depending on the GalNAc-transferase and context.
   See [the GalNAc-T substrate study](https://doi.org/10.1093/glycob/cwu089).
5. Propose explicitly numbered mutations, list every changed residue and rescan
   the full product for lost/created overlapping motifs. Evaluate structural and
   expression effects independently of glycosylation.
6. Validate occupancy and glycoforms experimentally, with localization evidence,
   glycan composition/structure confidence, and the biological assay required by
   the engineering objective. For biosimilar comparisons, match sample handling,
   analytical coverage and quantification before comparing glycan percentages.

## Local sequence analysis

The standard-library helper [scripts/glycoengineering_tools.py](scripts/glycoengineering_tools.py)
replaces copied snippets. Import it with this skill's `scripts/` directory on
`PYTHONPATH`, or run Python from that directory. It accepts raw canonical
20-amino-acid sequences, normalizes whitespace/case, and rejects FASTA headers,
gaps, unknown residues and empty input. Parse FASTA records first; do not remove
unknown residues because that changes coordinates. Coordinates are 1-based in the
normalized submitted sequence.

```python
from glycoengineering_tools import (
    normalize_sequence, find_n_glycosylation_sequons,
    eliminate_glycosite, add_glycosite, find_st_rich_sites,
)

sequence = normalize_sequence("NNST")
sites = find_n_glycosylation_sequons(sequence)
assert [site["position"] for site in sites] == [1, 2]

mutant = eliminate_glycosite(sequence, 1, "Q")
assert mutant == "QNST"  # the overlapping sequon at position 2 remains
assert [site["position"] for site in find_n_glycosylation_sequons(mutant)] == [2]

# Three intended changes: A1N, P2A, A3T. P->A must be explicit.
parent = "APA"
product = add_glycosite(parent, 1, "T", allow_proline_substitution=True)
changes = [(i, before, after) for i, (before, after) in
           enumerate(zip(parent, product), 1) if before != after]
assert product == "NAT" and len(changes) == 3

# Density is a fraction of residues, not an O-glycosylation probability.
o_candidates = find_st_rich_sites("STPST", window=7, min_st_fraction=0.4)
assert [site["position"] for site in o_candidates] == [1, 2, 4, 5]
```

`eliminate_glycosite` requires a complete canonical sequon and a one-residue
replacement other than N. `add_glycosite` can alter up to three residues; it retains
an existing S/T at +2. Neither function predicts a mutant's fold, glycan occupancy,
function or tolerability. An N-to-Q substitution can change protein behavior even
without a glycan effect.

The S/T density window must be a positive odd integer. Terminal windows shorten,
and their denominator is the actual window length. Dense regions may help triage
mucin-like sequence; isolated real O-GalNAc sites can be missed.

For batch analysis, keep identifiers and normalized sequence lengths:

```python
sequences = {"overlap": "NNST", "control": "APAA", "mucin_like": "STPST"}
rows = []
for name, raw_sequence in sequences.items():
    seq = normalize_sequence(raw_sequence)
    positions = [site["position"] for site in find_n_glycosylation_sequons(seq)]
    rows.append({"protein": name, "length": len(seq),
                 "n_sequon_positions": positions,
                 "n_sequons_per_100_residues": 100 * len(positions) / len(seq),
                 "st_rich_positions": [site["position"] for site in find_st_rich_sites(seq)]})
```

## Prediction services

Use the official forms, which accept FASTA and return web results; this skill does
not provide a stable submission API or job-polling endpoint. Saving a CGI URL is
not a submitted job. Preserve the output, input and service version together.

| Service | Current documented scope and interpretation |
| --- | --- |
| [NetNGlyc 1.0](https://services.healthtech.dtu.dk/services/NetNGlyc-1.0/) | Human N-glycosylation; reports network potential and jury agreement. Default threshold 0.5; not a calibrated occupancy probability. Up to 2,000 sequences / 200,000 residues total / 4,000 per sequence. SignalP runs, but extrac