"""Spotify Web API client — the official way to search and control playback.

WHAT THIS IS
    Spotify's supported playback model is Spotify Connect: the Web API does not
    stream audio to us (that is not an API Spotify offers), it tells a Spotify
    *device* — the desktop app, the phone, the web player — what to play. So
    "play X on Spotify" means "find X, then start it on the user's active
    Spotify device", and pause / resume / next / previous / stop / status all go
    through the same API. Nothing is downloaded, decrypted or re-streamed.

AUTHENTICATION
    OAuth 2.0 Authorization Code with PKCE. PKCE needs no client secret, which
    is the right choice for an app running on the user's own machine: there is
    no secret to leak. The user supplies a Client ID (create an app at
    https://developer.spotify.com/dashboard, add the redirect URI below) either
    as SPOTIFY_CLIENT_ID or as "spotify_client_id" in config/api_keys.json.

    The redirect URI must be registered on the Spotify app. The default is
    http://127.0.0.1:8888/callback — Spotify accepts a loopback IP literal over
    plain HTTP for desktop apps. Override with SPOTIFY_REDIRECT_URI if 8888 is
    taken or the app uses another port.

TOKENS
    config/spotify_token.json, mode 0600. It is gitignored by the existing
    `**/token*.json` rule. Access tokens are refreshed automatically; refresh
    tokens are replaced when Spotify rotates them. Nothing here ever prints a
    token, and error messages carry Spotify's own text, not the request headers.

ACCOUNT REQUIREMENTS
    Playback *control* (play/pause/skip) requires Spotify Premium and an active
    device. Search and status work on the free tier. Those failures are turned
    into sentences rather than stack traces — see _explain().
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

SCOPES = " ".join(
    [
        "user-read-playback-state",
        "user-modify-playback-state",
        "user-read-currently-playing",
        "playlist-read-private",
        "playlist-read-collaborative",
    ]
)

_DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"

_lock = threading.RLock()
_connect_state: dict = {"state": "idle", "url": "", "error": ""}


class SpotifyError(RuntimeError):
    """A failure with a sentence for the user, and Spotify's HTTP status."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# ── Configuration ───────────────────────────────────────────────────────────

def _base_dir() -> Path:
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def token_path() -> Path:
    return _base_dir() / "config" / "spotify_token.json"


def _config_path() -> Path:
    return _base_dir() / "config" / "api_keys.json"


def client_id() -> str:
    """The public Client ID. Never a secret — PKCE does not use one."""
    env = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    if env:
        return env
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
        return str(data.get("spotify_client_id") or "").strip()
    except Exception:
        return ""


def redirect_uri() -> str:
    return os.environ.get("SPOTIFY_REDIRECT_URI", "").strip() or _DEFAULT_REDIRECT


def configured() -> bool:
    return bool(client_id())


# ── Token store ─────────────────────────────────────────────────────────────

def load_token() -> dict:
    try:
        return json.loads(token_path().read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_token(token: dict) -> None:
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(token, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    tmp.replace(p)


def forget_token() -> None:
    try:
        token_path().unlink()
    except Exception:
        pass


def connected() -> bool:
    t = load_token()
    return bool(t.get("refresh_token") or t.get("access_token"))


def _access_token(force_refresh: bool = False) -> str:
    with _lock:
        token = load_token()
        if not token:
            raise SpotifyError(
                "Spotify is not connected yet. Say 'connect Spotify' and I will "
                "open the authorisation page."
            )
        expires = float(token.get("expires_at") or 0)
        if force_refresh or not token.get("access_token") or expires - 60 < time.time():
            token = _refresh(token)
        return str(token["access_token"])


def _refresh(token: dict) -> dict:
    refresh = str(token.get("refresh_token") or "")
    if not refresh:
        raise SpotifyError("Spotify's session expired — say 'connect Spotify' again.")
    r = _post(TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client_id(),
    })
    if r.status_code >= 400:
        forget_token()
        raise SpotifyError("Spotify's session expired — say 'connect Spotify' again.")
    data = r.json()
    token = {
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token") or refresh,
        "expires_at": time.time() + float(data.get("expires_in") or 3600),
        "scope": data.get("scope") or token.get("scope", ""),
        "token_type": data.get("token_type") or "Bearer",
    }
    save_token(token)
    return token


def _post(url: str, data: dict):
    import requests

    return requests.post(url, data=data, timeout=20)


# ── HTTP ────────────────────────────────────────────────────────────────────

def _explain(status: int, payload: dict | None) -> str:
    detail = ""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or "")
        elif isinstance(err, str):
            detail = err
    if status == 401:
        return "Spotify needs you to reconnect — say 'connect Spotify'."
    if status == 403:
        return (
            "Spotify refused that: playback control needs Spotify Premium and "
            "an active device."
        )
    if status == 404:
        return (
            "No active Spotify device was found. Open Spotify on this computer "
            "or your phone first, then ask again."
        )
    if status == 429:
        return "Spotify is rate-limiting requests; try again in a moment."
    return detail or f"Spotify returned HTTP {status}."


def _request(method: str, path: str, params: dict | None = None,
             body: dict | None = None, retry: bool = True) -> dict:
    import requests

    token = _access_token()
    try:
        r = requests.request(
            method,
            API + path,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=body,
            timeout=20,
        )
    except Exception as e:
        raise SpotifyError(f"Could not reach Spotify ({e}).") from e

    if r.status_code == 401 and retry:
        _access_token(force_refresh=True)
        return _request(method, path, params, body, retry=False)
    if r.status_code == 204 or not r.content:
        if r.status_code >= 400:
            raise SpotifyError(_explain(r.status_code, None), r.status_code)
        return {}
    try:
        payload = r.json()
    except Exception:
        payload = None
    if r.status_code >= 400:
        raise SpotifyError(_explain(r.status_code, payload), r.status_code)
    return payload or {}


# ── Reading ─────────────────────────────────────────────────────────────────

def _track_row(item: dict) -> dict:
    artists = ", ".join(a.get("name", "") for a in (item.get("artists") or []) if a)
    return {
        "kind": "track",
        "name": item.get("name", ""),
        "artist": artists,
        "uri": item.get("uri", ""),
        "url": (item.get("external_urls") or {}).get("spotify", ""),
        "duration_ms": item.get("duration_ms", 0),
    }


def search(query: str, kind: str = "track", limit: int = 5) -> list[dict]:
    """Search tracks, artists, albums or playlists. Free-tier friendly."""
    query = (query or "").strip()
    if not query:
        raise SpotifyError("What should I search Spotify for?")
    kind = (kind or "track").strip().lower()
    if kind not in ("track", "artist", "album", "playlist"):
        kind = "track"
    data = _request(
        "GET", "/search",
        params={"q": query, "type": kind, "limit": max(1, min(int(limit), 20)),
                "market": "from_token"},
    )
    key = f"{kind}s"
    rows = []
    for item in ((data.get(key) or {}).get("items") or []):
        if not item:
            continue
        if kind == "track":
            rows.append(_track_row(item))
        elif kind == "artist":
            rows.append({
                "kind": "artist", "name": item.get("name", ""),
                "artist": "", "uri": item.get("uri", ""),
                "url": (item.get("external_urls") or {}).get("spotify", ""),
            })
        elif kind == "album":
            rows.append({
                "kind": "album", "name": item.get("name", ""),
                "artist": ", ".join(a.get("name", "") for a in (item.get("artists") or [])),
                "uri": item.get("uri", ""),
                "url": (item.get("external_urls") or {}).get("spotify", ""),
            })
        else:
            rows.append({
                "kind": "playlist", "name": item.get("name", ""),
                "artist": ((item.get("owner") or {}).get("display_name") or ""),
                "uri": item.get("uri", ""),
                "url": (item.get("external_urls") or {}).get("spotify", ""),
            })
    return rows


def player_state() -> dict:
    """GET /me/player — {} when nothing is active."""
    return _request("GET", "/player")


def devices() -> list[dict]:
    data = _request("GET", "/me/player/devices")
    return data.get("devices") or []


def now_playing() -> str:
    st = player_state()
    item = st.get("item") or {}
    if not item:
        return "Nothing is playing on Spotify."
    name = item.get("name") or "something"
    artists = ", ".join(a.get("name", "") for a in (item.get("artists") or []))
    device = (st.get("device") or {}).get("name") or "Spotify"
    state = "playing" if st.get("is_playing") else "paused"
    return f"{name}{(' by ' + artists) if artists else ''} — {state} on {device}."


# ── Playback control ────────────────────────────────────────────────────────

def _spotify_uri(text: str) -> str:
    """A spotify: URI from a URI or an open.spotify.com link, else ''."""
    t = (text or "").strip()
    if t.startswith("spotify:"):
        return t
    try:
        u = urlparse(t)
        if u.hostname and u.hostname.endswith("spotify.com"):
            parts = [p for p in u.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in (
                "track", "album", "playlist", "artist", "episode", "show"
            ):
                return f"spotify:{parts[0]}:{parts[1]}"
    except Exception:
        pass
    return ""


def _target(query: str) -> tuple[str, str]:
    """(uri, label) for a search phrase, Spotify URI or link."""
    uri = _spotify_uri(query)
    if uri:
        return uri, uri.rsplit(":", 1)[-1]
    rows = search(query, "track", limit=1)
    if not rows:
        raise SpotifyError(f"I could not find '{query}' on Spotify.")
    row = rows[0]
    label = row["name"] + (f" by {row['artist']}" if row.get("artist") else "")
    return row["uri"], label


def play(query: str = "", uri: str = "", device_id: str = "") -> str:
    """Start a track/album/playlist on the active device (Premium + device)."""
    target = uri or ""
    label = ""
    if not target and query:
        target, label = _target(query)
    body: dict = {}
    if target:
        kind = target.split(":")[1] if target.count(":") >= 2 else "track"
        if kind == "track":
            body["uris"] = [target]
        else:
            body["context_uri"] = target
        if not label:
            label = target.rsplit(":", 1)[-1]
    params = {"device_id": device_id} if device_id else None
    _request("PUT", "/me/player/play", params=params, body=body or None)
    return f"Playing {label} on Spotify." if label else "Playing on Spotify."


def pause() -> str:
    _request("PUT", "/me/player/pause")
    return "Paused Spotify."


def resume() -> str:
    _request("PUT", "/me/player/play")
    return "Resumed Spotify."


def next_track() -> str:
    _request("POST", "/me/player/next")
    return "Skipped to the next track on Spotify."


def previous_track() -> str:
    _request("POST", "/me/player/previous")
    return "Went back a track on Spotify."


def set_volume(percent: int) -> str:
    pct = max(0, min(int(percent), 100))
    _request("PUT", "/me/player/volume", params={"volume_percent": pct})
    return f"Spotify volume set to {pct}%."


# ── Authorisation (PKCE) ────────────────────────────────────────────────────

def connect_state() -> dict:
    return dict(_connect_state)


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _exchange(code: str, verifier: str) -> None:
    r = _post(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
        "client_id": client_id(),
        "code_verifier": verifier,
    })
    if r.status_code >= 400:
        raise SpotifyError(_explain(r.status_code, r.json() if r.content else None))
    data = r.json()
    if not data.get("access_token"):
        raise SpotifyError("Spotify did not return an access token.")
    save_token({
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": time.time() + float(data.get("expires_in") or 3600),
        "scope": data.get("scope", SCOPES),
        "token_type": data.get("token_type", "Bearer"),
    })


def _authorise(timeout: float = 180.0) -> None:
    """Blocking loopback flow; runs on a background thread from connect()."""
    try:
        cid = client_id()
        if not cid:
            raise SpotifyError(
                "No Spotify Client ID is configured. Create an app at "
                "developer.spotify.com, add " + redirect_uri() + " as a "
                "redirect URI, then put spotify_client_id in config/api_keys.json."
            )
        verifier = secrets.token_urlsafe(64)[:96]
        state = secrets.token_urlsafe(16)
        params = {
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": redirect_uri(),
            "scope": SCOPES,
            "code_challenge_method": "S256",
            "code_challenge": _challenge(verifier),
            "state": state,
            "show_dialog": "false",
        }
        url = AUTH_URL + "?" + urlencode(params)
        _connect_state.update(state="waiting", url=url, error="")

        parsed = urlparse(redirect_uri())
        port = parsed.port or 8888
        result: dict = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (stdlib API)
                q = parse_qs(urlparse(self.path).query)
                if "error" in q:
                    result["error"] = q["error"][0]
                elif q.get("state", [""])[0] != state:
                    result["error"] = "the reply did not match this request"
                elif "code" in q:
                    result["code"] = q["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                ok = "code" in result
                self.wfile.write(
                    ("<html><body style='font-family:sans-serif;padding:2rem'>"
                     f"<h2>{'Spotify connected' if ok else 'Spotify was not connected'}</h2>"
                     "<p>You can close this tab and return to JARVIS.</p>"
                     "</body></html>").encode("utf-8")
                )

            def log_message(self, *_a):  # keep the console clean
                pass

        try:
            httpd = HTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            raise SpotifyError(
                f"Port {port} is busy, so Spotify cannot call back ({e}). "
                f"Set SPOTIFY_REDIRECT_URI to another loopback port."
            ) from e

        httpd.timeout = 1.0
        webbrowser.open(url)
        deadline = time.time() + timeout
        while time.time() < deadline and not result:
            httpd.handle_request()
        httpd.server_close()

        if not result:
            raise SpotifyError("Spotify authorisation timed out — ask me to connect again.")
        if result.get("error"):
            raise SpotifyError(f"Spotify authorisation failed: {result['error']}")
        _exchange(result["code"], verifier)
        _connect_state.update(state="connected", error="")
    except Exception as e:  # surfaced through connect_state()
        _connect_state.update(state="error", error=str(e))


def connect() -> str:
    """Start the PKCE flow in the background and return what to tell the user."""
    if connected():
        return "Spotify is already connected."
    if not client_id():
        return (
            "Spotify needs a free Client ID first. Create an app at "
            "developer.spotify.com, add " + redirect_uri() + " as its redirect "
            "URI, then put spotify_client_id in config/api_keys.json."
        )
    st = connect_state()
    if st.get("state") == "waiting":
        return "Spotify authorisation is already waiting in your browser."
    _connect_state.update(state="starting", url="", error="")
    threading.Thread(target=_authorise, daemon=True, name="spotify-auth").start()
    for _ in range(20):  # wait for the URL, not for the whole flow
        if _connect_state.get("url") or _connect_state.get("error"):
            break
        time.sleep(0.05)
    if _connect_state.get("error"):
        return f"Spotify could not start: {_connect_state['error']}"
    return (
        "I opened Spotify's authorisation page in your browser — approve it "
        "and the connection completes on its own."
    )
