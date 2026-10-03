---
name: scientific-brainstorming
description: Facilitates evidence-aware scientific ideation with independent generation, structured discussion, explicit assumptions, transparent evaluation, adversarial review, and decision logs. Use for early-stage research brainstorming or prioritizing candidate directions; hand off empirical validation, study design, ethics or regulatory review, and clinical questions to appropriate experts or skills.
license: MIT
compatibility: Core guidance works in any Agent Skills-compatible host. Optional bundled CLIs require Python 3.11+ and use only the standard library; they make no network or LLM calls and require no credentials.
metadata: 
  version: 1.4
  skill-author: K-Dense Inc.
  last-reviewed: 2026-10-01
---

# Scientific Brainstorming

## Purpose and boundaries

Use this skill to create, organize, challenge, and transparently prioritize
candidate research directions. Treat every output as a **proposal**, not a
finding. Creativity methods can alter participation and idea yield, but no
method universally improves originality, usefulness, or scientific validity.
The evidence base and its limits are summarized in
`references/sources.md`.

Keep these activities separate:

- **Ideation** creates questions, mechanisms, alternatives, or study concepts.
- **Evidence assessment** checks what reliable literature and data support.
- **Hypothesis validation** requires observations, predictions, suitable
  designs, analyses, and independent scrutiny; brainstorming cannot validate a
  hypothesis.
- **Ethics, biosafety, dual-use, regulatory, and institutional review** require
  the relevant authorized reviewers. A brainstorm is never approval.
- **Clinical advice** requires qualified clinicians and patient-specific
  context. Do not turn research ideas into diagnosis or treatment guidance.

For an observation-led testable hypothesis, hand off to
`hypothesis-generation`. For study architecture, use `experimental-design`;
for sample size, `statistical-power`; for existing evidence,
`literature-review`; and for analysis, `statistical-analysis`.

## Operating rules

1. Label claims as **idea**, **assumption**, **prediction**, **located
   evidence**, or **decision**. Never blur these categories.
2. Generate independently before exposing participants to other people's or
   AI-generated ideas. Face-to-face turn-taking can block production, and
   examples can anchor later output.
3. Preserve minority views, negative evidence, uncertainty, and abstentions.
   Consensus is not truth and vote counts are not effect sizes.
4. Record provenance without exposing confidential, personal, controlled, or
   unpublished information.
5. Define evaluation criteria and directions before scoring. Keep raw ratings,
   reasons, ranges, and disagreement visible.
6. Search the literature **after an initial independent round** when practical,
   then deliberately reopen ideation. This reduces early anchoring without
   mistaking an incomplete search for a research gap.
7. Do not automatically select a “winner.” Scores are traceable decision aids;
   qualitative judgment, uncertainty, feasibility, and ethics gates remain
   controlling.

## Reproducible workflow

### 1. Scope the session

Write one focal question and record:

- purpose, audience, decision owner, and time horizon;
- in-scope and out-of-scope topics;
- constraints that are real, assumed, negotiable, or unknown;
- current knowledge, unresolved observations, and prohibited outputs;
- whether human participants, animals, clinical care, sensitive data,
  pathogens, controlled technologies, or environmental release could be
  implicated.

If the request seeks patient-specific care, evasion of oversight, harmful
optimization, or operationally enabling dual-use details, stop ideation and
route to the appropriate professional or institutional process.

### 2. Diversify perspectives deliberately

Invite relevant methodological, domain, implementation, statistical, safety,
ethics, stakeholder, and lived-experience perspectives. Diversity is not a
guarantee of creativity: explain whose perspective is represented, missing, or
structurally disadvantaged. Use accessible participation modes and
pseudonymous participant IDs where appropriate.

The facilitator should disclose conflicts, avoid offering a preferred answer
first, prevent senior members from dominating, and ask leaders to contribute
after the independent round.

### 3. Generate independently

Give everyone the same neutral prompt, constraints, and fixed time window.
Participants write ideas privately and in parallel before discussion. For each
idea, capture:

- a stable ID and one-sentence statement;
- contributor ID(s) and stage (`independent`, `discussion`, or `post-check`);
- origin (`human`, `AI-assisted`, `literature-inspired`, `mixed`, or `other`);
- assumptions, predicted observations, uncertainties, and possible
  disconfirming evidence;
- source identifiers for literature-inspired ideas and tool/purpose disclosure
  for AI assistance.

Do not show example solutions before this round unless examples are necessary;
if they are, record them as potential anchors.

### 4. Share without immediate evaluation

Use round-robin or pooled silent sharing. Clarify wording without advocacy.
Permit a private or anonymous channel. Ask each participant what is missing,
what contradicts the dominant framing, and which idea became less obvious
after hearing the group.

### 5. Cluster structurally

Group ideas by an explicit relation such as shared outcome, mechanism,
population, scale, or method. Keep original IDs and text. Record merges and
splits. Similar wording is not proof of semantic equivalence; retain distinct
ideas when their assumptions, intervention, population, or predictions differ.
See `references/facilitation_workflows.md`.

### 6. Define transparent criteria

Before rating, define each criterion, direction, scale anchors, evidence
needed, conflicts, and explicit weights. Common dimensions include:

- potential information gain and discriminating predictions;
- relevance to the scoped question;
- originality relative to the checked literature, not merely to the room;
- feasibility, resources, and reversibility;
- methodological rigor and vulnerability to bias;
- ethics, safety, equity, dual-use, and regulatory burden;
- value if the result is null or contradicts the f