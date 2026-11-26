"""
tests/test_music_player.py — offline tests for actions/music_player.py.

No network, no browser, no media keys: every side channel the handler uses
(_saavn_song_url, _ytmusic_watch_url, _spotify_app_installed, _open_uri,
_open_web, _press_key) is monkeypatched, so the tests pin the DECISIONS —

1. Tool declaration — name, handler, OBJECT parameters with action enum.
2. Service aliases — "jio saavn"/"saavn" fold into jiosaavn, "yt music" into
   youtube_music; an unknown service is refused with the list, never guessed.
3. JioSaavn play — the resolved song page is opened and the title reaches the
   user; when the API finds nothing, the search page opens instead.
4. Spotify play — with the desktop app, a spotify:search: URI is handed to the
   OS; without it, open.spotify.com search opens in the browser; a query that
   is already a spotify: URI passes through untouched.
5. YouTube Music play — the yt-dlp watch URL is opened when resolution works.
6. No query — play degrades to opening the service home, not an error.
7. Transport — next/previous/pause map to the right media keys via _press_key,
   whatever module actually imported pyautogui.
8. Unknown action — refused with the available list.
9. New services (Gaana/Wynk/Amazon Music) — Gaana resolves a song page like
   JioSaavn; Wynk opens its player and discloses the Airtel sign-in without
   ever claiming playback; Amazon opens the marketplace search URL.
10. Now playing — the OS's own media session: text is formatted from queried
    sessions (playing vs paused, multiple players, no artist, nothing playing,
    query failure), player names are made human, and the gdbus GVariant text
    parser survives quoted titles and multi-artist lists (mocked subprocess).

Run:  .venv/bin/python -m pytest tests/test_music_player.py -q
Also standalone: .venv/bin/python tests/test_music_player.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from actions import music_player as mp  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_music_state():
    """Each test starts from a quiet player: no in-HUD track, no popup song,
    and no dashboard media payload, so one test's state never leaks into the
    next (the now_playing / transport tests assume silence otherwise)."""
    mp._hud_queue_clear()
    mp._MUSIC_POPUP.update({"on": False, "service": "", "queue": [], "i": 0,
                            "label": ""})
    try:
        from dashboard import media_bus

        media_bus.stop()
    except Exception:
        pass
    yield
    mp._hud_queue_clear()
    mp._MUSIC_POPUP.update({"on": False, "service": "", "queue": [], "i": 0,
                            "label": ""})
    try:
        from dashboard import media_bus

        media_bus.stop()
    except Exception:
        pass


def test_tool_declaration():
    assert mp.TOOL["name"] == "music_player"
    assert callable(mp.TOOL["handler"])
    assert mp.TOOL["handler"] is mp.music_player
    params = mp.TOOL["parameters"]
    assert params["type"] == "OBJECT"
    assert set(params["properties"]["action"]["enum"]) == {
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
    }
    print("✓ tool declaration: name/handler/params valid")
    return True


def test_service_aliases():
    assert mp._SERVICE_ALIASES["jio saavn"] == "jiosaavn"
    assert mp._SERVICE_ALIASES["saavn"] == "jiosaavn"
    assert mp._SERVICE_ALIASES["yt music"] == "youtube_music"
    assert mp._SERVICE_ALIASES["ytmusic"] == "youtube_music"
    assert mp._SERVICE_ALIASES["spotify"] == "spotify"

    out = mp.music_player({"action": "play", "service": "pandora", "query": "x"})
    assert "Unknown music service" in out and "jiosaavn" in out
    print("✓ service aliases fold, unknown service refused")
    return True


def test_jiosaavn_play_in_app_popup_not_chrome():
    """A popup-capable player plays the resolved song in-app and never opens a
    browser. This is the requested behaviour: JioSaavn inside RIO, not Chrome."""
    calls = []
    player = type(
        "P", (), {"play_music": lambda self, *a, **k: calls.append(a) or True}
    )()
    songs = [
        {"perma": "p", "stream": "https://x/a.mp4", "label": "Kesariya — Pritam",
         "title": "Kesariya", "artist": "Pritam", "artwork": "https://img"}
    ]
    opened = []
    with patch.object(mp, "_saavn_pick", return_value=songs), patch.object(
        mp, "_open_web", side_effect=lambda url: opened.append(url) or True
    ):
        out = mp.music_player(
            {"action": "play", "service": "jiosaavn", "query": "kesariya"},
            player=player,
        )
    assert len(calls) == 1, "the popup player was never started"
    assert calls[0][0] == "https://x/a.mp4"
    assert calls[0][1] == "Kesariya" and calls[0][2] == "Pritam"
    assert opened == [], "JioSaavn must not open a browser when a popup exists"
    assert "popup" in out
    mp._MUSIC_POPUP.update({"on": False, "service": "", "queue": [], "i": 0,
                            "label": ""})
    print("✓ jiosaavn play: in-app popup, Chrome never opened")
    return True


def test_jiosaavn_falls_back_to_search_page_without_a_stream():
    """With no in-app player and no resolvable stream, the search page opens —
    the only remaining honest way to reach JioSaavn."""
    opened = []
    with patch.object(mp, "_saavn_pick", return_value=[]), patch.object(
        mp, "_open_web", side_effect=lambda url: opened.append(url) or True
    ):
        out = mp.music_player(
            {"action": "play", "service": "jiosaavn", "query": "kesariya"}
        )
    assert opened == ["https://www.jiosaavn.com/search/kesariya"]
    assert "could not reach JioSaavn" not in out  # it opened the page, not an error
    print("✓ jiosaavn with no stream: opens the search page, not an error")
    return True


def test_jiosaavn_in_hud_stream_is_opt_in():
    """The old decrypted in-HUD path still exists, but only when asked for."""
    opened = []
    player = type("P", (), {
        "show_video": lambda self, src, title, muted=False, audio_source="":
            opened.append((src, title, muted)),
        "pause_video": lambda self, p: None,
        "stop_video": lambda self: None,
        "video_is_playing": lambda self: True,
    })()
    songs = [{"perma": "u", "stream": "s", "label": "Song One"}]
    with patch.object(mp, "_SAAVN_INHUD", True), patch.object(
        mp, "_saavn_pick", return_value=songs
    ):
        out = mp.music_player(
            {"action": "play", "service": "jiosaavn", "query": "x"}, player=player
        )
    assert opened == [("s", "Song One", False)]
    assert "Song One" in out
    mp._hud_queue_clear()
    print("✓ jiosaavn in-HUD streaming is opt-in (JARVIS_JIOSAAVN_INHUD)")
    return True


def test_jiosaavn_fallback_search_page():
    """No resolvable stream → the search page opens (last resort)."""
    opened = []
    with patch.object(mp, "_saavn_pick", return_value=[]), patch.object(
        mp, "_open_web", side_effect=lambda url: opened.append(url) or True
    ):
        out = mp.music_player(
            {"action": "play", "service": "jiosaavn", "query": "zzz no such song"}
        )
    assert opened == ["https://www.jiosaavn.com/search/zzz%20no%20such%20song"]
    assert "search" in out.lower()
    print("✓ jiosaavn fallback: search page when no stream resolves")
    return True


def test_spotify_app_uses_search_uri():
    opened = []
    with patch.object(mp, "_spotify_app_installed", return_value=True), patch.object(
        mp, "_open_uri", side_effect=lambda uri: opened.append(uri) or True
    ):
        out = mp.music_player(
            {"action": "play", "service": "spotify", "query": "kesariya"}
        )
    assert opened and opened[0].startswith("spotify:search:")
    assert "kesariya" in opened[0]
    assert "Spotify" in out
    print("✓ spotify with app: spotify:search: URI handed to the OS")
    return True


def test_spotify_no_app_opens_web_search():
    opened = []
    with patch.object(mp, "_spotify_app_installed", return_value=False), patch.object(
        mp, "_open_web", side_effect=lambda url: opened.append(url) or True
    ):
        out = mp.music_player(
            {"action": "play", "service": "spotify", "query": "kesariya"}
        )
    assert opened == ["https://open.spotify.com/search/kesariya"]
    assert "Spotify" in out
    print("✓ spotify without app: web search opened")
    return True


def test_spotify_uri_passthrough():
    opened = []
    with patch.object(
        mp, "_open_uri", side_effect=lambda uri: opened.append(uri) or True
    ):
        out = mp.music_player(
            {
                "action": "play",
                "service": "spotify",
                "query": "spotify:track:4uLU6hCjRIcuNfN9PBaxnB",
            }
        )
    assert opened == ["spotify:track:4uLU6hCjRIcuNfN9PBaxnB"]
    assert "Playing" in out
    print("✓ spotify: URI passes through untouched (autoplays)")
    return True


def test_ytmusic_watch_url():
    opened = []
    with patch.object(
        mp,
        "_ytmusic_watch_url",
        return_value="https://music.youtube.com/watch?v=abc123",
    ), patch.object(
        mp, "_open_web", side_effect=lambda url: opened.append(url) or True
    ):
        out = mp.music_player(
            {"action": "play", "service": "youtube_music", "query": "kesariya"}
        )
    assert opened == ["https://music.youtube.com/watch?v=abc123"]
    assert "YouTube Music" in out
    print("✓ youtube music: resolved watch URL opened")
    return True


def test_play_without_query_opens_the_service():
    """With no song named, the official path opens JioSaavn's player; the old
    chart auto-start needed the site API, so it is opt-in now."""
    opened = []
    with patch.object(mp, "_saavn_default_song") as api, patch.object(
        mp, "_open_web", side_effect=lambda url: opened.append(url) or True
    ):
        out = mp.music_player({"action": "play", "service": "jiosaavn"})
    assert not api.called
    assert opened == ["https://www.jiosaavn.com/"]
    assert "open" in out.lower()
    print("✓ play without query: JioSaavn's own player opens, no API call")
    return True


def test_play_without_query_chart_down_falls_back():
    opened = []
    with patch.object(mp, "_saavn_default_song", return_value=("", "")), patch.object(
        mp, "_open_web", side_effect=lambda url: opened.append(url) or True
    ):
        out = mp.music_player({"action": "play", "service": "jiosaavn"})
    assert opened == ["https://www.jiosaavn.com/"]
    assert "open" in out.lower()
    print("✓ play without query: chart down → service home, still not an error")
    return True


def test_open_without_query_opens_service_home():
    opened = []
    with patch.object(
        mp, "_open_web", side_effect=lambda url: opened.append(url) or True
    ):
        out = mp.music_player({"action": "open", "service": "jiosaavn"})
    assert opened == ["https://www.jiosaavn.com/"]
    assert "open" in out.lower()
    print("✓ open action: service home, unchanged")
    return True


def test_new_service_aliases():
    assert mp._SERVICE_ALIASES["gaana"] == "gaana"
    assert mp._SERVICE_ALIASES["wynk music"] == "wynk"
    assert mp._SERVICE_ALIASES["amazon music"] == "amazon_music"
    assert mp._SERVICE_ALIASES["prime music"] == "amazon_music"
    assert mp._SERVICE_ALIASES["amazon"] == "amazon_music"
    print("✓ new service aliases fold (gaana/wynk/amazon/prime)")
    return True


def test_unknown_service_lists_every_service():
    out = mp.music_player({"action": "play", "service": "pandora", "query": "x"})
    for service in mp.SERVICES:
        assert service in out, service
    print("✓ unknown service refusal lists all six services")
    return True


def test_gaana_play_opens_song_page():
    opened = []
    with patch.object(
        mp,
        "_gaana_song_url",
        return_value=(
            "https://gaana.com/song/kesariya-from-brahmastra",
            "kesariya from brahmastra",
        ),
    ), patch.object(
        mp, "_open_web", side_effect=lambda url: opened.append(url) or True
    ):
        out = mp.music_player(
            {"action": "play", "service": "gaana", "query": "kesariya"}
        )
    assert opened == ["https://gaana.com/song/kesariya-from-brahmastra"]
    assert "Gaana" in out and "kesariya" in out
    print("✓ gaana play: song page opened, label spoken")
    return True


def test_gaana_fallback_search_page():
    opened = []
    with patch.object(mp, "_gaana_song_url", return_value=("", "")), patch.object(
        mp, "_open_web", side_effect=lambda url: opened.append(url) or True
    ):
        out = mp.music_player(
            {"action": "play", "service": "gaana", "query": "zzz no such song"}
        )
    assert opened == ["https://gaana.com/search/zzz%20no%20such%20song"]
    assert "search" in out.lower()
    print("✓ gaana fallback: search page when scrape yields nothing")
    return True


def test_wynk_opens_player_without_claiming_playback():
    opened = []
    with patch.object(
        mp, "_open_web", side_effect=lambda url: opened.append(url) or True
    ):
        out = mp.music_player(
            {"action": "play", "service": "wynk", "query": "kesariya"}
        )
    assert opened == ["https://wynk.in/music"]
    assert "sign" in out.lower()  # honest about the Airtel sign-in
    assert "Playing" not in out  # never claims a song started
    print("✓ wynk: player opens, login honestly disclosed, no fake 'Playing'")
    return True


def test_amazon_music_search_page():
    opened = []
    with patch.object(
        mp, "_open_web", side_effect=lambda url: opened.append(url) or True
    ):
        out = mp.music_player(
            {"action": "play", "service": "amazon_music", "query": "adele"}
        )
    assert opened == ["https://music.amazon.in/search/adele"]
    assert "Amazon Music" in out
    print("✓ amazon music: marketplace search page opened")
    return True


def test_amazon_music_open_home():
    opened = []
    with patch.object(
        mp, "_open_web", side_effect=lambda url: opened.append(url) or True
    ):
        out = mp.music_player({"action": "open", "service": "amazon_music"})
    assert opened == ["https://music.amazon.in"]
    assert "open" in out.lower()
    print("✓ amazon music open: marketplace home")
    return True


def test_now_playing_playing():
    with patch.object(
        mp,
        "_query_media_sessions",
        return_value=[
            {
                "player": "spotify",
                "title": "Kesariya",
                "artist": "Arijit Singh",
                "status": "Playing",
            }
        ],
    ):
        out = mp.music_player({"action": "now_playing"})
    assert out == "Now playing: Kesariya — Arijit Singh (Spotify)."
    print("✓ now playing: title, artist and player reported")
    return True


def test_now_playing_paused_when_nothing_playing():
    with patch.object(
        mp,
        "_query_media_sessions",
        return_value=[
            {
                "player": "chromium.instance123",
                "title": "Tum Hi Ho",
                "artist": "",
                "status": "Paused",
            }
        ],
    ):
        out = mp.music_player({"action": "now_playing"})
    assert out.startswith("Paused:")
    assert "Tum Hi Ho" in out and "the browser" in out
    print("✓ now playing: paused sessions honestly labelled Paused")
    return True


def test_now_playing_prefers_playing_over_paused():
    with patch.object(
        mp,
        "_query_media_sessions",
        return_value=[
            {"player": "vlc", "title": "Old Song", "artist": "", "status": "Paused"},
            {
                "player": "spotify",
                "title": "Live Song",
                "artist": "A. R. Rahman",
                "status": "Playing",
            },
        ],
    ):
        out = mp.music_player({"action": "now_playing"})
    assert out.startswith("Now playing:")
    assert "Live Song" in out and "Old Song" not in out
    print("✓ now playing: a playing session wins over stale paused ones")
    return True


def test_now_playing_multiple_joined():
    with patch.object(
        mp,
        "_query_media_sessions",
        return_value=[
            {"player": "spotify", "title": "A", "artist": "", "status": "Playing"},
            {"player": "vlc", "title": "B", "artist": "", "status": "Playing"},
        ],
    ):
        out = mp.music_player({"action": "now_playing"})
    assert "A (Spotify); B (VLC)." in out
    print("✓ now playing: two playing players joined with ';; '")
    return True


def test_now_playing_nothing_and_failure():
    with patch.object(mp, "_query_media_sessions", return_value=[]):
        assert (
            mp.music_player({"action": "now_playing"})
            == "Nothing is playing right now, sir."
        )
    with patch.object(mp, "_query_media_sessions", side_effect=RuntimeError("no bus")):
        out = mp.music_player({"action": "now_playing"})
    assert "couldn't ask the system" in out and "no bus" in out
    print("✓ now playing: empty bus and query failure both speak honestly")
    return True


def test_friendly_player_names():
    assert mp._friendly_player("chromium.instance123") == "the browser"
    assert mp._friendly_player("spotify") == "Spotify"
    assert mp._friendly_player("SpotifyAB.SpotifyMusic_zpdnekdrzrea0") == "Spotify"
    assert mp._friendly_player("chrome.exe") == "the browser"
    assert mp._friendly_player("") == "an unknown player"
    print("✓ player names: AUMIDs and MPRIS instances become human words")
    return True


def test_linux_gdbus_parse():
    """The gdbus fallback must survive real GVariant text: quoted titles,
    multi-artist arrays, and MPRIS instance suffixes."""
    list_out = (
        "(['org.freedesktop.DBus', 'org.mpris.MediaPlayer2.spotify', "
        "'org.mpris.MediaPlayer2.chromium.instance123'],)"
    )
    meta_spotify = (
        "({'mpris:trackid': <objectpath '/org/mpris/Track/1'>, "
        "'xesam:artist': <['Arijit Singh', 'Pritam']>, "
        "'xesam:title': <'Kesariya\\'s (From \\\"Brahmastra\\\")'>, })"
    )
    meta_chromium = "({'xesam:title': <'Tum Hi Ho'>, " "'xesam:artist': <@as []>, })"

    def fake_run(cmd, capture_output=None, text=None, timeout=None):
        joined = " ".join(cmd)
        stdout = ""
        if "ListNames" in joined:
            stdout = list_out
        elif "Metadata" in joined:
            stdout = meta_chromium if "chromium" in joined else meta_spotify
        elif "PlaybackStatus" in joined:
            stdout = "<'Paused'>" if "chromium" in joined else "<'Playing'>"
        return type("R", (), {"stdout": stdout})()

    with patch.object(mp.subprocess, "run", side_effect=fake_run):
        sessions = mp._query_linux_gdbus()

    assert sessions == [
        {
            "player": "spotify",
            "title": 'Kesariya\'s (From "Brahmastra")',
            "artist": "Arijit Singh, Pritam",
            "status": "Playing",
        },
        {
            "player": "chromium.instance123",
            "title": "Tum Hi Ho",
            "artist": "",
            "status": "Paused",
        },
    ]
    print("✓ gdbus parser: quoted titles, empty artist arrays, instance names")
    return True


def test_decrypt_saavn_stream():
    # Fixed vector: DES-ECB(b'38346591') of the URL below, PKCS5-padded.
    enc = "ID2ieOjCrwfgWvL5sXl4B1ImC5QfbsDyFoFn8R20PnKugbZ/Ye2T/RgU7MasW8n/"
    assert mp._decrypt_saavn_stream(enc) == "https://aac.saavncdn.com/871/test_96.mp4"
    assert mp._decrypt_saavn_stream("garbage!!") == ""
    print("✓ saavn stream decryption: round-trips, junk → empty")
    return True


def test_saavn_pick_320kbps_preference():
    enc = "ID2ieOjCrwfgWvL5sXl4B1ImC5QfbsDyFoFn8R20PnKugbZ/Ye2T/RgU7MasW8n/"
    song = {
        "type": "song",
        "title": "Kesariya",
        "perma_url": "https://www.jiosaavn.com/song/kesariya/x",
        "more_info": {
            "encrypted_media_url": enc,
            "320kbps": "true",
            "artistMap": {"primary_artists": [{"name": "Arijit Singh"}]},
        },
    }
    resp = type("R", (), {"json": lambda self: {"results": [song]}})()
    with patch.object(mp.requests, "get", return_value=resp):
        picked = mp._saavn_pick("kesariya", n=1)
    assert picked[0]["label"] == "Kesariya — Arijit Singh"
    assert picked[0]["stream"].endswith("_320.mp4")

    song["more_info"]["320kbps"] = "false"
    with patch.object(mp.requests, "get", return_value=resp):
        picked = mp._saavn_pick("kesariya", n=1)
    assert picked[0]["stream"].endswith("_96.mp4")
    print("✓ saavn pick: labels built, 320kbps stream chosen when offered")
    return True


def test_in_hud_play_and_transport():
    opened, paused, stopped = [], [], []
    player = type(
        "P",
        (),
        {
            "show_video": lambda self,
            src,
            title,
            muted=False,
            audio_source="": opened.append((src, title, muted)),
            "pause_video": lambda self, p: paused.append(p),
            "stop_video": lambda self: stopped.append(True),
            "video_is_playing": lambda self: True,
        },
    )()
    songs = [
        {"perma": "u1", "stream": "s1", "label": "Song One"},
        {"perma": "u2", "stream": "s2", "label": "Song Two"},
        {"perma": "u3", "stream": "s3", "label": "Song Three"},
    ]
    with patch.object(mp, "_SAAVN_INHUD", True), patch.object(
        mp, "_saavn_pick", return_value=songs
    ):
        out = mp.music_player(
            {"action": "play", "service": "jiosaavn", "query": "kesariya"},
            player=player,
        )
    assert opened == [("s1", "Song One", False)]  # UNMUTED music
    assert out.startswith("Playing Song One")
    assert mp._HUD_MUSIC["on"] and mp._HUD_MUSIC["i"] == 0

    out = mp.music_player({"action": "next"}, player=player)
    assert opened[-1] == ("s2", "Song Two", False) and "Song Two" in out
    out = mp.music_player({"action": "previous"}, player=player)
    assert opened[-1] == ("s1", "Song One", False)

    out = mp.music_player({"action": "pause"}, player=player)
    assert paused == [True] and mp._HUD_MUSIC["paused"] is True
    out = mp.music_player({"action": "resume"}, player=player)
    assert paused[-1] is False and mp._HUD_MUSIC["paused"] is False

    out = mp.music_player({"action": "stop"}, player=player)
    assert stopped == [True] and mp._HUD_MUSIC["on"] is False
    print("✓ in-HUD music: play/next/prev/pause/resume/stop all hit the player")
    return True


def test_transport_without_hud_music_uses_media_keys():
    pressed = []
    with patch.object(mp, "_press_key", side_effect=lambda k: pressed.append(k)), \
         patch.object(mp, "_query_media_sessions",
                      return_value=[{"player": "spotify", "title": "X",
                                     "artist": "", "status": "Playing"}]):
        mp.music_player({"action": "pause"})
    assert pressed == ["playpause"]
    print("✓ transport with no in-HUD music: global media keys as before")
    return True


def test_transport_refuses_to_press_keys_over_silence():
    """No media session → say so rather than press a key into nothing."""
    pressed = []
    with patch.object(mp, "_press_key", side_effect=lambda k: pressed.append(k)), \
         patch.object(mp, "_query_media_sessions", return_value=[]):
        out = mp.music_player({"action": "pause"})
    assert pressed == []
    assert "Nothing is playing" in out
    return True


def test_now_playing_prefers_in_hud_and_detects_close():
    opened = []
    player = type(
        "P",
        (),
        {
            "show_video": lambda self,
            src,
            title,
            muted=False,
            audio_source="": opened.append(src),
            "pause_video": lambda self, p: None,
            "stop_video": lambda self: None,
            "video_is_playing": lambda self: self._open,
        },
    )()
    player._open = True
    songs = [{"perma": "u", "stream": "s", "label": "Song One"}]
    with patch.object(mp, "_SAAVN_INHUD", True), patch.object(
        mp, "_saavn_pick", return_value=songs
    ):
        mp.music_player(
            {"action": "play", "service": "jiosaavn", "query": "x"}, player=player
        )
    out = mp.music_player({"action": "now_playing"}, player=player)
    assert out.startswith("Now playing: Song One") and "on the display" in out

    # The user closed the video in the HUD: state must clear, and the OS
    # probe takes over instead of reporting a ghost track forever.
    player._open = False
    with patch.object(mp, "_query_media_sessions", return_value=[]):
        out = mp.music_player({"action": "now_playing"}, player=player)
    assert out == "Nothing is playing right now, sir."
    assert mp._HUD_MUSIC["on"] is False
    print("✓ now playing: in-HUD state first, and a user-closed video clears it")
    return True


def test_transport_media_keys():
    pressed = []
    session = [{"player": "spotify", "title": "X", "artist": "",
                "status": "Playing"}]
    with patch.object(mp, "_press_key", side_effect=lambda key: pressed.append(key)), \
         patch.object(mp, "_query_media_sessions", return_value=session):
        for action, key in [
            ("pause", "playpause"),
            ("stop", "playpause"),
            ("resume", "playpause"),
            ("next", "nexttrack"),
            ("previous", "prevtrack"),
        ]:
            out = mp.music_player({"action": action})
            assert pressed[-1] == key, (action, pressed)
            assert out
    print("✓ transport: actions map to the right media keys")
    return True


def test_unknown_action():
    out = mp.music_player({"action": "dance", "service": "jiosaavn"})
    assert "Unknown music action 'dance'" in out
    assert "play" in out and "stop" in out
    print("✓ unknown action refused with the available list")
    return True


if __name__ == "__main__":
    fns = [
        (n, f)
        for n, f in sorted(globals().items())
        if n.startswith("test_") and callable(f)
    ]
    failed = 0
    for name, fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"✗ {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
