"""
orchestrator/agents/productivity/drive_client.py — Google Drive operations.

The Drive twin of gmail_client: one typed wrapper over the Drive v3 API,
shared by the live tool (actions/google_drive.py) and the routed agent
(email_agent, which owns both email and Drive).

Design rules (same as gmail_client)
-----------------------------------
  * the googleapiclient service is INJECTED; tests pass a fake;
  * every method returns plain dicts/lists;
  * uploads verify the returned file id and re-read the metadata;
  * downloads never overwrite an existing local file unless told to;
  * permanent deletion and permission changes are separate, explicitly
    authorized operations — the caller gates them.

Scopes: https://www.googleapis.com/auth/drive (full management of files the
user can see, including Shared Drives and files shared with them). The token
lives in its own file (drive_token) so revoking Drive never breaks Gmail.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

FOLDER_MIME = "application/vnd.google-apps.folder"

GOOGLE_DOCS = "application/vnd.google-apps.document"
GOOGLE_SHEETS = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
GOOGLE_DRAWING = "application/vnd.google-apps.drawing"

# Export a native Google file as one of these (default first).
EXPORT_TYPES: dict[str, tuple[str, ...]] = {
    GOOGLE_DOCS: ("application/vnd.openxmlformats-officedocument."
                  "wordprocessingml.document", "application/pdf",
                  "text/plain"),
    GOOGLE_SHEETS: ("application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet", "text/csv", "application/pdf"),
    GOOGLE_SLIDES: ("application/vnd.openxmlformats-officedocument."
                    "presentationml.presentation", "application/pdf"),
    GOOGLE_DRAWING: ("application/pdf", "image/svg+xml", "image/png"),
}

EXPORT_EXT = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "image/svg+xml": ".svg",
    "image/png": ".png",
    "application/zip": ".zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml."
    "document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml."
    "presentation": ".pptx",
}

FILE_FIELDS = ("id,name,mimeType,size,modifiedTime,createdTime,parents,"
               "webViewLink,iconLink,md5Checksum,shared,trashed,description,"
               "owners(emailAddress,displayName),shortcutDetails")

MAX_RESULTS_CAP = 1000
DEFAULT_MAX_RESULTS = 25
RESUMABLE_THRESHOLD = 8 * 1024 * 1024       # 8 MiB
UPLOAD_CHUNK = 4 * 1024 * 1024

# Category folders used by organize_by_type.
CATEGORY_FOLDERS = {
    "documents": ("application/pdf", "application/msword", "text/plain",
                  "application/rtf", GOOGLE_DOCS, "application/vnd.openxml"
                  "formats-officedocument.wordprocessingml.document"),
    "spreadsheets": ("text/csv", GOOGLE_SHEETS, "application/vnd.ms-excel",
                     "application/vnd.openxmlformats-officedocument."
                     "spreadsheetml.sheet"),
    "presentations": (GOOGLE_SLIDES, "application/vnd.ms-powerpoint",
                      "application/vnd.openxmlformats-officedocument."
                      "presentationml.presentation"),
    "images": ("image/",),
    "audio": ("audio/",),
    "video": ("video/",),
    "archives": ("application/zip", "application/x-tar",
                 "application/gzip", "application/x-7z-compressed"),
}


class DriveError(Exception):
    """A Drive operation failed with a message safe to show the user."""


# ── helpers ──────────────────────────────────────────────────────────────────

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
        m = re.search(r'"message"\s*:\s*"([^"]+)"', str(content))
        if m:
            detail = m.group(1)
    except Exception:
        pass
    if status and detail:
        return f"Drive API error {status}: {detail}"
    if status:
        return f"Drive API error {status}."
    return f"Drive error: {type(e).__name__}"


def escape_query(value: str) -> str:
    """Escape a literal for a Drive `q` string (backslash, then quote)."""
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def _safe_name(name: str) -> str:
    base = os.path.basename(str(name or "")).strip() or "untitled"
    base = re.sub(r"[^\w.\- ()\[\]]+", "_", base)
    return base[:200] or "untitled"


def _guess_mime(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _md5(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def is_google_native(mime_type: str) -> bool:
    return str(mime_type or "").startswith("application/vnd.google-apps.") \
        and mime_type != FOLDER_MIME


def _category_for(mime_type: str) -> str:
    mime = str(mime_type or "")
    for category, prefixes in CATEGORY_FOLDERS.items():
        for prefix in prefixes:
            if mime == prefix or (prefix.endswith("/") and mime.startswith(prefix)):
                return category
    return "other"


def _fmt_size(n) -> str:
    try:
        size = int(n or 0)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ── the client ───────────────────────────────────────────────────────────────

class DriveClient:
    """Every Drive operation RIO needs, in one injectable object."""

    def __init__(self, service) -> None:
        self.svc = service

    # ── listing / search ─────────────────────────────────────────────────
    def list_files(self, query: str = "", folder_id: str = "",
                   max_results: int = DEFAULT_MAX_RESULTS,
                   order_by: str = "modifiedTime desc",
                   include_trashed: bool = False,
                   page_token: str = "") -> dict:
        n = max(1, min(MAX_RESULTS_CAP, int(max_results or DEFAULT_MAX_RESULTS)))
        clauses = []
        if str(query or "").strip():
            clauses.append(f"({query})")
        if folder_id:
            clauses.append(f"'{escape_query(folder_id)}' in parents")
        if not include_trashed:
            clauses.append("trashed = false")
        q = " and ".join(clauses)
        params: dict[str, Any] = {
            "pageSize": n, "fields": f"nextPageToken,files({FILE_FIELDS})",
            "orderBy": order_by or "modifiedTime desc",
        }
        if q:
            params["q"] = q
        if page_token:
            params["pageToken"] = page_token
        try:
            resp = self.svc.files().list(**params).execute()
        except Exception as e:
            raise DriveError(_err(e))
        files = [self._normalise(f) for f in (resp.get("files") or [])]
        return {"files": files, "next_page_token": str(
            resp.get("nextPageToken") or ""), "count": len(files)}

    def _normalise(self, f: dict) -> dict:
        owners = [o.get("emailAddress", "") for o in (f.get("owners") or [])]
        return {
            "id": str(f.get("id") or ""),
            "name": str(f.get("name") or "(untitled)"),
            "mime_type": str(f.get("mimeType") or ""),
            "size": int(f.get("size") or 0),
            "size_human": _fmt_size(f.get("size")),
            "modified": str(f.get("modifiedTime") or ""),
            "created": str(f.get("createdTime") or ""),
            "parents": list(f.get("parents") or []),
            "web_link": str(f.get("webViewLink") or ""),
            "md5": str(f.get("md5Checksum") or ""),
            "shared": bool(f.get("shared")),
            "trashed": bool(f.get("trashed")),
            "description": str(f.get("description") or ""),
            "owners": owners,
            "is_folder": str(f.get("mimeType") or "") == FOLDER_MIME,
            "is_google_native": is_google_native(
                str(f.get("mimeType") or "")),
        }

    def search(self, *, name: str = "", mime_type: str = "",
               modified_after: str = "", modified_before: str = "",
               folder_id: str = "", full_text: str = "",
               owner: str = "", shared_with_me: bool = False,
               max_results: int = DEFAULT_MAX_RESULTS) -> dict:
        clauses = []
        if name:
            clauses.append(f"name contains '{escape_query(name)}'")
        if mime_type:
            if mime_type.startswith("application/") or "/" in mime_type:
                clauses.append(f"mimeType = '{escape_query(mime_type)}'")
            else:
                ext = str(mime_type).lstrip(".").lower()
                guessed = mimetypes.guess_type(f"x.{ext}")[0]
                if guessed:
                    clauses.append(f"mimeType = '{escape_query(guessed)}'")
        if modified_after:
            clauses.append(f"modifiedTime > '{escape_query(modified_after)}'")
        if modified_before:
            clauses.append(f"modifiedTime < '{escape_query(modified_before)}'")
        if owner:
            clauses.append(f"'{escape_query(owner)}' in owners")
        if shared_with_me:
            clauses.append("sharedWithMe = true")
        if full_text:
            clauses.append(f"fullText contains '{escape_query(full_text)}'")
        return self.list_files(query=" and ".join(clauses),
                               folder_id=folder_id,
                               max_results=max_results)

    def files_by_ids(self, file_ids: Iterable[str]) -> list[dict]:
        out = []
        for fid in file_ids:
            try:
                out.append(self.get(fid))
            except DriveError as e:
                out.append({"id": str(fid), "error": str(e)})
        return out

    def get(self, file_id: str) -> dict:
        fid = str(file_id or "").strip()
        if not fid:
            raise DriveError("Give me a Drive file id.")
        try:
            f = self.svc.files().get(fileId=fid, fields=FILE_FIELDS).execute()
        except Exception as e:
            raise DriveError(_err(e))
        return self._normalise(f)

    def about(self) -> dict:
        try:
            data = self.svc.about().get(
                fields="user(emailAddress,displayName),"
                       "storageQuota(limit,usage,usageInDrive)").execute()
        except Exception as e:
            raise DriveError(_err(e))
        user = data.get("user") or {}
        quota = data.get("storageQuota") or {}
        return {"email": str(user.get("emailAddress") or ""),
                "name": str(user.get("displayName") or ""),
                "limit": int(quota.get("limit") or 0),
                "usage": int(quota.get("usage") or 0),
                "usage_in_drive": int(quota.get("usageInDrive") or 0)}

    # ── folders ──────────────────────────────────────────────────────────
    def create_folder(self, name: str, parent_id: str = "") -> dict:
        clean = _safe_name(name)
        body: dict[str, Any] = {"name": clean, "mimeType": FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        try:
            f = self.svc.files().create(
                body=body, fields=FILE_FIELDS).execute()
        except Exception as e:
            raise DriveError(_err(e))
        return self._normalise(f)

    def find_folder(self, name: str, parent_id: str = "") -> Optional[dict]:
        clauses = [f"name = '{escape_query(name)}'",
                   f"mimeType = '{FOLDER_MIME}'", "trashed = false"]
        if parent_id:
            clauses.append(f"'{escape_query(parent_id)}' in parents")
        try:
            resp = self.svc.files().list(
                q=" and ".join(clauses), pageSize=1,
                fields=f"files({FILE_FIELDS})").execute()
        except Exception as e:
            raise DriveError(_err(e))
        rows = resp.get("files") or []
        return self._normalise(rows[0]) if rows else None

    def ensure_folder(self, name: str, parent_id: str = "") -> dict:
        found = self.find_folder(name, parent_id)
        return found if found else self.create_folder(name, parent_id)

    def ensure_path(self, path: str, parent_id: str = "") -> dict:
        """Create/find a nested folder path ("Research/2026/Papers")."""
        current = parent_id
        last: Optional[dict] = None
        for part in [p for p in str(path or "").split("/") if p.strip()]:
            last = self.ensure_folder(part.strip(), current)
            current = last["id"]
        if last is None:
            raise DriveError("Give me a folder name.")
        return last

    # ── upload ───────────────────────────────────────────────────────────
    def upload(self, local_path: str | os.PathLike, folder_id: str = "",
               name: str = "", mime_type: str = "", resumable: bool = True,
               progress_cb: Optional[Callable[[float], None]] = None) -> dict:
        path = Path(str(local_path)).expanduser()
        if not path.is_file():
            raise DriveError(f"I can't find that file: {path}")
        size = path.stat().st_size
        mime = mime_type or _guess_mime(path)
        metadata: dict[str, Any] = {"name": name or path.name,
                                    "mimeType": mime}
        if folder_id:
            metadata["parents"] = [folder_id]

        from googleapiclient.http import MediaFileUpload

        try:
            if resumable and size >= RESUMABLE_THRESHOLD:
                request = self.svc.files().create(
                    body=metadata,
                    media_body=MediaFileUpload(str(path), mimetype=mime,
                                               chunksize=UPLOAD_CHUNK,
                                               resumable=True),
                    fields=FILE_FIELDS)
                response = None
                while response is None:
                    status, response = request.next_chunk()
                    if progress_cb and status is not None:
                        progress_cb(float(status.progress()))
                f = response
            else:
                media = MediaFileUpload(str(path), mimetype=mime,
                                        resumable=False)
                f = self.svc.files().create(
                    body=metadata, media_body=media,
                    fields=FILE_FIELDS).execute()
        except DriveError:
            raise
        except Exception as e:
            raise DriveError(_err(e))
        if not f or not f.get("id"):
            raise DriveError("Drive did not return a file id — upload is "
                             "not confirmed.")
        # Verify against the account, not just the create response.
        verified = self.get(str(f["id"]))
        if int(verified.get("size") or 0) != size and not is_google_native(
                verified.get("mime_type", "")):
            raise DriveError(
                f"Upload verification failed: local {size} bytes vs Drive "
                f"{verified.get('size')} bytes.")
        verified["local_path"] = str(path)
        verified["local_md5"] = _md5(path)
        return verified

    def upload_many(self, paths: Iterable[str | os.PathLike],
                    folder_id: str = "") -> dict:
        uploaded, failed = [], []
        for p in paths:
            try:
                uploaded.append(self.upload(p, folder_id=folder_id))
            except DriveError as e:
                failed.append({"path": str(p), "error": str(e)})
        return {"uploaded": uploaded, "failed": failed,
                "count": len(uploaded)}

    # ── download / export ────────────────────────────────────────────────
    def download(self, file_id: str, dest_dir: str | os.PathLike,
                 name: str = "", export_mime: str = "",
                 overwrite: bool = False) -> dict:
        meta = self.get(file_id)
        if meta.get("is_folder"):
            raise DriveError(f"“{meta['name']}” is a folder, not a file.")
        folder = Path(dest_dir).expanduser()
        folder.mkdir(parents=True, exist_ok=True)

        native = meta.get("mime_type") if meta.get("is_google_native") else ""
        if native:
            mime = export_mime or EXPORT_TYPES.get(native, ("application/pdf",))[0]
            if mime not in EXPORT_TYPES.get(native, ()) and native in EXPORT_TYPES:
                raise DriveError(
                    f"I can export {meta['name']} as: "
                    f"{', '.join(EXPORT_TYPES[native])}.")
            ext = EXPORT_EXT.get(mime) or mimetypes.guess_extension(mime) or ""
            filename = _safe_name(name or (meta["name"] + ext))
            request = self.svc.files().export_media(fileId=meta["id"],
                                                    mimeType=mime)
        else:
            filename = _safe_name(name or meta["name"])
            request = self.svc.files().get_media(fileId=meta["id"])

        out = folder / filename
        if out.exists() and not overwrite:
            stem, suffix = out.stem, out.suffix
            i = 2
            while (folder / f"{stem} ({i}){suffix}").exists():
                i += 1
            out = folder / f"{stem} ({i}){suffix}"

        try:
            from googleapiclient.http import MediaIoBaseDownload
            with out.open("wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _status, done = downloader.next_chunk()
        except Exception as e:
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
            raise DriveError(_err(e))
        return {"path": str(out), "filename": out.name,
                "size": out.stat().st_size, "mime_type": native or "",
                "exported_as": export_mime or (native and "auto") or "",
                "file_id": meta["id"]}

    def download_many(self, file_ids: Iterable[str],
                      dest_dir: str | os.PathLike) -> dict:
        downloaded, failed = [], []
        for fid in file_ids:
            try:
                downloaded.append(self.download(fid, dest_dir))
            except DriveError as e:
                failed.append({"id": str(fid), "error": str(e)})
        return {"downloaded": downloaded, "failed": failed,
                "count": len(downloaded)}

    def read_text(self, file_id: str, max_chars: int = 20_000) -> dict:
        """Export a Google Doc/Sheet/Slide to text and return it."""
        meta = self.get(file_id)
        native = meta.get("mime_type") if meta.get("is_google_native") else ""
        if native in (GOOGLE_DOCS,):
            mime = "text/plain"
        elif native in (GOOGLE_SHEETS,):
            mime = "text/csv"
        elif native in (GOOGLE_SLIDES,):
            mime = "text/plain"
        else:
            raise DriveError(
                f"“{meta['name']}” is not a Google document I can read as "
                f"text — download it instead.")
        try:
            data = self.svc.files().export_media(
                fileId=meta["id"], mimeType=mime).execute()
        except Exception as e:
            raise DriveError(_err(e))
        if isinstance(data, bytes):
            text = data.decode("utf-8", "replace")
        else:
            text = str(data or "")
        return {"file_id": meta["id"], "name": meta["name"],
                "text": text[:max_chars], "truncated": len(text) > max_chars}

    # ── organize ─────────────────────────────────────────────────────────
    def rename(self, file_id: str, name: str) -> dict:
        clean = str(name or "").strip()
        if not clean:
            raise DriveError("What should the new name be?")
        try:
            f = self.svc.files().update(
                fileId=str(file_id), body={"name": clean},
                fields=FILE_FIELDS).execute()
        except Exception as e:
            raise DriveError(_err(e))
        return self._normalise(f)

    def move(self, file_id: str, folder_id: str,
             remove_from: Iterable[str] = ()) -> dict:
        if not folder_id:
            raise DriveError("Which folder should I move it into?")
        meta = self.get(file_id)
        parents = list(remove_from) or [p for p in meta.get("parents", [])
                                        if p != folder_id]
        try:
            f = self.svc.files().update(
                fileId=meta["id"], addParents=folder_id,
                removeParents=",".join(parents), fields=FILE_FIELDS
            ).execute()
        except Exception as e:
            raise DriveError(_err(e))
        return self._normalise(f)

    def copy(self, file_id: str, name: str = "",
             folder_id: str = "") -> dict:
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if folder_id:
            body["parents"] = [folder_id]
        try:
            f = self.svc.files().copy(
                fileId=str(file_id), body=body,
                fields=FILE_FIELDS).execute()
        except Exception as e:
            raise DriveError(_err(e))
        return self._normalise(f)

    def describe(self, file_id: str, description: str) -> dict:
        try:
            f = self.svc.files().update(
                fileId=str(file_id), body={"description": str(description)[:2000]},
                fields=FILE_FIELDS).execute()
        except Exception as e:
            raise DriveError(_err(e))
        return self._normalise(f)

    def organize_by_type(self, folder_id: str, *, dry_run: bool = True,
                         recursive: bool = False) -> dict:
        """Group files into category folders. dry_run returns the plan only."""
        listing = self.list_files(folder_id=folder_id, max_results=MAX_RESULTS_CAP)
        moves, folders = [], {}
        for f in listing["files"]:
            if f["is_folder"]:
                continue
            category = _category_for(f["mime_type"])
            if category == "other":
                continue
            moves.append({"id": f["id"], "name": f["name"],
                          "category": category})
        for category in sorted({m["category"] for m in moves}):
            folders[category] = None
        if dry_run:
            return {"dry_run": True, "folder_id": folder_id,
                    "would_move": len(moves), "moves": moves,
                    "folders": sorted(folders)}
        created = {}
        for category in sorted(folders):
            created[category] = self.ensure_folder(category.capitalize(),
                                                   folder_id)
        done, failed = [], []
        for move in moves:
            try:
                self.move(move["id"], created[move["category"]]["id"])
                done.append(move)
            except DriveError as e:
                failed.append({**move, "error": str(e)})
        return {"dry_run": False, "moved": len(done), "failed": failed,
                "folders": {k: v["name"] for k, v in created.items()}}

    def find_duplicates(self, folder_id: str = "",
                        max_results: int = MAX_RESULTS_CAP) -> list[dict]:
        listing = self.list_files(folder_id=folder_id, max_results=max_results)
        groups: dict[str, list[dict]] = {}
        for f in listing["files"]:
            if f["is_folder"]:
                continue
            key = f["md5"] or f"{f['name'].lower()}|{f['size']}"
            groups.setdefault(key, []).append(f)
        out = []
        for key, rows in groups.items():
            if len(rows) > 1:
                out.append({"key": key, "count": len(rows),
                            "files": rows})
        out.sort(key=lambda g: -g["count"])
        return out

    # ── trash / delete ───────────────────────────────────────────────────
    def trash(self, file_ids) -> dict:
        ids = _ids(file_ids)
        if not ids:
            raise DriveError("Which file(s) should I move to Trash?")
        done, failed = [], []
        for fid in ids:
            try:
                f = self.svc.files().update(
                    fileId=fid, body={"trashed": True},
                    fields=FILE_FIELDS).execute()
                done.append(self._normalise(f))
            except Exception as e:
                failed.append({"id": fid, "error": _err(e)})
        return {"trashed": len(done), "files": done, "failed": failed}

    def untrash(self, file_ids) -> dict:
        ids = _ids(file_ids)
        if not ids:
            raise DriveError("Which file(s) should I restore?")
        done, failed = [], []
        for fid in ids:
            try:
                f = self.svc.files().update(
                    fileId=fid, body={"trashed": False},
                    fields=FILE_FIELDS).execute()
                done.append(self._normalise(f))
            except Exception as e:
                failed.append({"id": fid, "error": _err(e)})
        return {"restored": len(done), "files": done, "failed": failed}

    def delete_permanent(self, file_ids) -> dict:
        """Permanent delete. Callers MUST have explicit confirmation first."""
        ids = _ids(file_ids)
        if not ids:
            raise DriveError("Which file(s) should I delete permanently?")
        done, failed = [], []
        for fid in ids:
            try:
                self.svc.files().delete(fileId=fid).execute()
                done.append(fid)
            except Exception as e:
                failed.append({"id": fid, "error": _err(e)})
        return {"deleted": len(done), "ids": done, "failed": failed}

    # ── permissions ──────────────────────────────────────────────────────
    def permissions(self, file_id: str) -> list[dict]:
        try:
            resp = self.svc.permissions().list(
                fileId=str(file_id),
                fields="permissions(id,type,role,emailAddress,domain,"
                       "displayName,deleted)").execute()
        except Exception as e:
            raise DriveError(_err(e))
        return [{"id": str(p.get("id") or ""),
                 "type": str(p.get("type") or ""),
                 "role": str(p.get("role") or ""),
                 "email": str(p.get("emailAddress") or ""),
                 "domain": str(p.get("domain") or ""),
                 "name": str(p.get("displayName") or ""),
                 "deleted": bool(p.get("deleted"))}
                for p in resp.get("permissions") or []]

    def share(self, file_id: str, *, email: str = "", role: str = "reader",
              type_: str = "user", domain: str = "",
              send_notification: bool = False) -> dict:
        body: dict[str, Any] = {"type": type_, "role": role}
        if email:
            body["emailAddress"] = email
        if domain:
            body["domain"] = domain
        try:
            p = self.svc.permissions().create(
                fileId=str(file_id), body=body,
                sendNotificationEmail=bool(send_notification),
                fields="id,type,role,emailAddress,displayName").execute()
        except Exception as e:
            raise DriveError(_err(e))
        return {"id": str(p.get("id") or ""),
                "type": str(p.get("type") or type_),
                "role": str(p.get("role") or role),
                "email": str(p.get("emailAddress") or email)}

    def update_permission(self, file_id: str, permission_id: str,
                          role: str) -> dict:
        try:
            p = self.svc.permissions().update(
                fileId=str(file_id), permissionId=str(permission_id),
                body={"role": role},
                fields="id,type,role,emailAddress").execute()
        except Exception as e:
            raise DriveError(_err(e))
        return {"id": str(p.get("id") or permission_id),
                "role": str(p.get("role") or role)}

    def remove_permission(self, file_id: str, permission_id: str) -> dict:
        try:
            self.svc.permissions().delete(
                fileId=str(file_id), permissionId=str(permission_id)).execute()
        except Exception as e:
            raise DriveError(_err(e))
        return {"file_id": str(file_id), "permission_id": str(permission_id),
                "removed": True}


def _ids(values) -> list[str]:
    if isinstance(values, str):
        values = [v for v in re.split(r"[\s,;]+", values) if v]
    seen, out = set(), []
    for v in values or []:
        v = str(v or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


__all__ = [
    "DriveClient", "DriveError", "FOLDER_MIME", "EXPORT_TYPES",
    "is_google_native", "escape_query", "_category_for",
]
