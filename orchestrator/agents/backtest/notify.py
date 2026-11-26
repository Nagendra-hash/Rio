"""
orchestrator/agents/backtest/notify.py — one-way result notifications.

Delivery is deliberately boring and fail-safe:

    * dry_run (default in tests) appends to memory/backtest/outbox.jsonl and
      never touches a real channel;
    * otherwise the existing Hermes CLI is used (`hermes send -t <channel>`),
      located with shutil.which so a moved binary is reported, not guessed;
    * identical notifications are deduplicated for `dedup_hours`;
    * every message is redacted before it leaves the process, and the message
      body is built only from the result dictionary — never from os.environ.

A notification failure can never fail a research job: send() returns a small
dict and the caller records it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from orchestrator.agents.backtest.config import Config, default_config
from orchestrator.agents.backtest.jobs import JobStore
from orchestrator.agents.backtest.store import outbox_path

# Any long secret-looking token is masked. This is a safety net, not the
# primary defence: no secret is ever put into a message in the first place.
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|access[_-]?key)\b"
    r"\s*[:=]\s*\S+")
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def redact(text: str) -> str:
    """Mask credential-looking substrings in outgoing text."""
    out = _SECRET_RE.sub(lambda m: f"{m.group(1)}=***", str(text or ""))
    return _LONG_TOKEN_RE.sub("***", out)


def hermes_binary() -> str:
    """Locate the Hermes CLI without assuming a fixed path."""
    found = shutil.which("hermes")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "hermes",
                      Path.home() / ".hermes" / "hermes"):
        if candidate.exists():
            return str(candidate)
    return ""


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:+.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _rr(value: Any) -> str:
    try:
        return f"1 : {float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


def format_result(result: dict[str, Any], *, max_chars: int = 900) -> str:
    """A short, honest message: verdict first, caveats second."""
    status = str(result.get("status") or "?")
    symbol = str(result.get("symbol") or "?")
    market = str(result.get("market") or "")
    job_id = str(result.get("job_id") or "")
    inst = result.get("instrument") or {}
    head = f"Backtest {symbol}{f' ({market})' if market else ''}: {status}"

    if status == "NEEDS_INPUT":
        return redact("\n".join([
            head,
            str(result.get("question") or result.get("reason") or ""),
            f"job {job_id}",
        ]))

    if status == "BLOCKED":
        return redact("\n".join([
            head,
            str(result.get("reason") or "instrument could not be resolved"),
            f"job {job_id}",
        ]))

    if status == "INSUFFICIENT_DATA":
        rejected = ", ".join(t.get("timeframe", "") for t in
                             result.get("timeframes_rejected") or [])
        return redact("\n".join([
            head,
            str(result.get("reason") or ""),
            f"rejected: {rejected or 'all timeframes'}",
            f"job {job_id}",
        ]))

    champ = result.get("champion") or {}
    lines = [head,
             f"{inst.get('instrument_type', symbol)} · "
             f"{', '.join(result.get('timeframes_tested') or []) or 'no TF'} · "
             f"{result.get('experiments_total', 0)} configs"]
    if champ:
        oos = champ.get("oos") or {}
        bench = champ.get("benchmark_oos") or {}
        lines += [
            f"Best: {champ.get('name')} v{champ.get('version')} "
            f"on {champ.get('timeframe')}",
            f"OOS net {_pct(oos.get('net_return'))} · Sharpe "
            f"{oos.get('sharpe')} · maxDD {_pct(oos.get('max_drawdown'))} · "
            f"R:R {_rr(oos.get('risk_reward'))} · "
            f"{oos.get('trades', 0)} trades",
            f"Buy & hold same window: {_pct(bench.get('net_return'))}",
        ]
        if champ.get("stability") is not None:
            lines.append(f"Stability {champ.get('stability')} · "
                         f"{champ.get('strategy')}")
    else:
        lines += [str(result.get("reason") or
                      "No strategy cleared every out-of-sample gate.")]
    if result.get("warnings"):
        lines.append("⚠ " + str(result["warnings"][0])[:200])
    lines.append("Research only — not advice. No order was placed.")
    lines.append(f"job {job_id}")
    text = "\n".join(lines)
    return redact(text[:max_chars])


class Notifier:
    """Dedup + redact + deliver (dry-run by default in tests)."""

    def __init__(self, cfg: Config | None = None, *, store: JobStore | None = None,
                 channel: str | None = None, dry_run: bool | None = None) -> None:
        self.cfg = cfg or default_config()
        self.store = store or JobStore(cfg=self.cfg)
        settings = self.cfg.get("notify", {}) or {}
        self.channel = channel or str(settings.get("channel") or "telegram")
        self.dry_run = bool(settings.get("dry_run", True)
                            if dry_run is None else dry_run)
        self.dedup_hours = float(settings.get("dedup_hours", 12) or 12)
        self.enabled = bool(settings.get("enabled", True))

    # ── delivery ─────────────────────────────────────────────────────────
    def send_text(self, text: str, *, dedup_key: str, event: str = "",
                  job_id: str = "") -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "skipped": "notifications disabled"}
        body = redact(text)
        if not body.strip():
            return {"ok": False, "skipped": "empty message"}
        if not self.store.claim_notification(
                dedup_key, event=event, job_id=job_id, channel=self.channel,
                hours=self.dedup_hours):
            return {"ok": False, "deduplicated": True, "dedup_key": dedup_key}

        if self.dry_run:
            try:
                with open(outbox_path(), "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "at": time.time(), "channel": self.channel,
                        "dedup_key": dedup_key, "event": event,
                        "job_id": job_id, "text": body}) + "\n")
                self.store.mark_notified(dedup_key, True, "dry-run outbox")
                return {"ok": True, "dry_run": True, "dedup_key": dedup_key,
                        "outbox": str(outbox_path())}
            except Exception as exc:
                self.store.mark_notified(dedup_key, False,
                                         f"{type(exc).__name__}: {exc}")
                return {"ok": False, "error": str(exc), "dedup_key": dedup_key}

        binary = hermes_binary()
        if not binary:
            self.store.mark_notified(dedup_key, False, "hermes CLI not found")
            return {"ok": False, "error": "hermes CLI not found",
                    "dedup_key": dedup_key}
        try:
            proc = subprocess.run(
                [binary, "send", "-t", self.channel, body],
                capture_output=True, text=True, timeout=45)
            ok = proc.returncode == 0
            detail = (proc.stdout if ok else proc.stderr or "")[-300:]
            self.store.mark_notified(dedup_key, ok, detail)
            return {"ok": ok, "channel": self.channel,
                    "returncode": proc.returncode,
                    "detail": redact(detail), "dedup_key": dedup_key}
        except Exception as exc:
            self.store.mark_notified(dedup_key, False, str(exc))
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "dedup_key": dedup_key}

    def send_result(self, result: dict[str, Any], *, event: str = "job") -> dict:
        job_id = str(result.get("job_id") or "")
        symbol = str(result.get("symbol") or "")
        status = str(result.get("status") or "")
        key = f"{event}:{job_id}:{status}"
        return self.send_text(
            format_result(result), dedup_key=key, event=event, job_id=job_id)

    def test_message(self, text: str = "Backtest lab notification test") -> dict:
        """Explicit, user-triggered test. Still honours dry_run."""
        return self.send_text(
            redact(text), dedup_key=f"test:{int(time.time() // 60)}",
            event="test")


def notify_result(result: dict[str, Any], cfg: Config | None = None,
                  **kwargs) -> dict[str, Any]:
    """Module-level convenience used by the worker."""
    return Notifier(cfg, **kwargs).send_result(result)
