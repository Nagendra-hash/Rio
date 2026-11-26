"""
orchestrator/agents/replica/pipeline.py — run the pack in order.

Chains the seven small agents, writes every artifact under ``replica/`` and
keeps a resumable state, so a run that stops (no network, no reviews, a slow
Gradle build) picks up where it left off instead of starting over.

    from orchestrator.agents.replica import ReplicaPipeline
    pipe = ReplicaPipeline("~/Desktop/MyRebuild")
    pipe.run(listing_url="https://play.google.com/store/apps/details?id=…")

The pipeline NEVER raises to its caller: every stage failure is captured in
the returned summary and in ``replica/state.json``.
"""

from __future__ import annotations

import os
from pathlib import Path

from orchestrator.agents.replica import agents
from orchestrator.agents.replica.llm import HouseLLM
from orchestrator.agents.replica.project import ReplicaProject

ORDER = ("recon", "architecture", "design", "critic", "enhance", "build", "verify")


class ReplicaPipeline:
    def __init__(
        self,
        workspace: str | os.PathLike,
        *,
        llm=None,
        replica_dir: str | os.PathLike | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser()
        self.replica_dir = (
            Path(replica_dir).expanduser()
            if replica_dir
            else self.workspace / "replica"
        )
        self.llm = llm if llm is not None else HouseLLM()
        self.project = ReplicaProject(root=self.replica_dir)
        self.project.ensure()
        self.project.load_state()

    def run(
        self,
        *,
        listing_url: str = "",
        listing: dict | None = None,
        app_name: str = "",
        steps: list[str] | None = None,
        build: bool = False,
        resume: bool = True,
        fail_under: float = 80.0,
        source_text: str = "",
        project_dir: str = "",
    ) -> dict:
        wanted = tuple(steps) if steps else ORDER
        if app_name:
            self.project.app_name = app_name
        if listing_url:
            self.project.listing_url = listing_url

        results: dict[str, dict] = {}
        fixes: list = []
        for name in wanted:
            step = agents.BY_NAME.get(name)
            if step is None:
                results[name] = {"stage": name, "ok": False, "note": "unknown step"}
                continue
            if resume and self.project.done(name) and name not in ("build", "verify"):
                results[name] = {
                    "stage": name,
                    "ok": True,
                    "note": "already done (resumed)",
                }
                continue
            try:
                kwargs: dict = {}
                if name == "recon":
                    kwargs = {
                        "listing": listing,
                        "listing_url": listing_url,
                        "source_text": source_text,
                    }
                elif name == "enhance":
                    kwargs = {"fixes": fixes}
                elif name == "build":
                    kwargs = {"build": build}
                elif name == "verify":
                    kwargs = {"project_dir": project_dir, "fail_under": fail_under}
                result = step.run(self.project, self.llm, **kwargs)
            except Exception as exc:  # never break the caller
                result = {
                    "stage": name,
                    "ok": False,
                    "note": f"{type(exc).__name__}: {exc}",
                }
            results[name] = result
            if name == "critic":
                fixes = result.get("fixes") or []

        ok = (
            bool(results.get("verify", {}).get("ok"))
            if build
            else all(
                r.get("ok") for k, r in results.items() if k not in ("build", "verify")
            )
        )
        ready = bool(results.get("verify", {}).get("ready"))
        self.project.save_state()
        return {
            "ok": ok,
            "ready": ready,
            "workspace": str(self.workspace),
            "replica_dir": str(self.replica_dir),
            "app_name": self.project.app_name,
            "stages": results,
            "artifacts": self.artifacts(),
            "note": _summary_note(results, build),
        }

    def artifacts(self) -> list[str]:
        out: list[str] = []
        for p in sorted(self.replica_dir.rglob("*")):
            if p.is_file():
                out.append(str(p))
        return out

    def status(self) -> dict:
        return {
            "app_name": self.project.app_name,
            "listing_url": self.project.listing_url,
            "stages": self.project.state.get("stages") or {},
            "workspace": str(self.workspace),
            "artifacts": self.artifacts(),
        }


def _summary_note(results: dict, build: bool) -> str:
    parts = []
    for name in ORDER:
        r = results.get(name)
        if not r:
            continue
        parts.append(f"{name}:{'ok' if r.get('ok') else 'x'}")
    if not build:
        parts.append("(build skipped — pass build/action=build to compile an APK)")
    return " ".join(parts)
