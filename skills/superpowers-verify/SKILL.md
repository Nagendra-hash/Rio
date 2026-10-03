---
name: superpowers-verify
description: Use before claiming any JARVIS change is done, fixed, or passing — run the verification and read the output before making the claim
---

# Verification Before Completion (adapted for JARVIS)

Condensed from `obra/superpowers` → `verification-before-completion` (MIT).

**Iron law: no completion claims without fresh verification evidence.**
If you did not run the command in this same turn, you cannot claim it passes.

## The gate
Before saying anything is done, working, or fixed:

1. **Identify** — what command or test proves this specific claim?
2. **Run** it now, in full. Not from memory, not a partial run.
3. **Read** the whole output and the exit code. Count the failures.
4. **Decide** — does the output actually confirm the claim?
   - No → report the real status, with the evidence.
   - Yes → state the claim, with the evidence.

Skipping a step isn't efficiency, it's guessing.

## What each claim demands
| Claim | Requires |
|---|---|
| Tests pass | the run showing 0 failures |
| Build succeeds | build exit code 0 |
| Bug fixed | the original symptom no longer reproduces |
| Feature works | actually exercised it, not just imported |
| Config changed | re-read the file, confirm the value landed |

## Red flags — stop
- "should", "probably", "seems to", "I believe"
- Satisfaction before verification ("Done!", "That works!")
- Reporting success for something you only reasoned about
- Assuming a background/dashboard/user-facing path works because the
  unit-level function does
- Trusting a subagent or tool's success report without checking