"""
dashboard/rag_api.py — REST surface for the RAG service.

Endpoints (all bearer-auth via the dashboard's _auth, like every /api route):

    POST /api/rag/query          {query, collection?, top_k?, caller?, doc_id?}
    POST /api/rag/ingest         multipart file + collection/title/access/caller
    POST /api/rag/ingest-path    JSON {path, collection?, title?, caller?…} — Hermes flows
    POST /api/rag/ingest-text    JSON {text, title, collection?, access?, suffix?}
    POST /api/rag/update         multipart file + doc_id (content replacement)
    POST /api/rag/update-meta    JSON {doc_id, title?, collection?, access?, metadata?}
    POST /api/rag/forget         JSON {doc_id}
    GET  /api/rag/documents      → list (optional ?collection=&caller=)
    GET  /api/rag/stats          → stats (?caller=)

CALLER IDENTITY
    Queries name their caller (default "dashboard"); uploads default to the
    owner ("jarvis"). core/rag_service enforces collection ACL + write gating,
    so a token grants the *keyhole*, not the whole library. The owner caller
    bypasses ACL exactly like Jarvis's own action tool.

    Hermes document flows that already hold a saved PDF ingest it via
    /api/rag/ingest-path {path, ...} — a local path, not an upload, because
    the file never left this machine.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from core import rag, rag_service

# FastAPI upload support is optional (mirror dashboard/server.py: /api/upload
# degrades to 503 without python-multipart).
try:
    from fastapi import File, Form, UploadFile  # noqa: F401
    _UPLOAD_OK = True
except Exception:
    File = Form = UploadFile = None              # type: ignore[assignment]
    _UPLOAD_OK = False


def _status_for(result: dict) -> int:
    if result.get("ok"):
        return 200
    return 403 if str(result.get("error", "")).startswith("access denied") else 400


def _deny_auth() -> JSONResponse:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


def register(app, auth_check) -> None:
    """Attach the /api/rag/* endpoints to a running FastAPI app.

    auth_check(req) is the dashboard's _auth — token checking stays with the
    server; this module only maps HTTP to the service layer.
    """

    @app.post("/api/rag/query")
    async def rag_query(req: Request):
        if not auth_check(req):
            return _deny_auth()
        try:
            body = await req.json()
        except Exception:
            body = {}
        query = str(body.get("query") or "").strip()
        if not query:
            return JSONResponse({"ok": False, "error": "empty query"},
                                status_code=400)
        caller = str(body.get("caller") or "dashboard").strip() or "dashboard"
        try:
            top_k = max(1, min(int(body.get("top_k") or 5), 25))
        except (TypeError, ValueError):
            top_k = 5
        result = rag_service.search(
            caller=caller, query=query, top_k=top_k,
            collection=body.get("collection") or None,
            doc_id=body.get("doc_id") or None)
        return JSONResponse(result, status_code=_status_for(result))

    if _UPLOAD_OK:

        @app.post("/api/rag/ingest")
        async def rag_ingest(
            req: Request,
            file: UploadFile | None = File(None),
            collection: str | None = Form(None),
            title: str | None = Form(None),
            access: str | None = Form(None),
            caller: str | None = Form(None),
            source: str | None = Form(None),
        ):
            if not auth_check(req):
                return _deny_auth()
            if file is None or not getattr(file, "filename", None):
                return JSONResponse(
                    {"ok": False, "error": "no file part in request"},
                    status_code=400)
            saved = await _save_upload(file)
            result = rag_service.ingest(
                caller=str(caller or "jarvis"), file_path=saved,
                collection=str(collection or rag.DEFAULT_COLLECTION),
                title=title, source=str(source or "dashboard-upload"),
                access=str(access or "private"))
            return JSONResponse(result, status_code=_status_for(result))

        @app.post("/api/rag/ingest-path")
        async def rag_ingest_path(req: Request):
            """Ingest a file that already exists on disk (Hermes flows: the
            gateway already saved the document; no re-upload needed)."""
            if not auth_check(req):
                return _deny_auth()
            try:
                body = await req.json()
            except Exception:
                body = {}
            path = str(body.get("path") or "").strip()
            if not path:
                return JSONResponse({"ok": False, "error": "no path given"},
                                    status_code=400)
            result = rag_service.ingest(
                caller=str(body.get("caller") or "jarvis"), file_path=path,
                collection=str(body.get("collection")
                               or rag.DEFAULT_COLLECTION),
                title=body.get("title"), source=str(body.get("source")
                                                    or "hermes"),
                access=str(body.get("access") or "private"),
                metadata=body.get("metadata"))
            return JSONResponse(result, status_code=_status_for(result))

        @app.post("/api/rag/update")
        async def rag_update(
            req: Request,
            file: UploadFile | None = File(None),
            doc_id: str | None = Form(None),
            collection: str | None = Form(None),
            title: str | None = Form(None),
            caller: str | None = Form(None),
        ):
            if not auth_check(req):
                return _deny_auth()
            if not (doc_id or "").strip():
                return JSONResponse({"ok": False,
                                     "error": "doc_id required"}, status_code=400)
            saved = await _save_upload(file) if file is not None else None
            result = rag_service.update(
                caller=str(caller or "jarvis"), doc_id=str(doc_id),
                new_path=saved, collection=collection or None, title=title)
            return JSONResponse(result, status_code=_status_for(result))

    @app.post("/api/rag/ingest-text")
    async def rag_ingest_text(req: Request):
        if not auth_check(req):
            return _deny_auth()
        try:
            body = await req.json()
        except Exception:
            body = {}
        text = str(body.get("text") or "")
        title = str(body.get("title") or "").strip()
        if not title:
            return JSONResponse({"ok": False, "error": "title required"},
                                status_code=400)
        result = rag_service.ingest_text(
            caller=str(body.get("caller") or "jarvis"), text=text, title=title,
            collection=str(body.get("collection")
                           or rag.DEFAULT_COLLECTION),
            suffix=str(body.get("suffix") or "txt"),
            access=str(body.get("access") or "private"),
            metadata=body.get("metadata"))
        return JSONResponse(result, status_code=_status_for(result))

    @app.post("/api/rag/update-meta")
    async def rag_update_meta(req: Request):
        if not auth_check(req):
            return _deny_auth()
        try:
            body = await req.json()
        except Exception:
            body = {}
        doc_id = str(body.get("doc_id") or "").strip()
        if not doc_id:
            return JSONResponse({"ok": False, "error": "doc_id required"},
                                status_code=400)
        result = rag_service.update(
            caller=str(body.get("caller") or "jarvis"), doc_id=doc_id,
            title=body.get("title"), collection=body.get("collection"),
            access=body.get("access"), metadata=body.get("metadata"))
        return JSONResponse(result, status_code=_status_for(result))

    @app.post("/api/rag/forget")
    async def rag_forget(req: Request):
        if not auth_check(req):
            return _deny_auth()
        try:
            body = await req.json()
        except Exception:
            body = {}
        doc_id = str(body.get("doc_id") or "").strip()
        if not doc_id:
            return JSONResponse({"ok": False, "error": "doc_id required"},
                                status_code=400)
        result = rag_service.forget(caller=str(body.get("caller") or "jarvis"),
                                    doc_id=doc_id)
        return JSONResponse(result, status_code=_status_for(result))

    @app.get("/api/rag/documents")
    async def rag_documents(req: Request, collection: str = ""):
        if not auth_check(req):
            return _deny_auth()
        caller = str(req.query_params.get("caller") or "dashboard")
        result = rag_service.list_documents(caller=caller,
                                            collection=collection or None)
        return JSONResponse(result, status_code=_status_for(result))

    @app.get("/api/rag/stats")
    async def rag_stats(req: Request):
        if not auth_check(req):
            return _deny_auth()
        caller = str(req.query_params.get("caller") or "dashboard")
        return JSONResponse(rag_service.stats(caller=caller))


async def _save_upload(file) -> str:
    """Stream an upload to a temp file and return its path. The service moves
    bytes into knowledge/library itself — temp keeps the request stateless."""
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="rag-upload-", suffix=_suffix_of(file))
    try:
        with os.fdopen(fd, "wb") as fout:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                fout.write(chunk)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return tmp


def _suffix_of(file) -> str:
    name = getattr(file, "filename", "") or ""
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return suffix[:10] or ".bin"
