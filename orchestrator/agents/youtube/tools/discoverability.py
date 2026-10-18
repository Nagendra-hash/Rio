"""
orchestrator/agents/youtube/tools/discoverability.py — the SEO/GEO audit.

Adapted from the upstream pack's ``utils/discoverability-service.js`` (MIT —
see ``ATTRIBUTION.md``). Upstream shells out to the optional "DarkzSEO" Python
engine; that engine is not part of this pack, so this module keeps the same
*contract* (normalised findings, a stable fingerprint, and the reviewer
accept/dismiss rules where a dismissal needs a real reason) and supplies a
small, deterministic, offline rule engine behind it.

Findings are ADVISORY: like upstream's Review Studio, they never rewrite the
content and never block publication on their own.

    from orchestrator.agents.youtube.tools import discoverability
    report = discoverability.audit(package)          # package = contentPackage()

Pure and stdlib-only.
"""
from __future__ import annotations

import hashlib
import re

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
CATEGORIES = ("SEO", "GEO", "AIO", "AEO")
MIN_DISMISS_REASON = 5

_POWER_WORDS = ("how", "why", "best", "guide", "ultimate", "complete", "easy",
                "fast", "secret", "proven", "step")


def content_package(production: dict | None, profile: dict | None = None,
                    platform: str = "youtube") -> dict:
    """The upstream contentPackage() shape, from our production bundle."""
    production = production or {}
    profile = profile or {}
    script = production.get("script") or {}
    seo = production.get("seo") or {}
    provenance = production.get("provenance") or {}
    sections = script.get("sections")
    if not isinstance(sections, list):
        sections = (script.get("mainContent") or {}).get("sections") or []
    return {
        "id": production.get("id") or "",
        "platform": platform,
        "brand": profile.get("channel_name") or profile.get("channelName")
                 or "Brand",
        "title": seo.get("title") or script.get("title") or "",
        "description": seo.get("description") or "",
        "transcript": script.get("fullScript") or production.get("full_script") or "",
        "sections": [{"title": s.get("title", ""),
                      "content": s.get("content", "")} for s in sections
                     if isinstance(s, dict)],
        "chapters": seo.get("chapters") if isinstance(seo.get("chapters"), list)
                    else [],
        "sources": [{"url": s.get("url"), "title": s.get("title"),
                     "publisher": s.get("publisher")}
                    for s in (provenance.get("sources") or [])
                    if s.get("status") == "verified"],
        "hasComparisonTable": bool(script.get("hasComparisonTable")),
        "unitsExplained": bool(script.get("unitsExplained")),
    }


def fingerprint(platform: str, finding: dict) -> str:
    key = "\u0000".join([platform, str(finding.get("ruleId")),
                         str(finding.get("message"))])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def normalize_report(report: dict | None, platform: str) -> dict:
    report = report or {}
    findings = []
    for f in (report.get("findings") or []):
        f = f if isinstance(f, dict) else {}
        category = str(f.get("category") or "SEO").upper()
        severity = str(f.get("severity") or "INFO").upper()
        finding = {
            "ruleId": str(f.get("ruleId") or "unknown"),
            "category": category if category in CATEGORIES else "SEO",
            "severity": severity if severity in SEVERITIES else "INFO",
            "applicability": f.get("applicability") if isinstance(
                f.get("applicability"), list) else [platform, "content"],
            "message": str(f.get("message") or "Discoverability review required"),
            "remediation": str(f["remediation"]) if f.get("remediation") else None,
        }
        finding["fingerprint"] = fingerprint(platform, finding)
        findings.append(finding)
    summary_sev = {s: sum(1 for f in findings if f["severity"] == s)
                   for s in SEVERITIES}
    summary_cat = {c: sum(1 for f in findings if f["category"] == c)
                   for c in CATEGORIES}
    return {
        "schemaVersion": "1.0",
        "engine": {"name": "jarvis-local-seo", "version": "1.0"},
        "mode": "content",
        "status": report.get("status") or "completed",
        "summary": {"severity": summary_sev, "category": summary_cat},
        "findings": findings,
    }


def unavailable_report(error: str) -> dict:
    return {
        "schemaVersion": "1.0",
        "engine": {"name": "jarvis-local-seo", "version": None},
        "mode": "content",
        "status": "unavailable",
        "summary": {"severity": {s: 0 for s in SEVERITIES},
                    "category": {c: 0 for c in CATEGORIES}},
        "findings": [],
        "errorCode": "DISCOVERABILITY_UNAVAILABLE",
        "error": str(error),
    }


def audit(package: dict | None, platform: str = "youtube") -> dict:
    """Run the local rule engine over a content package."""
    pkg = package or {}
    try:
        findings = _rules(pkg)
    except Exception as exc:                       # never raise to the caller
        return unavailable_report(f"{type(exc).__name__}: {exc}")
    return normalize_report({"findings": findings, "status": "completed"},
                            platform)


def review_finding(status: str, reason: str = "") -> dict:
    """Accept/dismiss a finding; a dismissal needs a real reason (>= 5 chars)."""
    status = str(status or "").lower()
    if status not in ("accepted", "dismissed"):
        raise ValueError("Choose accepted or dismissed")
    reason = str(reason or "").strip()
    if status == "dismissed" and len(reason) < MIN_DISMISS_REASON:
        raise ValueError("Dismissed findings require a reason of at least "
                         f"{MIN_DISMISS_REASON} characters")
    return {"status": status, "reason": reason or None}


def render(report: dict | None) -> str:
    r = report or {}
    if r.get("status") == "unavailable":
        return (f"# Discoverability (advisory)\n\nEngine unavailable: "
                f"{r.get('error')}\n")
    sev = (r.get("summary") or {}).get("severity") or {}
    out = ["# Discoverability (advisory — never blocks on its own)", "",
           f"severity: " + ", ".join(f"{k} {v}" for k, v in sev.items()), ""]
    if not r.get("findings"):
        out.append("No findings — the title, description and tags look clean.")
        out.append("")
        return "\n".join(out)
    out += ["| severity | category | finding | fix |",
            "| --- | --- | --- | --- |"]
    for f in r["findings"]:
        out.append(f"| {f['severity']} | {f['category']} | {f['message']} | "
                   f"{f.get('remediation') or '—'} |")
    out.append("")
    return "\n".join(out)


# ── the deterministic local rules ─────────────────────────────────────────────
def _rules(pkg: dict) -> list[dict]:
    findings: list[dict] = []
    title = str(pkg.get("title") or "").strip()
    description = str(pkg.get("description") or "").strip()
    sections = pkg.get("sections") or []
    transcript = str(pkg.get("transcript") or "").strip()
    chapters = pkg.get("chapters") or []

    if not title:
        findings.append(_f("title.missing", "CRITICAL", "SEO",
                           "The video has no title.", "Write a title <= 100 "
                           "characters that names the payoff."))
    else:
        if len(title) > 70:
            findings.append(_f("title.long", "LOW", "SEO",
                               f"Title is {len(title)} characters.",
                               "Keep the visible title under ~70 characters so "
                               "it does not truncate in search."))
        if not any(w in title.lower() for w in _POWER_WORDS):            findings.append(_f("title.no_hook_word", "LOW", "SEO",
                               "Title has no curiosity/utility word.",
                               "Add a power word (how, why, best, guide) that "
                               "matches the content."))
        if not any(c.isdigit() for c in title):
            findings.append(_f("title.no_number", "INFO", "SEO",
                               "Title contains no number.",
                               "A number (e.g. '5 ways') often lifts CTR when "
                               "the content supports it."))

    if not description:
        findings.append(_f("description.missing", "HIGH", "SEO",
                           "The description is empty.",
                           "Write a 2-3 sentence description with the primary "
                           "keyword in the first line."))
    else:
        if len(description) < 120:
            findings.append(_f("description.thin", "LOW", "SEO",
                               f"Description is only {len(description)} chars.",
                               "Expand to at least ~150 characters of useful "
                               "context."))
        if "http" not in description:
            findings.append(_f("description.no_links", "INFO", "SEO",
                               "Description has no links.",
                               "Add links only when they are yours to add."))

    if not chapters and len(sections) >= 3:
        findings.append(_f("chapters.missing", "MEDIUM", "SEO",
                           "No chapters on a multi-section video.",
                           "Add '0:00 Intro' style timestamps for each section."))
    if sections and not transcript:
        findings.append(_f("transcript.missing", "MEDIUM", "GEO",
                           "No transcript/full script captured with the video.",
                           "Store the full script — AI answer engines cite "
                           "transcripts."))
    return findings


def _f(rule_id: str, severity: str, category: str, message: str,
       remediation: str) -> dict:
    return {"ruleId": rule_id, "severity": severity, "category": category,
            "message": message, "remediation": remediation}
