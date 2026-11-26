# RAG Group Chat — architecture checkpoint (Phase 19)

> Code: `core/rag_group.py` · Service hook: `core/rag_service.py` · REST: `dashboard/rag_api.py`
> Tests: `tests/test_rag_group.py` (18, fully offline) · State: `config/rag_groups.json` + `memory/rag_group_chat/<room>.jsonl`

This document is the checkpoint for Phase 19 of the RAG roadmap: **group
chat** — many people sharing one room, one transcript and one knowledge
pool, with per-user access control enforced on every retrieval.

## 1. One retrieval stack, not two

Group chat adds a dimension the library never had: PEOPLE. It does **not**
add a second retrieval stack. Every answer a room agent gives walks the
exact same path a single-user agent does:

```
user message (room, user)
  └─ core/rag_group            identity, room rules, per-user permission lens
       └─ core/rag_service.answer(caller="group_chat", extra_allowed_docs=scope)
            └─ core/rag_strategies (adaptive/agentic/CRAG/self-RAG + local twins)
                 └─ core/rag.search(allowed_docs=…)   the only vector store
```

The one change inside `core/rag_service.py` is `extra_allowed_docs`: an
optional document whitelist that is **intersected** with the caller's own
grant everywhere (`search`, `answer`, and the pre-scoped retriever closure
the strategies run against). The intersection rule makes it narrow-only:
the group layer can shrink a search, never widen one. That is the load-
bearing invariant — everything else in this phase is built on it.

## 2. The permission model (three gates, all must pass)

| Gate        | Who enforces            | Rule                                                       |
|-------------|-------------------------|------------------------------------------------------------|
| Membership  | `rag_group` (every call)| Not a member of the room → blind: no post, no history, no docs, no retrieval. Config is re-read per call, so removal takes effect immediately. |
| Collections | `rag_group.user_scope`  | Room `shared_collections` ∩ user's explicit grant (absent grant → the whole pool; **empty grant → blind**, deliberately). |
| Document    | `rag_group._doc_visible`| `private` → only its owner (`metadata.owner`) and the room owner (steward of the pool). An explicit `metadata.shared_with` list (`user_ids` or `["all"]`) overrides even the private flag — that is `grant_document()`. |

Roles inside a room: `owner > admin > member > guest`. Guests read and
chat but cannot add documents; only admins/owners manage membership; only
the owner grants admin or transfers ownership; the owner cannot be removed
(transfer or delete instead). Every membership change, share, grant and
denial lands in `knowledge/audit.log` tagged `group:*` with actor + detail,
so "who saw what, when" has an answer.

**The security requirement, stated as code:** a user cannot retrieve a
document they are not authorized for — not via `post()` agent answers, not
via scoped `search`, not via `visible_documents`, no matter the strategy,
the phrasing, or who else is in the room. `tests/test_rag_group.py` proves
each of those paths (see §6).

## 3. Rooms, transcripts, shared context

* **Rooms** live in `config/rag_groups.json` (rooms + user registry), the
  same re-read-per-use contract as `config/rag_callers.json` — revoking
  access never needs a restart.
* **Transcripts** are append-only JSONL per room under
  `memory/rag_group_chat/<room_id>.jsonl`: user messages, agent replies
  (with citations, doc ids, strategy, risk), system rows. Every member sees
  the same history — that IS the shared conversation context — paginated by
  `seq`, member-gated on read.
* **Shared context in retrieval:** the room's `context_turns` (default 3,
  capped at 10) most recent user messages are folded into the next
  retrieval query (bounded to 200 chars) so follow-ups like "and its page
  two?" resolve. Context shapes the **query only** — the ACL stays the
  asking user's.

## 4. Agent participation rules

Each room carries an agent list; each entry decides when the agent speaks
and what it may see:

```json
{"agents": [
  {"agent": "librarian", "trigger": "always",   "strategy": "auto"},
  {"agent": "auditor",   "trigger": "mention"},
  {"agent": "tickerbot", "trigger": "keywords:earnings,guidance"},
  {"agent": "archivist", "trigger": "manual"},
  {"agent": "legal",     "trigger": "always", "collections": ["legal_docs"]}
]}
```

* `always` — every user message · `mention` — only `@name` ·
  `keywords:a,b` — any keyword · `manual` — only via `ask_agent()`.
* **Agent messages never trigger agents** — loops are structurally
  impossible, not politely discouraged.
* An agent's optional `collections` cap narrows the *per-user scope* for
  its own retrieval: an agent can know **less** than the user it answers,
  never more.
* `strategy` picks the retrieval brain (`auto` default → LLM strategies
  when the model ladder is up, the offline local twins otherwise).

## 5. REST surface (`dashboard/rag_api.py`, same bearer auth)

| Endpoint | Purpose |
|---|---|
| `POST /api/rag/group/rooms` | create room (owner, pool, agents) · `GET ?user=` list |
| `GET  /api/rag/group/room`  | room info, member-gated |
| `POST /api/rag/group/members` | add / set role / `remove:true` |
| `POST /api/rag/group/message` | post; participation rules fire; replies carry citations |
| `POST /api/rag/group/ask-agent` | manually invoke one agent |
| `GET  /api/rag/group/history` | shared transcript (`?limit=&after=`), member-gated |
| `POST /api/rag/group/share-text` | contribute a doc (`share: "room"\|"private"`) |
| `POST /api/rag/group/grant` | per-document `shared_with` grant/revoke |
| `GET  /api/rag/group/documents` | the room's library through one member's lens |

Auth failures → 401; membership/permission denials → 403 (reusing the
existing `_status_for` mapping).

## 6. Test map (`tests/test_rag_group.py` — 18, offline, temp dirs + fake embedder)

1. rooms & roles, ownership transfer, owner-protection
2. non-member is blind (post/history/documents/scope)
3. shared pool + per-user collection narrowing (empty grant = blind)
4. private doc is owner-only; `grant_document`/revoke take effect next call
5. **the big one**: no path retrieves an unauthorized document
6. `extra_allowed_docs` cannot widen a caller grant (search + strategies)
7. agent replies carry in-scope citations; defence-in-depth sweep drops
   planted out-of-scope evidence
8. trigger rules: always / mention / keywords / manual; no agent→agent loops;
   agent collection cap below the user
9. shared context query folding; member-gated paginated history
10. share rules (guests blocked, room policy, out-of-pool refused)
11. HTTP end-to-end: create → share → ask → cite; 401 without token, 403
    for non-members

Known pre-existing issue (not from this phase): running
`tests/test_rag_offline.py` after the other RAG suites in one process trips
cross-file state pollution on the baseline tree too — each suite passes
cleanly in its own file and with this phase's suites.
