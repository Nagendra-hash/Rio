"""
tests/test_rag_group.py — PHASE 19: group chat RAG over the existing stack.

Everything runs against a TEMP knowledge directory + temp groups config +
temp transcripts, with a FAKE embedder and the LLM ladder forced offline
(local strategies + extractive answerer). No API calls, no real rooms.

What this proves (the Phase 19 deliverable's acceptance flow):

  1. ROOMS & ROLES      — create/join, role ranks (guest < member < admin <
                          owner), who may manage whom, ownership transfer.
  2. MEMBERSHIP GATE    — a non-member is blind: no post, no history, no
                          documents, empty scope. This is the perimeter.
  3. SHARED COLLECTIONS — the room pool; members retrieve from it; a member
                          with an explicit per-user collection grant sees the
                          INTERSECTION only; an empty grant is blind.
  4. DOCUMENT PERMISSIONS — private docs are owner-only in the room;
                          shared_with lists grant per-user access; grants and
                          revocations take effect on the next call.
  5. THE BIG ONE        — a user can NEVER retrieve a document they are not
                          authorized for: not via post/agent answers, not via
                          direct scoped search, not via visible_documents.
  6. NARROW-ONLY SCOPE  — extra_allowed_docs is intersected with the caller
                          grant in rag_service: the group layer can shrink a
                          search, never widen it.
  7. CITATIONS          — agent replies carry source citations and doc_ids,
                          every one inside the asking user's scope; the
                          defence-in-depth sweep drops anything else.
  8. AGENT RULES        — always / mention / keywords / manual triggers;
                          agent messages never trigger agents; an agent with
                          a collection cap knows less than the user it
                          answers, never more.
  9. SHARED CONTEXT     — the room transcript is the shared context; the last
                          context_turns user messages shape the next query
                          (never the ACL); history is member-gated + paginated.
 10. SHARING             — guests cannot add documents; members share into
                          the room's collections; out-of-pool collections are
                          refused.

Run:  .venv/bin/python -m pytest tests/test_rag_group.py -q
"""
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import core.rag as rag  # noqa: E402
from core import rag_service  # noqa: E402
from core import rag_group  # noqa: E402

# distinctive vocab: doc tokens never appear in other docs. Long enough
# (25+ meaningful words) to sail past the chart-page vision detector.
SECRET = ("the garnet falcon protocol requires rotating the amber cipher "
          "every twelve hours and logging every rotation in the vault ledger. "
          "any officer who skips a rotation must report the gap to the "
          "garnet council within one day of the missed amber cipher change")
SHARED = ("the copper harbor manual says every shipment needs a signed "
          "manifest and the manifest must list the harbor master by name. "
          "ships without a signed manifest wait at the copper harbor gate "
          "until the harbor master approves the shipment in person")


@pytest.fixture(autouse=True)
def sandbox(monkeypatch):
    """A FRESH knowledge dir + callers/groups/transcripts for every test.

    Same trick as tests/test_rag_service.py: the modules keep their paths as
    module globals, so each test repoints them instead of importing once."""
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-rag-group-test-"))
    rag.KB_DIR = tmp
    rag.LIBRARY_DIR = tmp / "library"
    rag.CHUNKS_FILE = tmp / "chunks.jsonl"
    rag.VECTORS_FILE = tmp / "vectors.npz"
    rag.REGISTRY_FILE = tmp / "registry.json"
    rag.AUDIT_FILE = tmp / "audit.log"
    rag_service.CALLERS_FILE = tmp / "rag_callers.json"
    rag_group.GROUPS_FILE = tmp / "rag_groups.json"
    rag_group.TRANSCRIPTS_DIR = tmp / "transcripts"
    (tmp / "rag_callers.json").write_text(json.dumps({
        "group_chat": {"collections": ["__all__"], "can_write": True},
        # a deliberately narrow caller, to prove the intersection rule
        "restricted": {"collections": ["research"], "can_write": False},
    }), encoding="utf-8")

    # the LLM ladder is forced OFFLINE: local strategies + extractive
    # answerer only — deterministic, no network, ever.
    import core.rag_strategies.llm as rs_llm
    monkeypatch.setattr(rs_llm, "llm_ready", lambda: False)

    # fake embedder: deterministic bag-of-words (same shape as
    # tests/test_rag_service.py) — shared words attract, distinct words don't
    import core as core_pkg

    class _FakeGemini:
        def embed(self, texts, is_query=False, **kw):
            single = isinstance(texts, str)
            outs = []
            for t in ([texts] if single else list(texts)):
                v = np.zeros(rag.EMBED_DIM, dtype=np.float32)
                for w in set(t.lower().split()):
                    seed = abs(hash(w)) % (rag.EMBED_DIM - 8)
                    v[seed:seed + 8] += 1.0
                n = float(np.linalg.norm(v))
                outs.append(v / n if n > 0 else v)
            return outs[0] if single else outs

    monkeypatch.setattr(core_pkg, "gemini", _FakeGemini(), raising=False)
    yield tmp


def _room(name="Deal Room", owner="alice", cols=("research",), agents=None,
          context_turns=0, member_can_ingest=True):
    r = rag_group.create_room(name.lower().replace(" ", "_"), name, owner,
                              shared_collections=list(cols), agents=agents,
                              context_turns=context_turns,
                              member_can_ingest=member_can_ingest)
    assert r["ok"], r
    return r["room"]["room_id"]


def _share(room, user, text, title, share="room", collection=None):
    r = rag_group.share_text(room, user, text, title, share=share,
                             collection=collection)
    assert r["ok"], r
    return r["doc_id"]


def _doc_id_of(title):
    for d in rag.list_documents():
        if d.get("title") == title:
            return d["doc_id"]
    return None


# ── 1. rooms & roles ─────────────────────────────────────────────────────────

def test_room_creation_and_roles():
    rid = _room()
    assert rag_group.set_member(rid, "alice", "bob", "member")["ok"]
    assert rag_group.set_member(rid, "alice", "carol", "guest")["ok"]
    assert rag_group.set_member(rid, "alice", "dave", "admin")["ok"]

    # a member cannot manage; an admin can add members but not admins
    assert not rag_group.set_member(rid, "bob", "eve", "member")["ok"]
    assert rag_group.set_member(rid, "dave", "frank", "member")["ok"]
    r = rag_group.set_member(rid, "dave", "frank", "admin")
    assert not r["ok"] and "owner" in r["error"]

    # the owner cannot be removed or demoted; ownership can be transferred
    assert not rag_group.remove_member(rid, "dave", "alice")["ok"]
    assert rag_group.set_member(rid, "alice", "dave", "owner")["ok"]
    room = rag_group.get_room(rid)
    assert room["owner"] == "dave" and room["members"]["alice"] == "admin"

    # unknown rooms/collections are refused
    assert not rag_group.create_room("x", "X", "alice",
                                     shared_collections=["not_real"])["ok"]
    assert not rag_group.set_member("nope", "alice", "bob")["ok"]


def test_non_admin_cannot_delete_room():
    rid = _room()
    assert rag_group.set_member(rid, "alice", "bob", "member")["ok"]
    assert not rag_group.delete_room(rid, "bob")["ok"]
    assert rag_group.delete_room(rid, "alice")["ok"]
    assert rag_group.get_room(rid) is None


# ── 2. the membership gate ───────────────────────────────────────────────────

def test_non_member_is_blind():
    rid = _room()
    _share(rid, "alice", SHARED, "harbor manual")
    for fn in (lambda: rag_group.post(rid, "eve", "hello"),
               lambda: rag_group.history(rid, "eve"),
               lambda: rag_group.visible_documents(rid, "eve"),
               lambda: rag_group.ask_agent(rid, "eve", "librarian", "q"),
               lambda: rag_group.share_text(rid, "eve", "t", "T")):
        r = fn()
        assert not r["ok"] and "not a member" in r["error"]
    assert rag_group.user_scope(rid, "eve") == set()


# ── 3. shared collections & per-user narrowing ──────────────────────────────

def test_shared_collections_and_user_narrowing():
    rid = _room(cols=("research", "market_rules"))
    rid = rag_group.get_room(rid)["room_id"]
    _share(rid, "alice", SHARED, "harbor manual", collection="research")
    _share(rid, "alice", SECRET.replace("garnet falcon", "market compass"),
           "compass memo", collection="market_rules")

    # member without an explicit grant → the whole room pool
    rag_group.set_member(rid, "alice", "bob", "member")
    assert len(rag_group.user_scope(rid, "bob")) == 2

    # explicit grant narrows to the intersection; empty grant is blind
    rag_group.upsert_user("bob", collections=["research"],
                          set_collections=True)
    scope = rag_group.user_scope(rid, "bob")
    assert len(scope) == 1
    assert _doc_id_of("harbor manual") in scope
    assert _doc_id_of("compass memo") not in scope

    rag_group.upsert_user("bob", collections=[], set_collections=True)
    assert rag_group.user_scope(rid, "bob") == set()

    # grant lifted → back to the pool
    rag_group.upsert_user("bob", collections=None, set_collections=True)
    assert len(rag_group.user_scope(rid, "bob")) == 2


# ── 4/5. document permissions + the big one ─────────────────────────────────

def test_private_document_is_owner_only():
    rid = _room()
    rag_group.set_member(rid, "alice", "bob", "member")
    secret_doc = _share(rid, "alice", SECRET, "falcon protocol",
                        share="private")
    shared_doc = _share(rid, "alice", SHARED, "harbor manual")

    # the listing honours it
    bob_docs = {d["doc_id"] for d in
                rag_group.visible_documents(rid, "bob")["documents"]}
    assert shared_doc in bob_docs and secret_doc not in bob_docs

    # scoped search: the private doc never appears for bob, but does for alice
    # (the shared doc may surface too — it IS in bob's scope; that is not a leak)
    q = "what does the garnet falcon protocol require?"
    bob_hits = rag_service.search(caller="group_chat", query=q,
                                  extra_allowed_docs=rag_group.user_scope(rid, "bob"))
    assert bob_hits["ok"]
    assert secret_doc not in {h["doc_id"] for h in bob_hits["results"]}
    alice_hits = rag_service.search(caller="group_chat", query=q,
                                    extra_allowed_docs=rag_group.user_scope(rid, "alice"))
    assert any(h["doc_id"] == secret_doc for h in alice_hits["results"])

    # the agent answer cannot leak it either (the in-scope harbor doc may
    # legitimately be cited — the private doc may not)
    out = rag_group.ask_agent(rid, "bob", "librarian", q)
    assert out["ok"]
    reply = out["reply"]
    assert secret_doc not in reply["docs"]
    assert "garnet" not in (reply.get("text") or "")

    # a doc_id outside the scope is refused outright at the service layer
    denied = rag_service.search(caller="group_chat", query=q, doc_id=secret_doc,
                                extra_allowed_docs=rag_group.user_scope(rid, "bob"))
    assert not denied["ok"] and "access denied" in denied["error"]


def test_grant_and_revoke_take_effect_immediately():
    rid = _room()
    rag_group.set_member(rid, "alice", "bob", "member")
    secret_doc = _share(rid, "alice", SECRET, "falcon protocol",
                        share="private")

    # before: bob blind
    assert secret_doc not in rag_group.user_scope(rid, "bob")
    # grant: bob sees it on the very next call
    assert rag_group.grant_document(rid, "alice", secret_doc,
                                    target_user="bob")["ok"]
    assert secret_doc in rag_group.user_scope(rid, "bob")
    hits = rag_service.search(caller="group_chat",
                              query="amber cipher rotation",
                              extra_allowed_docs=rag_group.user_scope(rid, "bob"))
    assert any(h["doc_id"] == secret_doc for h in hits["results"])
    # revoke: blind again
    assert rag_group.grant_document(rid, "alice", secret_doc,
                                    target_user="bob", revoke=True)["ok"]
    assert secret_doc not in rag_group.user_scope(rid, "bob")

    # only the doc owner / room owner can re-share; bob cannot self-grant
    assert not rag_group.grant_document(rid, "bob", secret_doc,
                                        target_user="bob")["ok"]
    # grant_all: every member sees it, still only through their collections
    assert rag_group.grant_document(rid, "alice", secret_doc,
                                    grant_all=True)["ok"]
    assert secret_doc in rag_group.user_scope(rid, "bob")


# ── 6. the scope passed down can never widen the caller grant ───────────────

def test_extra_allowed_docs_cannot_widen_caller_grant():
    # a document OUTSIDE the 'restricted' caller's collections
    _share_roomless = rag_service.ingest_text(
        caller="jarvis", text=SECRET, title="compass memo",
        collection="market_rules")
    assert _share_roomless["ok"]
    outside_doc = _share_roomless["doc_id"]

    hits = rag_service.search(caller="restricted",
                              query="amber cipher rotation",
                              extra_allowed_docs={outside_doc})
    assert hits["ok"] and not hits["results"]      # narrowed to nothing

    # the same trick through the strategies pipeline
    ans = rag_service.answer(caller="restricted",
                             query="what does the amber cipher need?",
                             strategy="local_adaptive",
                             extra_allowed_docs={outside_doc})
    assert ans["ok"] and not ans.get("used_evidence")

    # inside the grant, the intersection still returns the doc
    in_doc = rag_service.ingest_text(caller="jarvis", text=SHARED,
                                     title="harbor manual",
                                     collection="research")["doc_id"]
    hits = rag_service.search(caller="restricted", query="signed manifest",
                              extra_allowed_docs={in_doc, outside_doc})
    assert [h["doc_id"] for h in hits["results"]] == [in_doc]


# ── 7. citations ─────────────────────────────────────────────────────────────

def test_agent_reply_carries_in_scope_citations():
    rid = _room()
    rag_group.set_member(rid, "alice", "bob", "member")
    _share(rid, "alice", SHARED, "harbor manual")

    out = rag_group.ask_agent(rid, "bob", "librarian",
                              "what does the copper harbor manual require "
                              "for a shipment?")
    assert out["ok"]
    reply = out["reply"]
    assert reply["agent"] == "librarian"
    assert reply["docs"], "expected at least one cited doc"
    assert all(d in rag_group.user_scope(rid, "bob") for d in reply["docs"])
    assert reply["citations"]
    assert "harbor manual" in reply["citations"][0]
    assert "[research]" in reply["citations"][0]   # collection in the citation
    assert reply["strategy"].startswith("local_")  # offline twin ran


def test_defence_in_depth_drops_out_of_scope_evidence(monkeypatch):
    """Even if the retriever were compromised, nothing out of scope leaves
    the group layer: the sweep drops stray evidence before storing."""
    rid = _room()
    room = rag_group.get_room(rid)
    planted = {"ok": True, "strategy": "local_adaptive", "answer": "x",
               "citations": ["Evil doc, p.1 [research]"],
               "used_evidence": [{"doc_id": "evil123", "text": "stolen",
                                  "source": "Evil doc", "page": 1}],
               "grounded": True, "hallucination_risk": "low",
               "iterations": 1}
    import core.rag_group as rg
    orig = rag_service.answer
    monkey_hits = {}
    def fake_answer(**kw):
        monkey_hits.update(kw)
        return dict(planted)
    monkeypatch.setattr(rag_service, "answer", fake_answer)
    out = rg._run_agent(room, "alice", {"agent": "librarian"}, "q")
    assert out["ok"] and out["docs"] == [] and out["citations"] == []
    assert out["text"] == "" or "evil" not in out["text"].lower()
    # the scope WAS passed down to the service call
    assert monkey_hits.get("extra_allowed_docs") == set()
    monkeypatch.setattr(rag_service, "answer", orig)


# ── 8. agent participation rules ────────────────────────────────────────────

def test_agent_trigger_rules():
    agents = [
        {"agent": "auditor", "trigger": "mention"},
        {"agent": "tickerbot", "trigger": "keywords:earnings,guidance"},
        {"agent": "archivist", "trigger": "manual"},
    ]
    rid = _room(agents=agents)
    _share(rid, "alice", SHARED, "harbor manual")

    # plain text: nobody fires (no always-agent in this room)
    r = rag_group.post(rid, "alice", "good morning team")
    assert r["ok"] and r["replies"] == []

    # mention: only the auditor
    r = rag_group.post(rid, "alice", "@auditor what does the manifest need?")
    assert [a["agent"] for a in r["replies"]] == ["auditor"]

    # keyword: only the tickerbot
    r = rag_group.post(rid, "alice", "any thoughts on earnings today?")
    assert [a["agent"] for a in r["replies"]] == ["tickerbot"]

    # manual agents never auto-fire but answer on demand
    r = rag_group.ask_agent(rid, "alice", "archivist", "archive this room")
    assert r["ok"] and r["reply"]["agent"] == "archivist"

    # the transcript holds user + agent rows; agent rows never trigger agents
    hist = rag_group.history(rid, "alice", limit=100)["messages"]
    kinds = [m["kind"] for m in hist]
    assert kinds.count("agent") == 3
    for m in hist:
        if m["kind"] == "agent":
            assert m["agent"] in {"auditor", "tickerbot", "archivist"}

    # unknown agent → refused; direct ask to a mention agent is fine
    assert not rag_group.ask_agent(rid, "alice", "ghost", "hi")["ok"]


def test_always_agent_answers_every_message_and_loops_never():
    rid = _room(agents=[{"agent": "librarian", "trigger": "always"}])
    r = rag_group.post(rid, "alice", "hello there")
    assert [a["agent"] for a in r["replies"]] == ["librarian"]
    hist = rag_group.history(rid, "alice")["messages"]
    # exactly one agent row per user message — agent replies did not re-trigger
    assert len([m for m in hist if m["kind"] == "user"]) == 1
    assert len([m for m in hist if m["kind"] == "agent"]) == 1


def test_agent_collection_cap_narrows_below_the_user():
    agents = [{"agent": "legal", "trigger": "always",
               "collections": ["legal_docs"]}]
    rid = _room(cols=("research", "legal_docs"), agents=agents)
    rid = rag_group.get_room(rid)["room_id"]
    _share(rid, "alice", SHARED, "harbor manual", collection="research")
    _share(rid, "alice", SECRET, "falcon protocol", collection="legal_docs")
    room = rag_group.get_room(rid)

    # the USER sees both docs...
    assert len(rag_group.user_scope(rid, "alice")) == 2
    # ...but the capped agent cannot cite the research doc
    out = rag_group.ask_agent(rid, "alice", "legal",
                              "what does the copper harbor manual require?")
    harbor_doc = _doc_id_of("harbor manual")
    assert harbor_doc not in out["reply"]["docs"]
    assert set(out["reply"]["docs"]) <= {_doc_id_of("falcon protocol")}
    out2 = rag_group.ask_agent(rid, "alice", "legal",
                               "what does the garnet falcon protocol require?")
    assert out2["reply"]["docs"] == [_doc_id_of("falcon protocol")]
    assert room["agents"][0]["collections"] == ["legal_docs"]


# ── 9. shared context & history ──────────────────────────────────────────────

def test_contextual_query_folds_recent_user_messages():
    rid = _room(context_turns=2)
    rid = rag_group.get_room(rid)["room_id"]
    rag_group.set_member(rid, "alice", "bob", "member")
    rag_group.post(rid, "alice", "we are reviewing the amber cipher policy")
    rag_group.post(rid, "bob", "and the manifest checklist")
    room = rag_group.get_room(rid)
    q = rag_group._contextual_query(room, rid, "what about page two?")
    # the last two user messages shape the query; the new text rides at the end
    assert "amber cipher" in q and "manifest checklist" in q
    assert q.endswith("what about page two?")
    # a member's follow-up post gets the same treatment, excluding itself
    out = rag_group.post(rid, "bob", "and the falcon ledger?")
    assert out["ok"]
    # context shapes the query, never the ACL: bob's scope stays bob's
    rag_group.upsert_user("bob", collections=[], set_collections=True)
    assert rag_group.user_scope(rid, "bob") == set()


def test_history_is_member_gated_and_paginated():
    rid = _room()
    for i in range(6):
        rag_group.post(rid, "alice", f"note {i}")   # no agents configured →
        # default librarian fires; history grows by 2 per post
    full = rag_group.history(rid, "alice", limit=100)["messages"]
    assert len(full) >= 6
    seqs = [m["seq"] for m in full]
    assert seqs == sorted(seqs)
    page = rag_group.history(rid, "alice", limit=2)["messages"]
    assert page == full[-2:]
    after = full[2]["seq"]
    rest = rag_group.history(rid, "alice", after=after)["messages"]
    assert all(m["seq"] > after for m in rest)
    assert not rag_group.history(rid, "eve")["ok"]


# ── 10. sharing documents into the room ─────────────────────────────────────

def test_share_rules():
    rid = _room()
    rag_group.set_member(rid, "alice", "bob", "member")
    rag_group.set_member(rid, "alice", "carol", "guest")

    # guests cannot share
    assert not rag_group.share_text(rid, "carol", SHARED, "guest note")["ok"]
    # members share into room collections, stamped with their ownership
    doc = _share(rid, "bob", SHARED, "bob note")
    entry = rag._load_registry()[doc]
    assert entry["metadata"]["owner"] == "bob"
    assert entry["access"] == "shared" and entry["collection"] == "research"

    # private share → owner-only even though the room shares the collection
    priv = _share(rid, "bob", SECRET, "bob secret", share="private")
    assert priv in rag_group.user_scope(rid, "bob")

    # out-of-pool collections are refused
    assert not rag_group.share_text(rid, "bob", SHARED, "x",
                                    collection="legal_docs")["ok"]

    # private share → owner-only: a plain member and a guest cannot see it,
    # the sharer and the room owner (steward of the pool) can
    rag_group.set_member(rid, "alice", "dave", "member")
    assert priv not in rag_group.user_scope(rid, "dave")
    assert priv not in rag_group.user_scope(rid, "carol")
    assert priv in rag_group.user_scope(rid, "bob")       # its owner
    assert priv in rag_group.user_scope(rid, "alice")     # room owner

    # a member can be blocked from sharing by room policy (owners still can)
    rid2 = _room(name="Locked Room", member_can_ingest=False)
    rid2 = rag_group.get_room(rid2)["room_id"]
    rag_group.set_member(rid2, "alice", "frank", "member")
    assert not rag_group.share_text(rid2, "frank", SHARED, "y")["ok"]
    assert rag_group.share_text(rid2, "alice", SHARED, "owner note")["ok"]

    # empty text is refused
    assert not rag_group.share_text(rid, "bob", "  ", "empty")["ok"]


def test_room_listing_and_info():
    rid = _room()
    rag_group.set_member(rid, "alice", "bob", "member")
    rooms = rag_group.list_rooms("bob")["rooms"]
    assert [r["room_id"] for r in rooms] == [rid]
    assert not rag_group.list_rooms("eve")["rooms"]
    info = rag_group.room_info(rid, "bob")
    assert info["ok"] and info["your_role"] == "member"
    assert not rag_group.room_info(rid, "eve")["ok"]


def test_audit_trail_records_group_actions():
    rid = _room()
    rag_group.set_member(rid, "alice", "bob", "member")
    rag_group.post(rid, "bob", "hello")
    log = rag.AUDIT_FILE.read_text(encoding="utf-8")
    assert "group:room_create" in log
    assert "group:member_set" in log
    assert "group:post" in log


# ── 11. the HTTP surface ─────────────────────────────────────────────────────

def test_group_rest_flow_and_auth(monkeypatch):
    """The dashboard endpoints wire the same permission model end to end:
    create → share → ask → cite, a non-member gets 403, and so does an
    unauthenticated request."""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from fastapi import FastAPI
    from dashboard import rag_api

    token = "group-test-token"
    auth = {"Authorization": f"Bearer {token}"}
    app = FastAPI()
    rag_api.register(app, lambda req: (req.headers.get("authorization", "")
                                       .removeprefix("Bearer ").strip() == token))
    c = fastapi_testclient.TestClient(app)

    r = c.post("/api/rag/group/rooms", json={
        "room_id": "war_room", "name": "War Room", "owner": "alice",
        "shared_collections": ["research"]}, headers=auth)
    assert r.status_code == 200 and r.json()["ok"]

    r = c.post("/api/rag/group/share-text", json={
        "room_id": "war_room", "user": "alice", "title": "harbor manual",
        "text": SHARED, "share": "room"}, headers=auth)
    assert r.status_code == 200 and r.json()["ok"]

    # bob joins, asks, gets a cited answer over HTTP
    assert c.post("/api/rag/group/members", json={
        "room_id": "war_room", "actor": "alice", "user": "bob",
        "role": "member"}, headers=auth).json()["ok"]
    r = c.post("/api/rag/group/message", json={
        "room_id": "war_room", "user": "bob",
        "text": "what does the copper harbor manual require for a shipment?"},
        headers=auth)
    assert r.status_code == 200
    replies = r.json()["replies"]
    assert replies and replies[0]["citations"]

    # eve is not a member → 403 on message and history
    for resp in (c.post("/api/rag/group/message", json={
            "room_id": "war_room", "user": "eve", "text": "hi"},
            headers=auth),
            c.get("/api/rag/group/history",
                  params={"room_id": "war_room", "user": "eve"},
                  headers=auth)):
        assert resp.status_code == 403

    # no token → 401 everywhere
    assert c.get("/api/rag/group/rooms").status_code == 401
    assert c.post("/api/rag/group/message", json={
        "room_id": "war_room", "user": "bob", "text": "x"}).status_code == 401
