"""
core/video_agent/script_writer.py — SCRIPT_WRITER role for the video agent.

Uses the EXISTING house model router (orchestrator.model_router, task
RESEARCH — free chain first, Gemini last and budget-guarded) with
core.gemini's SMART tier behind it. This mirrors the proven `HouseLLM`
pattern in orchestrator/agents/youtube/llm.py:

  * the router itself implements the mission's fallback
    (free cloud LLM → another free LLM → local model) — no duplicate router
  * the LLM is injectable so tests run offline and a stage never fabricates
  * every helper returns a result dict; failure is honest (ok=False with the
    real reason), it never raises out of a pipeline stage

Task choice, documented: ModelTask.RESEARCH — (a) documentary scripts are
factual knowledge content, (b) unlike DIFFICULT_REASONING the RESEARCH chain
keeps the free tiers first in smart mode (router.py puts Gemini first on
DIFFICULT_REASONING, which would burn the paid budget by default).
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

SCRIPT_TASK_TIMEOUT_S = 180
SCRIPT_TASK_DEADLINE_S = 240


class HouseLLM:
    """The router-first, gemini-behind-it LLM. Injectable twin of the
    youtube agent's HouseLLM (kept separate so video policy can evolve
    without touching that agent)."""

    name = "model-router(research)"

    def __init__(self) -> None:
        self._router: Any = None
        self.hops: list[dict] = []

    def text(self, prompt: str, *, system: str = "") -> Optional[str]:
        try:
            from orchestrator.model_router import (ModelRouter, ModelTask,
                                                   RouteOptions)
            if self._router is None:
                self._router = ModelRouter()
            res = self._router.route(
                prompt, RouteOptions(task=ModelTask.RESEARCH, system=system,
                                     timeout_s=SCRIPT_TASK_TIMEOUT_S,
                                     deadline_s=SCRIPT_TASK_DEADLINE_S))
            self.hops = [vars(h) for h in (getattr(res, "hops", None) or [])]
            if res.ok and res.text:
                self.name = f"model-router:{getattr(res, 'provider', '?')}"
                return res.text
        except Exception:                          # noqa: BLE001
            pass
        try:
            from core import gemini
            out = gemini.text(f"{system}\n\n{prompt}".strip(),
                              tier=gemini.SMART,
                              timeout_ms=SCRIPT_TASK_TIMEOUT_S * 1000)
            if out:
                self.name = "core-gemini(smart)"
                return out
        except Exception:                          # noqa: BLE001
            pass
        return None


def extract_json(raw: Optional[str]) -> Optional[dict]:
    """Robust JSON extraction (```json fences, leading prose) — the house
    pattern from orchestrator/agents/youtube/llm.py."""
    if not raw:
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start:end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:                              # noqa: BLE001
        return None


def _script_prompt(spec: dict) -> tuple[str, str]:
    """Documentary script prompt: hook, chapters, transitions, strong ending,
    narration timing estimates — calm clear tone, no fabricated facts."""
    mode = str(spec.get("mode") or "DOCUMENTARY")
    duration = int(spec.get("duration_s") or 600)
    if mode == "SHORTS":
        chapters = ("3 short beats: hook (first 3 seconds), one core "
                    "explanation, a closing line")
        words = f"roughly {int(duration * 2.6)} words total (spoken pace)"
    else:
        chapters = ("4-8 clear chapters with natural transitions between them")
        words = f"roughly {int(duration * 2.6)} words total (spoken pace)"
    system = (
        "You are a documentary scriptwriter. Calm, clear, informative tone. "
        "No purple prose, no repeated phrases, no invented statistics, "
        "quotes or sources — if a claim needs a source, mark it "
        "[NEEDS SOURCE] instead of fabricating. Distinguish facts from "
        "interpretation. Ground factual claims in the available_sources "
        "when they support them; never invent additional sources. Respond "
        "with ONE JSON object and nothing else.")
    # permanent cast: when the default characters are requested, the script
    # must feature them (the Expert teaches, the Boy learns — per the sheet)
    if str(spec.get("characters") or "default") not in ("", "none"):
        try:
            from core.video_agent import characters as cast
            frag = cast.prompt_fragment()
            if frag:
                system += (" The video features the user's PERMANENT CAST — "
                           "write the narration as their dialogue/lesson and "
                           "keep their personalities consistent:\n" + frag)
        except Exception:                          # noqa: BLE001
            pass
    ask: dict[str, Any] = {
        "task": "write a documentary narration script",
        "mode": mode,
        "topic": spec.get("topic"),
        "duration_s": duration,
        "language": spec.get("language") or "simple English",
        "audience": spec.get("audience") or "general",
        "structure": {
            "opening_hook": "one narration paragraph that earns attention",
            "chapters": chapters,
            "transitions": "one bridging line between chapters",
            "ending": "a strong, honest closing paragraph",
        },
        "length": words,
        "output_format": {
            "title": "string",
            "hook": "string (narration text)",
            "chapters": [{"title": "string", "narration": "string",
                          "visual_notes": "string",
                          "estimated_seconds": "integer"}],
            "ending": "string (narration text)",
            "estimated_total_seconds": "integer",
            "claims_needing_sources": ["string"],
            "sources_used": ["urls from available_sources that ground claims"],
        },
    }
    sources = spec.get("sources") or []
    if sources:
        ask["available_sources"] = [
            {"title": s.get("title", ""), "url": s.get("url", ""),
             "snippet": s.get("snippet", "")} for s in sources[:8]]
    return json.dumps(ask, ensure_ascii=False), system


def generate_script(spec: dict, llm: Any = None) -> dict[str, Any]:
    """Generate a structured documentary/shorts script. Returns
    {ok, script?, provider, raw?, error?} — ok=False is an honest failure
    the calling stage turns into NEEDS_CONFIGURATION/FAILED, never a
    fabricated script."""
    llm = llm or HouseLLM()
    prompt, system = _script_prompt(spec)
    raw = llm.text(prompt, system=system)
    if not raw:
        return {"ok": False, "provider": getattr(llm, "name", "?"),
                "error": ("no model in the chain returned a script — the "
                          "router's free rungs and the Gemini fallback all "
                          "failed; nothing was fabricated")}
    data = extract_json(raw)
    if not data or not str(data.get("title") or "").strip():
        return {"ok": False, "provider": getattr(llm, "name", "?"),
                "raw": raw[:400],
                "error": "model reply was not a parsable script JSON"}
    # normalize + timing sanity (estimates, clearly labelled as estimates)
    chapters = data.get("chapters") or []
    est_total = 0
    for ch in chapters if isinstance(chapters, list) else []:
        try:
            ch["estimated_seconds"] = max(3, int(ch.get("estimated_seconds")
                                                 or 0))
        except (TypeError, ValueError):
            ch["estimated_seconds"] = 30
        est_total += ch["estimated_seconds"]
    return {"ok": True, "provider": getattr(llm, "name", "?"),
            "script": {
                "title": str(data.get("title") or "")[:200],
                "mode": spec.get("mode"),
                "topic": spec.get("topic"),
                "hook": str(data.get("hook") or ""),
                "chapters": chapters,
                "ending": str(data.get("ending") or ""),
                "estimated_total_seconds": est_total,
                "claims_needing_sources": list(
                    data.get("claims_needing_sources") or []),
                "sources_used": [str(u) for u in
                                 (data.get("sources_used") or [])][:12],
                "timing_is_estimate": True,
            },
            "raw_chars": len(raw)}
