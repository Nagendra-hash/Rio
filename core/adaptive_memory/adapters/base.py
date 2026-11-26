"""
core/adaptive_memory/adapters/base.py — Base adapter interface.

All source adapters implement this interface for consistent ingestion.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from core.adaptive_memory.schema import (
    RawItem,
    MemoryNode,
    SourceType,
    MemoryTier,
    AccessControl,
)
from core.adaptive_memory.config import get_config


@dataclass
class AdapterCapabilities:
    """Describes what an adapter can do."""
    source_type: SourceType
    supported_formats: list[str]  # File extensions, MIME types
    supports_streaming: bool = False
    supports_incremental: bool = False
    requires_auth: bool = False
    max_file_size_mb: int = 100


class BaseAdapter(ABC):
    """Abstract base class for all source adapters."""

    # Subclasses must define these
    source_type: SourceType
    capabilities: AdapterCapabilities

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self._seen_hashes: set[str] = set()

    @abstractmethod
    async def extract(self, item: RawItem) -> list[MemoryNode]:
        """
        Extract structured MemoryNodes from a raw item.

        This is the main entry point called by the ExtractStage.
        Should return a list of initial MemoryNodes.
        """
        pass

    @abstractmethod
    async def list_sources(self) -> AsyncIterator[RawItem]:
        """
        Discover and yield available source items.

        Used for incremental/scheduled ingestion (e.g., new emails,
        modified files, new videos in playlist).
        """
        pass

    async def normalize(self, nodes: list[MemoryNode]) -> list[MemoryNode]:
        """Optional normalization hook - called after extraction."""
        return nodes

    def _make_node(
        self,
        content: str,
        source_uri: str,
        metadata: dict[str, Any],
        acl: Optional[AccessControl] = None,
    ) -> MemoryNode:
        """Helper to create a MemoryNode with standard fields."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        if content_hash in self._seen_hashes:
            # This will be filtered by DedupStage, but we can skip early
            pass
        self._seen_hashes.add(content_hash)

        return MemoryNode(
            content=content,
            content_hash=content_hash,
            source=self.source_type,
            source_uri=source_uri,
            source_metadata=metadata,
            tier=MemoryTier.WORKING,
            confidence=0.5,
            acl=acl or AccessControl(owner="system"),
        )

    async def validate_access(self, item: RawItem) -> bool:
        """Check if we have permission to access this item."""
        # Override for authenticated sources
        return True

    def get_config(self, key: str, default: Any = None) -> Any:
        """Get adapter-specific config value."""
        return self.config.get(key, default)


class AdapterRegistry:
    """Registry for managing and discovering adapters."""

    def __init__(self):
        self._adapters: dict[SourceType, BaseAdapter] = {}
        self._capabilities: dict[SourceType, AdapterCapabilities] = {}

    def register(self, adapter: BaseAdapter) -> None:
        """Register an adapter instance."""
        self._adapters[adapter.source_type] = adapter
        self._capabilities[adapter.source_type] = adapter.capabilities

    def get(self, source_type: SourceType) -> Optional[BaseAdapter]:
        """Get adapter for a source type."""
        return self._adapters.get(source_type)

    def get_capabilities(self, source_type: SourceType) -> Optional[AdapterCapabilities]:
        """Get capabilities for a source type."""
        return self._capabilities.get(source_type)

    def list_registered(self) -> list[SourceType]:
        """List all registered source types."""
        return list(self._adapters.keys())

    def create_all_from_config(self, config: dict) -> dict[SourceType, BaseAdapter]:
        """Create and register adapters from configuration."""
        from core.adaptive_memory.adapters.document import DocumentAdapter
        from core.adaptive_memory.adapters.spreadsheet import SpreadsheetAdapter

        adapter_classes = {
            SourceType.DOCUMENT: DocumentAdapter,
            SourceType.SPREADSHEET: SpreadsheetAdapter,
            # Add more as implemented
        }

        for source_type, adapter_class in adapter_classes.items():
            source_config = config.get("sources", {}).get(source_type.value, {})
            if source_config.get("enabled", False):
                adapter = adapter_class(source_config)
                self.register(adapter)

        return self._adapters


# Global registry instance
_registry: Optional[AdapterRegistry] = None


def get_registry() -> AdapterRegistry:
    global _registry
    if _registry is None:
        _registry = AdapterRegistry()
    return _registry