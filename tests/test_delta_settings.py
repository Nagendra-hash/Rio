"""
tests/test_delta_settings.py — Delta Exchange India credentials in RIO Settings.

What this test proves (no network, no real credentials):
  1. config_manager round-trips the Delta key + secret and clears them;
  2. resolve_credentials prefers the environment and falls back to the stored
     pair, and credentials_source() names whichever is in force;
  3. GET/POST /api/settings/delta require auth, never echo a secret, reject a
     half-supplied pair, and clear on request;
  4. the RIO Settings page actually wires the card.

Isolation: config_manager is pointed at a temp api_keys.json and the DELTA_*
env vars are removed first, so the real config/ is never touched.

Run:  .venv/bin/python tests/test_delta_settings.py
Also pytest-compatible.
"""
from __future__ import annotations

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

# ── isolation: temp config BEFORE anything reads it ─────────────────────────
_TMPDIR = Path(tempfile.mkdtemp(prefix="jarvis-delta-settings-"))
_TMP_CONFIG = _TMPDIR / "api_keys.json"
_TMP_CONFIG.write_text(json.dumps({
    "dashboard_auth_token": "e2e-static-token-1234",
}), encoding="utf-8")

import memory.config_manager as cfg  # noqa: E402
cfg.CONFIG_DIR = _TMPDIR
cfg.CONFIG_FILE = _TMP_CONFIG

from orchestrator.agents import delta_exchange as dx  # noqa: E402
from dashboard.server import DashboardServer  # noqa: E402

KEY = "delta-test-api-key-abcdefghijklmnop"
SECRET = "delta-test-api-secret-0123456789"


def _clean_env(monkeypatch) -> None:
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)


def _reset_store() -> None:
    _TMP_CONFIG.write_text(json.dumps({
        "dashboard_auth_token": "e2e-static-token-1234",
    }), encoding="utf-8")


def test_config_manager_roundtrip(monkeypatch):
    _clean_env(monkeypatch)
    _reset_store()

    assert cfg.get_delta_api_key() == ""
    assert cfg.get_delta_api_secret() == ""

    cfg.save_delta_api_credentials(KEY, SECRET)
    assert cfg.get_delta_api_key() == KEY
    assert cfg.get_delta_api_secret() == SECRET

    # Masked previews must never contain the whole secret.
    masked_key = cfg.mask_secret(cfg.get_delta_api_key())
    masked_secret = cfg.mask_secret(cfg.get_delta_api_secret())
    assert KEY not in masked_key and SECRET not in masked_secret

    cfg.save_delta_api_credentials("", "")
    assert cfg.get_delta_api_key() == ""
    assert cfg.get_delta_api_secret() == ""


def test_resolution_prefers_env_then_file(monkeypatch):
    _clean_env(monkeypatch)
    _reset_store()

    assert dx.resolve_credentials() == ("", "")
    assert dx.credentials_source() == "none"
    assert dx.DeltaClient.credentials_configured() is False

    cfg.save_delta_api_credentials(KEY, SECRET)
    assert dx.resolve_credentials() == (KEY, SECRET)
    assert dx.credentials_source() == "settings"
    assert dx.DeltaClient.credentials_configured() is True

    # Environment wins over the file — an explicit export is never overridden.
    monkeypatch.setenv("DELTA_API_KEY", "env-key")
    monkeypatch.setenv("DELTA_API_SECRET", "env-secret")
    assert dx.resolve_credentials() == ("env-key", "env-secret")
    assert dx.credentials_source() == "env"

    # The stored pair still answers when only half the env is exported.
    monkeypatch.delenv("DELTA_API_SECRET")
    assert dx.resolve_credentials() == ("env-key", SECRET)

    cfg.save_delta_api_credentials("", "")


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


def test_dashboard_delta_routes(monkeypatch):
    try:
        import uvicorn  # noqa: F401
    except ImportError as e:
        print(f"SKIP: missing dep {e}")
        return

    _clean_env(monkeypatch)
    _reset_store()
    port = _free_port()
    srv, server = _start_server(port)
    srv._tokens.add("test-session-token")
    try:
        # Auth is required for both verbs.
        assert _get(port, "/api/settings/delta", auth=False
                    ).get("_status") == 401
        assert _post(port, "/api/settings/delta",
                     {"key": KEY, "secret": SECRET},
                     auth=False).get("_status") == 401

        # Fresh store: reported unset, no secret material in the payload.
        r = _get(port, "/api/settings/delta")
        assert r.get("ok") is True and r.get("set") is False, r
        assert r.get("source") == "none", r
        assert r.get("read_only") is True, r

        # A half-supplied pair is rejected and changes nothing.
        r = _post(port, "/api/settings/delta", {"key": KEY})
        assert r.get("ok") is False, r
        assert cfg.get_delta_api_key() == "", "rejected save persisted a key"

        # Save the pair, then read it back masked.
        r = _post(port, "/api/settings/delta", {"key": KEY, "secret": SECRET})
        assert r.get("ok") is True and r.get("set") is True, r
        r = _get(port, "/api/settings/delta")
        assert r.get("set") is True and r.get("source") == "settings", r
        assert KEY not in json.dumps(r), f"GET leaked the key: {r}"
        assert SECRET not in json.dumps(r), f"GET leaked the secret: {r}"
        assert r.get("masked_key") and r.get("masked_secret"), r

        # The shared client now reports itself configured.
        assert dx.DeltaClient.credentials_configured() is True

        # Env overrides the stored pair and is named as the source.
        monkeypatch.setenv("DELTA_API_KEY", "env-key")
        monkeypatch.setenv("DELTA_API_SECRET", "env-secret")
        r = _get(port, "/api/settings/delta")
        assert r.get("source") == "env", r
        assert "env-key" not in json.dumps(r), "GET leaked the env key"

        # Clear only removes the stored pair; the env pair still applies.
        r = _post(port, "/api/settings/delta", {"clear": True})
        assert r.get("ok") is True and r.get("set") is False, r
        assert cfg.get_delta_api_key() == "" and cfg.get_delta_api_secret() == ""
        r = _get(port, "/api/settings/delta")
        assert r.get("set") is False and r.get("source") == "env", r
    finally:
        try:
            server.should_exit = True
        except Exception:
            pass
        _reset_store()


def test_settings_page_wires_delta_card():
    app_js = (BASE / "dashboard/static/rio/app.js").read_text(encoding="utf-8")
    for needle in ('id="set-delta"', "/api/settings/delta",
                   "DELTA_API_KEY", "delta-key", "delta-secret"):
        assert needle in app_js, f"RIO Settings page is missing {needle}"

    # The legacy dashboard (served at "/") has its own Settings overlay; it must
    # expose the same pair rather than silently dropping the feature.
    legacy = (BASE / "dashboard/static/app.html").read_text(encoding="utf-8")
    for needle in ('id="delta-key-input"', 'id="delta-secret-input"',
                   "saveDeltaCreds()", "/api/settings/delta"):
        assert needle in legacy, f"legacy dashboard is missing {needle}"


if __name__ == "__main__":
    class _MP:
        """Tiny monkeypatch stand-in for the script-mode entry point."""

        def __init__(self):
            self._saved = {}

        def setenv(self, k, v):
            import os
            self._saved.setdefault(k, os.environ.get(k))
            os.environ[k] = v

        def delenv(self, k, raising=False):
            import os
            self._saved.setdefault(k, os.environ.get(k))
            os.environ.pop(k, None)

        def undo(self):
            import os
            for k, v in self._saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    mp = _MP()
    try:
        test_config_manager_roundtrip(mp)
        test_resolution_prefers_env_then_file(mp)
        mp.undo()
        test_dashboard_delta_routes(mp)
        mp.undo()
        test_settings_page_wires_delta_card()
    finally:
        mp.undo()
    print("OK — Delta credential settings")
