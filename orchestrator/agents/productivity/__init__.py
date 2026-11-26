"""
orchestrator/agents/productivity/ — the Phase 12 Email + Calendar agents.

Two specialists on the standard BaseAgent contract, sharing ONE secure
productivity tool layer:

    security.py       token redaction + permission levels (approvals).
    google_tools.py   shared injectable Gmail/Calendar tool layer.
    email_logic.py    message classification, newsletters, urgency,
                      summaries, reply drafts (deterministic).
    calendar_logic.py availability, conflicts, timezone math, slot
                      proposals, invite drafts, reschedule plans.
    store.py          the productivity-data boundary (0600/0700).
    checkpoint.py     crash-safe snapshots of pending approvals.
    email_agent.py    the Inbox Agent (TaskType.EMAIL).
    calendar_agent.py the Calendar Agent (TaskType.CALENDAR).

Both agents are off by default (shadow-mode convention):
    JARVIS_EMAIL_AGENT=on
    JARVIS_CALENDAR_AGENT=on
"""
