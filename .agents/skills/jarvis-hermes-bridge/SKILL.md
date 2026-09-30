---
name: jarvis-hermes-bridge
description: >
  Operate and extend the two-way JARVIS (Mark-LIII) ↔ Hermes Agent bridge —
  the messaging + scheduling layer that lets Telegram/WhatsApp users talk to
  JARVIS through Hermes, lets JARVIS delegate tasks to Hermes' full agent
  loop, and forwards remember-intent documents into JARVIS's RAG library.
  Use when adding a Hermes gateway hook, wiring a new Hermes skill into
  JARVIS, debugging the inbox → dashboard → reply flow, or changing anything
  under hermes-hooks/, scripts/jarvis_inbox.py, or actions/hermes_*.py.
  Triggers: "hermes hook", "hermes bridge", "jarvis inbox", "telegram pdf",
  "forward document to jarvis", "hermes skill for jarvis".
compatibility: "Linux (systemd user services). Requires a running Hermes install at ~/.hermes with the `hermes` CLI on PATH, and the JARVIS dashboard reachable at 127.0.0.1:8000. Python 3.10+; no extra pip packages beyond the repo's requirements.txt."
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
---

# JARVIS ↔ Hermes Bridge

The bridge is **two pipes plus one hook**, all in this repo:

```
                 OUTBOUND (JARVIS → Hermes)                INBOUND (Hermes → JARVIS)
┌──────────────────────────────────────────────┐  ┌─────────────────────────────────────────────┐
│ actions/hermes_task.py    (delegate a task)  │  │ hermes-hooks/jarvis-pdf-forward/            │
│ actions/hermes_skills.py  (list skills)      │  │   (gateway hook, event: agent:end)          │
│ actions/hermes_health.py  (5-probe health)   │  │        │                                    │
│        │ `hermes -z "<task>"`                │  │        ▼                                    │
│        ▼                                     │  │ scripts/jarvis_inbox.py                     │
│ ~/.hermes → Hermes agent loop                │  │   --channel --chat --text --file            │
│   (skills, terminal, browser)                │  │        │ multipart POST                     │
└──────────────────────────────────────────────┘  │        ▼                                    │
                                                  │ dashboard /api/message → intent layer       │
                                                  │   remember → RAG ingest (OCR + vision)      │
                                                  │   question → answer from library            │
                                                  │        ▼                                    │
                                                  │ `hermes send` back on the same channel      │
                                                  └─────────────────────────────────────────────┘
```

JARVIS is the head: intent decisions live in JARVIS (`core/prompt.txt`,
`actions/knowledge_base.py`), never in the hook. The hook is deliberately dumb
and the inbox script is deliberately stateless.

## 1. Install / reinstall the hook

```bash
cp -r hermes-hooks/jarvis-pdf-forward ~/.hermes/hooks/
hermes gateway restart          # hooks load at gateway startup only
journalctl --user -u hermes-gateway -f | grep -i pdf-forward
```

Directory hooks in `~/.hermes/hooks/` are trusted code — no consent allowlist
dance (that only applies to shell hooks). The hook fires on `agent:end` (NOT
`gateway_platform_event`, which never fires for documents) and forwards only
when **both** are true:

1. the turn's message mentions a PDF path that still exists on disk, and
2. the caption shows remember-intent (`remember`, `save this`,
   `add to my library`, `rag`, `ingest`, `index this` — intentionally
   conservative; a bare "look at this" must stay the read-once path).

It then runs `scripts/jarvis_inbox.py` with a fixed env
(`HOME=/home/nagendra`, minimal PATH) — edit `_JARVIS_ENV` in the handler when
the username changes. Every failure is swallowed and logged; a hook must never
break the gateway.

## 2. The inbox entry point

`scripts/jarvis_inbox.py` is the ONE normalized entry point from messaging
platforms into JARVIS. One call, four flags:

```bash
# document + caption (the pdf flow — what the hook does):
scripts/jarvis_inbox.py --channel telegram --chat <chat_id> \
    --file "/path/hermes-saved/report.pdf" --text "remember this document"

# text only (e.g. from a custom Hermes skill):
scripts/jarvis_inbox.py --channel telegram --chat <chat_id> --text "analyze NIFTY"
```

Config via env (both optional):
- `JARVIS_DASHBOARD_URL` — default `https://127.0.0.1:8000`
- `JARVIS_DASHBOARD_TOKEN` — else read from `config/api_keys.json`
  (`dashboard_auth_token`)

Exit codes: `0` delivered, `1` failed (reason on stderr). Delivery is
loopback with an unverified TLS context by design — the token is the
credential and the connection never leaves the machine.

## 3. Outbound: JARVIS → Hermes

Three self-describing actions (`TOOL` dict + `run()` shape, discovered
automatically by `core/action_loader.py`):

| Action | Use | CLI behind it |
|---|---|---|
| `hermes_task` | delegate a full multi-step task; returns the final answer | `hermes -z "<task>"` (timeout 240 s) |
| `hermes_skills` | list/filter Hermes' live skill inventory | `hermes skills list` / `skills search <q>` (timeout 30 s) |
| `hermes_health` | 5 probes: systemd unit, CLI, Telegram, OpenRouter, FreeLLMAPI (+optional deep `hermes -z`) | `systemctl --user`, `hermes --version`, … |

Conventions all three share — keep them in any new bridge action:

- Locate the CLI with `shutil.which("hermes") or "/home/nagendra/.local/bin/hermes"`.
- Run subprocesses with `capture_output=True, text=True`, a hard timeout, and
  a fixed env (`PATH` includes `~/.local/bin` and `~/.hermes/node/bin`,
  `HOME=/home/nagendra`) — the GUI process must never hang its audio loop and
  never inherits a hostile environment.
- Timeouts map to exit `124`; missing CLI to `127`; both get friendly text.
- No new pip dependencies — stdlib only.

To expose a NEW Hermes skill to JARVIS: either teach Hermes the skill (it is
self-improving — it appears in `hermes_skills` immediately, nothing to sync),
or add one small action file in `actions/` that shells out to the specific
`hermes …` subcommand, following the table above. JARVIS picks it up on next
launch.

## 4. Debug the end-to-end pdf flow

Work from the bottom up; the first layer that lies is the one that's broken.

```bash
# 1. hook loaded? (gateway startup logs list registered hooks)
journalctl --user -u hermes-gateway -b | grep -i hook

# 2. hook fired + forwarded? (its own stdout lands in the gateway journal)
journalctl --user -u hermes-gateway -f | grep '\[jarvis-pdf-forward\]'

# 3. inbox delivered to the dashboard?
scripts/jarvis_inbox.py --channel telegram --chat 1 --text "ping" ; echo $?

# 4. did JARVIS ingest? (audit trail of the library)
tail -f knowledge/audit.log

# 5. reply arm ran? (hermes send back to the platform)
journalctl --user -u hermes-gateway -f | grep -i 'send'
```

Common failure modes:

- **`gateway_platform_event` misconception** — it does NOT fire for incoming
  documents, only reactions/edits. New media hooks must use `agent:end`.
- **Lazy vs greedy path regex** — a PDF path containing spaces must be
  captured from the FIRST slash (`_PDF_RE_GREEDY` runs before the lazy
  variant); every candidate is existence-checked anyway.
- **Hermes doesn't walk the fallback chain on 401** — dead keys are terminal.
  If turns die silently, check `hermes fallback ls` and key validity first.
- **Dashboard down** → inbox exits 1 with "connection refused"; the hook logs
  it and moves on. Start JARVIS, retry.

## 5. House rules

- The hook forwards; it never decides. Intent logic belongs to JARVIS.
- The inbox is stateless — no queues, no retries beyond the single POST.
- Everything user-visible gets audited (`knowledge/audit.log` for the library;
  `memory/llm_usage.jsonl` for LLM hops).
- Long-running inbound work (OCR, embeddings) happens inside JARVIS after
  delivery, never inside the hook — the hook's 30 s budget is for the handoff
  only.
- Secrets stay in `config/api_keys.json` / `~/.hermes/.env`; never hardcode
  tokens in hooks or actions, never log them.
