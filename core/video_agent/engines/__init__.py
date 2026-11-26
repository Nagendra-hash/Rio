"""
core/video_agent/engines — the Universal Video Engine Registry (Phase 2).

Auto-discovers every `engines/<id>/engine.py` exporting `ENGINE`. Public API
lives in `.registry`; see `base.py` for the plugin contract and `.custom`
for the adapter documentation.

Bundled engines:
    BLENDER_3D       real 3D animation (Blender 3.6.23, verified)
    VIMAX            agentic AI cinematic via installed ViMax skill
    WAN_2_1          open-weight diffusion adapter (local/remote)
    LTX_VIDEO        open-weight diffusion adapter (local/remote)
    STOCK_FOOTAGE    licensed clips + ffmpeg assembly (never "AI video")
    CUSTOM_VIDEO_MODEL  documented adapter interface
    CANVAS           the free stills+motion path (documentary/Shorts)
"""
from core.video_agent.engines.base import (  # noqa: F401
    INCOMPATIBLE, INSTALLABLE, NOT_CONFIGURED, READY_LOCAL, READY_REMOTE,
    UNAVAILABLE, STATUSES, EngineDescriptor, EngineStatus, VideoEngine,
    best_status,
)
from core.video_agent.engines.registry import (  # noqa: F401
    all_engines, discover, get, methods_for_spec, refresh, register,
    status_table, status_text,
)
