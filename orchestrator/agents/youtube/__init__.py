"""
orchestrator/agents/youtube/ — a YouTube automation agent for JARVIS.

Adapted from the open-source **YouTube Automation Agent** ("Lumen",
https://github.com/darkzOGx/youtube-automation-agent, MIT — see
``tools/ATTRIBUTION.md``). The upstream pack is a Node service that runs a
channel end to end; this package keeps its *discipline* — the six-stage loop,
the honesty rules, and the human-approval gates — and rebuilds it in Python on
JARVIS's own model router, tooling and dashboard.

Six small, independently callable stages, each honest when a model is
unavailable (it degrades to a deterministic artifact, never a fake one):

    research    niche + audience + blocked topics → an editorial plan
    script      hook, narrative, CTA, claims, metadata, thumbnail concept
    production  narration + visuals + a real MP4 (or an honest placeholder)
    review      readiness + fact ledger + an advisory SEO audit → gates
    publish     schedule, and upload ONLY with explicit human approval
    learn       capture 24h/7d evidence, measure the KPI + economics, propose
                the next move (pending until approved)

The operator controls the things that matter: niche/audience/blocked topics,
voice/format/length, provider choice and media fallbacks, the facts and media
rights, privacy and timing and the final publish decision, and the KPI and each
learning.

Files:
    policy.py        the guardrails (approval, no fabrication, media rights)
    schema.py        the channel / script / bundle / publish / learning shapes
    toolkit.py       one import surface over the tools + deterministic helpers
    tools/           the adapted MIT tools + ATTRIBUTION.md
    llm.py           the model layer (router → gemini), injectable
    project.py       the artifact store (youtube/…, resumable state)
    agents.py        the six stage agents
    pipeline.py      run the pack in order (resumable)
    youtube_agent.py the BaseAgent wiring the pack as `youtube_agent`

Registered behind ``JARVIS_YOUTUBE_AGENT=on`` (shadow-mode convention). The
guardrails are enforced in code (``policy.assert_publishable`` /
``assert_no_fabrication``) and carried in every prompt — nothing is uploaded
without approval, no placeholder or unsupported claim is published, no metric
is invented.
"""
from __future__ import annotations

# Public names resolve lazily so importing this package never drags the whole
# pipeline (and its optional model layer) into a caller that only wants
# `policy`, say. Mirrors the shadow-mode convention used across the registry.
_SUBMODULES = ("agents", "policy", "schema", "toolkit", "llm", "project",
               "pipeline", "youtube_agent")
_ATTRS = {
    "YouTubeAgent": ("orchestrator.agents.youtube.youtube_agent", "YouTubeAgent"),
    "YouTubePipeline": ("orchestrator.agents.youtube.pipeline", "YouTubePipeline"),
    "YouTubeProject": ("orchestrator.agents.youtube.project", "YouTubeProject"),
}

__all__ = list(_SUBMODULES) + list(_ATTRS)


def __getattr__(name: str):
    import importlib
    if name in _SUBMODULES:
        value = importlib.import_module(f"{__name__}.{name}")
    elif name in _ATTRS:
        module = importlib.import_module(_ATTRS[name][0])
        value = getattr(module, _ATTRS[name][1])
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value
