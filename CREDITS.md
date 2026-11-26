# Credits & Attribution — Rajneesh / RIO

## Upstream project (this repository continues its line)

**MARK LV — JARVIS** by **FatihMakes**
* GitHub: <https://github.com/FatihMakes>
* Repository lineage: Mark LIII → Mark LIV → [Mark LV](https://github.com/FatihMakes/Mark-LV)
* Copyright (c) 2026 FatihMakes — licensed **CC BY-NC 4.0** (Creative Commons
  Attribution-NonCommercial 4.0 International, non-commercial use only).
  Canonical license text: <https://creativecommons.org/licenses/by-nc/4.0/legalcode>

This repository (Rajneesh / RIO, branch `upgrade-20260930`) continues the
Mark LIII → LIV → LV work line. The upstream license therefore applies to
the code inherited from it: **this project remains non-commercial under
CC BY-NC 4.0 for all inherited portions.** The full license text is in
[`LICENSE`](LICENSE). The upstream feature notes are preserved verbatim in
[`docs/mark-lv-readme.md`](docs/mark-lv-readme.md).

Historical reference (supplied by the maintainer; contents may differ from
this branch):
<https://github.com/Nagendra-hash/Rio/blob/upgrade-20260930/docs/mark-lv-readme.md>

## What RIO adds on top of upstream

The `upgrade-20260930` line adds, among other work: the video creation
system (`core/video_agent/` — documentary/Shorts/Blender pipelines, VAAPI
encoder ladder, engine registry, approval core), the model router
(`orchestrator/model_router/`), the RAG knowledge service, the YouTube
automation agent, the trading/backtest agents, and the RIO dashboard
refresh. These additions inherit the upstream CC BY-NC 4.0 terms because
they are part of the same combined work; no separate permissive license is
claimed for them.

## Third-party components

* **Blender** (used as an external headless tool, not distributed here) —
  Blender Foundation, GPL-2.0-or-later. Installed separately by the user.
* **FFmpeg** (external tool) — LGPL/GPL build depending on configuration.
* **Pinokio / himalaya** — present in the working tree as external
  checkouts referenced by gitlink only (no `.gitmodules`); their code is
  NOT part of this repository's history and keeps its own licenses.
* **VideoRAG** (<https://github.com/HKUDS/VideoRAG>, KDD'26) — installed as
  an external checkout at `~/JarvisTools/videorag`, **not** part of this
  repository's history. The framework architecture is MIT; the model
  checkpoints it expects (MiniCPM-V, ImageBind CC-BY-NC, faster-whisper) are
  downloaded separately by the user and keep their own licences. See
  `VIDEORAG_INTEGRATION_REPORT.md`.
* Python dependencies — see `requirements.txt` (each under its own license).

## Maintainer

* Rajneesh (GitHub: [Nagendra-hash](https://github.com/Nagendra-hash)) —
  continuation work on the `upgrade-20260930` branch.

No contributor names, ownership claims, or license permissions beyond the
facts above are asserted.
