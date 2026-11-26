"""
orchestrator/contracts.py — the standard request/response language.

Every agent, present and future, speaks exactly this format. The router, the
orchestrator, and the agents share these dataclasses so a Phase-2 agent can be
dropped in without touching any other file.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# Task types are the routing vocabulary. _FALLBACK is the terminal default:
# when the classifier says "unknown" the router routes to the EXISTING
# assistant, never to a stub that can only say "not implemented".
class OrchestratorError(RuntimeError):
    """Base class for orchestrator-domain errors."""


class AgentNotRegisteredError(OrchestratorError):
    """A named agent was required but is not in the registry."""


class TaskType(str, Enum):
    CODING = "coding"
    RESEARCH = "research"
    TRADING = "trading"
    RAG = "rag"
    SYSTEM = "system"
    # Phase 11-12 specialists.
    EMAIL = "email"
    CALENDAR = "calendar"
    HEALTH = "health"
    # Phase 13 daily-life specialists.
    BRIEFING = "briefing"
    MEAL = "meal"
    TRAVEL = "travel"
    # Phase 14 personal finance.
    FINANCE = "finance"
    # Phase 15 study + Meta Quest knowledge.
    STUDY = "study"
    META_QUEST = "meta_quest"
    # Phase 16 legal document analysis.
    LEGAL = "legal"
    # Phase 17 SQL over permitted databases.
    SQL = "sql"
    # Phase 18 growth: self-evaluation, lead scoring, marketing.
    SELF_EVAL = "self_eval"
    LEAD_SCORE = "lead_score"
    MARKETING = "marketing"
    # Phase 19 news curation.
    NEWS = "news"
    # Phase 20 clean-room app rebuilds (replica pack).
    REPLICA = "replica"
    # Phase 21 YouTube automation (research → … → learn).
    YOUTUBE = "youtube"
    # Phase 22 bounded crypto/gold backtest research (research only).
    BACKTEST = "backtest"
    # Phase 23 real-browser automation (RIO Browser Agent).
    BROWSER = "browser"
    # Phase 24 scientific research (RIO Scientific Research Agent).
    SCIENTIFIC = "scientific"
    _FALLBACK = "fallback"


class TaskStatus(str, Enum):
    """Statuses an agent returns; COMPLETED / FAILED / DENIED are terminal."""

    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_INPUT = "needs_input"
    DECLINED = "declined"  # agent exists but refuses this one → router may re-route


_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{4,128}$")


def new_task_id(prefix: str = "task") -> str:
    """Human-sortable, collision-safe task id: task-20261005-<hex12>."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:12]}"


def _as_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


@dataclass
class AgentRequest:
    """The standard envelope handed to every agent.

    Attributes:
        task_id: unique per task; pinned by the orchestrator if the caller
            omits it (or passes garbage) so ids stay collision-safe.
        task_type: suggested TaskType from the classifier. Agents may serve a
            broader set than their registry name implies (the trading stub
            also covers price/data asks), so this is advisory, not binding.
        text: the user's ask, verbatim.
        context: conversation handoff — prior turns in chronological order
            [(role, text), ...] plus optional arbitrary metadata.
        attachments: local file paths the agent may read (e.g. uploaded PDFs).
        metadata: channel info (channel/chat_id), timestamps, routing hints.
        deadline_s: soft per-task budget for Phase-2 agents; 0 = no budget.
    """

    task_id: str
    task_type: TaskType
    text: str
    context: list[tuple[str, str]] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    deadline_s: float = 0.0

    def __post_init__(self) -> None:
        if not _TASK_ID_RE.match(str(self.task_id or "")):
            self.task_id = new_task_id()
        if not isinstance(self.task_type, TaskType):
            self.task_type = TaskType(self.task_type)
        if self.deadline_s is None:
            self.deadline_s = 0.0

    def with_context(
        self, history: list[tuple[str, str]], extra: dict[str, Any] | None = None
    ) -> "AgentRequest":
        """Return a copy carrying the conversation handoff (context handoff)."""
        return AgentRequest(
            task_id=self.task_id,
            task_type=self.task_type,
            text=self.text,
            context=list(history),
            attachments=list(self.attachments),
            metadata={**self.metadata, **(extra or {})},
            deadline_s=self.deadline_s,
        )


@dataclass
class AgentResult:
    """The standard envelope every agent returns.

    status semantics:
      COMPLETED  — done; orchestrator may deliver `text` to the user.
      NEEDS_INPUT — agent needs a reply; text is the question to relay.
      NEEDS_APPROVAL — a human must approve before execution (approval state).
      DECLINED   — this agent won't take it; router may offer it to another.
      FAILED     — agent tried and failed; orchestrator records and falls back.
      DENIED     — request violates the agent's guardrails (no re-route).
    """

    task_id: str
    status: TaskStatus
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    agent: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not isinstance(self.status, TaskStatus):
            self.status = TaskStatus(self.status)
        self.created_at = _as_utc(self.created_at) or datetime.now(timezone.utc)

    @property
    def ok(self) -> bool:
        return self.status is TaskStatus.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "text": self.text,
            "data": self.data,
            "error": self.error,
            "agent": self.agent,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class ApprovalState:
    """A pending human-approval decision for one task."""

    task_id: str
    agent: str
    summary: str
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved: bool = False
    approved: bool = False
    resolved_at: datetime | None = None
    resolved_by: str = ""
    # Internal monotonic-ish clock reading for TTL expiry. time.time() is
    # fine here (approval TTLs are minutes, not sub-second), and it keeps
    # the gate injectable with a fake clock for tests.
    created_monotonic: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent": self.agent,
            "summary": self.summary,
            "requested_at": self.requested_at.isoformat(),
            "resolved": self.resolved,
            "approved": self.approved,
            "resolved_at": (self.resolved_at.isoformat() if self.resolved_at else None),
            "resolved_by": self.resolved_by,
        }
