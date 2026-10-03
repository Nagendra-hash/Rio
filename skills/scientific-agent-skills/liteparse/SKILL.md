---
name: liteparse
description: Local document and PDF parsing that returns spatial text with bounding boxes. Use for extracting text from PDFs, DOCX, Office files, and images; running OCR on scans; producing layout-preserved JSON for RAG; batch-ingesting folders of papers; or rendering pages to PNG for multimodal agents. Distinguishing capabilities are spatial text boxes, Markdown, page raster output, and local parsing with optional custom HTTP OCR.
license: Apache-2.0
allowed-tools: Read Write Edit Bash
compatibility: Python 3.10+ with liteparse 2.15.0. LibreOffice required for Office formats; images convert natively. Tesseract is bundled but missing language data downloads on first use. Optional HTTP OCR requires network access and server-specific authentication.
metadata: 
  version: 1.6
  last-reviewed: 2026-10-01
  upstream-version: 2.15.0
  skill-author: K-Dense Inc.
---

# LiteParse — Local Document Parsing

## Overview

LiteParse is an open-source document parser (Rust core, Python/Node bindings) for **local, layout-aware text extraction**. It produces layout text, structured JSON, or heuristic Markdown. Spatial text items may span several words; Python `emit_word_boxes=True` adds word boxes when needed.

**Verified release:** Python **liteparse 2.15.0** (September 29, 2026); CLI and synthetic local PDF/image fixtures tested on Python 3.13. Node/Rust examples below are source-checked and illustrative. Images convert through bundled Rust libraries, not ImageMagick. No cloud account is needed, but missing Tesseract language data can download from GitHub and an explicitly configured HTTP OCR service receives document images.

For parser selection vs MarkItDown, PDF manipulation libraries, or LlamaParse, see `references/choosing_a_parser.md`.

## When to Use This Skill

Use LiteParse when you need:

- **Fast local parsing** of PDFs or converted Office/image files without cloud dependencies
- **Spatial text** with bounding boxes for layout-aware RAG, citation grounding, or figure/table region logic
- **OCR** on scanned PDFs or images (bundled Tesseract, or a user-run HTTP OCR server)
- **Page screenshots** (PNG) for multimodal agents that must see charts, figures, or handwriting
- **Batch ingestion** of literature folders, supplementary PDFs, or protocol libraries
- **Page subsets** or **password-protected** PDFs

## When Not to Use

| Task | Use instead |
|------|-------------|
| Markdown for LLM ingestion (EPUB, audio, YouTube, HTML) | `markitdown` skill |
| Merge/split PDFs, forms, watermarks, rotation | A PDF manipulation library such as `pypdf` |
| Dense tables, handwriting, production cloud pipelines | [LlamaParse](https://developers.llamaindex.ai/llamaparse/parse/) (cloud; sign up separately) |

## Installation

```bash
uv pip install "liteparse==2.15.0"
```

This installs the Python bindings and the **`lit`** CLI. Verify:

```bash
lit --help
python -c "import liteparse; print(liteparse.__version__)"
```

**Optional system tool** (for Office inputs):

- **LibreOffice** — Word, Excel, PowerPoint, OpenDocument, CSV/TSV

PNG, JPEG, TIFF, WebP, SVG and other supported images convert natively.

Install commands are in `references/ocr_and_formats.md`.

**Node.js / TypeScript** (optional): `npm i @llamaindex/liteparse@2.15.0` — see `references/api_reference.md`.

---

## Quick Start

### Python

```python
from liteparse import LiteParse

parser = LiteParse(quiet=True)
result = parser.parse("paper.pdf")
print(result.text)

for page in result.pages:
    print(f"Page {page.page_num}: {len(page.text_items)} items")
```

### CLI

```bash
# Layout-preserved text (default)
lit parse paper.pdf

# Structured JSON with bounding boxes
lit parse paper.pdf --format json -o paper.json

# Heuristic Markdown, including headings, tables and links
lit parse paper.pdf --format markdown -o paper.md

# Disable OCR on text-native PDFs (faster)
lit parse paper.pdf --no-ocr
```

---

## Core Workflows

### 1. Parse to layout-preserved text

Best for quick full-document text or feeding chunkers that do not need coordinates.

```python
parser = LiteParse(ocr_enabled=True, quiet=True)
result = parser.parse("document.pdf")
full_text = result.text
```

```bash
lit parse document.pdf -o output.txt
```

### 2. Parse to structured JSON (bounding boxes)

Use when building layout-aware RAG, highlighting source regions, or joining text with screenshots.

```python
from liteparse import LiteParse

parser = LiteParse(output_format="json", quiet=True)
result = parser.parse("document.pdf")

# Programmatic access
for page in result.pages:
    for item in page.text_items:
        bbox = (item.x, item.y, item.width, item.height)
        # item.text, item.confidence, item.font_name, item.font_size
```

```bash
lit parse document.pdf --format json -o document.json
```

JSON field layout: `references/output_formats.md`.

### 3. Parse specific pages

```python
parser = LiteParse(target_pages="1-5,10,15-20", quiet=True)
result = parser.parse("long_paper.pdf")
```

```bash
lit parse long_paper.pdf --target-pages "1-5,10"
```

### 4. Parse from bytes or stdin

Useful for uploads, S3 downloads, or piping remote PDFs.

```python
with open("document.pdf", "rb") as f:
    result = parser.parse(f.read())
```

```bash
curl -sL https://example.com/report.pdf | lit parse -
```

### 5. Page screenshots for multimodal agents

Screenshots capture visual content that text extraction alone misses (figures, complex tables, handwriting).

```python
from pathlib import Path

parser = LiteParse(dpi=150, quiet=True)
shots = parser.screenshot("document.pdf", page_numbers=[1, 2, 3])
out = Path("screenshots")
out.mkdir(exist_ok=True)
for s in shots:
    (out / f"page_{s.page_num}.png").write_bytes(s.image_bytes)
```

```bash
lit screenshot document.pdf --target-pages "1,3,5" -o ./screenshots
lit screenshot document.pdf --dpi 300 -o ./screenshots
```

Combine **JSON parse + screenshots** when an agent needs both coordinates and pixels for the same pages.

### 6. Batch-parse a directory

Use the CLI or bundled script. OCR workers parallelize OCR tasks; they do not parallelize whole-document PDFium parsing. Python worker pools provide process-level parallelism and hard parse timeouts; see the API reference.

```bash
lit batch-parse ./papers ./parsed --format json --recursive
lit batch-parse ./papers ./parsed --extension .pdf --no-ocr
```

```bash