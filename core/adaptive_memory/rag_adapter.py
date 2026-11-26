"""
core/adaptive_memory/rag_adapter.py — Adapter for core.rag_service functional API.

Provides the interface expected by IngestionPipeline.IndexStage:
- ingest(collection, documents, metadatas, ids, caller)
"""
from __future__ import annotations

from typing import Any
from core import rag_service


class RAGServiceAdapter:
    """
    Adapter wrapping core.rag_service functional API to provide
    the interface expected by pipeline stages.
    """

    def __init__(self, caller: str = "adaptive_memory"):
        self.caller = caller

    async def ingest(
        self,
        collection: str,
        documents: list[str],
        metadatas: list[dict],
        ids: list[str],
        caller: str | None = None,
    ) -> dict:
        """
        Ingest multiple documents into a collection.

        Uses rag_service.ingest_text for each document since we have raw text,
        not file paths.
        """
        caller = caller or self.caller
        results = []

        for doc, metadata, doc_id in zip(documents, metadatas, ids):
            title = metadata.get("source_uri", doc_id)[:100]
            # Use ingest_text since we have raw content, not a file
            result = rag_service.ingest_text(
                caller=caller,
                text=doc,
                title=title,
                collection=collection,
                source=metadata.get("source", "adaptive_memory"),
                suffix="txt",
                access=metadata.get("acl_sensitivity", "private"),
                metadata=metadata,
            )
            results.append(result)

        # Return aggregate result
        ok_count = sum(1 for r in results if r.get("ok"))
        return {
            "ok": ok_count == len(results),
            "ingested": ok_count,
            "total": len(results),
            "results": results,
        }

    async def search(
        self,
        query: str,
        top_k: int = 5,
        collection: str | None = None,
        caller: str | None = None,
    ) -> dict:
        """Search the knowledge base."""
        caller = caller or self.caller
        return rag_service.search(
            caller=caller,
            query=query,
            top_k=top_k,
            collection=collection,
        )

    async def get_doc(self, doc_id: str, caller: str | None = None) -> dict:
        """Get a document by ID."""
        caller = caller or self.caller
        return rag_service.get(caller, doc_id)