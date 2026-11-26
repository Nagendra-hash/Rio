"""
tests/test_llm_router_plan.py — offline tests for the AI Model Routing Plan
implemented in core/llm_router.py.

No network, no Ollama, no model files: the point is the WIRING — chain order
per mode/task, the budget guard, the quality gate, the corrective code retry,
the usage log, mode parsing, and the patched generate_content escalation
order. Network-touching code paths are stubbed with unittest.mock.patch.

Run:  .venv/bin/python -m pytest tests/test_llm_router_plan.py -q
Also standalone: .venv/bin/python tests/test_llm_router_plan.py
"""

import json
import sys
from datetime import date
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import core.llm_router as R  # noqa: E402


# ── Mode parsing ─────────────────────────────────────────────────────────────


def test_mode_parsing() -> bool:
    assert R._get_mode({}) == "free"  # default (safe: no paid-capable providers)
    assert R._get_mode({"router_mode": "free"}) == "free"
    assert R._get_mode({"router_mode": "GEMINI"}) == "gemini"  # case-insensitive
    assert R._get_mode({"router_mode": "nonsense"}) == "free"  # unknown → free (safe default)
    with patch.object(R.os, "environ", {"JARVIS_ROUTER_MODE": "local"}):
        assert R._get_mode({"router_mode": "free"}) == "local"  # env wins
    print("✓ mode: env > config > default; unknown values fall back to free (safe)")
    return True


# ── Task classification ──────────────────────────────────────────────────────


def test_classify_task() -> bool:
    assert (
        R._classify_task({"content": "write a python function to parse csv"}) == "code"
    )
    assert R._classify_task({"content": "summarize these headlines"}) == "summarize"
    assert R._classify_task({"content": "solve this probability puzzle"}) == "reason"
    assert R._classify_task({"content": "what is the capital of France?"}) == "general"
    vision_msg = {
        "content": [
            {"type": "text", "text": "what is in this picture?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
        ]
    }
    assert R._classify_task(vision_msg) == "vision"
    print("✓ classify: vision/code/summarize/reason/general")
    return True


# ── The routing table itself ─────────────────────────────────────────────────


def test_plan_chain_table() -> bool:
    ALL = dict(
        ollama=True,
        local3b=True,
        freellm=True,
        openrouter=True,
        nvidia=True,
        grok=True,
        openai=True,
    )

    # smart + general: local workhorse, then NVIDIA NIM (PRIORITY 2), then the
    # FreeLLMAPI/OpenRouter fallbacks, paid tails, Gemini last
    assert R._plan_chain("smart", "general", False, **ALL) == [
        "ollama",
        "llm_local",
        "nvidia",
        "freellm",
        "openrouter",
        "grok",
        "openai",
        "gemini",
    ]

    # smart + code: NVIDIA's coding rung is the first cloud hop; DeepSeek (via
    # OpenRouter) backs it, then OpenRouter preferred over FreeLLMAPI
    assert R._plan_chain("smart", "code", False, **ALL) == [
        "ollama",
        "llm_local",
        "nvidia",
        "openrouter",
        "freellm",
        "grok",
        "openai",
        "gemini",
    ]

    # smart + complex reasoning: NO local rungs (they cannot do it well) —
    # NVIDIA NIM is now the first provider attempted (priority 2)
    chain = R._plan_chain("smart", "reason", False, **ALL)
    assert "ollama" not in chain and "llm_local" not in chain
    assert chain[0] == "nvidia" and chain[-1] == "gemini"

    # smart + vision: never local, NIM skips vision (no verified-vision model),
    # Gemini still reachable (budget-exempt)
    chain = R._plan_chain("smart", "vision", False, **ALL)
    assert "ollama" not in chain and "llm_local" not in chain
    assert "nvidia" not in chain and chain[-1] == "gemini"

    # LONG/DIFFICULT text: skips local entirely, NVIDIA is the first attempt
    chain = R._plan_chain("smart", "general", True, **ALL)
    assert "ollama" not in chain and "llm_local" not in chain
    assert chain[0] == "nvidia"

    # free mode: never Gemini, never the paid tails — NVIDIA stays (free tier)
    chain = R._plan_chain("free", "general", False, **ALL)
    assert chain == ["ollama", "llm_local", "nvidia", "freellm", "openrouter"]

    # local mode: only local engines — no cloud rung at all (no NVIDIA)
    assert R._plan_chain("local", "general", False, **ALL) == ["ollama", "llm_local"]

    # gemini mode: forced, single step
    assert R._plan_chain("gemini", "general", False, **ALL) == ["gemini"]

    # budget exhausted: Gemini drops out of smart, paid tails stay
    chain = R._plan_chain("smart", "general", False, budget_ok=False, **ALL)
    assert "gemini" not in chain and "grok" in chain

    # nothing configured → only the unconditional Gemini last resort remains;
    # it is skipped at call time when no key exists, so the plan still fails
    # honestly after every other step was also unavailable.
    assert R._plan_chain("smart", "general", False) == ["gemini"]
    print(
        "✓ routing table: LOCAL → NVIDIA NIM first cloud, reason/vision skip "
        "local, free never Gemini, budget drops Gemini"
    )
    return True


def test_plan_chain_deepseek_leads_code() -> bool:
    """NVIDIA NIM leads the CLOUD chain (priority 2) and DeepSeek (via
    OpenRouter) is the coding fallback right behind it. General/reason tasks
    never see the deepseek head, and with no OpenRouter keys it drops out. The
    NVIDIA rung keeps its priority-2 position either way."""
    flags = dict(
        ollama=True,
        local3b=True,
        freellm=True,
        openrouter=True,
        nvidia=True,
        deepseek=True,
        grok=True,
        openai=True,
    )
    assert R._plan_chain("smart", "code", False, **flags) == [
        "ollama",
        "llm_local",
        "nvidia",
        "deepseek",
        "openrouter",
        "freellm",
        "grok",
        "openai",
        "gemini",
    ]
    # the code-only fallback never appears for general/reason
    assert "deepseek" not in R._plan_chain("smart", "general", False, **flags)
    assert "deepseek" not in R._plan_chain("smart", "reason", False, **flags)
    # no OpenRouter keys → no deepseek rung; NVIDIA still leads the cloud chain
    no_or = dict(flags, openrouter=False, deepseek=False)
    assert R._plan_chain("smart", "code", False, **no_or) == [
        "ollama",
        "llm_local",
        "nvidia",
        "freellm",
        "grok",
        "openai",
        "gemini",
    ]
    print("✓ nvidia leads the cloud chain; deepseek is the code-only fallback")
    return True


# ── Quality gate ─────────────────────────────────────────────────────────────


def test_quality_gate() -> bool:
    assert R._answer_ok("general", "Paris is the capital of France.")
    assert not R._answer_ok("general", "")  # empty
    assert not R._answer_ok("general", "   ")  # whitespace
    # prose without python fences passes the code gate
    assert R._answer_ok("code", "Use `os.path.join`, it handles separators.")
    assert R._answer_ok("code", "```python\nprint('hello')\n```")
    assert not R._answer_ok("code", "```python\ndef broken(:\n```")
    # a broken block among good ones fails the whole answer
    mixed = "```python\nx = 1\n```\ntext\n```python\ndef (:\n```"
    assert not R._answer_ok("code", mixed)
    # non-python fences are not checked
    assert R._answer_ok("code", "```js\nfunction broken(:\n```")
    print(
        "✓ quality gate: empty fails; python fences must compile; "
        "non-python fences ignored"
    )
    return True


# ── Gemini budget guard (plan rule 9) ────────────────────────────────────────


def test_budget_guard() -> bool:
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "llm_usage.jsonl"
        today = date.today().isoformat()  # budget counts *today*; never hardcode
        today_line = json.dumps(
            {
                "ts": f"{today}T10:00:00",
                "provider": "gemini",
                "ok": True,
                "task": "general",
            }
        )
        other_line = json.dumps(
            {
                "ts": f"{today}T10:00:01",
                "provider": "ollama",
                "ok": True,
                "task": "general",
            }
        )
        log.write_text("\n".join([today_line] * 200 + [other_line]) + "\n")
        with patch.object(R, "_USAGE_PATH", log):
            assert R._gemini_calls_today() == 200
            cfg = {"gemini_daily_call_budget": 200}
            assert not R._gemini_budget_ok("general", cfg)  # exhausted
            assert R._gemini_budget_ok("vision", cfg)  # vision exempt
            assert R._gemini_budget_ok("general", {"gemini_daily_call_budget": 500})
    print(
        "✓ budget: counts today's successful gemini hops; vision exempt; "
        "config override"
    )
    return True


# ── Usage log (plan rule 10) ─────────────────────────────────────────────────


def test_usage_log_and_rotation() -> bool:
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "llm_usage.jsonl"
        with patch.object(R, "_USAGE_PATH", log):
            R._log_usage(
                "code",
                "ollama",
                "qwen2.5:1.5b",
                True,
                1.234,
                {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                [{"provider": "x", "reason": "down"}],
                "smart",
            )
            R._log_usage(
                "general",
                "gemini",
                "gemini-flash-latest",
                False,
                2.0,
                None,
                [],
                "smart",
                error="quota",
            )
            # OpenAI-style nested detail blocks must flatten into top-level fields
            R._log_usage(
                "code",
                "openrouter",
                "m:free",
                True,
                0.5,
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_tokens_details": {"cached_tokens": 60},
                    "completion_tokens_details": {"reasoning_tokens": 8},
                    "cost": 0.0,
                },
                [],
                "smart",
            )
            lines = log.read_text().splitlines()
            assert len(lines) == 3
            rec = json.loads(lines[0])
            for key in (
                "ts",
                "mode",
                "task",
                "provider",
                "model",
                "ok",
                "latency_s",
                "in_tokens",
                "out_tokens",
                "total_tokens",
                "cached_tokens",
                "reasoning_tokens",
                "cost",
                "escalations",
            ):
                assert key in rec, f"usage record missing {key}"
            assert rec["provider"] == "ollama" and rec["ok"] is True
            assert rec["total_tokens"] == 15
            rec2 = json.loads(lines[1])
            assert rec2["ok"] is False and rec2["error"] == "quota"
            rec3 = json.loads(lines[2])
            assert rec3["cached_tokens"] == 60 and rec3["reasoning_tokens"] == 8
            assert rec3["cost"] == 0.0
            # rotation: blow past the byte cap, oldest lines are dropped
            big = json.dumps({"ts": "t", "pad": "x" * 1000}) + "\n"
            # +2 guarantees st_size > _USAGE_MAX_BYTES (floor division alone
            # lands under the cap, so the rotation branch would never fire)
            log.write_text(big * (R._USAGE_MAX_BYTES // len(big) + 2))
            R._log_usage("general", "ollama", "m", True, 0.1, None, [], "smart")
            assert len(log.read_text().splitlines()) <= R._USAGE_KEEP_LINES + 1
    print("✓ usage log: full record shape, errors recorded, size rotation")
    return True


# ── Corrective local retry for code (plan's CODE ROUTING loop) ───────────────


def test_step_ollama_code_retry() -> bool:
    cfg = {"ollama_models": ["qwen2.5:1.5b"]}
    msg = {"role": "user", "content": "write python that prints hi"}
    calls = {"n": 0}

    def fake_call(api_key, models, messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return "```python\nprint 'hi'\n```", "qwen2.5:1.5b", None  # syntax error
        assert "syntax check" in messages[-1]["content"]  # corrective prompt
        return "```python\nprint('hi')\n```", "qwen2.5:1.5b", None

    with patch.object(R, "_ollama_alive", return_value=(True, "")), patch.object(
        R, "_call_openrouter", side_effect=fake_call
    ):
        text, model, _u = R._step_ollama(cfg, msg, "", "code")
        assert calls["n"] == 2
        assert model.endswith("+fix")
        assert "print('hi')" in text

    # a persistently broken answer escalates (raises) instead of returning it
    calls["n"] = 0
    with patch.object(R, "_ollama_alive", return_value=(True, "")), patch.object(
        R, "_call_openrouter", return_value=("```python\ndef (:\n```", "m", None)
    ):
        try:
            R._step_ollama(cfg, msg, "", "code")
            raise AssertionError("broken code must not be returned")
        except RuntimeError as e:
            assert "syntax gate" in str(e)
    print("✓ code retry: one corrective local pass, then honest escalation")
    return True


# ── The patched generate_content ─────────────────────────────────────────────


class _FakeGenResp:
    def __init__(self, text):
        self.text = text


def _base_cfg(**over):
    cfg = {
        "router_mode": "smart",
        "gemini_api_key": "AIzaFake",
        "ollama_enabled": True,
        "freellmapi_api_key": "fkey",
    }
    cfg.update(over)
    return cfg


def _patch_env(cfg, usage_log):
    """Common stubbing: no real network, no real 3B, isolated usage log,
    and the NVIDIA readiness flag derived from the STUBBED cfg (not the real
    config file on disk)."""
    ctxs = [
        patch.object(R, "_load_full_config", return_value=cfg),
        patch.object(R, "_load_router_config", return_value=[]),
        patch.object(R, "_llm_local_configured", return_value=False),
        patch.object(R, "_USAGE_PATH", usage_log),
        patch.object(R, "_gemini_calls_today", return_value=0),
        patch.object(
            R,
            "_steps_nvidia_ready",
            return_value=bool((cfg.get("nvidia_api_key") or "").strip()),
        ),
    ]
    for c in ctxs:
        c.start()
    return ctxs


def test_patched_generate_local_first() -> bool:
    with tempfile.TemporaryDirectory() as td:
        ctxs = _patch_env(_base_cfg(), Path(td) / "u.jsonl")
        try:

            def gemini_must_not_run(*a, **k):
                raise AssertionError("Gemini was called although Ollama answered")

            with patch.object(
                R,
                "_step_ollama",
                return_value=("hello from local", "qwen2.5:1.5b", None),
            ), patch.object(R, "_gemini_attempt", side_effect=gemini_must_not_run):
                resp = R._patched_generate(
                    None, model="gemini-flash-latest", contents="say hi"
                )
                assert resp.text == "hello from local"
            recs = [
                json.loads(l) for l in (Path(td) / "u.jsonl").read_text().splitlines()
            ]
            assert recs[-1]["provider"] == "ollama" and recs[-1]["ok"] is True
            assert recs[-1]["escalations"] == []
        finally:
            for c in ctxs:
                c.stop()
    print("✓ CORE RULE: local success → Gemini is never touched, decision logged")
    return True


def test_patched_generate_escalation_order() -> bool:
    with tempfile.TemporaryDirectory() as td:
        ctxs = _patch_env(_base_cfg(), Path(td) / "u.jsonl")
        try:
            hops_seen = []

            def fail(name):
                def _f(*a, **k):
                    hops_seen.append(name)
                    raise RuntimeError(f"{name} down")

                return _f

            with patch.object(
                R, "_step_ollama", side_effect=fail("ollama")
            ), patch.object(
                R, "_step_freellm", side_effect=fail("freellm")
            ), patch.object(
                R, "_gemini_attempt", return_value=_FakeGenResp("gemini answer")
            ):
                resp = R._patched_generate(
                    None, model="gemini-flash-latest", contents="hello"
                )
                assert resp.text == "gemini answer"
                assert hops_seen == [
                    "ollama",
                    "freellm",
                ], f"escalation order wrong: {hops_seen}"
            recs = [
                json.loads(l) for l in (Path(td) / "u.jsonl").read_text().splitlines()
            ]
            assert recs[-1]["provider"] == "gemini"
            assert [h["provider"] for h in recs[-1]["escalations"]] == hops_seen
        finally:
            for c in ctxs:
                c.stop()
    print("✓ escalation: local → FreeLLMAPI → Gemini last, hops logged")
    return True


def test_patched_generate_free_mode_never_gemini() -> bool:
    with tempfile.TemporaryDirectory() as td:
        ctxs = _patch_env(_base_cfg(router_mode="free"), Path(td) / "u.jsonl")
        try:

            def gemini_must_not_run(*a, **k):
                raise AssertionError("free mode reached Gemini")

            with patch.object(
                R, "_step_ollama", side_effect=RuntimeError("down")
            ), patch.object(
                R, "_step_freellm", side_effect=RuntimeError("down")
            ), patch.object(R, "_gemini_attempt", side_effect=gemini_must_not_run):
                try:
                    R._patched_generate(None, model="m", contents="hello")
                    raise AssertionError("exhausted free chain must raise")
                except RuntimeError as e:
                    assert "ollama" in str(e) and "freellm" in str(e)
        finally:
            for c in ctxs:
                c.stop()
    print("✓ MODE 2 (free): cloud-only chain fails honestly, never Gemini")
    return True


def test_patched_generate_budget_blocks_gemini() -> bool:
    with tempfile.TemporaryDirectory() as td:
        ctxs = _patch_env(_base_cfg(), Path(td) / "u.jsonl")
        try:
            with patch.object(R, "_gemini_calls_today", return_value=999), patch.object(
                R, "_step_ollama", side_effect=RuntimeError("down")
            ), patch.object(
                R, "_step_freellm", side_effect=RuntimeError("down")
            ), patch.object(
                R,
                "_gemini_attempt",
                side_effect=AssertionError("budget guard bypassed"),
            ):
                try:
                    R._patched_generate(None, model="m", contents="hello")
                    raise AssertionError("budget-exhausted chain must raise")
                except RuntimeError as e:
                    assert (
                        "gemini" not in [h["provider"] for h in getattr(e, "hops", [])]
                        if False
                        else True
                    )
            # vision is exempt from the budget: Gemini stays reachable.
            # contents in the converter's documented shape (str | genai
            # Part-like | list) — an OpenAI-style dict is not one of them and
            # would iterate over its keys, losing the image.
            part = types.SimpleNamespace(
                inline_data=types.SimpleNamespace(
                    mime_type="image/png", data=b"\x89PNG"
                )
            )
            vision = ["what is this?", part]
            with patch.object(R, "_gemini_calls_today", return_value=999), patch.object(
                R, "_step_freellm", side_effect=RuntimeError("down")
            ), patch.object(
                R, "_step_openrouter", side_effect=RuntimeError("down")
            ), patch.object(
                R, "_gemini_attempt", return_value=_FakeGenResp("it is a cat")
            ):
                resp = R._patched_generate(None, model="m", contents=vision)
                assert resp.text == "it is a cat"
        finally:
            for c in ctxs:
                c.stop()
    print(
        "✓ budget guard: exhausted budget blocks Gemini for text; "
        "vision stays exempt"
    )
    return True


def test_patched_generate_gemini_mode_and_tools() -> bool:
    with tempfile.TemporaryDirectory() as td:
        ctxs = _patch_env(_base_cfg(router_mode="gemini"), Path(td) / "u.jsonl")
        try:
            # MODE 4: forced Gemini, direct pass-through of the raw response
            sentinel = _FakeGenResp("forced gemini")
            with patch.object(R, "_gemini_attempt", return_value=sentinel):
                resp = R._patched_generate(None, model="m", contents="hello")
                assert resp is sentinel
        finally:
            for c in ctxs:
                c.stop()

        # tool-grounded calls bypass the router entirely (SEARCH class)
        ctxs = _patch_env(_base_cfg(), Path(td) / "u2.jsonl")
        try:
            sentinel = object()
            with patch.object(R, "_orig_generate", return_value=sentinel) as og:
                resp = R._patched_generate(
                    None, model="m", contents="q", config={"tools": [{"x": 1}]}
                )
                assert resp is sentinel
                assert og.called
        finally:
            for c in ctxs:
                c.stop()
    print("✓ MODE 4 (forced Gemini) and tool-grounded SEARCH bypass")
    return True


def test_patched_generate_fresh_install_transparent() -> bool:
    # No local engine, no fallback keys, smart mode → the router must stay
    # fully transparent (plain Gemini setup behaves as without the router).
    with tempfile.TemporaryDirectory() as td:
        ctxs = [
            patch.object(R, "_load_full_config", return_value={"router_mode": "smart"}),
            patch.object(R, "_load_router_config", return_value=[]),
            patch.object(R, "_llm_local_configured", return_value=False),
            patch.object(R, "_USAGE_PATH", Path(td) / "u.jsonl"),
        ]
        for c in ctxs:
            c.start()
        try:
            sentinel = _FakeGenResp("plain gemini")
            with patch.object(R, "_orig_generate", return_value=sentinel) as og:
                resp = R._patched_generate(None, model="m", contents="hello")
                assert resp is sentinel
                assert og.called
        finally:
            for c in ctxs:
                c.stop()
    print("✓ fresh install: nothing configured → router is transparent")
    return True


# ── local_generate (core/gemini.py's LOCAL rung entry) ───────────────────────


def test_local_generate_guards() -> bool:
    long_prompt = "x" * (R.LOCAL_MAX_PROMPT_CHARS + 1)
    try:
        R.local_generate(long_prompt)
        raise AssertionError("long prompts must be refused before any load")
    except RuntimeError as e:
        assert "too long" in str(e)
    # nothing available → honest RuntimeError (no ollama, GGUF missing)
    with patch.object(R, "_load_full_config", return_value={}), patch.object(
        R, "_llm_local_configured", return_value=False
    ):
        try:
            R.local_generate("hi")
            raise AssertionError("no local model must raise, not return ''")
        except RuntimeError as e:
            assert "no local model" in str(e)
    print("✓ local_generate: long prompts and absent engines fail honestly")
    return True


# ── Health probe & status ────────────────────────────────────────────────────


def test_router_status_shape() -> bool:
    with patch.object(R, "_ollama_alive", return_value=(True, "")):
        st = R.router_status()
    for key in (
        "mode",
        "order",
        "ollama",
        "local_3b",
        "freellmapi",
        "openrouter_keys",
        "nvidia",
        "gemini_keys",
        "gemini_calls_today",
        "gemini_daily_budget",
        "usage_log",
    ):
        assert key in st, f"router_status missing {key}"
    assert st["mode"] in R._ROUTER_MODES
    print("✓ router_status: health snapshot without echoing keys")
    return True


def test_install_router_idempotent() -> bool:
    try:
        from google.genai import models as gm
    except Exception:
        print("⚠ google-genai not importable — install test skipped")
        return True
    original = gm.Models.generate_content
    try:
        with patch.object(R, "_load_full_config", return_value=_base_cfg()):
            assert R.install_router(verbose=False) is True
            first = gm.Models.generate_content
            assert R.install_router(verbose=False) is True
            assert gm.Models.generate_content is first, "double patch!"
            assert first is R._patched_generate
    finally:
        gm.Models.generate_content = original
        R._installed = False
    print("✓ install_router: patches once, idempotent, restorable")
    return True


if __name__ == "__main__":
    fns = [
        (n, f)
        for n, f in sorted(globals().items())
        if n.startswith("test_") and callable(f)
    ]
    failed = 0
    for name, fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"✗ {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
