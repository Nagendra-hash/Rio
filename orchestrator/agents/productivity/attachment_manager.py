"""
orchestrator/agents/productivity/attachment_manager.py — the Gmail ↔ Drive bridge.

This is the one place that moves bytes between the two services. Everything
else (the live tool, the routed agent) calls these functions so that:

  * attachment bytes are staged on disk, validated, then uploaded;
  * duplicate uploads are skipped by filename + md5, not blindly repeated;
  * a Drive document can be turned back into a local file and attached to a
    draft or a reply;
  * failures are reported per file (partial success is real);
  * nothing here trusts an attachment: filenames are sanitised, sizes are
    checked, and nothing is ever executed.

Staging directory (JARVIS_HOME aware, never the system temp):
    $JARVIS_ATTACHMENT_DIR  or  <base>/uploads/attachments

Both clients are injected. `gmail` is a GmailClient, `drive` a DriveClient —
tests pass fakes, production builds them from actions/_google_auth.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Iterable, Optional

from orchestrator.agents.productivity.drive_client import DriveClient, DriveError
from orchestrator.agents.productivity.gmail_client import GmailClient, GmailError

MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024      # refuse absurd single files
_SAFE = re.compile(r"[^\w.\- ()\[\]]+")


# ── staging ──────────────────────────────────────────────────────────────────

def staging_dir(subdir: str = "") -> Path:
    """Writable local folder for staged attachment bytes."""
    env = os.environ.get("JARVIS_ATTACHMENT_DIR", "").strip()
    if env:
        base = Path(env).expanduser()
    else:
        from actions import _google_auth as ga
        base = ga.base_dir() / "uploads" / "attachments"
    path = base / subdir if subdir else base
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_filename(name: str, fallback: str = "attachment") -> str:
    base = os.path.basename(str(name or "")).strip() or fallback
    base = _SAFE.sub("_", base).strip(" .")
    return (base or fallback)[:180]


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def pick_attachment(message: dict, attachment_id: str = "",
                    filename: str = "") -> dict:
    """Choose one attachment from a message dict (from GmailClient.get)."""
    atts = [a for a in (message.get("attachments") or []) if a.get("attachment_id")]
    if not atts:
        raise GmailError("That email has no attachments.")
    if attachment_id:
        for a in atts:
            if a["attachment_id"] == attachment_id:
                return a
        raise GmailError(f"No attachment with id {attachment_id} in that email.")
    if filename:
        wanted = filename.lower()
        for a in atts:
            if a.get("filename", "").lower() == wanted:
                return a
        for a in atts:
            if wanted in a.get("filename", "").lower():
                return a
        raise GmailError(f"No attachment named “{filename}” in that email.")
    return atts[0]


def _drive_duplicate(drive: DriveClient, folder_id: str, filename: str,
                     md5: str) -> Optional[dict]:
    """An existing Drive file in `folder_id` with the same name and md5."""
    if not folder_id:
        return None
    try:
        listing = drive.list_files(
            query=f"name = '{filename.replace(chr(39), chr(92) + chr(39))}'",
            folder_id=folder_id, max_results=5)
    except DriveError:
        return None
    for f in listing.get("files") or []:
        if f.get("is_folder"):
            continue
        same_name = f.get("name", "").lower() == filename.lower()
        if same_name and (not md5 or not f.get("md5") or f["md5"] == md5):
            return f
    return None


# ── Gmail → Drive ────────────────────────────────────────────────────────────

def email_attachment_to_drive(
    gmail: GmailClient,
    drive: DriveClient,
    message_id: str,
    *,
    attachment_id: str = "",
    filename: str = "",
    folder_id: str = "",
    folder: str = "",
    create_folder: bool = False,
    dedupe: bool = True,
    overwrite: bool = False,
) -> dict:
    """Download one Gmail attachment and upload it to Drive.

    Returns a dict describing exactly what happened; raises GmailError or
    DriveError only for a hard failure (partial failure is reported, not
    raised). `folder` is a friendly path ("Research/2026") that is created
    when `create_folder` is true.
    """
    message = gmail.get(message_id, full=True)
    att = pick_attachment(message, attachment_id=attachment_id,
                          filename=filename)

    staged = gmail.download_attachment(
        message_id, att["attachment_id"], staging_dir("gmail"),
        filename=att.get("filename", ""), overwrite=True)
    path = Path(staged["path"])
    size = path.stat().st_size
    if size <= 0:
        raise GmailError(f"The attachment “{path.name}” came back empty.")
    if size > MAX_ATTACHMENT_BYTES:
        raise GmailError(
            f"“{path.name}” is {size // (1024 * 1024)} MB — too large to "
            f"upload blindly. Tell me the Drive folder and I'll stream it.")

    target_folder_id = folder_id
    if folder and not target_folder_id:
        target_folder_id = drive.ensure_path(folder)["id"]

    md5 = _md5_file(path)
    if dedupe and not overwrite:
        existing = _drive_duplicate(drive, target_folder_id, path.name, md5)
        if existing:
            return {
                "status": "skipped_duplicate",
                "message_id": message_id,
                "source_filename": att.get("filename", ""),
                "local_path": str(path),
                "file": existing,
                "reason": "A file with the same name and checksum is already "
                          "in that Drive folder.",
            }

    uploaded = drive.upload(str(path), folder_id=target_folder_id,
                            name=path.name)
    return {
        "status": "uploaded",
        "message_id": message_id,
        "source_filename": att.get("filename", ""),
        "local_path": str(path),
        "size": size,
        "md5": md5,
        "file": uploaded,
        "folder_id": target_folder_id,
    }


def email_attachments_to_drive(
    gmail: GmailClient,
    drive: DriveClient,
    message_id: str,
    *,
    folder_id: str = "",
    folder: str = "",
) -> dict:
    """All attachments of one email → Drive. Per-file results, no raise."""
    try:
        message = gmail.get(message_id, full=True)
    except GmailError as e:
        return {"message_id": message_id, "uploaded": [], "failed": [],
                "skipped": [], "error": str(e)}
    results = {"message_id": message_id, "uploaded": [], "failed": [],
               "skipped": [], "count": 0}
    for att in message.get("attachments") or []:
        try:
            out = email_attachment_to_drive(
                gmail, drive, message_id,
                attachment_id=att.get("attachment_id", ""),
                folder_id=folder_id, folder=folder)
        except (GmailError, DriveError) as e:
            results["failed"].append({"filename": att.get("filename", ""),
                                      "error": str(e)})
            continue
        if out["status"] == "uploaded":
            results["uploaded"].append(out)
        else:
            results["skipped"].append(out)
    results["count"] = len(results["uploaded"])
    return results


# ── Drive → Gmail ────────────────────────────────────────────────────────────

def drive_file_to_local(
    drive: DriveClient,
    file_id: str,
    *,
    dest_dir: str | os.PathLike | None = None,
    export_mime: str = "",
    filename: str = "",
    overwrite: bool = False,
) -> dict:
    """Materialise a Drive file locally so it can be attached to an email."""
    folder = Path(dest_dir) if dest_dir else staging_dir("drive")
    return drive.download(file_id, folder, name=filename or "",
                          export_mime=export_mime, overwrite=overwrite)


def attach_drive_files_to_draft(
    gmail: GmailClient,
    drive: DriveClient,
    *,
    file_ids: Iterable[str],
    to: str,
    subject: str = "",
    body: str = "",
    cc: str = "",
    bcc: str = "",
) -> dict:
    """Download Drive files and create a Gmail draft with them attached.

    Draft only — sending is a separate, explicitly-confirmed action.
    """
    paths, missing = [], []
    folder = staging_dir("drive")
    for fid in file_ids:
        try:
            got = drive.download(fid, folder)
            paths.append(got["path"])
        except DriveError as e:
            missing.append({"file_id": str(fid), "error": str(e)})
    if not paths:
        raise DriveError("None of those Drive files could be downloaded, so "
                         "there is nothing to attach.")
    draft = gmail.create_draft(to=to, subject=subject or "Files attached",
                              body=body or "Attached from Google Drive.",
                              cc=cc, bcc=bcc, attachment_paths=paths)
    draft["attachments"] = paths
    draft["missing"] = missing
    return draft


def send_drive_files_to(
    gmail: GmailClient,
    drive: DriveClient,
    *,
    file_ids: Iterable[str],
    to: str,
    subject: str = "",
    body: str = "",
    cc: str = "",
    bcc: str = "",
    html_body: str = "",
) -> dict:
    """Like attach_drive_files_to_draft but SENDS.

    Callers must confirm with the user first — this function does not gate.
    """
    paths, missing = [], []
    folder = staging_dir("drive")
    for fid in file_ids:
        try:
            paths.append(drive.download(fid, folder)["path"])
        except DriveError as e:
            missing.append({"file_id": str(fid), "error": str(e)})
    if not paths:
        raise DriveError("None of those Drive files could be downloaded, so "
                         "there is nothing to send.")
    sent = gmail.send(to=to, subject=subject or "Files from Google Drive",
                      body=body or "Attached from Google Drive.",
                      cc=cc, bcc=bcc, html_body=html_body,
                      attachment_paths=paths)
    sent["attachments"] = paths
    sent["missing"] = missing
    return sent


__all__ = [
    "staging_dir", "safe_filename", "pick_attachment",
    "email_attachment_to_drive", "email_attachments_to_drive",
    "drive_file_to_local", "attach_drive_files_to_draft",
    "send_drive_files_to", "MAX_ATTACHMENT_BYTES",
]
