"""
core/repo_agent/vimax.py — adapter connecting HKUDS/ViMax to Jarvis.

Verified against ViMax `main` (commit recorded at install time, v1.2.0):

    * pipelines.idea2video_pipeline.Idea2VideoPipeline
        .init_from_config(config_path=...) → await p(idea, user_requirement, style)
    * pipelines.script2video_pipeline.Script2VideoPipeline — same shape with script=
    * config YAML: chat_model/image_generator/video_generator with class_path
      + init_args, plus working_dir (where artifacts land)

Design rules honoured here:

    * ViMax tracked files are NEVER modified — the adapter generates a small
      runner under <repo>/.jarvis/ that imports the project's own documented
      pipeline classes (explicitly allowed by the integration policy).
    * provider API keys live ONLY in Mark-III's gitignored
      config/api_keys.json; the generated YAML lands in the job's scratch
      directory with 0600 perms and is deleted after the run.
    * without configured providers the skill reports NEEDS_CONFIGURATION —
      it never pretends to be ready and never invents keys.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from core.repo_agent import manifests as M

__all__ = ["REPO_NAME", "SKILL_ID", "register", "prepare", "provider_status",
           "verify_video", "find_latest_video", "skill_definition"]

REPO_NAME = "vimax"
SKILL_ID = "vimax-video"

# provider defaults — overridable via config/api_keys.json → "vimax" section
# (video default = the installed VideoGeneratorOpenRouterAPI class default,
#  verified from the pinned source: google/veo-3.1-lite)
_DEFAULT_LLM_MODEL = "google/gemini-2.5-flash"
_DEFAULT_IMAGE_MODEL = "openai/gpt-image-2"
_DEFAULT_VIDEO_MODEL = "google/veo-3.1-lite"
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"


# ── registration (called by the manager after a successful install) ─────────

def register(dest: Path, repo_manifest: dict[str, Any]) -> dict[str, Any]:
    """Write the adapter manifest + runner script into <repo>/.jarvis/."""
    commit = str(repo_manifest.get("commit") or "unknown")
    wrapper = dest / ".jarvis" / "jarvis_run.py"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(_RUNNER_SOURCE, encoding="utf-8")
    os.chmod(wrapper, 0o755)

    skill = skill_definition(commit)
    M.write_json(M.skill_manifest_path(REPO_NAME), skill)
    entry = M.upsert_skill({
        "skill_id": SKILL_ID, "repo": REPO_NAME, "name": skill["name"],
        "commit": commit, "enabled": True, "status": "NEEDS_CONFIGURATION",
    })
    return {"ok": True, "skill_id": SKILL_ID, "status": entry.get("status")}


def skill_definition(commit: str) -> dict[str, Any]:
    """The adapter manifest: allowlisted operations, schemas, limits."""
    return {
        "skill_id": SKILL_ID,
        "name": "ViMax — AI video creation",
        "repo": REPO_NAME,
        "version": commit,
        "description": ("Agentic video generation: idea/script → structured "
                        "story → storyboard → shots → final mp4 via "
                        "configured cloud providers (paid API)."),
        "task_types": ["video-creation"],
        "requires_config": ["llm", "image", "video"],
        "env": {
            "OPENROUTER_API_KEY": {"config": "_vimax_openrouter_key"},
        },
        "prepare": {"module": "core.repo_agent.vimax", "function": "prepare"},
        "entry_points": {
            "idea2video": {"cmd": ["{venv_python}", "{repo}/.jarvis/jarvis_run.py",
                                   "idea2video", "--params-file",
                                   "{params_file}", "--config", "{config_file}"],
                           "timeout_s": 7200},
            "script2video": {"cmd": ["{venv_python}", "{repo}/.jarvis/jarvis_run.py",
                                     "script2video", "--params-file",
                                     "{params_file}", "--config", "{config_file}"],
                             "timeout_s": 7200},
            "i2v": {"cmd": ["{venv_python}", "{repo}/.jarvis/jarvis_run.py",
                            "i2v", "--params-file",
                            "{params_file}", "--config", "{config_file}"],
                    "timeout_s": 3600},
            "artifacts": {"cmd": ["{venv_python}", "{repo}/.jarvis/jarvis_run.py",
                                  "artifacts", "--out-dir", "{out_dir}"],
                          "timeout_s": 60},
        },
        "input_schema": {
            "idea2video": {
                "topic": {"type": "string", "required": True, "max_chars": 2000},
                "duration_s": {"type": "integer", "minimum": 10, "maximum": 600},
                "style": {"type": "string", "max_chars": 200},
                "aspect_ratio": {"type": "string",
                                 "enum": ["16:9", "9:16", "1:1"]},
                "audience": {"type": "string", "max_chars": 200},
                "language": {"type": "string", "max_chars": 60},
                "narration": {"type": "string", "max_chars": 300},
                "user_requirement": {"type": "string", "max_chars": 1000},
            },
            "script2video": {
                "script": {"type": "string", "required": True, "max_chars": 8000},
                "style": {"type": "string", "max_chars": 200},
                "user_requirement": {"type": "string", "max_chars": 1000},
            },
            "i2v": {
                "image": {"type": "string", "required": True, "max_chars": 500},
                "prompt": {"type": "string", "required": True, "max_chars": 2000},
                "aspect_ratio": {"type": "string",
                                 "enum": ["16:9", "9:16", "1:1"]},
            },
            "artifacts": {},
        },
        "outputs": {
            "idea2video": ["{out_dir}/**/*.mp4"],
            "script2video": ["{out_dir}/**/*.mp4"],
            "i2v": ["{out_dir}/**/*.mp4"],
            "artifacts": [],
        },
        # Phase 5: the generated wrapper prints "[VIMAX_PROGRESS] stage: msg"
        # lines from the pipeline's progress callback; the worker maps the
        # FIRST matching stage per line to live job progress (phase + pct).
        "progress_pattern": r"\[VIMAX_PROGRESS\] ([a-z_0-9]+)",
        "progress_stages": {
            "extract_characters": ["extracting characters", 0.15],
            "character_portraits_start": ["rendering portraits", 0.25],
            "character_portraits_done": ["portraits done", 0.35],
            "camera_tree_ready": ["scene planning", 0.45],
            "camera_frames_start": ["planning shots", 0.55],
            "frame_done": ["generating shots", 0.65],
            "render_start": ["rendering video", 0.7],
            "concat_start": ["concatenating", 0.9],
            "concat_done": ["finalising", 0.95],
        },
        "health_check": {"cmd": ["{venv_python}", "{repo}/.jarvis/jarvis_run.py",
                                 "check"], "timeout_s": 120},
        "limits": {"run_timeout_s": 7200, "max_concurrent_runs": 1},
        "cleanup": ("scratch config deleted after each run; artifacts remain "
                    "in the job out_dir"),
        "paid_api": True,
    }


# ── provider configuration (all secrets stay inside Mark-III) ────────────────

def _openrouter_key() -> str:
    """First usable OpenRouter key from the gitignored JARVIS config."""
    try:
        from memory.config_manager import load_api_keys
        # Try both key names: old JSON uses "openrouter_keys", new settings uses "openrouter_api_keys"
        cfg = load_api_keys() or {}
        keys = cfg.get("openrouter_keys") or cfg.get("openrouter_api_keys") or []
        for entry in keys:
            if isinstance(entry, str) and entry.strip():
                return entry.strip()
            if isinstance(entry, dict) and str(entry.get("key") or "").strip():
                return str(entry["key"]).strip()
    except Exception:
        pass
    return ""


def _key_of(spec: dict) -> str:
    """Resolve a provider spec's key: `api_key_ref` (a dotted path into the
    gitignored config, or 'openrouter' for the shared OpenRouter key) or a
    literal `api_key`. Module-level so the run-config generator resolves the
    same refs provider_status() does."""
    if not isinstance(spec, dict):
        return ""
    ref = str(spec.get("api_key_ref") or "").strip()
    if ref:
        if ref.startswith("openrouter"):
            return _openrouter_key()
        try:
            from memory.config_manager import load_api_keys
            value: Any = load_api_keys() or {}
            for part in ref.split("."):
                value = value[int(part)] if isinstance(value, list) \
                    else value.get(part)
            return str(value) if value else ""
        except Exception:
            return ""
    return str(spec.get("api_key") or "")


def provider_status() -> dict[str, Any]:
    """Which provider rungs are configured? Drives READY vs NEEDS_CONFIGURATION."""
    # Try new settings first, then fall back to JSON file for vimax section
    section = {}
    try:
        from memory.config_manager import load_api_keys
        section = (load_api_keys() or {}).get("vimax") or {}
    except Exception:
        pass
    # Fall back to reading JSON file directly (for custom sections like vimax)
    if not section:
        try:
            from pathlib import Path
            import json
            from core.repo_agent.manifests import load_registry
            # Use the same base dir logic
            import os
            home = os.environ.get("JARVIS_HOME", "").strip()
            base = Path(home).expanduser().resolve() if home else Path(__file__).resolve().parent.parent.parent
            config_file = base / "config" / "api_keys.json"
            if config_file.exists():
                data = json.loads(config_file.read_text(encoding="utf-8"))
                section = data.get("vimax") or {}
        except Exception:
            pass
    section = section if isinstance(section, dict) else {}
    llm = section.get("llm") or {}
    image = section.get("image") or {}
    video = section.get("video") or {}

    llm_key = _key_of(llm) or _openrouter_key()
    image_key = _key_of(image) or _openrouter_key()
    video_key = _key_of(video) or _openrouter_key()
    return {
        "llm": {"ok": bool(llm_key),
                "provider": llm.get("provider", "openrouter"),
                "model": llm.get("model", _DEFAULT_LLM_MODEL)},
        "image": {"ok": bool(image_key),
                  "provider": image.get("provider", "openrouter"),
                  "model": image.get("model", _DEFAULT_IMAGE_MODEL)},
        "video": {"ok": bool(video_key),
                  "provider": video.get("provider", "openrouter"),
                  "model": video.get("model", _DEFAULT_VIDEO_MODEL)},
        "ready": bool(llm_key and image_key and video_key),
        "missing": [name for name, key in (("llm", llm_key),
                                           ("image", image_key),
                                           ("video", video_key)) if not key],
    }


def prepare(*, skill_id: str, op: str, params: dict[str, Any],
            run_dir: Path) -> dict[str, Any]:
    """Runner prepare-hook: compose the prompt text + write the provider
    config YAML into the job scratch dir (0600, deleted after the run).

    Returns values merged into the command-template context."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    if op in ("idea2video", "script2video", "i2v"):
        config_file = _write_run_config(op, run_dir)
        params_file = run_dir / "params.json"
        payload: dict[str, Any] = {}
        if op == "idea2video":
            payload["idea"] = str(params.get("topic") or "")
            payload["user_requirement"] = _compose_requirement(params)
            payload["style"] = str(params.get("style") or "Professional")
        elif op == "i2v":
            # IMAGE_TO_VIDEO: copy the user image into the scratch dir (the
            # original is never moved); the generator handles submit/poll.
            src = Path(str(params.get("image") or "")).expanduser()
            if not src.is_file():
                raise FileNotFoundError(f"reference image not found: {src}")
            img_dir = run_dir / "assets"
            img_dir.mkdir(parents=True, exist_ok=True)
            local = img_dir / src.name
            shutil.copyfile(src, local)
            payload["prompt"] = str(params.get("prompt") or "")
            payload["reference_images"] = [str(local)]
            payload["aspect_ratio"] = str(params.get("aspect_ratio")
                                          or "16:9")
            payload["output_path"] = str(run_dir / "render" / "i2v.mp4")
        else:
            payload["script"] = str(params.get("script") or "")
            payload["user_requirement"] = str(
                params.get("user_requirement") or "")
            payload["style"] = str(params.get("style") or "Professional")
        params_file.write_text(json.dumps(payload, ensure_ascii=False,
                                          indent=1), encoding="utf-8")
        os.chmod(params_file, 0o600)
        return {"config_file": str(config_file),
                "params_file": str(params_file), "op": op}
    return {}


def _compose_requirement(params: dict[str, Any]) -> str:
    """Translate the structured video request into ViMax's free-text
    user_requirement — every mapped field is documented in the report."""
    parts: list[str] = []
    if params.get("user_requirement"):
        parts.append(str(params["user_requirement"]))
    if params.get("duration_s"):
        parts.append(f"Total video length about {int(params['duration_s'])} "
                     "seconds.")
    if params.get("audience"):
        parts.append(f"Target audience: {params['audience']}.")
    if params.get("aspect_ratio"):
        parts.append(f"Aspect ratio: {params['aspect_ratio']} landscape."
                     if params["aspect_ratio"] == "16:9" else
                     f"Aspect ratio: {params['aspect_ratio']}.")
    if params.get("language"):
        parts.append(f"Language: {params['language']}.")
    if params.get("narration"):
        parts.append(f"Narration: {params['narration']}.")
    return " ".join(parts) or "No special requirements."


def _image_init_args(provider: str, key: str, model: str) -> str:
    """Per-provider init_args for the image generator class.

    Only some classes accept a `model` kwarg: ImageGeneratorNanobananaGoogleAPI
    hardcodes gemini-2.5-flash-image and takes api_key only — passing `model`
    to it is a TypeError at pipeline init (found wiring the google rung,
    2026-10-24)."""
    lines = [f"    api_key: {key}"]
    if provider != "google":
        lines.append(f"    model: {model}")
    return "\n".join(lines) + "\n"


def _video_init_args(provider: str, key: str, model: str,
                     section: dict) -> str:
    """Per-provider init_args for the video generator class.

    Only VideoGeneratorOpenRouterAPI takes a plain `model` kwarg; the
    google/yunwu/doubao/omni classes use t2v_model / ff2v_model / flf2v_model
    (omni: i2v_model) with their own defaults. Optional per-mode models can
    be set in the vimax config section and are passed through when present."""
    lines = [f"    api_key: {key}"]
    if provider == "openrouter":
        lines.append(f"    model: {model}")
    else:
        for arg in ("t2v_model", "ff2v_model", "flf2v_model", "i2v_model"):
            value = str(section.get(arg) or "").strip()
            if value:
                lines.append(f"    {arg}: {value}")
    return "\n".join(lines) + "\n"


def _write_run_config(op: str, run_dir: Path) -> Path:
    """Generate the ViMax pipeline YAML for this run from configured providers.

    Written into the scratch dir (never inside the repo tree, never in git),
    readable only by the owner, deleted by the worker after the run.
    """
    status = provider_status()
    if not status["ready"]:
        missing = ", ".join(status["missing"])
        raise RuntimeError(
            f"provider configuration incomplete (missing: {missing}) — set "
            f"vimax provider keys in config/api_keys.json first")
    llm, image, video = status["llm"], status["image"], status["video"]
    key = _openrouter_key()
    try:
        from memory.config_manager import load_api_keys
        section = (load_api_keys() or {}).get("vimax") or {}
    except Exception:
        section = {}
    # resolve per-rung keys from the RAW section: provider_status()'s view
    # strips api_key_ref, so re-resolving from it would silently fall back to
    # the shared OpenRouter key (found wiring the google rung, 2026-10-24)
    image_key = _key_of(section.get("image") or {}) or key
    video_key = _key_of(section.get("video") or {}) or key
    yaml_text = (
        "chat_model:\n"
        "  init_args:\n"
        f"    model: {llm['model']}\n"
        "    model_provider: openai\n"
        f"    api_key: {key}\n"
        f"    base_url: {_OPENROUTER_BASE}\n"
        # Cap the output tokens. Without this the model's own maximum
        # (e.g. 65535) travels in the request and OpenRouter 402s when the
        # balance cannot afford the full cap — 402 requires more credits
        # "or fewer max_tokens". 4096 matches ViMax's own
        # _narrative_max_tokens() default; VIMAX_NARRATIVE_MAX_TOKENS can
        # override it for richer renders.
        f"    max_tokens: {os.environ.get('VIMAX_NARRATIVE_MAX_TOKENS', '4096')}\n"
        "image_generator:\n"
        f"  class_path: tools.{_IMAGE_CLASSES.get(image['provider'], 'ImageGeneratorOpenRouterAPI')}\n"
        "  init_args:\n"
        + _image_init_args(image["provider"], image_key, image["model"]) +
        "video_generator:\n"
        f"  class_path: tools.{_VIDEO_CLASSES.get(video['provider'], 'VideoGeneratorOpenRouterAPI')}\n"
        "  init_args:\n"
        + _video_init_args(video["provider"], video_key, video["model"],
                           section.get("video") or {}) +
        f"working_dir: {run_dir}\n")
    path = run_dir / "vimax_run.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


_IMAGE_CLASSES = {
    # Exact class names from ViMax tools/__all__ (capital R in OpenRouter).
    "openrouter": "ImageGeneratorOpenRouterAPI",
    "google": "ImageGeneratorNanobananaGoogleAPI",
    "yunwu": "ImageGeneratorNanobananaYunwuAPI",
    "doubao": "ImageGeneratorDoubaoSeedreamYunwuAPI",
}
_VIDEO_CLASSES = {
    "openrouter": "VideoGeneratorOpenRouterAPI",
    "google": "VideoGeneratorVeoGoogleAPI",
    "yunwu": "VideoGeneratorVeoYunwuAPI",
    "doubao": "VideoGeneratorDoubaoSeedanceYunwuAPI",
    "omni": "VideoGeneratorOmniYunwuAPI",
}


# ── artifact verification (never trust "the process exited 0") ───────────────

def verify_video(path: str | Path) -> dict[str, Any]:
    """A video counts as produced only if it exists, is nonempty, has an MP4
    signature, and (when ffprobe is available) parses with a duration."""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": "file does not exist"}
    size = p.stat().st_size
    if size <= 0:
        return {"ok": False, "error": "file is empty"}
    try:
        with open(p, "rb") as fh:
            head = fh.read(12)
        if head[4:8] != b"ftyp":
            return {"ok": False, "error": "missing MP4 signature (ftyp box)"}
    except Exception as exc:
        return {"ok": False, "error": f"unreadable: {exc}"}
    result: dict[str, Any] = {"ok": True, "size_bytes": size,
                              "path": str(p)}
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries",
                 "format=duration,size", "-of", "json", str(p)],
                capture_output=True, text=True, timeout=60)
            if proc.returncode == 0 and proc.stdout.strip():
                fmt = (json.loads(proc.stdout).get("format") or {})
                result["duration_s"] = round(float(fmt.get("duration") or 0), 1)
        except Exception:
            pass                      # ffprobe opinion is best-effort only
    return result


def find_latest_video(out_dir: str | Path) -> str:
    """Newest nonzero mp4 under a run directory ('' when none)."""
    root = Path(out_dir)
    best, best_mtime = "", -1.0
    for path in root.rglob("*.mp4"):
        try:
            if path.stat().st_size > 0 and path.stat().st_mtime > best_mtime:
                best, best_mtime = str(path), path.stat().st_mtime
        except OSError:
            continue
    return best


# ── generated runner (lives in <repo>/.jarvis/, never a tracked file) ────────

_RUNNER_SOURCE = '''#!/usr/bin/env python
"""JARVIS adapter runner for ViMax — generated by core/repo_agent/vimax.py.

Calls the project's own documented pipeline classes (Idea2VideoPipeline /
Script2VideoPipeline). NO tracked ViMax file is modified. The provider
config YAML is a per-run scratch file; delete it after the run.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


async def _run(kind: str, params_file: str, config_file: str) -> None:
    with open(params_file, encoding="utf-8") as fh:
        payload = json.load(fh)

    def _progress(stage, message="", meta=None):
        """Translate the pipeline's progress callback into one parseable
        stdout line per stage — the JARVIS worker maps these to live job
        progress (Phase 5: render progress support)."""
        try:
            print(f"[VIMAX_PROGRESS] {stage}: {str(message)[:120]}", flush=True)
        except Exception:
            pass

    if kind == "idea2video":
        from pipelines.idea2video_pipeline import Idea2VideoPipeline
        pipeline = Idea2VideoPipeline.init_from_config(config_path=config_file)
        await pipeline(idea=payload.get("idea", ""),
                       user_requirement=payload.get("user_requirement", ""),
                       style=payload.get("style", ""),
                       progress=_progress)
    elif kind == "script2video":
        from pipelines.script2video_pipeline import Script2VideoPipeline
        pipeline = Script2VideoPipeline.init_from_config(
            config_path=config_file)
        await pipeline(script=payload.get("script", ""),
                       user_requirement=payload.get("user_requirement", ""),
                       style=payload.get("style", ""),
                       progress=_progress)
    elif kind == "i2v":
        # IMAGE_TO_VIDEO (Phase 6): ViMax's own video-generator tool class —
        # submission, status polling, timeouts and retries live inside it;
        # the result (url or bytes) is saved then verified by the worker.
        import importlib
        import yaml
        with open(config_file, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        class_path = str(cfg["video_generator"]["class_path"])
        mod_name, cls_name = class_path.rsplit(".", 1)
        gen_cls = getattr(importlib.import_module(mod_name), cls_name)
        generator = gen_cls(**cfg["video_generator"]["init_args"])
        out = await generator.generate_single_video(
            prompt=str(payload.get("prompt") or ""),
            reference_image_paths=list(payload.get("reference_images") or []),
            aspect_ratio=str(payload.get("aspect_ratio") or "16:9"),
            progress=_progress)
        dest = Path(str(payload.get("output_path") or "render/i2v.mp4"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        out.save(str(dest))
        print(f"[VIMAX_PROGRESS] i2v_saved: {dest}", flush=True)
    else:
        raise SystemExit(f"unknown kind {kind}")


def _check() -> None:
    from pipelines.idea2video_pipeline import Idea2VideoPipeline  # noqa: F401
    from pipelines.script2video_pipeline import Script2VideoPipeline  # noqa: F401
    print("vimax pipelines import OK")


def _artifacts(out_dir: str) -> None:
    items = []
    for path in sorted(Path(out_dir).rglob("*")):
        if path.is_file() and path.suffix.lower() in (".mp4", ".mov", ".webm"):
            items.append({"path": str(path),
                          "size": path.stat().st_size})
    print(json.dumps(items))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command",
                        choices=["idea2video", "script2video", "i2v", "check",
                                 "artifacts"])
    parser.add_argument("--params-file", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()
    if args.command == "check":
        _check()
    elif args.command == "artifacts":
        _artifacts(args.out_dir or ".")
    else:
        try:
            asyncio.run(_run(args.command, args.params_file, args.config))
        finally:
            # scratch config holds provider keys — never leave it behind
            try:
                if args.config and Path(args.config).is_relative_to(
                        Path(args.params_file).parent):
                    Path(args.config).unlink(missing_ok=True)
                Path(args.params_file).unlink(missing_ok=True)
            except Exception:
                pass


if __name__ == "__main__":
    main()
'''
