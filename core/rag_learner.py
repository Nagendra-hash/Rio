"""
core/rag_learner.py — the self-directed web knowledge harvester (RAG growth).

WHAT IT DOES
    Once a day (configurable), JARVIS goes to the web on its own, researches a
    rotating set of topics, distils what it found into a cited dossier, and
    stores that dossier in the RAG library (collection "web_knowledge"). The
    next day it merges the new material into the same dossier, so the knowledge
    base compounds instead of resetting.

WHY IT EXISTS
    A document library only knows what the user uploaded. This closes the loop:
    the assistant notices questions the library could not answer, researches
    them overnight, and can answer them the next time.

THE LEARNING LOOP (honest by construction)
    1. CURIOUS    pick topics: least-recently-studied first, weighted by the
                  gaps recorded when a RAG search returned nothing.
    2. SEARCH     run a few queries through the permitted-source toolbox
                  (orchestrator/agents/research_tools: URL policy + secret
                  masking + timeouts). Seen URLs are skipped — no re-reading
                  the same page every day.
    3. FETCH      optionally deep-read the strongest new pages.
    4. DISTIL     one bounded LLM pass merges OLD dossier + NEW sources into a
                  cited Markdown note. The prompt forbids outside knowledge.
                  If the model ladder is down, an EXTRACTIVE note is stored
                  instead — snippets + links, nothing invented.
    5. REMEMBER   ingest (first time) or update (content replacement) through
                  core.rag_service, as caller "rag_learner" (write-gated by
                  config/rag_callers.json like every other agent).
    6. EVOLVE     a topic that produced knowledge fades slightly (diminishing
                  returns); a topic that failed or found nothing fades faster;
                  a topic born from an unanswered user question starts with a
                  high weight and carries that exact question as a query.

STATE / FILES (all under memory/rag_learning/)
    state.json     topics, weights, seen URLs, doc ids, run counters
    notes/<slug>.md  the working dossier per topic (source of the next merge)
    gaps.jsonl     unanswered RAG queries (the curiosity signal)
    log.jsonl      one line per cycle and per topic outcome

CONTROL
    config/rag_learning.json — enabled, hour, interval_hours, topics_per_run,
    queries_per_topic, results_per_query, deep_fetch, collection, caller,
    interests, blocked_domains, min_note_chars, model_chars.

    `python scripts/rag_learn.py run|status|topics|add|remove|gaps`

Everything is injectable for offline tests (search_fn / fetch_fn /
summarize_fn / store_fn). No network is touched at import time.
"""
from __future__ import annotations

import collections
import copy
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from memory.config_manager import BASE_DIR

CONFIG_FILE = BASE_DIR / "config" / "rag_learning.json"
STATE_DIR = BASE_DIR / "memory" / "rag_learning"
STATE_FILE = STATE_DIR / "state.json"

DEFAULT_COLLECTION = "web_knowledge"
DEFAULT_CALLER = "rag_learner"

_MAX_TOPICS = 120
_MAX_SEEN_URLS = 3000
_MAX_GAP_QUERIES_PER_TOPIC = 4
_NOTE_MAX_CHARS = 14000
_MODEL_MATERIAL_CHARS = 26000
_MIN_TITLE_CHARS = 3
_SEARCH_RETRY_DELAY_S = 1.5

_QUERY_TEMPLATES = (
    "{topic} overview explained",
    "{topic} latest developments {year}",
    "{topic} key facts and background",
)

_SEED_INTERESTS = (
    "artificial intelligence",
    "finance",
    "crypto market",
    "share market",
    "economics",
    "technology",
    "business",
    "health",
    "politics",
    "world news",
)

_DEFAULT_CONFIG: dict = {
    "enabled": True,
    "hour": 3,                    # local hour: run once the day has started
    "interval_hours": 0,          # optional minimum gap (0 = once per day is enough)
    "topics_per_run": 3,
    "queries_per_topic": 2,
    "results_per_query": 4,
    "deep_fetch": 2,
    "min_note_chars": 320,
    "model_chars": _MODEL_MATERIAL_CHARS,
    "collection": DEFAULT_COLLECTION,
    "caller": DEFAULT_CALLER,
    "access": "private",
    "interests": list(_SEED_INTERESTS),
    "blocked_domains": [],
}

_run_lock = threading.Lock()
_state_lock = threading.RLock()

# Unanswered-question recording is best-effort and must never flood the
# curiosity store when the strategy pipeline rewrites a query a dozen times.
_GAP_TIMES: collections.deque[float] = collections.deque(maxlen=500)
_GAP_MAX_PER_HOUR = 40


# ── small helpers ────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _slug(topic: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    if not s:
        s = "topic-" + hashlib.sha1(topic.encode("utf-8")).hexdigest()[:8]
    return s[:80]


def _clean_topic(raw: str) -> str:
    t = re.sub(r"\s+", " ", str(raw or "")).strip(" \t\r\n.,;:!?\"'")
    return t[:120]


def _notes_dir() -> Path:
    # Derived from STATE_FILE so tests can relocate all state with one patch.
    return Path(STATE_FILE).parent / "notes"


def _gaps_file() -> Path:
    return Path(STATE_FILE).parent / "gaps.jsonl"


def _log_file() -> Path:
    return Path(STATE_FILE).parent / "log.jsonl"


def _read_json(path: Path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, row: dict) -> None:
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Effective config: defaults, then the user's file (unknown keys kept)."""
    cfg = dict(_DEFAULT_CONFIG)
    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            cfg.update(saved)
    except Exception:
        pass
    # Guard the knobs that cost network/quota.
    for key, lo, hi in (("hour", 0, 23), ("interval_hours", 0, 24 * 30),
                        ("topics_per_run", 1, 10), ("queries_per_topic", 1, 6),
                        ("results_per_query", 1, 10), ("deep_fetch", 0, 6),
                        ("min_note_chars", 80, 5000),
                        ("model_chars", 2000, 60000)):
        try:
            cfg[key] = max(lo, min(int(cfg.get(key, _DEFAULT_CONFIG[key])), hi))
        except (TypeError, ValueError):
            cfg[key] = _DEFAULT_CONFIG[key]
    if cfg.get("collection") not in _valid_collections():
        cfg["collection"] = DEFAULT_COLLECTION
    if not str(cfg.get("caller") or "").strip():
        cfg["caller"] = DEFAULT_CALLER
    if cfg.get("access") not in ("private", "shared"):
        cfg["access"] = "private"
    if not isinstance(cfg.get("interests"), list):
        cfg["interests"] = list(_SEED_INTERESTS)
    if not isinstance(cfg.get("blocked_domains"), list):
        cfg["blocked_domains"] = []
    return cfg


def _valid_collections() -> set[str]:
    try:
        from core import rag
        return set(rag.COLLECTIONS)
    except Exception:
        return {DEFAULT_COLLECTION, "research", "personal_knowledge"}


def save_config(patch: dict) -> dict:
    """Merge a patch into config/rag_learning.json and return the result."""
    current = {}
    try:
        current = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
    except Exception:
        current = {}
    current.update(patch or {})
    _write_json(CONFIG_FILE, current)
    return load_config()


# ── state ────────────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
        "version": 1,
        "created": _now_iso(),
        "topics": {},
        "seen_urls": {},
        "last_run": 0.0,
        "last_run_date": "",
        "runs": 0,
        "ingested": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "last_result": {},
    }


def load_state() -> dict:
    with _state_lock:
        state = _read_json(STATE_FILE, {})
        if not isinstance(state, dict) or "topics" not in state:
            return _default_state()
        base = _default_state()
        base.update(state)
        if not isinstance(base.get("topics"), dict):
            base["topics"] = {}
        if not isinstance(base.get("seen_urls"), dict):
            base["seen_urls"] = {}
        return base


def save_state(state: dict) -> None:
    with _state_lock:
        _prune_state(state)
        _write_json(STATE_FILE, state)


def _prune_state(state: dict) -> None:
    topics = state.get("topics") or {}
    if len(topics) > _MAX_TOPICS:
        def _keep_key(item):
            _name, e = item
            return (int(e.get("last_run") or 0),
                    float(e.get("weight") or 0),
                    bool(e.get("doc_id")))
        for name, _e in sorted(topics.items(), key=_keep_key)[:len(topics) - _MAX_TOPICS]:
            topics.pop(name, None)
    seen = state.get("seen_urls") or {}
    if len(seen) > _MAX_SEEN_URLS:
        newest = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)
        state["seen_urls"] = dict(newest[:_MAX_SEEN_URLS])


def _ensure_seeded(state: dict, cfg: dict) -> bool:
    """First run: grow initial interests from the user's own monitor topics."""
    if state.get("topics"):
        return False
    wanted: list[str] = []
    # The monitors the user already follows are the best statement of intent.
    try:
        from memory.memory_manager import load_memory
        mem = load_memory() or {}
        # The list lives under preferences in the shipped schema; older builds
        # used a top-level monitors section — accept both.
        for section in ("monitors", "preferences"):
            block = mem.get(section) or {}
            raw = block.get("monitored_topics_list", "") if isinstance(block, dict) else ""
            if isinstance(raw, str):
                wanted.extend(x.strip() for x in raw.split(",") if x.strip())
    except Exception:
        pass
    wanted.extend(str(t).strip() for t in (cfg.get("interests") or []))
    seen = set()
    for topic in wanted:
        topic = _clean_topic(topic)
        key = topic.lower()
        if len(topic) < _MIN_TITLE_CHARS or key in seen:
            continue
        seen.add(key)
        state["topics"][topic] = _new_topic(origin="seed")
        if len(state["topics"]) >= 24:
            break
    return bool(state["topics"])


def _new_topic(origin: str = "seed", weight: float = 1.0) -> dict:
    return {
        "weight": round(float(weight), 3),
        "origin": origin,
        "attempts": 0,
        "learned": 0,
        "failures": 0,
        "last_run": 0.0,
        "last_status": "",
        "queries": [],
        "doc_id": "",
        "note_hash": "",
        "pending": False,
        "created": _now_iso(),
    }


# ── curiosity: unanswered questions become tomorrow's research ──────────────

_GAP_STOPWORDS = {
    "what", "who", "when", "where", "why", "how", "is", "are", "was", "were",
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "about",
    "tell", "me", "please", "do", "does", "did", "can", "could", "would",
    "should", "explain", "define", "meaning", "kya", "hai", "ka", "ki", "ke",
}


def _topic_from_query(query: str) -> str:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-+.]*", str(query or ""))
    kept = [w for w in words if w.lower() not in _GAP_STOPWORDS]
    if not kept:
        return _clean_topic(query)[:80]
    return _clean_topic(" ".join(kept[:8]))


def note_gap(query: str, caller: str = "", collection: str | None = None,
             force: bool = False) -> None:
    """Record a question the library could not answer.

    Called (best-effort) by core.rag_service.search when it returns nothing.
    The topic is weighted up and the EXACT question is kept as a search query,
    so tomorrow's cycle researches what the user actually asked. Never raises.

    `force` lets tests exercise the path; without it a pytest run cannot
    pollute the real curiosity state with fixture queries.
    """
    try:
        if not force and os.environ.get("PYTEST_CURRENT_TEST"):
            return
        q = str(query or "").strip()
        if len(q) < 4 or caller in (DEFAULT_CALLER, "rag_learner"):
            return
        now_ts = time.time()
        while _GAP_TIMES and now_ts - _GAP_TIMES[0] > 3600:
            _GAP_TIMES.popleft()
        if len(_GAP_TIMES) >= _GAP_MAX_PER_HOUR:
            return
        _GAP_TIMES.append(now_ts)
        topic = _topic_from_query(q)
        if len(topic) < _MIN_TITLE_CHARS:
            return
        _append_jsonl(_gaps_file(), {"ts": _now_iso(), "query": q,
                                    "caller": str(caller or ""),
                                    "collection": collection or ""})
        with _state_lock:
            state = load_state()
            _ensure_seeded(state, load_config())
            entry = state["topics"].get(topic)
            if entry is None:
                # Case-insensitive reuse: "AI" and "ai" must not split a topic.
                low = topic.lower()
                for name in state["topics"]:
                    if name.lower() == low:
                        topic, entry = name, state["topics"][name]
                        break
            if entry is None:
                entry = _new_topic(origin="gap", weight=2.0)
                state["topics"][topic] = entry
            else:
                entry["weight"] = min(8.0, float(entry.get("weight") or 1.0) + 1.0)
            qs = entry.setdefault("queries", [])
            if q not in qs:
                qs.append(q)
                del qs[:-_MAX_GAP_QUERIES_PER_TOPIC]
            save_state(state)
    except Exception:
        pass


# ── topic management ─────────────────────────────────────────────────────────

def add_topic(topic: str, weight: float = 2.0, origin: str = "user") -> dict:
    name = _clean_topic(topic)
    if len(name) < _MIN_TITLE_CHARS:
        return {"ok": False, "error": "topic name too short"}
    with _state_lock:
        state = load_state()
        entry = state["topics"].get(name)
        if entry is None:
            for existing in state["topics"]:
                if existing.lower() == name.lower():
                    name, entry = existing, state["topics"][existing]
                    break
        if entry is None:
            state["topics"][name] = _new_topic(origin=origin, weight=weight)
        else:
            entry["weight"] = min(8.0, float(entry.get("weight") or 1.0) + float(weight))
        save_state(state)
    return {"ok": True, "topic": name}


def remove_topic(topic: str) -> dict:
    name = _clean_topic(topic)
    with _state_lock:
        state = load_state()
        entry = None
        for existing in list(state["topics"]):
            if existing.lower() == name.lower():
                entry = state["topics"].pop(existing)
                name = existing
                break
        save_state(state)
    if entry is None:
        return {"ok": False, "error": f"unknown topic: {topic}"}
    return {"ok": True, "topic": name}


def list_topics(limit: int = 50) -> list[dict]:
    state = load_state()
    rows = []
    for name, e in state.get("topics", {}).items():
        rows.append({
            "topic": name,
            "weight": round(float(e.get("weight") or 0), 2),
            "origin": e.get("origin", "seed"),
            "attempts": int(e.get("attempts") or 0),
            "learned": int(e.get("learned") or 0),
            "failures": int(e.get("failures") or 0),
            "last_run": e.get("last_run") or 0,
            "last_status": e.get("last_status", ""),
            "doc_id": e.get("doc_id", ""),
            "gap_queries": list(e.get("queries") or []),
        })
    rows.sort(key=lambda r: (r["last_run"], -r["weight"]))
    return rows[:max(1, int(limit))]


# ── scheduling ───────────────────────────────────────────────────────────────

def due(now: datetime | None = None) -> bool:
    """Should a cycle run now? Once per calendar day at/after `hour`, plus an
    optional minimum spacing (`interval_hours`)."""
    cfg = load_config()
    if not cfg.get("enabled"):
        return False
    now = now or datetime.now()
    state = load_state()
    last_ts = float(state.get("last_run") or 0)
    interval = float(cfg.get("interval_hours") or 0) * 3600
    if interval and (time.time() - last_ts) < interval:
        return False
    if state.get("last_run_date") == _today(now):
        return False
    return now.hour >= int(cfg.get("hour") or 0)


def tick(force: bool = False, *, now: datetime | None = None,
         search_fn=None, fetch_fn=None, summarize_fn=None,
         store_fn=None) -> dict:
    """One scheduler tick: due-check, then run. Safe to call every 30 minutes."""
    if not force and not due(now):
        return {"ok": False, "skipped": "not due"}
    return run_cycle(force=force, now=now, search_fn=search_fn,
                     fetch_fn=fetch_fn, summarize_fn=summarize_fn,
                     store_fn=store_fn)


# ── the cycle ────────────────────────────────────────────────────────────────

def run_cycle(force: bool = False, *, topics: list[str] | None = None,
              now: datetime | None = None, search_fn=None, fetch_fn=None,
              summarize_fn=None, store_fn=None) -> dict:
    """Research a few topics, merge findings into their RAG dossiers.

    Never raises. Per-topic failures are recorded and the cycle continues.
    Returns a summary suitable for a voice answer or the dashboard.
    """
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "a learning cycle is already running"}
    t0 = time.time()
    cfg = load_config()
    try:
        state = load_state()
        baseline = copy.deepcopy(state)
        seeded = _ensure_seeded(state, cfg)
        if seeded:
            save_state(state)

        picks = [ _clean_topic(t) for t in (topics or []) if _clean_topic(t) ]
        if not picks:
            picks = _select_topics(state, cfg)

        results = []
        for topic in picks:
            try:
                results.append(_learn_topic(state, cfg, topic, t0,
                                            search_fn=search_fn,
                                            fetch_fn=fetch_fn,
                                            summarize_fn=summarize_fn,
                                            store_fn=store_fn))
            except Exception as exc:               # one bad topic never stops
                entry = state["topics"].setdefault(topic, _new_topic())
                entry["failures"] = int(entry.get("failures") or 0) + 1
                entry["attempts"] = int(entry.get("attempts") or 0) + 1
                entry["last_run"] = t0
                entry["last_status"] = f"error: {type(exc).__name__}"
                state["failed"] = int(state.get("failed") or 0) + 1
                results.append({"topic": topic, "status": "failed",
                                "error": str(exc)[:200]})

        learned = sum(1 for r in results if r.get("status") in ("learned", "updated"))
        no_sources = sum(1 for r in results if r.get("status") == "no_sources")
        failed = sum(1 for r in results if r.get("status") == "failed")
        skipped = sum(1 for r in results if r.get("status") == "unchanged")

        state["runs"] = int(state.get("runs") or 0) + 1
        state["last_run"] = t0
        state["last_run_date"] = _today(now)
        state["last_result"] = {
            "at": _now_iso(), "topics": picks,
            "learned": learned, "no_sources": no_sources,
            "failed": failed, "skipped": skipped,
            "seconds": round(time.time() - t0, 1),
        }
        # A user question may have recorded a gap while we were searching the
        # web; merge it in so our save does not erase fresh curiosity.
        try:
            _merge_concurrent(state, load_state(), baseline)
        except Exception:
            pass
        save_state(state)
        summary = {"ok": True, "ran": len(picks), "learned": learned,
                   "no_sources": no_sources, "failed": failed,
                   "skipped": skipped, "topics": picks,
                   "seconds": round(time.time() - t0, 1),
                   "results": results,
                   "collection": cfg.get("collection"),
                   "stats": _stats_from(state)}
        _log({"event": "cycle", **{k: summary[k] for k in (
            "ran", "learned", "no_sources", "failed", "skipped", "seconds")},
            "topics": picks})
        print(f"[RAGLearner] cycle: {learned} learned, {no_sources} no-source, "
              f"{failed} failed, {skipped} unchanged in "
              f"{summary['seconds']}s")
        return summary
    except Exception as exc:
        _log({"event": "cycle_failed", "error": str(exc)[:300]})
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        _run_lock.release()


def _merge_concurrent(state: dict, disk: dict, baseline: dict) -> None:
    """Fold changes that landed on disk DURING this cycle back into our
    in-memory state, so the final save cannot erase a user question recorded
    while we were searching the web.

    Only the DELTA between disk and the baseline snapshot taken at cycle start
    is applied — otherwise merging the pre-cycle file would resurrect the gap
    queries this cycle just satisfied.
    """
    b_topics = baseline.get("topics") or {}
    for name, d_entry in (disk.get("topics") or {}).items():
        b_entry = b_topics.get(name)
        if b_entry is None:                 # brand-new topic (fresh gap)
            state["topics"][name] = d_entry
            continue
        s_entry = state["topics"].get(name)
        if s_entry is None:
            continue
        if float(d_entry.get("weight") or 0) != float(b_entry.get("weight") or 0):
            s_entry["weight"] = max(float(s_entry.get("weight") or 0),
                                    float(d_entry.get("weight") or 0))
        new_queries = [q for q in d_entry.get("queries") or []
                       if q not in (b_entry.get("queries") or [])]
        if new_queries:
            qs = list(s_entry.get("queries") or [])
            for q in new_queries:
                if q not in qs:
                    qs.append(q)
            s_entry["queries"] = qs[-_MAX_GAP_QUERIES_PER_TOPIC:]
        for k in ("failures", "learned", "attempts"):
            if int(d_entry.get(k) or 0) != int(b_entry.get(k) or 0):
                s_entry[k] = max(int(s_entry.get(k) or 0),
                                 int(d_entry.get(k) or 0))
    b_seen = baseline.get("seen_urls") or {}
    seen = state.setdefault("seen_urls", {})
    for url, ts in (disk.get("seen_urls") or {}).items():
        if url not in b_seen:
            seen[url] = ts
    for k in ("runs", "ingested", "updated", "skipped", "failed"):
        if int(disk.get(k) or 0) != int(baseline.get(k) or 0):
            state[k] = max(int(state.get(k) or 0), int(disk.get(k) or 0))


def _select_topics(state: dict, cfg: dict) -> list[str]:
    topics = state.get("topics") or {}
    if not topics:
        return []
    n = int(cfg.get("topics_per_run") or 3)
    today = _today()
    rows = []
    for name, e in topics.items():
        # A topic that already produced a dossier today is not picked again
        # (unless it carries an unanswered gap question — then re-check it).
        last = float(e.get("last_run") or 0)
        was_today = last and datetime.fromtimestamp(last).strftime("%Y-%m-%d") == today
        if was_today and not e.get("queries"):
            continue
        rows.append((last, -float(e.get("weight") or 1.0), name))
    rows.sort()
    return [name for _last, _w, name in rows[:max(1, n)]]


def _learn_topic(state: dict, cfg: dict, topic: str, t0: float, *,
                 search_fn=None, fetch_fn=None, summarize_fn=None,
                 store_fn=None) -> dict:
    entry = state["topics"].setdefault(topic, _new_topic())
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    entry["last_run"] = t0

    sources = _gather_sources(state, cfg, topic, entry,
                              search_fn=search_fn, fetch_fn=fetch_fn)
    if not sources:
        entry["last_status"] = "no_sources"
        entry["weight"] = max(0.35, round(float(entry.get("weight") or 1.0) * 0.94, 3))
        state["skipped"] = int(state.get("skipped") or 0) + 1
        _log({"event": "topic", "topic": topic, "status": "no_sources"})
        return {"topic": topic, "status": "no_sources", "sources": 0}

    slug = _slug(topic)
    note_path = _notes_dir() / f"{slug}.md"
    existing = ""
    try:
        if note_path.exists():
            existing = note_path.read_text(encoding="utf-8")[:_NOTE_MAX_CHARS]
    except Exception:
        existing = ""

    material = _format_sources(sources, cfg.get("model_chars"))
    note = ""
    if summarize_fn is None:
        note = _default_summarize(topic, existing, material)
    else:
        try:
            note = summarize_fn(topic, existing, material) or ""
        except Exception as exc:
            _log({"event": "summarize_failed", "topic": topic,
                  "error": str(exc)[:200]})
            note = ""
    if not note.strip():
        note = _extractive_note(topic, sources)
        if not note.strip():
            entry["last_status"] = "no_note"
            entry["weight"] = max(0.35, round(float(entry.get("weight") or 1.0) * 0.94, 3))
            return {"topic": topic, "status": "no_sources", "sources": len(sources)}
    elif len(note.strip()) < int(cfg.get("min_note_chars") or 0):
        # A two-line model answer is not a dossier. Prefer the extractive
        # evidence (snippets + links) when it carries more actual material.
        better = _extractive_note(topic, sources)
        if len(better) > len(note.strip()):
            note = better

    note = note.strip()
    if not note.lstrip().startswith("#"):
        note = f"# {topic}\n\n{note}"
    if len(note) > _NOTE_MAX_CHARS:
        note = note[:_NOTE_MAX_CHARS].rstrip() + "\n\n_[truncated]_"
    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()[:16]

    if digest == entry.get("note_hash") and not entry.get("pending"):
        entry["last_status"] = "unchanged"
        entry["weight"] = max(0.35, round(float(entry.get("weight") or 1.0) * 0.97, 3))
        state["skipped"] = int(state.get("skipped") or 0) + 1
        _log({"event": "topic", "topic": topic, "status": "unchanged"})
        return {"topic": topic, "status": "unchanged", "sources": len(sources)}

    # Write the working dossier BEFORE ingest: a failed ingest can be retried
    # from the local note without re-searching the web.
    try:
        _notes_dir().mkdir(parents=True, exist_ok=True)
        note_path.write_text(note, encoding="utf-8")
    except Exception:
        pass

    title = f"Web knowledge: {topic}"[:120]
    metadata = {
        "learner": True,
        "topic": topic,
        "learned_at": _now_iso(),
        "source_count": len(sources),
        "urls": [s.get("url") for s in sources if s.get("url")][:30],
        "origin": entry.get("origin", "seed"),
    }
    doc_id = str(entry.get("doc_id") or "")

    def _store(existing_id: str) -> dict:
        if store_fn is None:
            return _default_store(note, title, cfg.get("collection"),
                                  metadata, existing_id,
                                  caller=cfg.get("caller"),
                                  access=cfg.get("access"))
        return store_fn(note, title, cfg.get("collection"), metadata,
                        existing_id)

    try:
        result = _store(doc_id)
        if not (result or {}).get("ok") and doc_id:
            err = str((result or {}).get("error") or "").lower()
            if "unknown document" in err or "not found" in err:
                # The dossier was forgotten from the library (or the index
                # was reset). Re-create it instead of failing every night on
                # a dead doc id.
                entry["doc_id"] = ""
                doc_id = ""
                result = _store("")
    except Exception as exc:
        entry["last_status"] = f"store_failed: {type(exc).__name__}"
        entry["pending"] = True
        entry["failures"] = int(entry.get("failures") or 0) + 1
        entry["weight"] = max(0.35, round(float(entry.get("weight") or 1.0) * 0.9, 3))
        state["failed"] = int(state.get("failed") or 0) + 1
        _log({"event": "topic", "topic": topic, "status": "store_failed",
              "error": str(exc)[:200]})
        return {"topic": topic, "status": "failed", "error": str(exc)[:200]}

    if not (result or {}).get("ok"):
        entry["last_status"] = "store_rejected"
        entry["pending"] = True
        entry["failures"] = int(entry.get("failures") or 0) + 1
        entry["weight"] = max(0.35, round(float(entry.get("weight") or 1.0) * 0.9, 3))
        state["failed"] = int(state.get("failed") or 0) + 1
        _log({"event": "topic", "topic": topic, "status": "store_rejected",
              "error": str((result or {}).get("error"))[:200]})
        return {"topic": topic, "status": "failed",
                "error": str((result or {}).get("error"))[:200]}

    was_update = bool(doc_id)
    entry["doc_id"] = str(result.get("doc_id") or doc_id)
    entry["note_hash"] = digest
    entry["pending"] = False
    entry["last_status"] = "updated" if was_update else "learned"
    entry["learned"] = int(entry.get("learned") or 0) + 1
    entry["weight"] = max(0.4, round(float(entry.get("weight") or 1.0) * 0.88, 3))
    entry["queries"] = []                      # this curiosity is satisfied
    entry["last_error"] = ""
    for s in sources:
        url = s.get("url")
        if url:
            state.setdefault("seen_urls", {})[url] = time.time()
    if was_update:
        state["updated"] = int(state.get("updated") or 0) + 1
    else:
        state["ingested"] = int(state.get("ingested") or 0) + 1
    _log({"event": "topic", "topic": topic, "status": entry["last_status"],
          "sources": len(sources), "chars": len(note),
          "doc_id": entry["doc_id"]})
    return {"topic": topic, "status": entry["last_status"],
            "sources": len(sources), "chars": len(note),
            "doc_id": entry["doc_id"]}


# ── sources ──────────────────────────────────────────────────────────────────

def _gather_sources(state: dict, cfg: dict, topic: str, entry: dict, *,
                    search_fn=None, fetch_fn=None) -> list[dict]:
    year = datetime.now().year
    queries: list[str] = []
    for q in list(entry.get("queries") or [])[:2]:
        q = str(q).strip()
        if q and q.lower() not in (x.lower() for x in queries):
            queries.append(q)
    for template in _QUERY_TEMPLATES:
        q = template.format(topic=topic, year=year)
        if q.lower() not in (x.lower() for x in queries):
            queries.append(q)
    queries = queries[:int(cfg.get("queries_per_topic") or 2)]

    blocked = {str(d).strip().lower() for d in (cfg.get("blocked_domains") or [])
               if str(d).strip()}
    seen_now: set[str] = set()
    sources: list[dict] = []
    per_query = int(cfg.get("results_per_query") or 4)

    for q in queries:
        try:
            hits = (search_fn or _default_search)(q, per_query) or []
        except Exception as exc:
            # One short retry: the DDG backend intermittently answers
            # "No results found." under rate limiting, and losing a whole
            # topic for that would be wasteful. A second failure is logged.
            try:
                time.sleep(_SEARCH_RETRY_DELAY_S)
                hits = (search_fn or _default_search)(q, per_query) or []
            except Exception:
                _log({"event": "search_failed", "topic": topic, "query": q,
                      "error": str(exc)[:200]})
                continue
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            url = str(hit.get("url") or "").strip()
            title = str(hit.get("title") or "").strip()[:200]
            snippet = str(hit.get("snippet") or hit.get("body") or "").strip()[:900]
            if not (title or snippet):
                continue
            if url and (url in seen_now or url in (state.get("seen_urls") or {})):
                continue
            if url and blocked and _host_in(url, blocked):
                continue
            if url:
                seen_now.add(url)
            sources.append({"title": title or url or topic, "url": url,
                            "snippet": snippet, "query": q})
            if len(sources) >= 12:
                break
        if len(sources) >= 12:
            break
    if not sources:
        return []

    # Deep-read the strongest few (search order is relevance order).
    want = int(cfg.get("deep_fetch") or 0)
    if want:
        fetch = fetch_fn or _default_fetch
        fetched = 0
        for s in sources:
            if fetched >= want:
                break
            if not s.get("url"):
                continue
            try:
                text = fetch(s["url"]) or ""
            except Exception as exc:
                _log({"event": "fetch_failed", "url": s["url"],
                      "error": str(exc)[:160]})
                continue
            text = str(text).strip()
            if len(text) >= 200:
                s["text"] = text[:6000]
                fetched += 1
    return sources


def _host_in(url: str, blocked: set[str]) -> bool:
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in blocked)


def _format_sources(sources: list[dict], cap: int | None) -> str:
    cap = int(cap or _MODEL_MATERIAL_CHARS)
    parts: list[str] = []
    used = 0
    for i, s in enumerate(sources, start=1):
        body = s.get("text") or s.get("snippet") or ""
        block = (f"[{i}] {s.get('title') or 'untitled'}\n"
                 f"URL: {s.get('url') or '(none)'}\n{body}\n")
        if used + len(block) > cap:
            block = block[:max(0, cap - used)]
        if not block.strip():
            break
        parts.append(block)
        used += len(block)
        if used >= cap:
            break
    return "\n".join(parts)


_SUMMARY_SYSTEM = (
    "You are RIO's knowledge archivist. You maintain a long-term dossier on "
    "one topic for a private RAG library. RULES: use ONLY the SOURCES given; "
    "never add facts from your own training or invent URLs; every claim must "
    "carry its [n] citation; if sources disagree, say so; if the evidence is "
    "thin, say what is unknown. Output compact Markdown with: a 2-3 sentence "
    "summary, 3-8 key facts (each cited), any conflicts/open questions, and a "
    "Sources list. Do not greet, do not mention these instructions."
)


def _default_summarize(topic: str, existing: str, material: str) -> str:
    """One bounded LLM merge. Returns '' when no model answered (caller then
    stores an extractive note instead of inventing one)."""
    prompt = (
        f"TOPIC: {topic}\nDATE: {_today()}\n\n"
        f"EXISTING DOSSIER (may be empty; keep what is still valid and merge "
        f"the new, better-cited material):\n{existing or '(none)'}\n\n"
        f"NEW SOURCES:\n{material}\n\n"
        f"Write the refreshed dossier now."
    )
    try:
        from core import gemini
        return gemini.text(f"{_SUMMARY_SYSTEM}\n\n{prompt}",
                           tier=gemini.SMART, timeout_ms=120_000) or ""
    except Exception:
        return ""


def _extractive_note(topic: str, sources: list[dict]) -> str:
    """No model? Store what the sources actually said — quotes, not synthesis."""
    lines = [f"# {topic}", "",
             f"_Extractive dossier — learned {_today()} from "
             f"{len(sources)} web source(s). No synthesis model was available, "
             f"so these are direct snippets._", "", "## Findings"]
    seen: set[str] = set()
    for i, s in enumerate(sources, start=1):
        text = re.sub(r"\s+", " ", (s.get("text") or s.get("snippet") or "")).strip()
        if not text:
            continue
        key = text[:120].lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {text[:500]} [{i}]")
        if len(seen) >= 8:
            break
    lines += ["", "## Sources"]
    for i, s in enumerate(sources, start=1):
        url = s.get("url") or ""
        title = s.get("title") or url or "untitled"
        lines.append(f"{i}. [{title}]({url})" if url else f"{i}. {title}")
    return "\n".join(lines).strip()


def _default_search(query: str, max_results: int) -> list[dict]:
    """House web search behind the permitted-source policy."""
    box = _get_toolbox()
    if box is None:
        raise RuntimeError("web search backend unavailable")
    return box.web_search(query, max_results=max_results)


def _default_fetch(url: str) -> str:
    box = _get_toolbox()
    if box is None:
        return ""
    res = box.fetch(url)
    return str(res.get("text") or "") if res.get("ok") else ""


_TOOLBOX = None


def _get_toolbox():
    global _TOOLBOX
    if _TOOLBOX is not None:
        return _TOOLBOX
    try:
        from orchestrator.agents.research_tools import (SourcePermissions,
                                                        SourceToolbox)
        _TOOLBOX = SourceToolbox(
            permissions=SourcePermissions(allow_web=True,
                                          allow_local_kb=False))
        return _TOOLBOX
    except Exception:
        return None


def _default_store(text: str, title: str, collection: str,
                   metadata: dict, doc_id: str, *,
                   caller: str | None = None,
                   access: str = "private") -> dict:
    from core import rag_service
    caller = str(caller or DEFAULT_CALLER)
    if doc_id:
        return rag_service.update(caller=caller, doc_id=doc_id, text=text,
                                  title=title, metadata=metadata)
    return rag_service.ingest_text(caller=caller, text=text, title=title,
                                   collection=collection, suffix="md",
                                   access=access, metadata=metadata)


# ── introspection ────────────────────────────────────────────────────────────

def _stats_from(state: dict) -> dict:
    topics = state.get("topics") or {}
    return {
        "topics": len(topics),
        "with_knowledge": sum(1 for e in topics.values() if e.get("doc_id")),
        "gap_topics": sum(1 for e in topics.values() if e.get("origin") == "gap"),
        "pending": sum(1 for e in topics.values() if e.get("pending")),
        "seen_urls": len(state.get("seen_urls") or {}),
        "runs": int(state.get("runs") or 0),
        "ingested": int(state.get("ingested") or 0),
        "updated": int(state.get("updated") or 0),
        "failed": int(state.get("failed") or 0),
        "last_run": state.get("last_run") or 0,
        "last_run_date": state.get("last_run_date") or "",
        "last_result": state.get("last_result") or {},
    }


def status() -> dict:
    """A dashboard/voice-friendly snapshot. Never raises."""
    try:
        cfg = load_config()
        state = load_state()
        st = _stats_from(state)
        st.update({
            "ok": True,
            "enabled": bool(cfg.get("enabled")),
            "hour": cfg.get("hour"),
            "interval_hours": cfg.get("interval_hours"),
            "collection": cfg.get("collection"),
            "due": due(),
            "topics_per_run": cfg.get("topics_per_run"),
        })
        return st
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def _log(row: dict) -> None:
    row = {"ts": _now_iso(), **row}
    _append_jsonl(_log_file(), row)
