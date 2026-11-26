"""
orchestrator/model_router/usage.py — the usage journal (no secrets).

One JSONL line per routed request, appended to
memory/model_router/usage.jsonl (JARVIS_HOME aware, like every other
writable state path in this repo).

WHAT IS LOGGED:  timestamp, task, mode, provider, model, ok, latency,
prompt SIZE (chars — never the prompt itself), token counts when the
provider reports them, the fallback hops with their reasons, and the
error CLASS + a REDACTED error excerpt.

WHAT IS NEVER LOGGED: API keys, bearer tokens, prompt text, completions.
Provider error strings are famous for echoing credentials, so every error
passes through redact() before it touches disk. The journal is
best-effort: a write failure degrades to a log line and never breaks a
user turn (same contract as orchestrator/journal.py).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("jarvis.model_router")

# ── rotation caps (same shape as core/llm_router's usage log) ────────────────
MAX_BYTES = 5_000_000
KEEP_LINES = 2000

# ── secret patterns worth redacting from error strings ───────────────────────
_SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{8,}"),  # Google API keys
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),  # OpenAI-style keys
    re.compile(r"xai-[A-Za-z0-9_\-]{8,}"),  # xAI keys
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{8,}"),  # GitHub tokens
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(
        r"(?i)(api[_-]?key|token|authorization)(\"' ]*[:=][\"' ]*)"
        r"[A-Za-z0-9._\-]{8,}"
    ),
)
_REDACTED = "[redacted]"


def redact(text: str) -> str:
    """Strip anything that smells like a credential out of a string."""
    out = str(text or "")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: m.group(0)[:4] + _REDACTED, out)
    return out


def _default_path() -> Path:
    base = os.environ.get("JARVIS_HOME", "").strip()
    root = Path(base).expanduser() if base else Path(__file__).resolve().parents[2]
    return root / "memory" / "model_router" / "usage.jsonl"


class UsageJournal:
    """Append-only JSONL usage log. stdlib-only, thread-safe, never raises."""

    def __init__(
        self,
        path: str | Path | None = None,
        enabled: bool = True,
        max_bytes: int = MAX_BYTES,
        keep_lines: int = KEEP_LINES,
    ) -> None:
        self.path = Path(path) if path else _default_path()
        self.enabled = bool(enabled)
        self._max_bytes = int(max_bytes)
        self._keep_lines = int(keep_lines)
        self._lock = threading.Lock()

    def record(
        self,
        *,
        task: str,
        mode: str,
        provider: str,
        model: str,
        ok: bool,
        latency_s: float,
        prompt_chars: int = 0,
        tokens: Optional[dict] = None,
        hops: Optional[list] = None,
        error: str = "",
    ) -> None:
        """One line per routed request. Never raises.

        `hops` is the fallback trail: [{"provider": ..., "reason": ...}, …]
        for every provider tried before the one that answered (or all of
        them when the request failed everywhere).
        """
        rec: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "layer": "model_router",
            "task": task,
            "mode": mode,
            "provider": provider,
            "model": model,
            "ok": bool(ok),
            "latency_s": round(float(latency_s), 2),
            "prompt_chars": int(prompt_chars),
        }
        if tokens:
            rec["tokens"] = {
                k: tokens[k]
                for k in sorted(tokens)
                if isinstance(tokens.get(k), (int, float))
            }
        if hops:
            rec["hops"] = [
                {
                    "provider": h.get("provider", ""),
                    "reason": redact(h.get("reason", ""))[:160],
                }
                for h in hops
            ]
        if error:
            rec["error"] = redact(error)[:200]
        line = json.dumps(rec, ensure_ascii=False, default=str)
        if not self.enabled:
            log.debug("usage(off) %s", line)
            return
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self._max_bytes:
                    lines = self.path.read_text(encoding="utf-8").splitlines()[
                        -self._keep_lines :
                    ]
                    self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:  # degrade, never raise
            log.warning("usage journal write failed: %s", exc)
