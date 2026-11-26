"""
orchestrator/agents/legal/vault.py — the legal-document VAULT.

Isolated legal-document storage and access control:

    * ONE directory the rest of the system does not read:
      memory/legal/ (relocated by JARVIS_HOME), 0600 files in a 0700 dir.
    * A REGISTRY of ingested documents (doc_id, title, kind, version,
      supersedes chain), one VERSION-CHAIN entry per supersedes link.
    * A CLAUSE CACHE per document (extraction is deterministic; the cache
      makes repeated analysis O(1) and survives crashes — it is a trade
      of determinism for speed, invalidated on re-ingest).
    * An ACTIONS-ONLY audit trail (doc ids, kinds, actions — never the
      contract text).
    * redact_obj() before every disk write (finance/metaquest contract).

The retrieval ACL lives in config/rag_callers.json (legal_agent ->
legal_docs only); documents with retrieval live ONLY in that
collection — nothing else in the system reads them.

Thread-safe, stdlib-only, never raises outward (same contract as every
other Phase 13-16 vault).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from orchestrator.agents.legal.schema import (DocumentMeta,
                                              validate_document_meta)
from orchestrator.agents.productivity import security as lg_security
from orchestrator.agents.productivity.store import PERMS_DIR, PERMS_FILE

MAX_DOCS = 400
MAX_TEXT_CHARS = 400_000
MAX_JSON_BYTES = 4_000_000
MAX_AUDIT_LINES = 2_000

COLLECTION = "legal_docs"                 # the RAG collection with retrieval


def legal_dir(explicit: str | os.PathLike | None = None) -> Path:
    """The vault root: <JARVIS_HOME or repo>/memory/legal."""
    if explicit:
        return Path(explicit)
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        base = Path(home).expanduser().resolve()
    else:
        try:
            from memory.config_manager import BASE_DIR
            base = Path(BASE_DIR)
        except Exception:
            base = Path(__file__).resolve().parents[3]
    return base / "memory" / "legal"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, cap: int) -> str:
    return str(text or "").strip()[:cap]


def new_doc_id(title: str = "") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    slug = "".join(c for c in str(title or "")[:30].lower()
                   if c.isalnum()) or "doc"
    return f"lg-{stamp}-{slug}-{uuid.uuid4().hex[:6]}"


class LegalVault:
    """The only writer of legal vault data. Thread-safe, stdlib-only."""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        self.root = Path(directory) if directory else legal_dir()
        self._lock = threading.RLock()
        self._ensure_private_dir()

    # ── dirs / permissions ──────────────────────────────────────────────
    def _ensure_private_dir(self) -> None:
        try:
            (self.root / "documents").mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, PERMS_DIR)
            os.chmod(self.root / "documents", PERMS_DIR)
        except Exception:
            pass

    @staticmethod
    def _enforce(path: Path) -> None:
        try:
            os.chmod(path, PERMS_FILE)
        except Exception:
            pass

    @staticmethod
    def _safe_name(name: str, cap: int = 80) -> str:
        safe = "".join(c for c in str(name or "") if c.isalnum() or c in "-_")
        return safe[:cap] or "unnamed"

    def _inside(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root.resolve())
            return True
        except Exception:
            return False

    # ── low-level io ────────────────────────────────────────────────────
    def _write_json(self, name: str, payload: Any) -> bool:
        path = self.root / f"{self._safe_name(name)}.json"
        if not self._inside(path):
            return False
        blob = json.dumps(lg_security.redact_obj(payload),
                          ensure_ascii=False, default=str)[:MAX_JSON_BYTES]
        try:
            with self._lock:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             PERMS_FILE)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(blob)
                self._enforce(path)
            return True
        except Exception:
            return False

    def _read_json(self, name: str, default: Any) -> Any:
        path = self.root / f"{self._safe_name(name)}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    # ── registry (documents + chains) ───────────────────────────────────
    def documents(self) -> list[dict]:
        rows = self._read_json("registry", [])
        rows = rows if isinstance(rows, list) else []
        out = []
        for r in rows:
            clean, _errs = validate_document_meta(r)
            if clean is not None:
                out.append(clean.__dict__)     # dataclass asdict is enough
        return out

    def register_document(self, meta: DocumentMeta) -> DocumentMeta:
        clean, _ = validate_document_meta(meta)
        if clean is None:
            raise ValueError("invalid document meta")
        rows = self._read_json("registry", [])
        rows = rows if isinstance(rows, list) else []
        rows = [r for r in rows
                if str(r.get("doc_id") or "") != clean.doc_id]
        rows.append(clean.__dict__)
        self._write_json("registry", rows[:MAX_DOCS])
        self.audit("registered", doc_id=clean.doc_id, kind=clean.kind,
                   version=clean.version)
        if clean.supersedes:
            chain = self.version_chain(clean.supersedes)
            chain.append(clean.doc_id)
            self._write_json(f"chain-{self._safe_name(clean.doc_id)}",
                             chain)
        return clean

    def get_document(self, doc_id: str) -> Optional[dict]:
        doc_id = _clip(doc_id, 80)
        for row in self.documents():
            if row.get("doc_id") == doc_id:
                return row
        return None

    def forget_document(self, doc_id: str) -> bool:
        doc_id = _clip(doc_id, 80)
        rows = self._read_json("registry", [])
        rows = rows if isinstance(rows, list) else []
        keep = [r for r in rows
                if str(r.get("doc_id") or "") != doc_id]
        if len(keep) == len(rows):
            return False
        self._write_json("registry", keep)
        try:
            (self.root / "documents" /
             f"{self._safe_name(doc_id)}.txt").unlink(missing_ok=True)
        except Exception:
            pass
        self._write_json(f"clauses-{self._safe_name(doc_id)}", {})
        self.audit("forgotten", doc_id=doc_id)
        return True

    def version_chain(self, doc_id: str) -> list[str]:
        rows = self._read_json(f"chain-{self._safe_name(_clip(doc_id, 80))}",
                               [])
        return rows if isinstance(rows, list) else []

    # ── page text + clause cache ────────────────────────────────────────
    def save_page_text(self, doc_id: str, pages: list[dict]) -> bool:
        doc_id = _clip(doc_id, 80)
        capped = [{k: _clip(v, MAX_TEXT_CHARS) if isinstance(v, str) else v
                   for k, v in (p or {}).items() if k in ("num", "text")}
                  for p in (pages or [])][:400]
        return self._write_json(f"pages-{self._safe_name(doc_id)}",
                                {"doc_id": doc_id, "pages": capped})

    def load_page_text(self, doc_id: str) -> list[dict]:
        doc_id = _clip(doc_id, 80)
        data = self._read_json(f"pages-{self._safe_name(doc_id)}", {})
        pages = data.get("pages") if isinstance(data, dict) else None
        return pages if isinstance(pages, list) else []

    def save_clauses(self, doc_id: str, clauses: list[dict]) -> bool:
        doc_id = _clip(doc_id, 80)
        return self._write_json(f"clauses-{self._safe_name(doc_id)}",
                                {"doc_id": doc_id,
                                 "clauses": (clauses or [])[:300]})

    def load_clauses(self, doc_id: str) -> list[dict]:
        doc_id = _clip(doc_id, 80)
        data = self._read_json(f"clauses-{self._safe_name(doc_id)}", {})
        cls = data.get("clauses") if isinstance(data, dict) else None
        return cls if isinstance(cls, list) else []

    # ── audit (actions only — never contract text) ──────────────────────
    def audit(self, event: str, **fields: Any) -> None:
        safe = {"event": _clip(event, 40), "ts": _now_iso()}
        for k, v in (fields or {}).items():
            safe[_clip(k, 30)] = v if isinstance(v, (int, float, bool)) \
                else _clip(v, 60)
        self._append_jsonl("audit", safe)

    def _append_jsonl(self, name: str, obj: dict) -> bool:
        path = self.root / f"{self._safe_name(name)}.jsonl"
        if not self._inside(path):
            return False
        line = json.dumps(lg_security.redact_obj(obj), ensure_ascii=False,
                          default=str)
        try:
            with self._lock:
                lines = []
                if path.exists():
                    lines = path.read_text(encoding="utf-8") \
                        .splitlines()[-(MAX_AUDIT_LINES - 1):]
                lines.append(line)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             PERMS_FILE)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                self._enforce(path)
            return True
        except Exception:
            return False

    # ── summary / wipe ──────────────────────────────────────────────────
    def summary(self) -> dict:
        rows = self.documents()
        by_kind: dict[str, int] = {}
        for r in rows:
            k = str(r.get("kind") or "other")
            by_kind[k] = by_kind.get(k, 0) + 1
        return {"docs": len(rows), "by_kind": by_kind}

    def wipe(self) -> bool:
        ok = True
        with self._lock:
            for name in ("registry", "audit"):
                try:
                    (self.root / f"{name}.json").unlink(missing_ok=True)
                    (self.root / f"{name}.jsonl").unlink(missing_ok=True)
                except Exception:
                    ok = False
            try:
                for p in (self.root / "documents").iterdir():
                    p.unlink(missing_ok=True)
            except Exception:
                ok = False
            self._append_jsonl("audit", {"event": "wipe"})
        return ok
