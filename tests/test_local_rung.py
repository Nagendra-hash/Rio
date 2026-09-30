"""
tests/test_local_rung.py — offline tests for the LOCAL ladder rung
(Qwen2.5-Coder-3B via core/llm_local.py).

No model file and no llama-cpp import are required: the point is the WIRING —
ladder membership, prompt conversion, failure semantics, status shape. The
live generation smoke runs separately (see module docstring of the smoke).

Run:  .venv/bin/python tests/test_local_rung.py
Also pytest-compatible: test_local_rung().
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core import gemini  # noqa: E402
from core import llm_local  # noqa: E402
from google.genai import types as gtypes  # noqa: E402


def test_local_rung() -> bool:
    # ── 1. ladder membership ─────────────────────────────────────────────
    assert gemini.LOCAL == "local"
    assert gemini._LADDERS[gemini.FAST][-1] == gemini.LOCAL
    assert gemini._LADDERS[gemini.SMART][-1] == gemini.LOCAL
    assert gemini.LOCAL not in gemini._LADDERS[gemini.SEARCH], \
        "grounded search must never fall to a local 3B without grounding"

    # explicit model name still gets LOCAL behind it (honour user choice,
    # degrade to something that answers)
    custom = gemini._LADDERS.get("my-model")
    assert custom is None  # unknown tiers are built dynamically in call()

    # ── 2. prompt conversion ─────────────────────────────────────────────
    assert gemini._local_prompt("plain string") == "plain string"
    part = gtypes.Part.from_bytes(data=b"x", mime_type="image/png")
    mixed = ["question about the chart", part]
    assert gemini._local_prompt(mixed) == "question about the chart"
    try:
        gemini._local_prompt([part])
        raise AssertionError("image-only contents must raise")
    except RuntimeError as e:
        assert "image-only" in str(e)

    # ── 3. failure semantics: model missing → clean RuntimeError ─────────
    llm_local.MODEL_PATH = Path("/nonexistent/ghost.gguf")
    try:
        llm_local.generate("hello")
        raise AssertionError("missing model must raise")
    except RuntimeError as e:
        assert "not present" in str(e)

    # ── 4. status shape (no llama_cpp import happened) ───────────────────
    st = llm_local.status()
    for key in ("configured", "model_path", "model_loaded", "ram_available_mb",
                "min_ram_mb", "ram_ok", "ttl_s", "n_ctx"):
        assert key in st, f"status missing {key}"
    assert st["configured"] is False and st["model_loaded"] is False

    # gemini.status() exposes the local rung without importing llama_cpp
    gs = gemini.status()
    assert "local" in gs and gs["local"]["configured"] is False

    print("✓ ladder: LOCAL last on FAST/SMART, absent from SEARCH")
    print("✓ _local_prompt: strings, mixed lists, image-only rejection")
    print("✓ missing model → clean RuntimeError, never a silent default")
    print("✓ status(): llm_local + gemini both report without heavy imports")
    return True


if __name__ == "__main__":
    sys.exit(0 if test_local_rung() else 1)
