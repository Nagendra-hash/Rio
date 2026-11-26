"""
orchestrator/agents/backtest/store.py — filesystem layout, atomic writes and
bounded retention for the research lab.

    memory/backtest/
        config.json          user preferences + limits (config.Config)
        backtest.db          job + notification state (SQLite, stdlib)
        cache/<provider>/<market>/<symbol>/<timeframe>.csv.gz (+ .meta.json)
        coverage/<job_id>/<timeframe>.json
        jobs/<job_id>.json   full machine-readable result of one job
        reports/<job_id>/summary.md, report.md, result.json,
                         experiments.csv, trades_*.csv, equity_*.csv
        champions.json       champion/challenger history (champions.py)
        outbox.jsonl         dry-run notification sink
        worker_logs/

Retention: reports older than the newest `keep_reports` are deleted (champion
and job JSON stay). Cache is pruned by LRU to `max_cache_mb`.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from orchestrator.agents.backtest.config import Config, base_dir, default_config


def lab_root(cfg: Config | None = None) -> Path:
    root = base_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def cache_dir(cfg: Config | None = None) -> Path:
    p = lab_root() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def reports_dir(cfg: Config | None = None) -> Path:
    p = lab_root() / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def coverage_dir() -> Path:
    p = lab_root() / "coverage"
    p.mkdir(parents=True, exist_ok=True)
    return p


def jobs_dir() -> Path:
    p = lab_root() / "jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = lab_root() / "worker_logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def report_dir(job_id: str) -> Path:
    p = reports_dir() / job_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return lab_root() / "backtest.db"


def champions_path() -> Path:
    return lab_root() / "champions.json"


def outbox_path() -> Path:
    return lab_root() / "outbox.jsonl"


# ── atomic JSON ──────────────────────────────────────────────────────────────

def write_json(path: str | Path, payload: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False, default=str),
                   encoding="utf-8")
    tmp.replace(path)
    return path


def read_json(path: str | Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


# ── retention ────────────────────────────────────────────────────────────────

def prune_reports(keep: int | None = None, cfg: Config | None = None) -> list[str]:
    """Delete the oldest report directories beyond `keep`. Never raises."""
    cfg = cfg or default_config()
    keep = int(keep if keep is not None else cfg.get("keep_reports", 40))
    removed: list[str] = []
    try:
        dirs = [d for d in reports_dir().iterdir() if d.is_dir()]
        dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        for d in dirs[keep:]:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
            try:
                (jobs_dir() / f"{d.name}.json").unlink()
            except FileNotFoundError:
                pass
    except Exception:
        pass
    return removed


def cache_size_mb() -> float:
    total = 0
    for root, _dirs, files in os.walk(cache_dir()):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total / (1024 * 1024)


def prune_cache(max_mb: float | None = None, cfg: Config | None = None) -> list[str]:
    """LRU-prune cache files until the directory fits the cap."""
    cfg = cfg or default_config()
    max_mb = float(max_mb if max_mb is not None else cfg.get("max_cache_mb", 1500))
    removed: list[str] = []
    try:
        files: list[tuple[float, Path]] = []
        for p in cache_dir().rglob("*"):
            if p.is_file() and not p.name.endswith(".lock"):
                try:
                    files.append((p.stat().st_atime, p))
                except OSError:
                    pass
        total = sum(p.stat().st_size for _, p in files) / (1024 * 1024)
        if total <= max_mb:
            return removed
        files.sort(key=lambda t: t[0])
        for _atime, p in files:
            if total <= max_mb * 0.9:
                break
            try:
                size = p.stat().st_size / (1024 * 1024)
                p.unlink()
                if p.name.endswith(".csv.gz"):
                    p.with_name(p.name[:-7] + ".meta.json").unlink(missing_ok=True)
                total -= size
                removed.append(str(p.relative_to(cache_dir())))
            except OSError:
                continue
    except Exception:
        pass
    return removed


def human_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def now_iso(ts: float | None = None) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts if ts is not None else time.time(),
                                  timezone.utc).isoformat(timespec="seconds")
