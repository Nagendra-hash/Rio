"""
core/rag_strategies/heuristics.py — deterministic query/evidence scoring.

The offline floor of every strategy: with NO model calls at all these
functions still classify the query, score the evidence and grade the
hallucination risk. The LLM-based strategies refine these same decisions
with judges; the local strategies stop here.

Deterministic on purpose: evaluation tests must be reproducible.
"""
from __future__ import annotations

import re
from math import log

# ── query classification (step 1) ────────────────────────────────────────────

_QUESTION_MARKS = ("?", "؟")
_COMMAND_HINTS = ("open ", "close ", "launch ", "set volume", "brightness",
                  "shutdown", "reboot", "play ", "pause ", "send ", "type ")
_SOCIAL_HINTS = ("hello", "hi ", "hey", "good morning", "good evening",
                 "thanks", "thank you", "bye", "how are you")
_MULTI_HOP = ("and then", "after that", "which of the", "compare ",
              "difference between", "versus", " vs ", "first.*then",
              "relationship between", "how does .* affect")
_ARITHMETIC = re.compile(r"\b\d+\s*[+\-*/x×%]\s*\d+\b")
_ENTITY_Q = re.compile(r"\b(who|whose|when|where|which year|what year)\b", re.I)
_EXPLAINS = re.compile(r"\b(why|how|explain|what is|what are|define|meaning"
                       r"|difference|pros and cons|trade-?offs?|walk me "
                       r"through)\b", re.I)
_VAGUE = ("it", "this", "that", "stuff", "things", "recent", "latest", "news")


def classify_query(query: str) -> dict:
    """Step 1: complexity/type/multi-hop, no model involved.

    complexity: simple | moderate | complex
        simple   → chit-chat, one-word commands, arithmetic
        moderate → single-fact questions, greetings with content
        complex  → explanations, comparisons, multi-hop, vague references
    """
    q = (query or "").strip()
    low = q.lower()
    words = low.split()
    if not q:
        return {"complexity": "simple", "type": "empty", "multi_hop": False,
                "words": 0, "reason": "empty"}

    if any(h in low for h in _SOCIAL_HINTS) and len(words) <= 4:
        return {"complexity": "simple", "type": "social", "multi_hop": False,
                "words": len(words), "reason": "greeting/chit-chat"}
    if _ARITHMETIC.search(low):
        return {"complexity": "simple", "type": "arithmetic",
                "multi_hop": False, "words": len(words),
                "reason": "answer is computation, not knowledge"}
    if any(low.startswith(h) or f" {h}" in f" {low} " for h in _COMMAND_HINTS) \
            and "?" not in q and len(words) <= 6:
        return {"complexity": "simple", "type": "command", "multi_hop": False,
                "words": len(words), "reason": "OS/device action"}

    multi_hop = bool(re.search("|".join(_MULTI_HOP), low))
    is_question = q.endswith(_QUESTION_MARKS) or bool(_EXPLAINS.search(low))
    is_entity = bool(_ENTITY_Q.search(low))

    if multi_hop or (len(words) >= 25):
        complexity, rtype = "complex", ("multi_hop" if multi_hop else "open")
    elif is_question and (bool(_EXPLAINS.search(low)) or len(words) > 8):
        complexity, rtype = "complex", "explanation"
    elif is_question or is_entity or len(words) >= 4:
        complexity, rtype = "moderate", ("entity" if is_entity else "lookup")
    else:
        complexity, rtype = "moderate", "lookup"

    if len(words) <= 2 and not any(w in low for w in _VAGUE):
        complexity = "moderate" if is_question else complexity
    vague_ref = any(w in low for w in _VAGUE) and not q.endswith(_QUESTION_MARKS)
    return {"complexity": complexity, "type": rtype, "multi_hop": multi_hop,
            "words": len(words), "reason": ("vague reference" if vague_ref
                                            else rtype)}


# ── evidence evaluation (step 4) ─────────────────────────────────────────────

_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "have", "has",
    "are", "was", "were", "will", "would", "could", "should", "into",
    "about", "their", "them", "there", "than", "then", "when", "what",
    "which", "your", "you", "its", "it's", "also", "not", "but", "can",
    # short function words: they match almost every sentence and would let a
    # nearest-span answer slip past the refusal threshold
    "of", "to", "in", "on", "at", "by", "as", "is", "be", "do", "does",
    "did", "or", "if", "so", "up", "an", "we", "it", "no", "he", "she"}


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{2,}", text.lower())) - _STOPWORDS


def support_score(answer: str, evidence_text: str) -> float:
    """0..1: how much of the ANSWER's terms the evidence covers.

    Direction matters: coverage of the answer by the evidence (is what we
    claim present in what we retrieved?), never the reverse — a short answer
    against a library-sized text would otherwise always look unsupported.
    """
    a, e = _terms(answer), _terms(evidence_text)
    if not a or not e:
        return 0.0
    return round(len(a & e) / len(a), 4)


def score_evidence(text: str, queries: list[str]) -> float:
    """0..1: lexical coverage of the query terms by this chunk.

    Purely deterministic — the cheap judge every strategy gets for free. The
    LLM strategies layer a graded relevance ON TOP of this; the local
    strategies rely on it alone.
    """
    if not text or not queries:
        return 0.0
    ev = _terms(text)
    if not ev:
        return 0.0
    q_terms = set()
    for q in queries:
        q_terms |= _terms(q)
    if not q_terms:
        return 0.0
    overlap = ev & q_terms
    # coverage of the query matters more than raw hits, idf-ish damping on
    # very long chunks keeps a kitchen-sink chunk from scoring perfectly
    coverage = len(overlap) / len(q_terms)
    length_penalty = 1.0 if len(text) < 1500 else 1500.0 / len(text)
    return round(min(1.0, coverage * (0.6 + 0.4 * length_penalty)), 4)


# ── hallucination risk (step 9 telemetry) ────────────────────────────────────

_HEDGES = ("maybe", "perhaps", "likely", "possibly", "i think", "probably",
           "might be", "not sure", "unclear")
_ABSOLUTES = ("always", "never", "guaranteed", "definitely", "everyone knows")
_UNGROUNDED_MARKERS = ("as we all know", "it is well known", "obviously",
                       "in general", "research shows", "studies show")


def hallucination_risk(answer: str, evidence, grounded: bool) -> str:
    """low | medium | high — deterministic grade over the final answer.

    grounded answer + strong support      → low
    grounded but hedged/marker-heavy      → medium
    no grounding, or confident fabrication markers → high
    """
    a = (answer or "").strip()
    if not a:
        return "high"
    low = a.lower()
    # citation markers [1] are numbers to the regex — a well-cited answer
    # must not look like it invented "1". Strip them before counting.
    low_no_cites = re.sub(r"\[\d+\]", "", low)
    ev_texts = [e.text for e in evidence] if not isinstance(evidence, str) \
        else [evidence]
    support = max((support_score(a, t) for t in ev_texts), default=0.0) \
        if ev_texts else 0.0

    hedges = sum(low.count(h) for h in _HEDGES)
    absolutes = sum(low.count(m) for m in _ABSOLUTES)
    ungrounded = sum(low.count(m) for m in _UNGROUNDED_MARKERS)
    numbers = len(re.findall(r"\b\d+(\.\d+)?%?\b", low_no_cites))
    ev_numbers = len(re.findall(r"\b\d+(\.\d+)?%?\b", " ".join(ev_texts))) \
        if ev_texts else 0

    if not grounded or support < 0.15:
        base = "high"
    elif support < 0.65 or ungrounded or hedges >= 2:
        # over a third of the answer's terms are not in any evidence —
        # the model is bridging from its own head, even if it saw evidence
        base = "medium"
    else:
        base = "low"

    # confident numbers that appear nowhere in the evidence is the classic
    # fabrication tell — bump the grade when seen
    if numbers and ev_numbers == 0 and base != "high":
        base = "medium" if base == "low" else "high"
    if absolutes and base == "low":
        base = "medium"
    return base


# ── lexical fallback scoring (offline retrieval when embeddings are dead) ────

def lexical_overlap(query: str, text: str) -> float:
    q, t = _terms(query), _terms(text)
    if not q or not t:
        return 0.0
    overlap = len(q & t)
    return round(min(1.0, overlap * log(1 + overlap) / len(q)), 4) \
        if overlap else 0.0
