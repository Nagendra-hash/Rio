"""
core/repo_agent/notify.py — best-effort Hermes notifications for job events.

Follows the house pattern (autonomy engine Notifier → `hermes send`).
Delivery is OPTIONAL and never fatal: if the Hermes CLI is missing or the
send fails, the outcome is recorded in the job store and the worker moves on.
Secrets are redacted before anything leaves the process.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from core.repo_agent.jobs import RepoJobStore, config
from core.repo_agent.security import redact

__all__ = ["notify_job_result", "send"]


def _hermes_cli() -> str | None:
    found = shutil.which("hermes")
    if found:
        return found
    candidate = Path.home() / ".hermes" / "hermes"
    return str(candidate) if candidate.exists() else None


def send(text: str, target: str = "") -> dict[str, object]:
    """Fire-and-forget Hermes message. Returns {ok, detail}."""
    cli = _hermes_cli()
    if not cli:
        return {"ok": False, "detail": "hermes CLI not available"}
    argv = [cli, "send"]
    if target:
        argv += ["-t", target]
    argv.append(str(text)[:3500])
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=30, errors="replace")
        return {"ok": proc.returncode == 0,
                "detail": redact((proc.stderr or proc.stdout or "")[-200:])}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}


def notify_job_result(job: dict, result: dict | None = None) -> dict[str, object]:
    """Notify Hermes once per job terminal state (deduplicated in the store)."""
    cfg = config()
    if not cfg.get("notify_hermes", True):
        return {"ok": False, "detail": "notifications disabled"}
    store = RepoJobStore()
    dedup_key = f"job-terminal-{job.get('job_id')}"
    if not store.claim_notification(dedup_key, event="job_terminal",
                                    job_id=str(job.get("job_id"))):
        return {"ok": False, "detail": "already notified"}
    status = str(job.get("status") or "")
    icon = {"COMPLETED": "✅", "FAILED": "❌", "CANCELLED": "⛔"}.get(
        status, "ℹ️")
    kind = str(job.get("kind") or "")
    ref = str(job.get("ref") or "")
    if kind == "video":
        label = {"shorts": "short", "documentary": "documentary",
                 "smoke": "video smoke test"}.get(ref, f"video ({ref})")
        lines = [f"{icon} JARVIS video · {label} → {status}"]
    else:
        lines = [f"{icon} JARVIS repo-agent · {kind} {ref} → {status}"]
    message = str(job.get("message") or "")
    if message:
        lines.append(message[:400])
    outputs = list((result or {}).get("outputs") or [])[:4]
    for out in outputs:
        lines.append(f"• {out}")
    if (result or {}).get("error"):
        lines.append(f"error: {str(result['error'])[:300]}")
    # Phase 8: deliver the finished video as a Hermes attachment when the
    # platform allows it — otherwise the path above is the authorized access
    # method (never the whole filesystem, just this one file).
    attach_limit_mb = float(cfg.get("notify_attach_video_mb") or 45)
    attached = False
    if kind == "video" and status == "COMPLETED" and outputs:
        video = Path(outputs[0])
        if video.is_file():
            size_mb = video.stat().st_size / 1024 / 1024
            if size_mb <= attach_limit_mb:
                lines.append(f"MEDIA:{video}")
                attached = True
            else:
                lines.append(f"(file is {size_mb:.0f} MB — too large to "
                             f"attach; collect it from {video.parent})")
    target = str(cfg.get("notify_target") or "")
    outcome = send("\n".join(lines), target=target)
    store.mark_notified(dedup_key, bool(outcome.get("ok")),
                        str(outcome.get("detail") or "")
                        + (f" (attached {outputs[0]})" if attached else ""))
    return outcome
