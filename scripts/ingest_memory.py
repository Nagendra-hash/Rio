#!/usr/bin/env python3
"""
scripts/ingest_memory.py — CLI for ingesting content into adaptive memory.

Usage:
    python scripts/ingest_memory.py document --path ~/Documents/JARVIS --collection personal_knowledge
    python scripts/ingest_memory.py spreadsheet --path ~/Data/JARVIS --collection personal_knowledge
    python scripts/ingest_memory.py file --path ~/report.pdf --collection personal_knowledge
    python scripts/ingest_memory.py batch --source documents --collection personal_knowledge
    python scripts/ingest_memory.py stats
    python scripts/ingest_memory.py consolidate
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.adaptive_memory import (
    IngestionPipeline,
    DocumentAdapter,
    SpreadsheetAdapter,
    TierManager,
    RawItem,
    SourceType,
    MemoryTier,
    AccessControl,
    get_config,
    RAGServiceAdapter,
)
from core.adaptive_memory.adapters.base import AdapterRegistry
from memory.config_manager import BASE_DIR


console = Console()


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
@click.pass_context
def cli(ctx, verbose):
    """Adaptive Memory Ingestion CLI."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose


@cli.command()
@click.option("--path", "-p", required=True, help="Path to document file or directory")
@click.option("--collection", "-c", default="personal_knowledge", help="RAG collection name")
@click.option("--recursive", "-r", is_flag=True, help="Recursively process directories")
@click.option("--extensions", "-e", default=".pdf,.docx,.txt,.md", help="Comma-separated file extensions")
@click.pass_context
def document(ctx, path: str, collection: str, recursive: bool, extensions: str):
    """Ingest document files (PDF, DOCX, TXT, MD)."""
    asyncio.run(_ingest_documents(path, collection, recursive, extensions, ctx.obj["verbose"]))


@cli.command()
@click.option("--path", "-p", required=True, help="Path to spreadsheet file or directory")
@click.option("--collection", "-c", default="personal_knowledge", help="RAG collection name")
@click.option("--recursive", "-r", is_flag=True, help="Recursively process directories")
@click.option("--extensions", "-e", default=".xlsx,.xls,.csv,.tsv", help="Comma-separated file extensions")
@click.pass_context
def spreadsheet(ctx, path: str, collection: str, recursive: bool, extensions: str):
    """Ingest spreadsheet files (Excel, CSV, TSV)."""
    asyncio.run(_ingest_spreadsheets(path, collection, recursive, extensions, ctx.obj["verbose"]))


@cli.command()
@click.option("--path", "-p", required=True, help="Path to single file")
@click.option("--collection", "-c", default="personal_knowledge", help="RAG collection name")
@click.option("--source", "-s", type=click.Choice(["document", "spreadsheet", "auto"]), default="auto")
@click.pass_context
def file(ctx, path: str, collection: str, source: str):
    """Ingest a single file with auto-detected source type."""
    asyncio.run(_ingest_single_file(path, collection, source, ctx.obj["verbose"]))


@cli.command()
@click.option("--source", "-s", type=click.Choice(["documents", "spreadsheets"]), required=True)
@click.option("--collection", "-c", default="personal_knowledge", help="RAG collection name")
@click.option("--limit", "-l", type=int, default=100, help="Max files to process")
@click.pass_context
def batch(ctx, source: str, collection: str, limit: int):
    """Batch ingest from configured watch directories."""
    asyncio.run(_batch_ingest(source, collection, limit, ctx.obj["verbose"]))


@cli.command()
@click.pass_context
def stats(ctx):
    """Show memory tier statistics."""
    asyncio.run(_show_stats(ctx.obj["verbose"]))


@cli.command()
@click.pass_context
def consolidate(ctx):
    """Run nightly consolidation (promotion, decay, eviction)."""
    asyncio.run(_run_consolidation(ctx.obj["verbose"]))


@cli.command()
@click.option("--entity", "-e", help="Search by entity name")
@click.option("--tag", "-t", help="Search by tag")
@click.option("--tier", type=click.Choice(["working", "episodic", "semantic", "procedural", "long_term"]))
@click.option("--hours", type=int, default=24, help="Recent items within hours")
@click.pass_context
def search(ctx, entity: str, tag: str, tier: str, hours: int):
    """Search memory nodes."""
    asyncio.run(_search_memory(entity, tag, tier, hours, ctx.obj["verbose"]))


async def _ingest_documents(path: str, collection: str, recursive: bool, extensions: str, verbose: bool):
    """Ingest document files."""
    config = get_config()
    rag_service = RAGServiceAdapter()

    adapter = DocumentAdapter({
        "watch_dirs": [path] if Path(path).is_dir() else [],
        "max_file_size_mb": config.ingestion.max_file_size_mb,
    })

    pipeline = IngestionPipeline(
        adapters={SourceType.DOCUMENT: adapter},
        rag_service=rag_service,
    )

    # Find files
    path_obj = Path(path).expanduser()
    ext_list = [e.strip() for e in extensions.split(",")]

    files = []
    if path_obj.is_file():
        if path_obj.suffix.lower() in ext_list:
            files.append(path_obj)
    elif path_obj.is_dir():
        pattern = "**/*" if recursive else "*"
        for ext in ext_list:
            files.extend(path_obj.glob(f"{pattern}{ext}"))

    if not files:
        console.print("[yellow]No matching files found[/yellow]")
        return

    console.print(f"[green]Found {len(files)} document(s) to ingest[/green]")

    # Create RawItems
    items = []
    for f in files:
        items.append(RawItem(
            source=SourceType.DOCUMENT,
            source_uri=str(f),
            raw_content="",
            metadata={"filename": f.name, "extension": f.suffix.lower()},
        ))

    # Process with progress bar
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as progress:
        task = progress.add_task("Ingesting documents...", total=len(items))
        result = await pipeline.run(items)
        progress.update(task, completed=len(items))

    _print_result(result)


async def _ingest_spreadsheets(path: str, collection: str, recursive: bool, extensions: str, verbose: bool):
    """Ingest spreadsheet files."""
    config = get_config()
    rag_service = RAGServiceAdapter()

    adapter = SpreadsheetAdapter({
        "watch_dirs": [path] if Path(path).is_dir() else [],
        "max_file_size_mb": config.ingestion.max_file_size_mb,
    })

    pipeline = IngestionPipeline(
        adapters={SourceType.SPREADSHEET: adapter},
        rag_service=rag_service,
    )

    path_obj = Path(path).expanduser()
    ext_list = [e.strip() for e in extensions.split(",")]

    files = []
    if path_obj.is_file():
        if path_obj.suffix.lower() in ext_list:
            files.append(path_obj)
    elif path_obj.is_dir():
        pattern = "**/*" if recursive else "*"
        for ext in ext_list:
            files.extend(path_obj.glob(f"{pattern}{ext}"))

    if not files:
        console.print("[yellow]No matching files found[/yellow]")
        return

    console.print(f"[green]Found {len(files)} spreadsheet(s) to ingest[/green]")

    items = []
    for f in files:
        items.append(RawItem(
            source=SourceType.SPREADSHEET,
            source_uri=str(f),
            raw_content="",
            metadata={"filename": f.name, "extension": f.suffix.lower()},
        ))

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as progress:
        task = progress.add_task("Ingesting spreadsheets...", total=len(items))
        result = await pipeline.run(items)
        progress.update(task, completed=len(items))

    _print_result(result)


async def _ingest_single_file(path: str, collection: str, source: str, verbose: bool):
    """Ingest a single file with auto-detection."""
    config = get_config()
    rag_service = RAGServiceAdapter()

    path_obj = Path(path).expanduser()
    if not path_obj.exists():
        console.print(f"[red]File not found: {path}[/red]")
        return

    suffix = path_obj.suffix.lower()
    doc_exts = [".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".odt"]
    sheet_exts = [".xlsx", ".xls", ".csv", ".tsv", ".ods"]

    if source == "auto":
        if suffix in doc_exts:
            source = "document"
        elif suffix in sheet_exts:
            source = "spreadsheet"
        else:
            console.print(f"[red]Unknown file type: {suffix}[/red]")
            return

    if source == "document":
        adapter = DocumentAdapter()
    else:
        adapter = SpreadsheetAdapter()

    pipeline = IngestionPipeline(
        adapters={SourceType.DOCUMENT if source == "document" else SourceType.SPREADSHEET: adapter},
        rag_service=rag_service,
    )

    items = [RawItem(
        source=SourceType.DOCUMENT if source == "document" else SourceType.SPREADSHEET,
        source_uri=str(path_obj),
        raw_content="",
        metadata={"filename": path_obj.name, "extension": suffix},
    )]

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as progress:
        task = progress.add_task(f"Ingesting {path_obj.name}...", total=1)
        result = await pipeline.run(items)
        progress.update(task, completed=1)

    _print_result(result)


async def _batch_ingest(source: str, collection: str, limit: int, verbose: bool):
    """Batch ingest from watch directories."""
    config = get_config()
    rag_service = RAGServiceAdapter()

    source_type = SourceType.DOCUMENT if source == "documents" else SourceType.SPREADSHEET
    adapter_class = DocumentAdapter if source == "documents" else SpreadsheetAdapter

    source_config = config.sources.get(source, {})
    watch_dirs = source_config.get("watch_dirs", [])

    if not watch_dirs:
        console.print(f"[red]No watch directories configured for {source}[/red]")
        return

    adapter = adapter_class(source_config)
    pipeline = IngestionPipeline(
        adapters={source_type: adapter},
        rag_service=rag_service,
    )

    console.print(f"[green]Scanning watch directories for {source}...[/green]")

    items = []
    async for item in adapter.list_sources():
        items.append(item)
        if len(items) >= limit:
            break

    if not items:
        console.print("[yellow]No new files found[/yellow]")
        return

    console.print(f"[green]Found {len(items)} new/modified file(s)[/green]")

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as progress:
        task = progress.add_task(f"Batch ingesting {source}...", total=len(items))
        result = await pipeline.run(items)
        progress.update(task, completed=len(items))

    _print_result(result)


async def _show_stats(verbose: bool):
    """Show tier statistics."""
    config = get_config()
    tier_manager = TierManager(config)

    # Load existing data
    await tier_manager.load_all()

    stats = tier_manager.get_stats()

    table = Table(title="Memory Tier Statistics")
    table.add_column("Tier", style="cyan")
    table.add_column("Nodes", justify="right")
    table.add_column("Size (MB)", justify="right")
    table.add_column("Avg Confidence", justify="right")
    table.add_column("Avg Age (days)", justify="right")
    table.add_column("Promo Candidates", justify="right")
    table.add_column("Decay Candidates", justify="right")
    table.add_column("Utilization", justify="right")

    for tier_name, data in stats.items():
        table.add_row(
            tier_name,
            str(data["node_count"]),
            f"{data['size_mb']:.1f}",
            f"{data['avg_confidence']:.3f}",
            f"{data['avg_age_days']:.1f}",
            str(data["promotion_candidates"]),
            str(data["decay_candidates"]),
            f"{data['utilization_pct']:.1f}%",
        )

    console.print(table)

    if verbose:
        # Show recent nodes
        recent = tier_manager.get_recent(hours=24)
        if recent:
            console.print(f"\n[cyan]Recent nodes (24h): {len(recent)}[/cyan]")
            for node in recent[:10]:
                console.print(f"  • {node.id[:16]} | {node.tier.value} | {node.confidence:.2f} | {node.content[:80]}...")


async def _run_consolidation(verbose: bool):
    """Run consolidation job."""
    config = get_config()
    tier_manager = TierManager(config)

    await tier_manager.load_all()

    console.print("[green]Running consolidation...[/green]")

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as progress:
        task = progress.add_task("Consolidating...", total=None)
        result = await tier_manager.run_consolidation()
        progress.update(task, completed=True)

    console.print("[green]Consolidation complete[/green]")

    table = Table(title="Consolidation Results")
    table.add_column("Action", style="cyan")
    table.add_column("Count", justify="right")

    for tier, count in result.get("promotions", {}).items():
        table.add_row(f"Promoted from {tier.value}", str(count))

    for tier, count in result.get("decay", {}).items():
        table.add_row(f"Decayed in {tier.value}", str(count))

    for tier, count in result.get("evictions", {}).items():
        table.add_row(f"Evicted from {tier.value}", str(count))

    console.print(table)
    console.print(f"Duration: {result['duration_ms']:.0f}ms")

    if result.get("errors"):
        console.print("[red]Errors:[/red]")
        for err in result["errors"]:
            console.print(f"  • {err}")


async def _search_memory(entity: str, tag: str, tier: str, hours: int, verbose: bool):
    """Search memory nodes."""
    config = get_config()
    tier_manager = TierManager(config)

    await tier_manager.load_all()

    target_tier = MemoryTier(tier) if tier else None
    tiers = [target_tier] if target_tier else list(MemoryTier)

    results = []
    if entity:
        results = tier_manager.search_by_entity(entity, tiers)
    elif tag:
        results = tier_manager.search_by_tag(tag, tiers)
    else:
        results = tier_manager.get_recent(hours=hours, tiers=tiers)

    if not results:
        console.print("[yellow]No matching nodes found[/yellow]")
        return

    console.print(f"[green]Found {len(results)} matching node(s)[/green]")

    table = Table(title="Search Results")
    table.add_column("ID", style="cyan")
    table.add_column("Tier", style="magenta")
    table.add_column("Confidence", justify="right")
    table.add_column("Source", style="blue")
    table.add_column("Content Preview")

    for node in results[:20]:
        table.add_row(
            node.id[:16],
            node.tier.value,
            f"{node.confidence:.2f}",
            node.source.value,
            node.content[:100] + "..." if len(node.content) > 100 else node.content,
        )

    console.print(table)


def _print_result(result):
    """Print ingestion result summary."""
    table = Table(title="Ingestion Result")
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="green")

    table.add_row("Nodes Created", str(result.nodes_created))
    table.add_row("Nodes Updated", str(result.nodes_updated))
    table.add_row("Nodes Merged (dedup)", str(result.nodes_merged))
    table.add_row("Nodes Rejected", str(result.nodes_rejected))
    table.add_row("Processing Time", f"{result.processing_time_ms:.0f}ms")

    console.print(table)

    if result.errors:
        console.print("[red]Errors:[/red]")
        for err in result.errors[:10]:
            console.print(f"  • {err.get('stage', '?')}: {err.get('error', 'Unknown')}")


if __name__ == "__main__":
    cli()