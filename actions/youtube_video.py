#youtube_video.py
import json
import re
import sys
import time
import subprocess
import shutil
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus

from typing import Any, Iterator

try:
    import pyautogui
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import numpy as np
    _NUMPY = True
except ImportError:
    _NUMPY = False

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    _TRANSCRIPT_OK = True
except ImportError:
    _TRANSCRIPT_OK = False

from config import get_os, is_windows, is_mac, is_linux


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_YT_VIDEO_FILTER = "EgIQAQ%3D%3D"

# Search-upload-date filter: "This week" + sort "View count" — the closest
# public proxy for the trending feed (as of 2026-09 the /feed/trending page
# serves an empty home shell with no video data to anonymous scrapers).
_YT_TRENDING_FILTER = "CAMSBAgEEAE%3D"

_JUNK_TITLES = {
    "try searching to get started",
    "keyboard shortcuts",
    "playback",
    "sign in to like videos, comment and subscribe",
}


def _iter_nodes(node: Any, key: str) -> Iterator[dict]:
    """Yield every dict value stored under `key` anywhere in a nested JSON tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, dict):
                yield v
            else:
                yield from _iter_nodes(v, key)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_nodes(item, key)


def _runs_text(node: dict, field: str) -> str:
    """Extract text from a renderer field shaped as {"runs": [{"text": ...}]} or {"simpleText": ...}."""
    value = node.get(field)
    if isinstance(value, dict):
        if value.get("simpleText"):
            return str(value["simpleText"])
        runs = value.get("runs")
        if isinstance(runs, list) and runs:
            return "".join(str(r.get("text", "")) for r in runs if isinstance(r, dict))
    return ""


def _extract_videos_from_initial_data(data: dict) -> list[dict]:
    """Pull videos out of a parsed ytInitialData tree via its videoRenderer nodes.

    Walking the real JSON (instead of regexing raw HTML) means UI chrome —
    'Keyboard shortcuts', empty-state text — can never be mistaken for a video.
    """
    results: list[dict] = []
    seen: set[str] = set()
    for renderer in _iter_nodes(data, "videoRenderer"):
        video_id = renderer.get("videoId")
        title = _runs_text(renderer, "title")
        if not video_id or not title or video_id in seen:
            continue
        seen.add(video_id)
        results.append({
            "videoId": video_id,
            "title": title,
            "channel": _runs_text(renderer, "ownerText") or _runs_text(renderer, "longBylineText"),
        })
    return results


def _parse_yt_initial_data(html: str) -> dict | None:
    try:
        match = re.search(r"var ytInitialData\s*=\s*(\{.+?\});</script>", html, re.S)
        if not match:
            return None
        return json.loads(match.group(1))
    except (ValueError, TypeError) as e:
        print(f"[YouTube] ⚠️ ytInitialData parse failed: {e}")
        return None


def _scrape_trending_search(region: str, max_results: int) -> list[dict]:
    """Trending proxy: most-viewed videos uploaded this week in `region`.

    Region is best-effort: YouTube geolocates anonymous requests by IP, so
    the gl parameter may be ignored (results follow the server's location).
    Two attempts — a transient empty page has been observed in the wild.
    """
    if not _REQUESTS_OK:
        return []
    url = (
        f"https://www.youtube.com/results"
        f"?search_query=&sp={_YT_TRENDING_FILTER}"
        f"&gl={region.upper()}"
    )
    for attempt in (1, 2):
        try:
            r    = requests.get(url, headers=HEADERS, timeout=12)
            data = _parse_yt_initial_data(r.text)
            if data:
                videos = _extract_videos_from_initial_data(data)
                if videos:
                    return videos[:max_results]
        except Exception as e:
            print(f"[YouTube] ⚠️ Trending search fallback failed (attempt {attempt}): {e}")
    return []


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _open_url(url: str) -> None:
    try:
        if is_mac():
            subprocess.Popen(["open", url])
        elif is_linux():
            subprocess.Popen(["xdg-open", url])
        else:
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
    except Exception as e:
        print(f"[YouTube] ⚠️ open_url failed: {e}")

def _scrape_first_video_url(query: str) -> str | None:

    if not _REQUESTS_OK:
        return None

    search_url = (
        f"https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}"
        f"&sp={_YT_VIDEO_FILTER}"
    )

    try:
        r    = requests.get(search_url, headers=HEADERS, timeout=10)
        html = r.text

        video_ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html)

        seen = set()
        for vid in video_ids:
            if vid in seen:
                continue
            seen.add(vid)

            if f'/shorts/{vid}' in html:
                continue
            return f"https://www.youtube.com/watch?v={vid}"

    except Exception as e:
        print(f"[YouTube] ⚠️ scrape_first_video_url failed: {e}")

    return None

def _extract_video_id(url: str) -> str | None:
    match = re.search(
        r"(?:v=|\/v\/|youtu\.be\/|\/embed\/|\/shorts\/)([A-Za-z0-9_-]{11})", url
    )
    return match.group(1) if match else None


def _is_valid_youtube_url(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url or ""))


def _ask_for_url(prompt_text: str = "YouTube video URL:") -> str | None:
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk._default_root
        if root is None:
            root = tk.Tk()
            root.withdraw()

        url = simpledialog.askstring("J.A.R.V.I.S", prompt_text, parent=root)
        return url.strip() if url else None
    except Exception as e:
        print(f"[YouTube] ⚠️ URL dialog failed: {e}")
        return None


def _get_transcript(video_id: str) -> str | None:
    if not _TRANSCRIPT_OK:
        return None
    try:
        # youtube-transcript-api 1.x renamed list_transcripts() → list(), made
        # it an INSTANCE method (construct first), and made snippets objects
        # with a .text attribute (pre-1.0: dicts). Support both versions.
        if hasattr(YouTubeTranscriptApi, "list_transcripts"):
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)   # pre-1.0: static
        else:
            transcript_list = YouTubeTranscriptApi().list(video_id)             # 1.x: instance method
        transcript      = None

        lang_priority = ["en", "tr", "de", "fr", "es", "it", "pt", "ru", "ja", "ko", "ar", "zh"]

        try:
            transcript = transcript_list.find_manually_created_transcript(lang_priority)
        except Exception:
            pass

        if transcript is None:
            try:
                transcript = transcript_list.find_generated_transcript(lang_priority)
            except Exception:
                for t in transcript_list:
                    transcript = t
                    break

        if transcript is None:
            return None

        fetched = transcript.fetch()
        texts = []
        for entry in fetched:
            t = getattr(entry, "text", None)
            if t is None and isinstance(entry, dict):
                t = entry.get("text")
            if t:
                texts.append(t)
        return " ".join(texts)

    except Exception as e:
        print(f"[YouTube] ⚠️ Transcript fetch failed: {e}")
        return None


def _summarize_with_gemini(transcript: str, video_url: str) -> str:
    from google.genai import types

    max_chars = 80000
    truncated = transcript[:max_chars] + ("..." if len(transcript) > max_chars else "")
    # Mark-LIV ladder: a whole transcript can be 80k characters, hence the
    # long deadline — but a deadline there is, and the ladder in
    # core/gemini.py picks the model.
    from core import gemini
    response = gemini.call(
        f"Please summarize this YouTube video transcript:\n\n{truncated}",
        tier=gemini.SMART,
        timeout_ms=60_000,
        config=types.GenerateContentConfig(
            system_instruction=(
                "You are JARVIS, an AI assistant. "
                "Summarize YouTube video transcripts clearly and concisely. "
                "Structure: 1-sentence overview, then 3-5 key points. "
                "Be direct. Address the user as 'sir'. "
                "Match the language of the transcript."
            )
        )
    )
    if response is None:
        return "I couldn't reach Gemini to summarise that transcript, sir."
    return (response.text or "").strip()


def _save_summary(content: str, video_url: str) -> str:
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"youtube_summary_{ts}.txt"
    desktop  = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    filepath = desktop / filename

    header = (
        f"JARVIS — YouTube Summary\n"
        f"{'─' * 50}\n"
        f"URL    : {video_url}\n"
        f"Date   : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"{'─' * 50}\n\n"
    )
    filepath.write_text(header + content, encoding="utf-8")

    try:
        if is_windows():
            subprocess.Popen(["notepad.exe", str(filepath)])
        elif is_mac():
            subprocess.Popen(["open", "-t", str(filepath)])
        else:
            subprocess.Popen(["xdg-open", str(filepath)])
    except Exception as e:
        print(f"[YouTube] ⚠️ Could not open text editor: {e}")

    return str(filepath)


def _scrape_video_info(video_id: str) -> dict:
    if not _REQUESTS_OK:
        return {}
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        r    = requests.get(url, headers=HEADERS, timeout=12)
        html = r.text
        info = {}

        for key, pattern in [
            ("title",    r'"title":\{"runs":\[\{"text":"([^"]+)"'),
            ("channel",  r'"ownerChannelName":"([^"]+)"'),
            ("views",    r'"viewCount":"(\d+)"'),
            ("duration", r'"lengthSeconds":"(\d+)"'),
            ("likes",    r'"label":"([0-9,]+ likes)"'),
        ]:
            match = re.search(pattern, html)
            if match:
                raw = match.group(1)
                if key == "views":
                    info[key] = f"{int(raw):,}"
                elif key == "duration":
                    secs = int(raw)
                    info[key] = f"{secs // 60}:{secs % 60:02d}"
                else:
                    info[key] = raw

        return info
    except Exception as e:
        print(f"[YouTube] ⚠️ Info scrape failed: {e}")
        return {}


def _scrape_trending(region: str = "TR", max_results: int = 8) -> list[dict]:
    """Fetch trending videos for a region, with layered fallbacks.

    Order: (1) /feed/trending ytInitialData — works when YouTube serves real
    feed data again; (2) search proxy — most-viewed uploads this week. Videos
    are only taken from parsed videoRenderer JSON, so page chrome can never
    masquerade as a result.
    """
    if not _REQUESTS_OK:
        return []
    url = f"https://www.youtube.com/feed/trending?gl={region.upper()}&hl=en"
    videos: list[dict] = []
    try:
        r      = requests.get(url, headers=HEADERS, timeout=12)
        data   = _parse_yt_initial_data(r.text)
        videos = _extract_videos_from_initial_data(data) if data else []
    except Exception as e:
        print(f"[YouTube] ⚠️ Trending feed scrape failed: {e}")

    if not videos:
        print("[YouTube] ⚠️ Trending feed empty — falling back to search proxy")
        videos = _scrape_trending_search(region, max_results)

    # The blocklist guards BOTH tiers: page chrome must never be announced,
    # no matter which page it leaked in from.
    return [
        {"rank": i + 1, "title": v["title"], "channel": v["channel"]}
        for i, v in enumerate(videos[:max_results])
        if v["title"].strip().lower() not in _JUNK_TITLES
    ]

def _handle_play(parameters: dict, player) -> str:
    query = parameters.get("query", "").strip()
    if not query:
        return "Please tell me what you'd like to watch, sir."

    if player:
        player.write_log(f"[YouTube] Searching: {query}")

    print(f"[YouTube] 🔍 Scraping first non-Shorts video for: {query}")

    video_url = _scrape_first_video_url(query)

    if video_url:
        print(f"[YouTube] ▶️ Opening: {video_url}")
        _open_url(video_url)
        return f"Playing: {query}"

    print(f"[YouTube] ⚠️ Scrape failed, opening filtered search page")
    fallback_url = (
        f"https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}"
        f"&sp={_YT_VIDEO_FILTER}"
    )
    _open_url(fallback_url)
    return f"Opened YouTube search for: {query} (manual selection required)"


def _handle_summarize(parameters: dict, player, speak) -> str:
    if not _TRANSCRIPT_OK:
        return "youtube-transcript-api is not installed. Run: pip install youtube-transcript-api"

    url = _ask_for_url("Please paste the YouTube video URL:")
    if not url:
        return "No URL provided, sir. Summary cancelled."
    if not _is_valid_youtube_url(url):
        return "That doesn't appear to be a valid YouTube URL, sir."

    video_id = _extract_video_id(url)
    if not video_id:
        return "Could not extract video ID from that URL, sir."

    if player:
        player.write_log(f"[YouTube] Summarizing: {url}")
    if speak:
        speak("Fetching the transcript now, sir. One moment.")

    transcript = _get_transcript(video_id)
    if not transcript:
        return "I couldn't retrieve a transcript for that video, sir."

    if speak:
        speak("Transcript retrieved. Generating summary now.")

    try:
        summary = _summarize_with_gemini(transcript, url)
    except Exception as e:
        return f"Summary generation failed, sir: {e}"

    if speak:
        speak(summary)

    if parameters.get("save", False):
        saved_path = _save_summary(summary, url)
        return f"Summary complete and saved to Desktop: {saved_path}"

    return summary


def _handle_get_info(parameters: dict, player, speak) -> str:
    url = parameters.get("url", "").strip()
    if not url:
        url = _ask_for_url("Please paste the YouTube video URL:")
    if not url or not _is_valid_youtube_url(url):
        return "Please provide a valid YouTube URL, sir."

    video_id = _extract_video_id(url)
    if not video_id:
        return "Could not extract video ID, sir."

    if player:
        player.write_log(f"[YouTube] Getting info: {url}")

    info = _scrape_video_info(video_id)
    if not info:
        return "Could not retrieve video information, sir."

    lines = [
        f"{key.capitalize()}: {info[key]}"
        for key in ("title", "channel", "views", "duration", "likes")
        if key in info
    ]
    result = "\n".join(lines)

    if speak:
        speak(f"Here's the video info, sir. {result.replace(chr(10), '. ')}")

    return result


def _handle_trending(parameters: dict, player, speak) -> str:
    region = parameters.get("region", "TR").upper()

    if player:
        player.write_log(f"[YouTube] Trending: {region}")

    trending = _scrape_trending(region=region, max_results=8)
    if not trending:
        return f"Could not fetch trending videos for region {region}, sir."

    lines  = [f"Top trending videos in {region}:"]
    lines += [f"{v['rank']}. {v['title']} — {v['channel']}" for v in trending]
    result = "\n".join(lines)

    if speak:
        top3   = trending[:3]
        spoken = "Here are the top trending videos, sir. " + ". ".join(
            f"Number {v['rank']}: {v['title']} by {v['channel']}" for v in top3
        )
        speak(spoken)

    return result

_ACTION_MAP = {
    "play":      _handle_play,
    "summarize": _handle_summarize,
    "get_info":  _handle_get_info,
    "trending":  _handle_trending,
}


def youtube_video(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    params = parameters or {}
    action = params.get("action", "play").lower().strip()

    if player:
        player.write_log(f"[YouTube] Action: {action}")
    print(f"[YouTube] ▶️  Action: {action}  Params: {params}")

    handler = _ACTION_MAP.get(action)
    if handler is None:
        return (
            f"Unknown YouTube action: '{action}'. "
            "Available: play, summarize, get_info, trending."
        )

    try:
        if action == "play":
            return handler(params, player) or "Done."
        return handler(params, player, speak) or "Done."
    except Exception as e:
        print(f"[YouTube] ❌ Error in {action}: {e}")
        return f"YouTube {action} failed, sir: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "youtube_video",
    "description": "Controls YouTube. Use for: playing videos, summarizing a video's content, getting video info, or showing trending videos.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "play | summarize | get_info | trending (default: play)"
            },
            "query": {
                "type": "STRING",
                "description": "Search query for play action"
            },
            "save": {
                "type": "BOOLEAN",
                "description": "Save summary to Notepad (summarize only)"
            },
            "region": {
                "type": "STRING",
                "description": "Country code for trending e.g. TR, US (best-effort: YouTube geolocates anonymous requests by IP)"
            },
            "url": {
                "type": "STRING",
                "description": "Video URL for get_info action"
            }
        },
        "required": []
    },
    "handler": youtube_video,
}
