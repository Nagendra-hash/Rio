"""
orchestrator/agents/replica/ — clean-room app rebuilds from a Play listing.

Adapted for JARVIS from the Replica skill pack
(https://github.com/Jakeschincariol/replica-skill, MIT): *rebuild what an app
does, never what it owns*. Where the upstream pack targets the web (Next.js +
Playwright), this one targets Android and hangs off the existing coding
pipeline, so a rebuilt app is compiled by the same Gradle scaffold and the
same `android_support` repairs the dev agent uses.

Seven small agents, each independently callable and each honest when a model
is unavailable (it degrades to a deterministic artifact, never a fake one):

    scout    public Play listing  → recon map (screens, flows, features)
    study    recon map            → Android architecture + feature matrix
    design   screenshots/roles    → Material 3 tokens + component specs
    critic   real reviews         → what users hate/want, in their words
    enhance  the fixes            → enhancements + brand + store listing
    builder  the plan             → an Android APK (via the coding agent)
    verify   everything           → parity + rebrand + listing gates

Files:
    policy.py       the clean-room guardrails (allowed sources, no decompile)
    schema.py       the recon map / feature matrix / review shapes
    store.py        read a public Play listing (stdlib urllib + re)
    toolkit.py      one import surface over the vendored tools
    tools/          the MIT tools (parity, imgdiff, reviews, sweep, listing,
                    contrast) + ATTRIBUTION.md
    llm.py          the model layer (router → gemini SMART), injectable
    project.py      the artifact store (replica/…, resumable state)
    agents.py       the seven small agents
    pipeline.py     run the pack in order (resumable)
    replica_agent.py the BaseAgent wiring the pack as `replica_agent`

Registered behind JARVIS_REPLICA_AGENT=on (shadow-mode convention). The
clean-room rules are enforced in code (policy.assert_source_allowed /
assert_no_decompile) and carried in every prompt — a request to decompile the
original, or a source URL that serves its APK, is refused before any work.
"""

from __future__ import annotations

# Public names are resolved lazily so importing this package never drags the
# whole pipeline (and its optional model layer) into a caller that only wants
# `policy`, say. Mirrors the shadow-mode convention used across the registry.
_SUBMODULES = (
    "agents",
    "policy",
    "schema",
    "store",
    "toolkit",
    "llm",
    "project",
    "pipeline",
    "replica_agent",
)
_ATTRS = {
    "ReplicaAgent": ("orchestrator.agents.replica.replica_agent", "ReplicaAgent"),
    "ReplicaPipeline": ("orchestrator.agents.replica.pipeline", "ReplicaPipeline"),
    "ReplicaProject": ("orchestrator.agents.replica.project", "ReplicaProject"),
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
