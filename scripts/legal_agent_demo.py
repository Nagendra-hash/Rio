#!/usr/bin/env python3
"""
scripts/legal_agent_demo.py — offline end-to-end demo of the Phase 16
Legal Document Analysis agent.

NO network, NO LLM calls: the RAG KB is sandboxed into a temp JARVIS_HOME
and embeddings use a deterministic hash-vector fake (the same technique as
tests/test_metaquest_agent.py), so `retrieve` works offline too.

Walks the full story:
  1. ingest a lease (v1) through the approval gate → nothing stored until
     you approve → resume commits the parked payload
  2. ingest v2 of the same lease, superseding v1
  3. analyze v1 — clauses + extractive summary + risks/questions
  4. compare v2 against v1 — changed clauses with both pages cited
  5. retrieve passages with [n] citations
  6. show the guardrails: an advice-shaped ask is refused in code

Run:  .venv/bin/python scripts/legal_agent_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── sandbox FIRST: JARVIS_HOME before any jarvis import ──────────────────
_TMP = tempfile.mkdtemp(prefix="jarvis-legal-demo-")
os.environ["JARVIS_HOME"] = _TMP
os.environ["JARVIS_LEGAL_AGENT"] = "on"


def _sandbox_rag() -> None:
    """Offline mini KB: hash-vector embeddings, no network, no key."""
    import numpy as np
    from core import rag, rag_service
    kb = Path(_TMP) / "kb"
    kb.mkdir(parents=True, exist_ok=True)
    rag.KB_DIR = kb
    rag.LIBRARY_DIR = kb / "library"
    rag.CHUNKS_FILE = kb / "chunks.jsonl"
    rag.VECTORS_FILE = kb / "vectors.npz"
    rag.REGISTRY_FILE = kb / "registry.json"
    rag.AUDIT_FILE = kb / "audit.log"
    rag_service.CALLERS_FILE = Path(_TMP) / "rag_callers.json"
    rag_service.CALLERS_FILE.write_text(json.dumps({
        "legal_agent": {"collections": ["legal_docs"], "can_write": False},
    }), encoding="utf-8")

    def _fake_embed(texts, is_query=False, **kw):
        out = []
        for t in texts:
            v = np.zeros(rag.EMBED_DIM, dtype=np.float32)
            for w in set(str(t).lower().split()):
                seed = abs(hash(w)) % (rag.EMBED_DIM - 8)
                v[seed:seed + 8] += 1.0
            n = float(np.linalg.norm(v))
            out.append(v / n if n > 0 else v)
        return out

    class _FakeGemini:
        def embed(self, texts, is_query=False, **kw):
            single = isinstance(texts, str)
            vecs = _fake_embed([texts] if single else list(texts), is_query)
            return vecs[0] if single else vecs

    import core as core_pkg
    core_pkg.gemini = _FakeGemini()


LEASE_V1 = """Commercial Lease Agreement
Landlord: Alpha Properties LLC. Tenant: Nimbus Coffee Ltd.
This Lease Agreement is made between the Landlord and the Tenant.

1. Term and Renewal
The Lease Term shall commence on 2026-11-01 for an initial term of
thirty-six (36) months. This agreement shall automatically renew for
successive twelve (12) month periods unless either party gives ninety
(90) days written notice before expiry.

2. Rent and Late Charges
Tenant shall pay monthly rent of 2500.00 USD in advance. Late payment
interest at 5% per month applies to arrears, with no cap and no
limitation on accumulated late charges.

3. Termination
The Landlord may terminate this lease for convenience upon sixty (60)
days written notice. The Tenant may terminate only for Landlord's
material breach uncured within thirty (30) days.

4. Indemnification and Liability
Tenant shall indemnify and hold harmless the Landlord against any and
all claims arising from Tenant's use of the premises. The Landlord's
liability under this lease shall be unlimited.

5. Confidentiality
Tenant shall keep confidential all proprietary information of the
Landlord and non-disclosure obligations survive forever.

6. Governing Law and Arbitration
This Lease is governed by the laws of Delaware. Any dispute shall be
settled by arbitration in Wilmington, Delaware, and Tenant waives any
right to a class action or jury trial.
"""

LEASE_V2 = LEASE_V1.replace(
    "thirty-six (36) months. This agreement shall automatically renew",
    "twenty-four (24) months. This agreement shall not automatically renew"
).replace("2500.00 USD", "2950.00 USD")


def _hr(title: str) -> None:
    print(f"\n{'═' * 74}\n  {title}\n{'═' * 74}")


def main() -> int:
    _sandbox_rag()
    tmp = Path(_TMP) / "docs"
    tmp.mkdir(parents=True, exist_ok=True)

    from orchestrator.agents.legal.legal_agent import LegalAgent
    from orchestrator.agents.legal.vault import LegalVault
    from orchestrator.agents.legal.checkpoint import LegalCheckpoint
    from orchestrator.contracts import AgentRequest, TaskType

    agent = LegalAgent(store=LegalVault(Path(_TMP) / "vault"),
                       checkpoint=LegalCheckpoint(Path(_TMP) / "ckpt"))
    _seq = [0]

    def _ask(text: str, **meta):
        _seq[0] += 1
        return agent.handle(AgentRequest(
            task_id=f"legal-demo-{_seq[0]:04d}", task_type=TaskType.LEGAL,
            text=text, metadata=meta))

    def _approve(req):
        """The approval path: re-dispatch with the SAME task id."""
        return agent.handle(AgentRequest(
            task_id=req.task_id, task_type=TaskType.LEGAL, text="approve",
            metadata={"approved": True, "resume": req.task_id,
                      "by": "demo-user"}))

    _hr("1. INGEST v1 — parked behind approval, nothing stored until yes")
    v1 = tmp / "lease_v1.txt"
    v1.write_text(LEASE_V1, encoding="utf-8")
    req = _ask("ingest this contract", legal_action="ingest", path=str(v1))
    print(req.text)
    res = _approve(req)          # approve: re-dispatch, same task id
    print(f"\n→ {res.status.value}\n{res.text}")

    _hr("2. INGEST v2 — supersedes v1")
    v2 = tmp / "lease_v2.txt"
    v2.write_text(LEASE_V2, encoding="utf-8")
    docs = agent.store.documents()
    v1_id = docs[0]["doc_id"]
    req2 = _ask("ingest v2", legal_action="ingest", path=str(v2),
                supersedes=v1_id)
    res2 = _approve(req2)
    print(f"→ {res2.status.value}\n{res2.text}")
    docs = agent.store.documents()
    v2_id = next(d["doc_id"] for d in docs if d["supersedes"] == v1_id)

    _hr("3. ANALYZE v1 — clauses, extractive summary, risks/questions")
    res = _ask("analyze the current agreement", legal_action="analyze")
    print(res.text)

    _hr("4. COMPARE v2 against v1 — structural version diff")
    res = _ask("compare the two versions", legal_action="compare",
               doc_id_old=v1_id, doc_id_new=v2_id)
    print(res.text)

    _hr("5. RETRIEVE — passages from YOUR documents, [n]-cited")
    res = _ask("where is arbitration handled?",
               legal_action="retrieve",
               query="arbitration Wilmington dispute jury trial")
    print(res.text)

    _hr("6. THE GUARDRAIL — advice is refused in code")
    res = _ask("should i sign this lease?")
    print(f"→ {res.status.value}\n{res.text}")

    _hr("7. THE VAULT — what is stored, where")
    res = _ask("show me the library", legal_action="library")
    print(res.text)
    print(f"\n(vault root: {agent.store.root}")

    _hr("done")
    print(f"sandbox: {_TMP}  (nothing outside this dir was touched)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
