#!/usr/bin/env python3
"""
browser_use_run.py — shared runner for the browser-use agent skill.

Runs a natural-language browser task through the browser-use agent loop
(https://github.com/browser-use/browser-use) and prints the final answer.

Model routing (the same free-tier philosophy as the rest of the house):
  1. FreeLLMAPI  — 127.0.0.1:3001/v1, model 'auto' (~253 pooled free
     endpoints, zero quota burn on Gemini). Primary, verified 2026-09-26:
     example.com task completed in 2 steps.
  2. Gemini      — direct, model from --model (default gemini-3.8-flash;
     the 2.5 generation was retired for new users 2026-09).
  3. Ollama      — qwen2.5:1.5b on 127.0.0.1:11434, the zero-quota local
     floor (weak tool-caller: expect simple tasks only).

The browser is the SYSTEM Chrome at /usr/bin/google-chrome (headless by
default; --visible for demos/debugging). No playwright download needed.

Profile persistence: by default every run loads and saves a shared storage
state (cookies + localStorage) at ~/.local/share/jarvis-browser-profile/
storage_state.json, so logins survive between tasks. browser-use copies any
user_data_dir into a throwaway temp dir at launch (never back — Chrome lock
safety), so the supported persistence primitive is the storage-state file:
the StorageStateWatchdog loads it on connect and auto-saves on cookie change;
the runner also dispatches an explicit save before kill so short runs don't
lose cookies to the 30 s poll. Pass --no-persist for a clean ephemeral
session. The state file holds live session cookies — treat it like a secret:
it lives outside the repo and is never exported by the weekly backup.

Usage:
  python3 scripts/browser_use_run.py "task text" [--max-steps N] [--visible]
                                        [--brain auto|gemini|ollama]
                                        [--no-persist]

Output: the agent's final answer on stdout; progress on stderr. Exit 0 on
success, 1 on failure — the callers (JARVIS action / Hermes) rely on this.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_CONFIG = BASE / "config" / "api_keys.json"
_CHROME = "/usr/bin/google-chrome"
_OLLAMA_URL = "http://127.0.0.1:11434/v1"

_FREELLM_URL = "http://127.0.0.1:3001/v1"
_DEFAULT_MODEL = "gemini-3.8-flash"   # 2.5 generation retired for new users
_OLLAMA_MODEL = "qwen2.5:1.5b"

# Persistent storage state (cookies + localStorage), saved by the
# StorageStateWatchdog and reloaded on every run. Outside the repo
# deliberately - the file holds live session cookies (a secret).
_PROFILE_DIR = Path.home() / ".local" / "share" / "jarvis-browser-profile"
_STORAGE_STATE = _PROFILE_DIR / "storage_state.json"


def _key(name: str) -> str:
    try:
        return (json.loads(_CONFIG.read_text(encoding="utf-8")).get(name) or "").strip()
    except Exception:
        return ""


def _build_llm(brain: str, model_override: str | None = None):
    """Return (llm, brain_name) trying brains in order of preference."""
    from browser_use import ChatGoogle, ChatOpenAI

    gemini_model = model_override or _DEFAULT_MODEL

    order = {
        "auto":   ["freellm", "gemini", "ollama"],
        "gemini": ["gemini", "freellm", "ollama"],
        "ollama": ["ollama", "freellm", "gemini"],
    }[brain]

    last_err: Exception | None = None
    for name in order:
        try:
            if name == "freellm":
                key = _key("freellmapi_api_key")
                if not key:
                    raise RuntimeError("no freellmapi_api_key in config")
                return ChatOpenAI(model="auto", api_key=key,
                                  base_url=_FREELLM_URL), name
            if name == "gemini":
                key = _key("gemini_api_key")
                if not key:
                    raise RuntimeError("no gemini_api_key in config")
                return ChatGoogle(model=gemini_model, api_key=key), name
            if name == "ollama":
                return ChatOpenAI(model=_OLLAMA_MODEL, api_key="ollama",
                                  base_url=_OLLAMA_URL), name
        except Exception as e:               # missing key / bad import shape
            last_err = e
    raise RuntimeError(f"no usable LLM brain ({brain=}): {last_err}")


async def _run(task: str, max_steps: int, headless: bool, brain: str,
               persistent: bool = True, model_override: str | None = None) -> str:
    from browser_use import Agent, BrowserSession
    from browser_use.browser.events import SaveStorageStateEvent

    llm, used = _build_llm(brain, model_override)
    print(f"[browser-use] brain: {used}", file=sys.stderr)

    session_kwargs: dict = {}
    if persistent:
        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        # Existing cookies load automatically; a fresh file means a first run.
        print(f"[browser-use] storage state: {_STORAGE_STATE} "
              f"({'existing' if _STORAGE_STATE.exists() else 'new'})",
              file=sys.stderr)
        session_kwargs["storage_state"] = str(_STORAGE_STATE)
    else:
        print("[browser-use] storage state: ephemeral (--no-persist)",
              file=sys.stderr)

    session = BrowserSession(
        headless=headless,
        executable_path=_CHROME,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
        **session_kwargs,
    )
    try:
        agent = Agent(task=task, llm=llm, browser_session=session)
        history = await agent.run(max_steps=max_steps)
        if history.errors():
            print(f"[browser-use] step errors: {history.errors()[:2]}",
                  file=sys.stderr)
        # Flush cookies now — the watchdog's periodic save can be up to 30 s
        # behind, and the session dies right after this.
        if persistent:
            try:
                await session.event_bus.dispatch(SaveStorageStateEvent())
                print(f"[browser-use] cookies saved -> {_STORAGE_STATE}",
                      file=sys.stderr)
            except Exception as e:
                print(f"[browser-use] ⚠️ storage-state save failed: {e}",
                      file=sys.stderr)
        return history.final_result() or ""
    finally:
        await session.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a browser-use agent task")
    ap.add_argument("task", help="natural-language browser task")
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--visible", action="store_true",
                    help="show the browser window (default: headless)")
    ap.add_argument("--brain", choices=["auto", "gemini", "ollama"], default="auto",
                    help="LLM backend preference (default: auto = freellm→gemini→ollama)")
    ap.add_argument("--no-persist", action="store_true",
                    help="use a throwaway profile instead of the persistent one "
                         "(logins will NOT survive)")
    ap.add_argument("--model", default=None,
                    help="override the Gemini model used by the 'gemini' brain "
                         "(per-model free-tier quotas; e.g. gemini-3.5-flash)")
    args = ap.parse_args()

    try:
        answer = asyncio.run(_run(args.task, args.max_steps,
                                  headless=not args.visible, brain=args.brain,
                                  persistent=not args.no_persist,
                                  model_override=args.model))
    except Exception as e:
        print(f"browser-use failed: {e}", file=sys.stderr)
        return 1

    if not answer.strip():
        print("browser-use finished without a final answer.", file=sys.stderr)
        return 1
    print(answer.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
