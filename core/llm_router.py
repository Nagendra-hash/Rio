"""
llm_router.py — JARVIS's intelligent LLM traffic cop (the routing plan).

CORE RULE: do not send every request to Gemini. Every one-shot text request is
classified first, then served by the cheapest CAPABLE provider. Gemini is the
SPECIALIST / FINAL FALLBACK — never the default:

    1. LOCAL  — Ollama `qwen2.5:1.5b` (systemd user service, 127.0.0.1:11434),
                then the llama-cpp 3B coder GGUF (core/llm_local.py,
                RAM-guarded, auto-unloaded). Zero quota, zero network, zero
                Gemini tokens.
    2. FREELLMAPI — the local free-endpoint pool (127.0.0.1:3001/v1, ~253
                endpoints, cross-provider failover inside the pool).
    3. OPENROUTER FREE — task-aware free-model chain, then the documented Free
                Models Router id `openrouter/free`. NOT `openrouter/auto`
                (which can select PAID models).
    4. GEMINI — only when cheaper providers failed, the task is vision, or the
                daily budget guard still allows it.

TASK-AWARE ROUTING TABLE (implemented in _plan_chain):
    Task                        Chain (left → right)
    ─────────────────────────   ─────────────────────────────────────────────
    simple chat / general       Ollama → 3B → FreeLLMAPI → OpenRouter → Gemini
    coding                      Ollama → 3B → OpenRouter → FreeLLMAPI → Gemini
    simple summarization        Ollama → 3B → FreeLLMAPI → OpenRouter → Gemini
    complex reasoning           OpenRouter → FreeLLMAPI → Gemini   (no local)
    LONG/DIFFICULT text         FreeLLMAPI → OpenRouter → Gemini   (no local)
    image / PDF / multimodal    FreeLLMAPI → OpenRouter → Gemini   (never local)
    grounded search (tools)     Gemini only (no OpenAI-compatible equivalent)
    provider failure            next provider (cooldown so it is skipped
                                temporarily, per the health-check rules)

QUALITY ESCALATION, not reflex escalation: a hop only escalates when the model
errored, returned nothing, or — for code — failed a deterministic syntax gate
(every ```python block must compile). Code tasks get ONE corrective local
retry before escalating, per the plan's code-routing loop.

GEMINI TOKEN PROTECTION (the plan's ten rules, as implemented here):
    R1  Gemini is never the default — it is last in every smart chain.
    R2  Ordinary one-shot calls (search summaries, news, briefings, code
        helper…) land on local/free providers first.
    R3  These are one-shot calls; no conversation history is attached here.
    R4  RAG/context compaction is the callers' job; the router only sees the
        compact prompt it is handed.
    R5  Per-key cooldowns stop repeated hammering of a failing provider.
    R6  Answer caching lives at the call sites that can dedupe (e.g.
        plugins/_whatsapp_core.py caches per language for the install's life).
    R7  Deterministic Python stays deterministic — the router never replaces
        the trading computation engine; it only receives structured data.
    R8  OpenRouter requests use task-aware free models + `openrouter/free`.
    R9  `gemini_daily_call_budget` (default 200/day) hard-stops Gemini in
        smart mode; vision is exempt (it is genuinely the last resort).
    R10 Every routing decision is appended to memory/llm_usage.jsonl:
        timestamp, task, provider, model, ok, latency, tokens, escalations.

MODES (config "router_mode", or env JARVIS_ROUTER_MODE; default "smart"):
    "local"   Always local. Maximum privacy, zero cloud. Errors if the local
              engines cannot serve (e.g. vision, or a too-long prompt).
    "free"    Local → FreeLLMAPI → OpenRouter Free. Never Gemini.
    "smart"   Local → free cloud → Gemini (budget-guarded). Best default.
    "gemini"  Force Gemini (deliberate use only; bypasses the budget guard).

This module installs a transparent safety net: it patches
`Models.generate_content` at class level, so EVERY existing call site gets
the routing chain without per-file changes. The returned object mimics the
genai response surface the call sites use (`.text`), so no caller can tell
the difference. The Live voice session in main.py is NOT affected — it uses
client.aio.live.connect, which stays on Gemini (no compatible alternative).

Configuration (config/api_keys.json):
    "router_mode": "smart",                  # local | free | smart | gemini
    "gemini_daily_call_budget": 200,         # Gemini calls/day in smart mode
    "gemini_api_keys": ["AIza…", "AIza…"],   # extra Gemini keys, rotated on quota errors
    "freellmapi_api_key": "…",               # local FreeLLMAPI pool endpoint
    "openrouter_keys": [
        {"key": "sk-or-v1-…", "models": ["meta-llama/llama-3.3-70b-instruct:free"]},
        {"key": "sk-or-v1-…"}                # "auto" → task-aware chain
    ],
    "grok_api_key":  "xai-…",
    "openai_api_key": "sk-…",
    "ollama_enabled": true,
    "ollama_models": ["qwen2.5:1.5b"]
A plain string entry ("sk-or-v1-…") is also accepted; it uses the auto chain.

API keys are read ONLY from config/api_keys.json (gitignored) — never from
source code, never from client-side JavaScript.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
# JARVIS_HOME (pendrive mode): config is read here and llm_usage.jsonl is
# WRITTEN here — both are writable state, so both follow the relocatable home
# (same contract as memory_manager.get_base_dir).
_home = os.environ.get("JARVIS_HOME", "").strip()
if _home:
    _BASE = Path(_home).expanduser().resolve()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"
_USAGE_PATH = _BASE / "memory" / "llm_usage.jsonl"

DEFAULT_MODELS = [
    "z-ai/glm-5.2:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3.5-lightning:free",
]

# Task-aware auto-selection: preferred free models per task class, best first.
# Filtered against OpenRouter's live free-model catalog at call time, so stale
# ids simply drop out and unavailable preferences fall back to the next match.
# (Refreshed against the live catalog 2026-09: the old llama/deepseek free
# tiers were retired — current free pool is Nemotron-3/GLM-5.2/poolside etc.)
_TASK_PREFERENCES = {
    "code": [
        "poolside/laguna-s-2.1:free",
        "cohere/north-mini-code:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
    ],
    "vision": [
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "inclusionai/ling-3.0-flash-vl:free",
        "z-ai/glm-5.2:free",
    ],
    "reason": [
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "thinkingmachines/inkling:free",
    ],
    "summarize": [
        "nvidia/nemotron-3.5-lightning:free",
        "z-ai/glm-5.2:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
    ],
    "general": [
        "z-ai/glm-5.2:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "nvidia/nemotron-3.5-lightning:free",
    ],
}

# Keywords that mark a task class (checked against the prompt text).
_TASK_HINTS = [
    ("code", ("code", "function", "script", "debug", "traceback", "error:",
              "regex", "compile", "class ", "def ", "import ", "sql", "api call",
              "refactor", "```", "bug", "python", "javascript")),
    ("reason", ("solve", "calculate", "math", "equation", "logic", "puzzle",
                "step by step", "prove", "how many", "probability", "reason")),
    ("summarize", ("summarize", "summary", "tl;dr", "headlines", "news", "brief",
                   "condense", "key points", "recap")),
]

_models_cache = {"at": 0.0, "free": None, "vision_capable": None}   # id sets, TTL 6h
_MODELS_TTL_S = 6 * 3600

_OR_URL    = "https://openrouter.ai/api/v1/chat/completions"
_GROK_URL  = "https://api.x.ai/v1/chat/completions"
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_FREELLMAPI_URL = "http://127.0.0.1:3001/v1/chat/completions"
_OLLAMA_URL = "http://127.0.0.1:11434/v1/chat/completions"
_OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"

# ── Local rung (the plan's LOCAL WORKHORSE) ──────────────────────────────────
# Ollama first: it runs as a service, stays warm (~1.7 s a turn) and keeps no
# RAM the rest of the app doesn't already know about. The 3B coder GGUF
# (core/llm_local.py) is the second local engine — better at code, but a cold
# load costs ~30 s and ~2 GB RAM, so it only fires when Ollama can't.
_OLLAMA_DEFAULT_MODELS = ["qwen2.5:1.5b"]
_OLLAMA_TIMEOUT_S = 60        # CPU-only inference; long enough for a cold load
_OLLAMA_PROBE_TTL_OK = 60     # re-probe a healthy Ollama at most once a minute
_OLLAMA_PROBE_TTL_FAIL = 120  # a dead one is not retried more often than this

# Prompts longer than this skip the local rungs entirely (the plan's
# LONG/DIFFICULT TEXT class): a 1.5B/3B model with a truncated context
# produces confident garbage, and cloud free tiers handle length better.
LOCAL_MAX_PROMPT_CHARS = 4000

# Per-provider failover chains (override in config via grok_models / openai_models).
_GROK_DEFAULT_MODELS   = ["grok-4.6", "grok-4.5", "grok-3-mini"]
_OPENAI_DEFAULT_MODELS = ["gpt-4o-mini", "gpt-4.1-mini"]

# ── Modes & budget (plan rules 1 and 9) ──────────────────────────────────────
_ROUTER_MODES = ("local", "free", "smart", "gemini")
_GEMINI_BUDGET_DEFAULT = 200          # Gemini calls/day in smart mode

# FreeLLMAPI is a local service; when it is down, back off instead of
# paying a connection-refused round trip in front of every request.
_FREELLMAPI_COOLDOWN_S = 120

# Per-key cooldown after a Gemini failure. Quota (429 RESOURCE_EXHAUSTED)
# resets daily on the free tier, so a long cooldown is right. But transient
# 503s ("This model is currently overloaded / unavailable") clear in seconds
# — parking every key for 10 minutes on one 503 burns the whole failover
# chain for nothing (seen 2026-09-23: all 3 keys 503'd at once, OpenRouter
# picked up the slack, Gemini idle for the rest of the cooldown).
_GEMINI_COOLDOWN_S         = 600
_GEMINI_TRANSIENT_COOLDOWN_S = 45

_lock = threading.Lock()
_gemini_key_block: dict[str, float] = {}   # key → earliest retry time
_provider_block: dict[str, float] = {}     # provider → earliest retry time
_usage_lock = threading.Lock()
_installed = False
_orig_generate = None


class _Skip(Exception):
    """A provider cannot take this request right now (probe down, cooling,
    budget) — expected, not a failure worth surfacing."""


# ── Config ───────────────────────────────────────────────────────────────────

def _load_full_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_router_config() -> list[dict]:
    """Read openrouter_keys from config. Entries: str, {key, models:[…]} or
    {key, models:"auto"} (task-aware selection — the default)."""
    entries = _load_full_config().get("openrouter_keys") or []
    out = []
    for e in entries:
        if isinstance(e, str):
            out.append({"key": e, "models": "auto"})
        elif isinstance(e, dict) and e.get("key"):
            models = e.get("models") or "auto"
            out.append({"key": e["key"], "models": models})
    return out


def _get_mode(full_cfg: dict | None = None) -> str:
    """router_mode: env override → config → 'smart'. Unknown values → smart."""
    cfg = full_cfg if full_cfg is not None else _load_full_config()
    raw = os.environ.get("JARVIS_ROUTER_MODE") or (cfg.get("router_mode") or "smart")
    m = str(raw).strip().lower()
    return m if m in _ROUTER_MODES else "smart"


def _all_gemini_keys() -> list[str]:
    """Primary gemini_api_key + any extras from gemini_api_keys (deduped, in order)."""
    cfg = _load_full_config()
    keys: list[str] = []
    primary = (cfg.get("gemini_api_key") or "").strip()
    if primary:
        keys.append(primary)
    extra = cfg.get("gemini_api_keys") or []
    if isinstance(extra, str):
        extra = [extra]
    for k in extra:
        k = (k or "").strip()
        if k and k not in keys:
            keys.append(k)
    return keys


def _llm_local_configured() -> bool:
    """True when the 3B coder GGUF is on disk (core/llm_local.py's rung)."""
    try:
        from core import llm_local
        return llm_local.is_configured()
    except Exception:
        return False


def _nothing_configured(full_cfg: dict) -> bool:
    """True when no local engine and no fallback key exists at all — the
    fresh-install case where the router must stay fully transparent."""
    if _llm_local_configured():
        return False
    if (full_cfg.get("freellmapi_api_key") or "").strip():
        return False
    if _load_router_config():
        return False
    if (full_cfg.get("grok_api_key") or "").strip():
        return False
    if (full_cfg.get("openai_api_key") or "").strip():
        return False
    if bool(full_cfg.get("ollama_enabled") or full_cfg.get("ollama_models")):
        return False
    return True


# ── Error classification ─────────────────────────────────────────────────────

def _is_quota_error(e: Exception) -> bool:
    """Daily-limit exhaustion (long cooldown) — NOT a mere service blip."""
    text = str(e).lower()
    return any(m in text for m in ("429", "resource_exhausted", "quota"))


def _is_transient(e: Exception) -> bool:
    text = str(e).lower()
    return any(m in text for m in (
        "500", "503", "unavailable", "overloaded", "high demand",
        "internal", "deadline", "timed out", "timeout",
        "connection reset", "aborted", "no longer available",
    ))


def _is_auth_error(e: Exception) -> bool:
    text = str(e).lower()
    return any(m in text for m in ("401", "403", "api key", "api_key",
                                   "unregistered", "permission"))


# ── Task classification (the router's "traffic police") ─────────────────────

def _classify_task(message: dict) -> str:
    """Best-effort task class from the converted message: vision when images
    are attached, then keyword heuristics, else general."""
    content = message.get("content")
    if isinstance(content, list):
        has_image = any(isinstance(p, dict) and p.get("type") == "image_url"
                        for p in content)
        if has_image:
            return "vision"
        text = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    else:
        text = str(content or "")
    low = text.lower()
    for task, words in _TASK_HINTS:
        if any(w in low for w in words):
            return task
    return "general"


def _prompt_too_long(message: dict) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        return len(content) > LOCAL_MAX_PROMPT_CHARS
    return sum(len(p.get("text", "")) for p in content if isinstance(p, dict)) \
        > LOCAL_MAX_PROMPT_CHARS


def _free_model_catalog() -> tuple[set, set]:
    """(all free model ids, vision-capable free model ids) from OpenRouter's
    public /models endpoint. Cached 6h; on failure returns ({}, {}) which sends
    the caller to the static preference list."""
    now = time.time()
    if _models_cache["free"] is not None and now - _models_cache["at"] < _MODELS_TTL_S:
        return _models_cache["free"], _models_cache["vision_capable"]
    try:
        import requests
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=15)
        r.raise_for_status()
        free: set = set()
        vision: set = set()
        for m in r.json().get("data", []):
            mid = m.get("id", "")
            if not mid.endswith(":free"):
                continue
            free.add(mid)
            arch = m.get("architecture") or {}
            modality = (arch.get("input_modalities") or arch.get("modality") or "")
            if isinstance(modality, list):
                has_img = "image" in modality
            else:
                has_img = "image" in str(modality).lower()
            if has_img:
                vision.add(mid)
        if free:
            _models_cache.update({"at": now, "free": free, "vision_capable": vision})
        return free, vision
    except Exception as e:
        print(f"[LLM] ⚠️  could not fetch OpenRouter model catalog ({str(e)[:60]}) "
              "— using static preference list")
        return set(), set()


def _models_for_task(message: dict) -> list[str]:
    """The auto-selection chain: preferences for the task class, kept only if
    still alive in the live catalog; padded with catalog fallbacks sized for
    the modality; final fallback = static DEFAULT_MODELS. The documented Free
    Models Router (`openrouter/free`) trails the chain as a safety net — it
    selects from free models only, unlike `openrouter/auto`."""
    task  = _classify_task(message)
    prefs = _TASK_PREFERENCES.get(task, DEFAULT_MODELS)
    free, vision_capable = _free_model_catalog()
    if not free:
        return list(prefs) + ["openrouter/free"]
    chain = [m for m in prefs if m in free]
    if task == "vision" and not chain:
        # any free vision model, biggest context first
        chain = sorted(vision_capable, reverse=False)[:3]
    if not chain:
        chain = sorted(free)[:3]
    if "openrouter/free" not in chain:
        chain.append("openrouter/free")
    print(f"[LLM] task={task} → models: {chain[:2]}{'…' if len(chain) > 2 else ''}")
    return chain


# ── Content conversion: genai-style contents → OpenAI-compatible messages ───

def _pil_to_data_url(img) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _contents_to_message(contents) -> dict:
    """Build one user message. Accepts: str | PIL.Image | genai Part-like |
    list of any of those — the shapes the call sites actually pass."""
    text_parts: list[str] = []
    image_parts: list[str] = []      # data URLs

    def _walk(item):
        if item is None:
            return
        if isinstance(item, str):
            text_parts.append(item)
            return
        # Image carriers FIRST — genai Part/Blob are pydantic models, which are
        # iterable, so the generic iter branch below would otherwise consume
        # them and str() the raw bytes into the text (the model then "describes"
        # binary garbage instead of seeing the picture).
        inline = getattr(item, "inline_data", None)
        if inline is not None and getattr(inline, "data", None):
            mime = getattr(inline, "mime_type", "image/png") or "image/png"
            b64 = base64.b64encode(bytes(inline.data)).decode()
            image_parts.append(f"data:{mime};base64,{b64}")
            return
        if type(item).__name__ == "Image":            # PIL Image (duck-typed)
            image_parts.append(_pil_to_data_url(item))
            return
        if hasattr(item, "parts") and item.parts:     # genai Content
            for sub in item.parts:
                _walk(sub)
            return
        if isinstance(item, (bytes, bytearray)):      # raw bytes: no mime known
            return
        if hasattr(item, "__iter__"):
            for sub in item:
                _walk(sub)
            return
        text_parts.append(str(item))

    _walk(contents)

    if image_parts:
        content: list = [{"type": "text", "text": "\n".join(text_parts) or "Describe this."}]
        content += [{"type": "image_url", "image_url": {"url": u}} for u in image_parts]
        return {"role": "user", "content": content}
    return {"role": "user", "content": "\n".join(text_parts)}


def _extract_system(kwargs: dict) -> str:
    """The genai config's system_instruction, if the caller passed one —
    carried over to OpenAI-compatible providers so a routed answer keeps the
    caller's contract (persona, output format) instead of losing it."""
    cfg = kwargs.get("config")
    if cfg is None:
        return ""
    val = cfg.get("system_instruction") if isinstance(cfg, dict) \
        else getattr(cfg, "system_instruction", None)
    return str(val) if val else ""


def _build_messages(message: dict, system: str) -> list[dict]:
    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append(message)
    return msgs


# ── OpenAI-compatible caller (OpenRouter / Grok / OpenAI / FreeLLMAPI / Ollama)

class _ORResponse:
    """Mimics the slice of a genai response that call sites touch: `.text` and
    `candidates[0].content.parts[i].text` (actions/web_search.py iterates the
    latter to assemble the answer text)."""

    class _Part:
        def __init__(self, text: str):
            self.text = text

    class _Content:
        def __init__(self, text: str):
            self.parts = [_ORResponse._Part(text)]

    class _Candidate:
        def __init__(self, text: str):
            self.content = _ORResponse._Content(text)

    def __init__(self, text: str):
        self.text = text
        self.candidates = [_ORResponse._Candidate(text)]

    def __str__(self):
        return self.text


class _KeyDead(Exception):
    pass


def _call_openrouter(api_key: str, models: list[str], messages: list[dict],
                     timeout: int = 60, url: str = _OR_URL,
                     max_tokens: int | None = None) -> tuple[str, str, dict | None]:
    """OpenAI-compatible chat-completions caller — used for OpenRouter, Grok
    (api.x.ai), OpenAI (api.openai.com), FreeLLMAPI and Ollama alike; the wire
    format is identical. Returns (text, model, usage-or-None)."""
    import requests

    last_err = "no attempt made"
    for model in models:
        payload: dict = {"model": model, "messages": messages}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        try:
            r = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
            if r.status_code == 200:
                data = r.json()
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                if text:
                    return text, model, data.get("usage")
                last_err = f"{model}: empty completion"
                continue
            last_err = f"{model}: HTTP {r.status_code} {r.text[:120]}"
            # 401/402 → this key is dead/out of credit; try next key entirely
            if r.status_code in (401, 402):
                raise _KeyDead(last_err)
        except _KeyDead:
            raise
        except Exception as e:
            last_err = f"{model}: {type(e).__name__}: {str(e)[:120]}"
    raise RuntimeError(f"models exhausted — last error: {last_err}")


# ── Quality gate (escalate on evidence, not on taste) ───────────────────────

_FENCE_RE = re.compile(r"```(?:python|py)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _code_ok(text: str) -> bool:
    """Deterministic validation: every ```python block must at least compile.
    Prose answers and non-python fences pass — there is nothing to check."""
    blocks = _FENCE_RE.findall(text)
    for block in blocks:
        try:
            compile(block, "<llm-code>", "exec")
        except SyntaxError:
            return False
    return True


def _answer_ok(task: str, text: str) -> bool:
    if not text or not text.strip():
        return False
    if task == "code":
        return _code_ok(text)
    return True


# ── Provider health (the plan's PROVIDER HEALTH CHECK) ──────────────────────

_ollama_state = {"checked_at": 0.0, "alive": False, "reason": "", "model": ""}


def _ollama_alive(expected_model: str) -> tuple[bool, str]:
    """Is Ollama up AND is the configured model actually pulled? Cached per
    model with a short TTL, so a down Ollama costs one 1.5 s probe per two
    minutes, not one per request."""
    now = time.time()
    st = _ollama_state
    if (st["model"] == expected_model
            and now - st["checked_at"] < (_OLLAMA_PROBE_TTL_OK if st["alive"]
                                          else _OLLAMA_PROBE_TTL_FAIL)):
        return st["alive"], st["reason"]
    try:
        import requests
        r = requests.get(_OLLAMA_TAGS_URL, timeout=1.5)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
        base = expected_model.split(":")[0]
        if not any(n == expected_model or n == base or n.startswith(base + ":")
                   for n in names):
            ok, reason = False, (f"model '{expected_model}' not pulled — "
                                 f"run: ollama pull {expected_model}")
        else:
            ok, reason = True, ""
    except Exception as e:
        ok, reason = False, f"not reachable ({str(e)[:60]})"
    st.update({"checked_at": now, "alive": ok, "reason": reason, "model": expected_model})
    return ok, reason


def _provider_cooling(name: str) -> bool:
    with _lock:
        until = _provider_block.get(name, 0.0)
        if until and time.time() < until:
            return True
        _provider_block.pop(name, None)
        return False


def _cool_provider(name: str, seconds: float) -> None:
    with _lock:
        _provider_block[name] = time.time() + seconds


# ── Gemini usage budget (plan rule 9) + usage log (plan rule 10) ────────────

_USAGE_MAX_BYTES = 5_000_000
_USAGE_KEEP_LINES = 1000
_budget_cache = {"date": "", "size": -1, "count": 0}


def _log_usage(task: str, provider: str, model: str, ok: bool, latency_s: float,
               usage: dict | None, hops: list, mode: str, error: str = "") -> None:
    """One line per routed request — the MODEL USAGE LOG. Never raises.

    Token fields (plan rule 10, extended 2026-09-27): in/out tokens always
    present (None when a provider omits them); total/cached/reasoning/cost
    follow the same schema so every record has identical keys for analysis.
      * cached_tokens    — prompt tokens served from cache (Gemini context
                           caching / OpenAI prompt caching) — the cheap ones
      * reasoning_tokens — hidden thinking tokens (Gemini thoughts, OpenRouter
                           reasoning models) — output tokens you pay for but
                           never see in the answer
      * cost             — USD as reported by OpenRouter usage (0.0 on free)
    """
    try:
        u = dict(usage or {})
        # Flatten OpenAI-style nested detail blocks (OpenRouter / OpenAI / Ollama
        # return them as dicts; Gemini's come pre-flattened via _genai_usage).
        if u.get("cached_tokens") is None:
            u["cached_tokens"] = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
        if u.get("reasoning_tokens") is None:
            u["reasoning_tokens"] = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "mode": mode,
            "task": task,
            "provider": provider,
            "model": model,
            "ok": bool(ok),
            "latency_s": round(float(latency_s), 2),
            "in_tokens": u.get("prompt_tokens"),
            "out_tokens": u.get("completion_tokens"),
            "total_tokens": u.get("total_tokens"),
            "cached_tokens": u.get("cached_tokens"),
            "reasoning_tokens": u.get("reasoning_tokens"),
            "cost": u.get("cost"),
            "escalations": hops,
        }
        if error:
            rec["error"] = error[:200]
        with _usage_lock:
            _USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
            if _USAGE_PATH.exists() and _USAGE_PATH.stat().st_size > _USAGE_MAX_BYTES:
                lines = _USAGE_PATH.read_text(encoding="utf-8").splitlines()[-_USAGE_KEEP_LINES:]
                _USAGE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with _USAGE_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _genai_usage(resp) -> dict | None:
    """Extract a google-genai response's usage_metadata into the OpenAI-style
    dict _log_usage expects (prompt_tokens / completion_tokens / total_tokens /
    cached_tokens / reasoning_tokens). None-safe: on any shape change this
    returns None and the log line simply lacks token counts — never raises."""
    try:
        um = getattr(resp, "usage_metadata", None)
        if um is None:
            return None
        def _g(name):
            v = getattr(um, name, None)
            return int(v) if v else 0
        out = {"prompt_tokens": _g("prompt_token_count"),
               "completion_tokens": _g("candidates_token_count"),
               "total_tokens": _g("total_token_count")}
        if _g("cached_content_token_count"):
            out["cached_tokens"] = _g("cached_content_token_count")
        if _g("thoughts_token_count"):
            out["reasoning_tokens"] = _g("thoughts_token_count")
        return out
    except Exception:
        return None


def _gemini_calls_today() -> int:
    """Successful Gemini hops logged today — the budget guard's counter."""
    try:
        if not _USAGE_PATH.exists():
            return 0
        today = datetime.now().strftime("%Y-%m-%d")
        size = _USAGE_PATH.stat().st_size
        c = _budget_cache
        if c["date"] == today and c["size"] == size:
            return c["count"]
        n = 0
        for line in _USAGE_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if (rec.get("provider") == "gemini" and rec.get("ok")
                    and str(rec.get("ts", ""))[:10] == today):
                n += 1
        _budget_cache.update({"date": today, "size": size, "count": n})
        return n
    except Exception:
        return 0


def _gemini_budget_ok(task: str, full_cfg: dict) -> bool:
    """Plan rule 9: an explicit daily budget guard. Vision is exempt — it is
    the one class that genuinely needs Gemini when the free cloud can't."""
    if task == "vision":
        return True
    budget = int(full_cfg.get("gemini_daily_call_budget") or _GEMINI_BUDGET_DEFAULT)
    return _gemini_calls_today() < budget


# ── The chain planner (pure — the routing table in code) ────────────────────

def _plan_chain(mode: str, task: str, long_prompt: bool, *,
                ollama: bool = False, local3b: bool = False, freellm: bool = False,
                openrouter: bool = False, grok: bool = False, openai: bool = False,
                budget_ok: bool = True) -> list[str]:
    """Ordered provider names for (mode, task, prompt length). Pure so the
    routing table is testable without touching the network."""
    if mode == "gemini":
        return ["gemini"]
    steps: list[str] = []
    # The plan's LOCAL class: simple chat, coding, normal reasoning, short
    # text. Complex reasoning, vision and long prompts skip local by design.
    local_ok = task not in ("vision", "reason") and not long_prompt
    if mode in ("local", "free", "smart"):
        if local_ok and ollama:
            steps.append("ollama")
        if local_ok and local3b:
            steps.append("llm_local")
    if mode in ("free", "smart"):
        # Coding/complex-reasoning prefer OpenRouter Free second (table);
        # everything else prefers the FreeLLMAPI pool.
        if task in ("code", "reason"):
            if openrouter:
                steps.append("openrouter")
            if freellm:
                steps.append("freellm")
        else:
            if freellm:
                steps.append("freellm")
            if openrouter:
                steps.append("openrouter")
        if mode == "smart":
            # paid-key tails stay a smart-mode last resort, ahead of Gemini
            if grok:
                steps.append("grok")
            if openai:
                steps.append("openai")
            if budget_ok:
                steps.append("gemini")
    return steps


# ── The provider steps ───────────────────────────────────────────────────────

def _step_ollama(full_cfg: dict, message: dict, system: str, task: str):
    models = full_cfg.get("ollama_models") or _OLLAMA_DEFAULT_MODELS
    ok, reason = _ollama_alive(models[0])
    if not ok:
        raise _Skip(f"ollama {reason}")
    messages = _build_messages(message, system)
    text, mdl, usage = _call_openrouter(
        "ollama", models, messages, url=_OLLAMA_URL, timeout=_OLLAMA_TIMEOUT_S,
        max_tokens=700 if task == "code" else 350)
    if task == "code" and not _code_ok(text):
        # The plan's code loop: generate → local validation → one corrective
        # retry with the local model → only then escalate to the cloud.
        retry_msgs = messages[:-1] + [{
            "role": "user",
            "content": (str(message.get("content", ""))
                        + "\n\nYour previous reply failed a Python syntax check:\n"
                        + text[:2000]
                        + "\n\nReturn ONLY the corrected, complete code."),
        }]
        try:
            text2, mdl2, usage2 = _call_openrouter(
                "ollama", models, retry_msgs, url=_OLLAMA_URL,
                timeout=_OLLAMA_TIMEOUT_S, max_tokens=700)
            if text2 and _code_ok(text2):
                return text2, f"{mdl2}+fix", usage2
        except Exception:
            pass
        raise RuntimeError("local code failed the syntax gate after one retry")
    return text, mdl, usage


def _step_llm_local(message: dict, system: str, task: str):
    from core import llm_local
    if not llm_local.is_configured():
        raise _Skip("3B GGUF not on disk")
    if not llm_local.ram_ok():
        raise _Skip(f"not enough free RAM ({llm_local._available_ram_mb()} MB)")
    prompt = message["content"] if isinstance(message["content"], str) else ""
    if not prompt:
        raise _Skip("no text content for the local rung")
    text = llm_local.generate(prompt, system=system,
                              max_tokens=700 if task == "code" else 350)
    if not text:
        raise RuntimeError("local 3B returned nothing")
    return text, "qwen2.5-coder-3b (llama-cpp)", None


def _step_freellm(key: str, message: dict, system: str):
    if not key:
        raise _Skip("no freellmapi key")
    if _provider_cooling("freellm"):
        raise _Skip("freellm cooling down after a recent failure")
    try:
        text, mdl, usage = _call_openrouter(
            key, ["auto"], _build_messages(message, system), url=_FREELLMAPI_URL)
    except _KeyDead as e:
        raise _Skip(f"freellm key dead: {e}")
    except Exception:
        _cool_provider("freellm", _FREELLMAPI_COOLDOWN_S)
        raise
    return text, mdl, usage


def _step_openrouter(entries: list[dict], message: dict, system: str):
    if not entries:
        raise _Skip("no openrouter keys")
    messages = _build_messages(message, system)
    last = "no attempt made"
    for entry in entries:
        # "auto" → task-aware chain + `openrouter/free` (the documented free
        # router); an explicit list still wins (user override).
        models = entry["models"] if isinstance(entry["models"], list) \
            else _models_for_task(message)
        try:
            answer, used_model, usage = _call_openrouter(entry["key"], models, messages)
            print(f"[LLM] ✅ {used_model} via OpenRouter key …{entry['key'][-6:]}")
            return answer, used_model, usage
        except _KeyDead as e:
            print(f"[LLM] ⚠️  OpenRouter key …{entry['key'][-6:]} dead: {e}")
            last = str(e)
            continue
        except Exception as e:
            print(f"[LLM] ⚠️  OpenRouter key …{entry['key'][-6:]} failed: {e}")
            last = str(e)
            continue
    raise RuntimeError(f"OpenRouter keys exhausted — {last}")


def _step_grok(full_cfg: dict, message: dict, system: str):
    key = (full_cfg.get("grok_api_key") or "").strip()
    if not key:
        raise _Skip("no grok key")
    models = full_cfg.get("grok_models") or _GROK_DEFAULT_MODELS
    text, mdl, usage = _call_openrouter(key, models, _build_messages(message, system),
                                        url=_GROK_URL)
    print(f"[LLM] ✅ {mdl} via Grok (xAI)")
    return text, mdl, usage


def _step_openai(full_cfg: dict, message: dict, system: str):
    key = (full_cfg.get("openai_api_key") or "").strip()
    if not key:
        raise _Skip("no openai key")
    models = full_cfg.get("openai_models") or _OPENAI_DEFAULT_MODELS
    text, mdl, usage = _call_openrouter(key, models, _build_messages(message, system),
                                        url=_OPENAI_URL)
    print(f"[LLM] ✅ {mdl} via OpenAI")
    return text, mdl, usage


# ── Gemini steps ─────────────────────────────────────────────────────────────

def _try_gemini_key(key: str, self, model, contents, kwargs):
    """Try one Gemini key. Returns the genai response on success, or None if
    the key is on cooldown / hit a transient or auth error (it then goes on
    cooldown and the caller moves to the next key). Any other error raises."""
    with _lock:
        if time.time() < _gemini_key_block.get(key, 0.0):
            return None                        # this key is cooling down
    # Build a fresh client bound to THIS key — avoids poking SDK internals.
    try:
        from google import genai as _genai_mod
        client = self if _client_uses_key(self, key) else _genai_mod.Client(api_key=key)
    except Exception as e:
        print(f"[LLM] ⚠️  could not build Gemini client for key …{key[-6:]}: {str(e)[:70]}")
        with _lock:
            _gemini_key_block[key] = time.time() + _GEMINI_COOLDOWN_S
        return None
    try:
        # When the caller's client already uses this key, `client` IS `self` —
        # a Models instance, not a Client. Models has no `.models` attribute,
        # so calling `_orig_generate(client.models, …)` used to raise
        # AttributeError ('Models' object has no attribute 'models'), which is
        # non-transient and aborted the whole failover chain. A Models object
        # is exactly what _orig_generate binds to, so pass it directly.
        models_obj = client.models if hasattr(client, "models") else client
        return _orig_generate(models_obj, model=model, contents=contents, **kwargs)
    except RuntimeError as e:
        if "client has been closed" in str(e).lower():
            # Known google-genai/httpx quirk: rebuild the client once and retry.
            try:
                from google import genai as _genai_mod
                client = _genai_mod.Client(api_key=key)
                return _orig_generate(client.models, model=model, contents=contents, **kwargs)
            except Exception as e2:
                with _lock:
                    _gemini_key_block[key] = time.time() + _GEMINI_COOLDOWN_S
                print(f"[LLM] ⚠️  Gemini key …{key[-6:]} client rebuild failed: {str(e2)[:70]}")
                return None
        raise
    except Exception as e:
        if _is_quota_error(e) or _is_auth_error(e):
            # Daily quota or a bad key: it will not recover soon — long cooldown.
            with _lock:
                _gemini_key_block[key] = time.time() + _GEMINI_COOLDOWN_S
            kind = "auth" if _is_auth_error(e) else "quota"
            print(f"[LLM] ⚠️  Gemini key …{key[-6:]} {kind} error — cooling down "
                  f"{_GEMINI_COOLDOWN_S}s ({str(e)[:70]})")
            return None
        if _is_transient(e):
            # 503/overload/timeout: retry THIS key soon — a short cooldown,
            # otherwise a one-second blip benches Gemini for 10 minutes.
            with _lock:
                _gemini_key_block[key] = time.time() + _GEMINI_TRANSIENT_COOLDOWN_S
            print(f"[LLM] ⚠️  Gemini key …{key[-6:]} transient error — cooling down "
                  f"{_GEMINI_TRANSIENT_COOLDOWN_S}s ({str(e)[:70]})")
            return None
        raise                                  # real bug → surface it


def _client_uses_key(client, key: str) -> bool:
    """True if this client was constructed with this key (avoids rebuilding)."""
    return (getattr(client, "api_key", None) == key
            or getattr(getattr(client, "_api_client", None), "api_key", None) == key)


def _gemini_attempt(self, model, contents, kwargs):
    """Walk the Gemini keys (cooldowns respected). Returns the raw genai
    response; raises when every key is exhausted or cooling."""
    keys = _all_gemini_keys()
    if not keys:
        raise RuntimeError("no Gemini keys configured")
    for key in keys:
        resp = _try_gemini_key(key, self, model, contents, kwargs)
        if resp is not None:
            return resp
    raise RuntimeError("all Gemini keys failed or are cooling down")


# ── The local chain, exposed for other rungs (core/gemini.py's LOCAL rung) ──

def local_generate(prompt: str, system: str = "") -> tuple[str, str]:
    """One prompt through the local engines: Ollama first (warm, fast), then
    the RAM-guarded 3B coder GGUF. Returns (text, engine_name); raises
    RuntimeError when neither can serve — callers treat that like any failed
    rung and move on. Long prompts are refused: a small model with a
    truncated context answers confidently and wrongly."""
    if len(prompt) > LOCAL_MAX_PROMPT_CHARS:
        raise RuntimeError(f"prompt too long for the local model "
                           f"({len(prompt)} > {LOCAL_MAX_PROMPT_CHARS} chars)")
    full_cfg = _load_full_config()
    if bool(full_cfg.get("ollama_enabled") or full_cfg.get("ollama_models")):
        models = full_cfg.get("ollama_models") or _OLLAMA_DEFAULT_MODELS
        ok, reason = _ollama_alive(models[0])
        if ok:
            try:
                messages = ([{"role": "system", "content": system}] if system else []) \
                    + [{"role": "user", "content": prompt}]
                text, mdl, _u = _call_openrouter(
                    "ollama", models, messages, url=_OLLAMA_URL,
                    timeout=_OLLAMA_TIMEOUT_S, max_tokens=350)
                if text:
                    return text, f"ollama:{mdl}"
            except Exception as e:
                print(f"[LLM] ⚠️  ollama local hop failed: {str(e)[:100]}")
        else:
            print(f"[LLM] ⚠️  ollama skipped: {reason}")
    if _llm_local_configured():
        from core import llm_local
        if llm_local.ram_ok():
            text = llm_local.generate(prompt, system=system)
            if text:
                return text, "llama-cpp:qwen2.5-coder-3b"
    raise RuntimeError("no local model available (ollama down and GGUF missing "
                       "or RAM too tight)")


# ── The patched generate_content ─────────────────────────────────────────────

def _silence_sdk_advisories() -> None:
    """Drop the google-genai SDK's one-time AFC advisory from the console.

    The SDK logs "Direct use of automatic function calling (AFC) in
    Models.generate_content is not recommended…" whenever a plain call is
    eligible for AFC. JARVIS's auxiliary calls are one-shot prompt→text —
    there is no client-side tool loop to move into a Chat — and every real
    tool call goes through the Live API's function_declarations path, which
    disables AFC entirely. The advice does not apply; the filter removes only
    that exact message and leaves every other warning on the logger untouched.
    """
    import logging

    class _SkipAFCNotice(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return ("direct use of automatic function calling"
                    not in record.getMessage().lower())

    logging.getLogger("google_genai.models").addFilter(_SkipAFCNotice())


def _patched_generate(self, *, model=None, contents=None, **kwargs):
    # Tool-grounded calls (google_search etc.) only work on Gemini — no
    # OpenAI-compatible equivalent — so never redirect those (plan: the
    # SEARCH class stays on Gemini, deliberately with no local rung).
    cfg = kwargs.get("config") or {}
    if isinstance(cfg, dict) and cfg.get("tools"):
        return _orig_generate(self, model=model, contents=contents, **kwargs)

    full_cfg = _load_full_config()
    mode = _get_mode(full_cfg)
    system = _extract_system(kwargs)
    message = _contents_to_message(contents)
    task = _classify_task(message)
    long_prompt = _prompt_too_long(message)
    t0 = time.time()
    hops: list[dict] = []

    # MODE 4 — forced Gemini (deliberate use; bypasses the budget guard).
    if mode == "gemini":
        try:
            resp = _gemini_attempt(self, model, contents, kwargs)
            _log_usage(task, "gemini", str(model or "gemini"), True,
                       time.time() - t0, _genai_usage(resp), hops, mode)
            return resp
        except Exception as e:
            _log_usage(task, "gemini", str(model or "gemini"), False,
                       time.time() - t0, None, hops, mode, str(e))
            raise

    # Fresh install: no local engine, no fallback keys — stay transparent so
    # a plain Gemini setup behaves exactly as without the router.
    if mode == "smart" and _nothing_configured(full_cfg):
        return _orig_generate(self, model=model, contents=contents, **kwargs)

    flags = {
        "ollama": bool(full_cfg.get("ollama_enabled") or full_cfg.get("ollama_models")),
        "local3b": _llm_local_configured(),
        "freellm": bool((full_cfg.get("freellmapi_api_key") or "").strip()),
        "openrouter": bool(_load_router_config()),
        "grok": bool((full_cfg.get("grok_api_key") or "").strip()),
        "openai": bool((full_cfg.get("openai_api_key") or "").strip()),
    }
    plan = _plan_chain(mode, task, long_prompt,
                       budget_ok=_gemini_budget_ok(task, full_cfg), **flags)
    if not plan:
        reason = (f"router mode '{mode}' has no provider for task={task} "
                  f"(long={long_prompt})")
        _log_usage(task, "none", "", False, time.time() - t0, None, hops, mode, reason)
        raise RuntimeError(reason)

    fns = {
        "ollama":     lambda: _step_ollama(full_cfg, message, system, task),
        "llm_local":  lambda: _step_llm_local(message, system, task),
        "freellm":    lambda: _step_freellm((full_cfg.get("freellmapi_api_key") or "").strip(),
                                            message, system),
        "openrouter": lambda: _step_openrouter(_load_router_config(), message, system),
        "grok":       lambda: _step_grok(full_cfg, message, system),
        "openai":     lambda: _step_openai(full_cfg, message, system),
        "gemini":     lambda: _gemini_attempt(self, model, contents, kwargs),
    }

    for name in plan:
        try:
            out = fns[name]()
        except _Skip as e:
            hops.append({"provider": name, "reason": str(e)[:120]})
            continue
        except Exception as e:
            hops.append({"provider": name, "reason": str(e)[:120]})
            continue
        if not isinstance(out, tuple):
            # Raw genai response — the Gemini step answered.
            _log_usage(task, "gemini", str(model or "gemini"), True,
                       time.time() - t0, _genai_usage(out), hops, mode)
            return out
        text, used_model, usage = out
        if not _answer_ok(task, text):
            hops.append({"provider": name, "model": used_model,
                         "reason": "quality gate failed"})
            continue
        print(f"[LLM] ✅ {used_model} via {name}"
              + (f" ({len(hops)} hop(s) escalated)" if hops else ""))
        _log_usage(task, name, used_model, True, time.time() - t0, usage, hops, mode)
        return _ORResponse(text)

    _log_usage(task, "none", "", False, time.time() - t0, None, hops, mode,
               error="all providers failed")
    raise RuntimeError("All LLM providers failed — " +
                       "; ".join(f"{h['provider']}: {h['reason']}" for h in hops))


def install_router(verbose: bool = True) -> bool:
    """Patch genai's Models.generate_content with the routing version.
    Idempotent. Returns True when at least one provider (or a forced mode)
    is active."""
    global _installed, _orig_generate
    _silence_sdk_advisories()          # idempotent at the call sites below
    if _installed:
        return True
    try:
        from google.genai import models as _genai_models
    except Exception as e:
        if verbose:
            print(f"[LLM] ⚠️  could not import genai models — router off: {e}")
        return False

    _orig_generate = _genai_models.Models.generate_content
    _genai_models.Models.generate_content = _patched_generate
    _installed = True

    full_cfg = _load_full_config()
    mode = _get_mode(full_cfg)
    n_keys = len(_load_router_config())
    n_gemini = len(_all_gemini_keys())
    has_freellm = bool((full_cfg.get("freellmapi_api_key") or "").strip())
    has_ollama = bool(full_cfg.get("ollama_enabled") or full_cfg.get("ollama_models"))
    has_3b = _llm_local_configured()
    # Print order mirrors runtime priority: LOCAL → free cloud → Gemini.
    parts = []
    if has_ollama:
        parts.append("Local-Ollama")
    if has_3b:
        parts.append("Local-3B")
    if has_freellm:
        parts.append("FreeLLMAPI")
    if n_keys:
        parts.append(f"OpenRouter×{n_keys}")
    if (full_cfg.get("grok_api_key") or "").strip():
        parts.append("Grok")
    if (full_cfg.get("openai_api_key") or "").strip():
        parts.append("OpenAI")
    if mode == "smart":
        budget = "ok" if _gemini_budget_ok("general", full_cfg) else "exhausted"
        parts.append(f"Gemini×{n_gemini}(budget {budget})")
    if verbose:
        chain = " → ".join(parts) or "nothing configured"
        print(f"[LLM] Router active (mode={mode}) — {chain}")
    return bool(parts) or mode == "gemini"


def router_status() -> dict:
    """A snapshot for dashboards and debuggers (PROVIDER HEALTH CHECK).
    Never raises, never blocks on the network beyond the cached Ollama probe,
    and never echoes a key."""
    full_cfg = _load_full_config()
    models = full_cfg.get("ollama_models") or _OLLAMA_DEFAULT_MODELS
    ok, reason = _ollama_alive(models[0])
    return {
        "mode": _get_mode(full_cfg),
        "order": _plan_chain(
            _get_mode(full_cfg), "general", False,
            ollama=bool(full_cfg.get("ollama_enabled") or full_cfg.get("ollama_models")),
            local3b=_llm_local_configured(),
            freellm=bool((full_cfg.get("freellmapi_api_key") or "").strip()),
            openrouter=bool(_load_router_config()),
            grok=bool((full_cfg.get("grok_api_key") or "").strip()),
            openai=bool((full_cfg.get("openai_api_key") or "").strip()),
            budget_ok=_gemini_budget_ok("general", full_cfg),
        ),
        "ollama": {"enabled": bool(full_cfg.get("ollama_enabled") or full_cfg.get("ollama_models")),
                   "alive": ok, "model": models[0], "reason": reason},
        "local_3b": _llm_local_configured(),
        "freellmapi": bool((full_cfg.get("freellmapi_api_key") or "").strip()),
        "openrouter_keys": len(_load_router_config()),
        "gemini_keys": len(_all_gemini_keys()),
        "gemini_calls_today": _gemini_calls_today(),
        "gemini_daily_budget": int(full_cfg.get("gemini_daily_call_budget")
                                   or _GEMINI_BUDGET_DEFAULT),
        "usage_log": str(_USAGE_PATH),
    }
