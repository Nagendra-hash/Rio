"""
orchestrator/agents/youtube/tools/ — the vendored, adapted tools.

Python ports of the upstream YouTube Automation Agent utilities (MIT, see
``ATTRIBUTION.md``). Kept as importable modules with no network and no model
dependency, so the youtube agents' numbers are reproducible by hand:

    metadata        YouTube upload rules (title/description/tags/limits)
    provenance      the fact-check ledger (sources + claims → status)
    readiness       the production readiness gate (injectable probes)
    learning        metrics → baseline → deltas → economics (the closed loop)
    retention       a retention curve → which scene lost the viewers
    discoverability the advisory SEO/GEO audit + reviewer accept/dismiss
"""

from __future__ import annotations

__all__ = [
    "metadata",
    "provenance",
    "readiness",
    "learning",
    "retention",
    "discoverability",
]
