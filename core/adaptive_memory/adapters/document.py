"""
core/adaptive_memory/adapters/document.py — Document adapter for PDF, DOCX, TXT, MD.

Supports:
- PDF: Text extraction via PyMuPDF (fitz), with OCR fallback
- DOCX: Text + tables via python-docx
- TXT/MD: Direct reading with encoding detection
- markitdown: Universal document conversion (Microsoft's library)
"""
from __future__ import annotations

import asyncio
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from core.adaptive_memory.adapters.base import BaseAdapter, AdapterCapabilities
from core.adaptive_memory.schema import (
    RawItem,
    MemoryNode,
    SourceType,
    MemoryTier,
    AccessControl,
)
from core.adaptive_memory.config import get_config


class DocumentAdapter(BaseAdapter):
    """Adapter for document files: PDF, DOCX, TXT, MD, and more via markitdown."""

    source_type = SourceType.DOCUMENT
    capabilities = AdapterCapabilities(
        source_type=SourceType.DOCUMENT,
        supported_formats=[".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".odt"],
        supports_streaming=True,
        supports_incremental=True,
        max_file_size_mb=100,
    )

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config)
        self.watch_dirs = [Path(d).expanduser() for d in config.get("watch_dirs", [])]
        self.max_file_size_mb = config.get("max_file_size_mb", self.capabilities.max_file_size_mb)
        self._markitdown = None
        self._fitz = None

    def _get_markitdown(self):
        """Lazy load markitdown."""
        if self._markitdown is None:
            try:
                from markitdown import MarkItDown
                self._markitdown = MarkItDown()
            except ImportError:
                self._markitdown = False
        return self._markitdown

    def _get_fitz(self):
        """Lazy load PyMuPDF."""
        if self._fitz is None:
            try:
                import fitz
                self._fitz = fitz
            except ImportError:
                self._fitz = False
        return self._fitz

    async def extract(self, item: RawItem) -> list[MemoryNode]:
        """Extract text content from a document file."""
        path = Path(item.source_uri)
        if not path.exists():
            # Try relative to watch dirs
            for watch_dir in self.watch_dirs:
                test_path = watch_dir / item.source_uri
                if test_path.exists():
                    path = test_path
                    break

        if not path.exists():
            raise FileNotFoundError(f"Document not found: {item.source_uri}")

        # Check file size
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > self.max_file_size_mb:
            raise ValueError(f"File too large: {size_mb:.1f}MB > {self.max_file_size_mb}MB")

        # Extract based on format
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            content = await self._extract_pdf(path)
        elif suffix in (".docx", ".doc"):
            content = await self._extract_docx(path)
        elif suffix in (".txt", ".md", ".markdown", ".rst"):
            content = await self._extract_text(path)
        else:
            # Try markitdown as universal fallback
            content = await self._extract_via_markitdown(path)

        if not content or not content.strip():
            raise ValueError("No extractable content found")

        # Create nodes (will be chunked by NormalizeStage)
        nodes = [self._make_node(
            content=content.strip(),
            source_uri=str(path),
            metadata={
                "filename": path.name,
                "extension": suffix,
                "size_bytes": path.stat().st_size,
                "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                **item.metadata,
            },
            acl=item.acl,
        )]

        return nodes

    async def _extract_pdf(self, path: Path) -> str:
        """Extract text from PDF using PyMuPDF."""
        fitz = self._get_fitz()
        if not fitz:
            # Fallback to markitdown
            return await self._extract_via_markitdown(path)

        def _extract():
            doc = fitz.open(path)
            try:
                texts = []
                for page_num in range(len(doc)):
                    page = doc[page_num]
                    text = page.get_text()
                    if text.strip():
                        texts.append(f"[Page {page_num + 1}]\n{text}")
                return "\n\n".join(texts)
            finally:
                doc.close()

        return await asyncio.to_thread(_extract)

    async def _extract_docx(self, path: Path) -> str:
        """Extract text and tables from DOCX."""
        def _extract():
            try:
                from docx import Document
            except ImportError:
                return self._extract_via_markitdown(path)

            doc = Document(path)
            parts = []

            # Extract paragraphs
            for para in doc.paragraphs:
                if para.text.strip():
                    parts.append(para.text)

            # Extract tables
            for table_idx, table in enumerate(doc.tables):
                table_text = [f"[Table {table_idx + 1}]"]
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    table_text.append(" | ".join(cells))
                parts.append("\n".join(table_text))

            return "\n\n".join(parts)

        return await asyncio.to_thread(_extract)

    async def _extract_text(self, path: Path) -> str:
        """Extract text from plain text files with encoding detection."""
        def _extract():
            import chardet
            with open(path, "rb") as f:
                raw = f.read()
            detected = chardet.detect(raw)
            encoding = detected.get("encoding", "utf-8")
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                return raw.decode("utf-8", errors="replace")

        return await asyncio.to_thread(_extract)

    async def _extract_via_markitdown(self, path: Path) -> str:
        """Universal extraction via markitdown."""
        md = self._get_markitdown()
        if not md:
            raise RuntimeError("No document extraction library available. Install markitdown or python-docx/pymupdf")

        def _extract():
            result = md.convert(str(path))
            return result.text_content if hasattr(result, "text_content") else str(result)

        return await asyncio.to_thread(_extract)

    async def list_sources(self) -> AsyncIterator[RawItem]:
        """Yield new/modified documents in watch directories."""
        seen: dict[str, float] = {}  # path -> mtime

        while True:
            for watch_dir in self.watch_dirs:
                if not watch_dir.exists():
                    continue

                for ext in self.capabilities.supported_formats:
                    for path in watch_dir.rglob(f"*{ext}"):
                        if not path.is_file():
                            continue

                        mtime = path.stat().st_mtime
                        path_str = str(path)

                        if path_str not in seen or seen[path_str] < mtime:
                            seen[path_str] = mtime

                            yield RawItem(
                                source=SourceType.DOCUMENT,
                                source_uri=path_str,
                                raw_content="",  # Will be filled by extract()
                                metadata={
                                    "filename": path.name,
                                    "extension": path.suffix.lower(),
                                    "size_bytes": path.stat().st_size,
                                    "modified": datetime.fromtimestamp(mtime).isoformat(),
                                },
                            )

            # Check every 5 minutes
            await asyncio.sleep(300)


def create_document_adapter(config: Optional[dict] = None) -> DocumentAdapter:
    """Factory function for creating DocumentAdapter."""
    return DocumentAdapter(config)