"""
orchestrator/agents/youtube/policy.py — the guardrails the pack stands on.

The whole point of JARVIS's YouTube agent is the discipline the upstream pack
("Lumen") enforces: the operator stays in charge, placeholders never ship, and
no metric, quote, statistic or source is ever invented. These rules are code,
not vibes — the pipeline calls them *before* it acts, and the tests assert on
them:

  * replaces nothing silently — a *simulated* asset may not enter the publish
    path (upstream: "Simulated output cannot enter the approval or publishing
    path"), and narration is required unless an intentional-silence override is
    explicitly recorded;
  * refuses to publish without explicit human approval and an explicit publish
    intent — the final decision is the human's;
  * refuses to publish a bundle whose fact ledger is ``blocked`` (an
    unsupported claim is not allowed to ship);
  * refuses media without a recorded right (owned / licensed / generated /
    public-domain / CC) — no downloading third-party footage we have no right
    to use;
  * enforces the operator's blocked-topic list during planning;
  * refuses requests to fabricate evidence — fake statistics, bought views or
    subscribers, invented reviews, false claims;
  * redacts anything that looks like a key or token from anything it reports.

Every function is pure and stdlib-only, so the checks are testable offline and
cannot be "worked around" by a model drafting the accompanying text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# A media asset is only usable when its license is one of these. "unknown" and
# "third_party" are deliberately absent: the default is "you may not use it".
ALLOWED_LICENSES = ("owned", "licensed", "generated", "public-domain",
                    "public_domain", "cc0", "cc-by", "cc_by", "royalty-free",
                    "royalty_free")

# Asks we will not carry out, whatever the model says. Matched case-insensitively
# against the user's request.
_REFUSED = (
    re.compile(r"\b(fake|fabricat\w*|invent|make\s*up|made\s*up|bogus)\b"
               r"[^.?!\n]{0,40}\b(stat\w*|data|number|metric|review|"
               r"quote|source|claim|evidence)\b", re.I),
    re.compile(r"\b(buy|purchase|bot)\b[^.?!\n]{0,30}\b(views?|subscribers?|"
               r"likes?|comments?|watch\s?time)\b", re.I),
    re.compile(r"\b(astroturf|sockpuppet|fake\s+news)\b", re.I),
    re.compile(r"\b(scrape|download)\b[^.?!\n]{0,40}\b(copyright|all rights|"
               r"someone else'?s|their)\b[^.?!\n]{0,20}\b(video|footage|"
               r"music|audio)\b", re.I),
)

PUBLISH_REASONS = (
    "At least one real MP4 must be produced — a placeholder/simulated render "
    "may not be published.",
    "Audio narration must be present, or intentional silence recorded.",
    "Every factual claim must be supported by a verified source or waived.",
    "A human must approve the upload before it happens.",
    "No publish intent was given, so nothing is uploaded.",
)


class PolicyViolation(Exception):
    """Raised when a request would step outside the pack's rules."""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    violations: list[str] = field(default_factory=list)


def assert_no_fabrication(text: str) -> Decision:
    """Would this request ask us to lie or fake engagement?"""
    low = text or ""
    for pattern in _REFUSED:
        m = pattern.search(low)
        if m:
            return Decision(
                False,
                "I won't fabricate evidence or engagement. I can plan real "
                "content, cite real sources, and report real metrics — but not "
                f"'{m.group(0)[:60]}'.",
                [m.group(0)])
    return Decision(True)


def blocked_topic_hit(topic: str, blocked: list[str] | None) -> str:
    """Return the operator's blocked term a topic touches, or ''."""
    low = (topic or "").lower()
    for term in (blocked or []):
        term = str(term or "").strip().lower()
        if term and term in low:
            return term
    return ""


def media_violations(assets: list[dict] | None) -> list[str]:
    """Assets with no recorded, acceptable right of use."""
    out: list[str] = []
    for asset in (assets or []):
        asset = asset or {}
        if asset.get("generated"):
            continue
        license_ = str(asset.get("license") or "").strip().lower()
        if license_ not in ALLOWED_LICENSES:
            out.append(
                f"{asset.get('path') or asset.get('kind') or 'asset'}: no "
                f"recorded right of use (license={license_ or 'unknown'})")
    return out


def narration_present(bundle: dict | None) -> bool:
    b = bundle or {}
    audio = b.get("audio") or b.get("assets", {}).get("audio") or {}
    if b.get("intentionalSilence") is True:
        return True
    if isinstance(audio, dict):
        return bool(audio.get("path")) and not audio.get("simulated")
    return bool(audio)


def real_video(bundle: dict | None) -> bool:
    """A real, decodable MP4 was produced (not a simulated placeholder)."""
    b = bundle or {}
    final = b.get("finalVideo") or b.get("assets", {}).get("finalVideo") or {}
    if not isinstance(final, dict):
        return False
    path = str(final.get("path") or "")
    return (bool(path) and path.lower().endswith(".mp4")
            and not final.get("simulated") and final.get("exists") is not False)


def assert_publishable(bundle: dict | None, provenance: dict | None, *,
                       approved: bool, publish: bool,
                       readiness: dict | None = None) -> Decision:
    """The single gate every publish passes through.

    ``approved`` is the human's yes; ``publish`` is the explicit intent to
    upload now. Both are required. Any failed check is a reason the decision is
    refused, so the caller can show the operator exactly what to fix.
    """
    reasons: list[str] = []
    b = bundle or {}

    if not publish:
        reasons.append(PUBLISH_REASONS[4])
    if not approved:
        reasons.append(PUBLISH_REASONS[3])
    if not real_video(b):
        reasons.append(PUBLISH_REASONS[0])
    if not narration_present(b):
        reasons.append(PUBLISH_REASONS[1])
    if (provenance or {}).get("status") == "blocked":
        reasons.append(PUBLISH_REASONS[2])
    if readiness is not None and readiness.get("status") == "failed":
        reasons.append("The production readiness gate failed: "
                       + ", ".join(readiness.get("blockingFailures") or []))

    violations = media_violations(b.get("assets") or b.get("scenes") or [])
    reasons.extend(violations)

    return Decision(not reasons,
                    "publishable" if not reasons else "; ".join(reasons),
                    reasons)


def readiness_blocked(readiness: dict | None) -> bool:
    return (readiness or {}).get("status") == "failed"


def redact(message: str) -> str:
    text = str(message or "")
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"AIza[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~-]+", "Bearer [redacted]", text,
                  flags=re.I)
    text = re.sub(r"(access_token|refresh_token|api[_-]?key|token)=([^\s&]+)",
                  r"\1=[redacted]", text, flags=re.I)
    return text


def publishing_banner() -> str:
    return ("Human-in-command: I research, write, produce, review and report — "
            "but I never upload without your explicit approval, never publish a "
            "placeholder or an unsupported claim, and never invent a metric. "
            "Each new learning stays a proposal until you approve it.")


def policy_banner() -> str:
    return ("Guardrails: no fabricated statistics, quotes, sources or "
            "engagement; no unlicensed media; blocked topics are respected; "
            "simulated output never ships.")
