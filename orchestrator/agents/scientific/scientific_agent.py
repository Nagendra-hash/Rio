"""
orchestrator/agents/scientific/scientific_agent.py — RIO's science specialist.

WHAT IT IS
    A routed agent (TaskType.SCIENTIFIC) that turns a research request into a
    citable report. It composes what RIO already has — the source toolbox, the
    model router, RAG, the browser agent — with the science library
    (`core/scientific/`): skill discovery, real database clients, vetted
    analysis recipes and a report assembler that refuses to invent references.

DISPATCH
    literature  papers, reviews, gaps, conflicts, "find papers about X"
    database    UniProt / PubChem / ChEMBL / ClinicalTrials / DOI lookups
    analysis    a dataset + a recipe (EDA, t-test, ANOVA, regression, ML, plot)
    hypothesis  evidence-bounded candidate hypotheses
    explain     plain-language explanation of a paper/DOI/title
    skills      list/enable inspection of the skill library

HONESTY RULES (enforced in code, not just prompted)
    * References are generated from retrieved records. A claim citing nothing
      retrieved is downgraded to `unverified` by `report.verify_claims`.
    * If no model answers, the report is extractive: sources listed, findings
      section omitted, and the result says the synthesis model was unavailable.
    * Analysis numbers come from `scripts/science_worker.py`; the agent never
      computes a p-value itself and never writes analysis code.
    * Clinical requests are research-only; trade/order requests are refused and
      belong to the trading agent.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.base import BaseAgent
from orchestrator.contracts import AgentRequest, AgentResult, TaskStatus, TaskType

from core import outcome
from core.scientific import (
    analysis,
    databases,
    envs,
    report as report_mod,
    safety,
    skill_index,
    skill_loader,
)
from core.scientific.config import DEPTHS, UPSTREAM_TAG, load_config

ACTIONS = ("literature", "database", "analysis", "hypothesis", "explain", "skills")

_DB_NAMES = {
    "pubmed": "pubmed", "europepmc": "europepmc", "europe pmc": "europepmc",
    "arxiv": "arxiv", "crossref": "crossref", "doi": "crossref",
    "uniprot": "uniprot", "pubchem": "pubchem", "chembl": "chembl",
    "clinicaltrials": "clinicaltrials", "clinical trials": "clinicaltrials",
}

_SYNTH_SYSTEM = """You are RIO's scientific research analyst. You are given a
question, numbered reference records retrieved from public scientific databases,
and optional dataset analysis results. Write an evidence-bounded synthesis.

Return ONE JSON object, no prose outside it:
{
 "summary": "3-6 sentences. Say what the evidence supports and what it does not.",
 "findings": [{"text": "one specific finding", "citations": [1,2],
               "status": "established|hypothesis|contested"}],
 "gaps": ["what the retrieved evidence does not answer"],
 "conflicts": ["where sources disagree, naming both sides"],
 "hypotheses": ["testable candidate explanation, clearly speculative"],
 "limitations": ["search coverage, sample sizes, preprint status, retrieval date"]
}

Rules that will be checked mechanically:
- citations are INTEGER indexes into the reference list you were given;
- "established" needs at least two independent records, otherwise use
  "contested" or "hypothesis";
- never cite a number that is not in the list, and never add references;
- if the records do not answer the question, say so in summary and return an
  empty findings list.
"""


@dataclass
class _Progress:
    steps: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    cancelled: bool = False

    def note(self, text: str) -> None:
        self.steps.append(f"{time.monotonic() - self.started:5.1f}s {text}")


class _DepthLLM:
    """RIO's model router at the depth's task tier — no separate provider stack.

    Two details matter for a research workflow:

    * mode="free": the science agent must not depend on Gemini. RIO's "free"
      mode still uses every local and free provider the router knows; it only
      excludes the Gemini cost tier, which is the brief's stated preference.
    * ONE router instance per agent lifetime. The NVIDIA chain parks a model
      that timed out (`MODEL_COOLDOWN_S`), but that memory lives on the adapter;
      constructing a new router per call paid the dead-model tax on every
      single request.
    """

    TASKS = {
        "light_reasoning": "LIGHT_REASONING",
        "research": "RESEARCH",
        "difficult_reasoning": "DIFFICULT_REASONING",
    }
    _ROUTER = None

    def __init__(self, task: str = "research", timeout_s: float = 60.0):
        self.task_name = self.TASKS.get(task, "RESEARCH")
        self.timeout_s = timeout_s
        self.last_provider = ""
        self.last_error = ""

    @classmethod
    def _router(cls):
        if cls._ROUTER is None:
            from orchestrator.model_router.router import ModelRouter

            cls._ROUTER = ModelRouter(mode="free")
        return cls._ROUTER

    def complete(self, prompt: str, *, system: str = "") -> str | None:
        try:
            from orchestrator.model_router.capabilities import ModelTask
            from orchestrator.model_router.router import RouteOptions
        except Exception as exc:
            self.last_error = f"router unavailable: {type(exc).__name__}"
            return None

        task = getattr(ModelTask, self.task_name, ModelTask.RESEARCH)
        box: dict = {}

        def _call() -> None:
            try:
                box["result"] = self._router().route(
                    prompt,
                    RouteOptions(task=task, system=system,
                                 timeout_s=self.timeout_s,
                                 deadline_s=self.timeout_s + 15),
                )
            except BaseException as exc:  # surfaced to the caller
                box["error"] = exc

        worker = threading.Thread(target=_call, daemon=True, name="science-llm")
        worker.start()
        # Hard watchdog: a provider that ignores its own timeout must not hold
        # the research job hostage. The abandoned thread is a daemon.
        worker.join(self.timeout_s + 20)
        if worker.is_alive():
            self.last_error = f"model call exceeded {self.timeout_s + 20:.0f}s"
            return None
        if "error" in box:
            self.last_error = str(box["error"])[:200]
            return None
        result = box.get("result")
        if result is not None and result.ok and result.text.strip():
            self.last_provider = f"{result.provider}/{result.model}"
            return result.text
        if result is not None:
            self.last_error = (result.explanation or "")[:200]
        return None


def _json_block(text: str) -> dict:
    """First balanced JSON object in a model reply."""
    if not text:
        return {}
    start = text.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:index + 1])
                        if isinstance(value, dict):
                            return value
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return {}


def _queries(question: str, max_queries: int) -> list[str]:
    """Deterministic query expansion — no model call needed for this step."""
    base = re.sub(r"\s+", " ", question).strip()
    queries = [base]
    lowered = base.lower()
    if "review" not in lowered and re.search(r"\b(papers?|literature|evidence)\b", lowered):
        queries.append(f"{base} review")
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z-]{3,}", base)
             if w.lower() not in {"what", "which", "does", "with", "from", "that",
                                  "this", "have", "been", "about", "their", "there"}]
    if len(words) >= 4:
        queries.append(" ".join(words[:5]))
    seen: list[str] = []
    for query in queries:
        if query and query not in seen:
            seen.append(query)
    return seen[: max(1, max_queries)]


class ScientificAgent(BaseAgent):
    name = "scientific_agent"
    description = (
        "Evidence-based scientific research: systematic literature search with "
        "verified citations, public database lookups (PubMed, Europe PMC, arXiv, "
        "Crossref, UniProt, PubChem, ChEMBL, ClinicalTrials.gov), statistical and "
        "ML analysis of datasets, publication figures, reports and hypotheses. "
        "Research/education only — no clinical advice, no trade execution."
    )
    task_types = (TaskType.SCIENTIFIC,)
    priority = 10

    def __init__(self, cfg=None, llm=None) -> None:
        self._cfg = cfg
        self._llm = llm
        self.last_request = None
        self.last_result = None

    # ── dependencies ──────────────────────────────────────────────────────
    def _config(self):
        if self._cfg is None:
            self._cfg = load_config()
        return self._cfg

    def _profile(self, meta: dict):
        """Depth from the request (quick|standard|deep), else the config default."""
        requested = str((meta or {}).get("depth") or "").strip().lower()
        if requested in DEPTHS:
            return DEPTHS[requested]
        return self._config().depth_profile()

    def _model(self, depth: str):
        if self._llm is not None:
            return self._llm
        task = depth if depth in _DepthLLM.TASKS else "research"
        return _DepthLLM(task=task)

    def can_handle(self, request: AgentRequest) -> bool:
        try:
            if request.task_type in self.task_types:
                return True
            meta = request.metadata or {}
            return (str(meta.get("agent_override") or "") == self.name
                    and bool(skill_index.discover(str(request.text or ""), k=1)))
        except Exception:
            return False

    # ── entry point ───────────────────────────────────────────────────────
    def _handle_impl(self, request: AgentRequest) -> AgentResult:
        try:
            return self._dispatch(request)
        except Exception as exc:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED,
                text=f"The scientific agent hit an error: {type(exc).__name__}: "
                     f"{str(exc)[:200]}. Nothing was fabricated.",
                error=str(exc), agent=self.name,
            )

    def _dispatch(self, request: AgentRequest) -> AgentResult:
        text = str(request.text or "").strip()
        meta = request.metadata or {}
        action = self._infer_action(text, meta)
        self._started = time.monotonic()
        self._progress = _Progress(started=self._started)

        decision = safety.screen(text, dataset=bool(meta.get("dataset")))
        if decision.verdict == "refuse":
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.DENIED,
                text="Refused: " + "; ".join(decision.reasons),
                error="; ".join(decision.reasons),
                data={"safety": decision.public()}, agent=self.name,
            )

        if action == "skills":
            return self._skills(request)
        if action == "analysis":
            return self._analysis(request, decision)
        if action == "database":
            return self._database(request, decision)
        if action == "hypothesis":
            return self._literature(request, decision, hypothesis=True)
        if action == "explain":
            return self._explain(request, decision)
        return self._literature(request, decision, hypothesis=False)

    def _infer_action(self, text: str, meta: dict) -> str:
        explicit = str(meta.get("action") or "").strip().lower()
        if explicit in ACTIONS:
            return explicit
        if meta.get("dataset") or meta.get("recipe"):
            return "analysis"
        if meta.get("database"):
            return "database"
        lowered = text.lower()
        for name in _DB_NAMES:
            if re.search(rf"\b{re.escape(name)}\b", lowered):
                return "database"
        if re.search(r"\b(hypothes\w*|research gaps?)\b", lowered):
            return "hypothesis"
        if re.search(r"\b(explain|in simple english|lay summary)\b", lowered) and \
                re.search(r"\b(paper|study|article|preprint|doi)\b", lowered):
            return "explain"
        return "literature"

    # ── skills ────────────────────────────────────────────────────────────
    def _skills(self, request: AgentRequest) -> AgentResult:
        index = skill_index.get_index()
        summary = index.summary()
        enabled = sorted(s.name for s in index.enabled() if not s.guarded_reason)
        lines = [
            f"{summary['skills']} vendored skills at {UPSTREAM_TAG}; "
            f"{summary['enabled']} enabled, {summary['guarded']} guarded.",
            "Enabled: " + ", ".join(enabled),
            "Environment profiles: " + json.dumps(summary["env_profiles"]),
        ]
        env_report = envs.profile_report()
        for profile, info in env_report.items():
            present = ", ".join(f"{k} {v}" for k, v in info["packages"].items() if v)
            missing = ", ".join(k for k, v in info["packages"].items() if not v)
            lines.append(f"  {profile}: {present or 'empty'}"
                         + (f" | missing: {missing}" if missing else ""))
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED,
            text="\n".join(lines),
            data={"summary": summary, "envs": env_report, "enabled": enabled},
            agent=self.name,
        )

    # ── literature ────────────────────────────────────────────────────────
    def _literature(self, request: AgentRequest, decision: safety.SafetyDecision,
                    *, hypothesis: bool) -> AgentResult:
        cfg = self._config()
        meta = request.metadata or {}
        profile = self._profile(meta)
        question = str(request.text or "").strip()
        if not question:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.NEEDS_INPUT,
                text="What should I research? Give me the question or topic.",
                agent=self.name,
            )

        plan = skill_index.plan_skills(question, limit=cfg.max_skills_per_task)
        if not plan:
            # A literature run without a named science term still needs the
            # review/citation procedures; discovery alone would return nothing.
            defaults = ["literature-review", "paper-lookup", "citation-management"]
            index = skill_index.get_index()
            plan = [index.get(name) for name in defaults
                    if index.get(name) and index.get(name).enabled][: cfg.max_skills_per_task]
        self._progress.note("skills: " + (", ".join(s.name for s in plan) or "none"))
        budget = databases.Budget(max_calls=int(meta.get("max_db_calls") or profile.max_db_calls))

        queries = _queries(question, profile.max_queries)
        collected: list[dict] = []
        sources_ok: list[str] = []
        failed: list[dict] = []
        for query in queries:
            if self._cancelled(request):
                return self._cancelled_result(request)
            result = databases.literature(
                query, limit=profile.max_sources, include_preprints=True, budget=budget
            )
            sources_ok = list(dict.fromkeys(sources_ok + list(result.get("sources_ok") or [])))
            failed.extend(result.get("sources_failed") or [])
            collected.extend(result.get("records") or [])
            self._progress.note(f"search '{query[:40]}': {len(result.get('records') or [])} records")

        records = self._dedupe(collected)[: profile.max_sources]
        retrieval_notes = self._fetch_full_text(records, profile) if profile.read_full_text else []
        if retrieval_notes:
            self._progress.note(f"full text: {len(retrieval_notes)} note(s)")
        references = report_mod.references_from_records(records, retrieved_at=self._now())
        if not records:
            return self._no_evidence(request, question, sources_ok, failed)

        synthesis = self._synthesise(
            question=question,
            records=records,
            references=references,
            skills=plan,
            profile=profile,
            hypothesis=hypothesis,
        )

        claims, issues = report_mod.verify_claims(synthesis.get("claims") or [], references)
        narrative = synthesis.get("summary") or ""
        method = {
            "sources": sources_ok,
            "queries": queries,
            "databases": budget.public(),
            "retrieved_at": self._now(),
            "failed_sources": failed,
        }
        limitations = list(synthesis.get("limitations") or []) + retrieval_notes
        report_meta = {
            "depth": profile.name,
            "skills": [s.name for s in plan],
            "skills_tag": UPSTREAM_TAG,
            "model": synthesis.get("model", ""),
        }
        markdown = report_mod.render(
            question=question,
            references=references,
            claims=claims,
            narrative=narrative,
            method=method,
            gaps=synthesis.get("gaps"),
            conflicts=synthesis.get("conflicts"),
            hypotheses=synthesis.get("hypotheses"),
            limitations=limitations,
            meta=report_meta,
            scope=decision.scope,
        )
        issues.extend(report_mod.check_fabrication(markdown, references))
        # "adjusted:" entries are the integrity rules doing their job (a claim
        # demoted for lacking two sources). Only the rest are problems with the
        # report's citations, and those are what a caller must not ignore.
        claim_adjustments = [i for i in issues if str(i).startswith("adjusted:")]
        integrity_issues = [i for i in issues if not str(i).startswith("adjusted:")]
        path = report_mod.save(markdown, question)

        if meta.get("save_to_memory"):
            self._remember(path, question, records)

        text = self._summary_text(
            question, len(records), len(references), path, synthesis, hypothesis
        )
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED, text=text,
            data={
                "action": "hypothesis" if hypothesis else "literature",
                "report_path": str(path),
                "references": len(references),
                "records": len(records),
                "sources": sources_ok,
                "failed_sources": failed,
                "skills": [s.name for s in plan],
                "synthesis": "model" if synthesis.get("model") else "extractive",
                "issues": integrity_issues,
                "claim_adjustments": claim_adjustments,
                "retrieval_notes": retrieval_notes,
                "safety": decision.public(),
                "steps": self._progress.steps,
            },
            agent=self.name,
        )

    def _synthesise(self, *, question: str, records: list[dict],
                    references: list[report_mod.Reference], skills, profile,
                    hypothesis: bool) -> dict:
        llm = self._model(profile.model_task)
        context = databases.format_for_model(records, max_records=min(len(records), 14))
        skill_context = skill_loader.context_for(skills) if skills else ""
        prompt = (
            f"QUESTION: {question}\n\n"
            f"DEPTH: {profile.name}\n"
            f"TASK: {'generate evidence-bounded candidate hypotheses and discriminating tests' if hypothesis else 'answer the question from the records'}\n\n"
            f"REFERENCES (cite by these indexes only):\n"
            f"{outcome.untrusted('scientific-records', context)}\n\n"
        )
        if skill_context:
            prompt += skill_context + "\n\n"
        prompt += ("Return the JSON object now. If the records are insufficient, say so in "
                   "summary and leave findings empty.")
        reply = llm.complete(prompt, system=_SYNTH_SYSTEM + "\n" + safety.integrity_rules())
        data = _json_block(reply or "")
        model_used = getattr(llm, "last_provider", "") or ""
        if not data:
            return {
                "summary": (
                    f"Retrieved {len(records)} records, but no synthesis model was "
                    "available, so this report lists the sources without drawing "
                    "conclusions. The records are in the references below."
                ),
                "claims": [], "gaps": [], "conflicts": [], "hypotheses": [],
                "limitations": ["synthesis model unavailable — no interpretive claims made"],
                "model": "",
            }
        claims = []
        for item in data.get("findings") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            citations = []
            for value in item.get("citations") or []:
                try:
                    citations.append(int(value))
                except (TypeError, ValueError):
                    continue
            status = str(item.get("status") or report_mod.ESTABLISHED).lower()
            if status not in (report_mod.ESTABLISHED, report_mod.HYPOTHESIS,
                              report_mod.CONTESTED, report_mod.UNVERIFIED):
                status = report_mod.UNVERIFIED
            claims.append(report_mod.Claim(text, citations, status))
        return {
            "summary": str(data.get("summary") or "").strip(),
            "claims": claims,
            "gaps": [str(x) for x in data.get("gaps") or [] if str(x).strip()],
            "conflicts": [str(x) for x in data.get("conflicts") or [] if str(x).strip()],
            "hypotheses": [str(x) for x in data.get("hypotheses") or [] if str(x).strip()],
            "limitations": [str(x) for x in data.get("limitations") or [] if str(x).strip()],
            "model": model_used,
        }

    # ── database ──────────────────────────────────────────────────────────
    def _database(self, request: AgentRequest, decision: safety.SafetyDecision) -> AgentResult:
        text = str(request.text or "").strip()
        meta = request.metadata or {}
        cfg = self._config()
        profile = self._profile(meta)

        database = str(meta.get("database") or "").strip().lower()
        query = str(meta.get("query") or "").strip()
        if not database:
            lowered = text.lower()
            for name, canonical in _DB_NAMES.items():
                if re.search(rf"\b{re.escape(name)}\b", lowered):
                    database = canonical
                    break
        if not database:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.NEEDS_INPUT,
                text=("Which database should I query? Available: "
                      + ", ".join(sorted(databases.PUBLIC_DATABASES))),
                agent=self.name,
            )
        if not query:
            query = text
            for token in re.findall(r"\b[A-Z0-9][A-Za-z0-9.-]{2,}\b", text):
                if token.upper() == token and len(token) >= 3:
                    query = token
                    break
        if query.lower().startswith(("look up", "find", "search", "get", "query")):
            query = re.sub(r"^(look up|find|search for|search|get|query)\s+", "", query,
                           flags=re.I).strip()

        budget = databases.Budget(max_calls=4)
        result = databases.lookup(database, query, limit=profile.max_sources, budget=budget)
        if not result.ok:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED,
                text=f"{database} lookup failed: {result.error}",
                error=result.error,
                data={"provenance": result.public().get("provenance"),
                      "safety": decision.public()},
                agent=self.name,
            )
        payload = result.public()
        if not result.records:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.COMPLETED,
                text=f"{database}: no records found for '{query}'. "
                     + (" ".join(result.warnings) if result.warnings else ""),
                data={"action": "database", "records": 0, "provenance": payload["provenance"],
                      "safety": decision.public()},
                agent=self.name,
            )

        rendered = databases.format_for_model(result.records, max_records=8)
        text_out = (f"{database}: {len(result.records)} record(s) for '{query}'.\n"
                    + rendered[:1800])
        if decision.disclaimers:
            text_out += "\n\n" + " ".join(decision.disclaimers)
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED, text=text_out,
            data={"action": "database", "database": database, "query": query,
                  "records": result.records[:8], "provenance": payload["provenance"],
                  "warnings": result.warnings, "safety": decision.public()},
            agent=self.name,
        )

    # ── analysis ──────────────────────────────────────────────────────────
    def _analysis(self, request: AgentRequest, decision: safety.SafetyDecision) -> AgentResult:
        meta = request.metadata or {}
        cfg = self._config()
        path = str(meta.get("dataset") or meta.get("path") or "").strip()
        if not path:
            match = re.search(r"(/[^\s]+\.(?:csv|xlsx?|tsv|json))", str(request.text or ""))
            path = match.group(1) if match else ""
        if not path:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.NEEDS_INPUT,
                text=("Which dataset should I analyse? Give me a CSV/Excel path "
                      "(or upload it), and say which analysis you want."),
                agent=self.name,
            )
        recipe = str(meta.get("recipe") or "").strip().lower() or self._infer_recipe(
            str(request.text or "")
        )
        params = {k: v for k, v in meta.items()
                  if k in {"value_col", "group_col", "x", "y", "group", "target",
                           "features", "model", "test_size", "kind", "title", "bins", "sheet",
                           "method"}}
        if isinstance(params.get("features"), str):
            params["features"] = [p.strip() for p in params["features"].split(",") if p.strip()]
        if isinstance(params.get("x"), str) and recipe == "regression":
            params["x"] = [p.strip() for p in params["x"].split(",") if p.strip()]
        params["path"] = path
        if recipe == "plot" and not params.get("kind"):
            params["kind"] = "hist"

        result = analysis.run(
            recipe, params,
            task_id=request.task_id,
            timeout_s=float(meta.get("timeout_s") or cfg.run_timeout_s),
        )
        if not result.ok:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED,
                text=(f"The {recipe} analysis did not complete: {result.error}. "
                      "Nothing is reported as a result."),
                error=result.error,
                data={"action": "analysis", "recipe": recipe, "job_dir": result.job_dir,
                      "warnings": result.warnings, "safety": decision.public()},
                agent=self.name,
            )

        payload = result.public()
        question = str(request.text or f"{recipe} analysis of {Path(path).name}")
        markdown = report_mod.render(
            question=question,
            references=[],
            claims=[],
            narrative=result.summary,
            datasets=[payload],
            method={"sources": ["local dataset"], "queries": [], "retrieved_at": self._now()},
            limitations=result.warnings,
            meta={"depth": cfg.depth, "skills": [], "skills_tag": UPSTREAM_TAG,
                  "interpreter": result.interpreter},
            scope=decision.scope,
        )
        report_path = report_mod.save(markdown, question)
        if meta.get("save_to_memory"):
            self._remember(report_path, question, [])

        text = (
            f"{recipe}: {result.summary}\n"
            + (f"Figures: {', '.join(result.figures)}\n" if result.figures else "")
            + (f"Report: {report_path}\n" if report_path else "")
        )
        if decision.disclaimers:
            text += "\n" + " ".join(decision.disclaimers)
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED, text=text,
            data={"action": "analysis", "recipe": recipe, "job_dir": result.job_dir,
                  "summary": result.summary, "figures": result.figures,
                  "tables": result.tables, "stats": payload.get("stats"),
                  "rows": payload.get("rows"), "versions": payload.get("versions"),
                  "report_path": str(report_path), "duration_s": result.duration_s,
                  "peak_rss_mb": result.peak_rss_mb, "warnings": result.warnings,
                  "safety": decision.public()},
            agent=self.name,
        )

    def _infer_recipe(self, text: str) -> str:
        lowered = text.lower()
        if re.search(r"\b(t-test|t test|welch)\b", lowered):
            return "ttest"
        if re.search(r"\banova\b", lowered):
            return "anova"
        if re.search(r"\bcorrelat", lowered):
            return "correlation"
        if re.search(r"\bregress", lowered):
            return "regression"
        if re.search(r"\b(classifier|classify|machine learning|train a model)\b", lowered):
            return "ml_classify"
        if re.search(r"\b(plot|chart|figure|visuali[sz])\b", lowered):
            return "plot"
        return "describe"

    # ── explain ───────────────────────────────────────────────────────────
    def _explain(self, request: AgentRequest, decision: safety.SafetyDecision) -> AgentResult:
        text = str(request.text or "")
        doi_match = re.search(r"\b(10\.\d{4,9}/\S+)\b", text)
        records: list[dict] = []
        if doi_match:
            result = databases.lookup_doi(doi_match.group(1))
            records = result.records if result.ok else []
        else:
            title = re.sub(r"(?i)^(explain|summari[sz]e|describe)\s+(this\s+|the\s+)?"
                           r"(paper|study|article|preprint)[:\s]*", "", text).strip()
            if title:
                result = databases.literature(title, limit=3)
                records = result.get("records") or []
        if not records:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.NEEDS_INPUT,
                text=("Give me the paper's DOI, title, or the PDF and I'll explain it. "
                      "I could not identify a paper from that message."),
                agent=self.name,
            )
        references = report_mod.references_from_records(records, retrieved_at=self._now())
        llm = self._model("research")
        context = databases.format_for_model(records, max_records=3)
        reply = llm.complete(
            "Explain this paper for an intelligent non-specialist: what question it "
            "asked, what it did, what it found, and what it does NOT establish. "
            "Three short paragraphs, no invented details.\n\n"
            + outcome.untrusted("paper-records", context),
            system=safety.integrity_rules(),
        )
        if not reply:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED,
                text=("A synthesis model is unavailable, so I can only list the "
                      "record I found, not explain it.\n"
                      + databases.format_for_model(records, max_records=1)),
                data={"records": records[:1], "references": [r.line() for r in references]},
                agent=self.name,
            )
        body = reply.strip()
        issues = report_mod.check_fabrication(body, references)
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED,
            text=body + "\n\nReferences:\n" + "\n".join(r.line() for r in references),
            data={"action": "explain", "records": records[:3],
                  "references": [r.line() for r in references], "issues": issues,
                  "safety": decision.public()},
            agent=self.name,
        )

    # ── helpers ───────────────────────────────────────────────────────────
    def _fetch_full_text(self, records: list[dict], profile) -> list[str]:
        """Try to read the actual page for the top records.

        Static fetch first (`SourceToolbox.fetch`, SSRF-guarded, 200 KB cap).
        A browser is used only when the page is a JavaScript shell — never when
        the site refused access: a 401/402/403 is a paywall or a login, and
        going around it is out of scope. Returns notes for the report.
        """
        notes: list[str] = []
        if not getattr(profile, "read_full_text", False):
            return notes
        try:
            from orchestrator.agents.research_tools import SourcePermissions, SourceToolbox
        except Exception:
            return notes
        toolbox = SourceToolbox(SourcePermissions(allow_web=True, allow_local_kb=False))
        budget = 2
        for record in records[:6]:
            url = str(record.get("url") or "").strip()
            if not url or budget <= 0:
                continue
            if record.get("full_text"):
                continue
            budget -= 1
            try:
                page = toolbox.fetch(url)
            except Exception as exc:
                page = {"ok": False, "error": f"{type(exc).__name__}"}
            text = str((page or {}).get("text") or "")
            status = (page or {}).get("status")
            if (page or {}).get("ok") and len(text) >= 500:
                record["full_text"] = text[:8000]
                record["full_text_source"] = "static"
                notes.append(f"{record.get('source', 'source')}: full text read from {url[:80]}")
                continue
            if status in (401, 402, 403):
                record["access"] = "restricted"
                notes.append(f"{url[:80]} refused access ({status}); not bypassed")
                continue
            browser_result = self._browser_extract(url)
            if browser_result:
                record["full_text"] = browser_result[:8000]
                record["full_text_source"] = "browser"
                notes.append(f"{record.get('source', 'source')}: JS page read in the browser")
            else:
                notes.append(f"{url[:80]}: no full text retrieved (abstract only)")
        return notes

    @staticmethod
    def _browser_extract(url: str) -> str:
        """Last resort for JS-rendered pages: the RIO Browser Agent."""
        try:
            from core.browser_agent import runtime as browser_runtime

            ok, _version, _reason = browser_runtime.preflight()
            if not ok:
                return ""
            # Deliberately worded without "bypass"/"captcha": those words are
            # exactly what the browser agent's deny rule looks for, and this
            # instruction is the opposite of a bypass request.
            result = browser_runtime.run_task(
                f"Open {url} and extract the main readable text of the page. "
                "Read only what is publicly visible; stop if the site asks for "
                "a login or a security challenge.",
                mode="extract", wait=True, max_steps=8,
            )
            return result.final.strip() if result.ok else ""
        except Exception:
            return ""

    def _dedupe(self, records: list[dict]) -> list[dict]:
        seen: set[str] = set()
        out: list[dict] = []
        for record in records:
            key = databases._dedupe_key(record)
            if key in seen:
                continue
            seen.add(key)
            out.append(record)
        return out

    def _no_evidence(self, request: AgentRequest, question: str,
                     sources: list[str], failed: list[dict]) -> AgentResult:
        if failed and not sources:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED,
                text=("Every literature source failed: "
                      + "; ".join(f"{f['source']} ({f['error']})" for f in failed[:3])
                      + ". No evidence was retrieved, so nothing is reported."),
                error="all sources failed",
                data={"failed_sources": failed, "steps": self._progress.steps},
                agent=self.name,
            )
        markdown = report_mod.render(
            question=question, references=[], claims=[],
            narrative="The search returned no records for this question. "
                      "No findings are possible without retrieved evidence.",
            method={"sources": sources, "queries": _queries(question, 3),
                    "retrieved_at": self._now(), "failed_sources": failed},
            limitations=["no records retrieved"],
            meta={"depth": self._config().depth, "skills_tag": UPSTREAM_TAG},
        )
        path = report_mod.save(markdown, question)
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.COMPLETED,
            text=(f"No records found for: {question}. Tried {', '.join(sources) or 'no source'}. "
                  f"Report: {path}"),
            data={"report_path": str(path), "records": 0, "references": 0,
                  "sources": sources, "failed_sources": failed,
                  "steps": self._progress.steps},
            agent=self.name,
        )

    def _summary_text(self, question: str, records: int, references: int,
                      path: Path, synthesis: dict, hypothesis: bool) -> str:
        head = ("Hypotheses" if hypothesis else "Research report") + f": {question}"
        lines = [
            head,
            f"{records} records, {references} cited references "
            f"({'model synthesis' if synthesis.get('model') else 'extractive — no model'}).",
            f"Report: {path}",
        ]
        summary = (synthesis.get("summary") or "").strip()
        if summary:
            lines.append(summary[:700])
        if synthesis.get("hypotheses"):
            lines.append("Candidate hypotheses: " + "; ".join(synthesis["hypotheses"][:3]))
        if synthesis.get("gaps"):
            lines.append("Gaps: " + "; ".join(synthesis["gaps"][:3]))
        return "\n".join(lines)

    def _remember(self, report_path: Path, question: str, records: list[dict]) -> None:
        try:
            from core import rag_service

            rag_service.ingest_text(
                caller="scientific_agent",
                text=report_path.read_text(encoding="utf-8"),
                title=f"Scientific report: {question[:90]}",
                source="scientific_agent",
                metadata={"report_path": str(report_path),
                          "sources": [r.get("url") or r.get("doi") for r in records[:20]]},
            )
        except Exception:
            pass

    def _cancelled(self, request: AgentRequest) -> bool:
        if getattr(self, "_progress", None) and self._progress.cancelled:
            return True
        event = (request.metadata or {}).get("cancel_event")
        if event is not None and getattr(event, "is_set", lambda: False)():
            try:
                from core.scientific import runner

                runner.kill_task(request.task_id)
            except Exception:
                pass
            return True
        deadline = float((request.metadata or {}).get("deadline_s") or 0)
        if deadline and (time.monotonic() - getattr(self, "_started", time.monotonic())) > deadline:
            return True
        return False

    def _cancelled_result(self, request: AgentRequest) -> AgentResult:
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.DENIED,
            text="The research run was cancelled. Partial evidence is discarded; "
                 "nothing is reported as a finding.",
            data={"cancelled": True, "steps": self._progress.steps},
            agent=self.name,
        )

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
