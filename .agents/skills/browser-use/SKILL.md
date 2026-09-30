---
name: browser-use
description: >-
  Operate the browser-use agent integration shared by JARVIS and Hermes:
  run autonomous multi-step browser tasks on the system Chrome, route the
  agent's LLM brain through FreeLLMAPI → Gemini → Ollama, extend or debug
  the integration. Use when a browser task should be delegated to the
  agent, when the runner fails, or when changing brain routing or action
  wiring. Triggers: "browser agent", "browser-use", "automate this in the
  browser", "agent browsing task".
---

# browser-use integration — JARVIS + Hermes

One runner, two consumers (added 2026-09-26):

```scripts/browser_use_run.py            ← the only place that talks to browser-use
├── actions/browser_use_task.py     ← JARVIS action (TOOL dict, voice path)
├── actions/browser_use_relogin.py  ← JARVIS action: voice-triggered session recovery
├── scripts/browser_use_relogin.py  ← session recovery (deterministic, no LLM)
├── scripts/site_credentials.template.json ← schema for the chmod-600 login store
├── ~/.hermes/skills/browser-use/           ← Hermes skill (CLI path)
└── ~/.hermes/skills/browser-use-relogin/   ← Hermes skill (session recovery)
```

## Quick start

```bash
# Direct (what both consumers shell out to):
.venv/bin/python scripts/browser_use_run.py "Go to news.ycombinator.com and list the top 3 headlines" --max-steps 10

# From Hermes:
hermes -z "Use the browser-use skill: check example.com's heading and report it"
```

## Architecture

| Piece | File | Notes |
|---|---|---|
| Runner | `scripts/browser_use_run.py` | asyncio entry; brain pick + Chrome flags + step budget; exit 0/1 |
| JARVIS action | `actions/browser_use_task.py` | self-describing `TOOL`, auto-discovered by `core/action_loader.py`; 300 s cap |
| Hermes skill | `~/.hermes/skills/browser-use/SKILL.md` | frontmatter matches the `pdf-ocr` house format; `verified: true` |
| Library | `browser-use` 0.13.10 (venv) | pinned in `requirements.txt` |
| Browser | `/usr/bin/google-chrome` | headless default; `--no-sandbox --disable-dev-shm-usage` |
| Persistence | `~/.local/share/jarvis-browser-profile/storage_state.json` | cookies + localStorage reload every run; explicit save-flush before kill; `--no-persist` opts out. Do NOT use `user_data_dir=` for this: browser-use copies it into a throwaway temp dir and never copies back (Chrome-lock safety). The file holds live session cookies — never commit or export it |

## Brain routing (verified 2026-09-26)

1. **FreeLLMAPI** `127.0.0.1:3001/v1` model `auto` — free pool, primary.
   Live smoke: example.com task completed in 2 steps.
2. **Gemini** direct `gemini-3.8-flash` — fallback; `gemini-2.5-flash` was
   retired for new users (404, 2026-09) and 3.8 has hit 503 high-demand
   waves; that is exactly why FreeLLMAPI is first.
3. **Ollama** `qwen2.5:1.5b` — zero-quota local floor; weak tool-caller,
   simple tasks only.

Keys come from `config/api_keys.json` — never hardcode, never env-vars.

## Debug drill

1. Run the runner directly — is the failure in browser-use or the consumer?
2. `curl -s -H "Authorization: Bearer $(jq -r .freellmapi_api_key config/api_keys.json)" http://127.0.0.1:3001/v1/models | head -c 120` — brain 1 alive?
3. `journalctl --user -u freellmapi -n 20` — pool errors?
4. `--visible` reproduces headless-only page states; `--brain gemini`
   isolates FreeLLMAPI-specific failures.
5. `pip show browser-use` inside `.venv` — library present/upgraded?
6. Logins lost between runs? Check `~/.local/share/jarvis-browser-profile/storage_state.json` exists and grows after tasks (`ls -la`); the save-flush line `cookies saved -> …` must appear on the runner's stderr.
7. A task ends on a login page? The site's saved session expired — recover
   with `scripts/browser_use_relogin.py SITE_KEY --clear` (credentials in
   `~/.local/share/jarvis-browser-profile/site_credentials.json`, chmod 600;
   schema template committed at `scripts/site_credentials.template.json`).
   Verified 2026-09-26: forced expiry → re-login → headless run logged in.

A browser-use minor bump may rename agent APIs (`AgentHistoryList`
renamed `.steps` → `.agent_steps` before) — the runner only touches the
stable surface (`final_result()`, `errors()`), keep it that way.
