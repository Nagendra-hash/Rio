# Daily-Life Agents (Mark III / JARVIS) — Phase 13

Three daily-life specialists on the standard BaseAgent contract, sharing
ONE toolbox so the research/RAG/Google/market infrastructure is wrapped,
never duplicated. Registered under canonical names `briefing_agent`,
`meal_agent`, `travel_agent`; each replaces its Phase-1 stub when its env
gate is on (shadow-mode default, same convention as every other phase):

    JARVIS_BRIEFING_AGENT=on
    JARVIS_MEAL_AGENT=on
    JARVIS_TRAVEL_AGENT=on

Files:

    orchestrator/agents/dailylife/toolbox.py         # the shared tool interface
    orchestrator/agents/dailylife/store.py           # memory/dailylife/ boundary
    orchestrator/agents/dailylife/checkpoint.py      # crash-safe snapshots
    orchestrator/agents/dailylife/briefing_logic.py  # assembly + audio render
    orchestrator/agents/dailylife/meal_logic.py      # pantry/menu/grocery/budget
    orchestrator/agents/dailylife/travel_logic.py    # links/compare/itinerary/watch
    orchestrator/agents/dailylife/briefing_agent.py  # TaskType.BRIEFING
    orchestrator/agents/dailylife/meal_agent.py      # TaskType.MEAL
    orchestrator/agents/dailylife/travel_agent.py    # TaskType.TRAVEL
    tests/test_dailylife_agents.py                   # 80 offline tests

## The shared toolbox (`DailyLifeToolbox`)

Every outside-world read goes through one class — the three agents share
it, and the backends are injectable so everything runs offline in tests:

| Capability | Wrapped from | Notes |
|---|---|---|
| Web search / fetch / local KB | `research_tools.SourceToolbox` | permitted-source policy, SSRF guard, secret masking |
| Calendar events | `productivity.google_tools` | same token as the Calendar skill, reads only |
| Unread email | `productivity.google_tools` | metadata view, reads only |
| Email urgency | `productivity.email_logic` | the Inbox Agent's own classifier, reused |
| Market quotes | `trading.data.MarketData` | provenance travels: LIVE / DELAYED / SIMULATED / UNAVAILABLE |
| Redaction | `productivity.security` | every reply and every store write |

Every toolbox method returns `{"ok": bool, ...}` and never raises outward.
A failed source is REPORTED — never faked over, never silently retried.

## The honesty rule (all three agents)

Live prices, availability, weather and news are stated ONLY when a live
source was actually queried in that call and succeeded — and the label
travels with the data (`live web search 08:00`, provenance for market
quotes). When nothing ran, the agents say "unavailable — no live source
queried" and hand over deep links instead. Nothing is invented to fill a
gap: dietary gaps render as "(no vegan lunch recipe fits)", comparisons
skip rows missing a field, and a watch needs two live observations before
it will show a delta.

## Daily Briefing (`briefing_agent`)

Sections, in order: calendar (today) → important email (urgency-ranked
unread) → tasks → weather (user's city) → user-selected news topics →
market summary (only when enabled, provenance-labeled). Every reply also
carries an audio-ready variant in `data["audio"]` — flat speakable text,
no emoji or markdown.

    text: "morning briefing"                 # full briefing
    text: "add task pay the electricity bill"
    text: "done pay the electricity bill"
    text: "tasks"
    metadata: {city, topics, market_enabled, symbols, timezone}
    metadata: {briefing_action: "prefs"}     # show / set preferences

## Meal & Grocery (`meal_agent`)

Preferences (diet/allergies/household/budget/price hints), pantry with
expiry dates, a deterministic 7-day menu built from a small built-in
template library (use-up flags push soon-to-expire items first), a grocery
list = menu ingredients MINUS pantry, and budget awareness from the
user's OWN saved price hints — never a live price.

    text: "plan my weekly menu"              # metadata diet/allergies win per build
    text: "make a grocery list"              # budget line only from price hints
    text: "add rice to the pantry"           # or metadata {pantry_item, qty, unit, expires}
    text: "used 0.5 kg rice"
    text: "what's expiring"
    metadata: {meal_action: "prefs", diet: "veg", price_hints: {...},
               budget_week: 500}

## Travel (`travel_agent`)

Search deep links for flights, trains, buses, road, hotels and tourism
(built offline — opening them reaches live prices); optional live web
search labeled as such; route/time and price comparison over SUPPLIED
rows; itineraries saved as drafts; price/delay watches with on-demand
checks and baseline deltas.

    text: "flights from Delhi to Goa on 2026-10-20"
    metadata: {options: [{mode, minutes, cost}, …]}      # comparison
    metadata: {start: "2026-10-20", days: 3, items_by_day: {…}}
    text: "watch flights from Delhi to Goa on 2026-10-20"
    metadata: {watch_action: "check", value: 4200}       # live check + baseline

Watches run when asked (or when a scheduler calls the check action) — no
background scraping is implied, and a single observation never becomes a
"trend".

## Store & checkpoint

`memory/dailylife/` (relocated by `JARVIS_HOME`): `prefs.json`,
`tasks.json`, `pantry.json`, `menu.json`, `grocery.json`,
`watchlist.json`, `drafts.jsonl`, `audit.jsonl` — 0600 files in a 0700
directory, redacted through `productivity.security.redact_obj()` before
every write, audit log with ACTIONS ONLY. `DailyLifeCheckpoint` inherits
the productivity checkpoint (tmp+replace, prune, redaction) and writes
`dailylife-*.json` snapshots; prefs/pantry/menu/watch writes snapshot.

## Safety posture

All three agents are read-only over the outside world — the only writes
are the user's own local data (prefs/tasks/pantry/watches/drafts), so no
Phase-13 action needs an approval gate. The moment any of them gains an
outside-world write (booking, sending, ordering), it must adopt the
productivity pattern: park the exact payload behind
`security.approval_result()` and execute only the approved checkpoint.
