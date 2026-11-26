"""
core/video_agent/engines/base.py — the engine plugin contract (Phase 2).

An engine is ONE video-generation method. Every engine exposes the mission's
Phase 2 surface as data (descriptor) plus measured truth (probe). The registry
(`registry.py`) discovers engine modules; the core orchestration system never
needs modification to add one — drop a folder `engines/<id>/engine.py` that
exports `ENGINE = MyEngine()` and it appears everywhere.

Classification statuses (mission Phase 3, exact vocabulary):

    READY_LOCAL      verified working on this machine (real probe, not hope)
    READY_REMOTE     verified working through a configured remote provider
    INSTALLABLE      this hardware could run it after an approved install
    INCOMPATIBLE     this hardware can never satisfy it (no driver, no VRAM)
    NOT_CONFIGURED   installed/capable but user configuration is missing
    UNAVAILABLE      not installed and not installable here right now

Honesty rule: an engine is NEVER marked READY unless its dependencies AND its
inference/rendering backend were verified by a real probe (binary runs, skill
READY, provider answers). Presence of files alone is never readiness.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

READY_LOCAL = "READY_LOCAL"
READY_REMOTE = "READY_REMOTE"
INSTALLABLE = "INSTALLABLE"
INCOMPATIBLE = "INCOMPATIBLE"
NOT_CONFIGURED = "NOT_CONFIGURED"
UNAVAILABLE = "UNAVAILABLE"
STATUSES = (READY_LOCAL, READY_REMOTE, INSTALLABLE, INCOMPATIBLE,
            NOT_CONFIGURED, UNAVAILABLE)

# Aggregated status ordering: the best honest state wins when an engine has
# several execution paths (local vs remote).
_STATUS_RANK = {READY_LOCAL: 0, READY_REMOTE: 0, INSTALLABLE: 1,
                NOT_CONFIGURED: 2, UNAVAILABLE: 3, INCOMPATIBLE: 4}


def best_status(paths: dict[str, str]) -> str:
    """Overall status from per-path statuses (local/remote)."""
    values = [v for v in paths.values() if v in _STATUS_RANK]
    if not values:
        return UNAVAILABLE
    return sorted(values, key=lambda s: _STATUS_RANK[s])[0]


@dataclass(frozen=True)
class EngineDescriptor:
    """Static mission-Phase-2 surface — what this engine IS."""
    id: str
    name: str
    version: str
    summary: str
    execution: tuple[str, ...]              # subset of ("local", "remote")
    video_types: tuple[str, ...]            # content kinds it can produce
    resolutions: tuple[str, ...]
    duration_range_s: tuple[int, int]       # (min, max) accepted
    hardware_requirements: dict = field(default_factory=dict)   # machine
    hw_requirements_text: str = ""          # human
    estimated_resources: dict = field(default_factory=dict)
    estimated_cost: str = "free"
    privacy: str = "runs locally — no user data leaves the machine"
    install_supported: bool = False
    install_source: str = ""                # verified/official source only
    install_size_mb: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "version": self.version,
            "summary": self.summary, "execution": list(self.execution),
            "video_types": list(self.video_types),
            "resolutions": list(self.resolutions),
            "duration_range_s": list(self.duration_range_s),
            "hardware_requirements": dict(self.hardware_requirements),
            "hw_requirements_text": self.hw_requirements_text,
            "estimated_resources": dict(self.estimated_resources),
            "estimated_cost": self.estimated_cost,
            "privacy": self.privacy,
            "install": {
                "supported": self.install_supported,
                "source": self.install_source,
                "size_mb": self.install_size_mb,
            },
        }


@dataclass
class EngineStatus:
    """Measured truth — what this engine can do RIGHT NOW."""
    status: str
    detail: str
    paths: dict[str, str] = field(default_factory=dict)
    checked_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime()))

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail,
                "paths": dict(self.paths), "checked_at": self.checked_at}

    def is_runnable(self) -> bool:
        return self.status in (READY_LOCAL, READY_REMOTE)


class VideoEngine:
    """Base class — subclass and export `ENGINE = MyEngine()`."""

    descriptor: EngineDescriptor

    def probe(self, hw: dict[str, Any] | None = None) -> EngineStatus:
        """Measured capability. `hw` = an existing hardware.probe() report
        (pass one to avoid re-probing; engines must never REQUIRE one)."""
        raise NotImplementedError

    def health_check(self) -> dict[str, Any]:
        """Deeper check on demand (RIO health surface). Default = probe."""
        return self.probe().as_dict()

    def install_plan(self) -> dict[str, Any] | None:
        """What an approved install would do. None = not installable."""
        return None

    def install(self, approve: bool = False, log=None) -> dict[str, Any]:
        """Install ONLY after explicit approval. Default: refuse."""
        return {"ok": False, "error": "this engine has no installer",
                "approval_required": True, "approved": bool(approve)}

    def uninstall(self, approve: bool = False) -> dict[str, Any]:
        return {"ok": False, "error": "nothing managed to uninstall",
                "approval_required": True, "approved": bool(approve)}

    def start_params(self, spec: dict[str, Any],
                     parameters: dict[str, Any] | None = None
                     ) -> dict[str, Any] | None:
        """Job-store launch triple (kind/ref/params) for this engine, or None
        when the engine cannot run this spec. The approval layer calls this —
        engines never spawn jobs themselves."""
        return None

    def as_dict(self, status: EngineStatus | None = None) -> dict[str, Any]:
        st = (status or self.probe()).as_dict()
        return {"descriptor": self.descriptor.as_dict(), "status": st}
