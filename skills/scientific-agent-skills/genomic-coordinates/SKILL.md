---
name: genomic-coordinates
description: Converts genomic intervals between coordinate conventions, normalises and compares variant representations, and detects assembly or contig-naming mismatches before they corrupt an analysis. Used whenever coordinates cross a format, tool, or assembly boundary - converting between BED, GFF/GTF, VCF, SAM/BAM, WIG, PSL, genePred, Picard interval_list, or region strings; reconciling 0-based half-open with 1-based inclusive; left-aligning or trimming indels; checking whether two variant records describe the same change; mapping genomic to transcript, CDS, or protein positions; auditing a BED/GTF/VCF for convention violations; or diagnosing GRCh37 vs hg19 vs GRCh38 vs T2T, chr-prefix, and liftover problems. Triggers include "off by one", "0-based", "1-based", "half-open", "coordinate system", "left-align", "normalize variant", "bcftools norm", "chr prefix", "wrong genome build", "liftover", "REF mismatch", and "HGVS".
license: MIT
compatibility: Requires Python 3.11+. Scripts use only the standard library - no third-party packages and no network access. Variant normalisation needs an uncompressed reference FASTA with exact contig names; .fai enables indexed access. Without .fai the entire FASTA is loaded into memory.
allowed-tools: Read Write Edit Bash
metadata: 
  version: 1.3
  last-reviewed: 2026-10-01
  skill-author: K-Dense Inc.
---

# Genomic Coordinates

## When to use

Any time a coordinate crosses a boundary: between two file formats, between two
tools, between two assemblies, or between the genome and a transcript.

## The rule

**A coordinate is three facts, not one: the number, the convention it is written
in, and the assembly it was measured against.** Carry all three or the number is
not interpretable.

Coordinate errors are the quietest class of bug in genomics. An off-by-one BED
file parses, sorts, and intersects without complaint. A GRCh37 VCF joined against
a GRCh38 annotation returns rows. A right-shifted indel simply fails to match its
entry in ClinVar, and the result is a variant reported as novel. Nothing raises
an error; the answer is just wrong, and it is wrong in a direction that looks
plausible.

So: convert with the table, not from memory, and verify against the reference
whenever a reference is available.

## The two conversions

```
1-based inclusive  ->  0-based half-open :  start - 1,  end
0-based half-open  ->  1-based inclusive :  start + 1,  end
```

These formulas apply to nonempty spans on the same reference and strand. They do
not encode insertions, circular wraparound, liftover, or transcript mapping.

## Which format is which

| 0-based, half-open | 1-based, inclusive |
| --- | --- |
| BED, bedGraph, bigWig, narrowPeak | GFF3, GTF, VCF |
| BAM (binary POS), BCF (binary POS) | SAM text POS, CRAM absolute alignment start |
| PSL, genePred, refFlat | WIG, Picard interval_list |
| MAF (UCSC multiple alignment) | MAF (TCGA mutation annotation) |
| PyRanges, pybedtools | GRanges/IRanges, samtools & UCSC & Ensembl region strings |

Both "MAF" formats exist, they mean different things, and they disagree. UCSC
serves 0-based files through a 1-based browser box. `references/format-conventions.md`
has the full table with per-format detail.

```bash
cd skills/genomic-coordinates/scripts

python3 convert_coords.py --list                          # the table
python3 convert_coords.py --from bed --to gff chr1 999 1000
python3 convert_coords.py --from ucsc --to bed "chr7:5,530,601-5,530,625"
python3 convert_coords.py --from granges --to pyranges --input regions.tsv
```

```
contig  input                 output           length  status  detail
chr7    chr7:5530601-5530625  5530600-5530625  25      ok
```

Zero-length BED features (`chromStart == chromEnd`, a legal insertion point) are
reported as `unrepresentable` for an inclusive target because the converter lacks
feature semantics. GFF3 can encode insertion sites with equal endpoints and a
feature type; that is different from an ordinary single-base interval. Exit code
is 1 for invalid or unrepresentable output; valid zero-length half-open output
exits 0. Output is a diagnostic TSV/JSON table, not a rewritten GFF/VCF.

`--input` parses BED/bedGraph, GFF/GTF, literal-allele VCF REF spans, explicit
region strings, and three-column GRanges/PyRanges/Python TSVs. Other table rows
are conventions only: extract an interval with a native parser and pass a triple.
All bundled text readers expect uncompressed files.

## Variants are not intervals

For a simple VCF indel, `POS` normally identifies the unchanged padding base
before the event. At contig position 1 the padding can follow the event. Complex
substitutions need not have an unchanged anchor. And the same change can be written many ways:
`chr1:7:CAC:C`, `chr1:3:CAC:C` and `chr1:2:GCA:G` are one deletion. Joining,
deduplicating, or looking up variants before normalising loses real matches
silently, and it loses them preferentially in repeats, where indels concentrate.

Normalise — trim to parsimony, then left-align against the reference — before any
comparison:

```bash
python3 normalize_variant.py --fasta ref.fa chr1 7 CAC C
python3 normalize_variant.py --fasta ref.fa --split --input cohort.vcf
python3 normalize_variant.py --fasta ref.fa --compare chr1:7:CAC:C chr1:2:GCA:G
```

```
input         normalized    type      pos_shift  ref_check  changed
chr1:7:CAC:C  chr1:2:GCA:G  deletion  5          ok         yes
```

Literal alleles are checked against the FASTA using exact contig names. A
`MISMATCH` can indicate an assembly, sequence, strand, or coordinate error; stop
and investigate with `check_contigs.py` and sequence provenance. The helper
rejects unsplit ALT lists: use `--split` for independently normalized allele keys.
Its TSV discards genotypes and annotations; use `bcftools norm` for production
VCF rewriting. Symbolic/breakend/missing/spanning-deletion alleles are passed
through as `skipped`, without REF or structural validation.

The default left-shift window is 1,000 bp. If it prevents completion, the helper
returns `incomplete`, exits 1, and refuses an equivalence verdict. Increase
`--window` and rerun. Matching normalized keys tests individual literal alleles,
not haplotype equivalence across multiple records.

[HGVS applies the 3'-most rule](https://hgvs-nomenclature.org/stable/recommendations/general/)
to the 