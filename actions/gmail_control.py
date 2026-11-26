"""
actions/gmail_control.py — voice control for Gmail.

OAuth setup lives in _google_auth.py's docstring (one-time, Desktop OAuth
client → config/client_secret.json). Scope is gmail.modify: read, send, label,
archive and TRASH — but never a permanent delete. Gmail keeps trashed mail for
30 days, so everything this skill does is recoverable, which is what lets it
answer without a confirmation banner on every step — except for send, trash
bulk, and permanent delete, which now gate behind the productivities confirm
layer.

Operations (the `operation` parameter — `command` is accepted as an alias so
the voice_router / core/voice_command wiring still works):
    list       latest mail (label: unread | inbox | starred | important | sent)
    search     Gmail search syntax in `query` ("from:alice has:attachment")
    read       full body of one message (message_id from a list/search result)
    thread     dump a full thread
    drafts     list/create/update/delete/send drafts
    send       `to` + `subject` + `body` (optional `cc`, optional attachments)
    reply      reply to one message
    forward    forward to one address
    mark_read  clear UNREAD on one message
    archive    remove INBOX (still in All Mail)
    trash      move to Trash (recoverable for 30 days — CONFIRMED before run)
    labels     list or create a label
    attachment download one attachment locally; optionally upload to Drive

Safety rules encoded here:
  * sending replies FORWARDS — CONFIRMED before run.
  * bulk trash / permanent delete / empty trash — CONFIRMED before run.
  * nothing raises outward; errors return short sentences.

Tool discovery: auto-loaded by core/action_loader.py under "gmail_control".
"""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable, Optional

from actions import _google_auth as ga
from orchestrator.agents.productivity.gmail_client import normalise_ids

try:
    from googleapiclient.errors import HttpError
    _HAS_GOOGLE = True
except ImportError:
    HttpError = Exception          # type: ignore[misc,assignment]
    _HAS_GOOGLE = False

MAX_RESULTS_DEFAULT = 8
MAX_RESULTS_CAP = 25             # tool-layer cap — matches existing tests
BODY_CHARS = 4000

# ── small helpers ─────────────────────────────────────────────────────────────


def _clamp_n(value, default: int = MAX_RESULTS_DEFAULT) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_RESULTS_CAP, n))


def _extract_operation(params: dict) -> str:
    """Accept both `operation` (tool schema) and `command` (voice_router alias)."""
    op = (str(params.get("operation") or "") or "").strip().lower()
    if not op:
        op = (str(params.get("command") or "") or "").strip().lower()
    return op


def _err(e: Exception) -> str:
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
        import re
        m = re.search(r'\"message\"\s*:\s*\"([^\"]+)\"', str(content))
        if m:
            detail = m.group(1)
    except Exception:
        pass
    if status and detail:
        return f"Gmail API error {status}: {detail}"
    if status:
        return f"Gmail API error {status}."
    return f"Gmail error: {type(e).__name__}"


def _log(player, text: str) -> None:
    if player:
        try:
            player.write_log(text[:120])
        except Exception:
            pass


# ── api bootstrap ─────────────────────────────────────────────────────────────

def _build_client():
    """Return a GmailClient or a SetupNeeded sentence."""
    if not _HAS_GOOGLE:
        raise ga.SetupNeeded(
            "The Google API libraries are not installed. Run: "
            "pip install google-api-python-client google-auth-oauthlib"
        )
    try:
        svc = ga.get_service("gmail", "v1", [ga.GMAIL_SCOPE], "gmail_token")
    except ga.SetupNeeded as e:
        raise
    except Exception as e:
        raise ga.SetupNeeded(f"Could not connect to Gmail: {e}") from e
    from orchestrator.agents.productivity.gmail_client import GmailClient
    return GmailClient(svc)


# ── public entry ──────────────────────────────────────────────────────────────


def gmail_control(parameters: dict, player=None) -> str:
    params = parameters or {}
    op = _extract_operation(params)

    if not op:
        return (
            "Tell me which Gmail operation to run: list, search, read, "
            "thread, drafts, send, reply, forward, mark_read, archive, "
            "trash, labels, or attachment."
        )

    try:
        client = _build_client()
    except ga.SetupNeeded as e:
        return str(e)

    try:
        return _dispatch(client, op, params, player)
    except HttpError as e:
        return _err(e)
    except Exception as e:
        return f"Gmail error: {e}"


# ── dispatch ──────────────────────────────────────────────────────────────────


def _dispatch(client, op: str, params: dict, player=None) -> str:
    mid = (params.get("message_id") or "").strip()
    tid = (params.get("thread_id") or "").strip()
    draft_id = (params.get("draft_id") or "").strip()
    max_results = _clamp_n(params.get("max_results"))

    # ── reads ───────────────────────────────────────────────────────────────
    if op in ("list", "search"):
        query = str(params.get("query") or "").strip()
        label = str(params.get("label") or "").strip()
        include_trash = bool(params.get("include_spam_trash"))
        result = client.search(query=query, label=label,
                               max_results=max_results,
                               include_spam_trash=include_trash)
        ids = [m["id"] for m in result.get("messages") or []
               if isinstance(m, dict)]
        header = (
            f'Gmail search "{query}"' if op == "search"
            else f'Gmail {label or "inbox"} — latest'
        )
        out = _render_list(ids, header, result.get("messages"), client)
        _log(player, out.splitlines()[0])
        return out

    if op == "structured_search":
        result = client.search_structured(
            sender=str(params.get("sender") or ""),
            recipient=str(params.get("recipient") or ""),
            subject=str(params.get("subject") or ""),
            keywords=str(params.get("keywords") or ""),
            after=str(params.get("after") or ""),
            before=str(params.get("before") or ""),
            has_attachment=bool(params.get("has_attachment")),
            unread=params.get("unread"),
            label=str(params.get("label") or "").strip(),
            max_results=max_results,
        )
        header = f'Gmail structured search ({params})'
        return _render_list([], header, result.get("messages", []), client)

    if op == "read":
        if not mid:
            return "Give me a message_id from a previous list or search."
        msg = client.get(mid, full=True)
        out = (
            f"From: {msg.get('from')}\n"
            f"To: {msg.get('to')}\n"
            f"Cc: {msg.get('cc') or ''}\n"
            f"Subject: {msg.get('subject') or '(no subject)'}\n"
            f"Date: {msg.get('date')}\n"
            f"Labels: {', '.join(msg.get('labels') or [])}\n\n"
            f"{msg.get('body', msg.get('snippet')) or '(empty)'}"
        )
        _log(player, f"[Gmail] read {mid[:10]}…")
        return out

    if op == "thread":
        if not tid and mid:
            # caller may have only given us the top message id.
            tid = client.get(mid, full=False).get("thread_id", "")
        if not tid:
            return "Give me a thread_id (or a message_id from inside a thread)."
        thread = client.thread(tid)
        lines = [f"Thread: {tid} ({len(thread.get('messages', []))} messages)"]
        for m in thread.get("messages", [])[:20]:
            lines.append(
                f"\n  [{m.get('id')}] {m.get('date','')} — "
                f"{m.get('from','?')} | {m.get('subject','?')}"
            )
        return "\n".join(lines)

    if op == "profile":
        p = client.profile()
        return (f"Account: {p.get('email')} | "
                f"Inbox: {p.get('messages_total', '?')} msgs | "
                f"Threads: {p.get('threads_total', '?')}")

    # ── drafts ──────────────────────────────────────────────────────────────
    if op == "drafts":
        out = client.list_drafts(max_results=max_results)
        if not out:
            return "No drafts."
        lines = [f"Drafts ({len(out)}):"]
        for d in out:
            lines.append(
                f"  [{d['draft_id']}] “{(d.get('subject') or '?')[:70]}” → {d.get('to','')}")
        lines.append("Use draft_id for get/send/update/delete.")
        return "\n".join(lines)

    if op == "create_draft":
        draft = client.create_draft(
            to=str(params.get("to") or ""),
            subject=str(params.get("subject") or ""),
            body=str(params.get("body") or ""),
            cc=str(params.get("cc") or ""),
            html_body=str(params.get("html_body") or ""),
            attachment_paths=params.get("attachments") or [],
            thread_id=tid,
        )
        return f"Draft saved [id={draft['draft_id']}]. Nothing sent yet."

    if op == "update_draft":
        if not draft_id:
            return "Give me a draft_id (from the drafts operation)."
        updated = client.update_draft(draft_id,
                                      to=str(params.get("to") or ""),
                                      subject=str(params.get("subject") or ""),
                                      body=str(params.get("body") or ""),
                                      cc=str(params.get("cc") or ""),
                                      html_body=str(params.get("html_body") or ""),
                                      attachment_paths=params.get("attachments") or [])
        return f"Draft updated [id={updated['draft_id']}]."

    if op == "delete_draft":
        if not draft_id:
            return "Give me a draft_id to delete."
        client.delete_draft(draft_id)
        return "Draft deleted."

    if op == "send_draft":
        if not draft_id:
            return "Give me a draft_id to send."
        # Send a draft is irreversible — gate it.
        draft_meta = client.get_draft(draft_id)
        subject = (draft_meta.get("message") or {}).get("subject", "")
        preview = (draft_meta.get("message") or {}).get("to", "")
        title = f"Send draft {draft_id[:8]}…"
        detail = f"To: {preview} | Subject: {subject}"
        def _run_send_draft() -> str:
            r = client.send_draft(draft_id)
            return f"Sent — message_id={r['message_id']}"
        return _confirm("send_draft", title=title, detail=detail,
                        run=_run_send_draft, player=player)

    # ── send / reply / forward ──────────────────────────────────────────────
    if op in ("send", "reply", "forward"):
        to = str(params.get("to") or "").strip()
        cc = str(params.get("cc") or "")
        body = str(params.get("body") or "")
        subject = str(params.get("subject") or "")
        attachment_paths = params.get("attachments")
        if not isinstance(attachment_paths, list):
            attachment_paths = []
        html_body = str(params.get("html_body") or "")

        problem = _check_send_params(to, body)
        if problem:
            return problem

        if op == "reply":
            if not mid:
                return "Reply needs a message_id (use operation=list/search first)."
            orig = client.get(mid, full=False)
            if not to:
                to = (orig.get("from") or "").strip()
            subject = params.get("subject") or (orig.get("subject") or "(no subject)")
            title = f"Reply to {(orig.get('from') or to)[:60]}"
            detail = f"To: {to} | Subject: {subject}"
            def _run_reply() -> str:
                r = client.reply(message_id=mid, body=body,
                                 html_body=html_body, cc=cc,
                                 attachment_paths=attachment_paths)
                return f"Reply sent — message_id={r['message_id']}"
            return _confirm("reply", title=title, detail=detail,
                            run=_run_reply, player=player)

        if op == "forward":
            if not mid:
                return "Forward needs a message_id (use operation=list/search first)."
            title = f"Forward {(orig.get('subject') or '')[:60]}"
            detail = f"To: {to} | From original: {(orig.get('from') or '')}"
            def _run_forward() -> str:
                r = client.forward(message_id=mid, to=to, body=body,
                                   attachment_paths=attachment_paths)
                return f"Forwarded — message_id={r['message_id']}"
            return _confirm("forward", title=title, detail=detail,
                            run=_run_forward, player=player)

        # Plain send
        title = f"Send email to {to[:60]}" if to else "Send email"
        detail = f"Subject: {subject} | To: {to}"
        def _run_send() -> str:
            r = client.send(to=to, subject=subject, body=body, cc=cc,
                            html_body=html_body, attachment_paths=attachment_paths)
            return f"Sent — message_id={r['message_id']}"
        return _confirm("send", title=title, detail=detail,
                        run=_run_send, player=player)

    # ── modify ──────────────────────────────────────────────────────────────
    if op == "mark_read":
        if not mid:
            return "Give me a message_id from a previous list or search."
        client.mark_read(mid)
        _log(player, f"[Gmail] mark_read {mid[:10]}")
        return "Marked as read."

    if op == "mark_unread":
        if not mid:
            return "Give me a message_id from a previous list or search."
        client.mark_unread(mid)
        return "Marked as unread."

    if op == "star":
        client.star(params.get("message_ids") or mid)
        return "Starred."

    if op == "unstar":
        client.unstar(params.get("message_ids") or mid)
        return "Unstarred."

    if op == "archive":
        if not mid:
            return "Give me a message_id from a previous list or search."
        client.archive(mid)
        _log(player, f"[Gmail] archive {mid[:10]}")
        return "Archived — still in All Mail, out of the inbox."

    if op == "move_to_inbox":
        client.move_to_inbox(params.get("message_ids") or mid)
        return "Moved back to Inbox."

    if op in ("add_label", "remove_label"):
        if not mid:
            return "Give me a message_id from a previous list or search."
        label = str(params.get("label") or "").strip()
        if not label:
            return "Which label?"
        func = client.add_label if op == "add_label" else client.remove_label
        func(mid, label)
        return f'Applied {op.replace("_"," ").title()} “{label}”.'

    if op == "labels":
        rows = client.labels()
        if not rows:
            return "No user-defined labels found."
        lines = ["Labels:", *(f"  • {r['name']} ({r['id']})" for r in rows)]
        return "\n".join(lines)

    if op == "create_label":
        name = str(params.get("name") or "").strip()
        if not name:
            return "What should the new label be called?"
        row = client.create_label(name)
        return f'Created label “{row["name"]}” (id={row["id"]}).'

    # ── deletion ────────────────────────────────────────────────────────────
    if op == "trash":
        ids = normalise_ids(
            params.get("message_id") or params.get("message_ids") or "")
        count = len(ids)
        if count == 0:
            return "Which message(s)? Give me at least one id."
        if count > 1:
            preview = client.summaries(ids[:5])
            title = f"Move {count} message{'s' if count != 1 else ''} to Trash"
            detail = (f"This moves message(s) to Trash (recoverable for 30 days).\n"
                      f"{_preview_email(title, preview, count)}")
            def _run_trash() -> str:
                r = client.trash(ids)
                return f"Trashed {r['trashed']}/{count} messages."
            return _confirm("trash", title=title, detail=detail,
                            run=_run_trash, player=player)
        # single-item trash is a routine inbox-management action — execute directly
        r = client.trash(ids)
        _log(player, f"[Gmail] trash {ids[0][:10]}…")
        return f"Moved to Trash — Gmail keeps it for 30 days if you want it back."

    if op == "restore":
        ids = normalise_ids(
            params.get("message_id") or params.get("message_ids") or "")
        if not ids:
            return "Which message(s)? Give me at least one id."
        r = client.untrash(ids)
        return f"Restored {r['restored']}/{len(ids)} messages."

    if op == "delete_permanent":
        ids = normalise_ids(
            params.get("message_id") or params.get("message_ids") or "")
        count = len(ids)
        if count == 0:
            return "Which message(s)? Give me at least one id."
        preview = client.summaries(ids[:5])
        title = f"Permanently delete {count} message{'s' if count != 1 else ''}"
        detail = (f"This CANNOT be undone — the messages vanish immediately.\n"
                  f"{_preview_email(title, preview, count)}")
        def _run_delete() -> str:
            r = client.delete_permanent(ids)
            return f"Permanently deleted {r['deleted']}/{count} messages."
        return _confirm("permanent_delete", title=title, detail=detail,
                        run=_run_delete, player=player)

    if op == "empty_trash":
        count = len(client.ids(query="in:trash", max_results=1000))
        title = "Empty Trash"
        detail = (f"This permanently deletes {count} message(s) in Trash. "
                  f"It cannot be undone.")
        def _run_empty() -> str:
            r = client.empty_trash()
            return f"Emptied Trash — {r['deleted']} messages gone."
        return _confirm("empty_trash", title=title, detail=detail,
                        run=_run_empty, player=player)

    # ── attachments ─────────────────────────────────────────────────────────
    if op == "attachment":
        if not mid:
            return "Give me a message_id from a previous list or search."
        att_id = str(params.get("attachment_id") or "").strip()
        fname = str(params.get("filename") or "").strip()
        dest = str(params.get("dest_dir") or "~").strip()
        overwrite = bool(params.get("overwrite"))
        att = client.attachment(mid, att_id) if att_id else None
        if att:
            folder = Path(dest).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            safe = _safe_filename(fname or f"attachment-{att_id[:8]}")
            out = folder / safe
            if out.exists() and not overwrite:
                i = 2
                while (folder / f"{out.stem} ({i}){out.suffix}").exists():
                    i += 1
                out = folder / f"{out.stem} ({i}){out.suffix}"
            try:
                out.write_bytes(att)
                return f"Saved to {out}. Size: {len(att)//1024} KB."
            except OSError as e:
                return f"Could not save the attachment: {e}"
            return f"Saved attachment to {out} ({len(att)} bytes)."
        # Fall back: show available attachments from the message.
        msg = client.get(mid, full=False)
        atts = msg.get("attachments") or []
        if not atts:
            return "That message has no attachments."
        lines = [f"{len(atts)} attachment(s) in that message:"]
        for a in atts:
            lines.append(f"  [{a.get('attachment_id')}] “{a.get('filename')}” "
                         f"({int(a.get('size') or 0) // 1024} KB)")
        lines.append("Pass attachment_id (or filename) and dest_dir to download.")
        return "\n".join(lines)

    if op == "download_all_attachments":
        if not mid:
            return "Give me a message_id from a previous list or search."
        results = client.download_all_attachments(
            mid, Path(str(params.get("dest_dir") or "~")).expanduser())
        if not results:
            return "No attachments were downloaded."
        lines = ["Downloaded/failed:"]
        for r in results:
            if "path" in r:
                lines.append(f"  ✔ {r['path']}  ({r.get('size',0)} bytes)")
            else:
                lines.append(f"  ✘ {r.get('filename','?')}: {r.get('error','?')}")
        return "\n".join(lines)

    if op == "attachment_to_drive":
        # Convenience helper: download + upload in one shot.
        from orchestrator.agents.productivity.attachment_manager \
            import email_attachment_to_drive
        drive_client = _drive_client_or_skip()
        if drive_client is None:
            return (
                "Drive is not configured — run the 'config_drive' command to set "
                "up the Drive token."
            )
        try:
            r = email_attachment_to_drive(
                gmail=client, drive=drive_client, message_id=mid,
                attachment_id=str(params.get("attachment_id") or ""),
                filename=str(params.get("filename") or ""),
                folder_id=str(params.get("folder_id") or ""),
                folder=str(params.get("folder") or ""),
                create_folder=bool(params.get("create_folder")),
                dedupe=not bool(params.get("force_upload")),
                overwrite=bool(params.get("overwrite")),
            )
        except Exception as e:
            return f"Could not bridge attachment to Drive: {e}"
        return (_render_drive_result(r, mid)
                if "file" in r
                else f'{r.get("status")} — {_reason_from_result(r)}')

    if op == "briefing":
        return briefing_snapshot() or "Email: not connected right now."

    return (
        "I don't know that Gmail operation. Use list, search, read, thread, "
        "drafts, create_draft, update_draft, send_draft, delete_draft, send, "
        "reply, forward, mark_read, archive, trash, restore, delete_permanent, "
        "labels, create_label, add_label, remove_label, star, unstar, "
        "move_to_inbox, attachment, download_all_attachments, "
        "attachment_to_drive, or briefing."
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _check_send_params(to: str, body: str) -> str:
    """Validate send/reply/forward params; returns problem sentence or empty."""
    if "@" not in to:
        return (
            "I need the recipient's email address — a name alone is "
            "not enough for Gmail. Ask me for the address if unsure."
        )
    if not body.strip():
        return "I need some text to put in the email body."
    return ""


# ── backward-compat shims (tests reference these directly) ───────────────────

# Helpers the tests exercise by name — forward into gmail_client helpers.
def _decode_b64(data: str) -> str:
    import base64 as _b64
    if not data:
        return ""
    pad = "=" * (-len(data) % 4)
    try:
        return _b64.urlsafe_b64decode(data + pad).decode("utf-8", "replace")
    except Exception:
        return ""


def _strip_html(html: str) -> str:
    import re as _re
    t = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    t = _re.sub(r"(?s)<[^>]+>", " ", t)
    return _re.sub(r"[ \t\r\f\v]*\n[ \t]*", "\n", t).strip()


def _collect_body(payload: dict) -> str:
    from orchestrator.agents.productivity.gmail_client import (
        _collect_body as _inner,
    )
    plain, _html = _inner(payload or {})
    return plain


def _header(msg: dict, name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _short_from(value: str) -> str:
    return (value or "").strip()


def _fmt_date(msg: dict) -> str:
    from datetime import datetime
    try:
        ms = int(msg.get("internalDate") or 0)
        if ms:
            return (
                datetime.fromtimestamp(ms / 1000).astimezone()
                .strftime("%b %d %H:%M")
            )
    except (ValueError, OSError, OverflowError):
        pass
    return _header(msg, "Date")


def _send_raw(to: str, subject: str, body: str, cc: str) -> tuple[str, str]:
    """Return (raw_mime, problem_sentence)."""
    p = _check_send_params(to, body)
    if p:
        return "", p
    msg = EmailMessage()
    msg["To"] = to.strip()
    msg["Subject"] = (subject or "").strip() or "(no subject)"
    if cc and cc.strip():
        msg["Cc"] = cc.strip()
    msg.set_content(body)
    import base64 as _b64
    raw = _b64.urlsafe_b64encode(msg.as_bytes()).decode()
    return raw, ""


def _list_message_ids(svc, query: str, label: str, max_results: int) -> list[str]:
    from orchestrator.agents.productivity.gmail_client import (
    GmailClient, GmailError, build_raw_message, normalise_ids, LABELS)
    n = max(1, min(MAX_RESULTS_CAP, int(max_results)))
    params = {"userId": "me", "maxResults": n}
    label_ids = LABELS.get((label or "").strip().lower())
    if label_ids:
        params["labelIds"] = label_ids
    q = (query or "").strip()
    if q:
        params["q"] = q
    resp = svc.users().messages().list(**params).execute()
    return [m["id"] for m in (resp.get("messages") or []) if m.get("id")]


def _read_message(svc, message_id: str) -> str:
    from orchestrator.agents.productivity.gmail_client import (
        _collect_body as _ct,
    )
    msg = svc.users().messages().get(
        userId="me", id=message_id, format="full").execute()
    payload = msg.get("payload") or {}
    text, html = _ct(payload)
    body = text or msg.get("snippet") or "(empty body)"
    body = body[:BODY_CHARS]
    return (
        f"From: {_header(msg, 'From')}\n"
        f"To: {_header(msg, 'To')}\n"
        f"Date: {_fmt_date(msg)}\n"
        f"Subject: {_header(msg, 'Subject') or '(no subject)'}\n\n"
        f"{body}"
    )


def _render_list(ids, header: str, messages=None, client=None) -> str:
    """Compat: accepts old calling style (_render_list(summaries, header)) and
    the new dispatched form (_render_list(ids, header, messages=..., client=...))."""
    msgs: list[dict] = []
    # Old-style two-argument call: first arg is the summaries list itself.
    if isinstance(ids, list) and not isinstance(header, str):
        msgs = ids
        header = str(header or "")
    elif isinstance(ids, list) and messages is None:
        # New-style three-arg call: ids=[], header="...", messages=[...] was
        # NOT passed, which means the caller used the old summary-only path.
        msgs = ids
    elif messages is not None:
        msgs = list(messages)
    if not msgs and client:
        try:
            msgs = client.summaries(ids or [])
        except Exception:
            pass
    if not msgs:
        return f"{header}: no messages."
    lines = [f"{header}: {len(msgs)} message(s)."]
    for i, s in enumerate(msgs, 1):
        if "error" in s:
            lines.append(f"{i}. {s['id']} — could not fetch ({s['error']})")
            continue
        lines.append(
            f"{i}. id={s['id']} | {s.get('date','')} | From: "
            f"{s.get('from','')} | Subject: {s.get('subject','(none)')} | "
            f"{(s.get('snippet') or '').replace(chr(10),' ')[:140]}"
        )
    # Always hint at next operations when we produced a real list — callers on
    # both the legacy path (2-arg) and the dispatched path (passing messages
    # explicitly but maybe empty) get the trailing hint when msgs is non-empty.
    lines.append("Use read/mark_read/archive/trash with one of these ids.")
    return "\n".join(lines)


def _preview_email(title: str, rows, count: int) -> str:
    from orchestrator.agents.productivity.permissions import email_preview
    return email_preview(title.lower(), rows, total=count)


def _confirm(op_key: str, *, title: str, detail: str, run, player=None) -> str:
    from orchestrator.agents.productivity.confirmations import gate
    return gate(
        title=title, detail=detail, run=lambda: (run() or ""),
        player=player, key=f"gmail_{op_key}",
    )


def _render_drive_result(r: dict, mid: str = "") -> str:
    f = r.get("file") or {}
    msg_id = r.get("message_id") or r.get("message", "") or mid
    if r.get("status") == "skipped_duplicate":
        return (f'Attachment “{r.get("source_filename", "?")}” skipped — '
                f'a file with the same checksum is already in the target folder.')
    if r.get("status") == "uploaded":
        return (f'Uploaded attachment to Drive — file_id={f.get("id")}, '
                f'name="{f.get("name")}", size_human={f.get("size_human","")}')
    return f'Drive upload status={r.get("status")}.'


def _reason_from_result(r: dict) -> str:
    for k in ("error", "reason"):
        v = r.get(k)
        if v:
            return str(v)
    return "check the returned dict for details."


def _safe_filename(name: str) -> str:
    base = os.path.basename(str(name or "")).strip()
    import re
    base = re.sub(r"[^\w.\- ()\[\]]+", "_", base)
    return (base or "attachment")[:180]


def _drive_client_or_skip():
    """Build a DriveClient, or return None with a printed warning instead of
    raising — attachment_to_drive degrades gracefully."""
    try:
        drive_svc = ga.get_service("drive", "v3", [ga.DRIVE_SCOPE], "drive_token")
    except ga.SetupNeeded as e:
        print(f"[GmailDriveBridge] Drive not set up: {e}")
        return None
    from orchestrator.agents.productivity.drive_client import DriveClient
    return DriveClient(drive_svc)


def briefing_snapshot() -> str:
    """Unread-mail count for the morning briefing, or "" when Google is not
    connected. Consent-free on purpose (get_service_silent) — the briefing
    must never open a login tab. One labels.get call, never raises."""
    try:
        svc = ga.get_service_silent("gmail", "v1", [ga.GMAIL_SCOPE], "gmail_token")
        if svc is None:
            return ""
        label = svc.users().labels().get(userId="me", id="UNREAD").execute()
        n = int(label.get("messagesUnread") or 0)
        if n <= 0:
            return "Email: inbox is fully read."
        return f"Email: {n} unread message(s)."
    except Exception as e:
        print(f"[Gmail] briefing snapshot failed: {e}")
        return ""


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "gmail_control",
    "description": (
        "Manages the user's Gmail: list recent mail, search (Gmail syntax, e.g. "
        "'from:alice is:unread'), read a full message, create/send drafts, reply, "
        "forward, send a new email (gates with the confirmation banner first), "
        "mark read, archive, star/unstar, add/remove labels, move to/from Trash, "
        "download/save attachments, or bridge an attachment into Google Drive. "
        "For send/reply/forward, always asks the user to confirm on-screen before "
        "sending. For bulk trash or permanent delete, previews the affected items "
        "and requires explicit approval. The operation parameter accepts both "
        "'operation' (native tool shape) and 'command' (voice router alias)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {
                "type": "STRING",
                "description": (
                    "One of: list, search, read, thread, drafts, create_draft, "
                    "update_draft, delete_draft, send_draft, send, reply, "
                    "forward, mark_read, mark_unread, star, unstar, archive, "
                    "move_to_inbox, add_label, remove_label, trash, restore, "
                    "delete_permanent, empty_trash, labels, create_label, "
                    "attachment, download_all_attachments, attachment_to_drive, briefing"
                ),
            },
            "command": {
                "type": "STRING",
                "description": "Alias for operation — used by the voice router.",
            },
            "query": {
                "type": "STRING",
                "description": "Gmail search syntax for operation=search (e.g. 'from:boss@x.com has:attachment')",
            },
            "label": {
                "type": "STRING",
                "description": "For list: unread, inbox, starred, important, sent, drafts, trash, spam (default inbox)",
            },
            "max_results": {
                "type": "NUMBER",
                "description": "How many messages to list (default 8)",
            },
            "message_id": {
                "type": "STRING",
                "description": "Single message id from a list/search — required by read/mark_read/archive/trash",
            },
            "message_ids": {
                "type": "STRING",
                "description": "Comma-separated ids for bulk operations (trash/restore/star/etc.)",
            },
            "thread_id": {
                "type": "STRING",
                "description": "A Gmail thread id for thread/draft operations",
            },
            "draft_id": {
                "type": "STRING",
                "description": "A draft id from the drafts list — required by send_draft/update_draft/delete_draft",
            },
            "to": {
                "type": "STRING",
                "description": "Recipient email address (required for send/reply/forward/create_draft)",
            },
            "cc": {
                "type": "STRING",
                "description": "Optional CC address (send/reply/forward/create_draft)",
            },
            "subject": {
                "type": "STRING",
                "description": "Subject line (send/reply/forward/create_draft)",
            },
            "body": {
                "type": "STRING",
                "description": "Email body text, in the user's own language (send/reply/forward/create_draft)",
            },
            "html_body": {
                "type": "STRING",
                "description": "Optional HTML version of the body (send/reply/forward/create_draft)",
            },
            "attachments": {
                "type": "ARRAY",
                "items": {"type": "string"},
                "description": "Local file paths to attach (send/reply/forward/create_draft)",
            },
            "attachment_id": {
                "type": "STRING",
                "description": "Attachment id from a message — required by attachment/download for a specific file",
            },
            "filename": {
                "type": "STRING",
                "description": "Alternative way to pick an attachment by its filename",
            },
            "dest_dir": {
                "type": "STRING",
                "description": "Where to save a downloaded attachment (default: ~/uploads/attachments)",
            },
            "overwrite": {
                "type": "BOOLEAN",
                "description": "Overwrite existing local files (attachment/download_all_attachments)",
            },
            "folder_id": {
                "type": "STRING",
                "description": "Drive folder id for attachment_to_drive uploads",
            },
            "folder": {
                "type": "STRING",
                "description": "Human-readable Drive folder path (e.g. 'Research/2026/Papers') — creates parent folders if needed",
            },
            "create_folder": {
                "type": "BOOLEAN",
                "description": "Create intermediate folder hierarchy during attachment_to_drive",
            },
            "force_upload": {
                "type": "BOOLEAN",
                "description": "Upload even if a duplicate exists (attachment_to_drive)",
            },
            "include_spam_trash": {
                "type": "BOOLEAN",
                "description": "Include SPAM and TRASH in list/search results",
            },
        },
        "required": ["operation"],
    },
    "handler": gmail_control,
}
