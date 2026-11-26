"""actions/news_briefing.py — NEWS & IMPACT daily market briefing engine.

This is the data layer behind the EXISTING RIO News page. It does not replace
``actions/news_feed.py`` — it reuses that module's RSS/Atom parser and adds a
structured, market-aware briefing on top.

Hard rules
----------
* Every price comes from a provider response and carries the provider name,
  the instrument, a timestamp and a provenance label. No synthetic fallback.
* Every news item keeps its publisher URL. Items are grouped, never invented.
* FII/DII figures come from NSE's official endpoint. If it fails the section
  says "unavailable" and gives the real error.
* Technical levels are computed from Yahoo Finance daily bars and the formula
  is written next to every level.
* Every interpretation is labelled ``kind: interpretation``; measured values
  are ``kind: measured``. The UI renders the distinction.
* An unavailable provider is reported. A failed refresh returns the previous
  snapshot with ``stale: true`` and the real error — never mock data.

Public API
----------
    build_briefing(refresh=False, date=None) -> dict
    refresh_briefing() -> dict
    list_history(limit=30) -> list[dict]
    load_history(date) -> dict | None
    briefing_digest(payload, max_items=8) -> str
    install_schedule() -> dict
    alert_tick(send=False) -> dict
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

try:                                   # optional at runtime; honest if absent
    import yfinance as yf
except Exception:                      # pragma: no cover
    yf = None

from actions.news_feed import _clean_html, parse_feed

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
BASE_DIR = Path(__file__).resolve().parent.parent
BRIEF_DIR = BASE_DIR / "memory" / "news_briefing"
HISTORY_DIR = BRIEF_DIR / "history"
LATEST_FILE = BRIEF_DIR / "latest.json"
SETTINGS_FILE = BRIEF_DIR / "settings.json"
ALERTS_FILE = BRIEF_DIR / "alerts_seen.json"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 RIO-NewsImpact/2.0")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
NSE_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

DEFAULTS = {
    "refresh_ttl_s": 300,
    "news_max_age_h": 72,
    "alerts": {
        "enabled": False,
        "channel": "telegram",
        "min_importance": "high",
        "sentiments": ["negative", "positive"],
        "max_per_tick": 3,
    },
}

# ── feeds (all public RSS/Atom; no key, no paywall bypass) ───────────────────
FEEDS = (
    {"key": "et", "name": "Economic Times Markets", "section": "India",
     "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
     "reliability": "high"},
    {"key": "bs", "name": "Business Standard Markets", "section": "India",
     "url": "https://www.business-standard.com/rss/markets-106.rss",
     "reliability": "high"},
    {"key": "mint", "name": "LiveMint Markets", "section": "India",
     "url": "https://www.livemint.com/rss/markets", "reliability": "high"},
    {"key": "mc", "name": "Moneycontrol Markets", "section": "India",
     "url": "https://www.moneycontrol.com/rss/marketreports.xml",
     "reliability": "high"},
    {"key": "cnbc", "name": "CNBC Markets", "section": "Global",
     "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
     "reliability": "high"},
    {"key": "wsj", "name": "WSJ Markets", "section": "Global",
     "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
     "reliability": "high"},
    {"key": "bbc", "name": "BBC World", "section": "World",
     "url": "https://feeds.bbci.co.uk/news/rss.xml", "reliability": "high"},
    {"key": "aljazeera", "name": "Al Jazeera", "section": "World",
     "url": "https://www.aljazeera.com/xml/rss/all.xml",
     "reliability": "high"},
    {"key": "reuters", "name": "Reuters (via Google News)",
     "section": "Global",
     "url": ("https://news.google.com/rss/search?q=site%3Areuters.com+"
             "(India+markets+OR+oil+OR+rupee+OR+Federal+Reserve)"
             "&hl=en-IN&gl=IN&ceid=IN:en"),
     "reliability": "high", "google": True},
    {"key": "gn_india", "name": "Google News India Markets",
     "section": "India",
     "url": ("https://news.google.com/rss/search?q=India+markets+"
             "Nifty+OR+rupee+OR+RBI+when%3A2d&hl=en-IN&gl=IN&ceid=IN:en"),
     "reliability": "medium", "google": True},
    {"key": "rbi", "name": "RBI Press Releases", "section": "Official",
     "url": "https://www.rbi.org.in/pressreleases_rss.xml",
     "reliability": "primary"},
    {"key": "fed", "name": "Federal Reserve", "section": "Official",
     "url": "https://www.federalreserve.gov/feeds/press_all.xml",
     "reliability": "primary", "max_items": 12},
)

# ── Delta Exchange India (public market data; no key, no auth) ──────────────
DELTA_API = "https://api.india.delta.exchange"
DELTA_SITE = "https://india.delta.exchange/"
# (Delta product, display name, Yahoo fallback symbol)
DELTA_PRODUCTS = (
    ("BTCUSD", "Bitcoin", "BTC-USD"),
    ("ETHUSD", "Ethereum", "ETH-USD"),
)

# ── instruments (Yahoo symbols are explicit; labels never mix instruments) ──
PULSE = (
    {"symbol": "^NSEI", "name": "NIFTY 50", "group": "india"},
    {"symbol": "^NSEBANK", "name": "BANK NIFTY", "group": "india"},
    {"symbol": "^BSESN", "name": "SENSEX", "group": "india"},
    {"symbol": "^INDIAVIX", "name": "India VIX", "group": "india"},
    {"symbol": "BZ=F", "name": "Brent crude",
     "instrument": "Brent crude front-month futures (BZ=F)",
     "group": "commodity"},
    {"symbol": "GC=F", "name": "Gold",
     "instrument": "COMEX gold front-month futures (GC=F)",
     "group": "commodity"},
    {"symbol": "INR=X", "name": "USD/INR",
     "instrument": "USD/INR spot (Yahoo INR=X)", "group": "fx"},
    {"symbol": "BTC-USD", "name": "Bitcoin",
     "instrument": "Bitcoin spot (BTC-USD)", "group": "crypto", "is_247": True},
)

GLOBAL = {
    "US": (
        {"symbol": "^GSPC", "name": "S&P 500"},
        {"symbol": "^IXIC", "name": "Nasdaq Composite"},
        {"symbol": "^DJI", "name": "Dow Jones"},
    ),
    "Asia": (
        {"symbol": "^N225", "name": "Nikkei 225"},
        {"symbol": "^HSI", "name": "Hang Seng"},
        {"symbol": "^KS11", "name": "KOSPI"},
    ),
    "Europe": (
        {"symbol": "^FTSE", "name": "FTSE 100"},
        {"symbol": "^GDAXI", "name": "DAX"},
        {"symbol": "^FCHI", "name": "CAC 40"},
    ),
}

SECTORS = (
    {"key": "it", "name": "IT", "symbol": "^CNXIT",
     "keywords": ("it sector", "software", "infosys", "tcs", "wipro", "hcl",
                  "tech mahindra", "digital", "ai ", "cloud")},
    {"key": "metals", "name": "Metals", "symbol": "^CNXMETAL",
     "keywords": ("metal", "steel", "aluminium", "copper", "iron ore",
                  "tata steel", "jsw", "hindalco", "vedanta")},
    {"key": "banking", "name": "Banking & Financials", "symbol": "^NSEBANK",
     "keywords": ("bank", "nbfc", "hdfc", "icici", "sbi", "kotak", "axis",
                  "loan", "credit growth", "npa")},
    {"key": "energy", "name": "Energy & Oil", "symbol": "^CNXENERGY",
     "keywords": ("oil", "crude", "gas", "refiner", "ongc", "reliance",
                  "bpcl", "hpcl", "iocl", "opec", "petroleum")},
    {"key": "pharma", "name": "Pharmaceuticals", "symbol": "^CNXPHARMA",
     "keywords": ("pharma", "drug", "usfda", "generic", "sun pharma",
                  "cipla", "dr reddy", "lupin", "hospital")},
    {"key": "auto", "name": "Automobiles", "symbol": "^CNXAUTO",
     "keywords": ("auto", "car", "ev ", "vehicle", "maruti", "tata motors",
                  "mahindra", "two-wheeler", "bajaj")},
    {"key": "hotels", "name": "Hotels & Hospitality", "symbol": "INDHOTEL.NS",
     "proxy": "Indian Hotels Co. (INDHOTEL.NS) used as a listed proxy",
     "keywords": ("hotel", "hospitality", "tourism", "travel", "indhotel",
                  "lemontree", "chalet")},
    {"key": "fmcg", "name": "FMCG", "symbol": "^CNXFMCG",
     "keywords": ("fmcg", "consumer goods", "hindustan unilever", "itc",
                  "nestle", "dabur", "marico", "godrej consumer")},
    {"key": "realty", "name": "Realty", "symbol": "^CNXREALTY",
     "keywords": ("realty", "real estate", "housing", "property", "dlf",
                  "godrej properties", "oberoi realty")},
    {"key": "psu_banks", "name": "PSU Banks", "symbol": "^CNXPSUBANK",
     "keywords": ("psu bank", "public sector bank", "sbi", "bank of baroda",
                  "canara bank", "pnb")},
    {"key": "capital_goods", "name": "Capital Goods", "symbol": "^CNXINFRA",
     "keywords": ("capital goods", "infrastructure", "l&t", "siemens",
                  "abb", "bhel", "order book", "defence")},
    {"key": "telecom", "name": "Telecom", "symbol": "BHARTIARTL.NS",
     "proxy": "Bharti Airtel (BHARTIARTL.NS) used as a listed proxy",
     "keywords": ("telecom", "airtel", "jio", "vodafone", "spectrum",
                  "arpu", "5g")},
)

NSE_INDEX_FOR_SECTOR = {
    "it": "NIFTY IT",
    "metals": "NIFTY METAL",
    "banking": "NIFTY BANK",
    "energy": "NIFTY ENERGY",
    "pharma": "NIFTY PHARMA",
    "auto": "NIFTY AUTO",
    "fmcg": "NIFTY FMCG",
    "realty": "NIFTY REALTY",
    "psu_banks": "NIFTY PSU BANK",
    "capital_goods": "NIFTY INFRA",
}

WATCHLIST = (
    ("RELIANCE.NS", "Reliance Industries", "Energy"),
    ("TCS.NS", "Tata Consultancy Services", "IT"),
    ("HDFCBANK.NS", "HDFC Bank", "Banking"),
    ("ICICIBANK.NS", "ICICI Bank", "Banking"),
    ("INFY.NS", "Infosys", "IT"),
    ("SBIN.NS", "State Bank of India", "PSU Banks"),
    ("BHARTIARTL.NS", "Bharti Airtel", "Telecom"),
    ("LT.NS", "Larsen & Toubro", "Capital Goods"),
    ("ITC.NS", "ITC", "FMCG"),
    ("MARUTI.NS", "Maruti Suzuki", "Auto"),
    ("SUNPHARMA.NS", "Sun Pharmaceutical", "Pharma"),
    ("M&M.NS", "Mahindra & Mahindra", "Auto"),
)

EARNINGS_UNIVERSE = (
    ("TCS.NS", "TCS", "IT"),
    ("HDFCBANK.NS", "HDFC Bank", "Banking"),
    ("INFY.NS", "Infosys", "IT"),
    ("ICICIBANK.NS", "ICICI Bank", "Banking"),
    ("RELIANCE.NS", "Reliance Industries", "Energy"),
    ("SBIN.NS", "State Bank of India", "PSU Banks"),
    ("AXISBANK.NS", "Axis Bank", "Banking"),
    ("LT.NS", "Larsen & Toubro", "Capital Goods"),
    ("ITC.NS", "ITC", "FMCG"),
    ("MARUTI.NS", "Maruti Suzuki", "Auto"),
)

# ── keyword dictionaries ────────────────────────────────────────────────────
POS_WORDS = (
    "surge", "surges", "surged", "rally", "rallies", "gain", "gains",
    "rise", "rises", "jump", "jumps", "beat", "beats", "upgrade",
    "upgrades", "record high", "profit", "profits", "growth", "boost",
    "wins", "win", "order win", "expansion", "approval", "approved",
    "rate cut", "ceasefire", "truce", "deal", "rebound", "strong",
    "outperform", "buyback", "bonus", "dividend", "inflows", "inflow",
)
NEG_WORDS = (
    "fall", "falls", "drop", "drops", "slump", "slumps", "plunge",
    "plunges", "crash", "crashes", "miss", "misses", "downgrade",
    "downgrades", "loss", "losses", "weak", "cut", "cuts", "fear",
    "fears", "war", "attack", "attacks", "sanction", "sanctions",
    "tariff", "tariffs", "strike", "default", "fraud", "probe",
    "recall", "resign", "resigns", "delay", "delays", "inflation",
    "outage", "disruption", "selloff", "sell-off", "outflows", "outflow",
    "penalty", "fine", "ban", "banned", "protest", "escalation",
)

CATEGORY_RULES = (
    ("geopolitics", re.compile(
        r"\b(geopolitic\w*|war|conflict|ceasefire|truce|sanction\w*|tariff\w*|"
        r"trade (war|deal|talks)|iran|israel|gaza|lebanon|hezbollah|hamas|"
        r"russia|ukraine|china|taiwan|north korea|middle east|red sea|"
        r"white house|congress|senate|trump|president|election|nato|opec\+?|"
        r"strait of hormuz|border|missile|drone|military)\b", re.I)),
    ("oil", re.compile(
        r"\b(brent|wti|crude( oil)?|oil price\w*|petroleum|opec\+?|refin\w*|"
        r"barrel|gasoline|diesel|natural gas|lng|energy (market|sector)|"
        r"hpcl|bpcl|iocl|ongc|oil india|reliance)\b", re.I)),
    ("gold", re.compile(
        r"\b(gold|bullion|xauusd|comex|silver|sovereign gold|sgb|"
        r"jewell\w*|titan|kalyan|senco|muthoot|manappuram)\b", re.I)),
    ("rbi", re.compile(
        r"\b(rbi|reserve bank|monetary policy|repo rate|reverse repo|vrr|"
        r"reserve money|crr|slr|liquidity adjustment)\b", re.I)),
    ("rupee", re.compile(
        r"\b(rupee|usd/inr|usdinr|inr|dollar index|dxy|forex reserve\w*|"
        r"fpi|foreign (portfolio|investor)\w*)\b", re.I)),
    ("earnings", re.compile(
        r"\b(q[1-4]|quarter\w*|earnings?|results?|net profit|revenue|pat|"
        r"ebitda|margin\w*|guidance|consolidated|standalone|"
        r"profit after tax)\b", re.I)),
    ("corporate", re.compile(
        r"\b(order win|wins order|contract|bagged|acquisition|acquire\w*|"
        r"merger|amalgamation|capacity expansion|expansion|fund rais\w*|"
        r"qip|rights issue|dividend|bonus|stock split|buyback|"
        r"management change|appoint\w*|resign\w*|ceo|cfo|product launch|"
        r"regulatory|sebi|rbi notice|credit rating)\b", re.I)),
    ("ipo", re.compile(
        r"\b(ipo|initial public offer\w*|listing|subscription|price band|"
        r"anchor investor|grey market|mainboard|sme ipo)\b", re.I)),
    ("broker", re.compile(
        r"\b(broker\w*|brokerage|target price|price target|upgrade\w*|"
        r"downgrade\w*|initiate coverage|buy rating|sell rating|"
        r"jefferies|morgan stanley|goldman sachs|citi|nomura|kotak|"
        r"motilal oswal|icici securities|clsa|ubs|bofa|hsbc|macquarie)\b",
        re.I)),
    ("markets", re.compile(
        r"\b(nifty|sensex|bank nifty|stock market|equit\w*|shares|"
        r"fii|dii|volatility|vix|bond yield|yield|fed|fomc|rate cut|"
        r"rate hike|inflation|cpi|gdp|pmi|jobs report|payrolls)\b", re.I)),
)

BROKERS = (
    "Jefferies", "Morgan Stanley", "Goldman Sachs", "Citi", "Nomura",
    "Kotak Institutional Equities", "Kotak Securities", "Motilal Oswal",
    "ICICI Securities", "CLSA", "UBS", "BofA Securities", "BofA",
    "HSBC", "Macquarie", "Axis Capital", "HDFC Securities", "Emkay",
    "Elara", "Antique", "JM Financial", "Nuvama", "DAM Capital",
)

IMPACT_TEMPLATES = {
    "oil": ("Crude is a major component of India's import bill; a sustained "
            "move can feed into inflation, the current account and "
            "energy-sector earnings."),
    "gold": ("Gold reacts to the dollar, real yields and risk appetite; "
             "jewellery and gold-finance companies can be affected."),
    "rupee": ("A weaker rupee raises import costs and can affect inflation "
              "and import-heavy sectors; exporters may get a translation "
              "tailwind."),
    "rbi": ("RBI operations and releases can change banking-system liquidity, "
            "rate expectations and bank margins; read the actual circular or "
            "press release before drawing a market conclusion."),
    "geopolitics": ("Geopolitical risk can move crude, gold, the dollar and "
                    "global risk appetite, which transmit to Indian equities "
                    "through flows and commodity costs."),
    "earnings": ("Results reset sector earnings expectations; compare the "
                 "reported numbers with estimates rather than the headline "
                 "alone."),
    "corporate": ("Exchange filings are primary disclosures; price impact "
                  "depends on the size of the announcement relative to "
                  "expectations."),
    "ipo": ("IPOs absorb liquidity and can shift sentiment toward their "
            "sectors; subscription data is time-stamped and changes daily."),
    "broker": ("A broker view is an opinion, not a fact; target prices carry "
               "assumptions and the firm may have conflicts of interest."),
    "markets": ("Global risk sentiment and institutional flows influence "
                "Indian equities, especially around the open."),
}
DEFAULT_IMPACT = ("Monitored because it can affect sentiment, flows or "
                  "sector earnings; the market impact is not yet measurable.")

DISCLAIMER = (
    "Research and information only. Not investment advice. Prices are "
    "provider-delivered and may be delayed; every card shows its source and "
    "timestamp. The News Agent does not place orders."
)


# ── time / formatting helpers ───────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).astimezone(UTC).isoformat(timespec="seconds")


def _ist(dt: datetime | None = None) -> datetime:
    if isinstance(dt, str):
        dt = _parse_dt(dt)
    if isinstance(dt, _date) and not isinstance(dt, datetime):
        dt = datetime.combine(dt, datetime.min.time()).replace(tzinfo=IST)
    return (dt or _now()).astimezone(IST)


def _label(dt: datetime | None = None) -> str:
    d = _ist(dt)
    return d.strftime("%d %b %Y, %H:%M IST")


def _day_label(d: _date | str) -> str:
    if isinstance(d, str):
        try:
            d = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            return str(d)
    return d.strftime("%a, %d %b %Y")


def _parse_dt(value: Any, tz: ZoneInfo = IST) -> datetime | None:
    """Parse common provider timestamps. Naive values are treated as IST."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=tz)
    except Exception:
        pass
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y",
                "%d %b %Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def _age_s(value: Any) -> int | None:
    dt = _parse_dt(value)
    if not dt:
        return None
    return max(0, int((_now() - dt.astimezone(UTC)).total_seconds()))


def _ago(value: Any) -> str:
    sec = _age_s(value)
    if sec is None:
        return "time not served"
    if sec < 60:
        return "just now"
    if sec < 3600:
        return f"{sec // 60}m ago"
    if sec < 86400:
        return f"{sec // 3600}h ago"
    return f"{sec // 86400}d ago"


def _f(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _round(value: Any, digits: int = 2) -> float | None:
    x = _f(value)
    return round(x, digits) if x is not None else None


def _chg(last: Any, prev: Any) -> tuple[float | None, float | None]:
    a, b = _f(last), _f(prev)
    if a is None or b in (None, 0):
        return None, None
    d = a - b
    return round(d, 4), round(d / b * 100.0, 3)


def _pct_label(value: Any) -> str:
    x = _f(value)
    if x is None:
        return "—"
    return f"{x:+.2f}%"


def _money(value: Any, currency: str = "") -> str:
    x = _f(value)
    if x is None:
        return "—"
    prefix = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£",
              "JPY": "¥"}.get(currency, "")
    if abs(x) >= 1000:
        return f"{prefix}{x:,.2f}"
    if abs(x) >= 10:
        return f"{prefix}{x:,.2f}"
    return f"{prefix}{x:,.4f}".rstrip("0").rstrip(".")


def _cr(value: Any) -> str:
    x = _f(value)
    if x is None:
        return "—"
    return f"₹{x:,.2f} cr"


def _clip(text: Any, limit: int = 320) -> str:
    return _clean_html(str(text or ""), limit)


def _id(*parts: Any) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:14]


def _tokens(text: str) -> set[str]:
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for",
            "with", "as", "at", "by", "from", "is", "are", "was", "were",
            "be", "been", "this", "that", "it", "its", "after", "before",
            "over", "under", "amid", "says", "said", "will", "may", "could"}
    out = set()
    for t in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(t) > 2 and t not in stop:
            out.add(t)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False,
                                  default=str), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:
        return False


def settings() -> dict:
    data = _read_json(SETTINGS_FILE, {})
    out = copy.deepcopy(DEFAULTS)
    if isinstance(data, dict):
        for k, v in data.items():
            if k == "alerts" and isinstance(v, dict):
                out["alerts"].update(v)
            else:
                out[k] = v
    return out


def save_settings(patch: dict) -> dict:
    out = settings()
    if isinstance(patch, dict):
        for k, v in patch.items():
            if k == "alerts" and isinstance(v, dict):
                out["alerts"].update(v)
            elif k in DEFAULTS:
                out[k] = v
    _write_json(SETTINGS_FILE, out)
    return out


# ── provider caches ─────────────────────────────────────────────────────────
_cache_lock = threading.RLock()
_quote_cache: dict[str, tuple[float, dict]] = {}
_hist_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
_news_cache: dict[str, Any] = {"t": 0.0, "data": None}
_nse_cache: dict[str, tuple[float, Any]] = {}
_delta_cache: dict[str, tuple[float, dict]] = {}
_build_lock = threading.Lock()
_last_build: dict[str, Any] = {"t": 0.0, "data": None, "error": ""}

QUOTE_TTL = 120.0
HIST_TTL = 900.0
NSE_TTL = 240.0
DELTA_TTL = 60.0


def _cache_get(store: dict, key, ttl: float):
    with _cache_lock:
        hit = store.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return copy.deepcopy(hit[1])
    return None


def _cache_put(store: dict, key, value):
    with _cache_lock:
        store[key] = (time.monotonic(), copy.deepcopy(value))
        if len(store) > 180:
            oldest = min(store, key=lambda k: store[k][0])
            store.pop(oldest, None)


# ── quotes + history (Yahoo Finance, delayed research feed) ─────────────────
def _market_state_label(state: str, is_247: bool = False) -> str:
    s = str(state or "").upper()
    if is_247:
        return "24/7 market · delayed feed"
    if s == "REGULAR":
        return "Live session · delayed feed"
    if s in ("PRE", "PREPRE"):
        return "Pre-market"
    if s in ("POST", "POSTPOST"):
        return "Session closed · after-hours"
    if s == "CLOSED":
        return "Closed"
    return "Market state not served"


def _fetch_quote(symbol: str, name: str = "", instrument: str = "",
                 refresh: bool = False, is_247: bool = False) -> dict:
    cached = None if refresh else _cache_get(_quote_cache, symbol, QUOTE_TTL)
    if cached:
        cached["cached"] = True
        return cached
    base = {
        "symbol": symbol, "name": name or symbol,
        "instrument": instrument or name or symbol,
        "source": "Yahoo Finance", "provenance": "delayed",
        "as_of": "", "as_of_label": "", "quote_time_estimated": True,
        "price": None, "prev_close": None, "change": None, "change_pct": None,
        "currency": "", "exchange": "", "market_state": "",
        "session_label": "Unavailable", "is_247": bool(is_247),
        "ok": False, "error": "",
    }
    if yf is None:
        base["error"] = "yfinance is not installed — no quote provider"
        return base
    try:
        tk = yf.Ticker(symbol)
        info: dict = {}
        try:
            info = tk.info or {}
        except Exception:
            info = {}
        price = _f(info.get("regularMarketPrice"))
        prev = _f(info.get("regularMarketPreviousClose")
                  or info.get("previousClose"))
        if price is None or prev is None:
            try:
                fi = tk.fast_info
                price = price if price is not None else _f(fi.last_price)
                prev = prev if prev is not None else _f(fi.previous_close)
                if not info:
                    info = {"currency": getattr(fi, "currency", "") or "",
                            "exchange": getattr(fi, "exchange", "") or ""}
            except Exception:
                pass
        if price is None:
            base["error"] = "provider served no price for this symbol"
            return base
        ts = info.get("regularMarketTime")
        tzname = str(info.get("exchangeTimezoneName") or "UTC")
        as_of = ""
        estimated = True
        if ts:
            try:
                as_of = datetime.fromtimestamp(float(ts), tz=UTC).isoformat(
                    timespec="seconds")
                estimated = False
            except Exception:
                as_of = ""
        if not as_of:
            as_of = _iso()
        change, pct = _chg(price, prev)
        state = str(info.get("marketState") or "")
        out = dict(base)
        out.update({
            "ok": True,
            "price": _round(price, 6),
            "prev_close": _round(prev, 6),
            "change": change, "change_pct": pct,
            "currency": str(info.get("currency") or ""),
            "exchange": str(info.get("exchange") or ""),
            "market_state": state,
            "session_label": _market_state_label(state, is_247),
            "as_of": as_of, "as_of_label": _label(as_of),
            "quote_time_estimated": estimated,
            "timezone": tzname,
            "error": "",
        })
        _cache_put(_quote_cache, symbol, out)
        return out
    except Exception as exc:
        base["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        return base


def _fetch_history(symbol: str, period: str = "1y", interval: str = "1d",
                   refresh: bool = False) -> dict:
    key = (symbol, period, interval)
    cached = None if refresh else _cache_get(_hist_cache, key, HIST_TTL)
    if cached:
        cached["cached"] = True
        return cached
    out = {"ok": False, "symbol": symbol, "period": period,
           "interval": interval, "bars": [], "source": "Yahoo Finance",
           "provenance": "delayed", "as_of": _iso(), "error": ""}
    if yf is None:
        out["error"] = "yfinance is not installed — no history provider"
        return out
    try:
        df = yf.Ticker(symbol).history(period=period, interval=interval,
                                       auto_adjust=True)
        if df is None or len(df) == 0:
            out["error"] = "provider returned no bars"
            return out
        bars = []
        for idx, row in df.iterrows():
            close = _f(row.get("Close"))
            if close is None:
                continue
            bars.append({
                "ts": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
                "open": _f(row.get("Open")), "high": _f(row.get("High")),
                "low": _f(row.get("Low")), "close": close,
                "volume": _f(row.get("Volume")),
            })
        if not bars:
            out["error"] = "provider history had no usable closes"
            return out
        out.update({"ok": True, "bars": bars, "count": len(bars),
                    "first": bars[0]["ts"], "last": bars[-1]["ts"]})
        _cache_put(_hist_cache, key, out)
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        return out


# ── indicators (documented formulas) ────────────────────────────────────────
def _parse_delta_ticker(result: dict, product: str, name: str,
                        source: str = "Delta Exchange India",
                        source_url: str = DELTA_SITE) -> dict:
    """Normalise one Delta Exchange ticker without inventing a field.

    ``ltp_change_24h`` is a 24-hour change, not a previous-session change, so
    the label and ``change_basis`` say so explicitly. The provider's
    microsecond ``timestamp`` is preferred over the textual ``time`` field.
    """
    out = {
        "symbol": product, "name": name,
        "instrument": (f"{name} perpetual futures ({product}) · "
                       f"{source}"),
        "source": source, "source_url": source_url, "provenance": "exchange",
        "as_of": "", "as_of_label": "", "quote_time_estimated": True,
        "price": None, "prev_close": None, "change": None,
        "change_pct": None, "change_basis": "24h",
        "change_label": "24h change not served",
        "currency": "USD", "market_state": "24/7",
        "session_label": "24/7 market · exchange feed",
        "open": None, "high": None, "low": None,
        "turnover_usd": None, "open_interest_usd": None,
        "funding_rate_pct": None,
        "ok": False, "error": "",
    }
    if not isinstance(result, dict):
        out["error"] = "provider returned no ticker object"
        return out
    price = (_f(result.get("close")) or _f(result.get("mark_price"))
             or _f(result.get("spot_price")))
    pct = _f(result.get("ltp_change_24h"))
    if pct is None:
        pct = _f(result.get("mark_change_24h"))
    if price is None:
        out["error"] = "provider served no last price"
        return out

    ts = result.get("timestamp")
    dt = None
    if ts is not None:
        try:
            raw = float(ts)
            if raw > 1e14:          # microseconds (Delta's current format)
                raw /= 1_000_000.0
            elif raw > 1e11:        # milliseconds, tolerated
                raw /= 1_000.0
            dt = datetime.fromtimestamp(raw, tz=UTC)
        except (TypeError, ValueError, OSError):
            dt = None
    if dt is None:
        dt = _parse_dt(result.get("time"))
    if dt is None:
        dt = _now()
        out["quote_time_estimated"] = True
    else:
        out["quote_time_estimated"] = False

    prev = None
    change = None
    if pct is not None:
        if pct > -100.0:
            prev = price / (1.0 + pct / 100.0)
            change = price - prev
        out["change_label"] = f"{_pct_label(pct)} 24h"

    out.update({
        "ok": True,
        "price": _round(price, 6),
        "prev_close": _round(prev, 6) if prev is not None else None,
        "change": _round(change, 6) if change is not None else None,
        "change_pct": _round(pct, 4) if pct is not None else None,
        "as_of": dt.astimezone(UTC).isoformat(timespec="seconds"),
        "as_of_label": _label(dt),
        "open": _round(_f(result.get("open")), 6),
        "high": _round(_f(result.get("high")), 6),
        "low": _round(_f(result.get("low")), 6),
        "turnover_usd": _f(result.get("turnover_usd")),
        "open_interest_usd": _f(result.get("oi_value_usd")),
        "funding_rate_pct": (round(_f(result.get("funding_rate")) * 100.0, 4)
                             if _f(result.get("funding_rate")) is not None
                             else None),
        "error": "",
    })
    return out


def _fetch_delta_crypto(refresh: bool = False) -> dict:
    """Fetch Delta Exchange India BTC/ETH perpetuals in parallel.

    Returns ``{"quotes": {yahoo_symbol: quote}, "status": source_row}``. A
    failed product is omitted; the caller keeps Yahoo's spot quote as a
    labelled fallback rather than filling a gap.
    """
    status = {
        "name": "Delta Exchange India", "url": DELTA_API + "/v2/tickers",
        "ok": False, "count": 0, "detail": "", "mode": "official_api",
        "reliability": "high", "as_of": _iso(),
    }
    quotes: dict[str, dict] = {}
    errors: list[str] = []

    def one(product: str, name: str, fallback: str) -> tuple[str, dict]:
        cached = None if refresh else _cache_get(_delta_cache, product,
                                                 DELTA_TTL)
        if cached is not None:
            cached["cached"] = True
            return fallback, cached
        try:
            r = requests.get(f"{DELTA_API}/v2/tickers/{product}",
                             headers=HEADERS, timeout=10)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            body = r.json()
            if not body.get("success") or not isinstance(body.get("result"),
                                                         dict):
                raise RuntimeError("provider returned success=false or no result")
            q = _parse_delta_ticker(body["result"], product, name)
            if not q.get("ok"):
                raise RuntimeError(q.get("error") or "unusable ticker")
            _cache_put(_delta_cache, product, q)
            return fallback, q
        except Exception as exc:
            errors.append(f"{product}: {type(exc).__name__}: {str(exc)[:120]}")
            return fallback, {"ok": False, "symbol": product, "name": name,
                              "source": "Delta Exchange India",
                              "error": str(exc)[:180]}

    with ThreadPoolExecutor(max_workers=len(DELTA_PRODUCTS),
                            thread_name_prefix="delta") as pool:
        futs = [pool.submit(one, p, n, y) for p, n, y in DELTA_PRODUCTS]
        for f in futs:
            fallback, q = f.result()
            if q.get("ok"):
                quotes[fallback] = q

    status["count"] = len(quotes)
    status["ok"] = len(quotes) == len(DELTA_PRODUCTS)
    status["detail"] = (f"{len(quotes)}/{len(DELTA_PRODUCTS)} perpetuals · "
                        "24h change")
    if errors:
        status["detail"] += " · " + "; ".join(errors[:2])
    return {"quotes": quotes, "status": status}


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI. First average is a simple mean; later averages smooth."""
    if len(closes) <= period:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(bars: list[dict], period: int = 14) -> float | None:
    """Wilder ATR over true range = max(H-L, |H-prevC|, |L-prevC|)."""
    if len(bars) <= period:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l = _f(bars[i].get("high")), _f(bars[i].get("low"))
        pc = _f(bars[i - 1].get("close"))
        if h is None or l is None or pc is None:
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _pivots(bar: dict) -> dict:
    """Classic floor-trader pivots from one completed H/L/C session.

        P  = (H + L + C) / 3
        R1 = 2P - L      S1 = 2P - H
        R2 = P + (H-L)   S2 = P - (H-L)
    """
    h, l, c = _f(bar.get("high")), _f(bar.get("low")), _f(bar.get("close"))
    if h is None or l is None or c is None:
        return {}
    p = (h + l + c) / 3.0
    return {
        "p": round(p, 2), "r1": round(2 * p - l, 2), "s1": round(2 * p - h, 2),
        "r2": round(p + (h - l), 2), "s2": round(p - (h - l), 2),
        "r3": round(h + 2 * (p - l), 2), "s3": round(l - 2 * (h - p), 2),
        "basis": ("Classic floor-trader pivot from the last completed "
                  "session's high/low/close"),
    }


def _vwap_intraday(symbol: str) -> dict:
    """Session VWAP = Σ(typical price × volume) / Σ(volume) on the last
    available intraday session (5-minute bars)."""
    if yf is None:
        return {"value": None, "error": "yfinance not installed"}
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="5m",
                                       auto_adjust=False)
        if df is None or len(df) == 0:
            return {"value": None, "error": "provider returned no intraday bars"}
        # index is timezone-aware; use the last calendar date present
        last_date = df.index[-1].date()
        part = df[[getattr(i, "date", lambda: None)() == last_date
                   for i in df.index]]
        if part is None or len(part) == 0:
            return {"value": None, "error": "no bars for the latest session"}
        num = 0.0
        den = 0.0
        for _, r in part.iterrows():
            h, l, c = _f(r.get("High")), _f(r.get("Low")), _f(r.get("Close"))
            v = _f(r.get("Volume"))
            if None in (h, l, c, v) or v <= 0:
                continue
            num += ((h + l + c) / 3.0) * v
            den += v
        if den <= 0:
            return {"value": None, "error": "intraday bars carried no volume"}
        return {"value": round(num / den, 2), "session_date": str(last_date),
                "bars": len(part), "interval": "5m",
                "basis": "Σ(typical price × volume) / Σ(volume) on the last "
                         "intraday session"}
    except Exception as exc:
        return {"value": None,
                "error": f"{type(exc).__name__}: {str(exc)[:150]}"}


def _technicals(symbol: str, name: str, quote: dict,
                nse_row: dict | None = None,
                nse_as_of: str = "") -> dict:
    hist = _fetch_history(symbol, "1y", "1d")
    out = {
        "symbol": symbol, "name": name, "last": quote.get("price"),
        "change_pct": quote.get("change_pct"),
        "as_of": quote.get("as_of"), "as_of_label": quote.get("as_of_label"),
        "source": "Yahoo Finance daily bars", "provenance": "delayed",
        "status": "unavailable", "error": "", "trend": "", "momentum": "",
        "sma20": None, "sma50": None, "sma200": None, "rsi14": None,
        "atr14": None, "vwap": None, "vwap_note": "",
        "pivot": {}, "swing_high_20d": None, "swing_low_20d": None,
        "breakout": None, "breakdown": None,
        "levels": [], "scenarios": {}, "session_date": "",
        "price_source": quote.get("source") or "Yahoo Finance",
        "history_source": "Yahoo Finance daily bars",
        "history_last_close": None, "history_last_date": "",
        "price_note": "",
    }
    if nse_row and _f(nse_row.get("last")) is not None:
        out["last"] = _f(nse_row.get("last"))
        out["change_pct"] = _f(nse_row.get("percentChange"))
        out["as_of"] = nse_as_of or _iso()
        out["as_of_label"] = _label(_parse_dt(out["as_of"]))
        out["price_source"] = "NSE official allIndices"
    if not hist.get("ok"):
        out["error"] = hist.get("error") or "daily bars unavailable"
        return out
    bars = hist.get("bars") or []
    closes = [b["close"] for b in bars]
    last = bars[-1]
    out["session_date"] = str(last.get("ts", ""))[:10]
    out["history_last_close"] = last.get("close")
    out["history_last_date"] = out["session_date"]
    if _f(out["last"]) is not None and _f(last.get("close")) is not None:
        diff = _f(out["last"]) - _f(last.get("close"))
        if abs(diff) > 0.01:
            out["price_note"] = (
                f"Current level comes from {out['price_source']}; the last "
                f"Yahoo daily bar closed at {_f(last.get('close')):,.2f} on "
                f"{out['session_date']}. Indicators below use the daily bars.")
    out["sma20"] = _round(_sma(closes, 20))
    out["sma50"] = _round(_sma(closes, 50))
    out["sma200"] = _round(_sma(closes, 200))
    out["rsi14"] = _round(_rsi(closes, 14))
    out["atr14"] = _round(_atr(bars, 14))
    recent = bars[-20:]
    out["swing_high_20d"] = _round(max((b["high"] for b in recent
                                       if b.get("high") is not None),
                                      default=None))
    out["swing_low_20d"] = _round(min((b["low"] for b in recent
                                      if b.get("low") is not None),
                                     default=None))
    out["pivot"] = _pivots(last)
    out["breakout"] = out["pivot"].get("r1")
    out["breakdown"] = out["pivot"].get("s1")

    px = _f(out["last"])
    s20, s50 = _f(out["sma20"]), _f(out["sma50"])
    rsi = _f(out["rsi14"])
    if px is not None and s20 is not None and s50 is not None:
        if px > s20 > s50:
            out["trend"] = "Uptrend (price > 20-DMA > 50-DMA)"
        elif px < s20 < s50:
            out["trend"] = "Downtrend (price < 20-DMA < 50-DMA)"
        else:
            out["trend"] = "Rangebound / mixed moving averages"
    else:
        out["trend"] = "Insufficient moving-average history"
    if rsi is None:
        out["momentum"] = "RSI unavailable"
    elif rsi >= 70:
        out["momentum"] = f"Overbought (RSI {rsi:.1f})"
    elif rsi >= 60:
        out["momentum"] = f"Strong (RSI {rsi:.1f})"
    elif rsi >= 50:
        out["momentum"] = f"Positive (RSI {rsi:.1f})"
    elif rsi >= 40:
        out["momentum"] = f"Soft (RSI {rsi:.1f})"
    else:
        out["momentum"] = f"Weak (RSI {rsi:.1f})"

    vw = _vwap_intraday(symbol)
    out["vwap"] = vw.get("value")
    out["vwap_note"] = (vw.get("basis") if vw.get("value") is not None
                        else f"VWAP unavailable — {vw.get('error')}")

    levels = []
    p = out["pivot"]
    if p:
        for key, label, kind in (("s2", "S2", "support"),
                                 ("s1", "S1", "support"),
                                 ("p", "Pivot", "pivot"),
                                 ("r1", "R1", "resistance"),
                                 ("r2", "R2", "resistance")):
            if p.get(key) is not None:
                levels.append({"kind": kind, "label": label,
                               "price": p[key],
                               "basis": p.get("basis", "")})
    if out["swing_low_20d"] is not None:
        levels.append({"kind": "support", "label": "20-day swing low",
                       "price": out["swing_low_20d"],
                       "basis": "Lowest daily low of the last 20 sessions"})
    if out["swing_high_20d"] is not None:
        levels.append({"kind": "resistance", "label": "20-day swing high",
                       "price": out["swing_high_20d"],
                       "basis": "Highest daily high of the last 20 sessions"})
    out["levels"] = levels

    if px is not None and p:
        above = px > _f(p.get("p", px))
        out["scenarios"] = {
            "bullish": (f"A daily close above R1 {p.get('r1')} with RSI > 50 "
                        f"would keep the uptrend structure; next reference "
                        f"R2 {p.get('r2')}."),
            "bearish": (f"A daily close below S1 {p.get('s1')} with RSI < 50 "
                        f"would weaken the structure; next reference "
                        f"S2 {p.get('s2')}."),
            "neutral": (f"Between S1 {p.get('s1')} and R1 {p.get('r1')} the "
                        f"session is balanced; the pivot {p.get('p')} is the "
                        f"intraday bias line."),
            "current_bias": "above pivot" if above else "below pivot",
        }
    out["status"] = "ok"
    return out


# ── news collection ─────────────────────────────────────────────────────────
def _fetch_feed(src: dict, refresh: bool = False) -> tuple[list[dict], dict]:
    started = time.monotonic()
    status = {"name": src["name"], "url": src["url"], "ok": False,
              "count": 0, "ms": 0, "detail": "", "mode": "rss",
              "reliability": src.get("reliability", "medium"),
              "as_of": _iso()}
    try:
        resp = requests.get(src["url"], headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        items = parse_feed(resp.content, src)
        limit = int(src.get("max_items") or 30)
        items = items[:limit]
        for it in items:
            it["reliability"] = src.get("reliability", "medium")
            it["feed_key"] = src.get("key", "")
            it["feed_name"] = src.get("name", it.get("source", ""))
            it["time_type"] = "publication"
        status.update({"ok": True, "count": len(items),
                       "detail": f"{len(items)} items · RSS/Atom",
                       "ms": int((time.monotonic() - started) * 1000)})
        return items, status
    except Exception as exc:
        status.update({"detail": f"{type(exc).__name__}: {str(exc)[:160]}",
                       "ms": int((time.monotonic() - started) * 1000)})
        return [], status


def _collect_articles(refresh: bool = False) -> tuple[list[dict], list[dict]]:
    if not refresh:
        with _cache_lock:
            hit = _news_cache.get("data")
            age = time.monotonic() - float(_news_cache.get("t") or 0)
        if hit and age < 240:
            return copy.deepcopy(hit["items"]), copy.deepcopy(hit["sources"])
    items: list[dict] = []
    sources: list[dict] = []
    with ThreadPoolExecutor(max_workers=8,
                            thread_name_prefix="news-impact") as pool:
        futures = [pool.submit(_fetch_feed, src, refresh) for src in FEEDS]
        for fut in futures:
            try:
                rows, status = fut.result()
            except Exception as exc:
                rows, status = [], {"name": "feed", "ok": False, "count": 0,
                                    "detail": f"{type(exc).__name__}: {exc}",
                                    "mode": "rss", "as_of": _iso(),
                                    "url": "", "ms": 0}
            items.extend(rows)
            sources.append(status)
    # Keep the last 72 hours; official releases older than 7 days are dropped.
    cutoff = _now() - timedelta(hours=int(settings().get("news_max_age_h", 72)))
    kept = []
    for it in items:
        dt = _parse_dt(it.get("published"))
        if dt is not None and dt.astimezone(UTC) < cutoff:
            continue
        kept.append(it)
    with _cache_lock:
        _news_cache["t"] = time.monotonic()
        _news_cache["data"] = {"items": copy.deepcopy(kept),
                               "sources": copy.deepcopy(sources)}
    return kept, sources


def _sentiment(text: str) -> tuple[str, str]:
    low = (text or "").lower()
    pos = [w for w in POS_WORDS if w in low]
    neg = [w for w in NEG_WORDS if w in low]
    if pos and neg:
        return "mixed", (f"positive terms: {', '.join(pos[:4])}; "
                         f"negative terms: {', '.join(neg[:4])}")
    if pos:
        return "positive", f"positive terms: {', '.join(pos[:5])}"
    if neg:
        return "negative", f"negative terms: {', '.join(neg[:5])}"
    return "unclear", "no directional keyword in the headline/summary"


def _categories(text: str) -> list[str]:
    return [name for name, rx in CATEGORY_RULES if rx.search(text or "")]


def _importance(article: dict) -> tuple[str, int]:
    rel = {"primary": 30, "high": 20, "medium": 12}.get(
        str(article.get("reliability")), 10)
    age = _age_s(article.get("published"))
    if age is None:
        rec = 4
    elif age < 7200:
        rec = 25
    elif age < 21600:
        rec = 20
    elif age < 43200:
        rec = 14
    elif age < 86400:
        rec = 8
    else:
        rec = 4
    cats = set(article.get("categories") or [])
    hot = bool(cats & {"geopolitics", "oil", "gold", "rupee", "earnings",
                       "markets", "ipo"})
    seg = 15 if hot else 5
    score = min(100, rel + rec + seg)
    label = "high" if score >= 65 else "medium" if score >= 45 else "low"
    return label, score


def _why(cats: list[str]) -> str:
    for key in ("oil", "gold", "rbi", "rupee", "geopolitics", "earnings",
                "corporate", "ipo", "broker", "markets"):
        if key in cats:
            return IMPACT_TEMPLATES[key]
    return DEFAULT_IMPACT


def _enrich(article: dict) -> dict:
    title = str(article.get("title") or "")
    summary = _clip(article.get("summary") or "", 340)
    text = f"{title}. {summary}"
    cats = _categories(text)
    sentiment, evidence = _sentiment(text)
    sectors = []
    low = text.lower()
    for sec in SECTORS:
        if any(k in low for k in sec["keywords"]):
            sectors.append(sec["name"])
    out = {
        "id": article.get("id") or _id(title, article.get("url")),
        "title": title,
        "summary": summary,
        "url": article.get("url") or "",
        "source": article.get("source") or article.get("feed_name") or "",
        "feed": article.get("feed_name") or article.get("source") or "",
        "published": article.get("published") or "",
        "published_label": (_label(article.get("published"))
                            if article.get("published") else "time not served"),
        "age_s": _age_s(article.get("published")),
        "time_type": "publication",
        "reliability": article.get("reliability") or "medium",
        "categories": cats,
        "sectors": sectors,
        "sentiment": sentiment,
        "sentiment_evidence": evidence,
        "why": _why(cats),
        "summary_kind": "interpretation",
        "also": [],
    }
    imp, score = _importance(out)
    out["importance"] = imp
    out["score"] = score
    return out


def _group_articles(articles: list[dict]) -> list[dict]:
    """Deduplicate by URL, then group near-identical headlines.

    Similarity = title-token Jaccard >= 0.55, same primary category, published
    within 36 h. The group keeps the highest-scoring member as the primary and
    lists every other publisher link under ``also``.
    """
    seen_urls = set()
    unique = []
    for a in sorted(articles, key=lambda x: x.get("score", 0), reverse=True):
        url = str(a.get("url") or "")
        if url and url in seen_urls:
            continue
        seen_urls.add(url)
        unique.append(a)
    groups: list[dict] = []
    for a in unique:
        at = _tokens(a.get("title") or "")
        adt = _parse_dt(a.get("published"))
        placed = False
        for g in groups:
            gt = _tokens(g.get("title") or "")
            if _jaccard(at, gt) < 0.55:
                continue
            gdt = _parse_dt(g.get("published"))
            if adt and gdt and abs((adt - gdt).total_seconds()) > 36 * 3600:
                continue
            gcats = set(g.get("categories") or [])
            if gcats and a.get("categories") and \
                    not (gcats & set(a["categories"])):
                continue
            g.setdefault("also", []).append({
                "source": a.get("source"), "url": a.get("url"),
                "title": a.get("title"), "published": a.get("published"),
                "published_label": a.get("published_label"),
            })
            g["score"] = min(100, int(g.get("score", 0)) + 3)
            placed = True
            break
        if not placed:
            groups.append(a)
    return groups


def _articles_for(groups: list[dict], category: str,
                  limit: int = 8) -> list[dict]:
    rows = [a for a in groups if category in (a.get("categories") or [])]
    rows.sort(key=lambda a: (-int(a.get("score") or 0),
                             -(a.get("age_s") is not None and
                               86400 - a["age_s"]) or 0))
    return rows[:limit]


# ── official NSE data (primary source) ──────────────────────────────────────
def _nse_get(path: str, refresh: bool = False) -> tuple[Any, dict]:
    cached = None if refresh else _cache_get(_nse_cache, path, NSE_TTL)
    if cached is not None:
        return cached.get("data"), cached.get("status") or {}
    status = {"name": "NSE", "url": "https://www.nseindia.com" + path,
              "ok": False, "count": 0, "detail": "", "mode": "official_api",
              "as_of": _iso(), "reliability": "primary"}
    try:
        s = requests.Session()
        # A home-page cookie is not required by every endpoint, and the home
        # page itself may 403 while the JSON API answers. Ignore that response.
        try:
            s.get("https://www.nseindia.com/", headers=NSE_HEADERS, timeout=8)
        except Exception:
            pass
        r = s.get("https://www.nseindia.com" + path, headers=NSE_HEADERS,
                  timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        data = r.json()
        status["ok"] = True
        status["detail"] = "NSE official JSON API"
        _cache_put(_nse_cache, path, {"data": data, "status": status})
        return data, status
    except Exception as exc:
        status["detail"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        _cache_put(_nse_cache, path, {"data": None, "status": status})
        return None, status


def _fetch_nse_indices(refresh: bool = False) -> tuple[dict, dict]:
    """Official NSE current index snapshot, keyed by index name.

    Used for today's sector level/change because Yahoo's index bars can be
    stale or carry a different previous close. Historical indicators still
    come from Yahoo daily bars and are labelled as such.
    """
    data, status = _nse_get("/api/allIndices", refresh)
    out: dict[str, dict] = {}
    if isinstance(data, dict):
        for row in data.get("data") or []:
            name = str(row.get("index") or "").strip().upper()
            if name:
                out[name] = row
        status = dict(status)
        status["count"] = len(out)
        status["as_of"] = _iso()
    return out, status


def _fetch_fii_dii(refresh: bool = False) -> dict:
    data, status = _nse_get("/api/fiidiiTradeReact", refresh)
    out = {"status": "unavailable", "rows": [], "date": "", "source": "NSE",
           "source_url": "https://www.nseindia.com/reports/fii-dii",
           "as_of": _iso(), "error": "", "status_detail": status}
    if not isinstance(data, list) or not data:
        out["error"] = status.get("detail") or "NSE returned no FII/DII rows"
        return out
    for row in data[:6]:
        if not isinstance(row, dict):
            continue
        cat = str(row.get("category") or "")
        net = _f(row.get("netValue"))
        out["rows"].append({
            "category": cat,
            "buy": _f(row.get("buyValue")),
            "sell": _f(row.get("sellValue")),
            "net": net,
            "net_label": _cr(net),
            "direction": ("positive" if (net or 0) > 0 else
                          "negative" if (net or 0) < 0 else "neutral"),
            "date": str(row.get("date") or ""),
        })
        if row.get("date"):
            out["date"] = str(row.get("date"))
    if out["rows"]:
        out["status"] = "ok"
        out["note"] = ("Official NSE provisional figures, ₹ crore. "
                       "Net = buy value − sell value.")
        # The generic NSE helper cannot know how many rows a list endpoint
        # yielded; report the real count so the Sources panel agrees with G.
        out["status_detail"] = dict(out.get("status_detail") or {})
        out["status_detail"]["count"] = len(out["rows"])
    return out


def _fetch_announcements(refresh: bool = False) -> tuple[list[dict], dict]:
    today = _ist().date()
    frm = (today - timedelta(days=3)).strftime("%d-%m-%Y")
    to = today.strftime("%d-%m-%Y")
    path = (f"/api/corporate-announcements?index=equities"
            f"&from_date={frm}&to_date={to}")
    data, status = _nse_get(path, refresh)
    if not isinstance(data, list):
        return [], status
    rows = []
    for r in data:
        if not isinstance(r, dict):
            continue
        text = str(r.get("attchmntText") or "")
        desc = str(r.get("desc") or "")
        low = f"{desc} {text}".lower()
        if "financial result" in low or "outcome of board meeting" in low:
            cat = "Results"
            sentiment = "unclear"
        elif any(k in low for k in ("order", "contract", "bagged", "award")):
            cat = "Order / contract"
            sentiment = "positive"
        elif any(k in low for k in ("acquisition", "merger", "amalgamation",
                                    "takeover")):
            cat = "M&A"
            sentiment = "mixed"
        elif any(k in low for k in ("expansion", "capacity", "new plant")):
            cat = "Capacity expansion"
            sentiment = "positive"
        elif any(k in low for k in ("dividend", "buyback", "bonus", "split")):
            cat = "Capital return"
            sentiment = "positive"
        elif any(k in low for k in ("penalty", "fine", "notice", "probe",
                                   "default", "downgrade")):
            cat = "Regulatory / risk"
            sentiment = "negative"
        elif any(k in low for k in ("appoint", "resign", "ceo", "cfo",
                                    "management")):
            cat = "Management"
            sentiment = "mixed"
        elif any(k in low for k in ("launch", "product")):
            cat = "Product launch"
            sentiment = "positive"
        elif any(k in low for k in ("credit rating", "rating action")):
            cat = "Credit rating"
            sentiment = "mixed"
        elif any(k in low for k in ("fund rais", "qip", "rights issue",
                                    "preferential")):
            cat = "Fund raise"
            sentiment = "mixed"
        else:
            cat = "Corporate update"
            sentiment = "unclear"
        dt = _parse_dt(r.get("an_dt"))
        rows.append({
            "id": _id(r.get("seq_id"), r.get("symbol"), r.get("an_dt")),
            "company": str(r.get("sm_name") or r.get("symbol") or ""),
            "symbol": str(r.get("symbol") or ""),
            "date": _iso(dt) if dt else "",
            "date_label": _label(dt) if dt else str(r.get("an_dt") or ""),
            "category": cat,
            "summary": _clip(text, 300),
            "desc": desc,
            "impact": _why(["corporate"]),
            "sentiment": sentiment,
            "url": str(r.get("attchmntFile") or ""),
            "source": "NSE corporate announcements",
            "reliability": "primary",
            "importance": ("high" if cat in {"Results", "Order / contract",
                                             "M&A", "Regulatory / risk"}
                           else "medium"),
        })
    rows.sort(key=lambda x: x.get("date") or "", reverse=True)
    status["count"] = len(rows)
    return rows, status


def _fetch_corporate_actions(refresh: bool = False) -> tuple[list[dict], dict]:
    today = _ist().date()
    frm = (today - timedelta(days=3)).strftime("%d-%m-%Y")
    to = (today + timedelta(days=10)).strftime("%d-%m-%Y")
    path = (f"/api/corporates-corporateActions?index=equities"
            f"&from_date={frm}&to_date={to}")
    data, status = _nse_get(path, refresh)
    if not isinstance(data, list):
        return [], status
    rows = []
    for r in data:
        if not isinstance(r, dict):
            continue
        ex = str(r.get("exDate") or "").strip()
        rec = str(r.get("recDate") or "").strip()
        rows.append({
            "id": _id(r.get("symbol"), r.get("subject"), ex),
            "company": str(r.get("comp") or ""),
            "symbol": str(r.get("symbol") or ""),
            "subject": str(r.get("subject") or ""),
            "ex_date": ex if ex not in ("", "-") else "",
            "record_date": rec if rec not in ("", "-") else "",
            "face_value": str(r.get("faceVal") or ""),
            "status": "Confirmed by NSE" if ex not in ("", "-") else
                      "Record date not served",
            "source": "NSE corporate actions",
            "source_url": ("https://www.nseindia.com/companies-listing/"
                           "corporate-filings-corporate-actions"),
        })
    def key(x):
        try:
            return datetime.strptime(x.get("ex_date") or "01-01-2099",
                                     "%d-%b-%Y")
        except ValueError:
            return datetime(2099, 1, 1)
    rows.sort(key=key)
    status["count"] = len(rows)
    return rows[:40], status


def _fetch_ipos(refresh: bool = False) -> tuple[list[dict], dict]:
    data, status = _nse_get("/api/ipo-current-issue", refresh)
    if not isinstance(data, list):
        return [], status
    rows = []
    for r in data:
        if not isinstance(r, dict):
            continue
        try:
            offered = float(r.get("noOfSharesOffered") or 0)
            bid = float(r.get("noOfsharesBid") or 0)
        except (TypeError, ValueError):
            offered, bid = 0.0, 0.0
        times = _f(r.get("noOfTime"))
        rows.append({
            "company": str(r.get("companyName") or ""),
            "symbol": str(r.get("symbol") or ""),
            "open": str(r.get("issueStartDate") or ""),
            "close": str(r.get("issueEndDate") or ""),
            "price_band": str(r.get("issuePrice") or ""),
            "status": str(r.get("status") or ""),
            "subscription_times": times,
            "subscription_label": (f"{times:.2f}×" if times is not None
                                   else "not served"),
            "shares_offered": offered or None,
            "shares_bid": bid or None,
            "subscription_as_of": _iso(),
            "listing_date": "Not confirmed by the NSE IPO feed",
            "source": "NSE current IPO issues",
            "source_url": ("https://www.nseindia.com/market-data/"
                           "all-upcoming-issues-ipo"),
            "confirmed": True,
        })
    status["count"] = len(rows)
    return rows, status


def _fetch_events(refresh: bool = False) -> tuple[list[dict], dict]:
    today = _ist().date()
    frm = today.strftime("%d-%m-%Y")
    to = (today + timedelta(days=12)).strftime("%d-%m-%Y")
    path = (f"/api/event-calendar?index=equities"
            f"&from_date={frm}&to_date={to}")
    data, status = _nse_get(path, refresh)
    if not isinstance(data, list):
        return [], status
    rows = []
    for r in data:
        if not isinstance(r, dict):
            continue
        purpose = str(r.get("purpose") or "")
        if not any(k in purpose.lower() for k in
                   ("financial result", "dividend", "board meeting",
                    "fund rais", "rights", "bonus", "split")):
            continue
        dt = _parse_dt(r.get("date"))
        rows.append({
            "company": str(r.get("company") or ""),
            "symbol": str(r.get("symbol") or ""),
            "purpose": purpose,
            "description": _clip(r.get("bm_desc") or "", 220),
            "date": dt.date().isoformat() if dt else str(r.get("date") or ""),
            "date_label": _day_label(dt.date()) if dt else str(
                r.get("date") or ""),
            "source": "NSE event calendar",
            "source_url": ("https://www.nseindia.com/companies-listing/"
                           "corporate-filings-event-calendar"),
        })
    rows.sort(key=lambda x: x.get("date") or "")
    status["count"] = len(rows)
    return rows, status


# ── section helpers ─────────────────────────────────────────────────────────
def _section(key: str, title: str, icon: str, kind: str = "news",
             items: list[dict] | None = None, data: Any = None,
             status: str = "ok", note: str = "",
             source: str = "", source_url: str = "",
             count: int | None = None) -> dict:
    return {
        "key": key, "title": title, "icon": icon, "kind": kind,
        "status": status, "items": items or [], "data": data,
        "count": (count if count is not None
                  else len(items or []) if items is not None else 0),
        "note": note, "source": source, "source_url": source_url,
    }


def _quote_item(q: dict) -> dict:
    return {
        "symbol": q.get("symbol"), "name": q.get("name"),
        "instrument": q.get("instrument"), "price": q.get("price"),
        "prev_close": q.get("prev_close"), "change": q.get("change"),
        "change_pct": q.get("change_pct"),
        "change_basis": q.get("change_basis", ""),
        "change_label": q.get("change_label") or _pct_label(q.get("change_pct")),
        "currency": q.get("currency"), "market_state": q.get("market_state"),
        "session_label": q.get("session_label"),
        "as_of": q.get("as_of"), "as_of_label": q.get("as_of_label"),
        "quote_time_estimated": q.get("quote_time_estimated"),
        "source": q.get("source"), "provenance": q.get("provenance"),
        "ok": q.get("ok"), "error": q.get("error", ""),
        "kind": "measured",
    }


def _fetch_calendar() -> tuple[list[dict], dict]:
    try:
        from actions.news_feed import fetch_forex_factory
        res = fetch_forex_factory(impact="high", limit=40)
        rows = []
        for i in res.get("items") or []:
            rows.append({
                "title": i.get("title"), "currency": i.get("currency"),
                "impact": i.get("impact"), "date_label": i.get("date_label"),
                "time_label": i.get("time_label"),
                "tz_label": i.get("tz_label"),
                "forecast": i.get("forecast"), "previous": i.get("previous"),
                "actual": i.get("actual"),
                "published": i.get("published"),
                "source": "Forex Factory",
                "url": i.get("url") or "https://www.forexfactory.com/calendar",
                "state": i.get("state") or "",
            })
        return rows, {"name": "Forex Factory calendar", "ok": True,
                      "count": len(rows), "detail": "official weekly feed",
                      "mode": "official_json_feed", "as_of": _iso(),
                      "url": "https://www.forexfactory.com/calendar"}
    except Exception as exc:
        return [], {"name": "Forex Factory calendar", "ok": False,
                    "count": 0, "detail": f"{type(exc).__name__}: {exc}"[:160],
                    "mode": "official_json_feed", "as_of": _iso(),
                    "url": "https://www.forexfactory.com/calendar"}


def _fetch_earnings_row(symbol: str, name: str, sector: str) -> dict | None:
    """One verified recent result. Only values the provider actually served.

    Revenue / net profit are shown only when the quarterly statement carries
    both the latest quarter and the same quarter a year earlier. The provider
    does not state units in the frame, so values >= 500 for an INR-reporting
    Indian company are formatted as ₹ crore (Yahoo's Indian statements);
    smaller or ambiguous values are shown with the source unit only.
    """
    if yf is None:
        return None
    try:
        tk = yf.Ticker(symbol)
        info = {}
        try:
            info = tk.info or {}
        except Exception:
            info = {}
        earnings = None
        try:
            earnings = tk.get_earnings_dates(limit=10)
        except Exception:
            earnings = None
        reported = None
        eps_actual = None
        eps_est = None
        surprise = None
        if earnings is not None and len(earnings):
            for idx, row in earnings.iterrows():
                dt = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
                if not isinstance(dt, datetime):
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                if dt.astimezone(UTC) > _now():
                    continue
                reported = dt.astimezone(UTC)
                eps_actual = _f(row.get("Reported EPS"))
                eps_est = _f(row.get("EPS Estimate"))
                surprise = _f(row.get("Surprise(%)"))
                break
        if reported is None:
            return None
        if (_now() - reported).days > 120:
            return None

        revenue = revenue_prev = profit = profit_prev = None
        quarter_end = ""
        currency = str(info.get("financialCurrency")
                       or info.get("currency") or "")
        try:
            q = tk.quarterly_income_stmt
            cols = list(q.columns) if q is not None else []
            if cols:
                latest = cols[0]
                quarter_end = str(getattr(latest, "date",
                                          lambda: latest)())
                # same quarter a year earlier (allow a 20-day drift)
                prev_col = None
                for c in cols[1:]:
                    try:
                        if abs((latest - c).days - 365) <= 20:
                            prev_col = c
                            break
                    except Exception:
                        continue

                def pick(idx_names):
                    for n in idx_names:
                        if n in q.index:
                            v = _f(q.loc[n, latest])
                            if v is not None:
                                return v, (f_prev := _f(q.loc[n, prev_col])
                                           if prev_col is not None else None)
                    return None, None

                revenue, revenue_prev = pick(
                    ("Total Revenue", "Operating Revenue"))
                profit, profit_prev = pick(
                    ("Net Income", "Net Income Common Stockholders",
                     "Net Income From Continuing Operation Net Minority "
                     "Interest"))
        except Exception:
            pass

        # Yahoo serves Indian statements in raw INR and US statements in raw
        # USD. Convert to the units the label claims instead of mislabelling
        # raw rupees as crore.
        statement_stale = False
        statement_gap_days = None
        if quarter_end and reported is not None:
            try:
                qd = datetime.fromisoformat(quarter_end).date()
                statement_gap_days = (reported.date() - qd).days
                statement_stale = statement_gap_days > 45
            except Exception:
                statement_gap_days = None
        if statement_stale:
            # A statement that predates the reported quarter cannot describe
            # the result that was just published. Suppress it rather than imply
            # the old quarter is the new one.
            revenue = revenue_prev = profit = profit_prev = None
            margin = None

        def fmt_value(v):
            if v is None:
                return ("Not served (statement stale)" if statement_stale
                        else "Not served")
            if currency == "INR":
                return f"₹{v / 1e7:,.2f} cr"
            if currency == "USD":
                return f"${v / 1e6:,.2f} mn"
            units = {"EUR": "€", "GBP": "£", "JPY": "¥"}.get(currency, "")
            return (f"{units}{v / 1e6:,.2f} mn" if units
                    else f"{v:,.1f} {currency}".strip())

        def yoy(curr, prev):
            if curr is None or prev in (None, 0):
                return None
            return round((curr / prev - 1.0) * 100.0, 2)

        rev_yoy = yoy(revenue, revenue_prev)
        pat_yoy = yoy(profit, profit_prev)
        margin = (round(profit / revenue * 100.0, 2)
                  if revenue and profit and revenue > 0 else None)

        if statement_stale:
            eps_bit = (f"Reported EPS vs estimate: {surprise:+.2f}%. "
                       if surprise is not None else "EPS estimate not served. ")
            category = "Mixed — statement lag"
            reason = (eps_bit + "The provider's quarterly statement is for a "
                      "different quarter than the reported result, so revenue "
                      "and net profit are suppressed instead of being "
                      "presented as the latest quarter.")
        elif surprise is not None:
            if surprise >= 2:
                if (rev_yoy is not None and rev_yoy < 0) or \
                        (pat_yoy is not None and pat_yoy < 0):
                    category = "Mixed"
                    reason = (f"Reported EPS beat the provider estimate by "
                              f"{surprise:+.2f}%, but "
                              + ("revenue" if rev_yoy is not None
                                 and rev_yoy < 0 else "net profit")
                              + " declined year on year.")
                else:
                    category = "Strong / positive"
                    reason = (f"Reported EPS beat the provider estimate by "
                              f"{surprise:+.2f}%.")
            elif surprise <= -2:
                if (rev_yoy or 0) > 0 and (pat_yoy or 0) > 0:
                    category = "Mixed"
                    reason = (f"Reported EPS missed the provider estimate by "
                              f"{surprise:+.2f}%, but revenue and net profit "
                              "still grew year on year.")
                else:
                    category = "Below expectations / weak"
                    reason = (f"Reported EPS missed the provider estimate by "
                              f"{surprise:+.2f}%.")
            else:
                category = "Mixed"
                reason = (f"Reported EPS was within ±2% of the estimate "
                          f"({surprise:+.2f}%).")
        elif rev_yoy is not None and pat_yoy is not None:
            if rev_yoy > 0 and pat_yoy > 0:
                category = "Strong / positive"
                reason = "Revenue and net profit both grew year on year."
            elif rev_yoy < 0 and pat_yoy < 0:
                category = "Below expectations / weak"
                reason = "Revenue and net profit both declined year on year."
            else:
                category = "Mixed"
                reason = "Revenue and net profit moved in opposite directions."
        else:
            category = "Mixed — insufficient comparison"
            reason = ("The provider did not serve a like-for-like year-ago "
                      "quarter, so no verdict is inferred from history alone.")

        ann = None
        try:
            rows, _ = _fetch_announcements(False)
            for a in rows:
                if a.get("symbol") == symbol.replace(".NS", "") and \
                        a.get("category") == "Results":
                    ann = a
                    break
        except Exception:
            ann = None

        return {
            "company": name, "symbol": symbol, "sector": sector,
            "quarter": ("Quarter ended " + quarter_end) if quarter_end
                       else "Quarter end not served",
            "quarter_end": quarter_end,
            "statement_stale": statement_stale,
            "statement_gap_days": statement_gap_days,
            "reported_date": reported.date().isoformat(),
            "reported_label": _label(reported),
            "revenue": revenue, "revenue_label": fmt_value(revenue),
            "revenue_yoy": rev_yoy, "revenue_yoy_label": _pct_label(rev_yoy),
            "net_profit": profit, "net_profit_label": fmt_value(profit),
            "net_profit_yoy": pat_yoy,
            "net_profit_yoy_label": _pct_label(pat_yoy),
            "margin": margin,
            "margin_label": f"{margin:.2f}%" if margin is not None else "—",
            "eps_actual": eps_actual, "eps_estimate": eps_est,
            "eps_surprise": surprise,
            "surprise_label": (f"{surprise:+.2f}%" if surprise is not None
                               else "Not served"),
            "category": category, "category_reason": reason,
            "guidance": ("Not machine-verified. Read the exchange filing and "
                         "earnings-call transcript before acting."),
            "source": "Yahoo Finance (earnings + quarterly statement)",
            "source_url": f"https://finance.yahoo.com/quote/{symbol}",
            "filing_url": (ann or {}).get("url", ""),
            "currency_note": (f"Statement currency: {currency or 'not served'}"
                              + (f"; statement quarter is {statement_gap_days} "
                                 "days older than the reported result and was "
                                 "suppressed" if statement_stale else "")),
            "kind": "measured",
            "interpretation_kind": "category is a rules-based label",
        }
    except Exception:
        return None


def _build_results(refresh: bool = False) -> dict:
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=5,
                            thread_name_prefix="news-results") as pool:
        futs = [pool.submit(_fetch_earnings_row, sym, name, sector)
                for sym, name, sector in EARNINGS_UNIVERSE]
        for f in futs:
            try:
                r = f.result()
            except Exception:
                r = None
            if r:
                rows.append(r)
    rows.sort(key=lambda r: r.get("reported_date") or "", reverse=True)
    events, ev_status = _fetch_events(refresh)
    upcoming = [e for e in events if "financial result" in
                str(e.get("purpose", "")).lower()][:12]
    return {
        "recent": rows[:10], "upcoming": upcoming,
        "status": "ok" if rows or upcoming else "unavailable",
        "note": ("Recent results are limited to a configured large-cap "
                 "universe; the provider does not cover every NSE company. "
                 "Upcoming board meetings come from the NSE event calendar."),
        "event_source": ev_status,
    }


def _company_from_headline(title: str) -> str:
    """Best-effort company label from a broker headline.

    Only used as a display label; the source link is always kept so the reader
    can verify the actual subject.
    """
    title = str(title or "")
    for pat in (r"\bin\s+([A-Z][A-Za-z0-9&.\- ]{2,40}?)\s+(?:stock|shares)\b",
                r"^([A-Z][A-Za-z0-9&.\- ]{2,40}?)\s+(?:shares|stock)\b"):
        m = re.search(pat, title)
        if m:
            return m.group(1).strip()
    seg = re.split(r"[:|—]", title, maxsplit=1)[0].strip()
    return (seg or title)[:70]


def _build_broker_radar(groups: list[dict]) -> list[dict]:
    out = []
    phrases = ("upgrade", "downgrade", "initiate coverage", "buy rating",
               "sell rating", "outperform", "underperform", "neutral",
               "accumulate", "reduce")
    for a in groups:
        text = f"{a.get('title','')} {a.get('summary','')}"
        low = text.lower()
        broker = next((b for b in BROKERS if b.lower() in low), "")
        if not broker:
            continue
        target = ""
        m = re.search(r"(?:target price|price target|\btp\b)[^\d₹]{0,24}"
                      r"(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)", text, re.I)
        if m:
            target = m.group(1)
        # Use the earliest rating phrase so a later "buy rating" cannot
        # override an explicit "stays neutral" earlier in the same headline.
        found = [(low.find(p), p) for p in phrases if p in low]
        rec = min(found)[1] if found else ""
        if not target and not rec:
            continue
        out.append({
            "company": _company_from_headline(a.get("title", "")),
            "broker": broker,
            "recommendation": rec or "Mentioned (no explicit rating parsed)",
            "target": target or "Not published in the feed excerpt",
            "date": a.get("published"), "date_label": a.get("published_label"),
            "source": a.get("source"), "url": a.get("url"),
            "risks": ("Broker views are opinions and can carry conflicts of "
                      "interest. The full note, assumptions and disclosures "
                      "are not in the feed excerpt."),
            "kind": "reported_opinion",
            "is_agent_view": False,
            "title": a.get("title"),
        })
    return out[:8]


def _build_corporate_updates(anns: list[dict]) -> list[dict]:
    priority = {"Results": 0, "Order / contract": 1, "M&A": 1,
                "Regulatory / risk": 1, "Capacity expansion": 2,
                "Capital return": 2, "Fund raise": 3, "Management": 3,
                "Credit rating": 3, "Product launch": 3,
                "Corporate update": 9}
    rows = sorted(anns, key=lambda a: (priority.get(a.get("category"), 9),
                                      str(a.get("date") or "")),
                  reverse=False)[:24]
    for a in rows:
        a["impact"] = (f"{a.get('category')} filing. Price impact depends on "
                       "size versus expectations; read the attachment.")
        a["kind"] = "measured"
        a["interpretation_kind"] = "impact sentence is rules-based"
    return rows


def _build_geopolitics(groups: list[dict]) -> dict:
    items = _articles_for(groups, "geopolitics", 8)
    # US policy releases are primary and matter even without a war keyword.
    us = [a for a in groups if "us_policy" in (a.get("categories") or [])]
    for a in us:
        if len(items) >= 10:
            break
        if a["id"] not in {x["id"] for x in items}:
            a = dict(a)
            a["why"] = ("US monetary/regulatory policy changes global risk "
                        "appetite, the dollar and foreign flows into India.")
            items.append(a)
    items = sorted(items, key=lambda a: -int(a.get("score") or 0))[:10]
    return _section(
        "A", "US & Geopolitical", "radio", "news", items,
        status="ok" if items else "empty",
        note=("Only developments present in the configured feeds are shown. "
              "No event is assumed because it appeared in a template."),
        source="Public RSS/Atom feeds + official releases",
    )


def _build_oil(groups: list[dict], quotes: dict) -> dict:
    q = quotes.get("BZ=F") or {}
    news = _articles_for(groups, "oil", 6)
    drivers = []
    for sym, label in (("DX-Y.NYB", "US dollar index"), ("^TNX", "US 10Y yield")):
        d = quotes.get(sym) or {}
        if d.get("ok"):
            drivers.append({
                "label": label, "value": _money(d.get("price"), "USD"),
                "change_pct": d.get("change_pct"),
                "change_label": _pct_label(d.get("change_pct")),
                "direction": ("positive" if (d.get("change_pct") or 0) > 0
                              else "negative"),
                "as_of": d.get("as_of"), "source": d.get("source"),
            })
    return _section(
        "D", "Brent Crude Oil", "activity", "market",
        items=news, data={
            "quote": _quote_item(q) if q else None,
            "instrument": "Brent crude front-month futures (BZ=F)",
            "drivers": drivers,
            "implications": [
                "India imports most of its crude; a sustained rise raises the "
                "import bill and can pressure the current account and INR.",
                "Higher crude is usually supportive for upstream producers "
                "and mixed for refiners/OMCs depending on pricing and margins.",
                "Fuel inflation can influence RBI policy expectations.",
            ],
            "interpretation_kind": "implications are generic transmission "
                                   "channels, not a forecast",
        },
        status=("ok" if q.get("ok") and news else
                "partial" if q.get("ok") or news else "unavailable"),
        note=("Price is the front-month futures contract, not spot Brent or "
              "MCX crude. Contract roll and basis are not modelled."),
        source="Yahoo Finance · BZ=F", source_url="https://finance.yahoo.com/quote/BZ%3DF",
    )


def _build_gold(groups: list[dict], quotes: dict) -> dict:
    q = quotes.get("GC=F") or {}
    spot = quotes.get("XAUUSD=X") or {}
    news = _articles_for(groups, "gold", 6)
    drivers = []
    for sym, label in (("DX-Y.NYB", "US dollar index"), ("^TNX", "US 10Y yield")):
        d = quotes.get(sym) or {}
        if d.get("ok"):
            drivers.append({
                "label": label, "value": _money(d.get("price"), "USD"),
                "change_pct": d.get("change_pct"),
                "change_label": _pct_label(d.get("change_pct")),
                "direction": ("negative" if (d.get("change_pct") or 0) > 0
                              else "positive"),
                "note": ("A stronger dollar / higher nominal yield is "
                         "typically a headwind for gold."),
                "as_of": d.get("as_of"), "source": d.get("source"),
            })
    factors = []
    for n in news[:4]:
        factors.append({"label": n.get("title"), "sentiment": n.get("sentiment"),
                        "evidence": n.get("sentiment_evidence"),
                        "url": n.get("url"), "source": n.get("source")})
    return _section(
        "E", "Gold", "sparkles", "market",
        items=news, data={
            "quote": _quote_item(q) if q else None,
            "instrument": "COMEX gold front-month futures (GC=F)",
            "spot_quote": (_quote_item(spot) if spot.get("ok") else None),
            "spot_note": ("XAUUSD spot was requested separately; it is not "
                          "mixed into the COMEX card."),
            "drivers": drivers, "factors": factors,
            "interpretation_kind": "driver notes are interpretation, not "
                                   "price causation",
        },
        status=("ok" if (q.get("ok") or spot.get("ok")) and news else
                "partial" if q.get("ok") or spot.get("ok") or news
                else "unavailable"),
        note=("COMEX futures and XAUUSD spot are different instruments. MCX "
              "gold is not connected, so no MCX price is shown."),
        source="Yahoo Finance · GC=F",
        source_url="https://finance.yahoo.com/quote/GC%3DF",
    )


def _build_rupee(groups: list[dict], quotes: dict) -> dict:
    q = quotes.get("INR=X") or {}
    news = _articles_for(groups, "rupee", 6)
    factors = []
    dxy = quotes.get("DX-Y.NYB") or {}
    oil = quotes.get("BZ=F") or {}
    if dxy.get("ok"):
        factors.append({
            "label": "US dollar index", "value": _money(dxy.get("price"), "USD"),
            "change_label": _pct_label(dxy.get("change_pct")),
            "direction": ("negative" if (dxy.get("change_pct") or 0) > 0
                          else "positive"),
            "note": "A stronger dollar is typically a rupee headwind.",
            "as_of": dxy.get("as_of"), "source": dxy.get("source"),
        })
    if oil.get("ok"):
        factors.append({
            "label": "Brent crude", "value": _money(oil.get("price"), "USD"),
            "change_label": _pct_label(oil.get("change_pct")),
            "direction": ("negative" if (oil.get("change_pct") or 0) > 0
                          else "positive"),
            "note": "Costlier crude raises India's import bill, a rupee "
                    "headwind over time.",
            "as_of": oil.get("as_of"), "source": oil.get("source"),
        })
    for n in news[:4]:
        factors.append({"label": n.get("title"), "sentiment": n.get("sentiment"),
                        "evidence": n.get("sentiment_evidence"),
                        "url": n.get("url"), "source": n.get("source")})
    return _section(
        "F", "Indian Rupee", "trending-up", "market",
        items=news, data={
            "quote": _quote_item(q) if q else None,
            "instrument": "USD/INR spot (Yahoo INR=X)",
            "factors": factors,
            "implications": [
                "A weaker INR raises imported input costs and can feed "
                "inflation; import-heavy sectors are more exposed.",
                "IT/pharma exporters can get a translation tailwind, but "
                "hedging and realised rates differ.",
            ],
            "interpretation_kind": "transmission notes are interpretation",
        },
        status=("ok" if q.get("ok") and news else
                "partial" if q.get("ok") or news else "unavailable"),
        note=("USD/INR is an off-exchange spot reference from Yahoo; RBI's "
              "official reference rate is published separately."),
        source="Yahoo Finance · INR=X",
        source_url="https://finance.yahoo.com/quote/INR%3DX",
    )


def _build_flows(flows: dict) -> dict:
    rows = flows.get("rows") or []
    status = "ok" if flows.get("status") == "ok" else "unavailable"
    return _section(
        "G", "FII & DII Activity", "database", "market",
        data={
            "status": flows.get("status"), "date": flows.get("date"),
            "rows": rows, "note": flows.get("note", ""),
            "error": flows.get("error", ""),
            "source": flows.get("source"),
            "source_url": flows.get("source_url"),
            "as_of": flows.get("as_of"),
            "interpretation_kind": "no interpretation is added to the flows",
        },
        status=status,
        count=len(rows),
        note=("Official NSE provisional cash-market figures. If NSE is "
              "unavailable the section says so — no flow number is invented."),
        source="NSE FII/DII report",
        source_url=flows.get("source_url", ""),
    )


def _build_global(quotes: dict) -> dict:
    regions = []
    for region, instruments in GLOBAL.items():
        items = []
        for inst in instruments:
            q = quotes.get(inst["symbol"]) or {}
            row = _quote_item(q)
            row["name"] = inst["name"]
            row["instrument"] = f"{inst['name']} ({inst['symbol']})"
            items.append(row)
        regions.append({"region": region, "items": items,
                        "status": "ok" if any(i.get("ok") for i in items)
                        else "unavailable"})
    crypto = []
    for inst in ({"symbol": "BTC-USD", "name": "Bitcoin"},
                 {"symbol": "ETH-USD", "name": "Ethereum"}):
        q = quotes.get(inst["symbol"]) or {}
        row = _quote_item(q)
        row["name"] = inst["name"]
        row["instrument"] = (q.get("instrument")
                             or f"{inst['name']} spot ({inst['symbol']})")
        crypto.append(row)
    return _section(
        "C", "Global Markets", "gauge", "market",
        data={
            "regions": regions, "crypto": crypto,
            "crypto_note": ("Bitcoin and Ethereum use Delta Exchange India "
                            "perpetual futures with a 24-hour change. If the "
                            "exchange feed is unavailable, the row falls back "
                            "to Yahoo spot and says so."),
            "note": ("Session labels come from the provider's marketState. "
                     "A closed market's last quote is shown as the last "
                     "session, not as live. Crypto is 24/7."),
            "interpretation_kind": "none — quotes are measured values",
        },
        status=("ok" if any(r["status"] == "ok" for r in regions)
                or any(c.get("ok") for c in crypto) else "unavailable"),
        note="Delayed research feed for equities; crypto from Delta Exchange India.",
        source="Yahoo Finance + Delta Exchange India",
        source_url="https://finance.yahoo.com/",
    )


def _article_matches_sector(a: dict, sec: dict) -> bool:
    """Match a catalyst on the headline or explicit sector tags only.

    Summary bodies were matching every sector through unrelated words, so
    they are deliberately excluded.
    """
    if sec["name"] in (a.get("sectors") or []):
        title = str(a.get("title") or "").lower()
        # Sector tags are derived from title + summary; only accept the tag
        # when the headline itself carries a sector keyword, so a passing
        # mention in the summary cannot become the sector's catalyst.
        if not any(str(kw).strip().lower() in title
                   for kw in sec["keywords"]):
            return False
        return True
    title = str(a.get("title") or "").lower()
    for kw in sec["keywords"]:
        kw = str(kw).strip().lower()
        if not kw:
            continue
        if len(kw) <= 5 or " " not in kw:
            if re.search(rf"\b{re.escape(kw)}\b", title):
                return True
        elif kw in title:
            return True
    return False


def _sector_row(sec: dict, quotes: dict, groups: list[dict],
                nse_map: dict | None = None,
                nse_as_of: str = "") -> dict:
    q = quotes.get(sec["symbol"]) or {}
    hist = _fetch_history(sec["symbol"], "6mo", "1d")
    bars = hist.get("bars") or []
    closes = [b["close"] for b in bars]
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    px = _f(q.get("price"))
    trend = "Insufficient history"
    if px is not None and sma20 and sma50:
        if px > sma20 > sma50:
            trend = "Uptrend"
        elif px < sma20 < sma50:
            trend = "Downtrend"
        else:
            trend = "Rangebound"
    rows = [a for a in groups if _article_matches_sector(a, sec)]
    rows.sort(key=lambda a: -int(a.get("score") or 0))
    catalyst = rows[0] if rows else None
    supporting, opposing = [], []
    nse = (nse_map or {}).get(NSE_INDEX_FOR_SECTOR.get(sec["key"], "")) or {}
    nse_px = _f(nse.get("last"))
    nse_pct = _f(nse.get("percentChange"))
    if nse_px is not None:
        px = nse_px
        pct = nse_pct
        price_source = "NSE official allIndices"
        as_of = nse_as_of or _iso()
        as_of_label = _label(_parse_dt(as_of)) if as_of else ""
    else:
        pct = _f(q.get("change_pct"))
        price_source = q.get("source") or "Yahoo Finance"
        as_of = q.get("as_of")
        as_of_label = q.get("as_of_label")
    if pct is not None:
        (supporting if pct >= 0 else opposing).append(
            f"Sector instrument {_pct_label(pct)} today")
    if trend == "Uptrend":
        supporting.append("Price above 20-DMA and 50-DMA")
    elif trend == "Downtrend":
        opposing.append("Price below 20-DMA and 50-DMA")
    if catalyst:
        sent = catalyst.get("sentiment")
        if sent == "positive":
            supporting.append(f"Catalyst: {catalyst.get('title')}")
        elif sent == "negative":
            opposing.append(f"Catalyst: {catalyst.get('title')}")
        else:
            supporting.append(f"Headline to monitor: {catalyst.get('title')}")
    if pct is not None and len(supporting) > len(opposing):
        impact = "Positive"
    elif pct is not None and len(opposing) > len(supporting):
        impact = "Negative"
    elif pct is not None or catalyst:
        impact = "Mixed"
    else:
        impact = "Insufficient evidence"
    if pct is None and not catalyst:
        impact = "Insufficient evidence"
    reason_bits = []
    if pct is not None:
        reason_bits.append(f"today {_pct_label(pct)}")
    reason_bits.append(trend.lower())
    if catalyst:
        reason_bits.append(f"catalyst: {catalyst.get('title')[:90]}")
    else:
        reason_bits.append("no verified sector catalyst in the configured feeds")
    return {
        "key": sec["key"], "name": sec["name"], "symbol": sec["symbol"],
        "instrument": ("Listed proxy: " + sec["proxy"]) if sec.get("proxy")
                      else f"Sector index {sec['symbol']}",
        "price": px, "change_pct": pct,
        "change_label": _pct_label(pct),
        "price_source": price_source,
        "as_of": as_of, "as_of_label": as_of_label,
        "trend": trend,
        "history_bars": len(bars),
        "catalyst": catalyst.get("title") if catalyst else "",
        "catalyst_url": catalyst.get("url") if catalyst else "",
        "catalyst_source": catalyst.get("source") if catalyst else "",
        "supporting": supporting[:4], "opposing": opposing[:4],
        "impact": impact,
        "impact_reason": "; ".join(reason_bits) + ".",
        "evidence": ("limited" if trend == "Insufficient history"
                     else "sufficient") if pct is not None else "insufficient",
        "kind": "measured + interpretation",
        "not_a_recommendation": True,
        "status": "ok" if pct is not None else "unavailable",
        "error": "" if pct is not None else q.get("error", ""),
    }


def _build_sectors(groups: list[dict], quotes: dict,
                   refresh: bool = False) -> dict:
    nse_map, nse_status = _fetch_nse_indices(refresh)
    nse_as_of = nse_status.get("as_of") or _iso()
    rows = []
    with ThreadPoolExecutor(max_workers=6,
                            thread_name_prefix="news-sectors") as pool:
        futs = [pool.submit(_sector_row, sec, quotes, groups, nse_map,
                            nse_as_of)
                for sec in SECTORS]
        for f in futs:
            try:
                rows.append(f.result())
            except Exception as exc:
                rows.append({"name": "sector", "status": "unavailable",
                             "error": f"{type(exc).__name__}: {exc}",
                             "impact": "Insufficient evidence"})
    ok = sum(1 for r in rows if r.get("status") == "ok")
    nse_used = sum(1 for r in rows
                   if r.get("price_source") == "NSE official allIndices")
    return _section(
        "I", "Sector-Wise Outlook", "layers", "market", data={
            "sectors": rows,
            "note": ("Impact combines today's measured sector move, the "
                     "current trend and a verified catalyst. A positive "
                     "historical move alone is never used as a positive call. "
                     "Current level/change comes from NSE's official "
                     "allIndices snapshot; trend indicators use Yahoo daily "
                     "bars and say 'Insufficient history' when too few bars "
                     "are served."),
            "nse_snapshot_as_of": nse_as_of,
            "nse_snapshot_ok": bool(nse_map),
            "nse_rows": nse_used,
            "interpretation_kind": "impact labels are rules-based",
        },
        status="ok" if ok else "unavailable",
        note=f"{ok}/{len(rows)} sector instruments answered; "
             f"{nse_used} used the NSE official snapshot.",
        source="NSE allIndices + Yahoo Finance history + configured news feeds",
        source_url="https://www.nseindia.com/api/allIndices",
    )


def _build_technical(quotes: dict, refresh: bool = False) -> dict:
    nse_map, nse_status = _fetch_nse_indices(refresh)
    nse_as_of = nse_status.get("as_of") or _iso()
    items = []
    for sym, name, nse_name in (
        ("^NSEI", "NIFTY 50", "NIFTY 50"),
        ("^NSEBANK", "BANK NIFTY", "NIFTY BANK"),
    ):
        q = quotes.get(sym) or {}
        t = _technicals(sym, name, q, nse_map.get(nse_name), nse_as_of)
        items.append(t)
    ok = any(t.get("status") == "ok" for t in items)
    return _section(
        "L", "Technical Market View", "activity", "market", data={
            "indices": items,
            "note": ("SMA = simple moving average; RSI = Wilder 14; ATR = "
                     "Wilder 14 true range; VWAP = last intraday session's "
                     "volume-weighted typical price; pivots = classic "
                     "floor-trader formula from the last completed H/L/C."),
            "interpretation_kind": ("scenarios are conditional descriptions, "
                                    "not guarantees that a level is reached"),
        },
        status="ok" if ok else "unavailable",
        note="Every level carries its formula in `basis`.",
        source="Yahoo Finance daily + 5-minute bars",
    )


def _build_outlook(quotes: dict, technical: dict, sectors: dict,
                   flows: dict, events: list[dict], calendar: list[dict]) -> dict:
    factors = []
    score = 0.0
    total = 0
    for sym, label, invert in (
        ("^NSEI", "NIFTY 50", False),
        ("^NSEBANK", "BANK NIFTY", False),
        ("^INDIAVIX", "India VIX", True),
        ("^GSPC", "S&P 500 (US)", False),
        ("^N225", "Nikkei 225 (Asia)", False),
        ("BZ=F", "Brent crude", True),
        ("INR=X", "USD/INR", True),
    ):
        q = quotes.get(sym) or {}
        pct = _f(q.get("change_pct"))
        if pct is None:
            continue
        total += 1
        direction = "positive" if (pct > 0) != invert else "negative"
        score += 1 if direction == "positive" else -1
        factors.append({
            "label": label, "value": _pct_label(pct),
            "direction": direction, "measured": True,
            "invert_note": ("inverted: a rise is a headwind" if invert else ""),
            "source": q.get("source"), "as_of": q.get("as_of"),
            "as_of_label": q.get("as_of_label"),
        })
    if total:
        if score >= 2:
            direction = "Bullish"
        elif score <= -2:
            direction = "Bearish"
        else:
            direction = "Mixed"
        target_dir = {"Bullish": "positive", "Bearish": "negative"}.get(
            direction)
        pos_n = sum(1 for f in factors if f["direction"] == "positive")
        neg_n = sum(1 for f in factors if f["direction"] == "negative")
        if target_dir:
            agree = pos_n if target_dir == "positive" else neg_n
            confidence = (f"{agree} of {total} measured inputs point "
                          f"{direction.lower()}")
        else:
            confidence = (f"{pos_n} positive vs {neg_n} negative measured "
                          f"inputs — no clear direction")
    else:
        direction, confidence = "Insufficient evidence", "no quote answered"

    nifty = quotes.get("^NSEI") or {}
    bank = quotes.get("^NSEBANK") or {}
    vix = quotes.get("^INDIAVIX") or {}
    summary = (f"Rules-based read of measured inputs: {direction.lower()}. "
               f"NIFTY {_pct_label(nifty.get('change_pct'))}, "
               f"BANK NIFTY {_pct_label(bank.get('change_pct'))}, "
               f"India VIX {_pct_label(vix.get('change_pct'))}. "
               "This is an interpretation, not a forecast.")
    triggers = []
    if (quotes.get("BZ=F") or {}).get("ok"):
        triggers.append("A reversal in Brent crude would change the "
                        "inflation/energy read.")
    if events:
        triggers.append("NSE result/board meetings on the calendar can shift "
                        "sector expectations.")
    if calendar:
        triggers.append("High-impact global economic events can move the "
                        "dollar, yields and risk appetite.")
    if (flows.get("status") == "ok"):
        triggers.append("Fresh FII/DII flows can confirm or contradict the "
                        "price move.")
    if not triggers:
        triggers.append("No additional verified trigger was available.")

    today = _ist().date()
    todays_events = [e for e in events if e.get("date") == today.isoformat()]
    upcoming_events = [e for e in events if e.get("date") != today.isoformat()]
    tech_idx = {t.get("symbol"): t for t in (technical.get("data") or {}).get(
        "indices", [])}
    return _section(
        "B", "Market Outlook Today", "target", "market", data={
            "direction": direction,
            "confidence": confidence,
            "summary": summary,
            "factors": factors,
            "change_triggers": triggers,
            "events_today": todays_events[:8],
            "events_upcoming": upcoming_events[:10],
            "calendar_high_impact": calendar[:8],
            "levels": {
                "nifty": tech_idx.get("^NSEI", {}).get("levels", []),
                "banknifty": tech_idx.get("^NSEBANK", {}).get("levels", []),
            },
            "measured_kind": "measured inputs",
            "interpretation_kind": ("direction label is a rules-based "
                                    "interpretation of the measured inputs"),
        },
        status="ok" if total else "unavailable",
        note=("Measured inputs are quoted with source and timestamp. The "
              "direction is a transparent rule, not an AI prediction."),
        source="Yahoo Finance quotes + NSE event calendar + Forex Factory",
    )


def _build_focus(groups: list[dict], events: list[dict], ipos: list[dict],
                 flows: dict) -> dict:
    picks = sorted(groups, key=lambda a: -int(a.get("score") or 0))[:8]
    items = []
    for i, a in enumerate(picks, 1):
        items.append({
            "rank": i, "title": a.get("title"), "why": a.get("why"),
            "category": (a.get("categories") or ["market"])[0],
            "importance": a.get("importance"),
            "source": a.get("source"), "url": a.get("url"),
            "published_label": a.get("published_label"),
            "sectors": a.get("sectors") or [],
            "kind": "interpretation",
        })
    today = _ist().date()
    for e in events:
        if e.get("date") == today.isoformat() and len(items) < 10:
            items.append({
                "rank": len(items) + 1,
                "title": f"{e.get('company')} — {e.get('purpose')}",
                "why": ("Scheduled NSE event today; results/board outcomes "
                        "can move the stock and its sector."),
                "category": "event", "importance": "high",
                "source": e.get("source"), "url": e.get("source_url"),
                "published_label": e.get("date_label"), "sectors": [],
                "kind": "measured",
            })
    for ipo in ipos:
        if len(items) < 11 and str(ipo.get("status", "")).lower() == "active":
            items.append({
                "rank": len(items) + 1,
                "title": f"IPO open: {ipo.get('company')} "
                         f"({ipo.get('price_band')})",
                "why": ("An open IPO absorbs liquidity and its subscription "
                        "trend can affect sector sentiment."),
                "category": "ipo", "importance": "high",
                "source": ipo.get("source"), "url": ipo.get("source_url"),
                "published_label": f"{ipo.get('open')} → {ipo.get('close')}",
                "sectors": [], "kind": "measured",
            })
    return _section(
        "H", "Key Focus Areas", "zap", "market", data={
            "items": items,
            "note": ("Ranked by recency, source reliability, section "
                     "relevance and measured market significance. Ranking is "
                     "not a trade instruction."),
        },
        status="ok" if items else "empty",
        source="Configured feeds + NSE calendar",
    )


def _build_ipo(ipos: list[dict], actions: list[dict],
               groups: list[dict]) -> dict:
    news = _articles_for(groups, "ipo", 5)
    return _section(
        "N", "IPO & Corporate Actions", "calendar", "market",
        items=news, data={
            "ipos": ipos, "actions": actions,
            "note": ("Exchange dates are confirmed facts. A listing date the "
                     "NSE feed does not publish is shown as 'not confirmed', "
                     "never inferred."),
            "interpretation_kind": "none — dates and subjects are as filed",
        },
        status="ok" if ipos or actions or news else "empty",
        source="NSE current issues + corporate actions",
        source_url="https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
    )


def _research_idea(sym: str, name: str, sector: str, quote: dict,
                   groups: list[dict]) -> dict | None:
    hist = _fetch_history(sym, "6mo", "1d")
    if not hist.get("ok"):
        return None
    bars = hist.get("bars") or []
    closes = [b["close"] for b in bars]
    if len(closes) < 50 or not quote.get("ok"):
        return None
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    rsi = _rsi(closes, 14)
    px = _f(quote.get("price"))
    if px is None or not sma20 or not sma50 or rsi is None:
        return None
    chg20 = ((px / closes[-21] - 1.0) * 100.0 if len(closes) > 21 else None)
    atr = _atr(bars, 14)
    evidence = []
    if px > sma20:
        evidence.append("price above 20-DMA")
    else:
        evidence.append("price below 20-DMA")
    if sma20 > sma50:
        evidence.append("20-DMA above 50-DMA")
    else:
        evidence.append("20-DMA below 50-DMA")
    if 45 <= rsi <= 70:
        evidence.append(f"RSI {rsi:.1f} in a constructive band")
    elif rsi < 40:
        evidence.append(f"RSI {rsi:.1f} weak")
    else:
        evidence.append(f"RSI {rsi:.1f} neutral")
    constructive = px > sma20 > sma50 and 45 <= rsi <= 70
    weak = px < sma20 < sma50 and rsi < 50
    if constructive:
        classification = "WATCH — constructive trend"
    elif weak:
        classification = "WATCH — weak trend (no new longs)"
    else:
        classification = "NO CLEAR SETUP"
    strength = ("strong" if len([e for e in evidence if "above" in e or
                                 "constructive" in e]) >= 3 else
                "moderate" if px > sma20 or rsi >= 50 else "weak")
    catalyst = None
    for a in groups:
        text = f"{a.get('title','')} {a.get('summary','')}".lower()
        if name.lower() in text or sym.replace(".NS", "").lower() in text or \
                sector.lower() in (a.get("sectors") or ""):
            catalyst = a
            break
    risk = (f"Invalidation: a daily close below the 50-DMA "
            f"(₹{sma50:,.2f})." if px > sma50 else
            f"Invalidation: a daily close above the 20-DMA "
            f"(₹{sma20:,.2f}) would weaken the bearish read.")
    return {
        "symbol": sym, "exchange": "NSE", "name": name, "sector": sector,
        "price": _round(px), "as_of": quote.get("as_of"),
        "as_of_label": quote.get("as_of_label"),
        "change_pct": quote.get("change_pct"),
        "change_label": _pct_label(quote.get("change_pct")),
        "appearance_reason": (
            f"Technical screen: {', '.join(evidence)}; "
            f"20-session change {_pct_label(chg20)}."),
        "catalyst": catalyst.get("title") if catalyst else
                    "No verified company-specific catalyst in the feeds",
        "catalyst_url": catalyst.get("url") if catalyst else "",
        "technical_evidence": evidence,
        "sma20": _round(sma20), "sma50": _round(sma50),
        "rsi14": _round(rsi), "atr14": _round(atr, 2),
        "risk": risk,
        "evidence_strength": strength,
        "classification": classification,
        "setup_type": ("watchlist candidate" if "WATCH" in classification
                       else "no setup"),
        "not_a_recommendation": True,
        "kind": "interpretation over measured bars",
    }


def _build_research(groups: list[dict], quotes: dict) -> dict:
    ideas = []
    with ThreadPoolExecutor(max_workers=5,
                            thread_name_prefix="news-research") as pool:
        futs = [pool.submit(_research_idea, sym, name, sector,
                            quotes.get(sym) or {}, groups)
                for sym, name, sector in WATCHLIST]
        for f in futs:
            try:
                r = f.result()
            except Exception:
                r = None
            if r:
                ideas.append(r)
    order = {"strong": 0, "moderate": 1, "weak": 2}
    ideas.sort(key=lambda x: (0 if "constructive" in x["classification"] else
                              1 if "weak" in x["classification"] else 2,
                              order.get(x["evidence_strength"], 3)))
    return _section(
        "O", "Top Actionable Research Ideas", "search", "market", data={
            "ideas": ideas[:8],
            "note": ("A rules-based technical screen over a configured "
                     "large-cap watchlist. 'WATCH' is a research candidate, "
                     "not a buy/sell instruction. No order is placed."),
            "interpretation_kind": "technical screen is interpretation",
        },
        status="ok" if ideas else "insufficient",
        note="No idea is labelled BUY. Weak evidence is shown as WATCH or NO CLEAR SETUP.",
        source="Yahoo Finance bars + configured feeds",
    )


def _completed_session_date(hist: dict) -> _date | None:
    bars = hist.get("bars") or []
    if not bars:
        return None
    last_ts = str(bars[-1].get("ts") or "")[:10]
    try:
        d = datetime.strptime(last_ts, "%Y-%m-%d").date()
    except ValueError:
        return None
    now = _ist()
    if d == now.date() and now.time() < datetime.strptime("15:35", "%H:%M").time():
        return None
    return d


def _build_previous(quotes: dict, sectors: dict, flows: dict,
                    events: list[dict], groups: list[dict],
                    nse_map: dict | None = None,
                    nse_as_of: str = "") -> dict:
    nifty_hist = _fetch_history("^NSEI", "3mo", "1d")
    session = _completed_session_date(nifty_hist)
    if not session:
        return _section(
            "P", "Previous-Session Summary", "clock", "market", data=None,
            status="unavailable",
            note="A completed NSE session could not be identified from the "
                 "provider's daily bars.",
            source="Yahoo Finance daily bars",
        )
    rows = []
    for sym, name in (("^NSEI", "NIFTY 50"), ("^NSEBANK", "BANK NIFTY"),
                      ("^BSESN", "SENSEX")):
        h = _fetch_history(sym, "3mo", "1d")
        bars = h.get("bars") or []
        picked = None
        for i, b in enumerate(bars):
            if str(b.get("ts", ""))[:10] == session.isoformat():
                picked = (i, b)
                break
        if not picked:
            continue
        i, b = picked
        prev = bars[i - 1] if i > 0 else None
        pct = ((b["close"] / prev["close"] - 1.0) * 100.0
               if prev and prev.get("close") else None)
        rows.append({"name": name, "symbol": sym, "close": b["close"],
                     "change_pct": _round(pct),
                     "change_label": _pct_label(pct),
                     "date": session.isoformat(),
                     "source": "Yahoo Finance daily bars"})
    if nse_map and session == _ist().date():
        by_name = {r.get("name"): r for r in rows}
        for name, nse_name in (("NIFTY 50", "NIFTY 50"),
                               ("BANK NIFTY", "NIFTY BANK")):
            row = by_name.get(name)
            nse = nse_map.get(nse_name) or {}
            if row is not None and _f(nse.get("last")) is not None:
                row["close"] = _f(nse.get("last"))
                row["change_pct"] = _f(nse.get("percentChange"))
                row["change_label"] = _pct_label(row["change_pct"])
                row["source"] = "NSE official allIndices"
                row["as_of"] = nse_as_of
    leaders, laggards = [], []
    for s in (sectors.get("data") or {}).get("sectors", []):
        pct = _f(s.get("change_pct"))
        if pct is None:
            continue
        row = {"name": s.get("name"), "change_pct": pct,
               "change_label": _pct_label(pct)}
        (leaders if pct >= 0 else laggards).append(row)
    leaders.sort(key=lambda x: -x["change_pct"])
    laggards.sort(key=lambda x: x["change_pct"])
    drivers = []
    for a in groups:
        dt = _parse_dt(a.get("published"))
        if dt and dt.astimezone(IST).date() == session:
            drivers.append(a)
    drivers.sort(key=lambda a: -int(a.get("score") or 0))
    carry = [e for e in events if e.get("date") and e["date"] > session.isoformat()]
    return _section(
        "P", "Previous-Session Summary", "clock", "market", data={
            "session_date": session.isoformat(),
            "session_label": _day_label(session),
            "indices": rows,
            "drivers": drivers[:6],
            "leaders": leaders[:5], "laggards": laggards[:5],
            "flows": flows if flows.get("status") == "ok" else None,
            "carry_over": carry[:8],
            "note": ("This is the last COMPLETED NSE session, identified "
                     "from the provider's daily bars — not an arbitrary "
                     "calendar day."),
            "interpretation_kind": "drivers are sourced headlines; the "
                                   "selection is rules-based",
        },
        status="ok" if rows else "unavailable",
        source="Yahoo Finance daily bars + configured feeds + NSE",
    )


# ── session metadata ────────────────────────────────────────────────────────
def _session_info(nifty_hist: dict) -> dict:
    now = _ist()
    today = now.date()
    bars = nifty_hist.get("bars") or []
    last_bar_date = None
    if bars:
        try:
            last_bar_date = datetime.strptime(
                str(bars[-1].get("ts"))[:10], "%Y-%m-%d").date()
        except ValueError:
            last_bar_date = None
    weekday = today.weekday() < 5
    session_date = today if (weekday and last_bar_date == today) else (
        last_bar_date or today)
    if not weekday:
        phase, phase_label = "closed", "Weekend — NSE closed"
    elif now.time() < datetime.strptime("09:00", "%H:%M").time():
        phase, phase_label = "pre_open", "Pre-open"
    elif now.time() < datetime.strptime("09:15", "%H:%M").time():
        phase, phase_label = "pre_market", "Pre-market"
    elif now.time() <= datetime.strptime("15:30", "%H:%M").time():
        phase, phase_label = "open", "Market open"
    else:
        phase, phase_label = "post_market", "Post-market"
    trading_day = bool(weekday and last_bar_date == today)
    if weekday and not trading_day:
        session_note = ("No NSE daily bar for today was served — the exchange "
                        "may be closed for a holiday or the provider is "
                        "delayed. No holiday calendar is assumed.")
    elif not weekday:
        session_note = "NSE is closed on the weekend."
    else:
        session_note = "Today's NSE daily bar was served by the provider."
    prev = _completed_session_date(nifty_hist)
    return {
        "date": session_date.isoformat(),
        "date_label": _day_label(session_date),
        "current_date": today.isoformat(),
        "current_date_label": _day_label(today),
        "ist_time": now.strftime("%H:%M:%S"),
        "ist_time_label": now.strftime("%d %b %Y, %H:%M:%S IST"),
        "phase": phase, "phase_label": phase_label,
        "is_trading_day": trading_day,
        "session_note": session_note,
        "last_bar_date": last_bar_date.isoformat() if last_bar_date else "",
        "previous_session_date": prev.isoformat() if prev else "",
        "previous_session_label": _day_label(prev) if prev else "",
    }


# ── source registry ─────────────────────────────────────────────────────────
def _provider_status_quote() -> dict:
    return {
        "name": "Yahoo Finance quotes", "url": "https://finance.yahoo.com/",
        "ok": yf is not None, "count": 0,
        "detail": ("Delayed research feed; every quote carries its own "
                   "provider timestamp" if yf is not None
                   else "yfinance is not installed"),
        "mode": "provider_api", "as_of": _iso(),
        "reliability": "high",
    }


def _all_quote_symbols() -> list[tuple[str, str, str]]:
    rows = []
    seen = set()
    for p in PULSE:
        if p["symbol"] not in seen:
            rows.append((p["symbol"], p["name"],
                         p.get("instrument", p["name"])))
            seen.add(p["symbol"])
    for instruments in GLOBAL.values():
        for i in instruments:
            if i["symbol"] not in seen:
                rows.append((i["symbol"], i["name"], i["name"]))
                seen.add(i["symbol"])
    for s in SECTORS:
        if s["symbol"] not in seen:
            rows.append((s["symbol"], s["name"], s["name"]))
            seen.add(s["symbol"])
    for sym, name, _sector in WATCHLIST:
        if sym not in seen:
            rows.append((sym, name, name))
            seen.add(sym)
    for sym, name in (("DX-Y.NYB", "US dollar index"),
                      ("^TNX", "US 10Y yield"),
                      ("XAUUSD=X", "XAUUSD spot"),
                      ("ETH-USD", "Ethereum")):
        if sym not in seen:
            rows.append((sym, name, name))
            seen.add(sym)
    return rows


# ── the briefing build ──────────────────────────────────────────────────────
def _empty_payload(error: str) -> dict:
    return {
        "ok": False, "version": 2, "generated_at": _iso(),
        "date": _ist().date().isoformat(), "session": {},
        "pulse": [], "sections": {}, "sources": [], "errors": [error],
        "freshness": {"generated_at": _iso(), "age_s": 0, "ttl_s": 0,
                      "stale": True, "stale_reason": error},
        "meta": {"cached": False, "elapsed_ms": 0, "refresh": False,
                 "provider": "news_briefing"},
        "disclaimer": DISCLAIMER,
    }


def build_briefing(refresh: bool = False, date: str | None = None) -> dict:
    """Build (or load) the NEWS & IMPACT payload. Never raises."""
    started = time.monotonic()
    cfg = settings()
    if date:
        try:
            want = datetime.strptime(str(date), "%Y-%m-%d").date()
        except ValueError:
            return _empty_payload("date must be YYYY-MM-DD")
        if want != _ist().date():
            hist = load_history(want.isoformat())
            if hist:
                hist = copy.deepcopy(hist)
                hist.setdefault("meta", {})["historical"] = True
                return hist
            return _empty_payload(
                f"No stored briefing for {want.isoformat()} yet. Historical "
                "briefings are saved from the first successful build onward.")

    with _cache_lock:
        data = _last_build.get("data")
        age = time.monotonic() - float(_last_build.get("t") or 0)
    if data and not refresh and age < float(cfg.get("refresh_ttl_s", 300)):
        out = copy.deepcopy(data)
        out.setdefault("meta", {})["cached"] = True
        return out

    acquired = _build_lock.acquire(timeout=90)
    if not acquired:
        if data:
            out = copy.deepcopy(data)
            out.setdefault("meta", {})["cached"] = True
            out.setdefault("freshness", {})["stale"] = True
            out["freshness"]["stale_reason"] = (
                "another briefing build is still running; this is the last "
                "successful snapshot")
            return out
        return _empty_payload("another briefing build is still running")
    try:
        # double-check after acquiring the lock
        with _cache_lock:
            data = _last_build.get("data")
            age = time.monotonic() - float(_last_build.get("t") or 0)
        if data and not refresh and age < float(cfg.get("refresh_ttl_s", 300)):
            out = copy.deepcopy(data)
            out.setdefault("meta", {})["cached"] = True
            return out

        errors: list[str] = []
        # 1) parallel collection: news, official NSE, calendar, quotes
        symbols = _all_quote_symbols()
        with ThreadPoolExecutor(max_workers=12,
                                thread_name_prefix="news-brief") as pool:
            f_news = pool.submit(_collect_articles, refresh)
            f_fii = pool.submit(_fetch_fii_dii, refresh)
            f_ann = pool.submit(_fetch_announcements, refresh)
            f_act = pool.submit(_fetch_corporate_actions, refresh)
            f_ipo = pool.submit(_fetch_ipos, refresh)
            f_evt = pool.submit(_fetch_events, refresh)
            f_cal = pool.submit(_fetch_calendar)
            f_delta = pool.submit(_fetch_delta_crypto, refresh)
            f_quotes = {
                sym: pool.submit(_fetch_quote, sym, name, inst, refresh,
                                 sym in ("BTC-USD", "ETH-USD"))
                for sym, name, inst in symbols
            }
            try:
                articles, feed_sources = f_news.result()
            except Exception as exc:
                articles, feed_sources = [], []
                errors.append(f"news collection: {exc}")
            try:
                flows = f_fii.result()
            except Exception as exc:
                flows = {"status": "unavailable", "rows": [],
                         "error": str(exc)[:180],
                         "source_url": "https://www.nseindia.com/reports/fii-dii"}
                errors.append(f"FII/DII: {exc}")
            try:
                anns, ann_status = f_ann.result()
            except Exception as exc:
                anns, ann_status = [], {"ok": False, "detail": str(exc)}
                errors.append(f"NSE announcements: {exc}")
            try:
                actions, act_status = f_act.result()
            except Exception as exc:
                actions, act_status = [], {"ok": False, "detail": str(exc)}
                errors.append(f"NSE corporate actions: {exc}")
            try:
                ipos, ipo_status = f_ipo.result()
            except Exception as exc:
                ipos, ipo_status = [], {"ok": False, "detail": str(exc)}
                errors.append(f"NSE IPOs: {exc}")
            try:
                events, evt_status = f_evt.result()
            except Exception as exc:
                events, evt_status = [], {"ok": False, "detail": str(exc)}
                errors.append(f"NSE events: {exc}")
            try:
                calendar, cal_status = f_cal.result()
            except Exception as exc:
                calendar, cal_status = [], {"ok": False, "detail": str(exc)}
                errors.append(f"Forex Factory: {exc}")
            quotes = {}
            for sym, fut in f_quotes.items():
                try:
                    q = fut.result()
                except Exception as exc:
                    q = {"symbol": sym, "ok": False,
                         "error": f"{type(exc).__name__}: {exc}",
                         "source": "Yahoo Finance", "as_of": _iso()}
                quotes[sym] = q
                if not q.get("ok"):
                    errors.append(f"quote {sym}: {q.get('error', 'unavailable')}")

            # Delta Exchange perpetuals replace Yahoo's spot crypto quotes
            # only when the exchange feed actually answered. On failure the
            # Yahoo quote remains and is labelled as Yahoo.
            try:
                delta = f_delta.result()
            except Exception as exc:
                delta = {"quotes": {}, "status": {
                    "name": "Delta Exchange India",
                    "url": DELTA_API + "/v2/tickers", "ok": False,
                    "count": 0, "detail": f"{type(exc).__name__}: {exc}",
                    "mode": "official_api", "reliability": "high",
                    "as_of": _iso()}}
            delta_status = delta.get("status") or {
                "name": "Delta Exchange India", "ok": False, "count": 0,
                "detail": "no status served", "mode": "official_api",
                "reliability": "high", "as_of": _iso()}
            for _sym, _dq in (delta.get("quotes") or {}).items():
                quotes[_sym] = _dq
            if not delta_status.get("ok"):
                errors.append("Delta crypto: "
                              + (delta_status.get("detail") or "unavailable"))

        # Prefer the official NSE snapshot for the two Indian headline indices:
        # Yahoo's index previous-close can differ from NSE, which would make
        # the pulse, outlook and technical cards disagree with each other.
        nse_map, nse_status = _fetch_nse_indices(refresh)
        for _sym, _nse_name in (("^NSEI", "NIFTY 50"),
                                ("^NSEBANK", "NIFTY BANK")):
            _row = nse_map.get(_nse_name) or {}
            _q = quotes.get(_sym)
            if not _q or _f(_row.get("last")) is None:
                continue
            _q["price"] = _f(_row.get("last"))
            _q["prev_close"] = _f(_row.get("previousClose"))
            _q["change"] = _f(_row.get("variation"))
            _q["change_pct"] = _f(_row.get("percentChange"))
            _q["source"] = "NSE official allIndices"
            _q["provenance"] = "official"
            _q["as_of"] = nse_status.get("as_of") or _iso()
            _q["as_of_label"] = _label(_parse_dt(_q["as_of"]))
            _q["quote_time_estimated"] = False
            _q["ok"] = True
            _q["error"] = ""

        # 2) enrich + group news
        enriched = [_enrich(a) for a in articles]
        for a in enriched:
            if a.get("feed") == "Federal Reserve":
                low = f"{a.get('title','')} {a.get('summary','')}".lower()
                if any(k in low for k in ("monetary", "rate", "policy",
                                          "supervision", "regulation",
                                          "enforcement", "speech", "statement")):
                    a.setdefault("categories", []).append("us_policy")
        groups = _group_articles(enriched)
        groups.sort(key=lambda a: -int(a.get("score") or 0))

        # 3) sections
        technical = _build_technical(quotes, refresh)
        sectors = _build_sectors(groups, quotes, refresh)
        flows_sec = _build_flows(flows)
        outlook = _build_outlook(quotes, technical, sectors, flows,
                                 events, calendar)
        results = _build_results(refresh)
        ipo = _build_ipo(ipos, actions, groups)
        research = _build_research(groups, quotes)
        nse_map, nse_status = _fetch_nse_indices(refresh)
        prev = _build_previous(quotes, sectors, flows, events, groups,
                               nse_map, nse_status.get("as_of") or _iso())
        focus = _build_focus(groups, events, ipos, flows)
        corporate = _build_corporate_updates(anns)
        sections = {
            "A": _build_geopolitics(groups),
            "B": outlook,
            "C": _build_global(quotes),
            "D": _build_oil(groups, quotes),
            "E": _build_gold(groups, quotes),
            "F": _build_rupee(groups, quotes),
            "G": flows_sec,
            "H": focus,
            "I": sectors,
            "J": _section(
                "J", "Key Quarterly Results", "gauge", "market",
                items=[], data=results,
                status=("ok" if results.get("recent") or
                        results.get("upcoming") else "unavailable"),
                note=results.get("note", ""),
                source="Yahoo Finance quarterly statement + NSE event calendar",
                source_url="https://www.nseindia.com/companies-listing/"
                           "corporate-filings-financial-results",
                ),
            "K": _section(
                "K", "Results & Corporate Updates", "inbox", "news",
                items=corporate,
                status="ok" if corporate else "empty",
                note=("Primary NSE filings, newest first. The attachment is "
                      "the source of the full text."),
                source="NSE corporate announcements",
                source_url="https://www.nseindia.com/companies-listing/"
                           "corporate-filings-application",
                ),
            "L": technical,
            "M": _section(
                "M", "Broker & Analyst Radar", "user", "news",
                items=_build_broker_radar(groups),
                status="ok" if _build_broker_radar(groups) else "empty",
                note=("Only notes whose headline/summary actually names a "
                      "broker and a rating or target. No target is invented."),
                source="Configured news feeds",
                ),
            "N": ipo,
            "O": research,
            "P": prev,
        }

        # 4) pulse
        pulse = []
        for p in PULSE:
            q = quotes.get(p["symbol"]) or {}
            row = _quote_item(q)
            row["name"] = p["name"]
            row["group"] = p.get("group")
            pulse.append(row)

        # 5) session + sources
        session = _session_info(_fetch_history("^NSEI", "5d", "1d"))
        sources = list(feed_sources) + [
            _provider_status_quote(),
            delta_status,
            {**ann_status, "name": "NSE corporate announcements"},
            {**act_status, "name": "NSE corporate actions"},
            {**ipo_status, "name": "NSE current IPOs"},
            {**evt_status, "name": "NSE event calendar"},
            cal_status,
            {**(flows.get("status_detail") or {}),
             "name": "NSE FII/DII"},
        ]
        for s in sources:
            s.setdefault("as_of", _iso())
            s.setdefault("url", "")
            s.setdefault("ok", False)
            s.setdefault("count", 0)
            s.setdefault("detail", "")
        # provider quote count
        for s in sources:
            if s.get("name") == "Yahoo Finance quotes":
                s["count"] = sum(1 for q in quotes.values() if q.get("ok"))
                s["detail"] = (f"{s['count']}/{len(quotes)} quote requests "
                               "answered")
                s["ok"] = s["count"] > 0

        generated = _now()
        age_s = 0
        payload = {
            "ok": True,
            "version": 2,
            "generated_at": _iso(generated),
            "date": session.get("date"),
            "session": session,
            "pulse": pulse,
            "sections": sections,
            "sources": sources,
            "errors": errors[:30],
            "freshness": {
                "generated_at": _iso(generated),
                "age_s": age_s,
                "ttl_s": int(cfg.get("refresh_ttl_s", 300)),
                "stale": False,
                "stale_reason": "",
            },
            "meta": {
                "cached": False,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "refresh": bool(refresh),
                "provider": "news_briefing",
                "articles": len(groups),
                "quotes_ok": sum(1 for q in quotes.values() if q.get("ok")),
                "interpretation": ("Rules-based labels over measured data. "
                                   "AI/LLM memory is not used for prices or "
                                   "breaking news."),
            },
            "disclaimer": DISCLAIMER,
        }
        # keep payload honest about partial provider failure
        ok_sections = sum(1 for s in sections.values()
                          if s.get("status") == "ok")
        payload["partial"] = bool(errors) or ok_sections < len(sections)
        payload["sections_ok"] = ok_sections
        payload["sections_total"] = len(sections)

        with _cache_lock:
            _last_build.update({"t": time.monotonic(),
                                "data": copy.deepcopy(payload),
                                "error": ""})
        _write_json(BRIEF_DIR / "latest.json", payload)
        _write_json(HISTORY_DIR / f"{session.get('date')}.json", payload)
        return payload
    except Exception as exc:
        with _cache_lock:
            last = _last_build.get("data")
        if last:
            out = copy.deepcopy(last)
            out.setdefault("freshness", {})["stale"] = True
            out["freshness"]["stale_reason"] = (
                f"refresh failed: {type(exc).__name__}: {str(exc)[:180]}")
            out.setdefault("meta", {})["cached"] = True
            return out
        return _empty_payload(f"{type(exc).__name__}: {str(exc)[:220]}")
    finally:
        _build_lock.release()


def refresh_briefing() -> dict:
    return build_briefing(refresh=True)


# ── history / persistence ───────────────────────────────────────────────────
def list_history(limit: int = 30) -> list[dict]:
    rows = []
    try:
        for p in sorted(HISTORY_DIR.glob("*.json"), reverse=True)[:limit]:
            data = _read_json(p, {})
            if not isinstance(data, dict) or not data.get("ok"):
                continue
            rows.append({
                "date": p.stem,
                "date_label": _day_label(p.stem),
                "generated_at": data.get("generated_at"),
                "direction": ((data.get("sections") or {}).get("B", {})
                              .get("data", {}) or {}).get("direction", ""),
                "headline": (((data.get("sections") or {}).get("H", {})
                              .get("data", {}) or {}).get("items") or
                             [{}])[0].get("title", ""),
                "sections_ok": data.get("sections_ok"),
                "sections_total": data.get("sections_total"),
            })
    except Exception:
        pass
    return rows


def load_history(date: str) -> dict | None:
    try:
        d = datetime.strptime(str(date), "%Y-%m-%d").date()
    except ValueError:
        return None
    data = _read_json(HISTORY_DIR / f"{d.isoformat()}.json", None)
    return data if isinstance(data, dict) and data.get("ok") else None


def _freshness(payload: dict) -> dict:
    gen = _parse_dt(payload.get("generated_at"))
    ttl = int((payload.get("freshness") or {}).get("ttl_s") or 300)
    age = int((_now() - gen.astimezone(UTC)).total_seconds()) if gen else 0
    stale = age > ttl
    return {"generated_at": payload.get("generated_at"), "age_s": age,
            "ttl_s": ttl, "stale": stale,
            "stale_reason": (
                f"data is {age}s old; refresh interval is {ttl}s"
                if stale else "")}


# ── digest + alerts + schedule ─────────────────────────────────────────────
def briefing_digest(payload: dict, max_items: int = 8) -> str:
    if not payload or not payload.get("ok"):
        return ("NEWS & IMPACT: no verified briefing available — "
                + "; ".join((payload or {}).get("errors") or ["unknown error"]))
    s = payload.get("session") or {}
    sec = payload.get("sections") or {}
    lines = [
        f"NEWS & IMPACT — {s.get('date_label', '')} ({s.get('phase_label', '')})",
        f"Generated {_label(payload.get('generated_at'))}",
        "",
    ]
    outlook = (sec.get("B") or {}).get("data") or {}
    if outlook:
        lines += [f"Outlook: {outlook.get('direction')} — "
                  f"{outlook.get('confidence')}", outlook.get("summary", ""), ""]
    pulse = payload.get("pulse") or []
    if pulse:
        lines.append("Market pulse:")
        for p in pulse[:8]:
            if p.get("ok"):
                lines.append(f"- {p.get('name')}: {_money(p.get('price'), p.get('currency'))} "
                             f"({p.get('change_label')}) · {p.get('session_label')}")
        lines.append("")
    focus = ((sec.get("H") or {}).get("data") or {}).get("items") or []
    if focus:
        lines.append("Key focus:")
        for f in focus[:max_items]:
            lines.append(f"{f.get('rank')}. {f.get('title')} — {f.get('source')}")
        lines.append("")
    flows = (sec.get("G") or {}).get("data") or {}
    if flows.get("status") == "ok":
        lines.append("FII/DII (₹ crore, " + str(flows.get("date")) + "):")
        for r in flows.get("rows") or []:
            lines.append(f"- {r.get('category')}: net {r.get('net_label')}")
        lines.append("")
    lines.append("Sources and timestamps are on every card. " + DISCLAIMER)
    return "\n".join(lines)


def _seen_alerts() -> set[str]:
    data = _read_json(ALERTS_FILE, {"ids": []})
    ids = data.get("ids") if isinstance(data, dict) else []
    return set(ids or [])


def alert_tick(send: bool = False) -> dict:
    """Find new high-importance directional items; never repeat an alert.

    Sending is opt-in through settings.alerts.enabled AND ``send=True``. The
    caller (scheduler/API test) must explicitly ask for delivery.
    """
    cfg = settings()
    payload = build_briefing(False)
    if not payload.get("ok"):
        return {"ok": False, "error": "; ".join(payload.get("errors") or []),
                "sent": 0, "candidates": []}
    acfg = cfg.get("alerts") or {}
    min_imp = str(acfg.get("min_importance") or "high")
    wanted_sent = set(acfg.get("sentiments") or ["negative", "positive"])
    rank = {"low": 1, "medium": 2, "high": 3}
    seen = _seen_alerts()
    candidates = []
    for a in [x for sec in payload.get("sections", {}).values()
              for x in (sec.get("items") or [])]:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        if a.get("id") in seen:
            continue
        if rank.get(str(a.get("importance")), 0) < rank.get(min_imp, 3):
            continue
        if str(a.get("sentiment")) not in wanted_sent:
            continue
        candidates.append(a)
    candidates.sort(key=lambda a: -int(a.get("score") or 0))
    candidates = candidates[:int(acfg.get("max_per_tick") or 3)]

    sent = 0
    detail = ""
    if send and acfg.get("enabled") and candidates:
        try:
            from orchestrator.autonomy.engine import Notifier
            n = Notifier()
            text = "\n".join(
                [f"NEWS & IMPACT alert — {payload.get('session', {}).get('date_label', '')}"]
                + [f"• [{a.get('sentiment')}] {a.get('title')} ({a.get('source')})"
                   for a in candidates]
                + ["Open the News page for sourced detail. " + DISCLAIMER])
            res = n.notify(text, channel=str(acfg.get("channel") or "telegram"),
                           urgency="normal")
            detail = str(res.get("detail") or "")
            sent = len(candidates) if res.get("ok") else 0
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
    if sent:
        ids = list(seen | {a["id"] for a in candidates})[-1000:]
        _write_json(ALERTS_FILE, {"ids": ids, "updated_at": _iso()})
    return {"ok": True, "sent": sent, "detail": detail,
            "candidates": [{k: a.get(k) for k in
                            ("id", "title", "source", "url", "sentiment",
                             "importance", "published_label")}
                           for a in candidates],
            "enabled": bool(acfg.get("enabled"))}


def install_schedule() -> dict:
    """Register briefing tasks in the EXISTING autonomy TaskStore.

    The scheduler loop is not started here — it remains under the existing
    autonomy controls. Times assume the host clock is Asia/Kolkata (the
    Indian market schedule requested for this page).
    """
    try:
        from orchestrator.autonomy.scheduler import TaskStore
    except Exception as exc:
        return {"ok": False, "error": f"scheduler unavailable: {exc}"}
    store = TaskStore()
    specs = [
        {"name": "news-impact-premarket", "kind": "daily",
         "trigger": {"time": "08:30"}, "tools": ("news_briefing",),
         "steps": [{"tool": "news_briefing",
                    "payload": {"mode": "premarket", "notify": False},
                    "reason": "pre-market NEWS & IMPACT briefing"}],
         "level": 2, "enabled": True,
         "notify": {"channel": "telegram"}},
        {"name": "news-impact-market-refresh", "kind": "interval",
         "trigger": {"interval_s": 1800}, "tools": ("news_briefing",),
         "steps": [{"tool": "news_briefing",
                    "payload": {"mode": "market_refresh", "notify": False},
                    "reason": "market-hours refresh (configurable 30 min)"}],
         "level": 2, "enabled": True},
        {"name": "news-impact-postmarket", "kind": "daily",
         "trigger": {"time": "16:00"}, "tools": ("news_briefing",),
         "steps": [{"tool": "news_briefing",
                    "payload": {"mode": "postmarket", "notify": False},
                    "reason": "post-market summary"}],
         "level": 2, "enabled": True, "notify": {"channel": "telegram"}},
        {"name": "news-impact-alerts", "kind": "interval",
         "trigger": {"interval_s": 900}, "tools": ("news_briefing",),
         "steps": [{"tool": "news_briefing",
                    "payload": {"mode": "alerts", "notify": True},
                    "reason": "high-importance breaking-news alerts"}],
         "level": 2, "enabled": False,
         "notify": {"channel": "telegram"}},
    ]
    added, existing, errors = [], [], []
    for spec in specs:
        current = store.get(spec["name"])
        if current:
            existing.append(spec["name"])
            continue
        res = store.add(spec)
        if res.get("ok"):
            added.append(spec["name"])
        else:
            errors.append(f"{spec['name']}: {res.get('error')}")
    return {"ok": not errors, "added": added, "existing": existing,
            "errors": errors,
            "note": ("Registered in the existing TaskStore. The existing "
                     "scheduler must be running for them to fire; times use "
                     "the host clock (Asia/Kolkata on this machine).")}


def scheduled_run(mode: str = "", notify: bool = False) -> dict:
    """Handler for the autonomy `news_briefing` tool (registered by the API)."""
    mode = str(mode or "")
    if mode == "alerts":
        return alert_tick(send=bool(notify))
    if mode in ("premarket", "market_refresh", "postmarket"):
        payload = build_briefing(refresh=(mode != "premarket"))
        text = briefing_digest(payload)
        if notify and mode in ("premarket", "postmarket"):
            try:
                from orchestrator.autonomy.engine import Notifier
                cfg = settings().get("alerts") or {}
                res = Notifier().notify(
                    text, channel=str(cfg.get("channel") or "telegram"))
                return {"ok": True, "delivered": bool(res.get("ok")),
                        "detail": res.get("detail"), "text": text[:1500]}
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": bool(payload.get("ok")), "text": text[:1500],
                "generated_at": payload.get("generated_at"),
                "sections_ok": payload.get("sections_ok"),
                "sections_total": payload.get("sections_total")}
    return {"ok": False, "error": f"unknown briefing mode {mode!r}"}
