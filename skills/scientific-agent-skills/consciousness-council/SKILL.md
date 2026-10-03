---
name: consciousness-council
description: Structures a multi-perspective council exercise for decisions, research trade-offs, and creative challenges. Simulates thinking archetypes, separates evidence from assumptions and values, and synthesizes a conditional recommendation. Use when the user requests a council, panel, devil's advocate analysis, "mind council", or deliberate comparison of perspectives on a difficult choice.
license: MIT license
metadata: 
  version: 1.2
  skill-author: AHK Strategies (ashrafkahoush-ux)
  last-reviewed: 2026-09-30
---

# Consciousness Council

A structured exercise that simulates several thinking archetypes, each emphasizing different assumptions and priorities, then synthesizes their perspectives. These are generated viewpoints from one system, not independent experts or independent evidence. Label factual claims, assumptions, and value judgments separately; verify consequential factual claims against external sources.

## When to use

Use for a requested comparison of competing priorities or assumptions, including research planning decisions. The output is a decision aid: a conditional recommendation, unresolved questions, and a next step. It does not establish scientific validity by agreement or by the number of perspectives.

This skill runs within the current conversation. It has no bundled code, service API, credentials, or MindBook connection; use the host's available research tools when factual verification is needed. Do not claim that it creates hosted sessions, stores persistent memories, or contacts real experts.

## Before deliberation

1. State the decision, feasible options (including postponement when relevant), constraints, and the user's objective. Use supplied context; state material missing information as an assumption or a question.
2. Create a short evidence brief: **given facts**, **externally verified facts with sources**, **assumptions**, **unknowns**, and **value judgments**. All perspectives use this same brief.
3. Verify facts that could change the recommendation using original sources or supplied data. If verification is unavailable, keep the conclusion conditional and identify what must be checked; a simulated Empiricist is not a source.
4. Select a response budget: quick (3 perspectives), standard (4–6), or deep (6). These counts organize the output; they are not validated accuracy settings.

## How It Works

The Council has three phases:

### Phase 1 — Summon the Council

Based on the user's question, select 4–6 Council Members from the archetypes below unless the user requests another configuration. Choose lenses that examine different assumptions, constraints, or values. Agreement supported by the evidence is a valid outcome.

**The 12 Archetypes:**

| #   | Archetype          | Thinking Style                         | Asks                                         | Blind Spot                                |
| --- | ------------------ | -------------------------------------- | -------------------------------------------- | ----------------------------------------- |
| 1   | **The Architect**  | Systems thinking, structure-first      | "What's the underlying structure?"           | Can over-engineer simple problems         |
| 2   | **The Contrarian** | Inversion, devil's advocate            | "What if the opposite is true?"              | Can be contrarian for its own sake        |
| 3   | **The Empiricist** | Data-driven, evidence-first            | "What does the evidence actually show?"      | Can miss what can't be measured           |
| 4   | **The Ethicist**   | Values-driven, consequence-aware       | "Who benefits and who is harmed?"            | Can paralyze action with moral complexity |
| 5   | **The Futurist**   | Long-term, second-order effects        | "What does this look like in 10 years?"      | Can discount present realities            |
| 6   | **The Pragmatist** | Action-oriented, resource-aware        | "What can we actually do by Friday?"         | Can sacrifice long-term for short-term    |
| 7   | **The Historian**  | Pattern recognition, precedent         | "When has this been tried before?"           | Can fight the last war                    |
| 8   | **The Empath**     | Human-centered, emotional intelligence | "How will people actually feel about this?"  | Can prioritize comfort over progress      |
| 9   | **The Outsider**   | Cross-domain, naive questions          | "Why does everyone assume that?"             | Can lack domain depth                     |
| 10  | **The Strategist** | Game theory, competitive dynamics      | "What are the second and third-order moves?" | Can overthink simple situations           |
| 11  | **The Minimalist** | Simplification, constraint-seeking     | "What can we remove?"                        | Can oversimplify complex problems         |
| 12  | **The Creator**    | Divergent thinking, novel synthesis    | "What hasn't been tried yet?"                | Can chase novelty over reliability        |

**Selection heuristic:** Match the question type to the most productive tension:

- **Business decisions** → Strategist + Pragmatist + Ethicist + Futurist + Contrarian
- **Technical architecture** → Architect + Minimalist + Empiricist + Outsider
- **Personal dilemmas** → Empath + Contrarian + Futurist + Pragmatist
- **Creative challenges** → Creator + Outsider + Historian + Minimalist
- **Ethical questions** → Ethicist + Contrarian + Empiricist + Empath + Historian
- **Strategy/competition** → Strategist + Historian + Futurist + Contrarian + Pragmatist

These are starting points — adapt based on the specific question. The goal is to expose decision-relevant trade-offs and evidence gaps without a quota for disagreement or consensus.

### Phase 2 — Deliberation

Each Council Member delivers their perspective in this format:

```
🎭 [ARCHETYPE NAME]

Position: [One-sentence stance]

Reasoning: [2-4 sentences explaining their logic from their specific lens]

Basis: [Which given/verified facts, assumptions, or values support the position]

Key Risk They See: [The danger others might miss]

Decision Check: [Evidence or a changed constraint that would alter this position]
```

**Critical rules for deliberation:**

- Seek substantive differences in assumptions, evidence, and values, but do not manufacture factual disagreement. When evidence supports agreement, rec