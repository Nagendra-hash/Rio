---
name: superpowers-tdd
description: Use when implementing any feature or bugfix in JARVIS' code — write the failing test first, then the minimum code that passes it
---

# Test-Driven Development (adapted for JARVIS)

Condensed from `obra/superpowers` → `test-driven-development` (MIT).

**Core discipline: red → green → refactor.** A test written after the code
proves very little — it usually just re-states the implementation.

## The loop

**1. Red.** Write one failing test for the smallest next behaviour. Run it.
*Watch it fail for the right reason* — a test failing on a typo or bad import
proves nothing; fix the test first.

**2. Green.** Write the minimum code that passes it. Resist handling the next
case "while you're in there". Then run the WHOLE suite — you may have broken
something else.

**3. Refactor.** Only with green tests as your safety net. Re-run after.

One behaviour at a time.

## Wrote the code first?
Delete it and start over, *or* write the test now and verify it genuinely fails
against the pre-fix behaviour. A test that has never failed cannot detect
anything.

## Sizing a test
Test through the interface a caller actually uses. A test reaching into private
helpers breaks on every refactor and says nothing about the real contract. For
JARVIS: assert on what the *caller* receives — the string a user sees, the JSON
the dashboard reads — not on internal call structure.

## Clean tests
- One reason to fail per test.
- Name it after the behaviour, not the function.
- No dependence on test order or shared state.
- Stub network, clock and filesystem — a test hitting the real internet passes on
  your machine and flakes in CI.

**The rule that matters most: if you did not watch it fail, it is not a test —
it is a comment that runs.**