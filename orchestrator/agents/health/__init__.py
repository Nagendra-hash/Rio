"""
orchestrator/agents/health/ — the Phase 11 Health Assistant.

Safety-first health support on the standard agent contract:

    safety.py        red-flag emergency triage, diagnosis/prescribing
                     refusals, disclaimers, systems-of-medicine info,
                     urgent measurement-value flags — all in code, no LLM.
    parser.py        deterministic lab-report / measurement / medication /
                     symptom extraction; patient identity is dropped.
    store.py         the health-data boundary: memory/health/ with 0600/0700
                     permissions, audit log without content, wipe().
    checkpoint.py    crash-safe snapshots of the whole health state.
    health_agent.py  the BaseAgent wiring it all together.

Disabled by default (shadow-mode convention): JARVIS_HEALTH_AGENT=on.
"""
