---
name: scientific-schematics
description: Generates scientific diagram drafts using Nano Banana 2 AI with smart iterative refinement. Uses Gemini 3.7 Flash for quality review. Refines when the review requests improvement, with at most two generations. Specialized in neural network architectures, system diagrams, flowcharts, biological pathways, and complex scientific visualizations.
allowed-tools: Read Write Edit Bash
license: MIT license
compatibility: Requires Python 3.10+ with requests, network access, and an OpenRouter API key.
metadata: 
  version: 1.10
  last-reviewed: 2026-09-30
  skill-author: K-Dense Inc.
  openclaw: 
    primaryEnv: OPENROUTER_API_KEY
    envVars: 
    - name: OPENROUTER_API_KEY
      required: false
      description: OpenRouter API key for the skill's LLM-powered steps.
---

# Scientific Schematics and Diagrams

## Overview

Scientific schematics and diagrams transform complex concepts into clear visual representations for publication. **This skill uses Nano Banana 2 AI for diagram generation with Gemini 3.7 Flash quality review.**

**How it works:**
- Describe your diagram in natural language
- Nano Banana 2 generates a PNG draft from the specified components and relationships
- **Gemini 3.7 Flash reviews quality** against document-type thresholds
- **Smart iteration**: Refines when the score or verdict requests improvement
- Saved drafts and a review log for manual verification
- No coding, templates, or manual drawing required

**Local review thresholds by document type** (heuristics chosen by this helper, not publisher acceptance criteria):
| Document Type | Threshold | Description |
|---------------|-----------|-------------|
| journal | 8.5/10 | Nature, Science, peer-reviewed journals |
| conference | 8.0/10 | Conference papers |
| thesis | 8.0/10 | Dissertations, theses |
| grant | 8.0/10 | Grant proposals |
| preprint | 7.5/10 | arXiv, bioRxiv, etc. |
| report | 7.5/10 | Technical reports |
| poster | 7.0/10 | Academic posters |
| presentation | 6.5/10 | Slides, talks |
| default | 7.5/10 | General purpose |

Save diagrams under `figures/` using `-o figures/name.png`, then inspect every label and relationship before including them in a paper or poster. The output location is the path you choose.

**What the output is:** a raster PNG at whatever resolution the image model returns. This skill has
no vector path and no DPI control — if a journal demands PDF, EPS, or 300 dpi TIFF, convert the PNG
downstream and check the result at final print size.

## Quick Start: Generate Any Diagram

Run from this skill directory with `requests` installed and `OPENROUTER_API_KEY` set. The following paid-generation examples are illustrative; the request contracts are tested offline. Supply source-backed labels and relationships rather than asking the image model to invent them:

```bash
# Generate for journal paper (highest quality threshold: 8.5/10)
python scripts/generate_schematic.py "CONSORT participant flow diagram with 500 screened, 150 excluded, 350 randomized" -o figures/consort.png --doc-type journal

# Generate for presentation (lower threshold: 6.5/10 - faster)
python scripts/generate_schematic.py "Transformer encoder-decoder architecture showing multi-head attention" -o figures/transformer.png --doc-type presentation

# Generate for poster (moderate threshold: 7.0/10)
python scripts/generate_schematic.py "MAPK signaling pathway from EGFR to gene transcription" -o figures/mapk_pathway.png --doc-type poster

# Custom max iterations (max 2)
python scripts/generate_schematic.py "Complex circuit diagram with op-amp, resistors, and capacitors" -o figures/circuit.png --iterations 2 --doc-type journal
```

**What happens behind the scenes:**
1. **Generation 1**: Nano Banana 2 creates initial image following scientific diagram best practices
2. **Review 1**: **Gemini 3.7 Flash** evaluates quality against document-type threshold
3. **Decision**: If quality >= threshold and no improvement verdict → **DONE** (no more iterations needed!)
4. **If below threshold or improvement requested**: Improved prompt based on critique, regenerate
5. **Repeat**: Until quality meets threshold OR max iterations reached

**Smart Iteration Benefits:**
- ✅ Saves API calls if first generation is good enough
- ✅ Higher quality standards for journal papers
- ✅ Faster turnaround for presentations/posters
- ✅ Appropriate quality for each use case

**Output**: Versioned images (`name_v1.png`, `name_v2.png`), a copy of the latest successful version at the path you
asked for, and `name_review_log.json` with the score, critique, and any early-stop reason.

**When the review cannot run** — a rate limit, a content filter, a reviewer that answers in some
unexpected shape — the image is still generated and saved, but no score is invented for it. The log
records `"score": null` with the reason in `"review_error"`. `"reviewed": false` means the reviewer did not return a usable response; a prose answer with no parsable score can have `"reviewed": true`. `"final_reviewed"` remains false without a numeric score, and the run
prints `Review unavailable - image kept, quality not verified`. Treat that image as unchecked and
look at it yourself and diagnose the review error before spending on another generation. A generation failure ends the loop without automatic replay; if an earlier draft exists, it is retained with its score and the failure in the log.

### Configuration

Set your OpenRouter API key:
```bash
export OPENROUTER_API_KEY='your_api_key_here'
```

Get an API key at: https://openrouter.ai/keys

**Data leaves the machine.** Your prompt is sent to OpenRouter to generate the image, and the
generated image is sent back to OpenRouter for the quality review. Both are subject to OpenRouter's
data policies and those of the underlying model providers. Do not describe unpublished data,
patient information, or anything under embargo in the prompt.

### Current API contract

The image call uses OpenRouter `POST /api/v1/images` with `model`, `prompt`, and `n: 1`; it reads `data[0].b64_json` and checks the returned MIME type (when supplied) and PNG signature. Quality review separately uses `POST /api/v1/chat/completions` with text and an `image_url` data URL. Both use Bearer authentication. The current chat schema still allows image output; this helper follows the dedicated image-generation guide with