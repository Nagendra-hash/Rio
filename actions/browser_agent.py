"""
browser_agent.py — the RIO Browser Agent's tool entry point.

Voice and dashboard turns reach this file through the normal tool pipeline
(`core/action_loader.py` discovers the module-level `TOOL`). The actual work
lives in `core/browser_agent/` so the orchestrator agent, this action and the
legacy `browser_use_task` name all share one implementation, one policy and
one job log.

TIMING
    `_execute_tool` is awaited inside the Live receive loop, and a turn with no
    traffic for 30 s trips the session watchdog. So this handler returns in
    milliseconds with `outcome.started(...)`; a daemon thread waits for the
    real result and announces it through `player.request_say` / `speak`
    (the `actions/youtube_agent.py` pattern). A static single-page read is
    allowed a short inline wait because it finishes in about a second.

PERMISSIONS
    `core/browser_agent/policy.py` decides. A risky task (send, publish, buy,
    trade, transfer, delete, security settings, sensitive submission) is never
    executed here: it goes to the HUD confirmation when one is bound, or to the
    chat the request came from (`player.inbound_target`, set by main.py's
    Hermes reply arm), where a plain "yes" resolves it. A refused method
    (CAPTCHA/MFA bypass, cookie export, impersonation) is refused outright.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from core import outcome
from core.browser_agent import approvals, policy, runtime
from core.browser_agent.config import load_config

_TIMEOUT_PAD_S = 60.0
_INLINE_HTTP_WAIT_S = 12.0


# ── helpers ─────────────────────────────────────────────────────────────────


def _target(player) -> str:
    """`channel:chat` of the inbound message this turn belongs to, if any."""
    for attr in ("inbound_target", "_inbound_target"):
        value = getattr(player, attr, None) if player is not None else None
        if value:
            return str(value)
    return ""


def _send_channel(target: str, text: str) -> bool:
    """Deliver to the chat that asked. Never raises, never prints secrets."""
    if not target:
        return False
    try:
        from core import binance_ws

        return bool(binance_ws.send_hermes(target, str(text)[:3500]))
    except Exception:
        return False


def _human(result: runtime.BrowserRunResult) -> str:
    """Plain text for the HUD / chat, without the model-facing markers."""
    if result.ok:
        body = result.final.strip() or "(no text returned)"
        if len(body) > 4000:
            body = body[:4000].rstrip() + " …[truncated]"
        return body
    return f"Browser task did not finish ({result.status}): {result.error or 'no reason given'}"


def _remember(result: runtime.BrowserRunResult, task: str) -> str:
    """Save a research result to RIO's knowledge base — only when asked."""
    if not result.ok or not result.final.strip():
        return ""
    try:
        from core import rag_service

        title = (task.strip().splitlines()[0] if task.strip() else "browser research")[:80]
        res = rag_service.ingest_text(
            caller=rag_service.CALLER_OWNER,
            text=result.final,
            title=f"Browser research: {title}",
            source="browser_agent",
            metadata={"url": (result.downloads[0] if result.downloads else "")},
        )
        if isinstance(res, dict) and res.get("ok"):
            return f"saved to the knowledge base ({res.get('collection') or 'default'})"
        return "could not save it to the knowledge base"
    except Exception as exc:
        return f"could not save it to the knowledge base ({type(exc).__name__})"


def _announce(
    result: runtime.BrowserRunResult,
    player,
    speak,
    remember: bool,
    task: str,
    target: str = "",
) -> None:
    """Worker-thread completion: show, log, deliver, optionally speak."""
    note = _remember(result, task) if remember else ""
    body = _human(result)
    if note:
        body = f"{body}\n\n[{note}]"

    if player is not None:
        try:
            player.write_log(f"[browser] {result.status} {result.job_id or ''}".strip())
        except Exception:
            pass
        try:
            player.show_content("Browser agent", body)
        except Exception:
            pass

    # The target is captured when the job STARTS: by the time it finishes the
    # model may already have replied, which clears ui.inbound_target.
    if target:
        _send_channel(target, body)

    if not callable(speak):
        return
    if result.ok:
        first = result.final.strip().splitlines()[0] if result.final.strip() else "the task"
        if len(first) > 180:
            first = first[:180].rstrip() + "…"
        msg = f"The browser agent finished. {first}"
        if result.downloads:
            msg += f" I also downloaded {len(result.downloads)} file(s)."
    elif result.status == "cancelled":
        msg = "The browser task was cancelled."
    else:
        msg = (
            "The browser task did not finish. "
            + (result.error or "the reason is on screen.")[:200]
        )
    try:
        # Let the tool result leave before injecting a spoken turn.
        time.sleep(1.5)
        speak(msg)
    except Exception:
        pass


def _spawn_completion(
    job_id: str, player, speak, remember: bool, task: str, cfg, target: str = ""
) -> None:
    def _work() -> None:
        result = runtime.shared_runtime().wait(job_id, timeout=cfg.timeout_s + _TIMEOUT_PAD_S)
        _announce(result, player, speak, remember, task, target)

    threading.Thread(
        target=_work, daemon=True, name=f"browser-announce-{job_id}"
    ).start()


def _start_and_announce(
    task: str,
    player=None,
    speak=None,
    *,
    remember: bool = False,
    confirmed: bool = False,
    **opts,
) -> str:
    """Start one allowed task; return immediately unless it is a fast static read."""
    cfg = load_config()
    rt = runtime.shared_runtime()
    # Captured now: the model may answer (and clear the bridge state) while the
    # browser task is still running.
    target = _target(player)
    result = rt.start(
        task,
        confirmed=confirmed,
        log=(player.write_log if player is not None else None),
        wait=False,
        **opts,
    )

    if result.status != "running":
        # denied / needs_approval / delegated / unavailable / duplicate
        if player is not None:
            try:
                player.write_log(f"[browser] {result.status}")
            except Exception:
                pass
        return result.text or result.render(cfg)

    plan_kind = (result.plan or {}).get("kind")

    if plan_kind == "http":
        done = rt.wait(result.job_id, timeout=min(_INLINE_HTTP_WAIT_S, cfg.http_timeout_s + 4))
        if done.status != "timeout":
            _announce(done, player, None, remember, task, target)
            return done.text or done.render(cfg)

    _spawn_completion(result.job_id, player, speak, remember, task, cfg, target)
    return outcome.started(
        "browser task",
        job_id=result.job_id,
        plan=plan_kind or "browser",
        note="the result will be announced when it finishes",
    )


def run_confirmed(task: str, player=None, speak=None, **opts) -> str:
    """Execute an already-approved task (HUD accept, or a chat 'yes')."""
    return _start_and_announce(task, player, speak, confirmed=True, **opts)


def _request_approval(task: str, reason: str, player, speak) -> str:
    """Park a risky task behind the interface that asked. Executes nothing."""
    cfg = load_config()
    target = _target(player)

    if target:
        pending = approvals.STORE.request_for_target(
            target,
            task=task,
            reason=reason,
            ttl_s=float(cfg.approval_ttl_s),
        )
        _send_channel(
            target,
            "This needs your confirmation before I run it.\n"
            f"Task: {task[:300]}\n"
            f"Why: {reason}\n"
            f"Reply 'yes' to run it or 'no' to cancel (token {pending.token}).",
        )
        return outcome.ask(
            "browser task",
            "this needs the user's confirmation in the chat it came from",
            token=pending.token,
        )

    # Desktop: the existing non-blocking HUD confirmation, unchanged semantics.
    try:
        from core import confirm

        if confirm.pending_title():
            return outcome.ask(
                "browser task",
                "another confirmation is already on screen — resolve it first",
            )
        sentence = confirm.request(
            key="browser_agent",
            title="Run this browser action?",
            detail=f"{task[:150]}\n\n{reason}",
            run=lambda: run_confirmed(task, player, speak),
        )
        return sentence
    except Exception as exc:
        return outcome.failed(
            "browser task",
            f"no confirmation interface is available ({type(exc).__name__}), "
            "so nothing was run",
        )


# ── handler ─────────────────────────────────────────────────────────────────


def browser_agent(parameters: dict, player=None, speak=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "run") or "run").strip().lower()
    task = str(params.get("task", "") or "").strip()

    cfg = load_config()
    rt = runtime.shared_runtime()

    try:
        if action == "list":
            active = rt.active()
            if not active:
                return "No browser tasks are running."
            return "Running browser tasks:\n" + "\n".join(
                f"• {j['job_id']} ({j['elapsed_s']}s): {j['task']}" for j in active
            )

        if action == "status":
            job_id = str(params.get("job_id", "") or "").strip()
            if not job_id:
                return "Which job? Pass job_id, or action='list'."
            result = rt.status(job_id)
            if result.status == "running":
                return f"{job_id} is still running ({result.duration_s:.0f}s elapsed)."
            return result.text or result.render(cfg)

        if action == "cancel":
            job_id = str(params.get("job_id", "") or "").strip()
            if not job_id:
                active = rt.active()
                if len(active) != 1:
                    return (
                        "Which job? Pass job_id. Running: "
                        + (", ".join(j["job_id"] for j in active) or "none")
                    )
                job_id = active[0]["job_id"]
            result = rt.cancel(job_id)
            return f"Cancelled {job_id}." if result.status == "cancelled" else (
                result.text or result.render(cfg)
            )

        if action not in ("run", ""):
            return f"Unknown browser action '{action}'. Use run, status, cancel or list."

        if not task:
            return outcome.ask("browser task", "what should the browser do?")

        ok, version, reason = runtime.preflight()
        if not ok:
            return outcome.failed(
                "browser task",
                f"the browser runtime is not installed — {reason}. "
                "Install it with: ~/.jarvis-browser-venv/bin/pip install browser-use==1.0.5",
            )

        decision = policy.classify(task)
        if decision.verdict is policy.Verdict.DENY:
            return outcome.failed("browser task", decision.reason)

        if decision.verdict is policy.Verdict.NEEDS_APPROVAL:
            if not bool(params.get("confirmed", False)):
                return _request_approval(task, decision.reason, player, speak)
            return _start_and_announce(
                task, player, speak, confirmed=True,
                mode=str(params.get("mode", "auto") or "auto"),
                max_steps=params.get("max_steps"),
                visible=bool(params.get("visible", False)),
                auth=bool(params.get("auth", False)),
                site=(str(params.get("site")) if params.get("site") else None),
                domains=str(params.get("domains", "") or ""),
                remember=bool(params.get("remember", False)),
                fresh_profile=bool(params.get("fresh_profile", False)),
            )

        return _start_and_announce(
            task,
            player,
            speak,
            mode=str(params.get("mode", "auto") or "auto"),
            max_steps=params.get("max_steps"),
            visible=bool(params.get("visible", False)),
            auth=bool(params.get("auth", False)),
            site=(str(params.get("site")) if params.get("site") else None),
            domains=str(params.get("domains", "") or ""),
            remember=bool(params.get("remember", False)),
            fresh_profile=bool(params.get("fresh_profile", False)),
        )
    except Exception as exc:
        return outcome.failed(
            "browser task", f"{type(exc).__name__}: {str(exc)[:200]}"
        )


TOOL = {
    "name": "browser_agent",
    "description": (
        "Drive a REAL browser to finish a multi-step web task: open sites, search "
        "inside a site, click, fill forms, log into the user's saved sessions, read "
        "JavaScript-rendered pages, compare several websites, download files and "
        "summarise what it found. Use it when the answer needs interaction or "
        "several pages — 'open YouTube and search for X', 'check my dashboard', "
        "'compare three sites', 'download the PDF and summarise it', 'find the "
        "latest issues on my repo'. Do NOT use it for a single public page you only "
        "need to read (plain http fetch is faster), for a plain URL open (use "
        "browser_control), or for structured data an API already serves (web_search, "
        "delta_exchange, binance_analyzer). It returns immediately and announces the "
        "result when done. Sending messages, publishing, buying, trading, transfers, "
        "deletions and security changes need the user's confirmation first; the tool "
        "asks for it. Use action='status'/'cancel'/'list' with a job_id to manage a "
        "run."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "run (default) | status | cancel | list",
            },
            "task": {
                "type": "STRING",
                "description": "The complete browser task in one self-contained instruction.",
            },
            "mode": {
                "type": "STRING",
                "description": (
                    "auto (default: RIO picks http vs browser) | extract (prefer a "
                    "static read) | interact (force a browser) | research (several "
                    "pages in one session)"
                ),
            },
            "max_steps": {
                "type": "INTEGER",
                "description": "Step budget (default 25, capped at 60).",
            },
            "visible": {
                "type": "BOOLEAN",
                "description": "Show the browser window instead of headless (default false).",
            },
            "auth": {
                "type": "BOOLEAN",
                "description": (
                    "Use the user's real Chrome and its logins (default false). Needs "
                    "remote debugging enabled in that Chrome; ask before using it."
                ),
            },
            "site": {
                "type": "STRING",
                "description": "Credential-store site key when the login needs saved secrets.",
            },
            "domains": {
                "type": "STRING",
                "description": "Comma-separated domains to restrict this task to (optional).",
            },
            "remember": {
                "type": "BOOLEAN",
                "description": "Save the result to RIO's knowledge base (default false).",
            },
            "confirmed": {
                "type": "BOOLEAN",
                "description": (
                    "Set ONLY after the user explicitly approved this exact action. "
                    "Never set it on your own initiative."
                ),
            },
            "job_id": {
                "type": "STRING",
                "description": "Job id for action='status' or 'cancel'.",
            },
        },
        "required": [],
    },
    "handler": browser_agent,
}
