---
name: gtars
description: Supports Gtars for local genomic interval models and set algebra, overlaps and counts, consensus and coverage, tokenization, fragment processing, and refget/BEDbase planning across Python, Rust, and the CLI.
license: MIT
compatibility: Python bindings require Python 3.10+ and gtars 0.10.0. The Rust meta-crate and gtars-cli are 0.10.0 and require a Rust toolchain supporting Edition 2024; upstream declares no rust-version. Bundled audit CLIs use only Python 3.10+ standard library and are local/network-free. Remote constructors, pretrained tokenizers, refget, and BEDbase caching require explicit network and storage approval.
allowed-tools: Read Write Edit Bash Glob
metadata: 
  version: 1.5
  last-reviewed: 2026-10-01
  skill-author: K-Dense Inc.
---

# Gtars

Gtars provides native Rust implementations, Python bindings, and a feature-gated
`gtars` binary for genomic interval and reference-sequence work. Start with the
bundled local inspectors; call upstream code only after the data contract,
provenance, resource bounds, and side effects are explicit.

## Verified snapshot (2026-10-01)

- Published Python wheel: [`gtars==0.10.0`](https://pypi.org/project/gtars/0.10.0/),
  released 2026-09-05, `Requires-Python >=3.10`; local examples were exercised
  on Python 3.12 with synthetic intervals, fragments, and FASTA.
- Rust meta-crate and CLI: [`gtars=0.10.0`](https://crates.io/crates/gtars/0.10.0)
  and [`gtars-cli=0.10.0`](https://crates.io/crates/gtars-cli/0.10.0), released
  2026-09-05. The binary is `gtars`; the wrapper's default feature set is empty.
  Rust/CLI examples are source-reviewed templates, not compiled executions.
- Direct [`gtars-refget=0.11.0`](https://crates.io/crates/gtars-refget/0.11.0)
  was released 2026-09-22. Its Rust `list_sequences()` now returns a `Result`.
  The wrapper requests refget 0.10.x; keep Cargo.lock because component
  dependencies use compatible ranges, not exact pins.
- A [`gtars-python-v0.11.0` Git tag](https://github.com/databio/gtars/releases/tag/gtars-python-v0.11.0)
  exists, but PyPI still publishes 0.10.0. Do not assume that Git tags, Python
  packages, components, or CLI versions coincide.
- Published guides contain older examples. This review used the PyPI 0.10.0
  source distribution/runtime, published CLI crate source, and current releases.

The `license: MIT` field covers this skill. Published `gtars` crates declare MIT,
while the GitHub repository currently displays BSD-2-Clause at the root; verify
the exact artifact's license before redistribution.

## Native-code trust gate and exact pins

The Python wheel contains a PyO3 native extension. Cargo installation compiles a
native binary and can run dependency build scripts. Treat either path as code
execution:

1. Confirm the official PyPI/crates.io/GitHub owner and immutable version.
2. Review filenames, platform tags, release provenance, license, and SHA-256.
   Verify checksums from the exact release; do not reuse older binary archives.
3. Never run an untrusted prebuilt binary, wheel, source tree, Cargo build script,
   or archive installer. Use isolation and CPU/RAM/disk/time limits.
4. Keep a lockfile and artifact hashes with the analysis manifest.

After that review, create an isolated Python environment:

```bash
uv venv --python 3.11 .venv-gtars
uv pip install --dry-run --python .venv-gtars/bin/python "gtars==0.10.0"
uv pip install --python .venv-gtars/bin/python "gtars==0.10.0"
.venv-gtars/bin/python -c \
  "import gtars; assert gtars.__version__ == '0.10.0'; print(gtars.__version__)"
```

For the source-reviewed CLI release (installation template, not run in this audit):

```bash
cargo install gtars-cli --version 0.10.0 --locked
gtars --version
gtars --help
```

For a Rust project, this source-reviewed template pins the wrapper and enables
only required features; retain Cargo.lock for transitive versions:

```toml
[dependencies]
gtars = { version = "=0.10.0", default-features = false, features = [
  "core", "overlaprs", "uniwig", "tokenizers", "refget"
] }
```

Use `gtars-refget = "=0.11.0"` directly only when the newer direct component API is
required and compatibility has been tested. Do not replace these pins with a Git
branch or an unreviewed release.

## Genomic data contract

Apply this contract before every operation:

1. **Coordinates:** BED intervals are 0-based and half-open: `[start, end)`.
   Require `0 <= start < end <= contig_length`. Gtars coordinates are `u32`, so
   reject values above `4,294,967,295`.
2. **Assembly:** record an assembly accession/version and the SHA-256 of the exact
   chromosome-sizes or refget sequence-collection metadata. Never infer assembly
   from filenames or `chr` prefixes.
3. **Contigs:** compare names exactly. `1` and `chr1`, alternate loci, decoys, and
   mitochondrial aliases are not interchangeable. Rename or liftover only as a
   separately reviewed transformation.
4. **Sorting:** preserve the original file, then sort a copy by chromosome-sizes
   order and numeric start/end when the operation requires it. Python
   `RegionSet(path)` currently sorts lexicographically by contig and start while
   loading; do not rely on original row order afterward. Construction does not
   merge overlapping intervals; call `reduce()` explicitly when that is intended.
5. **Strand:** BED6 uses `+`, `-`, or `.`. `Region.rest` retains trailing BED
   fields, but a file-backed Python `RegionSet` currently initializes its separate
   `strands` vector to `*`. `sort()` reorders regions without reordering that
   vector, and several set operations drop strand. Preserve and
   validate strand externally when it is scientifically meaningful.
6. **Duplicates/adjacency:** choose policies explicitly. `reduce()` and consensus
   merge overlapping **and adjacent** intervals; ordinary half-open overlap does
   not treat `[0,10)` and `[10,20)` as overlapping.

Run the local validator first:

```bash
python3 -B scripts/bed_validator.py \
  --input data.bed.gz \
  --assembly GRCh38.p14 \
  --chrom-sizes GRCh38.p14.chrom.sizes \
  --require-sorted
```

## Safe local workflow

1. Inventory local files, checksums, assembly, contig dictionary, coordinate
   system, strand policy, patient/replicate groups, and intended outputs.
2. Validate BED/fragments and estimate work. Pilot a small synthetic file.
3. Choose Python, CLI, or Rust from the 