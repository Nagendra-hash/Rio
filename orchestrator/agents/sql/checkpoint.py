"""
orchestrator/agents/sql/checkpoint.py — crash-safe checkpoints.

The productivity checkpoint contract, sql-owned directory
(memory/sql/checkpoints — JARVIS_HOME-relocatable): every parked
approval (a mutating SQL run, permitting a new database) snapshots the
EXACT pending payload here, so "what exactly am I approving?" always
has an inspectable answer and the approved re-dispatch executes that
payload — including the EXACT statement class captured at park time,
never a fresh parse of the user's words.
"""
from __future__ import annotations

import os
from pathlib import Path

from orchestrator.agents.productivity.checkpoint import \
    ProductivityCheckpoint
from orchestrator.agents.sql.perms import sql_dir


class SqlCheckpoint(ProductivityCheckpoint):
    """Approval slots + rolling snapshots under memory/sql/checkpoints."""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        super().__init__(directory or (sql_dir() / "checkpoints"))
