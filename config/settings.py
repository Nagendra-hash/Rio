"""
config/settings.py — Single source of truth for all JARVIS configuration.

Replaces: config/api_keys.json, memory/config_manager.py, scattered env vars.
Validates on startup, supports .env files, type-safe everywhere.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Literal, Optional, Any
from functools import lru_cache

from pydantic import Field, SecretStr, HttpUrl, model_validator, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _get_base_dir() -> Path:
    """Same contract as memory/memory_manager.get_base_dir."""
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve()
    if getattr(__import__("sys"), "frozen", False):
        return Path(__import__("sys").executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
CONFIG_DIR = BASE_DIR / "config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = CONFIG_DIR / ".env"


class LLMSettings(BaseSettings):
    """LLM Router & Provider Configuration."""
    model_config = SettingsConfigDict(
        env_file=ENV_FILE if ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        env_prefix="JARVIS_LLM_",
        case_sensitive=False,
        extra="ignore",
    )

    # Router mode
    router_mode: Literal["smart", "free", "local", "gemini"] = "smart"

    # Budget
    gemini_daily_call_budget: int = 1000

    # Gemini Live streaming (continuous WebSocket audio) — EXPLICIT OPT-IN ONLY
    # Default FALSE for local-first architecture. Enable via JARVIS_LLM_GEMINI_LIVE_ENABLED=true
    # or voice command "enable cloud mode" / "disable cloud mode".
    gemini_live_enabled: bool = False

    # Keys (comma-separated or JSON array via env) — field names match env vars
    # (without JARVIS_LLM_ prefix) so pydantic-settings maps them correctly
    gemini_api_keys: str = ""
    openrouter_api_keys: str = ""
    nvidia_approved_models: str = ""
    ollama_models: str = ""

    # Live model override
    live_model: Optional[str] = None

    # NVIDIA NIM (Priority 2)
    nvidia_enabled: bool = True
    nvidia_api_key: Optional[SecretStr] = None
    nvidia_base_url: HttpUrl = "https://integrate.api.nvidia.com/v1"

    # FreeLLMAPI
    freellm_api_key: Optional[SecretStr] = None
    freellmapi_base_url: str = "http://127.0.0.1:3001/v1"

    # Grok / xAI
    grok_api_key: Optional[SecretStr] = None
    grok_base_url: str = "https://api.x.ai/v1"

    # OpenAI
    openai_api_key: Optional[SecretStr] = None
    openai_base_url: str = "https://api.openai.com/v1"

    # Ollama
    ollama_enabled: bool = True
    ollama_base_url: str = "http://127.0.0.1:11434"

    # Local 3B GGUF
    llm_local_model_path: Optional[str] = None

    # ── Computed properties (parsed from raw strings) ─────────────────────────
    @property
    def gemini_api_keys_parsed(self) -> list[SecretStr]:
        return self._parse_csv(self.gemini_api_keys, SecretStr)

    @property
    def openrouter_api_keys_parsed(self) -> list[SecretStr]:
        return self._parse_csv(self.openrouter_api_keys, SecretStr)

    @property
    def nvidia_approved_models_parsed(self) -> list[str]:
        default = [
            "nvidia/nemotron-3-super-120b-a12b",
            "deepseek-ai/deepseek-v4.1-flash",
            "openai/gpt-oss-20b",
            "z-ai/glm-5.3-flash",
        ]
        parsed = self._parse_csv(self.nvidia_approved_models)
        return parsed if parsed else default

    @property
    def ollama_models_parsed(self) -> list[str]:
        default = ["qwen2.5:1.5b"]
        parsed = self._parse_csv(self.ollama_models)
        return parsed if parsed else default

    @staticmethod
    def _parse_csv(raw: str, wrapper: type | None = None) -> list:
        """Parse comma-separated string or JSON array into list."""
        if not raw or not raw.strip():
            return []
        raw = raw.strip()
        # Try JSON first
        try:
            import json as _json
            parsed = _json.loads(raw)
            if isinstance(parsed, list):
                return [wrapper(str(x).strip()) if wrapper else str(x).strip()
                        for x in parsed if str(x).strip()]
        except Exception:
            pass
        # Fall back to comma-separated
        items = [x.strip() for x in raw.split(",") if x.strip()]
        return [wrapper(x) if wrapper else x for x in items]


class DBSettings(BaseSettings):
    """Database / Persistent State."""
    model_config = SettingsConfigDict(
        env_file=ENV_FILE if ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        env_prefix="JARVIS_DB_",
        case_sensitive=False,
        extra="ignore",
    )

    # SQLite for structured state (replaces JSONL files)
    url: str = Field(
        default_factory=lambda: f"sqlite+aiosqlite:///{BASE_DIR}/memory/jarvis.db"
    )
    pool_size: int = 5
    echo: bool = False


class UISettings(BaseSettings):
    """Dashboard / UI / HUD."""
    model_config = SettingsConfigDict(
        env_file=ENV_FILE if ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        env_prefix="JARVIS_UI_",
        case_sensitive=False,
        extra="ignore",
    )

    dashboard_port: int = 8000
    dashboard_host: str = "0.0.0.0"
    static_dir: str = "dashboard/static"

    # HUD
    assistant_name: str = "JARVIS"
    user_name: str = ""
    voice: str = "Kore"
    thinking_enabled: bool = True

    # Wake word
    wake_word_enabled: bool = False
    wake_word_model: str = "hey_jarvis"

    # Push-to-talk
    push_to_talk_enabled: bool = False

    # Audio devices
    input_device: Optional[str] = None
    output_device: Optional[str] = None


class TradingSettings(BaseSettings):
    """Trading / Backtest / Market Desk."""
    model_config = SettingsConfigDict(
        env_file=ENV_FILE if ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        env_prefix="JARVIS_TRADING_",
        case_sensitive=False,
        extra="ignore",
    )

    # Paper trading
    paper_enabled: bool = True
    paper_starting_balance: float = 10000.0
    paper_commission_bps: float = 5.0
    paper_slippage_bps: float = 2.0

    # Delta Exchange
    delta_api_key: Optional[SecretStr] = None
    delta_api_secret: Optional[SecretStr] = None
    delta_testnet: bool = True

    # Data
    data_provider: Literal["delta", "yahoo", "binance", "twelvedata"] = "delta"
    data_cache_dir: str = str(BASE_DIR / "memory" / "trading" / "cache")


class HermesSettings(BaseSettings):
    """Hermes Bridge (Telegram/WhatsApp)."""
    model_config = SettingsConfigDict(
        env_file=ENV_FILE if ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        env_prefix="JARVIS_HERMES_",
        case_sensitive=False,
        extra="ignore",
    )

    enabled: bool = False
    gateway_url: str = "http://127.0.0.1:8080"
    webhook_secret: Optional[SecretStr] = None


class AgentGatesSettings(BaseSettings):
    """Per-agent enable/disable gates (JARVIS_*_AGENT env vars)."""
    model_config = SettingsConfigDict(
        env_file=ENV_FILE if ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        env_prefix="JARVIS_",
        case_sensitive=False,
        extra="ignore",
    )

    coding_agent: bool = False
    research_agent: bool = False
    trading_agent: bool = False
    rag_agent: bool = False
    system_agent: bool = False
    email_agent: bool = False
    calendar_agent: bool = False
    health_agent: bool = False
    briefing_agent: bool = False
    meal_agent: bool = False
    travel_agent: bool = False
    finance_agent: bool = False
    study_agent: bool = False
    metaquest_agent: bool = False
    plan_agent: bool = False
    lead_agent: bool = False
    legal_agent: bool = False
    marketing_agent: bool = False
    self_eval_agent: bool = False
    sql_agent: bool = False
    news_agent: bool = False
    replica_agent: bool = False
    youtube_agent: bool = False
    backtest_agent: bool = False
    binance_agent: bool = False
    browser_agent: bool = False
    scientific_agent: bool = False

    orchestrator: Optional[str] = None  # shadow mode override


class Settings(BaseSettings):
    """Root settings — composes all namespaces."""
    model_config = SettingsConfigDict(
        env_file=ENV_FILE if ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    llm: LLMSettings = Field(default_factory=LLMSettings)
    db: DBSettings = Field(default_factory=DBSettings)
    ui: UISettings = Field(default_factory=UISettings)
    trading: TradingSettings = Field(default_factory=TradingSettings)
    hermes: HermesSettings = Field(default_factory=HermesSettings)
    agents: AgentGatesSettings = Field(default_factory=AgentGatesSettings)

    # Global
    env: Literal["development", "staging", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    jarvis_home: str = Field(default_factory=lambda: str(BASE_DIR))

    @model_validator(mode="after")
    def _validate_keys(self) -> "Settings":
        """Ensure at least one LLM provider is configured."""
        has_gemini = bool(self.llm.gemini_api_keys_parsed)
        has_nvidia = bool(self.llm.nvidia_api_key)
        has_openrouter = bool(self.llm.openrouter_api_keys_parsed)
        has_local = self.llm.ollama_enabled or bool(self.llm.llm_local_model_path)

        if not (has_gemini or has_nvidia or has_openrouter or has_local):
            # Allow in development; warn
            if self.env == "production":
                raise ValueError("No LLM provider configured — at least one required")
        return self


# ── Singleton instance ───────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Get the global settings instance (cached)."""
    return Settings()


def reload_settings() -> Settings:
    """Force reload (clears cache). Useful for tests or config hot-reload."""
    get_settings.cache_clear()
    return get_settings()


# ── Backward-compat shims for gradual migration ──────────────────────────────
def get_api_config() -> dict:
    """Return dict compatible with old config/api_keys.json format."""
    s = get_settings()
    return {
        "gemini_api_key": s.llm.gemini_api_keys_parsed[0].get_secret_value() if s.llm.gemini_api_keys_parsed else "",
        "gemini_api_keys": [k.get_secret_value() for k in s.llm.gemini_api_keys_parsed],
        "nvidia_api_key": s.llm.nvidia_api_key.get_secret_value() if s.llm.nvidia_api_key else "",
        "nvidia_nim_enabled": s.llm.nvidia_enabled,
        "nvidia_nim_base_url": str(s.llm.nvidia_base_url),
        "nvidia_approved_models": s.llm.nvidia_approved_models_parsed,
        "openrouter_api_keys": [k.get_secret_value() for k in s.llm.openrouter_api_keys_parsed],
        "freellmapi_api_key": s.llm.freellm_api_key.get_secret_value() if s.llm.freellm_api_key else "",
        "grok_api_key": s.llm.grok_api_key.get_secret_value() if s.llm.grok_api_key else "",
        "openai_api_key": s.llm.openai_api_key.get_secret_value() if s.llm.openai_api_key else "",
        "ollama_enabled": s.llm.ollama_enabled,
        "ollama_models": s.llm.ollama_models_parsed,
        "router_mode": s.llm.router_mode,
        "gemini_daily_call_budget": s.llm.gemini_daily_call_budget,
        "live_model": s.llm.live_model,
    }


def save_api_config(cfg: dict) -> None:
    """Save to .env file (NOT to JSON — .env is the source of truth now).

    Preserves all existing namespaces (UI, trading, Hermes, agent gates, DB, etc.)
    while updating only the settings from the cfg dict.
    """
    # Read existing .env file to preserve non-LLM settings
    existing_lines = []
    if ENV_FILE.exists():
        existing_lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    # Parse existing .env into a dict
    env_dict = {}
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            env_dict[key.strip()] = value

    # Update LLM settings from cfg
    if "gemini_api_key" in cfg and cfg["gemini_api_key"]:
        env_dict["JARVIS_LLM_GEMINI_API_KEYS"] = cfg["gemini_api_key"]
    if "nvidia_api_key" in cfg and cfg.get("nvidia_api_key"):
        env_dict["JARVIS_LLM_NVIDIA_API_KEY"] = cfg["nvidia_api_key"]
    if "nvidia_nim_enabled" in cfg:
        env_dict["JARVIS_LLM_NVIDIA_ENABLED"] = "true" if cfg["nvidia_nim_enabled"] else "false"
    if "openrouter_api_keys" in cfg and cfg["openrouter_api_keys"]:
        keys = ",".join(cfg["openrouter_api_keys"])
        env_dict["JARVIS_LLM_OPENROUTER_API_KEYS"] = keys
    if "freellmapi_api_key" in cfg and cfg.get("freellmapi_api_key"):
        env_dict["JARVIS_LLM_FREELLM_API_KEY"] = cfg["freellmapi_api_key"]
    if "grok_api_key" in cfg and cfg.get("grok_api_key"):
        env_dict["JARVIS_LLM_GROK_API_KEY"] = cfg["grok_api_key"]
    if "openai_api_key" in cfg and cfg.get("openai_api_key"):
        env_dict["JARVIS_LLM_OPENAI_API_KEY"] = cfg["openai_api_key"]
    if "ollama_enabled" in cfg:
        env_dict["JARVIS_LLM_OLLAMA_ENABLED"] = "true" if cfg["ollama_enabled"] else "false"
    if "router_mode" in cfg:
        env_dict["JARVIS_LLM_ROUTER_MODE"] = cfg["router_mode"]
    if "live_model" in cfg and cfg.get("live_model"):
        env_dict["JARVIS_LLM_LIVE_MODEL"] = cfg["live_model"]
    if "gemini_daily_call_budget" in cfg:
        env_dict["JARVIS_LLM_GEMINI_DAILY_CALL_BUDGET"] = str(cfg["gemini_daily_call_budget"])

    # Update UI settings from cfg (if present)
    if "ui" in cfg:
        ui = cfg["ui"]
        if "assistantName" in ui:
            env_dict["JARVIS_UI_ASSISTANT_NAME"] = ui["assistantName"]
        if "userName" in ui:
            env_dict["JARVIS_UI_USER_NAME"] = ui["userName"]
        if "voice" in ui:
            env_dict["JARVIS_UI_VOICE"] = ui["voice"]
        if "wakeWordEnabled" in ui:
            env_dict["JARVIS_UI_WAKE_WORD_ENABLED"] = "true" if ui["wakeWordEnabled"] else "false"
        if "pushToTalkEnabled" in ui:
            env_dict["JARVIS_UI_PUSH_TO_TALK_ENABLED"] = "true" if ui["pushToTalkEnabled"] else "false"
        if "thinkingEnabled" in ui:
            env_dict["JARVIS_UI_THINKING_ENABLED"] = "true" if ui["thinkingEnabled"] else "false"

    # Update trading settings from cfg (if present)
    if "trading" in cfg:
        trading = cfg["trading"]
        if "paperEnabled" in trading:
            env_dict["JARVIS_TRADING_PAPER_ENABLED"] = "true" if trading["paperEnabled"] else "false"
        if "deltaTestnet" in trading:
            env_dict["JARVIS_TRADING_DELTA_TESTNET"] = "true" if trading["deltaTestnet"] else "false"
        if "deltaApiKey" in trading and trading["deltaApiKey"]:
            env_dict["JARVIS_TRADING_DELTA_API_KEY"] = trading["deltaApiKey"]
        if "deltaApiSecret" in trading and trading["deltaApiSecret"]:
            env_dict["JARVIS_TRADING_DELTA_API_SECRET"] = trading["deltaApiSecret"]
        if "dataProvider" in trading:
            env_dict["JARVIS_TRADING_DATA_PROVIDER"] = trading["dataProvider"]

    # Update Hermes settings from cfg (if present)
    if "hermes" in cfg:
        hermes = cfg["hermes"]
        if "enabled" in hermes:
            env_dict["JARVIS_HERMES_ENABLED"] = "true" if hermes["enabled"] else "false"
        if "gatewayUrl" in hermes:
            env_dict["JARVIS_HERMES_GATEWAY_URL"] = hermes["gatewayUrl"]
        if "webhookSecret" in hermes and hermes["webhookSecret"]:
            env_dict["JARVIS_HERMES_WEBHOOK_SECRET"] = hermes["webhookSecret"]

    # Update agent gates from cfg (if present)
    if "agentGates" in cfg:
        gates = cfg["agentGates"]
        for gate_name, enabled in gates.items():
            env_key = f"JARVIS_{gate_name}"
            env_dict[env_key] = "true" if enabled else "false"

    # Update global settings from cfg (if present)
    if "env" in cfg:
        env_dict["JARVIS_ENV"] = cfg["env"]
    if "logLevel" in cfg:
        env_dict["JARVIS_LOG_LEVEL"] = cfg["logLevel"]

    # Write all settings in a consistent order
    output_lines = [
        "# JARVIS Configuration — managed by config/settings.py",
        "",
        "# Environment",
    ]

    # Environment
    for key in ["JARVIS_ENV", "JARVIS_LOG_LEVEL"]:
        if key in env_dict:
            output_lines.append(f"{key}={env_dict[key]}")

    output_lines.append("")
    output_lines.append("# LLM Providers")

    # LLM
    for key in [
        "JARVIS_LLM_ROUTER_MODE",
        "JARVIS_LLM_GEMINI_API_KEYS",
        "JARVIS_LLM_NVIDIA_API_KEY",
        "JARVIS_LLM_NVIDIA_ENABLED",
        "JARVIS_LLM_OPENROUTER_API_KEYS",
        "JARVIS_LLM_FREELLM_API_KEY",
        "JARVIS_LLM_GROK_API_KEY",
        "JARVIS_LLM_OPENAI_API_KEY",
        "JARVIS_LLM_OLLAMA_ENABLED",
        "JARVIS_LLM_OLLAMA_MODELS",
        "JARVIS_LLM_LIVE_MODEL",
        "JARVIS_LLM_GEMINI_DAILY_CALL_BUDGET",
    ]:
        if key in env_dict:
            output_lines.append(f"{key}={env_dict[key]}")

    output_lines.append("")
    output_lines.append("# Database")

    # Database
    for key in ["JARVIS_DB_URL", "JARVIS_DB_POOL_SIZE", "JARVIS_DB_ECHO"]:
        if key in env_dict:
            output_lines.append(f"{key}={env_dict[key]}")

    output_lines.append("")
    output_lines.append("# UI / Dashboard / HUD")

    # UI
    for key in [
        "JARVIS_UI_ASSISTANT_NAME",
        "JARVIS_UI_USER_NAME",
        "JARVIS_UI_VOICE",
        "JARVIS_UI_WAKE_WORD_ENABLED",
        "JARVIS_UI_PUSH_TO_TALK_ENABLED",
        "JARVIS_UI_THINKING_ENABLED",
        "JARVIS_UI_DASHBOARD_TOKEN",
    ]:
        if key in env_dict:
            output_lines.append(f"{key}={env_dict[key]}")

    output_lines.append("")
    output_lines.append("# Trading / Market Desk")

    # Trading
    for key in [
        "JARVIS_TRADING_PAPER_ENABLED",
        "JARVIS_TRADING_DELTA_API_KEY",
        "JARVIS_TRADING_DELTA_API_SECRET",
        "JARVIS_TRADING_DELTA_TESTNET",
        "JARVIS_TRADING_DATA_PROVIDER",
        "JARVIS_TRADING_FRED_API_KEY",
    ]:
        if key in env_dict:
            output_lines.append(f"{key}={env_dict[key]}")

    output_lines.append("")
    output_lines.append("# Hermes Bridge")

    # Hermes
    for key in [
        "JARVIS_HERMES_ENABLED",
        "JARVIS_HERMES_GATEWAY_URL",
        "JARVIS_HERMES_WEBHOOK_SECRET",
    ]:
        if key in env_dict:
            output_lines.append(f"{key}={env_dict[key]}")

    output_lines.append("")
    output_lines.append("# News APIs")

    # News
    for key in ["JARVIS_NEWS_CURRENTS_API_KEY", "JARVIS_NEWS_FRED_API_KEY"]:
        if key in env_dict:
            output_lines.append(f"{key}={env_dict[key]}")

    output_lines.append("")
    output_lines.append("# Agent Gates (all default to true)")

    # Agent gates (all JARVIS_*_AGENT)
    agent_gate_keys = [k for k in env_dict.keys() if k.startswith("JARVIS_") and k.endswith("_AGENT")]
    for key in sorted(agent_gate_keys):
        output_lines.append(f"{key}={env_dict[key]}")

    # Any remaining keys not yet written
    written_keys = set()
    for section_keys in [
        ["JARVIS_ENV", "JARVIS_LOG_LEVEL"],
        ["JARVIS_LLM_ROUTER_MODE", "JARVIS_LLM_GEMINI_API_KEYS", "JARVIS_LLM_NVIDIA_API_KEY", "JARVIS_LLM_NVIDIA_ENABLED", "JARVIS_LLM_OPENROUTER_API_KEYS", "JARVIS_LLM_FREELLM_API_KEY", "JARVIS_LLM_GROK_API_KEY", "JARVIS_LLM_OPENAI_API_KEY", "JARVIS_LLM_OLLAMA_ENABLED", "JARVIS_LLM_OLLAMA_MODELS", "JARVIS_LLM_LIVE_MODEL", "JARVIS_LLM_GEMINI_DAILY_CALL_BUDGET"],
        ["JARVIS_DB_URL", "JARVIS_DB_POOL_SIZE", "JARVIS_DB_ECHO"],
        ["JARVIS_UI_ASSISTANT_NAME", "JARVIS_UI_USER_NAME", "JARVIS_UI_VOICE", "JARVIS_UI_WAKE_WORD_ENABLED", "JARVIS_UI_PUSH_TO_TALK_ENABLED", "JARVIS_UI_THINKING_ENABLED", "JARVIS_UI_DASHBOARD_TOKEN"],
        ["JARVIS_TRADING_PAPER_ENABLED", "JARVIS_TRADING_DELTA_API_KEY", "JARVIS_TRADING_DELTA_API_SECRET", "JARVIS_TRADING_DELTA_TESTNET", "JARVIS_TRADING_DATA_PROVIDER", "JARVIS_TRADING_FRED_API_KEY"],
        ["JARVIS_HERMES_ENABLED", "JARVIS_HERMES_GATEWAY_URL", "JARVIS_HERMES_WEBHOOK_SECRET"],
        ["JARVIS_NEWS_CURRENTS_API_KEY", "JARVIS_NEWS_FRED_API_KEY"],
    ]:
        for k in section_keys:
            written_keys.add(k)
    for k in sorted(agent_gate_keys):
        written_keys.add(k)

    for key in sorted(env_dict.keys()):
        if key not in written_keys:
            output_lines.append(f"{key}={env_dict[key]}")

    ENV_FILE.write_text("\n".join(output_lines) + "\n")
    reload_settings()


# ── CLI helper ───────────────────────────────────────────────────────────────
def config_doctor() -> dict:
    """Validate configuration and return diagnostics."""
    s = get_settings()
    issues = []
    warnings = []

    # LLM providers
    if not s.llm.gemini_api_keys_parsed:
        warnings.append("No Gemini API keys configured")
    if not s.llm.nvidia_api_key and s.llm.nvidia_enabled:
        warnings.append("NVIDIA NIM enabled but no API key")
    if not s.llm.openrouter_api_keys_parsed:
        warnings.append("No OpenRouter API keys configured")
    if not s.llm.ollama_enabled and not s.llm.llm_local_model_path:
        warnings.append("No local LLM (Ollama or llama-cpp) configured")

    # Database
    if "sqlite" in s.db.url and s.env == "production":
        warnings.append("Using SQLite in production — consider PostgreSQL")

    # Secrets
    for name, val in [
        ("DELTA_API_KEY", s.trading.delta_api_key),
        ("DELTA_API_SECRET", s.trading.delta_api_secret),
        ("HERMES_WEBHOOK_SECRET", s.hermes.webhook_secret),
    ]:
        if not val and s.env == "production":
            issues.append(f"Missing secret: {name}")

    return {
        "ok": len(issues) == 0,
        "environment": s.env,
        "issues": issues,
        "warnings": warnings,
        "providers": {
            "gemini": len(s.llm.gemini_api_keys_parsed) > 0,
            "nvidia": bool(s.llm.nvidia_api_key),
            "openrouter": len(s.llm.openrouter_api_keys_parsed) > 0,
            "freellmapi": bool(s.llm.freellm_api_key),
            "grok": bool(s.llm.grok_api_key),
            "openai": bool(s.llm.openai_api_key),
            "ollama": s.llm.ollama_enabled,
            "local_3b": bool(s.llm.llm_local_model_path),
        },
    }


if __name__ == "__main__":
    # python -m config.settings → print diagnostics
    import json as _json
    print(_json.dumps(config_doctor(), indent=2))
