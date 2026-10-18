"""
orchestrator/agents/youtube/tools/provenance.py — the fact-check ledger.

A Python port of the upstream pack's ``utils/provenance-service.js`` (MIT —
see ``ATTRIBUTION.md``): every externally verifiable claim a script makes is
recorded, and the ledger refuses to call a bundle "verified" until each claim
links to at least one *verified* source, or a reviewer explicitly waives it
with a note.

The publish gate reads ``status``:

    not_required  the script asserted no verifiable claims
    verified      every claim is supported by a verified source, or waived
    blocked       at least one claim is unresolved — publishing is refused

    from orchestrator.agents.youtube.tools import provenance as prov
    prov.build({"sources": [...], "claims": [...]})

Pure and stdlib-only. Nothing here can be satisfied by a model *saying* a
claim is fine — the source has to be marked verified, and a waiver has to
carry a human's note.
"""
from __future__ import annotations

import re
import uuid
from urllib.parse import urlparse

SOURCE_TYPES = ("article", "video", "dataset", "official", "asset", "other")
SOURCE_STATUSES = ("pending", "verified", "rejected")
CLAIM_RISKS = ("standard", "high")
CLAIM_STATUSES = ("pending", "supported", "unsupported", "waived")

_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")


class Invalid(RuntimeError):
    """A 400-style validation error (mirrors the upstream error.status=400)."""
    status = 400


def _id(value: object, prefix: str) -> str:
    text = str(value or "").strip()
    return text if _ID_RE.match(text) else f"{prefix}_{uuid.uuid4().hex}"


def _valid_url(value: object) -> str:
    try:
        url = urlparse(str(value or "").strip())
        return str(value).strip() if url.scheme in ("http", "https") else ""
    except Exception:
        return ""


def _text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _date(value: object) -> str | None:
    if not value:
        return None
    import datetime as _dt
    try:
        dt = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc).isoformat()


def normalize_sources(items: object) -> list[dict]:
    if not isinstance(items, (list, tuple)):
        raise Invalid("Sources must be a list")
    seen: set[str] = set()
    out: list[dict] = []
    for i, item in enumerate(list(items)[:50]):
        item = item if isinstance(item, dict) else {}
        url = _valid_url(item.get("url"))
        if not url:
            raise Invalid(f"Source {i + 1} requires a valid http or https URL")
        if url in seen:
            raise Invalid(f"Duplicate source URL: {url}")
        seen.add(url)
        st = str(item.get("status") or "pending").lower()
        out.append({
            "id": _id(item.get("id"), "source"),
            "url": url,
            "title": _text(item.get("title"), 300) or url,
            "publisher": _text(item.get("publisher"), 200),
            "publishedAt": _date(item.get("publishedAt")),
            "accessedAt": _date(item.get("accessedAt")) or _now(),
            "sourceType": (str(item.get("sourceType") or "other").lower()
                           if str(item.get("sourceType") or "other").lower()
                           in SOURCE_TYPES else "other"),
            "status": st if st in SOURCE_STATUSES else "pending",
            "notes": _text(item.get("notes"), 1000),
        })
    return out


def normalize_claims(items: object, sources: list[dict],
                     source_id_by_url: dict | None = None) -> list[dict]:
    if not isinstance(items, (list, tuple)):
        raise Invalid("Claims must be a list")
    source_id_by_url = source_id_by_url or {}
    valid_ids = {s["id"] for s in sources}
    out: list[dict] = []
    for i, item in enumerate(list(items)[:100]):
        item = item if isinstance(item, dict) else {}
        text = _text(item.get("text") or item.get("claim"), 1000)
        if not text:
            raise Invalid(f"Claim {i + 1} requires text")
        mapped = []
        if isinstance(item.get("sourceUrls"), (list, tuple)):
            mapped = [source_id_by_url.get(_valid_url(u))
                      for u in item["sourceUrls"]]
            mapped = [m for m in mapped if m]
        raw_ids = item.get("sourceIds") if isinstance(item.get("sourceIds"),
                                                      (list, tuple)) else []
        ids = {str(v) for v in list(raw_ids) + mapped if str(v) in valid_ids}
        risk = str(item.get("riskLevel") or "standard").lower()
        status = str(item.get("status") or "pending").lower()
        out.append({
            "id": _id(item.get("id"), "claim"),
            "text": text,
            "riskLevel": risk if risk in CLAIM_RISKS else "standard",
            "sourceIds": sorted(ids),
            "status": status if status in CLAIM_STATUSES else "pending",
            "notes": _text(item.get("notes"), 1000),
        })
    return out


def build(input_: dict | None = None) -> dict:
    """Compute the ledger + its status. Raises ``Invalid`` on bad input."""
    inp = input_ or {}
    sources = normalize_sources(inp.get("sources") or [])
    by_url = {s["url"]: s["id"] for s in sources}
    claims = normalize_claims(inp.get("claims") or [], sources, by_url)

    verified_ids = {s["id"] for s in sources if s["status"] == "verified"}
    for claim in claims:
        claim["sourceIds"] = [i for i in claim["sourceIds"]
                             if i in {s["id"] for s in sources}]
        if claim["status"] == "supported" and not (set(claim["sourceIds"])
                                                   & verified_ids):
            raise Invalid("A supported claim must link to at least one "
                          "verified source")
        if claim["status"] == "waived" and not claim["notes"]:
            raise Invalid("A waived claim requires a reviewer note")

    unresolved = [c for c in claims
                  if c["status"] not in ("supported", "waived")]
    status = "not_required" if not claims else (
        "verified" if not unresolved else "blocked")

    return {
        "sources": sources,
        "claims": claims,
        "containsSyntheticMedia": inp.get("containsSyntheticMedia") is True,
        "status": status,
        "reviewedAt": _now() if status == "verified" else None,
        "summary": {
            "sourceCount": len(sources),
            "verifiedSources": sum(1 for s in sources
                                   if s["status"] == "verified"),
            "claimCount": len(claims),
            "resolvedClaims": len(claims) - len(unresolved),
            "highRiskClaims": sum(1 for c in claims
                                  if c["riskLevel"] == "high"),
            "unresolvedClaims": len(unresolved),
        },
    }


def needs_review(provenance: dict | None) -> bool:
    """True when the publish gate must block on unresolved claims."""
    return (provenance or {}).get("status") == "blocked"


def render(provenance: dict | None) -> str:
    p = provenance or {}
    s = p.get("summary") or {}
    out = ["# Provenance — every claim, and its evidence", "",
           f"**Status: {(p.get('status') or 'not_required').upper()}**", "",
           f"- sources: {s.get('sourceCount', 0)} "
           f"({s.get('verifiedSources', 0)} verified)",
           f"- claims: {s.get('claimCount', 0)} "
           f"({s.get('resolvedClaims', 0)} resolved, "
           f"{s.get('unresolvedClaims', 0)} open)",
           f"- contains synthetic media: "
           f"{'yes' if p.get('containsSyntheticMedia') else 'no'}", ""]
    if p.get("sources"):
        out += ["## Sources", "", "| # | title | status | URL |",
                "| --- | --- | --- | --- |"]
        for i, src in enumerate(p["sources"], 1):
            out.append(f"| {i} | {src['title']} | {src['status']} | "
                       f"{src['url']} |")
        out.append("")
    if p.get("claims"):
        out += ["## Claims", "", "| claim | risk | status | sources |",
                "| --- | --- | --- | --- |"]
        for c in p["claims"]:
            out.append(f"| {c['text']} | {c['riskLevel']} | {c['status']} | "
                       f"{', '.join(c['sourceIds']) or '—'} |")
        out.append("")
    return "\n".join(out)


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
