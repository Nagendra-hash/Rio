"""
orchestrator/agents/youtube/pipeline.py — run the pack in order.

Chains the six stage agents, writes every artifact under ``youtube/`` and keeps
a resumable state, so a run that stops (no model, no ffmpeg, a slow render, an
upload that needs credentials) picks up where it left off instead of starting
over.

    from orchestrator.agents.youtube import YouTubePipeline
    pipe = YouTubePipeline("~/Desktop/MyChannel")
    pipe.run(channel={"name": "My Channel", "pillars": ["Home espresso"]})

The pipeline NEVER raises to its caller: every stage failure is captured in the
returned summary and in ``youtube/state.json`` — and nothing is ever uploaded
without ``approved=True`` and ``publish=True``.
"""
from __future__ import annotations

import os
from pathlib import Path

from orchestrator.agents.youtube import agents, schema
from orchestrator.agents.youtube.llm import HouseLLM
from orchestrator.agents.youtube.project import YouTubeProject

ORDER = ("research", "script", "production", "review", "publish", "learn")


class YouTubePipeline:
    def __init__(self, workspace: str | os.PathLike, *, llm=None, tts=None,
                 youtube_dir: str | os.PathLike | None = None) -> None:
        self.workspace = Path(workspace).expanduser()
        self.youtube_dir = (Path(youtube_dir).expanduser() if youtube_dir
                            else self.workspace / "youtube")
        self.llm = llm if llm is not None else HouseLLM()
        # An injectable narration synthesizer tts(text, out_path) -> bool. The
        # pack has no TTS dependency; the host supplies its own (JARVIS's TTS).
        self.tts = tts
        self.project = YouTubeProject(root=self.youtube_dir)
        self.project.ensure()
        self.project.load_state()

    def run(self, *, channel: schema.Channel | dict | None = None,
            steps: list[str] | None = None, topic: str = "", count: int = 3,
            research_signals: list | None = None,
            source_catalog: list | None = None,
            approved_learnings: list | None = None,
            render: bool = False, provider: str = "slideshow",
            narration_file: str = "",
            approved: bool = False, publish: bool = False,
            publish_time: str = "", privacy: str = "",
            verified_sources: list | None = None, probes: dict | None = None,
            sources: list | None = None, claims: list | None = None,
            waive: list | None = None, waive_note: str = "",
            evidence: dict | None = None, retention_curves: dict | None = None,
            approve: list | None = None, reject: list | None = None,
            resume: bool = True) -> dict:
        wanted = tuple(steps) if steps else ORDER
        if channel is not None and isinstance(channel, schema.Channel):
            self.project.save_channel(channel)
        elif isinstance(channel, dict):
            self.project.save_channel(schema.parse_channel(channel))
        self.project.load_state()

        results: dict[str, dict] = {}
        for name in wanted:
            step = agents.BY_NAME.get(name)
            if step is None:
                results[name] = {"stage": name, "ok": False,
                                 "note": "unknown step"}
                continue
            if resume and self.project.done(name) and name not in ("publish",
                                                                   "learn"):
                results[name] = {"stage": name, "ok": True,
                                 "note": "already done (resumed)"}
                continue
            kwargs: dict = {}
            if name == "research":
                kwargs = {"channel": channel if isinstance(channel, schema.Channel)
                          else None, "topic": topic, "count": count,
                          "research_signals": research_signals,
                          "source_catalog": source_catalog,
                          "approved_learnings": approved_learnings}
            elif name == "script":
                kwargs = {"topic": topic}
            elif name == "production":
                kwargs = {"render": render, "provider": provider,
                          "narration_file": narration_file, "tts": self.tts}
            elif name == "review":
                kwargs = {"approved": approved,
                          "verified_sources": verified_sources,
                          "sources": sources, "claims": claims,
                          "waive": waive, "waive_note": waive_note,
                          "probes": probes}
            elif name == "publish":
                kwargs = {"approved": approved, "publish": publish,
                          "publish_time": publish_time, "privacy": privacy}
            elif name == "learn":
                kwargs = {"evidence": evidence,
                          "retention_curves": retention_curves,
                          "approve": approve, "reject": reject}
            try:
                results[name] = step.run(self.project, self.llm, **kwargs)
            except Exception as exc:                 # never break the caller
                results[name] = {"stage": name, "ok": False,
                                 "note": f"{type(exc).__name__}: {exc}"}

        decided = all(results[n].get("ok") for n in wanted
                      if n in results and n not in ("publish", "learn"))
        ready = bool((results.get("review") or {}).get("ready"))
        if publish and "publish" in results:
            ok = bool(results["publish"].get("ok"))
            ready = bool(results["publish"].get("allowed"))
        else:
            ok = decided
        self.project.save_state()
        return {
            "ok": ok,
            "ready": ready,
            "workspace": str(self.workspace),
            "youtube_dir": str(self.youtube_dir),
            "channel_name": self.project.channel_name,
            "stages": results,
            "artifacts": self.artifacts(),
            "note": _summary_note(results, publish),
        }

    def artifacts(self) -> list[str]:
        out: list[str] = []
        for p in sorted(self.youtube_dir.rglob("*")):
            if p.is_file():
                out.append(str(p))
        return out

    def status(self) -> dict:
        recs = self.project.load_recommendations()
        return {
            "channel_name": self.project.channel_name,
            "stages": self.project.state.get("stages") or {},
            "workspace": str(self.workspace),
            "artifacts": self.artifacts(),
            "pending_recommendations": [r for r in recs
                                        if (r.get("status") or "") == "pending"],
            "approved_recommendations": [r for r in recs
                                         if (r.get("status") or "") == "approved"],
        }


def _summary_note(results: dict, publish: bool) -> str:
    parts = [f"{n}:{'ok' if (results.get(n) or {}).get('ok') else 'x'}"
             for n in ORDER if n in results]
    if "publish" in results:
        parts.append("publish:" + ("allowed" if results["publish"].get("allowed")
                                   else "blocked"))
    else:
        ready = bool((results.get("review") or {}).get("ready"))
        parts.append("READY to publish" if ready else "NOT ready yet (see review)")
        parts.append("(publish not run — it needs approved=True and publish=True)")
    return " ".join(parts)
