"""
video_rag.py — bridge action: JARVIS asks questions about long videos.

VideoRAG (github.com/HKUDS/VideoRAG) indexes hours of footage per collection:
each segment gets an ASR transcript, a MiniCPM-V caption, and an ImageBind
visual embedding, and the lot is folded into a dual knowledge graph. A
question then retrieves across *all* indexed videos instead of stuffing a
context window — the point of the framework for 60+ hour material.

This action shells out to scripts/videorag_run.py, which runs the framework
in its own venv (.venv-videorag) because VideoRAG pins torch 2.1.2 /
transformers 4.37.1 / numpy 1.26.4 — a stack JARVIS must not inherit. The
heavy lifting (split → ASR → caption → embed → graph) is GPU-only upstream:
ImageBind is loaded with a hardcoded .cuda(), so the runner reports exactly
what is missing rather than failing mid-job. `status` is always safe.

Costs: no provider quota on the default route — the brain is ollama if it is
running (embeddings included), otherwise the FreeLLMAPI pool. Indexing is the
expensive half: minutes to hours per video, depending on length.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TOOLS = Path(os.environ.get("VIDEORAG_HOME", "/home/nagendra/JarvisTools/videorag"))
_PYTHON = str(Path(os.environ.get("VIDEORAG_PYTHON",
                                  _TOOLS / ".venv" / "bin" / "python")))
_SCRIPT = _ROOT / "scripts" / "videorag_run.py"
_UPSTREAM = str(Path(os.environ.get("VIDEORAG_ROOT", _TOOLS / "VideoRAG-algorithm")))
_TIMEOUT = {"status": 180, "list": 180, "ask": 1500, "index": 21600}


def _run(extra: list[str], timeout: int) -> tuple[int, str, str]:
    proc = subprocess.run(
        [_PYTHON, str(_SCRIPT), *extra],
        capture_output=True, text=True, timeout=timeout,
        env={"HOME": str(Path.home()),
             "PATH": "/usr/local/bin:/usr/bin:/bin",
             "VIDEORAG_ROOT": _UPSTREAM},
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _blocked_reason(text: str) -> str:
    """Turn the runner's preflight dump (stdout or stderr) into one sentence."""
    lines = [l.strip().lstrip("-").strip() for l in text.splitlines()]
    blockers = [l for l in lines
                if "CUDA" in l or "weights missing" in l or "upstream source missing" in l]
    gpu = any("CUDA" in l for l in blockers)
    weights = [l.split(" weights missing")[0] for l in blockers if "weights missing" in l]
    parts = []
    if gpu:
        parts.append("this machine has no CUDA GPU, and VideoRAG's visual encoder is CUDA-only")
    if weights:
        parts.append("the model weights are not downloaded (" + ", ".join(weights) + ")")
    return "; ".join(parts) or "the VideoRAG preflight failed"


def video_rag(parameters: dict, player=None, speak=None) -> str:
    """Ask questions about indexed videos, or index a new one."""
    parameters = parameters or {}
    action = str(parameters.get("action", "ask")).strip().lower()
    question = str(parameters.get("question", "")).strip()
    path = str(parameters.get("path", "")).strip()
    workdir = str(parameters.get("workdir", "")).strip()

    if not _PYTHON or not Path(_PYTHON).exists():
        return ("The VideoRAG environment is missing, sir — ~/JarvisTools/videorag/.venv "
                "was not built. Rebuild it with uv and the framework's pinned requirements.")
    if not _SCRIPT.exists():
        return "The VideoRAG runner is missing, sir — check scripts/videorag_run.py."

    common = ["--workdir", workdir] if workdir else []

    if action == "index":
        if not path:
            return "Which video should I index, sir?"
        video = Path(path).expanduser()
        if not video.exists():
            return f"I can't find {path}, sir."
        if player:
            player.write_log(f"[video-rag] indexing {video.name}")
        if speak:
            speak("Indexing that video now, sir. Hours of footage can take a while — "
                  "I'll report back when the knowledge graph is built.")
        code, out, err = _run([*common, "index", str(video)], _TIMEOUT["index"])
        if code == 2:
            return f"I can't build a video index on this machine yet, sir: {_blocked_reason(err)}."
        if code != 0:
            return f"The indexing run failed, sir: {(err or out).splitlines()[-1][:250]}"
        try:
            info = json.loads(out.splitlines()[-1] if out else "{}")
            return (f"Index built for {Path(info.get('videos', [str(video)])[0]).name}, sir. "
                    "Ask me anything about it.")
        except Exception:
            return "Index built, sir."

    if action == "status":
        code, out, err = _run([*common, "status"], _TIMEOUT["status"])
        if code == 0:
            return "The VideoRAG index is ready, sir:\n" + out
        return ("VideoRAG is installed but not runnable here yet, sir: "
                f"{_blocked_reason(out + chr(10) + err)}.")
    if action == "list":
        code, out, _err = _run([*common, "list"], _TIMEOUT["list"])
        if code != 0 or not out:
            return "I couldn't read the video index, sir."
        try:
            info = json.loads(out)
            videos = info.get("videos") or []
            if not videos:
                return "Nothing is indexed yet, sir."
            return "Indexed videos, sir: " + ", ".join(videos) + "."
        except Exception:
            return out[:1500]

    # ask (default)
    if not question:
        return "What should I ask the videos, sir?"
    if player:
        player.write_log(f"[video-rag] {question[:80]}")
    if speak:
        speak("Searching the video knowledge graph, sir.")
    code, out, err = _run([*common, "query", question], _TIMEOUT["ask"])
    if code == 2:
        if "Nothing indexed" in err:
            return "There's no video index yet, sir — ask me to index a video first."
        return f"I can't query videos on this machine yet, sir: {_blocked_reason(err)}."
    if code != 0:
        tail = (err or out).splitlines()
        return f"The video query failed, sir: {(tail[-1] if tail else 'unknown error')[:250]}"
    return out[:2500]


TOOL = {
    "name": "video_rag",
    "description": (
        "Answer questions from long videos using VideoRAG — an index of ASR "
        "transcripts, MiniCPM-V captions, and ImageBind visual embeddings "
        "across one or many videos, so questions can span hours of footage "
        "instead of a truncated context. action=ask (default) queries the "
        "existing index; action=index adds a video file to it first (slow: "
        "minutes to hours, GPU only); action=list shows what is indexed; "
        "action=status reports whether this machine can run it. Use for "
        "'what happens in this lecture series', 'search my indexed videos "
        "for X', or 'add this film to my video knowledge base'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "ask (default) | index | list | status."
            },
            "question": {
                "type": "STRING",
                "description": "The question to answer from the indexed videos. "
                               "Required for action=ask."
            },
            "path": {
                "type": "STRING",
                "description": "Video file to add to the index. Required for "
                               "action=index."
            },
            "workdir": {
                "type": "STRING",
                "description": "Optional index directory — use it to keep "
                               "separate collections (e.g. a lecture series "
                               "apart from a film). Defaults to memory/videorag."
            },
        },
        "required": [],
    },
    "handler": video_rag,
}
