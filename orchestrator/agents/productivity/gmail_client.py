"""
orchestrator/agents/productivity/gmail_client.py — Gmail operations.

One typed wrapper over the Gmail v1 API that BOTH consumers share:

    * actions/gmail_control.py  (the live model / voice / Hermes tool)
    * orchestrator/agents/productivity/email_agent.py (the routed agent)

Keeping the API knowledge here — and nowhere else — is what stops the two
surfaces from drifting: a fix to reply-threading or attachment encoding lands
once and both get it.

Design rules
------------
  * the googleapiclient service is INJECTED (tests pass a fake, production
    builds one via actions/_google_auth);
  * every method returns plain dicts/lists — never google client objects;
  * tokens never enter a return value;
  * failures raise `GmailError` with a short, user-safe message; the tool and
    agent layers decide how to present it;
  * bulk operations use Gmail's batch endpoints (batchModify / batchDelete),
    not a per-message loop, so a 500-message action is one API call.

Scopes: gmail.modify covers read, compose, send, labels, trash and drafts.
Permanent delete is a separate, explicitly-authorized operation.
"""
from __future__ import annotations

import base64
import mimetypes
import os
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Iterable, Optional

from orchestrator.agents.productivity import google_tools as gt

MAX_RESULTS_CAP = 500          # listing/search hard cap
DEFAULT_MAX_RESULTS = 10
BODY_CHARS = 20_000
ATTACHMENT_CHUNK = 4 * 1024 * 1024

# Search label shorthands → Gmail system label ids.
LABELS: dict[str, list[str]] = {
    "inbox": ["INBOX"],
    "unread": ["UNREAD"],
    "read": [],                # applied as -is:unread in the query instead
    "starred": ["STARRED"],
    "important": ["IMPORTANT"],
    "sent": ["SENT"],
    "draft": ["DRAFT"],
    "drafts": ["DRAFT"],
    "trash": ["TRASH"],
    "spam": ["SPAM"],
}

# Friendly name → system label id, for add/remove operations.
SYSTEM_LABELS = {
    "inbox": "INBOX",
    "unread": "UNREAD",
    "starred": "STARRED",
    "important": "IMPORTANT",
    "sent": "SENT",
    "trash": "TRASH",
    "spam": "SPAM",
}


class GmailError(Exception):
    """A Gmail operation failed with a message safe to show the user."""


# ── helpers ──────────────────────────────────────────────────────────────────

def _err(e: Exception) -> str:
    """Short, user-safe description of a google client error."""
    status = ""
    detail = ""
    try:
        status = str(getattr(e, "resp", None).status)
    except Exception:
        pass
    try:
        content = getattr(e, "content", b"")
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        m = re.search(r'"message"\s*:\s*"([^"]+)"', str(content))
        if m:
            detail = m.group(1)
    except Exception:
        pass
    if status and detail:
        return f"Gmail API error {status}: {detail}"
    if status:
        return f"Gmail API error {status}."
    return f"Gmail error: {type(e).__name__}"


def _b64(data: str) -> bytes:
    if not data:
        return b""
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad)
    except Exception:
        return b""


def _header(payload: dict, name: str) -> str:
    for h in (payload or {}).get("headers") or []:
        if str(h.get("name", "")).lower() == name.lower():
            return h.get("value", "")
    return ""


def _fmt_date(value_ms) -> str:
    try:
        ms = int(value_ms or 0)
        if ms:
            return (datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
                    .astimezone().strftime("%b %d %H:%M"))
    except (ValueError, OSError, OverflowError):
        pass
    return ""


def _walk_attachments(payload: dict, out: list[dict]) -> None:
    """Every part that carries a filename + attachmentId."""
    if not payload:
        return
    filename = payload.get("filename") or ""
    body = payload.get("body") or {}
    if filename and body.get("attachmentId"):
        out.append({
            "attachment_id": body["attachmentId"],
            "filename": filename,
            "mime_type": payload.get("mimeType", ""),
            "size": int(body.get("size") or 0),
        })
    for part in payload.get("parts") or []:
        _walk_attachments(part, out)


def _collect_html(payload: dict) -> str:
    """Concatenated text/html parts of a format=full payload."""
    htmls: list[str] = []

    def walk(node: dict) -> None:
        if str(node.get("mimeType") or "") == "text/html":
            data = (node.get("body") or {}).get("data", "")
            if data:
                htmls.append(_b64(data).decode("utf-8", "replace"))
        for part in node.get("parts") or []:
            walk(part)

    walk(payload or {})
    return "\n".join(htmls)


def _collect_body(payload: dict) -> tuple[str, str]:
    """(plain_text, html) walking a format=full payload."""
    return gt.collect_body(payload), _collect_html(payload)


def _clean_query(text: str) -> str:
    return " ".join(str(text or "").split())


def normalise_ids(message_ids) -> list[str]:
    if isinstance(message_ids, str):
        message_ids = [m for m in re.split(r"[\s,;]+", message_ids) if m]
    seen, out = set(), []
    for mid in message_ids or []:
        mid = str(mid or "").strip()
        if mid and mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def _guess_mime(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def build_raw_message(*, to: str, subject: str, body: str, cc: str = "",
                      bcc: str = "", html_body: str = "",
                      attachment_paths: Iterable[str] = (),
                      in_reply_to: str = "", references: str = "",
                      from_addr: str = "") -> str:
    """Build a base64url raw MIME message.

    Raises GmailError for a missing recipient/body or an unreadable
    attachment — the caller turns that into a sentence for the user.
    """
    to = str(to or "").strip()
    if "@" not in to:
        raise GmailError(
            "I need the recipient's email address — a name alone is not "
            "enough for Gmail.")
    text = str(body or "")
    if not text.strip() and not str(html_body or "").strip():
        raise GmailError("I need some text to put in the email body.")

    msg = EmailMessage()
    if from_addr:
        msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = (str(subject or "").strip() or "(no subject)")
    if str(cc or "").strip():
        msg["Cc"] = str(cc).strip()
    if str(bcc or "").strip():
        msg["Bcc"] = str(bcc).strip()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    if str(html_body or "").strip():
        msg.set_content(text or " ")
        msg.add_alternative(str(html_body), subtype="html")
    else:
        msg.set_content(text)

    for raw_path in attachment_paths or ():
        path = Path(str(raw_path)).expanduser()
        if not path.is_file():
            raise GmailError(f"Attachment not found: {path.name}")
        ctype = _guess_mime(path)
        maintype, _, subtype = ctype.partition("/")
        try:
            data = path.read_bytes()
        except OSError as e:
            raise GmailError(f"Could not read attachment {path.name}: {e}")
        msg.add_attachment(data, maintype=maintype or "application",
                           subtype=subtype or "octet-stream",
                           filename=path.name)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _reply_subject(subject: str) -> str:
    s = (subject or "").strip()
    if re.match(r"(?i)^re:", s):
        return s
    return f"Re: {s or '(no subject)'}"


def _forward_subject(subject: str) -> str:
    s = (subject or "").strip()
    if re.match(r"(?i)^fwd?:", s):
        return s
    return f"Fwd: {s or '(no subject)'}"


def _addresses(value: str) -> list[str]:
    return [addr for _name, addr in getaddresses([value or ""]) if addr]


# ── the client ───────────────────────────────────────────────────────────────

class GmailClient:
    """Every Gmail operation RIO needs, in one injectable object."""

    def __init__(self, service, user_id: str = "me") -> None:
        self.svc = service
        self.user_id = user_id
        self._profile: Optional[dict] = None

    # ── identity ─────────────────────────────────────────────────────────
    def profile(self) -> dict:
        if self._profile is None:
            try:
                p = self.svc.users().getProfile(userId=self.user_id).execute()
            except Exception as e:
                raise GmailError(_err(e))
            self._profile = {
                "email": str(p.get("emailAddress") or ""),
                "messages_total": int(p.get("messagesTotal") or 0),
                "threads_total": int(p.get("threadsTotal") or 0),
            }
        return dict(self._profile)

    @property
    def address(self) -> str:
        try:
            return self.profile()["email"]
        except Exception:
            return ""

    # ── listing / search ─────────────────────────────────────────────────
    def _list_ids(self, query: str = "", label: str = "",
                  max_results: int = DEFAULT_MAX_RESULTS,
                  page_token: str = "", include_spam_trash: bool = False,
                  ) -> tuple[list[str], str]:
        n = max(1, min(MAX_RESULTS_CAP, int(max_results or DEFAULT_MAX_RESULTS)))
        params: dict[str, Any] = {"userId": self.user_id, "maxResults": n}
        q = _clean_query(query)
        label_key = str(label or "").strip().lower()
        if label_key in ("read",):
            q = (q + " -is:unread").strip()
        elif label_key in LABELS and LABELS[label_key]:
            params["labelIds"] = LABELS[label_key]
        if q:
            params["q"] = q
        if page_token:
            params["pageToken"] = page_token
        if include_spam_trash:
            params["includeSpamTrash"] = True
        try:
            resp = self.svc.users().messages().list(**params).execute()
        except Exception as e:
            raise GmailError(_err(e))
        ids = [m["id"] for m in (resp.get("messages") or []) if m.get("id")]
        return ids, str(resp.get("nextPageToken") or "")

    def search(self, query: str = "", label: str = "",
               max_results: int = DEFAULT_MAX_RESULTS,
               page_token: str = "", include_spam_trash: bool = False
               ) -> dict:
        ids, nxt = self._list_ids(query=query, label=label,
                                  max_results=max_results,
                                  page_token=page_token,
                                  include_spam_trash=include_spam_trash)
        return {"messages": self.summaries(ids),
                "next_page_token": nxt, "count": len(ids)}

    def search_structured(self, *, sender: str = "", recipient: str = "",
                          subject: str = "", keywords: str = "",
                          after: str = "", before: str = "",
                          has_attachment: bool = False,
                          unread: Optional[bool] = None,
                          label: str = "", max_results: int = DEFAULT_MAX_RESULTS,
                          ) -> dict:
        """Build Gmail search syntax from structured filters."""
        parts = []
        if sender:
            parts.append(f"from:{sender}")
        if recipient:
            parts.append(f"to:{recipient}")
        if subject:
            parts.append(f'subject:("{subject}")' if " " in subject
                         else f"subject:{subject}")
        if keywords:
            parts.append(_clean_query(keywords))
        if after:
            parts.append(f"after:{after}")
        if before:
            parts.append(f"before:{before}")
        if has_attachment:
            parts.append("has:attachment")
        if unread is True:
            parts.append("is:unread")
        elif unread is False:
            parts.append("is:read")
        return self.search(query=" ".join(parts), label=label,
                           max_results=max_results)

    def ids(self, query: str = "", label: str = "",
            max_results: int = DEFAULT_MAX_RESULTS) -> list[str]:
        return self._list_ids(query=query, label=label,
                              max_results=max_results)[0]

    def summaries(self, ids: Iterable[str]) -> list[dict]:
        out = []
        for mid in ids:
            try:
                out.append(self.get(mid, full=False))
            except GmailError as e:
                out.append({"id": mid, "error": str(e)})
        return out

    # ── reading ──────────────────────────────────────────────────────────
    def get(self, message_id: str, full: bool = True) -> dict:
        mid = str(message_id or "").strip()
        if not mid:
            raise GmailError("Give me a message id from a list or search.")
        fmt = "full" if full else "metadata"
        kwargs: dict[str, Any] = {"userId": self.user_id, "id": mid,
                                  "format": fmt}
        if not full:
            kwargs["metadataHeaders"] = ["From", "To", "Cc", "Subject",
                                         "Date", "Message-ID", "References"]
        try:
            msg = self.svc.users().messages().get(**kwargs).execute()
        except Exception as e:
            raise GmailError(_err(e))

        payload = msg.get("payload") or {}
        out = {
            "id": mid,
            "thread_id": str(msg.get("threadId") or ""),
            "from": _header(payload, "From"),
            "to": _header(payload, "To"),
            "cc": _header(payload, "Cc"),
            "subject": _header(payload, "Subject") or "(no subject)",
            "date": _fmt_date(msg.get("internalDate"))
            or _header(payload, "Date"),
            "message_id_header": _header(payload, "Message-ID"),
            "references": _header(payload, "References"),
            "snippet": str(msg.get("snippet") or "").replace("\n", " ")[:300],
            "labels": list(msg.get("labelIds") or []),
            "size_estimate": int(msg.get("sizeEstimate") or 0),
            "unsubscribe": _header(payload, "List-Unsubscribe"),
            "attachments": [],
        }
        parts: list[dict] = []
        _walk_attachments(payload, parts)
        out["attachments"] = parts
        out["has_attachments"] = bool(parts)
        if full:
            text, html = _collect_body(payload)
            out["body"] = (text or out["snippet"] or "")[:BODY_CHARS]
            out["html"] = (html or "")[:BODY_CHARS * 2]
        return out

    def thread(self, thread_id: str) -> dict:
        tid = str(thread_id or "").strip()
        if not tid:
            raise GmailError("Give me a thread id.")
        try:
            resp = self.svc.users().threads().get(
                userId=self.user_id, id=tid, format="full").execute()
        except Exception as e:
            raise GmailError(_err(e))
        msgs = [m.get("id") for m in (resp.get("messages") or []) if m.get("id")]
        return {"thread_id": tid, "messages": self.summaries(msgs)}

    def attachment(self, message_id: str, attachment_id: str) -> bytes:
        try:
            resp = (self.svc.users().messages().attachments()
                    .get(userId=self.user_id, messageId=message_id,
                         id=attachment_id).execute())
        except Exception as e:
            raise GmailError(_err(e))
        return _b64(str(resp.get("data") or ""))

    def download_attachment(self, message_id: str, attachment_id: str,
                            dest_dir: str | os.PathLike,
                            filename: str = "", overwrite: bool = False) -> dict:
        data = self.attachment(message_id, attachment_id)
        if not data:
            raise GmailError("The attachment came back empty.")
        folder = Path(dest_dir).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        safe = _safe_filename(filename or f"attachment-{attachment_id[:8]}")
        path = folder / safe
        if path.exists() and not overwrite:
            stem, suffix = path.stem, path.suffix
            i = 2
            while (folder / f"{stem} ({i}){suffix}").exists():
                i += 1
            path = folder / f"{stem} ({i}){suffix}"
        try:
            path.write_bytes(data)
        except OSError as e:
            raise GmailError(f"Could not save the attachment: {e}")
        return {"path": str(path), "filename": path.name,
                "size": len(data), "message_id": message_id}

    def download_all_attachments(self, message_id: str,
                                 dest_dir: str | os.PathLike) -> list[dict]:
        msg = self.get(message_id, full=True)
        out = []
        for att in msg.get("attachments") or []:
            try:
                out.append(self.download_attachment(
                    message_id, att["attachment_id"], dest_dir,
                    filename=att.get("filename", "")))
            except GmailError as e:
                out.append({"error": str(e), "filename": att.get("filename")})
        return out

    # ── sending ──────────────────────────────────────────────────────────
    def send(self, *, to: str, subject: str, body: str, cc: str = "",
             bcc: str = "", html_body: str = "", attachment_paths=(),
             thread_id: str = "", in_reply_to: str = "",
             references: str = "") -> dict:
        raw = build_raw_message(to=to, subject=subject, body=body, cc=cc,
                                bcc=bcc, html_body=html_body,
                                attachment_paths=attachment_paths,
                                in_reply_to=in_reply_to,
                                references=references,
                                from_addr=self.address)
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        try:
            resp = self.svc.users().messages().send(
                userId=self.user_id, body=payload).execute()
        except Exception as e:
            raise GmailError(_err(e))
        mid = str(resp.get("id") or "")
        if not mid:
            raise GmailError(
                "Gmail did not return a message id, so delivery is not "
                "confirmed.")
        return {"message_id": mid,
                "thread_id": str(resp.get("threadId") or thread_id),
                "label_ids": list(resp.get("labelIds") or []),
                "to": to, "subject": subject}

    def reply(self, message_id: str, body: str, *, reply_all: bool = False,
              html_body: str = "", attachment_paths=(),
              cc: str = "", bcc: str = "") -> dict:
        orig = self.get(message_id, full=True)
        to_addrs = _addresses(orig.get("from", ""))
        cc_list: list[str] = []
        if reply_all:
            mine = (self.address or "").lower()
            for addr in (_addresses(orig.get("to", ""))
                         + _addresses(orig.get("cc", ""))):
                if addr.lower() != mine and addr.lower() not in {
                        a.lower() for a in to_addrs}:
                    cc_list.append(addr)
        if not to_addrs:
            raise GmailError("I could not work out who to reply to.")
        refs = orig.get("references", "")
        mid_header = orig.get("message_id_header", "")
        references = f"{refs} {mid_header}".strip() if mid_header else refs
        return self.send(
            to=", ".join(to_addrs),
            cc=cc or ", ".join(cc_list),
            bcc=bcc,
            subject=_reply_subject(orig.get("subject", "")),
            body=body,
            html_body=html_body,
            attachment_paths=attachment_paths,
            thread_id=orig.get("thread_id", ""),
            in_reply_to=mid_header,
            references=references,
        )

    def forward(self, message_id: str, to: str, body: str = "",
                attachment_paths=()) -> dict:
        orig = self.get(message_id, full=True)
        quoted = [
            "",
            "---------- Forwarded message ---------",
            f"From: {orig.get('from')}",
            f"Date: {orig.get('date')}",
            f"Subject: {orig.get('subject')}",
            f"To: {orig.get('to')}",
            "",
            orig.get("body", ""),
        ]
        text = (body.rstrip() + "\n" if body else "") + "\n".join(quoted)
        return self.send(to=to,
                         subject=_forward_subject(orig.get("subject", "")),
                         body=text,
                         attachment_paths=attachment_paths)

    # ── drafts ───────────────────────────────────────────────────────────
    def _raw(self, *, to, subject, body, cc, bcc, html_body,
             attachment_paths) -> str:
        return build_raw_message(to=to, subject=subject, body=body, cc=cc,
                                 bcc=bcc, html_body=html_body,
                                 attachment_paths=attachment_paths,
                                 from_addr=self.address)

    def create_draft(self, *, to: str, subject: str, body: str, cc: str = "",
                     bcc: str = "", html_body: str = "",
                     attachment_paths=(), thread_id: str = "") -> dict:
        raw = self._raw(to=to, subject=subject, body=body, cc=cc, bcc=bcc,
                        html_body=html_body,
                        attachment_paths=attachment_paths)
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        try:
            resp = self.svc.users().drafts().create(
                userId=self.user_id, body={"message": message}).execute()
        except Exception as e:
            raise GmailError(_err(e))
        return {"draft_id": str(resp.get("id") or ""),
                "message_id": str((resp.get("message") or {}).get("id") or ""),
                "to": to, "subject": subject}

    def update_draft(self, draft_id: str, *, to: str, subject: str, body: str,
                     cc: str = "", bcc: str = "", html_body: str = "",
                     attachment_paths=()) -> dict:
        raw = self._raw(to=to, subject=subject, body=body, cc=cc, bcc=bcc,
                        html_body=html_body,
                        attachment_paths=attachment_paths)
        try:
            resp = self.svc.users().drafts().update(
                userId=self.user_id, id=str(draft_id),
                body={"message": {"raw": raw}}).execute()
        except Exception as e:
            raise GmailError(_err(e))
        return {"draft_id": str(resp.get("id") or draft_id),
                "message_id": str((resp.get("message") or {}).get("id") or "")}

    def list_drafts(self, max_results: int = 10) -> list[dict]:
        n = max(1, min(MAX_RESULTS_CAP, int(max_results or 10)))
        try:
            resp = self.svc.users().drafts().list(
                userId=self.user_id, maxResults=n).execute()
        except Exception as e:
            raise GmailError(_err(e))
        out = []
        for row in resp.get("drafts") or []:
            did = str(row.get("id") or "")
            entry = {"draft_id": did}
            mid = str((row.get("message") or {}).get("id") or "")
            if mid:
                try:
                    m = self.get(mid, full=False)
                    entry.update({"message_id": mid, "to": m.get("to"),
                                  "subject": m.get("subject"),
                                  "date": m.get("date")})
                except GmailError:
                    entry["message_id"] = mid
            out.append(entry)
        return out

    def get_draft(self, draft_id: str) -> dict:
        try:
            resp = self.svc.users().drafts().get(
                userId=self.user_id, id=str(draft_id), format="full").execute()
        except Exception as e:
            raise GmailError(_err(e))
        mid = str((resp.get("message") or {}).get("id") or "")
        out = {"draft_id": str(resp.get("id") or draft_id),
               "message_id": mid}
        if mid:
            try:
                out["message"] = self.get(mid, full=True)
            except GmailError:
                pass
        return out

    def send_draft(self, draft_id: str) -> dict:
        try:
            resp = self.svc.users().drafts().send(
                userId=self.user_id,
                body={"id": str(draft_id)}).execute()
        except Exception as e:
            raise GmailError(_err(e))
        mid = str(resp.get("id") or "")
        if not mid:
            raise GmailError("Gmail did not confirm the draft send.")
        return {"message_id": mid,
                "thread_id": str(resp.get("threadId") or "")}

    def delete_draft(self, draft_id: str) -> dict:
        try:
            self.svc.users().drafts().delete(
                userId=self.user_id, id=str(draft_id)).execute()
        except Exception as e:
            raise GmailError(_err(e))
        return {"draft_id": str(draft_id), "deleted": True}

    # ── modifying ────────────────────────────────────────────────────────
    def modify(self, message_ids, *, add: Iterable[str] = (),
               remove: Iterable[str] = ()) -> dict:
        ids = normalise_ids(message_ids)
        if not ids:
            raise GmailError("Which message(s)? Give me at least one id.")
        add_ids = [a for a in add if a]
        rem_ids = [r for r in remove if r]
        if not add_ids and not rem_ids:
            raise GmailError("Nothing to change — no labels given.")
        if len(ids) == 1:
            try:
                self.svc.users().messages().modify(
                    userId=self.user_id, id=ids[0],
                    body={"addLabelIds": add_ids,
                          "removeLabelIds": rem_ids}).execute()
            except Exception as e:
                raise GmailError(_err(e))
        else:
            try:
                self.svc.users().messages().batchModify(
                    userId=self.user_id,
                    body={"ids": ids, "addLabelIds": add_ids,
                          "removeLabelIds": rem_ids}).execute()
            except Exception as e:
                raise GmailError(_err(e))
        return {"updated": len(ids), "ids": ids,
                "added": add_ids, "removed": rem_ids}

    def mark_read(self, ids) -> dict:
        return self.modify(ids, remove=["UNREAD"])

    def mark_unread(self, ids) -> dict:
        return self.modify(ids, add=["UNREAD"])

    def star(self, ids) -> dict:
        return self.modify(ids, add=["STARRED"])

    def unstar(self, ids) -> dict:
        return self.modify(ids, remove=["STARRED"])

    def archive(self, ids) -> dict:
        return self.modify(ids, remove=["INBOX"])

    def move_to_inbox(self, ids) -> dict:
        return self.modify(ids, add=["INBOX"])

    def add_label(self, ids, label: str) -> dict:
        return self.modify(ids, add=[self.resolve_label(label)])

    def remove_label(self, ids, label: str) -> dict:
        return self.modify(ids, remove=[self.resolve_label(label)])

    def trash(self, ids) -> dict:
        ids = normalise_ids(ids)
        if not ids:
            raise GmailError("Which message(s) should I move to Trash?")
        done, failed = [], []
        for mid in ids:
            try:
                self.svc.users().messages().trash(
                    userId=self.user_id, id=mid).execute()
                done.append(mid)
            except Exception as e:            # partial bulk failure is real
                failed.append({"id": mid, "error": _err(e)})
        return {"trashed": len(done), "ids": done, "failed": failed}

    def untrash(self, ids) -> dict:
        ids = normalise_ids(ids)
        if not ids:
            raise GmailError("Which message(s) should I restore?")
        done, failed = [], []
        for mid in ids:
            try:
                self.svc.users().messages().untrash(
                    userId=self.user_id, id=mid).execute()
                done.append(mid)
            except Exception as e:
                failed.append({"id": mid, "error": _err(e)})
        return {"restored": len(done), "ids": done, "failed": failed}

    def delete_permanent(self, ids) -> dict:
        """Permanent delete. Callers MUST have explicit confirmation first."""
        ids = normalise_ids(ids)
        if not ids:
            raise GmailError("Which message(s) should I delete permanently?")
        if len(ids) == 1:
            try:
                self.svc.users().messages().delete(
                    userId=self.user_id, id=ids[0]).execute()
            except Exception as e:
                raise GmailError(_err(e))
        else:
            try:
                self.svc.users().messages().batchDelete(
                    userId=self.user_id, body={"ids": ids}).execute()
            except Exception as e:
                raise GmailError(_err(e))
        return {"deleted": len(ids), "ids": ids}

    def empty_trash(self, max_results: int = 500) -> dict:
        ids = self.ids(query="in:trash", max_results=min(max_results,
                                                         MAX_RESULTS_CAP))
        if not ids:
            return {"deleted": 0, "ids": []}
        return self.delete_permanent(ids)

    # ── labels ───────────────────────────────────────────────────────────
    def labels(self) -> list[dict]:
        try:
            resp = self.svc.users().labels().list(
                userId=self.user_id).execute()
        except Exception as e:
            raise GmailError(_err(e))
        return [{"id": str(row.get("id") or ""),
                 "name": str(row.get("name") or ""),
                 "type": str(row.get("type") or "")}
                for row in resp.get("labels") or []]

    def resolve_label(self, label: str) -> str:
        """Friendly/system name or a user label name → label id."""
        key = str(label or "").strip()
        if not key:
            raise GmailError("Which label?")
        if key in SYSTEM_LABELS:
            return SYSTEM_LABELS[key]
        if re.fullmatch(r"Label_\d+", key) or key in (
                "INBOX", "UNREAD", "STARRED", "IMPORTANT", "SENT", "TRASH",
                "SPAM", "DRAFT"):
            return key
        for row in self.labels():
            if row["name"].lower() == key.lower() or row["id"] == key:
                return row["id"]
        raise GmailError(f"No Gmail label named “{key}”.")

    def create_label(self, name: str) -> dict:
        key = str(name or "").strip()
        if not key:
            raise GmailError("What should the new label be called?")
        try:
            resp = self.svc.users().labels().create(
                userId=self.user_id,
                body={"name": key, "labelListVisibility": "labelShow",
                      "messageListVisibility": "show"}).execute()
        except Exception as e:
            raise GmailError(_err(e))
        return {"id": str(resp.get("id") or ""),
                "name": str(resp.get("name") or key)}

    def rename_label(self, label: str, new_name: str) -> dict:
        lid = self.resolve_label(label)
        try:
            resp = self.svc.users().labels().patch(
                userId=self.user_id, id=lid,
                body={"name": str(new_name or "").strip()}).execute()
        except Exception as e:
            raise GmailError(_err(e))
        return {"id": lid, "name": str(resp.get("name") or new_name)}

    def delete_label(self, label: str) -> dict:
        lid = self.resolve_label(label)
        if not lid.startswith("Label_"):
            raise GmailError("System labels cannot be deleted.")
        try:
            self.svc.users().labels().delete(
                userId=self.user_id, id=lid).execute()
        except Exception as e:
            raise GmailError(_err(e))
        return {"id": lid, "deleted": True}


def _safe_filename(name: str) -> str:
    base = os.path.basename(str(name or "")).strip() or "attachment"
    base = re.sub(r"[^\w.\- ()\[\]]+", "_", base)
    return base[:180] or "attachment"


__all__ = [
    "GmailClient", "GmailError", "build_raw_message", "normalise_ids",
    "LABELS", "SYSTEM_LABELS", "MAX_RESULTS_CAP",
]
