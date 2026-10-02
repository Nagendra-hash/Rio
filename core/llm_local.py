"""
core/llm_local.py — the LOCAL rung: a small local model for when Gemini's
quota is exhausted.

WHY THIS EXISTS
    The ladder in core/gemini.py walks Live → pinned REST rungs. On the free
    tier the TEXT pool is the one that dies; when every REST rung is cooling
    or erroring, one-shot features used to answer with the caller's default.
    This rung keeps them answering — offline, quota-free, on this machine.

MODEL
    Qwen2.5-Coder-3B-Instruct, Q4_K_M GGUF (~1.9 GB). Chosen for a 4 GB RAM
    machine: the whole quantised model loads in ~1.9 GB, and context+logits
    add a few hundred MB at n_ctx=2048.

THE CONSTRAINT THAT SHAPES EVERYTHING: RAM
    This laptop has 3.7 GB and JARVIS itself uses a good slice. So the model
    is NEVER loaded at import and NEVER kept between calls:

      * load happens on the first LOCAL call that actually needs it,
      * every load is preceded by an available-RAM check (default 1.6 GB),
        because loading into a memory-tight box can trigger the OOM killer
        and take the whole assistant down with it,
      * each call unloads afterwards by default (LOCAL_TTL_S), so the RAM
        returns to the conversation.

THREADING
    A lock serialises load/generate/unload. Generation itself runs with the
    lock HELD — llama-cpp is not reliably reentrant, and two concurrent
    generations on 2 cores would be slower than one anyway.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import numpy as np

import os as _os_home
_BASE_HOME = _os_home.environ.get("JARVIS_HOME", "").strip()
BASE_DIR = (Path(_BASE_HOME).expanduser().resolve() if _BASE_HOME
            else Path(__file__).resolve().parent.parent)
DEFAULT_MODEL_PATH = BASE_DIR / "models" / \
    "qwen2.5-coder-3b-instruct-q4_k_m.gguf"

# ── knobs (env-overridable, sane defaults for this machine) ──────────────────
MODEL_PATH = Path(os.environ.get("JARVIS_LOCAL_MODEL", str(DEFAULT_MODEL_PATH)))
LOCAL_MIN_RAM_MB = int(os.environ.get("JARVIS_LOCAL_MIN_RAM_MB", "1600"))
LOCAL_TTL_S = float(os.environ.get("JARVIS_LOCAL_TTL_S", "300"))
LOCAL_N_CTX = int(os.environ.get("JARVIS_LOCAL_N_CTX", "2048"))
LOCAL_MAX_TOKENS = int(os.environ.get("JARVIS_LOCAL_MAX_TOKENS", "512"))
LOCAL_N_THREADS = max(1, (os.cpu_count() or 2) - 1)

_gen_lock = threading.Lock()
_llm = None
_llm_loaded_at = 0.0


def _available_ram_mb() -> int:
    try:
        import psutil
        return int(psutil.virtual_memory().available / (1024 * 1024))
    except Exception:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable"):
                        return int(line.split()[1]) // 1024
        except Exception:
            pass
    return 0


def is_configured() -> bool:
    """The rung exists only when the model file is on disk."""
    try:
        return MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 50_000_000
    except Exception:
        return False


def ram_ok() -> bool:
    """True when loading now would not squeeze the machine."""
    return _available_ram_mb() >= LOCAL_MIN_RAM_MB


def status() -> dict:
    loaded = _llm is not None
    return {
        "configured": is_configured(),
        "model_path": str(MODEL_PATH),
        "model_loaded": loaded,
        "loaded_for_s": round(time.monotonic() - _llm_loaded_at, 1) if loaded else 0.0,
        "ram_available_mb": _available_ram_mb(),
        "min_ram_mb": LOCAL_MIN_RAM_MB,
        "ram_ok": ram_ok(),
        "ttl_s": LOCAL_TTL_S,
        "n_ctx": LOCAL_N_CTX,
    }


def _load():
    global _llm, _llm_loaded_at
    if _llm is not None:
        return _llm
    from llama_cpp import Llama
    print(f"[Local] loading {MODEL_PATH.name} "
          f"(RAM avail {_available_ram_mb()} MB)…", flush=True)
    t0 = time.time()
    _llm = Llama(
        model_path=str(MODEL_PATH),
        n_ctx=LOCAL_N_CTX,
        n_threads=LOCAL_N_THREADS,
        n_batch=256,          # small batches: gentler on RAM and on 2 cores
        verbose=False,
    )
    _llm_loaded_at = time.time()
    print(f"[Local] ready in {time.time() - t0:.1f}s", flush=True)
    return _llm


def _return_ram_to_os() -> None:
    """Best-effort: hand freed glibc heap arenas back to the OS so the freed
    model RAM shows up in MemAvailable again. No-op elsewhere."""
    try:
        import ctypes
        libc = ctypes.CDLL(None)
        if hasattr(libc, "malloc_trim"):
            libc.malloc_trim(0)
    except Exception:
        pass


def unload() -> None:
    """Free the model and (best-effort) the arena. Only call while holding
    _gen_lock."""
    global _llm, _llm_loaded_at
    if _llm is not None:
        # llama-cpp ≥0.3 owns everything through Llama.close() (its ExitStack
        # frees model, context and batch). Dropping the reference without
        # closing leaks the ~2 GB of C allocations — Python's GC cannot see
        # them — and __del__ later crashes on the missing attribute.
        try:
            _llm.close()
        except Exception:
            pass
        _llm = None
        import gc
        gc.collect()
        _return_ram_to_os()
        _llm_loaded_at = 0.0


def _maybe_ttl_unload() -> None:
    if _llm is not None and (time.monotonic() - _llm_loaded_at) > LOCAL_TTL_S:
        print("[Local] TTL reached — unloading to free RAM", flush=True)
        unload()


def generate(prompt: str, system: str = "", max_tokens: int | None = None,
             temperature: float = 0.2, keep_loaded: bool = False) -> str:
    """One instruction → one completion through the local model.

    Raises RuntimeError with a HUMAN reason when the rung cannot run (model
    missing / not enough RAM) — the ladder treats that like any other failed
    rung and moves on (or reports, if it is the last one).
    """
    if not is_configured():
        raise RuntimeError(f"local model not present at {MODEL_PATH}")
    if not ram_ok():
        raise RuntimeError(
            f"not enough free RAM to load the local model "
            f"({_available_ram_mb()} MB < {LOCAL_MIN_RAM_MB} MB)")

    with _gen_lock:
        _maybe_ttl_unload()
        llm = _load()
        try:
            out = llm.create_chat_completion(
                messages=([{"role": "system", "content": system}] if system else [])
                + [{"role": "user", "content": prompt}],
                max_tokens=int(max_tokens or LOCAL_MAX_TOKENS),
                temperature=temperature,
                stream=False,
            )
            text = (out.get("choices") or [{}])[0].get("message", {}).get("content", "")
            return (text or "").strip()
        finally:
            if not keep_loaded:
                unload()


def embed_fallback(texts, is_query: bool = False) -> list:
    """Rarely-used fallback: hash-bag vectors for the RAG lexical layer when
    the embedding API is also down. NOT semantic — keeps search alive, never
    pretends to be a real embedding."""
    out = []
    for t in texts:
        v = np.zeros(EMBED_FALLBACK_DIM, dtype=np.float32)
        for w in str(t).lower().split():
            v[hash(w) % (EMBED_FALLBACK_DIM - 8)] += 1.0
        n = float(np.linalg.norm(v))
        out.append(v / n if n > 0 else v)
    return out


EMBED_FALLBACK_DIM = 256
