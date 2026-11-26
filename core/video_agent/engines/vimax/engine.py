"""
core/video_agent/engines/vimax/engine.py — the VIMAX engine.

Wraps the EXISTING ViMax installation (~/JarvisTools/vimax, skill
`vimax-video`) as a registry engine. Mission rule: never reinstall or
duplicate it — the probe reads the live registry/skill/provider state and the
install action DELEGATES to the proven repo agent instead of reimplementing.

Honesty: probe() is configuration-level truth (installed, enabled, provider
keys present). Billing is verified only at run time — measured 2026-10-24,
both image/video rungs were provider-blocked (OpenRouter 402, Google 429
limit:0); that state lives in providers.probe_video_generator() and is
surfaced next to this engine, never hidden by it.
"""
from __future__ import annotations

from typing import Any

from core.video_agent.engines.base import (INSTALLABLE, NOT_CONFIGURED,
                                           READY_LOCAL, UNAVAILABLE,
                                           EngineDescriptor, EngineStatus,
                                           VideoEngine)

SKILL_ID = "vimax-video"


class VimaxEngine(VideoEngine):
    descriptor = EngineDescriptor(
        id="VIMAX",
        name="ViMax",
        version="fd4b72e7",
        summary=("Agentic AI cinematic pipeline (idea/script → story → "
                 "storyboard → shots → MP4) running in its own isolated uv "
                 "environment; generation goes through configured cloud "
                 "providers."),
        execution=("local", "remote"),      # pipeline runs locally, shots are
        # generated provider-side (remote)
        video_types=("ai_cinematic", "text_to_video", "image_to_video"),
        resolutions=("1080p", "720p"),
        duration_range_s=(10, 600),
        hardware_requirements={},           # provider-side generation
        hw_requirements_text=("CPU only for the pipeline itself; AI shot "
                              "generation happens on the configured cloud "
                              "provider (paid credits)"),
        estimated_resources={"ram_mb": 800, "disk_mb": 600},
        estimated_cost="paid provider credits (per generated shot)",
        privacy=("scripts/topics are sent to the configured LLM/image/video "
                 "providers — cloud consent is required at approval time"),
        install_supported=True,
        install_source="https://github.com/HKUDS/ViMax (repo agent: "
                       "inspect → risk scan → uv-isolated install)",
        install_size_mb=594.0,
    )

    def probe(self, hw: dict[str, Any] | None = None) -> EngineStatus:
        from core.repo_agent import manifests, skills

        reg = manifests.load_registry()
        repo = (reg.get("repos") or {}).get("vimax") or {}
        if not repo:
            return EngineStatus(
                INSTALLABLE,
                "not installed — the repo agent can install it in an "
                "isolated environment after approval",
                {"local": INSTALLABLE})

        view = skills.resolve_skill(SKILL_ID)
        entry = view.get("entry") or {}
        if not view.get("ok"):
            return EngineStatus(
                UNAVAILABLE,
                "repo present but the vimax-video skill manifest is missing "
                "— re-register via the repo agent",
                {"local": UNAVAILABLE})
        if not entry.get("enabled", True):
            return EngineStatus(UNAVAILABLE, "skill is disabled",
                                {"local": UNAVAILABLE})
        skill_status = str(entry.get("status") or "")
        if skill_status != "READY":
            return EngineStatus(
                NOT_CONFIGURED,
                f"installed, skill status {skill_status} — "
                f"{entry.get('detail') or 'provider API keys missing'}",
                {"local": NOT_CONFIGURED})

        from core.repo_agent import vimax as vimax_mod
        providers = vimax_mod.provider_status()
        if not providers.get("ready"):
            return EngineStatus(
                NOT_CONFIGURED,
                "provider keys missing: "
                + ", ".join(providers.get("missing") or []),
                {"local": NOT_CONFIGURED})
        return EngineStatus(
            READY_LOCAL,
            "installed, providers configured — generation spends PAID "
            "credits and billing is only proven at run time (last measured "
            "2026-10-24: both rungs provider-blocked); every run is "
            "approval-gated",
            {"local": READY_LOCAL, "remote": READY_LOCAL})

    def health_check(self) -> dict[str, Any]:
        """Deeper on-demand check (registry entry + skill manifest)."""
        from core.repo_agent import skills
        result = self.probe().as_dict()
        try:
            result["skill_health"] = skills.skill_health(SKILL_ID)
        except Exception as exc:                    # noqa: BLE001
            result["skill_health"] = {"ok": False,
                                      "error": str(exc)[:200]}
        return result

    def install(self, approve: bool = False, log=None) -> dict[str, Any]:
        """Delegate to the repo agent — never duplicate its installer."""
        if not approve:
            return {"ok": False, "approval_required": True,
                    "error": "install requires explicit approval",
                    "how": "Say 'Jarvis, install "
                           "https://github.com/HKUDS/ViMax' — the repo agent "
                           "inspects, risk-scans and installs it isolated."}
        from core.repo_agent import manager
        try:
            report = manager.RepositoryManager().inspect(
                "https://github.com/HKUDS/ViMax")
            return {"ok": False, "delegated": "repo agent",
                    "message": "ViMax installs through the repo agent "
                               "(detached worker, approval-gated).",
                    "inspect": report}
        except Exception as exc:                    # noqa: BLE001
            return {"ok": False,
                    "error": f"repo agent unavailable: {exc}"}

    def uninstall(self, approve: bool = False) -> dict[str, Any]:
        if not approve:
            return {"ok": False, "approval_required": True,
                    "error": "uninstall requires explicit approval "
                             "(repo agent removes the managed repo)"}
        return {"ok": False, "delegated": "repo agent",
                "how": "Remove via the repo agent tools page "
                       "('remove' with confirm: true) so the registry stays "
                       "consistent."}

    def start_params(self, spec: dict[str, Any],
                     parameters: dict[str, Any] | None = None) -> None:
        """None — ViMax jobs are run_skill jobs whose op/args mapping lives
        in actions/video_creator._create_vimax (image/script/scenes aware).
        The approval layer routes method=VIMAX there; no duplication here."""
        return None


ENGINE = VimaxEngine()
