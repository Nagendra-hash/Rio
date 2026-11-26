"""Offline integration tests for the local freellmapi router.

The HTTP surface is exercised for real (server on an ephemeral port); only the
upstream providers are faked, so no network and no credentials are needed.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from services.freellmapi import server as svc  # noqa: E402

KEY = "test-key-123"


@pytest.fixture
def router(monkeypatch):
    config = svc.RouterConfig.load()
    instance = svc.Router(config)
    monkeypatch.setattr(svc, "ROUTER", instance)
    monkeypatch.setenv("FREELLMAPI_API_KEY", KEY)
    return instance


@pytest.fixture
def http_server(router):
    server = ThreadingHTTPServer(("127.0.0.1", 0), svc.Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _request(url, method="GET", body=None, key=KEY, timeout=10):
    headers = {}
    data = None
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), dict(exc.headers)


def _reply(content: str, model: str = "gpt-oss-20b") -> dict:
    return {"id": "x", "model": model, "choices": [{"message": {"role": "assistant", "content": content}}]}


# ── selection logic ────────────────────────────────────────────────────────


def test_config_is_ordered_and_complete():
    config = svc.RouterConfig.load()
    names = [p.name for p in config.providers]
    assert names[:3] == ["ovh-anon", "llm7", "kilo"], names
    assert config.min_max_tokens >= 32
    assert config.catalog.get("commit")


def test_auto_walks_the_pool_in_priority_order(router):
    candidates = router.candidates("auto")
    assert [p.name for p, _ in candidates][0] == "ovh-anon"
    assert len(candidates) >= 3


def test_explicit_model_routes_to_its_provider(router):
    assert [p.name for p, _ in router.candidates("gemini-3.8-flash")] == ["gemini"]
    assert [p.name for p, _ in router.candidates("kilo-auto/free")] == ["kilo"]


def test_unknown_model_has_no_candidates(router):
    assert router.candidates("no/such-model") == []


# ── HTTP auth and routes ───────────────────────────────────────────────────


def test_health_is_open_and_models_needs_a_key(http_server):
    status, body, _ = _request(f"{http_server}/health", key=None)
    assert status == 200 and body["status"] in ("ok", "degraded")
    assert any(p["name"] == "ovh-anon" for p in body["providers"])

    status, body, _ = _request(f"{http_server}/v1/models", key=None)
    assert status == 401 and "invalid API key" in json.dumps(body)

    status, body, _ = _request(f"{http_server}/v1/models", key="wrong")
    assert status == 401

    status, body, _ = _request(f"{http_server}/v1/models")
    assert status == 200
    ids = [entry["id"] for entry in body["data"]]
    assert ids[0] == "auto" and "gpt-oss-20b" in ids


def test_missing_key_configuration_refuses_service(monkeypatch):
    monkeypatch.delenv("FREELLMAPI_API_KEY", raising=False)
    monkeypatch.setattr(svc, "_env", lambda name: "" if name == "FREELLMAPI_API_KEY" else "")
    called = {"n": 0}
    monkeypatch.setattr(svc, "_post_json", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    server = ThreadingHTTPServer(("127.0.0.1", 0), svc.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        status, _, _ = _request(f"http://{host}:{port}/v1/models", key="anything")
        assert status == 503
        assert called["n"] == 0
    finally:
        server.shutdown()
        server.server_close()


# ── fallback behaviour ─────────────────────────────────────────────────────


def test_auto_falls_through_a_rate_limited_provider(router, http_server, monkeypatch):
    calls: list[str] = []

    def fake_post(url, payload, headers, timeout):
        calls.append(url)
        if "ovh" in url:
            return 429, {"error": {"message": "slow down"}}, "30"
        return 200, _reply("ok", payload["model"]), None

    monkeypatch.setattr(svc, "_post_json", fake_post)
    status, body, headers = _request(
        f"{http_server}/v1/chat/completions", "POST",
        {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert status == 200
    assert body["choices"][0]["message"]["content"] == "ok"
    assert headers.get("X-FreeLLM-Provider") == "llm7"
    assert len(calls) == 2
    # the rate-limited provider is parked, so the next request skips it
    assert router._state(router.config.providers[0]).cooling_for() > 0


def test_empty_content_is_not_an_answer(router, http_server, monkeypatch):
    def fake_post(url, payload, headers, timeout):
        if "kilo" in url:
            return 200, _reply("real answer", "kilo-auto/free"), None
        return 200, {"choices": [{"message": {"role": "assistant", "content": ""}}]}, None

    monkeypatch.setattr(svc, "_post_json", fake_post)
    status, body, headers = _request(
        f"{http_server}/v1/chat/completions", "POST",
        {"model": "auto", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
    )
    assert status == 200
    assert body["choices"][0]["message"]["content"] == "real answer"
    assert headers.get("X-FreeLLM-Provider") == "kilo"
    # the reasoning-model floor was applied
    assert headers.get("X-FreeLLM-MaxTokensFloor") == "64"


def test_tool_calls_count_as_an_answer(router, http_server, monkeypatch):
    monkeypatch.setattr(svc, "_post_json", lambda *a, **k: (
        200,
        {"choices": [{"message": {"role": "assistant", "content": None,
                                  "tool_calls": [{"id": "1", "type": "function"}]}}]},
        None,
    ))
    status, body, _ = _request(
        f"{http_server}/v1/chat/completions", "POST",
        {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert status == 200
    assert body["choices"][0]["message"]["tool_calls"]


def test_all_providers_failing_returns_503_with_reasons(router, http_server, monkeypatch):
    monkeypatch.setattr(svc, "_post_json", lambda *a, **k: (0, {"error": "boom"}, None))
    status, body, _ = _request(
        f"{http_server}/v1/chat/completions", "POST",
        {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert status == 503
    message = body["error"]["message"]
    assert "no free provider could answer" in message
    assert "ovh-anon" in message


def test_missing_provider_key_is_skipped(router, monkeypatch):
    monkeypatch.setattr(svc, "_env", lambda name: "" if name in ("NVIDIA_API_KEY",) else KEY)
    names = [p.name for p, _ in router.candidates("auto")]
    assert "nvidia" not in names


def test_bad_body_and_oversized_body(router, http_server):
    status, _, _ = _request(f"{http_server}/v1/chat/completions", "POST", body=None)
    assert status == 400

    request = urllib.request.Request(
        f"{http_server}/v1/chat/completions",
        data=b"x" * (svc.MAX_BODY_BYTES + 10),
        method="POST",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=10)
        raise AssertionError("oversized body was accepted")
    except urllib.error.HTTPError as exc:
        assert exc.code == 413
    except urllib.error.URLError as exc:
        # The server refuses before draining the body, so a client that writes
        # everything first can see the closed connection instead of the 413.
        # Either way the request was refused, never processed.
        assert "Broken pipe" in str(exc) or "Connection reset" in str(exc), exc


def test_health_reports_cooldowns(router, http_server, monkeypatch):
    monkeypatch.setattr(svc, "_post_json", lambda *a, **k: (429, {"error": "x"}, None))
    _request(f"{http_server}/v1/chat/completions", "POST",
             {"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    status, body, _ = _request(f"{http_server}/health", key=None)
    assert status == 200
    ovh = next(p for p in body["providers"] if p["name"] == "ovh-anon")
    assert ovh["cooling_for_s"] > 0
    assert ovh["last_status"] == "rate_limited"
