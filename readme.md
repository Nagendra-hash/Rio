# ⚙️ MARK LIII (53)
### The Ultimate Cross-Platform Personal AI Assistant — By FatihMakes

> 📺 **[Watch the full setup video on YouTube](https://www.youtube.com/@FatihMakes)**

A real-time voice AI that can hear, see, understand, and control your computer — on any OS. Supports Windows, macOS, and Linux. Built on the Gemini Live API for native audio streaming, delivering zero subscriptions and total digital autonomy.

---

## ✨ Overview

**MARK LIII is the hands-free & scalable release.** Say **"Hey Jarvis"** and it wakes; stay quiet and it slips back to sleep on its own — while asleep, your microphone never leaves the machine, so an off-hand *"I'll be right there"* to someone in the room no longer sets it off. Under the hood it now runs on the faster **Gemini 3.1 Flash Live** engine, and the moment you ask for something that takes a beat — analysing a file, searching the web — it answers instantly *("On it — going through that now…")* so you never wonder whether it heard you.

It's also built to grow: every skill — bundled or drop-in — now **describes itself in its own file**, so adding a tool is a one-file operation and the core stays lean.

It's not just an assistant — it's an extension of your digital life.

---

## 🚀 Capabilities

### Core Features
| Feature | Description |
|---|---|
| 🎙️ Wake Word | Local **"Hey Jarvis"** detection — sleeps until called, auto-sleeps after 2 min of silence, and never streams audio while asleep. Opt-in, one-click download, toggle & manual sleep/wake from the UI |
| ⚡ Instant Acknowledgment | Speaks a short, context-aware reply in **your language** the instant a longer task starts — no more silent waiting |
| 🚀 Faster Live Engine | Runs on **Gemini 3.1 Flash Live** — roughly 2× faster time-to-first-word than the previous model |
| 🧩 Self-Describing Skills | Actions and plugins share one shape (`TOOL` / `PLUGIN` dict + `run()`), auto-discovered at launch — adding or moving a skill is a single file, no core edits |
| 🌉 Agent Skill Library | Reusable operating skills under `.agents/skills/` — including **`jarvis-hermes-bridge`**, the end-to-end guide for the JARVIS ↔ Hermes bridge (hooks, inbox, delegation, debugging) |
| 🧠 Recallable Memory | No size limit and nothing silently forgotten — the prompt carries what fits, the rest is looked up on demand from a local search |
| 👁️ Memory Panel | See every fact JARVIS has stored about you, when it learned it, and delete any of it in one click |
| ↩️ Undo | Take back what the assistant did — files it moved, renamed, created or wrote, and settings it changed |
| ⚠️ Real Confirmation | Shutdown, restart and WiFi wait for a button **you** press — the model cannot confirm its own irreversible actions |
| 🎧 Audio Device Picker | Choose the microphone and speakers by name, filtered to the short list your OS shows — and measured, so every entry actually works |
| 🔗 Session Continuity | A dropped connection, a voice change or a device change no longer wipes the conversation |
| 🧩 Plugin System | Drop a single `.py` file into `plugins/` — JARVIS learns a new skill on next launch |
| 🎙️ Real-time Voice | Ultra-low latency conversation in any language via Gemini Live API |
| 🎨 Live Theming | Recolour the entire HUD from a hue wheel or hex — applied instantly across every panel |
| 〰️ Reactive HUD | Waveform and reactor core pulse to real audio — your mic while listening, JARVIS while speaking |
| 🎙️ Voice Picker | Choose from 5 native Gemini voices and switch live from the UI — no restart |
| ♾️ Unlimited Sessions | Sliding-window context compression — one conversation can last for hours |
| 🖥️ System Control | Launch apps, adjust volume/brightness, WiFi, shortcuts, power — all by voice |
| 🧩 Autonomous Tasks | High-level planning for complex multi-step goals via agent mode |
| 👁️ Visual Awareness | Real-time screen capture and webcam vision piped into your main Gemini session |
| 🧠 Persistent Memory | Deeply remembers projects, preferences, and personal context across sessions |
| ⌨️ Hybrid Input | Seamlessly switch between keyboard typing and voice commands |
| 🌅 Morning Briefing | On first boot: greets you, reads the time, recaps yesterday, and fetches live news |
| 🔔 Proactive 2.0 | Time-aware, context-aware check-ins — knows the time of day, your projects, and what you've been discussing |
| 🗓️ Session Memory | Summarises each conversation and mentions it naturally next morning — consumed after use, never repeats |
| 👁️‍🗨️ Background Monitoring | User-configured topic watching — checks for new headlines once a day and alerts naturally |
| 📊 Hardware Monitoring | Continuous CPU, RAM, GPU and temperature telemetry with localized voice alerts |
| 🌤️ Weather Report | Live weather data for your city, personalized from memory |
| 🗺️ Dynamic Content Panel | Scrollable display layer beneath the HUD that renders web results, news, and search data |
| 🔍 Multi-Mode Web Search | `news` / `research` / `price` / `compare` / `search` — Gemini Grounded first, DDG fallback |
| ⏰ Smart Reminders | OS-native scheduled notifications (Windows Task Scheduler / macOS LaunchAgent / Linux systemd) |
| ✈️ Flight Finder | Live flight price and availability lookup |
| 🎮 Game Updater | Checks and triggers game updates on Steam and Epic Games on demand |
| 📂 File Processor | Read, summarize, and answer questions about local files |
| 💻 Code Helper | Inline code review, debugging, and generation |
| 🌐 Browser Control | Open URLs, navigate tabs, and interact with the browser by voice |
| 📨 Send Message | Compose and send messages through WhatsApp, Telegram, and more |
| 🎬 YouTube Control | Search, play, and control YouTube playback by voice |
| 🌐 Browser Agent | Multi-step autonomous browsing (browser-use) — searches, forms, data extraction — on the system Chrome, brain routed FreeLLMAPI → Gemini → Ollama; logins persist between tasks via a shared storage-state file |
| 🖱️ Desktop Control | Taskbar, window management, and desktop-level operations |
| 🧑‍💻 Silent Language Memory | Detects spoken language on first use — all future sessions adapt automatically |
| 📱 Remote Dashboard | Control the assistant from your phone via QR code pairing — installable as a PWA or the native Android app (`scripts/build_apk.sh`) |
| ⚡ Auto-Start on Boot | Registers with the OS startup system (registry / LaunchAgent / .desktop) |
| 📈 Paper Trading | Simulated trading with position tracking, P&L, and mistake-learning system |
| 🎯 TradingView Integration | Live TradingView-style charts with auto-drawn support/resistance, EMAs, and trade plans |
| 📋 Clipboard Intelligence | Copy any text → floating panel with Translate / Summarise / Explain / Fix |
| 🪪 Assistant Customization | Change the assistant name, your name, voice, and colour from the UI — takes effect immediately |

---

## 🆕 What's New in Mark LIII

Mark LIII is about making JARVIS **hands-free, faster, and easy to extend** — all universal: no hardcoded language, no bundled asset files, works the same on Windows, macOS and Linux.

### 🎙️ Wake Word — "Hey Jarvis"
JARVIS can now sit quietly until you call it. Turn on **⚙ → WAKE WORD** (a one-click, opt-in download of a tiny local model) and it goes to sleep: the microphone is processed **only on your machine** by a local detector, and nothing is sent to the cloud until it hears **"Hey Jarvis."** Once awake it listens normally, then **auto-sleeps after 2 minutes** of silence. You can also **sleep/wake it by clicking** in the settings. Because it's a *local* gate, background chatter — *"I'm coming!"* to someone at home — never wakes it. It costs **zero** when off (the model isn't even loaded), and the detection runs in its own thread, so nothing else in the app slows down.

### ⚡ Instant Acknowledgment
No more silent gaps. When you ask for something that takes a moment — reading an uploaded file, a web/research search, building code — JARVIS **immediately** says one short, natural sentence *in your language* (*"Right away — going through that file now."*) and *then* runs the tool. Instant actions (opening an app, volume) stay snappy with no chatter.

### 🚀 Faster Live Engine — Gemini 3.1 Flash Live
The live session moved to **`gemini-3.1-flash-live-preview`**, cutting the time-to-first-word roughly in half while keeping tools, all five voices, transcription, session resumption and sliding-window compression intact.

### 🧩 Self-Describing Skills — a Scalable Core
Every bundled **action** now carries its own `TOOL` declaration in its own file (exactly like a drop-in **plugin's** `PLUGIN` dict), and the core auto-discovers them at launch. `main.py` no longer holds a giant list of tool definitions and dispatch branches — it shrank by hundreds of lines. Adding a new built-in skill, or promoting an `actions/*.py` file into a shareable plugin, is now just… moving a file.

> Built on the Mark LI/LII foundation: the **🧩 Plugin System**, **♾️ Unlimited Sessions**, **🎨 Live Theming**, **〰️ Reactive HUD** and **🎙️ Voice Picker** are all still here.

---

## 🔄 The Foundation Update — in every Mark from LII

These four landed across **Mark LII, LIII, LIV and LV at the same time**, after each of those releases had already shipped. They are not what any one of those versions originally introduced; they are the floor all of them now stand on, so moving up a Mark never costs you something the one below it had.

No new dependencies. No bundled asset files. No hardcoded language, and nothing that assumes one operating system.

### 🧠 A memory that actually remembers

The store was capped at **2,200 characters — the whole memory, not per entry** — because all of it was pasted into the system prompt on every connect, so growing the memory grew every request. When it filled, the oldest entries were deleted and one line was printed to a console nobody reads. An assistant advertised as remembering "projects, preferences and personal context" was in practice a two-page notepad that quietly forgot your sister's name after a few weeks.

Storage and prompt budget are now separate problems:

* **Nothing is deleted.** The cap is a runaway guard normal use never approaches, and if it is ever hit it says so in the activity log instead of on stdout.
* **The prompt carries a core, not a dump.** Identity in full, then the most recently updated facts, budgeted — measured at **971 characters on a memory holding 62 stored facts.** That is *smaller* than the old whole-store cap, so sessions now connect with fewer tokens than before.
* **The rest is fetched on demand.** A `recall_memory` tool searches the full store locally — no network, no second model, well under a millisecond.

The part that is easy to get wrong: **a model cannot look something up if it doesn't know the thing exists.** So the prompt also carries an **index of the keys** it had no room for. Without it, "who is Ayşe?" gets "I don't know" while `ayse_sister` sits on disk unread. That index interleaves categories rather than sorting by recency — sorted like the core, a memory with forty preferences pushed the one entry the index existed for off the end.

⚙ → **🧠 MEMORY** shows every stored fact, when it was learned, and a ✕ to forget it. Everything stays in `memory/long_term.json` on your machine.

### ↩️ Undo — it can take back what it did

JARVIS moves files, renames them, writes to them and changes your settings. None of that had a way back; if it misheard you, the only remedy was to fix it by hand.

Say **"undo"** — in any language — and it reverses its own last action:

| | |
|---|---|
| **Files** | move · rename · create · copy · write · delete · organize desktop |
| **Settings** | volume · brightness · dark mode |

Three things it deliberately does *not* do:

* **It does not guess.** Settings undo reads the current value *before* changing it. Where a platform won't report that value, nothing is registered — an undo that restores a guess is worse than no undo.
* **It does not hoard.** Undoing a write means keeping the old contents in memory, so files over 1 MB are excluded and it says so rather than holding a 200 MB log for the session.
* **It does not delete your files to undo a copy.** The reverse of a copy is removing the copy; the reverse of "create a folder" is removing it *only while it's still empty*.

`organize_desktop` gets special treatment — one command that moves dozens of files, which made it the least reversible thing the assistant could do. It journals every move and puts all of them back in one go, cleaning up the folders it created if they're still empty.

**Undo costs nothing at runtime.** It appends a closure to a list; nothing in it runs unless you ask.

### ⚠️ A confirmation the model can't forge

The old gate read like this:

```python
if action in _DANGEROUS_ACTIONS:            # {"restart", "shutdown"}
    confirmed = str(params.get("confirmed", "")).lower()
```

`confirmed` is a **tool parameter, which means the model fills it in.** Nothing stopped it sending `confirmed=yes` on the first call and nothing checked that a human was ever involved. It was a convention, not a gate. And its coverage was two actions — so `toggle_wifi`, which cuts the assistant's own connection to the Live API and therefore *cannot be asked to undo itself*, went through with no gate at all.

The token is now issued by the interface. Shutdown, restart and WiFi put a banner on the HUD and **return immediately**; the action runs only if you press CONFIRM. Nothing blocks — JARVIS keeps talking while the banner is up — so this is **cheaper than the old gate**, which burned two tool round trips on every power command.

> The split between the two mechanisms is about reversibility, not about how alarming a word sounds. Anything undoable is done at once; only the genuinely irreversible asks. An assistant that checks with you before turning the volume down is one you stop talking to.

### 🎧 It finally asks which microphone

Both audio streams opened with no device argument at all, so they always took whatever the OS called "default" — and on Windows that *moves on its own* the moment you plug a headset in. "JARVIS can't hear me" almost always meant "JARVIS is listening to the webcam".

⚙ → **🎧 AUDIO DEVICES** lets you pick the microphone and the speakers by name. Two things matter more than the dropdown:

**The list is short.** `query_devices()` returns one entry per *device × host API*, not per device — measured on an ordinary Windows machine, **41 entries for what the sound settings show as 4 microphones and 4 speakers.** The same microphone appears four times, under MME, DirectSound, WASAPI and WDM-KS, with nothing to say which is which. That is not a choice, it's a quiz. The picker takes one host API per direction, drops the "Sound Mapper" and "Primary Sound Driver" pseudo-devices that just mean "default", and deduplicates. **41 → 8.**

**Every entry has been measured, not assumed.** The obvious approach is to pick the host API with the nicest names — WASAPI on Windows, which in shared mode **doesn't resample**, so with 16 kHz in and 24 kHz out against 48 kHz hardware every open failed. Adding a rate check and moving to DirectSound passes that test on both sides, and PortAudio's DirectSound **output is a silent sink**: the stream opens, every write returns success in ~0 ms, and not one sample reaches the speakers.

| | write(2.0 s) took | |
|---|---|---|
| MME | **2.02 s** | consumed in real time |
| DirectSound | **0.00 s** | swallowed instantly |

No capability flag reports that. So the app measures it — once per host API per direction, on a background thread at startup, using silence. Two consequences worth stating plainly:

* **Each direction picks its own host API.** On Windows this lands on DirectSound for the microphone and MME for the speakers — a split no amount of reasoning would have produced.
* **The probe runs in the mode the app actually ships.** DirectSound input passes a callback stream and fails a blocking read; probing the wrong mode rejected a microphone that works perfectly.

Your choice is stored **by name, not by index** — indices shift whenever something is plugged in. If the saved device is gone, it falls back to the system default and says so in the log rather than failing to start.

### 🔗 It stops forgetting the conversation when the connection drops

`session_resumption` was switched on in the config and the handle the server sent back was **never read** — so every reconnect started an empty session. A dropped packet, or simply changing the voice, wiped the conversation. "Unlimited sessions" leaked through exactly this hole.

The handle is captured and replayed now. A network blip, or switching your microphone, keeps the conversation intact.

It is held in memory only, deliberately: writing it to disk would make a fresh launch continue yesterday's chat, which sounds appealing but breaks the session-summary flow — a conversation that never ends never produces a summary, and the "yesterday we talked about…" line in the morning briefing silently disappears. Changing the **voice** also starts clean on purpose, since resuming restores the server's session state and would likely bring the old voice back with it.

### 🩹 Fixes that came with it

* **The assistant could die on a log line.** Status lines carry emoji and arrows (`📤 file_controller → Moved: a.txt → Documents/`). On a non-UTF-8 console — cp1254 on a Turkish Windows, cp1251 on a Russian one, cp932 on a Japanese one — printing one raises `UnicodeEncodeError`, and because that print sits *after* the tool's own `try/except`, it escaped into the receive loop and took the session down.
* **Every computer command paid for two model round trips.** `computer_settings` made an *entire second Gemini call, inside the tool*, purely to translate the request into one of its own action names — because the declaration only said "The action to perform", so the model rarely filled it in. When that second call failed, the fallback was `description.lower().replace(" ", "_")`, which turns the Turkish for "turn it down" into `sesi_kis` and straight into "Unknown action". The declaration now names all 56 actions and the rest is spelling tolerance handled locally by `difflib` in microseconds. When nothing matches it suggests real action names instead of dead-ending.
* An unresolvable saved audio device, or one the driver refuses to open, falls back to the system default and says so — on both the microphone and the speakers.
* A rejected session-resumption handle is dropped after one attempt, so an expired handle can never be replayed on every retry and prevent the reconnect it exists to protect.

---

## 🛰️ Hermes Agent Integration

JARVIS now runs alongside **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** (Nous Research) as a full messaging + scheduling layer — installed to `~/.hermes/`, running as a systemd **user service with linger** (survives logout/reboot), and speaking with the same JARVIS persona.

| Component | Setup |
|---|---|
| 🧠 Model | OpenRouter free tier — picked live from the catalog with Hermes' ≥64K-context floor (`inclusionai/ling-3.0-flash-sante:free`, 262K, tools; the old `ling-3.0-flash-vl:free` was retired by OpenRouter 2026-09-24) |
| 🔁 LLM pool | **FreeLLMAPI router** (`~/freellmapi`, `freellmapi.service`, `127.0.0.1:3001/v1`) — pools ~253 free endpoints behind one OpenAI-compatible API with cross-provider failover; wired as Hermes' first fallback + JARVIS' second LLM step; dashboard at `localhost:3001` |
| 🏠 Local last resort | **Ollama** (user-space `~/.local/bin/ollama`, systemd user unit `ollama.service`) serving `qwen2.5:1.5b` on `127.0.0.1:11434` — zero-quota, fully offline final hop in **both** Hermes' fallback chain (hop 7) and JARVIS' `llm_router` (after OpenAI); tool-calling verified, warm turn ≈ 1.7 s (CPU-only) |
| 🎭 Persona | Custom **JARVIS** identity via `~/.hermes/SOUL.md` — the official override that replaces Hermes' built-in "You are Hermes Agent" identity in every run mode. Also set as `display.personality: jarvis` + `agent.system_prompt` |
| 🗣️ Voice | **TTS:** Gemini, voice `Charon` + JARVIS style prompt · **STT:** local faster-whisper `small`, `language: hi`, VAD on |
| 🌏 Timezone | `Asia/Kolkata` |
| 💬 Telegram | Bot **@Algotrade15bot** — inbound + outbound verified; owner allowlisted via `TELEGRAM_ALLOWED_USERS` |
| 💬 WhatsApp | Personal number paired by QR (**self-chat mode**) — inbound + outbound verified; unknown senders held for pairing approval |
| ⏰ Cron | `morning-briefing` — daily **09:00 IST** → Telegram: date, top India/world news, BTC price, tech tip (Hindi, emoji sections) |
| 🔗 Skill bridge | `actions/hermes_task.py` + `hermes_skills.py` — JARVIS delegates any task to Hermes' full agent loop and reads its live skill inventory; every skill Hermes learns from experience is available to JARVIS immediately, nothing to sync |
| 🌐 browser-use skill | `~/.hermes/skills/browser-use/` + `scripts/browser_use_run.py` — one shared runner drives the autonomous browser agent for both JARVIS (action `browser_use_task`) and Hermes (skill); system Chrome, free-tier brain routing |
| 🔁 Session recovery | `~/.hermes/skills/browser-use-relogin/` + `scripts/browser_use_relogin.py` — re-login an expired site session deterministically (no LLM, no quota) from a chmod-600 credential store, verified before saving |
| 📊 TradingAgents analysis | `.venv-tradingagents` + `scripts/trading_agents_run.py` — TauricResearch multi-agent equity research (analyst team → bull/bear debate → risk-adjusted 5-tier rating); JARVIS action `trading_agents` + Hermes skill `trading-agents`; brain on the FreeLLMAPI pool; research only, never executes trades |
| 📥 Message inbox | `scripts/jarvis_inbox.py` — the ONE normalized entry point from Telegram/WhatsApp (via Hermes) into JARVIS: normalizes channel/chat/text/attachments and POSTs to the dashboard's `/api/message`; the reply arm answers back through `hermes send` on the same channel |
| 🪝 Gateway hook | `hermes-hooks/jarvis-pdf-forward` — Hermes gateway hook (`agent:end`) that auto-forwards messaging documents with remember-intent ("remember this document") into JARVIS's RAG inbox; observer only, never blocks the gateway |
| 🧭 Bridge skill | `.agents/skills/jarvis-hermes-bridge/SKILL.md` — self-contained operating guide for the whole two-way bridge: hook install, inbox usage, outbound-action conventions, end-to-end debugging and house rules |
| 📄 OCR | **`pdf-ocr` skill** — scanned/image-only PDFs (typically Telegram documents cached in `~/.hermes/cache/documents/`) get OCR'd on demand. tesseract 5.5.2 + ocrmypdf 16.13.0 built/pip-installed into `~/.local` (no root). Languages: English + **Hindi/Devanagari** (`eng`, `hin`, `_fast` variants). Details below. |

### 📥 The Telegram/WhatsApp → JARVIS document flow

Messaging users don't need JARVIS running a microphone — they message **Hermes**, and the bridge delivers the intent to JARVIS:

```
Telegram/WhatsApp → Hermes gateway → jarvis-pdf-forward hook (agent:end)
    → scripts/jarvis_inbox.py --channel --chat --text --file
    → dashboard /api/message → JARVIS intent layer
        remember-intent → RAG ingest (pdfplumber/PyPDF2 + tesseract OCR + Gemini vision)
        question        → answer from the library, with page citations
    → reply arm → `hermes send` → back to the same chat
```

Install / update the hook after any change (hooks load at gateway startup only):

```bash
cp -r hermes-hooks/jarvis-pdf-forward ~/.hermes/hooks/
hermes gateway restart
```

The hook is deliberately **dumb**: it forwards only when a message mentions an on-disk PDF *and* the caption shows remember-intent (`remember`, `save this`, `add to my library`, `rag`…). All intent decisions live in JARVIS. See `.agents/skills/jarvis-hermes-bridge/SKILL.md` for the full operating guide — including the five-step debug drill (`journalctl` → hook log → inbox exit code → `knowledge/audit.log` → reply arm) and the known gateway quirks (`gateway_platform_event` never fires for documents; Hermes does not walk its fallback chain on 401).

### 📄 Scanned-PDF OCR — `pdf-ocr` skill

Scanned PDFs used to dead-end: Hermes' `read_file` (anydoc converter) treats one image-only page as grounds to reject the whole document (`NeedsOcrError`), and no hosted-OCR key was configured. The Fibonacci Trading PDF (321 pages, one scanned cover) hit exactly this. Three layers now handle it:

1. **Text-layer salvage** (in `~/.hermes/hermes-agent/tools/read_extract.py`, upstream since v0.21.4): when anydoc rejects a PDF and no hosted-OCR route exists, `read_file` extracts all text-layer pages via `pdftotext` instead of failing, and prepends a coverage note naming exactly which pages are images. Verified: 274K chars recovered from the 321-page book.
2. **`pdf-ocr` skill** (`~/.hermes/skills/pdf-ocr/SKILL.md`): teaches Hermes to OCR just the missing page ranges — `ocrmypdf --skip-text -f <first> -l <last> in.pdf /tmp/ocr_out.pdf` — re-read the output, verify with `pdftotext` before claiming success, and save canonical copies as `<name>_OCR.pdf` next to the original.
3. **Hosted OCR** (optional): setting `FIRECRAWL_API_KEY` in `~/.hermes/.env` enables anydoc's Firecrawl Parse route for fully-scanned documents.

**Local OCR toolchain** (all user-space in `~/.local`, no sudo — system dev headers were missing, so leptonica/libjpeg-turbo/libpng/libtiff were built from source): tesseract 5.5.2 (RPATH + tessdata path baked in), leptonica 1.85.0, ocrmypdf 16.13.0 (pip --user). Both binaries are on the gateway's systemd PATH, so they work in every Hermes context.

**Languages:** `eng`, `hin` (tessdata_best) + `eng_fast`, `hin_fast` (tessdata_fast) in `~/.local/share/tessdata`. Hindi verified end-to-end through ocrmypdf (`-l hin+eng`, primary script first) — Devanagari conjuncts, nukta (`बाज़ार`), and Devanagari numerals (`३२०`) all extract correctly. Quality heuristic in the skill: garbage runs of `॥ । ००` mean the scan is too low-res → re-render at `-r 300`.

### 🛠️ Gemini express-key routing (was a local patch, now upstream)

Gemini keys with an `AQ.` prefix are **Vertex express keys**, and Hermes used to force-route them to `aiplatform.googleapis.com` — which 403s when the project lacks the Agent Platform API, even though the very same key is fully served by the standard AI Studio host. As of **v0.21.4** upstream ships the env-gated opt-in this install previously carried as a local patch:

* `HERMES_GEMINI_STUDIO_FOR_EXPRESS=1` (already set in `~/.hermes/.env`) keeps `AQ.` keys on the Studio host **only when the base URL was left implicit** — default behavior is unchanged, and explicitly configured proxies/Vertex bases always win.
* The old scanned-PDF `read_extract.py` patch was likewise superseded by upstream's own text-salvage (see the OCR section above) — no local patches are carried anymore; the pre-update originals live in `~/.hermes/hermes-agent` stash `stash@{0}`.

### 🏠 True-local last resort — Ollama (added 2026-09-26)

Cloud free tiers all share one failure mode: a quota wave (Gemini 429 `RESOURCE_EXHAUSTED`, OpenRouter per-minute caps, retired `:free` models) can exhaust the **entire** fallback chain at once, and the turn dies with whatever the last provider said. Both chains now end in a provider that cannot be rate-limited — a model running on this machine:

* **Install:** fully user-space (no sudo), same convention as the OCR toolchain — binary + libs in `~/.local`, models in `~/.local/share/ollama/models`, service `~/.config/systemd/user/ollama.service` (enabled, starts at boot).
* **Model:** `qwen2.5:1.5b` (986 MB) — the best tool-calling model that fits this machine (no NVIDIA GPU, 3.7 GB RAM). Tool-call format verified against both Ollama's native API and the OpenAI-compatible `/v1` endpoint; warm latency ≈ 1.7 s, cold load ≈ 30 s.
* **Hermes:** appended as fallback hop 7 (`custom` → `http://127.0.0.1:11434/v1`, `model: qwen2.5:1.5b`) after the direct-Gemini bonus pool. `hermes fallback ls` shows all 7.
* **JARVIS:** `core/llm_router.py` step 7 — enabled by `"ollama_enabled": true` (+ optional `"ollama_models"`) in `config/api_keys.json`; fires after Gemini → FreeLLMAPI → OpenRouter → Grok → OpenAI.
* **Honest limits:** CPU-only on this box, so it is the slowest hop and keeps no session context — it is a guaranteed floor (simple turns still get answered during a total cloud outage), not a replacement for the cloud chain.
* **Live drill (2026-09-26, `hermes -z`):** with the primary model 404'd and Ollama promoted to first fallback, a full agent turn — including one terminal tool call — was answered entirely by `qwen2.5:1.5b` (1m57s; Ollama's journal shows the walk: provider probe → native-API retry → shape-reject → 200 with a 2,050-token prompt @ 26 tok/s). Two operational findings: Hermes does **not** walk the chain on 401 auth failures (dead keys are terminal — keep keys valid), and the custom-provider adapter probes `/api/v1/models` once (404 is harmless, it re-resolves via the native API).

### 🧪 Voice round-trip — verified

Synthetic Hindi speech → local whisper STT → agent turn → Gemini TTS → speakers, all through Hermes' own production code paths (`text_to_speech_tool`, `transcribe_recording`, `hermes -z`, `play_audio_file`). One empirical finding: whisper `base` ignores the Hindi language hint and emits Urdu script (same phonetics); `small` produces proper Devanagari — hence `stt.local.model: small`.

An optional cloud-STT upgrade recipe (Groq whisper-large-v3-turbo, free tier) lives at `~/.hermes/GROQ_STT_SETUP.md`.

### 🔧 JARVIS-side fixes in this build

* **Cross-thread `killTimer` crash warning** — the dashboard's uvicorn endpoints fired the phone-connected callback from the wrong thread. `notify_phone_connected()` now goes through a `_phone_conn_sig` Qt signal so the overlay update always lands on the GUI thread.
* **ALSA noise + a SEGV** — device probing now silences fd-2 `paInvalidSampleRate` spam behind `_quiet_stderr()` (opt-out: `JARVIS_AUDIO_VERBOSE=1`), and the libasound error-handler trampoline is kept alive at module level — the original local-variable version left libasound a dangling function pointer that segfaulted at the first underrun.
* **`Plugin rejected: trading_journal.py`** — it is a library, not a plugin; renamed to `plugins/_trading_journal.py` (same convention as `_trading_chart.py`) with imports updated. Plugin discovery is now clean.
* **`qt.text.font.db` spam** — the app font is seeded with an installed mono family so Qt's per-widget font probing stops.

* **`run.sh` X11 boot** — `${WAYLAND_DISPLAY:-}` stops an `unbound variable` abort under `set -u`, and the `libxcb-cursor0` loader fallback (see the 2026-09-27 field notes below) keeps the xcb plugin loadable without sudo.
* **NewsAPI key flow** — a news provider key can be set and tested from the dashboard settings; news no longer depends on the Gemini quota.

### ⌨️ Quick commands

```bash
hermes                      # interactive chat (JARVIS persona)
hermes -z "task"            # one-shot task
hermes gateway status       # messaging service status
scripts/bridge_selftest.py  # two-way bridge self-test: probe-file agent task (out) + inbox ping (in) — exit 0 = healthy
hermes cron list            # scheduled jobs
hermes cron create "0 21 * * *" --deliver telegram "…prompt…"
hermes pairing list         # pending platform authorizations
journalctl --user -u hermes-gateway -f   # live gateway logs
```

---

## 🩹 When the GUI won't start & when keys die — field notes (2026-09-27)

### 💥 `Could not load the Qt platform plugin "xcb"` (Linux boot crash)

Symptom: `bash run.sh` aborts (SIGABRT) right after the startup banner with

`qt.qpa.plugin: Could not load the Qt platform plugin "xcb" … even though it was found` — with `libxcb-cursor0` named in the line just above. Two stacked causes on Ubuntu 22.04:



1. **PyQt6's xcb platform plugin needs `libxcb-cursor0`**, which Ubuntu 22.04 does not preinstall.

2. **`opencv-python` bundles its own Qt**, whose plugin hijacks the process GUI backend and crashes first. JARVIS only uses cv2 for camera capture, so `requirements.txt` now pins **`opencv-python-headless`** (no Qt bundled) — a drop-in as long as no `cv2.imshow` window is ever used.



Fixes, in order of preference:



```bash

sudo apt-get install -y libxcb-cursor0     # the real fix (one-time)

```



No sudo handy? `run.sh` now carries a fallback: if the system copy is missing it prepends `.system-libs/` (a deb-extracted copy of `libxcb-cursor0`) to `LD_LIBRARY_PATH`. The directory is gitignored and can be recreated without root:



```bash

mkdir -p .system-libs && cd .system-libs

apt-get download libxcb-cursor0 && dpkg -x libxcb-cursor0*.deb .

```



The fallback is a harmless no-op once the real package is installed. Verify a fixed boot by listing X windows while the app runs — the HUD shows up as `"JARVIS — MARK LIV"` (`xwininfo -root -tree | grep -i jarvis`). Related: `run.sh` also stopped aborting under `set -u` on X11 sessions (`${WAYLAND_DISPLAY:-}`).



### 🔑 Dead API keys — the rotation drill (2026-09-27)



OpenRouter 401s don't fail over (see the Ollama drill note above — dead keys are terminal), and today **every OpenRouter key on the machine went 401 at once**: both `~/.hermes/.env` slots *and* all five in JARVIS's `openrouter_keys` pool. Hermes kept answering only because fallback hop 1 is the local FreeLLMAPI router. The drill that brought everything back to an 8/8 green health check:



1. **Audit every slot, masked — test, don't assume.** OpenRouter: `GET /api/v1/key` with the bearer. Gemini: `GET /v1beta/models?key=…&pageSize=1`. 200 = live, 401 = dead. Never print full keys in output.

2. **Rotate to keys that already exist on the machine when possible.** JARVIS's `gemini_api_keys[1..5]` were still live, so the dead `GEMINI_API_KEY` (Hermes) and `gemini_api_key` (JARVIS) were repointed at the live pool — no new key needed. (The same dead key was the source of JARVIS's startup `APIError 1008 … invalid authentication credentials`; gone after rotation, first-try connect verified on restart.)

3. **New keys: validate before wiring, then wire ALL slots** — `~/.hermes/.env` (`OPENROUTER_API_KEY`, `OPENROUTER_API_KEY_2`) *and* `config/api_keys.json` (`openrouter_keys` pool). Fixing only the primary leaves the fallback chain fake.

4. **Back up first, edit in place, restart, verify.** Timestamped copies (`*.bak-YYYYmmdd-HHMMSS`), then `hermes gateway restart`, then the `hermes_health` action — expect `Verdict: Hermes is fully connected.`



Scrub any temp file that held raw pasted keys immediately after wiring (the drill's `/tmp/or_live_keys.json` was deleted in the same step).



---



## 📈 Paper Trading System

JARVIS includes a complete paper trading system that lets you practice trading without risking real money. It integrates with the existing trading strategy engine and TradingView.

### Features

| Feature | Description |
|---|---|
| **Paper Positions** | Simulated long/short positions with entry, stop-loss, and staged targets |
| **Automatic Position Sizing** | Positions sized based on ATR-based risk (default 1% of paper capital per trade) |
| **P&L Tracking** | Full profit/loss tracking with win rate, total P&L, and max drawdown |
| **Mistake Detection** | Automatically analyzes losing trades and suggests improvements |
| **Lessons Learned** | Accumulates trading lessons over time to improve future decisions |
| **TradingView Links** | Direct links to TradingView charts for open positions |
| **TradingView Alert Tunnel** | `./tunnel_tv.sh` (+ `systemd/jarvis-tv-tunnel.service`) exposes `/api/tradingview` publicly via ngrok so real TradingView alerts reach JARVIS; current URL always in `memory/trading/tv_webhook_url.txt` |
| **Boot Auto-Start** | `systemd/jarvis-gui.service` (user unit, `graphical-session.target`) launches JARVIS with the desktop session via `jarvis_gui_wrapper.sh` — single-instance guarded; enable linger for pre-login start |
| **Rotation Smoke Check** | `systemd/jarvis-rotation-smoke.sh` (+ `.service`/`.timer`, every 5 min) scans the jarvis-gui journal window since the last run for session-death markers after each GoAway WebSocket rotation — `❌ Recv`/`google.genai.errors`/`websockets.exceptions` inside a rotation, resumption-handle rejections, stuck-retry loops, or a GoAway that never reconnects → `/tmp/jarvis_rotation_smoke.log` + failed unit (exit 1); tool tracebacks are deliberately ignored. On failure it pings desktop (notify-send, critical) AND Telegram (token/chat from `~/.hermes/.env`, `backup_notify.sh` convention) — same failure re-pings only after a 60-min cooldown (`JARVIS_SMOKE_NOTIFY_COOLDOWN`, 0 = always), a different failure pings immediately. Tests: `python3 tests/test_rotation_smoke.py` |
| **Backfill from Journal** | Converts historical signal performance into paper trading learnings |

### How It Works

1. **Signals come from the trading strategy engine** (9 technical indicators vote on direction)
2. **Paper positions are auto-opened** when a signal fires (if paper trading is enabled)
3. **Positions are tracked** with entry price, stop loss, targets, and unrealized P&L
4. **Mistakes are detected** when trades close — tight stops, counter-trend entries, holding too long, etc.
5. **Lessons are saved** and can be reviewed with `paper_learnings`

### Commands

| Command | Action |
|---|---|
| "Check my paper trading status" | Show open positions and performance |
| "Open a paper trade on RELIANCE" | Open a paper position |
| "Close my paper position in TCS" | Close a specific position |
| "Reset paper trading" | Clear all paper positions and stats |
| "What have I learned from trading?" | Show accumulated trading lessons |
| "Backfill trading lessons" | Analyze historical signals for mistakes |

### Paper Trading Files

All paper trading data lives in `memory/trading/`:

- `paper_positions.json` — active paper positions
- `paper_journal.jsonl` — completed trades with outcomes
- `paper_performance.json` — aggregated P&L stats
- `paper_mistakes.jsonl` — detected mistakes with suggestions
- `paper_learnings.json` — accumulated trading lessons

---

## 🤖 Live Auto Trading (Binance)

JARVIS can execute **real automated trades** on Binance based on its trading signals. This is the full automation mode.

### ⚠️ WARNING: Trading involves financial risk

- **Start with paper trading first** — verify the strategy works before using real money
- **Never enable withdrawals on API keys** — for safety, API keys should only trade
- **Start small** — begin with minimal position sizes until you trust the system
- **Monitor regularly** — automated trading needs oversight

### Setup Steps

1. **Create Binance API keys**:
   - Go to Binance → API Management → Create API
   - Enable "Spot & Margin Trading" ONLY
   - **DO NOT** enable "Withdrawals" (never needed for trading)
   - Add IP whitelist if available (recommended)

2. **Add keys to config/api_keys.json**:
   ```json
   {
     "binance_api_key": "your_api_key_here",
     "binance_api_secret": "your_secret_here"
   }
   ```

3. **Test the connection**:
   - Say: "Test Binance connection" → JARVIS verifies API works

4. **Start with paper trading**:
   - Say: "Configure trading mode paper" → JARVIS simulates trades only
   - Run Paper Trading for a few weeks to validate

5. **Enable live trading when ready**:
   - Say: "Enable live trading" → JARVIS can now execute real orders
   - Default: requires confirmation for each trade (safer)
   - Can disable confirmation for fully automatic trading

### Safety Features

| Feature | Description |
|---|---|
| **Daily Loss Limit** | Stops trading if daily loss exceeds threshold (default 2%) |
| **Position Size Cap** | Limits max position per trade (default $1000) |
| **Trade Cooldown** | Minimum time between trades (default 5 min) |
| **Daily Trade Limit** | Max trades per day (default 10) |
| **Confirmation Required** | Even with auto-trading, can require JARVIS to confirm each trade |
| **Whitelist/Blacklist** | Control which symbols can be traded |

### Commands

| Command | Action |
|---|---|
| "Test Binance connection" | Verify API keys work |
| "Show trading status" | Display account, positions, safety settings |
| "Configure trading mode paper" | Switch to paper trading |
| "Configure trading mode live" | Enable live trading |
| "Enable live trading" | Turn on real order execution |
| "Disable live trading" | Turn off real order execution |
| "Set max daily loss 2 percent" | Adjust safety limit |
| "Set max position 500 dollars" | Adjust position cap |

### How Auto-Trading Works

1. **JARVIS analyzes markets** using the 9-indicator strategy engine
2. **When a signal fires** (score above threshold), a trade is prepared
3. **Paper mode**: Trade is simulated and logged
4. **Live mode**: Trade is sent to Binance for execution (with safety checks)
5. **Stop loss & targets** are automatically set on the exchange
6. **All trades are logged** for audit and learning

### Files

- `plugins/binance_trader.py` — Binance API trading engine
- `memory/trading/live_trades.jsonl` — live trade audit log
- `memory/trading/trading_safety.json` — safety configuration
- `memory/trading/trading_state.json` — daily P&L tracking

---

## 🎯 TradingView Integration

The Trading Strategy plugin includes TradingView-style charting:

- **Auto-drawn charts** with support/resistance levels, EMAs, and trade entry/exit levels
- **Interactive live charts** via the dashboard (`/chart?symbol=RELIANCE.NS&method=swing`)
- **TradingView links** for open paper positions (NSE, BSE, Binance, US symbols)
- **Chart PNGs** saved to `memory/trading/charts/` and accessible via the dashboard

### Supported Markets

| Market | Examples | Session |
|---|---|---|
| India Equities | RELIANCE.NS, TCS.NS, INFY.NS | NSE: 09:15–15:30 IST |
| India Indices | ^NSEI, ^BSESN | NSE: 09:15–15:30 IST |
| Crypto | BTC-USD, ETH-USD, SOL-USD | 24/7 |
| US/Global | AAPL, MSFT, NVDA, SPY | US market hours |

---

## 🗺️ Mark Roadmap

| Mark | Focus |
|---|---|
| **XLIX** | Auto-start · clipboard intelligence · assistant customization |
| **L** | Session memory · background monitoring · proactive 2.0 · instant vision |
| **LI** | Plugin system · affective dialog · proactive audio · unlimited sessions |
| **LII** | Voice picker · live theming · reactive HUD · recallable memory · undo · real confirmation · audio device picker · session continuity |
| **LIII** | Wake word · Gemini 3.1 Flash Live · instant acknowledgment · self-describing action/plugin architecture |
| *shared* | The last five above also shipped to LIII, LIV and LV at the same time — moving up a Mark never loses them |
| **LIV+** | Plugin files: email · quiz mode · calendar · home assistant · 3D-printer · and more |

---

## ⚡ Quick Start

```bash
git clone https://github.com/FatihMakes/Mark-LIII.git
cd Mark-LIII
python setup.py        # installs deps for YOUR OS + the browser automation engine
python main.py
```

`setup.py` only ever installs what your operating system needs — the Windows-only libraries are skipped automatically on macOS and Linux (and vice-versa). Prefer to do it by hand? `pip install -r requirements.txt` works too.

> ⚠️ **Installation Note:** If you hit a `ModuleNotFoundError` for an OS-specific package, install it with `pip install <module_name>`. The optional **wake word** engine is *not* installed here — grab it in one click from **⚙ → WAKE WORD** inside the app.

---

## 📋 Requirements

| Requirement | Details |
| --- | --- |
| **OS** | Windows 10/11, macOS, or Linux |
| **Python** | 3.11 or 3.12 |
| **Microphone** | Required for voice interaction (and for the "Hey Jarvis" wake word) |
| **Speakers** | Required for voice replies |
| **API Key** | Free Gemini API key (entered on first launch → `config/api_keys.json`) |
| **Wake word** *(optional)* | One-click download from ⚙ → WAKE WORD (`openwakeword`, a few MB, fully local) |

---

## 🗂️ Project Structure

```
Mark LIII/
├── main.py                   # Core loop — Gemini Live session, audio I/O, wake/sleep state, tool dispatch
├── ui.py                     # PyQt6 HUD — reactive waveform, log panel, settings drawer, plugin manager, camera feed
├── setup.py                  # OS-aware installer (skips wrong-OS dependencies)
├── plugins/
│   ├── _template.py          # Copy this to write a new plugin — one file, drop in, done
│   └── ...                   # Drop-in skills (each self-describes via a PLUGIN dict + run())
│   ├── browser_use_task.py   # Bridge: autonomous browser agent (browser-use) for multi-step web tasks
│   ├── browser_use_relogin.py # Bridge: voice-triggered session recovery (re-login an expired site)
│   ├── browser_control.py    # Web browser control
│   ├── web_search.py         # Gemini + DDG parallel search (news, research, price, compare)
│   ├── screen_processor.py   # Screen & webcam capture for vision
│   ├── background_monitor.py # User-configured topic watching — daily DDG check, no crypto
│   ├── proactive.py          # Proactive 2.0 — time/context/rotation-aware check-ins
│   ├── reminder.py           # OS-native scheduled notifications
│   ├── system_monitor.py     # CPU / RAM / GPU / temperature telemetry
│   ├── computer_settings.py  # Volume, brightness, WiFi, power (per-OS)
│   ├── computer_control.py   # Keyboard shortcuts, mouse, window management
│   ├── open_app.py           # Application launcher (per-OS name map)
│   ├── browser_control.py    # Web browser control
│   ├── file_controller.py    # File system operations
│   ├── file_processor.py     # Document reading and summarization
│   ├── send_message.py       # Messaging integration
│   ├── weather_report.py     # Live weather data
│   ├── flight_finder.py      # Flight search
│   ├── youtube_video.py      # YouTube playback control
│   ├── game_updater.py       # Game update management (Steam / Epic)
│   ├── code_helper.py        # Code review and generation
│   ├── dev_agent.py          # Developer task agent
│   ├── hermes_task.py        # Bridge: delegate a task to Hermes' full agent loop
│   ├── hermes_skills.py      # Bridge: read Hermes' live skill inventory
│   ├── hermes_health.py      # Bridge: 5-probe Hermes connectivity check
│   ├── knowledge_base.py     # RAG interface — search / ingest / forget / list with citations
│   └── desktop.py            # Desktop and taskbar control
├── hermes-hooks/
│   └── jarvis-pdf-forward/   # Hermes gateway hook — forwards remember-intent PDFs into JARVIS (copy to ~/.hermes/hooks/)
├── memory/
│   ├── memory_manager.py     # Load/save long_term.json — sessions, monitors, identity
│   ├── config_manager.py     # api_keys.json access — key, OS, name, voice, colour, toggles
│   └── long_term.json        # Persistent store: identity, preferences, projects, sessions, monitors
├── core/
│   ├── prompt.txt            # Assistant personality and tool-routing rules
│   ├── undo.py               # One shared undo stack — actions register how to reverse themselves
│   ├── confirm.py            # Irreversible-action gate — the token is issued by the UI, not the model
│   ├── audio_devices.py      # Microphone / speaker list — filtered, measured, resolved by name
│   ├── plugin_loader.py      # Plugin engine — discovery, validation, crash isolation
│   ├── action_loader.py      # Bundled-action engine — the built-in twin of plugin_loader
│   ├── llm_router.py         # Free-tier LLM ladder — Gemini → FreeLLMAPI → OpenRouter → Grok → OpenAI → Ollama
│   ├── llm_local.py          # Local rung — Qwen2.5-Coder-3B GGUF via llama-cpp-python, RAM-gated, never resident
│   ├── rag.py                # RAG knowledge layer — ingest (OCR + vision), search with citations, audit
│   └── wake_word.py          # Local "Hey Jarvis" detector — own thread, offline, opt-in
├── scripts/
│   └── jarvis_inbox.py       # The ONE entry point from Hermes/Telegram/WhatsApp into JARVIS (dashboard POST)
└── config/
    └── api_keys.json         # API key, OS setting, assistant name, user name, voice, UI colour, toggles
```

---

## 💾 Backup & Restore

Full-system backup/restore scripts live in `scripts/`:

````
# Backup (slim mode, ~377 MB): stops services, archives Mark-LIII + ~/.hermes +
# ~/freellmapi (LLM router: code + builds + SQLite keys + ENCRYPTION_KEY .env) +
# ~/.local (bin/lib/tessdata/uv runtime) + systemd units + crontab, restarts on
# exit — with per-unit restart checks (a unit that fails to come back is called
# out in the log with its journal command)
scripts/export_system.sh /destination/dir

# Drill restore into an isolated fake home — safe, no live data touched:
TARGET_HOME=/tmp/fakehome scripts/restore_system.sh <archive.tar.zst>

# REAL restore (REPLACES live dirs — requires explicit confirmation + services stopped):
CONFIRM_RESTORE=yes scripts/restore_system.sh <archive.tar.zst>
````

Slim archives skip venvs/pip packages (rebuilt on restore; needs network) and
freellmapi's node_modules/.git (`npm install` on restore) but always keep the uv
Python runtime, compiled OCR libs (.local/lib) and all data — including the
freellmapi SQLite (its stored provider keys) and the `.env` ENCRYPTION_KEY those
keys are encrypted with; the pair must travel together or the keys are lost.
Use `--full` for a byte-for-byte offline-capable archive. Archives contain API
keys — treat them as secrets.

A **weekly cron** (Sundays 04:00) runs `weekly_backup.sh`: slim export into
`~/backups` with `--prune=30`, then offsite upload — newest archive encrypted
client-side (AES-256, key `~/.backup_offsite.key`) and pushed to **Cloudflare
R2** (`jarvis-hermes-backups` bucket, APAC; rclone remote `r2-backups`), with
60-day offsite retention. After every run a **Telegram summary** (via the
Hermes bot) reports per-stage status — 🟢 OK or 🔴 FAILURE with the log tail —
so failures reach the phone, not just the log. Offsite copy works only with the local key file
(`~/.backup_offsite.key`) — if the disk dies AND the key file is lost, the
cloud copy is unreadable. **A paper copy of the key exists** (hand-copied
recovery card, 16×4-char groups, fingerprint-checked; validate a paper copy
by typing it into the fingerprint checker — never re-type it into a file).
Log: `~/backups/cron.log` — each run ends with an **R2 usage report**
(object count, total size, % of the 10 GB free tier, ⚠️ above 80%).
Manual: `scripts/offsite_backup.sh --verify=<remote .enc>`.

---

## 📱 Android App (APK)

The phone dashboard ships as a native Android wrapper around the existing PWA —
same UI, plus mic-permission pass-through for voice, file-upload support, and
trust for the dashboard's local TLS CA out of the box.

````
scripts/build_apk.sh        # one-time SDK bootstrap (~1.5GB in ~/Android) + build
                            # → dist/JARVIS-Remote-1.0.apk (signed, sideload-ready)
````

* The dashboard LAN IP is baked in at build time; the in-app **URL** button (bottom-right) retargets it if the IP ever changes.
* If certs were regenerated (`make_certs.sh`), rebuild the APK so the new CA is bundled — or accept the in-app TOFU prompt once.
* Signing key: `android/jarvis-release.keystore` (gitignored) — back it up, or future installs won't upgrade in place.

---

## ⚠️ License

Personal and non-commercial use only.
Licensed under **[Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)**.

---

## 👤 Connect with the Creator

Engineered by a developer building a real-world JARVIS-style assistant.
⭐ **Star the repository to support the journey to Mark 100.**

| Platform | Link |
| --- | --- |
| YouTube | [@FatihMakes](https://www.youtube.com/@FatihMakes) |
| Instagram | [@fatihmakes](https://www.instagram.com/fatihmakes) |
