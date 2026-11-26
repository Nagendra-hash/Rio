# Phase 9 — Plan-and-Execute Engine

> Companion to `ARCHITECTURE.md` and the agent docs (`CODING_AGENT.md`,
> `RESEARCH_AGENT.md`). Status: **Built** · Schema: `orchestrator/plans.py` ·
> Planner: `orchestrator/planner.py` · Executor:
> `orchestrator/plan_executor.py` · Adapter: `orchestrator/plan_agent.py` ·
> Checkpoints: `orchestrator/checkpoint.py` · Tests:
> `tests/test_plan_engine.py`

---

## 1. What it is

Core orchestration infrastructure that lets Jarvis break a complex request
into small, inspectable steps and execute them sequentially:

```
Goal → Plan → Step 1 → Verify → Step 2 → Verify → … → Final result
```

It is **not another independent agent**: it owns no tools of its own. Every
step delegates to capabilities that already exist —

| Step action | Runs through |
|---|---|
| `agent`   | the `AgentRegistry` + `BaseAgent.handle()` contract (router lanes, not a new loop) |
| `llm`     | the Model Router (`orchestrator.model_router`), house Gemini ladder behind it |
| `shell`   | a read-only allowlist (`ls`, `cat`, `ps`, `df`, `git status/log/diff`, …), off by default (`JARVIS_PLAN_SHELL=on` to enable) |
| `noop`    | a marker / checkpoint step |

## 2. The pieces (deliverables)

| Deliverable | File |
|---|---|
| Plan + step schema, validation, dependency ordering | `orchestrator/plans.py` |
| Planner (goal → plan): LLM planner + heuristic floor | `orchestrator/planner.py` |
| Executor (step execution, retry, verify, progress) | `orchestrator/plan_executor.py` |
| Crash-safe checkpoint store | `orchestrator/checkpoint.py` |
| BaseAgent adapter + approval resolution | `orchestrator/plan_agent.py` |
| Tests (49, offline) | `tests/test_plan_engine.py` |

## 3. Plan schema (the inspectable artifact)

```python
Plan(
  goal="build the widget integration",
  steps=[
    PlanStep(id="s1", title="research the API", action="agent",
             params={"task_type": "research", "text": "find the widget API docs"},
             verification="contains:evidence"),
    PlanStep(id="s2", title="implement the widget", action="agent",
             params={"task_type": "coding", "text": "add the widget"},
             depends_on=["s1"], max_retries=2),
    PlanStep(id="s3", title="deploy to prod", action="noop",
             depends_on=["s2"], requires_approval=True),
  ],
  risk_notes=["touches production"],
)
```

Validation is strict and happens **before anything can run**: allowed
actions only, no duplicate ids, no unknown/self/cyclic dependencies, `max_retries`
clamped to 3, param strings capped, ≤24 steps. `Plan.ordered_steps()` is a
stable topological sort — a reviewed plan runs in the order shown.

## 4. Execution rules

1. **Order** — topological; a step runs only when its dependencies are done.
2. **Approval** — any `requires_approval` step parks the WHOLE plan
   (`awaiting_approval`) before *anything* runs. The returned
   `NEEDS_APPROVAL` result embeds the rendered plan for inspection.
   Approval is explicit per plan (never auto-granted); a deny marks pending
   steps `denied` and nothing executes.
3. **Verify** — per-step checks on the output: `contains:<text>`,
   `re:<regex>`, or non-empty by default. Failing verification burns retry
   budget like any error — a step that "ran" without evidence is a failure.
4. **Retry** — up to the step's `max_retries` (schema-clamped to ≤3).
5. **Fail** — per-step `on_fail`: `abort` (default) skips all remaining
   steps; `skip` drops this step and its dependents; `continue` lets
   dependents run anyway. The plan result is honest: any failed or skipped
   step → plan `FAILED`.
6. **Checkpoint** — `memory/plan_engine/<plan_id>.json` (JARVIS_HOME-aware,
   atomic temp+replace) saved on **every** transition; resume continues from
   the last verified step without re-running done work.
7. **Progress** — every transition emits a `PlanProgress` event
   (`plan_started`, `step_started`, `step_done`, `step_retry`,
   `step_failed`, `awaiting_approval`, `approved`, `plan_completed`,
   `plan_failed`) to the injectable reporter; `JournalReporter` bridges to
   the orchestrator JSONL journal.
8. **Final verification** — success requires every step `done`; the final
   text lists per-step results, failures, and skips. Never faked green.

`execute()` never raises; `agent` steps inherit the router's crash
containment (an agent exception becomes a step error, and the plan's
policies take over). A sub-capability answering `NEEDS_APPROVAL` fails its
step honestly rather than being silently escalated.

## 5. Approval checkpoints

The plan agent speaks the **existing** `ApprovalGate` contract:

- `handle()` on a gated plan → `NEEDS_APPROVAL` + `approval_summary` +
  rendered plan; the orchestrator parks it exactly like any other
  `NEEDS_APPROVAL` result.
- Approve → `metadata={"plan_action": "approve", "plan_id": …}` (or
  `orchestrator.plan_agent.resolve_plan(orch, plan_id, approved=True)`) →
  executor runs with `approved=True`.
- Deny → pending steps become `denied`, plan `failed`, "nothing was run".
- Resolving an already-terminal plan → safe `DECLINED`, never a re-run.

## 6. Registration (shadow-mode convention)

Off by default like every phase:

```bash
JARVIS_PLAN_AGENT=on          # register the plan_agent capability
JARVIS_PLAN_SHELL=on          # (optional) allow allowlisted shell steps
```

The agent's task type is `_FALLBACK` on purpose: it never steals a lane from
a specific agent; it is reached via explicit override
(`metadata={"agent_override": "plan_agent"}`), via `resolve_plan`, or by
direct invocation.

```python
from orchestrator.plan_agent import PlanAgent
from orchestrator.plan_executor import PlanExecutor
from orchestrator.checkpoint import PlanCheckpointStore
agent = PlanAgent(store=PlanCheckpointStore())       # planner built in
agent.executor = PlanExecutor(registry=my_registry)  # or built lazily
```

## 7. Tests

`python -m pytest tests/test_plan_engine.py -q` — 49 tests, offline, no
LLM/network: schema validation + cycles, planner JSON/heuristic fallbacks,
shell allowlist, ordered execution, verification + retry accounting,
on_fail policies, approval park/approve/deny, checkpoint resume, registry
wiring, and never-raises guarantees. (The 7 pre-existing failures in
`tests/test_model_router.py` are unrelated and fail without this change too.)
