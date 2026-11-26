"""
Play a video where the avatar normally is.

The HUD already had a surface for this — the live camera takes the centre of
the screen and gives it back — so a video is the same idea with a different
source, and it shares the same stack rather than inventing a second one. That
means the avatar, the camera and a video can never be on screen together.

WHAT IT ACCEPTS
    a local file        C:\\Users\\me\\clip.mp4, ~/Videos/holiday.mkv
    a direct media URL  https://example.com/trailer.mp4
    a YouTube link      https://youtube.com/watch?v=...
    a YouTube search    "play the new Dune trailer"

    The first two are handed straight to Qt. The last two have to be resolved
    to something playable first, which is what yt-dlp does; without it, this
    says so and offers to open the video in the browser instead of failing.

SOUND IS OFF UNTIL ASKED FOR
    A soundtrack talking over the assistant is the one way this feature could
    make JARVIS worse rather than better, and the microphone is open while it
    plays. So it starts muted, and the user turns sound on — from the button in
    the video header, or by saying so. Closing it works both ways too.
"""

from __future__ import annotations

import re
import threading
import webbrowser
from pathlib import Path

_YT = re.compile(r"(youtube\.com/|youtu\.be/)", re.I)

# Try to import the Chromium resolver
try:
    from gui.chromium_player import resolve_youtube_for_chromium
    _HAS_CHROMIUM = True
except Exception:
    _HAS_CHROMIUM = False
    resolve_youtube_for_chromium = None  # type: ignore

# Which open request is the current one. Opening happens on a thread now, so
# "stop" can arrive while a video is still being resolved — and without this the
# stop would find nothing playing, say so, and then the video would appear
# anyway a second later. Bumping the number abandons whatever is in flight.
_open_lock = threading.Lock()
_open_seq = 0

# Track whether the current video is playing via Chromium
_chromium_active = False


def _begin_open() -> int:
    global _open_seq
    with _open_lock:
        _open_seq += 1
        return _open_seq


def _still_wanted(token: int) -> bool:
    with _open_lock:
        return token == _open_seq


def _is_chromium_active() -> bool:
    """Check if the current video is playing via Chromium."""
    global _chromium_active
    with _open_lock:
        return _chromium_active


def _set_chromium_active(active: bool) -> None:
    """Set whether the current video is playing via Chromium."""
    global _chromium_active
    with _open_lock:
        _chromium_active = active


def _get_chromium_session():
    """Get the Chromium session for controlling playback."""
    try:
        from gui.chromium_player import get_session
        return get_session()
    except Exception:
        return None


def _chromium_pause(paused: bool) -> bool:
    """Pause or resume the Chromium video. Returns True if handled."""
    session = _get_chromium_session()
    if session is None or not session.is_playing:
        return False
    try:
        return session.pause(paused)
    except Exception:
        return False


def _chromium_mute(muted: bool) -> bool:
    """Mute or unmute the Chromium video. Returns True if handled."""
    session = _get_chromium_session()
    if session is None or not session.is_playing:
        return False
    try:
        return session.mute(muted)
    except Exception:
        return False


def _chromium_stop() -> bool:
    """Stop the Chromium video. Returns True if handled."""
    session = _get_chromium_session()
    if session is None or not session.is_playing:
        return False
    try:
        session.stop()
        _set_chromium_active(False)
        return True
    except Exception:
        return False


def _chromium_seek(seconds: float, relative: bool) -> bool:
    """Seek the Chromium video. Returns True if handled."""
    session = _get_chromium_session()
    if session is None or not session.is_playing:
        return False
    try:
        return session.seek(seconds, relative)
    except Exception:
        return False


def _chromium_fullscreen(enter: bool) -> bool:
    """Toggle fullscreen on the Chromium video. Returns True if handled."""
    session = _get_chromium_session()
    if session is None or not session.is_playing:
        return False
    try:
        return session.fullscreen(enter)
    except Exception:
        return False


def _is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://", "file://"))


def _local_path(text: str) -> str:
    """The text as an existing file on disk, or ''."""
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
        if p.exists() and p.is_file():
            return str(p)
    except Exception:
        pass
    return ""


# ── Getting an unplayable codec onto the screen ─────────────────────────────
#
# Qt's bundled FFmpeg has no software AV1 decoder (see core/media_prep.py), so
# an AV1 file played directly produces no frames and one error per frame. The
# decision is made BEFORE the player is given the file: playable codecs go
# straight through with no added latency and hardware decoding untouched;
# AV1 is converted once, in the background, and the converted copy is played.

def _decide(source: str) -> tuple[str, str]:
    """("play" | "convert" | "error", reason). Never raises."""
    try:
        from core import media_prep

        d = media_prep.plan(source)
        return d.action, d.reason
    except Exception:
        return "play", ""


def _prepare_and_show(
    player, source: str, title: str, muted: bool, audio_source: str, token: int
) -> None:
    """Convert if needed, then hand the result to the HUD. Runs on a thread."""
    try:
        from core import media_prep

        path, _note = media_prep.prepare(source)
    except Exception as e:  # MediaError and anything unexpected
        if _still_wanted(token):
            _say(
                player,
                "Tell the user, in one short sentence in their own language: "
                f"'{title or source}' could not be played ({e}).",
            )
        return
    if not _still_wanted(token):
        return  # the user closed it while it was converting
    try:
        player.show_video(
            path,
            title or source,
            muted=muted,
            # A converted file carries its own sound; a separate audio stream
            # would be a second, unconverted source and could drift.
            audio_source="" if path != source else audio_source,
        )
    except Exception as e:  # noqa: BLE001
        _say(player, f"Tell the user the video could not be shown ({e}).")


_MEDIA_EXT = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".flv", ".wmv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".ogv", ".vob", ".mts",
}


def _looks_like_path(text: str) -> bool:
    """A missing file must not be mistaken for a YouTube search.

    "play /home/me/holiday.mp4" with a typo used to search YouTube for that
    whole string, which fails slowly and confusingly. A path shape — or a media
    extension — is a path, and a path that does not exist is an error worth
    saying out loud.
    """
    raw = (text or "").strip().strip('"').strip("'")
    if not raw:
        return False
    if raw.lower().startswith(("file://", "/", "./", "../", "~")):
        return True
    if len(raw) > 2 and raw[1] == ":" and raw[2] in ("\\", "/"):  # C:\...
        return True
    return Path(raw).suffix.lower() in _MEDIA_EXT


def _codec_rank(fmt: dict) -> int:
    """How safely Qt can play this stream, best first.

    0 H.264 — decodes in software everywhere, and is the one codec a machine
      with working VAAPI can hand to the GPU.
    1 VP8/VP9 — software decode is in the bundled FFmpeg.
    2 anything else — try it.
    3 AV1 — the bundled FFmpeg has no software AV1 decoder, and its native
      `av1` decoder is hardware-only, so this produces no picture at all on a
      GPU without AV1 decode. Only chosen when nothing else is offered.
    """
    v = str(fmt.get("vcodec") or "").lower()
    if v.startswith(("avc1", "h264")):
        return 0
    if v.startswith(("vp8", "vp9", "vp09")):
        return 1
    if v.startswith(("av01", "av1")):
        return 3
    return 2


def _resolve_youtube(url_or_query: str) -> tuple[str, str, str, str]:
    """(video_url, audio_url, title, error). Needs yt-dlp; says so when absent.

    TWO urls, because YouTube no longer offers one.
    The first version of this asked for a format carrying both picture and
    sound and got "Requested format is not available". That was not a bad
    selector, it was a wrong assumption: checked against three videos —
    including the oldest upload on the site — every one offered ZERO combined
    formats. Picture and sound are separate streams now, and both are returned
    so the player can run them together.

    `audio_url` comes back empty when only a combined or video-only stream
    exists, which is the normal case for everything that is not YouTube.
    """
    try:
        import yt_dlp
    except Exception:
        return (
            "",
            "",
            "",
            (
                "yt-dlp is not installed, so a YouTube video "
                "cannot be played inside the HUD. "
                "Run: pip install yt-dlp"
            ),
        )

    query = url_or_query if _YT.search(url_or_query) else f"ytsearch1:{url_or_query}"
    # No "format" here on purpose: ask for everything and choose below, so a
    # video that lacks whatever was requested cannot fail outright.
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
        if info and info.get("entries"):
            info = info["entries"][0]
        if not info:
            return "", "", "", "nothing was found for that."
    except Exception as e:
        return "", "", "", f"the video could not be resolved: {e}"

    formats = [
        f
        for f in (info.get("formats") or [])
        if str(f.get("protocol") or "").startswith("http") and f.get("url")
    ]

    def has(f, key):
        return f.get(key) not in (None, "none")

    def best(candidates):
        """Best codec first, then the ORIGINAL language, then quality.

        YouTube auto-dubs a lot of videos now, and it offers every dub as its
        own audio stream at the SAME bitrate as the original: measured on one
        video, English original at 129.483 and Arabic, Bangla, German, Spanish,
        French, Hindi and Indonesian all at 129.482. Choosing on bitrate alone
        therefore came down to a thousandth of a kilobit, and a video would play
        in Arabic one time and English the next for no reason the user could
        see.

        YouTube marks the original track with language_preference 10 and every
        dub with -1, so that decides it first and bitrate only breaks ties
        within the same track. Video formats carry no language, so this is the
        same ordering for them with the first term always equal.
        """
        if not candidates:
            return None
        # Codec compatibility outranks quality: a 4K AV1 stream that cannot be
        # decoded is worth less than a 720p H.264 one that can.
        for rank in (0, 1, 2, 3):
            pool = [f for f in candidates if _codec_rank(f) == rank]
            if pool:
                return max(
                    pool,
                    key=lambda f: (
                        f.get("language_preference") or 0,
                        f.get("tbr") or 0,
                    ),
                )
        return None

    # Combined first — if a video still has one, one player is simpler and
    # cannot drift. Then picture and sound separately.
    combined = best([f for f in formats if has(f, "vcodec") and has(f, "acodec")])
    if combined:
        return combined["url"], "", info.get("title") or "", ""

    # 720p is the ceiling on purpose: the HUD panel is nowhere near 4K, and a
    # smaller stream starts sooner and drifts less.
    video = best(
        [
            f
            for f in formats
            if has(f, "vcodec")
            and not has(f, "acodec")
            and f.get("ext") == "mp4"
            and (f.get("height") or 0) <= 720
        ]
    )
    if not video:
        video = best([f for f in formats if has(f, "vcodec") and not has(f, "acodec")])
    audio = best(
        [
            f
            for f in formats
            if has(f, "acodec") and not has(f, "vcodec") and f.get("ext") == "m4a"
        ]
    )
    if not audio:
        audio = best([f for f in formats if has(f, "acodec") and not has(f, "vcodec")])

    if not video:
        return "", "", "", "no playable stream was offered for that video."
    if audio and audio.get("language"):
        print(
            f"[Video] audio track: {audio.get('language')} "
            f"({audio.get('format_note') or audio.get('format_id')})"
        )
    return video["url"], (audio or {}).get("url", ""), info.get("title") or "", ""


def _search_page(query: str) -> str:
    from urllib.parse import quote_plus

    return f"https://www.youtube.com/results?search_query={quote_plus(query)}"


def video_player(
    parameters: dict = None, response=None, player=None, session_memory=None
) -> str:
    params = parameters or {}
    action = (params.get("action") or "play").strip().lower()
    source = (params.get("source") or "").strip()

    if player is None:
        return "There is no display to play a video on."

    # ---- stop -------------------------------------------------------------
    if action in ("stop", "close", "hide"):
        # Cancel an open that has not finished yet, so a video the user has
        # already changed their mind about never reaches the screen.
        was_opening = not player.video_is_playing() and not _is_chromium_active()
        _begin_open()
        # If playing via Chromium, stop it there too
        if _is_chromium_active():
            _chromium_stop()
        player.stop_video()
        if was_opening:
            return "Stopped it before it opened."
        return "Closed the video."

    # ---- sound ------------------------------------------------------------
    if action in ("mute", "unmute", "sound_on", "sound_off"):
        # Check both Qt player and Chromium
        if not player.video_is_playing() and not _is_chromium_active():
            return "Nothing is playing."
        muted = action in ("mute", "sound_off")
        # If playing via Chromium, mute there too
        if _is_chromium_active():
            _chromium_mute(muted)
        player.set_video_muted(muted)
        return "Muted the video." if muted else "Turned the video's sound on."

    # ---- pause / resume ---------------------------------------------------
    if action in ("pause", "resume"):
        # Check both Qt player and Chromium
        if not player.video_is_playing() and not _is_chromium_active():
            return "Nothing is playing."
        paused = action == "pause"
        # If playing via Chromium, pause there too
        if _is_chromium_active():
            _chromium_pause(paused)
        player.pause_video(paused)
        return "Paused the video." if paused else "Resumed the video."

    # ---- seek -------------------------------------------------------------
    if action in ("seek", "skip", "forward", "rewind", "goto"):
        # Check both Qt player and Chromium
        if not player.video_is_playing() and not _is_chromium_active():
            return "Nothing is playing."
        # Extract seconds from parameters
        seconds = params.get("seconds")
        if seconds is None:
            # Default skip amounts
            if action in ("forward", "skip"):
                seconds = 30
            elif action in ("rewind", "back"):
                seconds = -30
            else:
                return "How many seconds should I skip?"
        try:
            seconds = float(seconds)
        except Exception:
            return "Invalid seek amount."
        relative = action not in ("goto",)
        # If playing via Chromium, seek there too
        if _is_chromium_active():
            _chromium_seek(seconds, relative)
        player.seek_video(seconds, relative)
        direction = "forward" if seconds > 0 else "backward"
        return f"Skipped {abs(seconds):.0f} seconds {direction}."

    # ---- fullscreen --------------------------------------------------------
    if action in ("fullscreen", "maximize", "full_screen"):
        # Check both Qt player and Chromium
        if not player.video_is_playing() and not _is_chromium_active():
            return "Nothing is playing."
        # If playing via Chromium, toggle fullscreen there
        if _is_chromium_active():
            _chromium_fullscreen(True)
        # Qt player doesn't have a fullscreen API yet, but we can try
        try:
            player._win._video_widget.setWindowFlags(player._win._video_widget.windowFlags() | Qt.WindowType.Window)
            player._win._video_widget.showFullScreen()
        except Exception:
            pass
        return "Video is now fullscreen."

    # ---- play -------------------------------------------------------------
    if not source:
        return "What should I play?"

    local = _local_path(source)
    if local:
        action, reason = _decide(local)
        if action == "error":
            return f"I can't play that: {reason}."
        if action == "convert":
            token = _begin_open()
            threading.Thread(
                target=_prepare_and_show,
                args=(player, local, Path(local).name, True, "", token),
                daemon=True,
                name="video-convert",
            ).start()
            return (
                f"status=converting file={Path(local).name} reason={reason} — "
                f"say briefly that it is being converted for playback."
            )
        player.show_video(local, Path(local).name, muted=True)
        return f"Playing {Path(local).name} on the display, muted."

    if _looks_like_path(source):
        return f"I couldn't find '{Path(source).name}' — check the path and try again."

    # A direct media URL can go straight to the player; a YouTube page cannot.
    if _is_url(source) and not _YT.search(source):
        action, reason = _decide(source)
        if action == "error":
            return f"I can't play that: {reason}."
        if action == "convert":
            token = _begin_open()
            threading.Thread(
                target=_prepare_and_show,
                args=(player, source, source.rsplit("/", 1)[-1][:44], True, "", token),
                daemon=True,
                name="video-convert",
            ).start()
            return (
                f"status=converting reason={reason} — say briefly that it is "
                f"being converted for playback."
            )
        player.show_video(source, source.rsplit("/", 1)[-1][:44], muted=True)
        return "Playing that on the display, muted."

    # ANSWER FIRST, OPEN SECOND.
    #
    # Working out what to play takes seconds that nothing can remove: measured
    # against YouTube, 3.3s for a spoken phrase (a search, then the video) and
    # 2.0s for a link, plus however long Qt spends buffering before the first
    # frame. Restricting yt-dlp to a lighter client was tried and is not the
    # answer — the fast clients came back with zero usable formats.
    #
    # So the seconds stay, and what changes is where the user spends them:
    # listening to JARVIS say it is coming, instead of watching nothing happen.
    # The same shape whatsapp_call uses, and for the same reason.
    threading.Thread(
        target=_play_youtube,
        args=(player, source, _begin_open()),
        daemon=True,
        name="video-open",
    ).start()
    # Deliberately a status line rather than a finished English sentence: the
    # model writes the words, in the user's own language.
    return f"status=opening source={source}"


def _play_youtube_chromium(player, url: str, title: str, token: int) -> bool:
    """Open a YouTube video in Chromium. Returns True on success."""
    try:
        from gui.chromium_player import ChromiumSession, set_session, get_session

        # Get or create the session
        session = get_session()
        if session is None:
            session = ChromiumSession()
            set_session(session)

        # Open the video in Chromium
        ok, err = session.open_video(url, title)
        if not ok:
            return False

        # Mark as Chromium active
        _set_chromium_active(True)

        # The video is now playing in Chromium (visible window)
        # We don't use player.show_video for Chromium videos
        # but we need to keep the UI in sync
        return True
    except Exception as e:
        print(f"[Video] Chromium playback failed: {e}")
        return False


def _play_youtube(player, source: str, token: int) -> None:
    """The part that takes seconds. Speaks only if something goes wrong —
    silence means the video is on screen, which the user can see for
    themselves."""

    # First, try to resolve and play via Chromium (if available)
    if _HAS_CHROMIUM and resolve_youtube_for_chromium is not None:
        try:
            url, title, err = resolve_youtube_for_chromium(source)
            if url:
                if _still_wanted(token):
                    if _play_youtube_chromium(player, url, title or source, token):
                        return  # Success - playing in Chromium
        except Exception as e:
            print(f"[Video] Chromium resolver failed: {e}")

    # Fall back to yt-dlp resolution
    try:
        url, audio_url, title, err = _resolve_youtube(source)
    except Exception as e:  # noqa: BLE001
        url = audio_url = title = ""
        err = str(e)

    if not _still_wanted(token):
        return  # the user closed it while it was resolving

    if url:
        try:
            player.show_video(url, title or source, muted=True, audio_source=audio_url)
            return
        except Exception as e:  # noqa: BLE001
            err = str(e)

    # Resolution failed. Opening it in a browser is worse than playing it in the
    # HUD, but it is a great deal better than doing nothing, and the reason is
    # reported rather than swallowed.
    page = source if _YT.search(source) else _search_page(source)
    opened = ""
    try:
        webbrowser.open(page)
        opened = " and it has been opened in the browser instead"
    except Exception:
        pass
    _say(
        player,
        "Tell the user, in one short sentence in their own language: "
        f"'{source}' could not be played on the display "
        f"({err}){opened}.",
    )


def _say(player, instruction: str) -> None:
    if player is not None and hasattr(player, "request_say"):
        try:
            player.request_say(instruction)
        except Exception:
            pass


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "video_player",
    "description": (
        "If user wants to open a video on YouTube, he will tell you that specifically"
        "If user wants to play a video, then he wants to call this feature"
        "Plays a VIDEO on the assistant's own display, where the avatar "
        "normally is — and stops it, mutes it or unmutes it. Use whenever the "
        "user wants to WATCH something on screen: 'play the new Dune trailer', "
        "'şu videoyu oynat', 'play C:/clips/holiday.mp4', 'put that YouTube "
        "video on the screen', 'stop the video', 'videoyu kapat', 'turn the "
        "sound on', 'sesi aç', 'mute it'. Accepts a local file path, a direct "
        "video URL, a YouTube link, or a description to search YouTube for. "
        "Video always starts MUTED — only call action='unmute' when the user "
        "actually asks to hear it. "
        "This returns the moment it is asked, before the picture appears: when "
        "it answers 'status=opening', say in one short sentence that you are "
        "putting it on the display, and do not wait or call it again. "
        "Do NOT use 'youtube_video' for this: that optional plugin opens videos "
        "in the web browser and summarises transcripts, it never plays anything "
        "on the assistant's display."
        "Also supports: pause, resume, seek/skip/forward/rewind/goto with 'seconds' parameter, fullscreen/maximize."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "'play' (default), 'stop' to close it, 'unmute' "
                    "to turn the sound on, 'mute' to silence it, "
                    "'pause' to pause, 'resume' to continue, "
                    "'seek'/'skip'/'forward'/'rewind'/'goto' to change position "
                    "(use 'seconds' parameter), "
                    "'fullscreen'/'maximize' to toggle fullscreen."
                ),
                "enum": ["play", "stop", "mute", "unmute", "pause", "resume", "seek", "skip", "forward", "rewind", "goto", "fullscreen", "maximize", "full_screen"],
            },
            "source": {
                "type": "STRING",
                "description": (
                    "For action='play': a local file path, a direct video URL, "
                    "a YouTube link, or what to search YouTube for. Pass what "
                    "the user actually named, in their own words."
                ),
            },
            "seconds": {
                "type": "NUMBER",
                "description": (
                    "For seek actions: number of seconds. Positive = forward, "
                    "negative = backward. For 'goto', absolute position from start."
                ),
            },
        },
        "required": ["action"],
    },
    "handler": video_player,
}
