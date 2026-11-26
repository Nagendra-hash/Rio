"""
orchestrator/autonomy — PHASE 20: autonomous Jarvis and scheduled workflows.

Modules:

    audit.py       append-only decision log (who/what/why, every gate)
    engine.py      autonomy levels, tool permissions, emergency stop,
                   plan-execute-verify runs, approval parking, recovery
    scheduler.py   interval / daily / event / condition triggers, task store,
                   resource limits, scheduled-task management
    monitoring.py  built-in condition probes (health, backups, email, market)

THE LAW OF THIS PACKAGE (see docs/AUTONOMY_ENGINE.md)

    * Autonomy is granted by CONFIG, never by code: the effective level of a
      step is min(workflow level, agent cap, tool cap) and nothing can raise it.
    * Dangerous tools (trading execution, spend, deletion, external sends)
      are capped at EXECUTE_APPROVED. A "trusted" workflow using one is
      downgraded, loudly, with an audit line — dangerous actions never
      silently become autonomous.
    * The global emergency stop outranks everything: when tripped, nothing
      runs, full stop.
"""
from orchestrator.autonomy.audit import AutonomyAudit
from orchestrator.autonomy.engine import (AutonomyEngine, AutonomyLevel,
                                          Notifier, Step, ToolSpec, Workflow,
                                          risk_rank)
from orchestrator.autonomy.scheduler import Scheduler, TaskStore

__all__ = ["AutonomyAudit", "AutonomyEngine", "AutonomyLevel", "Notifier",
           "Step", "ToolSpec", "Workflow", "risk_rank", "Scheduler",
           "TaskStore"]
