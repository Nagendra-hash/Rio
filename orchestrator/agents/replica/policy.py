"""
orchestrator/agents/replica/policy.py — the clean-room guardrails.

This is the line the whole replica pipeline stands on, adapted from the
upstream Replica pack's own rules (https://github.com/Jakeschincariol/replica-skill):
*rebuild what an app does, never what it owns*.

What the pipeline MAY use:
  * a Google Play **listing**: title, description, category, rating, review
    count, "What's new", the reviews users wrote, the public screenshots;
  * the app's **observable behaviour** as a normal user experiences it, and
    the user's own account with it;
  * public help docs, the app's marketing site, press coverage.

What it must NEVER do (enforced here and in the agent prompts):
  * decompile, disassemble or otherwise extract the app's APK/AAB to lift its
    code, resources or assets ("clone" by copying);
  * copy the original's code, strings, artwork, logos, icons, sounds or
    licensed fonts into the new app;
  * drive the original's servers with scripts/fuzzers/load (only by-hand,
    as a normal user, to observe behaviour);
  * ship anything that still carries the original's name, package id,
    domain, brand colour or store listing before a rebrand.

Every function here is pure and stdlib-only so the checks are testable
offline and cannot be "worked around" by a model: the pipeline calls
``assert_source_allowed`` before it fetches, and ``check_rebrand_clean``
before it will call a build shippable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── the allowed sources ──────────────────────────────────────────────────────
# A source the pipeline is permitted to READ. Play Store listings and the
# app's public site are fine; anything that would hand us the app's own code
# or assets is refused.
ALLOWED_HOSTS = (
    "play.google.com",
    "play.googleusercontent.com",   # listing screenshots CDN
    "apps.apple.com",
    "itunes.apple.com",
)

# Anything matching these is the original's *implementation*, not a public
# description of it, and is refused outright.
BLOCKED_PATTERNS = (
    re.compile(r"apkpure|apkmirror|apkcombo|apkfab|aptoide|apkpure\.net", re.I),
    re.compile(r"\.(apk|aab|xapk|dex|smali|jar)(\b|$|\?)", re.I),
    re.compile(r"decompil|jadx|apktool|smali|dex2jar|\bIDA\b|\bGhidra\b", re.I),
    re.compile(r"crack|mod apk|modded|premium unlocked|piracy|torrent", re.I),
    re.compile(r"frida|objection|hook(ing)? the app|mitm|reverse.?engineer the code", re.I),
)

# The reasons a build is refused a "shippable" verdict. Kept as data so the
# verify agent and the tests can assert on them.
REBRAND_REQUIRED = (
    "The original app's name must not appear in the new app or its metadata.",
    "The original's package id / applicationId must not be reused.",
    "The original's logo, icon, artwork and licensed fonts must not be copied.",
    "The original's brand colour must be replaced with your own palette.",
)


class PolicyViolation(Exception):
    """Raised when a request would step outside the clean-room rules."""


@dataclass(frozen=True)
class SourceCheck:
    allowed: bool
    reason: str = ""
    url: str = ""


def assert_source_allowed(url: str) -> SourceCheck:
    """Would reading this URL break the clean-room rules?

    Returns a SourceCheck; never raises. A Play Store / App Store listing is
    allowed. A mirror that serves the APK, or a page about decompiling the
    app, is not.
    """
    url = (url or "").strip()
    if not url:
        return SourceCheck(False, "empty source url", url)
    for pat in BLOCKED_PATTERNS:
        if pat.search(url):
            return SourceCheck(False,
                               f"refused: '{pat.pattern}' is not a public "
                               f"description of the app — it is its code or "
                               f"assets. Clean-room rules allow listings and "
                               f"public docs only.", url)
    scheme_host = re.match(r"^https?://([^/]+)", url)
    if not scheme_host:
        return SourceCheck(False, "not an http(s) url", url)
    host = scheme_host.group(1).lower().split(":")[0]
    if any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        return SourceCheck(True, "public store listing", url)
    # Other public sources (the app's own site, docs, press) are allowed for
    # *reading behaviour* — they only fail the stricter store-listing check.
    return SourceCheck(True, "public web source (read behaviour only)", url)


def assert_no_decompile(request_text: str) -> SourceCheck:
    """Reject an ask that is really 'rip the app apart'."""
    text = request_text or ""
    for pat in BLOCKED_PATTERNS:
        m = pat.search(text)
        if m:
            return SourceCheck(False,
                               f"refused: the request mentions '{m.group(0)}'. "
                               f"This pipeline rebuilds behaviour clean-room; "
                               f"it never decompiles the original.", "")
    return SourceCheck(True, "no decompilation asked for", "")


def rebrand_checklist() -> list[str]:
    return list(REBRAND_REQUIRED)


def clean_room_banner() -> str:
    """The one-paragraph policy the build/critic prompts carry."""
    return (
        "CLEAN ROOM. Rebuild what the app DOES, never what it OWNS. Use only "
        "the public Play Store listing, the app's public docs and its "
        "observable behaviour as a normal user. Write every line of code, "
        "every label and every empty state fresh. Never copy the original's "
        "code, strings, artwork, icons, sounds, logos or licensed fonts, and "
        "never decompile the APK. Rebrand fully before anything is called "
        "shippable."
    )


def check_rebrand_clean(original_names: list[str], original_domains: list[str],
                        original_colors: list[str],
                        project_root, *, include_planning: bool = False) -> dict:
    """Run the rebrand sweep over a project folder.

    Thin wrapper over the vendored ``sweep`` tool so both the verify agent and
    tests call one implementation. Returns
    ``{"clean": bool, "hits": [...], "message": str}``; never raises.
    """
    names = [n for n in (original_names or []) if str(n).strip()]
    domains = [d for d in (original_domains or []) if str(d).strip()]
    colors = [c for c in (original_colors or []) if str(c).strip()]
    if not (names or domains or colors):
        return {"clean": False,
                "message": ("nothing to sweep for: pass the original app's "
                            "name so leftovers can be found."),
                "hits": []}
    try:
        from orchestrator.agents.replica.tools import sweep as _sweep
    except Exception as exc:                       # pragma: no cover
        return {"clean": False, "hits": [],
                "message": f"rebrand sweep unavailable: {exc}"}
    try:
        hits = _sweep.sweep(str(project_root), names, domains, colors,
                            include_planning)
    except Exception as exc:
        return {"clean": False, "hits": [],
                "message": f"rebrand sweep failed: {exc}"}
    if hits:
        return {"clean": False, "hits": hits,
                "message": (f"{len(hits)} leftover(s) of the original in the "
                            f"build — replace every one before shipping.")}
    return {"clean": True, "hits": [],
            "message": "clean: nothing of the original's name, domain or "
                       "colours is left in the build."}
