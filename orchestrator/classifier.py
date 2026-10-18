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


# ── REPLICA (Phase 20 clean-room app rebuilds from a Play listing) ───────────
# Deliberately requires store/clone vocabulary so an ordinary "build an
# android app" stays in the CODING lane: only a Play listing, a "clone this
# app" or "my own version of <app>" lands here.
_REPLICA_RE = re.compile(
    r"\bplay\.?\s?store\b|\bplay\.google\.com\b"
    + r"|\b(?:clone|replicate|rebuild|recreate|reverse.?engineer)\b"
      r"[^.?!\n]{0,40}\b(?:app|application|game)\b"
    + r"|\b(?:app|application)\b[^.?!\n]{0,30}?\b(?:from|on)\s+(?:the\s+)?"
      r"(?:google\s+)?play\b"
    + r"|\b(?:better|enhanced|improved)\s+version\s+of\b"
    + r"|\bmy\s+own\s+version\s+of\b"
    + r"|\breplica\s+agent\b",
    re.IGNORECASE)

# ── CODING ────────────────────────────────────────────────────────────────────
_CODE_NOUNS = (r"code|bug|traceback|stack\s?trace|exception|error\s+message|"
               r"function|class|method|variable|refactor|unit\s?tests?|pytest|"
               r"script|module|package|repo|repository|commit|branch|merge|"
               r"regex|api\s+endpoint|syntax|compile|lint|snippet|"
               # Android / mobile builds (the coding agent creates these).
               r"android|kotlin|apk|gradle|android\s+app|mobile\s+app")
_CODING_RE = re.compile(
    _verb_noun(r"fix|debug|write|refactor|implement|add|create|run|review|"
               r"test|patch|optimi[sz]e|port|migrate|document|explain|build|make",
               _CODE_NOUNS)
    + r"|\b(?:python|javascript|typescript|java|c\+\+|rust|golang|sql|"
      r"html|css)\b[^.?!\n]{0,30}?\b(?:script|function|program|code|class)\b"
    + r"|\b(?:traceback|stack\s?trace)\b"
    + r"|\b(?:unit\s?tests?)\b\s+(?:for|that|which|to)\b"
    + r"|\b(?:my|this|the)\s+(?:app|program|script|project)\b"
      r"[^.?!\n]{0,30}?\b(?:is\s+)?(?:broken|crash\w*|fails?|errors?)\b"
    # A bare "create/build/make … android app" — no other code noun needed.
    + r"|\b(?:create|build|make|new|generate|develop|start)\b[^.?!\n]{0,40}?"
      r"\b(?:android|kotlin|apk)\b"
    + r"|\b(?:android|kotlin)\s+app\b",
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

# ── NEWS (Phase 19 news-curator specialist) ──────────────────────────────────
# Deliberately narrow: only head-line style asks ("news", "headlines",
# "top news today", "what's the latest news?"). A topic-research ask such as
# "what's the latest news about EVs" stays in the RESEARCH lane — the
# contract asserted across the agent test suite — so the curator never steals
# a generic research turn.
_NEWS_RE = re.compile(
    r"^\s*(?:news|headlines|top\s+stories)\s*[!?.]*$"
    + r"|^\s*(?:what'?s?|whats|give|show|tell|get)\s+(?:me\s+)?(?:the\s+)?"
      r"(?:latest\s+|top\s+|breaking\s+|today'?s?\s+|current\s+|daily\s+|world\s+)*"
      r"(?:news|headlines|top\s+stories)(?:\s+(?:today|now|please))?\s*[!?.]*$"
    + r"|\b(?:top\s+news|breaking\s+news|top\s+stories|news\s+headlines)\b",
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
    rf"(?!\s+(?:guide|manual|setup\s+guide))"
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
    + r"|\b(?:system|network)\s+(?:status|health|info|monitor)\b"
    # System-agent action verbs over system nouns (Phase-2 agent surface):
    # "kill systemd", "restart cups service", "delete file …" — guarded
    # service/process/file/maintenance asks belong to the SYSTEM lane.
    + r"|\b(?:kill|force\s+kill|stop|start|restart)\s+(?:the\s+)?"
      r"(?:systemd|init|sshd|ssh|docker|nginx|apache2|cups|cron|bluetooth|"
      r"networkmanager|networking|ollama|kernel|driver|firewall|process|"
      r"daemon|unit|port\s+\d+)\b"
    + r"|\b(?:restart|stop|start)\s+(?:the\s+)?[a-z0-9_.-]+\s+service\b"
    + r"|\b(?:delete|remove|unlink)\s+(?:the\s+)?file\b"
    + r"|\b(?:install|uninstall)\s+(?:the\s+)?package\b"
    + r"|\b(?:open|close)\s+(?:the\s+)?port\s+\d+\b"
    + r"|\b(?:edit|update|change)\s+(?:the\s+)?jarvis\s+config\b",
    re.IGNORECASE)

# ── EMAIL (Phase 12 inbox specialist) ─────────────────────────────────────────
_EMAIL_RE = re.compile(
    r"\b(?:check|read|triage|clean|clear|summar(?:y|ize|ise)|draft|reply|"
    r"answer|open|unread)\b[^.?!\n]{0,30}?\b(?:e-?mails?|mails?|inbox|"
    r"inboxes|newsletters?|messages?)\b"
    + r"|\b(?:e-?mails?|inbox|newsletters?|unreads?)\b[^.?!\n]{0,30}?"
      r"\b(?:check|triage|summar\w*|draft|reply|unread|clean|digest)\b"
    + r"|\b(?:newsletters?|digest)\b",
    re.IGNORECASE)

# ── CALENDAR (Phase 12 calendar specialist) ──────────────────────────────────
_CAL_RE = re.compile(
    r"\b(?:availabilit(?:y|ies)|conflicts?|double-?book\w*|slots?|"
    r"reschedul\w*|calendar|meetings?|invites?|schedule)\b",
    re.IGNORECASE)

# ── HEALTH (Phase 11 health specialist) ──────────────────────────────────────
_HEALTH_RE = re.compile(
    r"\b(?:fever|headaches?|migraines?|symptoms?|medications?|medicines?|"
    r"prescriptions?|dosages?|blood\s+pressure|blood\s+sugar|sugar\s+levels?|"
    r"glucose|lab\s+reports?|blood\s+tests?|homeopathy|ayurveda|allopathy|"
    r"remed(?:y|ies)|vaccines?|diagnos\w*|sick(ness)?)\b",
    re.IGNORECASE)

# ── BRIEFING (Phase 13 daily-briefing specialist) ────────────────────────────
_BRIEF_RE = re.compile(
    r"\b(?:briefings?|daily\s+brief\w*|morning\s+brief\w*|brief\s+me|"
    r"daily\s+digests?|daily\s+summary|my\s+day\s+ahead)\b",
    re.IGNORECASE)

# ── MEAL (Phase 13 meal/grocery specialist) ──────────────────────────────────
_MEAL_RE = re.compile(
    r"\b(?:meals?|menus?|recipes?|grocer(?:y|ies)|grocery\s+lists?|pantry|"
    r"ingredients?|cook\w*|dinner|lunch|breakfast|kitchen|weekly\s+menu|"
    r"expir(?:y|ies|ing))\b",
    re.IGNORECASE)

# ── TRAVEL (Phase 13 travel specialist) ──────────────────────────────────────
_TRAVEL_RE = re.compile(
    r"\b(?:flights?|hotels?|itinerar\w*|trips?|vacations?|holiday|"
    r"train\s+(?:tickets?|from|to|schedule|booking|book)\w*|"
    r"book\s+(?:a\s+)?(?:flight|hotel|train|bus)|buses?|road\s?trips?|"
    r"things\s+to\s+do|tourists?|tourism|sightseeing|routes?\s+"
    r"(?:from|to|between|comparison|compare)|pack(?:ing)?\s+lists?)\b",
    re.IGNORECASE)

# ── FINANCE (Phase 14 personal finance specialist) ──────────────────────
_FINANCE_RE = re.compile(
    r"\b(?:receipts?|expenses?|expenditure|spending|spent|spend|budgets?|"
    r"monthly\s+budget|recurring\s+(?:payments?|bills?|expenses?)|"
    r"subscriptions?|overspend\w*|bills?\s+paid|track\s+my\s+spending|"
    r"spend\s+analysis|expense\s+report|import\s+receipts?)\b",
    re.IGNORECASE)

# ── STUDY (Phase 15 study-partner specialist) ────────────────────────────
_STUDY_RE = re.compile(
    r"\b(?:study|studying|revise|revision|learn\s+\w+|learning\s+goal|"
    r"exam|exams|syllabus|curriculum|quiz|quizzes|flashcards|"
    r"practice\s+(?:problems?|questions?|exercises?)|study\s+plan|"
    r"explain\s+.{0,30}\b(?:simply|again|differently)|my\s+progress|"
    r"test\s+me|teach\s+me|memori[sz]e)\b",
    re.IGNORECASE)

# ── META QUEST (Phase 15 Meta Quest knowledge specialist) ────────────────
_METAQUEST_RE = re.compile(
    r"\b(?:meta\s+quest|quest\s+(?:[123]\b|pro\b|3s?\b|store\b|link\b)|"
    r"link\s+cable|air\s+link|sidequest|oculus\s+(?:go|rift|quest)|"
    r"standalone\s+(?:vr\s+)?headset|passthrough|guardian\s+boundary|"
    r"sideload\s+quest|horizon\s+(?:os|store|home)|beat\s+saber\s+quest|"
    r"vr\s+headset|quest\s+headset)\b",
    re.IGNORECASE)

# ── SQL (Phase 17 SQL-over-permitted-databases specialist) ───────────────
# Deliberately narrow: database/schema/SELECT/SQL-shaped asks. A generic
# "show me X" stays with its own lane; only database-vocabulary asks land
# here, so a miss costs nothing (fallback assistant already handles it).
_SQL_RE = re.compile(
    r"\b(?:select|insert|update|delete)\s+.+?\bfrom\b|\b(?:inner\s+|left\s+|right\s+|cross\s+)?join\b"
    + r"|\bgROUP\s+BY\b|\border\s+by\b|\bCREATE\s+TABLE\b|\bDROP\s+TABLE\b"
    + r"|\b(?:database|schema|sqlite|db\s+file)\b[^.?!\n]{0,40}?"
      r"\b(?:tables?|columns?|rows?|schema|inspect|query|count|list)\b"
    + r"|\b(?:tables?|columns?|schema)\s+(?:in|of|from)\s+(?:the\s+)?(?:database|db|sqlite)\b"
    + r"|\b(?:how\s+many|count|total)\b[^.?!\n]{0,40}?\b(?:in|from|of)\s+(?:the\s+)?(?:database|db|table\b)"
    + r"|\b(?:sql\s+query|sql|query\s+the\s+database)\b",
    re.IGNORECASE)

# ── GROWTH (Phase 18: self-eval / lead score / marketing) ────────────
# Deliberately narrow, same policy as the SQL lane: a miss costs nothing
# (fallback assistant), a false positive steals the turn. Only explicit
# business-vocabulary asks land here.
_SELF_EVAL_RE = re.compile(
    r"\b(?:self[- ](?:eval(?:uation)?|review|assessment)|"
    r"business\s+review|weekly\s+review|monthly\s+review|"
    r"retrospective|retro\b|improvement\s+plan|"
    r"(?:my|the|our)\s+goals?\b|\bgoal\s+progress|"
    r"how\s+(?:am|i\s+am)\s+doing\s+against)\b",
    re.IGNORECASE)

_LEAD_RE = re.compile(
    r"\b(?:lead\s+scor(?:e|ing)|score\s+(?:my\s+|the\s+)?leads?|"
    r"prioriti[sz]e\s+(?:my\s+)?(?:leads?|outreach|prospects?)|"
    r"(?:hot|warm|cold)\s+leads?|lead\s+(?:pipeline|priorit(?:y|ies)|list)|"
    r"(?:scoring\s+)?rules?\s+for\s+leads?|ingest\s+leads?)\b",
    re.IGNORECASE)

_MARKETING_RE = re.compile(
    r"\b(?:marketing\s+(?:strategy|plan|brief|campaign)|"
    r"campaign\s+(?:ideas?|plan|strategy)|go[- ]to[- ]market|"
    r"competitor\s+(?:analysis|research|teardown)|market\s+research|"
    r"positioning\s+options?|customer\s+research)\b",
    re.IGNORECASE)

# ── LEGAL (Phase 16 legal document analysis specialist) ──────────────────
_LEGAL_RE = re.compile(
    r"\b(?:contract|agreement|lease|tenancy|nda|non-?disclosure|"
    r"terms\s+of\s+(?:service|use)|privacy\s+policy|eula|"
    r"statement\s+of\s+work|sow|moa|mou|deed|"
    r"indenture|power\s+of\s+attorney|affidavit|settlement)\b"
    + r"|\b(?:clause|clauses|liability|indemnity|indemnification|"
      r"arbitration|governing\s+law|jurisdiction\s+clause|termination\s+"
      r"clause|confidentiality\s+clause|auto-?renewal|non-?compete)\b"
    + r"|\b(?:legal\s+)?(?:document|docs?)\s+(?:analysis|review)\b"
    + r"|\b(?:analyze|review|compare|summar(?:y|ize|ise)|extract)\b"
      r"[^.?!\n]{0,40}?\b(?:contract|agreement|lease|terms|policy)\b"
    + r"|\b(?:contract|agreement|lease|policy)\b[^.?!\n]{0,40}?"
      r"\b(?:analyze|review|compare|summar(?:y|ize|ise)|extract|risk|"
      r"clauses?|diff|version)\b",
    re.IGNORECASE)

# Explicit "route to X" override — lets users and tests force a lane.
# Each alternative captures the lane word in its OWN group; exactly one group
# matches per hit, so `next(g for g in m.groups() if g)` is safe.
_EXPLICIT_RE = re.compile(
    r"\b(coding|code|replica|clone|research|trading|trader|rag|docs?|system|email|"
    r"calendar|health|briefing|news|meal|travel|finance|study|tutor|"
    r"meta\s?quest|quest|legal|sql|lead|leads|marketing|campaign|"
    r"self\s?eval|retro)\s+agent\b"
    r"|\b(?:route|send|forward|hand)\s+(?:this|it)\s+to\s+(?:the\s+)?"
    r"(coding|replica|research|trading|rag|system|email|calendar|health|"
    r"briefing|news|meal|travel|finance|study|tutor|meta\s?quest|quest|legal|"
    r"sql|lead|leads|marketing|campaign|self\s?eval|retro)\b"
    r"|\[\s*(coding|replica|research|trading|rag|system|email|calendar|health|"
    r"briefing|news|meal|travel|finance|study|tutor|meta\s?quest|quest|legal|"
    r"sql|lead|leads|marketing|campaign|self\s?eval|retro)\s*\]",
    re.IGNORECASE)

_EXPLICIT_MAP = {
    "coding": TaskType.CODING, "code": TaskType.CODING,
    "replica": TaskType.REPLICA, "clone": TaskType.REPLICA,
    "research": TaskType.RESEARCH, "trading": TaskType.TRADING,
    "trader": TaskType.TRADING, "rag": TaskType.RAG, "docs": TaskType.RAG,
    "doc": TaskType.RAG, "system": TaskType.SYSTEM,
    "email": TaskType.EMAIL, "calendar": TaskType.CALENDAR,
    "health": TaskType.HEALTH, "briefing": TaskType.BRIEFING,
    "news": TaskType.NEWS,
    "meal": TaskType.MEAL, "travel": TaskType.TRAVEL,
    "finance": TaskType.FINANCE,
    "study": TaskType.STUDY, "tutor": TaskType.STUDY,
    "metaquest": TaskType.META_QUEST, "meta quest": TaskType.META_QUEST,
    "quest": TaskType.META_QUEST,
    "legal": TaskType.LEGAL,
    "sql": TaskType.SQL,
    "self eval": TaskType.SELF_EVAL, "selfeval": TaskType.SELF_EVAL,
    "self-eval": TaskType.SELF_EVAL, "retro": TaskType.SELF_EVAL,
    "lead": TaskType.LEAD_SCORE, "leads": TaskType.LEAD_SCORE,
    "lead score": TaskType.LEAD_SCORE, "marketing": TaskType.MARKETING,
    "campaign": TaskType.MARKETING,
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

    # Order matters only for ties; scoring handles overlap. The personal
    # lanes sit before RESEARCH so "summarize that email" resolves to the
    # inbox specialist, not the research one (both score 1.0 on a tie).
    _RULES: tuple = (
        (TaskType.TRADING, _TRADING_RE),
        (TaskType.RAG, _RAG_RE),
        (TaskType.REPLICA, _REPLICA_RE),
        (TaskType.CODING, _CODING_RE),
        (TaskType.SYSTEM, _SYS_RE),
        (TaskType.EMAIL, _EMAIL_RE),
        (TaskType.CALENDAR, _CAL_RE),
        (TaskType.HEALTH, _HEALTH_RE),
        (TaskType.BRIEFING, _BRIEF_RE),
        (TaskType.MEAL, _MEAL_RE),
        (TaskType.TRAVEL, _TRAVEL_RE),
        (TaskType.FINANCE, _FINANCE_RE),
        (TaskType.STUDY, _STUDY_RE),
        (TaskType.META_QUEST, _METAQUEST_RE),
        (TaskType.LEGAL, _LEGAL_RE),
        (TaskType.SQL, _SQL_RE),
        (TaskType.SELF_EVAL, _SELF_EVAL_RE),
        (TaskType.LEAD_SCORE, _LEAD_RE),
        (TaskType.MARKETING, _MARKETING_RE),
        (TaskType.NEWS, _NEWS_RE),
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
