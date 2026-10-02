# CURRENT_ARCHITECTURE.md — Phase 0 Repository Audit

> Status: **as-is snapshot**, no source code modified.
> Date: 2026-10-02 · Branch: `upgrade-20260930` · Host: Linux 5.15, 4 cores, 3.8 GB RAM

## Headline finding

**This repository already implements the large majority of the phased MVP plan.**
The plan is written as if starting from an empty directory ("Create the smallest
working local Jarvis", "Implement FastAPI backend", "Implement SQLite"). All of
that exists. The real work is **not** Phase 1–14 construction; it is
**reconciling plan assumptions with reality** and closing a short list of real gaps.

Three plan assumptions are already wrong in ways that change the plan:

| Plan assumption | Reality | Impact |
|---|---|---|
| "SQLite" is a Phase 1 deliverable | **No SQLite anywhere** — memory is JSON (`memory/long_term.json`), RAG is `numpy .npz` + JSONL | Phases 1/3/4 must be *re-specified*, not built |
| OpenHuman is a component to integrate | **Zero Python references** to `openhuman` from app code; it is a 112 MB vendored sibling tree | Phase 6 is greenfield *integration*, not wiring |
| The target box is 4 GB | This host **is** 4 GB — budget pressure is real, not hypothetical | Resource rules are binding on every phase |

---

## 1. Detected stack

| Layer | Detected | Evidence |
|---|---|---|
| Language | Python 3.11+ | `requirements.txt`, `setup.py`, `.venv/` |
| Primary UI | PyQt6 desktop HUD | `ui.py` (5,853 lines), `main.py` (3,457 lines) |
| Backend | FastAPI + uvicorn | `dashboard/server.py` (890 lines), port 8000 |
| Voice | Gemini Live API | `core/gemini.py`, `core/stt.py`, `core/tts.py`, `core/viseme.py` |
| Local LLM | Ollama + llama.cpp GGUF | `core/llm_local.py`, `core/llm_router.py` |
| Storage | JSON / JSONL / `.npz` — **no DB** | `memory/`, `knowledge/` |
| Tests | pytest, offline, 26 files | `tests/`, `pytest.ini` |
| Relocation | `JARVIS_HOME` env var | `PORTABLE.md`, honored across all modules |

## 2. Detection checklist (spec §57)

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | Repository | ✅ | git, branch `upgrade-20260930`, 57 commits |
| 2 | Language/framework | ✅ | Python + PyQt6 + FastAPI |
| 3 | Backend | ✅ | `dashboard/server.py` |
| 4 | Frontend | ✅ | `ui.py` HUD; dashboard serves inline HTML |
| 5 | Hermes | ✅ | `actions/hermes_task.py`, `hermes_skills.py`, `hermes_health.py`; binary on PATH |
| 6 | OpenHuman | ⚠️ vendored only | `openhuman/` 112 MB Rust/Tauri tree; **no app-code references** |
| 7 | Ollama | ✅ | on PATH; `qwen2.5:1.5b` pulled (986 MB) |
| 8 | Model integrations | ✅ | `core/llm_router.py` (1,204 lines), `core/gemini.py` 9-model ladder |
| 9 | RAG | ✅ | `core/rag.py` (930 lines), `knowledge/vectors.npz` + `chunks.jsonl`, 26 MB |
| 10 | Database | ❌ none | JSON stores only; no `import sqlite3` in `core/` or `memory/` |
| 11 | Telegram | 🟡 via bridge | `actions/hermes_health.py` probes `api.telegram.org`; no direct driver |
| 12 | WhatsApp | 🟡 via bridge | `hermes-hooks/jarvis-pdf-forward/` |
| 13 | MCP | ❌ missing | no `mcp` reference in `core/`, `actions/`, `scripts/` |
| 14 | Docker | ✅ available | `/usr/bin/docker`; `openhuman/docker-compose.yml`; JARVIS itself is Docker-free |
| 15 | Tests | ✅ | 26 files, `pytest.ini`, `conftest.py` |

## 3. Spec phase coverage (reality vs plan)

| Spec phase | Plan says "implement" | Reality | Verdict |
|---|---|---|---|
| 1 Core JARVIS | FastAPI, SQLite, health, chat, CLI `jarvis start/status/chat/stop` | FastAPI + chat ✅; **no CLI entrypoint, no SQLite, no `/health`** | ~60% |
| 2 Model fallback | Ollama→FreeLLM→OpenRouter→Gemini | Fully built, richer than planned (9-model ladder, cooldowns, TTLs) | ✅ done |
| 3 Memory MVP | Markdown + SQLite index, `remember/forget/search` | 3-layer JSON + journal + `recall_memory`; **no SQLite index, no CLI verbs** | ~70% |
| 4 RAG MVP | TXT/MD/PDF, lightweight local retrieval | Built — but embeddings are **3072-dim via Gemini API**, i.e. *cloud-dependent*, contradicting "local-first" | ⚠️ built, wrong dependency |
| 5 Hermes | `HermesClient` adapter w/ 5 ops | CLI bridge actions, no unified client class, no `cancel_task` | ~60% |
| 6 OpenHuman | Make it the orchestrator | Not integrated at all | ❌ 0% |
| 7 Telegram | First remote interface | Reached only through Hermes bridge | ~40% |
| 8 Tool + MCP | 5 tools, MCP | 37 actions ✅; **MCP absent**; no permission tiers | ~70% |
| 9 Coding agent | Scan→plan→edit→test | `actions/dev_agent.py`, `code_helper.py` | ~70% |
| 10 Web UI | 6 pages | Dashboard exists, single-page, phone-oriented | ~60% |
| 11 Workflows | Durable, resumable | Partial (`memory/market_desk/state.json`, handoffs) | ~40% |
| 12 Security | Sandboxing, secret masking, audit | `core/confirm.py` + `core/undo.py` ✅; **no sandbox, plaintext secrets, no audit log** | ~40% |
| 13 Trading | Read-only analysis | `actions/trader_research.py`, `trading_agents.py`, `price_levels.py` | ✅ done |
| 14 WhatsApp | After Telegram | Bridge only | ~20% |
| 17 Portable | Relative paths, backup/restore | `JARVIS_HOME` ✅, `build_portable.py` ✅; **no backup/restore commands** | ~80% |
| 18 Optimization | <15 s cold start | Unmeasured; Gemini Live session is network-bound at startup | ⚠️ |

## 4. Actual runtime architecture (as built)

```
  PyQt6 HUD (ui.py, 5.8k lines)
            │  Gemini Live API (voice I/O, visemes, tool dispatch)
            ▼
  main.py (3.4k lines) ── 8 inline tools, session memory
     │            │
     │            ├── core/llm_router.py ──► Ollama (qwen2.5:1.5b)
     │            │        (task-aware cascade)  └► llama.cpp GGUF (Qwen Coder 3B, 2 GB)
     │            │                    └► FreeLLM / OpenRouter / Grok / OpenAI / Gemini
     │            │
     │            ├── memory/memory_manager.py (3-layer JSON) + conversation_journal.py
     │            ├── core/rag.py (numpy .npz vectors, Gemini 3072-dim embeddings)
     │            └── core/action_loader.py ──► 37 × actions/*.py
     │                          └──► plugins/*.py (user drop-in)
     │
     └── dashboard/server.py (FastAPI :8000, AES-CBC auth, QR pairing, phone audio WS)
                │
                └── hermes-hooks/ + scripts/jarvis_inbox.py ──► Hermes ──► Telegram / WhatsApp
```

**Everything is in one process.** `main.py` drives voice, tools, memory, RAG and
dispatch together. There is no separate backend/worker split, which is why the
plan's `FastAPI → Router → Ollama` layering does not match: the dashboard is a
*remote-control surface* bolted onto the desktop process, not a backend.

## 5. Verified component inventory

- **Actions:** 37 files in `actions/` (`ls actions/*.py | wc -l`)
- **Python LOC (core):** 21,297 across main/ui/core/memory/dashboard
- **RAG index:** `knowledge/vectors.npz`, `knowledge/chunks.jsonl`, 26 MB total
- **Memory store:** `memory/long_term.json` 132 KB, plus JSONL journals
- **Tests:** 26 files, offline-only

## 6. Risk register (audit findings, ordered by severity)

1. **RAG depends on a cloud API for local documents.** `core/rag.py` uses 3072-dim
   Gemini embeddings. Indexing or querying a PDF requires network + a Gemini key,
   directly contradicting spec §81 ("Do not upload local documents … to cloud
   providers") and §61's "lightweight local retrieval". **This is the single
   biggest correctness gap against the spec.**
2. **No sandbox, no permission tiers.** `actions/desktop.py` runs model-generated
   Python through a restricted `exec`, and other actions run with full user
   privileges. Spec §69 requires a command guard and filesystem sandbox.
3. **Plaintext secrets.** `config/api_keys.json` holds Gemini keys unencrypted;
   `PORTABLE.md` acknowledges the USB stick *is* the credential.
4. **No ResourceManager.** Spec §77 requires RAM detection and concurrency caps
   (`MAX_LOCAL_LLM_TASKS=1`). `psutil` is already a dependency and used ad-hoc in
   `core/llm_local.py:63` and `actions/system_monitor.py` — the primitive exists,
   the policy layer does not.
5. **Two local model paths can both engage.** Ollama (`qwen2.5:1.5b`) and the
   llama.cpp GGUF (Qwen Coder 3B, 2 GB) are separate rungs. The router prefers
   Ollama, but nothing *prevents* both holding resident memory simultaneously —
   exactly what §78 forbids.
6. **Working tree is dirty.** 20+ modified files uncommitted on
   `upgrade-20260930`, including `main.py`, `core/llm_router.py`, and
   `dashboard/server.py`. Phase 0 success criterion says "no source code
   modified" — that holds for *my* changes, but the baseline was not clean.