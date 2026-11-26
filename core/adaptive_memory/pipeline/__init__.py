"""
core/adaptive_memory/pipeline — Ingestion pipeline stages and orchestration.

ARCHITECTURE POSITION
    This module is the PROCESSING ENGINE behind the adaptive memory system.
    It sits between the raw input adapters (core/adaptive_memory/adapters)
    and the storage layer (RAG service, vector store, graph store).
    The pipeline never talks to the model conversation directly — it only
    transforms and indexes data.

DESIGN
    Composable, resumable, and observable. Each stage is a self-contained
    processor implementing the PipelineStage interface. Stages can be
    reordered, replaced, or skipped without breaking the pipeline contract.

    Checkpointing: Every stage can persist its progress to disk, allowing
    the pipeline to resume after crashes or interruptions. Checkpoints are
    stored under memory/adaptive_memory/checkpoints/<run_id>/.

    Parallelism: WorkerPool provides controlled concurrency with backpressure,
    rate limiting, and per-item timeouts. BatchProcessor adds checkpointing
    on top for large-scale ingestion runs.

PIPELINE STAGES (default order)
    1. ExtractStage      — Convert RawItems to MemoryNodes via source adapters
    2. NormalizeStage    — Chunk text, normalize whitespace, detect language
    3. EnrichStage       — LLM or extractive enrichment (entities, relations, tags)
    4. DedupStage        — Exact and semantic deduplication
    5. EmbedStage        — Generate vector embeddings for semantic search
    6. IndexStage        — Write to vector store (via RAG service) and graph store
    7. PromoteStage      — Evaluate and apply tier promotions
    8. AuditStage        — Append immutable audit log entry

CONFIGURATION
    All tunables live in core/adaptive_memory/config.py (Pydantic Settings).
    Key knobs: chunk_size, chunk_overlap, similarity_threshold, embedding_model,
    promotion_thresholds, audit_log_path, checkpoints_dir.

USAGE
    from core.adaptive_memory.pipeline import IngestionPipeline, RawItem, SourceType

    pipeline = IngestionPipeline()
    items = [RawItem(source=SourceType.DOCUMENT, source_uri="...", content="...")]
    result = await pipeline.run(items)

    # For resumable runs with checkpoints:
    from core.adaptive_memory.pipeline import CheckpointManager, BatchProcessor
    checkpoint_mgr = CheckpointManager()
    processor = BatchProcessor(my_processor, checkpoint_manager=checkpoint_mgr, run_id="run-123")
    await processor.process_all(items, resume=True)

THREAD SAFETY
    Pipeline stages are stateless by design (except DedupStage which tracks
    seen hashes). Multiple pipelines can run concurrently. CheckpointManager
    uses file-based locking implicitly via atomic writes.
"""
from __future__ import annotations

from core.adaptive_memory.pipeline.stages import (
    IngestionPipeline,
    PipelineStage,
    PipelineContext,
    ExtractStage,
    NormalizeStage,
    EnrichStage,
    DedupStage,
    EmbedStage,
    IndexStage,
    PromoteStage,
    AuditStage,
)
from core.adaptive_memory.pipeline.checkpoint import CheckpointManager, run_with_checkpoint
from core.adaptive_memory.pipeline.parallel import (
    WorkerPool,
    WorkerStats,
    map_parallel,
)

# Pipeline stage order constant (for debugging / inspection)
DEFAULT_STAGE_NAMES = (
    "extract",
    "normalize",
    "enrich",
    "dedup",
    "embed",
    "index",
    "promote",
    "audit",
)

# Current pipeline version (bump when stage interface changes)
PIPELINE_VERSION = 1

__all__ = [
    # Core pipeline
    "IngestionPipeline",
    "PipelineStage",
    "PipelineContext",
    # Stages
    "ExtractStage",
    "NormalizeStage",
    "EnrichStage",
    "DedupStage",
    "EmbedStage",
    "IndexStage",
    "PromoteStage",
    "AuditStage",
    # Checkpointing
    "CheckpointManager",
    "run_with_checkpoint",
    # Parallel processing
    "WorkerPool",
    "WorkerStats",
    "map_parallel",
    # Constants
    "DEFAULT_STAGE_NAMES",
    "PIPELINE_VERSION",
]