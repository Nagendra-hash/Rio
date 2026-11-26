"""core/spotify_api.py — PKCE, token storage, and honest error mapping.

No network: every HTTP call is monkeypatched. What is tested is the part that
would leak a credential or lie to the user if it were wrong — the PKCE
challenge, the 0600 token file, the Premium/device explanations, and the
request shapes Spotify actually accepts.
"""

from __future__ import annotations

import os
import stat

import pytest

from core import spotify_api


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never read or write the real config/ while testing."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_REDIRECT_URI", raising=False)
    yield tmp_path


def test_pkce_challenge_matches_the_rfc_example():
    """RFC 7636 Appendix B — the one value everyone can check by hand."""
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert spotify_api._challenge(verifier) == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_the_token_file_is_private(isolated_home):
    spotify_api.save_token({"access_token": "a", "refresh_token": "r"})
    p = spotify_api.token_path()
    assert p.is_file()
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600, f"token file is {oct(mode)}, not 0600"


def test_connected_is_false_until_a_token_exists():
    assert spotify_api.connected() is False


def test_client_id_comes_from_config_not_only_the_environment(isolated_home):
    import json

    cfg = isolated_home / "config" / "api_keys.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"spotify_client_id": "abc123"}), encoding="utf-8")
    assert spotify_api.client_id() == "abc123"
    assert spotify_api.configured() is True


def test_errors_are_sentences_not_stack_traces():
    assert "Premium" in spotify_api._explain(403, None)
    assert "active Spotify device" in spotify_api._explain(404, None)
    assert "reconnect" in spotify_api._explain(401, None)
    assert "rate-limiting" in spotify_api._explain(429, None)
    assert "Custom message" in spotify_api._explain(
        400, {"error": {"message": "Custom message"}}
    )


def test_search_normalises_track_rows(monkeypatch):
    payload = {
        "tracks": {
            "items": [
                {
                    "name": "Kesariya",
                    "uri": "spotify:track:abc",
                    "artists": [{"name": "Arijit Singh"}, {"name": "Pritam"}],
                    "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
                    "duration_ms": 268000,
                }
            ]
        }
    }
    monkeypatch.setattr(spotify_api, "_request", lambda *a, **k: payload)
    rows = spotify_api.search("kesariya")
    assert rows == [
        {
            "kind": "track",
            "name": "Kesariya",
            "artist": "Arijit Singh, Pritam",
            "uri": "spotify:track:abc",
            "url": "https://open.spotify.com/track/abc",
            "duration_ms": 268000,
        }
    ]


def test_play_a_search_phrase_sends_a_track_uri(monkeypatch):
    calls = []

    def fake_request(method, path, params=None, body=None, retry=True):
        calls.append((method, path, params, body))
        if path == "/search":
            return {"tracks": {"items": [
                {"name": "Kesariya", "uri": "spotify:track:abc",
                 "artists": [{"name": "Arijit Singh"}]}
            ]}}
        return {}

    monkeypatch.setattr(spotify_api, "_request", fake_request)
    reply = spotify_api.play("kesariya")
    assert "Kesariya" in reply
    put = [c for c in calls if c[0] == "PUT"][0]
    assert put[1] == "/me/player/play"
    assert put[3] == {"uris": ["spotify:track:abc"]}


def test_play_an_album_link_sends_a_context_uri(monkeypatch):
    calls = []

    def fake_request(method, path, params=None, body=None, retry=True):
        calls.append((method, path, params, body))
        return {}

    monkeypatch.setattr(spotify_api, "_request", fake_request)
    spotify_api.play("https://open.spotify.com/album/xyz789")
    put = [c for c in calls if c[0] == "PUT"][0]
    assert put[3] == {"context_uri": "spotify:album:xyz789"}


def test_now_playing_reads_the_player_state(monkeypatch):
    monkeypatch.setattr(spotify_api, "player_state", lambda: {
        "is_playing": True,
        "device": {"name": "Desktop"},
        "item": {"name": "Kesariya", "artists": [{"name": "Arijit Singh"}]},
    })
    assert spotify_api.now_playing() == (
        "Kesariya by Arijit Singh — playing on Desktop."
    )


def test_now_playing_with_no_item_is_honest(monkeypatch):
    monkeypatch.setattr(spotify_api, "player_state", lambda: {})
    assert spotify_api.now_playing() == "Nothing is playing on Spotify."


def test_connect_without_a_client_id_explains_what_is_missing():
    reply = spotify_api.connect()
    assert "Client ID" in reply
    assert "developer.spotify.com" in reply


def test_an_expired_access_token_is_refreshed(monkeypatch):
    spotify_api.save_token({
        "access_token": "old",
        "refresh_token": "refresh",
        "expires_at": 0,
    })

    def fake_refresh(token):
        token = dict(token)
        token["access_token"] = "new"
        token["expires_at"] = 9e12
        spotify_api.save_token(token)
        return token

    monkeypatch.setattr(spotify_api, "_refresh", fake_refresh)
    assert spotify_api._access_token() == "new"


def test_no_token_means_a_sentence_not_a_crash():
    with pytest.raises(spotify_api.SpotifyError) as e:
        spotify_api._access_token()
    assert "not connected" in str(e.value).lower()


# ── The loopback authorisation flow, exercised for real ─────────────────────
# These two tests open an actual HTTP server on a free loopback port and make an
# actual HTTP request to it. Nothing leaves the machine: Spotify's token
# endpoint is replaced by a stub, so what is being tested is the wiring a real
# authorisation depends on — the PKCE parameters in the URL, the state check,
# and the hand-off of the code to the token exchange.

def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_flow(monkeypatch, port):
    import threading
    import time

    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-client")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", f"http://127.0.0.1:{port}/callback")
    seen: dict = {}
    monkeypatch.setattr(
        spotify_api.webbrowser, "open",
        lambda url: seen.setdefault("url", url) or True,
    )

    def fake_exchange(code, verifier):
        seen["code"] = code
        seen["verifier"] = verifier
        spotify_api.save_token({
            "access_token": "access", "refresh_token": "refresh",
            "expires_at": 9e12,
        })

    monkeypatch.setattr(spotify_api, "_exchange", fake_exchange)
    thread = threading.Thread(
        target=spotify_api._authorise, kwargs={"timeout": 10}, daemon=True
    )
    thread.start()
    deadline = time.time() + 5
    while "url" not in seen and time.time() < deadline:
        time.sleep(0.02)
    assert "url" in seen, "the browser was never sent to Spotify"
    return seen, thread


def _callback(port: int, query: str) -> str:
    import urllib.request

    url = f"http://127.0.0.1:{port}/callback?{query}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode("utf-8")


def test_the_authorisation_url_carries_pkce_and_a_loopback_redirect(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    port = _free_port()
    seen, thread = _start_flow(monkeypatch, port)
    q = parse_qs(urlparse(seen["url"]).query)
    assert q["code_challenge_method"] == ["S256"]
    assert q["client_id"] == ["test-client"]
    assert q["redirect_uri"] == [f"http://127.0.0.1:{port}/callback"]
    assert len(q["code_challenge"][0]) >= 43
    assert "code_verifier" not in q, "the verifier must never leave this machine"
    # Finish the flow so the server thread does not linger.
    _callback(port, f"code=abc&state={q['state'][0]}")
    thread.join(5)


def test_the_callback_accepts_a_matching_state_and_exchanges_the_code(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    port = _free_port()
    seen, thread = _start_flow(monkeypatch, port)
    state = parse_qs(urlparse(seen["url"]).query)["state"][0]
    body = _callback(port, f"code=the-code&state={state}")
    thread.join(5)
    assert seen["code"] == "the-code"
    assert seen["verifier"], "the PKCE verifier was not handed to the exchange"
    assert spotify_api.connected() is True
    assert spotify_api.connect_state()["state"] == "connected"
    assert "connected" in body.lower()


def test_the_callback_rejects_a_state_that_does_not_match(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    port = _free_port()
    seen, thread = _start_flow(monkeypatch, port)
    real_state = parse_qs(urlparse(seen["url"]).query)["state"][0]
    body = _callback(port, "code=the-code&state=someone-elses-state")
    thread.join(5)
    assert "code" not in seen, "a mismatched state must never be exchanged"
    assert spotify_api.connected() is False
    assert "not connected" in body.lower()
    assert real_state
