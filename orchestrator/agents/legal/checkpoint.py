"""
orchestrator/agents/legal/checkpoint.py — crash-safe checkpoints.

The productivity checkpoint contract, legal-owned directory
(memory/legal/checkpoints — JARVIS_HOME-relocatable): every parked
approval snapshots the EXACT pending payload here, so "what exactly am
I approving?" always has an inspectable answer and the approved
re-dispatch can execute that payload — never a fresh parse of the
user's words.
"""
from __future__ import annotations

import os
from pathlib import Path

from orchestrator.agents.legal.vault import legal_dir
from orchestrator.agents.productivity.checkpoint import \
    ProductivityCheckpoint

MAX_CHECKPOINTS = 20


class LegalCheckpoint(ProductivityCheckpoint):
    """Approval slots + rolling snapshots under memory/legal/checkpoints."""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        super().__init__(directory or (legal_dir() / "checkpoints"))
