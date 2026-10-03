---
name: scikit-bio
description: Biological data toolkit. Sequence analysis, alignments, phylogenetic trees, diversity metrics (alpha/beta, UniFrac), ordination (PCoA), PERMANOVA, FASTA/Newick I/O, for microbiome analysis.
license: BSD-3-Clause license
allowed-tools: Read Write Edit Bash
compatibility: Requires Python 3.10+ and scikit-bio 0.7.4 (uv pip install scikit-bio==0.7.4). NumPy 2.0+ is required. Optional matplotlib/seaborn/plotly for plotting; biom-format for BIOM tables; polars/anndata for table interoperability.
metadata: 
  version: 1.4
  last-reviewed: 2026-10-01
  upstream-version: 0.7.4
  skill-author: K-Dense Inc.
---

# scikit-bio

## Overview

Targets scikit-bio 0.7.4. Use it for sequence manipulation, alignment, phylogenetics, microbial ecology, and multivariate statistics. Native tests exercise small synthetic fixtures; examples with filenames or undefined inputs are workflow templates. See [review evidence and boundaries](references/review.md).

## When to Use This Skill

This skill should be used when the user:
- Works with biological sequences (DNA, RNA, protein)
- Needs to read/write biological file formats (FASTA, FASTQ, GenBank, Newick, BIOM, etc.)
- Performs sequence alignments or searches for motifs
- Constructs or analyzes phylogenetic trees
- Calculates diversity metrics (alpha/beta diversity, UniFrac distances)
- Performs ordination analysis (PCoA, CCA, RDA)
- Runs statistical tests on biological/ecological data (PERMANOVA, ANOSIM, Mantel)
- Analyzes microbiome or community ecology data
- Works with protein embeddings from language models
- Needs to manipulate biological data tables

## Core Capabilities

### 1. Sequence Manipulation

Work with biological sequences using specialized classes for DNA, RNA, and protein data.

**Key operations:**
- Read/write sequences from FASTA, FASTQ, GenBank, EMBL formats
- Sequence slicing, concatenation, and searching
- Reverse complement, transcription (DNA→RNA), and translation (RNA→protein)
- Find motifs and patterns using regex
- Calculate distances (Hamming, k-mer based)
- Handle sequence quality scores and metadata

**Common patterns:**
```python
import skbio

# Read sequences from file
seq = skbio.DNA.read('input.fasta')

# Sequence operations
rc = seq.reverse_complement()
rna = seq.transcribe()
protein = rna.translate()

# Find motifs
motif_positions = seq.find_with_regex('(ATG[ACGT]{3})')  # capture group required

# Check for properties
has_degens = seq.has_degenerates()
seq_no_gaps = seq.degap()
```

**Important notes:**
- Use `DNA`, `RNA`, `Protein` classes for grammared sequences with validation
- Use `Sequence` class for generic sequences without alphabet restrictions
- FASTQ quality scores load into positional metadata; specify the known `phred_offset=33` or `variant` (do not infer encoding from the filename). Translation needs an explicit genetic code/frame and a decision about incomplete codons.
- Metadata types: sequence-level (ID, description), positional (per-base), interval (regions/features)

### 2. Sequence Alignment

Perform pairwise and multiple sequence alignments using the `pair_align` engine (introduced in scikit-bio 0.7.0), a versatile and efficient dynamic-programming aligner.

**Key capabilities:**
- Global, local, and semi-global alignment (free ends configurable) in one function
- Convenience wrappers `pair_align_nucl` (BLASTN-like) and `pair_align_prot` (BLASTP-like)
- Configurable scoring: match/mismatch tuple or named substitution matrix; linear or affine gap penalties
- `PairAlignPath` results carry CIGAR strings and convert to aligned sequences
- Multiple sequence alignment with `multi_align_nucl` / `multi_align_prot` (0.7.4); storage and manipulation with `TabularMSA`

**Common patterns:**
```python
from skbio import DNA, Protein
from skbio.alignment import pair_align_nucl, pair_align_prot, pair_align, TabularMSA

# Nucleotide alignment with BLASTN-like defaults
seq1, seq2 = DNA('ACTACCAGATTACTTACGGATCAGG'), DNA('CGAAACTACTAGATTACGGATCTTA')
aln = pair_align_nucl(seq1, seq2)
aln.score                                  # alignment score (float)
path = aln.paths[0]                        # PairAlignPath (repr shows CIGAR)
aligned_seqs = path.to_aligned((seq1, seq2))  # list of gapped strings

# Build a TabularMSA from the alignment path + original sequences
msa = TabularMSA.from_path_seqs(path, (seq1, seq2))

# Customize the algorithm via pair_align (default mode='global')
aln = pair_align(seq1, seq2, mode='local')                       # Smith-Waterman
aln = pair_align(seq1, seq2, sub_score=(2, -3), gap_cost=(5, 2), free_ends=False)
aln = pair_align(seq1, seq2, sub_score='NUC.4.4', gap_cost=3)    # substitution matrix, linear gap

# Protein alignment (BLASTP-like, BLOSUM62)
aln = pair_align_prot(Protein('HEAGAWGHEE'), Protein('PAWHEAE'))

# Read a multiple alignment from file and summarize
msa = TabularMSA.read('alignment.fasta', constructor=DNA)
consensus = msa.consensus()
```

**Important notes:**
- `pair_align` replaces the removed SSW wrapper (`local_pairwise_align_ssw`, `StripedSmithWaterman`) and the deprecated pure-Python aligners (`global_pairwise_align`, `local_pairwise_align_nucleotide`, etc.)
- The result is a `PairAlignResult` that also unpacks as `score, paths, matrices` (use `keep_matrices=True` to retain the DP matrix)
- `sub_score` accepts a `(match, mismatch)` tuple or a matrix name (e.g., `'NUC.4.4'`, `'BLOSUM62'`); `gap_cost` accepts a single number (linear) or `(open, extend)` tuple (affine)
- `mode="global"` defaults to free terminal gaps (overlap); use `free_ends=False` for fully penalized global alignment. Affine gap cost is open + length × extend. Wrapper scoring resembles BLAST, but is not a BLAST search or E-value.
- Parse external CIGAR strings with `PairAlignPath.from_cigar('1I8M2D5M2I')`; score an existing alignment with `align_score(...)` and build a distance matrix from an MSA with `align_dists(...)`

### 3. Phylogenetic Trees

Construct, manipulate, and analyze phylogenetic trees representing evolutionary relationships.

**Key capabilities:**
- Tree construction from distance matrices (UPGMA/WPGMA, Neighbor Joining, GME, BME)
- Tree rearrangement with nearest neighbor interchange (`nni`)
- Tree manipulation (pruning, rerooting, traversal)
- Distance calculations (patristic via `cophenet`, Robinson-Foulds via `compare_rfd`)
- ASCII visualization
- 