"""
core/adaptive_memory/adapters/spreadsheet.py — Spreadsheet adapter for Excel, CSV.

Supports:
- Excel (.xlsx, .xls): openpyxl for reading sheets, formulas, formatting
- CSV/TSV: pandas for robust parsing with type inference
- Google Sheets: via CSV export URL (if configured)
"""
from __future__ import annotations

import asyncio
import io
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import pandas as pd

from core.adaptive_memory.adapters.base import BaseAdapter, AdapterCapabilities
from core.adaptive_memory.schema import (
    RawItem,
    MemoryNode,
    SourceType,
    MemoryTier,
    AccessControl,
)


class SpreadsheetAdapter(BaseAdapter):
    """Adapter for spreadsheet files: Excel, CSV, TSV."""

    source_type = SourceType.SPREADSHEET
    capabilities = AdapterCapabilities(
        source_type=SourceType.SPREADSHEET,
        supported_formats=[".xlsx", ".xls", ".csv", ".tsv", ".ods"],
        supports_streaming=True,
        supports_incremental=True,
        max_file_size_mb=50,
    )

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config)
        self.watch_dirs = [Path(d).expanduser() for d in config.get("watch_dirs", [])]
        self.max_file_size_mb = config.get("max_file_size_mb", self.capabilities.max_file_size_mb)
        self.include_headers = config.get("include_headers", True)
        self.max_rows_per_sheet = config.get("max_rows_per_sheet", 10000)
        self.sheet_name_pattern = config.get("sheet_name_pattern")  # regex to filter sheets

    async def extract(self, item: RawItem) -> list[MemoryNode]:
        """Extract structured data from a spreadsheet file."""
        path = Path(item.source_uri)
        if not path.exists():
            for watch_dir in self.watch_dirs:
                test_path = watch_dir / item.source_uri
                if test_path.exists():
                    path = test_path
                    break

        if not path.exists():
            raise FileNotFoundError(f"Spreadsheet not found: {item.source_uri}")

        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > self.max_file_size_mb:
            raise ValueError(f"File too large: {size_mb:.1f}MB > {self.max_file_size_mb}MB")

        suffix = path.suffix.lower()
        if suffix in (".xlsx", ".xls"):
            nodes = await self._extract_excel(path, item)
        elif suffix in (".csv", ".tsv"):
            nodes = await self._extract_csv(path, item)
        else:
            raise ValueError(f"Unsupported format: {suffix}")

        return nodes

    async def _extract_excel(self, path: Path, item: RawItem) -> list[MemoryNode]:
        """Extract from Excel workbook using openpyxl + pandas."""
        def _extract():
            nodes = []

            # Use pandas for data extraction (handles types, formulas)
            try:
                # Read all sheets
                xls = pd.ExcelFile(path)
                sheet_names = xls.sheet_names

                # Filter sheets if pattern provided
                if self.sheet_name_pattern:
                    import re
                    pattern = re.compile(self.sheet_name_pattern)
                    sheet_names = [s for s in sheet_names if pattern.search(s)]

                for sheet_name in sheet_names:
                    df = pd.read_excel(xls, sheet_name=sheet_name, nrows=self.max_rows_per_sheet)

                    if df.empty:
                        continue

                    # Convert to structured text representation
                    content_parts = [f"[Sheet: {sheet_name}]"]

                    if self.include_headers:
                        content_parts.append("Columns: " + ", ".join(str(c) for c in df.columns))

                    # Add sample rows (first 20)
                    sample_rows = df.head(20)
                    for _, row in sample_rows.iterrows():
                        row_data = []
                        for col, val in row.items():
                            if pd.notna(val):
                                row_data.append(f"{col}: {val}")
                        if row_data:
                            content_parts.append("; ".join(row_data))

                    # Add summary statistics for numeric columns
                    numeric_cols = df.select_dtypes(include=["number"]).columns
                    if len(numeric_cols) > 0:
                        content_parts.append("\n[Statistics]")
                        for col in numeric_cols:
                            stats = df[col].describe()
                            content_parts.append(
                                f"{col}: count={stats['count']:.0f}, "
                                f"mean={stats['mean']:.2f}, std={stats['std']:.2f}, "
                                f"min={stats['min']:.2f}, max={stats['max']:.2f}"
                            )

                    content = "\n".join(content_parts)

                    nodes.append(self._make_node(
                        content=content,
                        source_uri=f"{path}#[{sheet_name}]",
                        metadata={
                            "filename": path.name,
                            "sheet": sheet_name,
                            "rows": len(df),
                            "columns": list(df.columns),
                            "dtypes": {str(k): str(v) for k, v in df.dtypes.items()},
                            "size_bytes": path.stat().st_size,
                            **item.metadata,
                        },
                        acl=item.acl,
                    ))

            except Exception as e:
                # Fallback: try openpyxl directly for basic extraction
                nodes = self._extract_excel_fallback(path, item)

            return nodes

        return await asyncio.to_thread(_extract)

    def _extract_excel_fallback(self, path: Path, item: RawItem) -> list[MemoryNode]:
        """Fallback extraction using openpyxl directly."""
        try:
            import openpyxl
        except ImportError:
            raise RuntimeError("openpyxl required for Excel extraction")

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        nodes = []

        try:
            for sheet_name in wb.sheetnames:
                if self.sheet_name_pattern:
                    import re
                    if not re.search(self.sheet_name_pattern, sheet_name):
                        continue

                ws = wb[sheet_name]
                content_parts = [f"[Sheet: {sheet_name}]"]

                # Read first row as headers
                rows = list(ws.iter_rows(max_row=self.max_rows_per_sheet, values_only=True))
                if not rows:
                    continue

                headers = [str(c) if c is not None else f"Col_{i}" for i, c in enumerate(rows[0])]
                if self.include_headers:
                    content_parts.append("Columns: " + ", ".join(headers))

                # Sample data rows
                for row_idx, row in enumerate(rows[1:21]):  # First 20 data rows
                    row_data = []
                    for col_idx, val in enumerate(row):
                        if val is not None:
                            col_name = headers[col_idx] if col_idx < len(headers) else f"Col_{col_idx}"
                            row_data.append(f"{col_name}: {val}")
                    if row_data:
                        content_parts.append("; ".join(row_data))

                content = "\n".join(content_parts)

                nodes.append(self._make_node(
                    content=content,
                    source_uri=f"{path}#[{sheet_name}]",
                    metadata={
                        "filename": path.name,
                        "sheet": sheet_name,
                        "rows": ws.max_row,
                        "columns": headers,
                        "size_bytes": path.stat().st_size,
                        **item.metadata,
                    },
                    acl=item.acl,
                ))
        finally:
            wb.close()

        return nodes

    async def _extract_csv(self, path: Path, item: RawItem) -> list[MemoryNode]:
        """Extract from CSV/TSV using pandas."""
        def _extract():
            nodes = []

            # Detect delimiter
            delimiter = "\t" if path.suffix.lower() == ".tsv" else ","

            # Read with pandas (handles encoding, types, quoting)
            df = pd.read_csv(
                path,
                delimiter=delimiter,
                nrows=self.max_rows_per_sheet,
                encoding="utf-8",
                on_bad_lines="skip",
            )

            if df.empty:
                return nodes

            content_parts = [f"[File: {path.name}]"]

            if self.include_headers:
                content_parts.append("Columns: " + ", ".join(str(c) for c in df.columns))

            # Sample rows
            sample_rows = df.head(20)
            for _, row in sample_rows.iterrows():
                row_data = []
                for col, val in row.items():
                    if pd.notna(val):
                        row_data.append(f"{col}: {val}")
                if row_data:
                    content_parts.append("; ".join(row_data))

            # Statistics for numeric columns
            numeric_cols = df.select_dtypes(include=["number"]).columns
            if len(numeric_cols) > 0:
                content_parts.append("\n[Statistics]")
                for col in numeric_cols:
                    stats = df[col].describe()
                    content_parts.append(
                        f"{col}: count={stats['count']:.0f}, "
                        f"mean={stats['mean']:.2f}, std={stats['std']:.2f}, "
                        f"min={stats['min']:.2f}, max={stats['max']:.2f}"
                    )

            content = "\n".join(content_parts)

            nodes.append(self._make_node(
                content=content,
                source_uri=str(path),
                metadata={
                    "filename": path.name,
                    "format": "csv" if delimiter == "," else "tsv",
                    "rows": len(df),
                    "columns": list(df.columns),
                    "dtypes": {str(k): str(v) for k, v in df.dtypes.items()},
                    "size_bytes": path.stat().st_size,
                    **item.metadata,
                },
                acl=item.acl,
            ))

            return nodes

        return await asyncio.to_thread(_extract)

    async def list_sources(self) -> AsyncIterator[RawItem]:
        """Yield new/modified spreadsheets in watch directories."""
        seen: dict[str, float] = {}

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
                                source=SourceType.SPREADSHEET,
                                source_uri=path_str,
                                raw_content="",
                                metadata={
                                    "filename": path.name,
                                    "extension": path.suffix.lower(),
                                    "size_bytes": path.stat().st_size,
                                    "modified": datetime.fromtimestamp(mtime).isoformat(),
                                },
                            )

            await asyncio.sleep(300)


def create_spreadsheet_adapter(config: Optional[dict] = None) -> SpreadsheetAdapter:
    """Factory function for creating SpreadsheetAdapter."""
    return SpreadsheetAdapter(config)