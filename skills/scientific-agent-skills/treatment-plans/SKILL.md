---
name: treatment-plans
description: Formats and structurally validates local treatment-plan documentation after clinical decisions have already been supplied and verified by authorized licensed professionals. Use for source traceability, clinician-authored intervention records, goals and checkpoints, shared-decision records, reconciliation handoffs, and release gates—not for clinical decision-making.
license: MIT
compatibility: Python 3.11+ standard library; local JSON files only. Bundled CLIs require no network, external services, models, images, credentials, environment variables, or third-party packages.
metadata: 
  version: 2.4
  last-reviewed: 2026-10-01
  skill-author: K-Dense Inc.
---

# Treatment-Plan Documentation

## Hard safety boundary

This skill only **formats and validates documentation of decisions already made, supplied, and verified by authorized licensed professionals**.

Never use it to:

- diagnose, assess, classify, or screen a person;
- select, rank, recommend, substitute, or compare therapies;
- choose a medication, dose, route, frequency, duration, or monitoring threshold;
- start, stop, hold, resume, titrate, taper, or deprescribe anything;
- check interactions, allergies, contraindications, organ-function suitability, or treatment eligibility;
- infer missing clinical content, intervals, dates, targets, escalation criteria, or instructions;
- triage, determine urgency, provide emergency advice, or create a safety plan;
- predict outcomes, prognosis, response, benefit, harm, or clinical appropriateness;
- replace medication reconciliation, pharmacist review, informed consent, clinician review, or an authorized clinical system;
- claim FDA approval, HIPAA compliance, legal compliance, completeness of care, clinical safety, or standard-of-care conformity.

If a request crosses a boundary, stop. Ask for a locally verified clinician-authored record or route the matter to the responsible licensed professional. Do not redirect to another skill to obtain a patient-specific recommendation.

If a concern may be urgent or emergent, stop this workflow and route it through the institution's current clinical escalation or emergency process. This skill does not decide urgency and does not provide emergency instructions.

## Required visible notice

Every component and derived schedule must display:

> **DRAFT — NOT MEDICAL ADVICE — DOCUMENTATION-ONLY — AUTHORIZED CLINICIAN SIGN-OFF REQUIRED**

Structural success never removes this notice. Only the authorized local workflow may set the release gate.

## Data gate

Prefer synthetic or qualified de-identified structured manifests. Do not place patient names, medical-record numbers, contact details, dates of birth, addresses, free-text notes, images, or other direct identifiers in examples.

For any real-patient or patient-derived data:

1. Work only in a locally authorized environment under the institution's current privacy, security, retention, and access policies.
2. Use the minimum information necessary for the documented purpose, even when a legal exception may apply.
3. Do not send content to a model, search engine, API, image service, telemetry service, or any other external tool.
4. Do not copy content into chat prompts, command history, logs, test fixtures, examples, screenshots, or reports.
5. Run bundled scripts only against local paths. Their reports identify rule codes and field paths, not clinical values.
6. Require qualified privacy review before treating patient-derived material as de-identified or releasing it.

If these conditions are not documented, do not read or process the content. Use synthetic templates only.

## Allowed inputs

Accept only bounded UTF-8 JSON objects built from these generic templates:

- `assets/source_fact_manifest_template.json`
- `assets/clinician_authored_intervention_template.json`
- `assets/goals_monitoring_checkpoint_template.json`
- `assets/informed_preference_shared_decision_template.json`
- `assets/transition_reconciliation_template.json`
- `assets/intended_use_handoff_template.json`

The templates contain no disease-specific recommendations, example patients, clinical intervals, doses, targets, thresholds, or inferred care pathways. Empty template arrays and pending attestations are intentional release blockers.

## Workflow

### 1. Establish authority and intended use

- Confirm the accountable clinical owner and authorized licensed signatory.
- Confirm that every clinical decision already exists in a verified local source.
- Record jurisdiction, institution, setting, document owner, local policies, retention rule, and intended recipients.
- Record whether the package is synthetic, qualified de-identified, or real-patient minimum-necessary data.
- Keep the release gate `blocked` until every required review is complete.

Read `references/safety_scope.md` and `references/privacy_governance.md` before processing patient-derived material.

### 2. Generate a generic package

Run the following commands from the skill directory (`skills/treatment-plans` in this repository). The generated files are JSON, with no PDF, LaTeX, or rendered clinical plan.

```bash
python3 scripts/generate_template.py \
  --output-dir ./local-plan-package \
  --subject-ref SYNTHETIC-CASE-001 \
  --classification synthetic
```

The generator copies all six templates. It does not create clinical content and does not overwrite existing files.

### 3. Transcribe supplied decisions without inference

- Copy only clinician-authored facts and interventions from verified local sources.
- Preserve source locators, versions/dates, author role, verification role, and verification time.
- Record goals, monitoring items, checkpoint dates, and transition dates exactly as supplied.
- Record options, benefits, harms, uncertainty, preferences, and the outcome only as documented by the responsible clinician.
- Leave missing fields unresolved. Never fill them from general knowledge.
- For medication content, record the clinician-authored text and current local source references; do not interpret or validate it.

See `references/documentation_workflow.md`, `references/source_boundaries.md`, and `references/shared_decision_handoff.md`.

### 4. Run deterministic local checks

From the skill directory:

```bash
python3 scripts/validate_treatment_plan.py ./local-plan-package
python3 scripts/validate_traceability.py ./l