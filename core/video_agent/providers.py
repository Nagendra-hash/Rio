"""
core/video_agent/providers.py — the VIDEO provider capability registry.

The mission asks for a capability registry with provider, model, task,
API compatibility, free/paid status, quota availability, health, hardware
requirements and last-successful-test. The LLM side of that ALREADY EXISTS:
`orchestrator/model_router/capabilities.py` (ModelSpec: cost tiers, privacy,
tasks) + `health.py` (ProviderHealth/HealthBoard) + `usage.py` (UsageJournal).
This module therefore COMPOSES the existing registry for the LLM roles and
adds only what it does not cover — the media providers (TTS, editor, video
generation) and the video-specific fields (hardware requirements, quota
notes, last probe results).

Honesty rules baked in:
  * entries are updated ONLY by real probes (`probe_all`); a provider is
    never marked free merely because its weights are open-source
  * video GENERATION providers are marked paid unless a genuinely free
    path is configured — as measured on 2026-10-24 both OpenRouter
    (402 never-purchased) and Google's free tier (429, limit: 0) refuse
    image/video models, so every video-generation rung is PAID/BLOCKED
  * local Wan/LTX video is BLOCKED on this hardware (GeForce 820M has no
    working driver — nvidia-smi fails), recorded as hardware_requirements

Probe results persist to `memory/video_providers.json`.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from core.repo_agent.jobs import db_path

VIDEO_ROLES = ("SCRIPT_WRITER", "RESEARCH", "VIDEO_GENERATOR",
               "VOICE_GENERATOR", "VIDEO_EDITOR")

def _registry_path() -> Path:
    # lazy: JARVIS_HOME can change after import (tests, portable mode) —
    # resolve at call time exactly like jobs.db_path()
    return db_path().parent / "video_providers.json"


def _load_probes() -> dict[str, Any]:
    try:
        return json.loads(_registry_path().read_text(encoding="utf-8"))
    except Exception:                              # noqa: BLE001
        return {}


def _save_probes(probes: dict[str, Any]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(probes, indent=1, default=str),
                    encoding="utf-8")


# ── media probes (cheap, no model downloads, no paid calls) ──────────────────

def probe_editor() -> dict[str, Any]:
    """VIDEO_EDITOR: system ffmpeg/ffprobe. Real probe, always safe."""
    entry: dict[str, Any] = {"provider": "ffmpeg", "model": "system ffmpeg",
                             "task": "video_editing",
                             "api_compat": "subprocess (argument arrays)",
                             "free_or_paid": "free (open source)",
                             "hardware_requirements": "CPU only",
                             "quota": "none"}
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not (ffmpeg and ffprobe):
        entry.update({"health": "FAIL", "detail": "ffmpeg/ffprobe not found"})
        return entry
    try:
        out = subprocess.run([ffmpeg, "-version"], capture_output=True,
                             text=True, timeout=15)
        version = (out.stdout or "").splitlines()[0][:80] if out.returncode \
            == 0 else "unknown"
        entry.update({"health": "OK", "detail": version})
    except Exception as exc:                       # noqa: BLE001
        entry.update({"health": "FAIL", "detail": f"{type(exc).__name__}"})
    # real VAAPI probe (cheap, cached) — hardware_requirements stays honest
    try:
        from core.video_agent.encoder import probe_vaapi
        probe = probe_vaapi()
        if probe["available"]:
            entry["hardware_requirements"] = (
                f"CPU + Intel VAAPI H.264 ({probe['device']}, driver "
                f"{probe['driver']} — verified by real encode; CPU libx264 "
                "is the automatic fallback)")
            entry["detail"] = f"{version} · VAAPI verified"
        else:
            entry["hardware_requirements"] = (
                f"CPU only (VAAPI unavailable: {probe['detail'][:80]})")
    except Exception:                              # noqa: BLE001
        pass
    entry["last_tested"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime())
    return entry


def probe_voice() -> dict[str, Any]:
    """VOICE_GENERATOR ladder — installed-ness only, NEVER triggers a model
    download (kokoro's ~330 MB fetch needs explicit user approval)."""
    entries: list[dict[str, Any]] = []

    # 1. gTTS — free cloud voice, no key; proven in actions/youtube_agent.py
    gtts = importlib.util.find_spec("gtts") is not None
    entries.append({
        "provider": "gtts", "model": "Google Translate voice",
        "task": "narration", "api_compat": "python lib",
        "free_or_paid": "free (cloud, no key)",
        "hardware_requirements": "network",
        "quota": "generous, undocumented",
        "health": "INSTALLED" if gtts else "NOT_INSTALLED",
        "detail": "proven path (youtube_agent narration)",
    })

    # 2. Edge TTS — free cloud; engine class exists, package missing today
    edge = importlib.util.find_spec("edge_tts") is not None
    entries.append({
        "provider": "edge-tts", "model": "Microsoft neural voices",
        "task": "narration", "api_compat": "python lib (EdgeTTSEngine)",
        "free_or_paid": "free (cloud)",
        "hardware_requirements": "network",
        "quota": "undocumented",
        "health": "INSTALLED" if edge else "NOT_INSTALLED",
        "detail": "engine class present in core/tts.py",
    })

    # 3. Kokoro — the house LOCAL neural TTS; deps + ~330 MB model missing
    kokoro = importlib.util.find_spec("kokoro") is not None
    entries.append({
        "provider": "kokoro", "model": "Kokoro-82M (offline)",
        "task": "narration", "api_compat": "python lib (KokoroTTSEngine)",
        "free_or_paid": "free (open source, offline after download)",
        "hardware_requirements": "CPU ok (2C/4T fine, ~330 MB model download)",
        "quota": "none",
        "health": "INSTALLED" if kokoro else "BLOCKED (needs install approval)",
        "detail": ("engine + wav writer ready in core/tts.py; pip install "
                   "kokoro>=0.9 soundfile, model downloads on first use"),
    })

    # 4. Piper — not installed, not the house engine; recorded for the mission
    piper = shutil.which("piper") is not None
    entries.append({
        "provider": "piper", "model": "Piper TTS (offline)",
        "task": "narration", "api_compat": "CLI",
        "free_or_paid": "free (open source, offline)",
        "hardware_requirements": "CPU ok",
        "quota": "none",
        "health": "INSTALLED" if piper else "NOT_INSTALLED",
        "detail": "mission-named; would be added as a CLI adapter",
    })
    for e in entries:
        e["last_tested"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {"role": "VOICE_GENERATOR", "providers": entries,
            "available": gtts or edge or kokoro or piper}


def probe_video_generator() -> dict[str, Any]:
    """VIDEO_GENERATOR — honest paid/blocked status. NO network calls, NO
    paid calls: the 2026-10-24 live smoke tests already measured these
    providers (VIMAX_INTEGRATION_REPORT.md §4a) and their verdicts are
    recorded as quota evidence."""
    gpu_ok = False
    try:
        gpu_ok = subprocess.run(["nvidia-smi"], capture_output=True,
                                timeout=10).returncode == 0
    except Exception:                              # noqa: BLE001
        gpu_ok = False

    entries: list[dict[str, Any]] = [
        {"provider": "openrouter (via ViMax)",
         "model": "google/veo-3.1-lite + openai/gpt-image-2",
         "task": "text-to-video / image gen", "api_compat": "ViMax adapter",
         "free_or_paid": "PAID",
         "hardware_requirements": "cloud",
         "quota": "402 — account never purchased credits (measured "
                  "2026-10-24)",
         "health": "BLOCKED (billing)",
         "detail": "LLM rung works free; image/video rungs need credits"},
        {"provider": "google gemini (via ViMax)",
         "model": "image: gemini-2.5-flash-image / video: NO Veo model "
                  "available to this key (ListModels 2026-10-25: zero veo "
                  "entries; the suggested gemini-omni-1.1-flash does not "
                  "support predictLongRunning)",
         "task": "image gen / text-to-video", "api_compat": "ViMax adapter",
         "free_or_paid": "PAID (free tier quota is 0)",
         "hardware_requirements": "cloud",
         "quota": "image 429 RESOURCE_EXHAUSTED limit:0 (measured "
                  "2026-10-24/25); video: no usable model on this key",
         "health": "BLOCKED (billing/account)",
         "detail": "keys wired via api_key_ref; works unchanged once "
                   "billing is enabled and a Veo model is granted"},
    ]
    if not gpu_ok:
        entries.append({
            "provider": "local wan / ltx", "model": "open-weight diffusion",
            "task": "text-to-video", "api_compat": "none configured",
            "free_or_paid": "free (open source) but unusable here",
            "hardware_requirements": "CUDA GPU (8-24 GB VRAM) — this laptop's "
                                     "GeForce 820M has NO working driver",
            "quota": "n/a",
            "health": "BLOCKED (hardware)",
            "detail": "nvidia-smi fails; local diffusion is not an option",
        })
    for e in entries:
        e["last_tested"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {"role": "VIDEO_GENERATOR", "providers": entries,
            "available": False}


def probe_llm_roles() -> dict[str, Any]:
    """SCRIPT_WRITER + RESEARCH: read the EXISTING orchestrator capability
    registry + health board — composed, never duplicated. No network."""
    entries: list[dict[str, Any]] = []
    try:
        from orchestrator.model_router.capabilities import (ModelTask,
                                                            Privacy)
        from orchestrator.model_router.router import ModelRouter
        router = ModelRouter()
        for role, task in (("SCRIPT_WRITER", ModelTask.RESEARCH),
                           ("RESEARCH", ModelTask.RESEARCH)):
            specs = router.registry.models_for(task, privacy=None)
            for spec in specs[:4]:                 # cheapest-capable first
                health = router.health.snapshot([spec.provider]) \
                    .get(spec.provider, {})
                entries.append({
                    "role": role,
                    "provider": spec.provider,
                    "model": spec.model_id,
                    "task": str(task.value),
                    "api_compat": "orchestrator-model-router",
                    "free_or_paid": ("local/free" if spec.cost_tier <= 1
                                     else "paid" if spec.cost_tier == 2
                                     else "gemini (budget-guarded)"),
                    "hardware_requirements": "cloud" if spec.privacy is Privacy.CLOUD
                                             else "local (RAM-guarded)",
                    "health": str(health.get("state") or "idle"),
                    "quota": "router-managed cooldowns + gemini daily budget",
                    "last_tested": health.get("since") or "",
                    "detail": f"registry name {spec.name}",
                })
    except Exception as exc:                       # noqa: BLE001
        entries.append({"role": "SCRIPT_WRITER", "provider": "orchestrator",
                        "model": "-", "task": "-",
                        "api_compat": "orchestrator-model-router",
                        "free_or_paid": "-",
                        "hardware_requirements": "-",
                        "quota": "-", "health": "FAIL",
                        "detail": f"registry read failed: "
                                  f"{type(exc).__name__}: {str(exc)[:120]}"})
    return {"role": "SCRIPT_WRITER+RESEARCH", "providers": entries,
            "available": any(e.get("health") not in ("FAIL",)
                             for e in entries)}


# ── public API ───────────────────────────────────────────────────────────────

def probe_all(run_llm_probe: bool = True) -> dict[str, Any]:
    """Run the cheap probes and persist results. `run_llm_probe=False`
    skips nothing today (LLM composition is offline) — the flag exists for
    a future live-generation probe that WOULD cost tokens; tests pass False
    to keep the run hermetic."""
    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "editor": probe_editor(),
        "voice": probe_voice(),
        "video_generator": probe_video_generator(),
    }
    if run_llm_probe:
        report["llm_roles"] = probe_llm_roles()
    probes = _load_probes()
    probes["last_probe"] = report
    _save_probes(probes)
    return report


def registry_table() -> str:
    """Human-readable capability table for voice/RIO ('which video providers
    are available?'). Composes live probes + persisted probe history."""
    cached = _load_probes().get("last_probe") or {}
    live = probe_all(run_llm_probe=True)
    lines = ["Video provider capability registry:", ""]

    llm = live.get("llm_roles") or cached.get("llm_roles") or {}
    rows = llm.get("providers") or []
    if rows:
        lines.append("SCRIPT_WRITER / RESEARCH (via the house model router, "
                     "free-first):")
        seen: set[tuple] = set()
        for r in rows:
            key = (r.get("provider"), r.get("model"), r.get("free_or_paid"))
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  • {r.get('provider')} / {r.get('model')} — "
                         f"{r.get('free_or_paid')}, health: "
                         f"{r.get('health')}")
        lines.append("")

    v = live.get("voice") or {}
    lines.append(f"VOICE_GENERATOR ({'available' if v.get('available') else 'NOT available'}):")
    for p in v.get("providers", []):
        lines.append(f"  • {p.get('provider')} — {p.get('free_or_paid')}, "
                     f"{p.get('health')}")
    lines.append("")

    e = live.get("editor") or {}
    lines.append(f"VIDEO_EDITOR: ffmpeg — {e.get('health')} "
                 f"({e.get('detail') or 'version unknown'})")
    lines.append("")

    g = live.get("video_generator") or {}
    lines.append("VIDEO_GENERATOR (honest status):")
    for p in g.get("providers", []):
        lines.append(f"  • {p.get('provider')} — {p.get('health')}: "
                     f"{p.get('quota')}")
    lines.append("")
    lines.append("No genuinely free AI video generation exists on this "
                 "machine today — documentary-style renders (still images, "
                 "narration, ffmpeg) are the zero-cost path, clearly labelled "
                 "as such, never passed off as AI-generated video.")
    return "\n".join(lines)
