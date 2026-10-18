"""
orchestrator/agents/replica/store.py — read a Google Play listing, honestly.

Reads ONLY the public Play Store listing: title, developer, category, rating,
install count, description, "What's new" and the public screenshots. It never
touches an APK, a mirror site or the app's servers — ``policy`` refuses those
before a request is made.

The Play Store page is not an API and its markup changes; the parser is
deliberately best-effort and reports exactly which fields it found. When the
network is unavailable (or the page changes shape), the caller can pass a
manual ``listing.json`` instead — the rest of the pipeline is identical.

Stdlib only (urllib + re), no dependencies.
"""
from __future__ import annotations

import gzip
import html as _html
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from orchestrator.agents.replica import policy

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_LISTING_BYTES = 4_000_000

_OG = lambda prop: re.compile(
    r'<meta[^>]+property=["\']og:%s["\'][^>]+content=["\']([^"\']*)["\']'
    % prop, re.I)
_META = lambda name: re.compile(
    r'<meta[^>]+name=["\']%s["\'][^>]+content=["\']([^"\']*)["\']' % name, re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
# The displayed rating carries an accessible label ("Rated 4.7 stars out of
# five stars"); a data-blob guess is not good enough to publish a number.
_RATED = re.compile(r"Rated\s+([0-9](?:\.[0-9]{1,2})?)\s+stars", re.I)
_INSTALLS = re.compile(r"([\d.,]+\s?[KMB]?\+?)\s*(?:downloads|installs)", re.I)
_APP_SUFFIX = re.compile(r"\s*[-\u2013]\s*Apps on Google Play.*$", re.I)
_CAT_LINK = re.compile(r"/store/apps/category/([A-Z_]+)")
_VISIBLE_TEXT = re.compile(r">([A-Za-z][A-Za-z0-9 &'\-]{2,30})<")
_SCREENSHOT = re.compile(
    r"https://play-lh\.googleusercontent\.com/[A-Za-z0-9_\-]+"
    r"(?:=[a-z0-9\-]+)?", re.I)
# Play embeds long JSON strings; pick the biggest description-looking blob.
_JSON_STR = re.compile(r'"((?:[^"\\]|\\.){120,})"')


def play_url_parts(url: str) -> tuple[str, str, str]:
    """(package, hl, gl) from a Play Store URL. ('', 'en', 'US') when it is
    not a Play URL shape — the caller then treats the input as a manual
    source."""
    url = (url or "").strip()
    pkg = ""
    m = re.search(r"[?&]id=([A-Za-z0-9_.]+)", url)
    if m:
        pkg = m.group(1)
    else:                                   # /store/apps/details/<pkg>
        m = re.search(r"/details/([A-Za-z0-9_.]+)", url)
        if m:
            pkg = m.group(1)
    hl_m = re.search(r"[?&]hl=([A-Za-z\-]+)", url)
    gl_m = re.search(r"[?&]gl=([A-Za-z]+)", url)
    return pkg, (hl_m.group(1) if hl_m else "en"), (gl_m.group(1) if gl_m else "US")


def _get(url: str, timeout: int = 20) -> tuple[bool, str, str]:
    """Fetch a URL as text. Returns (ok, text, error). Never raises."""
    check = policy.assert_source_allowed(url)
    if not check.allowed:
        return False, "", check.reason
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",     # no gzip: urllib won't inflate
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_LISTING_BYTES)
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return True, raw.decode("utf-8", errors="replace"), ""
    except urllib.error.HTTPError as exc:
        return False, "", f"HTTP {exc.code}"
    except Exception as exc:                       # URLError, timeout, …
        return False, "", f"{type(exc).__name__}: {exc}"


def _extract_category(page: str) -> str:
    """The app's OWN category, not a nav / similar-apps link.

    Play links every category it mentions. The app's own one is a chip that
    appears after the title and repeats its label as visible page text; a
    slug that never shows as text (a nav tab like "Family") is skipped. When
    no slug has a matching visible label, nothing is guessed.
    """
    ref = -1
    for marker in ("og:title", "<title"):
        i = page.find(marker)
        if i != -1:
            ref = i
            break
    if ref == -1:
        return ""
    visible = {re.sub(r"[^a-z0-9]", "", t.lower())
               for t in _VISIBLE_TEXT.findall(page)}
    for m in _CAT_LINK.finditer(page):
        if m.start() < ref:
            continue
        label = m.group(1).replace("_", " ")
        if re.sub(r"[^a-z0-9]", "", label.lower()) in visible:
            return label.title()
    return ""


def parse_listing_html(page: str, url: str = "") -> dict:
    """Best-effort extraction of the public listing fields from page HTML."""
    page = page or ""
    data: dict = {"url": url, "found": []}

    def take(key, value):
        value = (value or "").strip()
        if value:
            data[key] = _html.unescape(value)
            data["found"].append(key)

    take("name", _APP_SUFFIX.sub(
        "", (_OG("title").search(page) or [None, ""])[1]))
    if not data.get("name"):
        m = _TITLE.search(page)
        if m:
            take("name", _APP_SUFFIX.sub("", m.group(1)))
    take("short_description", (_META("description").search(page) or [None, ""])[1])
    take("icon", (_OG("image").search(page) or [None, ""])[1])
    take("developer", (_META("author").search(page) or [None, ""])[1])

    # Rating: the displayed accessible label only. A data-blob guess is not
    # good enough to publish a number, so a miss leaves it unset.
    m = _RATED.search(page)
    if m:
        try:
            r = float(m.group(1))
            if 1.0 <= r <= 5.0:
                data["rating"] = r
                data["found"].append("rating")
        except ValueError:
            pass
    m = _INSTALLS.search(page)
    if m:
        take("installs", m.group(1))

    cat = _extract_category(page)
    if cat:
        take("category", cat)

    # Long description: the longest JSON-escaped string with enough words.
    best = ""
    for raw in _JSON_STR.findall(page):
        try:
            s = json.loads('"' + raw + '"')
        except Exception:
            continue
        if s.count(" ") >= 12 and len(s) > len(best):
            best = s
    if best:
        take("description", best)

    # "What's new" heuristically: the JSON string right after a data-reco.
    m = re.search(r'\[\s*"([^"]{0,40})"\s*,\s*null[^\]]{0,80}\],\s*\[\s*'
                  r'"((?:[^"\\]|\\.){40,4000})"\s*\]', page)
    if m:
        try:
            take("whats_new", json.loads('"' + m.group(2) + '"'))
        except Exception:
            pass

    shots, seen = [], set()
    for u in _SCREENSHOT.findall(page):
        base = u.split("=")[0]
        if base in seen:
            continue
        seen.add(base)
        shots.append(u)
    if shots:
        data["screenshots"] = shots[:12]
        data["found"].append("screenshots")
    return data


def fetch_listing(url: str, timeout: int = 20) -> dict:
    """Fetch + parse a Play listing. Returns a dict; never raises.

    ``{"ok": bool, "url", "package", "error", ...fields, "found": [...]}``.
    """
    check = policy.assert_source_allowed(url)
    if not check.allowed:
        return {"ok": False, "url": url, "error": check.reason, "found": []}
    pkg, hl, gl = play_url_parts(url)
    ok, text, err = _get(url, timeout=timeout)
    if not ok:
        return {"ok": False, "url": url, "package": pkg,
                "error": err or "could not fetch the listing", "found": []}
    data = parse_listing_html(text, url)
    data.update({"ok": True, "package": pkg, "raw_bytes": len(text)})
    return data


def load_manual_listing(path: str | os.PathLike) -> dict:
    """Read a hand-saved listing.json (the offline path). Never raises."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("listing.json must be an object")
        data.setdefault("found", sorted(k for k in data
                                        if k not in ("found", "ok", "error")))
        data["ok"] = True
        return data
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "found": []}


def download_screenshots(urls: list[str], out_dir: str | os.PathLike,
                         limit: int = 8) -> list[str]:
    """Save the original's public store screenshots for reference only.

    These live under ``replica/screens/`` which is never shipped and is
    excluded from the rebrand sweep's target. Returns the saved paths.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for i, url in enumerate((urls or [])[:limit], 1):
        ok, _, err = _get(url, timeout=20)
        if not ok:
            continue
        try:
            # _get decodes as text; re-fetch bytes for an image safely.
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                blob = resp.read(6_000_000)
            dest = out / f"S{i:02d}.png"
            dest.write_bytes(blob)
            saved.append(str(dest))
        except Exception:
            continue
    return saved


def listing_to_description(listing: dict) -> str:
    """A compact text block the planner can reason over."""
    if not listing or not listing.get("ok"):
        return ""
    parts = []
    if listing.get("name"):
        parts.append(f"App: {listing['name']}")
    if listing.get("category"):
        parts.append(f"Category: {listing['category']}")
    if listing.get("rating") is not None:
        parts.append(f"Rating: {listing['rating']}")
    if listing.get("installs"):
        parts.append(f"Installs: {listing['installs']}")
    if listing.get("short_description"):
        parts.append(f"Tagline: {listing['short_description']}")
    if listing.get("description"):
        parts.append("Description:\n" + listing["description"][:4000])
    if listing.get("whats_new"):
        parts.append("What's new:\n" + listing["whats_new"][:1000])
    return "\n".join(parts)
