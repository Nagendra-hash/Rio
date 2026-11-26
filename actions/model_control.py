"""
actions/model_control.py — report and switch which model actually answers.

WHAT THE USER ASKS FOR
    "Are you using the local model?"
    "Use the local model."
    "Switch to cloud."
    "Use local for Hermes too."

JARVIS has two independent answers to "which model":
  * the CONVERSATION engine — Gemini Live (cloud) or the local pipeline
    (Whisper -> Ollama -> system voice), chosen by `provider_mode`;
  * the one-shot TEXT router mode — local / free / smart / gemini, used by
    background calls.
Hermes has one: `model.provider` + `model.default` in ~/.hermes/config.yaml.

This action reports both truthfully, then switches what it can and explains
what it could not. It never claims a switch happened without writing the
setting and (for JARVIS) asking the live session to rebuild itself.

VOICE PHRASES are wired in core/voice_router.py, and the same tool is declared
to the Live model, so both the online and offline paths reach this file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

_HOME = Path.home()
_HERMES = shutil.which("hermes") or str(_HOME / ".local" / "bin" / "hermes")
_HERMES_ENV = {
    "PATH": os.pathsep.join(
        [
            str(_HOME / ".local" / "bin"),
            str(_HOME / ".hermes" / "node" / "bin"),
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]
    ),
    "HOME": str(_HOME),
}

_OLLAMA_URL = "http://127.0.0.1:11434"
_LOCAL_LLM_ID = "openai_compat@ollama"
_LOCAL_STT_ID = "whisper@local"
_LOCAL_TTS_ID = "system_tts@system"
_PREFERRED_LOCAL = ("qwen2.5:1.5b", "qwen2.5-coder:3b", "llama3.2:3b", "llama3.2:1b")

_MODES = ("local", "free", "smart", "gemini")
_HERMES_TIMEOUT = 20


# ── paths / tiny file helpers ────────────────────────────────────────────────


def _base_dir() -> Path:
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _state_path() -> Path:
    return _base_dir() / "memory" / "model_control_state.json"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
    tmp.replace(path)


# ── JARVIS state ─────────────────────────────────────────────────────────────


def _router_mode() -> str:
    """The effective one-shot routing mode, from the same sources the router
    itself consults (env -> api_keys.json -> .env -> smart)."""
    for var in ("JARVIS_ROUTER_MODE", "JARVIS_LLM_ROUTER_MODE"):
        raw = (os.environ.get(var) or "").strip().lower()
        if raw in _MODES:
            return raw
    cfg = _read_json(_base_dir() / "config" / "api_keys.json")
    raw = str(cfg.get("router_mode") or "").strip().lower()
    if raw in _MODES:
        return raw
    try:
        from config.settings import get_api_config

        raw = str(get_api_config().get("router_mode") or "").strip().lower()
        if raw in _MODES:
            return raw
    except Exception:
        pass
    return "smart"


def _set_router_mode(mode: str) -> None:
    mode = mode if mode in _MODES else "smart"
    # The live process must see it now (both spellings in use across modules).
    os.environ["JARVIS_ROUTER_MODE"] = mode
    os.environ["JARVIS_LLM_ROUTER_MODE"] = mode
    # Persist for the next start: api_keys.json is what core/llm_router reads,
    # .env is the settings source of truth.
    cfg_path = _base_dir() / "config" / "api_keys.json"
    cfg = _read_json(cfg_path)
    cfg["router_mode"] = mode
    _write_json(cfg_path, cfg)
    try:
        from config.settings import reload_settings, save_api_config

        save_api_config({"router_mode": mode})
        reload_settings()
    except Exception:
        pass


def _ollama_models() -> list[str]:
    try:
        with urllib.request.urlopen(f"{_OLLAMA_URL}/api/tags", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8", "ignore") or "{}")
        return [str(m.get("name") or "") for m in (data.get("models") or []) if m.get("name")]
    except Exception:
        return []


def _preferred_local_model(models: list[str], requested: str = "") -> str:
    if requested and requested in models:
        return requested
    cfg = _read_json(_base_dir() / "config" / "api_keys.json")
    configured = cfg.get("ollama_models") or []
    if isinstance(configured, str):
        configured = [configured]
    for name in list(configured) + list(_PREFERRED_LOCAL):
        name = str(name or "")
        if name and name in models:
            return name
    return models[0] if models else ""


def _provider_is_local(pid: str) -> bool:
    """A provider is local when its catalog says so, or its address is this
    machine. Ids look like 'openai_compat@ollama'."""
    if not pid:
        return False
    base, _, entry = str(pid).partition("@")
    base = base.strip()
    entry = (entry or base).strip()
    if entry in ("ollama", "lmstudio", "llamacpp", "vllm", "jan", "custom"):
        # `custom` can point anywhere, so only trust the address below for it.
        try:
            from memory.config_manager import get_provider_config

            host = str((get_provider_config(pid) or {}).get("base_url") or "")
            if entry != "custom":
                return True
            return any(h in host for h in ("127.0.0.1", "localhost", "::1"))
        except Exception:
            return entry != "custom"
    try:
        from memory.config_manager import get_provider_config
        from providers import registry

        cat = registry.catalog_entry(pid) or {}
        if "local" in cat:
            return bool(cat.get("local"))
        host = str((get_provider_config(pid) or {}).get("base_url") or "")
        return any(h in host for h in ("127.0.0.1", "localhost", "::1"))
    except Exception:
        return False


def _jarvis_state() -> dict:
    try:
        from memory.config_manager import get_pipeline, get_provider_mode

        mode = str(get_provider_mode() or "gemini")
        pipe = dict(get_pipeline() or {})
    except Exception:
        mode, pipe = "gemini", {}
    llm = str(pipe.get("llm") or "")
    local = mode == "pipeline" and _provider_is_local(llm)
    return {
        "provider_mode": mode,
        "local": local,
        "llm": llm,
        "stt": str(pipe.get("stt") or ""),
        "tts": str(pipe.get("tts") or ""),
        "router_mode": _router_mode(),
        "ollama_models": _ollama_models(),
        "ollama": bool(_ollama_models()),
    }


def _gemini_available() -> bool:
    try:
        cfg = _read_json(_base_dir() / "config" / "api_keys.json")
        if str(cfg.get("gemini_api_key") or "").strip():
            return True
        from config.settings import get_api_config

        return bool(str(get_api_config().get("gemini_api_key") or "").strip())
    except Exception:
        return False


def _remember(key: str, value: dict) -> None:
    data = _read_json(_state_path())
    data[key] = value
    _write_json(_state_path(), data)


def _recall(key: str, default: dict | None = None) -> dict:
    value = _read_json(_state_path()).get(key)
    return dict(value) if isinstance(value, dict) else dict(default or {})


# ── Hermes state / switching ─────────────────────────────────────────────────


def _hermes_config_path() -> Path:
    return _HOME / ".hermes" / "config.yaml"


def _hermes_state() -> dict:
    """Read the two keys that decide Hermes' model. Never starts the CLI."""
    model = provider = ""
    router_models: list[str] = []
    base_url = ""
    path = _hermes_config_path()
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="ignore")) or {}
        m = data.get("model") or {}
        model = str(m.get("default") or "")
        provider = str(m.get("provider") or "")
        router = ((data.get("providers") or {}).get("router") or {})
        base_url = str(router.get("base_url") or "")
        router_models = [str(x) for x in (router.get("models") or [])]
    except Exception:
        # PyYAML missing or the file is mid-write: a line reader is enough for
        # these flat keys and cannot break the tool.
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            lines = []
        in_model = False
        for line in lines:
            if re.match(r"^model:\s*$", line):
                in_model = True
                continue
            if in_model and re.match(r"^\S", line):
                in_model = False
            m = re.match(r"^\s+(default|provider):\s*(.+?)\s*$", line)
            if in_model and m:
                if m.group(1) == "default":
                    model = m.group(2).strip("'\"")
                else:
                    provider = m.group(2).strip("'\"")
    prov = provider.strip().lower()
    local = prov in ("router", "ollama", "lmstudio", "llamacpp", "jan")
    if not prov:
        # No provider recorded: only then is the endpoint itself evidence.
        local = any(h in base_url for h in ("127.0.0.1", "localhost", "::1"))
    return {
        "model": model,
        "provider": provider,
        "local": local,
        "router_models": router_models,
        "base_url": base_url,
        "cli": Path(_HERMES).exists(),
        "gateway_running": _pid_alive(_read_json(_HOME / ".hermes" / "gateway.pid")),
    }


def _pid_alive(data: Any) -> bool:
    pid = data.get("pid") if isinstance(data, dict) else data
    try:
        pid = int(pid)
    except Exception:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _run_hermes(args: list[str], timeout: int = _HERMES_TIMEOUT) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [_HERMES, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_HERMES_ENV,
        )
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except FileNotFoundError:
        return 127, "Hermes CLI not found on this machine."
    except subprocess.TimeoutExpired:
        return 124, "Hermes took too long to answer."


def _hermes_set_model(provider: str, model: str) -> tuple[bool, str]:
    if not _hermes_state().get("cli"):
        return False, "Hermes is not installed here."
    if not provider or not model:
        return False, "A provider and a model are both needed for Hermes."
    rc1, out1 = _run_hermes(["config", "set", "model.provider", provider])
    if rc1 != 0:
        return False, f"Could not set Hermes' provider ({out1[:160]})."
    rc2, out2 = _run_hermes(["config", "set", "model.default", model])
    if rc2 != 0:
        return False, f"Could not set Hermes' model ({out2[:160]})."
    return True, ""


def _hermes_use_local(model: str = "") -> tuple[bool, str]:
    models = _ollama_models()
    if not models:
        return False, "Hermes was not switched: Ollama is not reachable on this machine."
    chosen = _preferred_local_model(models, model)
    current = _hermes_state()
    if not current.get("local"):
        _remember("hermes_cloud", {
            "provider": current.get("provider") or "",
            "model": current.get("model") or "",
        })
    # The local route is an OpenAI-compatible endpoint, which is what the
    # pre-configured `router` provider already is. Point it at Ollama if the
    # user's config does not have that address yet.
    if not current.get("base_url"):
        _run_hermes(["config", "set", "providers.router.base_url", f"{_OLLAMA_URL}/v1"])
        _run_hermes(["config", "set", "providers.router.api_key", "no-key-needed"])
    ok, err = _hermes_set_model("router", chosen)
    if not ok:
        return False, f"Hermes was not switched: {err}"
    return True, f"Hermes is now on the local model {chosen} through Ollama."


def _hermes_use_cloud(model: str = "", provider: str = "") -> tuple[bool, str]:
    saved = _recall("hermes_cloud", {})
    chosen_provider = provider or str(saved.get("provider") or "") or "nvidia"
    chosen_model = model or str(saved.get("model") or "")
    if chosen_provider.lower() in ("router", "ollama", "lmstudio", "llamacpp"):
        chosen_provider = "nvidia"
    if not chosen_model or chosen_model.startswith("qwen"):
        # No remembered cloud pick: the install's own configured cloud model is
        # the honest default rather than a guess from a model catalogue.
        chosen_model = _hermes_default_cloud_model()
    if not chosen_model:
        return False, "Hermes was not switched: no cloud model is known. Name one, e.g. 'use cloud model nvidia/nemotron-3-ultra-550b-a55b'."
    ok, err = _hermes_set_model(chosen_provider, chosen_model)
    if not ok:
        return False, f"Hermes was not switched: {err}"
    return True, f"Hermes is now on the cloud model {chosen_model} via {chosen_provider}."


def _hermes_default_cloud_model() -> str:
    try:
        import yaml

        data = yaml.safe_load(_hermes_config_path().read_text(encoding="utf-8", errors="ignore")) or {}
        for entry in data.get("fallback_providers") or []:
            provider = str(entry.get("provider") or "").lower()
            model = str(entry.get("model") or "")
            if model and provider not in ("router", "ollama"):
                return model
    except Exception:
        pass
    return "nvidia/nemotron-3-ultra-550b-a55b"


# ── JARVIS switching ─────────────────────────────────────────────────────────


def _jarvis_use_local(model: str = "") -> tuple[bool, str]:
    models = _ollama_models()
    if not models:
        return False, (
            "JARVIS was not switched: Ollama is not reachable on this machine. "
            "Start Ollama with 'systemctl --user start ollama' and try again."
        )
    chosen = _preferred_local_model(models, model)
    llm_cfg = {
        "base_url": f"{_OLLAMA_URL}/v1",
        "model": chosen,
        "api_key": "",
        "vision": False,
        "max_tools": 12,
        "max_tool_chars": 8000,
        # Local models on CPU are slow — give them up to 5 minutes per turn.
        # Cloud models keep their shorter timeout so stale replies do not pile up.
        "timeout": 300,
    }
    stt_cfg = {
        "backend": "local",
        "model": "base",
        "device": "cpu",
        "language": "",
        "base_url": "",
        "api_key": "",
        "download_root": "",
    }
    tts_cfg = {"voice": "", "rate": 0}

    # Validate BEFORE writing: a half-written pipeline would strand the app.
    try:
        from providers import registry

        for pid, cfg in (
            (_LOCAL_LLM_ID, llm_cfg),
            (_LOCAL_STT_ID, stt_cfg),
            (_LOCAL_TTS_ID, tts_cfg),
        ):
            missing = registry.missing_fields(pid, cfg)
            if missing:
                return False, f"JARVIS was not switched: {pid} needs {', '.join(missing)}."
            registry.build(pid, cfg)
    except Exception as e:
        return False, f"JARVIS was not switched: the local pipeline could not be built ({e})."

    try:
        from memory.config_manager import (
            get_pipeline,
            save_pipeline,
            save_provider_config,
            save_provider_mode,
        )
    except Exception as e:
        return False, f"JARVIS was not switched: settings are unavailable ({e})."

    prior = _jarvis_state()
    if not prior.get("local"):
        _remember("jarvis_cloud", {
            "provider_mode": prior.get("provider_mode") or "gemini",
            "router_mode": prior.get("router_mode") or "smart",
        })

    try:
        save_provider_config(_LOCAL_LLM_ID, llm_cfg)
        save_provider_config(_LOCAL_STT_ID, stt_cfg)
        save_provider_config(_LOCAL_TTS_ID, tts_cfg)
        pipe = dict(get_pipeline() or {})
        pipe.update({"llm": _LOCAL_LLM_ID, "stt": _LOCAL_STT_ID, "tts": _LOCAL_TTS_ID})
        save_pipeline(pipe)
        save_provider_mode("pipeline")
        from providers import registry

        registry.reset()
    except Exception as e:
        return False, f"JARVIS was not switched: saving the local pipeline failed ({e})."

    _set_router_mode("local")

    from core import runtime_control

    # Switching engine type (gemini ↔ pipeline) needs extra time for audio
    # stream teardown — ALSA/PortAudio on some systems holds the device briefly
    # after close(), and a new stream opening too soon causes a segfault.
    reconnect = runtime_control.request_engine_reconnect(
        reason="local model", keep_context=True, delay_s=4.0
    )
    tail = "" if reconnect else " It will take effect on the next start."
    return True, (
        f"JARVIS is switching to the local model {chosen} through Ollama, "
        f"with Whisper hearing and the system voice speaking.{tail}\n\n"
        f"⚠️  Local inference is slow on this machine ({chosen} on CPU takes ~10-30s per turn). "
        "For real-time conversation, say 'switch to cloud'. Use local for background tasks only."
    )


def _jarvis_use_cloud(router_mode: str = "") -> tuple[bool, str]:
    saved = _recall("jarvis_cloud", {})
    mode = router_mode or str(saved.get("router_mode") or "") or "smart"
    if mode not in _MODES or mode == "local":
        mode = "smart"
    if not _gemini_available():
        return False, (
            "JARVIS was not switched to the cloud: no Gemini API key is configured. "
            "Add one in ENGINE and PROVIDERS, or stay on the local model."
        )
    try:
        from memory.config_manager import save_provider_mode
        from providers import registry

        save_provider_mode("gemini")
        registry.reset()
    except Exception as e:
        return False, f"JARVIS was not switched to the cloud ({e})."

    _set_router_mode(mode)

    from core import runtime_control

    # Same extra delay when switching back to cloud — the old pipeline's audio
    # stream may still be cleaning up.
    reconnect = runtime_control.request_engine_reconnect(
        reason="cloud model", keep_context=True, delay_s=4.0
    )
    tail = "" if reconnect else " It will take effect on the next start."
    return True, f"JARVIS is switching back to the cloud Gemini Live model.{tail}"


def _jarvis_use_router_mode(mode: str) -> tuple[bool, str]:
    _set_router_mode(mode)
    return True, f"JARVIS text routing is now set to {mode}."


# ── report ───────────────────────────────────────────────────────────────────


def _jarvis_sentence(state: dict) -> str:
    if state.get("local"):
        model = ""
        try:
            from memory.config_manager import get_provider_config

            model = str((get_provider_config(state.get("llm") or "") or {}).get("model") or "")
        except Exception:
            pass
        detail = f" ({model})" if model else ""
        return (
            f"JARVIS is using the local model{detail} through Ollama. "
            "Note: local inference on this machine is slow (seconds per turn). "
            "Use it for background tasks; switch to cloud for real-time conversation."
        )
    if state.get("provider_mode") == "pipeline":
        return (
            "JARVIS is in its own-pipeline mode, but that model is not a local one "
            f"({state.get('llm') or 'no model chosen'})."
        )
    return "JARVIS is using the cloud model, Gemini Live."


def _hermes_sentence(state: dict) -> str:
    if not state.get("cli"):
        return "Hermes is not installed on this machine."
    if state.get("local"):
        return (
            f"Hermes is using the local model {state.get('model') or 'unknown'} "
            "through Ollama."
        )
    return (
        f"Hermes is using the cloud model {state.get('model') or 'unknown'} "
        f"via {state.get('provider') or 'an unknown provider'}."
    )


def _status_text() -> str:
    js = _jarvis_state()
    hs = _hermes_state()
    parts = [_jarvis_sentence(js), f"Text routing is on {js.get('router_mode')}."]
    if js.get("ollama_models"):
        parts.append(
            "Ollama is running with " + ", ".join(js["ollama_models"][:3]) + "."
        )
    else:
        parts.append("Ollama is not reachable, so the local model is unavailable.")
    parts.append(_hermes_sentence(hs))
    parts.append("Say 'use the local model' or 'switch to cloud' to change both.")
    return " ".join(parts)


# ── the tool handler ─────────────────────────────────────────────────────────


def model_control(parameters: dict, player=None) -> str:
    """Report the live engine/model and switch JARVIS and Hermes."""
    parameters = parameters or {}
    action = str(parameters.get("action") or "status").strip().lower()
    target = str(parameters.get("target") or "both").strip().lower()
    model = str(parameters.get("model") or "").strip()
    provider = str(parameters.get("provider") or "").strip()

    if target not in ("jarvis", "hermes", "both"):
        target = "both"

    if action in ("status", "check", "which", "report"):
        return _status_text()

    if action in ("use_local", "local", "switch_local", "go_local", "offline"):
        said: list[str] = []
        if target in ("jarvis", "both"):
            _ok, msg = _jarvis_use_local(model)
            said.append(msg)
        if target in ("hermes", "both"):
            _ok, msg = _hermes_use_local(model)
            said.append(msg)
        return " ".join(said)

    if action in ("use_cloud", "cloud", "switch_cloud", "online"):
        said: list[str] = []
        if target in ("jarvis", "both"):
            _ok, msg = _jarvis_use_cloud("")
            said.append(msg)
        if target in ("hermes", "both"):
            _ok, msg = _hermes_use_cloud(model, provider)
            said.append(msg)
        return " ".join(said)

    if action in ("use_free", "free"):
        _ok, msg = _jarvis_use_router_mode("free")
        return msg

    if action in ("use_smart", "smart", "auto"):
        _ok, msg = _jarvis_use_router_mode("smart")
        return msg

    if action in ("use_gemini", "gemini"):
        _ok, msg = _jarvis_use_cloud("gemini")
        return msg

    if action in ("set_router", "router", "mode"):
        mode = str(parameters.get("mode") or parameters.get("router_mode") or "").lower()
        if mode not in _MODES:
            return f"Unknown routing mode. Choose one of: {', '.join(_MODES)}."
        _ok, msg = _jarvis_use_router_mode(mode)
        return msg

    return (
        "I can report the model, use the local model, or switch to cloud. "
        "Try 'which model are you using' or 'use the local model'."
    )


TOOL = {
    "name": "model_control",
    "description": (
        "Report which model/JARVIS engine and which Hermes model are actually in "
        "use, and switch them between the local model (Ollama on this machine) "
        "and cloud models. Use this when the user asks 'are you using the local "
        "model?', 'which model are you using?', 'use the local model', 'go "
        "local', 'switch to cloud', 'use Gemini', or asks to switch Hermes' "
        "model too. `target` selects JARVIS, Hermes, or both."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "status; use_local; use_cloud; use_free; use_smart; use_gemini; "
                    "or set_router."
                ),
                "enum": [
                    "status",
                    "use_local",
                    "use_cloud",
                    "use_free",
                    "use_smart",
                    "use_gemini",
                    "set_router",
                ],
            },
            "target": {
                "type": "STRING",
                "description": "Which assistant to change: jarvis, hermes, or both.",
                "enum": ["jarvis", "hermes", "both"],
            },
            "model": {
                "type": "STRING",
                "description": (
                    "Optional model name. For local: an Ollama model such as "
                    "qwen2.5:1.5b. For cloud: a Hermes model id."
                ),
            },
            "provider": {
                "type": "STRING",
                "description": "Optional Hermes cloud provider id, e.g. nvidia, openrouter, gemini.",
            },
            "mode": {
                "type": "STRING",
                "description": "For set_router: local, free, smart, or gemini.",
                "enum": ["local", "free", "smart", "gemini"],
            },
        },
        "required": [],
    },
    # A local 1.5B model gets a tight tool budget; model switching must never
    # be the tool that gets cut, or the user cannot switch back by voice.
    "tier": "core",
    "brief": "Report or switch JARVIS/Hermes between the local and cloud models.",
    "handler": model_control,
}
