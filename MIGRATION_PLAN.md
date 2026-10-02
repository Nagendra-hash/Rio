# MIGRATION_PLAN.md — Phase 0 Deliverable

> Companion to `CURRENT_ARCHITECTURE.md` and `RESOURCE_REPORT.md`.
> The audit found most of the phased plan already built. This plan **re-orders and
> re-scopes** the 19 phases around what is actually missing.

## 0. Strategy correction

The plan's premise — build Phases 1→14 incrementally from nothing — does not match
the repository. Re-reading it against the audit gives a different, shorter path:

- Phases **2, 13** are done and exceed spec. Skip.
- Phases **4, 9, 10, 17** are ~70% done. Finish, don't rebuild.
- Phases **1, 3, 4** assumed SQLite that does not exist. Re-spec rather than build.
- Phases **6, 12** are the genuine greenfield work, and **12 gates 13/14**.
- Phase **77 (ResourceManager)** is not a phase at all — it is a cross-cutting
  invariant that should land *before* anything else adds memory pressure.

Reordering by risk rather than by original number:

```
  P0  ResourceManager + concurrency caps      ← do first, protects everything after
  P1  Security: sandbox, command guard, audit, secret masking
  P2  Local embeddings (removes cloud dep from RAG)   ← fixes spec §81 violation
  P3  Memory store → SQLite index (opt-in, JSON stays authoritative)
  P4  HermesClient adapter  (unifies 3 loose actions)
  P5  OpenHuman orchestration integration     ← largest, do late
  P6  Direct Telegram driver                  ← after P4, per spec order
  P7  MCP client/server                       ← after security exists
  P8  CLI: jarvis start/status/chat/stop      ← cheap, high usability
  P9  Workflow resume/checkpoint completion
  P10 Backup/restore + startup optimization
```

## 1. Rationale per item

### P0 — ResourceManager (spec §77/§78) — do first
Not a feature; a guardrail. Every later phase adds RAM pressure to a **3.8 GB**
box. `psutil` is already installed and already used ad-hoc
(`core/llm_local.py:63`), so this is ~1 small module, not new infrastructure.

Required: RAM/CPU/disk detection; `MAX_LOCAL_LLM_TASKS=1`, `MAX_HEAVY_TASKS=1`,
`MAX_LIGHT_TASKS=2`, `MAX_RAG_INDEX_TASKS=1`; a single semaphore guaranteeing
Ollama and the llama.cpp GGUF are **never both resident** (closes risk #5).
Under pressure: disable parallel agents, queue new tasks.

### P1 — Security hardening (spec §69) — gates the dangerous features
Highest-severity finding. Currently `actions/desktop.py` executes model-generated
code and other actions run unsandboxed with user privileges. Required: filesystem
sandbox rooted at `JARVIS_HOME`, command allow/deny guard, secret masking in logs
and `memory/llm_usage.jsonl`, append-only audit log, ALWAYS-CONFIRM for dangerous
and financial operations.

### P2 — Local embeddings (spec §61/§81) — correctness, not a feature
`core/rag.py` calls Gemini for 3072-dim embeddings, so indexing a local PDF
uploads its content to a cloud provider. Spec forbids this. Replace with a local
sentence-embedding model (a small GGUF, consistent with the one-local-model rule),
keeping the existing `.npz` index format so no re-index of unchanged docs is
needed beyond one migration pass. Must run *after* P0 so the second embedding
model cannot push the box over budget.

### P3 — Memory → SQLite index (spec §60)
Keep `memory/long_term.json` as the authoritative human-readable store (do not
migrate data yet — "REUSE > REWRITE"). Add a SQLite index for search, mapping to
the plan's `memory/{user,projects,knowledge,conversations,tasks}` layout. Add
`remember` / `forget` / `memory search` as tool names over the existing manager.

### P4 — HermesClient (spec §62)
Three separate actions (`hermes_task`, `hermes_skills`, `hermes_health`) already
cover most of `send_task/get_status/health_check`. Add a thin client class with
`get_result()` and `cancel_task()` (currently missing) so Hermes is one
dependency with one interface — required before OpenHuman can call it uniformly.

### P5 — OpenHuman (spec §63)
Greenfield: `openhuman/` has **zero** references from app code. Use the official
API/MCP/A2A surface; do not fork. Run as a **separate process** (it is a Rust
workspace with its own Cargo world). Watch the budget — the box has ~1.5 GB
available and this phase is the most likely to breach it.

### P6 — Direct Telegram (spec §64)
Do after P4 so the client interface is stable. Today Telegram only works by
proxying through Hermes; a direct Bot API long-poll driver removes that
dependency. Explicitly **not** WhatsApp (spec §71 — after Telegram is stable).

### P7 — MCP (spec §65)
Requires P1 (remote tool definitions need injection scanning and a permission
tier: READ / WRITE / DANGEROUS). Client first, server later.

### P8 — CLI (spec §58 commands)
`jarvis start|status|chat|stop` never existed; the app is launched via
`main.py`/`portable.sh`/`run.sh`. Cheap, self-contained, and the natural home for
`remember`/`forget`/`memory search` from P3. Add a `/health` endpoint while here.

### P9 — Workflow resume (spec §68)
`memory/market_desk/state.json` and the handoff logs give a foundation; add
checkpoint/resume so a failed workflow does not restart at step 1.

### P10 — Portable backup/restore + optimization (spec §74/§75)
`JARVIS_HOME` relocation is done. Add `backup`/`restore`. Cold-start <15 s needs
measuring first — note the Gemini Live session is network-bound, so the target
may need redefinition as "excluding session connect".

## 2. Explicitly out of scope (unchanged from spec)

Automatic live trading, autonomous financial transactions, multiple simultaneous
local LLMs, Kubernetes, microservice explosion, distributed DBs, large vector DBs,
full WhatsApp automation, complex browser agents, multi-GPU, cloud-only design.

## 3. Definition of done per phase

Per spec §85, every phase must: implement the smallest version → test → measure
RAM/CPU/startup → verify no regression → commit → document → only then continue.

**Regression rule (spec §85):** any feature adding >20% normal RAM usage stops
the line until profiled.

## 4. Sequencing rationale — why security precedes features

The audit's top two risks are (a) unsandboxed execution of model-generated code
and (b) a 4 GB host. If P5 (OpenHuman) or P7 (MCP) lands first, the system gains
new execution surfaces and new memory pressure **before** either is guarded.
P0 → P1 first means every subsequent phase inherits the caps and the sandbox.

## 5. Effort summary

| Item | Size | Risk | Blocked by |
|---|---|---|---|
| P0 ResourceManager | S | Low | — |
| P1 Security | M | Medium | — |
| P2 Local embeddings | M | Medium | P0 |
| P3 Memory index | M | Low | — |
| P4 HermesClient | S | Low | — |
| P5 OpenHuman | **L** | **High** | P0, P1, P4 |
| P6 Telegram | M | Low | P4 |
| P7 MCP | M | Medium | P1 |
| P8 CLI + health | S | Low | — |
| P9 Workflow resume | M | Low | P0 |
| P10 Backup/restore | S | Low | — |

## 6. Open questions for the owner

1. **Is OpenHuman worth the budget?** It is the largest and riskiest item, on a
   3.8 GB box that already runs PyQt6 + a 2 GB GGUF + Ollama. Confirm before
   starting P5.
2. **Cloud-free RAG is a hard requirement?** P2 costs a model download and an
   index migration pass. If a Gemini key is always present, RAG works today.
3. **Should SQLite become authoritative, or stay an index?** This plan keeps JSON
   authoritative (P3) to honour "REUSE > REWRITE".
4. **The baseline tree is dirty** (20+ modified files). Commit or stash before
   Phase 1 so regressions are attributable.