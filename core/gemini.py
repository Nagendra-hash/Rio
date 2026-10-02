"""
One place where the assistant's one-shot Gemini calls are made.

WHY THIS EXISTS
    The live conversation runs on the Live API and is not what this file is
    about. Everything else — reading a screenshot, parsing a flight page,
    turning a request into a shell command, working out which WhatsApp button
    answers a call — was a separate `genai.Client(...)` built at the point of
    use, with the model name written inline. Sixteen files did it, twenty-six
    times, and not one of them set a timeout.

    That is not tidiness, it is three real faults:

    NO TIMEOUT.  The SDK waits forever by default. `gemini-flash-latest` spent
    an afternoon returning 504 DEADLINE_EXCEEDED, and every one of those calls
    became an unbounded hang — measured at ten seconds of silence while a phone
    rang, and worse elsewhere, because nothing was there to give up.

    NO FALLBACK.  One hardcoded alias meant that when that alias was unwell,
    the feature was simply gone. A ladder costs nothing when the first rung
    works and saves the feature when it does not.

    NO SINGLE PLACE TO CHANGE.  A new model release meant editing sixteen files
    and hoping none were missed.

THE LIVE MODEL DOES THIS WORK, AND IT LEADS THE LADDER
    Not the user's conversation — a separate, throwaway session per call, so
    nothing a plugin asks is ever heard by the person at the microphone.

    It leads because of quota. This is a voice assistant; the Live API is the
    dependency it already has, and it draws on a different pool from the text
    models. On the free tier it is the TEXT pool that runs out, and when it does
    every side call fails and the feature behind it dies with it. Putting Live
    first means ordinary use stops spending the pool that runs dry.

    The reply arrives through output_transcription, because these models refuse
    response_modalities=["TEXT"] with a 1007 — they only speak. That sounds
    fatal for structured output and is not: the transcription is the model's own
    text of what it said, and it returned "Mum ❤ click here for contact info",
    indented Python inside markdown fences, and src/utils/helpers_v2.py
    character for character.

    It cannot carry grounding metadata, so grounded web search stays on REST.

THE LADDER, MEASURED
    Live, one throwaway session:
        connect                     0.24s
        short structured JSON       1.7 - 2.8s
        2300 characters of code     3.39s, not truncated
        three concurrent sessions   all fine, 4.77s wall clock
    REST text models, same prompt, same afternoon:
        gemini-2.5-flash-lite       0.76s   ...then 429, quota exhausted
        gemini-2.5-flash            0.80s   ...then 429
        gemini-flash-lite-latest    2.58s
        gemini-flash-latest         504, every time
    So REST is two to four times quicker while it lasts, and the whole point of
    the ladder is that it does not last. Pinned REST names sit behind Live;
    rolling `-latest` aliases sit behind those, because they were the ones
    having a bad day.

    Where the answer depends only on stable input, cache it and neither pool is
    touched twice: `plugins/_whatsapp_core.py` is the worked example — one
    request per WhatsApp language for the lifetime of the install.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import threading
from pathlib import Path

import numpy as np

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent
# JARVIS_HOME (pendrive mode): the key file is writable state, so it follows
# the relocatable home — same contract as memory_manager.get_base_dir.
import os as _os_home
_home = _os_home.environ.get("JARVIS_HOME", "").strip()
if _home:
    _BASE = Path(_home).expanduser().resolve()

_KEY_FILE = _BASE / "config" / "api_keys.json"

# Ladders, tried left to right. Change a model HERE and the whole app follows.
FAST = "fast"      # short classification, extraction, one-line decisions
SMART = "smart"    # reasoning, generation, long documents, images
SEARCH = "search"  # grounded search — REST only, see below

# A rung that means "ask the LOCAL model instead" (see core/llm_local.py):
# the llm_router's local engines — Ollama `qwen2.5:1.5b` first (warm, ~1.7 s a
# turn, zero quota), then the RAM-guarded Qwen2.5-Coder-3B GGUF. Costs no
# quota and works offline. Set "local_rung_first": true in
# config/api_keys.json to lead the FAST/SMART ladders with it (the routing
# plan's strictest local-first posture); the default keeps Live first, which
# protects the separate Live quota pool the voice conversation depends on.
LOCAL = "local"

# A rung that means "ask the Live model instead", through a short throwaway
# session rather than the REST text API.
#
# WHY IT LEADS
#     This is a voice assistant: the Live API is the dependency it already has,
#     and it draws on a DIFFERENT quota pool from the text models. On the free
#     tier the text pool is the one that runs out — an afternoon of ordinary use
#     exhausts it, and when it does, every one of these side calls fails and the
#     feature behind it dies. The Live pool is untouched by that.
#
# WHAT IT COSTS, MEASURED
#     connect                      0.24s
#     short structured JSON        1.7 - 2.8s   (REST: 0.76s)
#     2300 characters of code      3.39s, not truncated
#     three concurrent sessions    all fine, 4.77s wall clock
#     So it is two to four times slower than REST when REST is available, and
#     infinitely faster than REST when REST is out of quota.
#
# THE THING WORTH KNOWING
#     These models only speak — response_modalities=["TEXT"] is refused with a
#     1007. The reply comes back through output_transcription, which sounds like
#     it would mangle anything structured. It does not: it is the model's own
#     text of what it said, and it survived "Mum ❤ click here for contact info",
#     indented Python inside markdown fences, and src/utils/helpers_v2.py
#     character for character. That is what makes this usable at all.
#
#     What it cannot carry is grounding metadata, so grounded web search stays
#     on REST — see SEARCH.
LIVE = "live"

_LADDERS = {
    FAST: (LIVE, "gemini-flash-lite-latest", "gemini-flash-latest", LOCAL),
    SMART: (LIVE, "gemini-flash-latest", "gemini-flash-lite-latest", LOCAL),
    # Grounded search needs response.candidates[...].grounding_metadata, which a
    # Live turn does not produce. REST only, and it says so rather than silently
    # returning an answer with no sources behind it. Deliberately NO local rung:
    # a 3B model without grounding would answer current-facts questions from
    # stale weights — the callers' DDG fallback is the honest degradation.
    SEARCH: ("gemini-flash-latest", "gemini-flash-lite-latest"),
}
# NOTE: measured against a live key (2026-09): the pinned gemini-2.5-* rungs
# were retired for new keys (permanent 404 "no longer available to new users"),
# so they only ever cost a dead round-trip. The rolling -latest aliases and the
# Live rung both answer; keep the pins as -latest aliases until a stable
# successor name is worth pinning.

# The Live model to use for one-shot calls. main.py owns the real one; this is
# only the fallback for when this module is imported without it (tests).
_LIVE_FALLBACK = "models/gemini-3.1-flash-live-preview"

# How many one-shot Live sessions may exist at once.
#
# THE USER'S CONVERSATION OUTRANKS EVERY SIDE CALL.
# Nothing here can reach the microphone or the speaker — main.py has exactly one
# `.receive()` and it is bound to its own session, and audio only reaches the
# speaker through that one loop — so a side call cannot answer the user or talk
# over the reply. Verified alongside a live main session: four side calls fired
# while it was connected, each got its own answer, and the main session replied
# correctly both before and after with no errors.
#
# What a side call CAN do is take up a concurrent-session slot. That is the one
# way it could hurt the conversation, so it is capped, and a call that cannot
# get a slot quickly does not queue behind the others — it falls to the REST
# rung, which is what the ladder is for.
# Three, from the measurement: four side calls plus the conversation ran
# together without complaint, so three leaves the conversation a slot in hand
# while still covering any burst this app actually produces — tool calls run one
# after another, and the screen agent's loop is sequential.
_LIVE_SLOTS = threading.BoundedSemaphore(3)
_LIVE_SLOT_WAIT = 3.0

_ONE_SHOT_SYSTEM = (
    "You are a data-processing function, not an assistant and not in a "
    "conversation. There is no person listening to you. Produce exactly the "
    "output the request asks for and nothing else: no greeting, no "
    "acknowledgement, no 'Understood', no explanation, no closing remark, no "
    "restating of the question. If the request asks for JSON, emit only the "
    "JSON. If it asks for code, emit only the code. If it asks for one word, "
    "emit that one word. Preserve the exact spelling, punctuation, capitals "
    "and whitespace of anything you are asked to copy or return."
)

# Milliseconds. Not a preference: the API rejects anything under ten seconds
# with "Minimum allowed deadline is 10s", so this is the tightest bound it will
# accept. Callers with a long job (a whole document, a big image) pass more.
DEFAULT_TIMEOUT_MS = 10_000
MIN_TIMEOUT_MS = 10_000

_key_lock = threading.Lock()
_cached_key: str | None = None

# A rung that answered 429 is out of quota, and on the free tier it will stay
# that way for a while. Retrying it on every single call is a wasted round trip
# in front of every request the assistant makes — measured on this key, the
# lite rung was 429ing continuously, so every call was paying for it before
# reaching the model that could actually answer. Remembering that for a few
# minutes turns the ladder from a cost into a saving.
_COOLDOWN_SECONDS = 300
_cooldown: dict[str, float] = {}
_cool_lock = threading.Lock()


def _cool(model: str) -> None:
    with _cool_lock:
        _cooldown[model] = time.monotonic() + _COOLDOWN_SECONDS


def _cooling(model: str) -> bool:
    with _cool_lock:
        until = _cooldown.get(model, 0.0)
        if until and time.monotonic() < until:
            return True
        _cooldown.pop(model, None)
        return False


def api_key(refresh: bool = False) -> str:
    """The Gemini key from config/api_keys.json. Cached; never raises."""
    global _cached_key
    with _key_lock:
        if _cached_key is not None and not refresh:
            return _cached_key
        try:
            data = json.loads(_KEY_FILE.read_text(encoding="utf-8"))
            _cached_key = str(data.get("gemini_api_key") or "")
        except Exception:
            _cached_key = ""
        return _cached_key


def client(timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = ""):
    """A configured genai.Client with a deadline on it. Raises if there is no
    key, because a caller that cannot work without one should say so."""
    from google import genai
    from google.genai import types as gtypes

    key = key or api_key()
    if not key:
        raise RuntimeError("no Gemini API key is configured")
    return genai.Client(
        api_key=key,
        http_options=gtypes.HttpOptions(timeout=max(MIN_TIMEOUT_MS, int(timeout_ms))),
    )


class _Reply:
    """What a Live turn hands back, shaped like the REST response's `.text` so
    every existing call site keeps working unchanged."""

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


def _live_model() -> str:
    """Whatever main.py is running, so upgrading the assistant upgrades this."""
    return getattr(sys.modules.get("main"), "LIVE_MODEL", None) or _LIVE_FALLBACK


def _to_live_parts(contents) -> list:
    """REST `contents` -> Live `parts`. Accepts a bare string, a list of
    strings, and the SDK's Part objects (which is how every image is passed
    here), because those are the three shapes the call sites actually use."""
    import base64

    items = contents if isinstance(contents, (list, tuple)) else [contents]
    parts = []
    for item in items:
        if isinstance(item, str):
            parts.append({"text": item})
            continue
        blob = getattr(item, "inline_data", None)
        if blob is not None:
            data = getattr(blob, "data", None)
            mime = getattr(blob, "mime_type", None) or "application/octet-stream"
            if isinstance(data, bytes):
                data = base64.b64encode(data).decode("ascii")
            parts.append({"inline_data": {"mime_type": mime, "data": data}})
            continue
        txt = getattr(item, "text", None)
        if txt:
            parts.append({"text": txt})
            continue
        if isinstance(item, dict):
            parts.append(item)
    return parts


async def _live_turn(parts: list, system: str, key: str, timeout_s: float) -> str:
    from google import genai
    from google.genai import types as gtypes

    cl = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
    # Silence the persona, or it answers instead of complying.
    #
    # These are conversational models and they behave like it: asked "Reply with
    # one word: ok" a Live turn came back with "Understood." — it treated the
    # instruction as something to acknowledge rather than something to do. The
    # REST models do not, because nobody ever taught them to be in a
    # conversation. Every call through this module wants a value, not a reply,
    # so the session is told what it is before it is told what to do.
    kwargs = {
        "response_modalities": ["AUDIO"],
        "output_audio_transcription": {},
        "system_instruction": _ONE_SHOT_SYSTEM + (f"\n\n{system}" if system else ""),
    }

    cm = cl.aio.live.connect(model=_live_model(),
                             config=gtypes.LiveConnectConfig(**kwargs))
    session = await asyncio.wait_for(cm.__aenter__(), 30)
    try:
        await session.send_client_content(
            turns={"role": "user", "parts": parts}, turn_complete=True)
        chunks: list[str] = []

        async def drain():
            async for resp in session.receive():
                sc = getattr(resp, "server_content", None)
                if sc and sc.output_transcription and sc.output_transcription.text:
                    chunks.append(sc.output_transcription.text)

        await asyncio.wait_for(drain(), timeout=timeout_s)
        # The transcription can trail the audio turn by a beat; a short second
        # drain stops a reply being cut mid-token. chat_takeover learned this
        # the same way and for the same reason.
        try:
            await asyncio.wait_for(drain(), timeout=1.5)
        except asyncio.TimeoutError:
            pass
        return "".join(chunks).strip()
    finally:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            pass


def _live_call(contents, config, timeout_ms: int, key: str):
    """One throwaway Live session, run on its own loop in its own thread.

    A dedicated thread rather than asyncio.run() on the caller's: these are
    invoked from plugin executor threads, from UI worker threads and from
    main.py's own event loop, and asyncio.run() inside a thread that already has
    a running loop raises. Its own thread has no loop to collide with, wherever
    it was called from.

    Reconnecting costs 0.24s, so nothing is kept alive between calls — no
    session lifetime cap to manage, no GoAway to handle, no shared state.
    """
    system = ""
    if config is not None:
        system = getattr(config, "system_instruction", None) or \
            (config.get("system_instruction") if isinstance(config, dict) else "") or ""

    parts = _to_live_parts(contents)
    if not parts:
        return None

    if not _LIVE_SLOTS.acquire(timeout=_LIVE_SLOT_WAIT):
        # Every slot is busy. Do not wait it out: falling to REST costs less
        # than holding a session the user's conversation might want.
        raise RuntimeError("no free Live slot — leaving them for the conversation")

    box: dict = {}

    def runner():
        try:
            box["text"] = asyncio.run(
                _live_turn(parts, str(system), key, max(10.0, timeout_ms / 1000.0)))
        except BaseException as e:                     # noqa: BLE001
            box["error"] = e

    try:
        th = threading.Thread(target=runner, daemon=True, name="gemini-live-oneshot")
        th.start()
        th.join(timeout=max(15.0, timeout_ms / 1000.0 + 20.0))
    finally:
        _LIVE_SLOTS.release()
    if "error" in box:
        raise box["error"]
    text = box.get("text")
    return _Reply(text) if text else None


def _ladder_for(tier: str) -> tuple:
    """The ladder for a tier, with one config-driven adjustment: when
    'local_rung_first' is set (the routing plan's strictest local-first
    posture), the LOCAL rung leads FAST/SMART instead of trailing."""
    ladder = _LADDERS.get(tier)
    if ladder is None:
        return ladder
    try:
        lead = bool(json.loads(_KEY_FILE.read_text(encoding="utf-8"))
                    .get("local_rung_first"))
    except Exception:
        lead = False
    if lead and LOCAL in ladder:
        return (LOCAL,) + tuple(m for m in ladder if m != LOCAL)
    return ladder


def call(contents, tier: str = FAST, config=None,
         timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = ""):
    """Run one generation, walking the ladder until one answers.

    Returns the SDK's own response object, so callers that need more than the
    text — grounding metadata, candidates, usage — still get it. Returns None
    when every model on the ladder failed; the reason for each is printed, since
    a silent None during a session nobody can debug is how the original problem
    stayed hidden.
    """
    # `tier` is normally FAST or SMART. Anything else is taken to be an explicit
    # model name — screen_agent lets the user pick one in its settings — and it
    # is tried first, with the reasoning ladder behind it. So a user's choice is
    # honoured, and a user's choice that is having an outage still degrades to
    # something that answers instead of to nothing.
    ladder = _ladder_for(tier)
    if ladder is None:
        ladder = (tier,) + tuple(m for m in _LADDERS[SMART] if m != tier)

    resolved_key = key or api_key()
    if not resolved_key:
        print("[Gemini] no Gemini API key is configured")
        return None

    cl = None
    tried = [m for m in ladder if not _cooling(m)] or list(ladder)
    for model in tried:
        try:
            if model == LIVE:
                reply = _live_call(contents, config, timeout_ms, resolved_key)
                if reply is not None:
                    return reply
                raise RuntimeError("the Live turn came back empty")
            if model == LOCAL:
                # Local first (the routing plan): Ollama `qwen2.5:1.5b` when
                # it is up, then the RAM-guarded 3B coder GGUF. When neither
                # can serve (ollama down, GGUF missing, RAM tight) the rung
                # fails like any other and the ladder moves on.
                from core import llm_router
                system = ""
                if config is not None:
                    system = getattr(config, "system_instruction", None) or \
                        (config.get("system_instruction") if isinstance(config, dict) else "") or ""
                text, _engine = llm_router.local_generate(
                    _local_prompt(contents), system=system)
                if text:
                    return _Reply(text)
                raise RuntimeError("the local model returned nothing")
            if cl is None:
                cl = client(timeout_ms=timeout_ms, key=resolved_key)
            kwargs = {"model": model, "contents": contents}
            if config is not None:
                kwargs["config"] = config
            return cl.models.generate_content(**kwargs)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                _cool(model)
                print(f"[Gemini] {model}: out of quota — skipping it for "
                      f"{_COOLDOWN_SECONDS // 60} minutes")
            else:
                print(f"[Gemini] {model}: {type(e).__name__}: {msg[:140]}")
    return None


def _local_prompt(contents) -> str:
    """REST contents → one plain-text prompt for the local model. String
    inputs pass through; lists keep only their text parts (a 3B text model
    cannot read images — those rungs fail loudly instead of guessing)."""
    if isinstance(contents, str):
        return contents
    parts: list[str] = []
    for item in contents if isinstance(contents, (list, tuple)) else [contents]:
        if isinstance(item, str):
            parts.append(item)
            continue
        txt = getattr(item, "text", None)
        if txt:
            parts.append(txt)
    if not parts:
        raise RuntimeError("local rung cannot serve image-only requests")
    return "\n\n".join(parts)


def text(contents, tier: str = FAST, config=None,
         timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = "", default: str = "") -> str:
    """`call`, reduced to the reply text. `default` when nothing answered."""
    resp = call(contents, tier=tier, config=config,
                timeout_ms=timeout_ms, key=key)
    if resp is None:
        return default
    return (getattr(resp, "text", None) or "").strip() or default


def as_json(contents, tier: str = FAST, config=None,
            timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = "", default=None):
    """`text`, parsed as JSON, tolerating the fences and prose a model wraps it
    in. `default` when nothing answered or the answer would not parse."""
    raw = text(contents, tier=tier, config=config, timeout_ms=timeout_ms, key=key)
    if not raw:
        return default
    if "{" in raw and "}" in raw:
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
    elif "[" in raw and "]" in raw:
        raw = raw[raw.find("["): raw.rfind("]") + 1]
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"[Gemini] reply was not JSON: {e}")
        return default


def clear_cooldowns() -> None:
    """Forget every 429 cooldown now — the rungs are retried immediately.

    A cooldown that was right at noon can be wrong at one; the dashboard's
    Gemini-client settings expose this so a person asking for a retry
    outranks a timestamp.
    """
    with _cool_lock:
        _cooldown.clear()


def status() -> dict:
    """A snapshot for dashboards and debuggers. Never raises, never touches
    the network — and the key is length-masked, never echoed."""
    try:
        ladders = {name: list(models) for name, models in _LADDERS.items()}
    except Exception:
        ladders = {}
    with _cool_lock:
        now = time.monotonic()
        cooling = sorted(m for m, until in _cooldown.items() if until > now)
    key = api_key()
    try:
        from core import llm_local
        local = llm_local.status()
    except Exception:
        local = {"configured": False}
    return {
        "key_configured": bool(key),
        "key_masked": (key[:6] + "…" + key[-4:]) if len(key) > 12
                      else ("•" * len(key) if key else ""),
        "timeout_ms": DEFAULT_TIMEOUT_MS,
        "live_model": _live_model(),
        "ladders": ladders,
        "cooling": cooling,
        "local": local,
    }


# ── Embeddings (RAG knowledge layer) ────────────────────────────────────────
# One model, one place. Embeddings have no Live rung — the Live API carries no
# embedding surface — so this is REST-only by nature, and the quota risk it
# carries is small: embedding calls are cheap and batching keeps the request
# count in the dozens per whole document, not per chunk.

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 3072                    # full size of the model's output

# Asymmetric retrieval: documents and queries embed with different task types
# so the query sits close to its answers in vector space.
_TASK_RETRIEVAL_DOCUMENT = "RETRIEVAL_DOCUMENT"
_TASK_RETRIEVAL_QUERY = "RETRIEVAL_QUERY"

# Batches above ~100 texts start rejecting; 32 keeps requests (and any single
# failure's blast radius) small.
_EMBED_BATCH = 32


def embed(texts, is_query: bool = False, timeout_ms: int = 30_000,
          key: str = "") -> list:
    """Embed a list of texts (or one string → list of one vector).

    Returns a list of np.float32 arrays, one per input, in order. A batch that
    fails raises — the caller decides whether that kills an ingest (it should:
    a half-indexed document is worse than none) or degrades a search (falling
    back to lexical search is honest).
    """
    from google.genai import types as gtypes

    single = isinstance(texts, str)
    items = [texts] if single else list(texts)
    if not items:
        return []

    task = _TASK_RETRIEVAL_QUERY if is_query else _TASK_RETRIEVAL_DOCUMENT
    resolved_key = key or api_key()
    if not resolved_key:
        raise RuntimeError("no Gemini API key is configured")
    cl = client(timeout_ms=timeout_ms, key=resolved_key)

    vectors: list = []
    for start in range(0, len(items), _EMBED_BATCH):
        chunk = items[start:start + _EMBED_BATCH]
        resp = cl.models.embed_content(
            model=EMBED_MODEL,
            contents=chunk,
            config=gtypes.EmbedContentConfig(task_type=task,
                                             output_dimensionality=EMBED_DIM),
        )
        batch = list(resp.embeddings or [])
        if len(batch) != len(chunk):
            raise RuntimeError(
                f"embedding batch short: {len(batch)} of {len(chunk)}")
        vectors.extend(np.asarray(e.values, dtype=np.float32) for e in batch)
    return vectors[0] if single else vectors
