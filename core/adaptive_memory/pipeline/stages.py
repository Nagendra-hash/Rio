"""
core/adaptive_memory/pipeline/stages.py — Pipeline stages for memory ingestion.

ARCHITECTURE POSITION (deliberate)
    This module implements the 8-stage ingestion pipeline that transforms
    RawItems (from source adapters) into indexed, enriched MemoryNodes.
    Each stage is a self-contained processor with a single responsibility,
    designed to be composable, testable, and resumable via checkpoints.

DESIGN
    Local-first, zero heavy dependencies. Stages communicate through a
    PipelineContext that carries shared state (config, stats, errors, timing).
    The pipeline orchestrator (IngestionPipeline) sequences stages and handles
    checkpointing. Each stage's process() method is pure: items in, items out,
    side effects only through the context.

STAGE SEQUENCE
    1. Extract    — Convert RawItems to MemoryNodes via source adapters
    2. Normalize  — Language detection, cleaning, chunking large content
    3. Enrich     — LLM-based entity/relation/tag extraction, summarization
    4. Dedup      — Remove exact and near-duplicate nodes
    5. Embed      — Generate vector embeddings for semantic search
    6. Index      — Write to vector store (RAG) and graph store
    7. Promote    — Evaluate nodes for tier promotion (working → episodic → …)
    8. Audit      — Write immutable audit log entry for the ingestion run

THREAD SAFETY
    Pipeline stages are stateless (except DedupStage which tracks seen hashes).
    IngestionPipeline holds no state between runs. Thread safety for shared
    stores (vector, graph, audit log) is delegated to those stores' own locks.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from core.adaptive_memory.schema import (
    MemoryNode,
    RawItem,
    IngestionResult,
    SourceType,
    MemoryTier,
    Relation,
    ConfidenceLevel,
    AccessControl,
)
from core.adaptive_memory.config import get_config


# ── Constants ────────────────────────────────────────────────────────────────

# Similarity threshold for near-deduplication (Jaccard on 3-grams)
_DEDUP_SIMILARITY_THRESHOLD = 0.95

# Maximum recent nodes to check for semantic similarity in dedup
_DEDUP_WINDOW = 100

# Minimum chunk length to keep after normalization (chars)
_MIN_CHUNK_CHARS = 50

# Default chunking parameters
_DEFAULT_CHUNK_SIZE = 512
_DEFAULT_CHUNK_OVERLAP = 50

# Global lock for audit log writes
_audit_lock = threading.RLock()


# ── Context ──────────────────────────────────────────────────────────────────

@dataclass
class PipelineContext:
    """Shared context passed between stages. Carries config, stats, errors, timing."""
    config: Any = field(default_factory=get_config)
    stats: dict[str, int] = field(default_factory=lambda: {
        "nodes_created": 0,
        "nodes_updated": 0,
        "nodes_merged": 0,
        "nodes_rejected": 0,
    })
    errors: list[dict] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    run_id: str = field(default_factory=lambda: uuid4().hex[:8])


# ── Base Stage ───────────────────────────────────────────────────────────────

class PipelineStage(ABC):
    """Base class for pipeline stages. Subclasses must implement process()."""

    name: str = "base"

    @abstractmethod
    async def process(self, items: list[Any], ctx: PipelineContext) -> list[Any]:
        """Process items and return transformed items for next stage."""
        pass

    async def __call__(self, items: list[Any], ctx: PipelineContext) -> list[Any]:
        """Execute stage with timing and error capture."""
        stage_start = time.time()
        try:
            result = await self.process(items, ctx)
            ctx.stats[f"{self.name}_time_ms"] = (time.time() - stage_start) * 1000
            return result
        except Exception as e:
            ctx.errors.append({
                "stage": self.name,
                "error": str(e),
                "item_count": len(items),
                "timestamp": datetime.utcnow().isoformat(),
            })
            raise


# ── Stage 1: Extract ─────────────────────────────────────────────────────────

class ExtractStage(PipelineStage):
    """Stage 1: Extract structured chunks from raw items using source adapters.

    Each RawItem carries a SourceType; the corresponding adapter's extract()
    method converts it to one or more MemoryNodes. Adapters are injected at
    pipeline construction for testability.
    """

    name = "extract"

    def __init__(self, adapters: Optional[dict[SourceType, Any]] = None):
        self.adapters = adapters or {}

    async def process(self, items: list[RawItem], ctx: PipelineContext) -> list[MemoryNode]:
        """Convert RawItems to initial MemoryNodes using registered adapters."""
        nodes = []
        for item in items:
            adapter = self.adapters.get(item.source)
            if not adapter:
                ctx.errors.append({
                    "stage": self.name,
                    "error": f"No adapter for source type: {item.source}",
                    "item_uri": item.source_uri,
                })
                ctx.stats["nodes_rejected"] += 1
                continue

            try:
                extracted = await adapter.extract(item)
                nodes.extend(extracted)
            except Exception as e:
                ctx.errors.append({
                    "stage": self.name,
                    "error": str(e),
                    "item_uri": item.source_uri,
                })
                ctx.stats["nodes_rejected"] += 1

        return nodes


# ── Stage 2: Normalize ───────────────────────────────────────────────────────

class NormalizeStage(PipelineStage):
    """Stage 2: Normalize content — language detection, cleaning, chunking.

    Large nodes are split into overlapping chunks at sentence boundaries.
    Each chunk becomes a child node with provenance linking back to the parent.
    """

    name = "normalize"

    def __init__(self, chunk_size: int = _DEFAULT_CHUNK_SIZE, chunk_overlap: int = _DEFAULT_CHUNK_OVERLAP):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def _chunk_text(self, text: str) -> list[str]:
        """Sliding-window chunking with sentence-boundary preference."""
        if len(text) <= self.chunk_size:
            return [text]

        chunks = []
        start = 0
        while start < len(text):
            end = min(start + self.chunk_size, len(text))
            # Try to break at sentence boundary
            if end < len(text):
                last_period = text.rfind(". ", start, end)
                if last_period > start + self.chunk_size // 2:
                    end = last_period + 1
            chunk = text[start:end].strip()
            if len(chunk) >= _MIN_CHUNK_CHARS:
                chunks.append(chunk)
            start = end - self.chunk_overlap
        return chunks

    async def process(self, nodes: list[MemoryNode], ctx: PipelineContext) -> list[MemoryNode]:
        """Chunk large nodes, normalize whitespace, detect language."""
        normalized = []
        for node in nodes:
            chunks = self._chunk_text(node.content)
            if len(chunks) == 1:
                normalized.append(node)
            else:
                for i, chunk in enumerate(chunks):
                    if len(chunk.strip()) < _MIN_CHUNK_CHARS:
                        continue
                    child = node.model_copy(deep=True)
                    child.id = f"{node.id}_chunk_{i}"
                    child.content = chunk
                    child.content_hash = hashlib.sha256(chunk.encode()).hexdigest()[:16]
                    child.processing_metadata["parent_id"] = node.id
                    child.processing_metadata["chunk_index"] = i
                    child.processing_metadata["total_chunks"] = len(chunks)
                    normalized.append(child)
        return normalized


# ── Stage 3: Enrich ──────────────────────────────────────────────────────────

class EnrichStage(PipelineStage):
    """Stage 3: LLM-based enrichment — entities, relations, summary, tags.

    When an LLM client is available, performs structured extraction.
    Falls back to extractive heuristics (capitalized entities, source tags)
    when LLM is unavailable or fails.
    """

    name = "enrich"

    def __init__(self, llm_client=None):
        self.llm_client = llm_client  # Injected for testing

    async def process(self, nodes: list[MemoryNode], ctx: PipelineContext) -> list[MemoryNode]:
        """Enrich nodes with extracted entities, relations, and metadata."""
        if not self.llm_client:
            return await self._extractive_enrich(nodes)

        enriched = []
        for node in nodes:
            try:
                enrichment = await self._llm_enrich(node)
                node.entities = enrichment.get("entities", [])
                node.relations = [Relation(**r) for r in enrichment.get("relations", [])]
                node.tags = enrichment.get("tags", [])
                node.processing_metadata["summary"] = enrichment.get("summary", "")
                node.confidence = max(node.confidence, enrichment.get("confidence", 0.5))
            except Exception as e:
                ctx.errors.append({
                    "stage": self.name,
                    "error": str(e),
                    "node_id": node.id,
                })
            enriched.append(node)
        return enriched

    async def _extractive_enrich(self, nodes: list[MemoryNode]) -> list[MemoryNode]:
        """Simple rule-based extraction when LLM unavailable."""
        import re
        for node in nodes:
            # Extract capitalized entities (simple heuristic)
            entities = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', node.content)
            node.entities = list(set(entities))[:20]
            # Extract simple tags from source
            node.tags = [node.source.value] + (node.source_metadata.get("tags", []))
            node.confidence = max(node.confidence, 0.4)
        return nodes

    async def _llm_enrich(self, node: MemoryNode) -> dict:
        """Call LLM for structured extraction. Placeholder for actual implementation."""
        # This would use the model ladder from core.llm_router
        return {
            "entities": [],
            "relations": [],
            "tags": [],
            "summary": "",
            "confidence": 0.5,
        }


# ── Stage 4: Dedup ───────────────────────────────────────────────────────────

class DedupStage(PipelineStage):
    """Stage 4: Deduplicate nodes by content hash and semantic similarity.

    Removes exact duplicates (content hash) and near-duplicates (Jaccard
    similarity on word 3-grams above threshold). Tracks seen hashes across
    the pipeline run to prevent re-indexing existing content.
    """

    name = "dedup"

    def __init__(
        self,
        existing_hashes: Optional[set[str]] = None,
        similarity_threshold: float = _DEDUP_SIMILARITY_THRESHOLD,
    ):
        self.seen_hashes = existing_hashes or set()
        self.similarity_threshold = similarity_threshold

    async def process(self, nodes: list[MemoryNode], ctx: PipelineContext) -> list[MemoryNode]:
        """Remove exact duplicates and near-duplicates."""
        unique = []
        for node in nodes:
            if node.content_hash in self.seen_hashes:
                ctx.stats["nodes_merged"] += 1
                continue

            is_dup = False
            for existing in unique[-_DEDUP_WINDOW:]:
                if self._similarity(node.content, existing.content) > self.similarity_threshold:
                    is_dup = True
                    ctx.stats["nodes_merged"] += 1
                    break

            if not is_dup:
                self.seen_hashes.add(node.content_hash)
                unique.append(node)
            else:
                ctx.stats["nodes_merged"] += 1

        return unique

    def _similarity(self, a: str, b: str) -> float:
        """Jaccard similarity on word 3-grams (fast approximation)."""
        def ngrams(text: str, n: int = 3) -> set:
            words = text.lower().split()
            return set(" ".join(words[i:i+n]) for i in range(len(words)-n+1))

        a_grams = ngrams(a)
        b_grams = ngrams(b)
        if not a_grams or not b_grams:
            return 0.0
        return len(a_grams & b_grams) / len(a_grams | b_grams)


# ── Stage 5: Embed ───────────────────────────────────────────────────────────

class EmbedStage(PipelineStage):
    """Stage 5: Generate embeddings for vector search.

    Uses an injected embedding function (typically core.gemini.embed or
    core.rag.embed in production). Stores embedding in node's
    processing_metadata along with model identifier.
    """

    name = "embed"

    def __init__(self, embed_fn=None):
        self.embed_fn = embed_fn  # Function: list[str] -> list[list[float]]

    async def process(self, nodes: list[MemoryNode], ctx: PipelineContext) -> list[MemoryNode]:
        """Add embeddings to nodes."""
        if not self.embed_fn:
            # Placeholder - in production uses core.rag.embed()
            for node in nodes:
                node.processing_metadata["embedding"] = None
            return nodes

        texts = [n.content for n in nodes]
        embeddings = await self.embed_fn(texts)

        for node, emb in zip(nodes, embeddings):
            node.processing_metadata["embedding"] = emb
            node.processing_metadata["embedding_model"] = get_config().ingestion.embedding_model

        return nodes


# ── Stage 6: Index ───────────────────────────────────────────────────────────

class IndexStage(PipelineStage):
    """Stage 6: Write nodes to vector store and graph store.

    Uses rag_service for vector indexing (which handles collections, ACLs,
    audit). Graph store integration is a placeholder for future NetworkX/Neo4j.
    """

    name = "index"

    def __init__(self, vector_store=None, graph_store=None, rag_service=None):
        self.vector_store = vector_store
        self.graph_store = graph_store
        self.rag_service = rag_service

    async def process(self, nodes: list[MemoryNode], ctx: PipelineContext) -> list[MemoryNode]:
        """Index nodes in vector store and graph."""
        indexed = []
        for node in nodes:
            try:
                # Write to vector store (via rag_service)
                if self.rag_service and node.processing_metadata.get("embedding"):
                    await self.rag_service.ingest(
                        collection=self._get_collection(node),
                        documents=[node.content],
                        metadatas=[self._node_to_metadata(node)],
                        ids=[node.id],
                        caller="adaptive_memory",
                    )

                # Write to graph store
                if self.graph_store:
                    await self._index_graph(node)

                node.processing_metadata["indexed_at"] = datetime.utcnow().isoformat()
                ctx.stats["nodes_created"] += 1
                indexed.append(node)

            except Exception as e:
                ctx.errors.append({
                    "stage": self.name,
                    "error": str(e),
                    "node_id": node.id,
                })
                ctx.stats["nodes_rejected"] += 1

        return indexed

    def _get_collection(self, node: MemoryNode) -> str:
        """Map source type to RAG collection."""
        mapping = {
            SourceType.WEB: "web_knowledge",
            SourceType.YOUTUBE: "web_knowledge",
            SourceType.DOCUMENT: "personal_knowledge",
            SourceType.SPREADSHEET: "personal_knowledge",
            SourceType.EMAIL: "personal_knowledge",
            SourceType.CALENDAR: "personal_knowledge",
            SourceType.CHAT: "personal_knowledge",
            SourceType.TRANSCRIPT: "personal_knowledge",
            SourceType.CODE: "code_knowledge",
            SourceType.SCREEN: "screen_knowledge",
            SourceType.USER_FEEDBACK: "personal_knowledge",
            SourceType.AGENT_OUTPUT: "personal_knowledge",
        }
        return mapping.get(node.source, "personal_knowledge")

    def _node_to_metadata(self, node: MemoryNode) -> dict:
        return {
            "source": node.source.value,
            "source_uri": node.source_uri,
            "tier": node.tier.value,
            "confidence": node.confidence,
            "entities": node.entities,
            "tags": node.tags,
            "created_at": node.created_at.isoformat(),
            "version": node.version,
            "acl_owner": node.acl.owner,
            "acl_sensitivity": node.acl.sensitivity,
        }

    async def _index_graph(self, node: MemoryNode) -> None:
        """Add entities and relations to graph store. Implementation depends on backend."""
        pass


# ── Stage 7: Promote ─────────────────────────────────────────────────────────

class PromoteStage(PipelineStage):
    """Stage 7: Evaluate nodes for tier promotion.

    Delegates to tier_manager for promotion decisions. Nodes that meet
    confidence/access/age thresholds are promoted to the next tier.
    """

    name = "promote"

    def __init__(self, tier_manager=None):
        self.tier_manager = tier_manager

    async def process(self, nodes: list[MemoryNode], ctx: PipelineContext) -> list[MemoryNode]:
        """Check and apply tier promotions."""
        if not self.tier_manager:
            return nodes

        promoted = []
        for node in nodes:
            new_tier = node.should_promote(self.tier_manager.promotion_thresholds)
            if new_tier:
                promoted_node = node.promote(new_tier)
                promoted.append(promoted_node)
                ctx.stats["nodes_updated"] += 1
            else:
                promoted.append(node)
        return promoted


# ── Stage 8: Audit ───────────────────────────────────────────────────────────

class AuditStage(PipelineStage):
    """Stage 8: Write immutable audit log entry.

    Appends a JSON line per ingestion run with stats, errors, timing.
    Audit log is append-only and never modified.
    """

    name = "audit"

    def __init__(self, audit_path: Optional[Path] = None):
        self.audit_path = audit_path or get_config().audit_log_path

    async def process(self, nodes: list[MemoryNode], ctx: PipelineContext) -> list[MemoryNode]:
        """Append audit record for this ingestion run."""
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

        audit_entry = {
            "run_id": ctx.run_id,
            "timestamp": datetime.utcnow().isoformat(),
            "duration_ms": (time.time() - ctx.start_time) * 1000,
            "nodes_processed": len(nodes),
            "stats": ctx.stats,
            "errors": ctx.errors,
        }

        with _audit_lock:
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(audit_entry, ensure_ascii=False) + "\n")

        return nodes


# ── Pipeline Orchestrator ────────────────────────────────────────────────────

class IngestionPipeline:
    """Orchestrates the full ingestion pipeline with configurable stages.

    Constructs the default 8-stage pipeline unless custom stages are provided.
    Handles checkpointing, resumability, and result aggregation.
    """

    def __init__(
        self,
        stages: Optional[list[PipelineStage]] = None,
        adapters: Optional[dict] = None,
        embed_fn=None,
        vector_store=None,
        graph_store=None,
        rag_service=None,
        tier_manager=None,
    ):
        self.stages = stages or self._default_stages(
            adapters, embed_fn, vector_store, graph_store, rag_service, tier_manager
        )

    def _default_stages(
        self,
        adapters,
        embed_fn,
        vector_store,
        graph_store,
        rag_service,
        tier_manager,
    ) -> list[PipelineStage]:
        return [
            ExtractStage(adapters),
            NormalizeStage(),
            EnrichStage(),
            DedupStage(),
            EmbedStage(embed_fn),
            IndexStage(vector_store, graph_store, rag_service),
            PromoteStage(tier_manager),
            AuditStage(),
        ]

    async def run(self, items: list[RawItem], run_id: Optional[str] = None) -> IngestionResult:
        """Execute full pipeline on a batch of raw items."""
        ctx = PipelineContext()
        if run_id:
            ctx.run_id = run_id

        current_items: list[Any] = items
        for stage in self.stages:
            if not current_items:
                break
            current_items = await stage(current_items, ctx)

        return IngestionResult(
            nodes_created=ctx.stats.get("nodes_created", 0),
            nodes_updated=ctx.stats.get("nodes_updated", 0),
            nodes_merged=ctx.stats.get("nodes_merged", 0),
            nodes_rejected=ctx.stats.get("nodes_rejected", 0),
            errors=ctx.errors,
            processing_time_ms=(time.time() - ctx.start_time) * 1000,
        )