---
name: pysam
description: Provides Python/HTSlib workflows for genomic files. Used when reading, querying, filtering, or writing SAM/BAM/CRAM, VCF/BCF, FASTA/FASTQ, or tabix data with pysam, including pileup, coverage, indexing, and CRAM references.
license: MIT
allowed-tools: Read Write Edit Bash
compatibility: Requires Python 3.9+ and pysam 0.24.1. Bundled scripts use local files. CRAM decoding may require the matching reference FASTA or an explicitly configured REF_PATH/REF_CACHE.
metadata: 
  version: 2.3
  last-reviewed: 2026-10-01
  skill-author: K-Dense Inc.
---

# pysam

## Overview

Use pysam for low-level, streaming access to HTSlib-supported genomic formats:

- `AlignmentFile` and `AlignedSegment` for SAM/BAM/CRAM
- `VariantFile`, `VariantHeader`, and `VariantRecord` for VCF/BCF
- `FastaFile` for indexed FASTA and `FastxFile` for sequential FASTA/FASTQ
- `TabixFile` for BGZF-compressed, tabix-indexed BED/GFF/GTF/custom tables
- `pysam.samtools` and `pysam.bcftools` for wrapped command dispatchers

Current upstream baseline: **pysam 0.24.1** (7 September 2026), wrapping
HTSlib/samtools/bcftools 1.24. Read `references/sources.md` before updating
version-specific guidance.

## Installation

Use the pinned release for reproducible work:

```bash
uv pip install "pysam==0.24.1"
```

Confirm the runtime:

```python
import pysam

print(pysam.__version__)           # 0.24.1
print(pysam.__samtools_version__)  # 1.24
```

Prebuilt wheels are available for supported macOS and Linux platforms. A
source build needs a C compiler and HTSlib build dependencies; read the
official installation guide linked from `references/sources.md`.

## First Decide

Before writing code:

1. Identify the real format, compression, sort order, and available index.
2. Decide whether coordinates are numeric Python coordinates or a region
   string. Do not mix them.
3. For CRAM, identify the exact reference assembly and FASTA.
4. Prefer indexed region access; use sequential iteration only when intended.
5. Preserve headers when writing and write to a new path by default.
6. State filtering semantics: mapping/base quality, flags, overlap handling,
   duplicate handling, and pileup depth cap.

For unfamiliar files, start with the bundled read-only inspector:

```bash
python scripts/inspect_hts.py sample.bam
python scripts/inspect_hts.py cohort.vcf.gz
python scripts/inspect_hts.py reference.fa
```

## Bundled Scripts

| Script | Purpose | Typical call |
|---|---|---|
| `scripts/inspect_hts.py` | Metadata-only inspection for alignment, variant, FASTA, FASTQ, and tabix files | `python scripts/inspect_hts.py sample.cram --reference ref.fa` |
| `scripts/alignment_qc.py` | Streaming aggregate read/QC counts as JSON | `python scripts/alignment_qc.py sample.bam --max-records 100000` |
| `scripts/variant_summary.py` | Streaming variant, FILTER, and genotype summary as JSON | `python scripts/variant_summary.py cohort.vcf.gz --region chr1:1-1000000` |
| `scripts/filter_alignments.py` | Filter SAM/BAM/CRAM without changing record order | `python scripts/filter_alignments.py input.bam output.bam --exclude-secondary` |

All scripts refuse to overwrite existing outputs. The filter also refuses stale
output indexes and offers `--index --csi` for large BAM contigs. FASTA inspection
requires an existing `.fai`; create it explicitly with `pysam.faidx()` first.
Run each with `--help` for coordinate, index, and privacy notes.

Examples use illustrative filenames and assay-specific thresholds. The local
synthetic suite exercises these API patterns on pysam 0.24.1; remote storage and
biological datasets are not part of that validation.

## Coordinate Contract

**Numeric coordinates accepted by pysam APIs are 0-based, half-open.** This
includes numeric `AlignmentFile.fetch()`, `VariantFile.fetch()`,
`FastaFile.fetch()`, `TabixFile.fetch()`, and `pileup()` arguments.

**Region strings are samtools-style: 1-based and inclusive.**

```python
# The same 100 bases:
bam.fetch("chr1", 99, 199)          # [99, 199)
bam.fetch(region="chr1:100-199")    # 1-based inclusive
```

VCF text uses 1-based `POS`, while record properties expose both systems:

```python
record.pos    # 1-based
record.start  # 0-based inclusive
record.stop   # 0-based exclusive
```

Read `references/coordinates_and_indexing.md` for format conversions, overlap
semantics, index choices, and contig-name checks.

## Alignment Files

Use context managers and explicit modes:

```python
import pysam

with pysam.AlignmentFile("sample.bam", "rb", threads=4) as bam:
    for read in bam.fetch("chr1", 1_000, 2_000):
        if (
            not read.is_unmapped
            and not read.is_secondary
            and not read.is_supplementary
            and read.mapping_quality >= 30
        ):
            print(read.query_name, read.reference_start, read.cigarstring)
```

Use `fetch(until_eof=True)` to stream every record in file order, including
unplaced unmapped reads, without requiring an index:

```python
with pysam.AlignmentFile("sample.bam", "rb") as bam:
    for read in bam.fetch(until_eof=True):
        ...
```

Important distinctions:

- `fetch()` returns placed alignment records overlapping a region; even an
  unmapped-flagged record can have a reference position. Filter `is_unmapped`.
- `count()` counts records and defaults to `read_callback="nofilter"`.
- `count_coverage()` returns A/C/G/T base counts and defaults to base quality
  15 plus `read_callback="all"`.
- `pileup()` exposes per-column reads and has its own filtering, base-quality,
  overlap, orphan, and `max_depth=8000` defaults.

For exact-region pileups, set `truncate=True` and explicit filters:

```python
with pysam.FastaFile("reference.fa") as fasta, pysam.AlignmentFile(
    "sample.bam", "rb"
) as bam:
    for column in bam.pileup(
        "chr1",
        1_000,
        2_000,
        truncate=True,
        stepper="samtools",
        fastafile=fasta,
        min_mapping_quality=20,
        min_base_quality=20,
        max_depth=100_000,
    ):
        base_depth = sum(
            not item.is_del and not item.is_refskip
            and item.query_position is not None
            for item in column.pileups
        )
        print(column.reference_pos, base_depth)
```

Read `references/alignment_files.md` for flags, CIGAR operations, tags,
modified bases, writing records, pileup details, and iterator lif