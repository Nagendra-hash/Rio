"""
core/video_agent/stock.py — Phase 7: licensed stock-footage providers.

Mission rules encoded here:

  * stock clips are REAL footage — every downloaded clip carries source and
    license metadata (provider, clip id, author, license, query) written
    into the run's assets manifest, and the final video is NEVER presented
    as newly AI-generated
  * providers are configured EXPLICITLY by the user
    (repo_agent.stock = {provider: pexels, api_key_ref: …}) — no keys, no
    network calls, honest NOT_CONFIGURED
  * clips are fetched per project, small and bounded (per-clip size cap),
    downloaded to .part files renamed atomically
  * the HTTP layer is urllib with strict timeouts; tests mock it
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

__all__ = ["get_provider", "download_clip", "SUPPORTED"]

SUPPORTED = ("pexels",)
MAX_CLIP_MB = 120
_SEARCH_TIMEOUT_S = 20
_DOWNLOAD_TIMEOUT_S = 120


def stock_config() -> dict[str, Any]:
    try:
        from memory.config_manager import load_api_keys
        section = (load_api_keys() or {}).get("repo_agent") or {}
        stock = section.get("stock") or {}
        return stock if isinstance(stock, dict) else {}
    except Exception:                              # noqa: BLE001
        return {}


def _resolve_key(api_key_ref: str) -> str:
    """api_key_ref = dotted path into config/api_keys.json (the house
    pattern from core/repo_agent/vimax.py)."""
    try:
        from memory.config_manager import load_api_keys
        data = load_api_keys() or {}
        node: Any = data
        for part in str(api_key_ref or "").split("."):
            if not part:
                continue
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                return ""
        return str(node or "")
    except Exception:                              # noqa: BLE001
        return ""


class PexelsProvider:
    """Pexels Videos API — https://www.pexels.com/api/ (free tier, key).
    License: Pexels License (free to use, attribution appreciated)."""

    name = "pexels"
    license = "Pexels License — free to use, attribution appreciated"
    endpoint = "https://api.pexels.com/videos/search"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def search(self, query: str, per_page: int = 4) -> list[dict[str, Any]]:
        url = (f"{self.endpoint}?query={urllib.parse.quote(query)}"
               f"&per_page={max(1, min(int(per_page), 8))}"
               f"&orientation=landscape&size=medium")
        req = urllib.request.Request(url, headers={
            "Authorization": self.api_key,
            "User-Agent": "jarvis-video-studio/1.0"})
        with urllib.request.urlopen(req, timeout=_SEARCH_TIMEOUT_S) as resp:
            data = json.loads((resp.read() or b"{}").decode("utf-8",
                                                            "replace"))
        clips: list[dict[str, Any]] = []
        for video in (data.get("videos") or [])[:per_page]:
            files = [f for f in (video.get("video_files") or [])
                     if str(f.get("file_type")) == "video/mp4"]
            if not files:
                continue
            best = min(files, key=lambda f: abs(
                int(f.get("height") or 0) - 720))
            clips.append({
                "provider": self.name,
                "clip_id": str(video.get("id")),
                "url": str(best.get("link") or ""),
                "width": int(best.get("width") or 0),
                "height": int(best.get("height") or 0),
                "duration_s": float(video.get("duration") or 0),
                "author": str((video.get("user") or {}).get("name")
                              or "unknown"),
                "page_url": str(video.get("url") or ""),
                "license": self.license,
                "query": query,
            })
        return clips


def get_provider() -> tuple[Any | None, str]:
    """The configured provider, or (None, reason). Config-level only."""
    config = stock_config()
    provider_name = str(config.get("provider") or "").strip().lower()
    if not provider_name:
        return None, ("no stock provider configured — set "
                      "repo_agent.stock = {provider: 'pexels', "
                      "api_key_ref: 'pexels_api_key'} in "
                      "config/api_keys.json")
    if provider_name not in SUPPORTED:
        return None, (f"provider '{provider_name}' not supported yet "
                      f"({', '.join(SUPPORTED)})")
    key = _resolve_key(str(config.get("api_key_ref") or "stock_api_key"))
    if not key:
        return None, (f"provider '{provider_name}' configured but the API "
                      f"key is missing (api_key_ref="
                      f"{config.get('api_key_ref')})")
    return PexelsProvider(key), ""


def download_clip(clip: dict[str, Any], dest: Path,
                  progress: Any = None) -> dict[str, Any]:
    """Download one clip (bounded, .part-atomic) and return its license
    metadata record. Raises on failure; never leaves a .part behind."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    limit = MAX_CLIP_MB * 1024 * 1024
    url = str(clip.get("url") or "")
    if not url:
        raise ValueError("clip has no url")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "jarvis-video-studio/1.0"})
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_S) as \
                resp, open(part, "wb") as f:
            done = 0
            while True:
                chunk = resp.read(1024 * 512)
                if not chunk:
                    break
                done += len(chunk)
                if done > limit:
                    raise ValueError(f"clip exceeds {MAX_CLIP_MB} MB cap")
                f.write(chunk)
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return {
        "provider": clip.get("provider"),
        "clip_id": clip.get("clip_id"),
        "url": clip.get("page_url") or url,
        "file_url": url,
        "author": clip.get("author"),
        "license": clip.get("license"),
        "query": clip.get("query"),
        "width": clip.get("width"), "height": clip.get("height"),
        "file": str(dest),
        "size_bytes": dest.stat().st_size,
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime()),
        "ai_generated": False,
    }


def probe() -> dict[str, Any]:
    """Config-level honesty for the registry (no network in probe)."""
    provider, reason = get_provider()
    if provider is None:
        return {"configured": False, "reason": reason}
    return {"configured": True, "provider": provider.name,
            "license": provider.license}
