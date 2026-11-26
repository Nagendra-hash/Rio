"""
core/browser_agent/router.py — decide *how* to answer a web ask.

The brief is explicit: RIO must not launch a browser for every internet
request. This module turns one task string into a plan, before any browser
process exists:

    "tool"     a known structured API or the existing search tool answers it
    "http"     one public page, static content -> requests + BeautifulSoup
    "browser"  the page needs a browser (JS, login, clicks, forms, downloads)

The plan is a *recommendation* with a reason, returned to the caller and
recorded in the run log, so a decision is never invisible. When the static
fetch returns an unusable shell (below `http_min_chars`), the caller falls
back to the browser instead of reporting an empty page as the answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
_BARE_DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9-]+\.)+(?:com|org|net|io|ai|dev|co|in|gov|edu|app|xyz|me|tv))\b",
    re.IGNORECASE,
)

_INTERACTION_RE = re.compile(
    r"\b(?:click|double[\s-]?click|fill(?:\s+in)?|type\s+(?:in|into)|select|dropdown|"
    r"scroll|drag|hover|upload|download|log\s?in|sign\s?in|sign\s?up|register|"
    r"create\s+an?\s+account|add\s+to\s+(?:cart|basket)|check\s?out|submit|"
    r"apply|book|reserve|pay|purchase|buy|post|publish|comment|send|reply|"
    r"open\s+(?:a\s+)?(?:new\s+)?tabs?|switch\s+tabs?|fill|form)\b",
    re.IGNORECASE,
)

_READ_RE = re.compile(
    r"\b(?:read|extract|what\s+does\s+(?:it|the\s+page)\s+say|summar(?:y|ize|ise)|"
    r"headline|heading|title|price|table|list\s+(?:the|all)|get\s+the|find\s+the|"
    r"report|show\s+me|fetch|scrape)\b",
    re.IGNORECASE,
)

_MULTISTEP_RE = re.compile(
    r"\b(?:and\s+then|then|after\s+that|afterwards|first\b.*\bthen|"
    r"compare\b.*\b(?:sites?|websites?|pages?|stores?)|across\s+\d+\s+(?:sites?|"
    r"websites?|pages?)|multi[\s-]?page|follow\s+the\s+link)\b",
    re.IGNORECASE,
)

# "open YouTube and search ..." / "open my dashboard, then check ..." — the
# user asked for a site AND an action on it, which is a browser job even when
# a plain "search" would otherwise be routed to the search tool.
_OPEN_THEN_RE = re.compile(
    r"\bopen\b[^.!?\n]{0,60}\b(?:and|then|,)\b", re.IGNORECASE
)

_MULTI_SITE_RE = re.compile(
    r"\b(?:two|three|four|five|several|\d+)\s+(?:websites?|sites?|pages?|sources?|stores?)\b"
    r"|\bcompare\b[^.!?\n]{0,40}\b(?:information|prices?|results?|offerings?|features?)\b",
    re.IGNORECASE,
)

# A named destination means the user wants that site driven, not a generic
# web search: "search python tutorials on youtube".
_NAMED_SITE_RE = re.compile(
    r"\b(?:search|look\s+up|find|browse)\b[^.!?\n]{0,50}\b(?:on|in)\s+"
    r"(?:youtube|google|amazon|flipkart|github|gitlab|reddit|linkedin|twitter|x|"
    r"facebook|instagram|stack\s?overflow|hacker\s?news|gmail|outlook|notion|"
    r"jira|slack|drive|maps?)\b",
    re.IGNORECASE,
)

_AUTH_RE = re.compile(
    r"\b(?:my\s+(?:gmail|inbox|mail|github|gitlab|dashboard|account|profile|"
    r"orders?|portfolio|bank|broker|drive|notion|jira|slack)|"
    r"logged[\s-]?in|authenticated|signed[\s-]?in|behind\s+(?:a\s+)?login)\b",
    re.IGNORECASE,
)

_RESEARCH_RE = re.compile(
    r"\b(?:research|investigate|look\s+into|gather|compile|deep\s+dive|"
    r"prepare\s+(?:a\s+)?report)\b",
    re.IGNORECASE,
)

_API_HINTS: tuple[tuple[str, re.Pattern], ...] = (
    ("binance", re.compile(r"\b(?:binance|btc|eth|sol|paxg|funding\s+rate)\b", re.I)),
    ("delta", re.compile(r"\bdelta\s+exchange\b", re.I)),
    ("weather", re.compile(r"\bweather\b", re.I)),
    ("news_api", re.compile(r"\b(?:latest\s+news|news\s+headlines)\b", re.I)),
)

_GREETING_RE = re.compile(r"^(?:hi|hello|hey|thanks|ok(?:ay)?)\b[\s!.?]*$", re.I)


@dataclass(frozen=True)
class Plan:
    kind: str  # "tool" | "http" | "browser"
    reason: str
    url: Optional[str] = None
    fallbacks: tuple[str, ...] = field(default_factory=lambda: ("browser",))

    def public(self) -> dict:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "url": self.url or "",
            "fallbacks": list(self.fallbacks),
        }


def extract_url(text: str) -> Optional[str]:
    """First http(s) URL, else a bare domain promoted to https://."""
    match = _URL_RE.search(text or "")
    if match:
        return match.group(0).rstrip(".,;:!?")
    bare = _BARE_DOMAIN_RE.search(text or "")
    if bare:
        return "https://" + bare.group(1)
    return None


def plan_task(task: str, mode: str = "auto", cfg=None) -> Plan:
    """The routing table from the plan, as code.

    mode: auto (apply the table) | extract (prefer static) |
          interact (force browser) | research (force browser, multi-site)
    """
    text = str(task or "").strip()
    mode = (mode or "auto").strip().lower()
    url = extract_url(text)

    if not text or _GREETING_RE.match(text):
        return Plan("tool", "not a web task", url)

    if mode in ("interact", "research", "browser"):
        return Plan("browser", f"{mode} mode requested a real browser", url)

    if mode == "extract":
        if url:
            return Plan("http", "extract mode: single public page", url)
        return Plan("browser", "extract mode without a direct URL", url)

    if _INTERACTION_RE.search(text):
        return Plan("browser", "the task needs clicks, forms, login or a download", url)

    if _MULTI_SITE_RE.search(text):
        return Plan("browser", "the task compares several sites in one session", url)

    if _NAMED_SITE_RE.search(text):
        return Plan("browser", "the user named a site to drive, not a generic search", url)

    if _OPEN_THEN_RE.search(text):
        return Plan("browser", "the task opens a site and acts on it", url)

    if _MULTISTEP_RE.search(text):
        return Plan("browser", "the task spans several steps or sites", url)

    if _AUTH_RE.search(text):
        return Plan("browser", "the task targets the user's authenticated session", url)

    if _RESEARCH_RE.search(text):
        return Plan("browser", "research needs several pages read in one session", url)

    for name, pattern in _API_HINTS:
        if pattern.search(text) and not url:
            return Plan("tool", f"structured data is available from the {name} integration", url)

    if url and _READ_RE.search(text) and cfg is not None and getattr(cfg, "http_first", True):
        return Plan("http", "one public page, static read — no browser needed", url)

    if url and not _READ_RE.search(text):
        # "open example.com" — the model should hand this to browser_control,
        # which opens the user's own browser instead of an agent loop.
        return Plan("tool", "a plain URL open belongs to browser_control", url)

    if url:
        return Plan("http", "one page, static read", url)

    return Plan("tool", "no site to drive; the search tool answers this", url)


# ── static fetch (the cheap path) ───────────────────────────────────────────

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_NOISE_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "nav", "footer")


@dataclass(frozen=True)
class StaticPage:
    url: str
    title: str
    text: str
    status: int
    error: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.text.strip()) and not self.error


def fetch_static(url: str, timeout: float = 15.0, max_chars: int = 12000) -> StaticPage:
    """Fetch and flatten one public page. Never raises; errors are values."""
    try:
        import requests
    except Exception as exc:  # pragma: no cover - requests is a core dependency
        return StaticPage(url, "", "", 0, f"requests unavailable: {type(exc).__name__}")

    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
        )
    except Exception as exc:
        return StaticPage(url, "", "", 0, f"{type(exc).__name__}: {str(exc)[:160]}")

    content_type = (resp.headers.get("content-type") or "").lower()
    if "html" not in content_type and "text/" not in content_type:
        return StaticPage(url, "", "", resp.status_code, f"not html ({content_type or 'unknown'})")
    if resp.status_code >= 400:
        return StaticPage(url, "", "", resp.status_code, f"HTTP {resp.status_code}")

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(list(_NOISE_TAGS)):
            tag.decompose()
        title = (soup.title.get_text(" ", strip=True) if soup.title else "").strip()
        text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
        return StaticPage(url, title, text[:max_chars], resp.status_code)
    except Exception as exc:
        return StaticPage(url, "", "", resp.status_code, f"parse failed: {type(exc).__name__}")
