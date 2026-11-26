"""
orchestrator/agents/dailylife/ — the Phase 13 daily-life specialists.

Three specialists on the standard BaseAgent contract, sharing ONE toolbox
(no duplicated research/RAG/Google/market plumbing):

    toolbox.py         the shared tool interface: permitted web search /
                       fetch / local KB (research_tools.SourceToolbox),
                       calendar + email reads (productivity.google_tools),
                       message urgency (productivity.email_logic), and
                       provenance-labeled market quotes (trading.data).
                       Every backend is injectable; nothing here pretends —
                       an unavailable source is reported, never faked.
    store.py           the daily-life data boundary: memory/dailylife/
                       (0600 files in a 0700 dir), actions-only audit.
    checkpoint.py      crash-safe checkpoints (same contract as the
                       productivity/health/trading ones).
    briefing_logic.py  deterministic briefing assembly + audio-ready render.
    meal_logic.py      pantry/expiry math, weekly menu builder, grocery
                       list + budget awareness (offline templates).
    travel_logic.py    deep-link builders, route/time + price comparison
                       over SUPPLIED data, itinerary builder, watch deltas.
    briefing_agent.py  the Daily Briefing Agent (TaskType.BRIEFING).
    meal_agent.py      the Meal & Grocery Agent (TaskType.MEAL).
    travel_agent.py    the Travel Agent (TaskType.TRAVEL).

All three are off by default (shadow-mode convention):
    JARVIS_BRIEFING_AGENT=on
    JARVIS_MEAL_AGENT=on
    JARVIS_TRAVEL_AGENT=on

THE HONESTY RULE (shared by all three): live prices, availability, weather
or news are only ever reported when a live source was actually queried and
succeeded — and the label travels with the number. When no source ran, the
agent says so and hands the user deep links instead of inventing figures.
"""
