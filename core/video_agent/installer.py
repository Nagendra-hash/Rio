"""
core/video_agent/installer.py — Phase 4 (Universal Video Studio): the safe
installation manager for video tools and engines.

Mission rules encoded here:

  * detect existing installations first — never reinstall what works
    (ViMax is survey-only: the repo agent owns it)
  * every install produces a PLAN (download size, disk use, source, steps)
    BEFORE anything happens, and needs EXPLICIT approval (the Phase 6
    approval core, method INSTALL)
  * official/verified sources only; downloads stream into `.part` files
    renamed atomically, with hard size caps and cancel checks
  * model weights live OUTSIDE Jarvis source (~/JarvisTools/models/…)
  * verification BEFORE success: blender --version, module import, file
    signature — a failed verification is a FAILED install, never READY
  * system packages (ffmpeg) and drivers/CUDA/kernel are NEVER automated —
    plans say so honestly
  * incompatible engines get NO weight downloads: the plan offers the
    remote-inference configuration instead (Phase 11 enables local options
    automatically when compatible hardware appears)

Execution runs INSIDE the detached worker (kind `engine_install`) with
progress + cancel callbacks — cancellation removes `.part` files for clean
recovery.
"""
from __future__ import annotations

import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from core.video_agent.engines import registry as engine_registry

__all__ = ["survey", "plan", "start", "execute_install", "status",
           "install_job_params", "guarded_download"]

WEIGHTS_MODELS_DIR = Path.home() / "JarvisTools" / "models"


# ── survey: what is already installed? ───────────────────────────────────────

def _blender_state() -> dict[str, Any]:
    from core.video_agent.engines.blender_3d.engine import (_version_probe,
                                                            blender_bin)
    verified, detail = _version_probe()
    return {"installed": verified, "detail": detail, "path": blender_bin()}


def _ffmpeg_state() -> dict[str, Any]:
    binary = shutil.which("ffmpeg")
    version = ""
    if binary:
        try:
            proc = subprocess.run(["ffmpeg", "-version"],
                                  capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                version = (proc.stdout or "").splitlines()[0][:80]
        except (OSError, subprocess.SubprocessError):
            pass
    return {"installed": bool(binary), "detail": version or "not found",
            "path": binary or ""}


def _vimax_state() -> dict[str, Any]:
    from core.repo_agent import manifests
    reg = manifests.load_registry()
    repo = (reg.get("repos") or {}).get("vimax") or {}
    skill = (reg.get("skills") or {}).get("vimax-video") or {}
    return {"installed": bool(repo),
            "detail": f"repo {repo.get('status', 'absent')}, skill "
                      f"{skill.get('status', 'absent')}",
            "path": str(manifests.repo_dir("vimax"))}


def _weights_state(dirname: str) -> dict[str, Any]:
    path = WEIGHTS_MODELS_DIR / dirname
    present = path.is_dir() and any(path.iterdir())
    size_mb = 0.0
    if present:
        size_mb = sum(f.stat().st_size for f in path.rglob("*")
                      if f.is_file()) / (1024 * 1024)
    return {"installed": present,
            "detail": (f"{size_mb:.0f} MB at {path}" if present
                       else f"not downloaded ({path})"),
            "path": str(path)}


def _tts_state() -> dict[str, Any]:
    engines = []
    for name, module in (("gTTS", "gtts"), ("edge-tts", "edge_tts"),
                         ("kokoro", "kokoro"), ("miniaudio", "miniaudio")):
        from importlib.util import find_spec
        try:
            spec = find_spec(module)
        except (ImportError, ValueError):
            spec = None
        engines.append({"name": name, "installed": spec is not None})
    return {"installed": any(e["installed"] for e in engines),
            "detail": ", ".join(f"{e['name']} "
                                f"{'yes' if e['installed'] else 'no'}"
                                for e in engines),
            "engines": engines}


def _llm_state() -> dict[str, Any]:
    ollama = shutil.which("ollama")
    gguf = Path.home() / "Mark-III" / "models" / \
        "qwen2.5-coder-3b-instruct-q4_k_m.gguf"
    return {"installed": bool(ollama or gguf.is_file()),
            "detail": f"ollama: {'yes' if ollama else 'no'} · "
                      f"local 3B GGUF: {'yes' if gguf.is_file() else 'no'}"}


def survey() -> dict[str, Any]:
    """What is already installed (mission rule 1: detect first)."""
    from core.video_agent import hardware as hw_mod
    return {
        "BLENDER_3D": _blender_state(),
        "FFMPEG": _ffmpeg_state(),
        "VIMAX": _vimax_state(),
        "WAN_2_1": _weights_state("wan-2-1"),
        "LTX_VIDEO": _weights_state("ltx-video"),
        "TTS": _tts_state(),
        "LOCAL_LLM": _llm_state(),
        "remote_workers": len(hw_mod.remote_workers()),
        "surveyed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ── plans: what WOULD an install do? ─────────────────────────────────────────

def plan(engine_id: str) -> dict[str, Any]:
    """The installation plan for one engine: size, disk, source, steps,
    hardware verdict. Nothing is downloaded. Returns {"ok", "plan"/"error"}.
    """
    engine_id = str(engine_id or "").strip().upper()
    engine = engine_registry.get(engine_id)
    if engine is None:
        # survey-only tools (FFMPEG, LOCAL_LLM…) have honest plans without
        # being registry engines
        state = survey().get(engine_id)
        if state is not None:
            if state.get("installed"):
                return {"ok": True, "plan": None,
                        "message": f"{engine_id} is already installed "
                                   f"({state.get('detail', '')}) — nothing "
                                   "to install."}
            return {"ok": True, "plan": None,
                    "message": f"{engine_id} is not installed — "
                               + _alternative_text(engine_id)}
        return {"ok": False, "error": f"unknown engine '{engine_id}'"}
    d = engine.descriptor
    state = survey().get(engine_id) or {}
    base = engine.install_plan() if hasattr(engine, "install_plan") else None
    if base is None:
        if state.get("installed"):
            return {"ok": True, "plan": None,
                    "message": f"{d.name} is already installed "
                               f"({state.get('detail', '')}) — nothing to "
                               "install."}
        # no engine-level plan: for the weights engines an incompatible
        # machine still deserves the honest BLOCKED plan (no downloads,
        # remote path offered) instead of a bare "no installer"
        hw_ok, hw_reason = _hardware_verdict(engine)
        if engine_id in ("WAN_2_1", "LTX_VIDEO") and not hw_ok:
            base = {
                "engine": engine_id,
                "name": d.name,
                "needs_download": True,
                "source": d.install_source,
                "download_size_mb": d.install_size_mb,
                "disk_use_mb": round(d.install_size_mb * 1.2),
                "steps": [
                    "measure hardware again immediately before any download",
                    f"download weights into ~/JarvisTools/models/ "
                    f"(outside Jarvis source)",
                    "run a small compatibility render before marking READY",
                ],
            }
        else:
            return {"ok": True, "plan": None,
                    "message": f"{d.name} has no local installer — "
                               + _alternative_text(engine_id)}

    hw_ok, hw_reason = _hardware_verdict(engine)
    base.update({
        "engine": engine_id,
        "name": d.name,
        "estimated_cost": d.estimated_cost,
        "hardware": {"compatible": hw_ok, "reason": hw_reason},
        "weights_outside_source": engine_id in ("WAN_2_1", "LTX_VIDEO"),
        "approval_required": True,
        "approval": ("say 'install <engine>' — Jarvis asks for approval, "
                     "then queues a detached, cancellable job"),
    })
    if not hw_ok and engine_id in ("WAN_2_1", "LTX_VIDEO"):
        # mission rule: no large weight downloads for incompatible models
        base["blocked"] = True
        base["blocked_reason"] = (
            f"local weights download refused: {hw_reason}. "
            "Alternative: configure a remote GPU worker "
            "(repo_agent.video_remote_workers) — the model then runs "
            "provider-side without any local download.")
    return {"ok": True, "plan": base}


def _alternative_text(engine_id: str) -> str:
    return {
        "FFMPEG": ("ffmpeg is a SYSTEM package (apt) — Jarvis never runs "
                   "system package managers automatically. Install it "
                   "yourself; Jarvis will detect it on the next survey."),
        "VIMAX": ("ViMax installs through the repo agent: say 'install "
                  "https://github.com/HKUDS/ViMax' (inspect → risk scan → "
                  "uv-isolated install, approval-gated)."),
        "WAN_2_1": ("local inference needs compatible CUDA hardware; "
                    "configure a remote GPU worker instead."),
        "LTX_VIDEO": ("local inference needs compatible CUDA hardware; "
                      "configure a remote GPU worker instead."),
        "CUSTOM_VIDEO_MODEL": ("custom engines are registered as adapter "
                               "modules, not installed — see "
                               "engines/custom/engine.py."),
    }.get(engine_id, "no installer available for this engine.")


def _hardware_verdict(engine) -> tuple[bool, str]:
    from core.video_agent import hardware as hw_mod
    return hw_mod.classify_requirements(
        engine.descriptor.hardware_requirements or {}, None)


# ── starting an approved install (detached worker) ───────────────────────────

def install_job_params(engine_id: str) -> dict[str, Any] | None:
    """The params payload for the detached engine_install job."""
    engine_id = str(engine_id or "").strip().upper()
    result = plan(engine_id)
    if not result.get("ok"):
        return None
    plan_data = result.get("plan")
    if not plan_data or plan_data.get("blocked"):
        return None
    return {"engine_id": engine_id, "plan": plan_data}


def install_approval_spec(engine_id: str) -> dict[str, Any]:
    """The pseudo-spec the Phase 6 approval binds an INSTALL to. The engine
    id rides INSIDE the plan so the binding is engine-exact."""
    engine = engine_registry.get(str(engine_id or "").strip().upper())
    name = engine.descriptor.name if engine else str(engine_id)
    d = engine.descriptor if engine else None
    return {
        "mode": "INSTALL",
        "topic": f"Install {name} ({str(engine_id).upper()})",
        "engine_id": str(engine_id or "").strip().upper(),
        "source": (d.install_source if d else ""),
        "download_size_mb": (d.install_size_mb if d else 0.0),
    }


def start(engine_id: str, approve_token: str) -> dict[str, Any]:
    """Queue an APPROVED install as a detached, cancellable job. The token
    must be an APPROVED Phase 6 approval (method INSTALL) for exactly this
    engine."""
    from core.video_agent import approval as vappr
    from core.repo_agent.jobs import start_job
    engine_id = str(engine_id or "").strip().upper()
    params = install_job_params(engine_id)
    if params is None:
        result = plan(engine_id)
        reason = result.get("error") or result.get("message") or \
            ((result.get("plan") or {}).get("blocked_reason"))
        return {"ok": False, "error": reason or
                "no installable plan for this engine"}
    expected_key = vappr.project_key(install_approval_spec(engine_id))
    consumed = vappr.consume(approve_token, project_key_expected=expected_key)
    if not consumed.get("ok"):
        return {"ok": False, "error": consumed.get("error")}
    record = consumed.get("record") or {}
    approved_method = (record.get("approved") or {}).get("method")
    if approved_method != "INSTALL":
        return {"ok": False,
                "error": "approval was not an INSTALL approval — request "
                         "the installation plan again"}
    result = start_job("engine_install", engine_id, source="jarvis",
                       params=params)
    if not result.get("ok"):
        return {"ok": False, "error": f"could not queue the install: "
                                      f"{result.get('error')}"}
    job = result.get("job") or {}
    if result.get("deduplicated"):
        return {"ok": True, "deduplicated": True,
                "job_id": job.get("job_id"),
                "message": f"An identical install job is already active "
                           f"({job.get('job_id')}) — nothing duplicated."}
    return {"ok": True, "job_id": job.get("job_id"),
            "message": f"Install queued ({job.get('job_id')}) — detached "
                       "and cancellable; the engine goes READY only after "
                       "verification."}


def status(engine_id: str) -> dict[str, Any]:
    engine_id = str(engine_id or "").strip().upper()
    state = survey().get(engine_id) or {}
    engine = engine_registry.get(engine_id)
    return {"engine": engine_id, "install_state": state,
            "engine_status": engine.probe().as_dict() if engine else {}}


# ── execution (inside the detached worker) ───────────────────────────────────

def execute_install(engine_id: str, params: dict[str, Any] | None = None,
                    progress: Callable[[str, float, str], None] | None = None,
                    cancel: Callable[[], bool] | None = None,
                    ) -> dict[str, Any]:
    """Run an approved install. Called ONLY by the detached worker."""
    engine_id = str(engine_id or "").strip().upper()
    params = params or {}

    def _p(phase: str, pct: float, message: str = "") -> None:
        if progress:
            progress(phase, pct, message)

    def _cancelled() -> bool:
        return bool(cancel and cancel())

    _p("installing", 0.05, f"installing {engine_id}")

    if engine_id == "BLENDER_3D":
        from core.video_agent.engines import _installer
        result = _installer.extract_blender(
            log=lambda m: _p("installing", 0.5, m))
        if not result.get("ok"):
            return {"status": "FAILED",
                    "error": result.get("error", "extract failed")}
        if _cancelled():
            return {"status": "CANCELLED",
                    "message": "cancelled after extraction"}
        verified, detail = _verify_blender()
        if not verified:
            return {"status": "FAILED",
                    "error": f"verification failed: {detail}"}
        _p("verifying", 0.95, detail)
        return {"status": "COMPLETED", "message": detail,
                "installed_path": result.get("installed_path")}

    if engine_id == "TTS_KOKORO":
        return _install_kokoro(_p, _cancelled)

    if engine_id in ("WAN_2_1", "LTX_VIDEO"):
        ok, reason = _hardware_verdict_now(engine_id)
        if not ok:
            return {"status": "FAILED",
                    "error": f"hardware no longer compatible — weights not "
                             f"downloaded ({reason})"}
        return {"status": "FAILED",
                "error": "weight downloads are enabled by the Phase 11 "
                         "hardware-refresh flow with tested per-model "
                         "fetchers — nothing was downloaded"}

    if engine_id == "FFMPEG":
        return {"status": "FAILED",
                "error": "ffmpeg is a system package — Jarvis never runs "
                         "system package managers (install it manually)"}

    return {"status": "FAILED",
            "error": f"no executable installer for {engine_id} — "
                     f"{_alternative_text(engine_id)}"}


def _verify_blender() -> tuple[bool, str]:
    from core.video_agent.engines.blender_3d.engine import _version_probe
    return _version_probe()


def _hardware_verdict_now(engine_id: str) -> tuple[bool, str]:
    """Re-measured at EXECUTION time (mission: measure again before acting)."""
    engine = engine_registry.get(engine_id)
    if engine is None:
        return False, "unknown engine"
    from core.video_agent import hardware as hw_mod
    return hw_mod.classify_requirements(
        engine.descriptor.hardware_requirements or {},
        hw_mod.probe(force=True))


def _install_kokoro(_p, _cancelled) -> dict[str, Any]:
    """Local neural TTS into the Jarvis venv (approval-gated upstream)."""
    _p("installing", 0.2, "pip-installing kokoro into the Jarvis venv")
    pip = Path.home() / "Mark-III" / ".venv" / "bin" / "pip"
    if not pip.is_file():
        pip = shutil.which("pip3") or shutil.which("pip")
    if not pip:
        return {"status": "FAILED", "error": "no pip found in the venv"}
    try:
        proc = subprocess.run([str(pip), "install", "--quiet",
                               "kokoro>=0.9"], capture_output=True,
                              text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"status": "FAILED", "error": "pip install timed out"}
    if _cancelled():
        return {"status": "CANCELLED",
                "message": "cancelled — pip is transactional; the venv is "
                           "unchanged if the install did not finish"}
    if proc.returncode != 0:
        return {"status": "FAILED",
                "error": f"pip install failed: {(proc.stderr or '')[-300]}"}
    _p("verifying", 0.8, "importing kokoro")
    try:
        from importlib.util import find_spec
        if find_spec("kokoro") is None:
            raise ImportError("kokoro not importable after install")
    except ImportError as exc:
        return {"status": "FAILED", "error": f"verification failed: {exc}"}
    return {"status": "COMPLETED",
            "message": "kokoro installed — the ~330 MB model downloads on "
                       "first use (cached under ~/.cache/huggingface)",
            "note": "existing core/tts.py KokoroTTSEngine picks it up "
                    "automatically"}


# ── generic guarded download (used by engine installers) ─────────────────────

def guarded_download(url: str, dest: Path, *, limit_mb: int = 500,
                     progress: Callable[[float], None] | None = None,
                     cancel: Callable[[], bool] | None = None) -> None:
    """Stream url → dest with a hard size cap, .part atomicity and cancel
    checks. Raises on failure; removes the .part on any abort (clean
    recovery)."""
    part = dest.with_suffix(dest.suffix + ".part")
    limit = limit_mb * 1024 * 1024
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, \
                open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancel and cancel():
                    raise InterruptedError("cancelled by user")
                chunk = resp.read(1024 * 512)
                if not chunk:
                    break
                done += len(chunk)
                if done > limit:
                    raise ValueError(f"download exceeds {limit_mb} MB cap")
                out.write(chunk)
                if progress and total:
                    progress(min(0.99, done / total))
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
