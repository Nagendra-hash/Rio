# Attribution — the tools in this folder

The modules under `orchestrator/agents/youtube/tools/` are Python ports and
adaptations of utilities from the **YouTube Automation Agent** project ("Lumen
v2.11.0"), which is distributed under the MIT License:

> https://github.com/darkzOGx/youtube-automation-agent
> Copyright (c) 2025 YouTube Automation Agent Contributors
> MIT License

MIT permits use, modification, and redistribution provided the copyright
notice and permission notice are retained. That notice lives here and in the
repository's `LICENSE`/`THIRD_PARTY` records; the upstream license text is
reproduced verbatim at the bottom of this file.

## What was ported, and how it changed

Nothing was copied file-for-file: each tool was re-implemented in Python for
JARVIS's model, with the upstream *rules and contracts* preserved so a number
the agent reports matches the upstream tool's number.

| This file | Upstream source | Change |
| --- | --- | --- |
| `metadata.py` | `utils/youtube-metadata-validator.js` | Faithful port of the limits (title 100, description 5000, tags 450/30) and the `normalize`/`validate` contract. |
| `provenance.py` | `utils/provenance-service.js` | Port of the source/claim ledger and its status rules (`supported` needs a *verified* source; `waived` needs a note). |
| `readiness.py` | `utils/production-readiness-service.js` | Port of the check *framework* (blocking vs warning, remediation, secret redaction). Upstream's live probes (OpenAI/Gemini/ffmpeg/YouTube API) are replaced by injectable callables so the gate is honest offline. |
| `learning.py` | `utils/channel-learning-engine.js` | Port of `normalizeMetrics` / baseline / deltas / confidence / net-revenue-ROI, including the "simulated analytics are excluded" rule. |
| `retention.py` | `utils/scene-retention-engine.js` | Port of the curve→scene mapping, the largest drop/lift per scene, and the "too thin to store" threshold (10 points). |
| `discoverability.py` | `utils/discoverability-service.js` | Port of the findings contract (severity/category vocabulary, stable fingerprint, accept/dismiss-with-reason). The external DarkzSEO engine is replaced by a small local, deterministic rule set; findings stay advisory. |

Upstream modules that are Node/network/service-specific (`ai-video-generator`,
`media-generation-service`, `video-providers`, `ffmpeg`, `credential-manager`,
`shorts-repurposing-service`, the SQLite `database`, the Express dashboard and
`index.js`) were **not** ported. JARVIS's own model router, `actions/dev_agent`
build path, and dashboard take their place.

## Design intent

The point of the port is the *discipline* of the upstream pipeline — never
publish a placeholder, never publish an unsupported claim, never let a metric
be invented, and let the human approve each learning before it steers the
next plan. JARVIS keeps that discipline and drops the paid-provider coupling.

## Upstream license (verbatim)

```
MIT License

Copyright (c) 2025 YouTube Automation Agent Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
