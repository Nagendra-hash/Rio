"""
orchestrator/model_router/budget.py — Cloud token budget enforcement.

Provides hard limits on cloud LLM usage with configurable daily/monthly caps,
per-request limits, alert thresholds, and a compatibility layer for the
legacy core/llm_router budget logic.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("jarvis.model_router.budget")

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "cloud_budget.json"

# Local providers that never count against budgets
_LOCAL_PROVIDERS = {"local", "ollama", "local_qwen3b", "ollama_qwen3b"}


class BudgetExceededError(RuntimeError):
    """Raised when a request would exceed the configured budget."""

    def __init__(self, provider: str, limit_type: str, current: int, limit: int):
        self.provider = provider
        self.limit_type = limit_type
        self.current = current
        self.limit = limit
        super().__init__(
            f"Budget exceeded: {provider} {limit_type} limit ({current}/{limit} tokens)"
        )


class BudgetManager:
    """Enforces token budgets per provider with daily/monthly limits."""

    def __init__(
        self,
        config_path: str | Path = _DEFAULT_CONFIG_PATH,
        usage_journal: Optional[Any] = None,
        clock: Optional[callable] = None,
    ):
        self.config_path = Path(config_path)
        self.usage_journal = usage_journal
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._config: dict = {}
        self._load_config()

    def _load_config(self) -> None:
        """Load budget configuration from JSON file."""
        try:
            if self.config_path.exists():
                self._config = json.loads(self.config_path.read_text(encoding="utf-8"))
            else:
                self._config = self._default_config()
                self.save()
        except Exception as e:
            log.warning(f"Failed to load budget config: {e}, using defaults")
            self._config = self._default_config()

    def _default_config(self) -> dict:
        """Default budget configuration (local-first: generous limits)."""
        return {
            "version": 1,
            "daily_limits": {
                "gemini": 200000,
                "openrouter": 50000,
                "nvidia": 100000,
                "freellm": 100000,
                "deepseek": 50000,
            },
            "monthly_limits": {
                "gemini": 5000000,
                "openrouter": 1000000,
                "nvidia": 2000000,
                "freellm": 2000000,
                "deepseek": 1000000,
            },
            "per_request_cap": 8000,
            "alert_thresholds": [0.5, 0.75, 0.9],
            "hard_block_at": 1.0,
            "exclude_local": True,
            "token_cost_usd_per_1k": {
                "gemini": 0.0,
                "openrouter": 0.0,
                "nvidia": 0.0,
                "freellm": 0.0,
                "deepseek": 0.0,
            },
        }

    def save(self) -> None:
        """Save current configuration to file."""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(
                json.dumps(self._config, indent=2), encoding="utf-8"
            )
        except Exception as e:
            log.error(f"Failed to save budget config: {e}")

    def is_local_provider(self, provider: str, model: str = "") -> bool:
        """Check if a provider/model is local (excluded from budgets)."""
        if self._config.get("exclude_local", True):
            provider_lower = provider.lower()
            model_lower = model.lower()
            if provider_lower in _LOCAL_PROVIDERS:
                return True
            if any(p in model_lower for p in ("local", "gguf", "llama-cpp")):
                return True
        return False

    def get_daily_limit(self, provider: str) -> int:
        """Get daily token limit for a provider."""
        return self._config.get("daily_limits", {}).get(provider, 0)

    def get_monthly_limit(self, provider: str) -> int:
        """Get monthly token limit for a provider."""
        return self._config.get("monthly_limits", {}).get(provider, 0)

    def get_per_request_cap(self) -> int:
        """Get per-request token cap."""
        return self._config.get("per_request_cap", 8000)

    def get_usage_today(self, provider: str) -> int:
        """Get token usage for today (from usage journal)."""
        if not self.usage_journal:
            return 0
        try:
            today = date.today().isoformat()
            # We need to read from the usage journal file
            # For now, estimate from the journal
            return self._read_usage_from_journal(provider, today, "daily")
        except Exception:
            return 0

    def get_usage_this_month(self, provider: str) -> int:
        """Get token usage for this month (from usage journal)."""
        if not self.usage_journal:
            return 0
        try:
            month_prefix = date.today().strftime("%Y-%m")
            return self._read_usage_from_journal(provider, month_prefix, "monthly")
        except Exception:
            return 0

    def _read_usage_from_journal(self, provider: str, date_prefix: str, period: str) -> int:
        """Read token usage from the usage journal file."""
        if not self.usage_journal or not hasattr(self.usage_journal, "path"):
            return 0

        path = Path(self.usage_journal.path)
        if not path.exists():
            return 0

        total = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("provider") != provider:
                        continue
                    ts = rec.get("ts", "")
                    if not ts.startswith(date_prefix):
                        continue
                    tokens = rec.get("tokens", {})
                    if tokens:
                        total += int(tokens.get("total_tokens", 0) or 0)
                except Exception:
                    continue
        except Exception:
            pass
        return total

    def check_budget(self, provider: str, model: str, estimated_tokens: int) -> tuple[bool, str]:
        """
        Check if a request would exceed budget.

        Returns:
            (allowed, reason) - allowed=False means budget exceeded
        """
        if self.is_local_provider(provider, model):
            return True, "local provider excluded from budget"

        daily_limit = self.get_daily_limit(provider)
        monthly_limit = self.get_monthly_limit(provider)

        if daily_limit <= 0 and monthly_limit <= 0:
            return True, "no budget limit configured"

        daily_used = self.get_usage_today(provider)
        monthly_used = self.get_usage_this_month(provider)

        # Check per-request cap
        per_request_cap = self.get_per_request_cap()
        if estimated_tokens > per_request_cap:
            return False, f"request exceeds per-request cap ({estimated_tokens} > {per_request_cap})"

        # Check daily limit
        if daily_limit > 0:
            projected_daily = daily_used + estimated_tokens
            if projected_daily > daily_limit:
                return False, f"daily budget exceeded ({projected_daily}/{daily_limit})"

        # Check monthly limit
        if monthly_limit > 0:
            projected_monthly = monthly_used + estimated_tokens
            if projected_monthly > monthly_limit:
                return False, f"monthly budget exceeded ({projected_monthly}/{monthly_limit})"

        return True, "ok"

    def get_budget_status(self, provider: str) -> dict:
        """Get current budget status for a provider."""
        daily_limit = self.get_daily_limit(provider)
        monthly_limit = self.get_monthly_limit(provider)
        daily_used = self.get_usage_today(provider)
        monthly_used = self.get_usage_this_month(provider)

        status = {
            "provider": provider,
            "daily": {
                "used": daily_used,
                "limit": daily_limit,
                "remaining": max(0, daily_limit - daily_used) if daily_limit > 0 else None,
                "percent": (daily_used / daily_limit * 100) if daily_limit > 0 else 0,
            },
            "monthly": {
                "used": monthly_used,
                "limit": monthly_limit,
                "remaining": max(0, monthly_limit - monthly_used) if monthly_limit > 0 else None,
                "percent": (monthly_used / monthly_limit * 100) if monthly_limit > 0 else 0,
            },
            "is_local": self.is_local_provider(provider),
        }

        # Add alert status
        for threshold in self._config.get("alert_thresholds", [0.5, 0.75, 0.9]):
            if daily_limit > 0 and status["daily"]["percent"] >= threshold * 100:
                status.setdefault("alerts", []).append(
                    f"daily usage at {status['daily']['percent']:.0f}% (threshold {threshold*100:.0f}%)"
                )
            if monthly_limit > 0 and status["monthly"]["percent"] >= threshold * 100:
                status.setdefault("alerts", []).append(
                    f"monthly usage at {status['monthly']['percent']:.0f}% (threshold {threshold*100:.0f}%)"
                )

        return status

    def get_all_status(self) -> dict:
        """Get budget status for all configured providers."""
        providers = set()
        providers.update(self._config.get("daily_limits", {}).keys())
        providers.update(self._config.get("monthly_limits", {}).keys())
        return {p: self.get_budget_status(p) for p in sorted(providers)}


# ── Compatibility with legacy core/llm_router ─────────────────────────────────

_LEGACY_BUDGET_DEFAULT = 200  # calls/day (legacy used calls, not tokens)


def _legacy_gemini_calls_today() -> int:
    """Legacy budget check: count Gemini calls from llm_usage.jsonl."""
    try:
        from orchestrator.model_router.legacy_shim import _load_full_config
        # We need to find the usage path - it's in the project root
        from pathlib import Path
        import os
        _home = os.environ.get("JARVIS_HOME", "").strip()
        base = Path(_home).expanduser().resolve() if _home else Path(__file__).resolve().parents[3]
        usage_path = base / "memory" / "llm_usage.jsonl"
        if not usage_path.exists():
            return 0
        today = datetime.now().strftime("%Y-%m-%d")
        n = 0
        for line in usage_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if (
                rec.get("provider") == "gemini"
                and rec.get("ok")
                and str(rec.get("ts", ""))[:10] == today
            ):
                n += 1
        return n
    except Exception:
        return 0


def _legacy_gemini_budget_ok(task: str, full_cfg: dict) -> bool:
    """Legacy budget check: vision is exempt, otherwise check daily call limit."""
    if task == "vision":
        return True
    budget = int(full_cfg.get("gemini_daily_call_budget") or _LEGACY_BUDGET_DEFAULT)
    return _legacy_gemini_calls_today() < budget