"""actions/news_feed.py — live, structured news for the RIO newsroom.

Two kinds of real item, fetched from the network (never invented):

  * headline  — live articles scraped from public RSS/Atom feeds
                (BBC, CNBC, WSJ, Al Jazeera, MarketWatch) or, for a topic
                query, Google News RSS with DuckDuckGo News as fallback.
  * calendar  — Forex Factory economic-calendar events. We first try the
                public calendar HTML (a real scrape); Forex Factory fronts
                that page with Cloudflare, so when it answers 403/challenge
                we fall back to Forex Factory's own public weekly calendar
                JSON feed (nfs.faireconomy.media). The payload says which
                path was used — the UI never pretends an HTML scrape
                happened when it did not.

Everything is bounded: per-request timeout, small worker pool, hard caps on
items, and a 90 s result cache so polling the card page cannot hammer the
publishers. A failing source is reported in ``sources`` with its real error;
other sources still return their items.

Public entry points:
    collect_news(query="", limit=30, kind="all", impact="", currency="")
    fetch_headlines(limit=30, query="")
    fetch_forex_factory(impact="", currency="", limit=40)
    parse_feed(blob, source)          # offline-testable RSS/Atom parser
    parse_forex_html(html, url="")    # offline-testable calendar scraper
    parse_forex_feed(payload)         # offline-testable JSON feed parser
"""
from __future__ import annotations

import copy
import hashlib
import html as _html
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse
import xml.etree.ElementTree as ET

import requests

try:                                    # bs4 is in requirements; regex fallback
    from bs4 import BeautifulSoup
except Exception:                       # pragma: no cover
    BeautifulSoup = None

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 RIO-Newsroom/1.0")
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

FF_HOME = "https://www.forexfactory.com/calendar"
FF_FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Public feeds that answered 200 in verification (2026-10-20). Each is a real
# publisher feed, not an aggregator scrape.
RSS_SOURCES = (
    {"key": "bbc", "name": "BBC News", "section": "World",
     "url": "https://feeds.bbci.co.uk/news/rss.xml"},
    {"key": "cnbc", "name": "CNBC", "section": "Markets",
     "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
    {"key": "wsj", "name": "WSJ Markets", "section": "Markets",
     "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"},
    {"key": "aljazeera", "name": "Al Jazeera", "section": "World",
     "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"key": "marketwatch", "name": "MarketWatch", "section": "Markets",
     "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
)

_CALENDAR_RE = re.compile(
    r"\b(forex\s*factory|forex\s*calendar|economic\s+calendar|"
    r"economic\s+events?|market\s+calendar|calendar\s+events?)\b", re.I)

_IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2, "holiday": 3, "": 4}
_CACHE_TTL = 90.0
_cache: dict = {}
_cache_lock = threading.Lock()


# ── small helpers ────────────────────────────────────────────────────────────

def is_calendar_query(text: str) -> bool:
    """True when the ask is about the economic calendar, not general news."""
    return bool(_CALENDAR_RE.search(str(text or "")))


def _local(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _clip(text: str, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if limit and len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _clean_html(text: str, limit: int | None = None) -> str:
    raw = _html.unescape(str(text or ""))
    if BeautifulSoup is not None:
        cleaned = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    else:                               # pragma: no cover - bs4 is installed
        cleaned = re.sub(r"<[^>]+>", " ", raw)
    # get_text(" ") separates inline tags from following punctuation
    cleaned = re.sub(r"\s+([.,;:!?%])", r"\1", cleaned)
    return _clip(cleaned, limit)


def _iso_utc(value: str) -> str:
    """RFC-822 or ISO timestamp → ISO-8601 UTC ('' when unparsable)."""
    value = str(value or "").strip()
    if not value:
        return ""
    dt = None
    try:
        dt = parsedate_to_datetime(value)
    except Exception:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return ""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _item_id(kind: str, *parts: str) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return f"{kind}-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def _dedupe(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        title = re.sub(r"[^a-z0-9]+", "", (item.get("title") or "").lower())
        url = (item.get("url") or "").strip().lower()
        if item.get("kind") == "calendar":
            # every calendar event shares the Forex Factory URL, so dedupe
            # on its own id (currency + title + timestamp) instead.
            key = item.get("id") or f"{title}|{item.get('published') or ''}"
        else:
            key = url or title
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# ── RSS / Atom scraping ──────────────────────────────────────────────────────

def parse_feed(blob: bytes | str, source: dict) -> list[dict]:
    """Parse RSS 2.0 or Atom into structured headline items.

    Kept pure (bytes/str in, list out) so tests never touch the network.
    """
    if isinstance(blob, str):
        blob = blob.encode("utf-8")
    root = ET.fromstring(blob)
    entries = [el for el in root.iter()
               if _local(el.tag) in ("item", "entry")]

    out: list[dict] = []
    for el in entries:
        fields: dict[str, str] = {}
        atom_link = ""
        source_name = ""
        for child in el:
            name = _local(child.tag)
            text = (child.text or "").strip()
            if name == "link":
                if child.get("href"):
                    rel = (child.get("rel") or "alternate").lower()
                    if rel == "alternate" or not atom_link:
                        atom_link = child.get("href") or ""
                elif text:
                    fields["link"] = text
            elif name == "source":
                source_name = _clean_html(text, 80)
            elif name in ("title", "description", "summary", "pubdate",
                          "published", "updated", "content"):
                if text and name not in fields:
                    fields[name] = text

        title = _clean_html(fields.get("title", ""), 220)
        url = (fields.get("link") or atom_link or "").strip()
        if not title or not url:
            continue
        summary = _clean_html(
            fields.get("description") or fields.get("summary") or
            fields.get("content") or "", 320)
        # Google News repeats the publisher in the title: "Headline - Publisher".
        pub = source_name or source.get("name") or _domain(url)
        if source_name and title.endswith(" - " + source_name):
            title = title[: -(len(source_name) + 3)].rstrip()
        # Google News descriptions are auto-generated lists of related
        # headlines, not a summary — showing them duplicates the title and
        # appends unrelated stories. Keep the card honest: no summary.
        if source.get("key") == "google":
            summary = ""
        elif source_name and summary.lower().startswith(title.lower()):
            rest = summary[len(title):].strip(" -–—·|:")
            if not rest or rest.lower() == source_name.lower():
                summary = ""

        out.append({
            "id": _item_id("headline", url, title),
            "kind": "headline",
            "title": title,
            "summary": summary,
            "url": url,
            "source": pub,
            "section": source.get("section", ""),
            "published": _iso_utc(
                fields.get("pubdate") or fields.get("published") or
                fields.get("updated") or ""),
            "domain": _domain(url),
            "currency": "", "impact": "", "forecast": "",
            "previous": "", "actual": "", "date_label": "",
            "time_label": "", "tz_label": "",
        })
    return out


def _fetch_rss_source(src: dict, limit: int) -> tuple[list[dict], dict]:
    started = time.monotonic()
    try:
        resp = requests.get(src["url"], headers=_HEADERS, timeout=8)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        items = parse_feed(resp.content, src)[:limit]
        return items, {
            "name": src["name"], "ok": True, "count": len(items),
            "ms": int((time.monotonic() - started) * 1000),
            "detail": "RSS/Atom feed", "mode": "rss",
        }
    except Exception as exc:
        return [], {
            "name": src["name"], "ok": False, "count": 0,
            "ms": int((time.monotonic() - started) * 1000),
            "detail": f"{type(exc).__name__}: {exc}"[:160], "mode": "rss",
        }


def fetch_headlines(limit: int = 30, query: str = "") -> tuple[list[dict], list[dict]]:
    """Live headline items + per-source status.

    With a query: Google News RSS first (scraped query feed), DDG News second.
    Without one: the publisher feeds in RSS_SOURCES, fetched in parallel.
    """
    limit = max(1, min(60, int(limit or 30)))
    q = _clip(query, 160)

    if q:
        gsrc = {"key": "google", "name": "Google News", "section": "Search",
                "url": ("https://news.google.com/rss/search?q=" +
                        quote_plus(q) + "&hl=en-US&gl=US&ceid=US:en")}
        items, status = _fetch_rss_source(gsrc, limit)
        if items:
            return items, [status]

        # DDG News — the existing structured search backend.
        started = time.monotonic()
        try:
            from actions.web_search import _ddg_news
            rows = _ddg_news(q, max_results=limit) or []
            out = []
            for row in rows:
                title = _clip(row.get("title"), 220)
                url = str(row.get("url") or "").strip()
                if not title or not url:
                    continue
                src = _clip(row.get("source"), 80) or _domain(url)
                out.append({
                    "id": _item_id("headline", url, title),
                    "kind": "headline", "title": title,
                    "summary": _clip(row.get("snippet"), 320),
                    "url": url, "source": src, "section": "Search",
                    "published": "", "domain": _domain(url),
                    "currency": "", "impact": "", "forecast": "",
                    "previous": "", "actual": "", "date_label": "",
                    "time_label": "", "tz_label": "",
                })
            ddg_status = {
                "name": "DuckDuckGo News", "ok": bool(out),
                "count": len(out),
                "ms": int((time.monotonic() - started) * 1000),
                "detail": "search fallback" if out else "no rows",
                "mode": "search",
            }
            return _dedupe(out)[:limit], [status, ddg_status]
        except Exception as exc:
            return [], [status, {
                "name": "DuckDuckGo News", "ok": False, "count": 0,
                "ms": int((time.monotonic() - started) * 1000),
                "detail": f"{type(exc).__name__}: {exc}"[:160],
                "mode": "search",
            }]

    items: list[dict] = []
    statuses: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(RSS_SOURCES),
                            thread_name_prefix="news-rss") as pool:
        futures = [pool.submit(_fetch_rss_source, src, limit)
                   for src in RSS_SOURCES]
        for fut in as_completed(futures):
            rows, status = fut.result()
            items.extend(rows)
            statuses.append(status)

    statuses.sort(key=lambda s: s["name"])
    items = _dedupe(items)
    items.sort(key=lambda i: i.get("published") or "", reverse=True)
    return items[:limit], statuses


# ── Forex Factory ────────────────────────────────────────────────────────────

def _ff_impact(text: str) -> str:
    t = str(text or "").lower()
    if "high" in t or "red" in t:
        return "High"
    if "medium" in t or "orange" in t:
        return "Medium"
    if "holiday" in t:
        return "Holiday"
    if "low" in t or "yellow" in t:
        return "Low"
    return ""


def _ff_impact_from_cell(cell) -> str:
    if cell is None:
        return ""
    span = cell.find("span")
    title = (span.get("title") if span is not None else "") or ""
    if not title:
        title = cell.get("title") or ""
    classes = " ".join(cell.get("class") or [])
    if span is not None:
        classes += " " + " ".join(span.get("class") or [])
    impact = _ff_impact(title) or _ff_impact(classes)
    return impact


def _ff_date(text: str, default_year: int | None = None) -> datetime | None:
    t = _clip(text, 80)
    t = re.sub(r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+",
               "", t, flags=re.I)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d", "%b %d", "%d %B %Y",
                "%d %b %Y"):
        try:
            dt = datetime.strptime(t, fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=default_year or datetime.now().year)
            return dt
        except ValueError:
            continue
    return None


def _ff_time(text: str) -> tuple[int, int] | None:
    t = _clip(text, 40).lower().replace("\u202f", " ")
    if not t or "all day" in t or t in {"day", "tentative"}:
        return None
    for fmt in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            dt = datetime.strptime(t.replace(" ", ""), fmt)
            return dt.hour, dt.minute
        except ValueError:
            continue
    return None


def parse_forex_html(page: str | bytes, url: str = FF_HOME) -> list[dict]:
    """Scrape Forex Factory calendar rows into structured events.

    Pure parser (no network) with the selectors Forex Factory's public
    calendar markup uses. Returns [] when the page is a Cloudflare challenge.
    """
    if BeautifulSoup is None:           # pragma: no cover
        return []
    text = page.decode("utf-8", "replace") if isinstance(page, bytes) else page
    if not text or "Just a moment" in text or "cf-challenge" in text:
        return []
    soup = BeautifulSoup(text, "html.parser")
    rows = soup.select("tr.calendar__row")
    if not rows:
        return []

    out: list[dict] = []
    current_day: datetime | None = None
    for row in rows:
        classes = " ".join(row.get("class") or [])
        day_cell = row.select_one(".calendar__date")
        if "day-breaker" in classes:
            current_day = _ff_date(day_cell.get_text(" ", strip=True)
                                   if day_cell else "") or current_day
            continue
        if day_cell is not None:
            current_day = _ff_date(day_cell.get_text(" ", strip=True),
                                   current_day.year if current_day else None) \
                or current_day

        title_el = (row.select_one(".calendar__event-title") or
                    row.select_one(".calendar__event"))
        title = _clip(title_el.get_text(" ", strip=True)
                      if title_el else "", 200)
        if not title or title.lower() in {"day", "date"}:
            continue
        cur_el = row.select_one(".calendar__currency")
        time_el = row.select_one(".calendar__time")
        impact_el = row.select_one(".calendar__impact")
        currency = _clip(cur_el.get_text(" ", strip=True) if cur_el else "", 8)
        time_text = _clip(time_el.get_text(" ", strip=True)
                          if time_el else "", 30)
        hm = _ff_time(time_text)
        impact = _ff_impact_from_cell(impact_el)

        published = ""
        date_label = ""
        time_label = time_text
        if current_day is not None:
            dt = current_day
            if hm:
                dt = current_day.replace(hour=hm[0], minute=hm[1])
                time_label = f"{hm[0]:02d}:{hm[1]:02d}"
            published = dt.isoformat()
            date_label = dt.strftime("%a %d %b")

        def val(sel: str) -> str:
            el = row.select_one(sel)
            return _clip(el.get_text(" ", strip=True) if el else "", 40)

        forecast, previous, actual = (val(".calendar__forecast"),
                                      val(".calendar__previous"),
                                      val(".calendar__actual"))
        summary_parts = [p for p in (
            f"{currency} · {impact} impact" if impact else currency,
            f"Forecast {forecast}" if forecast else "",
            f"Previous {previous}" if previous else "",
            f"Actual {actual}" if actual else "",
        ) if p]
        out.append({
            "id": _item_id("calendar", currency, title, published, time_text),
            "kind": "calendar",
            "title": title,
            "summary": " · ".join(summary_parts),
            "url": FF_HOME,
            "source": "Forex Factory",
            "section": "Economic calendar",
            "published": published,
            "domain": "forexfactory.com",
            "currency": currency,
            "impact": impact,
            "forecast": forecast,
            "previous": previous,
            "actual": actual,
            "date_label": date_label,
            "time_label": time_label,
            "tz_label": "",
        })
    return out


def parse_forex_feed(payload) -> list[dict]:
    """Parse Forex Factory's public weekly JSON feed (official fair-economy)."""
    if isinstance(payload, (bytes, str)):
        payload = json.loads(payload)
    if not isinstance(payload, list):
        return []

    out: list[dict] = []
    now = datetime.now(timezone.utc)
    for ev in payload:
        if not isinstance(ev, dict):
            continue
        title = _clip(ev.get("title"), 200)
        if not title:
            continue
        raw_date = str(ev.get("date") or "")
        dt = None
        try:
            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except Exception:
            pass
        published = dt.isoformat() if dt else ""
        date_label = time_label = tz_label = ""
        state = ""
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            date_label = dt.strftime("%a %d %b")
            time_label = dt.strftime("%H:%M")
            off = dt.strftime("%z")
            if off:
                tz_label = f"GMT{off[:3]}:{off[3:]}" if off != "+0000" else "GMT"
            state = "past" if dt < now else "upcoming"
        impact = _ff_impact(ev.get("impact")) or "Low"
        currency = _clip(ev.get("country"), 12)
        forecast = _clip(ev.get("forecast"), 40)
        previous = _clip(ev.get("previous"), 40)
        summary_parts = [p for p in (
            f"{currency} · {impact} impact" if currency else f"{impact} impact",
            f"Forecast {forecast}" if forecast else "",
            f"Previous {previous}" if previous else "",
        ) if p]
        out.append({
            "id": _item_id("calendar", currency, title, published),
            "kind": "calendar",
            "title": title,
            "summary": " · ".join(summary_parts),
            "url": FF_HOME,
            "source": "Forex Factory",
            "section": "Economic calendar",
            "published": published,
            "domain": "forexfactory.com",
            "currency": currency,
            "impact": impact,
            "forecast": forecast,
            "previous": previous,
            "actual": "",
            "date_label": date_label,
            "time_label": time_label,
            "tz_label": tz_label,
            "state": state,
        })
    return out


def _filter_forex(items: list[dict], impact: str, currency: str) -> list[dict]:
    wanted = {w.strip().lower() for w in str(impact or "").split(",") if w.strip()}
    wanted.discard("all")
    cur = str(currency or "").strip().upper()
    out = []
    for item in items:
        if wanted and (item.get("impact") or "").lower() not in wanted:
            continue
        if cur and (item.get("currency") or "").upper() != cur:
            continue
        out.append(item)
    return out


def _calendar_sort_key(item: dict):
    """Next events first (ascending), then released events (newest first).

    A 2-hour grace window keeps an event that just printed at the top instead
    of jumping behind later events the moment its timestamp passes.
    """
    raw = item.get("published") or ""
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = dt.timestamp()
    except Exception:
        return (2, 0.0, _IMPACT_ORDER.get((item.get("impact") or "").lower(), 9))
    if ts >= datetime.now(timezone.utc).timestamp() - 7200:
        return (0, ts, _IMPACT_ORDER.get((item.get("impact") or "").lower(), 9))
    return (1, -ts, _IMPACT_ORDER.get((item.get("impact") or "").lower(), 9))


def fetch_forex_factory(impact: str = "", currency: str = "", limit: int = 40) -> dict:
    """Forex Factory calendar via HTML scrape, then the official weekly feed."""
    limit = max(1, min(80, int(limit or 40)))
    raw = _forex_raw()
    items = _filter_forex(_dedupe(copy.deepcopy(raw["items"])), impact, currency)
    items.sort(key=_calendar_sort_key)
    return {
        "ok": bool(items),
        "items": items[:limit],
        "count": min(len(items), limit),
        "source_mode": raw.get("mode", ""),
        "url": FF_HOME,
        "sources": copy.deepcopy(raw.get("sources") or []),
    }


_ff_raw_cache: dict = {"t": 0.0, "data": None, "ttl": 0.0}
_ff_raw_lock = threading.Lock()
_ff_fetch_lock = threading.Lock()
_FF_RAW_TTL = 300.0          # the weekly calendar changes slowly; be a good guest


def _forex_raw() -> dict:
    """Fetch Forex Factory ONCE per 5 min, shared by every impact/currency
    filter. The public feed rate-limits (observed HTTP 429 when polled per
    filter), so filtering always happens on the cached raw event list."""
    with _ff_raw_lock:
        cached = _ff_raw_cache["data"]
        if cached is not None and \
                time.monotonic() - _ff_raw_cache["t"] < _ff_raw_cache["ttl"]:
            return copy.deepcopy(cached)

    # One thread refreshes; concurrent callers wait, then read the fresh cache
    # instead of sending a second burst of requests at a rate-limiting host.
    with _ff_fetch_lock:
        now = time.monotonic()
        with _ff_raw_lock:
            cached = _ff_raw_cache["data"]
            if cached is not None and \
                    now - _ff_raw_cache["t"] < _ff_raw_cache["ttl"]:
                return copy.deepcopy(cached)
        data = _forex_fetch_raw()
        with _ff_raw_lock:
            _ff_raw_cache["data"] = copy.deepcopy(data)
            _ff_raw_cache["t"] = time.monotonic()
            # a genuine empty/rate-limited result backs off for 30 s, not
            # 5 min, so one 429 cannot blank the calendar for the whole window
            _ff_raw_cache["ttl"] = _FF_RAW_TTL if data["items"] else 30.0
        return data


def _forex_fetch_raw() -> dict:
    """The actual HTML-then-feed fetch (no cache logic)."""
    sources: list[dict] = []
    items: list[dict] = []
    mode = ""

    # 1) real HTML scrape
    started = time.monotonic()
    try:
        resp = requests.get(FF_HOME, headers=_HEADERS, timeout=8)
        scraped = parse_forex_html(resp.content, FF_HOME) \
            if resp.status_code == 200 else []
        if not scraped:
            raise RuntimeError(
                f"HTML unavailable (HTTP {resp.status_code}; "
                "Cloudflare challenge)" if resp.status_code != 200
                else "HTML returned no calendar rows")
        items, mode = scraped, "html_scrape"
        sources.append({"name": "Forex Factory (HTML)", "ok": True,
                        "count": len(scraped),
                        "ms": int((time.monotonic() - started) * 1000),
                        "detail": "calendar HTML scraped", "mode": "html_scrape"})
    except Exception as exc:
        sources.append({"name": "Forex Factory (HTML)", "ok": False,
                        "count": 0,
                        "ms": int((time.monotonic() - started) * 1000),
                        "detail": f"{type(exc).__name__}: {exc}"[:160],
                        "mode": "html_scrape"})

    # 2) Forex Factory's own public feed (no HTML challenge)
    if not items:
        started = time.monotonic()
        try:
            resp = requests.get(FF_FEED, headers={
                "User-Agent": _UA, "Accept": "application/json"}, timeout=8)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            items = parse_forex_feed(resp.json())
            if not items:
                raise RuntimeError("feed contained no events")
            mode = "official_json_feed"
            sources.append({"name": "Forex Factory (feed)", "ok": True,
                            "count": len(items),
                            "ms": int((time.monotonic() - started) * 1000),
                            "detail": "official weekly calendar feed",
                            "mode": "official_json_feed"})
        except Exception as exc:
            sources.append({"name": "Forex Factory (feed)", "ok": False,
                            "count": 0,
                            "ms": int((time.monotonic() - started) * 1000),
                            "detail": f"{type(exc).__name__}: {exc}"[:160],
                            "mode": "official_json_feed"})

    return {"items": _dedupe(items), "mode": mode, "sources": sources}


# ── collector + digest formatters ────────────────────────────────────────────

def _search_headlines(query: str, limit: int) -> tuple[list[dict], list[dict]]:
    return fetch_headlines(limit=limit, query=query)


def collect_news(query: str = "", limit: int = 30, kind: str = "all",
                 impact: str = "", currency: str = "",
                 refresh: bool = False) -> dict:
    """Collect live cards. Never raises; failing sources are reported.

    kind: "all" | "headline" | "calendar". A query forces the headline lane
    unless the ask is explicitly about the economic calendar.
    """
    q = _clip(query, 200)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(60, limit))
    kind = kind if kind in {"all", "headline", "calendar"} else "all"
    if is_calendar_query(q):
        kind = "calendar"
    elif q and kind == "all":
        kind = "headline"

    key = (q.lower(), limit, kind, str(impact or "").lower(),
           str(currency or "").upper())
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not refresh and now - hit["t"] < hit.get("ttl", _CACHE_TTL):
            out = copy.deepcopy(hit["data"])
            out["cached"] = True
            return out

    want_headlines = kind in {"all", "headline"}
    want_calendar = kind in {"all", "calendar"}
    headlines: list[dict] = []
    calendar: list[dict] = []
    sources: list[dict] = []

    started = time.monotonic()
    if want_headlines and want_calendar:
        with ThreadPoolExecutor(max_workers=2,
                                thread_name_prefix="news-collect") as pool:
            fh = pool.submit(_search_headlines, q, limit)
            fc = pool.submit(fetch_forex_factory, impact, currency, limit)
            try:
                headlines, hs = fh.result()
            except Exception as exc:       # collector is defensive already
                hs = [{"name": "Headlines", "ok": False, "count": 0, "ms": 0,
                       "detail": f"{type(exc).__name__}: {exc}"[:160],
                       "mode": "rss"}]
            try:
                cal = fc.result()
            except Exception as exc:
                cal = {"ok": False, "items": [],
                       "sources": [{"name": "Forex Factory", "ok": False,
                                    "count": 0, "ms": 0,
                                    "detail": f"{type(exc).__name__}: {exc}"[:160],
                                    "mode": "feed"}]}
            sources.extend(hs)
            sources.extend(cal.get("sources") or [])
            calendar = cal.get("items") or []
    else:
        if want_headlines:
            try:
                headlines, hs = _search_headlines(q, limit)
            except Exception as exc:
                hs = [{"name": "Headlines", "ok": False, "count": 0, "ms": 0,
                       "detail": f"{type(exc).__name__}: {exc}"[:160],
                       "mode": "rss"}]
            sources.extend(hs)
        if want_calendar:
            try:
                cal = fetch_forex_factory(impact, currency, limit)
            except Exception as exc:
                cal = {"items": [], "sources": [
                    {"name": "Forex Factory", "ok": False, "count": 0,
                     "ms": 0, "detail": f"{type(exc).__name__}: {exc}"[:160],
                     "mode": "feed"}]}
            sources.extend(cal.get("sources") or [])
            calendar = cal.get("items") or []

    items = headlines + calendar
    failures = [s for s in sources if not s.get("ok")]
    out = {
        "ok": bool(items),
        "query": q,
        "kind": kind,
        "impact": impact,
        "currency": currency,
        "items": items,
        "counts": {
            "headline": len(headlines),
            "calendar": len(calendar),
            "total": len(items),
        },
        "sources": sources,
        "errors": [f"{s['name']}: {s.get('detail', 'failed')}"
                   for s in failures][:10],
        "partial": bool(items) and bool(failures),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "cached": False,
    }
    with _cache_lock:
        # a partial/failed result backs off for 30 s so a transient 429 does
        # not freeze the page for the full 90 s, while a healthy result keeps
        # the normal cache window.
        _cache[key] = {"t": time.monotonic(),
                       "ttl": 30.0 if failures else _CACHE_TTL,
                       "data": copy.deepcopy(out)}
        if len(_cache) > 32:
            oldest = min(_cache, key=lambda k: _cache[k]["t"])
            _cache.pop(oldest, None)
    return out


def headlines_digest(items: list[dict], query: str = "",
                     max_items: int = 8) -> str:
    """Sourced text digest (the shape the old pipeline produced)."""
    head = query or "top stories"
    rows = [i for i in items if i.get("kind") == "headline"][:max_items]
    if not rows:
        return f"No news found for: {head}"
    lines = [f"Latest news: {head}\n"]
    for n, item in enumerate(rows, 1):
        src = f"  [{item['source']}]" if item.get("source") else ""
        lines.append(f"{n}. {item.get('title', '')}{src}")
        if item.get("summary"):
            lines.append(f"   {item['summary'][:140]}")
        if item.get("url"):
            lines.append(f"   {item['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def calendar_digest(items: list[dict], max_items: int = 15) -> str:
    """Forex Factory digest; values are exactly what the calendar returned."""
    rows = items[:max_items]
    if not rows:
        return "No Forex Factory calendar events were returned."
    modes = {i.get("tz_label") for i in rows if i.get("tz_label")}
    lines = [f"Forex Factory calendar — {len(rows)} event"
             f"{'' if len(rows) == 1 else 's'}"
             + (f" ({', '.join(sorted(modes))})" if modes else ""), ""]
    for n, item in enumerate(rows, 1):
        when = " ".join(x for x in (item.get("date_label"),
                                    item.get("time_label")) if x)
        cur = item.get("currency") or ""
        imp = item.get("impact") or ""
        bits = " · ".join(x for x in (cur, f"{imp} impact" if imp else "") if x)
        lines.append(f"{n}. {when + ' — ' if when else ''}{bits}")
        lines.append(f"   {item.get('title', '')}")
        vals = []
        for label in ("forecast", "previous", "actual"):
            if item.get(label):
                vals.append(f"{label.title()} {item[label]}")
        if vals:
            lines.append("   " + " · ".join(vals))
        lines.append("")
    lines.append("Source: https://www.forexfactory.com/calendar")
    return "\n".join(lines).strip()
