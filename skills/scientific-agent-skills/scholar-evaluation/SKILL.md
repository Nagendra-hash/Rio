---
name: scholar-evaluation
description: Provides qualitative-first, evidence-traceable developmental review of scholarly works and audit low-stakes research-assessment rubrics with optional local quality controls. Never use for ranking people or consequential decisions.
license: MIT
compatibility: Requires Python 3.11+ for optional bundled standard-library CLIs. All tooling is local JSON/CSV processing with no network, credentials, external models, or subprocesses.
allowed-tools: Read Write Bash Glob Python
metadata: 
  version: 2.4
  last-reviewed: 2026-10-01
  skill-author: K-Dense Inc.
---

# Scholar Evaluation

## Purpose

Provide developmental, evidence-traceable feedback on a **scholarly work**:
paper, draft, protocol, literature synthesis, or research idea. Use
qualitative judgment first. Optional scores only describe how submitted
evidence maps to a predeclared bounded rubric.

This skill also audits whether a low-stakes assessment process documents its
construct, provenance, rater quality, uncertainty, traceability, sensitivity,
fairness, accessibility, privacy, and human governance.

## Hard safety boundary

Never use this skill to automate, recommend, materially influence, or score:

- hiring, promotion, or tenure;
- admissions;
- grants or other funding;
- prizes, honors, or awards;
- discipline, dismissal, or sanctions; or
- any other high-impact personnel decision.

Never rank people. Never reduce a person to a composite score. Never infer
ability, character, integrity, protected traits, future performance, or worth.
A nominal human-in-the-loop does not remove this boundary.

If asked for a prohibited use, stop. Offer developmental comments on a
scholarly work or a process-only audit that does not process applications,
compare people, recommend an outcome, or advise a decision.

Do not issue publication-readiness, accept/reject, or “top-tier” judgments.

Read `references/responsible_assessment.md` before any organizational use.

## ScholarEval status

The referenced ScholarEval project is an **experimental
literature-grounded research-idea evaluation framework**, not validated
psychometrics.

The verified primary record is Moussa et al., *ScholarEval: Research Idea
Evaluation Grounded in Literature*, arXiv:2510.16234v2, revised 2026-02-28.
It reports a retrieval-augmented soundness/contribution framework, a
117-idea four-discipline dataset, coverage experiments, and a user study.

Do not generalize those results to person assessment, consequential decisions,
all disciplines, or this skill's rubric. No peer-reviewed publication status
was verified during the dated review. See `references/source_ledger.md`.

## Metric and prestige policy

Do not score or infer quality from:

- Journal Impact Factor or other journal measures;
- h-index, publication counts, or citation counts;
- altmetrics or attention;
- journal, conference, venue, institution, employer, or geographic prestige;
- author affiliation, reputation, network, or career path.

The rubric validator rejects common proxy-measure criteria.

If a qualified reviewer mentions an indicator descriptively outside the
scoring tools, record its exact purpose, source, coverage, field and time
effects, uncertainty, missingness, biases, gaming risk, and why it does not
directly measure quality. Never hide indicators inside an opaque composite.

## Data boundary

Bundled scripts accept only strict local JSON/CSV containing pseudonymous IDs,
bounded ratings, statuses, uncertainty, and local references.

Do not put raw private applications, CVs, letters, reviewer identities,
contact details, protected attributes, or source-document text in inputs,
outputs, logs, examples, or prompts. Keep source content in the authorized
records system and use opaque local references.

Allowed classifications are:

- `synthetic`
- `public_scholarly_work`
- `deidentified_low_stakes`

No script searches the web, loads environment files, reads credentials, calls a
model, executes supplied text, deserializes executable objects, or launches a
process.

Use Bash only to invoke the documented local `python3` commands.

## Workflow

### 1. Confirm allowed use and authorization

Record:

- developmental purpose;
- unit of assessment: `scholarly_work`;
- work type, stage, discipline, language, and audience;
- authorized source location and data classification;
- accountable committee owner;
- conflicts and recusals;
- accessibility and accommodation process;
- appeal or correction route; and
- data purpose, access, retention, and deletion.

Stop on a prohibited decision context or unnecessary private data.

### 2. Define the construct before criteria

State:

- what quality or support is being examined;
- excluded constructs;
- intended interpretation;
- contexts where the interpretation does not travel;
- evidence requirements; and
- known limitations.

Start with values and disciplinary context, not available metrics.

### 3. Adapt and validate the rubric

Begin with `assets/rubric_template.json`, then obtain qualified disciplinary,
assessment-methods, stakeholder, accessibility, privacy, and fairness review.

The template deliberately records content validity as `not_established`.
Do not change that status without documented evidence for the exact intended
use.

Validate structure:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_rubric.py \
  --rubric assets/rubric_template.json
```

Read `references/evaluation_framework.md` for construct, anchor, validity, and
rater guidance.

### 4. Build traceable evidence records

Reviewers may read an authorized work outside the scripts. Record only stable
local locators and claim references in
`assets/evidence_manifest_template.json`.

For every criterion, distinguish:

- observed evidence from interpretation;
- supporting from contrary evidence;
- available from unavailable evidence;
- `missing` from `not_applicable`; and
- uncertainty from absence.

Failure to find prior work does not prove novelty. Freeze the exact work revision and evidence-access date before independent rating so raters assess the same material. For public papers, check publisher correction/retraction notices and [Crossmark](https://www.crossref.org/services/crossmark/) where available; record unresolved status rather than treating absence of a notice as verification. If the work changes materially, issue a new evaluation lin