"""
music_player.py — play music on Spotify, JioSaavn, Gaana, Wynk, Amazon
Music or YouTube Music.

Six services, honest paths to sound — none of them invents a
capability the service does not give away for free:

JIOSAAVN plays hands-free with zero setup
    JioSaavn's own site API (the one jiosaavn.com calls from the browser)
    answers a song search without any key, and every result carries its
    song-page URL. Opening that page starts the song in their web player
    immediately — search, resolve, open, and it is playing. If the API is
    unreachable the search page opens instead and the user picks. And a
    vague 'play some music' with no song named still STARTS something: a
    random song from a random editorial chart (two in a row never match),
    because opening a silent player for that request is not an answer.

SPOTIFY opens the app (or the web) with the search ready
    Autoplaying a specific track needs the Web API with OAuth plus a Premium
    account, and this repo deliberately carries no Spotify credentials. What
    needs no credentials is the `spotify:` URI scheme: if the desktop app is
    installed, `spotify:search:<query>` opens it with the results on screen —
    one tap on the first result and it plays. Without the app, the same
    search opens on open.spotify.com. A query that is already a full
    `spotify:track:`/`spotify:album:` URI is opened as-is, and those DO
    start playing the moment the app picks them up.

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

PAUSE / RESUME / NEXT / PREVIOUS ARE GLOBAL MEDIA KEYS, not per-service
    The assistant's microphone stays open while music plays, so any
    per-service control channel would need OAuth for each one and would
    still break the moment a tab lost focus. The keyboard's media keys
    reach whichever app or tab is actually playing — Spotify's desktop app,
    JioSaavn in a browser, YouTube Music — with one code path and no
    credentials. That is also why there is no separate "stop": nothing on
    the keyboard stops, so stop pauses.

INSIDE THE DISPLAY, NOT IN A BROWSER TAB
    A browser tab loses to Chrome's autoplay policy on first visit and
    needs the user to drive it. So when the action context carries a
    `player` — it always does inside the HUD — JioSaavn and YouTube Music
    play on the assistant's own display, the same surface video_player
    uses. JioSaavn ships each song's direct CDN stream DES-encrypted; the
    key is public knowledge (it is in their own player), decryption needs
    pycryptodome and one line, and the HUD's Qt player streams the result
    straight into the panel where the avatar normally is. YouTube Music
    resolves through yt-dlp, which video_player already depends on. The
    HUD closes the microphone while music sounds (its own rule, see
    ui.py's _sync_mic_for_video) and gives it back when the music stops.
    Spotify plays nowhere in-HUD — it gives away no free stream URLs — so
    it keeps opening the app; pretending otherwise would just be lying.

NOW PLAYING READS THE OPERATING SYSTEM, NOT ANY SERVICE
    Asking "what song is this" is answered from the same place the media
    keys act: the system's own media session. On Linux that is MPRIS over
    D-Bus — jeepney first (the pure-Python bus library screen_processor
    already uses for the screenshot portal), then playerctl, then one-shot
    gdbus property reads; every Chromium tab and Firefox instance exposes
    MPRIS, so browser players report too. On Windows it is the System
    Media Transport Controls — the very session the media keys hit — read
    through PowerShell; on macOS Spotify and Music answer over
    AppleScript. No service API, no login, no per-service code.
"""
from __future__ import annotations

import base64
import os
import random
import re
import shutil
import subprocess
import webbrowser
from pathlib import Path
from urllib.parse import quote

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    import pyautogui
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

from config import is_windows, is_mac, is_linux

SERVICES = ("spotify", "jiosaavn", "gaana", "wynk",
            "amazon_music", "youtube_music")

# Default service when the user never names one: they asked for JioSaavn
# specifically, and for everyone else it is the default Indian catalogue.
DEFAULT_SERVICE = "jiosaavn"

_SERVICE_ALIASES = {
    "spotify":        "spotify",
    "jiosaavn":       "jiosaavn",
    "jio saavn":      "jiosaavn",
    "saavn":          "jiosaavn",
    "gaana":          "gaana",
    "wynk":           "wynk",
    "wynk music":     "wynk",
    "amazon_music":   "amazon_music",
    "amazon music":   "amazon_music",
    "amazon":         "amazon_music",
    "prime music":    "amazon_music",
    "youtube_music":  "youtube_music",
    "youtube music":  "youtube_music",
    "yt music":       "youtube_music",
    "ytmusic":        "youtube_music",
}

# action → which key to press. `resume` and `pause` share playpause because a
# toggle is exactly what each of them wants, whichever state the player is in.
_MEDIA_KEYS = {
    "pause":    "playpause",
    "stop":     "playpause",
    "resume":   "playpause",
    "next":     "nexttrack",
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
            os.startfile(uri)                       # type: ignore[attr-defined]
            return True
        cmd = "open" if is_mac() else "xdg-open"
        subprocess.Popen([cmd, uri],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
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
    try:
        from Cryptodome.Cipher import DES
        raw = DES.new(b"38346591", DES.MODE_ECB).decrypt(base64.b64decode(enc))
        pad = raw[-1]
        if 1 <= pad <= 8:
            raw = raw[:-pad]
        return raw.decode("utf-8", "ignore")
    except Exception:
        return ""


def _saavn_label(song: dict) -> str:
    title = (song.get("title") or "").strip()
    artists = [
        (a.get("name") or "").strip()
        for a in (((song.get("more_info") or {}).get("artistMap")
                   or {}).get("primary_artists") or [])[:2]
    ]
    return " — ".join(x for x in (title, ", ".join(a for a in artists if a)) if x)


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
            params = {"__call": "search.getResults", "_format": "json",
                      "_marker": "0", "api_version": "4", "cc": "in",
                      "includeMetaTags": "1", "p": "1", "n": str(n),
                      "q": query}
            r = requests.get(_SAAVN_API, params=params,
                             headers=_HEADERS, timeout=10)
            # The field is a bare list of songs on some deployments and a
            # {"data": [...]} dict on others — accept both.
            payload = r.json().get("results") or []
            songs = payload.get("data") if isinstance(payload, dict) else payload
        else:
            r = requests.get(_SAAVN_API, params={
                "__call": "content.getCharts", "_format": "json",
                "_marker": "0", "api_version": "4", "cc": "in"},
                headers=_HEADERS, timeout=10)
            charts = [c for c in (r.json() or []) if c.get("id")]
            if not charts:
                return []
            chart = random.choice(charts)
            chart_title = (chart.get("title") or "").strip()
            r = requests.get(_SAAVN_API, params={
                "__call": "playlist.getDetails", "_format": "json",
                "_marker": "0", "api_version": "4", "cc": "in",
                "listid": chart["id"]}, headers=_HEADERS, timeout=10)
            songs = r.json().get("list") or []

        picked = []
        for song in (songs or []):
            if song.get("type") != "song" or not song.get("perma_url"):
                continue
            more = song.get("more_info") or {}
            label = _saavn_label(song)
            if not label:
                label = query
            if chart_title and label:
                label = f"{label} from '{chart_title}'"
            stream = ""
            if more.get("encrypted_media_url"):
                stream = _decrypt_saavn_stream(more["encrypted_media_url"])
                if stream and str(more.get("320kbps")) == "true":
                    stream = stream.replace("_96.mp4", "_320.mp4")
            picked.append({"perma": song["perma_url"], "stream": stream,
                           "label": label})
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
        r = requests.get(f"https://gaana.com/search/{quote(query)}",
                         headers=_HEADERS, timeout=12)
        match = re.search(r'href="(/song/[^"]+)"', r.text)
        if match:
            page = "https://gaana.com" + match.group(1)
            label = match.group(1)[len("/song/"):].replace("-", " ").strip()
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
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True}
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
    if not _PYAUTOGUI:
        return ("pyautogui is not installed, so I cannot send media keys. "
                "Run: pip install pyautogui")
    try:
        _press_key(_MEDIA_KEYS[action])
    except Exception as e:
        return f"Could not send the media key, sir: {e}"
    return {
        "pause":    "Paused the music.",
        "stop":     "Paused the music.",
        "resume":   "Resumed the music.",
        "next":     "Skipped to the next track.",
        "previous": "Back to the previous track.",
    }[action]


# ── Now playing: read the OS's own media session ────────────────────────────

_MPRIS_PREFIX = "org.mpris.MediaPlayer2."

_PLAYER_LABELS = {
    "spotify": "Spotify", "firefox": "Firefox", "chromium": "the browser",
    "chrome": "the browser", "google-chrome": "the browser",
    "msedge": "Edge", "brave": "Brave", "vlc": "VLC", "mpv": "mpv",
    "rhythmbox": "Rhythmbox", "strawberry": "Strawberry",
    "celluloid": "Celluloid", "lollypop": "Lollypop",
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
        bus = DBusAddress("/org/freedesktop/DBus",
                          bus_name="org.freedesktop.DBus",
                          interface="org.freedesktop.DBus")
        names = conn.send_and_get_reply(
            new_method_call(bus, "ListNames")).body[0]
        for name in names:
            if not name.startswith(_MPRIS_PREFIX):
                continue
            try:
                props = DBusAddress("/org/mpris/MediaPlayer2",
                                    bus_name=name,
                                    interface="org.freedesktop.DBus.Properties")
                meta = _unwrap_variant(conn.send_and_get_reply(new_method_call(
                    props, "Get", "ss",
                    ("org.mpris.MediaPlayer2.Player", "Metadata"))).body[0])
                status = str(_unwrap_variant(conn.send_and_get_reply(new_method_call(
                    props, "Get", "ss",
                    ("org.mpris.MediaPlayer2.Player", "PlaybackStatus"))).body[0]))
                title, artist = "", ""
                t = _unwrap_variant(meta.get("xesam:title"))
                if t:
                    title = str(t)
                a = _unwrap_variant(meta.get("xesam:artist"))
                if a:
                    artist = ", ".join(str(x) for x in a)
                sessions.append({"player": name[len(_MPRIS_PREFIX):],
                                 "title": title, "artist": artist,
                                 "status": status})
            except Exception:
                continue        # a half-dead player must not sink the rest
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
    r = subprocess.run(["playerctl", "--all-players", "metadata",
                        "--format", fmt],
                       capture_output=True, text=True, timeout=6)
    sessions = []
    for line in (r.stdout or "").splitlines():
        player, title, artist, status = (line.split("\x1f") + [""] * 4)[:4]
        if title.strip():
            sessions.append({"player": player.strip(), "title": title.strip(),
                             "artist": artist.strip(), "status": status.strip()})
    return sessions


def _gunquote(s: str) -> str:
    """Undo GVariant's minimal string escapes. Best effort — it only has to
    survive song titles, which contain quotes and backslashes, not history."""
    return (s.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\"))


def _gdbus(args: list[str]) -> str:
    r = subprocess.run(["gdbus"] + args, capture_output=True, text=True, timeout=6)
    return r.stdout or ""


def _query_linux_gdbus() -> list[dict]:
    """MPRIS via one-shot gdbus property reads. The screenshot portal could
    NOT work this way (its caller must stay on the bus), but a Metadata Get
    replies synchronously, so a short-lived process is fine here."""
    out = _gdbus(["call", "--session", "--dest", "org.freedesktop.DBus",
                  "--object-path", "/org/freedesktop/DBus",
                  "--method", "org.freedesktop.DBus.ListNames"])
    names = re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", out)
    sessions = []
    for name in names:
        try:
            meta = _gdbus(["call", "--session", "--dest", name,
                           "--object-path", "/org/mpris/MediaPlayer2",
                           "--method", "org.freedesktop.DBus.Properties.Get",
                           "org.mpris.MediaPlayer2.Player", "Metadata"])
            status = _gdbus(["call", "--session", "--dest", name,
                             "--object-path", "/org/mpris/MediaPlayer2",
                             "--method", "org.freedesktop.DBus.Properties.Get",
                             "org.mpris.MediaPlayer2.Player", "PlaybackStatus"])
        except Exception:
            continue
        st = re.search(r"<'(\w+)'>", status)
        title_m = re.search(r"'xesam:title':\s*<'((?:[^'\\]|\\.)*)'", meta)
        if not title_m:
            continue
        artist = ""
        artist_m = re.search(r"'xesam:artist':\s*<\[(.*?)\]>", meta)
        if artist_m:
            artist = ", ".join(_gunquote(x) for x in
                               re.findall(r"'((?:[^'\\]|\\.)*)'", artist_m.group(1)))
        sessions.append({"player": name[len(_MPRIS_PREFIX):],
                         "title": _gunquote(title_m.group(1)),
                         "artist": artist,
                         "status": st.group(1) if st else ""})
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
        capture_output=True, text=True, timeout=15)
    sessions = []
    for line in (r.stdout or "").splitlines():
        source, title, artist, status = (line.split("\x1f") + [""] * 4)[:4]
        if title.strip():
            sessions.append({"player": source.strip(), "title": title.strip(),
                             "artist": artist.strip(), "status": status.strip()})
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
    r = subprocess.run(["osascript", "-e", _MACOS_OSASCRIPT],
                       capture_output=True, text=True, timeout=10)
    sessions = []
    for line in (r.stdout or "").splitlines():
        player, title, artist, status = (line.split("\x1f") + [""] * 4)[:4]
        if title.strip():
            sessions.append({"player": player.strip(), "title": title.strip(),
                             "artist": artist.strip(), "status": status.strip()})
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
            _hud_queue_clear()          # the user closed it in the HUD
        else:
            who = "Paused" if _HUD_MUSIC["paused"] else "Now playing"
            return (f"{who}: {_HUD_MUSIC['label']} "
                    f"({_HUD_MUSIC['service'].replace('_', ' ')}, "
                    f"on the display).")
    try:
        sessions = _query_media_sessions()
    except Exception as e:
        return f"I couldn't ask the system what is playing, sir: {e}"
    sessions = [s for s in sessions if (s.get("title") or "").strip()]
    if not sessions:
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

_HUD_MUSIC: dict = {"on": False, "paused": False, "label": "", "service": "",
                    "queue": [], "i": -1}


def _hud_queue_clear() -> None:
    _HUD_MUSIC.update({"on": False, "paused": False, "label": "",
                       "service": "", "queue": [], "i": -1})


def _play_in_hud(songs: list[dict], i: int, service: str, player) -> str:
    s = songs[i]
    player.show_video(s["stream"], s["label"], muted=False)
    _HUD_MUSIC.update({"on": True, "paused": False, "service": service,
                       "queue": songs, "i": i, "label": s["label"]})
    return f"Playing {s['label']} on the display."


def _control_hud_music(action: str, player) -> str:
    """pause/resume/stop/next/previous for music playing in the HUD.

    The Qt player cannot hear global media keys — those reach the OS's media
    session, and this player is ours — so while in-HUD music is up, the
    controls go straight to it.
    """
    if action == "stop":
        player.stop_video()
        _hud_queue_clear()
        return "Stopped the music."
    if action in ("pause", "resume"):
        paused = action == "pause"
        player.pause_video(paused)
        _HUD_MUSIC["paused"] = paused
        return "Paused the music." if paused else "Resumed the music."
    i = _HUD_MUSIC["i"] + (1 if action == "next" else -1)
    songs = _HUD_MUSIC["queue"]
    if not (0 <= i < len(songs)):
        return ("That was the last track, sir." if action == "next"
                else "There is no previous track, sir.")
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
        # IN the assistant's own display when one exists — Chrome's autoplay
        # policy never gets a vote in there. Browser only for headless runs.
        if player is not None:
            songs = [s for s in _saavn_pick(query) if s["stream"]]
            if songs:
                return _play_in_hud(songs, 0, "jiosaavn", player)
            # nothing resolvable → fall through to the browser as before
        url, label = _saavn_song_url(query)
        if url:
            if _open_web(url):
                return f"Playing {label} on JioSaavn."
            return "I found it on JioSaavn but could not open a browser, sir."
        if _open_web(f"https://www.jiosaavn.com/search/{quote(query)}"):
            return f"JioSaavn search is open for '{query}' — pick one to play."
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
                player.show_video(video, title or query, muted=False,
                                  audio_source=audio)
                _HUD_MUSIC.update({"on": True, "paused": False,
                                   "service": "youtube_music",
                                   "queue": [{"perma": "", "stream": video,
                                              "label": title or query}],
                                   "i": 0, "label": title or query})
                return f"Playing {title or query} on the display."
        watch = _ytmusic_watch_url(query)
    if watch and _open_web(watch):
        return f"Playing {query} on YouTube Music."
    if _open_web(f"https://music.youtube.com/search?q={quote(query)}"):
        return f"YouTube Music search is open for '{query}' — pick one to play."
    return "I could not reach YouTube Music, sir."


def music_player(
    parameters:     dict = None,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    params  = parameters or {}
    action  = (params.get("action")  or "play").strip().lower()
    service = _SERVICE_ALIASES.get((params.get("service") or DEFAULT_SERVICE).strip().lower())
    query   = (params.get("query") or "").strip()

    if service is None:
        return ("Unknown music service. Available: "
                + ", ".join(SERVICES) + ".")

    if player is not None and hasattr(player, "write_log"):
        try:
            player.write_log(f"[Music] {action} on {service}: {query or '-'}")
        except Exception:
            pass
    print(f"[Music] 🎵 action={action} service={service} query='{query}'")

    if action in ("now_playing", "current", "whats_playing"):
        return _now_playing_text(player)

    if action in _MEDIA_KEYS:
        # In-HUD music first: the Qt player cannot hear global media keys.
        if _HUD_MUSIC["on"] and player is not None:
            if player.video_is_playing():
                return _control_hud_music(action, player)
            _hud_queue_clear()          # the user closed it in the HUD
        return _press_media_key(action)

    if action == "open":
        return _open_service(service)

    if action == "play":
        if query:
            return _play(service, query, player)
        # Nothing named: for JioSaavn that means START something — a random
        # trending song in the HUD beats both a silent player and a browser
        # tab the user must drive themselves.
        if service == "jiosaavn":
            if player is not None:
                songs = [s for s in _saavn_pick("", n=10) if s["stream"]]
                if songs:
                    return _play_in_hud(songs, random.randrange(len(songs)),
                                        "jiosaavn", player)
            url, label = _saavn_default_song()
            if url and _open_web(url):
                return f"Playing {label}."
        return _open_service(service)

    return ("Unknown music action '{a}'. "
            "Available: play, open, pause, resume, next, previous, stop."
            ).format(a=action)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "music_player",
    "description": (
        "Plays MUSIC on Spotify, JioSaavn, Gaana, Wynk, Amazon Music or "
        "YouTube Music. JioSaavn and YouTube Music play INSIDE the "
        "assistant's own display (no browser, no autoplay blocks) whenever "
        "a display exists; pause / resume / next / previous then control "
        "that playback directly, and now_playing reports its track. Spotify "
        "opens the app with results ready (it gives away no free streams); "
        "Gaana plays hands-free in the browser; Wynk and Amazon Music need "
        "the user's sign-in. Without a display, JioSaavn and Gaana still "
        "start playing in the browser. Use whenever the user wants to "
        "LISTEN to a song or music: 'play Kesariya on JioSaavn', 'play some "
        "Arijit Singh on Spotify', 'gaana chalao', 'put on some jazz', "
        "'pause the music', 'next song', 'what song is this'. Do NOT use "
        "this to WATCH a video: that is video_player or youtube_video. "
        "Volume belongs to computer_settings."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("play (default), open the service without "
                                "playing, now_playing to report the current "
                                "track, pause, resume, next, previous, stop "
                                "(pauses)."),
                "enum": ["play", "open", "now_playing", "pause", "resume",
                         "next", "previous", "stop"],
            },
            "service": {
                "type": "STRING",
                "description": ("spotify (default) | jiosaavn | gaana | "
                                "wynk | amazon_music | youtube_music — "
                                "pass the one the user names."),
                "enum": ["spotify", "jiosaavn", "gaana", "wynk",
                         "amazon_music", "youtube_music"],
            },
            "query": {
                "type": "STRING",
                "description": ("Song, artist, album or playlist to play "
                                "(play only). A full spotify: URI is opened "
                                "as-is and starts playing."),
            },
        },
        "required": [],
    },
    "handler": music_player,
}
