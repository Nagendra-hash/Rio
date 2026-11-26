"""
actions/engine_installer.py — Phase 4 voice surface: the safe installation
manager.

'Show my installed video tools'      → survey (detect first, never reinstall)
'What does installing Blender need?' → plan (size, disk, source — nothing runs)
'Install Blender'                    → plan + approval token, then a detached
                                       cancellable job after approval
'Install Wan 2.1'                    → honest refusal on this hardware (no
                                       CUDA driver) + remote-worker guidance

Every install is approval-gated through the Phase 6 approval core (method
INSTALL, single-use token bound to the exact engine). Nothing is downloaded
or installed without it. System packages (ffmpeg) and drivers/CUDA/kernel
are NEVER automated.
"""
from __future__ import annotations

from core.video_agent import approval as vappr
from core.video_agent import installer

_METHOD_INSTALL = [{
    "method_id": "INSTALL",
    "name": "Install",
    "status": "READY_LOCAL",
    "detail": "detached, cancellable install job; verified before READY",
    "execution": ["local"],
    "estimated_cost": "free (download only)",
    "privacy": "downloads from the official source only",
    "resolutions": ["n/a"],
    "duration_range_s": [0, 0],
    "recommended": True,
}]


def engine_installer(parameters: dict, player=None, speak=None) -> str:
    parameters = parameters or {}
    action = str(parameters.get("action") or "survey").strip().lower()
    engine_id = str(parameters.get("engine_id") or "").strip()
    try:
        if action == "survey":
            return _survey_text()
        if action == "check":
            # Phase 7: verify a remote GPU worker on demand (never automatic)
            from core.video_agent import remote as remote_mod
            if not engine_id:
                return remote_mod.state_text()
            result = remote_mod.check(engine_id)
            if result.get("ok"):
                return (f"Remote worker '{engine_id}' verified: "
                        f"{result.get('detail')}. Wan 2.1 / LTX-Video can "
                        "now use it for remote inference (still "
                        "approval-gated per video).")
            return (f"Refused: {result.get('error', 'probe failed')}. "
                    "The worker stays unverified.")
        if action == "test":
            # Phase 11: a small REAL compatibility test — an engine is
            # enabled only when its backend verifies
            if not engine_id:
                return ("Which engine should I test? e.g. 'test Blender' — "
                        "known ids: BLENDER_3D, CANVAS, WAN_2_1, "
                        "LTX_VIDEO, TTS_KOKORO, VIMAX.")
            from core.video_agent.refresh import compat_test_text
            return compat_test_text(engine_id)
        if action == "plan":
            if not engine_id:
                return ("Which engine should I plan an install for? "
                        "Blender 3D, TTS Kokoro, Wan 2.1, LTX-Video, ViMax "
                        "or Stock footage.")
            return _plan_text(engine_id)
        if action == "install":
            if not engine_id:
                return ("Which engine should I install? Say e.g. 'install "
                        "Blender' or 'install Kokoro TTS'.")
            return _install(engine_id,
                            str(parameters.get("approve_token") or ""))
        if action == "status":
            return _status_text(engine_id)
        return ("Unknown installer action. Use survey, check, plan, "
                "install or status.")
    except ValueError as exc:
        return f"Refused: {exc}. Nothing was installed."
    except Exception as exc:                        # noqa: BLE001
        return (f"The installer is unavailable: {type(exc).__name__}: "
                f"{str(exc)[:180]}. Nothing was downloaded or installed.")


def _survey_text() -> str:
    data = installer.survey()
    lines = ["Installed video tools (detected, not assumed):"]
    for key in ("BLENDER_3D", "FFMPEG", "VIMAX", "WAN_2_1", "LTX_VIDEO",
                "TTS", "LOCAL_LLM"):
        state = data.get(key) or {}
        mark = "installed" if state.get("installed") else "not installed"
        lines.append(f"• {key}: {mark} — {str(state.get('detail', ''))[:100]}")
    lines.append(f"• Remote GPU workers: {data.get('remote_workers', 0)} "
                 "configured")
    return "\n".join(lines)


def _plan_text(engine_id: str) -> str:
    result = installer.plan(engine_id)
    if not result.get("ok"):
        return f"Refused: {result.get('error')}."
    plan_data = result.get("plan")
    if plan_data is None:
        return result.get("message") or "Nothing to install."
    d = plan_data
    lines = [
        f"Installation plan — {d.get('name')} [{d.get('engine')}]:",
        f"• Source: {d.get('source') or 'see steps'}",
        f"• Download: {d.get('download_size_mb', 0)} MB · disk use: "
        f"{d.get('disk_use_mb', d.get('download_size_mb', 0))} MB",
        f"• Hardware: "
        f"{'compatible' if (d.get('hardware') or {}).get('compatible') else 'NOT compatible'}"
        + (f" — {d['hardware']['reason'][:120]}"
           if not (d.get("hardware") or {}).get("compatible") else ""),
    ]
    for i, step in enumerate(d.get("steps") or [], 1):
        lines.append(f"  {i}. {step}")
    if d.get("blocked"):
        lines.append(f"• BLOCKED: {d.get('blocked_reason', '')[:300]}")
        lines.append("Nothing was downloaded. The remote-GPU path needs no "
                     "local weights.")
    else:
        issued = vappr.issue(installer.install_approval_spec(engine_id),
                             _METHOD_INSTALL)
        rec = issued["record"]
        dedup = (" (your approval is already pending — same token)"
                 if issued.get("deduplicated") else "")
        lines += [
            f"Nothing has been downloaded or installed{dedup}.",
            f"Approve with token {rec['token']} — e.g. 'yes, install "
            f"{d.get('name')} — token {rec['token']}'. The install runs "
            "detached and cancellable, and is verified before READY.",
        ]
    return "\n".join(lines)


def _install(engine_id: str, approve_token: str) -> str:
    if not approve_token:
        return _plan_text(engine_id) + \
            "\n(Approval required before anything is downloaded — the plan " \
            "above includes your token.)"
    result = installer.start(engine_id, approve_token)
    if not result.get("ok"):
        return f"Refused: {result.get('error')}. Nothing was installed."
    return result.get("message") or f"Install queued ({result.get('job_id')})."


def _status_text(engine_id: str) -> str:
    if not engine_id:
        return ("Which engine? e.g. 'installer status for Blender'. "
                "Known ids: BLENDER_3D, VIMAX, WAN_2_1, LTX_VIDEO, "
                "TTS_KOKORO, FFMPEG.")
    data = installer.status(engine_id)
    engine_status = (data.get("engine_status") or {})
    install_state = (data.get("install_state") or {})
    return (f"{engine_id}: install={install_state.get('detail', '?')} · "
            f"engine status={engine_status.get('status', '?')} — "
            f"{engine_status.get('detail', '')[:160]}")


TOOL = {
    "name": "engine_installer",
    "description": (
        "The video-studio installation manager: detect what is already "
        "installed (Blender, ffmpeg, ViMax, Wan 2.1 / LTX-Video weights, "
        "TTS engines, local LLM, remote GPU workers), show an installation "
        "PLAN (download size, disk use, official source, hardware verdict) "
        "and run APPROVED installs as detached, cancellable jobs. Actions: "
        "survey ('show my installed video tools'), plan ('what does "
        "installing Blender need?'), install ('install Blender' — asks for "
        "approval first, token-gated), check ('check remote worker "
        "lab-gpu' — verifies a configured GPU worker on demand), test "
        "('test Blender' — a small REAL compatibility render; an engine is "
        "enabled only when its backend verifies), status ('installer "
        "status for Blender'). Never installs anything without "
        "explicit approval; never touches NVIDIA drivers, CUDA, the kernel "
        "or system packages; never downloads model weights for "
        "hardware-incompatible engines (offers remote-GPU configuration "
        "instead)."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("survey | plan | install | status"),
            },
            "engine_id": {
                "type": "STRING",
                "description": ("BLENDER_3D | VIMAX | WAN_2_1 | LTX_VIDEO | "
                                "STOCK_FOOTAGE | TTS_KOKORO | FFMPEG | "
                                "CANVAS | CUSTOM_VIDEO_MODEL"),
            },
            "approve_token": {
                "type": "STRING",
                "description": ("Approval token issued by a previous plan "
                                "call — REQUIRED for install; single-use, "
                                "bound to this engine."),
            },
        },
        "required": ["action"],
    },
    "handler": engine_installer,
}
