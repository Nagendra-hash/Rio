"""
orchestrator/agents/coding_agent.py — the real coding agent (Phase 5).

Replaces the Phase-1 stub under the SAME registry name ("coding_agent") —
the router, contracts and orchestrator are untouched (same convention the
rag_agent set in Phase 2). Enabled with JARVIS_CODING_AGENT=on (shadow-mode
by default, like every other phase).

WORKFLOW (fixed, checkpointed, honest)

    REQUEST → INSPECT → DOCS → PLAN → CHANGE → TEST → VERIFY → REPORT

    REQUEST   resolve the repo (metadata.repo_root, or one attachment that
              is a directory) and the run's knobs; or RESUME a checkpoint.
    INSPECT   repo tree, languages, git state, detected test command.
    DOCS      only when asked (metadata.use_docs): one RAG query for
              documentation evidence — scoped to this agent's caller grant
              in config/rag_callers.json; a blind caller degrades to "no
              docs", never to a failure.
    PLAN      the LLM proposes a JSON plan (edit/create/run steps). Every
              step is validated against the tool permissions BEFORE it
              runs; denied steps are dropped and reported, steps that need
              a human (big deletions, file deletes) park the task in
              NEEDS_APPROVAL.
    CHANGE    apply the validated steps through the toolbox (guarded,
              undo-registered, secret-masked).
    TEST      run the repo's tests (pytest default). On failure the agent
              understands the error (classify + traceback location), asks
              for fix steps, and re-tests — up to MAX_FIX_ROUNDS.
    VERIFY    tests green + read-only git diff review + secret re-scan of
              the change set. No tests configured → verified honestly as
              "change applied, nothing ran" (never faked as green).
    REPORT    deterministic change summary, test verdict, rollback info
              (checkpoint path + per-file before-sha + the shared undo
              stack), stage trace. `metadata.explain` adds one LLM
              paragraph on top of the deterministic summary.

SAFETY (contract from the phase brief)
    * never delete large areas without approval  → TOOL gate → NEEDS_APPROVAL
    * never expose secrets                       → mask_secrets on every
      output; secret-shaped files unreadable and unwritable
    * never push to a remote                     → git push is on the NEVER
      list at the permission layer; the agent cannot reach it even if the
      plan asks
    * change summary + rollback info always kept → checkpoint JSON per task
      under memory/coding_agent/ plus core/undo.py entries per file edit

THE MODEL LAYER
    LLM calls go through the Model Router (orchestrator/model_router) with
    ModelTask.SIMPLE_CODING; if the router cannot serve (nothing
    configured/healthy) it falls back to core/gemini's SMART ladder — the
    same house ladder every other surface uses. Both are injectable
    (llm=...) so tests and the offline demo run with a scripted backend and
    no network at all.

`handle()` NEVER raises. Every failure is an AgentResult(FAILED) plus a
checkpoint the task can resume from.
"""
from __future__ import annotations

import json
import os
import re
import time
import traceback as _tb
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from pathlib import Path
from typing import Any, Callable, Optional

from orchestrator.agents.coding_tools import (
    LARGE_DELETE_LINES,
    ToolPermissions,
    change_record,
    classify_error,
    missing_module,
    parse_traceback,
)
from orchestrator.agents import android_support
from orchestrator.base import BaseAgent
from orchestrator.contracts import AgentRequest, AgentResult, TaskStatus, TaskType

# ── tunables ─────────────────────────────────────────────────────────────────
MAX_FIX_ROUNDS      = 3       # fix→test cycles after the first failure
MAX_INSPECT_CHARS   = 3000    # repo snapshot budget inside the plan prompt
MAX_ERROR_CHARS     = 4000    # failing-output budget inside the fix prompt
MAX_STORED_TEXT     = 32_000  # per-field cap inside a checkpoint
TEST_TIMEOUT_S      = 120
ANDROID_TIMEOUT_S   = 900     # a cold gradle build downloads ~200 MB of deps
STAGES              = ("request", "inspect", "docs", "plan",
                       "change", "test", "verify", "report")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def checkpoint_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Writable checkpoints dir, relocated by JARVIS_HOME like every other
    writable path (memory/config_manager is the canonical BASE_DIR)."""
    if explicit:
        return Path(explicit)
    try:
        from memory.config_manager import BASE_DIR
        base = Path(BASE_DIR)
    except Exception:
        home = os.environ.get("JARVIS_HOME", "").strip()
        if home:
            base = Path(home)
        elif getattr(__import__("sys"), "frozen", False):
            base = Path(__import__("sys").executable).parent
        else:
            base = Path(__file__).resolve().parents[2]
    return base / "memory" / "coding_agent"


def _safe_name(task_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", str(task_id))[:120]


def load_checkpoint(task_id: str,
                    cdir: str | os.PathLike | None = None) -> dict | None:
    path = checkpoint_dir(cdir) / f"{_safe_name(task_id)}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_checkpoint(cp: dict, cdir: str | os.PathLike | None = None) -> Path:
    d = checkpoint_dir(cdir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_safe_name(cp['task_id'])}.json"
    cp["updated_at"] = _now()
    path.write_text(json.dumps(cp, indent=1, default=str)[:512_000],
                    encoding="utf-8")
    return path


def rollback_checkpoint(task_id: str,
                        cdir: str | os.PathLike | None = None) -> str:
    """Undo every change recorded in a task's checkpoint, newest first.

    This is the documented rollback path for the deliverable's 'rollback
    information' requirement — per-task and independent of the shared undo
    stack (which only holds the most recent MAX_DEPTH edits)."""
    cp = load_checkpoint(task_id, cdir)
    if not cp:
        return f"No checkpoint for '{task_id}' — nothing to roll back."
    # Paths inside a checkpoint are repo-relative (as displayed); rollback
    # resolves them against the repo the task ran in, wherever we are now.
    repo_root = Path(cp.get("request", {}).get("repo_root") or ".").resolve()

    def _target(rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else repo_root / p

    undone, skipped, failed = [], [], []
    for rec in reversed(cp.get("changes") or []):
        path = rec.get("path", "")
        if rec.get("kind") == "run":
            continue                          # nothing on disk to restore
        if rec.get("kind") == "scaffold" and rec.get("before") is None:
            # A scaffold file created fresh for a new project — remove it.
            try:
                _target(path).unlink(missing_ok=True)
                undone.append(path)
            except Exception as exc:
                failed.append(f"{path}: {exc}")
            continue
        if not rec.get("before_stored", True) and rec.get("kind") == "edit":
            skipped.append(path)              # too large to store; sha only
            continue
        try:
            target = _target(path)
            if rec.get("kind") == "create":
                target.unlink(missing_ok=True)
            elif rec.get("before") is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(rec["before"], encoding="utf-8")
            else:
                continue
            undone.append(path)
        except Exception as exc:
            failed.append(f"{path}: {exc}")
    cp["status"] = "rolled_back"
    try:
        save_checkpoint(cp, cdir)
    except Exception:
        pass
    out = (f"Rolled back {len(undone)} file(s): "
           + (", ".join(undone) if undone else "nothing"))
    if skipped:
        out += (f"; {len(skipped)} file(s) too large to store — restore "
                f"them from git (recorded sha covers the before-state)")
    if failed:
        out += f"; {len(failed)} failed ({'; '.join(failed)[:200]})"
    return out


def _clip(text: str | None, cap: int = MAX_STORED_TEXT) -> str | None:
    if text is None:
        return None
    return text if len(text) <= cap else text[:cap] + "\n… (truncated)"


def _extract_json(raw: str) -> dict | None:
    """Tolerant JSON extraction (fences/prose tolerated, like gemini.as_json)."""
    if not raw:
        return None
    raw = raw.strip()
    if "{" in raw and "}" in raw:
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ── the LLM layer ────────────────────────────────────────────────────────────
class LLMBackend:
    """Protocol: complete(prompt, *, system="") -> str | None."""
    def complete(self, prompt: str, *, system: str = "") -> Optional[str]:
        raise NotImplementedError


class RouterLLM(LLMBackend):
    """Model Router ONLY for SIMPLE_CODING — no Gemini fallback.
    The router's allow-list (ollama, nvidia) and cooldown/fallback logic
    are the single source of truth. If the router cannot serve, we fail
    honestly rather than silently sending code to a cloud model."""

    def __init__(self) -> None:
        self._router: Any = None

    def complete(self, prompt: str, *, system: str = "") -> Optional[str]:
        try:
            from orchestrator.model_router import (ModelRouter, ModelTask,
                                                   RouteOptions)
            if self._router is None:
                self._router = ModelRouter()
            res = self._router.route(
                prompt, RouteOptions(task=ModelTask.SIMPLE_CODING,
                                     system=system, timeout_s=90,
                                     deadline_s=120))
            if res.ok and res.text:
                return res.text
            # Router couldn't serve — fail honestly. No silent Gemini fallback.
            return None
        except Exception:
            return None

    def spec(self, prompt: str, *, system: str = "") -> Optional[str]:
        """Write an app spec / prompt — deliberately Gemini's SMART tier, NOT
        the cheapest-capable coding model the router would pick. The whole
        point of this stage is a *better* request than the user's one-liner,
        so paying for the reasoning tier here buys a richer app. Returns None
        (never raises) when Gemini is unavailable — the caller then plans from
        the raw request, exactly as before."""
        try:
            from core import gemini
            text = gemini.text(f"{system}\n\n{prompt}".strip(),
                               tier=gemini.SMART, timeout_ms=90_000)
            return text or None
        except Exception:
            return None


# ── prompts ──────────────────────────────────────────────────────────────────
PLAN_SYSTEM = (
    "You are the JARVIS coding agent's planner. You propose MINIMAL, "
    "reversible changes. You never delete large blocks, never touch "
    "credentials, never run git push. You answer with ONE JSON object only."
)

PLAN_PROMPT = """Plan the changes needed for this coding request.

REQUEST:
{request}

REPO SNAPSHOT:
{snapshot}

{docs}

REPLY WITH ONE JSON OBJECT, NOTHING ELSE:
{{
  "analysis": "what is wrong / what is needed (1-3 sentences)",
  "steps": [
    {{"action": "edit",        "path": "rel/path.py", "old": "<exact text from the file>", "new": "<replacement>", "why": "..."}},
    {{"action": "create",      "path": "rel/path.py", "content": "<full file>", "why": "..."}},
    {{"action": "run_python",  "path": "rel/script.py", "why": "..."}},
    {{"action": "run_tests",   "argv": ["pytest", "-q"], "why": "..."}}
  ],
  "tests_to_run": ["pytest", "-q"],
  "risks": ["one line per risk"]
}}

RULES:
- "old" MUST match the file content exactly (copy from the snapshot below).
- Steps run in order; keep the list as short as the work allows.
- Never include credentials; never propose git push/reset/rm.
- If the request is unclear, set "analysis" to explain and leave steps empty.
"""

FIX_PROMPT = """The previous changes did not make the tests pass.

REQUEST:
{request}

FAILING OUTPUT ({error_type}):
{output}

{location}

CHANGES SO FAR (summary):
{changes}

REPLY WITH ONE JSON OBJECT, NOTHING ELSE — the SAME schema as before
("analysis", "steps", "tests_to_run", "risks"). Fix ONLY what the failing
output points at; keep every working behaviour intact.
"""

EXPLAIN_PROMPT = """Explain in 2-3 plain sentences what was changed and why,
for the person who asked. No bullet points, no code fences.

REQUEST:
{request}

CHANGES:
{changes}

TESTS: {tests}
"""

# ── Android prompts ──────────────────────────────────────────────────────────
ANDROID_PLAN_PROMPT = """Plan a complete, BUILDABLE Android app (Kotlin) for
this request. The Gradle build files, AndroidManifest.xml and the base
resources are generated for you AFTER your steps run — do NOT write them.

REQUEST:
{request}
{spec}
APP NAME: {app_name}

{already}
{docs}
REPLY WITH ONE JSON OBJECT, NOTHING ELSE:
{{
  "analysis": "what the app does and how it is structured (1-3 sentences)",
  "package": "com.example.app",
  "entry_activity": "MainActivity",
  "dependencies": ["androidx.room:room-runtime", "androidx.room:room-ktx"],
  "steps": [
    {{"action": "create", "path": "app/src/main/java/com/example/app/MainActivity.kt", "content": "<full Kotlin file>", "why": "..."}},
    {{"action": "create", "path": "app/src/main/res/layout/activity_main.xml", "content": "<full layout XML>", "why": "..."}}
  ],
  "tests_to_run": [],
  "risks": ["one line per risk"]
}}

RULES:
- Build a PROPER, INTERACTIVE app, not a static screen: every control the
  spec names must have a real onClick/TextWatcher/Adapter that changes state
  or navigates. A screen whose only job is to print text is a failure.
- Implement all the screens the spec calls for, with working navigation
  (intents to other activities, or a RecyclerView/ViewPager for in-screen
  lists). Wire real state (e.g. an in-memory list the user adds to).
- Write real, complete Kotlin (no TODOs, no placeholders) and real layout XML.
- EVERY activity and fragment needs its layout XML created in this same plan
  (app/src/main/res/layout/<name>.xml), plus an item layout for any adapter.
  A layout you reference with setContentView/inflate but never write does not
  compile, and the build fails on unresolved references.
- Put every source under the "package" you declare, under
  app/src/main/java/<package as folders>/. Every file must declare the SAME
  package, and no file may live anywhere else (a file written outside
  app/src/main/java is not compiled at all — the app then installs and crashes
  on open). Do NOT invent extra packages per file or per folder.
- Use only androidx/material APIs. Reference strings as @string/name and any
  colour as @color/name — missing ones are auto-defined for you.
- The entry activity MUST be a class ending in "Activity" with onCreate and
  setContentView(R.layout.activity_main).
- Do NOT create: settings.gradle(.kts), build.gradle(.kts),
  gradle.properties, AndroidManifest.xml, strings.xml, colors.xml.
- Keep the file list as short as a working, interactive app allows.
- Never include credentials.
"""

ANDROID_FIX_PROMPT = """The Android build failed. Fix ONLY what the compiler
points at; keep every working behaviour intact.

REQUEST:
{request}

FAILING OUTPUT:
{output}

{location}

CHANGES SO FAR (summary):
{changes}

REPLY WITH ONE JSON OBJECT, NOTHING ELSE — the same schema as the plan
("analysis", "steps", "risks"). Steps may be "edit" or "create" steps on
Kotlin/XML files. Do NOT edit gradle files or AndroidManifest.xml — those are
regenerated by the scaffold; if the problem is a missing @string/@color or a
manifest/R-class mismatch it is fixed for you, you only fix app code.
"""


# ── the agent ────────────────────────────────────────────────────────────────
class CodingAgent(BaseAgent):
    name = "coding_agent"
    description = ("Repository coding tasks: inspect, search, plan, edit, "
                   "run tests, verify with git diff, report with rollback "
                   "info. Approval-gated for deletions; never pushes.")
    task_types = (TaskType.CODING,)

    def __init__(self, llm: LLMBackend | None = None,
                 checkpoint_dir_: str | os.PathLike | None = None,
                 undo_hook: Callable | None = None) -> None:
        self._llm = llm or RouterLLM()
        self._cdir = checkpoint_dir_
        self._undo_hook = undo_hook

    # ── entry ────────────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            return self._handle(request)
        except Exception as exc:                    # never break the router
            try:                                    # keep a trace anyway
                cp = load_checkpoint(request.task_id, self._cdir) or \
                    {"task_id": request.task_id, "status": "failed",
                     "trace": []}
                cp["status"] = "failed"
                cp["error"] = f"{type(exc).__name__}: {exc}"
                cp["traceback"] = _clip(_tb.format_exc())
                save_checkpoint(cp, self._cdir)
                loc = str(checkpoint_dir(self._cdir))
            except Exception:
                loc = ""
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED,
                text=f"[coding_agent] internal error: {exc}",
                data={"checkpoint": loc}, agent=self.name)

    def _handle(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        trace: list[dict] = []

        def mark(stage: str, ok: bool, detail: str = "") -> None:
            trace.append({"stage": stage, "ok": ok,
                          "detail": detail[:160], "at": _now()})

        # ── REQUEST stage ────────────────────────────────────────────────
        approved = bool(meta.get("approved"))
        explain = bool(meta.get("explain"))
        resume_id = str(meta.get("resume") or "").strip()
        cp: dict[str, Any] = {}
        if resume_id:
            cp = load_checkpoint(resume_id, self._cdir) or {}
            if not cp:
                return self._result(request, TaskStatus.FAILED,
                                    f"No checkpoint for '{resume_id}'.",
                                    trace, mark)
            repo_root = cp.get("request", {}).get("repo_root", "")
            plan = cp.get("plan")
            changes = list(cp.get("changes") or [])
            info = cp.get("inspect")
            new_project = cp.get("new_project")
            android_meta = cp.get("android") or {}
            mark("request", True, f"resumed {resume_id}")
        else:
            new_project = None
            android_meta: dict[str, Any] = {}
            explicit_root = str(meta.get("repo_root") or "").strip()
            # A repo_root that exists wins; one that does not is a *target hint*
            # for a brand-new Android app, not an error yet.
            repo_root = explicit_root if (explicit_root and
                                          Path(explicit_root).expanduser().is_dir()) \
                else ""
            if not repo_root:
                for att in request.attachments or []:
                    p = Path(att)
                    if p.is_dir():
                        repo_root = str(p.resolve())
                        break
            if not repo_root:
                # The asker often puts the project path IN the task text
                # ("Build APK for the project at /home/…/ToDoReminderApp").
                # A non-git project folder is a perfectly good workspace —
                # accept any mentioned path that exists on disk.
                import re as _re
                text = request.text or ""
                for cand in _re.findall(r"(?:~/|/home/\w+/|/[\w.-]+)*[\w.-]+"
                                        r"(?:/[\w.-]+)+", text):
                    p = Path(cand).expanduser()
                    if p.is_dir():
                        repo_root = str(p.resolve())
                        break
            # ── NEW ANDROID APP (no existing project) ────────────────────
            # "create an android app …" with no project path: scaffold a
            # brand-new Gradle project on the Desktop instead of asking for a
            # repo. Existing-app verbs (fix/debug/refactor…) do NOT trigger
            # this — see android_support.is_android_request.
            if not repo_root and (
                    meta.get("new_android_app") or meta.get("create_app")
                    or android_support.is_android_request(request.text or "")):
                app_name = android_support.sanitize_app_name(
                    str(meta.get("app_name") or "").strip()
                    or android_support.detect_app_name(request.text or ""))
                target = Path(explicit_root).expanduser() if explicit_root \
                    else android_support.unique_workspace(app_name)
                if target.exists() and not target.is_dir():
                    return self._result(request, TaskStatus.FAILED,
                                        f"Cannot create the app: {target} is "
                                        f"not a directory.", trace, mark)
                created = not target.exists()
                target.mkdir(parents=True, exist_ok=True)
                repo_root = str(target.resolve())
                new_project = {"app_name": app_name, "created": created}
                android_meta = {
                    "app_name": app_name,
                    "dependencies": [str(d) for d in (meta.get("dependencies") or [])],
                }
                mark("request", True, f"new android app '{app_name}' → {target}")
            if not repo_root:
                # Voice asks rarely carry a path ("todo app pe kaam karo").
                # Fuzzy-match the task text against the project folders the
                # dev agent / user actually use; auto-pick ONLY when exactly
                # one folder matches clearly, otherwise ask with the real
                # candidates so the next turn can resolve without guesswork.
                repo_root, candidates = _resolve_repo_by_name(
                    request.text or "")
                if not repo_root and candidates:
                    return self._result(
                        request, TaskStatus.NEEDS_INPUT,
                        "Which project should I work in? I found these: "
                        + ", ".join(candidates)
                        + ". Name one (repo_root) and say what to change.",
                        trace, mark,
                        data_extra={"needs": "repo_root",
                                    "candidates": candidates})
            if not repo_root:
                return self._result(
                    request, TaskStatus.NEEDS_INPUT,
                    "Which repository should I work in? Point me at the "
                    "project folder (repo_root) and say what to change. "
                    "(To create a brand-new Android app, say 'create an "
                    "android app …'.)",
                    trace, mark,
                    data_extra={"needs": "repo_root"})
            plan, changes = None, []
            if not new_project:
                mark("request", True, f"repo={Path(repo_root).name}")

        if not repo_root or not Path(repo_root).is_dir():
            return self._result(request, TaskStatus.FAILED,
                                f"Repository not found: {repo_root!r}",
                                trace, mark)

        toolbox = _toolbox(repo_root, self._undo_hook)
        task = {
            "task_id": request.task_id,
            "request": {"text": request.text,
                        "repo_root": str(repo_root),
                        "use_docs": bool(meta.get("use_docs"))},
            "status": "running", "stage": "request",
            "plan": plan, "changes": changes, "denials": [],
            "test_results": {}, "trace": trace,
            "approved": approved,
            "new_project": new_project,
            "android": android_meta,
        }
        self._store(task, self._cdir)

        # ── INSPECT stage ────────────────────────────────────────────────
        if plan is None:
            info = toolbox.inspect()
            task["inspect"] = info
            mark("inspect", True, f"{info['file_count']} files, git="
                 f"{info['git'].get('is_repo')}")
            self._store(task, self._cdir)
        else:
            info = cp.get("inspect") or toolbox.inspect()
            task["inspect"] = info

        # ── DOCS stage (opt-in; blind-safe) ──────────────────────────────
        docs_text = ""
        docs_meta: dict[str, Any] = {"consulted": False}
        if bool(meta.get("use_docs")) and plan is None:
            docs = self._docs_lookup(request.text)
            docs_meta = {"consulted": docs.get("ok", False),
                         "citations": docs.get("citations") or [],
                         "chars": len(docs.get("evidence") or "")}
            docs_text = docs.get("evidence") or ""
            if docs_text and len(docs_text) > MAX_INSPECT_CHARS:
                docs_text = docs_text[:MAX_INSPECT_CHARS] + "\n[…]"
            mark("docs", docs_meta["consulted"],
                 f"{len(docs_meta['citations'])} citation(s)")
            task["docs"] = docs_meta

        # ── PLAN stage ───────────────────────────────────────────────────
        is_android_task = bool(new_project) or bool(info.get("is_android"))
        spec_text = ""
        if plan is None:
            if is_android_task:
                app_name0 = ((new_project or {}).get("app_name")
                             or android_meta.get("app_name") or "")
                # First WRITE THE PROMPT: ask Gemini for a proper interactive
                # app spec, then plan the app from that spec (not the raw
                # one-liner). Never fatal — no spec just means "plan as before".
                spec_text = self._android_spec(request.text or "", app_name0)
                plan = self._plan_android(
                    request.text or "", toolbox, info, docs_text, app_name0,
                    spec=spec_text)
            else:
                plan = self._plan(request.text or "", toolbox, info, docs_text)
            task["plan"] = plan
            if plan is None:
                mark("plan", False, "no plan (model unavailable or bad JSON)")
                return self._finish(request, task, TaskStatus.FAILED,
                                    "I could not form a plan for this "
                                    "repository (model unavailable or "
                                    "unparseable answer).", mark, toolbox)
            if is_android_task:
                android_meta = {
                    "app_name": android_support.sanitize_app_name(
                        str(plan.get("app_name") or "").strip()
                        or (new_project or {}).get("app_name")
                        or android_meta.get("app_name") or ""),
                    "package": str(plan.get("package") or "").strip(),
                    "entry_activity": str(plan.get("entry_activity") or "").strip(),
                    "dependencies": list(android_meta.get("dependencies") or [])
                    + [str(d) for d in (plan.get("dependencies") or [])
                       if str(d).strip()],
                    "spec": bool(spec_text),
                }
                task["android"] = android_meta
            mark("plan", True, f"{len(plan.get('steps') or [])} step(s)")
            self._store(task, self._cdir)
        else:
            plan = cp.get("plan") or plan
            task["plan"] = plan

        # ── CHANGE stage ─────────────────────────────────────────────────
        need_approval = []
        if not changes:
            for step in plan.get("steps") or []:
                outcome = self._apply_step(toolbox, step, approved=approved)
                if outcome["kind"] == "change":
                    changes.append(outcome["change"])
                    task["changes"] = changes
                    self._store(task, self._cdir)   # crash-safe per step
                elif outcome["kind"] == "approval":
                    need_approval.append(
                        {"path": outcome.get("path", ""),
                         "reason": outcome.get("reason", "")})
                else:
                    task["denials"].append(
                        {"path": outcome.get("path", ""),
                         "reason": outcome.get("reason", ""),
                         "action": step.get("action", "?")})
            task["changes"] = changes
            if need_approval:
                mark("change", False, f"{len(need_approval)} step(s) need "
                                      f"human approval")
                return self._await_approval(request, task, need_approval,
                                            mark, trace)
            # Android: the sources are written — now write the deterministic
            # Gradle skeleton around them (build files, manifest, resources).
            if is_android_task:
                sc_res = toolbox.android_scaffold(
                    android_meta.get("dependencies"),
                    android_meta.get("app_name"),
                    android_meta.get("entry_activity"))
                if sc_res.get("ok"):
                    for entry in sc_res.get("entries") or []:
                        changes.append(change_record(
                            entry["path"], "scaffold",
                            entry.get("before"), entry.get("after")))
                    task["changes"] = changes
                    task["scaffold"] = sc_res.get("note", "")
                    self._store(task, self._cdir)
                    mark("change", True, sc_res.get("note", "scaffold"))
                else:
                    task["scaffold_error"] = sc_res.get("error", "")
                    self._store(task, self._cdir)
                    mark("change", False, f"scaffold failed: "
                                          f"{sc_res.get('error','')}")
            mark("change", True, f"{len(changes)} change(s), "
                 f"{len(task['denials'])} denied step(s)")
            self._store(task, self._cdir)
        else:
            task["changes"] = changes

        # ── TEST stage (+ understand errors → fix rounds) ────────────────
        test_res: dict[str, Any] = {"skipped": True}
        argv = None
        if is_android_task:
            test_res = self._android_test_loop(
                request, toolbox, plan, changes, task, mark, approved,
                need_approval, android_meta, info)
            if isinstance(test_res, AgentResult):
                return test_res                 # parked for approval
        else:
            tests_to_run = plan.get("tests_to_run") or []
            if meta.get("run_tests"):
                tests_to_run = list(meta.get("run_tests"))
            argv = self._tests_argv(tests_to_run, info)
            if argv:
                test_res = toolbox.run_tests(argv, timeout=TEST_TIMEOUT_S)
                task["test_results"] = self._clip_tests(test_res)
                mark("test", bool(test_res.get("passed")),
                     f"{argv[0]} → {'pass' if test_res.get('passed') else 'fail'}")
                self._store(task, self._cdir)

                rounds = 0
                while not test_res.get("passed") and rounds < MAX_FIX_ROUNDS:
                    rounds += 1
                    fix_plan = self._fix_plan(request.text, plan, test_res,
                                              changes, info)
                    if not fix_plan:
                        mark("test", False, f"round {rounds}: no fix available")
                        break
                    fixed = 0
                    for step in fix_plan.get("steps") or []:
                        outcome = self._apply_step(toolbox, step,
                                                   approved=approved)
                        if outcome["kind"] == "change":
                            changes.append(outcome["change"])
                            fixed += 1
                        elif outcome["kind"] == "denied":
                            task["denials"].append(
                                {"path": outcome.get("path", ""),
                                 "reason": outcome.get("reason", ""),
                                 "action": step.get("action", "?"),
                                 "round": rounds})
                        else:
                            need_approval.append(
                                {"path": outcome.get("path", ""),
                                 "reason": outcome.get("reason", ""),
                                 "round": rounds})
                    task["changes"] = changes
                    self._store(task, self._cdir)
                    if need_approval:
                        return self._await_approval(request, task,
                                                    need_approval, mark, trace)
                    if fixed == 0:
                        mark("test", False,
                             f"round {rounds}: fix produced no applicable step")
                        break
                    test_res = toolbox.run_tests(argv, timeout=TEST_TIMEOUT_S)
                    task["test_results"] = self._clip_tests(test_res)
                    mark("test", bool(test_res.get("passed")),
                         f"fix round {rounds} → "
                         f"{'pass' if test_res.get('passed') else 'fail'}")
                    self._store(task, self._cdir)
            else:
                mark("test", False, "no tests configured — verification is "
                                    "diff-review only")
                self._store(task, self._cdir)

        # ── VERIFY stage ─────────────────────────────────────────────────
        diff = toolbox.git_diff() if info.get("git", {}).get("is_repo") else \
            {"ok": False}
        stat = diff.get("stat") or self._tool_diff_stat(changes)
        if is_android_task:
            tests_passed = bool(test_res.get("passed"))
        else:
            tests_passed = bool(test_res.get("passed")) if argv else None
        verify = {
            "tests_passed": tests_passed,
            "changed_files": [c["path"] for c in changes],
            "git_diff_reviewed": bool(diff.get("ok")),
            "secrets_found": sorted({k for c in changes
                                     for k in (c.get("masked_kinds") or [])}),
        }
        if is_android_task:
            apk = test_res.get("apk")
            real = bool(test_res.get("apk_real"))
            desktop_export = (
                toolbox.export_to_desktop(apk, android_meta.get("app_name"))
                if real else
                {"project_dir": str(toolbox.root), "apk": apk, "copied": False,
                 "error": "the build did not produce a working APK"})
            task["desktop_export"] = desktop_export
            verify.update({"android": True, "apk": apk, "apk_real": real,
                           "apk_on_desktop": desktop_export.get("apk_on_desktop"),
                           "desktop": desktop_export})
        task["verify"] = verify
        if is_android_task:
            verify_ok = bool(test_res.get("apk_real"))
        else:
            verify_ok = (tests_passed is True) or (tests_passed is None and
                                                   bool(changes))
        mark("verify", verify_ok,
             f"tests={tests_passed} diff_reviewed="
             f"{verify['git_diff_reviewed']}")
        self._store(task, self._cdir)

        # ── REPORT stage ─────────────────────────────────────────────────
        report = self._report(request.text, changes, argv, test_res, stat,
                              verify, trace, task, toolbox, explain,
                              android_meta if is_android_task else None)
        mark("report", True, "")
        status = (TaskStatus.COMPLETED if verify_ok
                  else TaskStatus.FAILED)
        data_extra: dict[str, Any] = {"tests": verify["tests_passed"]}
        if is_android_task:
            data_extra.update({
                "android": True,
                "app_name": android_meta.get("app_name"),
                "project_dir": str(toolbox.root),
                "apk": verify.get("apk"),
                "apk_on_desktop": verify.get("apk_on_desktop"),
                "spec": bool(android_meta.get("spec")),
            })
        return self._finish(request, task, status, report, mark, toolbox,
                            data_extra=data_extra)

    # ── stage helpers ────────────────────────────────────────────────────
    def _plan(self, request_text: str, toolbox, info: dict,
              docs_text: str) -> dict | None:
        snapshot = json.dumps({
            "root": info.get("root"),
            "languages": info.get("languages"),
            "files": info.get("files", [])[:60],
            "test_command": info.get("test_command"),
            "git": info.get("git"),
        }, indent=1)[:MAX_INSPECT_CHARS]
        prompt = PLAN_PROMPT.format(request=request_text[:2000],
                                    snapshot=snapshot,
                                    docs=(f"DOCUMENTATION EVIDENCE:\n{docs_text}\n"
                                          if docs_text else ""))
        raw = self._llm.complete(prompt, system=PLAN_SYSTEM)
        plan = _extract_json(raw or "")
        if not plan:
            return None
        plan["steps"] = [s for s in (plan.get("steps") or [])
                         if isinstance(s, dict)]
        plan["tests_to_run"] = list(plan.get("tests_to_run") or [])
        plan["risks"] = list(plan.get("risks") or [])
        return plan

    def _android_spec(self, request_text: str, app_name: str) -> str:
        """Ask the design model (Gemini SMART) to WRITE THE PROMPT: a proper
        spec for an interactive app, which the app is then planned from.

        Optional and never fatal: a backend without a `spec` hook (scripted
        tests) or a Gemini outage yields "" and the app is built from the raw
        request exactly as before."""
        fn = getattr(self._llm, "spec", None)
        if fn is None:
            return ""
        try:
            spec = fn(android_support.spec_prompt(request_text, app_name),
                      system=android_support.SPEC_SYSTEM)
            return (spec or "").strip()[:4000]
        except Exception:
            return ""

    def _plan_android(self, request_text: str, toolbox, info: dict,
                      docs_text: str, app_name: str, spec: str = "") -> dict | None:
        """Plan a new (or extended) Android app: the model writes Kotlin/XML
        sources; the Gradle files are the scaffold's job. When `spec` is given
        (the design model's prompt) the plan builds THAT app, not the raw ask."""
        app_name = android_support.sanitize_app_name(app_name or "JarvisApp")
        existing = [t for t in (info.get("files") or [])
                    if t.endswith((".kt", ".java", ".xml"))][:40]
        already = ""
        if existing:
            already = ("EXISTING PROJECT FILES (edit these; don't recreate):\n"
                       + "\n".join(f"  - {t}" for t in existing) + "\n")
        spec_block = ""
        if spec:
            spec_block = ("\nAPP SPEC (written by the design model — build this;"
                          " treat it as the requirements):\n"
                          f"{spec[:3000]}\n")
        prompt = ANDROID_PLAN_PROMPT.format(
            request=request_text[:2000], spec=spec_block, app_name=app_name,
            already=already,
            docs=(f"DOCUMENTATION EVIDENCE:\n{docs_text}\n" if docs_text else ""))
        raw = self._llm.complete(prompt, system=PLAN_SYSTEM)
        plan = _extract_json(raw or "")
        if not plan:
            return None
        plan["steps"] = [s for s in (plan.get("steps") or [])
                         if isinstance(s, dict)]
        plan["tests_to_run"] = list(plan.get("tests_to_run") or [])
        plan["risks"] = list(plan.get("risks") or [])
        plan.setdefault("app_name", app_name)
        plan["dependencies"] = list(plan.get("dependencies") or [])
        return plan

    def _fix_plan(self, request_text: str, plan: dict, test_res: dict,
                  changes: list, info: dict, *, android: bool = False,
                  toolbox=None) -> dict | None:
        output = (test_res.get("stdout") or "") + "\n" + \
                 (test_res.get("stderr") or "")
        err_type = classify_error(output)
        files = [c["path"] for c in changes] or \
            info.get("files", [])[:30]
        if android and toolbox is not None:
            # The pre-change snapshot is empty for a new project — point the
            # traceback matcher at the sources that exist NOW.
            files = list(dict.fromkeys(list(files) + _android_source_files(toolbox)))
        target, line = parse_traceback(output, files)
        location = (f"The error points at {target} line {line}."
                    if target else "")
        if err_type == "dependency":
            # pip is denied at the permission layer, so the plan cannot
            # 'fix' this by installing — the honest fix is a better import.
            location += (f"\nMissing module: {missing_module(output) or '?'} "
                         f"— the agent never installs packages; fix the "
                         f"import or tell the user to install it.")
        change_lines = "\n".join(f"- {c['kind']} {c['path']} "
                                 f"(+{c['lines_added']}/-{c['lines_removed']})"
                                 for c in changes) or "none yet"
        if android:
            prompt = ANDROID_FIX_PROMPT.format(
                request=request_text[:1500], output=output[:MAX_ERROR_CHARS],
                location=location, changes=change_lines)
        else:
            prompt = FIX_PROMPT.format(
                request=request_text[:1500], error_type=err_type or "unknown",
                output=output[:MAX_ERROR_CHARS], location=location,
                changes=change_lines)
        raw = self._llm.complete(prompt, system=PLAN_SYSTEM)
        fix = _extract_json(raw or "")
        if not fix:
            return None
        fix["steps"] = [s for s in (fix.get("steps") or [])
                        if isinstance(s, dict)]
        return fix

    def _android_test_loop(self, request: AgentRequest, toolbox, plan: dict,
                           changes: list, task: dict, mark, approved: bool,
                           need_approval: list, android_meta: dict,
                           info: dict):
        """TEST for an Android project: build the APK, then run Android-aware
        fix rounds (re-scaffold on resource/R-class errors, edit sources on
        compile errors). Returns the final test_res dict, or an AgentResult
        when a fix step needs human approval (the task parks)."""
        app_name = android_meta.get("app_name")
        test_res = toolbox.build_android(timeout=ANDROID_TIMEOUT_S)
        task["test_results"] = self._clip_tests(test_res)
        mark("test", bool(test_res.get("passed")),
             f"gradle assembleDebug → "
             f"{'pass' if test_res.get('passed') else 'fail'}")
        self._store(task, self._cdir)

        rounds = 0
        scaffold_retries = 0
        while not test_res.get("passed") and rounds < MAX_FIX_ROUNDS:
            rounds += 1
            output = (test_res.get("stdout") or "") + "\n" + \
                     (test_res.get("stderr") or "")
            err_type = classify_error(output)
            # A resource-linking / R-class failure is the scaffold's to fix,
            # not the model's — re-scaffold and rebuild, no wasted LLM round.
            if err_type in ("scaffold", "resource") and scaffold_retries < 2:
                scaffold_retries += 1
                sc = toolbox.android_scaffold(android_meta.get("dependencies"),
                                              app_name,
                                              android_meta.get("entry_activity"))
                for entry in sc.get("entries") or []:
                    changes.append(change_record(entry["path"], "scaffold",
                                                 entry.get("before"),
                                                 entry.get("after")))
                task["changes"] = changes
                self._store(task, self._cdir)
                mark("test", False, f"round {rounds}: re-scaffolded ({err_type})")
                test_res = toolbox.build_android(timeout=ANDROID_TIMEOUT_S)
                task["test_results"] = self._clip_tests(test_res)
                mark("test", bool(test_res.get("passed")),
                     f"re-scaffold → "
                     f"{'pass' if test_res.get('passed') else 'fail'}")
                self._store(task, self._cdir)
                continue
            if err_type == "environment":
                mark("test", False, f"round {rounds}: toolchain/environment "
                                    f"error — not fixable by editing code")
                break
            fix_plan = self._fix_plan(request.text, plan, test_res, changes,
                                      info, android=True, toolbox=toolbox)
            if not fix_plan:
                mark("test", False, f"round {rounds}: no fix available")
                break
            fixed = 0
            for step in fix_plan.get("steps") or []:
                outcome = self._apply_step(toolbox, step, approved=approved)
                if outcome["kind"] == "change":
                    changes.append(outcome["change"])
                    fixed += 1
                elif outcome["kind"] == "denied":
                    task["denials"].append(
                        {"path": outcome.get("path", ""),
                         "reason": outcome.get("reason", ""),
                         "action": step.get("action", "?"), "round": rounds})
                else:
                    need_approval.append(
                        {"path": outcome.get("path", ""),
                         "reason": outcome.get("reason", ""), "round": rounds})
            task["changes"] = changes
            self._store(task, self._cdir)
            if need_approval:
                return self._await_approval(request, task, need_approval, mark,
                                            task.get("trace") or [])
            if fixed == 0:
                mark("test", False, f"round {rounds}: fix produced no "
                                    f"applicable step")
                break
            test_res = toolbox.build_android(timeout=ANDROID_TIMEOUT_S)
            task["test_results"] = self._clip_tests(test_res)
            mark("test", bool(test_res.get("passed")),
                 f"fix round {rounds} → "
                 f"{'pass' if test_res.get('passed') else 'fail'}")
            self._store(task, self._cdir)
        return test_res

    def _docs_lookup(self, query: str) -> dict:
        """One RAG query for documentation. The caller grant decides scope;
        a blind (unconfigured) caller gets an empty, honest answer."""
        try:
            from core import rag_service
            return rag_service.answer(caller="coding_agent", query=query)
        except Exception as exc:
            return {"ok": False, "error": f"knowledge base unavailable: {exc}",
                    "evidence": "", "citations": []}

    def _apply_step(self, toolbox, step: dict, *, approved: bool) -> dict:
        """Validate + run ONE plan step. Returns
        {kind: change|approval|denied, ...}. Permissions decide; the agent
        only relays the verdict."""
        action = str(step.get("action") or "").strip().lower()
        path = str(step.get("path") or "")
        if action == "edit":
            ok, reason = toolbox.perms.can_write(path)
            if not ok:
                return {"kind": "denied", "path": path, "reason": reason}
            res = toolbox.edit_file(path, str(step.get("old") or ""),
                                    str(step.get("new") or ""),
                                    force_large=approved)
            if res.get("needs_approval"):
                return {"kind": "approval", "path": path,
                        "reason": res.get("error", "needs approval")}
            if res.get("ok"):
                res["change"]["approved_large"] = (
                    approved and
                    res["change"].get("lines_removed", 0) > LARGE_DELETE_LINES)
                return {"kind": "change", "change": res["change"]}
            return {"kind": "denied", "path": path,
                    "reason": res.get("error", "edit failed")}
        if action == "create":
            ok, reason = toolbox.perms.can_write(path)
            if not ok:
                return {"kind": "denied", "path": path, "reason": reason}
            res = toolbox.create_file(path, str(step.get("content") or ""))
            if res.get("ok"):
                return {"kind": "change", "change": res["change"]}
            return {"kind": "denied", "path": path,
                    "reason": res.get("error", "create failed")}
        if action == "delete":
            if not approved:
                return {"kind": "approval", "path": path,
                        "reason": "deleting a file needs human approval"}
            res = toolbox.delete_file(path, force=True)
            if res.get("ok"):
                return {"kind": "change", "change": res["change"]}
            return {"kind": "denied", "path": path,
                    "reason": res.get("error", "delete failed")}
        if action == "run_python":
            ok, reason = toolbox.perms.can_read(path)
            if not ok:
                return {"kind": "denied", "path": path, "reason": reason}
            res = toolbox.run_python_file(path)
            if not res.get("ok"):
                return {"kind": "denied", "path": path,
                        "reason": res.get("error", "run failed")}
            return {"kind": "change",
                    "change": {"path": path, "kind": "run",
                               "before": None, "after": None,
                               "before_sha": "—", "after_sha": "—",
                               "lines_added": 0, "lines_removed": 0,
                               "run_result": res}}
        if action == "run_tests":
            return {"kind": "denied", "path": path,
                    "reason": "run_tests belongs to the TEST stage, not the "
                              "change set (nothing was run here)"}
        return {"kind": "denied", "path": path,
                "reason": f"unknown step action '{action}'"}

    @staticmethod
    def _tests_argv(tests_to_run: list, info: dict) -> list[str] | None:
        """Turn the plan's tests_to_run (string or list) into ONE argv, or
        the detected default, or None when nothing can run."""
        if isinstance(tests_to_run, str):
            tests_to_run = tests_to_run.split()
        argv = [str(t) for t in tests_to_run if str(t).strip()]
        if not argv:
            detected = info.get("test_command") or ""
            argv = detected.split()
        if not argv:
            return None
        if argv[0].lower() in ("pytest", "py.test"):
            argv[0] = "python"        # always our interpreter, never a PATH hit
            if "-m" not in argv:
                argv = [argv[0], "-m", "pytest"] + argv[1:]
        if argv[0].lower() == "unittest":
            argv = ["python", "-m"] + argv
        return argv

    @staticmethod
    def _clip_tests(res: dict) -> dict:
        out = dict(res)
        for key in ("stdout", "stderr"):
            if key in out:
                out[key] = _clip(out[key], 4000)
        return out

    @staticmethod
    def _tool_diff_stat(changes: list) -> str:
        return "\n".join(f"{c['path']}: +{c['lines_added']}/-{c['lines_removed']}"
                         for c in changes)

    def _await_approval(self, request: AgentRequest, task: dict,
                        need_approval: list, mark, trace: list) -> AgentResult:
        task["status"] = "awaiting_approval"
        task["pending_approval"] = need_approval
        self._store(task, self._cdir)
        names = ", ".join(p.get("path", "?") for p in need_approval)
        return self._result(
            request, TaskStatus.NEEDS_APPROVAL,
            f"This change needs your approval: {names}. "
            f"Approve to let me apply it — everything so far is reversible "
            f"and nothing was deleted.",
            trace, mark,
            data_extra={"approval_summary":
                        f"coding_agent: {len(need_approval)} destructive "
                        f"step(s) on {names}",
                        "pending_approval": need_approval,
                        "resume": task["task_id"]})

    def _report(self, request_text: str, changes: list, argv, test_res,
                stat: str, verify: dict, trace: list, task: dict,
                toolbox, explain: bool = False,
                android_meta: dict | None = None) -> str:
        if android_meta:
            apk = verify.get("apk")
            apk_ok = bool(verify.get("apk_real"))
            desktop_apk = verify.get("apk_on_desktop")
            model_changes = [c for c in changes if c.get("kind") != "scaffold"]
            scaffold_n = len(changes) - len(model_changes)
            verdict = ("APK built and saved on the Desktop"
                       if apk_ok else
                       f"BUILD FAILED after {MAX_FIX_ROUNDS} fix rounds "
                       f"— check the Gradle output")
            body = [f"[CodingAgent] Android app '{android_meta.get('app_name')}'",
                    toolbox.changes_summary(model_changes),
                    f"Project folder: {toolbox.root}"]
            if android_meta.get("spec"):
                body.append("Built from an interactive app spec the design "
                            "model (Gemini) wrote for this request.")
            if scaffold_n:
                body.append(f"Scaffold: generated/refreshed {scaffold_n} "
                            f"Gradle, manifest and resource file(s).")
            if apk_ok and apk:
                body.append(f"APK: {apk}")
                if desktop_apk:
                    body.append(f"Saved on your Desktop: {desktop_apk}")
                body.append("Install on a phone: adb install "
                            f'"{desktop_apk or apk}"')
            else:
                body.append(f"Build status: {verdict}")
            model_stat = self._tool_diff_stat(model_changes)
            if model_stat:
                body.append("Diff review:\n" + model_stat[:2000])
            body.append(
                "Rollback: every file's previous content is in the task "
                "checkpoint; say 'undo' for the last edit, or roll the whole "
                "task back with its task id.")
            explanation = ""
            if explain:
                explanation = self._llm.complete(EXPLAIN_PROMPT.format(
                    request=request_text[:1000],
                    changes=toolbox.changes_summary(model_changes),
                    tests=verdict), system=PLAN_SYSTEM) or ""
            if explanation:
                body.insert(1, explanation.strip())
            return "\n".join(body)

        argv_str = " ".join(argv) if argv else "(no tests detected)"
        if not argv:
            verdict = "no tests configured — nothing was run"
        elif test_res.get("passed"):
            verdict = f"passed ({argv_str})"
        else:
            verdict = f"STILL FAILING after {MAX_FIX_ROUNDS} fix rounds " \
                      f"({argv_str})"
        if verify.get("secrets_found"):
            verdict += (f" — NOTE: credential-shaped strings were redacted "
                        f"({', '.join(verify['secrets_found'])})")
        body = [
            f"[CodingAgent] {toolbox.changes_summary(changes)}",
            f"Tests: {verdict}",
        ]
        if stat:
            body.append("Diff review:\n" + stat[:2000])
        body.append(
            "Rollback: every file's previous content is in the task "
            "checkpoint; say 'undo' for the last edit, or roll the whole "
            "task back with its task id.")
        explanation = ""
        if explain:
            explanation = self._llm.complete(EXPLAIN_PROMPT.format(
                request=request_text[:1000],
                changes=toolbox.changes_summary(changes),
                tests=verdict), system=PLAN_SYSTEM) or ""
        if explanation:
            body.insert(2, explanation.strip())
        return "\n".join(body)

    # ── plumbing ─────────────────────────────────────────────────────────
    def _store(self, task: dict, cdir) -> None:
        task.setdefault("request_meta", {})
        try:
            save_checkpoint(task, cdir)
        except Exception:
            pass

    def _finish(self, request: AgentRequest, task: dict, status: TaskStatus,
                text: str, mark, toolbox, data_extra: dict | None = None):
        task["status"] = ("completed" if status is TaskStatus.COMPLETED
                          else "failed")
        self._store(task, self._cdir)
        loc = str(checkpoint_dir(self._cdir))
        data = {
            "stage_trace": task.get("trace") or [],
            "changes": [self._public_change(c) for c in
                        (task.get("changes") or [])],
            "denials": task.get("denials") or [],
            "tests": task.get("test_results") or {},
            "verify": task.get("verify") or {},
            "checkpoint": loc,
            "checkpoint_task": task["task_id"],
            "rollback": ("checkpoint stores each file's previous content; "
                         f"delete/restore via task id {task['task_id']}"),
        }
        if data_extra:
            data.update(data_extra)
        return AgentResult(task_id=request.task_id, status=status,
                           text=text, data=data, agent=self.name)

    @staticmethod
    def _public_change(c: dict) -> dict:
        """Change record without full file bodies (diff + hashes + undo
        info only) — result payloads stay small."""
        return {k: v for k, v in c.items()
                if k not in ("before", "after") and k not in ("run_result",)}

    def _result(self, request: AgentRequest, status: TaskStatus, text: str,
                trace: list, mark, data_extra: dict | None = None):
        mark(status.value if isinstance(status, TaskStatus) else status, True)
        data = {"stage_trace": trace}
        if data_extra:
            data.update(data_extra)
        return AgentResult(task_id=request.task_id, status=status,
                           text=text, data=data, agent=self.name)


def _android_source_files(toolbox) -> list[str]:
    """Kotlin/Java/XML files in the workspace NOW (for traceback matching in a
    new project, whose pre-change snapshot was empty)."""
    try:
        root = toolbox.root
    except Exception:
        return []
    out: list[str] = []
    for pat in ("app/src/main/java/**/*.kt", "app/src/main/java/**/*.java",
                "app/src/main/**/*.xml"):
        for p in sorted(root.glob(pat)):
            try:
                out.append(str(p.relative_to(root)))
            except Exception:
                continue
    return out


def _resolve_repo_by_name(text: str) -> tuple[str, list[str]]:
    """Fuzzy-resolve a project folder from the task text.

    Voice asks say "todo app pe kaam karo" — no path. This scans the folders
    projects actually live in (the dev agent's JarvisProjects first) and
    matches by name tokens. Returns (repo_root, candidates):
      repo_root  -> '' unless exactly ONE folder matches clearly (never a
                   guessed pick between equals - that edits the wrong code),
      candidates -> the matched names when ambiguous, so the NEEDS_INPUT
                   question lists real options instead of demanding a path.
    Never raises; everything is best-effort."""
    import os as _os
    low = " ".join((text or "").lower().split())

    roots = [Path.home() / "Desktop" / "JarvisProjects",
             Path.home() / "Desktop",
             Path.home() / "projects",
             Path.home() / "code"]
    scored: dict = {}
    try:
        for root in roots:
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                # name -> tokens, split on separators AND camelCase, dropped
                # generic words that match everything
                raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", entry.name)
                tokens = {t for t in re.split(r"[\s_\-.,()]+", raw.lower())
                          if len(t) >= 3
                          and t not in ("app", "the", "and", "project",
                                        "test", "demo", "new")}
                if not tokens:
                    continue
                flat = low.replace("-", " ").replace("_", " ")
                score = sum(1 for t in tokens
                            if t in flat or t.replace(" ", "")
                            in flat.replace(" ", ""))
                # full-name containment beats token noise: "ToDoReminderApp"
                # camel-splits into fragments that score 1 while its compact
                # name sits verbatim in the text
                compact_name = re.sub(r"[\s_\-]+", "", raw.lower())
                compact_text = flat.replace(" ", "")
                if len(compact_name) >= 6 and compact_name in compact_text:
                    score += 2
                if score:
                    scored[str(entry.resolve())] = score
    except Exception:
        return "", []

    if not scored:
        return "", []
    best = max(scored.values())
    winners = [p for p, s in scored.items() if s == best]
    if best >= 3 and len(winners) == 1:
        return winners[0], []
    # ambiguous or a single weak token - offer the strongest few, honestly
    ranked = sorted(scored, key=lambda p: -scored[p])[:4]
    return "", [_os.path.basename(p) for p in ranked]


def _toolbox(repo_root, undo_hook) -> "CodingToolbox":
    from orchestrator.agents.coding_tools import CodingToolbox
    return CodingToolbox(repo_root, undo_hook=undo_hook)


if TYPE_CHECKING:  # type-check-only name for the deferred annotation above
    from orchestrator.agents.coding_tools import CodingToolbox
