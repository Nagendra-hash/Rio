"""
orchestrator/agents/research_tools.py — permitted-source toolbox for the
research agent.

WHY A SEPARATE TOOLBOX (same reasoning as coding_tools.py)
    The research agent reads from the outside world. A model can propose a
    `file://` URL, an internal 192.168.x.x host, a credential-carrying link,
    or echo a leaked API key it "found" on a page. So every source access
    goes through SourcePermissions + the URL policy FIRST, and every piece
    of text a source produces (search snippets, fetched pages, KB chunks)
    is masked for secrets before it leaves this file. The model never
    touches the network itself — it only proposes queries, which this
    toolbox executes under the rules below.

PERMITTED SOURCES (and only these)
    WEB SEARCH — the existing house backend (actions/web_search's DDG text
                 search: no keys, no cookies, cached, rate-limit aware).
    WEB FETCH  — bounded plain-text GET of one http(s) page, opt-in
                 (metadata.deep_fetch). Text-like content types only,
                 size-capped, never sends credentials or cookies.
    LOCAL KB   — core/rag_service.search as caller "research_agent": the
                 same ACL/audit envelope the rag_agent uses. A blind caller
                 (no grant in config/rag_callers.json) degrades to "no
                 local docs", never to an error.

SOURCE POLICY (structural, not behavioural)
    * schemes: http/https only — file://, ftp://, data: cannot run
    * URLs carrying userinfo (user:pass@host) cannot run
    * hosts that are literal or resolve to loopback/private/link-local
      ranges cannot run (SSRF guard)
    * every output is secret-masked and size-capped

Everything here is offline-testable: the search and fetch backends are
injectable, so the agent's whole workflow runs with scripted sources and
no network at all.
"""
from __future__ import annotations

import html as _html
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from orchestrator.agents.coding_tools import mask_secrets

# ── tunables ─────────────────────────────────────────────────────────────────
MAX_RESULTS_PER_QUERY = 5      # per sub-query, per backend
MAX_SOURCES        = 24        # total source records per task
MAX_FETCH_BYTES    = 200_000   # raw download cap before text extraction
MAX_FETCH_CHARS    = 8_000     # extracted page text cap handed to the agent
FETCH_TIMEOUT_S    = 10.0
SNIPPET_CAP        = 600       # per evidence snippet
FETCH_LIMIT        = 2         # deep-fetch at most this many pages per task

USER_AGENT = ("JarvisResearchAgent/1.0 (personal assistant; research tool; "
              "no cookies, no credentials)")

ACCEPT_TYPES = "text/html,text/plain,application/json,text/xml;q=0.9"

BLOCKED_HOSTS = {"localhost", "metadata.google.internal",
                 "metadata.goog"}
BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")

PRIVATE_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "0.0.0.0/8", "100.64.0.0/10", "::1/128",
    "fc00::/7", "fe80::/10",
)]


class SourceUnavailable(RuntimeError):
    """A permitted source could not be reached. The agent records it and
    degrades honestly; it never invents evidence to cover the gap."""


# ── secrets ──────────────────────────────────────────────────────────────────

def mask(text: str) -> str:
    """Mask credential shapes; single-value convenience over coding_tools."""
    return mask_secrets(text)[0]


# ── the URL policy ───────────────────────────────────────────────────────────

def url_allowed(url: str) -> tuple[bool, str]:
    """Structural source policy. Returns (allowed, reason).

    Deliberately paranoid: an unresolvable host is refused too — the agent
    reports it rather than silently guessing.
    """
    try:
        parts = urlparse(str(url or "").strip())
    except Exception:
        return False, "unparseable URL"
    if parts.scheme not in ("http", "https"):
        return False, f"scheme '{parts.scheme or 'none'}' not permitted"
    host = (parts.hostname or "").lower()
    if not host:
        return False, "no host"
    if "@" in (parts.netloc or ""):
        return False, "URLs carrying credentials are not permitted"
    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_HOST_SUFFIXES):
        return False, "blocked host"
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False, "host does not resolve"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if any(ip in net for net in PRIVATE_NETS):
            return False, f"private/loopback address ({ip})"
    return True, ""


# ── backends (injectable; defaults are the house ones) ───────────────────────

def default_search(query: str, max_results: int) -> list[dict]:
    """House web search: actions/web_search's DDG text backend (no key,
    cached, rate-limit aware). Raises SourceUnavailable when unreachable —
    the agent degrades honestly instead of filling the gap."""
    try:
        from actions.web_search import _ddg_search
    except Exception as exc:
        raise SourceUnavailable(f"web search backend unavailable: {exc}") \
            from exc
    return _ddg_search(query, max_results=max_results)


def default_fetch_fn(url: str) -> str:
    """Bounded plain-text GET. One page, no cookies, no credentials,
    text-like content types only, hard size cap."""
    import requests

    resp = requests.get(
        url, timeout=FETCH_TIMEOUT_S, stream=True,
        headers={"User-Agent": USER_AGENT, "Accept": ACCEPT_TYPES},
    )
    if resp.status_code >= 400:
        raise SourceUnavailable(f"HTTP {resp.status_code}")
    ctype = (resp.headers.get("content-type") or "").lower()
    if not any(t in ctype for t in
               ("text/html", "text/plain", "application/json", "text/xml")):
        raise SourceUnavailable(
            f"content-type '{ctype.split(';')[0] or 'unknown'}' is not text")
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=2048, decode_unicode=False):
        total += len(chunk)
        if total > MAX_FETCH_BYTES:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def strip_html(raw: str) -> str:
    """Rough text extraction: drop script/style/comments, tags → space,
    unescape entities, collapse whitespace. Bounded and dependency-free."""
    text = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1\s*>",
                  " ", raw or "")
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


# ── permissions ──────────────────────────────────────────────────────────────

@dataclass
class SourcePermissions:
    """What this research run may touch. Web/KB tiers can each be switched
    off per task via metadata (allow_web / allow_local_kb)."""
    allow_web: bool = True
    allow_local_kb: bool = True
    max_results_per_query: int = MAX_RESULTS_PER_QUERY
    max_sources: int = MAX_SOURCES


# ── the toolbox ──────────────────────────────────────────────────────────────

class SourceToolbox:
    """Every source the research agent touches goes through here.

    Backends are injectable for tests/offline demos:
      search_fn  (query, max_results) -> list[{title, snippet, url}]
      fetch_fn   (url) -> str                    (raw page body)
      kb_search  (query, top_k, collection) -> rag_service.search-shaped dict
    """

    def __init__(self,
                 permissions: SourcePermissions | None = None,
                 search_fn: Callable[[str, int], list[dict]] | None = None,
                 fetch_fn: Callable[[str], str] | None = None,
                 kb_search: Callable[..., dict] | None = None) -> None:
        self.permissions = permissions or SourcePermissions()
        # Public injectable backends (tests/offline demos assign these).
        self.search_fn = search_fn
        self.fetch_fn = fetch_fn
        self.kb_search = kb_search

    # ── web search ───────────────────────────────────────────────────────
    def web_search(self, query: str,
                   max_results: int | None = None) -> list[dict]:
        """Permitted-source web search. Blocked URLs are dropped (not
        errored) — a poisoned result list must not leak a private host into
        the report. Backend failure raises SourceUnavailable."""
        if not self.permissions.allow_web:
            raise SourceUnavailable("web tier disabled for this task")
        k = int(max_results or self.permissions.max_results_per_query)
        backend = self.search_fn or default_search
        try:
            raw = backend(query, k)
        except SourceUnavailable:
            raise
        except Exception as exc:
            raise SourceUnavailable(f"web search failed: {exc}") from exc
        out: list[dict] = []
        for r in (raw or [])[:k]:
            if not isinstance(r, dict):
                continue
            url = str(r.get("url") or "").strip()
            if url:
                ok, _why = url_allowed(url)
                if not ok:
                    continue
            text = mask(str(r.get("snippet") or r.get("body") or "")) \
                .strip()[:SNIPPET_CAP]
            title = mask(str(r.get("title") or "")).strip()[:200]
            if not text and not title:
                continue
            out.append({"title": title, "snippet": text, "url": url})
        return out

    # ── local knowledge base (the RAG integration) ───────────────────────
    def local_kb(self, query: str, top_k: int = 3,
                 collection: str | None = None) -> dict:
        """rag_service.search as caller 'research_agent' — same ACL, audit
        trail and citation shape the rag_agent uses. A blind caller degrades
        to zero results; never an error."""
        if not self.permissions.allow_local_kb:
            return {"ok": True, "results": [], "citations": [],
                    "note": "local KB tier disabled for this task"}
        if self.kb_search is not None:
            try:
                return self.kb_search(query, top_k, collection)
            except Exception as exc:
                return {"ok": False, "error": f"local KB failed: {exc}"}
        try:
            from core import rag_service
            return rag_service.search(caller="research_agent", query=query,
                                      top_k=max(1, min(int(top_k), 10)),
                                      collection=collection)
        except Exception as exc:
            return {"ok": False, "error": f"local KB failed: {exc}"}

    # ── page fetch (opt-in deepening) ────────────────────────────────────
    def fetch(self, url: str) -> dict:
        """Fetch one permitted page as bounded text. Off-policy URLs are
        refused before any network happens."""
        ok, why = url_allowed(url)
        if not ok:
            return {"ok": False, "error": why}
        try:
            raw = (self.fetch_fn or default_fetch_fn)(url)
        except Exception as exc:
            return {"ok": False, "error": f"fetch failed: "
                                          f"{type(exc).__name__}: {exc}"[:200]}
        text = mask(strip_html(raw))
        return {"ok": True, "url": url, "text": text[:MAX_FETCH_CHARS]}
