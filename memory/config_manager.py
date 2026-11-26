import json
import os
import sys
from pathlib import Path


# ── Re-export BASE_DIR (same contract) ──────────────────────────────────────
def get_base_dir() -> Path:
    """Same contract as memory_manager.get_base_dir: JARVIS_HOME relocates every
    writable path (api_keys.json included) — required for pendrive mode."""
    import os

    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"

# ── New settings backend ──────────────────────────────────────────────────────
# All config functions now delegate to config.settings (Pydantic Settings)
# The old api_keys.json is kept for backward compat but .env is the source of truth.
from config.settings import get_settings, get_api_config, save_api_config


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def config_exists() -> bool:
    return CONFIG_FILE.exists()


def save_api_keys(gemini_api_key: str) -> None:
    """Persist Gemini key to .env (new source of truth)."""
    s = get_settings()
    keys = list(s.llm.gemini_api_keys_parsed)
    if keys:
        # Update first key, keep rest
        keys[0] = gemini_api_key.strip()
    else:
        keys = [gemini_api_key.strip()]
    # Save via new settings
    cfg = get_api_config()
    cfg["gemini_api_key"] = keys[0]
    cfg["gemini_api_keys"] = [k.get_secret_value() for k in keys]
    save_api_config(cfg)


def load_api_keys() -> dict:
    """Load all keys from new settings (backward compat)."""
    return get_api_config()


def get_gemini_key() -> str | None:
    s = get_settings()
    keys = s.llm.gemini_api_keys_parsed
    return keys[0].get_secret_value() if keys else None


def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)


def get_assistant_name() -> str:
    s = get_settings()
    return s.ui.assistant_name


def get_user_name() -> str:
    s = get_settings()
    return s.ui.user_name


def save_assistant_config(assistant_name: str, user_name: str) -> None:
    cfg = get_api_config()
    cfg["assistant_name"] = assistant_name.strip() or "JARVIS"
    cfg["user_name"] = user_name.strip()
    # Note: these aren't in the new settings yet, save to .env via save_api_config
    # For now, write to api_keys.json for backward compat
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["assistant_name"] = assistant_name.strip() or "JARVIS"
    data["user_name"] = user_name.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── News key ─────────────────────────────────────────────────────────────────
def get_news_key() -> str:
    s = get_settings()
    # Check currents_api_key from trading settings (migrated from api_keys.json)
    # For now, try to load from api_keys.json as fallback
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return (
                data.get("currents_api_key") or data.get("newsapi_key") or ""
            ).strip()
        except Exception:
            pass
    return ""


def save_news_key(key: str) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["currents_api_key"] = (key or "").strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def mask_secret(secret: str) -> str:
    secret = (secret or "").strip()
    if not secret:
        return ""
    if len(secret) < 12:
        return f"{secret[:1]}•••" if len(secret) >= 4 else "•••"
    return f"{secret[:6]}…{secret[-4:]}"


# ── Delta Exchange India credentials ─────────────────────────────────────────
def get_delta_api_key() -> str:
    s = get_settings()
    return s.trading.delta_api_key.get_secret_value() if s.trading.delta_api_key else ""


def get_delta_api_secret() -> str:
    s = get_settings()
    return (
        s.trading.delta_api_secret.get_secret_value()
        if s.trading.delta_api_secret
        else ""
    )


def save_delta_api_credentials(key: str, secret: str) -> None:
    cfg = get_api_config()
    cfg["delta_api_key"] = (key or "").strip()
    cfg["delta_api_secret"] = (secret or "").strip()
    save_api_config(cfg)


# ── Dashboard static token ───────────────────────────────────────────────────
def get_dashboard_token() -> str:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return (data.get("dashboard_auth_token") or "").strip()
        except Exception:
            pass
    return ""


def save_dashboard_token(token: str) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["dashboard_auth_token"] = (token or "").strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Owner birth profile ──────────────────────────────────────────────────────
def get_birth_profile() -> dict:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            p = data.get("birth_profile") or {}
            return p if isinstance(p, dict) else {}
        except Exception:
            pass
    return {}


def save_birth_profile(profile: dict) -> None:
    if not isinstance(profile, dict) or not (profile.get("dob") or "").strip():
        raise ValueError("birth profile requires a 'dob'")
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["birth_profile"] = {
        "dob": (profile.get("dob") or "").strip(),
        "birth_time": (profile.get("birth_time") or "").strip(),
        "birth_place": (profile.get("birth_place") or "").strip(),
        "gender": (profile.get("gender") or "").strip(),
        "name": (profile.get("name") or "").strip(),
    }
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Assistant voice ──────────────────────────────────────────────────────────
AVAILABLE_VOICES = ["Charon", "Puck", "Kore", "Fenrir", "Aoede"]
DEFAULT_VOICE = "Charon"


def get_voice() -> str:
    s = get_settings()
    v = s.ui.voice
    return v if v in AVAILABLE_VOICES else DEFAULT_VOICE


def save_voice(voice_name: str) -> None:
    cfg = get_api_config()
    v = (voice_name or "").strip()
    # Save to .env via new settings would need JARVIS_UI_VOICE
    # For now, keep in api_keys.json for backward compat
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["voice_name"] = v if v in AVAILABLE_VOICES else DEFAULT_VOICE
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_wake_word_enabled() -> bool:
    s = get_settings()
    return s.ui.wake_word_enabled


def save_wake_word_enabled(enabled: bool) -> None:
    cfg = get_api_config()
    cfg["wake_word_enabled"] = bool(enabled)
    save_api_config(cfg)


def get_push_to_talk_enabled() -> bool:
    s = get_settings()
    return s.ui.push_to_talk_enabled


def save_push_to_talk_enabled(enabled: bool) -> None:
    cfg = get_api_config()
    cfg["push_to_talk_enabled"] = bool(enabled)
    save_api_config(cfg)


HUD_STYLES = ("face", "core")


def get_hud_style() -> str:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            v = str(data.get("hud_style", "face")).strip().lower()
            return v if v in HUD_STYLES else "face"
        except Exception:
            pass
    return "face"


def save_hud_style(style: str) -> None:
    s = str(style or "").strip().lower()
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["hud_style"] = s if s in HUD_STYLES else "face"
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Music preference ─────────────────────────────────────────────────────────
# "Use Spotify as my default music app" / "JioSaavn is my default". Stored in
# the same gitignored config file as the other local preferences.
MUSIC_PROVIDERS = ("spotify", "jiosaavn")


def get_music_provider() -> str:
    env = os.environ.get("JARVIS_MUSIC_PROVIDER", "").strip().lower()
    if env in MUSIC_PROVIDERS:
        return env
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            v = str(data.get("music_provider", "")).strip().lower()
            if v in MUSIC_PROVIDERS:
                return v
        except Exception:
            pass
    return "jiosaavn"


def save_music_provider(name: str) -> str:
    v = str(name or "").strip().lower()
    if v not in MUSIC_PROVIDERS:
        v = "jiosaavn"
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["music_provider"] = v
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
    return v


# ── Live-session tuning ──────────────────────────────────────────────────────
def get_thinking_enabled() -> bool:
    s = get_settings()
    return s.ui.thinking_enabled


def save_thinking_enabled(enabled: bool) -> None:
    cfg = get_api_config()
    cfg["thinking_enabled"] = bool(enabled)
    save_api_config(cfg)


def get_turn_tuning() -> dict:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg = data.get("turn_tuning")
            if isinstance(cfg, dict):

                def _int(key, default, lo, hi):
                    try:
                        return max(lo, min(hi, int(cfg.get(key, default))))
                    except (TypeError, ValueError):
                        return default

                return {
                    "enabled": bool(cfg.get("enabled", False)),
                    "silence_ms": _int("silence_ms", 550, 200, 3000),
                    "prefix_ms": _int("prefix_ms", 150, 0, 1000),
                    "end_sensitivity": str(cfg.get("end_sensitivity", "high")).lower(),
                    "start_sensitivity": str(
                        cfg.get("start_sensitivity", "default")
                    ).lower(),
                }
        except Exception:
            pass
    return {
        "enabled": False,
        "silence_ms": 550,
        "prefix_ms": 150,
        "end_sensitivity": "high",
        "start_sensitivity": "default",
    }


def save_turn_tuning(values: dict) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    cur = data.get("turn_tuning")
    cur = dict(cur) if isinstance(cur, dict) else {}
    cur.update(values or {})
    data["turn_tuning"] = cur
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_proactive_audio_enabled() -> bool:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return bool(data.get("proactive_audio", True))
        except Exception:
            pass
    return True


def save_proactive_audio_enabled(enabled: bool) -> None:
    _save_flag("proactive_audio", enabled)


MEDIA_RESOLUTIONS = ("default", "low", "medium", "high")


def get_media_resolution() -> str:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            v = str(data.get("media_resolution", "medium")).strip().lower()
            return v if v in MEDIA_RESOLUTIONS else "medium"
        except Exception:
            pass
    return "medium"


def save_media_resolution(value: str) -> None:
    v = str(value or "").strip().lower()
    _save_flag("media_resolution", v if v in MEDIA_RESOLUTIONS else "medium")


def _save_flag(key: str, value) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[key] = bool(value) if isinstance(value, bool) else value
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_brief_enabled() -> bool:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return bool(data.get("morning_brief_enabled", True))
        except Exception:
            pass
    return True


def save_brief_enabled(enabled: bool) -> None:
    _save_flag("morning_brief_enabled", enabled)


# ── Audio devices ────────────────────────────────────────────────────────────
def get_input_device() -> str:
    s = get_settings()
    return s.ui.input_device or ""


def save_input_device(name: str) -> None:
    cfg = get_api_config()
    cfg["input_device"] = (name or "").strip()
    save_api_config(cfg)


def get_output_device() -> str:
    s = get_settings()
    return s.ui.output_device or ""


def save_output_device(name: str) -> None:
    cfg = get_api_config()
    cfg["output_device"] = (name or "").strip()
    save_api_config(cfg)


def get_plugin_enabled(plugin_name: str) -> bool:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return data.get("plugins_enabled", {}).get(plugin_name, True)
        except Exception:
            pass
    return True


# ── Per-plugin settings ──────────────────────────────────────────────────────
def get_plugin_config(namespace: str) -> dict:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg = data.get("plugin_config")
            val = cfg.get(namespace) if isinstance(cfg, dict) else None
            return dict(val) if isinstance(val, dict) else {}
        except Exception:
            pass
    return {}


def get_plugin_setting(namespace: str, key: str, default=None):
    return get_plugin_config(namespace).get(key, default)


def save_plugin_config(namespace: str, values: dict) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    pc = data.get("plugin_config")
    if not isinstance(pc, dict):
        pc = {}
    cur = pc.get(namespace)
    if not isinstance(cur, dict):
        cur = {}
    cur.update(values)
    pc[namespace] = cur
    data["plugin_config"] = pc
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def save_plugin_enabled(plugin_name: str, enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    plugins_cfg = data.get("plugins_enabled")
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
    plugins_cfg[plugin_name] = enabled
    data["plugins_enabled"] = plugins_cfg
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Provider configuration (pipeline engine & providers panel) ──────────────────
def get_provider_mode() -> str:
    """Return 'gemini' (Gemini Live) or 'pipeline' (local STT→LLM→TTS)."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            v = str(data.get("provider_mode", "gemini")).strip().lower()
            return v if v in ("gemini", "pipeline") else "gemini"
        except Exception:
            pass
    return "gemini"


def get_gemini_live_enabled() -> bool:
    """Return True when the conversation engine should be Gemini Live.

    ORDER, and why it matters
        1. `JARVIS_LLM_GEMINI_LIVE_ENABLED` in the process environment — an
           explicit override for a single run.
        2. `gemini_live_enabled` in config/api_keys.json — what
           `set_gemini_live_enabled()` writes.
        3. `provider_mode` in the same file — the user-facing switch the voice
           commands write ("switch to cloud" / "use the local model").
        4. `.env` through the settings object.
        5. False (local-first default).

    Reading ONLY the settings object was a real defect: `save_provider_mode`
    and `set_gemini_live_enabled` persisted their answer to the config file,
    nothing ever read it, and `_engine_mode()` therefore stayed on the local
    pipeline no matter what the user said. "Switch to cloud" acknowledged the
    change and left the local model running.
    """
    # 1. process environment (explicit, per-run)
    raw = os.environ.get("JARVIS_LLM_GEMINI_LIVE_ENABLED")
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    # 2/3. the config file — the switches the UI and the voice commands write
    try:
        data: dict = {}
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        if isinstance(data.get("gemini_live_enabled"), bool):
            return bool(data["gemini_live_enabled"])
        mode = str(data.get("provider_mode", "") or "").strip().lower()
        if mode in ("gemini", "pipeline"):
            return mode == "gemini"
    except Exception:
        pass

    # 4. .env / defaults
    try:
        from config.settings import get_settings
        s = get_settings()
        return bool(s.llm.gemini_live_enabled)
    except Exception:
        return False


def set_gemini_live_enabled(enabled: bool) -> None:
    """Set Gemini Live enabled state (persists to .env via settings)."""
    try:
        from config.settings import get_settings
        # Note: pydantic-settings doesn't support writing back to .env directly.
        # We use the config file for persistence (backward compat).
        ensure_config_dir()
        data: dict = {}
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data["gemini_live_enabled"] = bool(enabled)
        # Same decision, other name — see save_provider_mode().
        data["provider_mode"] = "gemini" if enabled else "pipeline"
        CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Config] Failed to save gemini_live_enabled: {e}")


def save_provider_mode(mode: str) -> None:
    """Save provider mode ('gemini' or 'pipeline')."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    v = str(mode or "").strip().lower()
    v = v if v in ("gemini", "pipeline") else "gemini"
    data["provider_mode"] = v
    # Keep the two switches that describe the same decision in step. The engine
    # reads `gemini_live_enabled`; older builds and the UI read `provider_mode`.
    # Writing only one is how "switch to cloud" used to acknowledge the change
    # and leave the local pipeline running.
    data["gemini_live_enabled"] = (v == "gemini")
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_pipeline() -> dict:
    """Return the three-slot pipeline config: {stt, llm, tts} -> service id."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            pipe = data.get("pipeline") or {}
            if isinstance(pipe, dict):
                return {
                    "stt": str(pipe.get("stt", "")).strip(),
                    "llm": str(pipe.get("llm", "")).strip(),
                    "tts": str(pipe.get("tts", "")).strip(),
                }
        except Exception:
            pass
    return {"stt": "", "llm": "", "tts": ""}


def save_pipeline(pipeline: dict) -> None:
    """Save the three-slot pipeline config."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["pipeline"] = {
        "stt": str(pipeline.get("stt", "")).strip(),
        "llm": str(pipeline.get("llm", "")).strip(),
        "tts": str(pipeline.get("tts", "")).strip(),
    }
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_provider_config(provider_id: str) -> dict:
    """Load config for a single provider/service id (e.g. 'openai_compat@minimax')."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            providers = data.get("providers") or {}
            if isinstance(providers, dict):
                cfg = providers.get(provider_id)
                if isinstance(cfg, dict):
                    return dict(cfg)
        except Exception:
            pass
    return {}


def save_provider_config(provider_id: str, values: dict) -> None:
    """Save config for a single provider/service id.

    MERGES into the existing entry rather than replacing it, so a partial
    update (e.g. the vision test writes just ``vision`` + ``vision_model``)
    does not clobber ``base_url``, ``model``, ``api_key`` etc. that were set
    when the user picked the service.
    """
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    providers = data.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    existing = providers.get(provider_id) or {}
    existing.update({k: v for k, v in values.items() if v is not None})
    providers[provider_id] = existing
    data["providers"] = providers
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_sidecar_provider() -> str:
    """Return the LLM service id used for background/sidecar requests."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            v = str(data.get("sidecar_provider", "")).strip()
            return v
        except Exception:
            pass
    return ""


def save_sidecar_provider(provider_id: str) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["sidecar_provider"] = str(provider_id or "").strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_sidecar_first() -> bool:
    """Return True if sidecar model is tried before Gemini for background requests."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return bool(data.get("sidecar_first", False))
        except Exception:
            pass
    return False


def save_sidecar_first(first: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["sidecar_first"] = bool(first)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_coding_provider() -> str:
    """Return the LLM service id used for coding agent."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            v = str(data.get("coding_provider", "")).strip()
            return v
        except Exception:
            pass
    return ""


def save_coding_provider(provider_id: str) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["coding_provider"] = str(provider_id or "").strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def save_providers(
    *,
    mode: str,
    pipeline: dict,
    creds: dict,
    sidecar: str,
    sidecar_first: bool,
    coding: str,
) -> None:
    """One-shot save for all provider settings (called from ProvidersOverlay._apply)."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["provider_mode"] = mode
    data["pipeline"] = {
        "stt": str(pipeline.get("stt", "")).strip(),
        "llm": str(pipeline.get("llm", "")).strip(),
        "tts": str(pipeline.get("tts", "")).strip(),
    }
    data["providers"] = {sid: dict(cfg) for sid, cfg in creds.items()}
    data["sidecar_provider"] = str(sidecar or "").strip()
    data["sidecar_first"] = bool(sidecar_first)
    data["coding_provider"] = str(coding or "").strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Generic update helper (used by gui/window.py) ───────────────────────────

def update_config(**fields) -> None:
    """Persist a set of arbitrary config keys in one shot.

    Called from two places in gui/window.py:

      1. Identity panel — ``assistant_name``, ``user_name``, optionally ``ui_color``
      2. Setup overlay   — ``os_system``, ``local_only``, optionally ``gemini_api_key``

    The function writes everything to the legacy api_keys.json *and* forwards
    structured pieces into the new .env settings backend via ``save_api_config``
    so that both paths stay in sync during the migration.

    Accepted keys (all optional):

        assistant_name   str  – UI / HUD display name
        user_name        str  – owner name shown in the briefings
        ui_color         str  – CSS hex accent for the HUD (not stored in .env;
                                only written to api_keys.json for the theme code)
        gemini_api_key   str  – primary Gemini key (also handled by save_api_keys)
        os_system        str  – host OS label recorded by setup wizard
        local_only       bool – whether the install runs without cloud APIs
    """
    ensure_config_dir()

    # ── Legacy JSON write (api_keys.json) ────────────────────────────────
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}

    if "assistant_name" in fields:
        data["assistant_name"] = str(fields["assistant_name"] or "").strip() or "JARVIS"
    if "user_name" in fields:
        data["user_name"] = str(fields["user_name"] or "").strip()
    if "ui_color" in fields:
        data["ui_color"] = str(fields["ui_color"] or "").strip().lower()
    if "os_system" in fields:
        data["os_system"] = str(fields["os_system"] or "").strip()
    if "local_only" in fields:
        data["local_only"] = bool(fields["local_only"])
    if "gemini_api_key" in fields and fields["gemini_api_key"]:
        # Also push the key through the new .env path so reload_settings picks
        # it up immediately without needing a restart.
        save_api_keys(str(fields["gemini_api_key"]).strip())
        data["gemini_api_key"] = data.get("gemini_api_key", "")

    if "ui_color" not in fields and "assistant_name" not in fields \
            and "user_name" not in fields and "os_system" not in fields \
            and "local_only" not in fields:
        # Only a gemini_api_key update — leave the rest untouched.
        pass
    elif not fields:
        return

    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

    # ── New .env path (UI fields only — gemini key handled above) ────────
    if "assistant_name" in fields or "user_name" in fields:
        cfg = get_api_config()
        if "assistant_name" in fields:
            cfg["ui"] = {"assistantName": str(fields["assistant_name"]).strip() or "JARVIS"}
        if "user_name" in fields:
            if "ui" not in cfg:
                cfg["ui"] = {}
            cfg["ui"]["userName"] = str(fields["user_name"] or "").strip()
        save_api_config(cfg)
