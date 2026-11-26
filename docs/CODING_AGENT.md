# Phase 5 — Coding Agent

> Companion to `ARCHITECTURE.md` and `MIGRATION_PLAN.md`.
> Status: **Built** · Agent: `orchestrator/agents/coding_agent.py` · Tools: `orchestrator/agents/coding_tools.py`

---

## 1. What it is

The coding agent is the **real Phase-2-style replacement** of the Phase-1
`coding_agent` stub, on the Jarvis Agent interface
(`orchestrator/base.py::BaseAgent`) — same registry name, same contracts,
same router. It implements, as code (not prompt hope):

| Capability | Where |
|---|---|
| Inspect repositories | `CodingToolbox.inspect()` — tree (bounded), languages, git state, detected test command |
| Read files | `read_file()` — line-numbered, windowed, bounded, secret-masked |
| Search code | `search()` — regex across text files, bounded hits, masked |
| Understand errors | `classify_error()` + `parse_traceback()` + `missing_module()` — same vocabulary as `actions/dev_agent.py` |
| Plan changes | one LLM call returning strict JSON; **validated step-by-step against permissions before anything runs** |
| Edit files | exact-match single replacement per step; create; delete (always approval-gated) |
| Run tests | `run_tests()` — python/pytest/unittest allowlist, hard timeout, masked + capped output |
| Run Python where appropriate | `run_python_file()` — repo-internal files only, no `-c` |
| Use Git safely | read-only subcommands only (`status/diff/log/show/rev-parse/branch/ls-files`); `add/commit/...` need approval; `push/pull/reset/clean/rebase` **cannot run at all** |
| Review diffs | `git_diff()` for repos, deterministic per-file diff records otherwise |
| Verify changes | tests green + diff review + secret re-scan of the change set; "no tests configured" is reported honestly, never faked green |
| Explain what changed | deterministic change summary always; optional LLM paragraph with `metadata.explain=true` |
| **Create Android apps** | `metadata.new_android_app=true` (or a "create an android app …" request): scaffolds a Gradle project, builds an APK, and saves the project folder + APK on the Desktop — see §8 |

**Disabled by default** (shadow-mode convention): set `JARVIS_CODING_AGENT=on`
to register the real agent instead of the stub.

```python
from orchestrator.agents import build_default_registry
reg = build_default_registry()          # stub unless the env flag is on
agent = reg.get("coding_agent")
```

## 2. Workflow

```
REQUEST → INSPECT → DOCS(opt) → PLAN → CHANGE → TEST → VERIFY → REPORT
```

1. **REQUEST** — resolve `repo_root` (metadata, or a directory attachment),
   or **resume** a checkpoint (`metadata.resume = <task_id>`).
2. **INSPECT** — repo snapshot for planning (files, languages, git dirty
   state, detected test command).
3. **DOCS** *(opt-in via `metadata.use_docs=true`)* — one RAG query for
   documentation evidence through `core/rag_service` as caller
   `coding_agent` (scope via `config/rag_callers.json`; a blind caller
   degrades to "no docs", never an error).
4. **PLAN** — `PLAN_PROMPT` → strict JSON. The model never acts; it only
   proposes `edit/create/delete/run_python/run_tests` steps which the
   permission layer then individually allows, denies, or parks for approval.
5. **CHANGE** — steps applied through the toolbox; each applied step is
   checkpointed immediately (crash-safe). Denied steps are **reported,
   not silent**.
6. **TEST** — plan's `tests_to_run` or the detected default
   (`pytest -q` / `python -m unittest discover`). On failure:
   classify error → locate the deepest frame in a project file → one fix
   round → re-test, up to `MAX_FIX_ROUNDS = 3`.
7. **VERIFY** — tests passed + diff review (`git diff --stat`) + secret
   scan of the change set. No test command ⇒ verify = "change applied,
   nothing ran".
8. **REPORT** — deterministic summary (files, ±lines, sha before→after),
   test verdict, rollback information, stage trace. Statuses:
   `COMPLETED` (tests green or none configured + changes applied),
   `FAILED` (still failing / no plan / internal error),
   `NEEDS_APPROVAL` (destructive step), `NEEDS_INPUT` (no repo).

Every stage transition is journaled into the task **checkpoint** at
`memory/coding_agent/<task_id>.json` (relocated by `JARVIS_HOME` like every
writable path).

## 3. Tool permissions (`coding_tools.ToolPermissions`)

Four tiers; every tool call consults them first:

| Tier | Covers | Enforcement |
|---|---|---|
| READ | tree, file reads, search, read-only git | denied for secret-shaped files |
| WRITE | create/edit **inside** the repo root | realpath containment (symlink/`..`-proof), `.git/` never, binary formats never, secret files never |
| EXECUTE | `python`/`python3` tests & project files, a guarded `gradle assembleDebug/…` Android build, read-only git | denylist first (`rm`, `sudo`, `curl`, `pip`, shells…), `python -c` refused, `-m` allowlist (`pytest/unittest/compileall`), gradle task/flag allowlist (`assembleDebug`/`assembleRelease`/`clean` only), every argv element path-checked |
| DANGEROUS | file deletion, >60-line deletions, git-mutating subcommands | surfaced as `NEEDS_APPROVAL`; honoured only when the task re-arrives with `metadata.approved=true` (same shape the orchestrator's `ApprovalGate` uses) |

`git push` (and `pull/fetch/remote/reset/rebase/clean/...`) sit in
`GIT_NEVER`: **no code path can execute them**, approved or not — the
"never push to a remote" rule is structural, not behavioural.

### Secrets

- `mask_secrets()` runs over every output (file contents, search hits,
  process stdout/stderr, diffs, reports): Google/OpenAI/GitHub/AWS/Slack
  token shapes, JWTs, bearer headers, `password/secret/token/api_key`
  assignments.
- Secret-shaped **files** (`api_keys.json`, `*.pem`, `id_rsa`,
  `*secret*`, `*token*`, …) are unreadable and unwritable — the agent
  cannot leak what it cannot read.

## 4. Safety deliverables

- **No large deletions without approval** — `LARGE_DELETE_LINES = 60`;
  exceeding it returns `needs_approval`, the change is *not* applied,
  and the task parks in `NEEDS_APPROVAL` with a resume id.
- **Change summary** — every report lists per-file kind, ±lines,
  content sha before→after (shas cover even files too large to embed).
- **Rollback information** — three layers:
  1. shared Jarvis undo stack (`core/undo.py`) for the most recent edits,
  2. per-task checkpoint storing each file's previous content
     (`rollback_checkpoint(task_id)` restores newest-first),
  3. per-file shas for verification against git when bodies were too
     large to store.
- **Model Router integration** — LLM calls go through
  `orchestrator/model_router` with `ModelTask.SIMPLE_CODING`
  (cheapest-capable-first, explained fallbacks); when no provider is
  configured it falls back to the house `core/gemini` SMART ladder, and
  if that fails the task fails honestly (never silently does nothing).
- **RAG integration** — documentation lookups through `core/rag_service`
  with the `coding_agent` caller identity (same ACL pattern as
  `rag_agent`).

## 5. Sample workflow (offline)

```bash
# end-to-end happy path on a disposable sample repo (no network, no key):
.venv/bin/python scripts/coding_agent_demo.py --task edit

# failing → fix loop:
.venv/bin/python scripts/coding_agent_demo.py --task fix

# approval flow (parks in NEEDS_APPROVAL, deletes nothing):
.venv/bin/python scripts/coding_agent_demo.py --task delete

# undo a completed run:
.venv/bin/python scripts/coding_agent_demo.py --task rollback --task-id <id>
```

## 6. Tests

```bash
.venv/bin/python -m pytest tests/test_coding_agent.py -q
```

Coverage: secret masking, permission tiers, path containment, command
denylist/allowlists, approval gating, toolbox operations, error
understanding, the full staged workflow with a scripted LLM (happy path,
create-only, failing-then-fixed, still-failing, denied steps reported,
delete→approve→resume, rollback, resume idempotence, garbage-plan
containment), registry integration, and checkpoint plumbing.

The offline Android suite additionally proves the app-spec stage: the spec is
written *before* planning, its text reaches the coder's plan prompt, a failed or
absent design model is not fatal, and the voice DevAgent writes the same spec
first. It also proves the two "installs but won't open" fixes: a library-only
APK is rejected because the launcher class is missing (a bytes-only check is
shown to be fooled), and stray sources written outside the module are relocated
into it under a single package with their `R`/cross-file/`databinding` imports
re-pointed. Run it with `tests/test_android_coding_agent.py`.

## 7. Files

| File | Role |
|---|---|
| `orchestrator/agents/coding_tools.py` | permissions, secret masking, guarded toolbox, error classification |
| `orchestrator/agents/android_support.py` | Android intent/naming, the design-model spec prompt, the deterministic Gradle scaffold, APK detection + Desktop export |
| `orchestrator/agents/coding_agent.py` | the staged agent, checkpoints, prompts, rollback |
| `orchestrator/agents/__init__.py` | registration behind `JARVIS_CODING_AGENT=on` |
| `tests/test_coding_agent.py` | offline test suite |
| `tests/test_android_coding_agent.py` | offline Android tests (scaffold, permissions, workflow, export) |
| `scripts/coding_agent_demo.py` | offline sample workflow |

## 8. Creating Android apps

Ask the coding agent to **create a new Android app** and it scaffolds a complete,
buildable Gradle project, builds a debug APK, and saves everything on the
Desktop:

```
"create an android app called TaskMaster for tracking my tasks"
   → repo_root = ~/Desktop/TaskMaster/            (the project folder)
   → APK saved to ~/Desktop/TaskMaster/TaskMaster.apk
                and  ~/Desktop/TaskMaster.apk
```

Trigger it with a plain-language request, or explicitly:

| metadata key | meaning |
|---|---|
| `new_android_app: true` | force the new-project flow (any text) |
| `app_name: "TaskMaster"` | the app name / folder (else parsed from the text) |
| `dependencies: [...]` | extra Gradle coordinates (androidx/room/…) to add |
| `repo_root: "~/Desktop/X"` | create the app at an explicit non-existing path |

How it works (and why it is reliable):

1. **Intent** — `android_support.is_android_request()` fires only on a *create*
   verb + an Android signal. "fix my android app" is deliberately excluded: that
   still goes down the inspect-an-existing-repo path.
2. **Workspace** — created at `~/Desktop/<AppName>` (`<App-2>`, `<App-3>`… if the
   name is taken, so nothing is clobbered). `JARVIS_DESKTOP` overrides the
   Desktop root.
3. **Plan** — the model writes the **Kotlin + XML sources only** (`AndroidPlanPrompt`);
   it is told not to write Gradle files.
3a. **Spec first ("write the prompt, then build")** — before planning, the design
   model (**Gemini, SMART tier** — `RouterLLM.spec()`) turns the one-line ask into
   a concrete spec for a *proper interactive* app (purpose, screens,
   interactions, state, must-have behaviour). That spec is what the coder's plan
   prompt builds from, so the result has real screens and click handlers instead
   of one static screen. It is never fatal: if Gemini is unavailable the app is
   planned from the raw request exactly as before (`data.spec` reports which
   happened). The shared wording lives in
   `android_support.SPEC_PROMPT` / `SPEC_SYSTEM`.
4. **Scaffold** — `android_support.scaffold_project()` deterministically writes
   `settings.gradle.kts`, `build.gradle.kts`, `gradle.properties`,
   `local.properties`, `app/build.gradle.kts`, `AndroidManifest.xml` and the base
   resources, detects the package/launcher activity from the sources, and
   auto-defines any `@string`/`@color` the layouts reference. This is the same
   hard-won scaffold `actions/dev_agent.py` uses (it is what fixes "package R
   does not exist").
4a. **Repair wrong source paths** — the model is told to write every source under
   `app/src/main/java/<package>/`, and sometimes writes them elsewhere (at the
   repo root as `ui/`, `data/`…) with a *different* package in each file.
   Gradle then compiles none of them. `relocate_stray_sources()` moves those
   files into the module and forces **one** package — rewriting each file's
   `package` line, its `<x>.R` import and its cross-file/`databinding` imports —
   so they compile and the R class resolves. It also declares every extra
   `*Activity` in the manifest (an undeclared activity compiles but crashes the
   instant a control starts it) and picks the launcher from the plan's
   `entry_activity` (not the alphabetically-first `*Activity`).
5. **Build** — `gradle assembleDebug` (via `gradlew` when present). Success needs
   **both** exit 0 **and** a real APK — and "real" means the app's own launcher
   class is compiled into the dex, not merely that the dex is large. A module
   whose sources never compiled still ships an APK full of *library* code: it
   passes a bytes-only check, installs, and then crashes with
   `ClassNotFoundException` the moment it is opened (observed 2026-10-17 with
   TodoListApp). `apk_looks_real(apk, launcher)` + `apk_has_class()` catch that;
   the failure is classified as a scaffold error so relocation runs and the
   build is honestly reported as failed otherwise.
6. **Fix rounds** — a resource/R-class failure re-scaffolds (no wasted LLM
   round); a compile error is located in the `.kt`/`.java` file and fixed;
   toolchain errors are reported honestly instead of looping.
7. **Verify + save** — the APK is copied to `~/Desktop/<App>.apk` and into the
   project folder, and the paths are reported. Rollback removes a freshly
   created project's files (or restores an existing project's).

Requirements: JDK 17 + gradle + Android SDK build-tools — install once with
`scripts/android_bootstrap.sh`. Without them the agent says so and still leaves
the generated project on the Desktop.

The voice **DevAgent** (`actions/dev_agent.py`) builds Android apps the same way
(`language="android"`) and now also saves the project to `~/Desktop/<App>` with
the APK exported next to it. It writes the **same app spec first**
(`_design_android_spec()`, Gemini SMART): the raw ask becomes an interactive-app
brief, then the planner *and* the per-file writer build from that brief; an
Android-specific block in `_plan_project()` keeps the plan from collapsing to a
single activity. If the spec call fails the original description is used, so the
build is never blocked by it. It also shares the orchestrator's deterministic
repairs (`relocate_stray_sources`, `launcher_activity`, `apk_has_class`) and
refuses to call a library-only APK a success — the same "installs but won't open"
guard.
