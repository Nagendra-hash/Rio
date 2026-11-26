"""media_play.py — play video/music on the RIO dashboard from chat.

"Play <song> on the dashboard" / "open this YouTube video on the UI" →
resolve the request into a PLAYABLE payload and hand it to the dashboard's
media bus (dashboard/media_bus.py); the RIO Home tab's media card renders
the right player:

- youtube  → embedded iframe player (videoId)
- jiosaavn → HTML5 <audio> (direct 320 kbps mp3 URL via the saavn.dev API)
- spotify  → embedded player (track/album/playlist/episode id). Full
             playback needs the user's browser Spotify session; otherwise
             the embed plays the official 30 s preview — stated honestly.
- direct URL to an .mp3/.m4a/.wav → HTML5 <audio>

Query-only Spotify search needs API credentials; when config lacks them the
lookup falls back to JioSaavn and SAYS so — never a silent substitution.
stdlib only (requests is already a project dependency).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

_TIMEOUT_S = 15
_YTDLP_TIMEOUT_S = 45

# The old JARVIS played music in its own HUD player (player.show_video) —
# sound never depended on a browser. RIO-window mode has no HUD, so when the
# dashboard dock cannot confirm playback (Chrome autoplay block, dead stream,
# page not open) we fall back to THIS: ffplay on the machine's own speakers.
_LOCAL_PROC: "subprocess.Popen | None" = None
_LOCAL_PAUSED = False
_LOCAL_TITLE = ""                # what the speaker fallback is sounding

_YT_ID_RE = re.compile(
    r"(?:[?&]v=|youtu\.be/|/embed/|/shorts/|/live/)([A-Za-z0-9_-]{11})")
_SPOT_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]{2}/)?(track|album|playlist|episode)"
    r"/([A-Za-z0-9]+)|spotify:(track|album|playlist|episode):([A-Za-z0-9]+)")


def _decrypt_saavn_url(enc_b64: str) -> str:
    """JioSaavn's encrypted_media_url → playable 320 kbps CDN link.

    The official api.php serves the media URL DES-ECB encrypted with the
    well-known static key '38346591' (what every community mirror does
    server-side — they all died, so the bridge does it locally now).
    TripleDES with an 8-byte key IS DES; PKCS padding is stripped."""
    try:
        try:    # newer cryptography moved DES to decrepit
            from cryptography.hazmat.decrepit.ciphers.algorithms import \
                TripleDES
        except Exception:
            from cryptography.hazmat.primitives.ciphers.algorithms import \
                TripleDES
        from cryptography.hazmat.primitives.ciphers import Cipher, modes
        import base64
        dec = Cipher(TripleDES(b"38346591"), modes.ECB()).decryptor()
        data = dec.update(base64.b64decode(enc_b64)) + dec.finalize()
        if data and 1 <= data[-1] <= 8:
            data = data[:-data[-1]]           # PKCS#7 padding
        url = data.decode("utf-8", "ignore").rstrip("\x00").strip()
        if url.startswith("http"):
            return url.replace("_96.mp4", "_320.mp4")
    except Exception:
        pass
    return ""


def saavn_search_many(query: str, n: int = 8) -> list[dict]:
    """Up to n playable songs from JioSaavn's own api.php — no mirror.

    Every result carries the DECRYPTED 320 kbps CDN URL (the DES key is in
    JioSaavn's own player), so the RIO controller can play straight from
    JioSaavn with zero third-party services. [] when unreachable."""
    import requests
    out: list[dict] = []
    try:
        r = requests.get(
            "https://www.jiosaavn.com/api.php",
            params={"__call": "search.getResults", "q": query,
                    "_format": "json", "_marker": "0", "ctx": "web6dot0",
                    "n": str(n), "p": "1"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=_TIMEOUT_S)
        for s in (r.json().get("results") or []):
            if not isinstance(s, dict) or not s.get("encrypted_media_url"):
                continue
            url = _decrypt_saavn_url(str(s["encrypted_media_url"]))
            if not url:
                continue
            out.append({
                "source": "jiosaavn", "url": url,
                "title": str(s.get("song") or query)[:120],
                "artist": str(s.get("singers")
                              or s.get("primary_artists") or "")[:80],
                "image": re.sub(r"\d+x\d+\.jpg", "500x500.jpg",
                                str(s.get("image") or "")),
            })
            if len(out) >= n:
                break
    except Exception:
        return out
    return out


def _saavn_search(query: str) -> dict | None:
    """One song from JioSaavn's own api.php — no third-party mirror."""
    hits = saavn_search_many(query, n=5)
    return hits[0] if hits else None


def _kill_local_player() -> None:
    """Stop the fallback speaker player, if one is running."""
    global _LOCAL_PROC, _LOCAL_PAUSED, _LOCAL_TITLE
    proc, _LOCAL_PROC, _LOCAL_PAUSED, _LOCAL_TITLE = \
        _LOCAL_PROC, None, False, ""
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def local_now_playing() -> str:
    """The track the speaker fallback is sounding ('' when none). Now-playing
    reads this because the fallback CLEARS the dock payload — so the bus no
    longer knows what is sounding, and OS media sessions cannot see ffplay."""
    proc = _LOCAL_PROC
    if proc is not None and proc.poll() is None and _LOCAL_TITLE:
        return ("Paused: " if _LOCAL_PAUSED else "") + _LOCAL_TITLE
    return ""


def _pause_local_player(paused: bool) -> bool:
    """SIGSTOP/SIGCONT the ffplay process — a real pause for the speaker
    fallback (the old HUD player's pause, minus the Qt surface)."""
    global _LOCAL_PAUSED
    proc = _LOCAL_PROC
    if proc is None or proc.poll() is not None:
        return False
    try:
        import signal
        proc.send_signal(signal.SIGSTOP if paused else signal.SIGCONT)
        _LOCAL_PAUSED = paused
        return True
    except Exception:
        return False


def owns_playback() -> bool:
    """True when OUR players — the dashboard dock track or the ffplay
    speaker fallback — are what would answer pause/resume/stop.

    music_player consults this and delegates to media_play instead of
    firing OS media keys those players can neither see nor hear (observed
    2026-10-17: 'pause' fell through to pyautogui → "key 'playpause' not
    mapped" while the speaker fallback was sounding)."""
    try:
        from dashboard import media_bus
        if media_bus.current():
            return True
    except Exception:
        pass
    return _LOCAL_PROC is not None and _LOCAL_PROC.poll() is None


def _pulse_sink(device: str) -> str:
    """Match a friendly device name ('jbl', 'headphones') to a PulseAudio
    sink name. '' when nothing matches — the caller says so honestly."""
    try:
        out = subprocess.run(["pactl", "list", "short", "sinks"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""
    want = (device or "").strip().lower()
    if not want:
        return ""
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and (want in parts[0].lower()
                                or want in parts[1].lower()):
            return parts[0]
    return ""


def _play_on_speakers(url: str, device: str = "") -> str:
    """Play one direct audio URL through ffplay on this machine's speakers
    (or a named PulseAudio sink, e.g. the JBL).

    Returns '' on a healthy start, else a short reason. This is the
    old-HUD-guarantee: the browser (RIO window may be closed, unpaired, or
    autoplay-blocked) is no longer a single point of failure for sound.
    """
    global _LOCAL_PROC
    try:
        # The ffmpeg bundle this machine already has ships ffplay next to it,
        # even when nothing named ffplay is on PATH.
        from core.media_prep import ffplay_bin

        ffplay = ffplay_bin()
    except Exception:
        ffplay = ""
    ffplay = ffplay or shutil.which("ffplay")
    if not ffplay:
        return "no local player (ffplay) is installed"
    import os
    env = dict(os.environ)
    sink = ""
    if device.strip():
        sink = _pulse_sink(device)
        if not sink:
            return (f"no audio device matches '{device.strip()[:40]}' — "
                    f"say devices to hear the options or leave it off for "
                    f"the default speakers")
        env["PULSE_SINK"] = sink
    try:
        _kill_local_player()
        _LOCAL_PROC = subprocess.Popen(
            [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet",
             "-fflags", "nobuffer", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, start_new_session=True)
        time.sleep(0.8)                 # a dead stream fails within ~0.5 s
        if _LOCAL_PROC.poll() is not None:
            _LOCAL_PROC = None
            return "the local player exited immediately (stream refused)"
        return ""
    except Exception as exc:
        _LOCAL_PROC = None
        return f"local playback failed ({exc})"


def _config() -> dict:
    import json
    try:
        return json.loads((Path(__file__).resolve().parent.parent
                           / "config" / "api_keys.json").read_text("utf-8"))
    except Exception:
        return {}


def _extract_youtube_id(url: str) -> str:
    m = _YT_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _yt_search(query: str) -> tuple[str, str]:
    """(videoId, title) for the first hit; ('', '') when nothing found."""
    # yt-dlp is the reliable path when installed.
    ytdlp = shutil.which("yt-dlp") or str(Path.home() / ".local" / "bin" / "yt-dlp")
    try:
        if shutil.which(ytdlp) or Path(ytdlp).exists():
            r = subprocess.run(
                [ytdlp, "--print", "%(id)s|%(title)s", "--no-download",
                 "--default-search", "ytsearch1:", query],
                capture_output=True, text=True, timeout=_YTDLP_TIMEOUT_S,
                env={"HOME": str(Path.home()),
                     "PATH": "/usr/local/bin:/usr/bin:/bin:"
                             + str(Path.home() / ".local" / "bin")})
            if r.returncode == 0 and r.stdout.strip():
                line = r.stdout.strip().splitlines()[0]
                vid, _, title = line.partition("|")
                if len(vid) == 11:
                    return vid, title[:120]
    except Exception:
        pass
    # Fallback: scrape the results page for the first videoId.
    try:
        import requests
        r = requests.get(
            "https://www.youtube.com/results",
            params={"search_query": query},
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                                   "AppleWebKit/537.36 Chrome/120 Safari/537.36",
                     "Accept-Language": "en-US,en;q=0.9"},
            timeout=_TIMEOUT_S)
        m = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', r.text)
        if m:
            t = re.search(r'"title":\{"runs":\[\{"text":"(.+?)"\}', r.text)
            return m.group(1), (t.group(1)[:120] if t else query)
    except Exception:
        pass
    return "", ""


def _spotify_credentials_search(query: str) -> dict | None:
    """Web-API search when the user configured client credentials."""
    cfg = _config()
    cid, secret = (cfg.get("spotify_client_id") or "").strip(), \
                  (cfg.get("spotify_client_secret") or "").strip()
    if not (cid and secret):
        return None
    try:
        import requests
        tok = requests.post("https://accounts.spotify.com/api/token",
                            data={"grant_type": "client_credentials"},
                            auth=(cid, secret), timeout=_TIMEOUT_S).json()
        r = requests.get("https://api.spotify.com/v1/search",
                         params={"q": query, "type": "track", "limit": 1},
                         headers={"Authorization":
                                  f"Bearer {tok.get('access_token', '')}"},
                         timeout=_TIMEOUT_S)
        items = ((r.json().get("tracks") or {}).get("items")) or []
        if not items:
            return None
        t = items[0]
        return {"source": "spotify", "spotifyType": "track",
                "spotifyId": t["id"],
                "title": t.get("name", query)[:120],
                "artist": ", ".join(a["name"] for a in t.get("artists", [])[:3])[:80]}
    except Exception:
        return None


def _spotify_from_url(url: str) -> dict | None:
    m = _SPOT_RE.search(url or "")
    if not m:
        return None
    kind, sid = m.group(1) or m.group(3), m.group(2) or m.group(4)
    return {"source": "spotify", "spotifyType": kind, "spotifyId": sid,
            "title": f"Spotify {kind} {sid}", "artist": ""}


def _canon_source(source: str) -> str:
    """'Jio Saavan' / 'jiosaavan' / 'YT' → the canonical source name.

    The tool description once spelled it 'jiosaavan' and the model believed
    it — resolve() answered "unknown media source 'jiosaavan'" (observed
    2026-10-16). Every romanization of JioSaavan people and models actually
    produce maps to 'jiosaavn' here, and the description now says the
    canonical word too."""
    key = re.sub(r"[^a-z]", "", (source or "").strip().lower())
    if not key or key == "auto":
        return "auto"
    if key in ("youtube", "yt", "youtubemusic"):
        return "youtube"
    if key == "spotify":
        return "spotify"
    if key in ("jiosaavn", "jiosaavan", "jiosaavaan", "jiosavaan",
               "jiosawaan", "saavn", "saavan", "savaan", "saawaan") \
            or "saav" in key:
        return "jiosaavn"
    return key


_AUDIO_EXT = {
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus",
    ".wma", ".aiff", ".aif", ".alac", ".m4b",
}


def local_audio_file(text: str) -> str:
    """An existing audio file on this machine, or ''.

    MP3 and friends are played by the local player rather than pushed to the
    dashboard: a web page served over HTTP cannot read file:// paths, and the
    user's own file needs no network in the first place.
    """
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


def resolve(source: str, query: str, url: str) -> tuple[dict | None, str]:
    """→ (payload | None, human note). Honest: '' note only on success."""
    source = _canon_source(source)
    url = (url or "").strip()
    query = (query or "").strip()

    if url and (yid := _extract_youtube_id(url)):
        return ({"source": "youtube", "videoId": yid, "title": "YouTube video"},
                "")
    if url and (sp := _spotify_from_url(url)):
        return sp, ""
    if url and re.search(r"\.(mp3|m4a|wav|ogg)(\?|$)", url, re.I):
        return ({"source": "audio", "url": url,
                 "title": url.rsplit("/", 1)[-1][:100]}, "")

    if source in ("", "auto"):
        source = "jiosaavn" if query else "youtube"

    if source == "youtube":
        if not query:
            return None, "give me a YouTube link or something to search for"
        vid, title = _yt_search(query)
        if vid:
            return ({"source": "youtube", "videoId": vid,
                     "title": title or query}, "")
        return None, f"no YouTube video found for '{query}'"

    if source == "jiosaavn":
        if not query:
            return None, "tell me the song name to search on JioSaavan"
        hit = _saavn_search(query)
        if hit:
            return hit, ""
        return None, f"JioSaavan search found nothing for '{query}'"

    if source == "spotify":
        if url:
            return None, "could not read that Spotify link"
        hit = _spotify_credentials_search(query) if query else None
        if hit:
            return hit, ""
        hit = _saavn_search(query) if query else None
        if hit:
            hit["note"] = ("no Spotify credentials in config — resolved via "
                           "JioSaavn (full song, not a 30 s preview)")
            return hit, ""
        return None, (f"could not resolve '{query}' on Spotify (set "
                      f"spotify_client_id/secret in config for direct search)")

    return None, f"unknown media source '{source}'"


def media_play(parameters: dict, player=None) -> str:
    global _LOCAL_TITLE
    parameters = parameters or {}
    action = str(parameters.get("action") or "play").strip().lower()
    if action == "start":                       # voice alias: "start the song"
        action = "play"

    if action == "stop":
        _kill_local_player()            # fallback speaker player, if any
        try:
            from dashboard import media_bus
            media_bus.stop()
            return "Media stopped on the dashboard."
        except Exception as exc:
            return f"Could not stop the dashboard player: {exc}"

    if action == "devices":
        try:
            out = subprocess.run(["pactl", "list", "short", "sinks"],
                                 capture_output=True, text=True,
                                 timeout=5).stdout
        except Exception as exc:
            return f"Could not list audio devices: {exc}"
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                rows.append(f"• {parts[1]}  (id: {parts[0]})")
        return ("Audio output devices: " + "; ".join(rows)) if rows \
            else "No audio output devices found."

    if action in ("pause", "resume"):
        try:
            from dashboard import media_bus
        except Exception as exc:
            return f"Could not reach the media controller: {exc}"
        cur = media_bus.current()
        local_paused = _pause_local_player(action == "pause")
        if not local_paused and not cur:
            return "Nothing is playing right now."
        what = ((cur or {}).get("title") or _LOCAL_TITLE or "the track")
        st = media_bus.state() or {}
        if not local_paused and st.get("status") == \
                ("paused" if action == "pause" else "playing"):
            return (f"{what} is already "
                    f"{'paused' if action == 'pause' else 'playing'}.")
        media_bus.set_cmd(action)
        # Wait for the dock's own report (≤2 s) — the pause/resume command
        # only matters if a LIVE player applied it; an old page that ignores
        # the command must not earn a "Paused." reply (observed 2026-10-17:
        # a stale page swallowed the pause and the song kept sounding).
        want = "paused" if action == "pause" else "playing"
        dock_ok = False
        for _ in range(8):
            time.sleep(0.25)
            s2 = media_bus.state() or {}
            if s2.get("status") == want and time.time() - s2.get("ts", 0) < 5:
                dock_ok = True
                break
        if dock_ok and not local_paused:
            return f"{'Paused' if action == 'pause' else 'Resumed'} {what}."
        if local_paused:
            where = "the speaker player" + (" and the dashboard player" if dock_ok else "")
            return (f"{'Paused' if action == 'pause' else 'Resumed'} "
                    f"{what} on {where}.")
        return (f"The {'pause' if action == 'pause' else 'resume'} command "
                f"reached the dashboard, but its player did not confirm — "
                f"the RIO page may need a refresh. Say stop to halt "
                f"{what} regardless.")

    # A local audio file — including MP3 — goes to the local player. There is
    # nothing to resolve and nothing to download.
    local = local_audio_file(
        str(parameters.get("url") or parameters.get("query") or "")
    )
    if local:
        why = _play_on_speakers(local, str(parameters.get("device") or ""))
        if why:
            return f"I could not play {Path(local).name}: {why}."
        _LOCAL_TITLE = Path(local).name
        return f"Playing {Path(local).name} on the speakers."

    payload, note = resolve(str(parameters.get("source") or "auto"),
                            str(parameters.get("query") or ""),
                            str(parameters.get("url") or ""))
    if payload is None:
        return f"Couldn't play that. {note}"

    try:
        from dashboard import media_bus
        media_bus.push(payload)
    except Exception as exc:
        return f"Resolved '{payload.get('title')}' but the dashboard is not reachable ({exc}). Start JARVIS and try again."

    what = payload.get("title") or payload.get("videoId") or "media"
    src = payload.get("source", "media")
    extra = f" ({payload['note']})" if payload.get("note") else ""

    # The push only DELIVERS the song; whether it actually sounds is decided
    # later in the dock (Chrome autoplay policy, stream health). The dock
    # reports back through media_bus.set_state — wait just long enough for
    # that report so the reply is fact, not hope (observed 2026-10-16:
    # "Playing on the dashboard media player: Kesariya" while the dock sat
    # blocked and silent). YouTube/Spotify render as iframes, which cannot
    # report — only <audio> payloads (jiosaavn / direct mp3) get the wait.
    if payload.get("source") in ("jiosaavn", "audio") and payload.get("url"):
        status = ""
        for _ in range(10):                   # ≤ ~2.5 s at 0.25 s steps
            time.sleep(0.25)
            st = media_bus.state() or {}
            status = st.get("status", "")
            if status == "playing":
                return (f"Playing on the dashboard media player: "
                        f"{what} [{src}]{extra}")
            if status == "paused":
                return (f"{what} is on the dashboard media player but paused "
                        f"— press play on the dock to resume.")
            if status in ("blocked", "error"):
                break                          # dock can't/won't sound it
            # "" = no report YET — keep waiting for the dock's verdict.
        # The dock did not confirm sound. The old JARVIS never had this
        # failure mode — its HUD player WAS the speaker. Restore that
        # guarantee: clear the dock (so a later tap can't double-play) and
        # play the stream on this machine's speakers.
        try:
            media_bus.stop()
        except Exception:
            pass
        dock_why = (st.get("status") or "no report from the RIO page")
        why = _play_on_speakers(payload["url"],
                                str(parameters.get("device") or ""))
        if not why:
            _LOCAL_TITLE = what            # now_playing must know it
            sink = str(parameters.get("device") or "").strip()
            where = (f"the '{sink}' speaker" if sink
                     else "this machine's speakers")
            return (f"Playing {what} on {where} — the dashboard player did "
                    f"not start ({dock_why}).{extra}")
        return (f"Could not play {what} — the dashboard player did not "
                f"start ({dock_why}) and the speaker fallback failed: {why}")

    return f"Playing on the dashboard media player: {what} [{src}]{extra}"


TOOL = {
    "name": "media_play",
    "description": (
        "Play music or video on the RIO dashboard (Home tab media player). "
        "Sources: 'youtube' (search or URL), 'jiosaavn' (song search, full "
        "MP3 stream — spell it exactly jiosaavn), 'spotify' (track/album/"
        "playlist URL, or search when credentials are configured), or a "
        "direct mp3 URL. Also 'action': 'stop' halts playback. Use when the "
        "user asks to play a song, video, or music on the dashboard/screen. "
        "If the dashboard player cannot start (browser autoplay block, dead "
        "stream, RIO page closed), jiosaavn/mp3 songs automatically fall "
        "back to playing on this machine's speakers — the reply always says "
        "which one is actually sounding."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "source": {
                "type": "STRING",
                "description": "'youtube' | 'jiosaavn' | 'spotify' | 'auto'."
            },
            "query": {
                "type": "STRING",
                "description": "What to search for, e.g. a song name or video topic."
            },
            "url": {
                "type": "STRING",
                "description": "Direct YouTube/Spotify/mp3 URL — wins over query."
            },
            "action": {
                "type": "STRING",
                "description": "'play' or 'start' (begin playback), 'pause', "
                               "'resume', 'stop', or 'devices' (list audio "
                               "output devices)."
            },
            "device": {
                "type": "STRING",
                "description": "Optional output device for speaker playback, "
                               "matched to a PulseAudio sink, e.g. 'jbl', "
                               "'headphones'. Use 'devices' action to list. "
                               "Omit for the default speakers."
            }
        }
    },
    "handler": media_play,
}
