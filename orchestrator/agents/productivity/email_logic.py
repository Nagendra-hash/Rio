"""
orchestrator/agents/productivity/email_logic.py — deterministic inbox brain.

No LLM in the loop. Everything here is rule-based and unit-testable:

    classify_message()   newsletter / transactional / personal / urgent
    is_newsletter()      List-Unsubscribe headers + bulk-sender shapes
    urgency_score()      0-3 from subject/body vocabulary + sender signals
    summarize_message()  extractive: first sentences + action asks
    draft_reply()        polite reply skeleton seeded by the message

The agent layers approval policy on top: nothing here sends anything.
"""
from __future__ import annotations

import re
from email.utils import parseaddr
from typing import Optional

# ── newsletter detection ─────────────────────────────────────────────────────

_BULK_HEADER_RE = re.compile(
    r"<[^>]*(?:unsubscri|list-unsubscribe|mailto:)[^>]*>"
    r"|\bhttps?://\S*(?:unsubscri|optout|opt-out)\S*", re.IGNORECASE)
_LIST_ID_RE = re.compile(r"(?i)\b(list(?:-id)?|mailing[- ]?list|newsletter|"
                         r"campaign|bulletin|digest|digests)\b")
_NEWS_SENDER_RE = re.compile(
    r"(?i)\b(?:newsletter\w*|digest|bulletin|updates?|no-?reply|"
    r"donotreply|noreply|news|mail(ing)?(er)?|promo\w*|offers?|"
    r"marketing|notifications?|alerts?|receipts?|billing)\b")
_NEWS_SUBJECT_RE = re.compile(
    r"(?i)\b(?:newsletter|digest|weekly|monthly|roundup|"
    r"\d+%\s*off|sale|deals?|webinar|unsubscribe|offer\b)")
_PREHEADER_RE = re.compile(
    r"(?i)(?:view (?:this|in) (?:email|browser)|trouble reading"
    r"(?: this)?|receiv\w+ this (?:email|message) because|"
    r"unsubscribe(?: from| link)?(?: any(?:time|where))?|"
    r"our (?:mailing )?(?:address|records)|sent to you by|"
    r"email preferences|manage (?:your )?preferences)")
_UNSUB_MAGIC_RE = re.compile(r"(?i)(?:^|\.\s)\s*(?:unsubscribe|opt[- ]?out)\b")


def _has_transactional_vocab(msg: dict) -> bool:
    """Receipt/OTP/verification vocabulary in subject or body head."""
    blob = " ".join([(msg.get("subject") or ""),
                     (msg.get("body") or "")[:400],
                     (msg.get("snippet") or "")])
    return bool(_TRANSACTIONAL_RE.search(blob))


def is_newsletter(msg: dict) -> bool:
    """Header + sender + subject + body-shape signals. Any TWO → newsletter.

    A strong TRANSACTIONAL message (receipt/OTP vocabulary) is never a
    newsletter, even when it carries bulk headers — verification mails and
    receipts carry List-Unsubscribe too, but the user must never 'clean'
    them as newsletters.
    """
    if _has_transactional_vocab(msg):
        return False
    sender = (msg.get("from") or "")
    subject = (msg.get("subject") or "")
    body = (msg.get("body") or msg.get("snippet") or "")[:2000]
    unsub = (msg.get("unsubscribe") or "")

    score = 0
    if _NEWS_SENDER_RE.search(_local_part(sender) or sender):
        score += 2
    if unsub and _BULK_HEADER_RE.search(unsub):
        score += 2
    if _LIST_ID_RE.search(subject) or _NEWS_SUBJECT_RE.search(subject):
        score += 1
    hits = sum(1 for _ in _PREHEADER_RE.finditer(body))
    if hits >= 1:
        score += 1
    if _UNSUB_MAGIC_RE.search(body[:200]):
        score += 1
    # A strong bulk header alone is decisive — that's what the header is FOR.
    if unsub and _BULK_HEADER_RE.search(unsub) and score >= 3:
        return True
    return score >= 2


def _local_part(sender: str) -> str:
    addr = parseaddr(sender)[1] or sender
    return addr.split("@", 1)[0]


# ── transactional detection (receipts, OTPs, notifications) ─────────────────

_TRANSACTIONAL_RE = re.compile(
    r"(?i)\b(?:receipt|invoice|order\s+(?:confirmation|shipped|delivered)|"
    r"payment|your\s+(?:order|account|subscription)|verify|verification|"
    r"one[- ]time\s+(?:code|password)|otp|password\s+reset|"
    r"security\s+(?:alert|code|notice)|2fa|login\s+(?:code|attempt))\b")


def is_transactional(msg: dict) -> bool:
    return _has_transactional_vocab(msg) and not is_newsletter(msg)


# ── urgency ──────────────────────────────────────────────────────────────────

# Priority senders are a deployment preference, not a hard rule.
DEFAULT_PRIORITY_SENDERS = ("boss@", "ceo@", "cto@", "manager@",
                            "hr@", "billing@", "admin@")

_URGENT_RE = re.compile(
    r"(?i)\b(?:urgent|asap|immediately|emergency|time[- ]sensitive|"
    r"deadline\s+(?:today|tomorrow|in\s+\d+\s+(?:hours?|days?))|"
    r"by\s+(?:end\s+of\s+)?(?:today|eod|tomorrow|friday|monday)|"
    r"action\s+required|response\s+needed|expires?\s+(?:today|tomorrow|soon)|"
    r"last\s+(?:chance|day)|final\s+(?:reminder|notice)|overdue|"
    r"account\s+(?:suspended|locked)|payment\s+(?:failed|declined|due))\b")
_IMPORTANT_RE = re.compile(
    r"(?i)\b(?:meeting|interview|contract|invoice|legal|doctor|"
    r"appointment|flight|hotel|booking|ticket|school|visa|"
    r"tax|bank|payment|application)\b")
_QUESTION_RE = re.compile(
    r"(?i)\b(?:can\s+you|could\s+you|please|need\s+you\s+to|"
    r"let\s+me\s+know|when\s+(?:are|can|would)|rsvp|confirm)\b")


def urgency_score(msg: dict, priority_senders: tuple = ()) -> int:
    """0 = routine … 3 = act now. Deliberately conservative."""
    subject = (msg.get("subject") or "")
    body = (msg.get("body") or msg.get("snippet") or "")[:1500]
    sender = (msg.get("from") or "").lower()
    score = 0

    if is_newsletter(msg):
        return 0

    if _URGENT_RE.search(subject) or _URGENT_RE.search(body[:400]):
        score = 3
    elif _IMPORTANT_RE.search(subject):
        score = max(score, 2)
    if _QUESTION_RE.search(body):
        score = max(score, 2 if score < 2 else score)
    if any(p in sender for p in (priority_senders
                                 or DEFAULT_PRIORITY_SENDERS)):
        score = max(score, 2)
    if "important" in (msg.get("labels") or []) or \
            "IMPORTANT" in (msg.get("labels") or []):
        score = max(score, 2)
    if is_transactional(msg):
        score = max(score, 1)
    return min(3, score)


URGENCY_LABELS = {0: "routine", 1: "notable", 2: "important", 3: "urgent"}


# ── classification ───────────────────────────────────────────────────────────

def classify_message(msg: dict, priority_senders: tuple = ()) -> dict:
    """One row: category + urgency + why. Deterministic, explainable."""
    if is_newsletter(msg):
        category = "newsletter"
    elif is_transactional(msg):
        category = "transactional"
    else:
        category = "personal"
    score = urgency_score(msg, priority_senders)
    why: list[str] = []
    if category == "newsletter":
        why.append("bulk sender / unsubscribe signals")
    elif category == "transactional":
        why.append("receipt / notification vocabulary")
    else:
        why.append("personal sender")
    if score >= 3:
        why.append("urgent vocabulary")
    elif score == 2:
        why.append("important topic or priority sender")
    return {"category": category,
            "urgency": score,
            "urgency_label": URGENCY_LABELS[score],
            "reasons": why}


# ── summarization (extractive) ───────────────────────────────────────────────

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])")
_QUESTIONY_RE = re.compile(r"(?i)\b(?:can|could|would|will|did|do|are|is|"
                           r"when|where|how)\b.*\?")
_LEADIN_RE = re.compile(
    r"(?i)^(?:hi|hello|hey|dear)[^.!?\n]{0,60}[.!?\n]?\s*")


def _sentences(text: str) -> list[str]:
    text = _LEADIN_RE.sub("", (text or "").strip())
    text = re.sub(r"\s+", " ", text)
    return [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()]


def summarize_message(msg: dict, max_chars: int = 300) -> str:
    """Extractive summary: first meaningful sentence(s) + any direct ask."""
    body = msg.get("body") or msg.get("snippet") or ""
    sents = _sentences(body)
    if not sents:
        return (msg.get("snippet") or msg.get("subject") or "")[:max_chars]
    asks = [s for s in sents if _QUESTIONY_RE.search(s)]
    picked: list[str] = []
    n = 0
    for s in sents[:4]:
        if n + len(s) > max_chars:
            break
        picked.append(s)
        n += len(s) + 1
        if n > max_chars * 0.6:
            break
    out = " ".join(picked)
    for a in asks[:1]:
        if a.lower() not in out.lower() and len(out) + len(a) < max_chars:
            out = (out + " " + a).strip()
    return out[:max_chars]


# ── reply drafting ───────────────────────────────────────────────────────────

_SIGNOFF = "Best regards,"


def _first_name_of(sender: str) -> str:
    display = parseaddr(sender)[0]
    if display:
        return display.split()[0].strip(",\"'")
    addr = parseaddr(sender)[1]
    return (addr or "").split("@", 1)[0].replace(".", " ").strip() or "there"


def draft_reply(msg: dict, user_name: str = "",
                intent: str = "acknowledge",
                notes: str = "") -> dict:
    """A reply skeleton the user edits, never an autonomous send.

    intent: acknowledge | interested | decline | question | meeting | thanks
    notes:  anything the user asked to include, quoted as-is (redacted
            upstream by the caller before persistence).
    Returns {subject, body, intent, to}.
    """
    to = parseaddr(msg.get("from") or "")[1]
    orig_subject = (msg.get("subject") or "").strip()
    reply_subject = orig_subject if orig_subject.lower().startswith(
        "re:") else f"Re: {orig_subject}"
    name = _first_name_of(msg.get("from") or "")

    openers = {
        "acknowledge": f"Hi {name},\n\nThanks for your email. I received "
                       f"it and will get back to you shortly.",
        "interested": f"Hi {name},\n\nThanks for reaching out — this sounds "
                      f"interesting. Could you share a bit more detail so "
                      f"I can review it properly?",
        "decline": f"Hi {name},\n\nThank you for thinking of me. I'm not "
                   f"able to take this on right now, but I appreciate the "
                   f"offer.",
        "question": f"Hi {name},\n\nThanks for your email. Quick question "
                    f"before I can answer fully:",
        "meeting": f"Hi {name},\n\nThanks for the invite. I'm checking my "
                   f"availability and will confirm a time shortly.",
        "thanks": f"Hi {name},\n\nThank you — much appreciated!",
    }
    body = openers.get(intent, openers["acknowledge"])
    if notes:
        body += "\n\n" + notes.strip()
    body += f"\n\n{_SIGNOFF}"
    if user_name:
        body += f"\n{user_name}"
    return {"to": to, "subject": reply_subject, "body": body,
            "intent": intent, "in_reply_to": msg.get("id", "")}


# ── inbox rendering ──────────────────────────────────────────────────────────

def render_inbox(rows: list[dict], header: str) -> str:
    if not rows:
        return f"{header}: no messages."
    lines = [f"{header}: {len(rows)} message(s), triaged."]
    for r in rows:
        flag = {"urgent": "‼️", "important": "❗"}.get(r["urgency_label"], "")
        tag = {"newsletter": "📰", "transactional": "🧾"}.get(r["category"], "")
        bits = [b for b in (flag, tag) if b]
        prefix = (" ".join(bits) + " ") if bits else ""
        why = ""
        if r["urgency_label"] in ("urgent", "important") and r["reasons"]:
            why = f" ({'; '.join(r['reasons'][:2])})"
        lines.append(f"{prefix}{r['idx']}. [{r['urgency_label']:9s}] "
                     f"From: {r['from']} — {r['subject']}{why}")
        if r.get("summary"):
            lines.append(f"      {r['summary']}")
    return "\n".join(lines)
