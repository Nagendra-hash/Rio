"""
core/adaptive_memory/schema.py — Core data models for adaptive memory.

All models use Pydantic v2 for validation and serialization.
Designed for: vector storage, graph storage, audit logging, and tier migration.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class SourceType(str, Enum):
    """Origin of the knowledge."""
    WEB = "web"
    YOUTUBE = "youtube"
    EMAIL = "email"
    CALENDAR = "calendar"
    CHAT = "chat"
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    CODE = "code"
    TRANSCRIPT = "transcript"
    SCREEN = "screen"
    USER_FEEDBACK = "user_feedback"
    AGENT_OUTPUT = "agent_output"


class MemoryTier(str, Enum):
    """Memory consolidation tier — higher = more permanent."""
    WORKING = "working"        # < 1 hour, high accessibility, LRU eviction
    EPISODIC = "episodic"      # Conversation turns, events, 30-day TTL
    SEMANTIC = "semantic"      # Facts, concepts, relationships, ∞ TTL
    PROCEDURAL = "procedural"  # Skills, workflows, patterns, ∞ TTL
    LONG_TERM = "long_term"    # Consolidated, high-confidence, verified facts


class ConfidenceLevel(float):
    """Confidence score 0.0–1.0 with validation."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        from pydantic_core import core_schema
        return core_schema.float_schema(ge=0.0, le=1.0)


class AccessControl(BaseModel):
    """Access control for a memory node."""
    owner: str
    readers: list[str] = Field(default_factory=list)  # user IDs, "public", "agents:*"
    writers: list[str] = Field(default_factory=list)
    sensitivity: Literal["public", "private", "sensitive", "secret"] = "private"

    def can_read(self, requester: str) -> bool:
        if self.sensitivity == "public":
            return True
        if requester == self.owner:
            return True
        if "public" in self.readers:
            return True
        if requester in self.readers:
            return True
        if any(requester.startswith(p.rstrip("*")) for p in self.readers if p.endswith("*")):
            return True
        return False

    def can_write(self, requester: str) -> bool:
        if requester == self.owner:
            return True
        if requester in self.writers:
            return True
        if any(requester.startswith(p.rstrip("*")) for p in self.writers if p.endswith("*")):
            return True
        return False


class Relation(BaseModel):
    """Entity-relation triple with provenance."""
    subject: str
    predicate: str
    object: str
    confidence: ConfidenceLevel = 0.5
    source_node_ids: list[str] = Field(default_factory=list)  # Evidence node IDs
    extracted_at: datetime = Field(default_factory=datetime.utcnow)
    extractor_version: str = "1.0"


class MemoryNode(BaseModel):
    """
    Atomic unit of knowledge with full provenance and lifecycle tracking.

    Lifecycle: WORKING → EPISODIC → SEMANTIC → (PROCEDURAL | LONG_TERM)
    """
    # Identity
    id: str = Field(default_factory=lambda: f"mem_{uuid.uuid4().hex}")
    content: str
    content_hash: str = Field(default="", exclude=True)  # SHA256, computed

    # Source provenance
    source: SourceType
    source_uri: str  # Original URL, file path, message ID, etc.
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    # Tier & confidence
    tier: MemoryTier = MemoryTier.WORKING
    confidence: ConfidenceLevel = 0.5

    # Extracted structure
    entities: list[str] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)  # References to other node IDs

    # Temporal
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    accessed_at: datetime = Field(default_factory=datetime.utcnow)
    access_count: int = 0

    # Decay & versioning
    decay_rate: float = 0.01  # Per day, tier-specific override in TierManager
    acl: AccessControl
    version: int = 1
    supersedes: Optional[str] = None  # Previous version ID
    superseded_by: Optional[str] = None  # Next version ID

    # Processing metadata
    processing_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def compute_hash(self) -> MemoryNode:
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.content.encode()).hexdigest()[:16]
        return self

    def touch(self) -> None:
        """Record access for decay/promotion calculations."""
        self.accessed_at = datetime.utcnow()
        self.access_count += 1

    def promote(self, new_tier: MemoryTier) -> MemoryNode:
        """Create a new version at a higher tier."""
        new_node = self.model_copy(deep=True)
        new_node.id = f"mem_{uuid.uuid4().hex}"
        new_node.tier = new_tier
        new_node.version = self.version + 1
        new_node.supersedes = self.id
        new_node.created_at = datetime.utcnow()
        new_node.updated_at = datetime.utcnow()
        self.superseded_by = new_node.id
        return new_node

    def update_content(self, new_content: str, confidence_delta: float = 0.0) -> MemoryNode:
        """Create new version with updated content."""
        new_node = self.model_copy(deep=True)
        new_node.id = f"mem_{uuid.uuid4().hex}"
        new_node.content = new_content
        new_node.content_hash = hashlib.sha256(new_content.encode()).hexdigest()[:16]
        new_node.version = self.version + 1
        new_node.confidence = max(0.0, min(1.0, self.confidence + confidence_delta))
        new_node.supersedes = self.id
        new_node.updated_at = datetime.utcnow()
        self.superseded_by = new_node.id
        return new_node

    def age_days(self) -> float:
        return (datetime.utcnow() - self.created_at).total_seconds() / 86400

    def effective_decay(self) -> float:
        """Daily decay rate adjusted by access frequency."""
        if self.access_count == 0:
            return self.decay_rate
        # Reduce decay with access (spacing effect)
        return self.decay_rate / (1 + self.access_count * 0.1)

    def should_promote(self, thresholds: dict) -> Optional[MemoryTier]:
        """Check if node meets promotion criteria for next tier."""
        next_tiers = {
            MemoryTier.WORKING: MemoryTier.EPISODIC,
            MemoryTier.EPISODIC: MemoryTier.SEMANTIC,
            MemoryTier.SEMANTIC: MemoryTier.LONG_TERM,
        }
        if self.tier not in next_tiers:
            return None

        next_tier = next_tiers[self.tier]
        thresh = thresholds.get(f"{self.tier.value}_to_{next_tier.value}", {})

        if self.confidence < thresh.get("min_confidence", 0.0):
            return None
        if self.access_count < thresh.get("min_access", 0):
            return None
        if self.age_days() < thresh.get("min_age_days", 0):
            return None

        return next_tier

    def should_decay(self, threshold: float = 0.1) -> bool:
        """Check if confidence has decayed below threshold."""
        days = self.age_days()
        decayed_confidence = self.confidence * (1 - self.effective_decay()) ** days
        return decayed_confidence < threshold


class RawItem(BaseModel):
    """Raw input from a source adapter before processing."""
    source: SourceType
    source_uri: str
    raw_content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    acl: Optional[AccessControl] = None


class IngestionResult(BaseModel):
    """Result of processing a batch of raw items."""
    nodes_created: int = 0
    nodes_updated: int = 0
    nodes_merged: int = 0
    nodes_rejected: int = 0
    errors: list[dict] = Field(default_factory=list)
    processing_time_ms: float = 0.0