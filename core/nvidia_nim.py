"""
nvidia_nim.py — NVIDIA NIM hosted-API provider (the plan's PRIORITY 2 cloud).

Position in RIO's provider priority (must agree with core/llm_router._plan_chain):

    P1  LOCAL   — Ollama → llama-cpp 3B GGUF (core/llm_router's local rungs).
                  ALWAYS attempted first; this module is never asked when a
                  local engine answered.
    P2  NVIDIA  — this module: https://integrate.api.nvidia.com/v1
                  (build.nvidia.com's hosted NIM endpoint, OpenAI-compatible,
                  `nvapi-…` key, free developer tier).
    P3  EXISTING CLOUD FALLBACKS — FreeLLMAPI → OpenRouter (free chain) →
                  Grok → OpenAI → Gemini, exactly as they were already
                  configured. Nothing was removed; NVIDIA was only promoted
                  ahead of them.

WIRE FORMAT (verified against docs.api.nvidia.com/nim/reference/llm-apis):
    POST {base}/chat/completions   {"model": id, "messages": [...]} → OpenAI shape
    GET  {base}/models             → {"data": [{"id": ...}, ...]}    → discovery
Auth: `Authorization: Bearer nvapi-…`. The key is read ONLY from the
NVIDIA_API_KEY environment variable or config/api_keys.json — never from
source code, never logged, never echoed.

MODEL REGISTRY (memory/nvidia_model_registry.json):
    Every model the /models list returns AND every request outcome is recorded:
      pricing       FREE (user-verified via the approved list) | UNKNOWN (the
                    catalog merely lists it — its pricing is NEVER confirmed) |
                    PAID (explicitly configured)
      availability  available | cooldown | unavailable
      last_ok / last_error / last_error_at / successes / failures

APPROVED MODELS — the explicit gate the plan requires: only models on the
approved list are EVER selected automatically; approved means the user
verified the model is free/available on their account (defaults are the four
ids this repo already verified live — see FREE_MODEL_PROVIDER_MATRIX.md).
Models reported by the catalog that are not approved keep pricing UNKNOWN and
are never routed automatically. Manual selection = edit the approved list in
the settings overlay.

FAILURE MANAGEMENT:
    401/403 → invalid key / no permission: ONE clear error; whole provider
              benched 10 min (NO continuous retry).
    404     → model gone: marked unavailable (no cooldown), next candidate.
    429     → rate limit / quota: THAT model cooled down 10 min; another
              eligible approved model tried — never repeat-hammered.
    5xx     → limited retries (2) with exponential backoff, then next model.
    network → 1 retry with backoff, then next model.
    context-length / empty responses → model marked error, next model.
    All failed → NimError (the router then fails over to the next provider).

No background polling: discovery runs on demand (UI action / 1h TTL) and
availability is rechecked through request outcomes and cooldown expiry.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_home = os.environ.get("JARVIS_HOME", "").strip()
if _home:
    _BASE = Path(_home).expanduser().resolve()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"
_REGISTRY_PATH = _BASE / "memory" / "nvidia_model_registry.json"

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
_CONNECT_TIMEOUT_S = 8      # connection phase
_READ_TIMEOUT_S = 60        # inference phase (NIM queues can be slow)
_MAX_RETRIES_5XX = 2        # limited retries, then the next model
_RETRIES_BACKOFF_S = 1.0    # exponential: 1s, 2s (bounded — no infinite loops)
_MODEL_COOLDOWN_429_S = 600 # rate-limited model parked 10 min
_AUTH_BLOCK_S = 600         # 401/403 benches the whole provider 10 min
_DISCOVERY_TTL_S = 3600

# Approved = user-verified as free & working on this account (live-probed in
# this repo). Explicit, never guessed. Override via config nvidia_approved_models.
_DEFAULT_APPROVED = [
    "nvidia/nemotron-3-super-120b-a12b",
    "deepseek-ai/deepseek-v4.1-flash",
    "openai/gpt-oss-20b",
    "z-ai/glm-5.3-flash",
]

# Task → preferred approved models (capability choice, all approved). Falls
# through to the remaining approved models; a model with recent successes
# floats up inside the rest.
_TASK_PREFS = {
    "code": ["deepseek-ai/deepseek-v4.1-flash",
             "nvidia/nemotron-3-super-120b-a12b"],
    "reason": ["nvidia/nemotron-3-super-120b-a12b",
               "openai/gpt-oss-20b"],
    "summarize": ["nvidia/nemotron-3-super-120b-a12b",
                  "z-ai/glm-5.3-flash"],
    "vision": [],          # vision stays on the upstream vision-capable rungs
    "general": ["nvidia/nemotron-3-super-120b-a12b",
                "z-ai/glm-5.3-flash", "openai/gpt-oss-20b"],
}
_TASK_CLASSIFY_HINTS = [
    ("code", ("code", "function", "script", "debug", "traceback", "error:",
              "regex", "compile", "class ", "def ", "import ", "sql",
              "refactor", "```", "bug", "python", "javascript")),
    ("summarize", ("summarize", "summary", "tl;dr", "condense", "key points",
                   "recap")),
]

_lock = threading.Lock()
_registry_cache: dict = {"at": 0.0, "data": None}
_discovery_cache: dict = {"at": 0.0, "ids": None, "error": ""}
_auth_block: dict = {"until": 0.0, "msg": ""}   # provider-level 401/403 bench


# ── configuration (env > config; secrets never echoed) ──────────────────────

def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _env_truthy(name: str, default: bool | None = None) -> bool | None:
    """NVIDIA_NIM_ENABLED-style parsing: 1/true/yes/on vs 0/false/no/off.
    None when unset so the config layer decides."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def base_url() -> str:
    """NVIDIA_NIM_BASE_URL env > config nvidia_nim_base_url > documented default."""
    env = os.environ.get("NVIDIA_NIM_BASE_URL", "").strip()
    if env:
        return env.rstrip("/")
    cfg = _load_config()
    return str(cfg.get("nvidia_nim_base_url") or DEFAULT_BASE_URL).rstrip("/")


def api_key() -> str:
    """NVIDIA_API_KEY env > config nvidia_api_key. Empty string when absent."""
    env = os.environ.get("NVIDIA_API_KEY", "").strip()
    if env:
        return env
    return (_load_config().get("nvidia_api_key") or "").strip()


def _enabled_config() -> bool:
    """The on/off switch, under either name it is written with.

    config/settings.py calls this field `nvidia_enabled` in its settings model
    but emits it as `nvidia_nim_enabled` in the runtime dict, and the two file
    writers (settings UI, hand edits) do not always agree on which lands in
    api_keys.json. Reading both means the switch the user sees is the switch
    that actually routes.
    """
    cfg = _load_config()
    for key in ("nvidia_nim_enabled", "nvidia_enabled"):
        if key in cfg:
            return bool(cfg[key])
    return True


def enabled() -> bool:
    """NVIDIA_NIM_ENABLED env > config nvidia_nim_enabled > default true."""
    return bool(_env_truthy("NVIDIA_NIM_ENABLED", _enabled_config()))


def has_key() -> bool:
    return bool(api_key())


def configured() -> bool:
    """True when NVIDIA belongs in the routing plan: enabled AND keyed."""
    return enabled() and has_key()


def skip_reason() -> str:
    """A human reason when NVIDIA must be skipped right now. '' = ready."""
    if not enabled():
        return "nvidia nim disabled"
    if not has_key():
        return "no nvidia key"
    with _lock:
        until = _auth_block.get("until", 0.0)
        if until and time.time() < until:
            return (f"cooling after auth error (retry in "
                    f"{int(until - time.time())}s)")
    return ""


def _redact(text: str) -> str:
    """Defense in depth: strip any bearer-token-looking substring before a
    message may reach a log or the UI."""
    return re.sub(r"nvapi-[A-Za-z0-9_\-]{8,}", "nvapi-<redacted>", text or "")


def _log(msg: str) -> None:
    print("[RIO AI] NVIDIA NIM " + _redact(str(msg)))


# ── the provider's three outcomes (the router's failover contract) ──────────

class NimSkip(Exception):
    """NVIDIA NIM cannot take this request right now (disabled, no key,
    cooling after an auth error, no eligible model, vision task). Expected
    skip — the router moves on; never surfaced as a crash."""


class NimAuthError(NimSkip):
    """401/403 — invalid key or no permission. Retrying will not help until
    the user fixes the key; the module benches the provider so nothing loops."""


class NimError(RuntimeError):
    """Every eligible approved model failed — the router should fail over to
    the next provider."""


# ── model registry (memory/nvidia_model_registry.json) ──────────────────────

def _registr(now: float) -> dict:
    with _lock:
        if _registry_cache["data"] is not None \
                and now - _registry_cache["at"] < 5.0:
            return _registry_cache["data"]
    try:
        data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    with _lock:
        _registry_cache.update({"at": now, "data": data})
    return data


def _save_registry(data: dict) -> None:
    with _lock:
        _registry_cache.update({"at": time.time(), "data": data})
    try:
        _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _REGISTRY_PATH.write_text(
            json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        _log(f"⚠ registry save failed: {e}")


def _approved() -> list[str]:
    """The user-approved model ids, however they were stored.

    api_keys.json is a flat settings file edited by hand, by the HUD and by
    config/settings.py — so this value arrives as a JSON list, a CSV string or
    a JSON-array string depending on who wrote it last. Accepting only a list
    meant a CSV override was silently ignored and the module fell back to
    _DEFAULT_APPROVED, i.e. it kept hammering models this account cannot use
    (404 / timeout) and every generation hung until the router gave up.
    """
    cfg = _load_config().get("nvidia_approved_models")
    items: list = []
    if isinstance(cfg, list):
        items = cfg
    elif isinstance(cfg, str) and cfg.strip():
        raw = cfg.strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                items = parsed if isinstance(parsed, list) else []
            except Exception:
                items = []
        if not items:
            items = raw.split(",")
    out = [str(m).strip() for m in items if str(m).strip()]
    return out or list(_DEFAULT_APPROVED)


def _registered(id_: str, data: dict) -> dict:
    return data.setdefault(id_, {
        "pricing": "FREE" if id_ in _approved() else "UNKNOWN",
        "availability": "available",
        "cooling_until": 0.0,
        "last_ok": 0.0,
        "last_error": "",
        "last_error_at": 0.0,
        "successes": 0,
        "failures": 0,
    })


def approved_models() -> list[str]:
    with _lock:
        return list(_approved())


def _mark(id_: str, *, availability: str | None = None, error: str | None = None,
          success: bool = False, cooling_for: float = 0.0) -> None:
    data = _registr(time.time())
    ent = _registered(id_, data)
    if availability is not None:
        ent["availability"] = availability
    if success:
        ent["successes"] = int(ent.get("successes", 0)) + 1
        ent["last_ok"] = time.time()
        ent["last_error"] = ""
        ent["last_error_at"] = 0.0
        ent["availability"] = "available"
        ent["cooling_until"] = 0.0
    else:
        ent["failures"] = int(ent.get("failures", 0)) + 1
        ent["last_error"] = _redact(str(error or ""))[:300]
        ent["last_error_at"] = time.time()
        if cooling_for > 0:
            ent["availability"] = "cooldown"
            ent["cooling_until"] = time.time() + cooling_for
    _save_registry(data)


def _cooling(id_: str, data: dict) -> bool:
    ent = _registered(id_, data)
    if time.time() < float(ent.get("cooling_until", 0) or 0):
        return True
    if ent.get("availability") == "cooldown":   # cooldown expired → recheck ok
        ent["availability"] = "available"
        ent["cooling_until"] = 0.0
    return False


def _last_ok_human(id_: str, data: dict) -> str:
    ts = float(_registered(id_, data).get("last_ok", 0) or 0)
    return (datetime.fromtimestamp(ts).isoformat(timespec="seconds")
            if ts else "never")


def model_status() -> list[dict]:
    """A UI-safe snapshot of the registry — ids, pricing, states, timings."""
    data = _registr(time.time())
    out = []
    for id_ in sorted(data):
        ent = data[id_]
        # pricing is recomputed against the CURRENT approved list: approval
        # (user-verified free/available on their account) is the single source
        # of truth for FREE; everything else keeps its stored status
        out.append({
            "id": id_,
            "pricing": "FREE" if id_ in _approved() else
                       ent.get("pricing", "UNKNOWN"),
            "availability": ent.get("availability", "available"),
            "cooling_for": max(0, int(float(ent.get("cooling_until", 0))
                                      - time.time())),
            "last_ok": _last_ok_human(id_, data),
            "successes": int(ent.get("successes", 0)),
            "failures": int(ent.get("failures", 0)),
            "last_error": ent.get("last_error", ""),
        })
    return out


# ── discovery (GET /models — on demand, 1h TTL; NO background polling) ──────

def discover_models(force: bool = False) -> tuple[list[str], str]:
    """Returns (model_ids, error). Catalog models are recorded with pricing
    UNKNOWN (a catalog listing does NOT prove free access — only the approved
    list does). Network failure is reported, never fatal."""
    now = time.time()
    with _lock:
        if not force and _discovery_cache["ids"] is not None \
                and now - _discovery_cache["at"] < _DISCOVERY_TTL_S:
            return list(_discovery_cache["ids"]), _discovery_cache["error"]
    if not configured():
        return [], "no nvidia key / disabled"
    ids, err = _http_list_models(api_key(), base_url())
    if err:
        with _lock:
            _discovery_cache.update({"at": now, "error": err})
        return ids, err
    data = _registr(time.time())
    approved = set(_approved())
    for mid in ids:
        ent = _registered(mid, data)       # pre-priced for approved, else UNKNOWN
        if ent.get("availability") == "unavailable":   # 404 recovered?
            ent["availability"] = "available"
    _save_registry(data)
    with _lock:
        _discovery_cache.update({"at": now, "ids": ids, "error": ""})
    _log(f"discovered {len(ids)} catalog models "
         f"({len(approved & set(ids))} approved present)")
    return ids, ""


def _http_list_models(key: str, base: str) -> tuple[list[str], str]:
    """GET {base}/models → ([ids], error_message)."""
    import requests
    try:
        r = requests.get(base + "/models",
                         headers={"Authorization": f"Bearer {key}",
                                  "Accept": "application/json"},
                         timeout=_CONNECT_TIMEOUT_S)
    except Exception as e:
        return [], f"model list unreachable: {type(e).__name__}: {str(e)[:100]}"
    if r.status_code == 401:
        _bench_auth("model-list check rejected the key (HTTP 401)")
        return [], "invalid API key (HTTP 401)"
    if r.status_code == 403:
        _bench_auth("model-list check denied access (HTTP 403)")
        return [], "access denied (HTTP 403)"
    if r.status_code != 200:
        return [], f"model list HTTP {r.status_code} {_redact(r.text[:80])}"
    try:
        data = r.json()
        ids = [str(d.get("id")) for d in data.get("data", [])
               if str(d.get("id") or "").strip()]
        return ids, ""
    except Exception as e:
        return [], f"model list unparsable: {e}"


# ── task classification (lightweight keyword router) ────────────────────────

def classify_task(messages: list[dict]) -> str:
    """general | code | summarize — keyword heuristics on text content only
    (messages arrive as OpenAI-style dicts; image content is ignored here)."""
    text = " ".join(m.get("content") for m in messages
                    if isinstance(m, dict) and isinstance(m.get("content"), str))
    low = text.lower()
    for task, words in _TASK_CLASSIFY_HINTS:
        if any(w in low for w in words):
            return task
    return "general"


# ── model selection (approved + available + task capability + recent wins) ──

def _recent_ok(m: str, data: dict) -> float:
    return float(_registered(m, data).get("last_ok", 0) or 0)


def _eligible_models(task: str) -> list[str]:
    """The ONLY models this provider may touch automatically: approved
    (pricing verified by the user), not cooling, not marked unavailable
    (404). UNKNOWN/PAID models are NEVER auto-selected. Order: task
    preference first (available ones), then the remaining approved models
    with the most recently successful first. Capability unknown for the task
    → the safe general-purpose approved preferences lead."""
    data = _registr(time.time())
    approved = _approved()
    avail = [m for m in approved
             if not _cooling(m, data)
             and _registered(m, data).get("availability") != "unavailable"]
    prefs = [m for m in _TASK_PREFS.get(task or "general", []) if m in avail]
    rest = sorted((m for m in avail if m not in prefs),
                  key=lambda m: (_recent_ok(m, data), -m.count("-")),
                  reverse=True)
    out = prefs + rest
    if not out:   # task-specific prefs all gone → general-purpose safe set
        out = [m for m in _TASK_PREFS["general"] if m in avail]
    return out


# ── the chat call itself ────────────────────────────────────────────────────

def _http_chat(key: str, model: str, payload: dict, base: str,
               timeout) -> tuple[int, str]:
    """POST {base}/chat/completions → (status_code, raw_body_text)."""
    import requests
    r = requests.post(base + "/chat/completions",
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"},
                      json=payload, timeout=timeout)
    return r.status_code, r.text


def chat(messages: list[dict], task: str | None = None, *,
         timeout: tuple | None = None, max_tokens: int = 700
         ) -> tuple[str, str, dict | None]:
    """One routed NVIDIA NIM request → (text, model_id, usage).
    Raises NimSkip (provider cannot serve now) / NimAuthError (bad key) /
    NimError (every eligible model failed) — never loops forever."""
    print("[RIO AI] Request received (provider: NVIDIA NIM)")
    if not enabled():
        raise NimSkip("nvidia nim disabled")
    if task == "vision":
        # No approved NIM model is verified vision-capable → vision requests
        # stay on the upstream vision rungs (FreeLLMAPI/OpenRouter/Gemini).
        raise NimSkip("vision tasks stay on vision-capable providers")
    key = api_key()
    if not key:
        print("[RIO AI] no NVIDIA key — skipping provider")
        raise NimSkip("no nvidia key")
    with _lock:
        if time.time() < _auth_block.get("until", 0.0):
            raise NimSkip("cooling after last auth error")
    base = base_url()
    if task is None:
        task = classify_task(messages)
    models = _eligible_models(task)
    if not models:
        _log("no available approved models — skipping provider")
        raise NimSkip("no available approved models")
    to = tuple(timeout) if timeout else (_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S)
    payload_base = {"messages": messages, "max_tokens": max_tokens}
    last_err = "no attempt made"

    for model in models:
        data = _registr(time.time())
        if _cooling(model, data):
            continue
        print(f"[RIO AI] Selected approved model: {model}")
        for attempt in range(_MAX_RETRIES_5XX + 1):
            payload = dict(payload_base)
            payload["model"] = model
            t0 = time.time()
            try:
                status, body = _http_chat(key, model, payload, base, to)
            except Exception as e:
                if attempt < _MAX_RETRIES_5XX:
                    _log(f"network failure on {model} — retry "
                         f"{attempt + 1}/{_MAX_RETRIES_5XX} after backoff")
                    _backoff_sleep(attempt)
                    continue
                _mark(model, error=f"network: {type(e).__name__}: {str(e)[:80]}")
                last_err = f"{model}: network {type(e).__name__}"
                break
            latency = time.time() - t0
            if status == 200:
                text, usage, err = _parse_ok(body)
                if text:
                    _mark(model, success=True)
                    _log(f"request successful {model} in {latency:.2f}s")
                    return text, model, usage
                _mark(model, error=err or "empty response")
                last_err = f"{model}: {err or 'empty response'}"
                break
            if status in (401, 403):
                _bench_auth(f"chat rejected the key (HTTP {status})")
                raise NimAuthError(_auth_message())
            if status == 404:
                _mark(model, availability="unavailable",
                      error="model not available (HTTP 404)")
                _log(f"model unavailable (404): {model} — trying next")
                last_err = f"{model}: HTTP 404"
                break
            if status == 429:
                _mark(model, availability="cooldown",
                      error="rate limited (HTTP 429)",
                      cooling_for=_MODEL_COOLDOWN_429_S)
                _log(f"rate limit (429) on {model} — cooled down "
                     f"{_MODEL_COOLDOWN_429_S}s; next candidate")
                last_err = f"{model}: HTTP 429 rate-limited"
                break
            if 500 <= status <= 599:
                if attempt < _MAX_RETRIES_5XX:
                    _log(f"HTTP {status} on {model} — retry "
                         f"{attempt + 1}/{_MAX_RETRIES_5XX} after backoff")
                    _backoff_sleep(attempt)
                    continue
                _mark(model, error=f"HTTP {status} (retries exhausted)")
                last_err = f"{model}: HTTP {status}"
                break
            if status == 400 and _is_context_error(body):
                _mark(model, error="context too long (HTTP 400)")
                _log(f"context-length error on {model} — trying next")
                last_err = f"{model}: context length"
                break
            _mark(model, error=f"HTTP {status}: {body[:120]}")
            last_err = f"{model}: HTTP {status}"
            break
    raise NimError(f"all eligible NVIDIA models failed — last: {last_err}")


def _auth_message() -> str:
    with _lock:
        return _redact(_auth_block.get("msg", ""))


def _bench_auth(msg: str) -> None:
    with _lock:
        _auth_block.update({"until": time.time() + _AUTH_BLOCK_S, "msg": msg})
    _log(f"⚠ auth error — provider benched {_AUTH_BLOCK_S}s ({_redact(msg)})")


def _backoff_sleep(attempt: int) -> None:
    time.sleep(_RETRIES_BACKOFF_S * (2 ** attempt))


def _parse_ok(body: str) -> tuple[str, dict | None, str]:
    """Returns (text, usage, error). Never raises. Handles the segment-list
    content shape some NIM models return."""
    try:
        data = json.loads(body)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        if isinstance(text, list):
            text = "".join(t.get("text", "") for t in text if isinstance(t, dict))
        usage = data.get("usage")
        if not text:
            err = (data.get("detail") or data.get("message")
                   or choice.get("finish_reason") or "empty completion")
            return "", usage, f"empty response ({err})"
        return text, usage, ""
    except Exception as e:
        return "", None, f"unparsable response: {e}"


def _is_context_error(body: str) -> bool:
    low = str(body).lower()
    return any(m in low for m in ("context length", "maximum context",
                                  "too many tokens", "reduce the length",
                                  "below the maximum"))


# ── connection test (for the settings UI — small, quota-safe) ───────────────

def test_connection() -> tuple[bool, str]:
    """1) auth + model-list test  2) ONE tiny completion (max_tokens=24) on
    the first eligible approved model. Returns (ok, human message). Never
    echoes the key, never burns quota beyond that single 24-token probe."""
    if not has_key():
        return False, "NVIDIA API key is not configured"
    if not enabled():
        return False, "NVIDIA NIM is set to disabled"
    ids, err = discover_models(force=True)
    if err:
        if "401" in err or "403" in err:
            return False, (err + "  →  check the key at build.nvidia.com "
                           "(must start with nvapi-)")
        return False, err
    if not _eligible_models("general"):
        return (False, f"auth OK — {len(ids)} models discovered but no approved "
                       "model is currently available (cooldown/404?)")
    try:
        text, used, _u = chat(
            [{"role": "user", "content": "Reply with the single word: ready"}],
            task="general", max_tokens=24)
    except NimAuthError as e:
        return False, f"authentication failed: {e}"
    except NimError as e:
        return False, f"auth OK ({len(ids)} models) but inference failed: {e}"
    except NimSkip as e:
        return False, f"skipped: {e}"
    return True, (f"NVIDIA NIM OK — key valid, {len(ids)} models discovered, "
                  f"test model {used} answered "
                  f"({str(text).strip()[:30] or '<empty>'})")


# ── status + settings section (consumed by ui.py's settings overlay) ───────

def status() -> dict:
    """Raw provider state for dashboards. Never echoes the key, no network."""
    data = _registr(time.time())
    oks = [(float(e.get("last_ok") or 0), m) for m, e in data.items()
           if float(e.get("last_ok") or 0)]
    last_success = ""
    if oks:
        ts, m = max(oks)
        last_success = (f"{m} at "
                        f"{datetime.fromtimestamp(ts).isoformat(timespec='seconds')}")
    return {
        "enabled": _enabled_config(),
        "env_enabled": _env_truthy("NVIDIA_NIM_ENABLED"),
        "key_set": has_key(),
        "base_url": base_url(),
        "skip_reason": skip_reason(),
        "auth_blocked_for": max(
            0, int(_auth_block.get("until", 0) - time.time())),
        "approved": approved_models(),
        "models": model_status(),
        "last_success": last_success,
    }


def active_indicator() -> str:
    """The clear provider-state banner the settings overlay shows, derived
    from the most recent successful routed request in the usage log
    (metadata only — conversation text never enters that log)."""
    try:
        path = _BASE / "memory" / "llm_usage.jsonl"
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()[-40:]
            for line in reversed(lines):
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not rec.get("ok") or not _fresh_enough(str(rec.get("ts", ""))):
                    continue
                prov, mdl, when = rec.get("provider", ""), \
                    rec.get("model", ""), rec.get("ts", "")
                if prov in ("ollama", "llm_local"):
                    return f"LOCAL MODEL ACTIVE  ({mdl} — last ok {when})"
                if prov == "nvidia":
                    return f"NVIDIA NIM ACTIVE  ({mdl} — last ok {when})"
                return (f"EXISTING CLOUD FALLBACK ACTIVE  ({prov}: {mdl}"
                        f" — last ok {when})")
    except Exception:
        pass
    try:
        return "ALL PROVIDERS IDLE — router will try: " + _priority_chain()
    except Exception:
        return "PROVIDER STATE UNKNOWN"


def _fresh_enough(when: str) -> bool:
    """Counts as 'active state' for max 1h after a request."""
    try:
        ts = datetime.fromisoformat(when).timestamp()
        return time.time() - ts < 3600
    except Exception:
        return False


def _priority_chain() -> str:
    from orchestrator.model_router.legacy_shim import router_status
    order = router_status().get("order") or []
    return " → ".join(order) or "nothing configured"


def settings_section() -> dict:
    """The settings section handed to ui.py's PluginSettingsOverlay — built
    HERE so the core UI stays generic. Values come from config; the key
    renders masked. `_apply_settings` is honored via sec['save']."""
    cfg = _load_config()
    try:
        chain_txt = _priority_chain()
    except Exception as e:
        chain_txt = f"(unavailable: {e})"
    st = status()
    model_lines = [
        f"{m['id']}  [{m['availability']}"
        + (f"({m['cooling_for']}s)" if m['cooling_for'] else "")
        + f"]  pricing={m['pricing']}  last_ok={m['last_ok']}"
        for m in st["models"][:8]
    ]
    return {
        "namespace": "nvidia_nim",
        "title": "⌁  RIO AI PROVIDERS  (P1 Local → P2 NVIDIA NIM → P3 Cloud)",
        "status": (f"{active_indicator()}\n"
                   f"Priority chain: {chain_txt}\n"
                   f"NVIDIA last success: {st['last_success'] or 'never'}\n"
                   + ("\n".join(model_lines) if model_lines
                      else "NVIDIA registry empty — run TEST + DISCOVER")),
        "fields": [
            {"key": "local_ai_enabled", "type": "toggle",
             "label": "Local AI (Ollama) — priority 1"},
            {"key": "nvidia_nim_enabled", "type": "toggle",
             "label": "NVIDIA NIM — priority 2"},
            {"key": "nvidia_api_key", "type": "password",
             "label": "NVIDIA API key (nvapi-…) — saved to config/api_keys.json",
             "placeholder": ("<unchanged — type only when setting a new key>")
             if cfg.get("nvidia_api_key") else "nvapi-…"},
            {"key": "router_mode", "type": "choice",
             "label": "Cloud fallback: smart = automatic, free = free-tier only, "
                      "local = cloud OFF",
             "options": ["smart", "free", "local", "gemini"]},
            {"key": "nvidia_approved_models", "type": "text",
             "label": "Approved models (comma-separated, user-verified free)",
             "placeholder": ", ".join(_DEFAULT_APPROVED)},
            {"key": "nvidia_nim_base_url", "type": "text",
             "label": "NVIDIA NIM base URL",
             "placeholder": DEFAULT_BASE_URL},
        ],
        "action": {"label": "TEST + DISCOVER MODELS",
                   "run": _ui_test_and_discover},
        "save": _apply_settings,
    }


def _save_config(cfg: dict) -> None:
    """config/api_keys.json is gitignored (verified in .gitignore) — the
    project's key store of record. Keys are written with indent=2 like the
    existing format so diffing stays human-readable."""
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                            encoding="utf-8")


def _apply_settings(values: dict) -> None:
    """Persist the section into config/api_keys.json. An EMPTY password field
    keeps the stored key (the overlay never echoes secrets back)."""
    cfg = _load_config()
    key = (values.get("nvidia_api_key") or "").strip()
    if key and not key.startswith("nvapi-"):
        raise ValueError("NVIDIA keys start with 'nvapi-' — nothing saved")
    cfg["nvidia_nim_enabled"] = bool(values.get("nvidia_nim_enabled"))
    if values.get("router_mode"):
        cfg["router_mode"] = str(values["router_mode"])
    if values.get("nvidia_approved_models", ""):
        lst = [s.strip() for s in str(values["nvidia_approved_models"]).split(",")
               if s.strip()]
        if lst:
            cfg["nvidia_approved_models"] = lst
    if (values.get("nvidia_nim_base_url") or "").strip():
        cfg["nvidia_nim_base_url"] = \
            str(values["nvidia_nim_base_url"]).strip().rstrip("/")
    cfg["ollama_enabled"] = bool(values.get("local_ai_enabled"))
    cfg["ollama_models"] = cfg.get("ollama_models") or ["qwen2.5:1.5b"]
    if key:
        cfg["nvidia_api_key"] = key
    _save_config(cfg)
    with _lock:                                   # config changed → invalidate
        _discovery_cache.update({"at": 0.0, "ids": None, "error": ""})


def _ui_test_and_discover(_values=None) -> tuple[bool, str]:
    """Action handler for the settings overlay: connection test + discovery."""
    ok, msg = test_connection()
    if ok:
        ids, err = discover_models(force=True)
        n_approved = len(set(_approved()) & set(ids))
        return True, (f"{msg} — catalog refreshed: {len(ids)} models "
                      f"({n_approved} approved present)")
    return ok, msg
