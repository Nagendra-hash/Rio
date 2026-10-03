---
name: hugging-science
description: Discovers and evaluates scientific datasets, models, methodology posts, and Spaces through the Hugging Science catalog. Used when selecting scientific ML resources in biology, chemistry, genomics, materials, climate, physics, astronomy, medicine, mathematics, protein design, single-cell analysis, or PDE modeling, and when checking their actual datasets, Transformers, native-runtime, Inference Providers, or Gradio interfaces.
compatibility: Requires Python 3.10+ and network access for the standard-library catalog fetcher. Resource use needs the matching scientific runtime; optional HF_TOKEN for gated access.
metadata: 
  version: 1.5
  last-reviewed: 2026-10-01
  skill-author: K-Dense Inc.
---

# Hugging Science

Hugging Science is a curated, LLM-friendly index of scientific datasets, models, blog posts, and interactive demos for ML researchers. Use it to find candidate resources, then verify their author documentation and scientific suitability; curation does not establish quality, openness, or executable compatibility.

There are two related surfaces, and you should use both:

- **The catalog at `huggingscience.co`** — a static, parseable index of resources across 17 scientific domains. It exposes `llms.txt` (compact), `llms-full.txt` (full content), and `topics/<slug>.md` (per-domain). These are markdown files designed to be fetched and read.
- **The `hugging-science` Hugging Face organization** — `huggingface.co/hugging-science` — community-submitted datasets, models, and a changing collection of Gradio, Docker and static Spaces. Not every listing exposes an inference API.

The catalog *points to* resources hosted on the broader Hugging Face Hub. For example, ESM2 supports Transformers, while Evo2 requires its native runtime and OpenGenome2 needs explicit file-format handling. The catalog provides discovery; use each resource through its verified loader or service.

## When to use this skill

Engage this skill when the user's task involves AI/ML applied to science. Common signals:

- Names a scientific domain (protein, genome, molecule, crystal, weather, climate, galaxy, EEG, microbiome, pathology, plasma, …)
- Asks "is there a dataset/model for X" where X is scientific
- Wants to fine-tune on scientific data, evaluate on scientific benchmarks, or reproduce a scientific ML paper
- Asks about specific known scientific models (Evo-2, ESM2, BoltzGen, Nucleotide Transformer, AlphaFold-derived, etc.)
- Needs an interactive demo for a scientific task (binder design, theorem proving, etc.)

If the task is generic ML (recommendation systems, chatbot RAG, vision on cats and dogs), this skill is **not** the right tool — defer to general HF Hub knowledge instead.

## Core workflow

Most invocations follow this five-step loop. Start with the relevant topic, then assess the underlying resource independently.

### 1. Identify the domain(s)

Map the user's task to one or more of the 17 topic slugs:

`astronomy` · `benchmark` · `biology` · `biotechnology` · `chemistry` · `climate` · `conservation` · `earth-science` · `ecology` · `energy` · `engineering` · `genomics` · `materials-science` · `mathematics` · `medicine` · `physics` · `scientific-reasoning`

Some tasks span multiple topics (e.g., drug discovery → `chemistry` + `biology` + `medicine`). Fetch each relevant topic.

### 2. Fetch the relevant catalog content

Use the bundled script for clean, structured access:

```bash
python scripts/fetch_catalog.py topic biology
python scripts/fetch_catalog.py topic materials-science --filter models
python scripts/fetch_catalog.py search "protein language model"
python scripts/fetch_catalog.py all     # full llms-full.txt
```

You can also fetch the raw markdown directly:

- `https://huggingscience.co/llms.txt` — compact index
- `https://huggingscience.co/llms-full.txt` — every entry, every domain
- `https://huggingscience.co/topics/<slug>.md` — one domain (slug is hyphenated, e.g. `materials-science.md`, `earth-science.md`, `scientific-reasoning.md`)

Each entry is a markdown block with `Type`, `Tags`, `HuggingFace` URL (or `Link` for blogs), and a one-line description. See `references/topics-and-slugs.md` for the entry schema and slug list.

### 3. Pick the right resource(s)

Read the descriptions and tags. Match to the user's task with judgment, not keyword overlap. Things to weigh:

- **Scale fit** — Evo-2 40B is overkill for a quick sequence classification on a laptop; ESM2 35M might be perfect.
- **License and access** — most are open, but check the underlying HF model card.
- **Modality alignment** — DNA vs. protein vs. SMILES vs. crystal structure; many "biology" models are not interchangeable.
- **Recency / supersession** — if both an older and newer entry cover the same task, prefer newer unless there's a reason not to.

Explain material tradeoffs between plausible candidates. Proceed with the best fit when task requirements resolve the choice; ask only if a missing preference would materially change the result.

For domain-specific go-to picks (the "if in doubt, start here" entries), see `references/flagship-resources.md`.

### 4. Use the resource

The mechanics depend on resource type. Read the matching reference file before writing code:

- **Datasets** → `references/using-datasets.md` — loading via `datasets`, streaming for huge corpora, common columns, splits
- **Models** → `references/using-models.md` — supported Transformers loaders, native scientific runtimes, verified Inference Provider mappings and memory limits
- **Spaces (interactive demos)** → `references/using-spaces.md` — `gradio_client` schema discovery and the source-verified BoltzGen contract, with its current runtime limitation

The reference files are short and focused. If you're already fluent in the relevant API, skim; if not, read fully before writing code. The patterns are different from generic HF usage in a few important places (e.g., `trust_remote_code` requirements, scientific-data dtype gotchas).

Before using a selected resource, record its exact Hub repository and immutable
commit, dataset configuration/split, license, and preprocessing/tokenizer
revision. [Dataset `revision`](https://huggingface.co/docs/datasets/loading)
can pin a commit; a moving branch name alone does not freeze the resource.
Resources from the same organization still need explicit vocabulary, input
m