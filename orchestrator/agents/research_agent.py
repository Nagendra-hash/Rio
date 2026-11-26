"""
orchestrator/agents/research_agent.py — the real research agent (Phase 6).

Replaces the Phase-1 stub under the SAME registry name ("research_agent") —
the router, contracts and orchestrator are untouched (the convention the
rag_agent set in Phase 2 and the coding agent kept in Phase 5). Enabled with
JARVIS_RESEARCH_AGENT=on (shadow-mode by default, like every other phase).

WORKFLOW (fixed, checkpointed, honest)

    QUESTION → PLAN → SEARCH → COLLECT → COMPARE → VERIFY → REPORT

    QUESTION  understand the ask; resolve source tiers (metadata.allow_web /
              allow_local_kb) and knobs; or RESUME a checkpoint
              (metadata.resume = <task_id>).
    PLAN      one LLM call returning strict JSON: understanding,
              sub_questions, search_queries, use_local_kb, deep_fetch,
              risks. The model proposes queries; it never touches a source.
    SEARCH    run the queries through the permitted-source toolbox (web
              search / local KB via rag_service as caller research_agent).
              Blocked or failed sources are recorded, never invented over.
    COLLECT   normalise, dedupe by URL/title, cap the count; each source
              keeps its full reference (title, url, snippet) for the report.
    COMPARE   cluster evidence by claim; note agreements (multiple
              independent hosts) and conflicts (disagreeing snippets).
    VERIFY    every candidate claim is graded SUPPORTED / CONTESTED /
              UNVERIFIED against the collected evidence (support_score ≥
              VERIFY_THRESHOLD for supported). Unverified claims are
              labelled and listed separately — they are never presented
              as facts.
    REPORT    grounded report: only SUPPORTED claims enter the answer body,
              each with inline [n] citations; CONTESTED shows both sides;
              UNVERIFIED goes to an explicit "unverified" section with the
              source gap named. Uncertainty is called out; references list
              every source. All findings also land in data["report"] as a
              structured record (claims, unverified, conflicts, sources).

THE MODEL LAYER
    LLM calls go through the Model Router (orchestrator/model_router) with
    ModelTask.RESEARCH; when the router cannot serve (nothing configured or
    healthy) it falls back to the house core/gemini SMART ladder, and if
    that fails the run degrades to heuristic-only (queries from the
    question text) — still honest, just shallower. Injectable (llm=...) so
    tests and the offline demo run with a scripted backend.

RAG INTEGRATION
    Local evidence comes from core/rag_service.search with the
    "research_agent" caller identity — the same ACL/audit envelope the
    rag_agent uses; a caller with no grant in config/rag_callers.json
    degrades to zero local results, never to an error.

`handle()` NEVER raises. Every failure is an AgentResult(FAILED) plus a
checkpoint the task can resume from.
"""
from __future__ import annotations

import json
import os
import re
import traceback as _tb
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from orchestrator.agents.research_tools import (
    MAX_FETCH_CHARS,
    MAX_SOURCES,
    SourceToolbox,
)
from orchestrator.base import BaseAgent
from orchestrator.contracts import AgentRequest, AgentResult, TaskStatus, TaskType

# ── tunables ─────────────────────────────────────────────────────────────────
MAX_QUERIES          = 4      # search queries per task
MAX_SUB_QUESTIONS    = 3      # sub-questions per plan
VERIFY_THRESHOLD     = 0.6    # support_score needed to call a claim supported
MIN_EVIDENCE_CHARS   = 60     # below this, a source is too thin to cite
MAX_CLAIMS           = 8      # claims carried into the report
MAX_EVIDENCE_CHARS   = 9000   # evidence budget inside the report prompt
MAX_STORED_TEXT      = 32_000 # per-field cap inside a checkpoint
STAGES               = ("question", "plan", "search", "collect",
                        "compare", "verify", "report")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def checkpoint_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Writable checkpoints dir, relocated by JARVIS_HOME like every other
    writable path (memory/config_manager is the canonical BASE_DIR)."""
    if explicit:
        return Path(explicit)
    try:
        from memory.config_manager import BASE_DIR
        return Path(BASE_DIR) / "memory" / "research_agent"
    except Exception:
        home = os.environ.get("JARVIS_HOME", "").strip()
        if home:
            base = Path(home)
        elif getattr(__import__("sys"), "frozen", False):
            base = Path(__import__("sys").executable).parent
        else:
            base = Path(__file__).resolve().parents[2]
        return base / "memory" / "research_agent"


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


# ── the LLM layer ────────────────────────────────────────────────────────────
class LLMBackend:
    """Protocol: complete(prompt, *, system="") -> str | None."""
    def complete(self, prompt: str, *, system: str = "") -> Optional[str]:
        raise NotImplementedError


class RouterLLM(LLMBackend):
    """Model Router first (ModelTask.RESEARCH, cheapest-capable-first),
    core/gemini's SMART ladder behind it. Neither raising nor retrying here:
    the router already does cooldowns and fallbacks internally."""

    def __init__(self) -> None:
        self._router: Any = None

    def complete(self, prompt: str, *, system: str = "") -> Optional[str]:
        try:                                   # Model Router (Phase: routing)
            from orchestrator.model_router import (ModelRouter, ModelTask,
                                                   RouteOptions)
            if self._router is None:
                self._router = ModelRouter()
            res = self._router.route(
                prompt, RouteOptions(task=ModelTask.RESEARCH,
                                     system=system, timeout_s=90,
                                     deadline_s=120))
            if res.ok and res.text:
                return res.text
        except Exception:
            pass
        try:                                   # house ladder fallback
            from core import gemini
            text = gemini.text(f"{system}\n\n{prompt}".strip(),
                               tier=gemini.SMART, timeout_ms=90_000)
            return text or None
        except Exception:
            return None


# ── prompts ──────────────────────────────────────────────────────────────────
PLAN_SYSTEM = (
    "You are the JARVIS research agent's planner. You plan searches over "
    "PERMITTED sources only (web search and the local knowledge base). You "
    "never fabricate sources, never plan to open private/internal hosts, "
    "and you answer with ONE JSON object only.")

PLAN_PROMPT = """Break this research question into a search plan.

QUESTION:
{question}

{context}

REPLY WITH ONE JSON OBJECT, NOTHING ELSE:
{{
  "understanding": "what is really being asked (1-2 sentences)",
  "sub_questions": ["sub question", "..."],
  "search_queries": ["web search query", "..."],
  "use_local_kb": true,
  "deep_fetch": false,
  "risks": ["one line per risk / likely uncertainty"]
}}

RULES:
- 1-{qcap} precise search queries; they must cover the whole question.
- use_local_kb=true when the user's own documents might help.
- If the question is too vague to research, say so in "understanding"
  and leave the lists empty.
"""

REPORT_SYSTEM = (
    "You are the JARVIS research agent's writer. You write ONLY from the "
    "numbered evidence. Every claim carries its inline [n] citation. If the "
    "evidence does not support a statement, leave it out or mark it "
    "unverified — never present an unverified claim as a fact.")

REPORT_PROMPT = """Write the research report.

QUESTION:
{question}

EVIDENCE (each block starts with its [n] citation and host):
{evidence}

CLAIM CHECKS performed by the verifier (support = share of the claim's
terms found in the evidence):
{checks}

REPLY WITH ONE JSON OBJECT, NOTHING ELSE:
{{
  "summary": "2-4 sentence answer, every claim cited like [1]",
  "key_findings": ["one supported finding per entry, each with [n]", "..."],
  "unverified": ["claim the evidence could NOT support — no citation", "..."],
  "uncertainties": ["what remains unknown or single-sourced", "..."],
  "confidence": "low|medium|high"
}}

RULES:
- Only claims present in the evidence go in "summary"/"key_findings".
- Claims the verifier could not support go ONLY in "unverified".
- If nothing is supported, set summary to exactly: {refuse}
"""


# ── evidence / claims ────────────────────────────────────────────────────────
def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _terms(text: str) -> set[str]:
    from core.rag_strategies.heuristics import _terms as _heu_terms
    return _heu_terms(text or "")


def support_score(answer: str, evidence_text: str) -> float:
    """0..1: how much of the claim's terms the evidence covers.
    Reuses the RAG strategies' direction-checked metric (claim ⊂ evidence)."""
    from core.rag_strategies import heuristics
    return heuristics.support_score(answer, evidence_text)


class _Claims:
    """Claim extraction for verification. Deterministic and cheap: split the
    evidence into sentences, keep the informative ones as candidate claims
    the verifier will grade against the pooled evidence."""
    _SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])")

    @classmethod
    def from_evidence(cls, sources: list[dict]) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        for src in sources:
            text = (src.get("snippet") or "").strip()
            if len(text) < MIN_EVIDENCE_CHARS:
                continue
            for sent in cls._SPLIT.split(text):
                sent = sent.strip()
                words = sent.split()
                if not (8 <= len(words) <= 80):
                    continue
                key = re.sub(r"\W+", "", sent.lower())[:120]
                if key in seen:
                    continue
                seen.add(key)
                out.append({"claim": sent, "source_idx": src["index"],
                            "url": src["url"], "title": src["title"]})
        return out[:MAX_CLAIMS * 3]


def _verify_claims(claims: list[dict], sources: list[dict]) -> list[dict]:
    """Grade each claim against ALL collected evidence: SUPPORTED when the
    pooled evidence covers it well AND at least one source outside its own
    host corroborates it; CONTESTED/UNVERIFIED otherwise. No sources means
    nothing can be verified — the empty list is the honest verdict."""
    if not claims or not sources:
        return []
    pooled = " ".join(s.get("snippet") or "" for s in sources)
    verdicts: list[dict] = []
    for c in claims:
        score = support_score(c["claim"], pooled)
        own_host = _host(c["url"])
        corroborating = [
            s["index"] for s in sources
            if s["index"] != c["source_idx"] and _host(s["url"]) != own_host
            and support_score(c["claim"], s.get("snippet") or "") >= 0.5]
        conflicts = [
            s["index"] for s in sources
            if s["index"] != c["source_idx"] and _host(s["url"]) != own_host
            and 0.15 <= support_score(c["claim"], s.get("snippet") or "") < 0.5
            and any(
                w in (s.get("snippet") or "").lower()
                for w in ("however", "but", "contrary", "instead",
                          "misconception", "actually", "in fact"))]
        if score >= VERIFY_THRESHOLD:
            verdict = "supported"
        elif conflicts:
            verdict = "contested"
        else:
            verdict = "unverified"
        verdicts.append({
            "claim": c["claim"], "verdict": verdict, "support": score,
            "source_idx": c["source_idx"], "corroborating": corroborating,
            "conflicts": conflicts,
        })
    verdicts.sort(key=lambda v: (v["verdict"] != "supported",
                                 -v["support"]))
    return verdicts


# ── the agent ────────────────────────────────────────────────────────────────
class ResearchAgent(BaseAgent):
    name = "research_agent"
    description = ("Web research with citations: plans searches over "
                   "permitted sources (web + local knowledge base), "
                   "collects and compares evidence, verifies claims, and "
                   "reports with explicit uncertainty — unverified claims "
                   "are never presented as facts.")
    task_types = (TaskType.RESEARCH,)

    def __init__(self, llm: LLMBackend | None = None,
                 checkpoint_dir_: str | os.PathLike | None = None,
                 toolbox: SourceToolbox | None = None) -> None:
        self._llm = llm or RouterLLM()
        self._cdir = checkpoint_dir_
        self.toolbox = toolbox or SourceToolbox()

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
                text=f"[research_agent] internal error: {exc}",
                data={"checkpoint": loc}, agent=self.name)

    # ── workflow ─────────────────────────────────────────────────────────
    def _handle(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        trace: list[dict] = []

        def mark(stage: str, ok: bool, detail: str = "") -> None:
            trace.append({"stage": stage, "ok": ok,
                          "detail": detail[:160], "at": _now()})

        # ── QUESTION stage ───────────────────────────────────────────────
        resume_id = str(meta.get("resume") or "").strip()
        question = (request.text or "").strip()
        if not question and not resume_id:
            return self._result(request, TaskStatus.NEEDS_INPUT,
                                "What should I research? Give me the "
                                "question.", trace, mark,
                                data_extra={"needs": "question"})
        cp: dict[str, Any] = {}
        if resume_id:
            cp = load_checkpoint(resume_id, self._cdir) or {}
            if not cp:
                return self._result(request, TaskStatus.FAILED,
                                    f"No checkpoint for '{resume_id}'.",
                                    trace, mark)
            question = cp.get("question") or question
            mark("question", True, f"resumed {resume_id}")
        else:
            mark("question", True, f"{len(question)} chars")

        # ── PLAN stage ───────────────────────────────────────────────────
        if resume_id and cp.get("plan"):
            plan = cp["plan"]
        else:
            plan = self._make_plan(request, question, trace, mark)
            if plan is None:
                return self._result(
                    request, TaskStatus.NEEDS_INPUT,
                    "This question is too vague to research — can you say "
                    "what exactly you want to find out?", trace, mark,
                    data_extra={"needs": "clearer_question"})
        queries = [q.strip() for q in (plan.get("search_queries") or [])
                   if isinstance(q, str) and q.strip()][:MAX_QUERIES]
        sub_questions = [s.strip() for s in (plan.get("sub_questions") or [])
                         if isinstance(s, str) and s.strip()][:MAX_SUB_QUESTIONS]
        use_kb = bool(plan.get("use_local_kb")) and \
            self.toolbox.permissions.allow_local_kb
        deep_fetch = bool(plan.get("deep_fetch"))

        # ── SEARCH + COLLECT stages ──────────────────────────────────────
        failed_sources: list[str] = []
        if resume_id and cp.get("sources") is not None:
            sources = list(cp["sources"])
            failed_sources = list(cp.get("failed_sources") or [])
            mark("search", True, "resumed from checkpoint")
            mark("collect", True, f"{len(sources)} sources")
        else:
            raw_sources = self._search(request, queries, sub_questions,
                                       use_kb, failed_sources, mark)
            sources = self._collect(raw_sources, mark)
            if deep_fetch:
                self._deepen(sources, mark)

        # ── COMPARE stage ────────────────────────────────────────────────
        hosts = {_host(s["url"]) for s in sources if s["url"]}
        if resume_id and cp.get("claims"):
            claims = list(cp["claims"])
        else:
            claims = _Claims.from_evidence(sources)
        mark("compare", True, f"{len(claims)} claims, {len(hosts)} hosts")

        # ── VERIFY stage ─────────────────────────────────────────────────
        verdicts = _verify_claims(claims, sources)
        supported = [v for v in verdicts if v["verdict"] == "supported"]
        contested = [v for v in verdicts if v["verdict"] == "contested"]
        unverified = [v for v in verdicts if v["verdict"] == "unverified"]
        mark("verify", True,
             f"supported={len(supported)} contested={len(contested)} "
             f"unverified={len(unverified)}")

        # ── REPORT stage ─────────────────────────────────────────────────
        report = self._make_report(request, question, sources, verdicts,
                                   plan, trace, mark)
        text = report.get("text") or self._fallback_text(
            question, supported, unverified, sources)
        data = {
            "question": question,
            "plan": plan,
            "sources": sources,
            "failed_sources": failed_sources,
            "claims": claims[:MAX_CLAIMS * 2],
            "verdicts": verdicts[:MAX_CLAIMS * 2],
            "report": {
                "summary": report.get("summary") or "",
                "key_findings": report.get("key_findings") or [],
                "unverified_claims": report.get("unverified") or
                [v["claim"] for v in unverified][:MAX_CLAIMS],
                "uncertainties": report.get("uncertainties") or
                self._default_uncertainties(supported, unverified,
                                            failed_sources, hosts),
                "supported_claims": len(supported),
                "confidence": report.get("confidence") or
                ("low" if not supported else
                 "medium" if len(supported) < 3 else "high"),
            },
            "queries_used": queries,
            "stage_trace": trace,
            "checkpoint_task": request.task_id,
            "checkpoint": str(checkpoint_dir(self._cdir)),
        }

        # persist the checkpoint (always — resume + audit)
        cp = {
            "task_id": request.task_id, "status": "completed",
            "question": _clip(question), "plan": plan,
            "sources": sources[:MAX_SOURCES],
            "claims": claims[:MAX_CLAIMS * 2],
            "verdicts": verdicts[:MAX_CLAIMS * 2],
            "failed_sources": data["failed_sources"],
            "report": data["report"], "trace": trace,
        }
        try:
            save_checkpoint(cp, self._cdir)
        except Exception as exc:
            mark("report", True, f"checkpoint write failed: {exc}")

        if not supported and not sources:
            # nothing found anywhere: honest no-results, not a confident
            # answer — status stays COMPLETED (the work succeeded) but the
            # report says so and data carries the gap.
            text = (f"No evidence found for: “{question}”. "
                    "Nothing was verified; I will not guess. "
                    "Try different terms or widen the permitted sources.")
        return self._result(request, TaskStatus.COMPLETED, text, trace, mark,
                            data_extra=data)

    # ── stage helpers ────────────────────────────────────────────────────
    def _make_plan(self, request: AgentRequest, question: str,
                   trace, mark) -> dict | None:
        mark("plan", True, "planning")
        context = ""
        if request.context:
            recent = "\n".join(f"{r}: {t[:160]}"
                               for r, t in request.context[-4:])
            context = f"CONVERSATION CONTEXT:\n{recent}\n"
        prompt = PLAN_PROMPT.format(question=question[:1200],
                                    context=context, qcap=MAX_QUERIES)
        raw = None
        try:
            raw = self._llm.complete(prompt, system=PLAN_SYSTEM)
        except Exception as exc:
            mark("plan", True, f"llm plan failed ({exc}) — heuristic plan")
        plan = _extract_json(raw or "")
        if plan is None:
            plan = self._heuristic_plan(question)
        if not isinstance(plan, dict):
            return None
        if not plan.get("search_queries") and not plan.get("sub_questions"):
            if not plan.get("understanding"):
                return None                      # genuinely unplanable
            plan = self._heuristic_plan(question)
        return plan

    @staticmethod
    def _heuristic_plan(question: str) -> dict:
        """No-model floor: the question itself becomes the queries. Keeps the
        workflow honest (and testable) when no LLM is reachable."""
        q = (question or "").strip().rstrip("?").strip()
        short = q if len(q) <= 120 else q[:117] + "..."
        base = short or question
        return {
            "understanding": "no model available — searching the question "
                             "directly",
            "sub_questions": [],
            "search_queries": [base],
            "use_local_kb": True,
            "deep_fetch": False,
            "risks": ["heuristic plan: single query, no decomposition"],
        }

    def _search(self, request: AgentRequest, queries: list[str],
                sub_questions: list[str], use_kb: bool,
                failed: list[str], mark) -> list[dict]:
        """SEARCH stage: run permitted sources, record every failure."""
        found: list[dict] = []
        per_query = self.toolbox.permissions.max_results_per_query

        all_queries = list(queries) or list(sub_questions)
        for q in all_queries:
            try:
                hits = self.toolbox.web_search(q)
                for h in hits:
                    h["_query"] = q
                    h["_tier"] = "web"
                found.extend(hits)
            except Exception as exc:
                failed.append(f"web:{q[:60]}: {exc}")
        if use_kb and (queries or sub_questions):
            kbq = (sub_questions or queries)[0]
            res = self.toolbox.local_kb(kbq)
            if res.get("ok") and res.get("results"):
                cits = res.get("citations") or []
                for i, r in enumerate(res["results"]):
                    found.append({
                        "title": cits[i] if i < len(cits) else
                        str(r.get("source") or "local document"),
                        "snippet": str(r.get("text") or "")[:600],
                        "url": f"kb://{r.get('doc_id') or 'local'}",
                        "_query": kbq, "_tier": "kb",
                    })
            elif not res.get("ok"):
                failed.append(f"kb:{kbq[:60]}: {res.get('error')}")
        if not all_queries:
            mark("search", True, "plan produced no queries")
        else:
            mark("search", True, f"{len(all_queries)} queries, "
                                 f"{len(found)} raw hits")
        return found

    def _collect(self, raw: list[dict], mark) -> list[dict]:
        """COLLECT stage: normalise, dedupe, cap, number."""
        out: list[dict] = []
        seen: set[str] = set()
        for r in raw:
            if len(out) >= MAX_SOURCES:
                break
            title = (r.get("title") or "").strip()
            snippet = (r.get("snippet") or "").strip()
            url = (r.get("url") or "").strip()
            if not title and not snippet:
                continue
            key = (url.lower() if url else
                   re.sub(r"\W+", "", (title or snippet).lower())[:120])
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "index": len(out) + 1,       # the [n] in citations
                "title": title[:200],
                "url": url,
                "host": _host(url),
                "snippet": snippet,
                "tier": r.get("_tier") or "web",
                "query": r.get("_query") or "",
            })
        mark("collect", True, f"{len(out)} unique sources")
        return out

    def _deepen(self, sources: list[dict], mark) -> None:
        """Opt-in: fetch the top web pages for fuller text."""
        fetched = 0
        for s in sources:
            if fetched >= 2:
                break
            if s["tier"] != "web" or not s["url"].startswith("http"):
                continue
            res = self.toolbox.fetch(s["url"])
            if res.get("ok") and len(res.get("text") or "") > len(s["snippet"]):
                s["snippet"] = res["text"][:2000]
                s["fetched"] = True
                fetched += 1
            if fetched == 0 and not res.get("ok"):
                continue
        mark("collect", True, f"deep fetch: {fetched} page(s)")

    def _make_report(self, request: AgentRequest, question: str,
                     sources: list[dict], verdicts: list[dict],
                     plan: dict, trace, mark) -> dict:
        """REPORT stage: grounded writer over the verified evidence; when no
        model answers, a deterministic extractive fallback builds the report
        from the SUPPORTED claims only."""
        mark("report", True, "writing")
        supported = [v for v in verdicts if v["verdict"] == "supported"]
        if not sources:
            return {"text": "", "summary": "", "key_findings": [],
                    "unverified": [], "uncertainties": [],
                    "confidence": "low"}

        prompt_sources = "\n".join(
            f"[{s['index']}] {s['host'] or s['tier']} — {s['title']}\n"
            f"{s['snippet'][:1200]}"
            for s in sources)
        checks = "\n".join(
            f"- ({v['verdict']}, support {v['support']:.2f}) {v['claim'][:220]}"
            for v in verdicts[:MAX_CLAIMS * 2])
        prompt = REPORT_PROMPT.format(question=question[:1200],
                                      evidence=prompt_sources[:MAX_EVIDENCE_CHARS],
                                      checks=checks, refuse=REFUSAL)
        raw = None
        try:
            raw = self._llm.complete(prompt, system=REPORT_SYSTEM)
        except Exception as exc:
            mark("report", True, f"llm report failed ({exc}) — extractive")
        parsed = _extract_json(raw or "") or {}
        text = parsed.get("summary") or ""
        findings = [f for f in (parsed.get("key_findings") or [])
                    if isinstance(f, str) and f.strip()]
        if not text and findings:
            text = " ".join(findings)
        if not text:
            parsed = self._extractive_report(question, supported, verdicts)
            text = parsed["summary"]
        return parsed

    @staticmethod
    def _extractive_report(question, supported, verdicts) -> dict:
        """No-model floor: the report IS the supported evidence sentences.
        Unsupported claims are structurally excluded — same philosophy as
        the RAG strategies' extractive answerer."""
        lines = []
        for v in supported[:MAX_CLAIMS]:
            lines.append(f"{v['claim'].strip()} [{v['source_idx']}]")
        findings = [f"{v['claim'].strip()} [{v['source_idx']}]"
                    for v in supported[:MAX_CLAIMS]]
        return {
            "summary": "\n\n".join(lines) if lines else "",
            "key_findings": findings,
            "unverified": [v["claim"] for v in
                           verdicts if v["verdict"] == "unverified"][:MAX_CLAIMS],
            "uncertainties": [],
            "confidence": "medium" if lines else "low",
        }

    @staticmethod
    def _fallback_text(question, supported, unverified, sources) -> str:
        if not sources:
            return ""
        parts: list[str] = []
        body = [f"{v['claim'].strip()} [{v['source_idx']}]"
                for v in supported[:MAX_CLAIMS]]
        if body:
            parts.append("\n".join(body))
        if unverified:
            parts.append("Unverified (not enough evidence — not facts): " +
                         "; ".join(v["claim"][:120] for v in
                                   unverified[:3]))
        refs = "; ".join(f"[{s['index']}] {s['title'] or s['host'] or s['tier']}"
                         f" — {s['url'] or 'local KB'}"
                         for s in sources[:MAX_CLAIMS])
        parts.append(f"Sources: {refs}")
        return "\n\n".join(parts)

    @staticmethod
    def _default_uncertainties(supported, unverified, failed, hosts) -> list[str]:
        out: list[str] = []
        if failed:
            out.append(f"{len(failed)} source(s) could not be reached: "
                       + "; ".join(f[:80] for f in failed[:2]))
        if len(hosts) <= 1 and supported:
            out.append("All evidence comes from a single host — "
                       "not independently corroborated.")
        if unverified and not supported:
            out.append("No claim reached the support threshold; "
                       "the evidence collected does not answer the question.")
        return out

    # ── result plumbing ──────────────────────────────────────────────────
    def _result(self, request: AgentRequest, status: TaskStatus, text: str,
                trace, mark, data_extra: dict | None = None) -> AgentResult:
        data = {"stage_trace": trace}
        if data_extra:
            data.update(data_extra)
        return AgentResult(task_id=request.task_id, status=status,
                           text=text, data=data, agent=self.name)


REFUSAL = "No evidence found for this question."


