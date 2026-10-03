---
name: pptx-posters
description: Creates and audits editable scientific posters in macro-free PowerPoint (.pptx) from author-approved local content and assets. Used when the requested deliverable is a PowerPoint research/conference poster and exact physical, printer, accessibility, provenance, and package-security checks are required.
license: MIT
compatibility: Requires Python 3.10+, uv, and exact generation pins python-pptx 1.0.2, Pillow 12.3.0, and lxml 6.1.3. Validation and PPTX ZIP/XML inspection are local and network-free; final PowerPoint, accessibility, PDF, printer, and author review are manual.
allowed-tools: Read Write Bash Glob Grep Python
metadata: 
  version: 2.4
  last-reviewed: 2026-10-01
  skill-author: K-Dense Inc.
---

# PPTX posters

## Scope

Use this skill only when the requested source/deliverable is an editable PowerPoint
poster. Do not route an unspecified poster request here merely because PowerPoint is
available.

The manifest 2.0 workflow generates a real one-slide `.pptx` from strict local JSON. It does not use
HTML conversion, external templates, schematic/image-generation services, API keys,
environment files, network requests, or mandatory figure styles.

## Hard gates

Stop instead of guessing when any gate is unmet:

1. The author has not supplied exact poster content and source records.
2. Any claim, number, citation, author, affiliation, funding statement, figure,
   license, or QR target is unresolved.
3. Current conference and printer requirements are not confirmed.
4. Author approval is not bound to the current manifest content hash.
5. An asset is remote, outside the manifest directory, unhashed, or unapproved.
6. An input is `.pptm`, contains macros/external relationships/OLE/embedded files,
   or is an untrusted template.
7. The requested workflow needs PowerPoint to be opened or executed automatically.
8. A script reports a package, layout, DPI, contrast, or output-plan blocker.

Never fabricate missing material or leave a plausible placeholder. Drafts fail closed.

## Install exact generation dependencies

In the poster project directory, create an environment outside this skill tree.
Activate it before running the tools (macOS/Linux shown):

```bash
uv venv
source .venv/bin/activate
uv pip install "python-pptx==1.0.2" "Pillow==12.3.0" "lxml==6.1.3"
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`. Then run the
CLI commands below from the skill directory with that environment still active.
Package installation needs network access unless the exact wheels are cached; the
poster tools themselves are local. Generation requires exactly:

```text
python-pptx==1.0.2
Pillow==12.3.0
lxml==6.1.3
```

All CLIs use lazy optional imports, so `python -B scripts/<tool>.py --help` works
without these packages. Use `-B` to avoid bytecode artifacts.

## Establish requirements before layout

Record these separately:

- physical trim width/height and orientation;
- bleed on each edge;
- safe margin inside trim;
- PowerPoint canvas width/height;
- uniform physical-artboard/canvas print scale;
- conference maximum dimensions and delivery format;
- printer trim, bleed, margin, scaling, color-mode, and proof requirements;
- final-output font and raster-DPI thresholds, each labeled as a heuristic or tied to
  an exact source.
- required font faces, workstation availability, embedding permission, and the
  substitution/proof workflow.

There is no universal poster size. Microsoft currently limits each custom PowerPoint
dimension to 1–56 inches and uses one size for all slides. If the physical artboard
is larger, use a proportional canvas only when the printer confirms scaling.

Read `references/poster_layout_design.md`.

## Build the manifest

Copy `assets/poster_manifest_template.json` into the project. The template is
deliberately invalid until every replacement token, false confirmation, and draft
approval is resolved.

Follow `references/manifest_spec.md` and `references/poster_content_guide.md`.

The manifest requires:

- exact source IDs for document metadata, every element, and every asset;
- document title, subject, and author names joined by `; ` each at most 255 characters
  for the library core-properties API; no silent truncation;
- `author_verified: true` on every source;
- `author_approved: true` on every element and asset;
- local PNG/JPEG paths and lowercase SHA-256 hashes;
- exact provenance and license/permission for every optional image;
- approved alt text and, when needed, a source-bound native long description;
- explicit reading order and design rectangles;
- visible exact fallback URL/text for every local QR image;
- confirmed conference/printer rules;
- declared sRGB contrast pairs and redundant data encoding;
- approval bound to canonical manifest content.

To obtain the content hash after all non-approval fields pass:

```bash
python -B scripts/validate_manifest.py poster.json \
  --print-content-hash
```

Give that exact manifest and hash to the author. Then set `approval.status` to
`approved`, record approver and offset-aware timestamp, and copy the hash. Any
non-approval edit invalidates approval.

Validate the approved manifest and local assets:

```bash
python -B scripts/validate_manifest.py poster.json
```

## Audit assets and palette before generation

```bash
python -B scripts/inventory_images.py poster.json \
  --output poster.assets.json

python -B scripts/check_palette.py poster.json \
  --output poster.palette.json

python -B scripts/plan_export.py poster.json \
  --output poster.export-plan.json
```

Effective DPI is pixels divided by final placed inches, not image metadata DPI.
The inventory fully decodes bounded images and blocks EXIF/XMP/comments and embedded
text/application metadata;
strip those offline, then rehash and reapprove the asset.
Contrast uses WCAG 2.2 sRGB mathematics; applying those values to a physical poster is
a design target, not a standalone conformance claim. Keep color-redundant labels,
markers, shapes, patterns, or line styles.

If the printer requires CMYK, the plan blocks print-readiness until a
printer-approved conversion/profile and proof exist. Do not claim that a native
PowerPoint PDF is CMYK-compliant.

Read `references/poster_design_principles.md`.

## Generate the PPTX

Use a new output path:

```bash
python -B scripts/generate_poster.py poster.json \
  --output 