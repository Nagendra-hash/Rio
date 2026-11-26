"""
core/adaptive_memory/config.py — Configuration via Pydantic Settings v2.

Loads from config/adaptive_memory.json with environment variable overrides.
All settings are validated at startup.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from memory.config_manager import BASE_DIR


class TierConfig(BaseSettings):
    max_size_mb: int = 100
    ttl_days: Optional[int] = None  # None = infinite


class PromotionConfig(BaseSettings):
    min_confidence: float = 0.5
    min_access: int = 0
    min_age_days: int = 0
    min_sources: int = 1
    min_pattern_count: int = 5


class DecayConfig(BaseSettings):
    working_per_hour: float = 0.1
    episodic_per_day: float = 0.05
    semantic_per_day: float = 0.001
    procedural_per_day: float = 0.0001
    long_term_per_day: float = 0.0


class IngestionConfig(BaseSettings):
    parallel_workers: int = 4
    batch_size: int = 50
    chunk_size: int = 512
    chunk_overlap: int = 50
    embedding_model: str = "text-embedding-3-large"
    max_file_size_mb: int = 100


class LoopConfig(BaseSettings):
    enabled: bool = True
    interval_hours: int = 6
    topics_per_run: int = 5
    batch_turns: int = 10
    hour: int = 3
    interval_days: int = 30
    sample_pct: float = 0.1


class SourceConfig(BaseSettings):
    enabled: bool = True
    # Web
    max_depth: int = 2
    rate_limit_rpm: int = 30
    # YouTube
    max_duration_min: int = 60
    # Gmail
    labels: list[str] = Field(default_factory=lambda: ["INBOX", "IMPORTANT"])
    max_per_run: int = 100
    # Calendar
    lookahead_days: int = 30
    # Chat
    platforms: list[str] = Field(default_factory=lambda: ["telegram", "whatsapp"])
    # Documents/Spreadsheets
    watch_dirs: list[str] = Field(default_factory=list)
    # Code
    repos: list[str] = Field(default_factory=list)
    # Transcripts
    # Screen
    interval_sec: int = 30


class PrivacyConfig(BaseSettings):
    pii_detection: bool = True
    auto_redact: list[str] = Field(default_factory=lambda: ["ssn", "credit_card", "api_key", "password"])
    local_only_sources: list[str] = Field(default_factory=lambda: ["email", "calendar", "documents", "screen"])


class AdaptiveMemoryConfig(BaseSettings):
    """Main configuration for the adaptive memory system."""

    model_config = SettingsConfigDict(
        env_prefix="JARVIS_MEMORY_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    enabled: bool = True

    # Tier settings
    tiers: dict[str, TierConfig] = Field(default_factory=lambda: {
        "working": TierConfig(max_size_mb=100, ttl_days=None),
        "episodic": TierConfig(max_size_mb=500, ttl_days=30),
        "semantic": TierConfig(max_size_mb=2000, ttl_days=None),
        "procedural": TierConfig(max_size_mb=500, ttl_days=None),
        "long_term": TierConfig(max_size_mb=1000, ttl_days=None),
    })

    # Promotion thresholds
    promotion: dict[str, PromotionConfig] = Field(default_factory=lambda: {
        "working_to_episodic": PromotionConfig(min_confidence=0.5),
        "episodic_to_semantic": PromotionConfig(min_confidence=0.7, min_access=3),
        "semantic_to_procedural": PromotionConfig(min_pattern_count=5),
        "semantic_to_long_term": PromotionConfig(min_confidence=0.9, min_sources=2),
    })

    # Decay rates
    decay: DecayConfig = Field(default_factory=DecayConfig)

    # Ingestion pipeline
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)

    # Learning loops
    loops: dict[str, LoopConfig] = Field(default_factory=lambda: {
        "curiosity": LoopConfig(enabled=True, interval_hours=6, topics_per_run=5),
        "experience": LoopConfig(enabled=True, batch_turns=10),
        "synthesis": LoopConfig(enabled=True, interval_hours=24),
        "consolidation": LoopConfig(enabled=True, hour=3),
        "verification": LoopConfig(enabled=True, interval_days=30, sample_pct=0.1),
    })

    # Source adapters
    sources: dict[str, SourceConfig] = Field(default_factory=lambda: {
        "web": SourceConfig(enabled=True, max_depth=2, rate_limit_rpm=30),
        "youtube": SourceConfig(enabled=True, max_duration_min=60),
        "gmail": SourceConfig(enabled=False, labels=["INBOX", "IMPORTANT"], max_per_run=100),
        "calendar": SourceConfig(enabled=False, lookahead_days=30),
        "chat": SourceConfig(enabled=True, platforms=["telegram", "whatsapp"]),
        "documents": SourceConfig(enabled=True, watch_dirs=["~/Documents/JARVIS"]),
        "spreadsheets": SourceConfig(enabled=True, watch_dirs=["~/Data/JARVIS"]),
        "code": SourceConfig(enabled=False, repos=[]),
        "transcripts": SourceConfig(enabled=True, watch_dirs=["~/Recordings"]),
        "screen": SourceConfig(enabled=False, interval_sec=30),
    })

    # Privacy
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)

    # Paths
    state_dir: Path = BASE_DIR / "memory" / "adaptive_memory"
    nodes_dir: Path = BASE_DIR / "memory" / "adaptive_memory" / "nodes"
    graph_path: Path = BASE_DIR / "memory" / "adaptive_memory" / "graph.json"
    checkpoints_dir: Path = BASE_DIR / "memory" / "adaptive_memory" / "checkpoints"
    audit_log_path: Path = BASE_DIR / "memory" / "adaptive_memory" / "audit.jsonl"

    @field_validator("state_dir", "nodes_dir", "checkpoints_dir", mode="before")
    @classmethod
    def expand_path(cls, v: Any) -> Path:
        if isinstance(v, str):
            return Path(v).expanduser().resolve()
        return v

    def ensure_dirs(self) -> None:
        """Create all required directories."""
        for d in [self.state_dir, self.nodes_dir, self.checkpoints_dir]:
            d.mkdir(parents=True, exist_ok=True)


def load_config() -> AdaptiveMemoryConfig:
    """Load config from JSON file with env overrides."""
    config_path = BASE_DIR / "config" / "adaptive_memory.json"
    if config_path.exists():
        with open(config_path) as f:
            data = json.load(f)
        return AdaptiveMemoryConfig(**data)
    return AdaptiveMemoryConfig()


def save_config(config: AdaptiveMemoryConfig) -> None:
    """Save config to JSON file."""
    config_path = BASE_DIR / "config" / "adaptive_memory.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(config.model_dump(mode="json"), f, indent=2, default=str)


# Global instance (lazy-loaded)
_config: Optional[AdaptiveMemoryConfig] = None


def get_config() -> AdaptiveMemoryConfig:
    global _config
    if _config is None:
        _config = load_config()
        _config.ensure_dirs()
    return _config