#!/usr/bin/env python3
"""
videorag_run.py — run VideoRAG (HKUDS) indexing and queries from JARVIS.

VideoRAG (github.com/HKUDS/VideoRAG, KDD'26) is a retrieval-augmented
framework for extremely long videos: it slices a video into segments, runs
ASR, captions every segment with MiniCPM-V, embeds each segment with
ImageBind, then builds a dual knowledge graph so a question can be answered
from hours of footage.

The framework lives in its OWN venv (.venv-videorag) because it pins a
2024-era stack (torch 2.1.2, transformers 4.37.1, numpy 1.26.4) that must
never touch JARVIS'. The upstream source is pip-installed (editable) from

    /home/nagendra/VideoRAG/VideoRAG-algorithm      (override: VIDEORAG_ROOT)

which is also where the model checkpoints must sit, because upstream
hardcodes relative model paths ('./MiniCPM-V-2_6-int4',
'./faster-distil-whisper-large-v3', '.checkpoints/imagebind_huge.pth').
This runner therefore chdir()s to that directory before touching the
framework, and keeps every workdir absolute.

SCOPE ON THIS BOX
-----------------
Indexing and querying are GPU-only upstream: ImageBind is loaded with a
hardcoded .cuda() (videorag/_storage/vdb_nanovectordb.py) and the caption
model is MiniCPM-V int4. With no CUDA device the runner refuses early with a
precise reason instead of dying halfway through a multi-hour job. `status`
is always safe to run and reports exactly what is missing.

BRAIN
-----
Two OpenAI-compatible routes, no direct provider quota spent:
  ollama    — 127.0.0.1:11434, zero-quota local floor (embed: nomic-embed-text)
  openai    — any OpenAI-compatible base URL, default the house FreeLLMAPI
              pool at 127.0.0.1:3001/v1 (key from config/api_keys.json)
`auto` probes ollama first (it is the only route guaranteed to serve
embeddings), then FreeLLMAPI.

Usage:
  python3 scripts/videorag_run.py status [--json]
  python3 scripts/videorag_run.py index VIDEO [VIDEO ...] [--workdir DIR]
  python3 scripts/videorag_run.py query "QUESTION" [--references]
  python3 scripts/videorag_run.py list [--workdir DIR]

Exit codes: 0 ok · 2 preflight blocked (GPU/weights/brain missing) · 1 failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
_CONFIG = BASE / "config" / "api_keys.json"

# Install location follows the house convention for repository installs
# (~/JarvisTools/<name>), so the pinned 2024-era stack stays out of Mark-III.
# Deliberately NOT derived from Path.home(): the repo-agent worker runs with a
# fake HOME, and these paths must not follow it.
_TOOLS = Path(os.environ.get("VIDEORAG_HOME", "/home/nagendra/JarvisTools/videorag"))
_VENV_PY = Path(os.environ.get("VIDEORAG_PYTHON", _TOOLS / ".venv" / "bin" / "python"))

# Upstream checkout + where its checkpoints live. Overridable for a rebuild.
UPSTREAM = Path(os.environ.get("VIDEORAG_ROOT", _TOOLS / "VideoRAG-algorithm"))
WORKDIR = Path(os.environ.get("VIDEORAG_WORKDIR", BASE / "memory" / "videorag"))

_MINICPM = "MiniCPM-V-2_6-int4"
_WHISPER = "faster-distil-whisper-large-v3"
_IMAGEBIND = ".checkpoints/imagebind_huge.pth"

_OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
_FREELLM_URL = os.environ.get("VIDEORAG_OPENAI_BASE_URL", "http://127.0.0.1:3001/v1")


# ─────────────────────────────────────────────────────────── helpers
def _gb(path: Path) -> float:
    """Size of a file or directory tree in GB (0.0 when absent)."""
    if not path.exists():
        return 0.0
    if path.is_file():
        return path.stat().st_size / 1e9
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total / 1e9


def _keys() -> dict:
    try:
        return json.loads(_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _probe(url: str, timeout: float = 2.0) -> dict | list | None:
    """GET a JSON endpoint; None when nothing is listening."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _ollama_models() -> list[str]:
    data = _probe(f"{_OLLAMA_URL}/api/tags")
    if isinstance(data, dict):
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    return []


def _pick_ollama(embed_preferred=("nomic-embed-text", "bge-m3", "mxbai-embed")):
    """Return (chat_model, embed_model) from the local ollama catalogue."""
    models = _ollama_models()
    if not models:
        return "", ""
    embed = next((m for m in models if any(p in m for p in embed_preferred)), "")
    chat = next((m for m in models if m != embed), "")
    return chat, embed


# ─────────────────────────────────────────────────────────── preflight
def collect_status() -> dict:
    """Everything the action and a human need to know *without* running a job."""
    weights = {
        "minicpm_v": {"path": str(UPSTREAM / _MINICPM),
                      "present": (UPSTREAM / _MINICPM).exists(),
                      "gb": round(_gb(UPSTREAM / _MINICPM), 2)},
        "whisper": {"path": str(UPSTREAM / _WHISPER),
                    "present": (UPSTREAM / _WHISPER).exists(),
                    "gb": round(_gb(UPSTREAM / _WHISPER), 2)},
        "imagebind": {"path": str(UPSTREAM / _IMAGEBIND),
                      "present": (UPSTREAM / _IMAGEBIND).exists(),
                      "gb": round(_gb(UPSTREAM / _IMAGEBIND), 2)},
    }

    gpu = {"available": False, "devices": [], "torch": "", "error": ""}
    try:
        import torch

        gpu["torch"] = torch.__version__
        if torch.cuda.is_available():
            gpu["available"] = True
            gpu["devices"] = [
                {"name": torch.cuda.get_device_name(i),
                 "vram_gb": round(torch.cuda.get_device_properties(i).total_memory / 1e9, 1)}
                for i in range(torch.cuda.device_count())
            ]
    except Exception as exc:                                  # pragma: no cover
        gpu["error"] = f"{type(exc).__name__}: {exc}"

    chat_model = os.environ.get("VIDEORAG_CHAT_MODEL", "")
    embed_model = os.environ.get("VIDEORAG_EMBED_MODEL", "")
    brain = {"mode": os.environ.get("VIDEORAG_BRAIN", "auto"),
             "base_url": "", "chat_model": chat_model, "embed_model": embed_model,
             "key": False}
    ollama_chat, ollama_embed = _pick_ollama()
    if ollama_chat and ollama_embed:
        brain.update(base_url=_OLLAMA_URL, chat_model=chat_model or ollama_chat,
                     embed_model=embed_model or ollama_embed, key=True)
    elif _probe(f"{_FREELLM_URL}/models") is not None:
        brain.update(base_url=_FREELLM_URL,
                     chat_model=chat_model or "auto",
                     embed_model=embed_model or "text-embedding-3-small",
                     key=bool(_keys().get("freellmapi_api_key")))
    else:
        brain.update(base_url=_FREELLM_URL,
                     chat_model=chat_model or "auto",
                     embed_model=embed_model or "text-embedding-3-small",
                     key=bool(_keys().get("freellmapi_api_key")))

    blocked = []
    if not UPSTREAM.exists():
        blocked.append(f"upstream source missing at {UPSTREAM}")
    if not gpu["available"]:
        blocked.append("no CUDA device — ImageBind is hardcoded .cuda() upstream")
    for name, w in weights.items():
        if not w["present"]:
            blocked.append(f"{name} weights missing ({w['path']})")

    videos = []
    kv = WORKDIR / "kv_store_video_path.json"
    if kv.exists():
        try:
            videos = sorted(json.loads(kv.read_text(encoding="utf-8")).keys())
        except Exception:
            videos = []

    return {
        "upstream": str(UPSTREAM),
        "venv_python": str(_VENV_PY),
        "venv_installed": _VENV_PY.exists(),
        "gpu": gpu,
        "weights": weights,
        "brain": brain,
        "workdir": str(WORKDIR),
        "indexed_videos": videos,
        "blockers": blocked,
        "ready": not blocked,
    }


def _require_ready(status: dict, need_weights: bool = True) -> None:
    """Refuse a job that cannot finish, with the reason printed to stderr."""
    problems = list(status["blockers"])
    if not need_weights:
        problems = [p for p in problems if "weights missing" not in p]
    if problems:
        print("VideoRAG preflight failed:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "\nGPU + the three checkpoints are required for indexing and querying.\n"
            "  git lfs install\n"
            f"  cd {UPSTREAM}\n"
            "  git lfs clone https://huggingface.co/openbmb/MiniCPM-V-2_6-int4\n"
            "  git lfs clone https://huggingface.co/Systran/faster-distil-whisper-large-v3\n"
            "  mkdir -p .checkpoints && cd .checkpoints && "
            "wget https://dl.fbaipublicfiles.com/imagebind/imagebind_huge.pth",
            file=sys.stderr,
        )
        raise SystemExit(2)


# ─────────────────────────────────────────────────────────── brain
def build_llm(prefer: str = "auto"):
    """Build a videorag LLMConfig on top of an OpenAI-compatible endpoint."""
    sys.path.insert(0, str(UPSTREAM))
    from videorag._llm import (            # noqa: E402  (import after chdir)
        LLMConfig,
        openai_complete_if_cache,
        openai_embedding,
        ollama_complete,
        ollama_embedding,
    )

    chat_model = os.environ.get("VIDEORAG_CHAT_MODEL", "")
    embed_model = os.environ.get("VIDEORAG_EMBED_MODEL", "")
    embed_dim = int(os.environ.get("VIDEORAG_EMBED_DIM", "0") or 0)

    ollama_chat, ollama_embed = _pick_ollama()
    use_ollama = prefer == "ollama" or (prefer == "auto" and bool(ollama_chat and ollama_embed))

    if use_ollama:
        os.environ.setdefault("OLLAMA_HOST", _OLLAMA_URL)
        chat = chat_model or ollama_chat
        embed = embed_model or ollama_embed
        dim = embed_dim or 768
        raw_chat, raw_embed = ollama_complete, ollama_embedding
        route = f"ollama {_OLLAMA_URL}"
    else:
        key = os.environ.get("OPENAI_API_KEY") or _keys().get("freellmapi_api_key") or ""
        if not key:
            print("No LLM key: set OPENAI_API_KEY or config/api_keys.json:freellmapi_api_key",
                  file=sys.stderr)
            raise SystemExit(2)
        os.environ["OPENAI_API_KEY"] = key
        os.environ.setdefault("OPENAI_BASE_URL", _FREELLM_URL)
        chat = chat_model or "auto"
        embed = embed_model or "text-embedding-3-small"
        dim = embed_dim or 1536
        raw_chat, raw_embed = openai_complete_if_cache, openai_embedding
        route = f"openai {os.environ['OPENAI_BASE_URL']}"

    cfg = LLMConfig(
        embedding_func_raw=raw_embed,
        embedding_model_name=embed,
        embedding_dim=dim,
        embedding_max_token_size=8192,
        embedding_batch_num=1 if use_ollama else 32,
        embedding_func_max_async=1 if use_ollama else 8,
        query_better_than_threshold=0.2,
        best_model_func_raw=raw_chat,
        best_model_name=chat,
        best_model_max_token_size=16384 if use_ollama else 32768,
        best_model_max_async=1 if use_ollama else 4,
        cheap_model_func_raw=raw_chat,
        cheap_model_name=chat,
        cheap_model_max_token_size=16384 if use_ollama else 32768,
        cheap_model_max_async=1 if use_ollama else 4,
    )
    return cfg, {"route": route, "chat_model": chat, "embed_model": embed, "embed_dim": dim}


# ─────────────────────────────────────────────────────────── commands
def cmd_status(args) -> int:
    status = collect_status()
    status["llm"] = {
        "chat_model": os.environ.get("VIDEORAG_CHAT_MODEL", ""),
        "embed_model": os.environ.get("VIDEORAG_EMBED_MODEL", ""),
        "note": "run `query`/`index` to pin the route actually used",
    }
    if args.json:
        print(json.dumps(status, indent=2))
        return 0 if status["ready"] else 2

    print(f"upstream   : {status['upstream']}")
    print(f"venv       : {status['venv_python']} ({'ok' if status['venv_installed'] else 'MISSING'})")
    gpu = status["gpu"]
    if gpu["available"]:
        for i, d in enumerate(gpu["devices"]):
            print(f"gpu[{i}]     : {d['name']} · {d['vram_gb']} GB")
    else:
        print(f"gpu        : none visible ({gpu['error'] or 'torch ' + gpu['torch'] + ' cpu build'})")
    for name, w in status["weights"].items():
        print(f"{name:11s}: {'ok' if w['present'] else 'MISSING'} "
              f"{w['gb']:5.2f} GB  {w['path']}")
    b = status["brain"]
    print(f"brain      : {b['base_url']} · chat={b['chat_model']} · embed={b['embed_model']}"
          f" · key={'yes' if b['key'] else 'no'}")
    print(f"workdir    : {status['workdir']}")
    print(f"indexed    : {len(status['indexed_videos'])} video(s)"
          + (f" — {', '.join(status['indexed_videos'])}" if status["indexed_videos"] else ""))
    if status["ready"]:
        print("status     : ready")
        return 0
    print("blockers   :")
    for p in status["blockers"]:
        print(f"  - {p}")
    return 2


def _bootstrap():
    """chdir upstream (hardcoded model paths) and make relative paths sane."""
    if not UPSTREAM.exists():
        print(f"VideoRAG source not found at {UPSTREAM}", file=sys.stderr)
        raise SystemExit(2)
    os.chdir(UPSTREAM)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


def _make_rag(cfg, workdir: Path):
    from videorag import VideoRAG

    workdir.mkdir(parents=True, exist_ok=True)
    return VideoRAG(llm=cfg, working_dir=str(workdir))


def cmd_index(args) -> int:
    status = collect_status()
    _require_ready(status)
    videos = [str(Path(v).expanduser().resolve()) for v in args.videos]
    missing = [v for v in videos if not Path(v).is_file()]
    if missing:
        print("Not a file: " + ", ".join(missing), file=sys.stderr)
        return 1

    _bootstrap()
    cfg, route = build_llm(args.brain)
    print(f"brain: {route['route']} chat={route['chat_model']} embed={route['embed_model']}",
          file=sys.stderr)
    workdir = Path(args.workdir).expanduser().resolve()

    rag = _make_rag(cfg, workdir)
    rag.insert_video(video_path_list=videos)
    print(json.dumps({"ok": True, "workdir": str(workdir), "videos": videos}, indent=2))
    return 0


def cmd_query(args) -> int:
    status = collect_status()
    _require_ready(status)
    if not status["indexed_videos"]:
        print(f"Nothing indexed in {status['workdir']} — run `index` first.", file=sys.stderr)
        return 2

    _bootstrap()
    from videorag import QueryParam

    cfg, route = build_llm(args.brain)
    print(f"brain: {route['route']} chat={route['chat_model']} embed={route['embed_model']}",
          file=sys.stderr)
    workdir = Path(args.workdir).expanduser().resolve()

    rag = _make_rag(cfg, workdir)
    rag.load_caption_model(debug=False)
    param = QueryParam(mode="videorag", top_k=args.top_k)
    param.wo_reference = not args.references
    answer = rag.query(query=args.question, param=param)
    print(answer)
    return 0


def cmd_list(args) -> int:
    status = collect_status()
    payload = {"workdir": status["workdir"], "videos": status["indexed_videos"],
               "weights": status["weights"]}
    print(json.dumps(payload, indent=2))
    return 0


# ─────────────────────────────────────────────────────────── entry
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="VideoRAG runner for JARVIS")
    ap.add_argument("--workdir", default=str(WORKDIR), help="index storage directory")
    ap.add_argument("--brain", default=os.environ.get("VIDEORAG_BRAIN", "auto"),
                    choices=("auto", "ollama", "openai"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="what is installed / what is missing")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_index = sub.add_parser("index", help="index one or more videos")
    p_index.add_argument("videos", nargs="+")
    p_index.set_defaults(func=cmd_index)

    p_query = sub.add_parser("query", help="ask a question about indexed videos")
    p_query.add_argument("question")
    p_query.add_argument("--top-k", type=int, default=20)
    p_query.add_argument("--references", action="store_true",
                         help="append retrieved clip references")
    p_query.set_defaults(func=cmd_query)

    p_list = sub.add_parser("list", help="videos in the workdir")
    p_list.set_defaults(func=cmd_list)

    args = ap.parse_args(argv)
    if args.cmd in ("index", "query"):
        try:
            import multiprocessing

            multiprocessing.set_start_method("spawn", force=True)
        except RuntimeError:
            pass
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
