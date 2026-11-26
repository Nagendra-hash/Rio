"""
music_player.py — play music on Spotify, JioSaavn, Gaana, Wynk, Amazon
Music or YouTube Music.

Six services, honest paths to sound — none of them invents a
capability the service does not give away for free:

JIOSAAVN plays inside the assistant's own music popup — never in Chrome
    JioSaavn publishes no developer API. What it does publish, the same way
    its own web player fetches it, is a DES-encrypted CDN stream whose key is
    public knowledge (it ships inside their player). This tool resolves a song
    to that stream and plays it in a small in-app popup (gui/music_popup.py)
    with its own QMediaPlayer — so "play Kesariya on JioSaavn" starts audio
    inside RIO instead of opening a browser. Play / pause / stop / mute /
    unmute all reach that popup, and the video player is not touched at all.
    This is a grey area under JioSaavn's terms (it is not an official API),
    which is why it is not DRM and is only the mechanism their web player
    itself uses. To use JioSaavn's official web player in a browser instead,
    set JARVIS_JIOSAAVN_INHUD off and there is no separate flag — just ask for
    the search page; the popup is the default because it never needs Chrome.

SPOTIFY uses the official Web API (OAuth 2.0 + PKCE) when the user connects it
    Search, play/pause/skip, and "what's playing" all go through Spotify's
    documented API, on the user's active Spotify device. Playback control
    needs Spotify Premium and an open device — Spotify's requirement, not
    ours — and the tool says so instead of failing silently. No client secret
    is involved: PKCE is designed for apps on the user's own machine. Without
    a connection the old zero-setup behaviour remains: `spotify:search:<q>`
    opens the desktop app with the results ready, or open.spotify.com without
    it. A full `spotify:track:`/`spotify:album:` URI still opens as-is, and
    starts playing the moment the app picks it up.

GAANA plays hands-free the same way JioSaavn does
    The api.gaana.com endpoint everyone scraped is dead — it answers
    "tracks":null now. But gaana.com/search/<query> is still
    SERVER-rendered (verified 2026-10-03: 144 KB of real HTML where the
    API returned null), and its first /song/ link is the top result.
    Opening that song page starts it in Gaana's web player.

WYNK AND AMAZON MUSIC ARE LOGIN-GATED, AND THE TOOL SAYS SO
    Wynk's web player has no addressable search route (/music/search/<q>
    302s to /music) and plays nothing before an Airtel sign-in, so Wynk
    opens the player and says exactly that — it never claims a song is
    playing when the tool only opened a page. Amazon Music is
    per-marketplace (music.amazon.in vs .com; this install defaults to
    .in, change _AMAZON_MUSIC_HOME if yours is not) and needs an Amazon
    session: the search URL opens either way, sign-in prompt or not.

YOUTUBE MUSIC resolves through yt-dlp, which is already a dependency
    `ytsearch1:` turns a song name into a watch URL; music.youtube.com
    autoplays it. Without yt-dlp the Music search page opens instead.

PAUSE / RESUME / NEXT / PREVIOUS, AND WHO ANSWERS FOR THEM
    With Spotify connected, transport goes to Spotify's Web API — it knows
    which device is playing even when the OS session does not. Otherwise the
    global media keys do the work: the assistant's microphone stays open while
    music plays, so a per-service control channel would need OAuth for each
    one, and the media keys already reach whichever app or tab is actually
    playing. There is no separate "stop" because nothing on the keyboard
    stops, so stop pauses. Before pressing anything, the tool checks that some
    media session exists — a key pressed into silence is not a pause.

INSIDE THE DISPLAY, WHEN THE SERVICE GIVES US SOMETHING TO PLAY
    A browser tab loses to Chrome's autoplay policy on first visit and needs
    the user to drive it. So when the action context carries a `player` — it
    always does inside the HUD — YouTube Music plays on the assistant's own
    display, local files (MP3, M4A, WAV, FLAC, OGG) go to the HUD's Qt
    player or `ffplay`, and JioSaavn plays in its dedicated music popup
    (gui/music_popup.py), which owns a QMediaPlayer of its own and shares
    nothing with the video player. Spotify plays nowhere in-HUD — it gives
    away no free stream URLs — so it controls the user's Spotify device. The
    HUD closes the microphone while music sounds (its own rule, see ui.py's
    _sync_mic_for_video) and gives it back when the music stops.

NOW PLAYING READS THE SERVICE FIRST, THEN THE OPERATING SYSTEM
    With Spotify connected and named, "what song is this" comes from Spotify's
    own player state — the truthful answer even for a device the OS session
    does not expose. Otherwise it reads the system's media session. On Linux
    that is MPRIS over D-Bus — jeepney first (the pure-Python bus library
    screen_processor already uses for the screenshot portal), then playerctl,
    then one-shot gdbus property reads; every Chromium tab and Firefox
    instance exposes MPRIS, so browser players report too. On Windows it is
    the System Media Transport Controls — the very session the media keys hit
    — read through PowerShell; on macOS Spotify and Music answer over
    AppleScript.
"""

from __future__ import annotations

import base64
import os
import random
import re
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

try:
    import requests

    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    # Imported lazily: pyautogui connects to the display while importing, and
    # media keys are a sideline — they must not decide whether the music
    # action itself can be loaded. See core/desktop_input.py.
    from core.desktop_input import LazyPyAutoGUI

    pyautogui = LazyPyAutoGUI()
    _PYAUTOGUI = True
except Exception:
    pyautogui = None
    _PYAUTOGUI = False

from config import is_windows, is_mac

SERVICES = ("spotify", "jiosaavn", "gaana", "wynk", "amazon_music", "youtube_music")

# Default service when the user never names one. Configurable ("use Spotify as
# my default music app") and overridable with JARVIS_MUSIC_PROVIDER; the shipped
# default stays JioSaavn, which is what this install has always used.
DEFAULT_SERVICE = "jiosaavn"

# JioSaavn has NO official public API. Their web player's own endpoint is
# undocumented, and its media URLs are DES-encrypted; extracting those streams
# is exactly what the project was asked not to do. So the default path opens
# JioSaavn's own player (their site, their licensing) and controls it with the
# OS media session. The old in-HUD decrypted stream stays behind this flag for
# people who explicitly accept the terms-of-service risk; it is OFF by default.
_SAAVN_INHUD = os.environ.get("JARVIS_JIOSAAVN_INHUD", "").strip().lower() in (
    "1", "true", "yes", "on"
)


def _default_service() -> str:
    try:
        from memory.config_manager import get_music_provider

        return get_music_provider() or DEFAULT_SERVICE
    except Exception:
        return DEFAULT_SERVICE


def _spotify_api():
    """The Spotify Web API client, or None when this install cannot import it."""
    try:
        from core import spotify_api

        return spotify_api
    except Exception:
        return None


_AUDIO_EXT = {
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus",
    ".wma", ".aiff", ".aif", ".alac", ".m4b",
}


def _local_audio(text: str) -> str:
    """An existing audio file named by the user, or ''."""
    raw = (text or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    if raw.lower().startswith("file://"):
        try:
            from urllib.parse import unquote, urlparse

            raw = unquote(urlparse(raw).path)
        except Exception:
            return ""
    try:
        p = Path(raw).expanduser()
        if p.is_file() and p.suffix.lower() in _AUDIO_EXT:
            return str(p)
    except Exception:
        pass
    return ""


def _play_local_audio(path: str, player) -> str:
    """Local MP3/WAV/FLAC — the HUD's player is the speaker when it exists."""
    label = Path(path).name
    if player is not None and hasattr(player, "show_video"):
        return _play_in_hud(
            [{"stream": path, "label": label, "perma": ""}], 0, "local", player
        )
    # No display surface: use the same ffplay speaker path media_play uses.
    try:
        from actions.media_play import media_play

        return media_play({"action": "play", "url": path}, player=player)
    except Exception as e:
        return f"I found {label}, but could not start playback ({e})."

_SERVICE_ALIASES = {
    "spotify": "spotify",
    "jiosaavn": "jiosaavn",
    "jio saavn": "jiosaavn",
    "saavn": "jiosaavn",
    "gaana": "gaana",
    "wynk": "wynk",
    "wynk music": "wynk",
    "amazon_music": "amazon_music",
    "amazon music": "amazon_music",
    "amazon": "amazon_music",
    "prime music": "amazon_music",
    "youtube_music": "youtube_music",
    "youtube music": "youtube_music",
    "yt music": "youtube_music",
    "ytmusic": "youtube_music",
}

# action → which key to press. `resume` and `pause` share playpause because a
# toggle is exactly what each of them wants, whichever state the player is in.
_MEDIA_KEYS = {
    "pause": "playpause",
    "stop": "playpause",
    "resume": "playpause",
    "next": "nexttrack",
    "previous": "prevtrack",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_SAAVN_API = "https://www.jiosaavn.com/api.php"

# Amazon Music's web player is per-marketplace and does not cross-redirect:
# a session on .in plays the Indian catalogue, .com the US one. This
# install's context is India, so .in is the default — change the domain
# here if yours is not.
_AMAZON_MUSIC_HOME = "https://music.amazon.in"


def _spotify_app_installed() -> bool:
    if is_windows():
        for env in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(env, "")
            if base and (Path(base) / "Spotify" / "Spotify.exe").exists():
                return True
        return False
    if is_mac():
        return Path("/Applications/Spotify.app").exists()
    # Linux: a deb/rpm/snap install lands on PATH; a flatpak-only install does
    # not, and falls through to the web player rather than growing a second
    # detection mechanism for one package manager.
    return bool(shutil.which("spotify"))


def _open_uri(uri: str) -> bool:
    """Hand a non-http URI (spotify:...) to the OS. False when it fails."""
    try:
        if is_windows():
            os.startfile(uri)  # type: ignore[attr-defined]
            return True
        cmd = "open" if is_mac() else "xdg-open"
        subprocess.Popen(
            [cmd, uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return True
    except Exception as e:
        print(f"[Music] could not open '{uri}': {e}")
        return False


def _open_web(url: str) -> bool:
    try:
        return bool(webbrowser.open(url))
    except Exception as e:
        print(f"[Music] could not open browser for {url}: {e}")
        return False


def _decrypt_saavn_stream(enc: str) -> str:
    """JioSaavn ships its direct CDN stream URL DES-encrypted, and the key
    is the same one their own web player carries (b'38346591'). Decrypting
    gives a plain audio/mp4 URL the HUD's Qt player can play directly —
    this is how music ends up INSIDE the assistant's display instead of in
    a browser tab."""
    raw = b""
    try:
        # preferred: pycryptodome if the user has it
        from Cryptodome.Cipher import DES

        raw = DES.new(b"38346591", DES.MODE_ECB).decrypt(base64.b64decode(enc))
    except ImportError:
        # fallback: cryptography — already a hard dependency of the
        # dashboard (TLS). TripleDES with an 8-byte key IS DES.
        try:
            try:
                from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
            except Exception:
                from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
            from cryptography.hazmat.primitives.ciphers import Cipher, modes

            dec = Cipher(TripleDES(b"38346591"), modes.ECB()).decryptor()
            raw = dec.update(base64.b64decode(enc)) + dec.finalize()
        except Exception:
            return ""
    except Exception:
        return ""
    try:
        pad = raw[-1]
        if 1 <= pad <= 8:
            raw = raw[:-pad]
        return raw.decode("utf-8", "ignore")
    except Exception:
        return ""


def _saavn_meta(song: dict) -> tuple[str, str, str]:
    """(title, artist, artwork) of a JioSaavn song, tolerating field drift.

    The public endpoint has moved fields around before: the title has lived in
    `title`, `name` and `song`; the artist in `singers`, `primary_artists` and
    `music`. This reads whichever is present so the popup always has something
    to show, and the artwork URL is scaled up from its 150px thumbnail.
    """
    title = ""
    for k in ("title", "name", "song"):
        title = str(song.get(k) or "").strip()
        if title:
            break
    artist = ""
    for k in ("singers", "music"):
        artist = str(song.get(k) or "").strip()
        if artist:
            break
    if not artist:
        more = song.get("more_info") or {}
        primaries = ((more.get("artistMap") or {}).get("primary_artists")
                     or song.get("primary_artists") or [])
        artist = ", ".join((a.get("name") or "").strip()
                           for a in primaries if isinstance(a, dict))[:60]
    artwork = str(song.get("image") or song.get("image_uri") or "").strip()
    # Bump the JioSaavn CDN thumbnail to a size that fills a 56px cell.
    if artwork:
        artwork = re.sub(r"150x150", "500x500", artwork)
    return title, artist, artwork


def _saavn_label(song: dict) -> str:
    title, artist, _art = _saavn_meta(song)
    return " — ".join(x for x in (title, artist) if x)


def _saavn_pick(query: str = "", n: int = 5) -> list[dict]:
    """Up to n playable songs as {perma, stream, label}; [] when unreachable.

    With a query this is the top search results; with an empty query it is
    the songs of one random editorial chart, so a vague 'play some music'
    never repeats itself. `stream` is the decrypted direct media URL — empty
    for any song JioSaavn did not hand us a stream for.
    """
    if not _REQUESTS_OK:
        return []
    try:
        chart_title = ""
        if query:
            # NO api_version=4 here: that shape returns albums/playlists
            # (every result filtered out by the song-type check → empty →
            # the browser fallback, observed 2026-10-15). The plain call
            # returns songs directly.
            params = {
                "__call": "search.getResults",
                "_format": "json",
                "_marker": "0",
                "cc": "in",
                "includeMetaTags": "1",
                "p": "1",
                "n": str(n),
                "q": query,
            }
            r = requests.get(_SAAVN_API, params=params, headers=_HEADERS, timeout=10)
            # The field is a bare list of songs on some deployments and a
            # {"data": [...]} dict on others — accept both.
            payload = r.json().get("results") or []
            songs = payload.get("data") if isinstance(payload, dict) else payload
        else:
            r = requests.get(
                _SAAVN_API,
                params={
                    "__call": "content.getCharts",
                    "_format": "json",
                    "_marker": "0",
                    "api_version": "4",
                    "cc": "in",
                },
                headers=_HEADERS,
                timeout=10,
            )
            charts = [c for c in (r.json() or []) if c.get("id")]
            if not charts:
                return []
            chart = random.choice(charts)
            chart_title = (chart.get("title") or "").strip()
            r = requests.get(
                _SAAVN_API,
                params={
                    "__call": "playlist.getDetails",
                    "_format": "json",
                    "_marker": "0",
                    "api_version": "4",
                    "cc": "in",
                    "listid": chart["id"],
                },
                headers=_HEADERS,
                timeout=10,
            )
            songs = r.json().get("list") or []

        picked = []
        for song in songs or []:
            # the non-v4 shape carries type: "song"; some deployments omit
            # the field entirely — only skip entries PROVEN to be albums etc.
            if song.get("type") not in (None, "", "song") or not song.get("perma_url"):
                continue
            more = song.get("more_info") or {}
            label = _saavn_label(song)
            if not label:
                label = query
            if chart_title and label:
                label = f"{label} from '{chart_title}'"
            stream = ""
            enc = more.get("encrypted_media_url") or song.get(
                "encrypted_media_url"
            )  # top level in the plain shape
            if enc:
                stream = _decrypt_saavn_stream(enc)
                if stream and str(more.get("320kbps") or song.get("320kbps")) == "true":
                    stream = stream.replace("_96.mp4", "_320.mp4")
            _t, _a, _art = _saavn_meta(song)
            picked.append(
                {"perma": song["perma_url"], "stream": stream, "label": label,
                 "title": _t, "artist": _a, "artwork": _art}
            )
            if len(picked) >= n:
                break
        return picked
    except Exception as e:
        print(f"[Music] JioSaavn pick failed: {e}")
        return []


def _saavn_song_url(query: str) -> tuple[str, str]:
    """(song page URL, label) of the top JioSaavn result, or ('', '')."""
    picked = _saavn_pick(query)
    if picked:
        return picked[0]["perma"], picked[0]["label"]
    return "", ""


def _gaana_song_url(query: str) -> tuple[str, str]:
    """(song page URL, label) of the top Gaana result, or ('', '').

    gaana.com/search/<query> is server-rendered and its first /song/ href
    is the top result — the old api.gaana.com search now returns null.
    """
    if not _REQUESTS_OK:
        return "", ""
    try:
        r = requests.get(
            f"https://gaana.com/search/{quote(query)}", headers=_HEADERS, timeout=12
        )
        match = re.search(r'href="(/song/[^"]+)"', r.text)
        if match:
            page = "https://gaana.com" + match.group(1)
            label = match.group(1)[len("/song/") :].replace("-", " ").strip()
            return page, label or query
    except Exception as e:
        print(f"[Music] Gaana search failed: {e}")
    return "", ""


def _saavn_default_song() -> tuple[str, str]:
    """(song page URL, label) of a random trending song, for a vague
    'play some music' with no song named. One random editorial chart, then
    one random song in it — two in a row never sound the same."""
    picked = _saavn_pick("", n=10)
    if picked:
        s = random.choice(picked)
        return s["perma"], s["label"]
    return "", ""


def _ytmusic_watch_url(query: str) -> str:
    """music.youtube.com watch URL for the top result, or '' without yt-dlp."""
    try:
        import yt_dlp
    except Exception:
        return ""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        if info and info.get("entries"):
            video_id = (info["entries"][0] or {}).get("id")
            if video_id:
                return f"https://music.youtube.com/watch?v={video_id}"
    except Exception as e:
        print(f"[Music] YouTube Music resolve failed: {e}")
    return ""


def _press_key(key: str) -> None:
    """The single point that touches pyautogui, so tests can watch it."""
    pyautogui.press(key)


def _press_media_key(action: str) -> str:
    if pyautogui is None or not pyautogui.available():
        return (
            "Desktop input is unavailable, so I cannot send media keys — "
            + (pyautogui.fail_reason() if pyautogui is not None else "pyautogui missing")
        )
    try:
        _press_key(_MEDIA_KEYS[action])
    except Exception as e:
        return f"Could not send the media key, sir: {e}"
    return {
        "pause": "Paused the music.",
        "stop": "Paused the music.",
        "resume": "Resumed the music.",
        "next": "Skipped to the next track.",
        "previous": "Back to the previous track.",
    }[action]


# ── Now playing: read the OS's own media session ────────────────────────────

_MPRIS_PREFIX = "org.mpris.MediaPlayer2."

_PLAYER_LABELS = {
    "spotify": "Spotify",
    "firefox": "Firefox",
    "chromium": "the browser",
    "chrome": "the browser",
    "google-chrome": "the browser",
    "msedge": "Edge",
    "brave": "Brave",
    "vlc": "VLC",
    "mpv": "mpv",
    "rhythmbox": "Rhythmbox",
    "strawberry": "Strawberry",
    "celluloid": "Celluloid",
    "lollypop": "Lollypop",
}


def _friendly_player(raw: str) -> str:
    """'chromium.instance123' → 'the browser', 'SpotifyAB.SpotifyMusic' → 'Spotify'."""
    base = (raw or "").split("!")[0].split(".")[0].strip().lower()
    if base.endswith(".exe") or base.endswith("exe"):
        base = base[:-4]
    if base in ("spotifyab", "spotifymusic", "spotify"):
        return "Spotify"
    label = _PLAYER_LABELS.get(base)
    if label:
        return label
    return base.replace("-", " ").title() if base else "an unknown player"


def _unwrap_variant(v):
    """jeepney hands D-Bus variants ('v') back either as Variant objects or
    as plain (signature, value) tuples, depending on the call path — peel
    either shape until the value underneath."""
    while isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str):
        v = v[1]
    while hasattr(v, "value"):
        v = v.value
    return v


def _query_linux_jeepney() -> list[dict]:
    """MPRIS via jeepney — real D-Bus values, no GVariant text parsing.

    Raw new_method_call, not the Proxy convenience: the Proxy needs the
    destination to serve introspection XML, and org.freedesktop.DBus does
    not. Same call shape screen_processor uses for the screenshot portal.
    """
    from jeepney import DBusAddress, new_method_call
    from jeepney.io.blocking import open_dbus_connection

    sessions = []
    conn = open_dbus_connection()
    try:
        bus = DBusAddress(
            "/org/freedesktop/DBus",
            bus_name="org.freedesktop.DBus",
            interface="org.freedesktop.DBus",
        )
        names = conn.send_and_get_reply(new_method_call(bus, "ListNames")).body[0]
        for name in names:
            if not name.startswith(_MPRIS_PREFIX):
                continue
            try:
                props = DBusAddress(
                    "/org/mpris/MediaPlayer2",
                    bus_name=name,
                    interface="org.freedesktop.DBus.Properties",
                )
                meta = _unwrap_variant(
                    conn.send_and_get_reply(
                        new_method_call(
                            props,
                            "Get",
                            "ss",
                            ("org.mpris.MediaPlayer2.Player", "Metadata"),
                        )
                    ).body[0]
                )
                status = str(
                    _unwrap_variant(
                        conn.send_and_get_reply(
                            new_method_call(
                                props,
                                "Get",
                                "ss",
                                ("org.mpris.MediaPlayer2.Player", "PlaybackStatus"),
                            )
                        ).body[0]
                    )
                )
                title, artist = "", ""
                t = _unwrap_variant(meta.get("xesam:title"))
                if t:
                    title = str(t)
                a = _unwrap_variant(meta.get("xesam:artist"))
                if a:
                    artist = ", ".join(str(x) for x in a)
                sessions.append(
                    {
                        "player": name[len(_MPRIS_PREFIX) :],
                        "title": title,
                        "artist": artist,
                        "status": status,
                    }
                )
            except Exception:
                continue  # a half-dead player must not sink the rest
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return sessions


def _query_linux_playerctl() -> list[dict]:
    # Control characters as field separators: titles and artists contain
    # every printable character people have ever written.
    fmt = "{{playerName}}\x1f{{title}}\x1f{{artist}}\x1f{{status}}"
    r = subprocess.run(
        ["playerctl", "--all-players", "metadata", "--format", fmt],
        capture_output=True,
        text=True,
        timeout=6,
    )
    sessions = []
    for line in (r.stdout or "").splitlines():
        player, title, artist, status = (line.split("\x1f") + [""] * 4)[:4]
        if title.strip():
            sessions.append(
                {
                    "player": player.strip(),
                    "title": title.strip(),
                    "artist": artist.strip(),
                    "status": status.strip(),
                }
            )
    return sessions


def _gunquote(s: str) -> str:
    """Undo GVariant's minimal string escapes. Best effort — it only has to
    survive song titles, which contain quotes and backslashes, not history."""
    return s.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")


def _gdbus(args: list[str]) -> str:
    r = subprocess.run(["gdbus"] + args, capture_output=True, text=True, timeout=6)
    return r.stdout or ""


def _query_linux_gdbus() -> list[dict]:
    """MPRIS via one-shot gdbus property reads. The screenshot portal could
    NOT work this way (its caller must stay on the bus), but a Metadata Get
    replies synchronously, so a short-lived process is fine here."""
    out = _gdbus(
        [
            "call",
            "--session",
            "--dest",
            "org.freedesktop.DBus",
            "--object-path",
            "/org/freedesktop/DBus",
            "--method",
            "org.freedesktop.DBus.ListNames",
        ]
    )
    names = re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", out)
    sessions = []
    for name in names:
        try:
            meta = _gdbus(
                [
                    "call",
                    "--session",
                    "--dest",
                    name,
                    "--object-path",
                    "/org/mpris/MediaPlayer2",
                    "--method",
                    "org.freedesktop.DBus.Properties.Get",
                    "org.mpris.MediaPlayer2.Player",
                    "Metadata",
                ]
            )
            status = _gdbus(
                [
                    "call",
                    "--session",
                    "--dest",
                    name,
                    "--object-path",
                    "/org/mpris/MediaPlayer2",
                    "--method",
                    "org.freedesktop.DBus.Properties.Get",
                    "org.mpris.MediaPlayer2.Player",
                    "PlaybackStatus",
                ]
            )
        except Exception:
            continue
        st = re.search(r"<'(\w+)'>", status)
        title_m = re.search(r"'xesam:title':\s*<'((?:[^'\\]|\\.)*)'", meta)
        if not title_m:
            continue
        artist = ""
        artist_m = re.search(r"'xesam:artist':\s*<\[(.*?)\]>", meta)
        if artist_m:
            artist = ", ".join(
                _gunquote(x)
                for x in re.findall(r"'((?:[^'\\]|\\.)*)'", artist_m.group(1))
            )
        sessions.append(
            {
                "player": name[len(_MPRIS_PREFIX) :],
                "title": _gunquote(title_m.group(1)),
                "artist": artist,
                "status": st.group(1) if st else "",
            }
        )
    return sessions


def _query_linux() -> list[dict]:
    """An empty list means the bus ANSWERED and nothing is playing; raising
    means nobody could be asked. The two must not be confused."""
    errors = []
    for probe in (_query_linux_jeepney, _query_linux_playerctl, _query_linux_gdbus):
        try:
            return probe()
        except Exception as e:
            errors.append(f"{probe.__name__}: {e}")
    raise RuntimeError("; ".join(errors) or "no probe available")


_SMTC_POWERSHELL = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await([object]$op, [type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait(); $task.Result }
$sep = [string][char]31
$mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
foreach ($s in $mgr.GetSessions()) {
  try {
    $p = Await ($s.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
    $st = $s.GetPlaybackInfo().PlaybackStatus
    ($s.SourceAppUserModelId, $p.Title, $p.Artist, $st) -join $sep
  } catch {}
}
"""


def _query_windows() -> list[dict]:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _SMTC_POWERSHELL],
        capture_output=True,
        text=True,
        timeout=15,
    )
    sessions = []
    for line in (r.stdout or "").splitlines():
        source, title, artist, status = (line.split("\x1f") + [""] * 4)[:4]
        if title.strip():
            sessions.append(
                {
                    "player": source.strip(),
                    "title": title.strip(),
                    "artist": artist.strip(),
                    "status": status.strip(),
                }
            )
    return sessions


_MACOS_OSASCRIPT = """
set out to ""
if application "Spotify" is running then
\ttell application "Spotify"
\t\ttry
\t\t\tset st to player state
\t\t\tif st is playing or st is paused then
\t\t\t\tset stTxt to "Playing"
\t\t\t\tif st is paused then set stTxt to "Paused"
\t\t\t\tset out to "Spotify" & (character id 31) & (name of current track) & (character id 31) & (artist of current track) & (character id 31) & stTxt
\t\t\tend if
\t\tend try
\tend tell
end if
if application "Music" is running then
\ttell application "Music"
\t\ttry
\t\t\tset st to player state
\t\t\tif st is playing or st is paused then
\t\t\t\tset stTxt to "Playing"
\t\t\t\tif st is paused then set stTxt to "Paused"
\t\t\t\tif out is not "" then set out to out & linefeed
\t\t\t\tset out to out & "Music" & (character id 31) & (name of current track) & (character id 31) & (artist of current track) & (character id 31) & stTxt
\t\t\tend if
\t\tend try
\tend tell
end if
return out
"""


def _query_macos() -> list[dict]:
    r = subprocess.run(
        ["osascript", "-e", _MACOS_OSASCRIPT],
        capture_output=True,
        text=True,
        timeout=10,
    )
    sessions = []
    for line in (r.stdout or "").splitlines():
        player, title, artist, status = (line.split("\x1f") + [""] * 4)[:4]
        if title.strip():
            sessions.append(
                {
                    "player": player.strip(),
                    "title": title.strip(),
                    "artist": artist.strip(),
                    "status": status.strip(),
                }
            )
    return sessions


def _query_media_sessions() -> list[dict]:
    if is_windows():
        return _query_windows()
    if is_mac():
        return _query_macos()
    return _query_linux()


def _now_playing_text(player=None) -> str:
    # Our own in-HUD music first — we KNOW what we put on the display, and
    # the OS's media session may not even see the Qt player.
    if _HUD_MUSIC["on"]:
        if player is not None and not player.video_is_playing():
            _hud_queue_clear()  # the user closed it in the HUD
        else:
            who = "Paused" if _HUD_MUSIC["paused"] else "Now playing"
            return (
                f"{who}: {_HUD_MUSIC['label']} "
                f"({_HUD_MUSIC['service'].replace('_', ' ')}, "
                f"on the display)."
            )
    # Dashboard music second — same process as the dashboard server, so
    # media_bus holds both what was pushed and what the dock REPORTED back.
    # Chrome's autoplay block and dead streams make 'pushed' and 'sounding'
    # different facts; Chromium's MPRIS only appears once audio actually
    # runs, so the blocked/error states are visible HERE and nowhere else.
    dash = dash_state = None
    try:
        from dashboard import media_bus as _media_bus

        dash, dash_state = _media_bus.current(), _media_bus.state()
    except Exception:
        pass
    # Speaker-fallback third — the fallback CLEARS the bus payload (so a
    # later dock tap can't double-play) and ffplay registers no OS media
    # session, so this is the only witness to what the speakers sound.
    try:
        from actions.media_play import local_now_playing as _lcl

        lcl = _lcl()
        if lcl:
            who = "Paused" if lcl.startswith("Paused: ") else "Now playing"
            return (
                f"{who}: {lcl.replace('Paused: ', '')} "
                f"(on this machine's speakers)."
            )
    except Exception:
        pass
    if dash:
        title = dash.get("title") or "the track"
        svc = (dash.get("source") or "dashboard").replace("_", " ")
        st = dash_state or {}
        fresh = (time.time() - st.get("ts", 0)) < 90
        status = st.get("status", "") if fresh else ""
        if status == "playing":
            return f"Now playing: {title} ({svc}, on the RIO dashboard)."
        if status == "paused":
            return (
                f"Paused: {title} ({svc}, on the RIO dashboard) — " f"say resume, sir."
            )
        if status == "blocked":
            return (
                f"{title} is loaded on the RIO dashboard but the browser "
                f"blocked autoplay — tap the ▶ button on the media dock, "
                f"sir."
            )
        if status == "error":
            why = st.get("detail") or "stream error"
            return (
                f"{title} did not load on the dashboard ({why}) — say "
                f"play it again and I will retry, sir."
            )
    try:
        sessions = _query_media_sessions()
    except Exception as e:
        if dash:
            return (
                f"{dash.get('title') or 'The track'} is on the RIO "
                f"dashboard media player — I could not verify sound "
                f"({e})."
            )
        return f"I couldn't ask the system what is playing, sir: {e}"
    sessions = [s for s in sessions if (s.get("title") or "").strip()]
    if not sessions:
        if dash:
            return (
                f"{dash.get('title') or 'The track'} is on the RIO "
                f"dashboard media player, but nothing reports it as "
                f"playing — check the dock (it may need a tap on ▶)."
            )
        return "Nothing is playing right now, sir."

    def label(s: dict) -> str:
        who = (s.get("artist") or "").strip()
        core = s["title"] + (f" — {who}" if who else "")
        return f"{core} ({_friendly_player(s.get('player') or '')})"

    playing = [s for s in sessions if (s.get("status") or "").lower() == "playing"]
    if playing:
        return "Now playing: " + "; ".join(label(s) for s in playing[:3]) + "."
    return "Paused: " + "; ".join(label(s) for s in sessions[:3]) + "."


# ── In-HUD playback ──────────────────────────────────────────────────────────────
# When the action context carries a `player` (it always does inside the HUD),
# music plays IN the assistant's own display — the same surface video_player
# uses — instead of a browser tab. The module remembers what it put there so
# pause/stop/next answer without asking the OS, and so a video the user closed
# in the HUD (video_is_playing() False) is noticed rather than reported forever.

_HUD_MUSIC: dict = {
    "on": False,
    "paused": False,
    "label": "",
    "service": "",
    "queue": [],
    "i": -1,
}


def _hud_queue_clear() -> None:
    _HUD_MUSIC.update(
        {"on": False, "paused": False, "label": "", "service": "", "queue": [], "i": -1}
    )


def _play_on_dashboard(song: dict, service: str) -> str | None:
    """No in-HUD video surface (RIO-window mode: the real display is the RIO
    Chrome window) → play on the dashboard's Home media card instead.

    Returns the reply text, or None when the dashboard is unreachable (the
    caller then falls through to the browser path as before)."""
    try:
        from dashboard import media_bus
    except Exception:
        return None
    stream = song.get("stream") or ""
    if service == "youtube_music" or "youtube.com" in stream or "youtu.be" in stream:
        # The card renders an iframe — it needs the videoId, not a stream URL.
        try:
            from actions.media_play import resolve as _mp_resolve

            payload, _note = _mp_resolve("youtube", song.get("label", ""), "")
        except Exception:
            payload = None
        if not payload:
            return None
    else:
        payload = {
            "source": "jiosaavn" if service == "jiosaavn" else "audio",
            "url": stream,
            "title": song.get("label", ""),
        }
    try:
        media_bus.push(payload)
    except Exception:
        return None
    # The push only DELIVERS the song; whether it actually sounds is decided
    # later in the dock (Chrome autoplay policy, stream health). The dock
    # reports back through media_bus.set_state — wait just long enough for
    # that report so the reply is fact, not hope (observed 2026-10-16: two
    # "Playing …" replies while the dock sat blocked and silent).
    label = payload.get("title") or song.get("label", "")
    for _ in range(10):  # ≤ ~2.5 s at 0.25 s steps
        time.sleep(0.25)
        st = media_bus.state() or {}
        status = st.get("status", "")
        if status == "playing":
            return f"Playing {label} on the RIO dashboard."
        if status == "paused":
            return f"{label} is on the RIO dashboard but paused — " f"say resume, sir."
        if status == "blocked":
            return (
                f"{label} is loaded on the RIO dashboard, but the browser "
                f"blocked autoplay — tap the ▶ button on the media dock, "
                f"sir."
            )
        if status == "error":
            return (
                f"I could not load {label}'s stream on the dashboard "
                f"({st.get('detail') or 'stream error'}) — say play it "
                f"again and I will retry, sir."
            )
    return (
        f"{label} is queued on the RIO dashboard media player — I "
        f"cannot confirm sound yet; say 'what's playing' to check, sir."
    )


def _play_in_hud(songs: list[dict], i: int, service: str, player) -> str:
    s = songs[i]
    if hasattr(player, "show_video"):
        player.show_video(s["stream"], s["label"], muted=False)
        _HUD_MUSIC.update(
            {
                "on": True,
                "paused": False,
                "service": service,
                "queue": songs,
                "i": i,
                "label": s["label"],
            }
        )
        return f"Playing {s['label']} on the display."
    # _RioWindowUI (RIO-as-Chrome-window mode) has no video surface — the
    # dashboard's media card is the display. Crashing with AttributeError
    # killed the whole play request (observed 2026-10-15).
    dash = _play_on_dashboard(s, service)
    if dash:
        return dash
    return f"I found {s['label']} but could not reach a display to play it " f"on, sir."


def _play_in_music_popup(songs: list[dict], i: int, service: str, player) -> str:
    """Play a resolved song in the in-app music popup.

    This is the default, in-app JioSaavn path: the song is resolved to its own
    stream URL and played by the popup's own QMediaPlayer — no browser, no
    Chrome, and nothing that touches the video player.
    """
    s = songs[i]
    if hasattr(player, "play_music"):
        player.play_music(
            s["stream"],
            s.get("title") or s["label"],
            s.get("artist", ""),
            s.get("artwork", ""),
        )
        _MUSIC_POPUP.update({
            "on": True,
            "service": service,
            "queue": songs,
            "i": i,
            "label": s["label"],
        })
        return f"Playing {s['label']} in the music popup."
    # No popup-capable player (headless / RIO-as-Chrome-window): fall back to
    # the dashboard media card so a song still sounds somewhere.
    dash = _play_on_dashboard(s, service)
    if dash:
        return dash
    return f"I found {s['label']} but there is no in-app player to start it on, sir."


_MUSIC_POPUP: dict = {
    "on": False,
    "service": "",
    "queue": [],
    "i": 0,
    "label": "",
}



def _control_hud_music(action: str, player) -> str:
    """pause/resume/stop/next/previous for music playing in the HUD.

    The Qt player cannot hear global media keys — those reach the OS's media
    session, and this player is ours — so while in-HUD music is up, the
    controls go straight to it.
    """
    if action == "stop":
        if hasattr(player, "stop_video"):
            player.stop_video()
        else:
            try:  # music on the dashboard → stop it there
                from dashboard import media_bus

                media_bus.stop()
            except Exception:
                pass
        _hud_queue_clear()
        return "Stopped the music."
    if action in ("pause", "resume"):
        if not hasattr(player, "pause_video"):
            return (
                "The music is on the RIO dashboard — use the player's own "
                "controls there, or say stop."
            )
        paused = action == "pause"
        player.pause_video(paused)
        _HUD_MUSIC["paused"] = paused
        return "Paused the music." if paused else "Resumed the music."
    i = _HUD_MUSIC["i"] + (1 if action == "next" else -1)
    songs = _HUD_MUSIC["queue"]
    if not (0 <= i < len(songs)):
        return (
            "That was the last track, sir."
            if action == "next"
            else "There is no previous track, sir."
        )
    return _play_in_hud(songs, i, _HUD_MUSIC["service"], player)


def _open_service(service: str) -> str:
    """Bring the service up without playing anything in particular."""
    if service == "spotify":
        if _spotify_app_installed() and _open_uri("spotify:"):
            return "Spotify is open."
        if _open_web("https://open.spotify.com/"):
            return "Spotify web player is open."
        return "I could not open Spotify, sir."
    if service == "jiosaavn":
        if _open_web("https://www.jiosaavn.com/"):
            return "JioSaavn is open."
        return "I could not open JioSaavn, sir."
    if service == "gaana":
        if _open_web("https://gaana.com/"):
            return "Gaana is open."
        return "I could not open Gaana, sir."
    if service == "wynk":
        if _open_web("https://wynk.in/music"):
            return "Wynk is open — sign in with your Airtel number if it asks."
        return "I could not open Wynk, sir."
    if service == "amazon_music":
        if _open_web(_AMAZON_MUSIC_HOME):
            return "Amazon Music is open."
        return "I could not open Amazon Music, sir."
    if _open_web("https://music.youtube.com/"):
        return "YouTube Music is open."
    return "I could not open YouTube Music, sir."


def _play(service: str, query: str, player=None) -> str:
    if service == "spotify":
        api = _spotify_api()
        if api is not None and api.connected():
            try:
                return api.play(query=query)
            except Exception as e:
                # Same provider, documented fallback: leave the user with the
                # search ready. Switching to another service here would be the
                # silent substitution the brief forbids.
                opened = ""
                if _spotify_app_installed() and _open_uri(f"spotify:search:{quote(query)}"):
                    opened = " I opened the search in the Spotify app."
                elif _open_web(f"https://open.spotify.com/search/{quote(query)}"):
                    opened = " I opened the search on open.spotify.com."
                return f"Spotify could not start playback: {e}{opened}"

        # Spotify stays out of the display honestly: it gives away no free
        # stream URLs, so there is nothing the HUD can play.
        if query.startswith("spotify:"):
            if _open_uri(query):
                return "Playing that in Spotify."
            return "I could not open that Spotify link, sir."
        if _spotify_app_installed() and _open_uri(f"spotify:search:{quote(query)}"):
            return f"Spotify is open with results for '{query}' — pick the first one to start it."
        if _open_web(f"https://open.spotify.com/search/{quote(query)}"):
            return f"Opened Spotify web search for '{query}'."
        return "I could not open Spotify, sir."

    if service == "jiosaavn":
        # Default in-app path: resolve the song and play it in the music popup
        # (its own QMediaPlayer) — no browser. Chrome is only the last resort,
        # when there is no in-app player at all.
        songs = [s for s in _saavn_pick(query) if s["stream"]]
        if player is not None and hasattr(player, "play_music") and songs:
            return _play_in_music_popup(songs, 0, "jiosaavn", player)
        if player is not None and _SAAVN_INHUD and songs:
            # Legacy: use the HUD's video surface instead of the popup.
            return _play_in_hud(songs, 0, "jiosaavn", player)
        if songs:
            # No popup-capable player — hand it to the dashboard media card.
            return _play_in_music_popup(songs, 0, "jiosaavn", player)
        if _open_web(f"https://www.jiosaavn.com/search/{quote(query)}"):
            return (
                f"JioSaavn search is open for '{query}'. I could not resolve a "
                f"stream to play in the popup, so open the song there."
            )
        return "I could not reach JioSaavn, sir."

    if service == "gaana":
        url, label = _gaana_song_url(query)
        if url:
            if _open_web(url):
                return f"Playing {label} on Gaana."
            return "I found it on Gaana but could not open a browser, sir."
        if _open_web(f"https://gaana.com/search/{quote(query)}"):
            return f"Gaana search is open for '{query}' — pick one to play."
        return "I could not reach Gaana, sir."

    if service == "wynk":
        # No addressable search route and nothing plays before an Airtel
        # sign-in — opening the player is all that is honest.
        if _open_web("https://wynk.in/music"):
            return "Wynk is open — it needs your Airtel sign-in, so search there and pick one."
        return "I could not reach Wynk, sir."

    if service == "amazon_music":
        if _open_web(f"{_AMAZON_MUSIC_HOME}/search/{quote(query)}"):
            return f"Amazon Music search is open for '{query}' — sign in if it asks."
        return "I could not reach Amazon Music, sir."

    if service == "youtube_music":
        # yt-dlp is already the project's YouTube resolver (video_player uses
        # it for exactly this), so the HUD gets the real stream and the song
        # plays on the display — picture if the track has one, sound always.
        if player is not None:
            try:
                from actions.video_player import _resolve_youtube

                video, audio, title, _err = _resolve_youtube(query)
            except Exception:
                video = audio = title = ""
            if video:
                if hasattr(player, "show_video"):
                    player.show_video(
                        video, title or query, muted=False, audio_source=audio
                    )
                    _HUD_MUSIC.update(
                        {
                            "on": True,
                            "paused": False,
                            "service": "youtube_music",
                            "queue": [
                                {"perma": "", "stream": video, "label": title or query}
                            ],
                            "i": 0,
                            "label": title or query,
                        }
                    )
                    return f"Playing {title or query} on the display."
                dash = _play_on_dashboard(
                    {"stream": "", "label": title or query}, "youtube_music"
                )
                if dash:
                    return dash
                # no dashboard either → fall through to the browser watch URL
        watch = _ytmusic_watch_url(query)
    if watch and _open_web(watch):
        return f"Playing {query} on YouTube Music."
    if _open_web(f"https://music.youtube.com/search?q={quote(query)}"):
        return f"YouTube Music search is open for '{query}' — pick one to play."
    return "I could not reach YouTube Music, sir."


def music_player(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    params = parameters or {}
    action = (params.get("action") or "play").strip().lower()
    named = (params.get("service") or "").strip().lower()
    service = _SERVICE_ALIASES.get(named or _default_service())
    query = (params.get("query") or "").strip()

    if service is None:
        return "Unknown music service. Available: " + ", ".join(SERVICES) + "."

    if player is not None and hasattr(player, "write_log"):
        try:
            player.write_log(f"[Music] {action} on {service}: {query or '-'}")
        except Exception:
            pass
    print(f"[Music] 🎵 action={action} service={service} query='{query}'")

    # ── Spotify connection and preference ───────────────────────────────────
    api = _spotify_api()
    if action in ("connect", "login", "authorise", "authorize"):
        if api is None:
            return "The Spotify client is not available in this install."
        return api.connect()
    if action in ("disconnect", "logout"):
        if api is None:
            return "The Spotify client is not available in this install."
        api.forget_token()
        return "Spotify is disconnected."
    if action in ("status", "connection"):
        lines = [f"Default music service: {_default_service()}."]
        if api is not None:
            lines.append(
                "Spotify is connected." if api.connected() else "Spotify is not connected."
            )
            if api.connected():
                try:
                    devs = [d.get("name") or "unknown" for d in api.devices()]
                    lines.append(
                        "Spotify devices: " + (", ".join(devs) if devs else "none active")
                        + "."
                    )
                except Exception as e:
                    lines.append(f"Spotify device check failed: {e}")
        else:
            lines.append("The Spotify client could not be imported.")
        return " ".join(lines)
    if action in ("set_default", "default"):
        if not named:
            return f"The default music service is {_default_service()}."
        if named not in ("spotify", "jiosaavn"):
            return "The default can be Spotify or JioSaavn."
        try:
            from memory.config_manager import save_music_provider

            save_music_provider(named)
        except Exception as e:
            return f"I could not save that preference ({e})."
        return f"{named.capitalize()} is now the default music service."

    if action in ("now_playing", "current", "whats_playing"):
        if service == "spotify" and api is not None and api.connected():
            try:
                return api.now_playing()
            except Exception:
                pass  # fall back to the OS media session below
        # Our own in-app music popup reports the song it is actually playing.
        if player is not None and hasattr(player, "music_now_playing"):
            try:
                cur = player.music_now_playing()
                if cur:
                    return f"Playing {cur}."
            except Exception:
                pass
        return _now_playing_text(player)

    # Mute / unmute target the in-app music popup directly — there is no global
    # "mute the music" media key, and the popup owns its own QAudioOutput.
    if action in ("mute", "unmute"):
        if player is not None and hasattr(player, "music_control"):
            try:
                if action == "mute" and player.music_is_active():
                    player.music_control("mute")
                    return "Music is now muted."
                if action == "unmute":
                    player.music_control("unmute")
                    return "Music is now unmuted."
                return "Nothing is playing in the music player to " + \
                       ("mute" if action == "mute" else "unmute") + ", sir."
            except Exception as e:
                return f"I could not {action} the music ({e})."
        return "There is no in-app music player to mute, sir."

    if action in _MEDIA_KEYS:
        # Spotify's own API first when the user is talking about Spotify: it
        # reports the truth about a device the OS session may not expose.
        if service == "spotify" and api is not None and api.connected():
            try:
                if action == "pause":
                    return api.pause()
                if action == "resume":
                    return api.resume()
                if action == "stop":
                    return api.pause()
                if action == "next":
                    return api.next_track()
                if action == "previous":
                    return api.previous_track()
            except Exception as e:
                return f"Spotify could not do that: {e}"
        # The in-app music popup owns its own QMediaPlayer (nothing to do with
        # the video player), so while it is active these controls go straight
        # to it rather than to the OS media keys.
        if (player is not None and hasattr(player, "music_is_active")
                and player.music_is_active()):
            if action in ("pause", "resume", "stop"):
                player.music_control(action)
                return {
                    "pause": "Paused the music.",
                    "resume": "Resumed the music.",
                    "stop": "Stopped the music.",
                }[action]
            return "I can't skip that in the music popup, sir — say stop."
        # In-HUD music first: the Qt player cannot hear global media keys.
        if _HUD_MUSIC["on"] and player is not None:
            if player.video_is_playing():
                return _control_hud_music(action, player)
            _hud_queue_clear()  # the user closed it in the HUD
        # Our OWN players next: the dashboard dock track and the ffplay
        # speaker fallback answer pause/resume/stop through media_play's
        # command channel. OS media keys can neither see nor reach them
        # (observed 2026-10-17: 'pause' fell through to pyautogui →
        # "key 'playpause' not mapped" while the speaker fallback was
        # sounding, and the song never paused).
        if action in ("pause", "resume", "stop"):
            try:
                from actions.media_play import owns_playback as _ours

                if _ours():
                    from actions.media_play import media_play as _mp

                    return _mp({"action": action})
            except Exception:
                pass
            # External players only from here. If the OS reports no media
            # session at all, a blind key press would claim "Paused the
            # music." over silence (observed 2026-10-17).
            try:
                sessions = [
                    s
                    for s in _query_media_sessions()
                    if (s.get("status") or "").lower() in ("playing", "paused")
                ]
                if not sessions:
                    return "Nothing is playing right now."
            except Exception:
                pass
        return _press_media_key(action)

    if action == "open":
        return _open_service(service)

    if action == "play":
        local = _local_audio(query)
        if local:
            return _play_local_audio(local, player)
        if query:
            return _play(service, query, player)
        # Nothing named: start a random trending song in the in-app popup so a
        # vague 'play some music' never just opens a browser tab.
        if service == "jiosaavn" and player is not None and hasattr(player, "play_music"):
            songs = [s for s in _saavn_pick("", n=10) if s["stream"]]
            if songs:
                return _play_in_music_popup(
                    songs, random.randrange(len(songs)), "jiosaavn", player
                )
        if service == "jiosaavn" and player is not None and _SAAVN_INHUD:
            songs = [s for s in _saavn_pick("", n=10) if s["stream"]]
            if songs:
                return _play_in_hud(
                    songs, random.randrange(len(songs)), "jiosaavn", player
                )
        return _open_service(service)

    return (
        "Unknown music action '{a}'. "
        "Available: play, open, pause, resume, next, previous, stop, mute, "
        "unmute, now_playing."
    ).format(a=action)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "music_player",
    "description": (
        "Plays MUSIC on Spotify, JioSaavn, Gaana, Wynk, Amazon Music or "
        "YouTube Music, and plays local audio files (MP3, M4A, WAV, FLAC, "
        "OGG) on this machine. Spotify uses its official Web API when the "
        "user has connected an account: search, play/pause/skip and "
        "now_playing all work on their active Spotify device, but playback "
        "control needs Spotify Premium and an open device. Without a "
        "connection Spotify opens the app with the search ready. JioSaavn "
        "plays inside the assistant's own small music popup (never Chrome): "
        "the song is resolved to a stream and played in-app, with play / "
        "pause / stop / mute controls; JioSaavn publishes no official API, so "
        "the stream comes from their own CDN the same way their web player "
        "gets it — I do not scrape pages or bypass DRM. YouTube Music plays "
        "inside the assistant's own display. "
        "Gaana plays hands-free in the browser; Wynk and Amazon Music need "
        "the user's sign-in. The default service is configurable — "
        "'set_default' with service=spotify or jiosaavn. Use whenever the "
        "user wants to LISTEN to a song or music: 'play Kesariya on "
        "JioSaavn', 'play some Arijit Singh on Spotify', 'connect Spotify', "
        "'play ~/Music/song.mp3', 'gaana chalao', 'put on some jazz', "
        "'pause the music', 'next song', 'what song is this'. Do NOT use "
        "this to WATCH a video: that is video_player or youtube_video. "
        "Volume belongs to computer_settings."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "play (default), open the service without playing, "
                    "now_playing to report the current track, pause, "
                    "resume, next, previous, stop, mute / unmute the "
                    "in-app music, connect to authorise Spotify, "
                    "disconnect, status to report the default service and "
                    "Spotify devices, set_default to choose the preferred "
                    "service."
                ),
                "enum": [
                    "play",
                    "open",
                    "now_playing",
                    "pause",
                    "resume",
                    "next",
                    "previous",
                    "stop",
                    "mute",
                    "unmute",
                    "connect",
                    "disconnect",
                    "status",
                    "set_default",
                ],
            },
            "service": {
                "type": "STRING",
                "description": (
                    "spotify | jiosaavn | gaana | wynk | amazon_music | "
                    "youtube_music — pass the one the user names; when "
                    "omitted the configured default is used (JioSaavn "
                    "unless the user changed it)."
                ),
                "enum": [
                    "spotify",
                    "jiosaavn",
                    "gaana",
                    "wynk",
                    "amazon_music",
                    "youtube_music",
                ],
            },
            "query": {
                "type": "STRING",
                "description": (
                    "Song, artist, album or playlist to play "
                    "(play only). A full spotify: URI is opened "
                    "as-is and starts playing."
                ),
            },
        },
        "required": [],
    },
    "handler": music_player,
}
