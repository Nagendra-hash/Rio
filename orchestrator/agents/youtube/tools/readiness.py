"""
orchestrator/agents/youtube/tools/readiness.py — the production readiness gate.

A Python port of the upstream pack's ``utils/production-readiness-service.js``
(MIT — see ``ATTRIBUTION.md``): a fixed set of named probes runs before any
content is allowed near the publish path. A *blocking* probe that fails turns
the whole status to ``failed`` and refuses the action; a non-blocking failure
is a warning. The upstream probes (AI text/image/video provider, narration,
ffmpeg assembly, YouTube access, upload metadata) are re-expressed here as
injectable callables so the same framework is honest offline: nothing is
"probed" by pretending — a missing live capability is reported as one.

    from orchestrator.agents.youtube.tools import readiness
    report = readiness.run({"text": lambda: ..., "youtube": lambda: ...})

Every probe returns either a string (a pass message), or a dict with keys
``status`` ("passed" | "warning" | "skipped"), ``message`` and optional
``details`` / ``remediation``. A raised exception is a failure with the
remediation the upstream pack documents for that check id.
"""
from __future__ import annotations

import re
import time

STALE_AFTER_S = 24 * 60 * 60

# id → (label, blocking, remediation) — the upstream check set and messages.
CHECKS: tuple[tuple[str, str, bool, str], ...] = (
    ("text_provider", "AI text provider", True,
     "Verify the selected model and confirm provider credits or quota."),
    ("image_provider", "Image provider", False,
     "Verify paid image access or rely on the built-in slideshow fallback."),
    ("video_provider", "AI video provider", True,
     "Configure the selected provider credentials, choose the local "
     "slideshow, or add the paid video probe."),
    ("voice_narration", "Voice narration", True,
     "Configure a TTS provider, or explicitly confirm an intentional silent "
     "video."),
    ("video_assembly", "Audio/video assembly", True,
     "Install ffmpeg, or set FFMPEG_PATH to a working binary."),
    ("youtube_access", "YouTube channel access", True,
     "Reconnect YouTube OAuth and ensure the Google account owns a channel."),
    ("upload_metadata", "Upload metadata", True,
     "Fix the queued title, description, category, language, and tags before "
     "publishing."),
)

_REMEDIATION = {cid: r for cid, _l, _b, r in CHECKS}


def check_ids() -> list[str]:
    return [cid for cid, _l, _b, _r in CHECKS]


def run(probes: dict | None = None, *, include_paid_media: bool = False,
        include_paid_video: bool = False, now=None) -> dict:
    """Run every probe and summarise the run. ``probes`` maps id → callable.

    A check present in ``CHECKS`` with no probe supplied runs as a
    non-blocking ``warning`` saying the capability was not configured — the
    honest reading, never a fake pass.
    """
    probes = probes or {}
    checks: list[dict] = []
    started_all = time.time()
    for cid, label, blocking, remediation in CHECKS:
        started = time.time()
        probe = probes.get(cid)
        if probe is None:
            checks.append({
                "id": cid, "label": label, "blocking": blocking,
                "status": "warning",
                "message": f"{label}: no probe configured (capability not "
                           f"verified).",
                "remediation": remediation, "details": {},
                "durationMs": 0,
            })
            continue
        try:
            kwargs = {}
            if cid == "image_provider":
                kwargs = {"includePaidMedia": include_paid_media}
            elif cid == "video_provider":
                kwargs = {"includePaidVideo": include_paid_video}
            result = probe(**kwargs) if _accepts_kwargs(probe) else probe()
            checks.append(_coerce(cid, label, blocking, remediation, result,
                                  int((time.time() - started) * 1000)))
        except Exception as exc:
            checks.append({
                "id": cid, "label": label, "blocking": blocking,
                "status": "failed", "message": redact(str(exc)),
                "remediation": remediation, "details": {},
                "durationMs": int((time.time() - started) * 1000),
            })

    blocking_failures = [c["id"] for c in checks
                         if c["blocking"] and c["status"] == "failed"]
    warnings = [c for c in checks
                if c["status"] in ("warning", "skipped")
                or (not c["blocking"] and c["status"] == "failed")]
    if blocking_failures:
        status = "failed"
    elif warnings:
        status = "warning"
    else:
        status = "passed"

    return {
        "status": status,
        "checks": checks,
        "summary": {
            "passed": sum(1 for c in checks if c["status"] == "passed"),
            "warnings": len(warnings),
            "failed": sum(1 for c in checks if c["status"] == "failed"),
            "blockingFailures": blocking_failures,
        },
        "blockingFailures": blocking_failures,
        "stale": False,
        "startedAt": _iso(now, offset=-int((time.time() - started_all) * 1000)),
        "completedAt": _iso(now),
    }


def assert_ready(report: dict | None, action: str = "Publishing") -> None:
    """Raise ``Blocked`` when the gate has failed. Mirrors assertReady()."""
    summary = report or {}
    if summary.get("status") == "failed":
        failures = ", ".join(summary.get("blockingFailures")
                             or _ids(summary))
        raise Blocked(f"{action} is blocked by the production readiness gate. "
                      f"Fix {failures} and run the check again.",
                      report=summary)


class Blocked(RuntimeError):
    status = 409

    def __init__(self, message: str, *, report: dict | None = None) -> None:
        super().__init__(message)
        self.report = report or {}


def is_blocked(report: dict | None) -> bool:
    return (report or {}).get("status") == "failed"


def redact(message: str) -> str:
    """Strip anything that smells like a key or token from a probe message."""
    text = str(message or "Probe failed")
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"AIza[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~-]+", "Bearer [redacted]", text,
                  flags=re.I)
    text = re.sub(r"(access_token|refresh_token|api[_-]?key|token)=([^\s&]+)",
                  r"\1=[redacted]", text, flags=re.I)
    return text[:500]


def render(report: dict | None) -> str:
    r = report or {}
    out = ["# Production readiness", "",
           f"**Status: {(r.get('status') or 'unverified').upper()}**", "",
           "| check | blocking | status | message |",
           "| --- | --- | --- | --- |"]
    for c in r.get("checks") or []:
        out.append(f"| {c['label']} | {'yes' if c['blocking'] else 'no'} | "
                   f"{c['status']} | {c['message']} |")
    failed = [c for c in (r.get("checks") or []) if c["status"] == "failed"]
    if failed:
        out += ["", "## What to fix", ""]
        for c in failed:
            out.append(f"- **{c['label']}**: {c.get('remediation') or '—'}")
    return "\n".join(out) + "\n"


def remediation_for(check_id: str) -> str:
    return _REMEDIATION.get(check_id, "Correct the configuration and rerun.")


# ── helpers ───────────────────────────────────────────────────────────────────
def _coerce(cid, label, blocking, remediation, result, ms) -> dict:
    if isinstance(result, dict):
        status = str(result.get("status") or "passed").lower()
        if status not in ("passed", "warning", "skipped", "failed"):
            status = "passed"
        return {"id": cid, "label": label, "blocking": blocking,
                "status": status,
                "message": str(result.get("message") or label),
                "remediation": result.get("remediation") or remediation,
                "details": result.get("details") or {}, "durationMs": ms}
    return {"id": cid, "label": label, "blocking": blocking, "status": "passed",
            "message": str(result or f"{label} passed."), "remediation": None,
            "details": {}, "durationMs": ms}


def _accepts_kwargs(fn) -> bool:
    import inspect
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return any(p.kind in (p.VAR_KEYWORD, p.KEYWORD_ONLY) or p.default is not p.empty
               for p in sig.parameters.values())


def _ids(report: dict) -> list[str]:
    return [c["id"] for c in (report.get("checks") or [])
            if c.get("blocking") and c.get("status") == "failed"]


def _iso(now, offset: int = 0) -> str:
    import datetime as _dt
    base = now() if callable(now) else (now or _dt.datetime.now(_dt.timezone.utc))
    if isinstance(base, str):
        return base
    return (base + _dt.timedelta(milliseconds=offset)).isoformat()
