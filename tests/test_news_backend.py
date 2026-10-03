"""
tests/test_news_backend.py — the CurrentsAPI news backend and its dashboard
settings routes.

What this test proves (no network, no real credentials):
  1. config_manager: the key round-trips, a legacy newsapi.org key still works
     as a fallback, and mask_secret never leaks the middle of a secret.
  2. _newsapi_top: the query/no-query split maps to the right CurrentsAPI
     endpoint (search vs latest-news — they are NOT interchangeable), the key
     travels in an Authorization header rather than the URL, and the publisher
     is split back out of the "Headline - Publisher" title.
  3. The dashboard routes exist, require auth, and report the key masked.
     A key CurrentsAPI would reject is NOT persisted.

Isolation: config_manager is pointed at a temp file and requests.get is
monkeypatched, so the real config/ is never touched and no HTTP call is made.

Run:  .venv/bin/python tests/test_news_backend.py
Also pytest-compatible: test_news_backend().
"""
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── isolation: temp config BEFORE importing anything that reads it ───────────
_TMPDIR = tempfile.mkdtemp(prefix="jarvis-news-e2e-")
_TMP_CONFIG = Path(_TMPDIR) / "api_keys.json"
_TMP_CONFIG.write_text(json.dumps({
    "dashboard_auth_token": "e2e-static-token-1234",
}))

import memory.config_manager as cfg  # noqa: E402
cfg.CONFIG_FILE = _TMP_CONFIG

import actions.web_search as ws  # noqa: E402
from dashboard.server import DashboardServer  # noqa: E402

GOOD_KEY = "testkey-abcdefghijklmnopqrstuvwxyz-0123456789"


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_config_manager() -> None:
    cfg.save_news_key(GOOD_KEY)
    assert cfg.get_news_key() == GOOD_KEY, "key did not round-trip"

    # GET must report the key as set but must NOT echo it back.
    masked = cfg.mask_secret(cfg.get_news_key())
    assert GOOD_KEY not in masked, f"mask leaked the full key: {masked}"
    assert GOOD_KEY[:6] in masked and GOOD_KEY[-4:] in masked, masked

    # A short secret must be masked more aggressively, not nearly revealed.
    short_mask = cfg.mask_secret("abcd")
    assert "abcd" not in short_mask, f"short secret leaked: {short_mask}"

    assert cfg.mask_secret("") == "", "empty secret should mask to empty"

    # Legacy newsapi.org key still resolves when the new field is absent.
    data = json.loads(_TMP_CONFIG.read_text(encoding="utf-8"))
    data.pop("currents_api_key", None)
    data["newsapi_key"] = "legacy-newsapi-key-value"
    _TMP_CONFIG.write_text(json.dumps(data), encoding="utf-8")
    assert cfg.get_news_key() == "legacy-newsapi-key-value", "legacy fallback broken"

    # New field wins when both are present.
    cfg.save_news_key(GOOD_KEY)
    assert cfg.get_news_key() == GOOD_KEY, "new field should win over legacy"

    cfg.save_news_key("")


def test_queryless_news_allowed() -> None:
    """"What's in the news?" / the morning briefing sends no query, and that must
    reach the top-headlines endpoint rather than being rejected as empty."""
    orig_get, orig_key = ws.requests.get, ws.get_news_key
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, **kw):
        calls.append({"url": url, "params": params or {}})
        return _FakeResp(200, {"status": "ok", "news": [
            {"title": "A headline - A Publisher", "description": "d",
             "url": "https://example.com/b", "author": "", "image": "None",
             "language": "en", "category": [], "published": ""}]})

    ws.requests.get = fake_get
    ws.get_news_key = lambda: GOOD_KEY
    try:
        out = ws.web_search({"query": "", "mode": "news"})
        assert "Please provide a search query" not in out, \
            f"query-less news must be allowed: {out}"
        assert calls, "query-less news should still hit the backend"
        assert calls[-1]["url"].endswith("/v1/latest-news"), calls[-1]["url"]
    finally:
        ws.requests.get = orig_get
        ws.get_news_key = orig_key

    # Other modes still require a query.
    out = ws.web_search({"query": "", "mode": "search"})
    assert "Please provide a search query" in out, \
        f"search mode should still guard empty query: {out}"


def test_backend_endpoints() -> None:
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, **kw):
        calls.append({"url": url, "params": params or {},
                      "headers": headers or {}})
        # Mirror the real shape: "news" (not "articles"), publisher folded
        # into the title.
        return _FakeResp(200, {"status": "ok", "page": 1, "news": [
            {"id": "1", "title": "Big election result - The Daily Times",
             "description": "Votes counted.", "url": "https://example.com/a",
             "author": "", "image": "None", "language": "en",
             "category": ["politics_government"],
             "published": "2026-10-03 06:00:00 +0000"},
        ]})

    orig_get, orig_key = ws.requests.get, ws.get_news_key
    ws.requests.get = fake_get
    ws.get_news_key = lambda: GOOD_KEY
    try:
        # Query present -> search endpoint, and it must use `keywords`.
        out = ws._newsapi_top("india", 3)
        assert calls[-1]["url"].endswith("/v1/search"), calls[-1]["url"]
        assert calls[-1]["params"].get("keywords") == "india", calls[-1]
        assert "keywords" not in calls[-1]["url"], "params must not be in URL"

        # No query -> latest-news endpoint, and NO keywords (that 400s).
        out2 = ws._newsapi_top("", 3)
        assert calls[-1]["url"].endswith("/v1/latest-news"), calls[-1]["url"]
        assert "keywords" not in calls[-1]["params"], calls[-1]

        # Key must travel in the Authorization header, never the query string.
        assert calls[-1]["headers"].get("Authorization") == f"Bearer {GOOD_KEY}", \
            calls[-1]["headers"]

        # Publisher split back out of the title, headline kept clean.
        assert "Big election result" in out, out
        assert "The Daily Times" in out, f"publisher missing: {out}"
        assert "https://example.com/a" in out, out
        assert " - The Daily Times" not in out.split("http")[0], \
            f"publisher left in headline: {out}"

        # A bad key must raise so the chain falls through to Gemini/DDG.
        def bad_get(url, params=None, headers=None, timeout=None, **kw):
            return _FakeResp(401, {"status": "error", "message": "Invalid key"})
        ws.requests.get = bad_get
        try:
            ws._newsapi_top("india", 3)
            raise AssertionError("expected a raise on 401")
        except RuntimeError as e:
            assert "401" in str(e), e

        # No key configured at all must raise, not silently return junk.
        ws.get_news_key = lambda: ""
        try:
            ws._newsapi_top("india", 3)
            raise AssertionError("expected a raise when no key is configured")
        except RuntimeError:
            pass
    finally:
        ws.requests.get = orig_get
        ws.get_news_key = orig_key

    cfg.save_news_key("")


def test_key_validation() -> None:
    def make(status, payload):
        def _g(url, params=None, headers=None, timeout=None, **kw):
            return _FakeResp(status, payload)
        return _g

    orig = ws.requests.get
    try:
        ws.requests.get = make(200, {"status": "ok", "news": []})
        ok, msg = ws._newsapi_key_valid(GOOD_KEY)
        assert ok, msg

        ws.requests.get = make(401, {"status": "error", "message": "nope"})
        ok, msg = ws._newsapi_key_valid(GOOD_KEY)
        assert not ok and "rejected" in msg.lower(), msg

        ws.requests.get = make(429, {"status": "error", "message": "quota"})
        ok, msg = ws._newsapi_key_valid(GOOD_KEY)
        assert not ok and "quota" in msg.lower(), msg

        ok, msg = ws._newsapi_key_valid("")
        assert not ok, "empty key must not validate"
    finally:
        ws.requests.get = orig


# ── dashboard routes ────────────────────────────────────────────────────────

def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(port: int):
    srv = DashboardServer()
    import uvicorn
    config = uvicorn.Config(srv.app, host="127.0.0.1", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    end = time.time() + 20
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=2)
            return srv, server
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server did not come up")


_AUTH = {"Authorization": "Bearer test-session-token"}


def _get(port: int, path: str, auth: bool = True):
    headers = dict(_AUTH) if auth else {}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=headers)
    try:
        return json.load(urllib.request.urlopen(req, timeout=5))
    except urllib.error.HTTPError as e:
        return {"_status": e.code}


def _post(port: int, path: str, body: dict, auth: bool = True):
    headers = dict(_AUTH) if auth else {}
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=5))
    except urllib.error.HTTPError as e:
        return {"_status": e.code}


def test_dashboard_news_routes() -> None:
    try:
        import uvicorn  # noqa: F401
    except ImportError as e:
        print(f"SKIP: missing dep {e}")
        return

    # Never let a route reach the real network in this test.
    orig_get = ws.requests.get
    ws.requests.get = lambda *a, **k: _FakeResp(
        200, {"status": "ok", "news": [
            {"title": "T - P", "description": "d", "url": "u",
             "author": "", "image": "None", "language": "en",
             "category": [], "published": ""}]})
    # Start from a genuinely empty store: the legacy fallback key left by
    # test_config_manager() would otherwise read as "set".
    _TMP_CONFIG.write_text(json.dumps({
        "dashboard_auth_token": "e2e-static-token-1234",
    }), encoding="utf-8")
    port = _free_port()
    srv, server = _start_server(port)
    # _auth() only accepts live session tokens (what /login hands out), so
    # mint one the same way rather than assuming a static token is accepted.
    srv._tokens.add("test-session-token")
    try:
        # Unauthenticated must be rejected, not served.
        r = _get(port, "/api/settings/newsapi", auth=False)
        assert r.get("_status") == 401, f"expected 401, got {r}"
        r = _post(port, "/api/settings/newsapi", {"key": GOOD_KEY}, auth=False)
        assert r.get("_status") == 401, f"expected 401, got {r}"

        # No key yet -> set False, and the response must not carry a secret.
        r = _get(port, "/api/settings/newsapi")
        assert r.get("set") is False, f"fresh config should report unset: {r}"

        # Save a key the (faked) API accepts.
        r = _post(port, "/api/settings/newsapi", {"key": GOOD_KEY})
        assert r.get("ok") is True, f"save failed: {r}"

        # Now GET reports it as set, masked — and never in full.
        r = _get(port, "/api/settings/newsapi")
        assert r.get("set") is True, f"key should be set: {r}"
        assert GOOD_KEY not in json.dumps(r), f"GET leaked the key: {r}"
        assert r.get("masked"), f"expected a masked preview: {r}"

        # Empty key is rejected, and must not clear the stored one.
        r = _post(port, "/api/settings/newsapi", {"key": ""})
        assert r.get("ok") is False, f"empty key should be rejected: {r}"
        assert cfg.get_news_key() == GOOD_KEY, "empty save cleared the key"

        # A key the provider rejects must NOT be persisted.
        ws.requests.get = lambda *a, **k: _FakeResp(
            401, {"status": "error", "message": "Invalid key"})
        r = _post(port, "/api/settings/newsapi", {"key": "bad-key-value-xyz"})
        assert r.get("ok") is False, f"bad key should be rejected: {r}"
        assert cfg.get_news_key() == GOOD_KEY, "rejected key was persisted"

        # TEST validates without storing.
        r = _post(port, "/api/settings/newsapi/test", {"key": "bad-key-value-xyz"})
        assert r.get("ok") is False, f"test should reject a bad key: {r}"
        assert cfg.get_news_key() == GOOD_KEY, "TEST must not persist"

        ws.requests.get = lambda *a, **k: _FakeResp(
            200, {"status": "ok", "news": []})
        r = _post(port, "/api/settings/newsapi/test", {"key": GOOD_KEY})
        assert r.get("ok") is True, f"test should accept a good key: {r}"
    finally:
        ws.requests.get = orig_get
        try:
            server.should_exit = True
        except Exception:
            pass
        cfg.save_news_key("")


def test_news_backend() -> bool:
    test_config_manager()
    print("  ✓ config_manager: round-trip, legacy fallback, masking")
    test_queryless_news_allowed()
    print("  ✓ query-less news reaches top-headlines; other modes still guarded")
    test_backend_endpoints()
    print("  ✓ backend: endpoint split, bearer auth, title parsing, fail-through")
    test_key_validation()
    print("  ✓ key validation: accept / reject / quota / empty")
    test_dashboard_news_routes()
    print("  ✓ dashboard routes: auth, masked GET, rejected key not persisted")
    return True


if __name__ == "__main__":
    test_news_backend()
    print("\nPASS: news backend")
