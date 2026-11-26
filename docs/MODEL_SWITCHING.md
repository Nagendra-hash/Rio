# Model Switching — local vs cloud

> Companion to `ARCHITECTURE.md`. Status: **Built** · Tool:
> `actions/model_control.py` · Voice intents: `core/voice_router.py` ·
> Reconnect bridge: `core/runtime_control.py` · Tests:
> `tests/test_voice_router.py`, `tests/test_voice_commands.py`

---

## 1. What "the model" means here

There are two independent layers, and the status answer names both:

| Layer | Setting | Local choice | Cloud choice |
|---|---|---|---|
| **Conversation engine** (what you talk to) | `provider_mode` in `config/api_keys.json` | `pipeline` — Whisper → Ollama → system voice | `gemini` — Gemini Live |
| **One-shot text routing** (background calls) | `router_mode` in `config/api_keys.json` / `.env` | `local` (Ollama first) | `free`, `smart`, or `gemini` |

Hermes has its own single switch in `~/.hermes/config.yaml`:
`model.provider` + `model.default`. Its `router` provider already points at
`http://localhost:11434/v1`, so the local route needs no new service.

## 2. Voice phrases

The Live model is told about the tool. The phrase router is the offline
backup, and its intents are ordered so a question never becomes a command.

| Phrase | Effect |
|---|---|
| "which model are you using" · "is Hermes on local" · "local or cloud" | Report JARVIS engine, router mode, Ollama state, and Hermes' model |
| "use the local model" · "go local" · "run locally" | Switch **both** to local |
| "switch to cloud" · "back to cloud" · "use Gemini" | Switch **both** to cloud |
| "use Jarvis local" · "switch Jarvis to local" | JARVIS only |
| "use Hermes local" · "use the local model for Hermes" | Hermes only |
| "use Hermes cloud" · "switch Hermes to cloud" | Hermes only |
| "use the local model qwen2.5:1.5b" | Pick a named Ollama model |
| "use cloud model nvidia/nemotron-3-ultra-550b-a55b" | Pick a named Hermes cloud model |
| "use free models" · "switch to smart mode" | Change JARVIS text routing only |

## 3. What a switch actually writes

**JARVIS → local**

1. Validates the three providers before touching config:
   `openai_compat@ollama` (`http://127.0.0.1:11434/v1`, model chosen from
   Ollama's live `/api/tags`), `whisper@local`, `system_tts@system`.
2. Saves their configs and sets the pipeline slots.
3. Sets `provider_mode = "pipeline"` and `router_mode = "local"`.
4. Remembers the previous cloud mode in
   `memory/model_control_state.json`.
5. Asks the live session to rebuild after 2.5 s, so the **current** engine can
   finish speaking the confirmation first.

**JARVIS → cloud**

1. Requires a Gemini key (reports honestly if none is configured).
2. Sets `provider_mode = "gemini"` and restores the remembered router mode
   (falling back to `smart`).
3. Rebuilds the live session with context preserved.

**Hermes**

Uses `hermes config set model.provider` and `model.default`. Local sets
provider `router` with the chosen Ollama model; cloud restores the remembered
`provider`/`model` pair. The change lands on Hermes' next task or CLI run
(there is no gateway restart).

## 4. Prerequisites for local JARVIS

- Ollama running: `systemctl --user start ollama` (listens on
  `127.0.0.1:11434`); at least one model pulled.
- `faster-whisper` installed for the local STT backend, plus a system voice
  backend (`spd-say` / `espeak-ng`).
- A model with **tool-calling** support for best results; `model_control` is
  `tier: core`, so it survives a tight local tool budget and the user can
  always switch back by voice.

## 5. Local model performance caveats

Local inference on CPU is **orders of magnitude slower** than cloud:

| Task | Cloud (Gemini) | Local (qwen2.5:1.5b, CPU) |
|---|---|---|
| Simple reply | <1s | ~10–30s |
| Tool call + reply | ~2s | ~30–60s+ |
| Context window | 1M+ tokens | 2k–8k tokens |

The timeout for local models has been increased to **300 s** (5 min) so slow
inference does not abort mid-turn. However, real-time conversation remains
 Impractical — use local mode for background tasks (coding agents, research,
 batch jobs) and keep cloud for live voice interaction.

Say **"switch to cloud"** to return to responsive Gemini Live. The
`model_control.status` response now warns about this trade-off explicitly.

## 5. Honest fallbacks

- No live session registered (headless/tests): settings are still saved and
  the tool says "It will take effect on the next start."
- Ollama unreachable: the switch is **refused** with the reason; nothing is
  half-written.
- No Gemini key: cloud switch is refused with what to add.
- Status reports what is configured, not what is merely installed.
