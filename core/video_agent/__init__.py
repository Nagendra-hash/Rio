"""
core/video_agent — the Video Creation Agent machinery (Phase 2+).

spec.py      typed job spec, mode detection, per-mode defaults, stage machine
pipelines.py stage-runner registry (smoke pipeline; documentary/shorts later)
runner.py    stage-machine executor used by the detached worker and tests

The agent surface (voice/RIO) lives in actions/video_creator.py; AI_CINEMATIC
keeps routing through the existing ViMax path (core/repo_agent/vimax.py).
"""
from core.video_agent.spec import (AI_CINEMATIC, DOCUMENTARY, MODES,   # noqa: F401
                                   SHORTS, VIDEO_STAGES, build_spec,
                                   detect_mode, job_state, parse_duration_s)
