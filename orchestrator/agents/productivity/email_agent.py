"""
orchestrator/agents/productivity/email_agent.py — the Inbox Agent (Phase 12).

A BaseAgent on the standard contract. Connects to the user's Gmail through
the shared google_tools layer (injectable service; the real one comes from
actions/_google_auth with the EXISTING gmail.modify token — no new scopes).

Capabilities:
    • triage the inbox: classify messages, detect newsletters,
      prioritize urgent/important, extractive summaries
    • read one message in full
    • draft a reply (locally saved, never sent without approval)
    • unsubscribe prep for newsletters (draft only — never auto-sends)
    • mark-read / trash — trash is APPROVAL_REQUIRED, permanent delete is
      BLOCKED outright

Approval policy (never-send-without-approval, in code):
    • `email_action=send` parks NEEDS_APPROVAL with the exact payload; the
      orchestrator's ApprovalGate holds it; on approval the agent re-reads
      the SAVED draft and sends — it never parses fresh text on the
      approval path.
    • `trusted_mode` (JARVIS_EMAIL_AGENT_TRUSTED=on) does NOT apply to
      sending: email always needs a human yes. There is no flag for
      autonomous email sending, by design.

All outbound text and all persisted payloads run through security.redact().
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from orchestrator.agents.productivity import email_logic, security
from orchestrator.agents.productivity.checkpoint import \
    ProductivityCheckpoint
from orchestrator.agents.productivity.drive_client import DriveClient
from orchestrator.agents.productivity.gmail_client import GmailClient
from orchestrator.agents.productivity.google_tools import (ToolError,
                                                           google_available)
from orchestrator.agents.productivity.store import ProductivityStore
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)

log = logging.getLogger("jarvis.email")

READ_ACTIONS = {"triage", "read", "summary", "newsletter_digest"}
DRAFT_ACTIONS = {"draft_reply", "draft_unsubscribe"}
OUTBOUND_ACTIONS = {"send_reply"}                 # always approval-gated
MSG_ACTIONS = {"mark_read", "trash"}


class EmailAgent(BaseAgent):
    name = "email_agent"
    description = ("Inbox specialist: triage/classify mail, detect "
                   "newsletters, prioritize urgent/important, summarize, "
                   "draft replies. Never sends without explicit approval.")
    task_types = (TaskType.EMAIL,)

    def __init__(self, store: ProductivityStore | None = None,
                 checkpoint: ProductivityCheckpoint | None = None,
                 service_factory=None, priority_senders: tuple = ()) -> None:
        self.store = store or ProductivityStore()
        self.checkpoint = checkpoint or ProductivityCheckpoint()
        self._service_factory = service_factory or self._default_service
        self.priority_senders = priority_senders

    # ── service plumbing (injectable; never exposes tokens) ───────────────
    @staticmethod
    def _default_service():
        from actions import _google_auth as ga
        return ga.get_service("gmail", "v1", [ga.GMAIL_SCOPE], "gmail_token")

    def _svc(self):
        return self._service_factory()

    # ── gmail / drive clients (lazy, so a missing token never breaks __init__) ─
    _gmail_client = None
    _drive_client = None

    def _gmail(self):
        if self._gmail_client is None:
            self._gmail_client = GmailClient(self._svc())
        return self._gmail_client

    def _drive_svc(self):
        from actions import _google_auth as ga
        return ga.get_service("drive", "v3", [ga.DRIVE_SCOPE], "drive_token")

    def _drive(self):
        if self._drive_client is None:
            self._drive_client = DriveClient(self._drive_svc())
        return self._drive_client

    @property
    def gmail(self):
        return self._gmail()

    @property
    def drive(self):
        return self._drive()

    def _check_drive_ready(self, request: AgentRequest) -> AgentResult | None:
        if not google_available():
            return self._reply(request, (
                "The Google API libraries aren't installed. Run: "
                "pip install google-api-python-client google-auth-oauthlib"),
                action="not_installed")
        try:
            self._drive_svc()
        except Exception as exc:
            msg = str(exc)
            if "setup" in msg.lower() or "consent" in msg.lower():
                return self._reply(request,
                                   "Google Drive needs one-time setup: save "
                                   "client_secret.json in the config folder, "
                                   "then ask me again to open consent.",
                                   action="setup_needed")
            return self._reply(request, f"Could not connect to Google Drive: "
                                        f"{type(exc).__name__}.",
                               action="connect_failed")
        return None

    # ── entry point ───────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        try:
            # Approval path: the orchestrator re-dispatches with approved=True.
            if meta.get("approved") and meta.get("resume"):
                return self._resume_approved(request, str(meta["resume"]),
                                             str(meta.get("by") or "user"))
            action = str(meta.get("email_action") or "").strip().lower() \
                or self._intent(request.text or "")
            handler = self._HANDLERS.get(action)
            if handler is None:
                return self._menu(request)
            return handler(self, request)
        except ToolError as exc:
            return self._completed(request, f"Email tool error: {exc}",
                                   {"tool_error": True})
        except Exception as exc:                  # never raise outward
            log.warning("email agent error: %s", type(exc).__name__)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error="email action failed", agent=self.name)

    # ── intent detection ──────────────────────────────────────────────────
    @staticmethod
    def _intent(text: str) -> str:
        t = (text or "").lower()
        if "unread" in t and ("digest" in t or "newsletters" in t):
            return "newsletter_digest"
        if "newsletter" in t and "digest" in t:
            return "newsletter_digest"
        if "unsubscri" in t:
            return "draft_unsubscribe"
        # SEND checks BEFORE draft checks: 'send the reply draft' means
        # send, not draft.
        if "send" in t and ("draft" in t or "reply" in t or "email" in t
                            or "mail" in t or "it" in t):
            return "send_reply"
        if "draft" in t and ("reply" in t or "response" in t):
            return "draft_reply"
        if "reply" in t or "respond" in t:
            return "send_reply"
        if "trash" in t or "delete" in t:
            return "trash"
        if "mark" in t and ("read" in t or "done" in t):
            return "mark_read"
        if "read" in t or "open" in t or "full" in t:
            return "read"
        if "summar" in t:
            return "summary"
        if "inbox" in t or "email" in t or "mail" in t or "triage" in t \
                or "check" in t or "unread" in t:
            return "triage"
        return ""

    _HANDLERS: dict = {}

    # ── result helpers ────────────────────────────────────────────────────
    def _reply(self, request: AgentRequest, text: str, *, action: str,
               status: TaskStatus = TaskStatus.COMPLETED,
               extra: dict | None = None) -> AgentResult:
        return AgentResult(
            task_id=request.task_id, status=status,
            text=security.redact(text),
            data={"productivity": True, "domain": "email",
                  "action": action, **(extra or {})},
            agent=self.name)

    def _completed(self, request: AgentRequest, text: str,
                   extra: dict) -> AgentResult:
        return self._reply(request, text, action=extra.pop("action", "unknown"),
                           extra=extra)

    def _menu(self, request: AgentRequest) -> AgentResult:
        return self._reply(request, (
            "I can help with your inbox:\n"
            "  • triage my inbox — classify + prioritize + summarize\n"
            "  • summarize <id> — summarize one message\n"
            "  • read <id> — full message\n"
            "  • draft reply to <id> — a reply you edit, saved locally\n"
            "  • send the reply draft — parks behind your approval\n"
            "  • newsletter digest / draft unsubscribe <id>\n"
            "  • mark <id> read · trash <id> (needs approval)\n"
            "Nothing is ever sent without your explicit approval."),
            action="menu", status=TaskStatus.NEEDS_INPUT,
            extra={"menu": True})

    # ── google-backed helpers ─────────────────────────────────────────────
    def _connected(self, request: AgentRequest) -> AgentResult | None:
        if not google_available():
            return self._reply(request, (
                "The Google API libraries aren't installed. Run: "
                "pip install google-api-python-client google-auth-oauthlib"),
                action="not_installed")
        try:
            self._svc()
        except Exception as exc:
            msg = str(exc)
            if "setup" in msg.lower() or "consent" in msg.lower():
                return self._reply(request,
                                   "Gmail needs one-time setup: save "
                                   "client_secret.json in the config folder, "
                                   "then ask me again to open consent.",
                                   action="setup_needed")
            return self._reply(request, f"Could not connect to Gmail: "
                                        f"{type(exc).__name__}.",
                               action="connect_failed")
        return None

    def _triage(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        meta = request.metadata or {}
        n = int(meta.get("max_results") or 10)
        label = str(meta.get("label") or "inbox")
        svc = self._svc()
        from orchestrator.agents.productivity import google_tools as gt
        ids = gt.list_message_ids(svc, query=str(meta.get("query") or ""),
                                  label=label, max_results=n)
        msgs = gt.fetch_messages(svc, ids)
        classified = []
        for i, m in enumerate(msgs, 1):
            if "error" in m:
                continue
            c = email_logic.classify_message(m, self.priority_senders)
            c.update({"idx": i, "id": m["id"], "from": m.get("from", ""),
                      "subject": m.get("subject", ""),
                      "summary": email_logic.summarize_message(m, 140)})
            classified.append(c)
        classified.sort(key=lambda r: -r["urgency"])
        for i, row in enumerate(classified, 1):
            row["idx"] = i
        header = (f"Inbox ({label}" +
                  (f", “{meta.get('query')}”" if meta.get("query") else "") +
                  ")")
        text = email_logic.render_inbox(classified, header)
        self.store.audit("inbox_triaged", count=len(classified))
        return self._reply(request, text, action="triage",
                           extra={"messages": [
                               {k: row[k] for k in
                                ("id", "category", "urgency", "urgency_label")}
                               for row in classified[:20]]})

    def _read(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        mid = self._message_id(request)
        if not mid:
            return self._reply(request,
                               "Which message? Give me its id from a triage "
                               "list.", action="read",
                               status=TaskStatus.NEEDS_INPUT)
        svc = self._svc()
        from orchestrator.agents.productivity import google_tools as gt
        m = gt.fetch_message(svc, mid, full=True)
        c = email_logic.classify_message(m, self.priority_senders)
        text = (f"From: {m.get('from')}\n"
                f"Subject: {m.get('subject')}\n"
                f"Date: {m.get('date')}\n"
                f"Category: {c['category']} · {c['urgency_label']}\n\n"
                f"{m.get('body', '')}")
        return self._reply(request, text, action="read",
                           extra={"message_id": mid, "category": c["category"],
                                  "urgency": c["urgency"]})

    def _summary(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        mid = self._message_id(request)
        if not mid:
            return self._reply(request,
                               "Which message should I summarize? Give me "
                               "its id.", action="summary",
                               status=TaskStatus.NEEDS_INPUT)
        svc = self._svc()
        from orchestrator.agents.productivity import google_tools as gt
        m = gt.fetch_message(svc, mid, full=True)
        s = email_logic.summarize_message(m, 400)
        return self._reply(request,
                           f"Summary of “{m.get('subject')}” "
                           f"(from {m.get('from')}):\n{s}",
                           action="summary", extra={"message_id": mid})

    def _newsletter_digest(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        svc = self._svc()
        from orchestrator.agents.productivity import google_tools as gt
        ids = gt.list_message_ids(svc, label="inbox",
                                  max_results=gt.MAX_RESULTS_CAP)
        msgs = gt.fetch_messages(svc, ids)
        news = [m for m in msgs if "error" not in m
                and email_logic.is_newsletter(m)]
        if not news:
            return self._reply(request,
                               "No newsletters spotted in the latest inbox "
                               "messages.", action="newsletter_digest")
        lines = [f"{len(news)} newsletter(s) in the latest inbox messages:"]
        for i, m in enumerate(news, 1):
            lines.append(f"  {i}. {m.get('from')} — {m.get('subject')}")
        lines.append("\nSay 'draft unsubscribe <id>' and I'll prepare the "
                     "unsubscribed draft — I never click or send it for "
                     "you without approval.")
        return self._reply(request, "\n".join(lines),
                           action="newsletter_digest")

    def _draft_reply(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        mid = self._message_id(request)
        if not mid:
            return self._reply(request,
                               "Which message should I draft a reply to? "
                               "Give me its id.", action="draft_reply",
                               status=TaskStatus.NEEDS_INPUT)
        meta = request.metadata or {}
        svc = self._svc()
        from orchestrator.agents.productivity import google_tools as gt
        m = gt.fetch_message(svc, mid, full=True)
        draft = email_logic.draft_reply(
            m, user_name=str(meta.get("user_name") or ""),
            intent=str(meta.get("reply_intent") or "acknowledge"),
            notes=str(meta.get("notes") or ""))
        saved = self.store.save_draft("email_reply", draft,
                                      task_id=request.task_id)
        self._save_checkpoint()
        return self._reply(request, (
            f"Reply draft saved (id {saved['id']}) — it has NOT been "
            f"sent:\n\nTo: {draft['to']}\nSubject: {draft['subject']}\n\n"
            f"{draft['body']}\n\nSay 'send the reply draft' to park it "
            f"behind your approval, or 'draft reply' again with notes to "
            f"revise it."),
            action="draft_reply",
            extra={"draft_id": saved["id"], "message_id": mid})

    def _draft_unsubscribe(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        mid = self._message_id(request)
        if not mid:
            return self._reply(request,
                               "Which newsletter? Give me its id.",
                               action="draft_unsubscribe",
                               status=TaskStatus.NEEDS_INPUT)
        svc = self._svc()
        from orchestrator.agents.productivity import google_tools as gt
        m = gt.fetch_message(svc, mid, full=True)
        link = ""
        if m.get("unsubscribe"):
            mm = __import__("re").search(r"https?://\S+",
                                         m["unsubscribe"])
            link = mm.group(0) if mm else ""
        draft = {"to": m.get("from", ""), "subject": f"Unsubscribe — "
                                                       f"{m.get('subject', '')}",
                 "body": ("Please remove my address from this mailing list "
                          "and stop all further emails.\n\nThank you."),
                 "unsubscribe_url": link}
        saved = self.store.save_draft("unsubscribe", draft,
                                      task_id=request.task_id)
        text = (f"Unsubscribe draft saved (id {saved['id']})."
                + (f"\nOne-click link from the headers (you'd open this "
                   f"yourself): {link}" if link else "")
                + "\nNothing was sent or clicked — say the word and it "
                  "parks behind your approval.")
        return self._reply(request, text, action="draft_unsubscribe",
                           extra={"draft_id": saved["id"]})

    def _send_reply(self, request: AgentRequest) -> AgentResult:
        """Parks the LATEST saved reply draft behind the approval gate."""
        blocked = self._connected(request)
        if blocked:
            return blocked
        draft = self.store.latest_draft("email_reply")
        if not draft:
            return self._reply(request,
                               "I don't have a saved reply draft yet — ask "
                               "me to draft one first.",
                               action="send_reply",
                               status=TaskStatus.NEEDS_INPUT)
        payload = {"draft_id": draft["id"],
                   "to": draft["payload"].get("to", ""),
                   "subject": draft["payload"].get("subject", ""),
                   "body": draft["payload"].get("body", "")}
        raw, problem = self._raw_for(payload)
        if problem:
            return self._reply(request, problem, action="send_reply",
                               status=TaskStatus.NEEDS_INPUT)
        cp = {"task_id": request.task_id, "agent": self.name,
              "action": "send_reply", "payload": payload, "raw": raw,
              "status": "awaiting_approval"}
        self.checkpoint.save_for(request.task_id, cp)
        detail = (f"send email to {payload['to']} — "
                  f"subject “{payload['subject']}”")
        return security.approval_result(request, self.name, "send_reply",
                                        detail)

    def _raw_for(self, payload: dict) -> tuple[str, str]:
        from orchestrator.agents.productivity import google_tools as gt
        return gt.build_raw_email(payload.get("to", ""),
                                  payload.get("subject", ""),
                                  payload.get("body", ""))

    def _resume_approved(self, request: AgentRequest, resume_id: str,
                         by: str) -> AgentResult:
        """The approval path. Sends the EXACT saved payload — no fresh parse."""
        cp = self.checkpoint.load_for(resume_id)
        if not cp or cp.get("task_id") != resume_id:
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               text=f"No pending approval checkpoint for "
                                    f"{resume_id!r} — nothing was sent.",
                               agent=self.name)
        if cp.get("status") != "awaiting_approval":
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.DECLINED,
                               text=f"Approval {resume_id!r} is "
                                    f"'{cp.get('status')}' — already "
                                    f"handled.",
                               agent=self.name)
        action = cp.get("action")
        payload = cp.get("payload") or {}
        if action == "send_reply":
            raw, problem = self._raw_for(payload)
            if problem:
                return AgentResult(task_id=request.task_id,
                                   status=TaskStatus.FAILED,
                                   text=problem, agent=self.name)
            svc = self._svc()
            from orchestrator.agents.productivity import google_tools as gt
            mid = gt.send_raw(svc, raw)
            self.store.record_sent(resume_id, self.name, "send_reply", by,
                                   ref=payload.get("draft_id", ""))
            cp["status"] = "sent"
            self.checkpoint.save_for(resume_id, cp)
            return self._reply(request,
                               f"Sent (approved by {by}): email to "
                               f"{payload.get('to')} — "
                               f"“{payload.get('subject')}”.",
                               action="send_reply",
                               extra={"sent": True, "message_id": mid})
        if action == "trash":
            svc = self._svc()
            from orchestrator.agents.productivity import google_tools as gt
            gt.trash_message(svc, payload.get("message_id", ""))
            self.store.record_sent(resume_id, self.name, "trash", by,
                                   ref=payload.get("message_id", ""))
            cp["status"] = "done"
            self.checkpoint.save_for(resume_id, cp)
            return self._reply(request,
                               f"Moved to Trash (approved by {by}) — "
                               f"recoverable for 30 days.",
                               action="trash", extra={"trashed": True})
        return AgentResult(task_id=request.task_id, status=TaskStatus.FAILED,
                           text=f"Unknown approved action {action!r}.",
                           agent=self.name)

    def _mark_read(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        mid = self._message_id(request)
        if not mid:
            return self._reply(request, "Which message? Its id, please.",
                               action="mark_read",
                               status=TaskStatus.NEEDS_INPUT)
        svc = self._svc()
        from orchestrator.agents.productivity import google_tools as gt
        gt.modify_labels(svc, mid, remove=["UNREAD"])
        self.store.audit("mark_read", message=_mid_clip(mid))
        return self._reply(request, "Marked as read.", action="mark_read")


    def _mark_unread(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        mid = self._message_id(request)
        if not mid:
            return self._reply(request, "Which message? Its id, please.",
                               action="mark_unread",
                               status=TaskStatus.NEEDS_INPUT)
        self.gmail.mark_unread(mid)
        return self._reply(request, "Marked as unread.", action="mark_unread")


    def _star(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        ids = self._ids(request)
        if not ids:
            return self._reply(request, "Which message(s)? Id from a list.",
                               action="star", status=TaskStatus.NEEDS_INPUT)
        self.gmail.star(ids)
        return self._reply(request, f"Starred {len(ids)} message(s).",
                           action="star")


    def _unstar(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        ids = self._ids(request)
        if not ids:
            return self._reply(request, "Which message(s)?",
                               action="unstar", status=TaskStatus.NEEDS_INPUT)
        self.gmail.unstar(ids)
        return self._reply(request, f"Unstarred {len(ids)} message(s).",
                           action="unstar")


    def _archive(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        ids = self._ids(request)
        if not ids:
            return self._reply(request, "Which message(s)?",
                               action="archive", status=TaskStatus.NEEDS_INPUT)
        self.gmail.archive(ids)
        return self._reply(request, f"Archived {len(ids)} message(s) — still in All Mail.",
                           action="archive")


    def _move_to_inbox(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        ids = self._ids(request)
        if not ids:
            return self._reply(request, "Which message(s)?",
                               action="move_to_inbox",
                               status=TaskStatus.NEEDS_INPUT)
        self.gmail.move_to_inbox(ids)
        return self._reply(request, f"Moved {len(ids)} message(s) back to Inbox.",
                           action="move_to_inbox")


    def _label_op(self, request: AgentRequest, op: str = "") -> AgentResult:
        if not op:
            op = str((request.metadata or {}).get("email_action") or "").strip().lower()
        if op not in ("add_label", "remove_label"):
            op = "add_label"
        blocked = self._connected(request)
        if blocked:
            return blocked
        ids = self._ids(request)
        if not ids:
            return self._reply(request, "Which message(s)? Give an id from a list.",
                               action=op, status=TaskStatus.NEEDS_INPUT)
        label = str((request.metadata or {}).get("label") or "").strip()
        if not label:
            return self._reply(request, "Which label?",
                               action=op, status=TaskStatus.NEEDS_INPUT)
        try:
            func = self.gmail.add_label if op == "add_label" else self.gmail.remove_label
            result = func(ids, label)
            return self._reply(
                request,
                f"{'Added' if op == 'add_label' else 'Removed'} label "
                f"“{label}” on {len(ids)} message(s).",
                action=op)
        except Exception as exc:
            return self._reply(request, f"Label operation failed: {exc}",
                               action=op, status=TaskStatus.FAILED)


    def _trash(self, request: AgentRequest) -> AgentResult:
        """APPROVAL_REQUIRED: trashing mail affects the user's records."""
        blocked = self._connected(request)
        if blocked:
            return blocked
        mid = self._message_id(request)
        if not mid:
            return self._reply(request, "Which message? Its id, please.",
                               action="trash", status=TaskStatus.NEEDS_INPUT)
        cp = {"task_id": request.task_id, "agent": self.name,
              "action": "trash", "payload": {"message_id": mid},
              "status": "awaiting_approval"}
        self.checkpoint.save_for(request.task_id, cp)
        return security.approval_result(
            request, self.name, "trash",
            f"move message {mid} to Gmail Trash (recoverable 30 days)")

    def _delete_permanent(self, request: AgentRequest) -> AgentResult:
        """APPROVAL_REQUIRED — explicitly confirm before permanent delete."""
        blocked = self._connected(request)
        if blocked:
            return blocked
        ids = self._ids(request)
        if not ids:
            return self._reply(request, "Which message(s)? Ids from a list/search.",
                               action="delete_permanent",
                               status=TaskStatus.NEEDS_INPUT)
        cp = {"task_id": request.task_id, "agent": self.name,
              "action": "delete_permanent", "payload": {"ids": ids},
              "status": "awaiting_approval"}
        self.checkpoint.save_for(request.task_id, cp)
        preview = "\n".join(f"  • {m['id']} — {(m.get('subject') or '?')}"
                            for m in self.gmail.summaries(ids[:5]))
        return security.approval_result(
            request, self.name, "delete_permanent",
            f"Permanently delete {len(ids)} message(s)? This CANNOT be undone.\n"
            f"Affected:\n{preview}")

    def _labels_list(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        rows = self.gmail.labels()
        lines = ["User-defined Gmail labels:"]
        for r in rows:
            lines.append(f"  • {r['name']} ({r['id']})")
        return self._reply(request, "\n".join(lines), action="labels")

    def _create_label(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        name = str((request.metadata or {}).get("name") or "").strip()
        if not name:
            return self._reply(request, "What should the new label be called?",
                               action="create_label",
                               status=TaskStatus.NEEDS_INPUT)
        row = self.gmail.create_label(name)
        return self._reply(request,
                           f'Created label "{row["name"]}" (id={row["id"]}).',
                           action="create_label")

    # ── drive operations (only run when Drive is configured) ───────────────

    def _drive_list(self, request: AgentRequest) -> AgentResult:
        if not self._check_drive_ready(request):
            return self._reply(request,
                               "Google Drive is not connected — first use opens "
                               "a browser consent tab, then you can manage files.",
                               action="drive_list")
        max_results = int((request.metadata or {}).get("max_results") or 25)
        folder_id = str((request.metadata or {}).get("folder_id") or "")
        query = str((request.metadata or {}).get("query") or "").strip()
        result = self.drive.list_files(query=query, folder_id=folder_id,
                                       max_results=max_results)
        files = result.get("files") or []
        lines = [f'Drive — {len(files)} item(s).']
        for i, f in enumerate(files, 1):
            kind = "(folder)" if f.get("is_folder") else f.get("size_human") or ""
            lines.append(f"  {i}. \"{f['name']}\" {kind}")
        return self._reply(request, "\n".join(lines), action="drive_list")

    def _drive_get(self, request: AgentRequest) -> AgentResult:
        if not self._check_drive_ready(request):
            return self._reply(request, "Drive is not connected yet.",
                               action="drive_get")
        fid = str((request.metadata or {}).get("file_id") or "").strip()
        if not fid:
            return self._reply(request, "Which Drive file? Give its file_id.",
                               action="drive_get",
                               status=TaskStatus.NEEDS_INPUT)
        f = self.drive.get(fid)
        return self._reply(request,
                           f'File: "{f["name"]}" ({f["mime_type"]})\n'
                           f'  Id: {f["id"]}\n  Size: {f["size_human"]}\n'
                           f'  Modified: {f["modified"]}\n'
                           f'  Link: {f["web_link"]}',
                           action="drive_get")

    def _drive_upload(self, request: AgentRequest) -> AgentResult:
        if not self._check_drive_ready(request):
            return self._reply(request, "Drive is not connected yet.",
                               action="drive_upload")
        path_str = str((request.metadata or {}).get("path") or "").strip()
        folder_id = str((request.metadata or {}).get("folder_id") or "")
        if not Path(path_str).expanduser().is_file():
            return f"I can't find that file: {path_str}"
        f = self.drive.upload(path_str, folder_id=folder_id)
        return self._reply(request,
                           f'Uploaded "{f["name"]}" — file_id={f["id"]} '
                           f'({f["size_human"]}).',
                           action="drive_upload")

    def _drive_download(self, request: AgentRequest) -> AgentResult:
        if not self._check_drive_ready(request):
            return self._reply(request, "Drive is not connected yet.",
                               action="drive_download")
        fid = str((request.metadata or {}).get("file_id") or "").strip()
        dest = str((request.metadata or {}).get("dest_dir") or "~").strip()
        overwrite = bool((request.metadata or {}).get("overwrite"))
        if not fid:
            return self._reply(request, "Which Drive file? Give its file_id.",
                               action="drive_download",
                               status=TaskStatus.NEEDS_INPUT)
        r = self.drive.download(fid, Path(dest).expanduser(), overwrite=overwrite)
        out = Path(r["path"])
        return self._reply(request, f'Saved to {out} ({r["size"]} bytes).',
                           action="drive_download")

    def _drive_rename(self, request: AgentRequest) -> AgentResult:
        if not self._check_drive_ready(request):
            return self._reply(request, "Drive is not connected yet.",
                               action="drive_rename")
        fid = str((request.metadata or {}).get("file_id") or "").strip()
        new_name = str((request.metadata or {}).get("new_name") or "").strip()
        if not fid or not new_name:
            return self._reply(request, "Give a file_id and a new name.",
                               action="drive_rename",
                               status=TaskStatus.NEEDS_INPUT)
        updated = self.drive.rename(fid, new_name)
        return self._reply(request,
                           f'Renamed to "{updated["name"]}" (id={updated["id"]}).',
                           action="drive_rename")

    def _drive_move(self, request: AgentRequest) -> AgentResult:
        if not self._check_drive_ready(request):
            return self._reply(request, "Drive is not connected yet.",
                               action="drive_move")
        fid = str((request.metadata or {}).get("file_id") or "").strip()
        target_folder = str((request.metadata or {}).get("target_folder") or "").strip()
        if not fid or not target_folder:
            return self._reply(request, "Give a file_id and a target folder id.",
                               action="drive_move",
                               status=TaskStatus.NEEDS_INPUT)
        moved = self.drive.move(fid, target_folder)
        return self._reply(request,
                           f'Moved — "{moved["name"]}" (id={moved["id"]}).',
                           action="drive_move")

    def _drive_copy(self, request: AgentRequest) -> AgentResult:
        if not self._check_drive_ready(request):
            return self._reply(request, "Drive is not connected yet.",
                               action="drive_copy")
        fid = str((request.metadata or {}).get("file_id") or "").strip()
        target_folder = str((request.metadata or {}).get("target_folder") or "")
        copy_name = str((request.metadata or {}).get("new_name") or "").strip() or None
        if not fid:
            return self._reply(request, "Which Drive file? Give its file_id.",
                               action="drive_copy",
                               status=TaskStatus.NEEDS_INPUT)
        copied = self.drive.copy(fid, name=copy_name, folder_id=target_folder)
        return self._reply(request,
                           f'Copied — "{copied["name"]}" (id={copied["id"]}).',
                           action="drive_copy")

    def _drive_create_folder(self, request: AgentRequest) -> AgentResult:
        if not self._check_drive_ready(request):
            return self._reply(request, "Drive is not connected yet.",
                               action="drive_create_folder")
        name = str((request.metadata or {}).get("name") or "").strip()
        parent_id = str((request.metadata or {}).get("parent_id") or "")
        if not name:
            return self._reply(request, "What should the new folder be called?",
                               action="drive_create_folder",
                               status=TaskStatus.NEEDS_INPUT)
        folder = self.drive.create_folder(name, parent_id=parent_id or "")
        return self._reply(request,
                           f'Created folder "{folder["name"]}" (id={folder["id"]}).',
                           action="drive_create_folder")

    def _drive_trash(self, request: AgentRequest) -> AgentResult:
        if not self._check_drive_ready(request):
            return self._reply(request, "Drive is not connected yet.",
                               action="drive_trash")
        ids = self._ids_drive(request)
        count = len(ids)
        if count == 0:
            return self._reply(request,
                               "Which file(s)? Give a file_id or comma-separated ids.",
                               action="drive_trash",
                               status=TaskStatus.NEEDS_INPUT)
        preview = self.drive.files_by_ids(ids[:5])
        detail = (f"Move {count} file{'s' if count != 1 else ''} to Drive Trash "
                  f"(recoverable for 30 days).\nFirst few affected:")
        for f in preview:
            detail += f"\n  • {f.get('id')} — {(f.get('name'))[:80]}"
        cp = {"task_id": request.task_id, "agent": self.name,
              "action": "drive_trash", "payload": {"ids": ids},
              "status": "awaiting_approval"}
        self.checkpoint.save_for(request.task_id, cp)
        return security.approval_result(
            request, self.name, "drive_trash", detail)

    def _drive_delete_permanent(self, request: AgentRequest) -> AgentResult:
        if not self._check_drive_ready(request):
            return self._reply(request, "Drive is not connected yet.",
                               action="drive_delete_permanent")
        ids = self._ids_drive(request)
        count = len(ids)
        if count == 0:
            return self._reply(request,
                               "Which file(s)? Give a file_id or comma-separated ids.",
                               action="drive_delete_permanent",
                               status=TaskStatus.NEEDS_INPUT)
        preview = self.drive.files_by_ids(ids[:5])
        detail = (f"Permanently delete {count} file(s) from Drive. "
                  f"This CANNOT be undone.\nFirst few affected:")
        for f in preview:
            detail += f"\n  • {f.get('id')} — {(f.get('name'))[:80]}"
        cp = {"task_id": request.task_id, "agent": self.name,
              "action": "drive_delete_permanent", "payload": {"ids": ids},
              "status": "awaiting_approval"}
        self.checkpoint.save_for(request.task_id, cp)
        return security.approval_result(
            request, self.name, "drive_delete_permanent", detail)

    def _drive_share(self, request: AgentRequest) -> AgentResult:
        if not self._check_drive_ready(request):
            return self._reply(request, "Drive is not connected yet.",
                               action="drive_share")
        fid = str((request.metadata or {}).get("file_id") or "").strip()
        email = str((request.metadata or {}).get("email") or "").strip()
        role = str((request.metadata or {}).get("role") or "reader").strip()
        if not fid:
            return self._reply(request, "Which Drive file? Give its file_id.",
                               action="drive_share",
                               status=TaskStatus.NEEDS_INPUT)
        if not email:
            return self._reply(request, "Who should get access? Provide an email.",
                               action="drive_share",
                               status=TaskStatus.NEEDS_INPUT)
        detail = (f"Grant “{role}” access on “{self.drive.get(fid)['name']}” "
                  f"to {email}.\nThis may make it accessible outside your org.")
        def _run() -> str:
            p = self.drive.share(fid, email=email, role=role)
            return f'Shared — permission id={p["id"]}, role={p["role"]}.'
        return self._confirm("drive_share", title=f"Share file with {email}",
                             detail=detail, run=_run, player=None)

    def _drive_search(self, request: AgentRequest) -> AgentResult:
        if not self._check_drive_ready(request):
            return self._reply(request, "Drive is not connected yet.",
                               action="drive_search")
        kwargs = {k: str(v or "") for k, v in (request.metadata or {}).items()
                  if k not in ("file_id", "dest_dir", "folder_id")}
        result = self.drive.search(**kwargs)
        files = result.get("files") or []
        lines = [f'Drive search — {len(files)} item(s).']
        for i, f in enumerate(files, 1):
            lines.append(f"  {i}. {_fmt_drive_f(f)}")
        return self._reply(request, "\n".join(lines), action="drive_search")

    # ── attachment bridge ──────────────────────────────────────────────────

    def _download_attachment(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        mid = self._message_id(request)
        att_id = str((request.metadata or {}).get("attachment_id") or "").strip()
        dest_dir = str((request.metadata or {}).get("dest_dir") or "~").strip()
        filename = str((request.metadata or {}).get("filename") or "").strip()
        if not mid:
            return self._reply(request, "Which message? Give its id.",
                               action="download_attachment",
                               status=TaskStatus.NEEDS_INPUT)
        results = self.gmail.download_all_attachments(mid, Path(dest_dir).expanduser())
        ok = [r for r in results if "path" in r]
        fail = [r for r in results if "error" in r]
        lines = [f"Downloaded {len(ok)} attachment(s) from message {mid}:"]
        for r in ok:
            lines.append(f"  ✔ {r['path']}  ({r.get('size',0)//1024} KB)")
        for r in fail:
            lines.append(f"  ✘ {r.get('filename','?')}: {r['error']}")
        return self._reply(request, "\n".join(lines), action="download_attachment")

    def _email_to_drive(self, request: AgentRequest) -> AgentResult:
        if not self._check_drive_ready(request):
            return self._reply(request, "Drive is not connected yet.",
                               action="email_to_drive")
        mid = self._message_id(request)
        folder_id = str((request.metadata or {}).get("folder_id") or "").strip()
        folder = str((request.metadata or {}).get("folder") or "").strip()
        if not mid:
            return self._reply(request, "Which email? Give its message id.",
                               action="email_to_drive",
                               status=TaskStatus.NEEDS_INPUT)
        from orchestrator.agents.productivity.attachment_manager \
            import email_attachments_to_drive
        r = email_attachments_to_drive(self.gmail, self.drive, mid,
                                       folder_id=folder_id, folder=folder)
        lines = [
            f"Bridged {r.get('count',0)} attachment(s) from email {mid} to Drive.",
            *(f"  ✔ {x['file']['name']} ({x.get('size_human','')})"
              for x in r.get("uploaded") or []),
            *(f"  ✘ {x['filename']}: {x['error']}"
              for x in r.get("failed") or []),
            *(f"  ○ skipped duplicate: {x['source_filename']}"
              for x in r.get("skipped") or []),
        ]
        if r.get("error"):
            lines.insert(0, f"Error: {r['error']}")
        return self._reply(request, "\n".join(lines), action="email_to_drive")

    def _drive_to_email_draft(self, request: AgentRequest) -> AgentResult:
        blocked = self._connected(request)
        if blocked:
            return blocked
        if not self._check_drive_ready(request):
            return self._reply(request, "Drive is not connected yet.",
                               action="drive_to_email_draft")
        file_ids_raw = str((request.metadata or {}).get("file_ids") or "").strip()
        file_ids = [x.strip() for x in re.split(r"[,;\s]+", file_ids_raw) if x.strip()]
        to = str((request.metadata or {}).get("to") or "").strip()
        subject = str((request.metadata or {}).get("subject") or "").strip()
        body = str((request.metadata or {}).get("body") or "").strip()
        if not file_ids or not to:
            return self._reply(request,
                               "Need file_ids from Drive plus a recipient email.",
                               action="drive_to_email_draft",
                               status=TaskStatus.NEEDS_INPUT)
        from orchestrator.agents.productivity.attachment_manager \
            import attach_drive_files_to_draft
        draft = attach_drive_files_to_draft(self.gmail, self.drive,
                                            file_ids=file_ids, to=to,
                                            subject=subject, body=body)
        lines = [f"Draft saved with {len(draft.get('attachments') or [])} Drive file(s) attached."]
        for path in draft.get("attachments") or []:
            lines.append(f"  ✔ {path}")
        for miss in draft.get("missing") or []:
            lines.append(f"  ✘ {miss['file_id']}: {miss['error']}")
        return self._reply(request, "\n".join(lines), action="drive_to_email_draft")

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _message_id(request: AgentRequest) -> str:
        meta = request.metadata or {}
        mid = str(meta.get("message_id") or "").strip()
        if mid:
            return mid
        m = re.search(r"\b([A-Za-z0-9_-]{12,})\b", request.text or "")
        return m.group(1) if m else ""

    @staticmethod
    def _ids(request: AgentRequest) -> list[str]:
        meta = request.metadata or {}
        csv = str(meta.get("ids") or meta.get("message_ids") or "").strip()
        if csv:
            return [x.strip() for x in re.split(r"[,;\s]+", csv) if x.strip()]
        mid = EmailAgent._message_id(request)
        return [mid] if mid else []

    @staticmethod
    def _ids_drive(request: AgentRequest) -> list[str]:
        meta = request.metadata or {}
        csv = str(meta.get("ids") or meta.get("file_ids") or "").strip()
        if csv:
            return [x.strip() for x in re.split(r"[,;\s]+", csv) if x.strip()]
        fid = str(meta.get("file_id") or "").strip()
        return [fid] if fid else []

    @staticmethod
    def _confirm(op_key: str, *, title: str, detail: str, run,
                 player=None) -> AgentResult:
        from orchestrator.agents.productivity.confirmations import gate
        sent = gate(title=title, detail=detail, run=lambda: (run() or ""),
                    player=player, key=f"email_{op_key}")
        return AgentResult(task_id="", status=TaskStatus.NEEDS_APPROVAL,
                           text=sent, agent="email_agent",
                           data={"confirmation_sent": True})


    def _save_checkpoint(self) -> None:
        try:
            self.checkpoint.save({
                "saved_at": self.store.summary(),
                "drafts": self.store.drafts(limit=5)})
        except Exception:
            pass

    _HANDLERS = {
        "triage": _triage,
        "read": _read,
        "summary": _summary,
        "newsletter_digest": _newsletter_digest,
        "draft_reply": _draft_reply,
        "draft_unsubscribe": _draft_unsubscribe,
        "send_reply": _send_reply,
        "mark_read": _mark_read,
        "trash": _trash,
        "mark_unread": _mark_unread,
        "star": _star,
        "unstar": _unstar,
        "archive": _archive,
        "move_to_inbox": _move_to_inbox,
        "add_label": _label_op,
        "remove_label": _label_op,
        "delete_permanent": _delete_permanent,
        "labels": _labels_list,
        "create_label": _create_label,
        "drive_list": _drive_list,
        "drive_get": _drive_get,
        "drive_search": _drive_search,
        "drive_upload": _drive_upload,
        "drive_download": _drive_download,
        "drive_rename": _drive_rename,
        "drive_move": _drive_move,
        "drive_copy": _drive_copy,
        "drive_create_folder": _drive_create_folder,
        "drive_trash": _drive_trash,
        "drive_delete_permanent": _drive_delete_permanent,
        "drive_share": _drive_share,
        "download_attachment": _download_attachment,
        "email_to_drive": _email_to_drive,
        "drive_to_email_draft": _drive_to_email_draft,
    }


def _mid_clip(mid: str) -> str:
    return (mid or "")[:12]


def _fmt_drive_f(f: dict) -> str:
    name = f.get("name") or "?"
    kind = "(folder)" if f.get("is_folder") else f.get("size_human") or ""
    return f'"{name}" {kind}'
