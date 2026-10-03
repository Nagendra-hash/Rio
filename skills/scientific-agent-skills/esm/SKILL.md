---
name: esm
description: Uses the Biohub esm Python SDK for ESM3 protein generation, ESMC embeddings, and ESMFold2 all-atom folding. Applies to local model inference and Biohub hosted clients, including former Forge workflows; distinguishes the separate legacy fair-esm distribution.
license: MIT license
compatibility: Requires Python 3.12+ and esm 3.4.1.post1. Local pretrained inference needs model weights and sufficient RAM or GPU memory; hosted inference needs network access and ESM_API_KEY. Use an isolated environment, separate from fair-esm.
metadata: 
  version: 2.0
  skill-author: K-Dense Inc.
  last-reviewed: 2026-09-30
  upstream-version: 3.4.1.post1
---

# ESM: protein generation, embeddings, and folding

## When to use

Use for Biohub's `esm` SDK: ESM3 masked multimodal generation, ESMC sequence
representations, and ESMFold2 all-atom prediction. The former Forge platform
migrated to `https://biohub.ai`, including ESM3; SDK class names still contain
`Forge`. This skill targets the released **esm 3.4.1.post1**, verified against
its wheel and current official documentation.

`fair-esm` is Meta's separate, older ESM2/ESMFold/ESM-IF distribution. Both
packages import as `esm`; install them in separate environments. The legacy
`esm.pretrained.esm2_*` interface is not the Biohub ESMC interface. Hugging Face
Transformers' native ESMC implementation is another API: do not interchange its
version requirements or output types with this SDK.

## Setup and model choice

```bash
uv venv --python 3.12 .venv-esm
uv pip install --python .venv-esm/bin/python "esm==3.4.1.post1"
```

The release declares Python >=3.12, Torch >=2.11,<2.12 and Transformers
>=4.57.6,<5. Python 3.12, Torch 2.11.0 and Transformers 4.57.6 were tested on
CPU. Linux x86_64 installs include GPU-specific dependencies; use a supported
inference machine and budget disk space. Flash Attention is optional; it is
unnecessary for the tiny CPU tests. Do not install this stack into a shared
environment containing incompatible Transformers or Torch pins.

| Task | Local model/API | Hosted client/model |
| --- | --- | --- |
| ESM3 sequence/structure/function generation | `ESM3.from_pretrained("esm3-sm-open-v1")` | `client("esm3-medium-2024-08")`; small/large IDs in the ESM3 reference |
| ESMC embeddings | `EsmcForMaskedLM` and `EsmcTokenizer`; `biohub/ESMC-300M`, `biohub/ESMC-600M`, `biohub/ESMC-6B` | `esmc_client("esmc-600m-2024-12")`; 300M/6B also documented |
| ESMFold2 all-atom structures | `EsmFold2Model`, `ESMFold2InputBuilder`; `biohub/ESMFold2` | `esmfold2_client("esmfold2-fast-2026-05")` |

ESMC 6B weights are now available locally. Current Biohub model cards identify
MIT licensing, with ESMC cards also linking third-party notices; review the
exact selected artifact's card and access requirements.
The older `ESMC` class remains as a deprecated compatibility wrapper. Local
model sizes, hosted availability and account quotas are different constraints;
a larger model does not guarantee better performance on a particular assay.

## Workflow

1. Specify the sequence/chain/residue mapping and whether the objective is
   generation, representation extraction or folding. Preserve input IDs and
   source provenance. Reject accidental `...`, spaces, FASTA headers or gaps
   in plain single-chain sequences rather than silently deleting them.
2. Choose a local checkpoint or explicit hosted model ID. Record SDK, checkpoint
   revision, model settings, mask locations and random seed where supported.
3. Check every SDK result for `ESMProteinError`. Hosted failures may be returned
   as values. Use finite request timeouts, context managers and bounded
   concurrency. See [hosted contracts](references/forge-api.md).
4. Validate the output contract: sequence length and fixed residues, residue-only
   embedding axes, or chain/atom mapping and confidence. Keep model predictions
   separate from experimental validation.
5. Save sequences/structures with settings and identifiers. Avoid caches keyed
   only by sequence when model, structure, function or generation settings differ.

## ESM3 completion and fresh structure prediction

Illustrative pretrained inference; no weights or hosted jobs were run in this
refresh. This toy sequence demonstrates API mechanics, not a functional design.

```python
import torch
from esm.models.esm3 import ESM3
from esm.sdk.api import ESMProtein, ESMProteinError, GenerationConfig

model = ESM3.from_pretrained("esm3-sm-open-v1", device=torch.device("cpu"))
prompt = "MPRT___KEND"
completed = model.generate(
    ESMProtein(sequence=prompt),
    GenerationConfig(track="sequence", num_steps=3, temperature=0.7),
)
if isinstance(completed, ESMProteinError):
    raise completed
assert completed.sequence is not None and len(completed.sequence) == len(prompt)
assert "_" not in completed.sequence
assert all(a == "_" or a == b for a, b in zip(prompt, completed.sequence))

# A fresh sequence-only prompt prevents old coordinates conditioning the check.
folded = model.generate(
    ESMProtein(sequence=completed.sequence),
    GenerationConfig(track="structure", num_steps=8),
)
if isinstance(folded, ESMProteinError):
    raise folded
assert folded.coordinates is not None
folded.to_pdb("candidate.pdb")
```

Generation fills masked positions. Calling it again on a completed track does
not implement refinement or temperature annealing; explicitly remask selected
positions or clear the track. ESM3 structure generation and ESMFold2 prediction
use different models and result types. See [ESM3](references/esm3-api.md) for
inverse folding, function vocabulary and coordinate conventions.

## ESMC embeddings with correct residue pooling

Illustrative pretrained loading; the same API and pooling were executed with a
tiny randomly initialized model on CPU. Add this skill's `scripts/` directory to
`PYTHONPATH` when importing the bundled helper.

```python
from esm.models.esmc import EsmcForMaskedLM, EsmcTokenizer
from esm_embeddings import embed_sequences

model = EsmcForMaskedLM.from_pretrained("biohub/ESMC-300M", device="cpu").eval()
tokenizer = EsmcTokenizer()
sequences = ["MPRTKEINDAGLIVHSPQWFYK", "ACDEFGHIK"]
features = embed_sequences(model, tokenizer, sequences)
assert features.shape == (2, 960)
```

`output.last_hidden_state` has shape `(B,T,D)` and includes CLS, EOS and padding;
`T` is not the raw residue count. [scripts/esm_e