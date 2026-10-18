# Vendored tools — attribution

The Python tools in this folder are copied verbatim from the **Replica skill
pack** and are reused here under its MIT licence. They are standard-library
only and each keeps its own CLI, so they can also be run by hand.

- Upstream: https://github.com/Jakeschincariol/replica-skill
- Licence: MIT — Copyright (c) 2026 Jake Schincariol (see below)
- Files: `parity.py`, `imgdiff.py`, `reviews.py` (+ `themes.json`),
  `sweep.py`, `listing.py`, `contrast.py`

Nothing here was modified except this note. `replica/toolkit.py` imports them
as modules so the JARVIS agents call the exact same logic the CLI does — one
implementation, no drift.

## Why these six

| tool | used by | for |
| --- | --- | --- |
| `parity.py` | verify agent | feature-parity score from the feature matrix, missing list in build order |
| `imgdiff.py` | verify agent | layout diff of clone screenshots vs the original's, colour-ignored so a rebrand isn't penalised |
| `reviews.py` | critic agent | rank what real users hate/want, every quote verbatim + linked |
| `sweep.py` | verify agent | rebrand sweep: the original's name/domain/colours left in the codebase |
| `listing.py` | verify agent | Play Store listing lint against the store's field limits |
| `contrast.py` | design agent | WCAG AA contrast check on the token palette |

## MIT licence

```
MIT License

Copyright (c) 2026 Jake Schincariol

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
