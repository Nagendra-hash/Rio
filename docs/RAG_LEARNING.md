# RAG continuous learning — the web harvester

*October 2026. Status: implemented; 12 dedicated tests + the 218-test RAG/orchestrator
suite green.*

The library in `core/rag.py` only knew what someone uploaded. `core/rag_learner.py`
closes that loop: once a day JARVIS researches a rotating set of topics on the web,
distils the findings into a cited dossier, and files it in the RAG collection
`web_knowledge`. The next day it merges new material into the same dossier, so
knowledge compounds instead of resetting.

## 1. Where it lives

```
        main.py background task (every 30 min)          scripts/rag_learn.py
        actions/knowledge_base.py  (voice: learn)       POST /api/rag/learning
                      │                                        │
                      └──────────────┬─────────────────────────┘
                                     ▼
                         core/rag_learner.py
             curiosity → search → fetch → distil → remember → evolve
                                     │
                  orchestrator/agents/research_tools.py  (URL policy,
                  secret masking, timeouts — the permitted-source toolbox)
                                     │
                         core/rag_service.py  (caller "rag_learner",
                         ACL + audit; collection "web_knowledge")
                                     │
                              core/rag.py  (embed + index)
```

`rag_learner` is a first-party subsystem, but it is **not** exempt from the ACL:
it is minted in `config/rag_callers.json` with `can_write` only for
`web_knowledge`. `rag_agent` is granted read access to `web_knowledge` and
`research` so the orchestrator can answer from what was learned.

## 2. The loop

1. **Curious — where topics come from**
   - On first run, the learner seeds itself from the user's own monitor list
     (`memory/long_term.json` → `monitors.monitored_topics_list`) plus
     `interests` in the config.
   - When a RAG search returns nothing, `core/rag_service.search` records a
     **gap**. `core/rag_learner.note_gap` turns it into a topic with a high
     weight and keeps the user's *exact question* as the first query for the
     next cycle. The library grows from real unanswered questions, not just a
     fixed list.
   - Selection is least-recently-studied first; weight breaks ties. A topic
     already filed today is not picked again unless it carries a new gap.

2. **Search** — a few query templates (`overview explained`, `latest
   developments <year>`, `key facts and background`) plus any gap question,
   through the research toolbox (DDG). Already-seen URLs are skipped; blocked
   domains are dropped. One transient backend failure is retried once.

3. **Fetch** — up to `deep_fetch` strongest new pages are read as bounded
   plain text (URL policy + secret masking apply). Remaining sources contribute
   their search snippets.

4. **Distil** — one bounded LLM pass merges the **old dossier + new sources**
   into a Markdown note. The system prompt forbids outside knowledge, demands
   `[n]` citations, and asks it to state conflicts/unknowns. If no model
   answers, an **extractive** note is stored instead: direct snippets plus
   source links, nothing synthesized, nothing invented.

5. **Remember** — first time: `rag_service.ingest_text`; afterwards:
   `rag_service.update(text=…)` on the same doc. One living dossier per topic,
   not a pile of dated duplicates. The working copy is written to
   `memory/rag_learning/notes/<slug>.md` **before** ingest, so an embedding
   outage leaves the note for a retry (`pending` in state) instead of losing it.

6. **Evolve** — a topic that produced knowledge fades slightly (diminishing
   returns); a topic that found nothing fades faster; repeated failures park it
   near the floor. Successful gaps are cleared. State is bounded (120 topics,
   3000 URLs).

## 3. Files and configuration

`config/rag_learning.json`:

| key | default | meaning |
| --- | --- | --- |
| `enabled` | `true` | nightly cycle on/off |
| `hour` | `3` | local hour: run once the day has started |
| `interval_hours` | `0` | optional *minimum* gap between cycles (0 = once per calendar day is enough) |
| `topics_per_run` | `3` | topics per night |
| `queries_per_topic` | `2` | web searches per topic |
| `results_per_query` | `4` | results per search |
| `deep_fetch` | `2` | pages deep-read per topic |
| `min_note_chars` | `320` | below this an extractive note is used |
| `collection` | `web_knowledge` | RAG destination |
| `caller` | `rag_learner` | ACL identity (write-gated) |
| `access` | `private` | `private` or `shared` in RAG |
| `interests` | 10 seeds | used only when state has no topics |
| `blocked_domains` | `[]` | domains never fetched |

Runtime state (gitignored): `memory/rag_learning/`

```
state.json       topics, weights, seen URLs, doc ids, counters
notes/<slug>.md  working dossier per topic (source of the next merge)
gaps.jsonl       unanswered RAG questions (curiosity signal)
log.jsonl        one line per cycle / topic outcome
```

## 4. Operating it

```bash
.venv/bin/python scripts/rag_learn.py status
.venv/bin/python scripts/rag_learn.py run                    # one cycle now
.venv/bin/python scripts/rag_learn.py run "quantum computing"
.venv/bin/python scripts/rag_learn.py topics
.venv/bin/python scripts/rag_learn.py add "macro economics"
.venv/bin/python scripts/rag_learn.py remove "macro economics"
.venv/bin/python scripts/rag_learn.py gaps
.venv/bin/python scripts/rag_learn.py config enabled=false topics_per_run=2
```

- **Voice / live session:** `knowledge_base` action `learn` (optional `topics`)
  or `learn_status`.
- **Orchestrator:** `rag_agent` with `rag_action="learn"` / `"learning_status"`.
- **Dashboard:** `GET /api/rag/learning`, `POST /api/rag/learning`
  (`{action: run|add_topic|remove_topic|config}`).

The nightly task in `main.py` waits 150 s after session start, then checks every
30 minutes; `due()` + `last_run_date` make a reconnect respawn idempotent. A
process-wide lock prevents a voice-triggered cycle from overlapping the nightly
one.

## 5. Honesty and failure behaviour

- No sources → `no_sources`, nothing stored, nothing claimed.
- Model down → extractive note with snippets + links (still cited).
- Embedding/ingest failure → note kept locally, topic marked `pending`, retried
  next cycle; failures counted in state/log.
- Never raises into the session; every per-topic failure is recorded.
- Web access goes only through `research_tools.SourceToolbox` (URL policy,
  secret masking, bounded fetch), and harvested text is treated as untrusted
  evidence — the dossier prompt forbids using model prior knowledge.

## 6. Tests

`tests/test_rag_learner.py` (offline, injected search/fetch/summarize/store):

1. cycle searches, fetches, summarises and stores a cited dossier;
2. seen URLs are skipped; new sources update the existing doc;
3. an unanswered question becomes a weighted topic carrying the exact query;
4. a pytest run cannot pollute the real curiosity store (`note_gap` is
   no-op under pytest unless `force=True`);
5. no sources → no store, no note, no invention;
6. model failure → extractive note with citations;
7. transient search failure is retried once;
8. `due()` runs once a day after the configured hour;
9. a forgotten dossier is re-created instead of failing forever;
10. store failure marks `pending` and keeps the note;
11. a gap recorded mid-cycle survives the cycle's save;
12. the dashboard endpoints delegate to the learner (status, add_topic,
    run, config).
