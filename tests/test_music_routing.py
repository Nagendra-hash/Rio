"""Where a music request goes, and what it is allowed to touch.

Three rules are pinned here:
  * the configured default provider is used when the user names none,
  * JioSaavn plays in the in-app music popup (its own stream, resolved the same
    way their web player fetches it) and never opens Chrome; a browser is only
    the last resort when no in-app player can start it,
  * Spotify's Web API is used only when an account is actually connected.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from actions import music_player as mp


class FakePlayer:
    def __init__(self):
        self.shown = []
        self.paused = None

    def show_video(self, *a, **k):
        self.shown.append((a, k))

    def video_is_playing(self):
        return bool(self.shown)

    def pause_video(self, paused=True):
        self.paused = paused

    def stop_video(self):
        self.shown.clear()

    def write_log(self, *_a):
        pass


def _fake_spotify(connected=True, play_reply="Playing Kesariya on Spotify."):
    calls = []

    def record(name, reply=""):
        def f(*a, **k):
            calls.append((name, a, k))
            return reply
        return f

    api = SimpleNamespace(
        connected=lambda: connected,
        play=record("play", play_reply),
        pause=record("pause", "Paused Spotify."),
        resume=record("resume", "Resumed Spotify."),
        next_track=record("next", "Skipped."),
        previous_track=record("previous", "Back."),
        now_playing=record("now_playing", "Kesariya — playing."),
        devices=record("devices", [{"name": "Desktop"}]),
        connect=record("connect", "I opened the authorisation page."),
        forget_token=record("forget", None),
    )
    api.calls = calls
    return api


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    mp._hud_queue_clear()
    monkeypatch.setattr(mp, "_open_web", lambda url: True)
    monkeypatch.setattr(mp, "_open_uri", lambda uri: True)
    monkeypatch.setattr(mp, "_spotify_app_installed", lambda: True)
    yield
    mp._hud_queue_clear()


def test_the_configured_default_provider_is_used(monkeypatch):
    import memory.config_manager as cfg

    monkeypatch.setattr(cfg, "get_music_provider", lambda: "spotify")
    assert mp._default_service() == "spotify"


def test_unknown_service_is_rejected():
    reply = mp.music_player({"action": "play", "service": "napster", "query": "x"})
    assert "Unknown music service" in reply


def test_jiosaavn_plays_in_app_popup_and_never_opens_chrome(monkeypatch):
    """The requested behaviour: a JioSaavn song plays in the in-app popup, not
    in a browser tab. The site API is used only to resolve the stream (the same
    endpoint their own web player uses) — no page scraping, no browser launch."""
    calls = []
    player = type("P", (), {
        "play_music": lambda self, *a, **k: calls.append(a) or True,
        "music_is_active": lambda self: False,
    })()
    songs = [
        {"perma": "p", "stream": "https://x/a.mp4", "label": "Kesariya — Pritam",
         "title": "Kesariya", "artist": "Pritam", "artwork": "https://img"}
    ]
    monkeypatch.setattr(mp, "_saavn_pick", lambda *a, **k: songs)
    opened = []
    monkeypatch.setattr(mp, "_open_web", lambda url: opened.append(url) or True)

    reply = mp.music_player(
        {"action": "play", "service": "jiosaavn", "query": "Kesariya"},
        player=player,
    )
    assert len(calls) == 1 and calls[0][0] == "https://x/a.mp4"
    assert opened == [], "no browser may open when the popup is available"
    assert "popup" in reply


def test_spotify_uses_the_web_api_when_connected(monkeypatch):
    api = _fake_spotify(connected=True)
    monkeypatch.setattr(mp, "_spotify_api", lambda: api)
    reply = mp.music_player(
        {"action": "play", "service": "spotify", "query": "Kesariya"}
    )
    assert reply == "Playing Kesariya on Spotify."
    assert api.calls and api.calls[0][0] == "play"


def test_spotify_without_a_connection_opens_the_app_and_never_switches_provider(monkeypatch):
    api = _fake_spotify(connected=False)
    monkeypatch.setattr(mp, "_spotify_api", lambda: api)
    opened = []
    monkeypatch.setattr(mp, "_open_uri", lambda uri: opened.append(uri) or True)
    monkeypatch.setattr(mp, "_open_web", lambda url: opened.append(url) or True)

    reply = mp.music_player(
        {"action": "play", "service": "spotify", "query": "Kesariya"}
    )
    assert opened and opened[0].startswith("spotify:search:")
    assert "JioSaavn" not in reply


def test_pause_goes_to_spotify_when_it_is_the_named_service(monkeypatch):
    api = _fake_spotify(connected=True)
    monkeypatch.setattr(mp, "_spotify_api", lambda: api)
    reply = mp.music_player({"action": "pause", "service": "spotify"})
    assert reply == "Paused Spotify."
    assert [c[0] for c in api.calls] == ["pause"]


def test_now_playing_prefers_spotify_state(monkeypatch):
    api = _fake_spotify(connected=True)
    monkeypatch.setattr(mp, "_spotify_api", lambda: api)
    assert mp.music_player(
        {"action": "now_playing", "service": "spotify"}
    ) == "Kesariya — playing."


def test_a_local_mp3_plays_in_the_hud(monkeypatch, tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"ID3" + b"\x00" * 64)
    player = FakePlayer()
    reply = mp.music_player({"action": "play", "query": str(song)}, player=player)
    assert player.shown, "the HUD player was never given the file"
    assert player.shown[0][1]["muted"] is False
    assert "song.mp3" in reply


def test_a_local_file_is_recognised_with_a_file_url(tmp_path):
    song = tmp_path / "track.flac"
    song.write_bytes(b"fLaC")
    assert mp._local_audio(song.as_uri()) == str(song)


def test_set_default_saves_the_preference(monkeypatch):
    saved = {}
    import memory.config_manager as cfg

    monkeypatch.setattr(
        cfg, "save_music_provider", lambda name: saved.setdefault("name", name)
    )
    reply = mp.music_player({"action": "set_default", "service": "spotify"})
    assert saved["name"] == "spotify"
    assert "default" in reply.lower()


def test_set_default_rejects_an_unsupported_service():
    reply = mp.music_player({"action": "set_default", "service": "gaana"})
    assert "Spotify or JioSaavn" in reply


def test_status_reports_the_default_and_the_connection(monkeypatch):
    api = _fake_spotify(connected=True)
    monkeypatch.setattr(mp, "_spotify_api", lambda: api)
    import memory.config_manager as cfg

    monkeypatch.setattr(cfg, "get_music_provider", lambda: "jiosaavn")
    reply = mp.music_player({"action": "status"})
    assert "jiosaavn" in reply
    assert "Spotify is connected" in reply
    assert "Desktop" in reply
