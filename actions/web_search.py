#web_search.py
import json
import sys
import threading
import time
from pathlib import Path

from core.gemini_retry import with_gemini_retry, in_daily_quota_cooldown
import requests
from memory.config_manager import get_newsapi_key

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


# ── Quota breaker (Mark-LIV) ─────────────────────────────────────────────────
# core.gemini already cools a 429ing rung for 5 minutes, but grounded search
# additionally loses the whole day's free-tier search quota at once. Once that
# happens the fastest useful answer is DDG — so when a quota rejection gets
# through, skip Gemini grounding entirely for a while and say it once.
_QUOTA_COOLDOWN_SEC  = 900          # 15 minutes
_quota_blocked_until = 0.0
_quota_lock          = threading.Lock()


class _QuotaCooldown(RuntimeError):
    """Raised instead of calling Gemini while the quota breaker is open.

    The message deliberately contains none of the marker words that
    core.gemini_retry.is_transient_error classifies as transient ("quota",
    "timeout", "429", …) — a retry inside the backoff window would fail
    identically, so this must fail through to DDG on attempt 1.
    """


def _gemini_available() -> bool:
    with _quota_lock:
        return time.monotonic() >= _quota_blocked_until


def _note_gemini_error(exc: Exception) -> None:
    """Trip the breaker when the error is a quota / rate-limit rejection."""
    global _quota_blocked_until
    msg = str(exc)
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        with _quota_lock:
            already = time.monotonic() < _quota_blocked_until
            _quota_blocked_until = time.monotonic() + _QUOTA_COOLDOWN_SEC
        if not already:
            print(
                "[WebSearch] Gemini grounding quota exhausted — skipping it for "
                f"{_QUOTA_COOLDOWN_SEC // 60} min and serving results from DDG."
            )


@with_gemini_retry(label="WebSearch")
def _gemini_search(query: str) -> str:
    # Daily cap gone (seen within the last 10 min): every retry inside the
    # decorator would fail identically — skip straight to the caller's DDG
    # fallback instead of stalling each search ~14 s. The message deliberately
    # avoids words like "quota"/"timeout": those are backoff triggers in
    # core/gemini_retry.is_transient_error, and this skip must fail through on
    # attempt 1, not be retried.
    if in_daily_quota_cooldown():
        raise RuntimeError("Gemini skipped: daily cap in effect (cooldown)")
    if not _gemini_available():
        raise _QuotaCooldown("Gemini grounding skipped: rate-limit breaker open")

    # Mark-LIV ladder: grounded search stays REST-only (a Live turn cannot
    # carry grounding metadata), but walks pinned models → -latest aliases
    # with a real deadline and 429 cooldowns, instead of one hardcoded alias
    # with no timeout.
    from core import gemini

    try:
        response = gemini.call(query, tier=gemini.SEARCH,
                               config={"tools": [{"google_search": {}}]},
                               timeout_ms=30_000)
        if response is None:
            raise RuntimeError("every Gemini model on the ladder failed")
    except Exception as e:
        _note_gemini_error(e)
        raise

    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text


# Short-TTL cache for DDG queries. A repeated query inside the window must not
# re-hit the search engines: DuckDuckGo ratelimits by repetition (the log shows
# news.js 403s on the same "top world news today" query asked back-to-back),
# and JARVIS re-asks the same question whenever a turn is retried or the user
# repeats themselves. TTL is short on purpose — news must stay news.
_DDG_CACHE_MAX = 32
_DDG_CACHE: dict[str, tuple[float, object]] = {}
_DDG_CACHE_TTL_S = 90.0


def _ddg_cached(key: str, fn):
    """Return cached DDG results for `key`, or run fn() and cache the result.
    Exceptions propagate — the callers' fallback chains handle them."""
    now = time.monotonic()
    hit = _DDG_CACHE.get(key)
    if hit and now - hit[0] < _DDG_CACHE_TTL_S:
        return hit[1]
    result = fn()
    if len(_DDG_CACHE) >= _DDG_CACHE_MAX:      # keep insertions O(1); 32 entries of snippet text is small
        _DDG_CACHE.pop(next(iter(_DDG_CACHE)))
    _DDG_CACHE[key] = (now, result)
    return result


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    results = _ddg_cached(
        f"text:{max_results}:{query}",
        lambda: _ddg_text_raw(query, max_results),
    )
    return results


def _ddg_text_raw(query: str, max_results: int) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append({
                "title":   r.get("title",  ""),
                "snippet": r.get("body",   ""),
                "url":     r.get("href",   ""),
            })
    return results


def _ddg_news(query: str, max_results: int = 8) -> list[dict]:
    """News search — returns actual articles, not website homepages.

    ddgs ≥9 fans each news query out to bing, duckduckgo and yahoo and only
    fails if every engine fails, so an upstream 403 on one engine no longer
    sinks the call (the old duckduckgo_search package hit DDG's news.js alone
    — that is the 403 in the old logs). Repeated queries within the TTL are
    served from cache, which is what actually keeps the periodic ratelimit
    403s away. If every news engine is ratelimited, a text search (different
    engine pool) still answers — its results just lack a 'source' field."""
    def _news_with_fallback():
        try:
            return _ddg_news_raw(query, max_results)
        except Exception as e:
            print(f"[WebSearch] ⚠️ DDG news() failed ({e}) — falling back to text search")
            return _ddg_search(query, max_results=max_results)

    return _ddg_cached(f"news:{max_results}:{query}", _news_with_fallback)


def _ddg_news_raw(query: str, max_results: int) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.news(query, max_results=max_results):
            results.append({
                "title":   r.get("title",  ""),
                "snippet": r.get("body",   ""),
                "url":     r.get("url",    ""),
                "source":  r.get("source", ""),
            })
    return results


def _format_ddg(query: str, results: list[dict]) -> str:
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):   lines.append(f"{i}. {r['title']}")
        if r.get("snippet"): lines.append(f"   {r['snippet']}")
        if r.get("url"):     lines.append(f"   Source: {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_news(query: str, results: list[dict]) -> str:
    if not results:
        return f"No news found for: {query}"

    lines = [f"Latest news: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        if not title:
            continue
        src = f"  [{r['source']}]" if r.get("source") else ""
        lines.append(f"{i}. {title}{src}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:140]}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _newsapi_top(query: str, max_results: int = 8) -> str:
    """NewsAPI.org backend — live headlines via the user's own key
    (set it in the dashboard ⚙ Settings). With a query it searches everything;
    without one it returns top headlines — the morning-briefing case.

    Raises on any failure; the caller falls through to Gemini/DDG.
    Deliberately NO retry decorator: this is the first-choice backend, and a
    fast fail-through (bad key, daily quota gone) beats a slow retry."""
    key = get_newsapi_key()
    if not key:
        raise RuntimeError("NewsAPI key not configured")

    if query.strip():
        url, params = "https://newsapi.org/v2/everything", {
            "q": query.strip(), "language": "en",
            "sortBy": "publishedAt", "pageSize": max_results,
        }
    else:
        url, params = "https://newsapi.org/v2/top-headlines", {
            "country": "us", "pageSize": max_results,
        }
    resp = requests.get(url, params={**params, "apiKey": key}, timeout=8)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or body.get("status") != "ok":
        msg = (body.get("message") or resp.text or "").strip()[:120]
        raise RuntimeError(f"NewsAPI {resp.status_code}: {msg}")

    articles = [
        {
            "title":   a.get("title") or "",
            "snippet": (a.get("description") or "")[:140],
            "url":     a.get("url") or "",
            "source":  (a.get("source") or {}).get("name", ""),
        }
        for a in body.get("articles", [])
    ]
    articles = [a for a in articles if a["title"]]
    return _format_news(query.strip() or "top stories", articles)


# ── Briefing helper ────────────────────────────────────────────────────────────

@with_gemini_retry(label="WebSearch")
def _gemini_headlines(n: int = 5) -> tuple[list[str], str]:
    """
    Fetches current headlines via Gemini grounded search.
    Optimised for speed: minimal prompt + strict token cap.
    Returns (headline_list, raw_text_for_display).
    """
    import re

    # Mark-LIV ladder: same grounding config, now with fallbacks + a deadline.
    from core import gemini

    response = gemini.call(
        f"Current world news: {n} headlines. Numbered list, titles only.",
        tier=gemini.SEARCH,
        config={"tools": [{"google_search": {}}]},
        timeout_ms=30_000,
    )
    if response is None:
        return [], ""

    raw = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            raw += part.text

    headlines = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Only accept lines that begin with a number — skips preamble/closing sentences
        if not re.match(r'^[\d]+[.\)\-]', line):
            continue
        clean = re.sub(r'^[\d]+[.\)\-]\s*', '', line)
        clean = re.sub(r'^\*+\s*',          '', clean).strip()
        if clean and len(clean) > 10:
            headlines.append(clean)

    return headlines[:n], raw.strip()


# ── Modes ──────────────────────────────────────────────────────────────────────

def _search(query: str) -> str:
    """Default search — Gemini grounded, DDG fallback."""
    try:
        return _gemini_search(query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Gemini failed ({e}) — trying DDG...")
        results = _ddg_search(query)
        return _format_ddg(query, results)


def _news(query: str) -> str:
    """
    News with a three-backend chain: NewsAPI (user key, dashboard ⚙) →
    Gemini grounded search → DDG news. NewsAPI gets a short grace window to
    answer first (highest-quality backend); the Gemini/DDG race is unchanged
    behind it.
    """
    import threading

    # ── Backend 0: NewsAPI — short grace, fast fail-through ─────────────
    napi_box = [None]           # late result may still land (see the end)
    napi_thread = None
    if get_newsapi_key():
        napi_evt = threading.Event()

        def _try_newsapi():
            try:
                napi_box[0] = _newsapi_top(query)
            except Exception as e:
                print(f"[WebSearch] ⚠️ NewsAPI failed ({e})")
            finally:
                napi_evt.set()

        napi_thread = threading.Thread(target=_try_newsapi, daemon=True)
        napi_thread.start()
        if napi_evt.wait(timeout=5.0) and napi_box[0]:
            return napi_box[0]
        # Miss (slow or failed) → fall through; the Gemini/DDG race below
        # takes over. A late NewsAPI result is still picked up at the end.

    gemini_query = f"latest news today: {query}" if query else "top world news today"
    ddg_query    = query if query else "world news today"

    result_box  = [None]   # first valid result lands here
    lock        = threading.Lock()
    done_evt    = threading.Event()
    failures    = [0]

    def _store(r: str) -> None:
        if r and len(r) > 60:
            with lock:
                if result_box[0] is None:
                    result_box[0] = r
            done_evt.set()
        else:
            with lock:
                failures[0] += 1
                if failures[0] >= 2:   # both failed — unblock caller
                    done_evt.set()

    def _try_gemini():
        try:
            _store(_gemini_search(gemini_query))
        except Exception as e:
            print(f"[WebSearch] ⚠️ Gemini news failed ({e})")
            _store("")

    def _try_ddg():
        try:
            results = _ddg_news(ddg_query, max_results=8)
            _store(_format_news(ddg_query, results))
        except Exception as e:
            print(f"[WebSearch] ⚠️ DDG news failed ({e})")
            _store("")

    gemini_thread = threading.Thread(target=_try_gemini, daemon=True)
    ddg_thread    = threading.Thread(target=_try_ddg,    daemon=True)
    gemini_thread.start()
    ddg_thread.start()

    # Fast path: whoever delivers first (usually DDG) ends the wait.
    done_evt.wait(timeout=10.0)
    if result_box[0] is not None:
        return result_box[0]

    # Slow path: DDG came up empty and Gemini is mid-retry (exponential
    # backoff inside _gemini_search) — give it until its last attempt lands
    # instead of abandoning a recovery that is seconds away.
    deadline = time.monotonic() + 45.0   # ≈ 3 attempts incl. backoff + call time
    while result_box[0] is None:
        if done_evt.is_set():                                  # both failed
            break
        if not (gemini_thread.is_alive() or ddg_thread.is_alive()):
            break
        if time.monotonic() >= deadline:
            break
        done_evt.wait(timeout=0.5)
    # Nothing arrived from the race and a NewsAPI call that missed its 5s
    # grace may still be in flight — it is the highest-quality backend, so
    # give it one bounded moment to land instead of answering "no news"
    # while a better answer is seconds away. (Bounded: NewsAPI has its own
    # 8s HTTP timeout and no retries.)
    if result_box[0] is None and napi_thread is not None and napi_thread.is_alive():
        napi_thread.join(timeout=6.0)
    return napi_box[0] or result_box[0] or f"No news found for: {query}"


def _research(query: str) -> str:
    """
    Deep dive — asks Gemini for a comprehensive answer with context.
    Falls back to a wider DDG fetch.
    """
    research_query = (
        f"Comprehensive, detailed explanation of: {query}. "
        "Include background context, key facts, current state, and important nuances."
    )
    try:
        return _gemini_search(research_query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Research Gemini failed ({e}) — DDG fallback...")
        results = _ddg_search(query, max_results=10)
        return _format_ddg(query, results)


def _price(query: str) -> str:
    """Product price lookup — searches for current market prices."""
    price_query = f"current price of {query} — how much does it cost today"
    try:
        return _gemini_search(price_query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Price Gemini failed ({e}) — DDG fallback...")
        results = _ddg_search(f"{query} price buy", max_results=6)
        return _format_ddg(query, results)


def _compare(items: list[str], aspect: str) -> str:
    query = (
        f"Compare {', '.join(items)} in terms of {aspect}. "
        "Give specific facts and data."
    )
    try:
        return _gemini_search(query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Gemini compare failed: {e} — falling back to DDG")

    all_results: dict[str, list] = {}
    for item in items:
        try:
            all_results[item] = _ddg_search(f"{item} {aspect}", max_results=3)
        except Exception:
            all_results[item] = []

    lines = [f"Comparison — {aspect.upper()}", "─" * 40]
    for item in items:
        lines.append(f"\n▸ {item}")
        for r in all_results.get(item, [])[:2]:
            if r.get("snippet"):
                lines.append(f"  • {r['snippet']}")
            if r.get("url"):
                lines.append(f"    {r['url']}")
    return "\n".join(lines)


# ── Public entry point ─────────────────────────────────────────────────────────

def web_search(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    query  = params.get("query", "").strip()
    mode   = params.get("mode",  "search").lower().strip()
    items  = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"

    if not query and not items:
        return "Please provide a search query."

    if items and mode not in ("compare",):
        mode = "compare"

    if player:
        player.write_log(f"[Search:{mode}] {query or ', '.join(items)}")

    print(f"[WebSearch] 🔍 mode={mode!r}  query={query!r}")

    try:
        if mode == "compare" and items:
            return _compare(items, aspect)
        if mode == "news":
            return _news(query)
        if mode == "research":
            return _research(query)
        if mode == "price":
            return _price(query)
        return _search(query)

    except Exception as e:
        print(f"[WebSearch] ❌ All backends failed: {e}")
        return f"Search failed: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "web_search",
    "description": "Searches the web. Use for ANY question about current facts, events, prices, or topics — always prefer this over guessing. Modes: 'search' (default), 'news' (latest headlines on a topic), 'research' (deep comprehensive answer), 'price' (product cost lookup), 'compare' (side-by-side comparison of items).",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Search query or topic"
            },
            "mode": {
                "type": "STRING",
                "description": "search | news | research | price | compare"
            },
            "items": {
                "type": "ARRAY",
                "items": {
                    "type": "STRING"
                },
                "description": "Items to compare (compare mode)"
            },
            "aspect": {
                "type": "STRING",
                "description": "Comparison aspect: price | specs | reviews | features"
            }
        },
        "required": [
            "query"
        ]
    },
    "handler": web_search,
}
