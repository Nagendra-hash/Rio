"""
scientific_agent.py — voice/dashboard entry point for the Science Agent.

Same shape as `actions/browser_agent.py`, because the constraints are the same:
the Live receive loop awaits tool calls serially and a silent turn for 30 s
trips the session watchdog. So this handler starts a background job and returns
immediately; the real answer is announced from a worker thread and, when the
turn came from Telegram/WhatsApp, pushed back to that chat with the report file
attached.

It also owns the job lifecycle the agent itself has no business knowing about:
status, cancellation, duplicate suppression, and the voice-level rendering of a
result that may be a long report plus figures.
"""

from __future__ import annotations

import atexit
import hashlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from core import outcome
from core.scientific import delivery, envs, runner
from core.scientific.config import load_config
from core.scientific.safety import screen

DEFAULT_WAIT_S = 25.0


@dataclass
class _Job:
    job_id: str
    task: str
    task_hash: str
    started: float
    done: bool = False
    cancelled: bool = False
    event: threading.Event = field(default_factory=threading.Event)
    result: object | None = None
    target: str = ""
    handbook: bool = False
    thread: threading.Thread | None = None


_LOCK = threading.RLock()
_JOBS: dict[str, _Job] = {}


def _hash(task: str) -> str:
    return hashlib.sha256(" ".join(task.lower().split()).encode()).hexdigest()[:16]


def _target(player) -> str:
    for attr in ("inbound_target", "_inbound_target"):
        value = getattr(player, attr, None) if player is not None else None
        if value:
            return str(value)
    return ""


def _human(result) -> str:
    text = str(getattr(result, "text", "") or "").strip()
    data = getattr(result, "data", {}) or {}
    report_path = data.get("report_path")
    if report_path and Path(str(report_path)).is_file():
        body = Path(str(report_path)).read_text(encoding="utf-8", errors="replace")
        return body[:6000]
    return text or "(no output)"


def _announce(result, player, speak, target: str, task: str) -> None:
    data = getattr(result, "data", {}) or {}
    report_path = data.get("report_path")
    files = [str(report_path)] if report_path and Path(str(report_path)).is_file() else []
    for figure in data.get("figures") or []:
        if Path(str(figure)).is_file():
            files.append(str(figure))

    body = _human(result)
    if player is not None:
        try:
            player.write_log(f"[science] {getattr(result, 'status', '')} — {task[:70]}")
        except Exception:
            pass
        try:
            player.show_content("Scientific research", body[:4000])
        except Exception:
            pass

    if target:
        delivery.deliver(target, str(getattr(result, "text", "") or body)[:3000], files=files)

    if callable(speak):
        status = str(getattr(getattr(result, "status", None), "value", "failed"))
        if status == "completed":
            refs = data.get("references")
            msg = "The research report is ready."
            if refs:
                msg = f"The research report is ready, with {refs} cited references."
            if files:
                msg += " I've put the files on screen."
        elif status == "needs_input":
            msg = "I need a bit more information to continue."
        elif status == "denied":
            msg = "I did not run that — it is outside what a research assistant may do."
        else:
            msg = "The research run did not finish. The reason is on screen."
        try:
            time.sleep(1.5)
            speak(msg)
        except Exception:
            pass


def _work(job: _Job, meta: dict, player, speak) -> None:
    try:
        from orchestrator.agents.scientific.scientific_agent import ScientificAgent
        from orchestrator.contracts import AgentRequest, TaskType, new_task_id

        agent = ScientificAgent()
        request = AgentRequest(
            task_id=job.job_id,
            task_type=TaskType.SCIENTIFIC,
            text=job.task,
            metadata={**meta, "cancel_event": job.event, "channel": "science_action"},
        )
        job.result = agent.handle(request)
    except Exception as exc:
        from orchestrator.contracts import AgentResult, TaskStatus

        job.result = AgentResult(
            task_id=job.job_id, status=TaskStatus.FAILED,
            text=f"The science agent failed to start: {type(exc).__name__}: {exc}",
            error=str(exc), agent="scientific_agent",
        )
    finally:
        job.done = True
        if job.result is not None and not job.cancelled:
            _announce(job.result, player, speak, job.target, job.task)
        try:
            from orchestrator.integration import shared_orchestrator

            status = str(getattr(getattr(job.result, "status", None), "value", "failed"))
            shared_orchestrator().journal.agent_result(
                job.job_id, "scientific_agent",
                "completed" if status == "completed" else status,
                str(getattr(job.result, "text", ""))[:200],
            )
        except Exception:
            pass


def _new_job(task: str, meta: dict, player, speak) -> tuple[_Job | None, str]:
    hash_ = _hash(task)
    with _LOCK:
        for existing in _JOBS.values():
            if existing.task_hash == hash_ and not existing.done:
                return None, existing.job_id
        job_id = "sci-" + time.strftime("%Y%m%d-%H%M%S") + "-" + hash_[:6]
        job = _Job(job_id=job_id, task=task, task_hash=hash_,
                   started=time.monotonic(), target=_target(player))
        _JOBS[job_id] = job
    job.thread = threading.Thread(target=_work, args=(job, meta, player, speak),
                                  daemon=True, name=f"science-{job_id}")
    job.thread.start()
    return job, ""


def _render_result(result) -> str:
    status = str(getattr(getattr(result, "status", None), "value", "failed"))
    text = str(getattr(result, "text", "") or "")
    data = getattr(result, "data", {}) or {}
    if status == "completed":
        head = text or "Research finished."
        report_path = str(data.get("report_path") or "")
        if report_path and report_path not in head:
            head += f"\nReport: {report_path}"
        return head[:3500]
    if status in ("needs_input", "needs_approval"):
        return text or "I need more information."
    if status == "denied":
        return text or "That request is outside what I can do."
    return outcome.failed("scientific research", text or "the run did not finish")


def _join_on_exit() -> None:
    """Bounded join of background jobs at interpreter shutdown."""
    with _LOCK:
        threads = [job.thread for job in _JOBS.values() if job.thread is not None]
    for thread in threads:
        try:
            thread.join(timeout=2)
        except Exception:
            pass


atexit.register(_join_on_exit)


def scientific_agent(parameters: dict, player=None, speak=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "run").strip().lower()
    task = str(params.get("task") or "").strip()

    # `action` carries two ideas for the model's convenience: job control
    # (status/cancel/list) and the research mode (literature/database/...).
    # A mode is sugar for action="run" plus that mode.
    research_modes = {"literature", "database", "analysis", "hypothesis", "explain"}
    mode = ""
    if action in research_modes:
        mode, action = action, "run"

    try:
        if action == "list":
            with _LOCK:
                active = [job for job in _JOBS.values() if not job.done]
            if not active:
                return "No scientific research jobs are running."
            return "Running research jobs:\n" + "\n".join(
                f"• {job.job_id} ({time.monotonic() - job.started:.0f}s): {job.task[:70]}"
                for job in active
            )

        if action == "status":
            job_id = str(params.get("job_id") or "").strip()
            with _LOCK:
                job = _JOBS.get(job_id) if job_id else None
                if job is None and not job_id:
                    active = [j for j in _JOBS.values() if not j.done]
                    job = active[0] if len(active) == 1 else None
            if job is None:
                return "Which job? Pass job_id, or action='list'."
            if not job.done:
                return f"{job.job_id} is still running ({time.monotonic() - job.started:.0f}s)."
            if job.thread is not None:
                job.thread.join(timeout=5)
            return _render_result(job.result)

        if action == "cancel":
            job_id = str(params.get("job_id") or "").strip()
            with _LOCK:
                job = _JOBS.get(job_id) if job_id else None
                if job is None and not job_id:
                    active = [j for j in _JOBS.values() if not j.done]
                    job = active[0] if len(active) == 1 else None
            if job is None:
                return "Which job? Pass job_id, or action='list'."
            job.cancelled = True
            job.event.set()
            runner.kill_task(job.job_id)
            return f"Cancelled {job.job_id}. Partial evidence is not reported."

        if action == "skills":
            from orchestrator.agents.scientific.scientific_agent import ScientificAgent
            from orchestrator.contracts import AgentRequest, TaskType

            result = ScientificAgent().handle(AgentRequest(
                task_id="sci-skills", task_type=TaskType.SCIENTIFIC,
                text="list scientific skills", metadata={"action": "skills"},
            ))
            return str(getattr(result, "text", "")) or "No skill information."

        if action == "provision":
            profile = str(params.get("profile") or "stats").strip().lower()
            if not bool(params.get("confirmed")):
                return outcome.ask(
                    "provision science environment",
                    f"Installing the {profile} profile downloads scientific packages. "
                    "Run it with confirmed=true?",
                    profile=profile,
                )
            return _provision(profile)

        if action not in ("run", ""):
            return f"Unknown science action '{action}'. Use run, status, cancel, list, skills or provision."

        if not task:
            return outcome.ask("scientific research", "what should I research?")

        decision = screen(task, dataset=bool(params.get("dataset")))
        if decision.verdict == "refuse":
            return outcome.failed("scientific research", "; ".join(decision.reasons))

        meta = {
            "action": mode,
            "depth": str(params.get("depth") or load_config().depth),
            "dataset": params.get("dataset") or params.get("path") or "",
            "recipe": params.get("recipe") or "",
            "database": params.get("database") or "",
            "query": params.get("query") or "",
            "save_to_memory": bool(params.get("save_to_memory")),
        }
        for key in ("value_col", "group_col", "x", "y", "group", "target",
                    "features", "model", "test_size", "kind", "title", "bins",
                    "sheet", "method", "max_db_calls", "timeout_s"):
            if key in params:
                meta[key] = params[key]

        job, duplicate = _new_job(task, meta, player, speak)
        if job is None:
            return outcome.ask(
                "scientific research",
                f"an identical research task is already running ({duplicate})",
                job_id=duplicate,
            )

        cfg = load_config()
        if job.target:
            return outcome.started(
                "scientific research",
                job_id=job.job_id,
                depth=meta["depth"],
                note="the report will be sent to this chat when it is ready",
            )

        # A short inline wait makes quick database/analysis answers feel instant
        # without ever risking the 30 s session watchdog.
        deadline = time.monotonic() + min(DEFAULT_WAIT_S, float(cfg.run_timeout_s))
        while time.monotonic() < deadline and not job.done:
            time.sleep(0.2)
        if job.done and job.result is not None:
            # The worker also writes the journal and delivers the result; wait
            # for it to finish so teardown never races a live daemon thread.
            if job.thread is not None:
                job.thread.join(timeout=10)
            return _render_result(job.result)
        return outcome.started(
            "scientific research",
            job_id=job.job_id,
            depth=meta["depth"],
            note="still running; ask for the status with this job id",
        )
    except Exception as exc:
        return outcome.failed("scientific research", f"{type(exc).__name__}: {str(exc)[:200]}")


def _provision(profile: str) -> str:
    """Create the science venv and install its pins. Only on explicit request."""
    import subprocess

    venv = envs.SCIENCE_VENV
    requirements = envs.REQUIREMENTS_SCIENCE
    if not requirements.exists():
        return outcome.failed("provision science environment",
                              f"missing {requirements.name}")
    try:
        if not (venv / "bin" / "python").exists():
            proc = subprocess.run(
                ["uv", "venv", "--python", "3.12", str(venv)],
                capture_output=True, text=True, timeout=600,
            )
            if proc.returncode != 0:
                return outcome.failed("provision science environment",
                                      (proc.stderr or proc.stdout)[-300:])
        proc = subprocess.run(
            ["uv", "pip", "install", "--python", str(venv / "bin" / "python"),
             "-r", str(requirements)],
            capture_output=True, text=True, timeout=3600,
        )
    except FileNotFoundError:
        return outcome.failed("provision science environment", "uv is not installed")
    except subprocess.TimeoutExpired:
        return outcome.failed("provision science environment", "installation timed out")
    if proc.returncode != 0:
        return outcome.failed("provision science environment",
                              (proc.stderr or proc.stdout)[-400:])
    envs.clear_cache()
    report = envs.profile_report().get(profile, {})
    return outcome.done(
        "provision science environment",
        packages=", ".join(f"{k} {v}" for k, v in (report.get("packages") or {}).items() if v),
        profile=profile,
    )


TOOL = {
    "name": "scientific_agent",
    "description": (
        "Scientific research with verified citations: literature reviews, paper "
        "search, public scientific databases (PubMed, Europe PMC, arXiv, Crossref, "
        "UniProt, PubChem, ChEMBL, ClinicalTrials.gov), statistical/ML analysis of "
        "a CSV/Excel dataset, publication figures, reports and hypotheses. Use it "
        "for 'find papers about X', 'prepare a literature review with references', "
        "'look up TP53 in UniProt', 'analyze this CSV with a t-test', 'plot the "
        "results', 'identify research gaps'. It returns immediately and announces "
        "the report; reports cite only records it actually retrieved. Research "
        "only: no medical advice, no trade execution. Use action='status'/'cancel'/"
        "'list' with a job id, or action='skills' to see the skill library."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "run (default) | literature | database | analysis | hypothesis | "
                    "explain | status | cancel | list | skills | provision"
                ),
            },
            "task": {
                "type": "STRING",
                "description": "The research question or instruction, in one sentence or a few.",
            },
            "depth": {
                "type": "STRING",
                "description": "quick | standard | deep — controls sources, queries and report size.",
            },
            "dataset": {
                "type": "STRING",
                "description": "Path to a CSV/Excel dataset for analysis.",
            },
            "recipe": {
                "type": "STRING",
                "description": (
                    "describe | ttest | anova | correlation | regression | "
                    "ml_classify | plot"
                ),
            },
            "value_col": {"type": "STRING", "description": "Numeric column for ttest/anova."},
            "group_col": {"type": "STRING", "description": "Grouping column for ttest/anova."},
            "x": {"type": "STRING", "description": "x column (plot/correlation/regression)."},
            "y": {"type": "STRING", "description": "y column (plot/correlation/regression)."},
            "group": {"type": "STRING", "description": "Group column for a box plot."},
            "target": {"type": "STRING", "description": "Target column for ml_classify."},
            "features": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Feature columns for ml_classify (default: all other columns).",
            },
            "database": {
                "type": "STRING",
                "description": "pubmed | europepmc | arxiv | crossref | uniprot | pubchem | chembl | clinicaltrials",
            },
            "query": {"type": "STRING", "description": "Database query: gene, compound, term or DOI."},
            "save_to_memory": {
                "type": "BOOLEAN",
                "description": "Store the finished report in RIO's knowledge base (default false).",
            },
            "job_id": {"type": "STRING", "description": "Job id for status/cancel."},
            "profile": {"type": "STRING", "description": "stats | chem — for action='provision'."},
            "confirmed": {
                "type": "BOOLEAN",
                "description": "Only after the user approved a package install.",
            },
        },
        "required": [],
    },
    "handler": scientific_agent,
}
