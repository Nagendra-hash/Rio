"""
core/rag_strategies/llm.py — the two ANSWERERS + the model wiring.

An answerer is one callable:  answer(query, evidence_budget) → dict with
    text        the answer
    grounded    is it supported by the evidence (not the model's head)?
    confidence  0..1 (strategies may override — agentic's judge does)
    error       None or why generation failed

TWO IMPLEMENTATIONS

    llm_answerer        — grounded generation through the model ladder
                          (gemini.as_json → gemini.text), with a SUPPORT
                          check (Self-RAG's ISSUP) on the answer it gave.
                          Readiness is probed once per process via
                          llm_ready(); when the ladder is dead this answerer
                          is not even offered to a strategy.
    extractive_answerer — the offline floor. NO model at all: picks the
                          best-matching sentence span(s) from the retrieved
                          evidence itself. Unsupported claims are structurally
                          impossible — the answer IS the evidence, so it
                          always arrives with its citation attached.
"""
from __future__ import annotations

import re

from core.rag_strategies import heuristics

# ── the model bridge ─────────────────────────────────────────────────────────


def llm_ready() -> bool:
    """Is any generation rung reachable right now?

    Checked cheaply: an API key exists, OR the local rung (Ollama/GGUF) can
    serve. No network call happens here — the ladder itself does that on the
    first real call and its failure mode is a plain `None`.
    """
    try:
        from core import gemini
        if gemini.api_key():
            return True
    except Exception:
        pass
    try:
        from core import llm_local
        st = llm_local.status()
        return bool(st.get("configured") and st.get("ram_ok"))
    except Exception:
        return False


def llm_call(prompt: str, tier: str = "fast") -> dict | None:
    """One JSON-verdict call through the ladder. Returns dict | None.

    tier: "fast" (routers/judges) or "smart" (final answers). The ladder walks
    live → REST → LOCAL rung, so a fully offline machine still gets answers
    whenever the local model can serve — that keeps the LLM strategies usable
    without any API key, which is what 'offline available when practical'
    means here.
    """
    try:
        from core import gemini
        verdict = gemini.as_json(prompt, tier=tier)
        return verdict if isinstance(verdict, dict) else None
    except Exception:
        return None


def llm_text(prompt: str, tier: str = "fast") -> str:
    """One plain-text call through the ladder (offline falls back inside)."""
    try:
        from core import gemini
        return gemini.text(prompt, tier=tier)
    except Exception:
        return ""


# ── 1. the LLM answerer (steps 6 + Self-RAG ISSUP) ───────────────────────────

_GROUND_SYSTEM = (
    "You answer STRICTLY from the numbered evidence passages provided. "
    "If the passages do not contain the answer, say exactly: "
    "The knowledge base does not cover this. Never invent facts, names or "
    "numbers. Cite like [1] after each claim.")

_PROMPT = """Answer the question using ONLY the evidence below.

Question: {query}

Evidence:
{evidence}

Rules:
- Cite passages inline like [1], [2].
- If the evidence is insufficient, reply exactly: {refuse}
- Do not use anything you know that is not in the evidence.

Answer:"""


def llm_answerer(query: str, evidence_budget: str) -> dict:
    if not evidence_budget.strip():
        # retrieval was skipped (chit-chat / command): answer conversationally,
        # but mark it UNGROUNDED — nothing here came from the library
        text = llm_text(query, tier="fast")
        return {"text": text, "grounded": False,
                "confidence": 0.4 if text else 0.0,
                "error": None if text else "no model answered"}
    prompt = _PROMPT.format(query=query, evidence=evidence_budget,
                            refuse="The knowledge base does not cover this.")
    text = llm_text(prompt, tier="smart")
    if not text:
        return {"text": "", "grounded": False, "confidence": 0.0,
                "error": "no model answered"}
    grounded = "does not cover this" not in text.lower()
    conf = 0.0
    if grounded:
        # ISSUP-style support check: how much of the answer's terms are in
        # the evidence it cites? One cheap deterministic pass — a dedicated
        # judge call here cost more than it caught in testing.
        cited = re.findall(r"\[(\d+)\]", text)
        support = heuristics.support_score(text, evidence_budget)
        conf = round(min(1.0, 0.5 + 0.5 * support), 3)
        if not cited and support < 0.3:
            grounded = False          # claims with no citation, no support
    return {"text": text, "grounded": grounded, "confidence": conf,
            "error": None}


# ── 2. the extractive answerer (offline floor) ───────────────────────────────

_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def extractive_answerer(query: str, evidence_budget: str) -> dict:
    """Answer = the best-matching sentence(s) FROM the evidence, verbatim.

    Returns the answer text plus, via the pipeline's evidence rows, the same
    chunks — so every claim ships with its citation. When nothing in the
    evidence matches, it says so instead of guessing: that refusal IS the
    hallucination reduction.
    """
    if not evidence_budget.strip():
        return {"text": "", "grounded": False, "confidence": 0.0,
                "error": "no evidence"}
    # split the budget back into its [n] blocks (same format we built it in)
    blocks: list[tuple[int, str, str]] = []   # (n, source_ref, block_text)
    for part in re.split(r"\n\n(?=\[\d+\])", evidence_budget):
        m = re.match(r"\[(\d+)\]\s*([^\n]+)\n?(.*)", part, re.S)
        if m:
            blocks.append((int(m.group(1)), m.group(2).strip(),
                           m.group(3).strip()))

    q_terms = heuristics._terms(query)
    best: list[tuple[float, int, str]] = []   # (score, block_n, sentence)
    for n, ref, body in blocks:
        for sent in _SPLIT.split(body):
            sent = sent.strip()
            if len(sent) < 25:
                continue
            s_terms = heuristics._terms(sent)
            if not q_terms or not s_terms:
                continue
            overlap = len(q_terms & s_terms) / len(q_terms)
            if overlap > 0:
                best.append((overlap, n, sent))
    if not best:
        return {"text": "The knowledge base does not cover this.",
                "grounded": False, "confidence": 0.0, "error": None}

    best.sort(key=lambda t: (-t[0], t[1]))
    top_score = best[0][0]
    if top_score < 0.2:
        return {"text": "The knowledge base does not cover this.",
                "grounded": False, "confidence": 0.0, "error": None}

    # cite by BLOCK NUMBER — [1] matches the evidence budget's numbering,
    # so a reader (human or model) can jump from the claim to the passage,
    # and the citation text itself never pollutes the answer's terms
    picked, seen = [], set()
    for score, n, sent in best:
        if n in seen:
            continue
        seen.add(n)
        picked.append(f"{sent} [{n}]")
        if len(picked) >= 3:
            break
    return {"text": " ".join(picked), "grounded": True,
            "confidence": round(min(1.0, top_score + 0.3), 3), "error": None}
