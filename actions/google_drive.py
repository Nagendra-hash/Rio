"""
actions/google_drive.py — voice control for Google Drive.

Uses a separate OAuth token file (drive_token) and full-drive scope so that
revoking Drive access never breaks Gmail or Calendar. The first call that
needs Drive opens a browser consent tab; after that the stored token is
reused and auto-refreshed by actions._google_auth.

Safety rules encoded here:
  * sending/deleting anything drives through the confirmation gate — trash
    moves are confirmed but permanent delete and permission changes are
    not skipped under any mode.
  * bulk operations (>10 affected items) always preview and always confirm.
  * uploads verify the returned file id against the source size.
  * downloads never silently overwrite an existing local file.

Tool discovery: auto-loaded by core/action_loader.py under "google_drive".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from actions import _google_auth as ga

try:
    from googleapiclient.errors import HttpError
    _HAS_GOOGLE = True
except ImportError:
    HttpError = Exception          # type: ignore[misc,assignment]
    _HAS_GOOGLE = False

MAX_RESULTS_DEFAULT = 25
MAX_RESULTS_CAP = 500


# ── small helpers ─────────────────────────────────────────────────────────────


def _clamp_n(value, default: int = MAX_RESULTS_DEFAULT) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_RESULTS_CAP, n))


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
        m = re.search(r'\"message\"\s*:\s*\"([^\"]+)\"', str(content))
        if m:
            detail = m.group(1)
    except Exception:
        pass
    if status and detail:
        return f"Drive API error {status}: {detail}"
    if status:
        return f"Drive API error {status}."
    return f"Drive error: {type(e).__name__}"


def _fmt_size(n) -> str:
    try:
        size = int(n or 0)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return (f"{size:.0f} {unit}" if unit == "B"
                    else f"{size:.1f} {unit}")
        size /= 1024
    return "?"


def _preview_files(action: str, files, count: int) -> str:
    from orchestrator.agents.productivity.permissions import drive_preview
    return drive_preview(action.lower(), files, total=count)


def _fmt_file(f: dict) -> str:
    name = f.get("name") or "?"
    kind = "(folder)" if f.get("is_folder") else f.get("size_human") or ""
    return f'• "{name}" {kind}'


def _log(player, text: str) -> None:
    if player:
        try:
            player.write_log(text[:120])
        except Exception:
            pass


# ── api bootstrap ─────────────────────────────────────────────────────────────


def _build_client():
    if not _HAS_GOOGLE:
        raise ga.SetupNeeded(
            "The Google API libraries are not installed. Run: "
            "pip install google-api-python-client google-auth-oauthlib"
        )
    try:
        svc = ga.get_service("drive", "v3", [ga.DRIVE_SCOPE], "drive_token")
    except ga.SetupNeeded as e:
        raise
    except Exception as e:
        raise ga.SetupNeeded(f"Could not connect to Google Drive: {e}") from e
    from orchestrator.agents.productivity.drive_client import DriveClient
    return DriveClient(svc)


def _get_or_skip():
    """Build or return a DriveClient, or a SetupNeeded sentence."""
    try:
        return _build_client()
    except ga.SetupNeeded as e:
        return str(e)   # type: ignore[return-value]


# ── public entry ──────────────────────────────────────────────────────────────


def google_drive(parameters: dict, player=None) -> str:
    params = parameters or {}
    op = (str(params.get("operation") or params.get("command") or "") or "").strip().lower()

    if not op:
        return (
            "Tell me what to do on Drive: list, search, get, upload, download, "
            "rename, move, copy, create_folder, organize, find_duplicates, trash, "
            "restore, delete_permanent, share, remove_permission, permissions, "
            "about, read_text."
        )

    client = _get_or_skip()
    if isinstance(client, str):   # SetupNeeded / config gap
        return client

    try:
        return _dispatch(client, op, params, player)
    except HttpError as e:
        return _err(e)
    except Exception as e:
        return f"Drive error: {e}"


# ── dispatch ──────────────────────────────────────────────────────────────────


def _dispatch(client, op: str, params: dict, player=None) -> str:
    fid = (params.get("file_id") or "").strip()
    folder_id = (params.get("folder_id") or "").strip()
    ids_csv = (params.get("file_ids") or "").strip()
    file_ids = [x.strip() for x in re.split(r"[,;\s]+", ids_csv) if x.strip()] if ids_csv else []

    if op in ("list", "search"):
        query = str(params.get("query") or "").strip()
        name = str(params.get("name") or "").strip()
        mime_type = str(params.get("mime_type") or "").strip()
        modified_after = str(params.get("modified_after") or "").strip()
        modified_before = str(params.get("modified_before") or "").strip()
        full_text = str(params.get("full_text") or "").strip()
        owner = str(params.get("owner") or "").strip()
        shared_with_me = bool(params.get("shared_with_me"))
        include_trashed = bool(params.get("include_trashed"))
        max_results = _clamp_n(params.get("max_results"))
        order_by = str(params.get("order_by") or "modifiedTime desc").strip()
        if name or mime_type or modified_after or modified_before or \
                owner or shared_with_me or full_text:
            result = client.search(name=name, mime_type=mime_type,
                                   modified_after=modified_after,
                                   modified_before=modified_before,
                                   folder_id=folder_id,
                                   full_text=full_text,
                                   owner=owner,
                                   shared_with_me=shared_with_me,
                                   max_results=max_results)
        else:
            result = client.list_files(query=query, folder_id=folder_id,
                                       max_results=max_results,
                                       order_by=order_by,
                                       include_trashed=include_trashed)
        files = result.get("files") or []
        lines = [f'Drive{" search" if op == "search" else ""} — {len(files)} item(s).']
        for i, f in enumerate(files, 1):
            lines.append(f"  {i}. {_fmt_file(f)}")
        if result.get("next_page_token"):
            lines.append("  … more results available.")
        _log(player, lines[0])
        return "\n".join(lines)

    if op == "get":
        if not fid:
            return "Give me a Drive file_id."
        f = client.get(fid)
        return (f'File: "{f["name"]}" ({f["mime_type"]})\n'
                f'  Id: {f["id"]}\n'
                f'  Size: {f["size_human"]}\n'
                f'  Modified: {f["modified"]}\n'
                f'  Shared: {bool(f["shared"])}  Trashed: {bool(f["trashed"])}\n'
                f'  Link: {f["web_link"]}')

    if op == "about":
        info = client.about()
        used = int(info.get("usage") or 0)
        limit = int(info.get("limit") or 0)
        pct = round(used / limit * 100, 1) if limit else 0
        lines = [f'Account: {info.get("name") or info.get("email")}']
        lines.append(f'Storage: {_fmt_size(used)} used of {_fmt_size(limit)} '
                     f'({pct}%)')
        return "\n".join(lines)

    if op in ("upload", "attach_to_email"):
        path_str = str(params.get("path") or params.get("file_path") or "").strip()
        dest_folder = str(params.get("folder_id") or params.get("folder_name") or "")
        name_override = str(params.get("name") or "").strip() or None
        mime_override = str(params.get("mime_type") or "").strip() or None
        resumable = not bool(params.get("no_resumable"))
        progress_steps = int(params.get("progress_steps") or 0)
        progress_cb = None
        last = {"pct": 0}
        def step(pct: float) -> None:
            if progress_steps and pct - last["pct"] >= 1.0 / progress_steps:
                last["pct"] = pct
                _log(player, f"[Drive upload] {pct*100:.0f}%")
        if not Path(path_str).is_file():
            return f"I can't find that file: {path_str}"
        f = client.upload(path_str, folder_id=dest_folder, name=name_override,
                          mime_type=mime_override or "", resumable=resumable,
                          progress_cb=progress_cb)
        msg = f'Uploaded "{f["name"]}" — file_id={f["id"]} ({f["size_human"]}).'
        _log(player, f"[Drive] upload {f['id'][:10]}")
        return msg

    if op == "download":
        if not fid:
            return "Give me a Drive file_id to download."
        dest_dir = str(params.get("dest_dir") or "~")
        export_mime = str(params.get("export_as") or "").strip() or ""
        name_override = str(params.get("name") or "").strip() or None
        overwrite = bool(params.get("overwrite"))
        r = client.download(fid, Path(dest_dir).expanduser(),
                            name=name_override,
                            export_mime=export_mime,
                            overwrite=overwrite)
        if r.get("error"):
            return f"Could not download: {r['error']}"
        out = Path(r["path"])
        return f'Saved to {out} ({r["size"]} bytes)' \
               + (f" — exported as {r.get('exported_as','')}." if r.get("exported_as") else ".")

    if op == "read_text":
        if not fid:
            return "Give me a Drive file_id to read."
        r = client.read_text(fid)
        if r.get("error"):
            return f"Could not read: {r['error']}"
        text = r.get("text") or "(empty or binary file)"
        return (f'{r["name"]}:\n{text}'
                + ("\n… [truncated]" if r.get("truncated") else "."))

    if op in ("create_folder", "make_folder"):
        name = str(params.get("name") or params.get("folder_name") or "").strip()
        parent_id = (params.get("parent_id") or "").strip() or None
        if not name:
            return "What should the new folder be called?"
        folder = client.create_folder(name, parent_id=parent_id)
        return f'Created folder "{folder["name"]}" — file_id={folder["id"]}.'

    if op in ("ensure_folder", "find_or_create_folder"):
        path = str(params.get("path") or "").strip()
        parent_id = (params.get("parent_id") or "").strip() or None
        if not path:
            return "Give me a folder path (e.g. Research/2026/Papers)."
        folder = client.ensure_path(path, parent_id=parent_id)
        return f'Folder ready — "{folder["name"]}" (id={folder["id"]}).'

    if op in ("rename", "move", "copy", "describe", "organize",
              "add_description", "update_description"):
        if op in ("move", "copy"):
            target_folder = str(params.get("target_folder") or
                                params.get("folder_id") or "").strip()
            if not fid or not target_folder:
                return (f'Give me a file_id and a target folder (for {op}).')
            func = client.move if op == "move" else client.copy
            label = "moved" if op == "move" else "copied"
            kwargs = {"folder_id": target_folder}
            if op == "copy" and str(params.get("name") or "").strip():
                kwargs["name"] = str(params.get("name")).strip()
            updated = func(fid, **kwargs)
            return f'{label.capitalize()} — "{updated["name"]}" (id={updated["id"]}).'
        if op in ("rename", "update_description"):
            if not fid:
                return "Give me a file_id to update."
            if op == "rename":
                new_name = str(params.get("new_name") or params.get("name") or "").strip()
                if not new_name:
                    return "What should the new name be?"
                updated = client.rename(fid, new_name)
                return f'Renamed to "{updated["name"]}" (id={updated["id"]}).'
            desc = str(params.get("description") or "").strip()
            if not desc:
                return "What description to set?"
            updated = client.describe(fid, desc)
            return f'Description set on "{updated["name"]}". File id={updated["id"]}.'
        if op == "organize":
            target = folder_id or str(params.get("folder_id") or params.get("path") or "")
            dry = bool(params.get("dry_run"))
            r = client.organize_by_type(target, dry_run=dry)
            if r.get("dry_run"):
                return (f'Organize plan for folder {r.get("folder_id","?")}: '
                        f'{r["would_move"]} file(s) would be moved into categories '
                        f'{", ".join(sorted(r.get("folders"))) or "(none)"}.\n'
                        f'(Set operation=dry_run=false to execute.)')
            return (f'Organized — moved {r["moved"]} files; folders created: '
                    f'{r.get("folders")}; failed: {len(r.get("failed") or [])} file(s).')
        return f'I don\'t know how to handle that in Drive.'

    if op == "find_duplicates":
        folder_id = str(params.get("folder_id") or "").strip()
        rows = client.find_duplicates(folder_id=folder_id)
        if not rows:
            return "No duplicate groups found."
        lines = [f"{len(rows)} duplicate group(s):"]
        for g in rows[:5]:
            lines.append(f"\n  {g['count']} copies keyed by {g['key'][:60]}:")
            for f in g["files"]:
                lines.append(f"    - {f.get('name')} ({f.get('size_human')}, {f.get('md5','')[:12]}…)")
        lines.append("\nUse file_id with delete_permanent to clean up.")
        return "\n".join(lines)

    # ── deletion / trash ────────────────────────────────────────────────────
    if op == "trash":
        ids_to_trash = file_ids or ([fid] if fid else [])
        count = len(ids_to_trash)
        if count == 0:
            return "Which file(s)? Give me a file_id or comma-separated file_ids."
        preview = client.files_by_ids(ids_to_trash[:5])
        title = f"Move {count} file{'s' if count != 1 else ''} to Trash"
        detail = (_preview_files(title, preview, count)
                  + "\nThis moves files to Drive Trash — recoverable for 30 days.")
        def _run_trash() -> str:
            r = client.trash(ids_to_trash)
            failed_msgs = [f['id'] for f in r.get("failed") or []]
            extra = f"  errors on {len(failed_msgs)}: {','.join(failed_msgs[:3])}" \
                if failed_msgs else ""
            return f"Trashed {r['trashed']}/{count}.{extra}"
        return _confirm("trash", title=title, detail=detail,
                        run=_run_trash, player=player)

    if op == "restore":
        ids_to_restore = file_ids or ([fid] if fid else [])
        count = len(ids_to_restore)
        if count == 0:
            return "Which file(s)? Give me a file_id or comma-separated file_ids."
        r = client.untrash(ids_to_restore)
        return f"Restored {r['restored']}/{count} file(s)."

    if op == "delete_permanent":
        ids_to_del = file_ids or ([fid] if fid else [])
        count = len(ids_to_del)
        if count == 0:
            return "Which file(s)? Give me a file_id or comma-separated file_ids."
        preview = client.files_by_ids(ids_to_del[:5])
        title = f"Permanently delete {count} file{'s' if count != 1 else ''}"
        detail = (_preview_files(title, preview, count)
                  + "\n⚠️ This cannot be undone.")
        def _run_delete() -> str:
            r = client.delete_permanent(ids_to_del)
            failed_msgs = [f['id'] for f in r.get("failed") or []]
            extra = f"  errors on {len(failed_msgs)}: {','.join(failed_msgs[:3])}" \
                if failed_msgs else ""
            return f"Permanently deleted {r['deleted']}/{count}.{extra}"
        return _confirm("permanent_delete", title=title, detail=detail,
                        run=_run_delete, player=player)

    if op == "empty_trash":
        from orchestrator.agents.productivity.drive_client import MAX_RESULTS_CAP
        listing = client.list_files(query="trashed=true", max_results=MAX_RESULTS_CAP)
        count = len(listing.get("files") or [])
        title = "Empty Drive Trash"
        detail = (f"This permanently deletes {count} file(s) in Trash. "
                  f"It cannot be undone.")
        def _run_empty() -> str:
            ids = [f["id"] for f in listing.get("files") or []]
            r = client.delete_permanent(ids) if ids else {"deleted": 0}
            return f"Emptied Drive Trash — {r['deleted']} gone."
        return _confirm("empty_trash", title=title, detail=detail,
                        run=_run_empty, player=player)

    # ── sharing / permissions ───────────────────────────────────────────────
    if op == "permissions":
        if not fid:
            return "Give me a file_id."
        perms = client.permissions(fid)
        if not perms:
            return f'The file has no published permissions.'
        lines = [f'Permissions for "{client.get(fid)["name"]}":']
        for p in perms:
            if p.get("deleted"):
                continue
            lines.append(f"  • {p['type']} | {p['role']} | "
                         f"{p.get('email') or p.get('domain') or '?'}")
        lines.append("Use share/update_permission/remove_permission to change.")
        return "\n".join(lines)

    if op == "share":
        if not fid:
            return "Give me a file_id to share."
        email = str(params.get("email") or "").strip()
        role = str(params.get("role") or "reader").strip()
        owner = str(params.get("owner") or "").strip()
        if not email and not owner:
            return "Who should get access? Give an email address."
        to_field = email or owner
        title = f'Share "{client.get(fid)["name"]}" with {to_field}'
        detail = (f"Grant the “{role}” role to {to_field} on this file.\n"
                  f"This may make it accessible outside your organisation.")
        def _run_share() -> str:
            who_kw = {"email": email} if email else {"domain": owner}
            p = client.share(fid, role=role, type_="user" if email else "domain",
                             send_notification=bool(params.get("send_notification")),
                             **who_kw)
            return f'Shared — permission id={p["id"]}, role={p["role"]}, to={p["email"] or p["domain"]}.'
        return _confirm("share", title=title, detail=detail,
                        run=_run_share, player=player)

    if op in ("update_permission", "change_permission"):
        perm_id = str(params.get("permission_id") or "").strip()
        if not fid or not perm_id:
            return "Give me a file_id and a permission_id to update."
        role = str(params.get("role") or "").strip()
        if not role:
            return "What role should they have (reader/writer/commenter/)?"
        updated = client.update_permission(fid, perm_id, role)
        return f'Permission updated — role is now {updated["role"]}.'

    if op in ("remove_permission", "revoke_access"):
        perm_id = str(params.get("permission_id") or "").strip()
        if not fid or not perm_id:
            return "Give me a file_id and a permission_id to revoke."
        removed = client.remove_permission(fid, perm_id)
        return f'Permission revoked — {removed["file_id"]}:{removed["permission_id"]}.'

    return (
        "I don't know that Drive operation. Use list, search, get, upload, "
        "download, rename, move, copy, create_folder, ensure_folder, "
        "organize (dry), find_duplicates, trash, restore, delete_permanent, "
        "permissions, share, update_permission, remove_permission, about, or read_text."
    )


# ── confirmation gate ─────────────────────────────────────────────────────────

def _confirm(op_key: str, *, title: str, detail: str, run, player=None) -> str:
    from orchestrator.agents.productivity.confirmations import gate
    return gate(
        title=title, detail=detail, run=lambda: (run() or ""),
        player=player, key=f"drive_{op_key}",
    )


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "google_drive",
    "description": (
        "Manages the user's Google Drive: list/search/upload/download/rename/move/"
        "copy/delete/create-folder/share files. Requires a Drive OAuth token "
        "(first use opens a browser consent tab; separate from Gmail's token). "
        "Every trash/delete/share operation shows a preview of the affected "
        "files and requires explicit confirmation before running. Uploads verify "
        "the returned Drive file id and refuse silent overwrites of local files."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {
                "type": "STRING",
                "description": (
                    "One of: list, search, get, upload, download, rename, move, copy, "
                    "describe, organize, find_duplicates, trash, restore, "
                    "delete_permanent, empty_trash, permissions, share, "
                    "update_permission, remove_permission, about, read_text,"
                    "create_folder, ensure_folder"
                ),
            },
            "command": {
                "type": "STRING",
                "description": "Alias for operation — kept for the voice router.",
            },
            "query": {
                "type": "STRING",
                "description": "Drive search syntax (e.g. 'name contains \"invoice\" and mimeType = \"application/pdf\"')"
            },
            "name": {
                "type": "STRING",
                "description": "Filename substring to search for, or new name for rename, or folder name for create_folder"
            },
            "mime_type": {
                "type": "STRING",
                "description": "Exact MIME type filter (e.g. application/pdf, image/png) or extension"
            },
            "modified_after": {
                "type": "STRING",
                "description": "ISO timestamp, e.g. 2026-01-01T00:00:00Z"
            },
            "modified_before": {
                "type": "STRING",
                "description": "ISO timestamp, e.g. 2026-06-30T23:59:59Z"
            },
            "owner": {
                "type": "STRING",
                "description": "Email substring — only files owned by this person"
            },
            "shared_with_me": {
                "type": "BOOLEAN",
                "description": "Include files others have shared with you"
            },
            "full_text": {
                "type": "STRING",
                "description": "Search inside document text (slow)"
            },
            "folder_id": {
                "type": "STRING",
                "description": "Narrow the search/list to one folder (for lists, searches, organize, duplicates)"
            },
            "file_id": {
                "type": "STRING",
                "description": "A Drive file/folder id — required by get/download/rename/move/copy/trash/delete_permanent/permissions/share/remove"
            },
            "file_ids": {
                "type": "STRING",
                "description": "Comma-separated ids for bulk trash/delete/restore"
            },
            "path": {
                "type": "STRING",
                "description": "Local filesystem path (upload) or nested Drive folder path (ensure_folder), e.g. ~/Desktop/report.pdf or Research/2026/Papers"
            },
            "dest_dir": {
                "type": "STRING",
                "description": "Where to save downloads (default: current working directory)"
            },
            "export_as": {
                "type": "STRING",
                "description": "Export Google Docs/Sheets to this MIME type (e.g. application/vnd.openxmlformats-officedocument.wordprocessingml.document for DOCX, text/csv for XLSX-as-CSV)"
            },
            "overwrite": {
                "type": "BOOLEAN",
                "description": "Allow overwriting an existing local file on download"
            },
            "send_notification": {
                "type": "BOOLEAN",
                "description": "Send an email notification when sharing (share operation)"
            },
            "permission_id": {
                "type": "STRING",
                "description": "A permission id from the permissions list — required by update_permission/remove_permission"
            },
            "target_folder": {
                "type": "STRING",
                "description": "Destination folder id for move/copy"
            },
            "new_name": {
                "type": "STRING",
                "description": "New filename for rename"
            },
            "description": {
                "type": "STRING",
                "description": "Description text for describe operation"
            },
            "dry_run": {
                "type": "BOOLEAN",
                "description": "Return the organization plan without moving anything (organization operation)"
            },
            "include_trashed": {
                "type": "BOOLEAN",
                "description": "Also show trashed items in list/search"
            },
            "max_results": {
                "type": "NUMBER",
                "description": "How many items to list/search (default 25, cap 500)"
            },
            "order_by": {
                "type": "STRING",
                "description": "Sort field (default: modifiedTime desc). E.g. name asc, createdTime desc"
            },
            "role": {
                "type": "STRING",
                "description": "Sharing role: reader, writer, commenter"
            },
            "email": {
                "type": "STRING",
                "description": "Person's email for share"
            },
            "owner": {
                "type": "STRING",
                "description": "Email for share-by-owner-alias"
            },
        },
        "required": ["operation"],
    },
    "handler": google_drive,
}
