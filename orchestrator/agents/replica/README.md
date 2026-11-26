# replica — clean-room app rebuilds from a Play listing

Adapted for JARVIS from the [Replica skill pack](https://github.com/Jakeschincariol/replica-skill)
(MIT). One job: **rebuild what an app does, never what it owns.**

Point it at a Google Play link and it reverse-engineers the app from its
**public listing**, plans an Android build, reads the app's real reviews to
find what users hate, rebuilds it as your own improved app, then verifies
parity + rebrand before it ships.

## The seven small agents

| agent | reads | writes |
| --- | --- | --- |
| **scout** (`recon`) | the public Play listing | `replica/recon.md`, `source-listing.json` |
| **study** (`architecture`) | the recon map | `replica/architecture.md`, `features.csv`, the app spec |
| **design** | screenshots + colour roles | `replica/design/tokens.json`, `components.md` |
| **critic** (`critic`) | `replica/reviews.csv` (real reviews) | `replica/feedback.md`, `fixes.md` |
| **enhance** | the fixes | enhancements in `features.csv`, `brand.md/json`, `launch/listing.json`, `positioning.md` |
| **builder** (`build`) | the app spec | an APK, via `actions/dev_agent` (the Android coding pipeline) |
| **verify** | everything | `replica/parity.md`, the ship gates |

Each agent is independently callable and each degrades to a deterministic
artifact when no model is available — never a fake one. The critic never
invents a review: no `reviews.csv`, nothing ranked.

## Use it

Voice / chat (auto-discovered action):

```
clone this play store app and make it better:
https://play.google.com/store/apps/details?id=com.example.app
```

Or the action directly:

```python
replica_agent({"url": "…app details?id=…"})                       # full recon+plan+enhance
replica_agent({"url": "…", "action": "recon"})                    # just the recon map
replica_agent({"url": "…", "action": "build", "build": True})     # …and compile the APK
replica_agent({"url": "…", "reviews": "/path/reviews.csv"})       # rank real reviews
replica_agent({"action": "status"})                               # what a run produced
```

Or as a routed agent (`JARVIS_REPLICA_AGENT=on`, on by default via `main.py`):

```bash
python -m pytest tests/test_replica_agent.py -q
```

## The four gates before anything is "shippable"

`replica/parity.md` reports all four; **every** one must pass:

1. **must-haves done** — every `must` feature in the matrix is `clone=yes`
2. **parity ≥ 80** — the weighted feature-parity score (vendored `parity.py`)
3. **rebrand clean** — the sweep finds none of the original's name/domain/colours
4. **listing ok** — the new Play listing lints clean (`listing.py`) + AA contrast

## Clean-room rules (enforced in `policy.py`, carried in every prompt)

* **Allowed:** the public Play listing, the app's public docs, and its
  observable behaviour as a normal user.
* **Never:** decompile/disassemble the APK to lift code or assets; copy the
  original's code, strings, artwork, icons, sounds, logos or licensed fonts;
  hammer the original's servers; ship anything still carrying the original's
  name, package id or brand colour.
* A request that asks to decompile, or a URL that serves the APK, is
  **refused before any work** (`policy.assert_no_decompile` /
  `assert_source_allowed`).

## Tools

`tools/` vendors six MIT tools from the upstream pack verbatim (see
`tools/ATTRIBUTION.md`): `parity.py`, `imgdiff.py`, `reviews.py`,
`sweep.py`, `listing.py`, `contrast.py`. `toolkit.py` imports them so the
agents and the CLIs share one implementation — a number the agent reports is
a number you can reproduce by hand:

```bash
python3 orchestrator/agents/replica/tools/parity.py replica/features.csv
python3 orchestrator/agents/replica/tools/sweep.py . --config replica/brand.json
```
