"""
core/adaptive_memory/adapters — Source adapters for memory ingestion.

ARCHITECTURE POSITION
    Adapters sit at the ingestion boundary: they translate raw external sources
    (files, APIs, streams) into the unified RawItem → MemoryNode pipeline.
    The rest of adaptive_memory never knows about PDFs, IMAP, or YouTube — it
    only sees RawItems and MemoryNodes.

DESIGN
    Each adapter implements BaseAdapter (base.py) with two core operations:
      - extract(item): RawItem → list[MemoryNode]  (synchronous-ish, async for I/O)
      - list_sources(): AsyncIterator[RawItem]      (for incremental/scheduled ingest)

    Adapters are registered in AdapterRegistry (base.py) keyed by SourceType.
    The registry supports config-driven creation and lazy loading.

    Adding a new source = one new file in this directory with a class that
    inherits BaseAdapter, plus one line in create_all_from_config().

SUPPORTED SOURCES (so far)
    DOCUMENT      → document.py   (PDF, DOCX, TXT, MD, RTF, ODT via markitdown)
    SPREADSHEET   → spreadsheet.py (Excel, CSV, TSV via pandas/openpyxl)

PLANNED / STUBBED
    WEB           → web.py        (HTTP + readability + sitemap)
    YOUTUBE       → youtube.py    (transcript + metadata via yt-dlp)
    EMAIL         → email.py      (IMAP/Gmail API)
    CALENDAR      → calendar.py   (CalDAV/Google Calendar)
    CHAT          → chat.py       (Telegram, Discord, Slack exports)
    CODE          → code.py       (Git repos, GitHub API)
    TRANSCRIPT    → transcript.py (Whisper, meeting platforms)
    SCREEN        → screen.py     (OCR + window capture)
    USER_FEEDBACK → feedback.py   (Explicit corrections, ratings)
    AGENT_OUTPUT  → agent.py      (Structured agent results)

CONFIGURATION
    Adapters are configured via config/adaptive_memory.json under "sources":
    {
      "sources": {
        "document": {"enabled": true, "watch_dirs": ["~/Documents"], "max_file_size_mb": 100},
        "spreadsheet": {"enabled": false, "watch_dirs": [], "include_headers": true}
      }
    }
    The registry's create_all_from_config() reads this and instantiates only
    enabled adapters with their respective config sections.

THREAD SAFETY
    Registry mutations (register) happen at startup under the GIL.
    Adapter instances are stateless across calls (only _seen_hashes is per-instance
    and not shared). list_sources() yields from its own async generator.

LAZY LOADING
    Heavy dependencies (pandas, markitdown, fitz) are imported inside methods,
    not at module level — so importing this package never triggers installs.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

from core.adaptive_memory.adapters.base import (
    BaseAdapter,
    AdapterCapabilities,
    AdapterRegistry,
    get_registry as _get_base_registry,
)
from core.adaptive_memory.schema import SourceType

# Re-export base types for convenience
__all__ = [
    "BaseAdapter",
    "AdapterCapabilities",
    "AdapterRegistry",
    "SourceType",
    "get_registry",
    "create_adapter",
    "create_all_adapters",
    "list_available_adapters",
    "load_adapters_from_config",
]

# ──────────────────────────────────────────────────────────────────────────────
# Adapter class mapping — single source of truth for "what adapters exist"
# Add new adapters here AND in create_all_from_config() in base.py
# ──────────────────────────────────────────────────────────────────────────────

_ADAPTER_CLASSES: dict[SourceType, str] = {
    SourceType.DOCUMENT: "core.adaptive_memory.adapters.document:DocumentAdapter",
    SourceType.SPREADSHEET: "core.adaptive_memory.adapters.spreadsheet:SpreadsheetAdapter",
    # SourceType.WEB: "core.adaptive_memory.adapters.web:WebAdapter",
    # SourceType.YOUTUBE: "core.adaptive_memory.adapters.youtube:YouTubeAdapter",
    # SourceType.EMAIL: "core.adaptive_memory.adapters.email:EmailAdapter",
    # SourceType.CALENDAR: "core.adaptive_memory.adapters.calendar:CalendarAdapter",
    # SourceType.CHAT: "core.adaptive_memory.adapters.chat:ChatAdapter",
    # SourceType.CODE: "core.adaptive_memory.adapters.code:CodeAdapter",
    # SourceType.TRANSCRIPT: "core.adaptive_memory.adapters.transcript:TranscriptAdapter",
    # SourceType.SCREEN: "core.adaptive_memory.adapters.screen:ScreenAdapter",
    # SourceType.USER_FEEDBACK: "core.adaptive_memory.adapters.feedback:FeedbackAdapter",
    # SourceType.AGENT_OUTPUT: "core.adaptive_memory.adapters.agent:AgentOutputAdapter",
}

# Cache for lazily imported adapter classes
_ADAPTER_CLASS_CACHE: dict[SourceType, type[BaseAdapter]] = {}
_import_lock = threading.Lock()


def _import_adapter_class(source_type: SourceType) -> type[BaseAdapter] | None:
    """Lazily import an adapter class by its module:ClassName string."""
    if source_type in _ADAPTER_CLASS_CACHE:
        return _ADAPTER_CLASS_CACHE[source_type]

    import_path = _ADAPTER_CLASSES.get(source_type)
    if not import_path:
        return None

    with _import_lock:
        # Double-check after acquiring lock
        if source_type in _ADAPTER_CLASS_CACHE:
            return _ADAPTER_CLASS_CACHE[source_type]

        try:
            module_path, class_name = import_path.split(":")
            module = __import__(module_path, fromlist=[class_name])
            adapter_class = getattr(module, class_name)
            _ADAPTER_CLASS_CACHE[source_type] = adapter_class
            logging.getLogger(__name__).debug("Loaded adapter: %s (%s)", source_type.value, import_path)
            return adapter_class
        except Exception as e:
            logging.getLogger(__name__).warning("Failed to load adapter %s: %s", source_type.value, e)
            _ADAPTER_CLASS_CACHE[source_type] = None  # Cache negative result
            return None


def list_available_adapters() -> list[SourceType]:
    """Return all SourceTypes that have adapter implementations available."""
    return list(_ADAPTER_CLASSES.keys())


def create_adapter(
    source_type: SourceType,
    config: Optional[dict] = None,
) -> BaseAdapter | None:
    """
    Create an adapter instance for the given source type.

    Args:
        source_type: The type of source (e.g., SourceType.DOCUMENT)
        config: Optional configuration dict for this adapter

    Returns:
        BaseAdapter instance, or None if the adapter type is unknown or failed to load.
    """
    adapter_class = _import_adapter_class(source_type)
    if adapter_class is None:
        return None
    try:
        return adapter_class(config or {})
    except Exception as e:
        logging.getLogger(__name__).error("Failed to instantiate %s adapter: %s", source_type.value, e)
        return None


def create_all_adapters(config: Optional[dict] = None) -> dict[SourceType, BaseAdapter]:
    """
    Create all available adapters with the given config.

    Args:
        config: Full config dict or Pydantic model (will extract per-source configs from config["sources"])

    Returns:
        Dict mapping SourceType to instantiated adapter (only for adapters that loaded successfully).
    """
    if config is None:
        config = {}
    # Handle both dict and Pydantic model
    if hasattr(config, "model_dump"):
        config = config.model_dump()
    elif hasattr(config, "dict"):
        config = config.dict()

    sources_config = config.get("sources", {})
    adapters: dict[SourceType, BaseAdapter] = {}

    for source_type in _ADAPTER_CLASSES:
        source_config = sources_config.get(source_type.value, {})
        if source_config.get("enabled", False):
            adapter = create_adapter(source_type, source_config)
            if adapter:
                adapters[source_type] = adapter

    return adapters


def load_adapters_from_config(config: Optional[dict] = None) -> AdapterRegistry:
    """
    Load and register all enabled adapters from configuration.

    This is the main entry point for the ingestion pipeline. It:
      1. Creates the global registry (or returns existing)
      2. Instantiates all enabled adapters from config
      3. Registers them in the registry
      4. Returns the populated registry

    Args:
        config: Optional full config dict. If None, loads from adaptive_memory config.

    Returns:
        AdapterRegistry with all enabled adapters registered.
    """
    from core.adaptive_memory.config import get_config as get_am_config

    if config is None:
        config = get_am_config()

    registry = _get_base_registry()
    adapters = create_all_adapters(config)

    for source_type, adapter in adapters.items():
        registry.register(adapter)
        logging.getLogger(__name__).info("Registered adapter: %s", source_type.value)

    return registry


def get_registry() -> AdapterRegistry:
    """
    Get the global adapter registry, initializing from config if empty.

    This is the primary way to access adapters in the ingestion pipeline.
    First call triggers config load and adapter registration.
    """
    registry = _get_base_registry()

    # Auto-initialize on first use if registry is empty
    if not registry.list_registered():
        load_adapters_from_config()

    return registry


# ──────────────────────────────────────────────────────────────────────────────
# Convenience re-exports of concrete adapters (for type hints and direct use)
# ──────────────────────────────────────────────────────────────────────────────

# These are imported lazily to avoid loading heavy deps at package import time
_DocumentAdapter: type[BaseAdapter] | None = None
_SpreadsheetAdapter: type[BaseAdapter] | None = None


def _get_document_adapter_class() -> type[BaseAdapter] | None:
    global _DocumentAdapter
    if _DocumentAdapter is None:
        try:
            from core.adaptive_memory.adapters.document import DocumentAdapter
            _DocumentAdapter = DocumentAdapter
        except Exception:
            _DocumentAdapter = False  # type: ignore[assignment]
    return _DocumentAdapter if _DocumentAdapter is not False else None  # type: ignore[return-value]


def _get_spreadsheet_adapter_class() -> type[BaseAdapter] | None:
    global _SpreadsheetAdapter
    if _SpreadsheetAdapter is None:
        try:
            from core.adaptive_memory.adapters.spreadsheet import SpreadsheetAdapter
            _SpreadsheetAdapter = SpreadsheetAdapter
        except Exception:
            _SpreadsheetAdapter = False  # type: ignore[assignment]
    return _SpreadsheetAdapter if _SpreadsheetAdapter is not False else None  # type: ignore[return-value]


# For backward compatibility and type hints — these will be None if deps missing
DocumentAdapter = _get_document_adapter_class()
SpreadsheetAdapter = _get_spreadsheet_adapter_class()