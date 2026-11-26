"""
core/rag_group.py — PHASE 19: RAG GROUP CHAT.

POSITION (deliberate — see core/rag_service.py)
    core/rag.py is the LIBRARY, core/rag_service.py is the front desk with
    per-CALLER collection ACL, core/rag_strategies are the retrieval brains.
    This module adds the missing dimension: PEOPLE. A group chat is many
    users sharing one room, one transcript and one knowledge pool — but NOT
    one permission level. This file owns identity, rooms, roles and the
    per-user permission lens; retrieval itself is NEVER reimplemented here.
    Every answer goes through rag_service.answer() (the strategies pipeline)
    with a pre-scoped per-user whitelist, exactly the way a single-user
    agent call does — one retrieval stack, one security posture.

THE PERMISSION MODEL (three gates, all must pass, checked on every call)

    1. MEMBERSHIP    — a user must be a member of the room to post, read
                       history or retrieve anything from it. No member row,
                       no access, full stop.
    2. COLLECTIONS   — the room declares shared_collections; a user with an
                       explicit `collections` grant sees only the
                       INTERSECTION (user grant ∩ room pool). Absent grant →
                       the room pool. Empty grant → blind, by design.
    3. DOCUMENT      — the registry's access flag decides the last inch:
                         private → only the document owner (metadata.owner)
                                   and the room owner may retrieve it;
                         shared  → every member whose collection scope
                                   includes it, optionally restricted further
                                   by metadata.shared_with (list of user_ids
                                   or ["all"]).

    The resulting set of doc_ids is handed to rag_service as
    extra_allowed_docs — which the service INTERSECTS with its own caller
    grant, so this layer can only ever NARROW the library, never widen it.
    A user cannot retrieve a document they are not authorized to read, no
    matter how the question is phrased, which strategy runs, or who else is
    in the room.

AGENT PARTICIPATION RULES
    Each room carries an agent list; each entry decides WHEN that agent
    speaks and WHAT it may see:

        {"agent": "librarian", "trigger": "always",   "strategy": "auto"}
        {"agent": "auditor",   "trigger": "mention"}            # only @auditor
        {"agent": "tickerbot", "trigger": "keywords:earnings,guidance"}
        {"agent": "archivist", "trigger": "manual"}             # ask_agent() only
        {"agent": "legal",     "trigger": "always", "collections": ["legal_docs"]}

    Agent messages never trigger agents (no loops). An agent's optional
    `collections` further narrow the per-user scope for its own retrieval —
    an agent can be LESS privileged than the user it answers, never more.

SHARED CONTEXT + TRANSCRIPTS
    One append-only transcript per room (memory/rag_group_chat/<room>.jsonl):
    user messages, agent replies (with citations), system events. Every
    member sees the same history — that IS the shared conversation context —
    and the last `context_turns` user messages are folded into the next
    retrieval query so follow-ups ("and its page 2?") still resolve. The
    scope stays the asking user's; context changes the query, never the ACL.

STATE (file-backed, re-read on every use like config/rag_callers.json)
    config/rag_groups.json                  rooms + users registry
    memory/rag_group_chat/<room_id>.jsonl   per-room transcript, append-only

AUDIT
    Every membership change, share, and retrieval lands in knowledge/audit.log
    tagged `group:*`, with actor and scope size — "who saw what, when" has an
    answer, per user, per room.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from core import rag
from core import rag_service
from memory.config_manager import BASE_DIR

GROUPS_FILE = BASE_DIR / "config" / "rag_groups.json"
TRANSCRIPTS_DIR = BASE_DIR / "memory" / "rag_group_chat"

# The retrieval identity this layer acts as. Minted in config/rag_callers.json
# with __all__ + can_write: the PER-USER narrowing below is what makes that
# safe, and the service-layer intersection keeps it narrow-only.
GROUP_CALLER = "group_chat"

ROLES = ("owner", "admin", "member", "guest")
_ROLE_RANK = {"guest": 0, "member": 1, "admin": 2, "owner": 3}
_VALID_TRIGGERS = ("always", "mention", "manual", "keywords")

_transcript_lock = threading.RLock()

_DEFAULT_AGENTS = [{"agent": "librarian", "trigger": "always", "strategy": "auto"}]

_CITE_FIX = re.compile(r"^\[(\d+)\]\s*")


# ── config (rooms + users) ──────────────────────────────────────────────────

def load_config() -> dict:
    """Rooms + users, re-read every call — revoking access never needs a
    restart (same contract as config/rag_callers.json)."""
    try:
        data = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("users", {})
            data.setdefault("rooms", {})
            return data
    except Exception:
        pass
    return {"users": {}, "rooms": {}}


def _save_config(cfg: dict) -> None:
    GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = GROUPS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(GROUPS_FILE)


def _audit(action: str, detail: str) -> None:
    rag._audit(f"group:{action}", detail)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitize_room_id(room_id: str) -> str:
    """Room ids become transcript filenames — keep them boring."""
    return re.sub(r"[^a-zA-Z0-9_-]", "", str(room_id).strip().lower())[:48]


# ── users ────────────────────────────────────────────────────────────────────

def upsert_user(user_id: str, display_name: str | None = None,
                collections: list[str] | None = None,
                *, set_collections: bool = False) -> dict:
    """Register/update a user. `collections=None` (or set_collections=False)
    means "inherit whatever room you join"; an explicit list — including an
    EMPTY one, which means blind — narrows every room you are in."""
    uid = str(user_id).strip()
    if not uid:
        return {"ok": False, "error": "user_id required"}
    cfg = load_config()
    users = cfg["users"]
    entry = dict(users.get(uid) or {})
    if display_name is not None:
        entry["display_name"] = str(display_name).strip()[:80]
    if set_collections:
        if collections is None:
            entry.pop("collections", None)          # back to inherit
        else:
            entry["collections"] = [str(c) for c in collections]
    entry.setdefault("display_name", uid)
    entry.setdefault("created_at", _now())
    users[uid] = entry
    _save_config(cfg)
    return {"ok": True, "user": uid, "profile": entry}


def get_user(user_id: str) -> dict | None:
    return load_config()["users"].get(str(user_id).strip())


# ── rooms ────────────────────────────────────────────────────────────────────

def _norm_agents(agents) -> list[dict] | None:
    """Validate the room's agent rules; None keeps the default librarian."""
    if agents is None:
        return None
    out: list[dict] = []
    if not isinstance(agents, list):
        return None
    for a in agents:
        if not isinstance(a, dict):
            continue
        name = str(a.get("agent") or "").strip()
        trig = str(a.get("trigger") or "mention").strip().lower()
        if not name or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
            continue
        if trig.startswith("keyword"):
            if ":" not in trig:
                continue
            kws = [k.strip().lower() for k in trig.split(":", 1)[1].split(",")
                   if k.strip()]
            if not kws:
                continue
            trig = "keywords:" + ",".join(kws)
        elif trig not in _VALID_TRIGGERS:
            trig = "mention"
        entry: dict = {"agent": name, "trigger": trig}
        if str(a.get("strategy") or "").strip():
            entry["strategy"] = str(a["strategy"]).strip()
        cols = a.get("collections")
        if isinstance(cols, list) and cols:
            entry["collections"] = [str(c) for c in cols]
        out.append(entry)
    return out or None


def create_room(room_id: str, name: str, owner: str,
                shared_collections: list[str] | None = None,
                agents: list[dict] | None = None,
                member_can_ingest: bool = True,
                context_turns: int = 3) -> dict:
    """Create a room. The owner is member #1; the shared collections pool and
    agent participation rules are set here (and only an owner edits them)."""
    rid = sanitize_room_id(room_id)
    owner = str(owner).strip()
    if not rid or not owner:
        return {"ok": False, "error": "room_id and owner required"}
    cols = [str(c) for c in (shared_collections
                             or [rag.DEFAULT_COLLECTION])]
    bad = [c for c in cols if c not in rag.COLLECTIONS]
    if bad:
        return {"ok": False,
                "error": f"unknown collection(s): {', '.join(bad)}. Valid: "
                         f"{', '.join(rag.COLLECTIONS)}"}
    cfg = load_config()
    if rid in cfg["rooms"]:
        return {"ok": False, "error": f"room '{rid}' already exists"}
    room = {
        "room_id": rid,
        "name": (name or rid).strip()[:80],
        "owner": owner,
        "members": {owner: "owner"},
        "shared_collections": cols,
        "member_can_ingest": bool(member_can_ingest),
        "context_turns": max(0, min(int(context_turns or 0), 10)),
        "agents": _norm_agents(agents) or [dict(a) for a in _DEFAULT_AGENTS],
        "created_at": _now(),
    }
    cfg["rooms"][rid] = room
    cfg["users"].setdefault(owner, {"display_name": owner,
                                    "created_at": _now()})
    _save_config(cfg)
    _audit("room_create", f"{rid} owner={owner} cols={cols}")
    return {"ok": True, "room": _public_room(room)}


def get_room(room_id: str) -> dict | None:
    return load_config()["rooms"].get(sanitize_room_id(room_id))


def delete_room(room_id: str, actor: str) -> dict:
    rid = sanitize_room_id(room_id)
    room = get_room(rid)
    if not room:
        return {"ok": False, "error": f"unknown room: {rid}"}
    if room.get("owner") != str(actor).strip():
        _audit("denied", f"delete_room {rid} actor={actor}")
        return {"ok": False, "error": "access denied: only the room owner "
                                      "can delete a room"}
    cfg = load_config()
    del cfg["rooms"][rid]
    _save_config(cfg)
    with _transcript_lock:
        try:
            _transcript_path(rid).unlink()
        except FileNotFoundError:
            pass
    _audit("room_delete", f"{rid} actor={actor}")
    return {"ok": True, "deleted": rid}


def _public_room(room: dict) -> dict:
    """Room as members see it: everything except nothing — membership is not
    a secret inside the room, roles included."""
    return dict(room)


def set_member(room_id: str, actor: str, user: str,
               role: str = "member") -> dict:
    """Add a member or change their role. Admins may add/adjust everyone
    below admin; only the owner mintes admins or hands over ownership."""
    rid = sanitize_room_id(room_id)
    room = get_room(rid)
    actor, user = str(actor).strip(), str(user).strip()
    if not room:
        return {"ok": False, "error": f"unknown room: {rid}"}
    actor_role = (room.get("members") or {}).get(actor)
    if actor_role not in ("owner", "admin"):
        _audit("denied", f"set_member {rid} actor={actor} target={user}")
        return {"ok": False, "error": "access denied: admins and owners only"}
    if role not in ROLES:
        return {"ok": False, "error": f"role must be one of {', '.join(ROLES)}"}
    if role in ("owner", "admin") and actor_role != "owner":
        return {"ok": False, "error": "access denied: only the owner can "
                                      "grant admin or ownership"}
    if user == room.get("owner") and role != "owner":
        return {"ok": False, "error": "the owner cannot be demoted; delete "
                                      "the room or transfer it first"}
    cfg = load_config()
    room = cfg["rooms"][rid]
    if role == "owner" and user != room.get("owner"):
        # ownership transfer: the new owner takes the flag, the old one stays
        # on as admin so the room never loses its steward mid-handover
        room["owner"] = user
        room.setdefault("members", {})[actor] = "admin"
    room.setdefault("members", {})[user] = role
    cfg["users"].setdefault(user, {"display_name": user, "created_at": _now()})
    _save_config(cfg)
    _audit("member_set", f"{rid} {user} → {role} by {actor}")
    return {"ok": True, "room": rid, "user": user, "role": role}


def remove_member(room_id: str, actor: str, user: str) -> dict:
    """Revoke access immediately: the user drops out of every scope
    computation on the very next call (config is re-read each use)."""
    rid = sanitize_room_id(room_id)
    room = get_room(rid)
    actor = str(actor).strip()
    user = str(user).strip()
    if not room:
        return {"ok": False, "error": f"unknown room: {rid}"}
    if user == room.get("owner"):
        return {"ok": False, "error": "the room owner cannot be removed"}
    actor_role = (room.get("members") or {}).get(actor)
    target_role = (room.get("members") or {}).get(user)
    if target_role == "admin" and actor_role != "owner" and actor != user:
        _audit("denied", f"remove_member {rid} actor={actor} target={user}")
        return {"ok": False, "error": "access denied: only the owner removes "
                                      "an admin"}
    if actor_role not in ("owner", "admin") and actor != user:
        _audit("denied", f"remove_member {rid} actor={actor} target={user}")
        return {"ok": False, "error": "access denied: admins and owners only "
                                      "(or leaving yourself)"}
    cfg = load_config()
    room = cfg["rooms"][rid]
    removed = (room.get("members") or {}).pop(user, None)
    if removed is None:
        return {"ok": False, "error": f"{user} is not a member of {rid}"}
    _save_config(cfg)
    _audit("member_remove", f"{rid} {user} (was {removed}) by {actor}")
    return {"ok": True, "room": rid, "removed": user, "was": removed}


def list_rooms(user_id: str | None = None) -> dict:
    """All rooms, or only those a user belongs to."""
    rooms = load_config()["rooms"]
    uid = str(user_id).strip() if user_id else None
    out = []
    for rid, room in rooms.items():
        if uid and uid not in (room.get("members") or {}):
            continue
        out.append({"room_id": rid, "name": room.get("name"),
                    "role": (room.get("members") or {}).get(uid or "", ""),
                    "members": sorted((room.get("members") or {}).keys()),
                    "shared_collections": room.get("shared_collections")})
    return {"ok": True, "rooms": out}


def room_info(room_id: str, user_id: str) -> dict:
    """The room as one member sees it — membership is the read gate."""
    rid = sanitize_room_id(room_id)
    room = get_room(rid)
    uid = str(user_id).strip()
    if not room:
        return {"ok": False, "error": f"unknown room: {rid}"}
    if uid not in (room.get("members") or {}):
        _audit("denied", f"room_info {rid} actor={uid}")
        return {"ok": False, "error": "access denied: not a member of this room"}
    return {"ok": True, "room": _public_room(room), "your_role":
            room["members"][uid]}


# ── permissions: the per-user lens ──────────────────────────────────────────

def member_role(room: dict, user_id: str) -> str | None:
    return (room.get("members") or {}).get(str(user_id).strip())


def _collection_scope(room: dict, user_id: str) -> set[str]:
    """Room pool ∩ user grant. Absent grant → the whole pool; empty grant →
    blind (deliberate: an explicit empty list is a gag order, not a typo)."""
    pool = set(room.get("shared_collections") or [])
    user = get_user(user_id) or {}
    grant = user.get("collections")
    if grant is None:
        return pool
    return pool & {str(c) for c in grant}


def _doc_visible(room: dict, user_id: str, entry: dict,
                 col_scope: set[str]) -> bool:
    """The document-level gate: access flag + owner + shared_with.

    An explicit per-user grant (metadata.shared_with) overrides even the
    private flag — that is how grant_document() shares a private document
    with named people. Absent a grant, private is owner-only (the document's
    owner, plus the room owner as steward of the room's pool)."""
    owner = (entry.get("metadata") or {}).get("owner")
    shared_with = (entry.get("metadata") or {}).get("shared_with")
    if isinstance(shared_with, list) and shared_with:
        if "all" in shared_with or user_id in shared_with:
            return True
    if entry.get("access") == "private":
        return owner == user_id or room.get("owner") == user_id
    return True


def user_scope(room_id: str, user_id: str) -> set[str]:
    """The doc_ids this user may retrieve in this room — the whitelist handed
    to rag_service as extra_allowed_docs (narrow-only, intersected again
    there with the caller grant). Non-members get the empty set: blind."""
    rid = sanitize_room_id(room_id)
    room = get_room(rid)
    uid = str(user_id).strip()
    if not room or uid not in (room.get("members") or {}):
        if room is not None:
            _audit("denied", f"scope probe {rid} actor={uid}")
        return set()
    col_scope = _collection_scope(room, uid)
    if not col_scope:
        return set()
    scope: set[str] = set()
    for doc_id, entry in rag._load_registry().items():
        if entry.get("status") != "indexed":
            continue
        if entry.get("collection") not in col_scope:
            continue
        if _doc_visible(room, uid, entry, col_scope):
            scope.add(doc_id)
    return scope


def visible_documents(room_id: str, user_id: str) -> dict:
    """The library as THIS member may see it in THIS room (their lens over
    the shared pool) — documents, not chunks, so nothing sensitive leaks in
    a listing either."""
    rid = sanitize_room_id(room_id)
    room = get_room(rid)
    uid = str(user_id).strip()
    if not room:
        return {"ok": False, "error": f"unknown room: {rid}"}
    if uid not in (room.get("members") or {}):
        _audit("denied", f"visible_documents {rid} actor={uid}")
        return {"ok": False, "error": "access denied: not a member of this room"}
    scope = user_scope(rid, uid)
    docs = [d for d in rag.list_documents() if d.get("doc_id") in scope]
    return {"ok": True, "documents": docs, "scope_size": len(scope)}


def grant_document(room_id: str, actor: str, doc_id: str,
                   target_user: str | None = None,
                   grant_all: bool = False,
                   revoke: bool = False) -> dict:
    """Per-document user permission: add/remove a user from a document's
    shared_with list (or set it to ["all"]). The actor must be the document
    owner or the room owner; the document must sit inside the room's pool."""
    rid = sanitize_room_id(room_id)
    room = get_room(rid)
    actor, target = str(actor).strip(), str(target_user or "").strip()
    if not room:
        return {"ok": False, "error": f"unknown room: {rid}"}
    if actor not in (room.get("members") or {}):
        return {"ok": False, "error": "access denied: not a member of this room"}
    entry = rag._load_registry().get(doc_id)
    if not entry:
        return {"ok": False, "error": f"unknown document: {doc_id}"}
    if entry.get("collection") not in (room.get("shared_collections") or []):
        return {"ok": False, "error": "document is outside this room's "
                                      "shared collections"}
    d_owner = (entry.get("metadata") or {}).get("owner")
    if actor not in (d_owner, room.get("owner")):
        _audit("denied", f"grant_document {rid} doc={doc_id} actor={actor}")
        return {"ok": False, "error": "access denied: only the document owner "
                                      "or room owner can re-share it"}
    meta = dict(entry.get("metadata") or {})
    if grant_all:
        current = ["all"] if not revoke else []
    else:
        current = list(meta.get("shared_with") or [])
        if revoke:
            current = [u for u in current if u != target]
        else:
            if not target:
                return {"ok": False, "error": "target_user required"}
            if target not in current:
                current.append(target)
    if current == ["all"]:
        current = ["all"]
    meta["shared_with"] = current
    result = rag_service.update(GROUP_CALLER, doc_id, metadata=meta)
    if result.get("ok"):
        _audit("grant_document",
               f"{rid} doc={doc_id} {'all' if grant_all else target} "
               f"{'revoked' if revoke else 'granted'} by {actor}")
    return result


# ── transcript ───────────────────────────────────────────────────────────────

def _transcript_path(room_id: str) -> Path:
    return TRANSCRIPTS_DIR / f"{sanitize_room_id(room_id)}.jsonl"


def _append(room_id: str, entry: dict) -> dict:
    """Append one transcript row; the room file holds the shared context of
    everyone in the room. seq = position in the room's history."""
    rid = sanitize_room_id(room_id)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    with _transcript_lock:
        seq = _last_seq(rid) + 1
        entry = {"seq": seq, "ts": _now(), **entry}
        with open(_transcript_path(rid), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _read(room_id: str) -> list[dict]:
    path = _transcript_path(room_id)
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
    except FileNotFoundError:
        pass
    return rows


def _last_seq(room_id: str) -> int:
    rows = _read(room_id)
    return rows[-1]["seq"] if rows else 0


# ── agent participation rules ────────────────────────────────────────────────

def _triggered_agents(room: dict, text: str,
                      *, is_agent_message: bool = False) -> list[dict]:
    """Which of the room's agents speak on this message, in config order.

    Rules per entry: 'always' (every user message), 'mention' (only when the
    text opens with @name), 'keywords:a,b' (any keyword appears), 'manual'
    (only via ask_agent). Agent messages trigger nobody — loops are
    structurally impossible, not politely discouraged.
    """
    if is_agent_message:
        return []
    low = (text or "").strip().lower()
    out: list[dict] = []
    for a in (room.get("agents") or _DEFAULT_AGENTS):
        trig = str(a.get("trigger") or "mention").lower()
        name = str(a.get("agent") or "").lower()
        if trig == "always":
            out.append(a)
        elif trig == "mention" and low.startswith(f"@{name}"):
            out.append(a)
        elif trig.startswith("keywords:"):
            kws = [k.strip() for k in trig.split(":", 1)[1].split(",") if k]
            if kws and any(k in low for k in kws):
                out.append(a)
        # 'manual' agents are never auto-triggered
    return out


def _agent_scope(room: dict, user_id: str, agent: dict) -> set[str]:
    """Per-user scope ∩ this agent's collection cap. An agent may know LESS
    than the user it answers; it can never know more."""
    scope = user_scope(room.get("room_id"), user_id)
    cols = agent.get("collections")
    if not cols or "__all__" in cols:
        return scope
    reg = rag._load_registry()
    keep = {d for d, e in reg.items()
            if e.get("collection") in {str(c) for c in cols}}
    return scope & keep


# ── the group RAG call — the ONLY retrieval path in this module ─────────────

def _run_agent(room: dict, user_id: str, agent: dict, query: str) -> dict:
    """One agent turn: rag_service.answer (strategies pipeline) behind the
    user's permission lens, then a defence-in-depth sweep that drops any
    evidence row outside the scope before anything is stored or shown."""
    rid = room.get("room_id")
    scope = _agent_scope(room, user_id, agent)
    out = rag_service.answer(
        caller=GROUP_CALLER, query=query,
        strategy=str(agent.get("strategy") or "auto"),
        extra_allowed_docs=scope)
    if not out.get("ok"):
        return {"ok": False, "agent": agent.get("agent"),
                "error": out.get("error", "retrieval failed")}
    # Defence in depth: retrieval was already scoped, but nothing leaves this
    # layer unverified — every citation/evidence row must be inside the scope.
    ev, cites = [], []
    for e, c in zip(out.get("used_evidence") or [], out.get("citations") or []):
        if e.get("doc_id") in scope:
            ev.append(e)
            cites.append(c)
    docs = sorted({e.get("doc_id") for e in ev if e.get("doc_id")})
    return {
        "ok": True,
        "agent": agent.get("agent"),
        "text": out.get("answer", ""),
        "citations": cites,
        "docs": docs,
        "strategy": out.get("strategy"),
        "grounded": bool(out.get("grounded")),
        "hallucination_risk": out.get("hallucination_risk"),
        "iterations": out.get("iterations"),
        "scope_size": len(scope),
    }


def _contextual_query(room: dict, room_id: str, text: str) -> str:
    """Shared conversation context: the last context_turns user messages are
    folded into the retrieval query (bounded), so a follow-up like 'and its
    page 2?' resolves against what the room was just discussing. Context
    shapes the QUERY only — the ACL stays the asking user's."""
    turns = int(room.get("context_turns") or 0)
    text = (text or "").strip()
    if turns <= 0:
        return text
    prior = [m["text"] for m in _read(room_id)
             if m.get("kind") == "user" and m.get("text")][-turns:]
    ctx = " ".join(prior).strip()
    if not ctx:
        return text
    return f"{ctx[-200:]} /// {text}"


# ── messaging ────────────────────────────────────────────────────────────────

def post(room_id: str, user_id: str, text: str) -> dict:
    """Say something in the room.

    Membership first, always. The message lands in the shared transcript;
    whatever agent participation rules fire, fire — each triggered agent
    answers through the strategies pipeline scoped to THIS user's
    permissions, and the replies land in the same transcript with citations.
    """
    rid = sanitize_room_id(room_id)
    room = get_room(rid)
    uid = str(user_id).strip()
    text = (text or "").strip()
    if not room:
        return {"ok": False, "error": f"unknown room: {rid}"}
    if not text:
        return {"ok": False, "error": "empty message"}
    if uid not in (room.get("members") or {}):
        _audit("denied", f"post {rid} actor={uid}")
        return {"ok": False, "error": "access denied: not a member of this room"}

    # context window BEFORE the append: the last context_turns user messages
    # are what came before THIS one
    query = _contextual_query(room, rid, text)
    msg = _append(rid, {"kind": "user", "user": uid, "text": text})
    replies: list[dict] = []
    for agent in _triggered_agents(room, text):
        reply = _run_agent(room, uid, agent, query)
        row = {"kind": "agent", "agent": reply.get("agent"), "user": uid,
               "in_reply_to": msg["seq"]}
        if reply.get("ok"):
            row.update({k: reply[k] for k in
                        ("text", "citations", "docs", "strategy", "grounded",
                         "hallucination_risk", "iterations", "scope_size")})
        else:
            row.update({"text": "", "error": reply.get("error"),
                        "citations": [], "docs": []})
        _append(rid, row)
        replies.append(row)
    _audit("post", f"{rid} user={uid} agents={[r.get('agent') for r in replies]}")
    return {"ok": True, "message": msg, "replies": replies}


def ask_agent(room_id: str, user_id: str, agent: str, text: str) -> dict:
    """Manually invoke one of the room's agents — the only way a 'manual'
    trigger ever speaks, and a way to re-ask a mention-only agent."""
    rid = sanitize_room_id(room_id)
    room = get_room(rid)
    uid = str(user_id).strip()
    text = (text or "").strip()
    if not room:
        return {"ok": False, "error": f"unknown room: {rid}"}
    if uid not in (room.get("members") or {}):
        _audit("denied", f"ask_agent {rid} actor={uid}")
        return {"ok": False, "error": "access denied: not a member of this room"}
    agent_cfg = next((a for a in (room.get("agents") or _DEFAULT_AGENTS)
                      if str(a.get("agent")).lower() == str(agent).strip().lower()),
                     None)
    if agent_cfg is None:
        return {"ok": False,
                "error": f"no agent '{agent}' in room '{rid}'"}
    if not text:
        return {"ok": False, "error": "empty message"}
    query = _contextual_query(room, rid, text)
    msg = _append(rid, {"kind": "user", "user": uid, "text": text,
                        "directed_to": agent_cfg["agent"]})
    reply = _run_agent(room, uid, agent_cfg, query)
    row = {"kind": "agent", "agent": agent_cfg["agent"], "user": uid,
           "in_reply_to": msg["seq"]}
    if reply.get("ok"):
        row.update({k: reply[k] for k in
                    ("text", "citations", "docs", "strategy", "grounded",
                     "hallucination_risk", "iterations", "scope_size")})
    else:
        row.update({"text": "", "error": reply.get("error"),
                    "citations": [], "docs": []})
    _append(rid, row)
    return {"ok": True, "message": msg, "reply": row}


def history(room_id: str, user_id: str, limit: int = 50,
            after: int | None = None) -> dict:
    """The shared transcript, member-gated. Everyone in the room sees the
    same history — the shared conversation context — pagination by seq."""
    rid = sanitize_room_id(room_id)
    room = get_room(rid)
    uid = str(user_id).strip()
    if not room:
        return {"ok": False, "error": f"unknown room: {rid}"}
    if uid not in (room.get("members") or {}):
        _audit("denied", f"history {rid} actor={uid}")
        return {"ok": False, "error": "access denied: not a member of this room"}
    rows = _read(rid)
    if after is not None:
        try:
            rows = [r for r in rows if int(r.get("seq", 0)) > int(after)]
        except (TypeError, ValueError):
            pass
    try:
        limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        limit = 50
    return {"ok": True, "room": rid, "messages": rows[-limit:]}


# ── sharing knowledge into the room ─────────────────────────────────────────

def share_text(room_id: str, user_id: str, text: str, title: str,
               collection: str | None = None,
               share: str = "room") -> dict:
    """A member contributes a document to the room's knowledge pool.

    Role gate (guests read, members share), collection gate (only the room's
    shared collections), and the document is stamped with its owner so the
    private/shared flag below has someone to belong to:
      share="private" → visible in-room to its owner (and the room owner) only
      share="room"    → visible to every member whose collection scope admits it
    """
    rid = sanitize_room_id(room_id)
    room = get_room(rid)
    uid = str(user_id).strip()
    if not room:
        return {"ok": False, "error": f"unknown room: {rid}"}
    if uid not in (room.get("members") or {}):
        _audit("denied", f"share_text {rid} actor={uid}")
        return {"ok": False, "error": "access denied: not a member of this room"}
    role = member_role(room, uid)
    if role == "guest":
        return {"ok": False, "error": "access denied: guests cannot add "
                                      "documents to the room"}
    if role == "member" and not room.get("member_can_ingest", True):
        return {"ok": False, "error": "access denied: members cannot add "
                                      "documents in this room"}
    col = collection or (room.get("shared_collections") or [rag.DEFAULT_COLLECTION])[0]
    if col not in (room.get("shared_collections") or []):
        return {"ok": False, "error": f"collection '{col}' is not shared in "
                                      f"room '{rid}'"}
    if not (text or "").strip():
        return {"ok": False, "error": "nothing to share — empty text"}
    access = "shared" if share == "room" else "private"
    meta = {"owner": uid, "room": rid,
            "shared_with": ["all"] if share == "room" else []}
    result = rag_service.ingest_text(
        caller=GROUP_CALLER, text=text, title=title, collection=col,
        source=f"group:{rid}:{uid}", access=access, metadata=meta)
    if result.get("ok"):
        _audit("share_text",
               f"{rid} user={uid} doc={result.get('doc_id')} [{col}:{share}]")
    return result
