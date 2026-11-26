"""
core/browser_agent/policy.py — what the browser agent may do without asking.

ONE decision point, shared by the voice/dashboard action and the orchestrator
agent, so the two cannot drift apart. Three verdicts:

    ALLOW           run it
    NEEDS_APPROVAL  park it behind a human confirmation (HUD or the chat the
                    request came from); never execute inline
    DENY            refuse even with confirmation — these are the rules that
                    are not the user's to waive on the agent's behalf

DENY is deliberately narrow and about *method*, not about intent: getting past
a CAPTCHA/MFA challenge, harvesting cookies/passwords, impersonating another
account, or following instructions embedded in a page. Disabling 2FA in one's
own account is a *settings change* — consequential, so it needs approval, but
it is the user's to authorise. Nobody can approve the agent defeating a live
security check.

The classifier is regex-based and deterministic — the same wording always lands
in the same bucket, which is what makes it testable. It is a guardrail, not a
sandbox: the runner also hands browser-use hard rules and reports its judge's
injection/CAPTCHA flags.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Verdict(str, Enum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str = ""
    category: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def public(self) -> dict:
        return {"verdict": self.verdict.value, "reason": self.reason, "category": self.category}


# ── DENY: method-level rules, never waivable ────────────────────────────────

_DENY_RULES: tuple[tuple[str, re.Pattern], ...] = (
    (
        "security_bypass",
        re.compile(
            r"\b(?:bypass|circumvent|defeat|get\s+around|get\s+past|evade)"
            r"(?:s|es|ed|ing)?\b"
            r"[^.!?\n]{0,40}\b(?:captcha|mfa|2fa|two[\s-]?factor|security\s+check|"
            r"bot\s+(?:check|detection|protection)|login\s+wall|otp|one[\s-]?time\s+code)\b"
            r"|\bsolve(?:s|d)?\s+(?:the\s+)?(?:captcha|mfa|2fa|two[\s-]?factor|"
            r"bot\s+check)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "credential_harvest",
        re.compile(
            r"\b(?:export|dump|extract|steal|copy|read|list|print|reveal)\b"
            r"[^.!?\n]{0,40}\b(?:cookies?|passwords?|session\s+tokens?|auth\s+tokens?|"
            r"credentials?(?:\s+store)?|local\s?storage|browser\s+profile)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "instructions_hijack",
        re.compile(
            r"\b(?:ignore|disregard|override|forget)\b[^.!?\n]{0,40}"
            r"\b(?:system\s+(?:prompt|instructions?)|your\s+(?:rules|instructions)|"
            r"rio'?s\s+(?:rules|instructions)|safety\s+rules)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "impersonation",
        re.compile(
            r"\b(?:log\s?in|sign\s?in)\b[^.!?\n]{0,30}\bas\s+(?:someone|somebody|"
            r"another\s+(?:user|person|account)|the\s+admin|an?\s+admin)\b",
            re.IGNORECASE,
        ),
    ),
)


# ── NEEDS_APPROVAL: consequential, but the user can authorise it ────────────

_APPROVAL_RULES: tuple[tuple[str, re.Pattern], ...] = (
    (
        "send_message",
        re.compile(
            r"\b(?:send|reply|forward|compose|write|draft\s+and\s+send)\b"
            r"[^.!?\n]{0,40}\b(?:e-?mail|mail|message|dm|sms|text|whatsapp|"
            r"telegram|invite|newsletter)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "publish",
        re.compile(
            r"\b(?:publish|post|tweet|repost|share|upload|comment\s+on|"
            r"submit\s+(?:a\s+)?review)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "purchase",
        re.compile(
            r"\b(?:buy|purchase|checkout|check\s+out|place\s+(?:an?\s+)?order|"
            r"order\s+(?:the|a|an|some)|add\s+to\s+(?:cart|basket)|pay\s+for|"
            r"subscribe|book\s+(?:a|the|an)?\s*(?:flight|hotel|ticket|table|"
            r"appointment|test|ride)|reserve)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "money_movement",
        re.compile(
            r"\b(?:transfer|wire|withdraw|deposit|remit|top\s?up|send\s+(?:money|funds|"
            r"crypto|btc|eth|usdt)|place\s+(?:a\s+)?trade|execute\s+(?:a\s+)?trade|"
            r"trade|buy\s+(?:btc|eth|sol|crypto|bitcoin|stock|shares?)|"
            r"sell\s+(?:btc|eth|sol|crypto|bitcoin|stock|shares?))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "destructive",
        re.compile(
            r"\b(?:delete|remove|erase|deactivate|close|terminate)\b"
            r"[^.!?\n]{0,40}\b(?:account|profile|repository|repo|post|tweet|"
            r"message|conversation|data|files?|folder|workspace|project)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "security_settings",
        re.compile(
            r"\b(?:change|update|reset|disable|enable|turn\s+(?:on|off))\b"
            r"[^.!?\n]{0,40}\b(?:password|2fa|two[\s-]?factor|mfa|recovery\s+"
            r"(?:email|phone|code)|security\s+(?:settings?|question)|phone\s+number|"
            r"backup\s+codes?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "sensitive_submission",
        re.compile(
            r"\b(?:submit|file|apply\s+for|lodge)\b[^.!?\n]{0,50}\b(?:application|"
            r"visa|passport|loan|mortgage|insurance|tax\s+return|claim|"
            r"government\s+form|consent\s+form)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "consent",
        re.compile(
            r"\b(?:accept|agree\s+to|sign)\b[^.!?\n]{0,30}\b(?:terms|conditions|"
            r"agreement|contract|consent|policy)\b",
            re.IGNORECASE,
        ),
    ),
)


def classify(task: str) -> Decision:
    """Read-only decision for one browser task. Deterministic, offline."""
    text = str(task or "")
    if not text.strip():
        return Decision(Verdict.DENY, "the task is empty", "empty")

    for category, pattern in _DENY_RULES:
        if pattern.search(text):
            return Decision(
                Verdict.DENY,
                "this asks the browser agent to bypass a security control or "
                "expose secrets, which it never does",
                category,
            )

    for category, pattern in _APPROVAL_RULES:
        if pattern.search(text):
            return Decision(
                Verdict.NEEDS_APPROVAL,
                "this action has an external or irreversible effect and needs "
                "your explicit confirmation",
                category,
            )

    return Decision(Verdict.ALLOW, "", "")


# ── domain policy ───────────────────────────────────────────────────────────


def domain_policy(cfg, extra_allowed=()) -> dict:
    """browser-use `Browser(...)` domain arguments.

    Empty lists mean "unrestricted". Localhost is never blocked here: the
    dashboard/AlgoTrade checks in the brief depend on it, and the local
    Chromium is the only browser that can reach it.
    """
    allowed = list(dict.fromkeys((*getattr(cfg, "allowed_domains", ()), *tuple(extra_allowed))))
    prohibited = list(getattr(cfg, "prohibited_domains", ()))
    return {"allowed_domains": allowed or None, "prohibited_domains": prohibited or None}
