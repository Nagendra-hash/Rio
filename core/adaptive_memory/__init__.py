"""
core/adaptive_memory — Unified adaptive learning & persistent memory system.

Public API:
    MemoryNode, Relation, AccessControl — Core data models
    SourceType, MemoryTier, ConfidenceLevel — Enums and types
    RawItem, IngestionResult — Pipeline I/O models
    IngestionPipeline — Main pipeline orchestrator
    DocumentAdapter, SpreadsheetAdapter — Source adapters
    TierManager — Tier promotion/decay/eviction manager
    RAGServiceAdapter — Adapter for core.rag_service functional API
    get_config, load_config, save_config — Configuration management

All models use Pydantic v2 for validation and serialization.
Designed for: vector storage, graph storage, audit logging, and tier migration.

Lifecycle: WORKING → EPISODIC → SEMANTIC → (PROCEDURAL | LONG_TERM)
"""
from __future__ import annotations

from core.adaptive_memory.schema import (
    MemoryNode,
    Relation,
    AccessControl,
    SourceType,
    MemoryTier,
    ConfidenceLevel,
    RawItem,
    IngestionResult,
)
from core.adaptive_memory.pipeline import (
    IngestionPipeline,
)
from core.adaptive_memory.adapters.document import (
    DocumentAdapter,
)
from core.adaptive_memory.adapters.spreadsheet import (
    SpreadsheetAdapter,
)
from core.adaptive_memory.tier_manager import (
    TierManager,
)
from core.adaptive_memory.rag_adapter import (
    RAGServiceAdapter,
)
from core.adaptive_memory.config import (
    get_config,
    load_config,
    save_config,
)

__all__ = [
    "MemoryNode",
    "Relation",
    "AccessControl",
    "SourceType",
    "MemoryTier",
    "ConfidenceLevel",
    "RawItem",
    "IngestionResult",
    "IngestionPipeline",
    "DocumentAdapter",
    "SpreadsheetAdapter",
    "TierManager",
    "RAGServiceAdapter",
    "get_config",
    "load_config",
    "save_config",
]