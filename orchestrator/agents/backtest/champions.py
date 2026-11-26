"""
orchestrator/agents/backtest/champions.py — champion / challenger ledger.

One active champion per (symbol, market). Every promotion, rejection and
retirement is recorded with the evidence that caused it, and the previous
champions are kept in a bounded stack so a rollback is always one command.

Rules (enforced here, not in prose):
    * a challenger is promoted ONLY after passing the configured
      out-of-sample gates, and only if it beats the incumbent by the
      configured margin (or the incumbent was retired as degraded);
    * a champion is never re-tuned against the same data to "improve" itself;
    * nothing here executes a trade — a champion is a research record.
"""
from __future__ import annotations

import threading
from typing import Any

from orchestrator.agents.backtest.config import Config, default_config
from orchestrator.agents.backtest.store import champions_path, now_iso, read_json, write_json

MAX_STACK = 5


class ChampionStore:
    def __init__(self, cfg: Config | None = None, path=None) -> None:
        self.cfg = cfg or default_config()
        self.path = path or champions_path()
        self._lock = threading.RLock()

    # ── io ───────────────────────────────────────────────────────────────
    def _read(self) -> dict:
        data = read_json(self.path, {}) or {}
        data.setdefault("champions", {})
        return data

    def _write(self, data: dict) -> None:
        write_json(self.path, data)

    # ── queries ──────────────────────────────────────────────────────────
    def get(self, key: str) -> dict | None:
        with self._lock:
            return (self._read()["champions"] or {}).get(key)

    def all(self) -> dict:
        with self._lock:
            return dict(self._read()["champions"])

    def history(self, key: str) -> list[dict]:
        entry = self.get(key) or {}
        return list(entry.get("history") or [])

    # ── decisions ────────────────────────────────────────────────────────
    def decide(self, key: str, candidate: dict, job_id: str = "",
               *, retired: bool = False) -> dict[str, Any]:
        """Record and apply the promotion decision for one candidate."""
        with self._lock:
            data = self._read()
            entry = data["champions"].get(key) or {
                "key": key, "champion": None, "stack": [], "history": []}
            incumbent = entry.get("champion")
            passed = str(candidate.get("status") or "") == "CANDIDATE"
            margin = float(self.cfg.get("promotion_margin", 0.05))
            challenger_score = float(candidate.get("oos_score") or -1e9)
            incumbent_score = float((incumbent or {}).get("oos_score") or -1e9)

            if not passed:
                action, reason = "rejected", (candidate.get("reason") or
                                              "did not pass out-of-sample gates")
            elif incumbent is None:
                action, reason = "promoted", "first champion to pass all gates"
            elif retired:
                action, reason = "promoted", (
                    "incumbent was retired as degraded; challenger passed all "
                    "out-of-sample gates")
            elif challenger_score >= incumbent_score + margin:
                action, reason = "promoted", (
                    f"out-of-sample score {challenger_score:.3f} beats incumbent "
                    f"{incumbent_score:.3f} by ≥{margin:.2f}")
            else:
                action, reason = "kept", (
                    f"challenger score {challenger_score:.3f} does not beat "
                    f"incumbent {incumbent_score:.3f} + margin {margin:.2f}")

            event = {
                "at": now_iso(), "action": action, "reason": reason,
                "job_id": job_id,
                "strategy": candidate.get("strategy"),
                "version": candidate.get("version"),
                "timeframe": candidate.get("timeframe"),
                "params": candidate.get("params"),
                "oos_score": candidate.get("oos_score"),
                "oos": candidate.get("oos"),
                "stability": candidate.get("stability"),
            }
            entry["history"] = (entry.get("history") or [])[-99:] + [event]

            if action == "promoted":
                # A promoted challenger replaces a retired incumbent: clear the
                # retirement marker so the entry is not simultaneously active
                # and "retired".
                entry.pop("retired_at", None)
                entry.pop("retired_reason", None)
                if incumbent:
                    entry["stack"] = ([incumbent] + (entry.get("stack") or []))[:MAX_STACK]
                entry["champion"] = {
                    "strategy": candidate.get("strategy"),
                    "name": candidate.get("name"),
                    "version": candidate.get("version"),
                    "timeframe": candidate.get("timeframe"),
                    "params": candidate.get("params"),
                    "oos": candidate.get("oos"),
                    "oos_score": candidate.get("oos_score"),
                    "stability": candidate.get("stability"),
                    "benchmark_oos": candidate.get("benchmark_oos"),
                    "provider": candidate.get("provider"),
                    "instrument": candidate.get("instrument"),
                    "market": candidate.get("market"),
                    "job_id": job_id,
                    "promoted_at": now_iso(),
                }
            entry["key"] = key
            data["champions"][key] = entry
            self._write(data)
            return {"action": action, "reason": reason, "champion": entry.get("champion")}

    def retire(self, key: str, reason: str, job_id: str = "") -> dict:
        with self._lock:
            data = self._read()
            entry = data["champions"].get(key)
            if not entry or not entry.get("champion"):
                return {"ok": False, "error": f"no champion for {key}"}
            entry.setdefault("history", []).append({
                "at": now_iso(), "action": "retired", "reason": reason,
                "job_id": job_id, "strategy": entry["champion"].get("strategy"),
                "timeframe": entry["champion"].get("timeframe"),
                "params": entry["champion"].get("params")})
            entry["retired_at"] = now_iso()
            entry["retired_reason"] = reason
            self._write(data)
            return {"ok": True, "retired": key, "reason": reason}

    def rollback(self, key: str, reason: str = "manual rollback") -> dict:
        """Restore the previous champion from the stack."""
        with self._lock:
            data = self._read()
            entry = data["champions"].get(key)
            if not entry or not entry.get("stack"):
                return {"ok": False, "error": f"no previous champion for {key}"}
            previous = entry["stack"].pop(0)
            current = entry.get("champion")
            entry["champion"] = previous
            entry.setdefault("history", []).append({
                "at": now_iso(), "action": "rollback", "reason": reason,
                "restored": {k: previous.get(k) for k in
                             ("strategy", "version", "timeframe", "params",
                              "oos_score")},
                "replaced": {k: (current or {}).get(k) for k in
                             ("strategy", "version", "timeframe", "params",
                              "oos_score")}})
            entry.pop("retired_at", None)
            entry.pop("retired_reason", None)
            self._write(data)
            return {"ok": True, "champion": previous}

    def snapshot(self) -> dict:
        return {"champions": self.all(), "path": str(self.path)}
