---
name: superpowers-debugging
description: Use when hitting any bug, failing test, or unexpected behaviour in JARVIS' own code, before proposing a fix — find the root cause instead of guessing
---

# Systematic Debugging (adapted for JARVIS)

Condensed from `obra/superpowers` → `systematic-debugging` (MIT).

**Iron law: no fixes without root-cause investigation first.**
Symptom fixes are failure. A fix that works for the wrong reason is a latent bug.

## When this applies
Any bug, failing test, build failure, or "that's not what I expected" —
*especially* when it seems trivial, you're in a hurry, or a previous fix
didn't stick. Rushing guarantees rework.

## The four phases — complete each before the next

**1. Investigate the root cause.**
- Read the error in full. Stack traces, line numbers and codes often carry
  the answer; don't skim past warnings.
- Reproduce it reliably. If you cannot, gather more data — do not guess.
- Check what recently changed (`git diff`, recent commits, new deps, config).
- In multi-component flows (dashboard → action → LLM, tool → API), log what
  enters and exits each boundary before theorising.

**2. Form one hypothesis.** State it in a sentence and say what evidence
would confirm or kill it. One at a time.

**3. Test the hypothesis.** Change one thing. If it doesn't hold, discard it
and go back to phase 1 — do not stack a second guess on a first.

**4. Fix at the cause.** Remove the root cause, then check nothing else
depends on the symptom.

## Red flags — stop and return to phase 1
- You've tried two or more fixes without understanding the cause.
- You're editing files you haven't read.
- You're changing a file you cannot explain the purpose of.
- The fix makes the test pass but you don't know *why*.