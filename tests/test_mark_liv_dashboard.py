"""
tests/test_mark_liv_dashboard.py — regression tests for the Mark-LIV
dashboard settings endpoints.

What this test proves (against the REAL DashboardServer, no browser):
  1. Push-to-talk: GET reports the saved flag + whether the app-side hook is
     live; POST persists the flag to config and, when the hook is registered,
     calls it and reports the scope actually achieved ("global"/"window").
     Without the hook the flag still persists and takes effect next restart.
  2. One-shot Gemini client: GET reports ladder/key/cooldowns (key length-
     masked, never echoed); POST clear-cooldowns empties the 429 cooldowns;
     POST refresh-key re-reads the key; an unknown action is rejected.

Isolation: the config store is a temp file — the real config/ is never
touched, and core.gemini's key file is pointed at the same temp copy.

Run:  .venv/bin/python tests/test_mark_liv_dashboard.py
Also pytest-compatible: test_mark_liv_dashboard_settings().
"""

import json
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── isolation: point config_manager at temp files BEFORE any import ──────────
_TMPDIR = tempfile.mkdtemp(prefix="jarvis-markliv-e2e-")
_TMP_CONFIG = Path(_TMPDIR) / "api_keys.json"
# NOTE: the SAME token value the other dashboard tests use. pytest imports
# every test module before running any, and each module re-points the shared
# cfg.CONFIG_FILE at its own temp copy — a different token here would 401 the
# offline/PWA tests depending on import order.
_TMP_CONFIG.write_text(
    json.dumps(
        {
            "dashboard_auth_token": "e2e-static-token-1234",
            "gemini_api_key": "AIzaFAKE-masking-check-0123456789",
        }
    )
)

import memory.config_manager as cfg  # noqa: E402  (after temp paths are set)

cfg.CONFIG_FILE = _TMP_CONFIG  # sandbox the static-token store

from dashboard.server import DashboardServer  # noqa: E402


def _env_ok() -> tuple[bool, str]:
    try:
        import uvicorn  # noqa: F401
    except ImportError as e:
        return False, f"missing dep: {e} (.venv/bin/pip install uvicorn)"
    return True, ""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(port: int):
    srv = DashboardServer()
    import uvicorn

    config = uvicorn.Config(srv.app, host="127.0.0.1", port=port, log_level="error")
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


_AUTH = {"Authorization": "Bearer e2e-static-token-1234"}


def _get(port: int, path: str, auth: bool = True):
    headers = dict(_AUTH) if auth else {}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        return json.load(urllib.request.urlopen(req, timeout=5))
    except urllib.error.HTTPError as e:
        return {"_status": e.code}


def _post(port: int, path: str, body: dict, auth: bool = True):
    headers = dict(_AUTH) if auth else {}
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        return json.load(urllib.request.urlopen(req, timeout=5))
    except urllib.error.HTTPError as e:
        return {"_status": e.code}


def test_mark_liv_dashboard_settings() -> bool:
    ok, why = _env_ok()
    if not ok:
        print(f"SKIP: {why}")
        return True

    # core.gemini must read the SANDBOXED key file, never the real config/
    import core.gemini as g

    g._KEY_FILE = _TMP_CONFIG
    g._cached_key = None
    g.clear_cooldowns()

    port = _free_port()
    srv, server = _start_server(port)
    try:
        # ── 1. push-to-talk: unauthenticated is rejected ────────────────────
        r = _get(port, "/api/settings/push-to-talk", auth=False)
        assert r.get("_status") == 401, f"expected 401, got {r}"

        # ── 2. GET without app hook: flag + config-only live status ─────────
        r = _get(port, "/api/settings/push-to-talk")
        assert r.get("enabled") is False, f"fresh config should be off: {r}"
        assert r.get("live") is False, f"no hook registered → not live: {r}"
        assert (
            isinstance(r.get("chord"), str) and r["chord"]
        ), f"chord label missing: {r}"

        # ── 3. POST without hook: still persists, next-restart semantics ────
        r = _post(port, "/api/settings/push-to-talk", {"enabled": True})
        assert r.get("ok") is True and r.get("enabled") is True, r
        assert r.get("scope") is None, f"no hook → no scope: {r}"
        assert cfg.get_push_to_talk_enabled() is True, "flag not persisted"
        r = _post(port, "/api/settings/push-to-talk", {"enabled": False})
        assert r.get("ok") is True and cfg.get_push_to_talk_enabled() is False, r

        # ── 4. with the app-side hook: live toggle + scope reported ─────────
        seen: list[bool] = []
        srv.set_ptt_callback(lambda enable: (seen.append(enable), "global")[1])
        r = _post(port, "/api/settings/push-to-talk", {"enabled": True})
        assert r.get("scope") == "global" and seen == [True], r
        r = _get(port, "/api/settings/push-to-talk")
        assert r.get("live") is True and r.get("enabled") is True, r
        r = _post(port, "/api/settings/push-to-talk", {"enabled": False})
        assert seen == [True, False], f"hook not called with False: {seen}"

        # ── 5. gemini client: status shape, key masked, never echoed ────────
        g._KEY_FILE = _TMP_CONFIG
        g._cached_key = None
        g._cooldown["gemini-2.5-flash"] = time.monotonic() + 300
        r = _get(port, "/api/settings/gemini-client")
        assert r.get("ok") is True, r
        assert r.get("key_configured") is True, f"temp config has a key: {r}"
        assert r.get("key_masked") == "AIzaFA…6789", f"masking wrong: {r}"
        assert "AIzaFAKE-masking-check-0123456789" not in json.dumps(
            r
        ), "full key must never be echoed"
        assert "smart" in {k.lower() for k in r["ladders"]}, r
        assert "gemini-2.5-flash" in r.get("cooling", []), r

        # ── 6. actions: clear-cooldowns + refresh-key + unknown ─────────────
        r = _post(port, "/api/settings/gemini-client", {"action": "clear-cooldowns"})
        assert r.get("ok") is True and r.get("cooling") == [], r
        assert not g._cooling("gemini-2.5-flash"), "cooldown still set"
        r = _post(port, "/api/settings/gemini-client", {"action": "refresh-key"})
        assert r.get("ok") is True and r.get("key_configured") is True, r
        r = _post(port, "/api/settings/gemini-client", {"action": "bogus"})
        assert r.get("_status") == 400, f"unknown action must 400: {r}"

        # ── 7. gemini text() with a bogus key: default, not a crash ────────
        assert (
            g.text("say ok", tier=g.FAST, default="") == ""
        ), "unusable key → empty default, no exception"

        print("✓ push-to-talk: auth, GET/POST, persistence, live hook + scope")
        print("✓ gemini client: status shape, masking, cooldowns, actions")
        return True
    finally:
        server.should_exit = True
        g._KEY_FILE = g.__dict__.get("_KEY_FILE_ORIG", g._KEY_FILE)
        g._cached_key = None
        g.clear_cooldowns()


if __name__ == "__main__":
    sys.exit(0 if test_mark_liv_dashboard_settings() else 1)
