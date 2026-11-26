"""
tests/test_youtube_trending.py — offline tests for the YouTube trending
fallback chain in actions/youtube_video.py.

Background (the incident, 2026-09-26): /feed/trending stopped serving video
data to anonymous scrapers — the page is an 890KB ytInitialData shell whose
only "content" is a feedNudgeRenderer ("Try searching to get started"). The
old regex scrape matched UI chrome and JARVIS read "Keyboard shortcuts" as a
trending video. The fix parses ytInitialData as JSON and walks videoRenderer
nodes, falling back to a search proxy (most-viewed uploads this week).

These tests run the WHOLE chain against saved fixtures captured from the live
site (tests/fixtures/) — no network, no headless browser:

1. Parse tier   — a real search-results ytInitialData yields exactly its
                  videoRenderer nodes (id/title/channel), in order.
2. Empty-shell  — the captured trending shell yields ZERO videos, which is
                  what must trigger the fallback, not a junk-title list.
3. Fallback     — with the feed serving the shell and the search proxy
                  serving the fixture page, _scrape_trending returns real
                  videos with rank/title/channel, and the old UI-chrome
                  junk titles can never reappear (blocklist + structure).
4. Handler      — the voice-facing youtube_video(action='trending') path
                  formats the same result without junk.
5. Robustness   — malformed JSON and search pages whose videos sit under
                  richGridRenderer (the newer layout) parse correctly.

Fixtures were captured 2026-09-26 via a slimming pass (long strings trimmed,
tracking noise dropped) but renderer fields keep the exact wire shape —
"runs" text and "simpleText" both included — so the parser is exercised
against real data, not an idealized subset.

Run:  .venv/bin/python -m pytest tests/test_youtube_trending.py -q
Also standalone: .venv/bin/python tests/test_youtube_trending.py
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from actions import youtube_video as yt  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SEARCH_FIXTURE = FIXTURES / "youtube_search_initialdata.json"
SHELL_FIXTURE = FIXTURES / "youtube_trending_shell_initialdata.json"


def _page(html_text: str):
    """Stub requests.get(url, headers=..., timeout=...) → object with .text."""
    return type("R", (), {"text": html_text})()


def _html_with(initial_data: dict) -> str:
    """Wrap a ytInitialData dict the way YouTube wraps it in the page."""
    return (
        "<html><script>var ytInitialData = "
        + json.dumps(initial_data)
        + ";</script></html>"
    )


# ── Fixtures load and represent reality ──────────────────────────────────────


def test_fixtures_exist_and_parse() -> bool:
    assert SEARCH_FIXTURE.exists(), f"missing fixture: {SEARCH_FIXTURE}"
    assert SHELL_FIXTURE.exists(), f"missing fixture: {SHELL_FIXTURE}"
    search = json.loads(SEARCH_FIXTURE.read_text(encoding="utf-8"))
    shell = json.loads(SHELL_FIXTURE.read_text(encoding="utf-8"))
    # The search fixture is only useful if it actually embeds renderers…
    assert len(list(yt._iter_nodes(search, "videoRenderer"))) == 3
    # …and the shell fixture is only useful if it embeds none.
    assert len(list(yt._iter_nodes(shell, "videoRenderer"))) == 0
    print("✓ fixtures parse; search=3 renderers, shell=0")
    return True


# ── Tier 1: ytInitialData → videoRenderer extraction ─────────────────────────


def test_extract_videos_from_search_fixture() -> bool:
    data = json.loads(SEARCH_FIXTURE.read_text(encoding="utf-8"))
    videos = yt._extract_videos_from_initial_data(data)
    assert len(videos) == 3, f"expected 3 videos, got {len(videos)}"
    first = videos[0]
    assert first["videoId"] == "JqFzhcWo3EU"
    assert first["title"].startswith("Yeshanagula")
    assert first["channel"] == "Saregama Telugu"
    # order preserved, ids unique
    ids = [v["videoId"] for v in videos]
    assert ids == ["JqFzhcWo3EU", "LYoxE6MCM1k", "X3m-rEYPYUE"]
    assert len(set(ids)) == len(ids)
    print("✓ extract: 3 renderers → (videoId, title, channel), order kept")
    return True


def test_empty_trending_shell_yields_nothing() -> bool:
    data = json.loads(SHELL_FIXTURE.read_text(encoding="utf-8"))
    videos = yt._extract_videos_from_initial_data(data)
    assert (
        videos == []
    ), "the shell must produce ZERO videos — junk means the parser is matching chrome again"
    print(
        "✓ empty shell: zero videos (old code returned 'Keyboard shortcuts' from this page)"
    )
    return True


# ── Tier 2: the full fallback chain, both tiers mocked ───────────────────────


def test_scrape_trending_falls_back_to_search() -> bool:
    search_data = json.loads(SEARCH_FIXTURE.read_text(encoding="utf-8"))
    shell_data = json.loads(SHELL_FIXTURE.read_text(encoding="utf-8"))

    def fake_get(url, headers=None, timeout=None):
        # First call = /feed/trending (the shell); second = search proxy.
        if "/feed/trending" in url:
            return _page(_html_with(shell_data))
        if "/results?" in url:
            assert (
                f"sp={yt._YT_TRENDING_FILTER}" in url
            ), "search proxy lost its view-count filter"
            return _page(_html_with(search_data))
        raise AssertionError(f"unexpected URL fetched: {url}")

    with patch.object(yt.requests, "get", side_effect=fake_get):
        results = yt._scrape_trending(region="US", max_results=2)

    assert len(results) == 2
    assert results[0] == {
        "rank": 1,
        "title": "Yeshanagula | The Paradise | Nani | Keerthy Suresh | Anirudh Ravichander | Srikanth Odela",
        "channel": "Saregama Telugu",
    }
    for i, v in enumerate(results):
        assert set(v) == {"rank", "title", "channel"}, f"bad result shape: {v}"
        assert v["rank"] == i + 1
        assert (
            v["title"].strip().lower() not in yt._JUNK_TITLES
        ), "junk title leaked into results"
    print("✓ fallback chain: shell → search proxy → ranked real videos")
    return True


def test_scrape_trending_uses_feed_data_when_served() -> bool:
    """If YouTube serves real videos on /feed/trending again, tier 1 wins."""
    feed_with_videos = {
        "contents": {
            "some": [
                {
                    "videoRenderer": {
                        "videoId": "abc12345678",
                        "title": {"runs": [{"text": "Real Trending Video"}]},
                        "ownerText": {"runs": [{"text": "Real Channel"}]},
                    }
                }
            ]
        }
    }
    with patch.object(
        yt.requests, "get", return_value=_page(_html_with(feed_with_videos))
    ) as m:
        results = yt._scrape_trending(region="TR", max_results=8)
    assert m.call_count == 1, "search proxy must NOT fire when the feed has data"
    assert results == [
        {"rank": 1, "title": "Real Trending Video", "channel": "Real Channel"}
    ]
    print("✓ feed tier wins when YouTube serves real data again")
    return True


def test_search_proxy_retries_on_transient_empty_page() -> bool:
    """One observed-in-the-wild empty page must not end the request."""
    search_data = json.loads(SEARCH_FIXTURE.read_text(encoding="utf-8"))
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _page(_html_with({}))  # transient empty
        return _page(_html_with(search_data))  # good page

    with patch.object(yt.requests, "get", side_effect=fake_get):
        results = yt._scrape_trending_search("US", max_results=5)
    assert len(results) == 3 and calls["n"] == 2
    print("✓ search proxy: transient empty page retried, not fatal")
    return True


def test_fallback_junk_titles_blocked() -> bool:
    """Even if a page smuggles UI chrome as renderers, the blocklist holds."""
    poisoned = {
        "contents": {
            "x": [
                {
                    "videoRenderer": {
                        "videoId": "dQw4w9WgXcQ",
                        "title": {"simpleText": "Keyboard shortcuts"},
                    }
                }
            ]
        }
    }
    with patch.object(yt.requests, "get", return_value=_page(_html_with(poisoned))):
        results = yt._scrape_trending("US", max_results=8)
    assert results == [], "UI-chrome titles must never reach the user"
    print("✓ junk blocklist: 'Keyboard shortcuts' can never be announced again")
    return True


# ── Robustness: malformed data and the newer grid layout ─────────────────────


def test_malformed_initial_data_returns_empty() -> bool:
    with patch.object(
        yt.requests, "get", return_value=_page("var ytInitialData = {not json")
    ):
        assert yt._scrape_trending("US", max_results=8) == []
    assert yt._parse_yt_initial_data("no blob here at all") is None
    print("✓ robustness: malformed/absent ytInitialData → [] not a crash")
    return True


def test_parse_rich_grid_layout() -> bool:
    """Newer YouTube layouts nest videos under richItemRenderer — the walk
    must find them regardless of the container chain."""
    grid = {
        "contents": {
            "primaryContents": {
                "richGridRenderer": {
                    "contents": [
                        {
                            "richItemRenderer": {
                                "content": {
                                    "videoRenderer": {
                                        "videoId": "gridVID001",
                                        "title": {"simpleText": "Grid Layout Video"},
                                        "longBylineText": {
                                            "runs": [{"text": "Grid Channel"}]
                                        },
                                    }
                                }
                            }
                        },
                    ]
                }
            }
        }
    }
    videos = yt._extract_videos_from_initial_data(grid)
    assert len(videos) == 1
    assert videos[0]["videoId"] == "gridVID001"
    assert videos[0]["title"] == "Grid Layout Video"
    assert videos[0]["channel"] == "Grid Channel"  # longBylineText fallback
    print("✓ richGridRenderer layout parsed; longBylineText fallback works")
    return True


# ── The voice-facing handler end-to-end (both tiers mocked) ──────────────────


def test_handler_trending_voice_path() -> bool:
    search_data = json.loads(SEARCH_FIXTURE.read_text(encoding="utf-8"))
    shell_data = json.loads(SHELL_FIXTURE.read_text(encoding="utf-8"))

    def fake_get(url, headers=None, timeout=None):
        if "/feed/trending" in url:
            return _page(_html_with(shell_data))
        return _page(_html_with(search_data))

    with patch.object(yt.requests, "get", side_effect=fake_get):
        out = yt.youtube_video({"action": "trending", "region": "IN"})

    assert "Top trending videos" in out
    assert "Yeshanagula" in out
    assert "Keyboard shortcuts" not in out and "Try searching" not in out
    print("✓ handler: spoken/returned text lists real videos, zero chrome")
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
