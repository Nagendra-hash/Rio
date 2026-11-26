# INSTALLED_SKILLS_MANIFEST.md

**Generated 2026-10-24** by the Universal Repository Agent (see
`~/JarvisTools/registry.json` for the live index).

## Registered skills

| Skill ID | Repository | Pinned commit | Status | Enabled | Ops (allowlisted) |
|---|---|---|---|---|---|
| `vimax-video` | `HKUDS/ViMax` (v1.2.0) | `fd4b72e7731be6e5f88486206d25ea59849b0285` | **READY** (providers configured) | yes | `idea2video`, `script2video`, `artifacts` |
| `videorag-chat` | `HKUDS/VideoRAG` | `c412a093a820ef7a0e0dda31076ed871136198b3` | **NEEDS_CONFIGURATION** (no CUDA GPU, weights not fetched) | yes | `ask`, `index`, `list`, `status` |

## Installed repositories

| Name | Source | Commit | Licence | Risk | Env | Size |
|---|---|---|---|---|---|---|
| `vimax` | https://github.com/HKUDS/ViMax | `fd4b72e7731be6e5f88486206d25ea59849b0285` | MIT | low | uv (`uv sync --frozen --python 3.12`), Python 3.12.14 | ~580 MB incl. venv |
| `videorag` | https://github.com/HKUDS/VideoRAG | `c412a093a820ef7a0e0dda31076ed871136198b3` | dual: MIT (framework) + model licences | medium | uv (manual: Python 3.11.16, torch 2.1.2+cpu), `NEEDS_CONFIGURATION` | ~1.6 GB incl. venv (checkpoints not fetched) |

Install path: `/home/nagendra/JarvisTools/vimax` — completely outside
`~/Mark-III`; JARVIS's `.venv` untouched.

## Provider configuration (ViMax)

Read from `~/Mark-III/config/api_keys.json` (gitignored) — section
`vimax` overrides, falling back to the existing `openrouter_keys`:

| Rung | Provider | Model | Verified against installed source |
|---|---|---|---|
| LLM / script / planning | OpenRouter | `google/gemini-2.5-flash` | `chat_model.init_args` shape per `pipelines/idea2video_pipeline.init_from_config` |
| Image keyframes | OpenRouter | `openai/gpt-image-2` | `ImageGeneratorOpenrouterAPI.__init__` default (exact match) |
| Video shots | OpenRouter | `google/veo-3.1-lite` | `VideoGeneratorOpenRouterAPI.__init__` default (exact match) |

**Cost reality:** every render consumes paid OpenRouter credits (script →
keyframes → video shots). No free tier is assumed; each run requires
explicit confirmation.

## Skill manifest (adapter contract)

`~/JarvisTools/vimax/.jarvis/skill.json` declares:
* entry points (fixed argv templates → `.jarvis/jarvis_run.py`, ViMax's own
  pipeline classes; no tracked ViMax file is modified),
* input schemas (`topic`, `duration_s` 10–600 s, `style`, `aspect_ratio`
  16:9/9:16/1:1, `audience`, `language`, `narration`, `script`),
* output globs (`{out_dir}/**/*.mp4`, verified: exists + nonzero + `ftyp`
  + ffprobe duration),
* timeouts (7200 s), health check (`jarvis_run.py check` — imports both
  pipeline classes; **verified passing**),
* `paid_api: true`.

## Adapter registry (first-party adapters in code)

| Repo dir name | Adapter module |
|---|---|
| `vimax` | `core/repo_agent/vimax.py` |

New repositories install fine WITHOUT an adapter (manifest + registry
only); exposing their capabilities as skills requires writing an adapter
module and adding it to `_known_adapters()` — generic auto-derivation of
tools from repo code is intentionally not supported.
