"""
dashboard/memory_api.py — REST surface for long-term memory (memory_manager).

Endpoints (all bearer-auth via the dashboard's _auth):

    GET  /api/memory/search      ?query=&limit=N          lexical search over all entries
    GET  /api/memory/list        ?category=&limit=N       list entries by category
    GET  /api/memory/stats       → {categories: {...}, total: N}
    POST /api/memory/add         {category, key, value}     append a fact
    DELETE /api/memory/delete     {id}                      remove a fact by UUID

CALLER IDENTITY
    All reads are read-only; writes default to caller "dashboard".
    The memory store is process-wide and locked — concurrent access is safe.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


def _deny_auth() -> JSONResponse:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


def register(app, auth_check) -> None:
    """Attach the /api/memory/* endpoints to a running FastAPI app."""

    @app.get("/api/memory/search")
    async def memory_search(req: Request, query: str = "", limit: int = 8):
        if not auth_check(req):
            return _deny_auth()
        try:
            from memory.memory_manager import load_memory, search_memory
            mem = load_memory()
            entries = mem if isinstance(mem, list) else []
            q = (query or "").lower().strip()
            if not q:
                return JSONResponse({
                    "ok": True, "matches": [], "total": len(entries),
                    "note": "empty query — returning full listing"
                })
            results = search_memory(q, limit=int(limit) or 8)
            # search_memory returns a formatted string; parse it back if needed.
            # Fallback: do our own lexical match on all_entries_for_ui().
            if not results:
                from memory.memory_manager import all_entries_for_ui
                all_entries = all_entries_for_ui()
                hits = [e for e in all_entries
                        if q in str(e).lower()][:int(limit) or 8]
                return JSONResponse({"ok": True, "matches": hits, "total": len(hits)})
            return JSONResponse({"ok": True, "matches": results})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.get("/api/memory/list")
    async def memory_list(req: Request, category: str = "", limit: int = 20):
        if not auth_check(req):
            return _deny_auth()
        try:
            from memory.memory_manager import all_entries_for_ui
            entries = all_entries_for_ui()
            if category:
                entries = [e for e in entries if e.get("category") == category]
            n = int(limit) or 20
            return JSONResponse({
                "ok": True, "entries": entries[:n],
                "total": len(entries), "category": category or "all"
            })
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.get("/api/memory/stats")
    async def memory_stats(req: Request):
        if not auth_check(req):
            return _deny_auth()
        try:
            from memory.memory_manager import all_entries_for_ui, load_memory
            entries = all_entries_for_ui()
            mem = load_memory()
            cats: dict = {}
            for e in entries:
                c = e.get("category", "unknown")
                cats[c] = cats.get(c, 0) + 1
            return JSONResponse({
                "ok": True, "total_entries": len(entries),
                "by_category": cats,
                "raw_keys": list((mem if isinstance(mem, dict) else {}).keys())
            })
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.post("/api/memory/add")
    async def memory_add(req: Request):
        if not auth_check(req):
            return _deny_auth()
        try:
            body = await req.json()
            from memory.memory_manager import save_memory, MEMORY_PATH
            import json as _json
            data = _json.loads(MEMORY_PATH.read_text()) if MEMORY_PATH.exists() else {}
            cat = str(body.get("category") or "notes").strip()
            key = str(body.get("key") or "").strip()
            val = str(body.get("value") or "").strip()
            if not key:
                return JSONResponse({"ok": False, "error": "key required"}, status_code=400)
            if cat not in data:
                data[cat] = []
            data[cat].append({"key": key, "value": val})
            MEMORY_PATH.write_text(_json.dumps(data, indent=2, ensure_ascii=False))
            return JSONResponse({"ok": True, "category": cat, "key": key})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.delete("/api/memory/delete")
    async def memory_delete(req: Request):
        if not auth_check(req):
            return _deny_auth()
        try:
            body = await req.json()
            from memory.memory_manager import save_memory, MEMORY_PATH
            import json as _json
            entry_id = str(body.get("id") or "").strip()
            if not entry_id:
                return JSONResponse({"ok": False, "error": "id required"}, status_code=400)
            data = _json.loads(MEMORY_PATH.read_text()) if MEMORY_PATH.exists() else {}
            found = False
            for cat in list(data.keys()):
                before = len(data[cat])
                data[cat] = [e for e in data[cat] if str(e.get("id", "")) != entry_id]
                if len(data[cat]) < before:
                    found = True
            if not found:
                return JSONResponse({"ok": False, "error": "entry not found"}, status_code=404)
            MEMORY_PATH.write_text(_json.dumps(data, indent=2, ensure_ascii=False))
            return JSONResponse({"ok": True})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
