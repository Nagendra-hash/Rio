"""
orchestrator/agents/metaquest/store.py — Meta Quest metadata vault.

Everything the citation lines need, outside the RAG library itself: the
list of KNOWN products (Quest 2 / 3 / 3S / Pro with firmware families),
per-document metadata overrides (which product + version + collected-on a
manual belongs to) and a small audit trail (actions only).

memory/metaquest/ (relocated by JARVIS_HOME), 0600 files in 0700 dir,
redaction before disk, thread-safe, never raises outward — same contract
as every other Phase 13-15 vault.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from orchestrator.agents.productivity import security as mq_security
from orchestrator.agents.productivity.store import PERMS_DIR, PERMS_FILE

MAX_PRODUCTS = 40
MAX_DOCS = 500
MAX_TEXT_CHARS = 2_000
MAX_JSON_BYTES = 512_000

COLLECTION = "metaquest_docs"

# The canonical products; version strings the agent recognises on its own.
KNOWN_PRODUCTS = {
    "quest 3s": "Quest 3S",
    "quest 3": "Quest 3",
    "quest pro": "Quest Pro",
    "quest 2": "Quest 2",
    "quest (gen 1)": "Quest (gen 1)",
    "oculus go": "Oculus Go",
    "rift s": "Rift S",
}

VERSION_RE = re.compile(r"\b(?:v)\s?(\d[\w.\-]{0,20})", re.IGNORECASE)


def metaquest_dir(explicit: str | os.PathLike | None = None) -> Path:
    """The Meta Quest vault root: <JARVIS_HOME or repo>/memory/metaquest."""
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
    return base / "memory" / "metaquest"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, cap: int = MAX_TEXT_CHARS) -> str:
    return str(text or "").strip()[:cap]


def canon_product(text: str) -> str:
    """'Quest 3S' from 'quest3 s' / 'my quest 3s headset' ('' when none)."""
    low = " ".join(str(text or "").lower().split())
    hits = [name for pat, name in KNOWN_PRODUCTS.items()
            if re.search(r"\b" + re.escape(pat) + r"\b", low)]
    if hits:
        hits.sort(key=len, reverse=True)
        return hits[0]
    return ""


def extract_version(text: str) -> str:
    m = VERSION_RE.search(str(text or ""))
    return (m.group(1).strip(".") if m else "")


class MetaQuestStore:
    """The only writer of metaquest metadata. Thread-safe, stdlib-only."""

    def __init__(self, directory: str | os.PathLike | None = None) -> None:
        self.root = Path(directory) if directory else metaquest_dir()
        self._lock = threading.RLock()
        self._ensure_private_dir()

    # ── permissions / containment ───────────────────────────────────────
    def _ensure_private_dir(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, PERMS_DIR)
        except Exception:
            pass

    @staticmethod
    def _enforce(path: Path) -> None:
        try:
            os.chmod(path, PERMS_FILE)
        except Exception:
            pass

    @staticmethod
    def _safe_name(name: str, cap: int = 40) -> str:
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
        path = self.root / (self._safe_name(name) + ".json")
        if not self._inside(path):
            return False
        blob = json.dumps(mq_security.redact_obj(payload),
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
        path = self.root / (self._safe_name(name) + ".json")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def audit(self, event: str, **fields: Any) -> None:
        """ACTIONS-ONLY audit row."""
        safe = {"event": _clip(event, 40), "ts": _now_iso()}
        for k, v in (fields or {}).items():
            safe[_clip(k, 30)] = v if isinstance(v, (int, float, bool)) \
                else _clip(v, 60)
        self._append_jsonl("audit", safe)

    def _append_jsonl(self, name: str, obj: dict,
                      cap_lines: int = MAX_DOCS) -> bool:
        path = self.root / (self._safe_name(name) + ".jsonl")
        if not self._inside(path):
            return False
        line = json.dumps(mq_security.redact_obj(obj), ensure_ascii=False,
                          default=str)
        try:
            with self._lock:
                lines = []
                if path.exists():
                    lines = path.read_text(encoding="utf-8") \
                        .splitlines()[-(cap_lines - 1):]
                lines.append(line)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             PERMS_FILE)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                self._enforce(path)
            return True
        except Exception:
            return False

    # ── products ────────────────────────────────────────────────────────
    def products(self) -> list[dict]:
        rows = self._read_json("products", [])
        return rows if isinstance(rows, list) else []

    def upsert_product(self, name: str, version: str = "") -> Optional[dict]:
        name = canon_product(name) or _clip(name, 60)
        if not name:
            return None
        rows = self.products()
        hit = next((p for p in rows
                    if p.get("name", "").lower() == name.lower()), None)
        if not hit:
            hit = {"id": f"mq-{uuid.uuid4().hex[:10]}", "name": name,
                   "latest_version": _clip(version, 40),
                   "added_at": _now_iso()}
            rows.append(hit)
        if version:
            hit["latest_version"] = _clip(version, 40)
            hit["version_at"] = _now_iso()
        self._write_json("products", rows[:MAX_PRODUCTS])
        self.audit("product_upserted", name=name.lower())
        return hit

    # ── doc metadata (cited in answers) ─────────────────────────────────
    def doc_meta(self, doc_id: str, default: dict | None = None) -> dict:
        rows = self._read_json("doc_meta", {})
        row = rows.get(_clip(doc_id, 40)) if isinstance(rows, dict) else None
        return row if isinstance(row, dict) else dict(default or {})

    def set_doc_meta(self, doc_id: str, product: str = "", version: str = "",
                     collected: str = "") -> dict:
        blocks = self._read_json("doc_meta", {})
        cur = blocks.get(_clip(doc_id, 40)) if isinstance(blocks, dict) \
            else None
        row = dict(cur) if isinstance(cur, dict) else {}
        row.update({"doc_id": _clip(doc_id, 40),
                    "product": canon_product(product) or _clip(product, 60),
                    "version": _clip(version, 40),
                    "collected": _clip(collected, 10) or _today()})
        blocks = blocks if isinstance(blocks, dict) else {}
        blocks[row["doc_id"]] = row
        self._write_json("doc_meta", blocks)
        self.audit("doc_meta_set")
        return row

    def enrich(self, docs: list[dict]) -> list[dict]:
        """Every doc row gets product/version hints — from stored metadata
        first, then the RAG document's own metadata dict, then title text.
        Nothing is invented: an unknown product stays ''."""
        enriched: list[dict] = []
        for d in (docs or []):
            if not isinstance(d, dict):
                continue
            doc_id = str(d.get("doc_id") or "")
            meta = dict(d.get("metadata") or {})
            stored = self.doc_meta(doc_id)
            product = (stored.get("product") or meta.get("product")
                       or canon_product(d.get("title") or d.get("original_name")
                                        or ""))
            version = (stored.get("version") or meta.get("version")
                       or extract_version(d.get("title") or ""))
            collected = (stored.get("collected")
                         or meta.get("collected")
                         or str(d.get("ingested_at", ""))[:10])
            enriched.append({**d, "product": product, "version": version,
                             "collected": collected})
        return enriched

    # ── summary / wipe ──────────────────────────────────────────────────
    def summary(self) -> dict:
        return {"products": len(self.products()),
                "doc_meta": len(self._read_json("doc_meta", {}))}

    def wipe(self) -> bool:
        ok = True
        with self._lock:
            for name in ("products", "doc_meta"):
                try:
                    (self.root / (name + ".json")).unlink(missing_ok=True)
                except Exception:
                    ok = False
            self._append_jsonl("audit", {"event": "wipe"})
        return ok


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
