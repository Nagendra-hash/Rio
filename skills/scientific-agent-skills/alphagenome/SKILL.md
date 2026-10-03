---
name: alphagenome
description: Looks up precomputed AlphaGenome Atlas effects for any GRCh38 single-nucleotide variant (AVI score with Phred and 18 SHAP feature attributions, plus raw and quantile scores for RNA-seq, DNase, ATAC, ChIP-TF, ChIP-histone, CAGE, PRO-cap, splicing, polyadenylation and contact-map tracks), scores variants or scans windows on demand with the AlphaGenome model for human and mouse (variant scoring, in silico mutagenesis, REF-versus-ALT track prediction), and builds Atlas website deep links. Use when the user mentions AlphaGenome, AlphaGenome Atlas, AVI or AlphaGenome Variant Impact, DeepMind variant effect prediction, or wants to prioritise or mechanistically interpret non-coding, regulatory, splicing, enhancer, promoter, or chromatin-accessibility effects of SNVs from a VCF, credible set, or region. Research use only; not a clinical tool.
license: MIT
compatibility: Python 3.10+ with the alphagenome package (0.9.0 or later for the Atlas client; brings numpy, pandas, anndata, grpcio). Network access to gdmscience.googleapis.com:443 and a free non-commercial AlphaGenome API key in ALPHAGENOME_API_KEY (ALPHA_GENOME_API_KEY also read). Human data is GRCh38 only; mouse is mm10 (model only).
allowed-tools: Read Write Edit Bash
metadata: 
  version: 1.2
  skill-author: K-Dense Inc.
  upstream-version: alphagenome 0.9.0
  last-reviewed: 2026-09-30
  openclaw: 
    primaryEnv: ALPHAGENOME_API_KEY
    envVars: 
    - name: ALPHAGENOME_API_KEY
      required: true
      description: AlphaGenome API key, free for non-commercial use from deepmind.google.com/science/alphagenome. ALPHA_GENOME_API_KEY is accepted as an alternative spelling.
---

# AlphaGenome and the AlphaGenome Atlas

AlphaGenome is DeepMind's sequence-to-function model: 1 Mb of DNA in,
predictions at modality-specific resolutions for eleven assay types across
thousands of human and mouse tracks out. The **AlphaGenome Atlas** (released 2026-09-08) is that model run once over
every possible single-nucleotide change in GRCh38, about 9 billion variants,
stored with a single ranking number, the **AlphaGenome Variant Impact (AVI)**
score, its genome-wide percentile, and an 18-way attribution of what drives it.
Both are reached through one `pip install alphagenome` and one API key.

> Research and theoretical modelling only. API outputs must not be used to train
> other models, and are not for diagnostic procedures or medical decisions.
> Permissive-use downloads have separate terms; check the artifact licence.

## When to use which

| You have | Use | Why |
| --- | --- | --- |
| hg38 SNVs (a VCF, a credible set, a region up to ~1 kb) | **Atlas** via `scripts/atlas_query.py` | precomputed, higher quota, includes AVI and attributions |
| indels, mouse variants, a non-reference background, a custom scorer or window | **model** via `scripts/score_variants.py` or Python | the Atlas is SNV-only and hg38-only |
| a hypothesis to explain (which motif, which tissue, REF vs ALT tracks) | model `predict_variant` + plots, Atlas track scores, portal link | mechanism, not just rank |
| GRCh37 coordinates, rsIDs, unnormalised indels | `genomic-coordinates` first, then come back | wrong build or swapped REF gives a plausible wrong answer |
| ClinVar assertions, gene-disease validity, ACMG framing | `folklore-variant-evidence`, `database-lookup` | AlphaGenome is one evidence line, never the verdict |
| promoter/enhancer/expression predictions without a DeepMind key | `genomic-intelligence` | different provider, keyless demo tier |

## Setup

```bash
uv pip install alphagenome                     # PyPI; tested on Python 3.12 and 3.13, alphagenome 0.9.0
export ALPHAGENOME_API_KEY="..."               # https://deepmind.google.com/science/alphagenome
cd skills/alphagenome/scripts
python atlas_query.py scorers                  # proves key + network in one call
```

Shell examples quote variant strings because `>` otherwise redirects output.
Network examples below are illustrative and were checked against SDK 0.9.0
contracts, without authenticated prediction/Atlas calls during this review.

Never put the key on a command line or in a file you commit; the scripts only
read it from the environment. Authentication failures may surface as
`ValueError` or `PermissionError`,
depending on the gRPC status returned by the service.

## The coordinate contract

- A variant is **1-based** `chr:pos:ref>alt` (`chr22:36201698:A>C`). gnomAD
  (`22-36201698-A-C`), GTEx (`chr22_36201698_A_C_b38`), and Open Targets
  spellings are autodetected by the scripts. In the SDK, pass the matching
  `variant_format=genome.VariantFormat.GNOMAD` (or `GTEX`, `OPEN_TARGETS`,
  `OPEN_TARGETS_BIGQUERY`); `from_str` defaults to `chr:pos:ref>alt`.
- An interval on the command line is **1-based closed** `chr:start-end`; the
  SDK's `genome.Interval` is **0-based half-open**. The scripts convert.
- Human is **GRCh38 only**. The Atlas key is `chr:pos:alt`; REF is implied by
  the reference. Swapping REF/ALT can cause a miss; a wrong REF or build can
  misidentify a record. The scripts reject a returned variant that differs
  from the request, but cannot validate the build. Check REF against GRCh38 FASTA.
- rsIDs are not accepted by the API or the portal. Resolve them to coordinates.
- Use the `chr` prefix; `MT` becomes `chrM`.

## Atlas workflow

### 1. Rank with AVI

```bash
python atlas_query.py avi --variant "chr22:36201698:A>C" "chr9:128225994:G>A"
python atlas_query.py avi --input candidates.vcf --min-phred 20 -o avi.tsv
python atlas_query.py avi --interval chr11:5225727-5226575 --top-k 25 -o hbb_window.tsv
python atlas_query.py avi --input credible_set.tsv --with-tracks -o avi_tracks.tsv
```

Output, one row per variant:

| Column | Meaning |
| --- | --- |
| `avi_raw` | composite model output (the 18 attributions sum to it) |
| `avi_cdf_quantile` | cumulative quantile against all genome-wide SNVs, as served |
| `avi_tail_quantile`, `avi_phred`, `avi_top_percent` | `tail = 1 - cdf`, `phred = -10 log10(tail)`; Phred 20 = top 1 %, 30 = top 0.1 % |
| `top_feature`, `top_feature_value` | largest absolute SHAP attribution and its value |
| `fi_MERGED_SPLICING` ... `fi_IS_DELETION` | all 18 attributions (keys in `references/atlas.md`) |
| `top_track_*` (with `--with-tracks`) | the strongest track behind the top feature: scorer, track, biosample, ontology CURIE, g