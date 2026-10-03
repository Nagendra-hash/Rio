---
name: pufferlib
description: Version-aware guidance for PufferLib reinforcement-learning environments, vectorization, policies, PuffeRL training, evaluation, and safe checkpoint review. Covers the native 5.0 build and environment API, published 3.0.0 Gymnasium/PettingZoo adaptation, and a pinned historical 4.0 profile.
license: MIT
compatibility: Bundled CLIs require Python 3.10+ (standard library only). PufferLib 5.0 requires a native C/CUDA toolchain; CPU builds support evaluation, not training. PyPI 3.0.0 declares Python >=3.9 and needs a native source build with NumPy <2 and Gymnasium <=0.29.1. Native dependencies and network access are needed for installation; bundled checks require neither.
allowed-tools: Read Bash Grep Python
metadata: 
  version: 1.4
  skill-author: K-Dense Inc.
  last-reviewed: 2026-10-01
---

# PufferLib

Choose the version before choosing an API. Reviewed **2026-10-01**:

| Profile | Status | Main use |
|---|---|---|
| Native source `5.0` | Current default branch and live documentation | C/CUDA environments, native trainer; CPU evaluation only |
| `pufferlib==3.0.0` | Latest PyPI release, published 2025-06-23; sdist only | Python/Gymnasium/PettingZoo adaptation and Torch PuffeRL |
| Pinned source `4.0` | Historical snapshot | C Ocean interface with an optional Torch fallback |

For **5.0**, read [references/native-5.md](references/native-5.md). The reviewed
revision is `6ffa5b10dbbbe4d1e8288367c7d9d3acd3bad4a2`. Its CLI is
`./puffer train` after building an environment, not `puffer train ENV_NAME`.
There is no 5.0 Python emulation/vector API or `--slowly` fallback.

The bundled plan schema deliberately supports only 3.0 and pinned 4.0; it does
not launch training. All native/PufferLib training examples are **source-reviewed,
illustrative, and not executed in this review**. Bundled synthetic checks are
executed CPU tests, not evidence of PufferLib installation or learning quality.
The PyPI sdist was hash-verified and its Python sources inspected; the moving
`3.0` branch differs, including its `load_policy` and logger contracts.

## Safe defaults

1. Start with bundled synthetic, CPU-only, network-free tools.
2. Do not import an arbitrary environment by dotted path. Bundled tools accept
   only allowlisted built-ins and slug identifiers.
3. Do not install or execute an unreviewed environment package, native
   extension, ROM, map, checkpoint, or pickle file.
4. Verify official source, immutable revision, licenses, checksums or
   attestations, and build hooks. Sandbox native builds and first execution.
5. Cap steps, environments, agents, workers, threads, buffers, memory, disk,
   render size, and wall time.
6. Keep training and evaluation environments/seeds separate.
7. Default logging to local/none. External logging requires explicit opt-in,
   disclosure acknowledgment, and separate artifact-upload approval.
8. Never pass W&B or Neptune credentials via CLI, INI, JSON, tags, run names, or
   logger configuration. Never print them.
9. Never dump all environment variables or recursively search for `.env`.
10. Hash checkpoint bytes before trusted, sandboxed loading; metadata inspection
    is not proof of safety.

## First local checks

All bundled CLIs are dependency-free and emit strict JSON:

```bash
python3 scripts/env_template.py --help
python3 scripts/env_contract_validator.py
python3 scripts/benchmark_vectorization.py --backend serial
python3 scripts/train_template.py
python3 scripts/validate_plan.py
python3 scripts/repro_plan.py
```

Defaults are synthetic, deterministic, bounded, local, CPU-only, no-network,
and dry-run where training would otherwise occur.

## Installation and provenance

### Published 3.0.0

PyPI supplies only `pufferlib-3.0.0.tar.gz`:

```text
sha256: 7df3a3e3f5f894d78d2a1f5374097890aec01473183e748abefe4f3faa10eaa9
Requires-Python: >=3.9
```

After source/build review, create a pinned uv project:

```bash
uv venv --python 3.11
uv add --exact --no-sync "pufferlib==3.0.0"
uv lock
uv sync --frozen
```

These installation commands are illustrative and were not executed.
Commit `pyproject.toml` and `uv.lock`; verify the archive digest and every
resolved dependency. The source build can compile native code and fetch build
assets, so resolve/build in a sandbox without credentials or sensitive mounts.
The archive declares NumPy `<2`, Gymnasium `<=0.29.1`, and PettingZoo `<=1.24.1`;
latest Gymnasium/NumPy are not valid substitutes for this profile. Its setup
supports Linux/macOS and rejects other systems. Python classifiers alone do
not establish a successful native build. The uploaded metadata does not pin Torch or CUDA; do not claim a supported CUDA
matrix that PyPI does not declare.

### Pinned 4.0 source

The reviewed branch head on 2026-07-23 was:

```text
25647630e1b15330bb3153a5a0d3ff8d234c3acf
```

Pin the commit, not branch `4.0`:

```bash
uv add --no-sync \
  "pufferlib @ git+https://github.com/PufferAI/PufferLib.git@25647630e1b15330bb3153a5a0d3ff8d234c3acf"
uv lock
```

The reviewed 4.0 package declares Python `>=3.10` and Torch `>=2.9`. The reviewed
PufferTank snapshot uses Ubuntu 24.04, Python 3.12, and an NVIDIA CUDA
13.0.2/cuDNN development image with the `cu130` Torch index, but does not pin
the exact Torch wheel or all system packages. Treat it as a reference, not a
complete lock. Never execute a remote installer directly from a pipe.

Read `references/training.md` before any installation or build.

## Environment workflow

### 1. Validate the contract

Gymnasium reset returns `(observation, info)`. Step returns:

```python
(observation, reward, terminated, truncated, info)
```

Validate spaces, shapes, dtypes, finite rewards, booleans, reset-before-step,
reset-after-end, seeding, and cleanup. `terminated` is an MDP terminal;
`truncated` is an external cutoff such as a time limit. Preserve the distinction
for bootstrapping and metrics. Both flags can be true in general Gymnasium.
Check autoreset timing and retain the final pre-reset observation; do not
bootstrap from the next episode. The 3.0 trainer has unresolved truncation and
inactive-agent mask handling, described in `references/training.md`.

```bash
python3 scripts/env_contract_validator.py \
  --steps 64 --episodes 8 --seed 42
```

### 2. Adapt only after review

Published 3.0 uses explicit wrappers:

```python
import pufferlib.emulation

wrapped = pufferli