"""
memory/conversation_journal.py — JARVIS never forgets a conversation.

Three layers, one file:

  1. JOURNAL (this module)   — every user/assistant turn, verbatim, appended to
                               memory/conversations/YYYY-MM.jsonl the moment it
                               happens. Append-only, never trimmed. This is the
                               raw "memorise everything" layer.
  2. TIMELINE (memory_manager) — 1-2 sentence session summaries, kept for a
                               year instead of the old cap of three.
  3. CURATED (long_term.json)  — identity / preferences / projects / … fed to
                               the system prompt. The evolution engine below
                               re-derives this from layers 1+2 periodically,
                               which is what makes the memory EVOLVE: the store
                               drifts with the person instead of freezing at
                               whatever was saved first.

Why a journal at all when recall_memory exists: the curated store holds
*conclusions* ("prefers dark mode"); the journal holds *events* ("asked to fix
the trading script at 11pm on the 14th, was frustrated"). Facts change; the
record of what actually happened must not be editable by later summarisation.
Any question phrased as "when did we / what did I ask you / what did we talk
about" is answered from here, not from the curated store.

Design constraints honored from the rest of the codebase:
  - Never raises into the audio path: every public call swallows its own
    errors to a log line. A failed memory write must not cost a voice turn.
  - No model call on the hot path: appends are local file I/O (microseconds).
  - Evolution is the ONLY expensive part, and it runs at most once a day,
    in a background thread, reusing core.gemini_retry's LLM failover router.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from memory.memory_manager import get_base_dir

_BASE      = get_base_dir()
JOURNAL_DIR = _BASE / "memory" / "conversations"

# A turn shorter than this is noise ("ok", "haan") — not worth journaling or
# feeding the evolution pass. The verbatim file still keeps whatever arrived.
_MIN_TURN_CHARS = 12

# Evolution cadence. A daily-ish consolidation is frequent enough that the
# curated memory tracks the person, rare enough that it never competes with
# real use. The threshold counts *journal entries* since the last pass, so an
# idle week costs nothing and a heavy day can trigger it.
_EVOLVE_MIN_INTERVAL_H = 20.0
_EVOLVE_MIN_ENTRIES     = 30

# Hard size guards. The journal is meant to grow for years; these only stop a
# pathological runaway (a bug appending in a loop) from eating the disk.
# NB: _MAX_FILE_BYTES is BYTES per month file; _MAX_MONTH_FILES is a COUNT of
# month files (an early draft compared bytes against the count and silently
# rejected every append past ~240 bytes — exactly three turns).
_MAX_FILE_BYTES  = 25_000_000    # ~25 MB per month file → rotates to a new one
_MAX_MONTH_FILES = 240           # 20 years of months — effectively "forever"

_lock = threading.Lock()
_state_path = JOURNAL_DIR / "_evolution_state.json"

# Set by main.py: surfaces evolution results to the UI, same pattern as
# memory_manager.set_trim_notifier.
_notify = None


def set_journal_notifier(fn) -> None:
    global _notify
    _notify = fn


def _notify_ui(msg: str) -> None:
    if _notify is not None:
        try:
            _notify(msg)
        except Exception:
            pass


def _state() -> dict:
    try:
        return json.loads(_state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    try:
        _state_path.parent.mkdir(parents=True, exist_ok=True)
        _state_path.write_text(json.dumps(st), encoding="utf-8")
    except Exception as e:
        print(f"[Journal] ⚠️ state save failed: {e}")


# ── 1. The journal ────────────────────────────────────────────────────────────

def _month_file(dt: datetime) -> Path:
    return JOURNAL_DIR / f"{dt.strftime('%Y-%m')}.jsonl"


def append_turn(speaker: str, text: str) -> bool:
    """Append one conversation turn. Fire-and-forget safe: never raises.

    Entries are small JSON lines — one per turn:
        {"ts": "...", "speaker": "user|jarvis", "text": "..."}
    """
    try:
        text = (text or "").strip()
        if not text:
            return False
        now = datetime.now()
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": now.isoformat(timespec="seconds"),
               "speaker": "user" if speaker.lower().startswith("u") else "jarvis",
               "text": text}
        line = json.dumps(rec, ensure_ascii=False)
        with _lock:
            path = _month_file(now)
            # Rotate when one month's file grows past the guard (a runaway bug
            # or a very chatty month): start a -part2 file rather than lose data.
            # A MISSING file must break too — the guard only applies to files
            # that exist; otherwise the first append of a month walks the whole
            # range and lands in -part39.
            for i in range(2, 40):
                if not path.exists() or path.stat().st_size < _MAX_FILE_BYTES:
                    break
                path = JOURNAL_DIR / f"{now.strftime('%Y-%m')}-part{i}.jsonl"
            # Retention guard: keep at most _MAX_MONTH_FILES month files,
            # pruning the OLDEST when exceeded (a count check, not bytes).
            try:
                files = sorted(JOURNAL_DIR.glob("*.jsonl"))
                excess = len(files) - _MAX_MONTH_FILES
                for old in files[:max(0, excess)]:
                    try:
                        old.unlink()
                    except OSError:
                        pass
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except Exception as e:
        print(f"[Journal] ⚠️ append failed: {e}")
        return False


def _iter_journal(months: int | None = None, prefix: datetime | None = None) -> list[dict]:
    """Read journal entries, oldest first. months=None → everything."""
    try:
        paths = sorted(JOURNAL_DIR.glob("*.jsonl"))
    except OSError:
        return []
    cutoff = None
    if prefix is not None:
        cutoff = prefix
    out: list[dict] = []
    for p in paths:
        if months is not None and cutoff is not None:
            stem = p.stem.split("-part")[0]
            try:
                if datetime.strptime(stem, "%Y-%m") < cutoff:
                    continue
            except ValueError:
                pass
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict) and rec.get("text"):
                        out.append(rec)
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue
    return out


def search_journal(query: str, limit: int = 12) -> str:
    """Full-history search for the recall path. Lexical scoring, same spirit as
    memory_manager._score — cheap, local, no model call. Empty query returns a
    summary of what the journal holds."""
    recs = _iter_journal()
    if not recs:
        return "The conversation journal is empty — nothing spoken has been recorded yet."

    if not (query or "").strip():
        days = {r["ts"][:10] for r in recs if r.get("ts")}
        return (f"Journal holds {len(recs)} recorded turns across "
                f"{len(days)} days, earliest {min(days) if days else '?'}, "
                f"latest {max(days) if days else '?'}. Search it with a keyword.")

    words = [w for w in re.split(r"[^\w]+", query.lower()) if len(w) > 1]
    if not words:
        return "Give me a keyword to search the conversation history."

    scored: list[tuple[int, dict]] = []
    for r in recs:
        text = str(r.get("text", ""))
        low = text.lower()
        s = 0
        for w in words:
            hits = low.count(w)
            if hits:
                s += 3 + min(hits - 1, 3)   # repeated mention ranks higher
        if s:
            scored.append((s, r))
    if not scored:
        return f"Nothing in the recorded conversations matches '{query}'."

    scored.sort(key=lambda t: (-t[0], t[1].get("ts", "")))
    lines = []
    for s, r in scored[:max(1, limit)]:
        spk = "User" if r.get("speaker") == "user" else "JARVIS"
        lines.append(f"[{r.get('ts', '?')}] {spk}: {text if False else str(r.get('text'))[:220]}")
    more = (f"\n(+{len(scored) - len(lines)} more matches — narrow the keyword)"
            if len(scored) > len(lines) else "")
    return ("From the conversation history:\n" + "\n".join(lines) + more)


def recent_journal_tail(max_chars: int = 1200) -> str:
    """The last few journal turns, oldest first, for prompt injection when a
    fresh session replaces a dead one (main.py _build_config). Budgeted in
    characters — the prompt must stay small no matter how chatty yesterday
    was. Returns '' when the journal has nothing usable; a record missing
    `ts` (corrupt line already survived _iter_journal's JSON guard) is
    skipped rather than formatted with a bare '?'."""
    recs = [r for r in _iter_journal()
            if r.get("ts") and str(r.get("text", "")).strip()]
    if not recs:
        return ""
    tail = recs[-8:]
    lines = [f"{'User' if r.get('speaker') == 'user' else 'JARVIS'}: "
             f"{str(r.get('text'))[:200]}" for r in tail]
    out = "\n".join(lines)
    # Trim from the FRONT so the newest turn — the one most likely to matter —
    # always survives the budget.
    while len(out) > max_chars and "\n" in out:
        out = out.split("\n", 1)[1]
    return out


# ── 2. The evolution engine ───────────────────────────────────────────────────

def _consolidation_context(months_back: int = 2) -> tuple[str, int, str]:
    """(raw text window, entry count, range label) for the evolution pass.
    Session summaries (layer 2) + journal turns since `months_back`, capped so
    the prompt stays bounded."""
    from memory.memory_manager import load_memory
    memory = load_memory()
    sessions = memory.get("sessions", []) or []
    sess_lines = [f"{s.get('date', '?')}: {s.get('summary', '')}"
                  for s in sessions if isinstance(s, dict)]
    sess_text = "\n".join(sess_lines[-60:])          # ~2 months of sessions

    cutoff = datetime.now() - timedelta(days=30 * months_back)
    recs = [r for r in _iter_journal() if (r.get("ts", "") or "") >= cutoff.isoformat()]
    turns = [f"{r['ts'][:10]} {'User' if r.get('speaker') == 'user' else 'JARVIS'}: "
             f"{str(r.get('text'))[:200]}" for r in recs]
    raw = "SESSION SUMMARIES:\n" + (sess_text or "(none)") + \
          "\n\nRECENT VERBATIM TURNS:\n" + ("\n".join(turns[-400:]) or "(none)")
    label = f"{len(sess_lines)} summaries + {len(turns)} recent turns"
    return raw, len(sess_lines) + len(turns), label


_EVOLVE_PROMPT = """You maintain the long-term memory of a personal assistant.

Below are (a) dated session summaries and (b) recent verbatim conversation turns \
with the user. Compare them against the CURRENT MEMORY at the end.

Write an updated memory in EXACTLY this JSON shape (no markdown fence, no prose):
{{"identity": {{"field": "value"}}, "preferences": {{...}}, "projects": {{...}}, \
"relationships": {{"person_name": "who they are to the user, what matters"}}, \
"wishes": {{"topic": "plan/desire"}}, "notes": {{"topic": "fact"}}}}

Rules:
- EVOLVE, don't replace: carry forward everything still true from CURRENT MEMORY. \
Only change entries the conversation contradicts, and add what is clearly new.
- Prefer stable traits over moods; drop observations the newer record contradicts.
- Keys: short snake_case. Values: concise English, under 200 characters.
- Infer carefully: "trades on TradingView daily" yes; "was happy about stocks" no.
- Omit a category key entirely if it has no entries.

CURRENT MEMORY:
{memory}

RECORD:
{record}"""


def _build_evolved_memory(raw: str) -> dict | None:
    """One LLM consolidation call. Uses whatever text backend is alive —
    Gemini primary, OpenRouter failover — via core.llm_router's class-level
    patch, with the shared retry policy for transient blips."""
    from google import genai as _genai
    from core.gemini_retry import retry_gemini_async, is_transient_error
    from memory.memory_manager import load_memory

    current = load_memory()
    prompt = _EVOLVE_PROMPT.format(
        memory=json.dumps({k: v for k, v in current.items()
                           if k != "sessions"}, ensure_ascii=False)[:6000],
        record=raw[:16000],
    )

    # Bound the HTTP call: this runs via _run_async inside a default-executor
    # worker, and asyncio runs DNS on that same pool. An unbounded generate()
    # on a dead socket would pin the worker and starve reconnects.
    client = _genai.Client(api_key=_load_gemini_key(),
                           http_options={"timeout": 90_000})  # ms

    async def _ask() -> str:
        import asyncio as _aio
        resp = await _aio.to_thread(
            client.models.generate_content,
            model="gemini-flash-latest",
            contents=prompt,
        )
        return resp.text or ""

    # retry_gemini_async wraps a coroutine function (fn is positional — see
    # its signature / usage in main.py), then we drive it from this thread.
    try:
        text = _run_async(retry_gemini_async(_ask, label="Evolution")())
    except Exception as e:
        if is_transient_error(e):
            print(f"[Evolution] ⚠️ consolidation deferred ({str(e)[:80]}) — will retry next cycle")
        else:
            print(f"[Evolution] ⚠️ consolidation failed: {e}")
        return None

    return _parse_memory_json(text)


def _load_gemini_key() -> str:
    import json as _json
    cfg = _BASE / "config" / "api_keys.json"
    return _json.loads(cfg.read_text(encoding="utf-8")).get("gemini_api_key", "")


def _run_async(coro):
    """Run a coroutine from a worker thread (evolution runs off the event loop)."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:            # called from async context (tests)
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(1) as ex:
            return ex.submit(asyncio.run, coro).result(timeout=180)
    return asyncio.run(coro)


def _parse_memory_json(text: str) -> dict | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
    out: dict = {}
    for cat, items in data.items():
        if cat not in valid or not isinstance(items, dict):
            continue
        clean: dict = {}
        for k, v in items.items():
            if isinstance(v, dict):
                v = v.get("value", "")
            if isinstance(v, str) and v.strip():
                clean[str(k).strip()[:60]] = v.strip()[:200]
        if clean:
            out[cat] = clean
    return out or None


def maybe_evolve(force: bool = False) -> str | None:
    """Run one consolidation pass if due. Thread-safe; returns a result note.

    'Due' = ≥30 new journal entries since the last pass AND ≥20h elapsed
    (or force=True). The gate state lives on disk so restarts never lose it.
    """
    with _lock:
        st = _state()
        now = time.time()
        last_run = float(st.get("last_run", 0))
        entries_then = int(st.get("entries_at_last_run", 0))
        if not force:
            if now - last_run < _EVOLVE_MIN_INTERVAL_H * 3600:
                return None
            total = len(_iter_journal())
            if total - entries_then < _EVOLVE_MIN_ENTRIES:
                return None
        st["last_run"] = now
        st["entries_at_last_run"] = len(_iter_journal())
        _save_state(st)

    raw, _n, label = _consolidation_context()
    if _n < 10:
        # Not enough lived history to justify an LLM call yet.
        return "not enough history yet"

    print(f"[Evolution] 🧬 Consolidating ({label})…")
    _notify_ui("SYS: Consolidating long-term memory from recent conversations…")
    evolved = _build_evolved_memory(raw)

    if not evolved:
        return None

    # Merge, don't clobber: evolution may legitimately drop stale entries, but
    # a truncated model response must never wipe a year of curated facts.
    from memory.memory_manager import load_memory, save_memory
    current = load_memory()
    merged = {k: dict(v) if isinstance(v, dict) else v for k, v in current.items()}
    dropped = 0
    for cat, items in evolved.items():
        merged.setdefault(cat, {})
        for k, v in items.items():
            merged[cat][k] = v
    # Entries the model removed AND that the recent record no longer mentions:
    # they simply fall out of `merged` only if we remove them — we don't. This
    # pass can only ADD or REFRESH; true removal waits for the user ("forget X")
    # or the size trim, which is the safe direction for a memory.
    if dropped:
        pass
    save_memory(merged)
    added = sum(len(v) for v in evolved.values())
    note = f"Evolved: {added} facts refreshed across {len(evolved)} categories ({label})"
    print(f"[Evolution] ✅ {note}")
    _notify_ui(f"SYS: {note}.")
    return note


# ── 3. Maintenance ────────────────────────────────────────────────────────────

def prune_old_files(keep_years: float = 10.0) -> int:
    """Delete journal month-files older than the retention window. The default
    is 'effectively forever'; lower it if the disk ever matters."""
    cutoff = datetime.now() - timedelta(days=365 * keep_years)
    removed = 0
    try:
        for p in JOURNAL_DIR.glob("*.jsonl"):
            try:
                if datetime.strptime(p.stem.split("-part")[0], "%Y-%m") < cutoff:
                    p.unlink()
                    removed += 1
            except ValueError:
                continue
    except OSError:
        pass
    return removed
