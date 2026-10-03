---
name: superpowers-brainstorm
description: Use before building any new JARVIS feature, action, or integration — clarify what is actually wanted before writing code
---

# Brainstorming Before Building (adapted for JARVIS)

Condensed from `obra/superpowers` → `brainstorming` (MIT).

**Core idea: understand the real requirement before writing code.**
Most bad implementations are correct answers to a question nobody asked.

## Before any creative work
Building a feature, adding an action, wiring an integration, changing
behaviour? Do not open a file yet.

**1. Establish the actual goal.** What problem does this solve? What does the
user do today that is hard? Often the first request is a *solution*, not the
problem behind it.

**2. Ask about what matters, briefly.** Only genuine unknowns — intent,
constraints, preferences that change the design. Do not interrogate someone
who already told you, and do not stall on questions whose answers don't change
what you build.

**3. Offer 2–3 real approaches**, not one. Include a minimal option. State each
trade-off in a sentence. Then stop and let the user choose.

**4. Agree on scope explicitly.** State what you are building *and* what you
are deliberately not building. Scope creep is where these projects die.

## Then, and only then
Write the plan in small, independently verifiable steps. Confirm it, then
implement — see `superpowers-tdd` and `superpowers-verify`.

## The trap
"Could you just add X?" — the fastest-feeling path. Resist it: X sits inside a
design you have not looked at yet, and matching surrounding conventions usually
costs more than the feature.

## Already-clear cases
Small, obvious and reversible (rename, typo fix, an extra log line)? Just do
it. Over-asking is its own failure mode.