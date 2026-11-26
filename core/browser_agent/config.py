"""
core/browser_agent/config.py — settings for the RIO Browser Agent.

Defaults live in code; an optional override file at
`memory/browser_agent/config.json` may change any field. Secrets never belong
here — provider keys stay in `config/api_keys.json`, and credentials stay in
the chmod-600 store under `~/.local/share/jarvis-browser-profile/`.

Deliberately small and dependency-free: this module is imported by the action,
the orchestrator agent, the runtime and the tests.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

_BASE = Path(__file__).resolve().parents[2]
STATE_DIR = _BASE / "memory" / "browser_agent"
CONFIG_PATH = STATE_DIR / "config.json"

PROFILE_DIR = Path.home() / ".local" / "share" / "jarvis-browser-profile" / "bu"
CRED_STORE = Path.home() / ".local" / "share" / "jarvis-browser-profile" / "site_credentials.json"

ALLOWED_BRAINS = ("auto", "ollama", "nvidia", "freellm")


def _as_tuple(raw) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tuple(p.strip() for p in raw.split(",") if p.strip())
    if isinstance(raw, (list, tuple)):
        return tuple(str(p).strip() for p in raw if str(p).strip())
    return ()


@dataclass(frozen=True)
class BrowserAgentConfig:
    """Everything the browser agent may decide locally."""

    enabled: bool = True
    # Provider preference for the browser-use brain (see brain.py / runner).
    brain: str = "auto"
    # The 1.0 agent writes Python cells; a small budget keeps cost bounded.
    max_steps: int = 25
    max_steps_cap: int = 60
    timeout_s: float = 240.0
    # Opt-in paid-specialist off by default (brief: no unnecessary Gemini).
    allow_gemini: bool = False
    # Safety: the post-run judge flags injection / captcha cheaply.
    judge: bool = True
    # Dedicated profile keeps logins across runs without touching real cookies.
    profile_dir: str = str(PROFILE_DIR)
    attach_chrome_default: bool = False
    allowed_domains: tuple[str, ...] = ()
    prohibited_domains: tuple[str, ...] = ()
    # Static-page fast path (requests + BeautifulSoup) before any browser opens.
    http_first: bool = True
    http_min_chars: int = 200
    http_timeout_s: float = 15.0
    # Approval TTL for headless / Telegram confirmations.
    approval_ttl_s: int = 600
    keep_failure_artifacts: bool = True
    # Downstream text budget for voice answers.
    max_result_chars: int = 3000
    # Where run logs / downloads / failure artifacts live (tests override it).
    state_dir_path: str = str(STATE_DIR)

    # ── derived paths ─────────────────────────────────────────────────────
    @property
    def state_dir(self) -> Path:
        return Path(self.state_dir_path)

    @property
    def runs_log(self) -> Path:
        return self.state_dir / "runs.jsonl"

    @property
    def downloads_dir(self) -> Path:
        return self.state_dir / "downloads"

    @property
    def failures_dir(self) -> Path:
        return self.state_dir / "failures"

    def bounded_steps(self, requested: int | None) -> int:
        try:
            n = int(requested) if requested is not None else self.max_steps
        except (TypeError, ValueError):
            n = self.max_steps
        return max(1, min(n, self.max_steps_cap))

    def with_domains(self, domains) -> "BrowserAgentConfig":
        extra = _as_tuple(domains)
        if not extra:
            return self
        merged = tuple(dict.fromkeys((*self.allowed_domains, *extra)))
        return replace(self, allowed_domains=merged)


_FIELDS = {f.name for f in BrowserAgentConfig.__dataclass_fields__.values()}
_LOCK = threading.Lock()
_CACHED: BrowserAgentConfig | None = None


def _coerce(name: str, raw):
    if name in ("allowed_domains", "prohibited_domains"):
        return _as_tuple(raw)
    if name in ("max_steps", "max_steps_cap", "approval_ttl_s", "http_min_chars", "max_result_chars"):
        return int(raw)
    if name in ("timeout_s", "http_timeout_s"):
        return float(raw)
    if name in ("enabled", "allow_gemini", "judge", "attach_chrome_default", "http_first",
                "keep_failure_artifacts"):
        return bool(raw)
    return str(raw)


def load_config(refresh: bool = False) -> BrowserAgentConfig:
    """Defaults, with `memory/browser_agent/config.json` merged on top."""
    global _CACHED
    with _LOCK:
        if _CACHED is not None and not refresh:
            return _CACHED
        cfg = BrowserAgentConfig()
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        if isinstance(raw, dict):
            changes = {}
            for name, value in raw.items():
                if name not in _FIELDS or name.startswith("_"):
                    continue
                try:
                    changes[name] = _coerce(name, value)
                except Exception:
                    continue
            if changes:
                cfg = replace(cfg, **changes)
        # Environment overrides — the house pattern for ops tweaks.
        env_brain = os.environ.get("JARVIS_BROWSER_BRAIN", "").strip().lower()
        if env_brain in ALLOWED_BRAINS:
            cfg = replace(cfg, brain=env_brain)
        env_steps = os.environ.get("JARVIS_BROWSER_MAX_STEPS", "").strip()
        if env_steps.isdigit():
            cfg = replace(cfg, max_steps=cfg.bounded_steps(int(env_steps)))
        env_timeout = os.environ.get("JARVIS_BROWSER_TIMEOUT", "").strip()
        try:
            if env_timeout:
                cfg = replace(cfg, timeout_s=max(30.0, float(env_timeout)))
        except ValueError:
            pass
        if os.environ.get("JARVIS_BROWSER_ALLOWED_DOMAINS"):
            cfg = replace(
                cfg,
                allowed_domains=_as_tuple(os.environ["JARVIS_BROWSER_ALLOWED_DOMAINS"]),
            )
        _CACHED = cfg
        return cfg


def reset_config_cache() -> None:
    """Test hook — the next load_config() re-reads disk and env."""
    global _CACHED
    with _LOCK:
        _CACHED = None


def save_config(updates: dict) -> BrowserAgentConfig:
    """Merge `updates` into the override file (used by dashboards/tests)."""
    current: dict = {}
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            current = loaded
    except Exception:
        current = {}
    for name, value in updates.items():
        if name in _FIELDS and not name.startswith("_"):
            current[name] = list(value) if isinstance(value, tuple) else value
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
    return load_config(refresh=True)
