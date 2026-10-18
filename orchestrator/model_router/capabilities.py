"""
orchestrator/model_router/capabilities.py — the model capability registry.

The router's source of truth about WHAT each model can do and WHAT it costs:

  * ModelTask     — the routing vocabulary (what kind of ask is this?)
  * ModelSpec     — one model's capabilities, cost tier, privacy, context cap
  * CapabilityRegistry — lookup: "which models can serve this task, cheapest
    capable first?"

The default registry encodes the house strategy:

    1. LOCAL  — Qwen2.5-Coder 3B (core/llm_local.py GGUF): simple coding,
                private tasks, transformations, lightweight reasoning.
                Cost 0, never leaves the machine.
    2. FREE   — NVIDIA NIM (DeepSeek-V4.1-Flash, the coding specialist),
                the FreeLLMAPI pool and OpenRouter free models, when
                configured.
    3. GEMINI — difficult reasoning / research, and the last resort for
                everything else (never for private tasks).

Private asks are a HARD rule, not a preference: a task marked PRIVATE is only
ever offered models whose privacy is "local" — the router will fail honestly
rather than ship private text to a cloud provider.

Pure data + pure ordering: no I/O, no imports beyond stdlib, fully testable
offline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ModelTask(str, Enum):
    """The routing vocabulary. One ask maps to exactly one task."""

    SIMPLE_CODING = "simple_coding"          # write/fix/explain small code
    TRANSFORMATION = "transformation"        # reformat, extract, translate, summarize
    PRIVATE = "private"                      # anything that must not leave the machine
    LIGHT_REASONING = "light_reasoning"      # short answers, classifications, math-lite
    RESEARCH = "research"                    # web-ish knowledge, comparisons, news
    DIFFICULT_REASONING = "difficult_reasoning"  # multi-step analysis, hard puzzles


class Privacy(str, Enum):
    LOCAL = "local"      # runs on this machine only
    CLOUD = "cloud"      # sends the prompt off-machine


# Cost tiers — LOWER is tried FIRST. Tiers, not exact prices: the router picks
# the cheapest class that can do the job, and within a class a per-task
# priority breaks ties (e.g. OpenRouter before FreeLLMAPI for code).
TIER_LOCAL = 0
TIER_FREE = 1
TIER_PAID = 2
TIER_GEMINI = 3

# Local context cap in characters — kept identical to core/llm_router.py's
# LOCAL_MAX_PROMPT_CHARS: a 3B model with a truncated context answers
# confidently and wrongly, so long prompts must skip the local rung.
LOCAL_MAX_PROMPT_CHARS = 4000

_CLOUD_TASKS = frozenset(t for t in ModelTask if t is not ModelTask.PRIVATE)


@dataclass(frozen=True)
class ModelSpec:
    """One routable model and what it can do.

    Attributes:
        name: router-facing id, unique in the registry (e.g. "local_qwen3b").
        provider: which adapter serves it ("local", "freellm", "openrouter",
            "gemini"). Several specs may share a provider.
        model_id: the model identifier passed to the provider.
        cost_tier: TIER_LOCAL / TIER_FREE / TIER_PAID / TIER_GEMINI.
        tasks: the ModelTasks this model can genuinely serve.
        privacy: LOCAL (never leaves the machine) or CLOUD.
        context_chars: max prompt length accepted (0 = unlimited).
        priority: tie-break within a cost tier, lower first.
        task_priority: optional per-task priority overriding `priority`.
    """
    name: str
    provider: str
    model_id: str
    cost_tier: int
    tasks: frozenset
    privacy: Privacy = Privacy.CLOUD
    context_chars: int = 0
    priority: int = 50
    task_priority: dict = field(default_factory=dict)

    def serves(self, task: ModelTask) -> bool:
        return task in self.tasks

    def accepts_length(self, prompt_chars: int) -> bool:
        return self.context_chars <= 0 or prompt_chars <= self.context_chars

    def order_key(self, task: ModelTask) -> tuple:
        return (self.cost_tier, self.task_priority.get(task, self.priority),
                self.name)


class CapabilityRegistry:
    """Thread-safe registry of ModelSpecs with task→models lookup."""

    # Providers an adapter can actually serve — the router keys its adapters
    # by these names, so an unknown provider would create a spec that can
    # never be routed (or worse, silently shadows a real one).
    KNOWN_PROVIDERS = frozenset({"local", "freellm", "openrouter", "nvidia",
                                 "deepseek", "gemini"})

    def __init__(self) -> None:
        self._specs: dict[str, ModelSpec] = {}

    # ── registration ──────────────────────────────────────────────────────
    def register(self, spec: ModelSpec) -> ModelSpec:
        if not isinstance(spec, ModelSpec):
            raise TypeError(f"not a ModelSpec: {spec!r}")
        if not spec.name or spec.name.strip() != spec.name:
            raise ValueError(f"bad model name: {spec.name!r}")
        if spec.provider not in self.KNOWN_PROVIDERS:
            raise ValueError(f"{spec.name}: unknown provider {spec.provider!r} "
                             f"(known: {sorted(self.KNOWN_PROVIDERS)})")
        if spec.name in self._specs:
            raise ValueError(f"model already registered: {spec.name}")
        if spec.privacy is Privacy.LOCAL and ModelTask.PRIVATE not in spec.tasks:
            raise ValueError(f"{spec.name}: a local model should serve PRIVATE")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> Optional[ModelSpec]:
        return self._specs.get(name)

    def all(self) -> list[ModelSpec]:
        return list(self._specs.values())

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    # ── lookup ────────────────────────────────────────────────────────────
    def models_for(self, task: ModelTask,
                   privacy: Optional[Privacy] = None) -> list[ModelSpec]:
        """Capable models for the task, cheapest capable first.

        privacy=LOCAL narrows to on-machine models only (the PRIVATE rule);
        passing CLOUD is accepted but meaningless — local models stay listed
        since running a private-shape ask locally is always allowed.
        """
        want_local_only = privacy is Privacy.LOCAL
        return sorted(
            (s for s in self._specs.values()
             if s.serves(task)
             and (not want_local_only or s.privacy is Privacy.LOCAL)),
            key=lambda s: s.order_key(task))

    def describe(self) -> list[dict]:
        """Manifest for logs/dashboards — no secrets, pure metadata."""
        return [
            {
                "name": s.name,
                "provider": s.provider,
                "model_id": s.model_id,
                "cost_tier": s.cost_tier,
                "privacy": s.privacy.value,
                "tasks": sorted(t.value for t in s.tasks),
                "context_chars": s.context_chars,
            }
            for s in sorted(self._specs.values(), key=lambda s: s.name)
        ]


# ── the default registry (the house strategy, in data) ──────────────────────

def build_default_registry() -> CapabilityRegistry:
    """The five default models. Adapters decide whether each is actually
    configured — the registry only says what a model CAN do."""
    reg = CapabilityRegistry()

    # 1. LOCAL — Qwen2.5-Coder 3B GGUF (core/llm_local.py). Simple coding,
    #    transformations, private asks, lightweight reasoning. Not for
    #    research/difficult reasoning: a 3B model hallucinates exactly where
    #    those tasks punish it.
    reg.register(ModelSpec(
        name="local_qwen3b",
        provider="local",
        model_id="qwen2.5-coder-3b-instruct (llama-cpp)",
        cost_tier=TIER_LOCAL,
        tasks=frozenset({ModelTask.SIMPLE_CODING, ModelTask.TRANSFORMATION,
                         ModelTask.PRIVATE, ModelTask.LIGHT_REASONING}),
        privacy=Privacy.LOCAL,
        context_chars=LOCAL_MAX_PROMPT_CHARS,
        priority=0,
    ))

    # 2a. FreeLLMAPI — the local free-endpoint pool (127.0.0.1:3001), when
    #     configured. Preferred free cloud for prose-ish tasks.
    reg.register(ModelSpec(
        name="freellm",
        provider="freellm",
        model_id="auto",
        cost_tier=TIER_FREE,
        tasks=frozenset(_CLOUD_TASKS),
        privacy=Privacy.CLOUD,
        priority=10,
        task_priority={
            ModelTask.SIMPLE_CODING: 20,
            ModelTask.TRANSFORMATION: 10,
            ModelTask.DIFFICULT_REASONING: 20,
        },
    ))

    # 2b. OpenRouter free models — task-aware chain. Preferred free cloud for
    #     coding and hard reasoning (mirrors core/llm_router's table).
    reg.register(ModelSpec(
        name="openrouter_free",
        provider="openrouter",
        model_id="task-aware free chain + openrouter/free",
        cost_tier=TIER_FREE,
        tasks=frozenset(_CLOUD_TASKS),
        privacy=Privacy.CLOUD,
        priority=20,
        task_priority={
            ModelTask.SIMPLE_CODING: 10,
            ModelTask.TRANSFORMATION: 20,
            ModelTask.DIFFICULT_REASONING: 10,
        },
    ))

    # 2c. DeepSeek coders via OpenRouter — the WORKING coding head. NVIDIA's
    #     NIM endpoint for deepseek-v4.1-flash stalls (0-byte responses), so
    #     the same family is reached through the OpenRouter key here, first in
    #     the coding chain. `deepseek-chat` answers clean Kotlin/Python in
    #     seconds. Coding-shaped tasks only — difficult reasoning stays with
    #     Gemini.
    reg.register(ModelSpec(
        name="deepseek_chat",
        provider="deepseek",
        model_id="auto",
        cost_tier=TIER_FREE,
        tasks=frozenset({ModelTask.SIMPLE_CODING, ModelTask.TRANSFORMATION,
                         ModelTask.LIGHT_REASONING}),
        privacy=Privacy.CLOUD,
        priority=12,
        task_priority={
            ModelTask.SIMPLE_CODING: 1,       # head of the code chain
            ModelTask.TRANSFORMATION: 5,
        },
    ))

    # 2d. NVIDIA NIM (build.nvidia.com) — the coding specialist chain, led by
    #     DeepSeek-V4.1-Flash (`deepseek-ai/deepseek-v4.1-flash`) and falling
    #     back to nemotron / gpt-oss-20b. model_id is "auto": the adapter walks
    #     its chain (the big model is often queued/absent on the free tier),
    #     and the router journals the model that actually answered. The second
    #     free-cloud coding choice, behind the OpenRouter DeepSeek head.
    #     Coding-shaped tasks only — difficult reasoning stays with Gemini.
    reg.register(ModelSpec(
        name="nvidia_deepseek",
        provider="nvidia",
        model_id="auto",
        cost_tier=TIER_FREE,
        tasks=frozenset({ModelTask.SIMPLE_CODING, ModelTask.TRANSFORMATION,
                         ModelTask.LIGHT_REASONING}),
        privacy=Privacy.CLOUD,
        priority=15,
        task_priority={
            ModelTask.SIMPLE_CODING: 5,       # ahead of openrouter (10)
        },
    ))

    # 3. GEMINI — difficult reasoning / research, and last resort for the
    #    other cloud tasks. Highest tier: only reached when everything
    #    cheaper failed (or the mode forces it).
    reg.register(ModelSpec(
        name="gemini",
        provider="gemini",
        model_id="gemini-flash-latest",
        cost_tier=TIER_GEMINI,
        tasks=frozenset(_CLOUD_TASKS),
        privacy=Privacy.CLOUD,
        priority=30,
    ))
    return reg
