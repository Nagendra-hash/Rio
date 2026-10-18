# YouTube automation agent (JARVIS)

A six-stage YouTube automation agent for JARVIS, adapted from the open-source
[YouTube Automation Agent](https://github.com/darkzOGx/youtube-automation-agent)
("Lumen", MIT — see `tools/ATTRIBUTION.md`). It keeps the upstream pack's
discipline — the loop, the honesty rules and the approval gates — and rebuilds
it in Python on JARVIS's model router.

| Stage | What it does | What you control |
| --- | --- | --- |
| **Research** | Finds topics and builds a content strategy | Niche, audience, blocked topics |
| **Script** | Writes the hook, narrative, CTA and metadata | Voice, format, length, brand direction |
| **Production** | Generates narration and visuals, then assembles a real MP4 | Provider choice and media fallbacks |
| **Review** | Runs quality checks and opens the video in Review Studio | Facts, media rights, edits, approval |
| **Publish** | Schedules and uploads approved content | Privacy, timing, final decision |
| **Learn** | Captures 24-hour and 7-day evidence, measures the configured outcome and economics, then proposes the next move | Choose the KPI and approve or reject each learning before it guides planning |

Placeholders are distinguished from real MP4s: a **simulated** result cannot
enter the approval or publishing path. No statistic, quote, source or metric is
ever invented; every factual claim a script makes is recorded in a ledger and
must be supported by a *verified* source or explicitly waived before publishing.

## Use

```python
from orchestrator.agents.youtube import YouTubePipeline

pipe = YouTubePipeline("~/Desktop/MyChannel")
pipe.run(channel={
    "name": "Espresso Weekends",
    "objective": "help home baristas pull better shots",
    "audience": "home espresso beginners",
    "value_proposition": "clear, testable espresso advice",
    "pillars": ["Dialing in", "Gear on a budget", "Milk texture"],
    "default_format": "tutorial",
    "voice": "warm, precise, no hype",
    "kpi": "watch_time", "target_value": 4000, "target_window_days": 28,
    "blocked_topics": ["competitor bashing", "medical claims"],
    "privacy_status": "private",
}, render=True)          # render=True tries a real MP4 (ffmpeg + system TTS)
```

Then, later, close the loop with real numbers:

```python
pipe.run(steps=["learn"], evidence={"24h": {...}, "7d": {...}})
pipe.run(steps=["learn"], approve=["Fix the watch_time shortfall"])
```

Publishing requires both flags — the human's decision:

```python
pipe.run(steps=["publish"], approved=True, publish=True)
```

## As a routed agent / chat action

`youtube_agent` is the `BaseAgent` (TaskType `YOUTUBE`), registered behind
`JARVIS_YOUTUBE_AGENT=on`. The chat action lives in `actions/youtube_agent.py`:

```
youtube_agent(action="run", channel={"name": "...", "pillars": [...]})
youtube_agent(channel={...}, action="learn", evidence={...}, approve=["*"])
```

## Artifacts

Everything lands under `<workspace>/youtube/`: `research.md`, `script.md`,
`seo.json`, `production.md`, `review.md`, `review-studio.html`, `publish.json`,
`learning.md`, `recommendations.json`, and the resumable `state.json`.

## Guardrails

Enforced in code (`policy.py`) and carried in every prompt:

- nothing uploads without `approved=True` **and** `publish=True`;
- a simulated/placeholder render or missing narration blocks publishing;
- a script's unsupported factual claims block publishing (fact ledger);
- media without a recorded right of use is refused;
- the operator's blocked topics are never planned;
- requests to fake statistics, buy views/subscribers or invent reviews are
  declined before any work happens.
