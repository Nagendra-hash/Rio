"""
orchestrator/classifier.py — intent / task classification.

Phase 1 uses a fast, explainable, fully-offline lexical classifier. It is
deliberately conservative: a miss costs nothing (the orchestrator falls back
to the existing assistant, which is exactly today's behavior), while a false
positive steals the turn from the assistant that already works. Accuracy can
be swapped for an LLM later without touching the router — the interface is
one method: classify(text) -> Classification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from orchestrator.contracts import TaskType

_WORD = r"[a-z0-9+#._-]*"


def _verb_noun(verbs: str, nouns: str, gap: str = r"[^.?!\n]{0,40}?") -> str:
    """verb ... noun within a short window, or noun ... verb (Hinglish order)."""
    return (rf"\b(?:{verbs})\b{gap}\b(?:{nouns})\b"
            rf"|\b(?:{nouns})\b{gap}\b(?:{verbs})\b")


# ── CODING ────────────────────────────────────────────────────────────────────
_CODE_NOUNS = (r"code|bug|traceback|stack\s?trace|exception|error\s+message|"
               r"function|class|method|variable|refactor|unit\s?tests?|pytest|"
               r"script|module|package|repo|repository|commit|branch|merge|"
               r"regex|api\s+endpoint|syntax|compile|lint|snippet")
_CODING_RE = re.compile(
    _verb_noun(r"fix|debug|write|refactor|implement|add|create|run|review|"
               r"test|patch|optimi[sz]e|port|migrate|document|explain",
               _CODE_NOUNS)
    + r"|\b(?:python|javascript|typescript|java|c\+\+|rust|golang|sql|"
      r"html|css)\b[^.?!\n]{0,30}?\b(?:script|function|program|code|class)\b"
    + r"|\b(?:traceback|stack\s?trace)\b"
    + r"|\b(?:unit\s?tests?)\b\s+(?:for|that|which|to)\b"
    + r"|\b(?:my|this|the)\s+(?:app|program|script|project)\b"
      r"[^.?!\n]{0,30}?\b(?:is\s+)?(?:broken|crash\w*|fails?|errors?)\b",
    re.IGNORECASE)

# ── RESEARCH ──────────────────────────────────────────────────────────────────
_RESEARCH_RE = re.compile(
    r"\b(?:search|look\s+up|find\s+out|google|research|browse)\b[^.?!\n]{0,40}?"
    r"|\b(?:what(?:'s|s|)|who(?:'s|s|)|when|where|why|which)\s+(?:is|are|was|were|does|do|did)\b"
    + r"|\bhow\s+(?:do|does|did|can|to)\b"
    + r"|\b(?:summar(?:y|ize|ise)|tldr)\b(?:\s+(?:this|that|the|it))?\b"
    + r"|\b(?:latest|current|today'?s?|breaking)\s+(?:news|updates?|price|scores?|weather)\b"
    + r"|\b(?:compare|comparison\s+(?:of|between))\b",
    re.IGNORECASE)

# ── TRADING ───────────────────────────────────────────────────────────────────
_TICKER = r"(?:[A-Z]{1,5}|BTC|ETH|SOL|NIFTY|BANKNIFTY|SENSEX|SPX|SPY|QQQ|GOLD|XAUUSD)"
_TRADING_RE = re.compile(
    rf"\b(?:buy|sell|hold|exit|entry|stop\s?loss|target|book(?:ed)?\s+(?:profit|loss)|"
    rf"position|portfolio|pnl|p&l|margin|leverage|candle|chart|indicator|"
    rf"rsi|macd|ema|sma|moving\s+average|support|resistance)\b"
    rf"|\$\b{_TICKER}\b"
    rf"|\b{_TICKER}\s*(?:usd)?\s*(?:price|chart|analysis|setup|trade|target|"
    rf"stop|outlook|level|levels)\b"
    rf"|\b(?:nifty|banknifty|sensex|crypto|bitcoin|btc|eth|sol)\b"
    rf"[^.?!\n]{{0,30}}?\b(?:analysis|setup|outlook|trade|levels?|chart|target)\b"
    rf"|\b(?:my\s+)?(?:portfolio|holdings?|watchlist)\b",
    re.IGNORECASE)

# ── RAG (private docs) ────────────────────────────────────────────────────────
_RAG_RE = re.compile(
    r"\b(?:from|in|across)\s+(?:my|the|our)\s+(?:docs?|documents?|files?|notes?|pdfs?|reports?)\b"
    + r"|\b(?:my|the)\s+(?:docs?|documents?|files?|notes?|pdfs?|reports?|knowledge\s?base)\b"
      r"[^.?!\n]{0,30}?\b(?:say|says|said|contain|contains|mention|mentions|show|showed|state|states)\b"
    + r"|\b(?:remember(?:ed)?|memoriz(?:e|ed)|index(?:ed)?)\b[^.?!\n]{0,30}?"
      r"\b(?:this|that|the|my)\s+(?:doc|document|file|pdf|note|report)\b"
    + r"|\b(?:which|what)\s+(?:doc|document|file|pdf|note|report)\b"
    + r"|\bupload(?:ed)?\s+(?:this|that|the|my)\s+(?:doc|document|file|pdf|note|report)\b",
    re.IGNORECASE)

# ── SYSTEM (device / process / settings control) ─────────────────────────────
_SYS_RE = re.compile(
    r"\b(?:open|launch|kill|close|start|stop|restart|toggle|mute|unmute|set|turn)\s+"
    r"(?:the\s+)?(?:volume|brightness|wifi|wi-fi|bluetooth|app|application|"
    r"process|service|computer|pc|laptop|screen|browser|music|youtube|"
    r"reminders?|alarm|timer|settings|night\s?light|dnd)\b"
    + r"|\b(?:battery|cpu|ram|memory\s+usage|disk\s+(?:space|usage)|uptime|"
      r"temperature|screenshot|screen\s?shot|lock\s+(?:the\s+)?(?:pc|computer|screen))\b"
    + r"|\b(?:system|network)\s+(?:status|health|info|monitor)\b",
    re.IGNORECASE)

# Explicit "route to X" override — lets users and tests force a lane.
# Each alternative captures the lane word in its OWN group; exactly one group
# matches per hit, so `next(g for g in m.groups() if g)` is safe.
_EXPLICIT_RE = re.compile(
    r"\b(coding|code|research|trading|trader|rag|docs?|system)\s+agent\b"
    r"|\b(?:route|send|forward|hand)\s+(?:this|it)\s+to\s+(?:the\s+)?"
    r"(coding|research|trading|rag|system)\b"
    r"|\[\s*(coding|research|trading|rag|system)\s*\]",
    re.IGNORECASE)

_EXPLICIT_MAP = {
    "coding": TaskType.CODING, "code": TaskType.CODING,
    "research": TaskType.RESEARCH, "trading": TaskType.TRADING,
    "trader": TaskType.TRADING, "rag": TaskType.RAG, "docs": TaskType.RAG,
    "doc": TaskType.RAG, "system": TaskType.SYSTEM,
}

# Greetings / chit-chat / control words must never match a task lane.
_NEUTRAL_RE = re.compile(
    r"^(?:hi|hello|hey|yo|thanks|thank\s+you|ok(?:ay)?|good\s+"
    r"(?:morning|evening|afternoon|night)|stop|cancel|never\s?mind|"
    r"what(?:'s|s|)\s+up|who\s+are\s+you|help)\b[\s!.?]*$",
    re.IGNORECASE)


@dataclass
class Classification:
    task_type: TaskType
    confidence: float          # 0..1
    reason: str                # which rule fired — for logs and tests
    scores: dict = field(default_factory=dict)

    @property
    def is_known(self) -> bool:
        return self.task_type is not TaskType._FALLBACK


class IntentClassifier:
    """Lexical intent classifier. One method, deterministic, offline."""

    # Order matters only for ties; scoring handles overlap.
    _RULES: tuple = (
        (TaskType.TRADING, _TRADING_RE),
        (TaskType.RAG, _RAG_RE),
        (TaskType.CODING, _CODING_RE),
        (TaskType.SYSTEM, _SYS_RE),
        (TaskType.RESEARCH, _RESEARCH_RE),
    )
    _MAX_SCORE = float(len(_RULES))

    def classify(self, text: str) -> Classification:
        text = (text or "").strip()
        if not text:
            return Classification(TaskType._FALLBACK, 0.0, "empty")

        explicit = _EXPLICIT_RE.search(text)
        if explicit:
            key = next(g for g in explicit.groups() if g) if explicit.groups() \
                else explicit.group(0).split()[0].lower().strip("[]")
            key = key.lower()
            if key in _EXPLICIT_MAP:
                return Classification(_EXPLICIT_MAP[key], 0.95,
                                      f"explicit route: {key}")

        if _NEUTRAL_RE.match(text):
            return Classification(TaskType._FALLBACK, 0.9, "neutral/chit-chat")

        scores: dict[str, float] = {}
        best_type, best_reason, best_score = TaskType._FALLBACK, "", 0.0
        for ttype, regex in self._RULES:
            m = regex.search(text)
            if m:
                scores[ttype.value] = round(1.0 + 0.05 * len(m.groups()), 2)
                if scores[ttype.value] > best_score:
                    best_type, best_score = ttype, scores[ttype.value]
                    best_reason = f"matched {ttype.value}: {m.group(0)[:40]!r}"

        if best_score <= 0.0:
            return Classification(TaskType._FALLBACK, 0.0, "no rule matched")

        confidence = min(0.85, 0.5 + 0.35 * (best_score / self._MAX_SCORE))
        return Classification(best_type, round(confidence, 2), best_reason,
                              scores)
