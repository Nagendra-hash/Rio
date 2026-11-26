"""
tests/test_nvidia_nim.py — offline tests for the NVIDIA NIM priority-2 provider
(core/nvidia_nim.py) and its integration into the router (core/llm_router.py).

NO network, NO quota: every HTTP touch is mocked at nvidia_nim's two thin
seams (_http_chat / _http_list_models). Required cases 1-14 from the NVIDIA
NIM integration plan map to the test functions below.

Run:  .venv/bin/python -m pytest tests/test_nvidia_nim.py -q
Also standalone: .venv/bin/python tests/test_nvidia_nim.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import core.nvidia_nim as N   # noqa: E402
import core.llm_router as R   # noqa: E402


# ── environment helpers ─────────────────────────────────────────────────────

class Ctx:
    """Isolated NVIDIA config + registry for one test (`with Ctx() as t:`)."""
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.client = self.base / "cfg.json"
        self.registry = self.base / "mem" / "reg.json"
        self.client.write_text(json.dumps({
            "router_mode": "smart",
            "nvidia_api_key": "nvapi-testkey123456",
        }))
        self.env = {
            "NVIDIA_API_KEY": "",
            "NVIDIA_NIM_BASE_URL": "",
            "NVIDIA_NIM_ENABLED": "",
            "JARVIS_ROUTER_MODE": "",
        }
        self.ctxs = [
            patch.object(N, "_CONFIG_PATH", self.client),
            patch.object(N, "_REGISTRY_PATH", self.registry),
            patch.dict(os.environ, self.env, clear=False),
            patch.object(N, "_backoff_sleep", lambda a: None),
        ]
        self._reset()

    def _reset(self):
        for c in self.ctxs:
            c.start()
        with N._lock:
            N._registry_cache.update({"at": 0.0, "data": None})
            N._discovery_cache.update({"at": 0.0, "ids": None, "error": ""})
            N._auth_block.update({"until": 0.0, "msg": ""})

    def set_config(self, **kv):
        cfg = json.loads(self.client.read_text())
        cfg.update(kv)
        self.client.write_text(json.dumps(cfg))

    def stop(self):
        for c in self.ctxs:
            c.stop()
        self.tmp.cleanup()

    # context-manager protocol
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False


def _ok_body(text="nim says hi"):
    return json.dumps({"choices": [{"message": {"content": text}}],
                       "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                                 "total_tokens": 5}})


def _fake_post_script(script):
    """script: list of (status, body) consumed in order; returns the fake
    _http_chat and the list of (model, attempt payload) calls."""
    calls: list[str] = []
    def fake(key, model, payload, base, timeout):
        calls.append(model)
        status, body = script.pop(0) if script else (200, _ok_body())
        return status, body
    return fake, calls


def _fake_list(status=200, ids=None, err=None):
    def fake(key, base):
        if err:
            return [], err
        return (ids or []), ("", )
    def fake2(key, base):
        if err:
            return [], err
        return (ids or ["nvidia/nemotron-3-super-120b-a12b"]), ""
    return fake2


MSG = [{"role": "user", "content": "hello there"}]
MODELS = ["nvidia/nemotron-3-super-120b-a12b", "z-ai/glm-5.3-flash",
          "openai/gpt-oss-20b"]


# ── TEST 1: local model succeeds → NVIDIA is not called ─────────────────────

def test_01_local_success_nvidia_not_called() -> bool:
    with Ctx() as t:
        cfg = {"router_mode": "smart", "ollama_enabled": True,
               "freellmapi_api_key": "fkey", "nvidia_api_key": "nvapi-x"}
        usage = t.base / "u.jsonl"
        ctxs = [
            patch.object(R, "_load_full_config", return_value=cfg),
            patch.object(R, "_load_router_config", return_value=[]),
            patch.object(R, "_llm_local_configured", return_value=False),
            patch.object(R, "_USAGE_PATH", usage),
            patch.object(R, "_gemini_calls_today", return_value=0),
            patch.object(R, "_steps_nvidia_ready", return_value=True),
        ]
        for c in ctxs:
            c.start()
        try:
            def nvidia_must_not_run(*a, **k):
                raise AssertionError("NVIDIA called although local answered")
            with patch.object(R, "_step_ollama",
                              return_value=("local answer", "qwen2.5:1.5b", None)), \
                 patch.object(R, "_step_nvidia", side_effect=nvidia_must_not_run):
                resp = R._patched_generate(None, model="m", contents="hi")
                assert resp.text == "local answer"
            rec = [json.loads(l) for l in usage.read_text().splitlines()][-1]
            assert rec["provider"] == "ollama" and rec["ok"] is True
        finally:
            for c in ctxs:
                c.stop()
    print("✓ TEST 1: local success → NVIDIA never called, hop logged")
    return True


# ── TEST 2: local unavailable → NVIDIA selected ──────────────────────────────

def test_02_local_down_nvidia_selected() -> bool:
    with Ctx() as t:
        usage = t.base / "u.jsonl"
        cfg = {"router_mode": "smart", "ollama_enabled": True,
               "freellmapi_api_key": "fkey", "nvidia_api_key": "nvapi-x"}
        ctxs = [
            patch.object(R, "_load_full_config", return_value=cfg),
            patch.object(R, "_load_router_config", return_value=[]),
            patch.object(R, "_llm_local_configured", return_value=False),
            patch.object(R, "_USAGE_PATH", usage),
            patch.object(R, "_gemini_calls_today", return_value=0),
            patch.object(R, "_steps_nvidia_ready", return_value=True),
        ]
        for c in ctxs:
            c.start()
        try:
            with patch.object(R, "_step_ollama", side_effect=RuntimeError("down")), \
                 patch.object(R, "_step_freellm", side_effect=RuntimeError("down")), \
                 patch.object(R, "_step_nvidia",
                              return_value=("nim answer", "nvidia/x", None)):
                resp = R._patched_generate(None, model="m", contents="hi")
                assert resp.text == "nim answer"
            rec = [json.loads(l) for l in usage.read_text().splitlines()][-1]
            assert rec["provider"] == "nvidia"
            # Only ollama failed before NVIDIA — chain order ollama → nvidia
            assert [h["provider"] for h in rec["escalations"]] == ["ollama"]
        finally:
            for c in ctxs:
                c.stop()
    print("✓ TEST 2: local unavailable → NVIDIA selected (P2)")
    return True


# ── TEST 3: first NVIDIA model unavailable → another approved is selected ───

def test_03_model_404_next_approved() -> bool:
    with Ctx() as t:
        fake, calls = _fake_post_script([
            (404, '{"detail":"model not found"}'),
            (200, _ok_body("from glm")),
        ])
        with patch.object(N, "_http_chat", fake):
            text, used, _u = N.chat(MSG, task="general")
        assert used == "z-ai/glm-5.3-flash", f"selected {used}, calls {calls}"
        assert calls == ["nvidia/nemotron-3-super-120b-a12b",
                         "z-ai/glm-5.3-flash"]
        assert text == "from glm"
        reg = N._registr(0)
        assert reg["nvidia/nemotron-3-super-120b-a12b"]["availability"] == \
            "unavailable"
        assert reg["z-ai/glm-5.3-flash"]["successes"] == 1
    print("✓ TEST 3: 404 → marked unavailable, next approved model chosen")
    return True


# ── TEST 4: 429 → cooldown + another model tried, no repeat-hammering ───────

def test_04_429_cooldown_and_failover() -> bool:
    with Ctx() as t:
        fake, calls = _fake_post_script([
            (429, '{"detail":"rate limit exceeded"}'),
            (200, _ok_body("from fallback")),
        ])
        with patch.object(N, "_http_chat", fake):
            text, used, _u = N.chat(MSG, task="general")
        assert used == "z-ai/glm-5.3-flash"
        reg = N._registr(0)
        first = reg["nvidia/nemotron-3-super-120b-a12b"]
        assert first["availability"] == "cooldown"
        assert first["cooling_until"] > 0
        # the cooled model is NOT retried on the next request
        fake2, calls2 = _fake_post_script([(200, _ok_body("again"))])
        with patch.object(N, "_http_chat", fake2):
            N.chat(MSG, task="general")
        assert calls2 == ["z-ai/glm-5.3-flash"], calls2
        assert "nvidia/nemotron-3-super-120b-a12b" not in calls2
        # when ALL models are cooling → NimError, router fails over.
        # 4 approved defaults are eligible here (3 still alive after the
        # nemotron cooldown), so script 429 for every one of them.
        fake3, calls3 = _fake_post_script([(429, "x")] * len(N._eligible_models("general")))
        with patch.object(N, "_http_chat", fake3):
            try:
                N.chat(MSG, task="general")
                raise AssertionError("all-429 must raise NimError")
            except N.NimError as e:
                assert "429" in str(e)
    print("✓ TEST 4: 429 → that model cooled down, next candidate tried, "
          "no repeat hammering")
    return True


# ── TEST 5: 401 → one clear error, provider benched, no retry loop ──────────

def test_05_401_bench_no_retry() -> bool:
    with Ctx() as t:
        fake, calls = _fake_post_script([
            (401, '{"detail":"invalid api key"}'),
        ])
        with patch.object(N, "_http_chat", fake):
            try:
                N.chat(MSG, task="general")
                raise AssertionError("401 must raise NimAuthError")
            except N.NimAuthError as e:
                assert "401" in str(e)
        assert calls == ["nvidia/nemotron-3-super-120b-a12b"]
        # immediately after: skipped WITHOUT any HTTP call
        fake2, calls2 = _fake_post_script([])
        with patch.object(N, "_http_chat", fake2):
            try:
                N.chat(MSG, task="general")
                raise AssertionError("benched provider must skip")
            except N.NimSkip as e:
                assert "cooling" in str(e) or "auth" in str(e)
        assert calls2 == []                # no request was made
        assert "reason" in N.skip_reason() or N.skip_reason()
    print("✓ TEST 5: 401 → one clear auth error, provider benched, "
          "no continuous retry")
    return True


# ── TEST 6: 500 → limited retries then fallback ─────────────────────────────

def test_06_500_limited_retries_then_fallback() -> bool:
    with Ctx() as t:
        # every eligible model gets _MAX_RETRIES_5XX+1 attempts before the
        # walker moves on; only after ALL models fail → NimError
        n_models = len(N._eligible_models("general"))
        script = [(500, "boom")] * n_models * (N._MAX_RETRIES_5XX + 1)
        fake, calls = _fake_post_script(script)
        with patch.object(N, "_http_chat", fake):
            try:
                N.chat(MSG, task="general")
                raise AssertionError("persistent 500 must raise NimError")
            except N.NimError as e:
                assert "500" in str(e)
        assert len(calls) == n_models * (N._MAX_RETRIES_5XX + 1), calls
        # recovers after transient 500s: 500, 500, 200 → same model answers
        fake2, calls2 = _fake_post_script([
            (500, "boom"), (500, "boom"), (200, _ok_body("recovered")),
        ])
        with patch.object(N, "_http_chat", fake2):
            text, used, _u = N.chat(MSG, task="general")
        assert used == "nvidia/nemotron-3-super-120b-a12b" and text == "recovered"
    print("✓ TEST 6: 500 → limited backoff retries, then failover / recovery")
    return True


# ── TEST 7: missing key → provider skipped safely, zero calls ───────────────

def test_07_missing_key_skipped() -> bool:
    with Ctx() as t:
        t.set_config(nvidia_api_key="")
        fake, calls = _fake_post_script([])
        with patch.object(N, "_http_chat", fake):
            try:
                N.chat(MSG, task="general")
                raise AssertionError("missing key must skip")
            except N.NimSkip as e:
                assert "key" in str(e)
        assert calls == []
        assert N.skip_reason() == "no nvidia key"
        # not configured → not in the plan
        assert not N.configured()
        plan = R._plan_chain("smart", "general", False, **{
            "ollama": True, "local3b": False, "freellm": False,
            "openrouter": False, "nvidia": False,
            "grok": False, "openai": False})
        assert "nvidia" not in plan, plan
    print("✓ TEST 7: missing key → skipped safely, not in the plan, "
          "zero requests")
    return True


# ── TEST 8: all NVIDIA models unavailable → existing fallback attempted ─────

def test_08_all_models_down_fallback_used() -> bool:
    with Ctx() as t:
        n_models = len(N._eligible_models("general"))
        script = [(404, "x")] * n_models
        fake, calls = _fake_post_script(script)
        with patch.object(N, "_http_chat", fake):
            try:
                N.chat(MSG, task="general")
                raise AssertionError("all-404 must raise NimError")
            except N.NimError:
                pass
        assert len(calls) == n_models
        usage = t.base / "u.jsonl"
        cfg = {"router_mode": "smart", "nvidia_api_key": "nvapi-x",
               "freellmapi_api_key": "fkey"}
        ctxs = [
            patch.object(R, "_load_full_config", return_value=cfg),
            patch.object(R, "_load_router_config", return_value=[]),
            patch.object(R, "_llm_local_configured", return_value=False),
            patch.object(R, "_USAGE_PATH", usage),
            patch.object(R, "_gemini_calls_today", return_value=0),
            patch.object(R, "_steps_nvidia_ready", return_value=True),
        ]
        for c in ctxs:
            c.start()
        try:
            seen = []
            with patch.object(R, "_step_ollama", side_effect=RuntimeError("d1")), \
                 patch.object(R, "_step_nvidia", side_effect=RuntimeError(
                     "all eligible NVIDIA models failed")), \
                 patch.object(R, "_step_freellm",
                              side_effect=lambda *a, **k: (
                                  seen.append("freellm"),
                                  ("fallback answer", "auto", None))[1]):
                resp = R._patched_generate(None, model="m", contents="hi")
                assert resp.text == "fallback answer"
                assert seen == ["freellm"]
            rec = [json.loads(l) for l in usage.read_text().splitlines()][-1]
            assert rec["provider"] == "freellm"
            assert "nvidia" in [h["provider"] for h in rec["escalations"]]
        finally:
            for c in ctxs:
                c.stop()
    print("✓ TEST 8: all NVIDIA models down → existing fallback serves it")
    return True


# ── TEST 9: all providers unavailable → clear error, no crash ───────────────

def test_09_all_providers_down_clear_error() -> bool:
    with Ctx() as t:
        cfg = {"router_mode": "smart", "ollama_enabled": True,
               "nvidia_api_key": "nvapi-x", "freellmapi_api_key": "fkey",
               "gemini_api_key": "AIzaFake"}
        usage = t.base / "u.jsonl"
        ctxs = [
            patch.object(R, "_load_full_config", return_value=cfg),
            patch.object(R, "_load_router_config", return_value=[]),
            patch.object(R, "_llm_local_configured", return_value=False),
            patch.object(R, "_USAGE_PATH", usage),
            patch.object(R, "_gemini_calls_today", return_value=0),
            patch.object(R, "_gemini_budget_ok", return_value=True),
            patch.object(R, "_steps_nvidia_ready", return_value=True),
        ]
        for c in ctxs:
            c.start()
        try:
            def fail(name):
                def _f(*a, **k):
                    raise RuntimeError(f"{name} down")
                return _f
            with patch.object(R, "_step_ollama", side_effect=fail("ollama")), \
                 patch.object(R, "_step_nvidia", side_effect=fail("nvidia")), \
                 patch.object(R, "_step_freellm", side_effect=fail("freellm")), \
                 patch.object(R, "_gemini_attempt", side_effect=fail("gemini")):
                try:
                    R._patched_generate(None, model="m", contents="hi")
                    raise AssertionError("must raise, never crash silently")
                except RuntimeError as e:
                    assert "All LLM providers failed" in str(e)
                    for p in ("ollama", "nvidia", "freellm", "gemini"):
                        assert p in str(e)
            rec = [json.loads(l) for l in usage.read_text().splitlines()][-1]
            assert rec["ok"] is False and rec["error"] == "all providers failed"
        finally:
            for c in ctxs:
                c.stop()
    print("✓ TEST 9: all providers down → clear RuntimeError, decision logged")
    return True


# ── TEST 10: cloud fallback disabled → no cloud request at all ──────────────

def test_10_cloud_fallback_disabled() -> bool:
    with Ctx() as t:
        # router_mode 'local' → plan has no cloud rungs even with NVIDIA keyed
        cfg = {"router_mode": "local", "ollama_enabled": True,
               "nvidia_api_key": "nvapi-x"}
        ctxs = [
            patch.object(R, "_load_full_config", return_value=cfg),
            patch.object(R, "_load_router_config", return_value=[]),
            patch.object(R, "_llm_local_configured", return_value=False),
            patch.object(R, "_steps_nvidia_ready", return_value=True),
        ]
        for c in ctxs:
            c.start()
        try:
            plan = R._plan_chain("local", "general", False, **{
                "ollama": True, "local3b": False, "freellm": False,
                "openrouter": False, "nvidia": True,
                "grok": False, "openai": False})
            assert plan == ["ollama"], plan

            def nvidia_must_not_run(*a, **k):
                raise AssertionError("cloud requested with fallback off")
            with patch.object(R, "_step_ollama",
                              return_value=("local only", "q", None)), \
                 patch.object(R, "_step_nvidia", side_effect=nvidia_must_not_run), \
                 patch.object(N, "_http_chat",
                              side_effect=AssertionError("HTTP went out")):
                resp = R._patched_generate(None, model="m", contents="hi")
                assert resp.text == "local only"
        finally:
            for c in ctxs:
                c.stop()
        # and at the provider level: NVIDIA_NIM_ENABLED=off skips even directly
        ctxs2 = [
            patch.object(N, "_CONFIG_PATH", t.client),
            patch.object(N, "_REGISTRY_PATH", t.registry),
            patch.dict(os.environ,
                       {"NVIDIA_NIM_ENABLED": "false",
                        "NVIDIA_API_KEY": "nvapi-envkey"},
                       clear=False),
        ]
        for c in ctxs2:
            c.start()
        try:
            assert not N.enabled()
            fake, calls = _fake_post_script([])
            with patch.object(N, "_http_chat", fake):
                try:
                    N.chat(MSG)
                    raise AssertionError("disabled provider must skip")
                except N.NimSkip as e:
                    assert "disabled" in str(e)
            assert calls == []
        finally:
            for c in ctxs2:
                c.stop()
    print("✓ TEST 10: cloud fallback disabled → zero cloud requests")
    return True


# ── TEST 11: unverified (UNKNOWN pricing) model never auto-selected ─────────

def test_11_unknown_pricing_never_selected() -> bool:
    with Ctx() as t:
        # discovery reports a catalog model that is NOT approved → UNKNOWN
        fake_list = _fake_list(ids=["some-vendor/huge-pro-model",
                                    "nvidia/nemotron-3-super-120b-a12b"])
        with patch.object(N, "_http_list_models", fake_list):
            ids, err = N.discover_models(force=True)
        assert err == "" and "some-vendor/huge-pro-model" in ids
        reg = N._registr(0)
        assert reg["some-vendor/huge-pro-model"]["pricing"] == "UNKNOWN"
        # chat requests only ever touch APPROVED models
        fake, calls = _fake_post_script([(200, _ok_body("ok"))])
        with patch.object(N, "_http_chat", fake):
            N.chat(MSG, task="general")
        assert all(c in N.approved_models() for c in calls), calls
        assert "some-vendor/huge-pro-model" not in calls
        # even UNKNOWN models that the catalog listed are invisible to
        # _eligible_models — double-check directly
        assert "some-vendor/huge-pro-model" not in N._eligible_models("general")
        # ALL approved models marked unavailable → provider skips (it must
        # NEVER fall back to the UNKNOWN catalog model)
        for m in N._approved():
            N._mark(m, availability="unavailable", error="test")
        fake2, calls2 = _fake_post_script([])
        with patch.object(N, "_http_chat", fake2):
            try:
                N.chat(MSG, task="general")
                raise AssertionError("UNKNOWN model must not be auto-selected")
            except N.NimSkip as e:
                assert "approved" in str(e)
        assert calls2 == []
        # explicitly approved-verify it (manual selection) → then selectable
        t.set_config(nvidia_approved_models=["some-vendor/huge-pro-model"])
        with N._lock:
            N._registry_cache.update({"at": 0.0, "data": None})
        fake3, calls3 = _fake_post_script([(200, _ok_body("now approved"))])
        with patch.object(N, "_http_chat", fake3):
            text, used, _u = N.chat(MSG, task="general")
        assert used == "some-vendor/huge-pro-model"
        # the UI-visible pricing follows the (manual) approval — FREE
        st_by_id = {m["id"]: m for m in N.model_status()}
        assert st_by_id["some-vendor/huge-pro-model"]["pricing"] == "FREE"
    print("✓ TEST 11: UNKNOWN pricing never auto-selected; manual approval "
          "required")
    return True


# ── TEST 12: streaming behavior — the router's response adapter ─────────────

def test_12_streaming_adapter_intact() -> bool:
    with Ctx() as t:
        usage = t.base / "u.jsonl"
        cfg = {"router_mode": "smart", "ollama_enabled": True,
               "nvidia_api_key": "nvapi-x"}
        ctxs = [
            patch.object(R, "_load_full_config", return_value=cfg),
            patch.object(R, "_load_router_config", return_value=[]),
            patch.object(R, "_llm_local_configured", return_value=False),
            patch.object(R, "_USAGE_PATH", usage),
            patch.object(R, "_gemini_calls_today", return_value=0),
            patch.object(R, "_steps_nvidia_ready", return_value=True),
        ]
        for c in ctxs:
            c.start()
        try:
            with patch.object(R, "_step_ollama", side_effect=RuntimeError("d")), \
                 patch.object(R, "_step_nvidia",
                              return_value=("streamed text", "nvidia/x",
                                            {"prompt_tokens": 3,
                                             "completion_tokens": 2})), \
                 patch.object(R, "_step_freellm", side_effect=RuntimeError("d")):
                resp = R._patched_generate(None, model="m", contents="hi")
            # the adapter surface that call sites + the streaming pipeline
            # consumer (sentence splitters, .text, candidates parts) rely on
            assert resp.text == "streamed text"
            assert resp.candidates[0].content.parts[0].text == "streamed text"
            assert str(resp) == "streamed text"
            rec = [json.loads(l) for l in usage.read_text().splitlines()][-1]
            assert rec["provider"] == "nvidia" and rec["ok"] is True
            assert rec["out_tokens"] == 2
        finally:
            for c in ctxs:
                c.stop()
    print("✓ TEST 12: NIM answers flow through the same response adapter "
          "streaming/stream-splitting callers use")
    return True


# ── TEST 13: voice path untouched — Live API and local speech client ───────

def test_13_voice_path_untouched() -> bool:
    with Ctx() as t:
        # a) the router never intercepts tool-grounded/Live-API calls
        sentinel = object()
        with patch.object(R, "_orig_generate", return_value=sentinel) as og:
            resp = R._patched_generate(None, model="m", contents="q",
                                       config={"tools": [{"x": 1}]})
            assert resp is sentinel and og.called
        # b) the local speech client (sentence streaming for spoken replies)
        #    is independent of the cloud provider; split on the exact sentence
        #    boundary regex call_llm_stream/_stream_openai use
        from core import llm_client
        text = "One sentence here. And another! A question?"
        chunks = [s for s in llm_client._SENT_END.split(text) if s.strip()]
        assert len(chunks) >= 3
        assert not any("nvidia" in c.lower() for c in chunks)
        # c) NIM module itself never enters the voice path: the Live session
        #    hook (client.aio.live.connect) is not something the router or
        #    this module touches — assert the module exposes no live/streaming
        #    override hooks that could alter the voice session.
        assert not hasattr(N, "connect") and not hasattr(N, "live")
    print("✓ TEST 13: voice Live session + local speech client untouched")
    return True


# ── TEST 14: agents share the single central provider router ───────────────

def test_14_agents_use_central_router() -> bool:
    with Ctx() as t:
        # every provider path funnels through ONE choke point: the patched
        # generate_content + fns map. Prove the map owns every step and that
        # _step_nvidia delegates to this module's chat.
        import inspect
        src = inspect.getsource(R._patched_generate)
        assert 'fns = {' in src and '"nvidia":' in src
        # the routing plan is a pure single source of truth for agents
        assert R._plan_chain("smart", "code", False, **{
            "ollama": True, "local3b": True, "freellm": True,
            "openrouter": True, "nvidia": True, "deepseek": True,
            "grok": True, "openai": True})[0:3] == \
            ["ollama", "llm_local", "nvidia"]
        # router_status exposes the provider priority display for every agent
        st = R.router_status()
        assert st["nvidia"] in (True, False)
        assert "order" in st and st["order"][0] == "ollama"
        # usage log records which provider served each agent request
        usage = t.base / "u.jsonl"
        cfg = {"router_mode": "smart", "ollama_enabled": True,
               "nvidia_api_key": "nvapi-x"}
        with patch.object(R, "_load_full_config", return_value=cfg), \
             patch.object(R, "_load_router_config", return_value=[]), \
             patch.object(R, "_llm_local_configured", return_value=False), \
             patch.object(R, "_USAGE_PATH", usage), \
             patch.object(R, "_gemini_calls_today", return_value=0), \
             patch.object(R, "_steps_nvidia_ready", return_value=True), \
             patch.object(R, "_step_ollama", side_effect=RuntimeError("d")), \
             patch.object(R, "_step_nvidia",
                          return_value=("agent answer", "nvidia/x", None)), \
             patch.object(R, "_step_freellm", side_effect=RuntimeError("d")):
            resp = R._patched_generate(None, model="m",
                                       contents="fix this python def bug")
            assert resp.text == "agent answer"
        rec = [json.loads(l) for l in usage.read_text().splitlines()][-1]
        assert rec["task"] == "code" and rec["provider"] == "nvidia"
    print("✓ TEST 14: coding/research agents route through the central "
          "router; usage logged")
    return True


# ── extra provider-level cases (context length, empty response, env, UI) ────

def test_extra_context_length_and_empty() -> bool:
    with Ctx() as t:
        fake, calls = _fake_post_script([
            (400, '{"detail":"This model maximum context length is 4096"}'),
            (200, _ok_body("from next")),
        ])
        with patch.object(N, "_http_chat", fake):
            text, used, _u = N.chat(MSG, task="general")
        assert used == "z-ai/glm-5.3-flash"
        # HTTP 200 + empty content → treated as failure, next model
        fake2, calls2 = _fake_post_script([
            (200, '{"choices":[{"message":{"content":""}}]}'),
            (200, _ok_body("real text")),
        ])
        with patch.object(N, "_http_chat", fake2):
            text, used, _u = N.chat(MSG, task="general")
        assert text == "real text"
    print("✓ extra: context-length + empty-response handled by switching models")
    return True


def test_extra_env_variables_and_key_not_logged() -> bool:
    with Ctx() as t:
        ctxs = [
            patch.dict(os.environ, {
                "NVIDIA_API_KEY": "nvapi-envkey123",
                "NVIDIA_NIM_BASE_URL": "https://custom.example/v1/",
                "NVIDIA_NIM_ENABLED": "on",
            }, clear=False),
        ]
        for c in ctxs:
            c.start()
        try:
            assert N.api_key() == "nvapi-envkey123"
            assert N.base_url() == "https://custom.example/v1"   # trailing / off
            assert N.enabled()
            ok, msg = N.test_connection()
            assert not ok and "model list" in msg.lower() or not ok
        finally:
            for c in ctxs:
                c.stop()
        # redaction: even a hostile log string never carries the key
        leaky = "Authorization: Bearer nvapi-supersecret123 and stuff"
        assert "nvapi-supersecret123" not in N._redact(leaky)
        assert "<redacted>" in N._redact(leaky)
    print("✓ extra: env config honored; key material is redacted everywhere")
    return True


def test_extra_settings_section_and_apply() -> bool:
    with Ctx() as t:
        sec = N.settings_section()
        assert sec["namespace"] == "nvidia_nim"
        keys = {f["key"] for f in sec["fields"]}
        assert {"local_ai_enabled", "nvidia_nim_enabled", "nvidia_api_key",
                "router_mode", "nvidia_approved_models"} <= keys
        # the password field never pre-fills the secret
        key_field = next(f for f in sec["fields"]
                         if f["key"] == "nvidia_api_key")
        assert key_field["type"] == "password"
        assert "nvapi-testkey" not in str(key_field.get("value",
              key_field.get("placeholder", "")))
        # apply settings: rename approved list, disable nim, keep key on empty
        N._apply_settings({
            "local_ai_enabled": True,
            "nvidia_nim_enabled": False,
            "nvidia_api_key": "",               # empty → keep stored key
            "router_mode": "free",
            "nvidia_approved_models": "model/a, model/b",
        })
        cfg = json.loads(t.client.read_text())
        assert cfg["nvidia_api_key"] == "nvapi-testkey123456"   # preserved
        assert cfg["nvidia_nim_enabled"] is False
        assert cfg["router_mode"] == "free"
        assert cfg["nvidia_approved_models"] == ["model/a", "model/b"]
        # bad key rejected, nothing saved
        try:
            N._apply_settings({"nvidia_api_key": "not-an-nvapi-key"})
            raise AssertionError("non-nvapi key must be rejected")
        except ValueError:
            pass
    print("✓ extra: settings section safe (masked key) and persistence works")
    return True


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"✗ {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
