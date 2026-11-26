"""
core/scientific/config.py — settings and provenance for the Science Agent.

Everything is filesystem-based and stdlib-only so the registry can be read by
the agent, the action and the tests without importing numpy or pandas.

Two roots, deliberately separate:
    vendor/scientific-agent-skills/skills   pristine upstream files (read-only)
    memory/scientific/registry.json         our state: enabled, notes, status

Keeping the flags out of the vendored tree means a hash of an upstream skill
never changes because we toggled it, and a re-vendor can never resurrect a
skill somebody disabled.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[2]

# ── upstream (pinned) ───────────────────────────────────────────────────────
VENDOR_DIR = BASE_DIR / "vendor" / "scientific-agent-skills"
SKILLS_ROOT = VENDOR_DIR / "skills"
UPSTREAM_TAG = "v2.72.0"
UPSTREAM_COMMIT = "526ebce143bc326f44471554e33d66a19c840360"
UPSTREAM_REPO = "https://github.com/K-Dense-AI/scientific-agent-skills"

# ── our state ──────────────────────────────────────────────────────────────
STATE_DIR = BASE_DIR / "memory" / "scientific"
INDEX_PATH = STATE_DIR / "skills_index.json"
REGISTRY_PATH = STATE_DIR / "registry.json"
LOCK_PATH = BASE_DIR / "SCIENTIFIC_SKILLS_LOCK.json"
SECURITY_PATH = BASE_DIR / "SCIENTIFIC_SKILLS_SECURITY.json"
JOBS_DIR = STATE_DIR / "jobs"
REPORTS_DIR = STATE_DIR / "reports"
UPLOADS_DIR = BASE_DIR / "uploads"
RUNS_LOG = STATE_DIR / "runs.jsonl"

# The brief's "start with relevant skills". The other 161 are vendored and
# discoverable, but disabled until somebody enables them.
CORE_SKILLS: tuple[str, ...] = (
    "literature-review",
    "paper-lookup",
    "database-lookup",
    "research-lookup",
    "scientific-critical-thinking",
    "experimental-design",
    "statistical-analysis",
    "statistical-power",
    "exploratory-data-analysis",
    "scikit-learn",
    "scientific-visualization",
    "matplotlib",
    "seaborn",
    "scientific-writing",
    "citation-management",
    "hypothesis-generation",
)

# Never enabled by default: they either need licensed access, prescribe for a
# patient, or execute paid cloud compute.
GUARDED_SKILLS: dict[str, str] = {
    "clinical-decision-support": "patient-specific decision support is out of scope",
    "treatment-plans": "treatment planning requires a qualified clinician",
    "modal": "requires Modal cloud compute (paid)",
    "benchling-integration": "requires a Benchling tenant + credentials",
    "omero-integration": "requires an OMERO server + credentials",
    "rowan": "paid cloud chemistry platform",
    "tamarind": "paid cloud platform",
    "alphagenome": "requires a registered API key",
}

# ── budgets ────────────────────────────────────────────────────────────────
MAX_SKILL_CHARS = 8000          # one SKILL.md body handed to the model
MAX_TASK_SKILL_CHARS = 24000    # all skill text in one task
MAX_SKILLS_PER_TASK = 3
MAX_REFERENCE_CHARS = 6000

DB_TIMEOUT_S = 15.0
DB_RETRIES = 2
DB_MAX_RECORDS = 25
DB_MAX_CALLS = 40               # per task; the skill's own guidance is 100
DB_USER_AGENT = "RIO-ScientificAgent/1.0 (+local research assistant)"

RUN_TIMEOUT_S = 180.0
RUN_RLIMIT_BYTES = 4 * 1024**3  # 4 GB address space for analysis jobs
RUN_MAX_OUTPUT_CHARS = 20000
RUN_THREADS = 2                 # OMP/MKL cap: this machine is RAM-constrained

MAX_REPORT_CHARS = 60000


@dataclass(frozen=True)
class Depth:
    """Research depth knobs — the brief's configurable research depth."""

    name: str
    max_queries: int
    max_sources: int
    max_db_calls: int
    read_full_text: bool
    model_task: str          # orchestrator ModelTask value
    max_report_sections: int


DEPTHS: dict[str, Depth] = {
    "quick": Depth("quick", 2, 6, 6, False, "light_reasoning", 3),
    "standard": Depth("standard", 4, 14, 16, True, "research", 5),
    "deep": Depth("deep", 6, 24, 30, True, "difficult_reasoning", 8),
}


@dataclass
class ScienceConfig:
    skills_root: Path = SKILLS_ROOT
    state_dir: Path = STATE_DIR
    index_path: Path = INDEX_PATH
    registry_path: Path = REGISTRY_PATH
    reports_dir: Path = REPORTS_DIR
    jobs_dir: Path = JOBS_DIR
    depth: str = "standard"
    allow_generated_code: bool = False
    store_reports_in_rag: bool = False
    max_skill_chars: int = MAX_SKILL_CHARS
    max_task_skill_chars: int = MAX_TASK_SKILL_CHARS
    max_skills_per_task: int = MAX_SKILLS_PER_TASK
    db_timeout_s: float = DB_TIMEOUT_S
    db_max_calls: int = DB_MAX_CALLS
    run_timeout_s: float = RUN_TIMEOUT_S
    run_rlimit_bytes: int = RUN_RLIMIT_BYTES
    extra: dict = field(default_factory=dict)

    def depth_profile(self) -> Depth:
        return DEPTHS.get(self.depth, DEPTHS["standard"])

    def ensure_dirs(self) -> None:
        for path in (self.state_dir, self.reports_dir, self.jobs_dir):
            path.mkdir(parents=True, exist_ok=True)


_CONFIG_PATH = STATE_DIR / "config.json"
_FIELDS = set(ScienceConfig.__dataclass_fields__)
_CACHE: ScienceConfig | None = None


def load_config(refresh: bool = False) -> ScienceConfig:
    """Defaults + optional `memory/scientific/config.json` + env overrides."""
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    cfg = ScienceConfig()
    try:
        raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key not in _FIELDS or key in ("skills_root", "state_dir", "index_path",
                                             "registry_path", "reports_dir", "jobs_dir"):
                continue
            try:
                current = getattr(cfg, key)
                if isinstance(current, bool):
                    cfg.__setattr__(key, bool(value))
                elif isinstance(current, int):
                    cfg.__setattr__(key, int(value))
                elif isinstance(current, float):
                    cfg.__setattr__(key, float(value))
                else:
                    cfg.__setattr__(key, value)
            except Exception:
                continue

    env_depth = os.environ.get("JARVIS_SCIENCE_DEPTH", "").strip().lower()
    if env_depth in DEPTHS:
        cfg.depth = env_depth
    if os.environ.get("JARVIS_SCIENCE_ALLOW_CODE", "").strip().lower() in {"1", "true", "on"}:
        cfg.allow_generated_code = True
    try:
        timeout = os.environ.get("JARVIS_SCIENCE_TIMEOUT", "").strip()
        if timeout:
            cfg.run_timeout_s = max(10.0, float(timeout))
    except ValueError:
        pass
    _CACHE = cfg
    return cfg


def reset_config_cache() -> None:
    global _CACHE
    _CACHE = None


def save_config(updates: dict[str, Any]) -> ScienceConfig:
    current: dict[str, Any] = {}
    try:
        loaded = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            current = loaded
    except Exception:
        current = {}
    for key, value in updates.items():
        if key in _FIELDS and key not in ("skills_root", "state_dir"):
            current[key] = value
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
    return load_config(refresh=True)


def provenance() -> dict[str, str]:
    return {
        "repo": UPSTREAM_REPO,
        "tag": UPSTREAM_TAG,
        "commit": UPSTREAM_COMMIT,
        "vendor_dir": str(VENDOR_DIR),
    }
