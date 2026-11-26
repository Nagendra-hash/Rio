"""
core/adaptive_memory/tier_manager.py — Memory tier promotion, decay, and eviction.

Manages the lifecycle of memory nodes across tiers:
WORKING → EPISODIC → SEMANTIC → (PROCEDURAL | LONG_TERM)
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from core.adaptive_memory.schema import (
    MemoryNode,
    MemoryTier,
    SourceType,
)
from core.adaptive_memory.config import get_config


@dataclass
class TierStats:
    """Statistics for a memory tier."""
    tier: MemoryTier
    node_count: int = 0
    total_size_bytes: int = 0
    avg_confidence: float = 0.0
    avg_age_days: float = 0.0
    promotion_candidates: int = 0
    decay_candidates: int = 0


class TierManager:
    """
    Manages memory tier transitions, decay, and capacity enforcement.

    Responsibilities:
    - Evaluate nodes for promotion based on confidence, access, age
    - Apply decay curves per tier
    - Enforce tier capacity limits (LRU eviction)
    - Run consolidation jobs (nightly)
    """

    def __init__(self, config: Optional[Any] = None):
        self.config = config or get_config()
        self.promotion_thresholds = self._build_promotion_thresholds()
        self.decay_rates = self._build_decay_rates()
        self.tier_limits = self._build_tier_limits()

        # In-memory tier indexes (node_id -> node)
        self._tiers: dict[MemoryTier, dict[str, MemoryNode]] = {
            tier: {} for tier in MemoryTier
        }

        # Persistence paths
        self.nodes_dir = self.config.nodes_dir
        self.nodes_dir.mkdir(parents=True, exist_ok=True)

        # Stats
        self.stats = {tier: TierStats(tier=tier) for tier in MemoryTier}

    def _build_promotion_thresholds(self) -> dict[str, dict]:
        """Build promotion threshold dict from config."""
        thresholds = {}
        for key, promo_config in self.config.promotion.items():
            thresholds[key] = {
                "min_confidence": promo_config.min_confidence,
                "min_access": promo_config.min_access,
                "min_age_days": promo_config.min_age_days,
                "min_sources": promo_config.min_sources,
                "min_pattern_count": promo_config.min_pattern_count,
            }
        return thresholds

    def _build_decay_rates(self) -> dict[MemoryTier, float]:
        """Build decay rate dict from config."""
        decay = self.config.decay
        return {
            MemoryTier.WORKING: decay.working_per_hour / 24,  # Convert to per-day
            MemoryTier.EPISODIC: decay.episodic_per_day,
            MemoryTier.SEMANTIC: decay.semantic_per_day,
            MemoryTier.PROCEDURAL: decay.procedural_per_day,
            MemoryTier.LONG_TERM: decay.long_term_per_day,
        }

    def _build_tier_limits(self) -> dict[MemoryTier, int]:
        """Build tier size limits in bytes."""
        limits = {}
        for tier_name, tier_config in self.config.tiers.items():
            try:
                tier = MemoryTier(tier_name)
                limits[tier] = tier_config.max_size_mb * 1024 * 1024
            except ValueError:
                pass
        return limits

    # ==================== NODE MANAGEMENT ====================

    def add_node(self, node: MemoryNode) -> None:
        """Add a node to its tier."""
        self._tiers[node.tier][node.id] = node
        self._update_stats(node.tier)

    def remove_node(self, node_id: str, tier: MemoryTier) -> Optional[MemoryNode]:
        """Remove a node from a tier."""
        node = self._tiers[tier].pop(node_id, None)
        self._update_stats(tier)
        return node

    def get_node(self, node_id: str, tier: MemoryTier) -> Optional[MemoryNode]:
        """Get a node from a tier."""
        return self._tiers[tier].get(node_id)

    def move_node(self, node: MemoryNode, new_tier: MemoryTier) -> None:
        """Move a node to a different tier."""
        if node.id in self._tiers[node.tier]:
            del self._tiers[node.tier][node.id]
        node.tier = new_tier
        self._tiers[new_tier][node.id] = node
        self._update_stats(node.tier)
        self._update_stats(new_tier)

    def get_tier_nodes(self, tier: MemoryTier) -> list[MemoryNode]:
        """Get all nodes in a tier."""
        return list(self._tiers[tier].values())

    def get_all_nodes(self) -> list[MemoryNode]:
        """Get all nodes across all tiers."""
        all_nodes = []
        for tier_nodes in self._tiers.values():
            all_nodes.extend(tier_nodes.values())
        return all_nodes

    # ==================== PROMOTION ====================

    def evaluate_promotion(self, node: MemoryNode) -> Optional[MemoryTier]:
        """Check if a node qualifies for promotion."""
        return node.should_promote(self.promotion_thresholds)

    def promote_node(self, node: MemoryNode) -> Optional[MemoryNode]:
        """Promote a node to the next tier if eligible."""
        new_tier = self.evaluate_promotion(node)
        if not new_tier:
            return None

        # Create promoted version
        promoted = node.promote(new_tier)

        # Update tier membership
        self.move_node(promoted, new_tier)

        return promoted

    def promote_all_eligible(self) -> dict[MemoryTier, int]:
        """Promote all eligible nodes across all tiers."""
        promoted_counts = defaultdict(int)

        for tier in [MemoryTier.WORKING, MemoryTier.EPISODIC, MemoryTier.SEMANTIC]:
            nodes = self.get_tier_nodes(tier)
            for node in nodes:
                if self.promote_node(node):
                    promoted_counts[tier] += 1

        return dict(promoted_counts)

    # ==================== DECAY ====================

    def apply_decay(self, node: MemoryNode) -> float:
        """Apply decay to a node's confidence. Returns new confidence."""
        days = node.age_days()
        decay_rate = self.decay_rates.get(node.tier, 0.0)

        if decay_rate == 0.0:
            return node.confidence

        # Exponential decay: confidence * (1 - rate)^days
        effective_rate = node.effective_decay()
        new_confidence = node.confidence * (1 - effective_rate) ** days

        node.confidence = max(0.0, new_confidence)
        node.updated_at = datetime.utcnow()

        return node.confidence

    def apply_decay_all(self) -> dict[MemoryTier, int]:
        """Apply decay to all nodes. Returns count of decayed nodes per tier."""
        decayed_counts = defaultdict(int)

        for tier, nodes in self._tiers.items():
            for node in nodes.values():
                old_conf = node.confidence
                self.apply_decay(node)
                if node.confidence < old_conf:
                    decayed_counts[tier] += 1

        return dict(decayed_counts)

    def get_decay_candidates(self, threshold: float = 0.1) -> list[MemoryNode]:
        """Get nodes whose confidence has decayed below threshold."""
        candidates = []
        for nodes in self._tiers.values():
            for node in nodes.values():
                if node.should_decay(threshold):
                    candidates.append(node)
        return candidates

    # ==================== CAPACITY ENFORCEMENT ====================

    def enforce_capacity(self) -> dict[MemoryTier, int]:
        """Enforce tier capacity limits via LRU eviction."""
        evicted_counts = defaultdict(int)

        for tier, limit_bytes in self.tier_limits.items():
            nodes = self.get_tier_nodes(tier)
            total_size = sum(n.processing_metadata.get("size_bytes", 1000) for n in nodes)

            if total_size <= limit_bytes:
                continue

            # Sort by last accessed (LRU first)
            nodes.sort(key=lambda n: n.accessed_at)

            # Evict oldest until under limit
            for node in nodes:
                if total_size <= limit_bytes:
                    break

                self.remove_node(node.id, tier)
                total_size -= node.processing_metadata.get("size_bytes", 1000)
                evicted_counts[tier] += 1

        return dict(evicted_counts)

    # ==================== CONSOLIDATION ====================

    async def run_consolidation(self) -> dict[str, Any]:
        """
        Run nightly consolidation job.

        Returns summary of actions taken.
        """
        start_time = time.time()
        results = {
            "timestamp": datetime.utcnow().isoformat(),
            "promotions": {},
            "decay": {},
            "evictions": {},
            "errors": [],
        }

        try:
            # 1. Apply decay to all nodes
            results["decay"] = self.apply_decay_all()

            # 2. Promote eligible nodes
            results["promotions"] = self.promote_all_eligible()

            # 3. Enforce capacity limits
            results["evictions"] = self.enforce_capacity()

            # 4. Persist changes
            await self.persist_all()

        except Exception as e:
            results["errors"].append(str(e))

        results["duration_ms"] = (time.time() - start_time) * 1000
        return results

    # ==================== PERSISTENCE ====================

    async def persist_all(self) -> None:
        """Persist all tiers to disk."""
        for tier in MemoryTier:
            await self._persist_tier(tier)

    async def _persist_tier(self, tier: MemoryTier) -> None:
        """Persist a single tier to JSONL."""
        tier_file = self.nodes_dir / f"{tier.value}.jsonl"
        nodes = self.get_tier_nodes(tier)

        # Write atomically
        temp_file = tier_file.with_suffix(".tmp")
        with open(temp_file, "w") as f:
            for node in nodes:
                f.write(node.model_dump_json() + "\n")

        temp_file.replace(tier_file)

    async def load_all(self) -> dict[MemoryTier, int]:
        """Load all tiers from disk."""
        loaded = {}

        for tier in MemoryTier:
            count = await self._load_tier(tier)
            loaded[tier] = count

        return loaded

    async def _load_tier(self, tier: MemoryTier) -> int:
        """Load a single tier from JSONL."""
        tier_file = self.nodes_dir / f"{tier.value}.jsonl"
        if not tier_file.exists():
            return 0

        count = 0
        with open(tier_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    node = MemoryNode(**data)
                    self._tiers[tier][node.id] = node
                    count += 1
                except Exception:
                    continue  # Skip corrupt lines

        self._update_stats(tier)
        return count

    def _update_stats(self, tier: MemoryTier) -> None:
        """Update statistics for a tier."""
        nodes = self.get_tier_nodes(tier)
        if not nodes:
            self.stats[tier] = TierStats(tier=tier)
            return

        total_size = sum(n.processing_metadata.get("size_bytes", 1000) for n in nodes)
        avg_conf = sum(n.confidence for n in nodes) / len(nodes)
        avg_age = sum(n.age_days() for n in nodes) / len(nodes)

        # Count promotion candidates
        promo_candidates = sum(1 for n in nodes if n.should_promote(self.promotion_thresholds))
        decay_candidates = sum(1 for n in nodes if n.should_decay())

        self.stats[tier] = TierStats(
            tier=tier,
            node_count=len(nodes),
            total_size_bytes=total_size,
            avg_confidence=avg_conf,
            avg_age_days=avg_age,
            promotion_candidates=promo_candidates,
            decay_candidates=decay_candidates,
        )

    def get_stats(self) -> dict[str, Any]:
        """Get comprehensive tier statistics."""
        return {
            tier.value: {
                "node_count": s.node_count,
                "size_mb": s.total_size_bytes / (1024 * 1024),
                "avg_confidence": round(s.avg_confidence, 3),
                "avg_age_days": round(s.avg_age_days, 1),
                "promotion_candidates": s.promotion_candidates,
                "decay_candidates": s.decay_candidates,
                "limit_mb": self.tier_limits.get(tier, 0) / (1024 * 1024),
                "utilization_pct": round(
                    s.total_size_bytes / self.tier_limits.get(tier, 1) * 100, 1
                ) if self.tier_limits.get(tier, 0) > 0 else 0,
            }
            for tier, s in self.stats.items()
        }

    # ==================== QUERY HELPERS ====================

    def search_by_entity(self, entity: str, tiers: Optional[list[MemoryTier]] = None) -> list[MemoryNode]:
        """Find nodes containing an entity."""
        tiers = tiers or list(MemoryTier)
        results = []
        for tier in tiers:
            for node in self._tiers[tier].values():
                if entity.lower() in [e.lower() for e in node.entities]:
                    results.append(node)
        return results

    def search_by_tag(self, tag: str, tiers: Optional[list[MemoryTier]] = None) -> list[MemoryNode]:
        """Find nodes with a tag."""
        tiers = tiers or list(MemoryTier)
        results = []
        for tier in tiers:
            for node in self._tiers[tier].values():
                if tag.lower() in [t.lower() for t in node.tags]:
                    results.append(node)
        return results

    def get_recent(self, hours: int = 24, tiers: Optional[list[MemoryTier]] = None) -> list[MemoryNode]:
        """Get nodes created/accessed in the last N hours."""
        tiers = tiers or list(MemoryTier)
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        results = []
        for tier in tiers:
            for node in self._tiers[tier].values():
                if node.created_at >= cutoff or node.accessed_at >= cutoff:
                    results.append(node)
        return sorted(results, key=lambda n: n.accessed_at, reverse=True)