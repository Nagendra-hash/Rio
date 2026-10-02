# Architecture: JARVIS Mark LV vs. Modular Local-First Personal AI

> **Phase 1 — Architecture Document**
> Status: Draft · Author: Codebuff
> Created: 2026-10-02

---

## 1. Executive Summary

This document maps the existing **JARVIS Mark LV** codebase (PyQt6 + Gemini Live API voice assistant, by FatihMakes) against a **modular local-first personal AI spec**, as embodied by the reference implementation **OpenHuman** (`openhuman/` subdirectory and the `tinyhumansai/openhuman` project).

JARVIS Mark LV is a single-process PyQt6 desktop application written in Python. It is tightly integrated with Google's Gemini Live API, keeps everything local by default (API key, memory, config, RAG indexes, SQLite knowledge base), and exposes a 12-skills action system + a user-plugin system. A companion dashboard (`dashboard/server.py`) provides a phone web UI.

OpenHuman is a multi-crate Rust workspace with a Tauri v2 desktop shell, a React + Vite frontend, a standalone TUI, and a typed library facade (`openhuman-embed`) for embedding the core. It defines a vision of a modular, local-first agent harness with a persistent **Memory Tree** (SQLite + Obsidian Markdown vault), 118+ third-party integrations via Composio, 15 messaging channels, MCP server support, visual **workflows** on the tinyflows engine, a **Turing Test** approval gate, and a pluggable engine layer that supports managed, BYOK, and fully-local (Ollama) inference.

The two share DNA — both are personal AI assistants that keep user data local and support Telegram as a channel — but they are built on entirely different stacks and architectural philosophies. This document compares them feature-by-feature, marks present/partial/missing with file-path evidence from the JARVIS repo, and provides migration guidance for the phases where JARVIS could adopt OpenHuman's approaches incrementally.

---

## 2. Project Context and Scope

### 2.1 JARVIS Mark LV — Existing Repo

| Aspect | Detail |
|---|---|
| Repository | `github.com/FatihMakes/Mark-LV` |
| Primary language | Python (3.11+) |
| UI framework | PyQt6 (HUD with holographic avatar, waveform, video overlay) |
| Core session | `main.py` (3,458 lines) — Gemini Live session, audio I/O, viseme extraction, tool dispatch |
| Config & secrets | `config/api_keys.json` (plaintext, gitignored) |
| Key env var | `JARVIS_HOME` — relocates ALL writable paths (config/, memory/, hf-cache/, knowledge/, skills/) |
| License | Creative Commons BY-NC 4.0 (non-commercial) |
| Integrations | Gemini Live API (primary), DDG (search fallback), Telegram/WhatsApp (via Hermes bridge) |
| Test suite | 27 test files, offline-only (pytest) |

**Repository layout (at a glance):**

```
Mark LV/
├── main.py                # 3,458-line core loop (Gemini Live session, audio I/O, viseme, dispatch)
├── ui.py                  # PyQt6 HUD — avatar canvas, waveform, settings drawers, camera
├── setup.py               # OS-aware installer
├── plugins/               # User plugin system — drop a single .py, auto-discovered
├── actions/               # 30+ bundled skills (web_search, screen_processor, browser_control, …)
├── core/                  # AI + infra: gemini.py, llm_router.py, rag.py, action_loader.py …
├── memory/                # Memory stack: memory_manager, conversation_journal, config_manager
├── dashboard/server.py    # FastAPI phone dashboard (port 8000) — AES-256-CBC auth, QR pairing
├── config/                # api_keys.json + TLS certs (gitignored)
├── scripts/               # Portable build (build_portable.py), jarvis_inbox.py
├── hermes-hooks/          # DIRECTORY hooks forwarding Telegram/WhatsApp PDFs
├── tests/                 # 27 offline-only pytest files
├── openhuman/             # SEPARATE Rust/Tauri/React reference project (see §2.2)
├── PORTABLE.md            # Pendrive mode documentation
└── requirements.txt
```

### 2.2 OpenHuman — Reference Implementation (Spec)

The `openhuman/` subdirectory is a **separate Rust/Tauri/React project** that is the reference implementation for the "modular local-first personal AI" spec. It is **not integrated** into JARVIS; it lives as a sibling tree and shares no runtime code. The README inside it links to `github.com/tinyhumansai/openhuman` (GNU GPL3).

| Aspect | Detail |
|---|---|
| Repository | `github.com/tinyhumansai/openhuman` |
| Primary language | Rust (2021 edition), TypeScript (React) |
| UI framework | React 19 + Vite 7 + Tauri v2 |
| Core | In-process Rust `openhuman_core` library (tokio task) |
| Secondary frontends | `openhuman-tui` (ratatui), `openhuman-embed` (typed library facade) |
| Managed backend | TinyHumans backend (Socket.IO + REST) — one API key |
| Config | `config.toml` + OS keyring for secrets |
| License | GNU GPL 3.0 |

**OpenHuman repository layout (at a glance):**

```
openhuman/
├── app/                           # Vite/React frontend
├── crates/
│   ├── openhuman-app/             # Thin Tauri v2 desktop host (separate Cargo world)
│   ├── openhuman-core/            # Cargo package `openhuman` — business logic, Rust core
│   │   └── src/                   # Flat domain modules: agent, memory, tools, channels, …
│   ├── openhuman-rpc/             # JSON-RPC 2.0 envelopes, HTTP client, server
│   ├── openhuman-embed/           # Typed library facade for embedding
│   ├── openhuman-tinyhumans/      # SDK-backed backend transport layer
│   ├── openhuman-cli/             # `openhuman-core` binary + dev/benchmark bins
│   └── openhuman-tui/             # Standalone ratatui terminal frontend
├── vendor/                        # Recursive git submodules (tinyagents, tinyflows, …)
├── gitbooks/                      # Public product + contributor documentation
└── docs/                          # Internal maintainer documentation
```

### 2.3 Spec Phases

The modular local-first spec (as described by OpenHuman) decomposes a personal AI into these phases/domains:

1. **Head / Presentation Layer** — desktop (Tauri+React), TUI, library facade
2. **Agent Harness** — tinyagents graph engine, durable checkpointing, cost accounting
3. **Memory Tree** — local-first hierarchical summary tree, SQLite + Obsidian vault
4. **Model Routing** — task-aware provider router with managed/BYOK/local engines
5. **Skills & MCP** — SKILL.md catalog (~90k entries), MCP server/client
6. **Integrations & Channels** — 118+ OAuth integrations, 15 messaging channels
7. **Workflows** — durable visual automations on tinyflows
8. **Voice** — STT/TTS with local and managed options
9. **Portable / Local-First** — on-device execution, privacy mode, single-file export
10. **Security** — OS keyring, AES-256-GCM memory encryption, sandboxed tools, approval gate

---

## 3. Comparison Matrix

The matrix below maps each spec domain to the corresponding JARVIS component(s), marking **✅ Present** (existing functionality covers the spec), **🟡 Partial** (some elements exist, gaps remain), or **🔴 Missing** (not implemented). File-path evidence follows each entry. Where OpenHuman has a direct parallel, it is noted.

### 3.1 Core Runtime & Language

| Spec Domain | JARVIS Status | Evidence | OpenHuman Parallel |
|---|---|---|---|
| **Language** | Python 3.11+ | `README.md`, `requirements.txt` | Rust 2021 (core) + TypeScript 5.8 (React 19) |
| **UI Framework** | PyQt6 HUD | `ui.py` (avatar canvas, waveform, video overlay) | Tauri v2 + React 19 + Vite 7 + Wry webview |
| **JS Runtime** | N/A (pure Python) | | Managed Node.js for tool helpers and skill-adjacent JS |
| **Headless / TUI** | ❌ Missing | No TUI client exists | `crates/openhuman-tui/` (ratatui in-process, no HTTP) |
| **Library Embedding** | ❌ Missing | No programmatic API for other products | `crates/openhuman-embed/` (typed facade) |
| **Single Process** | ✅ Present | `main.py` is a monolithic loop | Core runs in-process as tokio task in Tauri shell |
| **Async Runtime** | 🟡 Partial | Python threads/asyncio; no formal runtime | Tokio async runtime (thread pool for all I/O, websockets, file ops, inter-skill communication) |
| **Module System** | ✅ Present (plugins) | `plugins/*.py` + `core/plugin_loader.py` auto-discovers | Cargo feature gates (9 default: `media`, `skills`, `flows`, `mcp`, `channels`, `http-server`, `scheduler-gate`, `file-logging`, `modules`); shipped product enables wider set from `scripts/ci/product-features.txt` |
| **Build Size** | N/A (Python interpreter + wheels) | | Feature-gated Rust binary: 51 MiB slim stripped (no gates), 60 MiB (skills+flows), 116 MiB (all gates); 42 MiB RSS baseline |
| **Compile-time Safety** | ❌ Missing | | Rust ownership model — no GC pauses, memory safety at compile time |
| **Performance / Scaling** | N/A (single-process) | | In-process fleet: 500 agents at ~1.77 MiB marginal RSS each (~25× denser than one-OS-process-per-agent at ~48 MiB/instance); cold agent turn 102 ms, full nine-phase bootstrap 476 ms; slim build settles at 15.2 MiB private / 42 MiB RSS (of which ~18.7 MiB is reclaimable executable text, ~3.2 MiB live heap); thread count grows ~0.35 per agent, idle CPU flat regardless of N |

### 3.2 LLM Routing & Model Ladder

| Spec Domain | JARVIS Status | Evidence | OpenHuman Parallel |
|---|---|---|---|
| **Model Router** | ✅ Present | `core/llm_router.py` (~1,204 lines) — 4 modes: local/free/smart/gemini; `_plan_chain` task-aware routing | `ModelRouter` in Rust core; `hint:reasoning`, `hint:fast`, `hint:vision`, `hint:code`, `hint:burst` |
| **Task-Aware Routing** | 🟡 Partial | `_plan_chain`: simple→ollama,llm_local,freellm,openrouter,grok,openai,gemini; code→ollama,llm_local,openrouter; reason→no local; vision→no local, gemini | Hint prefixes on every agent turn — task itself emits hints |
| **Local Model** | ✅ Present | `core/llm_local.py` (~209 lines) — Qwen2.5-Coder-3B Q4_K_M GGUF (~1.9GB), RAM guard 1600MB, TTL 300s | Ollama / LM Studio / MLX, any OpenAI-compatible server |
| **Local Embedding** | ✅ Present | `core/rag.py` — 3072-dim embeddings via Gemini API | Configurable; `bge-m3` recommended (1024-dim, local) |
| **Model Fallbacks** | ✅ Present | `core/gemini.py` — 9-model ladder ordered by measured response time, 429/503/404 cooldowns (5min/30min/6hr) | Router wraps multiple pre-created providers; falls back automatically |
| **Usage Tracking** | ✅ Present | Writes usage log to `memory/llm_usage.jsonl`; constants `_USAGE_PATH`, `_USAGE_MAX_BYTES`, `_USAGE_KEEP_LINES` | Per-call token and cost accounting in run journals |
| **Managed Backend** | ❌ Missing | No TinyHumans-style managed backend; direct Gemini API calls | TinyHumans backend brokers 200+ models, one subscription |

> See also: [§4.1 LLM Routing Analysis](#41-llm-routing-jarvis-ladder-vs-openhuman-router)

### 3.3 Memory

| Spec Domain | JARVIS Status | Evidence | OpenHuman Parallel |
|---|---|---|---|
| **Long-term Memory** | ✅ Present | `memory/memory_manager.py` (~514 lines) — 3-layer JSON: journal append-only, timeline summaries, identity/prefs; `MEMORY_MAX_CHARS=200000`, `PROMPT_CORE_CHARS=900` | Memory Tree (SQLite `chunks.db` + `.md` vault); `tinycortex` engine vendored |
| **Conversation Journal** | ✅ Present | `memory/conversation_journal.py` (~469 lines) — verbatim monthly JSONL + daily consolidation | Per-thread transcript JSONL; compaction seals generations, no deletion |
| **Memory Panel / UI** | ✅ Present | "🧠 MEMORY" settings tab shows every stored fact, when learned, delete button | Memory tab → "View vault in Obsidian" deep link; edit `.md` files directly |
| **On-Disk Vault** | 🟡 Partial | `memory/long_term.json` (single JSON file) | SQLite `chunks.db` + `.md` files in `wiki/` (Obsidian-compatible, linkable) |
| **Retrieval** | ✅ Present | `recall_memory` tool — local search, well under 1ms | `memory_tree` tool: search / drill down / topic / global / fetch |
| **Summary Trees** | ❌ Missing | Memory is flat JSON; summaries are hand-curated identity/prefs | L0 buffers → source summaries → topic summaries → global digests |
| **Auto-Fetch / Ingest** | ❌ Missing | No automatic ingestion from external services | 20-minute sync from 118+ integrations into Memory Tree |
| **Privacy Mode** | 🟡 Partial | Everything is local by default; no cloud fallback except Gemini | `[autonomy] enabled = false` (off by default); Privacy Mode switch blocks all cloud calls |

> See also: [§4.2 Memory Analysis](#42-memory-json-store-vs-memory-tree)

### 3.4 RAG / Knowledge Base

| Spec Domain | JARVIS Status | Evidence | OpenHuman Parallel |
|---|---|---|---|
| **Vector Store** | ✅ Present | `core/rag.py` (~930 lines) — `numpy vectors.npz` + `chunks.jsonl` + `registry.json` + `audit.log` | SQLite FTS5 + semantic search (OpenAI embeddings + SQLite) |
| **Embeddings** | ✅ Present | `core/rag.py` — `EMBED_DIM=3072` via Gemini | Configurable; `bge-m3` for local, OpenAI for managed |
| **Collections / Scoping** | ✅ Present | `trading_books/strategies/research/personal_knowledge/market_rules/temporary` | Source/topic/date scoping in Memory Tree |
| **OCR** | ✅ Present | `pdftoppm + tesseract` for PDFs; Gemini vision for chart pages | Document extraction via `tinydocs` (PDF reading, DOCX/PPTX generation) |
| **Chunking** | 🟡 Partial | JSON-file-based chunking in `rag.py` | Deterministic ≤3k-token chunks with provenance frontmatter |
| **Token Compression** | ❌ Missing | No output compression | TokenJuice (tinyjuice) — 7-stage pipeline (size gate → content detect → compressor select → compress → CCR offload → recovery marker → savings accounting); 7 content-specific compressors (SmartCrusher JSON, Code, Log, Search, Diff, Html, MlText); CCR cache with `⟦tj:<hash>⟧` footers; handle-preview REPL tools (`juice_find`, `juice_extract`, `juice_summarize`) eliminate LLM summarization for large outputs; opt-in ModernBERT salience compression via `kompress` sidecar |
| **External Docs** | ❌ Missing | Reads local files only | Web-fetch scraper pulls clean text from any URL |

> See also: [§4.3 Action / Skill System](#43-action--skill-system)

### 3.5 Actions / Skills

| Spec Domain | JARVIS Status | Evidence | OpenHuman Parallel |
|---|---|---|---|
| **Action Loader** | ✅ Present | `core/action_loader.py` (~221 lines) — auto-discovers `actions/*.py` via module-level `TOOL` dict, dispatches by signature introspection; ctx kwargs: player/speak/response/session_memory/dashboard | Native tools via `tools/` domain; Skill metadata via `skills/` domain |
| **Action Count** | ✅ Present | 30+ action files: `web_search.py`, `screen_processor.py`, `browser_control.py`, `send_message.py`, `file_controller.py`, `reminder.py`, `system_monitor.py`, `computer_settings.py`, `video_player.py`, `trader_research.py`, … | ~215 core tools + 90k skills via catalog; 15 native tool families |
| **Inline Tools** | ✅ Present | `main.py` TOOL_DECLARATIONS reserves 8 inline tools: `system_status`, `screen_process`, `close_camera`, `manage_monitor`, `shutdown_jarvis`, `save_memory`, `recall_memory`, `undo` (+ Mark-LIV visemes/wake-word/PTT) | System tools, memory tools, agent coordination tools |
| **User Plugins** | ✅ Present | `plugins/*.py` — drop single file, auto-discovered via `plugin_loader.py`; `_template.py` ships for guidance | SKILL.md bundles from catalog; MCP servers from registry |
| **Skill Catalog** | 🟡 Partial | `core/skill_registry.py` (~193 lines) — install tracking in `skills/installed_skills.json`, content hash registry, SKILL.md injection (`CONTEXT_MAX_CHARS=4500`) | ~90,000-entry SKILL.md catalog aggregated from HermesHub, ClawHub, LobeHub |
| **MCP Support** | ❌ Missing | No Model Context Protocol client or server | Both client (browse Smithery + official registry) and server (`openhuman-core mcp`) |
| **Tool Search / Jev** | ❌ Missing | No tool-classification layer | Jev: small decision model, 150ms tool scoring vs 22.5% BM25 baseline |
| **AI-Powered Sandbox Execution** | 🟡 Partial | `actions/desktop.py` — AI-generated Python code executed with restricted `exec` (no `__import__`, guarded `open`, output capture + timeout, dangerous builtins blocked) | No direct equivalent; tool execution flows through native Rust handlers gated by `SecurityPolicy` and a host-appropriate sandbox backend |
| **Computer Vision Control** | 🟡 Partial | `actions/computer_control.py` — `screen_find` uses AI-vision to locate UI elements on screen by description; AI-driven screen interactions | Native tools include full filesystem/git/lint/test/grep coder toolset; screen control via `tinycomputer` |
| **Computer Settings** | 🟡 Partial | `actions/computer_settings.py` — OS-native volume/brightness/DNS/DND control with `push_undo` for reversibility | Native platform integration tools (window management, display, etc.) |

### 3.6 Confirmation & Security

| Spec Domain | JARVIS Status | Evidence | OpenHuman Parallel |
|---|---|---|---|
| **Irreversible Action Gate** | ✅ Present | `core/confirm.py` (~161 lines) — UI banner + callback, 90s timeout, irreversible-only; "the model cannot forge the token" | Approval gate (`approval-gate.md`); "Prompt"-class tool calls approved inline via channel |
| **Undo System** | ✅ Present | `core/undo.py` (full) — shared undo stack, `MAX_DEPTH=10`, reversible actions only (files: move/rename/create/copy/write/delete; settings: volume/brightness/dark-mode) | No direct equivalent; workflows have checkpoint/resume but not action undo |
| **Secrets at Rest** | 🟡 Partial | `config/api_keys.json` (plaintext, gitignored); `config/certs/` (self-signed TLS) | OS keyring (macOS Keychain / Windows Credential Manager / Linux Secret Service); AES-256-GCM memory encryption with Argon2id; ciphertext on disk, key in keyring, plaintext only in memory |
| **TLS / Network** | 🟡 Partial | `dashboard/server.py` uses AES-256-CBC auth, plain HTTP on port 8000 (HTTPS alias on 8001) | rustls TLS for all WebSocket and HTTP connections; no OpenSSL dependency; single-use login tokens (5-min TTL) |
| **Sandboxing** | ❌ Missing | Actions run with full user privileges | Platform jail or Docker sandbox; `SecurityPolicy` (`security/policy/`: path_checks, command_checks, enforcement) enforces path/command checks; sandbox backend selected at runtime |
| **Prompt Injection Scanning** | ❌ Missing | No prompt-injection detection | Every remote tool definition passes an injection scan before reaching the model; server-side enforcement (allow / review / block) |
| **Secure Remote Transport** | ❌ Missing | No E2E encrypted channel transport | iOS client: X25519 key agreement → XChaCha20-Poly1305 symmetric session; tunnel backend is a blind forwarder |

### 3.7 Integrations & Channels

| Spec Domain | JARVIS Status | Evidence | OpenHuman Parallel |
|---|---|---|---|
| **Telegram / WhatsApp** | 🟡 Partial | `actions/hermes_task.py` (CLI bridge), `actions/hermes_skills.py`, `actions/hermes_health.py`; `scripts/jarvis_inbox.py` forwards to dashboard `/api/message`; `hermes-hooks/jarvis-pdf-forward/` DIRECTORY hook | 15 built-in channels (Telegram, Discord, Web, iMessage, Slack, WhatsApp, Email, …); native Telegram driver |
| **Hermes Bridge** | ✅ Present | `actions/hermes_task.py` — `hermes -z` CLI bridge (240s timeout, telegram deliver option); `hermes_skills.py` — skill listing/search; `hermes_health.py` — 5-8 parallel probes | Channels implement a small Rust contract (`send` + `listen` paths) |
| **Hermes Health Probes** | ✅ Present | `actions/hermes_health.py` — gateway/bridge/telegram/llm/fallback/prices, deep mode, price-source freshness stale-check + BTC cross-source agreement | N/A (part of channel runtime) |
| **PDF Forwarding** | ✅ Present | `hermes-hooks/jarvis-pdf-forward/handler.py` — DIRECTORY hook (event: `agent:end`) forwarding PDFs with remember-intent → `jarvis_inbox.py` → `/api/message` | N/A (handled by channel integrations) |
| **Web Search** | ✅ Present | `actions/web_search.py` — Gemini Grounded first, DDG fallback (`news`/`research`/`price`/`compare`/`search`) | Managed Exa proxy, BYOK Exa/Brave/Querit/Tavily, self-hosted SearXNG |
| **Third-Party Integrations** | ❌ Missing | No OAuth integration catalog; relies on Hermes bridge for Telegram/WhatsApp | 118+ integrations via Composio (Gmail, Notion, GitHub, Slack, Stripe, …) |
| **Integrations Catalog** | ❌ Missing | No integration discovery or auto-fetch | One-click OAuth; 20-minute auto-fetch into Memory Tree |
| **Direct Telegram** | ❌ Missing | Depends on Hermes bridge proxy | Native Telegram channel driver (Bot API long-poll, managed or BYO BotFather token) |
| **Email channel** | ❌ Missing |  | Native self-hosted IMAP IDLE + SMTP connector (no third-party broker); `allowed_senders` allowlist |

> See also: [§4.4 Hermes Bridge](#44-hermes-bridge) for detailed bridge action analysis


### 3.8 Workflows & Tasks

| Spec Domain | JARVIS Status | Evidence | OpenHuman Parallel |
|---|---|---|---|
| **Agent Mode** | 🟡 Partial | `actions/dev_agent.py` (CLI bridge for autonomous multi-step tasks, browser-use style); `actions/browser_use_task.py` | tinyagents harness: durable checkpointing, sub-agent fleets, no-progress circuit breaker |
| **Reminders** | ✅ Present | `actions/reminder.py` — OS-native scheduled notifications (Task Scheduler / LaunchAgent / systemd) | Cron & Scheduling toolbelt; workflows on schedules or live app events |
| **Proactive Check-ins** | ✅ Present | `actions/proactive.py` — Proactive 2.0 (time/context/rotation-aware) | Workflows with triggers; proactive delivery via channels |
| **Background Monitoring** | ✅ Present | `actions/background_monitor.py` — daily DDG checks for user-configured topics | Auto-fetch (20-min sync) + workflows |
| **Visual Workflows** | ❌ Missing | No workflow canvas or graph editor | tinyflows engine; 22 node kinds; visual canvas; approval gates |
| **Flight Finding** | ✅ Present | `actions/flight_finder.py` | N/A (specialized tool) |

### 3.9 Voice & Avatar

| Spec Domain | JARVIS Status | Evidence | OpenHuman Parallel |
|---|---|---|---|
| **Avatar** | ✅ Present | `core/avatar.py` + `core/avatar_mesh.py` + `core/face_model.obj` — holographic, software-rendered, no GPU needed | Native voice + mascot lip-sync; meets Google Meet as participant |
| **Viseme / Lip-Sync** | ✅ Present | `core/viseme.py` — ~50 mouth shapes/sec from audio formants + transcript; Unicode reduction for Latin/Cyrillic/Greek | `tinyvoice` primitives; mascot lip-sync |
| **Facial Acting** | ✅ Present | Brows track phrase, gaze saccades, natural blinking, nod on stressed syllables | Mascot reactions (face-as-status) |
| **Wake Word** | ✅ Present | `core/wake_word.py` — local "Hey Jarvis" detection, own thread, offline, opt-in (openwakeword) | `tinyvoice` wake-word gating |
| **STT** | ✅ Present | `core/stt.py` — Whisper (faster-whisper, VAD-buffered) + Vosk (streaming) | Hosted STT proxy or BYOK; no local STT engine |
| **TTS** | ✅ Present | `core/tts.py` — EdgeTTS (free Microsoft), Kokoro (offline ~330MB), ElevenLabs (cloud) | Hosted TTS streaming + Piper (local option) |
| **Push-to-Talk** | ✅ Present | `core/hotkey.py` — Ctrl+Space; global on Windows, window-scoped elsewhere | PTT plugin: `packages/tauri-plugin-ptt/` (Swift: AVAudioEngine + SFSpeechRecognizer) |
| **Live API** | ✅ Present | Gemini Live API for real-time voice (1.0s TTFT) | N/A (OpenHuman is not voice-first) |
| **Self-Echo Guard** | ✅ Present | `core/echo.py` — recognizes own voice tail, drops without muting user | N/A |

### 3.10 Dashboard & Remote Control

| Spec Domain | JARVIS Status | Evidence | OpenHuman Parallel |
|---|---|---|---|
| **Web Dashboard** | ✅ Present | `dashboard/server.py` (~890 lines) — FastAPI + uvicorn, port 8000, AES-256-CBC auth, QR pairing | Same core ships React SPA in browser (`pnpm dev:app:web`); also desktop Web chat |
| **Phone Audio** | ✅ Present | `/ws/phone-audio` WebSocket — streams audio to phone | iOS client: thin transport, E2E encrypted tunnel |
| **File Upload** | ✅ Present | `/api/upload` (500MB cap); multipart form support | N/A (handled by integrations) |
| **Remote Control** | ✅ Present | `/api/command`, `/api/revoke-devices`, `/api/message` | Channel delivery (Telegram, Discord) + triggers |
| **QR Pairing** | ✅ Present | `dashboard/server.py` — QR code pairing on port 8000 | QR pairing for iOS client (`openhuman://pair?...`) |
| **HTTPS** | 🟡 Partial | Self-signed certs in `config/certs/`; HTTP on 8000, HTTPS alias on 8001 | rustls TLS for all network; single-use login tokens (5-min TTL) |

> See also: [§4.5 Dashboard & Phone Integration](#45-dashboard--phone-integration)

### 3.11 Portability

| Spec Domain | JARVIS Status | Evidence | OpenHuman Parallel |
|---|---|---|---|
| **Relocatable Paths** | ✅ Present | `JARVIS_HOME` env var relocates ALL writable paths (honored in main.py, ui.py, config_manager, memory_manager, conversation_journal, dashboard/server.py, skill_registry, llm_local, all actions/*.py) | Workspace dir (`<workspace>/memory_tree/`, `<workspace>/wiki/`); `OPENHUMAN_WORKSPACE` override |
| **Portable Mode** | ✅ Present | `PORTABLE.md` — pendrive mode via JARVIS_HOME; portable.sh/portable.bat launchers; `scripts/build_portable.py` (`--linux`, `--windows`, `--offline-wheels`, `--linux-venv`) | AppImage / .deb / .msi installers; library embedding facade |
| **Offline Install** | ✅ Present | `build_portable.py --offline-wheels` | N/A (shipped binary) |

### 3.12 Testing

| Spec Domain | JARVIS Status | Evidence | OpenHuman Parallel |
|---|---|---|---|
| **Test Suite** | ✅ Present | 27 test files, offline-only; `pytest.ini` at root; `tests/conftest.py` with result adapter + `browser_euse` marker | Rust E2E tests (`tests/json_rpc_e2e.rs`); Vitest + WDIO; changed-line coverage ≥80% |
| **Router Tests** | ✅ Present | `tests/test_llm_router_plan.py` (full) | N/A |
| **RAG Tests** | ✅ Present | `tests/test_rag_offline.py` | N/A |
| **Hermes Bridge Tests** | ✅ Present | `tests/test_hermes_bridge_skill.py`, `tests/test_hermes_price_probe.py` | N/A |
| **Local Rung Tests** | ✅ Present | `tests/test_local_rung.py` | N/A |
| **Portable Mode Tests** | ✅ Present | `tests/test_portable_mode.py` | N/A |
| **Live Model Tests** | ✅ Present | `tests/test_live_model_watch.py`, `tests/test_rotation_smoke.py` | N/A |
| **Dashboard Tests** | ✅ Present | `tests/test_dashboard_offline.py` | N/A |
| **Session Resumption** | ✅ Present | `tests/test_session_resumption_1008.py` | N/A |
| **Browser Automation** | ✅ Present | `tests/test_browser_use_skill.py` | N/A |
| **Camera Diagnostics** | ✅ Present | `tests/test_camera_diagnosis.py` | N/A |

---

## 4. Detailed Subsystem Analysis

### 4.1 LLM Routing: JARVIS Ladder vs. OpenHuman Router

> See also: [§3.2 LLM Routing & Model Ladder](#32-llm-routing--model-ladder), [§3.5 Actions/Skills](#35-actions--skills), [§3.7 Integrations & Channels](#37-integrations--channels)

**JARVIS (`core/llm_router.py`):**

JARVIS implements a 4-mode router (`_ROUTER_MODES = ("local", "free", "smart", "gemini")`) that patches `Models.generate_content` at the class level. The `_plan_chain` function routes tasks through a cascade based on task type:

```
simple   → ollama, llm_local, freellm, openrouter, grok, openai, gemini
code     → ollama, llm_local, openrouter, freellm, …
reason   → no local (ollama/freellm excluded)
vision   → no local, gemini budget-exempt
gemini   → forced single-step
tool-grounded calls → bypass router (SEARCH class)
```

The router also maintains a **task-aware preference table** (`_TASK_PREFERENCES`) with keyword hints (`_TASK_HINTS`) that classify prompts into `code`/`reason`/`summarize`/`vision`/`general` and resolve them to an ordered list of free-tier models:

| Task Class | Keyword Hints | Preferred Free Models |
|---|---|---|
| `code` | code, function, script, debug, traceback, error:, regex, compile, class, def, import, sql, api call, refactor, ```, bug, python, javascript | poolside/laguna-s-2.1:free, cohere/north-mini-code:free, nvidia/nemotron-3-super-120b-a12b:free |
| `vision` | (implicit via vision mode) | nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free, inclusionai/ling-3.0-flash-vl:free, z-ai/glm-5.2:free |
| `reason` | solve, calculate, math, equation, logic, puzzle, step by step, prove, how many, probability, reason | nvidia/nemotron-3-ultra-550b-a55b:free, nvidia/nemotron-3-super-120b-a12b:free, thinkingmachines/inkling:free |
| `summarize` | summarize, summary, tl;dr, headlines, news, brief, condense, key points, recap | nvidia/nemotron-3.5-lightning:free, z-ai/glm-5.2:free, nvidia/nemotron-3-super-120b-a12b:free |
| `general` | (fallback, no keyword match) | z-ai/glm-5.2:free, nvidia/nemotron-3-super-120b-a12b:free, nvidia/nemotron-3.5-lightning:free |

Model lists are cached with a 6-hour TTL (`_MODELS_TTL_S = 6 * 3600`). The free-tier pool was refreshed in September 2026 — the old llama/deepseek free tiers were retired; the current pool is Nemotron-3/GLM-5.2/poolside.

**Local rung strategy**: `core/llm_local.py` ships Qwen2.5-Coder-3B Q4_K_M (~1.9GB) with a 1600MB RAM guard and 300s TTL (auto-unload). It only fires when Ollama is unavailable or the prompt exceeds `LOCAL_MAX_PROMPT_CHARS = 4000` (long/difficult text is skipped). Ollama defaults to `qwen2.5:1.5b` with a 60s timeout, 60s healthy probe TTL, 120s failure probe TTL.

**Provider failover chains** (bypass router, used as fallbacks):
- Grok: `grok-4.6`, `grok-4.5`, `grok-3-mini` (via `api.x.ai`)
- OpenAI: `gpt-4o-mini`, `gpt-4.1-mini` (via `api.openai.com`)

`core/gemini.py` (~611 lines) implements a 9-model ladder ordered by **measured response time**, with three failure cooldowns: quota exhaustion (5 min), service unavailable (30 min), not-found/access (6 hr). Usage tracking writes to `memory/llm_usage.jsonl`. Key constants: `_ROUTER_MODES`, `_USAGE_PATH`, `_USAGE_MAX_BYTES`, `_USAGE_KEEP_LINES`, `LOCAL_MAX_PROMPT_CHARS = 4000`, `_GEMINI_BUDGET_DEFAULT = 200` (Gemini calls/day in smart mode).

**OpenHuman:** OpenHuman's router (`crates/openhuman-core/src/inference/providers/router.rs`) wraps pre-created providers and resolves model hints (`hint:reasoning`, `hint:fast`, `hint:vision`, `hint:code`, `hint:burst`) to `(provider, model)` pairs via `ModelRouter::resolve()` in Rust. Three routes are supported:
- **Managed** — TinyHumans backend brokers 200+ models, one subscription
- **BYOK cloud** — user's own API keys (OpenAI, Anthropic, Google, OpenRouter, Groq, etc.); 26 providers
- **Local runtime** — Ollama, LM Studio, MLX, OMLX, or any OpenAI-compatible server

Per-agent model pins are supported via `[teams.<agent_id>]` config or inline `model` on sub-agent spawns. Resolution order: inline model → `[orchestrator].model` → `[teams.*]` → archetype hint + route table. Providers are pre-created at config load — no cached TTL.

**Key differences:**
- JARVIS is Gemini-centric (one API key, 9-model ladder with measured-TTFB ordering, 6h model-list TTL); OpenHuman is provider-agnostic (TinyHumans managed + 26 BYOK providers + local runtimes)
- JARVIS routes by task type at the `_plan_chain` level with provider-specific failover chains; OpenHuman routes by hint prefixes emitted per agent turn, wrapping pre-created providers
- JARVIS sends the full tool list to the model on every turn; OpenHuman uses **Jev** (a small decision model) as a fast-path tool classifier to pre-narrow candidates before the model sees them
- JARVIS patches `Models.generate_content` at the class level; OpenHuman uses `ModelRouter::resolve()` in Rust
- JARVIS does not compress token output; OpenHuman has **TokenJuice** (tinyjuice)

### 4.2 Memory: JSON Store vs. Memory Tree

**JARVIS:**

The memory system (`memory/memory_manager.py`, ~514 lines) is a 3-layer JSON architecture:
1. **Journal** — append-only log of facts learned per session
2. **Timeline summaries** — daily/periodic summaries
3. **Identity/prefs** — `PROMPT_CORE_CHARS=900`, `PROMPT_MAX_PER_CATEGORY=6`, `MEMORY_MAX_CHARS=200000`

`memory/conversation_journal.py` (~469 lines) writes verbatim monthly JSONL with daily consolidation. Memory is stored in a single file: `memory/long_term.json`.

Retrieval is via the `recall_memory` tool — local search, well under 1ms. The prompt carries a core (identity + most recently updated facts, ~971 chars for 62 facts) plus an index of keys it couldn't fit.

**OpenHuman:**

The Memory Tree (`crates/openhuman-core/src/memory/`) is built on the `tinycortex` engine (vendored under `vendor/tinymemory/vendor/tinycortex`). It uses:
- **SQLite** (`<workspace>/memory_tree/chunks.db`) for vectors and retrieval
- **Markdown vault** (`.md` files in `<workspace>/wiki/`) compatible with Obsidian, linkable via `[[wiki-link]]` syntax
- **Hierarchical summary trees**: L0 buffers → source summaries → topic summaries → global digests

The 9-step pipeline (from [Architecture README](openhuman/gitbooks/developing/architecture/README.md)):
1. **Connect** — OAuth into integrations; backend stores tokens; core never sees plaintext
2. **Auto-fetch** — every 20 minutes, scheduler walks active connections; native providers sync
3. **Canonicalize** — provider output normalized into provenance-tagged Markdown
4. **Chunk** — Markdown split into ≤3k-token deterministic chunks
5. **Store** — chunks land in SQLite (`chunks.db`) and `.md` files in `wiki/`
6. **Score** — background workers run embeddings, entity extraction, hotness scoring
7. **Summarize** — source/topic/global summary trees built and refreshed from chunk pool
8. **Retrieve** — agent queries the Memory Tree (search / drill down / topic / global / fetch)
9. **Compress** — TokenJuice compacts tool output and large source data before entering LLM context

Memory is encrypted at rest with **AES-256-GCM**, keyed by **Argon2id**. The **OS keyring** (via `keyring` crate: macOS Keychain, Windows Credential Manager, Linux Secret Service) stores transport credentials — never plaintext on disk for UI-managed channels.

Conversation state is separate from memory: each thread's transcript is a JSONL file keyed by thread and agent ID, with compaction that seals generations (never deletes), so the full history stays recoverable. Session identity (`SessionRef`) is derived deterministically from thread ID + agent ID — no timestamp, so one conversation resolves to one file in every process and launch.

**Migration note:** JARVIS's single-file JSON approach is simple and fully local, but does not scale to the volume or structure that OpenHuman's Memory Tree targets (118+ integrations, 20-minute auto-fetch, Obsidian compatibility, git-backed diff workflow).

> See also: [§3.3 Memory](#33-memory)

### 4.3 Action / Skill System

> See also: [§3.5 Actions/Skills](#35-actions--skills), [§3.4 RAG / Knowledge Base](#34-rag--knowledge-base), [§3.6 Confirmation & Security](#36-confirmation--security)

**JARVIS:**

JARVIS has two plugin systems:

1. **Bundled actions** (`core/action_loader.py`): auto-discovers `actions/*.py` via module-level `TOOL` dict. Dispatches by signature introspection. Context kwargs: `player`, `speak`, `response`, `session_memory`, `dashboard`. 30+ bundled actions including `web_search`, `screen_processor`, `browser_control`, `send_message`, `file_controller`, `reminder`, `system_monitor`, `computer_settings`, `video_player`, `trader_research`. Notable patterns:
   - **AI-powered sandbox execution** (`actions/desktop.py`): generates Python code in-model, executes with a restricted `exec` using `restricted_builtins` (no `__import__`, `open` guarded, dangerous functions blocked) with output capture and timeout
   - **Computer control** (`actions/computer_control.py`): `screen_find` uses AI-vision to locate UI elements on screen by description
   - **Computer settings** (`actions/computer_settings.py`): OS-native volume/brightness/DNS/DND control with `push_undo` for reversibility

2. **User plugins** (`core/plugin_loader.py`): drop a single `.py` file into `plugins/`. Skips files starting with `_` (shared helpers). Validates `PLUGIN` dict constant with `name`, `description`, `parameters` (OBJECT type), and `run(parameters, ...)` callable. Handles import errors, validation errors, and name collisions gracefully — never raises. Also supports optional `PLUGIN_SETTINGS` schema for configuration forms.

3. **Skill registry** (`core/skill_registry.py`, ~193 lines): tracks installed skills in `skills/installed_skills.json`, maintains a content hash registry, injects SKILL.md into system prompt (`CONTEXT_MAX_CHARS=4500`).

The `main.py` TOOL_DECLARATIONS also reserves 8 inline tools: `system_status`, `screen_process`, `close_camera`, `manage_monitor`, `shutdown_jarvis`, `save_memory`, `recall_memory`, `undo`.

4. **Skill manager action** (`actions/skill_manager.py`): Install/upgrade/remove lifecycle (`skills add`, `skills find`, `skills upgrade`, `skills remove`) with content-hash verification and SKILL.md context injection.

**OpenHuman:**

Skills are `SKILL.md` packages (metadata, instructions, optional bundled scripts). Responsibilities are split across three domains:
- `crates/openhuman-core/src/skills/` — metadata: discover/install/parse SKILL.md, inject descriptors into agent prompts (`ops_create`, `ops_discover`, `ops_install`, `ops_parse`, `registry`, `tools`)
- `crates/openhuman-core/src/skills/catalog/` — registry of installed skills
- `crates/openhuman-core/src/skills/runtime/` — execution of SKILL.Md workflows: starts/cancels runs, reads metadata/logs, resolves language runtimes

**Key architectural shift**: The legacy QuickJS VM was removed entirely. Skills now contribute metadata + tool descriptors injected into agent prompts, while tool execution flows through native Rust handlers and Node/Python-backed helpers (`runtime::node`, `runtime::python`), gated by `SecurityPolicy`. Language runtimes are resolved by `runtime::node` (installs managed Node.js distribution with SHA-256 verification) and `runtime::python` (same pattern for Python). Scheduling is owned by the `cron` domain (`scheduler_gate`), not per-skill — there is no `onCronTrigger()` handler.

The catalog has ~90,000 entries aggregated from HermesHub, ClawHub, LobeHub. Discovery uses `SKILL.md` fields: `name`, `description`, `metadata.id` (stable slug), `allowed-tools` (allowlist guidance), `bundled resources` (scripts, references, assets).

Additionally, OpenHuman implements MCP on both sides:
- **As client**: browses Smithery and the official MCP registry, connects servers declared in `mcp.json`, surfaces their tools through the same tool registry as native tools
- **As server**: `openhuman-core mcp` exposes OpenHuman's own tools over stdio or HTTP so external MCP hosts (Claude Desktop, Cursor, Zed) can call them
- Every remote tool definition passes a prompt-injection scan before reaching the model


### 4.4 Hermes Bridge

> See also: [§3.7 Integrations & Channels](#37-integrations--channels), [§3.2 LLM Routing & Model Ladder](#32-llm-routing--model-ladder)

JARVIS integrates with the **Hermes Agent** ecosystem through three dedicated actions:

- **`actions/hermes_task.py`**: CLI bridge (`hermes -z` invocation, 240s timeout). Supports telegram delivery option. Bridges JARVIS autonomous tasks to Hermes.
- **`actions/hermes_skills.py`**: Skill listing and search — surfaces HermesHub skills to JARVIS.
- **`actions/hermes_health.py`**: Parallel health probes (5-8 concurrent: gateway, bridge, telegram, llm, fallback, prices). Deep mode includes price-source freshness stale-check and BTC cross-source agreement.

The **`hermes-hooks/jarvis-pdf-forward/`** DIRECTORY hook fires on `agent:end` event, forwarding Telegram/WhatsApp PDFs with remember-intent to `scripts/jarvis_inbox.py`, which normalizes the entry (multipart form, channel/chat_id/text/file, AES-256-CBC or static bearer token auth) → dashboard `/api/message`.

**OpenHuman parallel:** Channels implement a small Rust contract (send + listen paths). Telegram is the most fully featured channel. Email is a self-hosted connector (IMAP IDLE + SMTP). The iOS client uses a thin transport layer with E2E encrypted tunnels.

### 4.5 Dashboard & Phone Integration

> See also: [§3.10 Dashboard & Remote Control](#310-dashboard--remote-control), [§3.7 Integrations & Channels](#37-integrations--channels), [§3.6 Confirmation & Security](#36-confirmation--security)

**JARVIS (`dashboard/server.py`, ~890 lines):**

FastAPI + uvicorn server on port 8000 with:
- AES-256-CBC auth (static bearer token or AES encryption)
- QR code pairing
- `/ws/phone-audio` — phone audio WebSocket
- `/api/upload` — file upload (500MB cap)
- `/api/message`, `/api/command`, `/api/revoke-devices`
- Plain HTTP by default (HTTPS alias on port 8001; self-signed certs in `config/certs/`)

**OpenHuman:**

The React frontend talks to the core over HTTP JSON-RPC (loopback) + Socket.IO (loopback) for live events. The iOS client (experimental) uses a TransportManager with three strategies: `LanHttpTransport` (direct HTTP to desktop core), `TunnelTransport` (socket.io relay, E2E encrypted), `CloudHttpTransport` (fallback via backend). Pairing uses single-use tokens (5-min TTL) exchanged via the Rust HTTP client (bypasses CORS).

### 4.6 Testing Strategy & Development Conventions

**JARVIS** (`tests/`):

JARVIS has 27 offline-only test files covering the most critical subsystems. Tests are `pytest`-based with `pytest.ini` at root. `tests/conftest.py` provides a result adapter and `browser_euse` marker. All tests are designed to run without network access where possible.

**OpenHuman**:

OpenHuman uses a multi-layered testing strategy:

| Layer | Tooling | Convention |
|---|---|---|
| **Rust unit tests** | `cargo test` | Never inline; sibling `<module>_tests.rs` declared with `#[cfg(test)]` + `#[path = "<module>_tests.rs">` above `mod tests;` |
| **Rust E2E tests** | `cargo test -p openhuman-cli --test <name>` | `tests/json_rpc_e2e.rs` for JSON-RPC behavior |
| **Frontend unit** | Vitest | `*.test.ts` / `*.test.tsx` under `app/src/`; test behavior not implementation |
| **Frontend E2E** | WDIO | `app/test/e2e/specs/` using `element-helpers.ts` |
| **Coverage** | Changed-line coverage | Must be ≥80% on PRs to main/release |
| **Inference debugging** | Capture proxy (`scripts/debug/capture-first-inference.mjs`) | Intercepts inference requests between core and backend; `CAPTURE_ALL=1 pnpm debug capture` |

CI has two tiers: **CI Lite** (area-specific checks, changed-line coverage on PRs) and **CI Full** (complete suites for release). Debugging follows strict conventions: new files named `test.rs`/`tests.rs`/`<module>_test.rs` fail `pnpm rust:layout`; inline `#[cfg(test)] mod` blocks also fail layout checks.

The capture-first-inference proxy dumps every inference request body under `target/debug-logs/inference-sequence/` and prints a one-line summary per response (`served_by`, `ttfb`, `prompt`, `cached`, `cache_key`, status, error). Self-test: `scripts/__tests__/capture-first-inference.test.mjs`.

| Test File | Coverage |
|---|---|
| `test_llm_router_plan.py` | LLM router `_plan_chain` task routing |
| `test_rag_offline.py` | RAG indexing and retrieval (offline) |
| `test_hermes_bridge_skill.py` | Hermes bridge skill listing/search |
| `test_hermes_price_probe.py` | Health probe price-source freshness |
| `test_local_rung.py` | Local LLM (Qwen) loading and RAM guard |
| `test_portable_mode.py` | JARVIS_HOME path relocation |
| `test_live_model_watch.py` | Live API model ladder and cooldowns |
| `test_session_resumption_1008.py` | Session handle capture/replay |
| `test_rotation_smoke.py` | Key rotation smoke test |
| `test_browser_use_skill.py` | Browser automation task |
| `test_camera_diagnosis.py` | Camera capture diagnostics |

`tests/conftest.py` provides a result adapter and `browser_euse` marker. All tests are designed to run without network access where possible.

---

## 5. Spec Phase Coverage Summary

### 5.1 Existing JARVIS Coverage

The following spec phases are **fully or substantially present** in JARVIS Mark LV:

| Phase | Status | Key Files |
|---|---|---|
| Core runtime (Python + PyQt6) | ✅ Full | `main.py`, `ui.py` |
| LLM routing + model ladder | ✅ Full | `core/llm_router.py`, `core/gemini.py` |
| Local model execution | ✅ Full | `core/llm_local.py`, `core/llm_client.py` |
| Memory stack | ✅ Present | `memory/memory_manager.py`, `memory/conversation_journal.py` |
| RAG / knowledge base | ✅ Present | `core/rag.py` |
| Action system | ✅ Full | `core/action_loader.py`, `actions/*.py` |
| User plugin system | ✅ Full | `core/plugin_loader.py`, `plugins/` |
| Skill registry | ✅ Present | `core/skill_registry.py` |
| Confirmation gate | ✅ Full | `core/confirm.py` |
| Undo system | ✅ Full | `core/undo.py` |
| Voice (wake word, STT, TTS) | ✅ Full | `core/wake_word.py`, `core/stt.py`, `core/tts.py` |
| Avatar and viseme lip-sync | ✅ Full | `core/avatar.py`, `core/viseme.py`, `core/avatar_mesh.py` |
| Hermes bridge (Telegram/WhatsApp) | ✅ Partial | `actions/hermes_task.py`, `actions/hermes_skills.py`, `actions/hermes_health.py` |
| Dashboard / phone UI | ✅ Full | `dashboard/server.py` |
| Portable mode | ✅ Full | `PORTABLE.md`, `scripts/build_portable.py`, `JARVIS_HOME` |
| Testing (27 offline tests) | ✅ Full | `tests/` |

### 5.2 Partial Coverage

| Phase | Gap | Key Files |
|---|---|---|
| Local memory format | Stored as single JSON, not structured tree | `memory/long_term.json` |
| Token compression | No output compression before model context | N/A | TokenJuice (tinyjuice) — 7-stage pipeline, 7 content-type compressors, CCR cache with `⟦tj:<hash>⟧` recovery markers |
| Multi-provider LLM | Gemini-only; no Anthropic/OpenAI/Groq direct | `core/gemini.py` | Router wraps pre-created providers; 26 BYOK providers (OpenRouter, Anthropic, OpenAI, Google, Groq, Mistral, DeepSeek, Together, Fireworks, etc.) |
| Task-aware routing | 🟡 Partial | `_plan_chain`: simple→ollama,llm_local,freellm,openrouter,grok,openai,gemini; code→ollama,llm_local,openrouter; reason→no local; vision→no local, gemini | Hint prefixes (`hint:reasoning`, `hint:fast`, `hint:vision`, `hint:code`, `hint:burst`) on every agent turn |
| MCP support | No MCP client or server | N/A |
| Tool classification (Jev) | No fast-path tool search/decision model | N/A | Jev: small decision model, 150ms tool scoring vs 22.5% BM25 baseline |
| Visual workflows | No workflow canvas/graph editor | N/A |
| Sandboxing | Actions run with full user privileges | N/A | Platform jail or Docker sandbox; `SecurityPolicy` enforces path/command checks |
| OS keyring | Secrets in plaintext `api_keys.json` | `memory/config_manager.py` | OS keyring (macOS Keychain / Windows Credential Manager / Linux Secret Service); AES-256-GCM + Argon2id |
| Integration catalog | No OAuth integration broker | N/A |
| 3rd-party channels | 🟡 Partial | Telegram/WhatsApp via Hermes bridge only | Hermes integration files | 15 built-in channels (Telegram, Discord, Web, iMessage, Slack, WhatsApp, Email, …) |
| TLS everywhere | HTTP on port 8000; HTTPS alias only | `dashboard/server.py` | rustls TLS for all connections; no OpenSSL dependency |
| Prompt injection scanning | No prompt-injection detection | N/A | Injection scan on remote tool definitions |
| Secure remote transport | No E2E encrypted channel transport | N/A | X25519 → XChaCha20-Poly1305 session |

### 5.3 Missing

| Spec Domain | What's Needed |
|---|---|
| Headless / TUI client | Standalone terminal frontend for headless or SSH use |
| Library embedding facade | Typed API for other products to embed the core |
| Rust core / Tauri shell | Full rewrite of runtime in Rust (out of scope for incremental migration) |
| Memory Tree (SQLite + Obsidian vault) | Replace single-file JSON with structured tree |
| Auto-fetch scheduler | Background sync from integrations every 20 min |
| 118+ integrations | OAuth integration catalog via Composio or equivalent |
| MCP client and server | Model Context Protocol support |
| TokenJuice compression | Smart token compression for tool output |
| Jev decision model | Fast-path tool classification (150ms vs 22ms BM25) |
| Visual workflows | Workflow canvas editor on tinyflows |
| Platform sandbox | Docker / Bubblewrap / Firejail isolation per action |

---

## 6. Migration Guidance

### 6.1 Short-term (within JARVIS, Python stack)

These are incremental improvements that align JARVIS with the spec while keeping the existing Python/PyQt6 stack:

1. **Memory Tree migration**: Replace `memory/long_term.json` with an SQLite-backed memory tree. Keep the 3-layer model (journal, timeline, identity/prefs) but add structured chunking, embedding indexing, and summary trees. *Effort: High. Impact: High.*

2. **MCP client bridge**: Add an MCP client layer that lets JARVIS tools be exposed to external MCP hosts, and lets external MCP servers' tools be imported into JARVIS. This would require a small MCP protocol implementation. *Effort: Medium. Impact: Medium.*

3. **TokenJuice-style compression**: Add lightweight output compression for long tool results (summarize or truncate before sending to Gemini). *Effort: Low. Impact: Medium.*

4. **OS keyring integration**: Migrate `config/api_keys.json` secrets to OS keyring storage (macOS Keychain, Windows Credential Manager, Linux Secret Service). Keep `api_keys.json` for the model name and non-secret config. *Effort: Low. Impact: High (security).*

5. **Workflow runner**: Implement a simple workflow engine that lets users define multi-step agent sequences (cron, schedules, triggers). Start with a JSON/YAML format, add a visual editor later. *Effort: Medium. Impact: High.*

6. **Tool search / Jev layer**: Implement a lightweight tool-classification layer that narrows tool candidates before sending the full tool list to Gemini, reducing token usage and tool-call latency. *Effort: Medium. Impact: Medium.*

### 6.2 Medium-term (Hermes / OpenHuman integration)

JARVIS already has a Hermes bridge (`actions/hermes_task.py`, `actions/hermes_skills.py`). The spec envisions deeper integration:

1. **Full Telegran channel**: Instead of the Hermes bridge proxy, implement a native Telegram channel driver (using `python-telegram-bot` or similar) so messages flow directly without the Hermes intermediary. *Effort: Medium. Impact: Medium.*

2. **Integration catalog**: Add OAuth-based integration discovery and one-click connection for common services (Gmail, GitHub, Slack, Notion). Start with a small set, expand over time. *Effort: High. Impact: High.*

3. **Skill catalog**: Replace the file-drop plugin system with a catalog-backed skill installer (`skills find`, `skills add` — as described in the system prompt). Integrate with HermesHub/ClawHub/LobeHub catalogs. *Effort: Medium. Impact: Medium.*

### 6.3 Long-term (rewrite consideration)

The OpenHuman reference implementation represents a fundamentally different architecture: Rust core, Tauri shell, React frontend, in-process execution, feature-gated compilation, and a managed backend. Migrating JARVIS to this stack is a **rewrite decision** that should be evaluated as a separate project, not an incremental task.

If pursued, the recommended path is:
1. Identify the JARVIS-specific domain logic to preserve (Gemini Live API session, avatar/viseme rendering, wake-word, self-echo guard).
2. Implement these as native Rust modules or `tiny*` submodule extensions.
3. Use `openhuman-embed` to embed the core in a new Tauri + React host.
4. Gradually port bundled actions as native Rust tools.

The voice-first nature of JARVIS (real-time Gemini Live API streaming, 50fps viseme extraction, push-to-talk with waveform) is a distinguishing feature that would need careful re-implementation in Rust.

---

## 7. Key Architectural Differences at a Glance

| Dimension | JARVIS Mark LV | OpenHuman |
|---|---|---|
| **Stack** | Python + PyQt6 | Rust + Tauri + React |
| **Language** | Python 3.11+ | Rust (core), TypeScript (UI) |
| **Architecture** | Single-process monolith (`main.py`) | In-process core + separate frontends (desktop, TUI, library) |
| **AI focus** | Voice-first (Gemini Live API, real-time STT/TTS) | Agent harness (text + voice, meetings, channels) |
| **Model provider** | Gemini API (free tier; no subscription) | TinyHumans managed backend + BYOK + local Ollama |
| **Memory format** | Single JSON file (`long_term.json`) | SQLite + Obsidian Markdown vault (Memory Tree) |
| **Action system** | Python `.py` files with TOOL dict | Native Rust handlers + SKILL.md catalog + MCP |
| **Integrations** | Hermes bridge (Telegram/WhatsApp only) | 118+ OAuth via Composio |
| **Channels** | Web dashboard, Hermes bridge | 15 built-in (Telegram, Discord, Web, Email, …) |
| **Portables** | Pendrive mode (`JARVIS_HOME`, `build_portable.py`) | AppImage / .deb / .msi installers |
| **Security** | Plaintext API keys; AES-256-CBC auth | OS keyring, AES-256-GCM, sandboxed tools |
| **Testing** | 27 pytest files, offline-only | Rust E2E + Vitest + WDIO, 80% changed-line coverage |
| **Embedding** | ❌ Not available | `openhuman-embed` typed library facade |
| **License** | CC BY-NC 4.0 (non-commercial) | GNU GPL 3.0 |

---

## 8. Appendix: File-Path Evidence

### 8.1 JARVIS Core Files

| File | Lines | Status | Notes |
|---|---|---|---|
| `main.py` | 3,458 | Partial (L1–1080 read) | Nuclear `CREATE_NO_WINDOW` Popen patch (Windows), console encoding reconfigure, `HF_HOME` redirection, TaskGroup/ExceptionGroup shim, inline TOOL_DECLARATIONS (8 tools), JarvisLive class, session resumption handle |
| `core/llm_router.py` | ~1,204 | Partial | 4 modes (local/free/smart/gemini), `_plan_chain`, class-level `Models.generate_content` patch |
| `core/gemini.py` | ~611 | Partial | FAST/SMART/SEARCH tiers, 9-model ladder, 429/503/404 cooldowns, key rotation, 3072-dim embeddings |
| `core/llm_local.py` | ~209 | Partial | Qwen2.5-Coder-3B Q4_K_M (~1.9GB), RAM guard 1600MB, TTL 300s, auto-unload |
| `core/llm_client.py` | ~590 | Partial | Ollama/native OpenAI-compatible server client |
| `core/rag.py` | ~930 | Full | numpy vectors.npz + chunks.jsonl + registry.json + audit.log; EMBED_DIM=3072; OCR via pdftoppm+tesseract |
| `core/action_loader.py` | ~221 | Full | Auto-discovers actions/*.py via TOOL dict; signature introspection dispatch |
| `core/plugin_loader.py` | ~400 | Full | Plugin discovery, validation, collision detection, crash isolation |
| `core/skill_registry.py` | ~193 | Full | Install tracking, content hash registry, SKILL.md injection (CONTEXT_MAX_CHARS=4500) |
| `core/confirm.py` | ~161 | Partial | Model-cannot-forge confirmation gate; UI banner + callback; 90s timeout |
| `core/undo.py` | Full | | Shared undo stack; MAX_DEPTH=10; reversible actions only |
| `core/avatar.py` | Large | Partial | Holographic avatar renderer (QPainter) |
| `core/avatar_mesh.py` | Large | Partial | Head geometry, skull/neck/rigs |
| `core/viseme.py` | Large | Partial | Transcript → mouth shapes (~50fps), audio formant fusion |
| `core/stt.py` | Full | Full | Whisper (faster-whisper, VAD-buffered) + Vosk (streaming) |
| `core/tts.py` | Full | Full | EdgeTTS, Kokoro (offline), ElevenLabs (cloud) |
| `core/wake_word.py` | Large | Partial | Local "Hey Jarvis" detection (openwakeword), own thread, offline |
| `core/hotkey.py` | Large | Partial | Push-to-talk chord (Ctrl+Space; global on Windows) |
| `core/echo.py` | Large | Partial | Self-echo guard (recognizes own voice tail) |
| `core/audio_devices.py` | Large | Partial | Microphone/speaker list, filtered and measured |
| `core/palm_engine.py` | ~Large | Partial | Palm reading engine |
| `core/astro_engine.py` | Large | Partial | Astrology engine |
| `core/self_heal.py` | Large | Partial | Self-healing mechanisms |

### 8.2 JARVIS Memory Files

| File | Lines | Status | Notes |
|---|---|---|---|
| `memory/memory_manager.py` | ~514 | Full | 3-layer JSON: journal, timeline, identity/prefs; PROMPT_CORE_CHARS=900 |
| `memory/conversation_journal.py` | ~469–496 | Full | Verbatim monthly JSONL + daily consolidation |
| `memory/config_manager.py` | ~470 | Full | API keys + voice/wake/PTT/HUD/audio-device config |

### 8.3 JARVIS Hermes Integration

| File | Status | Notes |
|---|---|---|
| `actions/hermes_task.py` | Full | CLI bridge (`hermes -z`, 240s timeout, telegram deliver) |
| `actions/hermes_skills.py` | Full | Skill listing/search from HermesHub |
| `actions/hermes_health.py` | Full | 5–8 parallel probes; deep mode; price-source freshness |
| `scripts/jarvis_inbox.py` | Full | Normalized entry from Hermes → dashboard `/api/message` |
| `hermes-hooks/jarvis-pdf-forward/HOOK.yaml` | Full | DIRECTORY hook, event: `agent:end` |
| `hermes-hooks/jarvis-pdf-forward/handler.py` | Full | PDF forwarding with remember-intent |

### 8.4 JARVIS Dashboard

| File | Lines | Status | Notes |
|---|---|---|---|
| `dashboard/server.py` | ~890 | Full | FastAPI + uvicorn, AES-256-CBC auth, port 8000, QR pairing, `/ws/phone-audio`, `/api/upload` (500MB cap) |

### 8.5 JARVIS Actions (Bundled Skills)

30+ confirmed action files in `actions/`:
`hermes_task`, `hermes_skills`, `hermes_health`, `skill_manager`, `code_helper`, `desktop`, `computer_control`, `system_monitor`, `web_search`, `youtube_video`, `screen_processor`, `send_message`, `weather_report`, `file_controller`, `file_processor`, `browser_control`, `reminder`, `video_player`, `open_app`, `proactive`, `price_levels`, `flight_finder`, `palm_reading`, `astro_reading`, `browser_use_task`, `browser_use_relogin`, `dev_agent`, `trader_research`, `trading_agents`, `android_control`, `camera_watch`, `background_monitor`, `computer_settings`, `youtube_transcribe`, `game_updater`

### 8.6 JARVIS Configuration

| File | Notes |
|---|---|
| `.gitignore` | Blocks `config/api_keys.json`, `config/certs/`, `memory/long_term.json`, `memory/conversations/`, `.env*`, `knowledge/`, `models/`, `*.log`, `__pycache__/`, `.venv/`. Does NOT gitignore `skills/`. |
| `config/api_keys.json` | Gitignored. Plaintext API keys. Created on first launch. |
| `config/certs/` | Gitignored. Self-signed TLS pair for phone dashboard. |
| `requirements.txt` | PyQt6, sounddevice, numpy, google-genimi, requests, beautifulsoup4, playwright, yt-dlp, pyautogui, pyperclip, pillow, opencv-python, mss, psutil, send2trash, python-pptx, fastapi, uvicorn[standard], cryptography, python-multipart, qrcode[pil], google-api-python-client, google-auth-oauthlib, tinytuya, paho-mqtt, openpyxl, youtube-transcript-api, pygetwindow, ddgs, pdfplumber, PyPDF2, python-docx, pynvml |
| `PORTABLE.md` | Pendrive mode via JARVIS_HOME; portable.sh/portable.bat; `scripts/build_portable.py` |

### 8.7 JARVIS Tests

27 test files (offline-only), `pytest.ini` at root, `tests/conftest.py` with result adapter + `browser_euse` marker:

| Test File | Coverage |
|---|---|
| `test_llm_router_plan.py` | Router `_plan_chain` task routing |
| `test_rag_offline.py` | RAG indexing/retrieval |
| `test_hermes_bridge_skill.py` | Hermes bridge skill listing/search |
| `test_hermes_price_probe.py` | Health probe price-source freshness |
| `test_local_rung.py` | Local LLM (Qwen) loading + RAM guard |
| `test_portable_mode.py` | JARVIS_HOME path relocation |
| `test_live_model_watch.py` | Live API model ladder + cooldowns |
| `test_rotation_smoke.py` | Key rotation smoke test |
| `test_session_resumption_1008.py` | Session handle capture/replay |
| `test_dashboard_offline.py` | Dashboard server offline tests |
| `test_browser_use_skill.py` | Browser automation task |
| `test_camera_diagnosis.py` | Camera capture diagnostics |

### 8.8 OpenHuman Reference (for comparison)

| Path | Description |
|---|---|
| `openhuman/` | Separate Rust/Tauri/React project — NOT integrated into JARVIS |
| `openhuman/Cargo.toml` | Virtual workspace root (v0.64.10, edition 2021, GPL-3.0) |
| `openhuman/crates/openhuman-core/` | Cargo package `openhuman` — flat domain modules |
| `openhuman/crates/openhuman-app/` | Thin Tauri v2 desktop host |
| `openhuman/crates/openhuman-rpc/` | JSON-RPC 2.0 envelopes, HTTP client/server |
| `openhuman/crates/openhuman-embed/` | Typed library facade |
| `openhuman/crates/openhuman-tui/` | Standalone ratatui terminal frontend |
| `openhuman/crates/openhuman-tinyhumans/` | SDK-backed backend transport |
| `openhuman/vendor/` | Recursive git submodules (tinyagents, tinyflows, tinymemory, …) |
| `openhuman/gitbooks/` | Public product + contributor documentation |

---

## 9. Conclusion

JARVIS Mark LV is a mature, voice-first personal AI assistant with a well-structured Python codebase, comprehensive local-first data handling, and a robust plugin/skill system. It covers most of the core spec domains — memory, RAG, actions, voice, avatar, portable mode, testing — but is constrained by its Gemini-only model access, single-file JSON memory, and lack of third-party integrations beyond the Hermes bridge.

OpenHuman represents the next evolution: a modular, Rust-based architecture with a managed backend, 118+ integrations, Memory Tree (SQLite + Obsidian), MCP support, visual workflows, and enterprise-grade security. The two projects are not currently integrated.

**Recommended next steps:**
1. Implement OS keyring integration for secrets (low effort, high security impact)
2. Add MCP client support to expose/import JARVIS tools (medium effort, medium impact)
3. Evaluate SQLite-backed Memory Tree migration (high effort, high impact)
4. Assess whether to pursue a long-term rewrite on the OpenHuman stack or continue incremental Python improvements

---

*Generated with Codebuff 🤖*
