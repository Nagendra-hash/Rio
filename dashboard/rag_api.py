"""
dashboard/rag_api.py — REST surface for the RAG service.

Endpoints (all bearer-auth via the dashboard's _auth, like every /api route):

    POST /api/rag/query          {query, collection?, top_k?, caller?, doc_id?}
    POST /api/rag/answer         {query, strategy?, collection?, doc_id?, caller?}
                                 — the strategy pipeline: cited, risk-graded answer
    POST /api/rag/ingest         multipart file + collection/title/access/caller
    POST /api/rag/ingest-path    JSON {path, collection?, title?, caller?…} — Hermes flows
    POST /api/rag/ingest-text    JSON {text, title, collection?, access?, suffix?}
    POST /api/rag/update         multipart file + doc_id (content replacement)
    POST /api/rag/update-meta    JSON {doc_id, title?, collection?, access?, metadata?}
    POST /api/rag/forget         JSON {doc_id}
    GET  /api/rag/documents      → list (optional ?collection=&caller=)
    GET  /api/rag/stats          → stats (?caller=)

SELF-DIRECTED LEARNING (core/rag_learner)

    GET  /api/rag/learning       → learner status (topics, runs, gaps, due)
    POST /api/rag/learning       → {action: run|add_topic|remove_topic|config}

GROUP CHAT (Phase 19 — user identity, per-user permissions; see core/rag_group):

    POST /api/rag/group/rooms    create room | GET ?user= → rooms for a user
    GET  /api/rag/group/room     room info (?room_id=&user=) — member-gated
    POST /api/rag/group/members  {room_id, actor, user, role? | remove:true}
    POST /api/rag/group/message  {room_id, user, text} — participation rules fire
    POST /api/rag/group/ask-agent{room_id, user, agent, text} — manual invoke
    GET  /api/rag/group/history  ?room_id=&user=&limit=&after= — member-gated
    POST /api/rag/group/share-text {room_id, user, text, title, share?}
    POST /api/rag/group/grant    {room_id, actor, doc_id, target_user?|grant_all?, revoke?}
    GET  /api/rag/group/documents?room_id=&user= — the member's permission lens

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

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from core import rag, rag_service
from core import rag_group

# FastAPI upload support is optional (mirror dashboard/server.py: /api/upload
# degrades to 503 without python-multipart).
try:
    from fastapi import File, Form, UploadFile  # noqa: F401

    _UPLOAD_OK = True
except Exception:
    File = Form = UploadFile = None  # type: ignore[assignment]
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
            return JSONResponse({"ok": False, "error": "empty query"}, status_code=400)
        caller = str(body.get("caller") or "dashboard").strip() or "dashboard"
        try:
            top_k = max(1, min(int(body.get("top_k") or 5), 25))
        except (TypeError, ValueError):
            top_k = 5
        result = rag_service.search(
            caller=caller,
            query=query,
            top_k=top_k,
            collection=body.get("collection") or None,
            doc_id=body.get("doc_id") or None,
        )
        return JSONResponse(result, status_code=_status_for(result))

    @app.post("/api/rag/answer")
    async def rag_answer(req: Request):
        """The strategy pipeline: question → cited, risk-graded answer.

        Body: {query, strategy?, collection?, doc_id?, caller?}
        strategy: auto (default) | adaptive | agentic | corrective |
                  self_rag | local_adaptive | local_self_rag. An LLM strategy
                  requested while the model ladder is down degrades to its
                  local twin instead of failing.
        """
        if not auth_check(req):
            return _deny_auth()
        try:
            body = await req.json()
        except Exception:
            body = {}
        query = str(body.get("query") or "").strip()
        if not query:
            return JSONResponse({"ok": False, "error": "empty query"}, status_code=400)
        result = rag_service.answer(
            caller=str(body.get("caller") or "dashboard"),
            query=query,
            strategy=str(body.get("strategy") or "auto"),
            collection=body.get("collection") or None,
            doc_id=body.get("doc_id") or None,
        )
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
                    {"ok": False, "error": "no file part in request"}, status_code=400
                )
            saved = await _save_upload(file)
            result = rag_service.ingest(
                caller=str(caller or "jarvis"),
                file_path=saved,
                collection=str(collection or rag.DEFAULT_COLLECTION),
                title=title,
                source=str(source or "dashboard-upload"),
                access=str(access or "private"),
            )
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
                return JSONResponse(
                    {"ok": False, "error": "no path given"}, status_code=400
                )
            result = rag_service.ingest(
                caller=str(body.get("caller") or "jarvis"),
                file_path=path,
                collection=str(body.get("collection") or rag.DEFAULT_COLLECTION),
                title=body.get("title"),
                source=str(body.get("source") or "hermes"),
                access=str(body.get("access") or "private"),
                metadata=body.get("metadata"),
            )
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
                return JSONResponse(
                    {"ok": False, "error": "doc_id required"}, status_code=400
                )
            saved = await _save_upload(file) if file is not None else None
            result = rag_service.update(
                caller=str(caller or "jarvis"),
                doc_id=str(doc_id),
                new_path=saved,
                collection=collection or None,
                title=title,
            )
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
            return JSONResponse(
                {"ok": False, "error": "title required"}, status_code=400
            )
        result = rag_service.ingest_text(
            caller=str(body.get("caller") or "jarvis"),
            text=text,
            title=title,
            collection=str(body.get("collection") or rag.DEFAULT_COLLECTION),
            suffix=str(body.get("suffix") or "txt"),
            access=str(body.get("access") or "private"),
            metadata=body.get("metadata"),
        )
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
            return JSONResponse(
                {"ok": False, "error": "doc_id required"}, status_code=400
            )
        result = rag_service.update(
            caller=str(body.get("caller") or "jarvis"),
            doc_id=doc_id,
            title=body.get("title"),
            collection=body.get("collection"),
            access=body.get("access"),
            metadata=body.get("metadata"),
        )
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
            return JSONResponse(
                {"ok": False, "error": "doc_id required"}, status_code=400
            )
        result = rag_service.forget(
            caller=str(body.get("caller") or "jarvis"), doc_id=doc_id
        )
        return JSONResponse(result, status_code=_status_for(result))

    @app.get("/api/rag/documents")
    async def rag_documents(req: Request, collection: str = ""):
        if not auth_check(req):
            return _deny_auth()
        caller = str(req.query_params.get("caller") or "dashboard")
        result = rag_service.list_documents(
            caller=caller, collection=collection or None
        )
        return JSONResponse(result, status_code=_status_for(result))

    @app.get("/api/rag/stats")
    async def rag_stats(req: Request):
        if not auth_check(req):
            return _deny_auth()
        caller = str(req.query_params.get("caller") or "dashboard")
        return JSONResponse(rag_service.stats(caller=caller))

    # ── self-directed web learning (core/rag_learner) ───────────────────
    @app.get("/api/rag/learning")
    async def rag_learning_status(req: Request):
        if not auth_check(req):
            return _deny_auth()
        from core import rag_learner

        return JSONResponse(rag_learner.status())

    @app.post("/api/rag/learning")
    async def rag_learning_action(req: Request):
        """{action: run|add_topic|remove_topic|config, ...}

        run          → one learning cycle now (topics?: [..])
        add_topic    → {topic}
        remove_topic → {topic}
        config       → {patch: {enabled, hour, …}}
        """
        if not auth_check(req):
            return _deny_auth()
        try:
            body = await req.json()
        except Exception:
            body = {}
        from core import rag_learner

        action = str(body.get("action") or "status").strip().lower()
        if action == "run":
            topics = body.get("topics")
            if isinstance(topics, str):
                topics = [t.strip() for t in topics.split(",") if t.strip()]
            # A cycle does network + embeddings + LLM work; keep the event
            # loop free (the dashboard stays responsive while it learns).
            result = await asyncio.to_thread(
                rag_learner.run_cycle, True, topics=topics or None
            )
            return JSONResponse(result, status_code=200 if result.get("ok") else 409)
        if action == "add_topic":
            try:
                weight = float(body.get("weight") or 2.0)
            except (TypeError, ValueError):
                weight = 2.0
            result = rag_learner.add_topic(str(body.get("topic") or ""), weight=weight)
            return JSONResponse(result, status_code=_status_for(result))
        if action == "remove_topic":
            result = rag_learner.remove_topic(str(body.get("topic") or ""))
            return JSONResponse(result, status_code=_status_for(result))
        if action == "config":
            patch = body.get("patch")
            if not isinstance(patch, dict):
                return JSONResponse(
                    {"ok": False, "error": "patch object required"}, status_code=400
                )
            return JSONResponse({"ok": True, "config": rag_learner.save_config(patch)})
        return JSONResponse(
            {
                "ok": False,
                "error": f"unknown action '{action}'",
                "status": rag_learner.status(),
            },
            status_code=400,
        )

    # ── Phase 19: group chat ────────────────────────────────────────────
    # Same bearer auth; identity is the USER, and every route re-derives
    # permissions per call via core/rag_group (config re-read each use).

    @app.post("/api/rag/group/rooms")
    async def rag_group_rooms(req: Request):
        """Create a room {room_id, name, owner, shared_collections?, agents?,
        member_can_ingest?, context_turns?} — or list rooms ?user=."""
        if not auth_check(req):
            return _deny_auth()
        try:
            body = await req.json()
        except Exception:
            body = {}
        if body.get("list") or not body.get("room_id"):
            return JSONResponse(rag_group.list_rooms(body.get("user")))
        result = rag_group.create_room(
            str(body.get("room_id")),
            str(body.get("name") or ""),
            str(body.get("owner") or ""),
            shared_collections=body.get("shared_collections"),
            agents=body.get("agents"),
            member_can_ingest=bool(body.get("member_can_ingest", True)),
            context_turns=int(body.get("context_turns") or 3),
        )
        return JSONResponse(result, status_code=_status_for(result))

    @app.get("/api/rag/group/rooms")
    async def rag_group_rooms_list(req: Request):
        if not auth_check(req):
            return _deny_auth()
        return JSONResponse(rag_group.list_rooms(req.query_params.get("user") or None))

    @app.get("/api/rag/group/room")
    async def rag_group_room(req: Request):
        if not auth_check(req):
            return _deny_auth()
        result = rag_group.room_info(
            req.query_params.get("room_id") or "", req.query_params.get("user") or ""
        )
        return JSONResponse(result, status_code=_status_for(result))

    @app.post("/api/rag/group/members")
    async def rag_group_members(req: Request):
        """{room_id, actor, user, role?} add/set role · remove:true removes.
        A user may always remove themselves (leave)."""
        if not auth_check(req):
            return _deny_auth()
        try:
            body = await req.json()
        except Exception:
            body = {}
        rid = str(body.get("room_id") or "")
        actor = str(body.get("actor") or "")
        user = str(body.get("user") or "")
        if not rid or not actor or not user:
            return JSONResponse(
                {"ok": False, "error": "room_id, actor and user required"},
                status_code=400,
            )
        if body.get("remove"):
            result = rag_group.remove_member(rid, actor, user)
        else:
            result = rag_group.set_member(
                rid, actor, user, str(body.get("role") or "member")
            )
        return JSONResponse(result, status_code=_status_for(result))

    @app.post("/api/rag/group/message")
    async def rag_group_message(req: Request):
        """Post as a user: {room_id, user, text}. Participation rules decide
        which room agents answer; replies carry citations."""
        if not auth_check(req):
            return _deny_auth()
        try:
            body = await req.json()
        except Exception:
            body = {}
        result = rag_group.post(
            str(body.get("room_id") or ""),
            str(body.get("user") or ""),
            str(body.get("text") or ""),
        )
        return JSONResponse(result, status_code=_status_for(result))

    @app.post("/api/rag/group/ask-agent")
    async def rag_group_ask_agent(req: Request):
        """Directly invoke one room agent: {room_id, user, agent, text}."""
        if not auth_check(req):
            return _deny_auth()
        try:
            body = await req.json()
        except Exception:
            body = {}
        result = rag_group.ask_agent(
            str(body.get("room_id") or ""),
            str(body.get("user") or ""),
            str(body.get("agent") or ""),
            str(body.get("text") or ""),
        )
        return JSONResponse(result, status_code=_status_for(result))

    @app.get("/api/rag/group/history")
    async def rag_group_history(req: Request):
        if not auth_check(req):
            return _deny_auth()
        qp = req.query_params
        after = qp.get("after")
        result = rag_group.history(
            qp.get("room_id") or "",
            qp.get("user") or "",
            limit=int(qp.get("limit") or 50),
            after=int(after) if after else None,
        )
        return JSONResponse(result, status_code=_status_for(result))

    @app.post("/api/rag/group/share-text")
    async def rag_group_share_text(req: Request):
        """Contribute a document to the room pool: {room_id, user, text,
        title, collection?, share?: 'room'|'private'}."""
        if not auth_check(req):
            return _deny_auth()
        try:
            body = await req.json()
        except Exception:
            body = {}
        result = rag_group.share_text(
            str(body.get("room_id") or ""),
            str(body.get("user") or ""),
            str(body.get("text") or ""),
            str(body.get("title") or ""),
            collection=body.get("collection") or None,
            share=str(body.get("share") or "room"),
        )
        return JSONResponse(result, status_code=_status_for(result))

    @app.post("/api/rag/group/grant")
    async def rag_group_grant(req: Request):
        """Per-document permission: {room_id, actor, doc_id, target_user? |
        grant_all?, revoke?}. Owner of the doc or of the room only."""
        if not auth_check(req):
            return _deny_auth()
        try:
            body = await req.json()
        except Exception:
            body = {}
        result = rag_group.grant_document(
            str(body.get("room_id") or ""),
            str(body.get("actor") or ""),
            str(body.get("doc_id") or ""),
            target_user=body.get("target_user"),
            grant_all=bool(body.get("grant_all")),
            revoke=bool(body.get("revoke")),
        )
        return JSONResponse(result, status_code=_status_for(result))

    @app.get("/api/rag/group/documents")
    async def rag_group_documents(req: Request):
        """The room's library through one member's permission lens."""
        if not auth_check(req):
            return _deny_auth()
        qp = req.query_params
        result = rag_group.visible_documents(
            qp.get("room_id") or "", qp.get("user") or ""
        )
        return JSONResponse(result, status_code=_status_for(result))


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
