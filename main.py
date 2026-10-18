import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **                       kw)

    _subprocess.Popen = _Popen

# ─────────────────────────────────────────────────────────────────────────────

# ── Console encoding ─────────────────────────────────────────────────────────
# Status lines in this app carry emoji and arrows ("📤 file_controller → Moved:
# a.txt → Documents/"). On a non-UTF-8 console — cp1254 on a Turkish Windows,
# cp1251 on a Russian one, cp932 on a Japanese one — printing one of those
# raises UnicodeEncodeError, and because the print sits after the tool's own
# try/except, the exception escapes into the receive loop and takes the session
# down. The assistant dies on a log line.
#
# Reconfiguring costs nothing and makes the app start the same way in every
# locale. `errors="replace"` is the belt and braces — a console that genuinely
# cannot render a glyph shows a box instead of killing the process.
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# sounddevice 0.5.x sets .shape on NumPy arrays internally — deprecated in
# NumPy 2.5 and re-emitted for EVERY audio callback, which spams megabytes of
# identical warnings into the log within minutes. Harmless today; filter just
# this message instead of silencing DeprecationWarnings globally.
import warnings as _warnings
_warnings.filterwarnings("ignore", message=".*Setting the shape on a NumPy array.*")

import asyncio
import concurrent.futures

# ── Portable model cache (pendrive mode) ──────────────────────────────────────
# Whisper (STT), Kokoro (TTS) and the wake-word models download into the
# HuggingFace cache, which defaults to ~/.cache/huggingface on the HOST — that
# both leaks ~400 MB onto a stranger's PC and breaks the promise that models
# travel on the stick (PORTABLE.md). Pointing HF_HOME at jarvis-home before any
# of those modules load redirects every HF download into the drive. The user's
# own JARVIS_HOME / HF_HOME always win; without JARVIS_HOME nothing changes.
import os as _os
if _os.environ.get("JARVIS_HOME", "").strip() and not _os.environ.get("HF_HOME", "").strip():
    from pathlib import Path as _Path
    _hf_home = _Path(_os.environ["JARVIS_HOME"].strip()).expanduser().resolve() / "hf-cache"
    _hf_home.mkdir(parents=True, exist_ok=True)
    _os.environ["HF_HOME"] = str(_hf_home)
    _os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# ── Agent fleet gates: opt-OUT (2026-10-14) ──────────────────────────────────
# The orchestrator's factory registers a STUB placeholder for every agent
# whose JARVIS_*_AGENT env gate is not exactly "on" — with nothing set, the
# RIO Agent Center showed every implemented agent OFFLINE ("registered
# placeholder — set JARVIS_X_AGENT=on…"). The real implementations ship in
# this repo and all import cleanly, so the fleet now defaults to ENABLED:
# each gate defaults to "on" unless you explicitly set it (off/0/false
# returns that one agent to its stub). Note: chat ROUTING stays in the
# orchestrator's shadow mode (JARVIS_ORCHESTRATOR), untouched by this.
for _agent_gate in (
    "JARVIS_CODING_AGENT", "JARVIS_RESEARCH_AGENT", "JARVIS_TRADING_AGENT",
    "JARVIS_RAG_AGENT", "JARVIS_SYSTEM_AGENT", "JARVIS_EMAIL_AGENT",
    "JARVIS_CALENDAR_AGENT", "JARVIS_HEALTH_AGENT", "JARVIS_BRIEFING_AGENT",
    "JARVIS_MEAL_AGENT", "JARVIS_TRAVEL_AGENT", "JARVIS_FINANCE_AGENT",
    "JARVIS_STUDY_AGENT", "JARVIS_METAQUEST_AGENT", "JARVIS_PLAN_AGENT",
    "JARVIS_LEAD_AGENT", "JARVIS_LEGAL_AGENT", "JARVIS_MARKETING_AGENT",
    "JARVIS_SELF_EVAL_AGENT", "JARVIS_SQL_AGENT", "JARVIS_NEWS_AGENT",
    "JARVIS_REPLICA_AGENT",
):
    _os.environ.setdefault(_agent_gate, "on")

# ── Python 3.10 compatibility: asyncio.TaskGroup & exception groups ──────────
# asyncio.TaskGroup and the (Base)ExceptionGroup builtins are Python 3.11+
# features; the deployment machine runs the 3.10 system Python. The shims below
# reproduce the TaskGroup behavior the session loop depends on. On a 3.11+
# interpreter this whole block is skipped and the stdlib originals are kept.
if _sys.version_info < (3, 11):

    class BaseExceptionGroup(BaseException):
        """3.10 shim for the 3.11+ builtin: a group of exceptions with mixed
        base types (e.g. an Exception alongside a CancelledError)."""

        def __new__(cls, message, excs):
            if all(isinstance(e, Exception) for e in excs):
                cls = ExceptionGroup
            return super().__new__(cls, message, excs)

        def __init__(self, message, excs):
            super().__init__(message)
            self.message    = message
            self.exceptions = tuple(excs)

        def split(self, predicate):
            match, rest = [], []
            for e in self.exceptions:
                (match if predicate(e) else rest).append(e)
            first  = type(self)(self.message, match) if match else None
            second = type(self)(self.message, rest)  if rest  else None
            return first, second

        def subgroup(self, condition):
            if condition(self):
                return self
            match = [e for e in self.exceptions if isinstance(e, condition)]
            return type(self)(self.message, match) if match else None

        def derive(self, excs):
            return type(self)(self.message, excs)

    class ExceptionGroup(BaseExceptionGroup, Exception):
        """3.10 shim: a group whose members are all regular Exceptions."""

    def _taskgroup_on_done(tg, task):
        """Done-callback mirroring 3.11 TaskGroup._on_task_done: record the
        error (if any); on the first failure cancel all sibling tasks."""
        if task.cancelled():
            if not tg._aborting:
                # A child cancelled from outside aborts the whole group, like
                # the real TaskGroup: unwind siblings, then re-raise CancelledError.
                tg._parent_cancel_requested = True
                tg._aborting = True
                for t in tg._tasks:
                    if t is not task and not t.done():
                        t.cancel()
            return
        child_exc = task.exception()
        if child_exc is None:
            return
        tg._errors.append(child_exc)
        if not tg._aborting:
            tg._aborting = True
            for t in tg._tasks:
                if t is not task and not t.done():
                    t.cancel()

    class _TaskGroupShim:
        """Minimal asyncio.TaskGroup for Python 3.10.

        Faithful to the behaviors the session loop relies on:
          • a failing task cancels all sibling tasks;
          • multiple failures surface together as an (Exception|Base)ExceptionGroup;
          • a single failure propagates AS-IS, unwrapped, so
            _is_reconnect_signal() sees the original _ReconnectSignal;
          • an external cancellation of the parent task cancels the children
            and re-raises CancelledError in the parent.
        """

        def __init__(self):
            self._entered   = False
            self._exited    = False
            self._aborting  = False
            self._errors    = []
            self._tasks     = []
            self._parent_cancel_requested = False

        async def __aenter__(self):
            if self._entered:
                raise RuntimeError("TaskGroup is already active")
            self._entered = True
            return self

        def create_task(self, coro, *, name=None, context=None):
            # context= accepted for signature parity with 3.11+; unused here.
            if not self._entered:
                raise RuntimeError("TaskGroup has not been entered")
            if self._exited:
                raise RuntimeError("TaskGroup is finished")
            task = asyncio.get_running_loop().create_task(coro, name=name)
            task.add_done_callback(lambda t, tg=self: _taskgroup_on_done(tg, t))
            self._tasks.append(task)
            return task

        async def __aexit__(self, et, exc, tb):
            assert self._entered and not self._exited
            self._exited = True

            body_cancelled = isinstance(exc, asyncio.CancelledError)

            if exc is not None and not self._aborting:
                # The body itself failed — cancel all children, like 3.11 does.
                self._aborting = True
                for t in self._tasks:
                    if not t.done():
                        t.cancel()

            # Wait for every child to finish. If our own await is cancelled
            # again mid-wait, re-cancel the children and keep waiting — they
            # must not be abandoned.
            cancelled_again = False
            while True:
                pending = [t for t in self._tasks if not t.done()]
                if not pending:
                    break
                try:
                    await asyncio.wait(pending)
                except asyncio.CancelledError:
                    cancelled_again = True
                    self._aborting = True
                    for t in self._tasks:
                        if not t.done():
                            t.cancel()

            # Fold the body's own error into the collected set.
            if exc is not None and not body_cancelled:
                if isinstance(exc, BaseExceptionGroup):
                    self._errors.extend(exc.exceptions)
                else:
                    self._errors.append(exc)

            if self._errors:
                # A single failure propagates unwrapped, like the 3.11+ TaskGroup.
                if len(self._errors) == 1:
                    raise self._errors[0]
                base = (ExceptionGroup
                        if all(isinstance(e, Exception) for e in self._errors)
                        else BaseExceptionGroup)
                raise base("unhandled errors in a TaskGroup", self._errors)

            if body_cancelled or self._parent_cancel_requested or cancelled_again:
                raise asyncio.CancelledError()
            return None

    asyncio.TaskGroup = _TaskGroupShim

import itertools
import re
import threading
import time
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types

# ── Live API wire tap (opt-in via JARVIS_WIRE_TAP=1) ─────────────────────────
# Logs the raw connect handshake (setup TX summary, first RX, close frames)
# so production's rejected connects can be diffed byte-for-byte against the
# diag harness's accepted ones. Zero cost when disabled.
import os as _os_wiretap
if _os_wiretap.environ.get("JARVIS_WIRE_TAP"):
    import google.genai.live as _glive

    class _WireTap:
        def __init__(self, ws):
            self._ws = ws

        def __getattr__(self, name):
            return getattr(self._ws, name)

        async def send(self, data):
            try:
                import json as _json
                _j = _json.loads(data)
                _setup = _j.get("setup", {})
                _sr = _setup.get("sessionResumption", {})
                # Only setups carry signal; skip per-frame audio/content TX.
                if _setup:
                    print(f"[WIRE] TX setup keys={sorted(_setup.keys())} "
                          f"sessionResumption={_json.dumps(_sr)[:120]}", flush=True)
            except Exception:
                print(f"[WIRE] TX {str(data)[:120]}", flush=True)
            return await self._ws.send(data)

        async def recv(self, *a, **k):
            _r = await self._ws.recv(*a, **k)
            _s = str(_r)
            # Skip the per-frame audio/content firehose — keep protocol events.
            if '"inlineData"' not in _s and '"serverContent": {}' not in _s \
                    and 'modelTurn' not in _s:
                print(f"[WIRE] RX {_s[:160]}", flush=True)
            return _r

        async def __aenter__(self):
            await self._ws.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._ws.__aexit__(*exc)

    class _WireTapCtx:
        def __init__(self, cm):
            self._cm = cm

        async def __aenter__(self):
            return _WireTap(await self._cm.__aenter__())

        async def __aexit__(self, *exc):
            return await self._cm.__aexit__(*exc)

    _orig_ws_connect = _glive.ws_connect

    def _tapped_ws_connect(uri, **kw):
        print(f"[WIRE] connect {str(uri)[:120]}", flush=True)
        return _WireTapCtx(_orig_ws_connect(uri, **kw))

    _glive.ws_connect = _tapped_ws_connect
# ── end wire tap ──────────────────────────────────────────────────────────────

from ui import JarvisUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary, pop_last_session, was_session_spoken,
    _mark_session_spoken,
    search_memory, set_trim_notifier,
)
from memory.conversation_journal import (
    append_turn, search_journal, maybe_evolve, set_journal_notifier,
    set_current_conversation as _journal_set_conv,
)
from core.response_state import (
    tracker as _resp_tracker,
    RECEIVED as _RS_RECEIVED, PROCESSING as _RS_PROCESSING,
    GENERATING as _RS_GENERATING, COMPLETED as _RS_COMPLETED,
    FAILED as _RS_FAILED, CANCELLED as _RS_CANCELLED,
)

# The file-backed tools (open_app, web_search, browser_control, …) are no longer
# imported or declared here — they self-describe via a TOOL dict in their own
# actions/*.py file and are auto-discovered by core.action_loader at startup.
# Only tools that are tied to live-session state stay inline in this file
# (screen_process, close_camera, save_memory, manage_monitor, shutdown_jarvis,
# system_status).
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from actions.camera_watch       import CameraWatcher
from actions.web_search        import _news as _fetch_news_sync
from actions._google_auth      import briefing_context as _fetch_day_ahead
from memory.config_manager import (
    get_brief_enabled, get_voice, get_wake_word_enabled, save_wake_word_enabled,    get_input_device, get_output_device,
    get_push_to_talk_enabled, get_thinking_enabled,
)
from core.plugin_loader        import discover_plugins
from core                      import undo as undo_stack
from core                      import confirm as confirm_gate
from core                      import audio_devices
from core.action_loader        import discover_actions
from core.echo                 import EchoGuard
from core.viseme               import VisemeStream
from core.wake_word            import (
    WakeWordDetector, is_ready as wake_is_ready, install_and_download as wake_install,
)

# How long the assistant stays awake with no user speech before it auto-sleeps
# again (wake-word mode only).
WAKE_SLEEP_TIMEOUT = 120.0   # seconds (2 minutes)

def get_base_dir():
    # JARVIS_HOME relocates the whole app for pendrive mode — see
    # memory/memory_manager.get_base_dir for the full contract.
    import os
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
# Live model is configurable so a server-side model incident (2026-09-30:
# gemini-3.1-flash-live crashed every audio session with 1011 and never
# transcribed speech; text turns worked) can be rode out by pointing at a
# healthy model without a code change. Set "live_model" in
# config/api_keys.json; fall back to the historical default when absent.
_DEFAULT_LIVE_MODEL = "models/gemini-3.1-flash-live-preview"


def _get_live_model() -> str:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as _f:
            _m = (json.load(_f) or {}).get("live_model")
        if isinstance(_m, str) and _m.strip():
            _m = _m.strip()
            return _m if _m.startswith("models/") else f"models/{_m}"
    except Exception:
        pass
    return _DEFAULT_LIVE_MODEL


LIVE_MODEL          = _get_live_model()
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000 
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

# Speaker stream latency target (seconds). The PortAudio default ("low", roughly
# 40 ms on ALSA) underran audibly whenever the writer thread was briefly delayed
# — the ring buffer had no headroom. 150 ms removed most of them; 250 ms got a
# full session down to just 2 — both while JARVIS spoke AND drove input
# automation at the same time (screen capture + uinput work on the same box).
# 350 ms is sized for exactly that worst case: the voice rides out concurrent
# tool execution without audible clicks. Costs ~100 ms more start/stop latency
# and a still softer interrupt cutoff — the trade was accepted deliberately.
# Bounded on purpose: a bare "high" hint can map to hundreds of ms on some
# devices.
SPEAKER_LATENCY_S   = 0.35

# RMS below which 16-bit PCM is treated as room silence; above _LEVEL_FULL it
# reads as a full-height waveform. Tuned so ordinary speech lands mid-range and
# the bars still move for a quiet talker — language- and device-independent.
_LEVEL_FLOOR = 60.0
_LEVEL_FULL  = 2600.0


def _pcm_level(samples) -> float:
    """Map a block of int16 PCM samples to a 0.0–1.0 loudness level for the HUD
    waveform. Returns 0.0 on empty/invalid input so it can never raise."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    return min(1.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR))


# ── Viseme extraction (Mark-LIV) ─────────────────────────────────────────────
# Reads the *shape* of each 20 ms slice straight from the spectrum of the audio
# being played, so no transcript, no forced alignment and no language
# assumption: it works the same for Turkish and English. Openness tracks the
# first formant (jaw drop), width the second (spread vs rounded lips).
_TAIL_MARGIN = 0.25    # extra time beyond the device's output latency before the mic is trusted again

_VIS_WIN = 1024        # ~43 ms analysis window at 24 kHz: enough for formants
_VIS_HOP = 480         # 20 ms between frames, i.e. 50 shapes a second

# Delay from handing the first bytes of a reply to an already-running output
# stream to hearing them: one callback period, plus whatever the DAC adds.
_FIRST_SOUND = CHUNK_SIZE / RECEIVE_SAMPLE_RATE      # ~43 ms
# How far past the device's own buffer the mouth's timeline may drift before it
# is re-anchored. The buffer is the hard limit on how much audio can be queued
# ahead, so anything beyond it plus a margin for clock error is impossible.
_CURSOR_SLACK = 0.15


def _pcm_visemes(samples, sr: int = 24000):
    """Slice a PCM block into (level, openness, width) frames, one per 20 ms.

    Returns [] on anything unexpected — the mouth falls back to loudness-only
    articulation rather than the caller having to handle an error.
    """
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size < _VIS_WIN:
            return []
        win = np.hanning(_VIS_WIN).astype(np.float32)
        freqs = np.fft.rfftfreq(_VIS_WIN, 1.0 / sr)
        b_f1_lo = (freqs >= 150) & (freqs < 450)     # F1 of close vowels
        b_f1_hi = (freqs >= 450) & (freqs < 1100)    # F1 of open vowels
        b_f2_bk = (freqs >= 600) & (freqs < 1300)    # F2 of rounded vowels
        b_f2_fr = (freqs >= 1700) & (freqs < 3200)   # F2 of spread vowels
        b_hiss = (freqs >= 3800) & (freqs < 8000)    # fricatives

        # One frame per hop across the *whole* block, so batches line up with
        # the audio exactly and no schedule time is lost.
        out = []
        for start in range(0, x.size, _VIS_HOP):
            # The level gates closures, so it is measured over exactly this
            # 20 ms and never looks ahead.
            level = _pcm_level(x[start:start + _VIS_HOP])
            seg = x[start:start + _VIS_WIN]
            if seg.size < _VIS_WIN:
                seg = np.concatenate([seg, np.zeros(_VIS_WIN - seg.size,
                                                    dtype=np.float32)])
            if level <= 0.0:
                out.append((0.0, 0.0, 0.0))
                continue
            mag = np.abs(np.fft.rfft((seg - seg.mean()) * win))
            f1l, f1h = float(mag[b_f1_lo].sum()), float(mag[b_f1_hi].sum())
            f2b, f2f = float(mag[b_f2_bk].sum()), float(mag[b_f2_fr].sum())
            hiss = float(mag[b_hiss].sum())

            openness = f1h / (f1l + f1h + 1e-6)
            width = (f2f - f2b) / (f2f + f2b + 1e-6)
            # A wide-open jaw physically cannot purse; /a/ has low enough F2 to
            # read as "rounded" on the bands alone — damp width by openness.
            width *= (1.0 - openness) ** 0.8
            # Fricatives are formed with a nearly closed mouth.
            h = hiss / (f1l + f1h + f2b + f2f + hiss + 1e-6)
            openness *= 1.0 - 0.65 * min(1.0, h * 2.5)
            out.append((level,
                        float(min(1.0, max(0.0, openness))),
                        float(min(1.0, max(-1.0, width)))))
        return out
    except Exception:
        return []


_gemini_key_index = 0   # pinned per process; advanced only on quota/invalid-key
# (2026-10-01: this used to be itertools.count() round-robin — every client
# build picked the NEXT key. A Live resumption handle is bound to the project
# that issued it, so each reconnect presented the handle to a different
# project and the server rejected it with `1008 None. Session does not belong
# to this project` → "starting a fresh session" → every rotation wiped the
# conversation. Rotating only on failure keeps the handle and its project
# together; a 429'd key is still skipped via _rotate_api_key below.)


def _load_gemini_keys() -> list[str]:
    """Primary Gemini key plus any extras in 'gemini_api_keys', deduplicated,
    order-stable."""
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    keys = [cfg.get("gemini_api_key") or ""]
    extra = cfg.get("gemini_api_keys") or []
    if isinstance(extra, str):
        extra = [extra]
    keys += [k for k in extra if isinstance(k, str) and k.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for k in (k.strip() for k in keys):
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _get_api_key() -> str:
    """The CURRENT Gemini key — pinned for the life of the process.

    A resumption handle only resumes on the project that issued it, so the
    Live session must rebuild with the SAME key every time. Rotation happens
    exclusively through _rotate_api_key(), on a 429 (quota exhausted) or an
    invalid-key error — never implicitly.
    (Text-generation calls additionally fail over via core.llm_router.)"""
    global _gemini_key_index
    keys = _load_gemini_keys()
    if not keys:
        return ""
    return keys[_gemini_key_index % len(keys)]


def _rotate_api_key() -> bool:
    """Advance to the next configured Gemini key. Returns True when the index
    actually moved (multiple keys configured); False when there is only one —
    the caller then falls back to whatever it does without an alternate."""
    global _gemini_key_index
    n = len(_load_gemini_keys())
    if n <= 1:
        return False
    _gemini_key_index += 1
    return True


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)


def _is_quota_error(e: BaseException) -> bool:
    """True when `e` (or anything it wraps — TaskGroup bundles children) is a
    quota/limit error: HTTP 429, RESOURCE_EXHAUSTED, or rate/budget text.
    The shapes seen live: '429 You exceeded your current quota',
    'RESOURCE_EXHAUSTED', and websockets close 1013/1008 with quota reason
    text. String-keyed like every other classifier in this file, so a change
    in google-genai's error formatting degrades to the generic retry path
    instead of crashing."""
    seen: set[int] = set()
    stack: list[BaseException] = [e]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        s = str(cur)
        if ("429" in s
                or "RESOURCE_EXHAUSTED" in s
                or "rate limit" in s.lower()
                or "quota" in s.lower()):
            return True
        for nxt in (getattr(cur, "__cause__", None), getattr(cur, "__context__", None)):
            if isinstance(nxt, BaseException):
                stack.append(nxt)
        for sub in getattr(cur, "exceptions", ()) or ():
            if isinstance(sub, BaseException):
                stack.append(sub)
    return False


def _fmt_time_left(td) -> str:
    """Format a GoAway time_left (datetime.timedelta or None) as a short
    human string like '2h05' or '45s'. Returns '' when absent/unparsable."""
    if td is None:
        return ""
    try:
        secs = int(td.total_seconds())
    except Exception:
        return ""
    if secs <= 0:
        return ""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}"
    if m:
        return f"{m}m{s:02d}"
    return f"{s}s"

# The server force-closes a Live session shortly after GoAway (WebSocket close
# code 1008, "policy violation") unless the client disconnects first. The
# grace window is short — never rely on a bare `time_left` of hours being
# trustworthy. If JARVIS is mid-sentence the reconnect is deferred to
# turn_complete; this deadline exists so a long turn can't outlive the grace
# and turn the polite disconnect into a socket kill.
GOAWAY_GRACE_S = 120.0

# Dashboard command loop bounds. Module-level so tests can shrink them;
# production values: wait up to 8 s for a (re)connecting session before
# requeuing, hold the queue for one answer turn at most 120 s, and requeue a
# session-less command at most 3 times before dropping it.
_CMD_SESSION_WAIT_S = 8.0
_CMD_TURN_WAIT_S    = 120.0
_CMD_MAX_REQUEUES   = 3

def _is_session_limit_close(e: BaseException) -> bool:
    """True if `e` is the WebSocket 1008 close the server uses to enforce its
    GoAway window — an expected event that must not look like an error.

    The reason text is NOT stable. Two live shapes so far, same cure (calm
    keep-context rebuild via the recv/send safety nets):
      1008 (policy violation) The operation was aborted.
      1008 None. The operation was aborted.   (2026-09-28: killed mid-turn,
      BEFORE the client's deferred reconnect deadline — the server's real
      grace can be shorter than GOAWAY_GRACE_S, so this safety net, not the
      deadline watcher, is what must own the recovery.)
    The one 1008 needing a DIFFERENT cure is a dead resumption handle:
      1008 None. BidiGenerateContent session not found
    — that one must stay an error so the run loop's handle-rejection branch
    drops the handle instead of replaying it forever (2026-09-27 fix)."""
    s = str(e)
    if "1008" not in s:
        return False
    if "session not found" in s.lower():
        return False
    return True

def _is_transient_server_error(e: BaseException) -> bool:
    """True for a Google-side 1011 "Internal error encountered" websocket
    close. Observed 2026-10-13 mid-turn during a long tool response; the run
    loop's 3 s reconnect + the resumption handle restored the conversation
    untouched, so this is a hiccup, not a code failure. It gets ONE calm line
    instead of a traceback (the recovery is identical either way)."""
    code = getattr(e, "code", None)
    try:
        return int(code) == 1011
    except (TypeError, ValueError):
        return False


def _is_invalid_argument_close(e: BaseException) -> bool:
    """True for the WebSocket 1007 "Request contains an invalid argument"
    close — a REQUEST problem (bad field or oversized payload), never an
    API-key problem and never a dead session.

    2026-10-14: arrived right after a send_tool_response carrying the
    trading_strategy plugin's output. The old run-loop branch matched bare
    "1007" as "API key invalid" and wedged JARVIS on a reconfiguration
    prompt mid-conversation. The resumption handle carries the conversation
    across a rebuild, so the cure is a calm reconnect with context.
    Genuine key rejections carry "api key"/"unauthenticated" in the reason
    and stay with the key branch."""
    s = str(e)
    if "1007" not in s:
        return False
    low = s.lower()
    if "api key" in low or "unauthenticated" in low or "permission" in low:
        return False
    return True


def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()


# Tool results ride the realtime session inside send_tool_response. A 2026-10-14
# crash started with a 1007 "Request contains an invalid argument" close right
# after a trading_strategy response was sent back: the plugin's output is a
# multi-KB strategy dump that can carry vendor-decoded control characters or
# lone UTF-16 surrogates, any of which can make the payload unacceptable to the
# Live API. Harden at the one boundary every tool passes through.
_TOOL_RESULT_MAX_CHARS = 20000

def _sanitize_tool_result(result) -> str:
    """Make a tool result safe to send as a Live API FunctionResponse.

    1. Repair lone UTF-16 surrogates (bytes decoded by vendor SDKs can carry
       them; they cannot be encoded to a wire-safe JSON string).
    2. Strip control characters (the transcript cleaner's rules).
    3. Cap the size — the model reads the head of the payload; a wholesale
       strategy dump has no business on the realtime socket.
    """
    s = str(result)
    if not s:
        return s
    try:
        # surrogatepass round-trip: intact text survives unchanged, lone
        # surrogates fail the strict decode and fall back to UTF-8 'replace'.
        s.encode("utf-16", "surrogatepass").decode("utf-16")
    except (UnicodeDecodeError, UnicodeEncodeError):
        s = s.encode("utf-8", "replace").decode("utf-8")
    s = _CTRL_RE.sub("", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    if len(s) > _TOOL_RESULT_MAX_CHARS:
        s = (s[:_TOOL_RESULT_MAX_CHARS]
             + f"\n[...tool output truncated at {_TOOL_RESULT_MAX_CHARS} chars]")
    return s


# Overlap-aware assembly for the Live API output transcription.
# The transcription stream occasionally RESTATES text it already emitted
# (a restart / re-sync chunk). Appending naively produced verbatim doubled
# sentences — 11 confirmed A+A rows in memory/conversations/2026-10.jsonl
# (e.g. 2026-10-13T20:21:35, "नमस्ते बॉस, कैसे हैं आप? नमस्ते बॉस, कैसे हैं आप?").
# The old guard only dropped a chunk identical to the immediately PRECEDING
# chunk, so any restart re-emitting more than the last chunk still doubled
# the turn (journal + UI + Hermes all carried the double).
#
# Trade-off, stated plainly: a DELIBERATE verbatim repeat of a ≥6-char
# phrase within one turn is also merged away now. Empirically the artifact
# (doubles) is common and deliberate verbatim repeats are not observed;
# audio is unaffected either way — this layer only assembles the text.
_MERGE_MIN_CHARS = 6   # below this a match is treated as new text — a false
                       # merge would EAT real words, which is worse than a
                       # rare duplicate, so the rules stay conservative

def _collapse_doubled(text: str) -> str:
    """If text is exactly a verbatim double (A + whitespace + A), keep A.

    Catches the observed end state of a restated transcription in one step.
    Only exact halves (whitespace-normalized) collapse; split candidates are
    the middle ±1 because the join seam contributes 0 or 1 spaces.
    """
    t = re.sub(r"\s+", " ", text).strip()
    n = len(t)
    if n < 2 * _MERGE_MIN_CHARS:
        return text
    for i in (n // 2, n // 2 + 1, n // 2 - 1):
        if i < _MERGE_MIN_CHARS or (n - i) < _MERGE_MIN_CHARS:
            continue
        if t[:i].strip() == t[i:].strip():
            return t[:i].strip()
    return text

def _merge_transcript(prev: str, chunk: str) -> str:
    """Merge one transcription chunk into the accumulated turn text.

    Rules (conservative):
      0. empty chunk → unchanged
      1. chunk already contained verbatim in the accumulated text AND
         ≥ _MERGE_MIN_CHARS → unchanged (covers whole-turn restatements and
         piece-wise restarts; the caller's consecutive guard covers shorts)
      2. chunk begins with a ≥ _MERGE_MIN_CHARS exact tail of the
         accumulated text → append only the remainder (overlap stitch)
      3. otherwise → append as new text
      Every append path then runs _collapse_doubled as the final safety net.
    """
    if not chunk:
        return prev
    if not prev:
        return chunk
    if len(chunk) >= _MERGE_MIN_CHARS and chunk in prev:
        return prev
    max_ov = min(len(prev), len(chunk))
    for ov in range(max_ov, _MERGE_MIN_CHARS - 1, -1):
        if prev[-ov:] == chunk[:ov]:
            return _collapse_doubled(prev + " " + chunk[ov:])
    return _collapse_doubled(prev + " " + chunk)

TOOL_DECLARATIONS = [
    # ── Inline tools ─────────────────────────────────────────────────────────
    # These stay here (rather than in an actions/*.py TOOL dict) because their
    # handling is woven into live-session state — vision capture/injection,
    # camera stream, memory writes, the monitor engine, and shutdown. All other
    # tools live in their own action file and are auto-discovered by
    # core.action_loader (see JarvisLive.__init__).
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when the user says (in ANY language): close camera, stop camera, "
            "turn off camera, that's creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background monitoring topics. "
            "JARVIS checks these topics once a day and alerts the user when there is a new development. "
            "Use 'add' when the user says 'monitor X', 'track X', 'follow X'. "
            "Use 'remove' when the user says 'stop monitoring X'. "
            "Use 'list' when the user asks what is being monitored. "
            "Do NOT add crypto, financial, or trading topics."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Topic to monitor or stop monitoring (e.g. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Look up a fact you have stored about the user but which is NOT in "
            "the memory block of your system prompt. "
            "The prompt lists the keys it did not have room for under "
            "'[ALSO REMEMBERED]' — if the user asks about anything named there, "
            "call this FIRST. "
            "Also call it before saying you do not know something personal, and "
            "when the user asks what you remember about them (leave query empty "
            "for everything). "
            "This is a local file search: it is instant and costs nothing."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Keyword to search for — a name, a topic, a category "
                        "(e.g. 'ayse', 'coffee', 'projects'). "
                        "Leave empty to list everything stored."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "undo",
        "description": (
            "Reverse the last change YOU made to this computer — a file you "
            "moved, renamed, created or wrote, or a setting you changed such as "
            "volume, brightness, dark mode or WiFi. "
            "Call this whenever the user says undo, revert, take it back, put it "
            "back, cancel that, or tells you that you did the wrong thing, in ANY "
            "language. "
            "Use action='list' when they ask what can be undone. "
            "This only covers your own actions — it is not the Ctrl+Z of whatever "
            "application is on screen (that is computer_settings with action 'undo')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "undo (default) — reverse the last change | list — show what can be undone",
                },
            },
            "required": [],
        },
    },
]

class _ReconnectSignal(Exception):
    """Raised inside the session TaskGroup to force a clean, voluntary reconnect
    (e.g. the user picked a new voice — the voice is fixed at connect time, so
    the session must be rebuilt).

    Carries `keep_context`: True for an ordinary rebuild, where the stored
    resumption handle is replayed and the conversation continues; False when the
    new session must genuinely start clean (see the voice-change note in
    _on_voice_change)."""

    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def _unwrapped_sig(exc: BaseException, _seen=None):
    """Extract the _ReconnectSignal from `exc`, unwrapping every container
    shape a TaskGroup can produce: an (Exception|Base)ExceptionGroup (3.11+
    builtin or the 3.10 shim), or a CancelledError whose __cause__/__context__
    carries the signal from a child task that was cancelled mid-raise.
    Returns None if there is no reconnect signal inside."""
    if _seen is None:
        _seen = set()
    if id(exc) in _seen:
        return None
    _seen.add(id(exc))
    if isinstance(exc, _ReconnectSignal):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            sig = _unwrapped_sig(sub, _seen)
            if sig is not None:
                return sig
        return None
    if isinstance(exc, asyncio.CancelledError):
        cause = exc.__cause__ or exc.__context__
        if cause is not None:
            return _unwrapped_sig(cause, _seen)
    return None


def _is_reconnect_signal(exc: BaseException) -> bool:
    """True if `exc` is a _ReconnectSignal, or a(n) (Base)ExceptionGroup that
    wraps one — TaskGroup bundles child exceptions into a group."""
    return _unwrapped_sig(exc) is not None


def _is_executor_shutdown(exc: BaseException) -> bool:
    """True if `exc` (or anything it wraps) is the interpreter-teardown
    executor error — 'cannot schedule new futures after (interpreter)
    shutdown'.

    str() on the outer exception is NOT enough: a single-part
    BaseExceptionGroup raised by asyncio/TaskGroup str()s as just
    'unhandled errors in a TaskGroup' — the RuntimeError's text lives in
    sub-exception. That exact miss kept a daemon thread spinning in the
    reconnect loop at 3–6s intervals after the Qt window was gone
    (2026-09-30)."""
    seen = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        msg = str(cur)
        if ("cannot schedule new futures after shutdown" in msg
                or "cannot schedule new futures after interpreter shutdown" in msg):
            return True
        cause = getattr(cur, "__cause__", None)
        ctx = getattr(cur, "__context__", None)
        for nxt in (cause, ctx):
            if isinstance(nxt, BaseException):
                stack.append(nxt)
        # (Base)ExceptionGroup members (Python 3.11+)
        for sub in getattr(cur, "exceptions", ()) or ():
            if isinstance(sub, BaseException):
                stack.append(sub)
    return False


def _keep_context_of(exc: BaseException) -> bool:
    """Read `keep_context` off a reconnect signal, unwrapping the group the
    TaskGroup put it in. Defaults to True: an unexpected shape must not silently
    wipe the conversation."""
    sig = _unwrapped_sig(exc)
    if sig is not None:
        return getattr(sig, "keep_context", True)
    return True


class JarvisLive:
    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self._asst_name     = "JARVI    S"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                     = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pc_speaker_ducked   = False   # True while phone mic is live; silences PC speakers
        self._pc_mic_paused       = False   # True while phone mic is live; PC mic stream stopped
        self._pc_mic_stream       = None    # sd.InputStream — paused/resumed by phone-mic state
        self._pc_mic_lock         = threading.Lock()
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        # Mark-LIV: transcript-driven mouth shapes for the avatar, echo guard,
        # push-to-talk. Fed from the receive loop as words arrive, drained by
        # the playback loop against the audio.
        self._visemes              = VisemeStream()
        self._last_mouth_tx        = 0.0        # RIO face WS throttle (mouth)
        self._ptt_enabled          = False
        self._ptt_held             = False
        self._ptt                  = None    # core.hotkey.PushToTalk
        self._out_level            = 0.0     # level of the audio being played right now
        self._echo                 = EchoGuard()
        # `stream.write()` returns when the buffer accepts the audio, not when
        # the speaker has finished, so sound is still in the room after the
        # speaking flag drops — the echo tail covers exactly that gap.
        self._out_latency          = 0.20    # seconds, replaced with the real value
        self._tail_until           = 0.0     # monotonic time the echo tail expires
        # Wall-clock time at which the audio written next will begin to sound.
        # The mouth is scheduled against this, never against "now": batches are
        # handed to the device far faster than they play.
        self._play_cursor          = 0.0
        self._goaway_pending       = False   # GoAway seen — reconnect once the turn finishes
        self._goaway_deadline      = 0.0     # monotonic time the GoAway grace expires
        self.ui.on_push_to_talk   = self.set_push_to_talk
        self.ui.ptt_hold          = self._on_ptt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_change   = self._on_voice_change     # voice picker → rebuild session
        self.ui.on_audio_device_change = self._on_audio_device_change
        self.ui.uptime_getter     = self.conversation_uptime   # HUD conversation clock
        self._reconnect_event: asyncio.Event | None = None
        self._reconnect_keep = True   # False → next rebuild drops the resumption handle

        # ── Conversation uptime ────────────────────────────────────────
        # Wall-clock start of the CURRENT conversation. Deliberately NOT reset
        # by a connection rotation: a GoAway rebuild replays the resumption
        # handle, so from the user's point of view it is the same conversation
        # that continues for hours across ~10-minute WebSockets. Only a
        # genuinely fresh session (no handle — see the connect path and the
        # handle-rejection branch) restarts the clock. None = no conversation
        # running yet this process.
        self._conversation_started: float | None = None

        # ── Session resumption ─────────────────────────────────────────
        # The server issues a resumption handle every few seconds and reissues
        # it as the conversation moves on. Before this, session_resumption was
        # switched ON in the config and the update was never read, so the handle
        # was thrown away and EVERY reconnect — a dropped packet, a voice change,
        # switching microphone — started an empty session. "Unlimited sessions"
        # leaked through exactly this hole.
        #
        # Deliberately in RAM only, never written to disk. Persisting it would
        # make a fresh launch continue yesterday's conversation, which sounds
        # appealing but breaks the session-summary flow: _save_session_summary
        # runs at shutdown and the morning briefing pops it the next day. A
        # conversation that never ends never produces a summary, and the
        # "yesterday we talked about…" line silently disappears.
        self._resume_handle: str | None = None
        # Diagnostics (2026-09-28 handle-reaping incident): last handle string
        # the server sent us, so the recv loop can log each DISTINCT update
        # exactly once instead of on every message.
        self._last_seen_handle: str | None = None
        # When a resumption handle is rejected (server-side session reaping —
        # upstream Live API bug, see scripts/diag_*), the fresh session gets
        # the recent conversation tail folded into its system instruction so
        # the reset is graceful instead of a full context wipe.
        self._carry_context: str | None = None
        self._turn_done_event: asyncio.Event | None = None
        # Hermes bridge state: which channel the current inbound message came
        # from ("telegram:12345") and whether a file rode along. Set by
        # _on_text_command when it sees an [INBOUND ...] tag; cleared when the
        # reply is delivered. Replies go through `hermes send`, reusing the
        # gateway's credentials — Jarvis never touches a chat API itself.
        self._inbound_channel: str | None = None
        self._inbound_had_file = False
        # Monotonic timestamp of when the inbound request was actually SENT to
        # the model. A turn that completes BEFORE this moment is an unrelated
        # (earlier) turn and must never be delivered to the messaging channel.
        self._inbound_req_ts = 0.0
        # Conversation tag for the RIO chat turn currently in flight (consumed
        # once, at the next turn_complete, by the journal — see _receive_audio).
        # Replaces the racy set-around-send of the module-global journal stamp,
        # which never survived long enough to reach the journal write.
        self._pending_conv = ""
        # Desktop HUD direct-path duplicate latch (double-Enter / double-click
        # on send): identical text within the window is dropped.
        self._last_direct_text: str | None = None
        self._last_direct_ts = 0.0
        # Per-request response lifecycle tracking (core/response_state.py).
        self._active_req_id = ""          # correlation id of the in-flight turn
        self._last_req_id  = ""           # last completed id (audio tracking)
        # Set at every turn_complete; the dashboard command loop waits on it
        # so one message is fully answered before the next is generated —
        # this is what keeps reply routing per-message instead of per-slot.
        self._dash_turn_done = asyncio.Event()
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary

        self._enhanced_live = True  # proactive audio; auto-disabled if the server rejects it

        _base_dir = Path(__file__).resolve().parent
        _inline_names = {t["name"] for t in TOOL_DECLARATIONS}

        # File-backed tools: every actions/*.py with a TOOL dict, discovered the
        # same way plugins are. Reserved names = the inline tools above, so an
        # action can never shadow one.
        self._action_registry = discover_actions(
            actions_dir=_base_dir / "actions",
            reserved_names=_inline_names,
            logger=lambda msg: print(f"[Actions] {msg}"),
        )

        # Plugins must not collide with either an inline tool or a discovered action.
        _core_names = _inline_names | self._action_registry.names()
        self._plugin_registry = discover_plugins(
            plugins_dir=_base_dir / "plugins",
            core_tool_names=_core_names,
            logger=lambda msg: (print(f"[Plugins] {msg}"), self.ui.write_log(f"SYS: {msg}")),
        )
        self.ui.get_plugins = self._plugin_registry.list_for_ui
        self.ui.get_plugin_settings = self._plugin_registry.settings_schemas  # ⚙ settings tab
        self.ui.request_say = self.plugin_say   # plugins: mid-task speech channel

        # ── Wake word ────────────────────────────────────────────────────────
        # _awake gates the mic (see _listen_audio) and the background speakers.
        # It is True whenever wake word is OFF, so default behaviour is unchanged.
        self._wake_enabled     = get_wake_word_enabled()
        self._awake            = not self._wake_enabled
        self._wake_detector: WakeWordDetector | None = None
        self._cam_watcher    = CameraWatcher(logger=lambda m: print(f"[CameraWatch] {m}"))
        self._wake_sleep_timeout = WAKE_SLEEP_TIMEOUT
        # UI control surface for the Wake Word settings section.
        self.ui.wake_is_ready    = wake_is_ready          # () -> bool
        self.ui.wake_get_state   = self._wake_state       # () -> dict
        self.ui.on_wake_toggle   = self._ui_wake_toggle   # (enable: bool) -> str
        self.ui.on_wake_manual   = self._ui_wake_manual   # () -> toggle awake/asleep
        self.ui.on_wake_install  = self._ui_wake_install  # () -> (ok, msg)

        # Mark-LIV: restore the saved push-to-talk preference. Doing it here
        # rather than in __init__ means the hotkey thread only exists once
        # there is a session to talk to.
        if get_push_to_talk_enabled():
            try:
                self.set_push_to_talk(True)
            except Exception as e:
                print(f"[JARVIS] ⚠ Push-to-talk unavailable: {e}")

    # ── Wake word: state machine ─────────────────────────────────────────────

    def _wake_state(self) -> dict:
        # A loaded, running detector is definitively ready; otherwise fall back
        # to the cheap on-disk model-file check (no Model construction).
        ready = bool(self._wake_detector and self._wake_detector.ready) or wake_is_ready()
        return {"enabled": self._wake_enabled, "awake": self._awake, "ready": ready}

    def _ensure_wake_detector(self) -> bool:
        """Load the detector once (model loads on first start). Idempotent."""
        if self._wake_detector is None:
            self._wake_detector = WakeWordDetector(
                on_detect=self._on_wake_detected,
                logger=lambda m: (print(f"[Wake] {m}"), self.ui.write_log(f"SYS: {m}")),
            )
        if not self._wake_detector.ready:
            return self._wake_detector.start()
        return True

    def _on_wake_detected(self) -> None:
        """Called from the detector thread when 'Hey Jarvis' is heard."""
        self.wake(reason="wake word")

    def wake(self, reason: str = "wake word") -> None:
        if self._awake:
            return
        self._awake = True
        print(f"[JARVIS] 👂 WAKE — {reason}")   # stdout proof: wake word fired
        self._last_user_speech = time.monotonic()   # start the auto-sleep clock now
        if not self.ui.muted:
            self._face_state("LISTENING")
        self.ui.write_log(f"SYS: Awake — {reason}.")

    def sleep(self, reason: str = "timeout") -> None:
        if not self._awake:
            return
        self._awake = False
        self.set_speaking(False)
        self._face_state("SLEEPING")
        self.ui.write_log(f"SYS: Sleeping — {reason}. Say 'Hey Jarvis' to wake me.")

    async def _run_sleep_watch(self) -> None:
        """Auto-sleep after the configured silence window (wake-word mode only)."""
        while True:
            await asyncio.sleep(5)
            if not self._wake_enabled or not self._awake:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue
            if (time.monotonic() - self._last_user_speech) > self._wake_sleep_timeout:
                self.sleep(reason="no speech for 2 minutes")

    # ── Nightly trading learner ───────────────────────────────────────────────

    async def _run_market_desk(self) -> None:
        """Market Intelligence Desk scheduler (plugins/_desk_engine.py): every
        5 minutes, execute any due ACTIVE routines from the desk's IST
        production schedule. Routines are born PAUSED — nothing runs until the
        user activates them (voice: 'market desk' → activate). The sweep runs
        in a worker thread, budget-capped so it can never flood the executor;
        all its LLM clients carry their own HTTP timeouts."""
        await asyncio.sleep(120)             # let startup (session, briefing) settle
        while True:
            try:
                from memory.config_manager import get_plugin_enabled
                if self.session and get_plugin_enabled("market_desk"):
                    from plugins._desk_engine import due_routines, run_schedule
                    if due_routines():
                        result = await asyncio.to_thread(run_schedule)
                        if result:
                            print(f"[MarketDesk] {result}")
            except Exception as e:
                print(f"[MarketDesk] ⚠️ scheduler tick failed: {e}")
            await asyncio.sleep(300)         # every 5 minutes

    async def _run_trading_learner(self) -> None:
        """Once per evening (configurable hour): run the trading plugin's
        evaluate action — score yesterday's signals against what happened and
        adapt rule weights — so the strategy improves even when the user never
        asks. The learning_enabled / learn_hour settings live with the plugin
        (⚙ → Plugin Settings → Trading Strategy).

        Spawned per session by the TaskGroup, so reconnects respawn it; the
        plugin's state file (last_run_date) makes the tick idempotent — a
        respawn can never double-step the weights. If the app was closed at
        the scheduled hour, the first tick after launch catches up."""
        await asyncio.sleep(90)          # let startup (session, briefing) settle
        while True:
            try:
                if self.session:
                    await asyncio.to_thread(self._trading_learn_tick)
            except Exception as e:
                print(f"[TradingLearn] ⚠️ tick failed: {e}")
            await asyncio.sleep(1800)    # check every 30 minutes

    def _trading_learn_tick(self) -> None:
        """Sync body — runs in a worker thread. Due-check, run, record."""
        try:
            import plugins.trading_strategy as ts   # loader registered it
        except Exception:
            return                                  # plugin missing/rejected
        from memory.config_manager import get_plugin_enabled
        if not get_plugin_enabled("trading_strategy"):
            return
        settings = ts._settings()
        if not settings.get("learning_enabled", True):
            return
        try:
            learn_hour = int(float(settings.get("learn_hour", 21)))
        except (TypeError, ValueError):
            learn_hour = 21
        learn_hour = max(0, min(23, learn_hour))

        state = ts._load_learn_state()
        now = datetime.now()
        if not ts._due_now(now, state, learn_hour):
            return

        # Through the registry: same dispatch path as a voice command —
        # enabled-check, error capture, everything. player=self.ui lets the
        # plugin reach the HUD (e.g. data-source outage warnings).
        result = self._plugin_registry.run("trading_strategy", {"action": "evaluate"},
                                           player=self.ui)
        ts._save_learn_state({
            "last_run_date": now.strftime("%Y-%m-%d"),
            "last_run_at":   now.isoformat(timespec="seconds"),
            "result":        str(result)[:300],
        })
        self.ui.write_log(f"SYS: Nightly strategy learning — {str(result)[:140]}")

    # ── Wake word: UI callbacks (called from the Qt thread) ──────────────────

    def _ui_wake_toggle(self, enable: bool) -> str:
        """Enable/disable wake word from the settings UI. Returns a status token:
        'enabled' | 'disabled' | 'need_download'."""
        if enable:
            if not wake_is_ready():
                return "need_download"
            self._wake_enabled = True
            save_wake_word_enabled(True)
            self._ensure_wake_detector()
            self.sleep(reason="wake word enabled")
            return "enabled"
        else:
            self._wake_enabled = False
            save_wake_word_enabled(False)
            self.wake(reason="wake word disabled")
            return "disabled"

    def _ui_wake_manual(self) -> None:
        """Manual sleep/wake button in the UI."""
        if not self._wake_enabled:
            return
        if self._awake:
            self.sleep(reason="you tapped sleep")
        else:
            self.wake(reason="you tapped wake")

    def _ui_wake_install(self) -> tuple[bool, str]:
        """Download openwakeword + the model (runs in a UI worker thread)."""
        return wake_install(logger=lambda m: self.ui.write_log(f"SYS: {m}"))

    def plugin_say(self, instruction: str) -> None:
        """
        Thread-safe speech channel for plugins: lets a plugin ask JARVIS to
        say something short WHILE its run() is still executing (plugins block
        their executor thread, so they can't speak through the tool response
        until they finish). The instruction is injected into the Live session
        exactly like a proactive check-in; Gemini phrases it naturally in the
        user's language. Silently a no-op when no session is connected.
        """
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            return

        async def _say():
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": instruction}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[PluginSay] {e}")

        try:
            asyncio.run_coroutine_threadsafe(_say(), loop)
        except Exception as e:
            print(f"[PluginSay] {e}")

    def request_reconnect(self, keep_context: bool = True, reason: str = ""):
        """Thread-safe: ask the run loop to tear down and rebuild the Live
        session. Called from the Qt thread. No-op until the async loop and
        reconnect event exist.

        `keep_context=False` drops the resumption handle so the new session
        starts empty — only for changes the server cannot apply to a resumed
        session."""
        loop = getattr(self, "_loop", None)
        ev   = self._reconnect_event
        self._reconnect_keep   = keep_context
        self._reconnect_reason = reason
        if loop and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def _on_voice_change(self):
        """Voice picker applied.

        The voice is baked into the session at connect time, so a rebuild is
        required. It is rebuilt WITHOUT the resumption handle on purpose:
        resuming restores the server's own session state, and the safe reading
        is that it restores the voice with it — which would make the picker
        appear to do nothing. Losing context here is acceptable because changing
        voice is a deliberate, rare act; losing it on a dropped packet was not."""
        self.request_reconnect(keep_context=False, reason="new voice")

    def _on_audio_device_change(self):
        """Microphone or speaker changed. Both streams are opened inside the
        session TaskGroup, so they can only be re-opened by rebuilding it —
        but the conversation is kept, which is the whole reason resumption
        landed before this feature did."""
        self.request_reconnect(keep_context=True, reason="audio device")

    async def _watch_reconnect(self):
        """Session-scoped task: when a voluntary reconnect is requested, raise a
        signal that unwinds the TaskGroup so the run loop rebuilds the session."""
        assert self._reconnect_event is not None
        await self._reconnect_event.wait()
        self._reconnect_event.clear()
        keep   = self._reconnect_keep
        reason = getattr(self, "_reconnect_reason", "") or "settings"
        self.ui.write_log(
            f"SYS: Applying {reason} — reconnecting"
            + ("..." if keep else " (starting a fresh conversation)...")
        )
        raise _ReconnectSignal(keep_context=keep)

    async def _watch_goaway_deadline(self):
        """Session-scoped task: after a GoAway, the server force-closes the
        socket once its grace expires (WebSocket 1008 "policy violation").
        The recv loop defers reconnecting until turn_complete so JARVIS can
        finish a sentence — but a long turn can outlive the grace, and then
        the socket dies mid-turn. This watcher makes the client close FIRST
        (exactly what the protocol asks for) at the last clean cut point: the
        moment JARVIS stops speaking."""
        while True:
            await asyncio.sleep(1)
            if self._goaway_deadline <= 0.0 or time.monotonic() < self._goaway_deadline:
                continue
            # Deadline reached: close only when not talking (clean cut) or when
            # the grace is well and truly blown (already-abusive cut).
            with self._speaking_lock:
                _speaking_now = self._is_speaking
            _over = time.monotonic() - self._goaway_deadline
            if _speaking_now and _over < 15.0:
                continue
            self._goaway_deadline = 0.0
            print(f"[JARVIS] 🔔 GoAway grace ending — reconnecting now (over by {_over:.0f}s)")
            # NOT a session limit: the conversation continues via resumption.
            # Google rotates every Live API WebSocket at ~10 min (hard server-
            # side limit); the session itself is unlimited with compression +
            # resumption. Log it as the routine hop it is.
            self.ui.write_log("SYS: Connection rotation — continuing the same conversation.")
            raise _ReconnectSignal(keep_context=True)

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        # Duplicate-submit guard for the direct HUD path (this bypasses the
        # dashboard queue, so the intake dedup cannot see it): an identical
        # text within 1.5 s is a double-Enter / double-click, not a new
        # question. Different text always passes.
        now = time.monotonic()
        if text == self._last_direct_text and (now - self._last_direct_ts) < 1.5:
            print("[JARVIS] dedup: duplicate direct text within 1.5 s — dropped")
            return
        self._last_direct_text = text
        self._last_direct_ts = now
        if not self._loop or not self.session:
            return
        # Respect wake-word sleep: a typed command must not be answered while
        # asleep either (the sleep gate is not just for the mic). Wake first with
        # "Hey Jarvis" or the WAKE NOW button.
        if self._wake_enabled and not self._awake:
            self.ui.write_log("SYS: I'm asleep — say 'Hey Jarvis' or tap WAKE NOW first.")
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def _tail_active(self) -> bool:
        """True while the speakers may still be finishing our last sentence."""
        return time.monotonic() < self._tail_until

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self._tail_until = 0.0
        else:
            # Hold the guard open across the device's own output latency plus a
            # margin for the room. The microphone is NOT muted during it — the
            # guard still lets a genuine reply through, so answering instantly
            # still works. Only our own echo is dropped.
            self._tail_until = time.monotonic() + self._out_latency + _TAIL_MARGIN
        if not value:
            # The echo history is deliberately NOT cleared here: the tail above
            # still needs it to recognise our own voice.
            self._out_level = 0.0
        if value:
            self._face_state("SPEAKING")
        elif not self.ui.muted:
            self._face_state("LISTENING")

    def _face_state(self, name: str) -> None:
        """Drive the HUD state AND the RIO 3D face over the dashboard WS
        (transient frames — never recorded in the WS replay history). The
        face maps: SPEAKING → yellow glow + lip-sync, THINKING/WORKING →
        orange, LISTENING → blue, SLEEPING → dim, MUTED → gray."""
        self.ui.set_state(name)
        if self._dashboard is not None:
            try:
                asyncio.create_task(self._dashboard.broadcast_transient(
                    {"type": "assist_state", "state": name,
                     "ts": time.time()}))
            except Exception:
                pass  # a dead dashboard must never break the audio path

    def set_mic_muted(self, muted: bool) -> str:
        """Mic mute from the RIO UI (Home / Chat mute buttons) — the SAME
        switch the HUD mute button flips. `ui.muted` gates the PC mic in the
        audio callback (main.py ~2041), so a muted assistant stops SENDING
        your voice to the live session; replies already playing finish.
        Session-scoped: restarts unmute, exactly like the HUD."""
        self.ui.muted = bool(muted)
        self.ui.write_log("SYS: Microphone muted."
                          if muted else "SYS: Microphone live.")
        try:
            self._face_state("MUTED" if muted else "LISTENING")
        except Exception:
            pass
        return "session (live immediately)"

    def set_push_to_talk(self, enabled: bool) -> str:
        """Turn hold-to-talk on or off. Returns the scope actually achieved."""
        from core.hotkey import PushToTalk

        self._ptt_enabled = bool(enabled)
        self._ptt_held = False
        if not enabled:
            if self._ptt is not None:
                self._ptt.stop()
                self._ptt = None
            return "off"

        if self._ptt is None:
            self._ptt = PushToTalk(self._on_ptt)
        scope = self._ptt.start()
        # A window-scoped chord is a real limitation — say it once in the log
        # so nobody wonders why it does nothing while another app is focused.
        try:
            self.ui.write_log(
                f"SYS: Push-to-talk on — hold {self._ptt.label}"
                + ("." if scope == "global"
                   else " (works while this window is focused)."))
        except Exception:
            pass
        return scope

    def _on_ptt(self, held: bool) -> None:
        """Chord pressed or released — may arrive on the hotkey thread."""
        self._ptt_held = held
        if held:
            # Holding the key is also a way to wake it, so push-to-talk works
            # without having to say the wake word first.
            if self._wake_enabled and not self._awake:
                self._awake = True
                self._last_user_speech = time.monotonic()
        try:
            self._face_state("LISTENING" if held else "SLEEPING")
        except Exception:
            pass

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        # The words we were about to mouth are never going to be spoken now.
        self._visemes.reset()
        self._play_cursor = 0.0     # next batch starts a fresh timeline
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            with open(API_CONFIG_PATH, encoding="utf-8") as _f:
                _cfg = json.loads(_f.read())
            self._asst_name = (_cfg.get("assistant_name") or "JARVIS").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "JARVIS"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Fresh-session journal tail (2026-10-01). A process restart previously
        # started the model with a completely blank slate — the conversation
        # journal was only reachable via the recall_memory tool, which the
        # model does not think to call for anything it does not know was
        # forgotten. Folding the last few turns into the prompt closes that
        # gap the same way the in-proc _carry_context seam does, but across
        # restarts. Gated to fresh sessions only: a resumed or carried session
        # already has its context from the handle or the continuation block,
        # and pasting the same turns twice teaches the model to repeat itself.
        journal_ctx = ""
        if not self._resume_handle and not self._carry_context:
            try:
                from memory.conversation_journal import (recent_journal_tail,
                                                         last_user_request)
                _tail = recent_journal_tail(max_chars=1200)
                if _tail:
                    # Anchor the resume on what the user actually asked last —
                    # alerts/monitors are filtered out of _tail, and this line
                    # states the dropped-in-the-middle-of task outright so the
                    # model continues it instead of merely sounding informed.
                    _last = last_user_request()
                    _anchor = (
                        f"The user's most recent request was: {_last}\n"
                        "Continue from exactly there — do not start over.\n\n"
                        if _last else "")
                    journal_ctx = (
                        "[CONVERSATION RESUMED]\n"
                        "This assistant process restarted. Your recent past "
                        "exchanges with the user, oldest first:\n\n"
                        + _tail + "\n\n" + _anchor
                        + "Do NOT re-greet or reintroduce yourself. "
                        "Continue naturally; you may reference these only if "
                        "the user brings them up.\n\n"
                    )
            except Exception as _journal_err:
                print(f"[Memory] journal tail skipped: {_journal_err}")

        # Identity injection — overrides any hardcoded name in prompt.txt
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else "ADDRESS: Address the user with the ordinary respectful form "
                      "for a superior in the language you are currently speaking — "
                      "\"sir\" in English, its everyday equivalent in any other "
                      "language. Never an archaic or aristocratic form, and never "
                      "the form from a different language than the one you are "
                      "speaking in this sentence.")
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        # Astrology & numerology persona + owner profile (2026-09-30). The
        # expert framing routes birth-date questions to the astrology_reading
        # tool (which computes everything deterministically via Swiss
        # Ephemeris + digit arithmetic) instead of the model guessing numbers
        # or falling back to web search. Computed once per (re)connect.
        astro_ctx = ""
        try:
            from memory.config_manager import get_birth_profile
            from core           import astro_engine
            _prof = get_birth_profile()
            astro_ctx = (
                "[ASTROLOGY & NUMEROLOGY EXPERTISE]\n"
                "You are a master Vedic astrologer and numerologist. For ANY "
                "birth-date question (moolank, bhagyank, life path, kundali, "
                "nakshatra, compatibility, lucky numbers) you MUST call the "
                "astrology_reading tool and present its computed numbers — "
                "NEVER calculate or guess numbers yourself; your arithmetic is "
                "not trustworthy for this. Weave the tool's JSON into a warm, "
                "personal reading in the user's spoken language. If the tool "
                "flags moon_sign_uncertain, say so and ask for the birth time. "
                "When the user asks you to remember their birth details, call "
                "the tool with save=true.\n\n"
            )
            if _prof.get("dob"):
                _y, _m, _d = astro_engine.parse_dob(_prof["dob"]) or (None, None, None)
                if _y:
                    _r = astro_engine.full_reading(
                        _y, _m, _d,
                        gender=_prof.get("gender", ""),
                        birth_time=_prof.get("birth_time", ""),
                        birth_place=_prof.get("birth_place", ""),
                        name=_prof.get("name", ""))
                    _n = _r.get("numerology", {})
                    _v = _r.get("vedic", {})
                    astro_ctx += (
                        f"[OWNER BIRTH PROFILE] (the user — use whenever they ask "
                        f"about themselves without giving a DOB)\n"
                        f"DOB: {_prof['dob']}"
                        + (f", {_prof['birth_time']}" if _prof.get("birth_time") else "")
                        + (f", {_prof['birth_place']}" if _prof.get("birth_place") else "")
                        + (f", name: {_prof['name']}" if _prof.get("name") else "")
                        + (f", gender: {_prof['gender']}" if _prof.get("gender") else "")
                        + f"\nComputed: moolank {_n.get('moolank')}, bhagyank {_n.get('bhagyank')}, "
                    )
                    if _v:
                        astro_ctx += (
                            f"Vedic sun sign {_v.get('sun_sign')} ({_v.get('sun_sign_sanskrit')}), "
                            f"moon sign {_v.get('moon_sign')} ({_v.get('moon_sign_sanskrit')}), "
                            f"nakshatra {_v.get('nakshatra')} pada {_v.get('pada')}."
                        )
                    astro_ctx += "\n\n"
        except Exception as _astro_err:
            print(f"[Astro] profile context skipped: {_astro_err}")

        parts = [time_ctx, identity_ctx]
        if astro_ctx:
            parts.append(astro_ctx)
        if mem_str:
            parts.append(mem_str)
        if journal_ctx:
            parts.append(journal_ctx)
        # Installed GitHub skills: injected on every (re)build so installs,
        # upgrades and enable/disable take effect without a restart.
        try:
            from core.skill_registry import installed_skills_context
            _skills_ctx = installed_skills_context()
            if _skills_ctx:
                parts.append(_skills_ctx)
        except Exception as _skills_err:
            print(f"[Skills] context injection failed: {_skills_err}")
        parts.append(sys_prompt)
        if self._carry_context:
            # Post-rejection context carry-over: the fresh session must know
            # it is CONTINUING a conversation, not starting one. Compact tail
            # (12 turns) — enough to bridge the seam without bloating setup.
            parts.append(
                "[CONTINUATION]\n"
                "You are resuming an ongoing conversation that was interrupted "
                "by a technical connection rotation. Do NOT reintroduce yourself "
                "or greet the user again. Continue seamlessly from this recent "
                "exchange:\n\n" + self._carry_context
            )
            self._carry_context = None   # one-shot: consumed by this rebuild

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": (
                TOOL_DECLARATIONS
                + self._action_registry.get_tool_declarations()
                + self._plugin_registry.get_tool_declarations()
            )}],
            # Hand back the handle captured from the last session_resumption
            # update. `handle=None` is exactly the old behaviour (ask for
            # handles, start fresh), so the first connect of a run is unchanged.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            # Sliding-window compression: session never dies from a full context
            # window — JARVIS can stay in one conversation for hours
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )
        if self._enhanced_live:
            # Proactive audio: JARVIS stays silent when speech isn't addressed
            # to it (background chatter, talking to someone else in the room).
            # (Affective dialog was dropped: gemini-3.1-flash-live does not
            #  support it, and it never reliably detected tone in practice.
            #  To restore it on a 2.5 native-audio model, add back:
            #  cfg["enable_affective_dialog"] = True )
            cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)
        # Deliberate thinking (2026-10-01): get_thinking_enabled() existed in
        # config_manager for two releases but was never read by anyone — a
        # dead toggle. The Live models accept thinking_config at connect time,
        # so the switch now reaches the session. Off by default (latency is
        # this app's brand); on, the model reasons before hard answers instead
        # of guessing. Auto-reverts on the same config-drift path as
        # proactivity above if a future model rejects the field.
        if get_thinking_enabled():
            cfg["thinking_config"] = types.ThinkingConfig(include_thoughts=False)
        return types.LiveConnectConfig(**cfg)

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")
        self._face_state("THINKING")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self._face_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "recall_memory":
                # Local file search: no network, no second model. Kept out of
                # the executor deliberately — it is a dictionary scan over a few
                # hundred short strings, and a thread hop would cost more than
                # the work itself.
                _q = args.get("query", "")
                result = search_memory(_q, limit=8)
                # Curated store came up empty → fall through to the verbatim
                # conversation journal. This is what makes "what did we talk
                # about / when did I ask you X" answerable even when the fact
                # was never distilled into long_term.json.
                if ("Nothing stored" in result or "have not stored" in result) and _q:
                    _j = search_journal(_q, limit=10)
                    if "empty" not in _j and "Nothing in the recorded" not in _j:
                        result = result + "\n" + _j

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = ("Things I can undo, most recent first:\n"
                              + "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
                              ) if items else "I have not changed anything I can undo yet."
                else:
                    result = await loop.run_in_executor(None, undo_stack.undo_last)

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        # Show what JARVIS sees — the same overlay the camera
                        # preview uses, so the user can confirm the capture
                        # matches reality (e.g. spot a black frame instantly).
                        try:
                            self.ui.show_camera_frame(img_b)
                        except Exception:
                            pass
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                        f"Immediately say ONE short natural sentence in the user's own language, "
                        f"telling them you are looking at their {_stall} right now. "
                        f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": "Say a brief natural goodbye to the user."}]},
                                turn_complete=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            elif self._action_registry.has(name):
                # file_processor: fall back to the currently-uploaded file when none is given
                if name == "file_processor" and not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                _ctx = {"player": self.ui, "speak": self.speak,
                        "response": None, "session_memory": None,
                        "dashboard": self._dashboard}
                r = await loop.run_in_executor(None, lambda: self._action_registry.run(name, args, _ctx))
                result = r or "Done."
                # web_search: mirror results to the on-screen content panel
                if (name == "web_search" and r
                        and not r.startswith("No results")
                        and not r.startswith("Search failed")):
                    _mode  = args.get("mode", "search")
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)

            else:
                if self._plugin_registry.has(name):
                    r = await loop.run_in_executor(
                        None,
                        lambda: self._plugin_registry.run(name, args, player=self.ui, session_memory=None)
                    )
                    result = r or "Done."
                else:
                    result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self._face_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": _sanitize_tool_result(result)}
        )

    async def _send_realtime(self):
        # A half-dead websocket looks EXACTLY like a healthy one from here:
        # the run loop still says "Connected" and mic frames keep queueing, but
        # every send disappears into a CLOSE-WAIT socket (observed live: server
        # closed during a quota burst, the client never raised, JARVIS went
        # deaf-mute with zero errors and zero reconnects). A healthy Live
        # session completes send_realtime_input in well under a second, so a
        # long stall is proof of a dead socket: time each send, and after two
        # consecutive stalls force the run loop to rebuild the session.
        stall_count = 0
        while True:
            msg = await self.out_queue.get()
            # Gemini 3.x Live rejects the old realtime_input.media_chunks field
            # (what `media=...` maps to) and closes the socket with a 1007. Send
            # mic / phone PCM through the new `audio` field instead. Queue items
            # are {"data": <bytes>, "mime_type": <str>} from _listen_audio and
            # the phone relay.
            try:
                await asyncio.wait_for(
                    self.session.send_realtime_input(
                        audio=types.Blob(
                            data=msg["data"],
                            mime_type=msg.get("mime_type", "audio/pcm"),
                        )
                    ),
                    timeout=15,
                )
                stall_count = 0
            except asyncio.TimeoutError:
                stall_count += 1
                print(f"[JARVIS] ⚠️ Live send stalled {stall_count}×15 s — rebuilding session")
                if stall_count >= 2:
                    self.request_reconnect(
                        keep_context=True,
                        reason="live send stalled — dead session",
                    )
                    stall_count = 0
                    await asyncio.sleep(1)   # let the teardown land
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if _is_session_limit_close(e):
                    # The GoAway grace expired — the server did exactly what
                    # the protocol says it will. Close first next time (the
                    # deadline watcher owns that); here, just rebuild calmly
                    # with context intact.
                    print("[JARVIS] 🔔 Session limit reached during send — reconnecting")
                    self.request_reconnect(
                        keep_context=True,
                        reason="session limit reached",
                    )
                    await asyncio.sleep(1)
                    continue
                # Any other send failure: surface it and rebuild immediately —
                # the run loop's reconnect machinery owns the teardown.
                print(f"[JARVIS] ⚠️ Live send error: {e} — rebuilding session")
                self.request_reconnect(keep_context=True, reason=f"send error: {e}")
                await asyncio.sleep(1)

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            # ── Wake-word gate ───────────────────────────────────────────────
            # While asleep, the mic audio NEVER goes to Gemini (nothing is
            # streamed, so JARVIS can't respond to speech not addressed to it and
            # nothing leaves the machine). Frames are instead handed to the local
            # detector, which runs its model in ITS OWN thread — the cost here is
            # only a queue push, so the audio path is never slowed. When wake word
            # is off (default) or we're awake, this is a single boolean check.
            if self._wake_enabled and not self._awake:
                det = self._wake_detector
                if det is not None:
                    det.feed(indata)
                return
            if self._mic_pause_state() or self._phone_mic_active_now():
                return   # phone mic is live — PC mic stream is paused (belt & braces: even if the OS-level pause hasn't taken effect yet)
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking

            # ── Echo tail (Mark-LIV) ─────────────────────────────────────────
            # While JARVIS talks the mic is not streamed. The speaking flag
            # dropping is not the end of it though: the speakers have not
            # finished. The microphone stays OPEN — the guard only drops blocks
            # that are our own voice, so replying the instant it stops still
            # works.
            if jarvis_speaking:
                return
            if self._tail_active():
                try:
                    if not self._echo.is_user_speech(
                            indata, SEND_SAMPLE_RATE, _pcm_level(indata)):
                        return
                    self._tail_until = 0.0      # a real voice ends the tail early
                except Exception:
                    return
            elif self._echo._hist:
                self._echo.reset()

            # ── Push-to-talk (Mark-LIV) ──────────────────────────────────────
            # When it is on the microphone is closed by default and the chord
            # opens it: nothing leaves the machine unless the key is held.
            if self._ptt_enabled and not self._ptt_held:
                return

            if not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm"}
                )
                # Feed the live mic level to the HUD so the waveform reacts to
                # the user's actual voice while listening. Purely cosmetic — any
                # failure here must never disturb the mic.
                try:
                    self.ui.set_audio_level(_pcm_level(indata))
                except Exception:
                    pass

        try:
            def _open_mic(dev):
                return sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                )

            # Which microphone. resolve() returns None for "system default" and
            # for a saved device that is no longer present — so a headset
            # unplugged since the last run falls back to the built-in mic
            # instead of raising on startup and taking the session with it.
            _mic_name = get_input_device()
            _mic_dev  = audio_devices.resolve(_mic_name, "input")
            if _mic_dev is not None:
                print(f"[JARVIS] 🎤 Input device: {_mic_name}")
            try:
                with audio_devices._quiet_stderr():
                    _mic_stream = _open_mic(_mic_dev)
            except Exception as _e:
                # A device the picker listed but the driver will not open right
                # now — exclusive mode, a webcam already in use, a virtual mic
                # whose source went away. Chosen hardware failing must never
                # mean the assistant cannot hear at all.
                if _mic_dev is None:
                    raise
                print(f"[JARVIS] ⚠️  Mic '{_mic_name}' failed: {_e} — using default")
                self.ui.write_log(
                    f"SYS: Microphone '{_mic_name}' unavailable — using system default."
                )
                with audio_devices._quiet_stderr():
                    _mic_stream = _open_mic(None)

            with _mic_stream:
                print("[JARVIS] 🎤 Mic stream open")
                self._pc_mic_stream = _mic_stream   # pause/resume on phone-mic state
                # Re-sync: a session rebuild mid-pause replaces the stream
                # handle; the flag would otherwise stop() the OLD handle and
                # leave the new one untracked (harmless — the callback gates
                # still hold — but pause/resume bookkeeping must match the
                # live stream). If the phone mic is STILL live, re-pause the
                # fresh stream immediately; frames stay gated either way via
                # _phone_active + phone_mic_active_now().
                with self._pc_mic_lock:
                    still_live = (
                        self._dashboard is not None
                        and self._dashboard.phone_mic_active())
                    self._pc_mic_paused = False
                if still_live:
                    self._pause_pc_mic()
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_text, in_buf = "", []      # out_text: overlap-aware merged transcript
        out_last = ""                  # last raw chunk (consecutive-repeat guard)

        try:
            while True:
                async for response in self.session.receive():

                    # ── Session resumption ───────────────────────────────────
                    # The server sends this periodically. `resumable` goes false
                    # while a turn is mid-flight — replaying a handle from that
                    # moment is what the flag exists to prevent — so only
                    # resumable handles are kept. This is three lines and it is
                    # the entire fix for "every reconnect forgets everything".
                    _sru = getattr(response, "session_resumption_update", None)
                    if _sru is not None:
                        _h = getattr(_sru, "new_handle", None)
                        # 2026-09-28 diagnostics: every rotation since 03:54 was
                        # rejected with "session not found" even though the
                        # handle was armed seconds before. Log each DISTINCT
                        # handle (prefix only) + the full update payload, so a
                        # rejection can be correlated with exactly what was
                        # sent and what the server had told us in between.
                        if _h and _h != self._last_seen_handle:
                            self._last_seen_handle = _h
                            try:
                                _dump = _sru.model_dump(exclude_none=True)
                            except Exception:
                                _dump = str(_sru)
                            print(f"[JARVIS] 🔗 sru → {_h[:14]}… {_dump}")
                        if getattr(_sru, "resumable", False) and _h:
                            if self._resume_handle is None:
                                print("[JARVIS] 🔗 Session resumption armed")
                            self._resume_handle = _h

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    # ── GoAway: the session is about to hit its server-side
                    # duration limit. The protocol expects the client to close
                    # the connection ITSELF after this message; ignoring it gets
                    # the socket force-killed with 1008 (policy violation),
                    # which also poisons the resumption handle — hence
                    # "Resumption handle rejected" and a conversation wiped.
                    # Raise the voluntary reconnect signal instead: the run loop
                    # rebuilds the session and replays the last valid handle,
                    # so the conversation carries on seamlessly.
                    _go_away = getattr(response, "go_away", None)
                    if _go_away is not None:
                        _left_hm = _fmt_time_left(getattr(_go_away, "time_left", None))
                        _left = f" (~{_left_hm} left)" if _left_hm else ""
                        self._goaway_pending = True
                        with self._speaking_lock:
                            _speaking_now = self._is_speaking
                        if not _speaking_now:
                            print(f"[JARVIS] 🔔 GoAway received{_left} — reconnecting proactively")
                            # NOT a session limit — only the WebSocket rotates (~10 min);
                            # the conversation continues via resumption.
                            self.ui.write_log("SYS: Connection rotation — continuing the same conversation.")
                            raise _ReconnectSignal(keep_context=True)
                        # Mid-sentence: defer to the next turn boundary. The
                        # deadline watcher guarantees the close still happens
                        # inside the server's grace window.
                        self._goaway_deadline = (
                            time.monotonic() + max(15.0, GOAWAY_GRACE_S / 2)
                        )
                        print(f"[JARVIS] 🔔 GoAway received{_left} — will reconnect after this turn")

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt and txt != out_last:
                                # Overlap-aware merge: restart / re-sync chunks
                                # that restate already-transcribed words are
                                # stitched, not appended (see _merge_transcript).
                                merged = _merge_transcript(out_text, txt)
                                if len(merged) > len(out_text):
                                    # Hand only the NEW words to the mouth, so
                                    # the avatar forms consonants for speech it
                                    # has not already lip-synced (Mark-LIV).
                                    self._visemes.feed_text(
                                        merged[len(out_text):].lstrip())
                                out_text = merged
                            out_last = txt

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_text, out_last = "", ""
                                # An interrupted turn still ends the turn the
                                # command loop may be waiting on (Stop button,
                                # barge-in) — release it, or the next queued
                                # message would wait out the full timeout.
                                self._dash_turn_done.set()
                                if self._active_req_id:
                                    _resp_tracker().update(
                                        self._active_req_id, _RS_CANCELLED,
                                        error="interrupted by user")
                                    self._active_req_id = ""
                                self._pending_conv = ""
                                # The messaging request mid-answer will never
                                # get its reply now — notify its chat once
                                # instead of leaving permanent silence (and a
                                # stale slot that would misroute the next
                                # voice answer).
                                if self._inbound_channel:
                                    self._notify_inbound_lost("interrupted")
                                continue

                            full_in = " ".join(in_buf).strip()
                            _turn_conv = (self._pending_conv or "").strip() or None
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                # The conversation uptime clock starts at the
                                # first user turn — not at connect, which in
                                # wake-word mode can precede any conversation
                                # by hours.
                                if self._conversation_started is None:
                                    self._conversation_started = time.monotonic()
                                # Verbatim journal — the never-forgets layer.
                                # The RIO conversation tag travels with THIS
                                # turn explicitly (consumed once below); the
                                # old set-around-send of the module global
                                # always lost the race with the executor and
                                # no row was ever stamped.
                                asyncio.get_event_loop().run_in_executor(
                                    None, append_turn, "user", full_in, _turn_conv)
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = out_text.strip()
                            if full_out:
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                # Verbatim journal — the never-forgets layer.
                                asyncio.get_event_loop().run_in_executor(
                                    None, append_turn, "jarvis", full_out, _turn_conv)
                                # Hermes bridge: this answer was for a Telegram/
                                # WhatsApp message → deliver it back on the SAME
                                # channel it came from (spec §8/§11). The reply
                                # gate makes sure it is THIS turn's answer: a
                                # turn that completed before the request was
                                # even sent (interleaved voice, replay after
                                # resume) must not be delivered to the chat.
                                if self._inbound_channel:
                                    if time.monotonic() >= self._inbound_req_ts:
                                        asyncio.create_task(self._hermes_reply(full_out))
                                    else:
                                        print(f"[Hermes] reply gate: pre-request "
                                              f"turn not delivered to {self._inbound_channel}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            elif self._inbound_channel and time.monotonic() >= self._inbound_req_ts:
                                # Empty answer for a pending messaging request:
                                # the user would get silence forever. One
                                # honest failure notice (no auto-retry — that
                                # could double-process the request).
                                print("[Hermes] ⚠️ turn completed with no answer — "
                                      "sending failure notice")
                                asyncio.create_task(self._hermes_reply(
                                    "I couldn't generate a reply just now — "
                                    "please send that again."))
                                if self._active_req_id:
                                    _resp_tracker().update(
                                        self._active_req_id, FAILED,
                                        finish="none", error="empty model response")
                                    self._active_req_id = ""
                            out_text, out_last = "", ""

                            # Turn lifecycle: the dashboard/bridge request that
                            # produced this turn is now complete (spoken audio
                            # is tracked separately by the play loop).
                            if self._active_req_id:
                                _resp_tracker().update(
                                    self._active_req_id, _RS_COMPLETED,
                                    finish="turn_complete")
                                self._last_req_id = self._active_req_id
                                self._active_req_id = ""
                            # This turn is fully answered — release the next
                            # queued dashboard/bridge command, and scope the
                            # conversation tag to exactly this turn.
                            self._dash_turn_done.set()
                            self._pending_conv = ""

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                b64 = _b64.b64encode(img_b).decode("ascii")
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                                await self.session.send_client_content(
                                    turns={"role": "user", "parts": [
                                        {"inline_data": {"mime_type": mime_t, "data": b64}},
                                        {"text": question},
                                    ]},
                                    turn_complete=True,
                                )
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until JARVIS finishes speaking the answer
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                            # Deferred GoAway reconnect — the turn just ended AND
                            # its bookkeeping (journal, reply, broadcast, vision
                            # follow-up) is done, so this is a clean cut point
                            # (see the go_away block above for why the client must
                            # close first). Raising any earlier cancelled the
                            # playback mid-sentence and threw away the
                            # just-finished turn's journal row, UI broadcast and
                            # Hermes reply.
                            if self._goaway_pending:
                                self._goaway_pending = False
                                print("[JARVIS] 🔔 Turn finished — reconnecting before the connection rotates")
                                self.ui.write_log("SYS: Connection rotation — continuing the same conversation.")
                                raise _ReconnectSignal(keep_context=True)

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except _ReconnectSignal:
            raise   # voluntary reconnect — the run loop logs this quietly; a ❌ + traceback here is noise
        except Exception as e:
            if _is_session_limit_close(e):
                # The server enforcing its GoAway grace is session
                # bookkeeping, not a failure: the deadline watcher exists to
                # close first, and this is only its safety net. Log calmly,
                # keep the resumption handle, and rebuild.
                # (2026-09-28: the server can also abort the socket mid-turn
                # with 1008 "The operation was aborted" before this deadline
                # is ever reached — the reason text varies, the 1008 close is
                # what matters. The watcher owns the polite cut; this branch
                # owns the recovery when the server cuts first.)
                print("[JARVIS] 🔔 Session limit reached — refreshing connection")
                self.ui.write_log("SYS: Session limit reached — refreshing connection.")
                raise _ReconnectSignal(keep_context=True) from e
            if getattr(self, "_shutting_down", False):
                # We are closing the session as part of window-close teardown
                # — the server's 1000 close landing as APIError 1000 inside
                # receive() is EXPECTED. Log a one-liner, don't traceback.
                print(f"[JARVIS] 👂 Recv ended (shutdown): {type(e).__name__}")
                raise
            if _is_transient_server_error(e):
                # 1011 internal error — Google-side hiccup; the run loop's
                # 3 s reconnect + resumption handle restore the conversation
                # (verified 2026-10-13). One calm line, no traceback noise.
                print(f"[JARVIS] 🔔 Server hiccup ({getattr(e, 'code', '?')}) — reconnecting")
                raise
            if _is_invalid_argument_close(e):
                # 1007 invalid argument — OUR request was rejected (e.g. a
                # tool response the server would not accept). The session is
                # fine server-side; rebuild with the resumption handle. One
                # calm line — a traceback here taught nothing (2026-10-14).
                print("[JARVIS] 🔔 Request rejected (1007 invalid argument) — reconnecting")
                raise _ReconnectSignal(keep_context=True) from e
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        _spk_name = get_output_device()
        _spk_dev  = audio_devices.resolve(_spk_name, "output")
        if _spk_dev is not None:
            print(f"[JARVIS] 🔊 Output device: {_spk_name}")

        def _open_spk(dev):
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
                # Extra buffering headroom to stop audible ALSA underruns —
                # see the SPEAKER_LATENCY_S note above.
                latency=SPEAKER_LATENCY_S,
            )
            st.start()
            return st

        try:
            with audio_devices._quiet_stderr():
                stream = _open_spk(_spk_dev)
        except Exception as _e:
            # A chosen output that the host API accepts by name but refuses to
            # open (exclusive mode, wrong sample rate, device asleep) must not
            # cost the user their voice. Fall back to the default and say so.
            if _spk_dev is None:
                raise
            print(f"[JARVIS] ⚠️  Output device '{_spk_name}' failed: {_e} — using default")
            self.ui.write_log(f"SYS: Speaker '{_spk_name}' unavailable — using system default.")
            with audio_devices._quiet_stderr():
                stream = _open_spk(None)

        # Mark-LIV: ask the device how far behind the speakers actually are,
        # rather than assuming. This is what the echo tail is sized from, so a
        # machine with a large audio buffer gets a correspondingly longer guard.
        try:
            lat = float(getattr(stream, "latency", 0.0) or 0.0)
            if 0.0 < lat < 1.0:
                self._out_latency = lat
            print(f"[JARVIS] 🔊 Output latency {self._out_latency*1000:.0f} ms "
                  f"→ echo tail {(self._out_latency + _TAIL_MARGIN)*1000:.0f} ms")
        except Exception:
            pass

        _write_task = None   # the in-flight stream.write executor task, if any
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                        # Voice playback for the last answered turn is now
                        # fully played — tracked separately from the text
                        # state (completed text never implies completed audio).
                        if self._last_req_id:
                            _resp_tracker().mark_spoken(self._last_req_id)
                            self._last_req_id = ""
                    continue

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:   # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # Drive the HUD waveform and the avatar's mouth from JARVIS's
                # own voice (Mark-LIV). The batch is up to 200 ms long, so we
                # hand over a *schedule* of 20 ms viseme frames instead of a
                # single averaged level and let the HUD play it out in step
                # with the audio.
                try:
                    pcm = np.frombuffer(bytes(batch), dtype=np.int16)
                    hop = _VIS_HOP / RECEIVE_SAMPLE_RATE
                    frames = _pcm_visemes(pcm, sr=RECEIVE_SAMPLE_RATE)
                    # When does this batch become audible? The first bytes of a
                    # reply reach the speaker about one callback period later —
                    # NOT one buffer later. After the anchor nothing needs
                    # measuring: the device consumes at exactly realtime, so
                    # each batch sounds one batch-duration after the one before
                    # it. The cursor is re-anchored only when it leaves the
                    # range physically possible.
                    now = time.time()
                    horizon = self._out_latency + _CURSOR_SLACK
                    if not (now <= self._play_cursor <= now + horizon):
                        self._play_cursor = now + _FIRST_SOUND
                    at = self._play_cursor
                    # Advance by the batch's own duration whether or not it
                    # yielded frames, so a block too short to analyse cannot
                    # shift everything after it out of step with the audio.
                    self._play_cursor += pcm.size / RECEIVE_SAMPLE_RATE
                    if frames:
                        frames = self._visemes.frames(frames, hop)
                        self.ui.push_visemes(frames, hop, at)
                        # RIO 3D face: real lip-sync. The viseme level IS the
                        # mouth — throttled to ~11 Hz over the dashboard's
                        # transient WS fan-out (never in the replay history).
                        now_tx = time.time()
                        if (self._dashboard is not None
                                and now_tx - self._last_mouth_tx >= 0.09):
                            self._last_mouth_tx = now_tx
                            lvl = max(f[0] for f in frames)
                            asyncio.create_task(
                                self._dashboard.broadcast_transient(
                                    {"type": "mouth",
                                     "lvl": round(float(min(1.0, lvl * 3.0)), 3),
                                     "ts": now_tx}))
                        # Barge-in needs to know what we are playing, not just
                        # how loud: the guard subtracts this from the mic.
                        self._out_level = max(f[0] for f in frames)
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE,
                                               self._out_level)
                    else:
                        lvl = _pcm_level(pcm)
                        self.ui.set_audio_level(lvl)
                        self._out_level = lvl
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE, lvl)
                except Exception:
                    pass

                # Double-voice fix: when the phone's mic is live, JARVIS's reply
                # is heard on the PHONE — playing it on the PC speakers too makes
                # the phone's mic (and the PC's) pick it up again, creating an
                # echo loop that confuses Gemini's turn detection. Duck the PC
                # speakers while the phone listens; PC-only use is unaffected.
                # The flag is driven by the dashboard callback, but also polled
                # directly so a missed callback can never leave the echo up.
                _ducked = self._pc_speaker_ducked or (
                    self._dashboard is not None
                    and self._dashboard.phone_mic_active())
                if _ducked:
                    try:
                        self._mirror_phone_audio(bytes(batch))
                    except Exception:
                        pass
                    continue

                # Mirror JARVIS's voice to the phone dashboard so a remote
                # user HEARS the reply, not just reads it. Fire-and-forget:
                # PC playback is never delayed or blocked by the mirror.
                try:
                    self._mirror_phone_audio(bytes(batch))
                except Exception:
                    pass

                try:
                    # ALSA writes block while the device is wedged; without a
                    # timeout the play loop would hang forever with no log.
                    # The write runs as a tracked task: two writes must never
                    # overlap on one native stream (the previous stalled one
                    # is waited out before a new batch starts), and the
                    # teardown below must never close the stream under one.
                    if _write_task is not None and not _write_task.done():
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(_write_task), timeout=1.0)
                        except Exception:
                            pass   # TimeoutError — still wedged; drop this batch
                        if not _write_task.done():
                            continue   # wedged — drop this batch, keep looping
                    _write_task = asyncio.create_task(
                        asyncio.to_thread(stream.write, bytes(batch))
                    )
                    await asyncio.wait_for(
                        asyncio.shield(_write_task), timeout=10,
                    )
                except asyncio.TimeoutError:
                    print("[JARVIS] ⚠️ Speaker write stalled >10 s — audio device may be wedged")
                except RuntimeError:
                    break   # stream/executor gone — exit cleanly
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            # 2026-10-14 malloc abort: the session's death cancelled this task
            # mid-write, and the old teardown called stop()/close() while an
            # executor thread was still inside stream.write() — concurrent
            # access to one native PortAudio stream corrupts the heap. Wait
            # out the in-flight write (bounded), THEN close, and never raise
            # from teardown.
            if _write_task is not None and not _write_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(_write_task), timeout=3.0)
                except (Exception, asyncio.CancelledError):
                    print("[JARVIS] ⚠️ Speaker write still in flight at close — dropping")
            try:
                stream.stop()
                stream.close()
            except Exception as _te:
                print(f"[JARVIS] ⚠️ Speaker teardown: {_te}")

    def _mirror_phone_audio(self, pcm: bytes) -> None:
        """Queue a PCM batch for the phone dashboard's audio player.
        Called from the play loop; never blocks, never raises."""
        try:
            # NOTE: the attribute is `_phone_audio_queue` (dashboard/server.py).
            # It was `_phone_audio_q` here for months — an AttributeError the
            # caller's `except Exception: pass` swallowed, so the phone never
            # heard the reply audio.
            self._dashboard._phone_audio_queue.put_nowait(pcm)
        except asyncio.QueueFull:
            # Phone tab gone/stalled — drop rather than grow unbounded.
            try:
                self._dashboard._phone_audio_queue.get_nowait()
                self._dashboard._phone_audio_queue.put_nowait(pcm)
            except Exception:
                pass

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing optimized for speed:
          Phase 1 — instant greeting (no tools) → speech starts in <1s
          Phase 2 — news (opt-in) and the day-ahead snapshot (tomorrow's
                    calendar events + unread mail count) pre-fetch in
                    background threads while Phase 1 plays, delivered as ready
                    text (no Gemini tool-call round-trip) and shown on the UI
                    content panel. Waits for turn_complete event instead of a
                    fixed sleep so there is no unnecessary gap. The day-ahead
                    fetch is consent-free: with Google connected it is two API
                    calls, without it it resolves to "" and Phase 2 stays
                    quiet — a briefing must never open a login tab.
        """
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        # News is spoken ONLY when the user asks (preference set 2026-09-21):
        # the startup briefing no longer fetches or announces headlines unless
        # briefing_news=true in config/api_keys.json. This also kills the
        # startup news 429 noise on exhausted-quota days. On-demand news via
        # the web_search tool is unchanged.
        briefing_news = False
        try:
            from memory.config_manager import load_api_keys
            briefing_news = bool(load_api_keys().get("briefing_news", False))
        except Exception:
            pass

        loop = asyncio.get_event_loop()

        news_future = None
        if briefing_news:
            news_future = loop.run_in_executor(None, _fetch_news_sync, "top world news today")

        # Day-ahead context (tomorrow's calendar + unread mail) pre-fetches in
        # the same breath as the news. _fetch_day_ahead is consent-free and
        # never raises: no Google setup → "" → Phase 2 simply omits it.
        brief_future = loop.run_in_executor(None, _fetch_day_ahead)

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Phase 1: instant greeting ─────────────────────────────────────────
        # The briefing fires before the user has said anything, so the
        # remembered language is the only signal there is. It is a starting
        # point, not a setting: the moment they reply, their language wins.
        lang_clause = (f" Speak this greeting in {lang}, then follow the "
                       f"user's own language from their first reply onward."
                       if lang else "")
        name_clause = f" Address the user as {name}." if name else ""

        # Inject last session context if available. The summary now SURVIVES
        # (pop_last_session no longer deletes); repetition is prevented by a
        # spoken-marker sidecar file instead, so the timeline stays intact for
        # the evolution engine.
        last = await asyncio.to_thread(pop_last_session)
        session_clause = ""
        if last and not await asyncio.to_thread(was_session_spoken, last.get("date", "")):
            try:
                _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                _when  = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
            except Exception:
                _when = "last time"
            session_clause = (
                f" Also briefly and naturally mention that {_when}: {last['summary']}"
            )
            await asyncio.to_thread(_mark_session_spoken, last.get("date", ""))

        if briefing_news:
            p1 = (
                f"Greet the user warmly, mention it is {time_str}, and say you are fetching today's news now.{session_clause} "
                f"Keep it to 2 short sentences max. Do not call any tools.{lang_clause}{name_clause}"
            )
        else:
            p1 = (
                f"Greet the user warmly and mention it is {time_str}.{session_clause} "
                f"Keep it to 2 short sentences max. Do not mention news and do not call any tools.{lang_clause}{name_clause}"
            )

        # Clear the turn-done event so we can wait for Phase 1 to finish
        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"role": "user", "parts": [{"text": p1}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Briefing phase 1 (greeting) sent.")

        # ── Phase 2: fire as soon as Phase 1 audio is done ───────────────────
        async def _deliver_phase2():
            try:
                lang_str = (f" Speak in {lang} unless the user has since "
                            f"spoken another language, in which case use theirs."
                            if lang else "")

                # Wait for the prefetches (already running) and Phase 1
                # turn-complete in parallel — whichever takes longer determines
                # the wait time.
                news_done   = (asyncio.wrap_future(news_future)
                               if news_future is not None else None)
                brief_done  = asyncio.wrap_future(brief_future)
                turn_waited = False
                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=6.0)
                        turn_waited = True
                    except asyncio.TimeoutError:
                        pass

                # Extra buffer: turn_complete fires when Gemini finishes *generating*
                # Phase 1, but audio may still be playing.  Waiting a beat here
                # prevents Phase 2 audio from arriving while Phase 1 is mid-sentence
                # (which sounds like a "repeated first response" to the user).
                if turn_waited:
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(1.0)

                news_text = ""
                if news_done is not None:
                    try:
                        news_text = await asyncio.wait_for(news_done, timeout=4.0)
                    except Exception:
                        news_text = ""

                try:
                    brief_text = (await asyncio.wait_for(brief_done, timeout=8.0) or "").strip()
                except Exception:
                    brief_text = ""

                if not self.session:
                    return

                has_news  = bool(news_text and len(news_text) > 60)
                has_brief = bool(brief_text)

                if has_news and has_brief:
                    self.ui.show_content("BRIEFING — headlines & day ahead",
                                         f"{news_text}\n\n{brief_text}")
                    p2 = (
                        f"[BRIEFING] Here are today's top news headlines:\n{news_text}\n\n"
                        f"And here is the day ahead:\n{brief_text}\n\n"
                        "Pick ONE headline and summarise it in one sentence, then give "
                        "the unread-mail count and tomorrow's events in one or two short "
                        "sentences, and say the full list is displayed on screen. "
                        f"Do not call any tools.{lang_str}"
                    )
                elif has_news:
                    # Show on UI content panel immediately
                    self.ui.show_content("NEWS — top world news today", news_text)

                    p2 = (
                        f"[BRIEFING] Here are today's top news headlines:\n{news_text}\n\n"
                        "Pick ONE headline, summarise it in one sentence, then say the full list "
                        f"is displayed on screen. Do not call any tools.{lang_str}"
                    )
                elif has_brief:
                    self.ui.show_content("BRIEFING — day ahead", brief_text)
                    p2 = (
                        f"[BRIEFING] Here is the day ahead:\n{brief_text}\n\n"
                        "In one or two short sentences, give the unread-mail count and "
                        "tomorrow's events — how many there are, and the first one or two "
                        f"with their times. Do not call any tools.{lang_str}"
                    )
                elif briefing_news:
                    p2 = (
                        "News headlines could not be fetched right now. "
                        f"Let the user know briefly.{lang_str}"
                    )
                else:
                    return          # nothing to deliver — stay quiet

                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": p2}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Briefing phase 2 (news/day-ahead) sent.")
            except Exception as e:
                print(f"[Briefing] Phase 2 error: {e}")
                self.ui.write_log(f"SYS: Briefing phase 2 failed: {e}")

        # Phase 2 always runs now: news when enabled, day-ahead whenever the
        # Google connection is set up. With neither it exits silently.
        asyncio.create_task(_deliver_phase2())

    # ── Conversation uptime ─────────────────────────────────────────────────

    def conversation_uptime(self) -> float | None:
        """Seconds the current conversation has been running, or None if no
        conversation is in progress. Survives connection rotations by design:
        a GoAway rebuild replays the resumption handle, so from the user's
        point of view the same conversation simply continues (potentially for
        hours across ~10-minute WebSockets). Read from the Qt timer thread —
        a float attribute read is atomic under the GIL, same contract as
        set_audio_level."""
        start = self._conversation_started
        if start is None:
            return None
        return max(0.0, time.monotonic() - start)

    # ── Session memory ───────────────────────────────────────────────────────

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            # Mark-LIV: side calls go through core/gemini.py — one place for
            # the model ladder (Live first, so the text quota pool is spared),
            # a timeout on every call, and 429 cooldowns so a rung that is out
            # of quota is skipped instead of stalling every call.
            # `SMART` because a summary is a reasoning job, 30s because the
            # last 40 turns of a long session are a big prompt.
            from core import gemini
            summary = await asyncio.to_thread(
                gemini.text, prompt, gemini.SMART, None, 30_000,
            )
            summary = (summary or "").strip()
            if summary:
                save_session_summary(summary, lang)
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    async def _evolve_memory_pass(self) -> str | None:
        """One bounded Evolution consolidation pass.

        Runs OUTSIDE the summary's shutdown budget: graceful_shutdown's 5s
        wait_for covers only the summary LLM call, so the ~80s consolidation
        (2026-10-12 observation, ~6000 journal turns) can never fall inside
        step 3 and delay the exit. Once this coroutine returns, the whole
        pass — including maybe_evolve's own gate commit — has durably
        finished on disk, which is why it is awaited before the process
        exits rather than fire-and-forgotten.
        """
        return maybe_evolve()

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert or not self.session or not self._awake:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session and self._awake:
                # Don't interrupt if user spoke recently or JARVIS is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        memory = load_memory()
                        lang_e = memory.get("identity", {}).get("language", {})
                        lang   = (lang_e.get("value", "") if isinstance(lang_e, dict) else str(lang_e)).strip() or "English"
                        for alert in alerts:
                            msg = (
                                f"{alert}\n\n"
                                f"Inform the user about this development naturally in {lang}. "
                                "One brief sentence only."
                            )
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            self.ui.write_log(f"SYS: Monitor alert sent.")
                            await asyncio.sleep(6)   # gap between consecutive alerts
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Camera watcher ───────────────────────────────────────────────────────

    async def _run_camera_watch(self) -> None:
        """Announce webcam connect/disconnect events as they happen.

        CameraWatcher.watch_debounced() blocks in a worker thread until the
        /dev/video* device list changes and settles, then returns short event
        messages. Each is logged to the HUD and — when a session is live and
        the assistant is awake — spoken through the Live session, like the
        hardware-monitor and background-monitor alerts. When asleep, events
        are still logged so nothing is lost silently.
        """
        while True:
            try:
                events = await asyncio.to_thread(self._cam_watcher.watch_debounced, 2.0)
            except Exception as e:
                print(f"[CameraWatch] ⚠️ watcher error: {e}")
                await asyncio.sleep(5)
                continue
            if not events:
                continue                    # watcher stopped — should not happen here; loop anyway
            for msg in events:
                self.ui.write_log(f"SYS: {msg}")
                if self.session and self._awake:
                    try:
                        await self.session.send_client_content(
                            turns={"role": "user",
                                   "parts": [{"text": f"{msg}\n\nInform the user about this naturally in one short sentence."}]},
                            turn_complete=True,
                        )
                    except Exception as e:
                        print(f"[CameraWatch] ⚠️ announce failed: {e}")

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session or not self._awake:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # Grace after the phone mic stops before PC speakers resume — a short
    # reply tail mid-duck would otherwise pop in with an audible click/cut.
    SPEAKER_UNDUCK_GRACE_S = 1.5

    def _on_phone_mic_state(self, active: bool) -> None:
        """Phone mic started/stopped (fired from the dashboard's asyncio loop).
        Thread-safe: posts the decision onto this app's asyncio loop."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._set_speaker_duck, bool(active))
        except RuntimeError:
            pass

    def _set_speaker_duck(self, active: bool) -> None:
        if active:
            self._pc_speaker_ducked = True
            self._pause_pc_mic()
        else:
            # Resume after a grace period so reply tails aren't cut off.
            async def _unduck_later():
                await asyncio.sleep(self.SPEAKER_UNDUCK_GRACE_S)
                if not self._dashboard.phone_mic_active():
                    self._pc_speaker_ducked = False
                    self._resume_pc_mic()
            asyncio.create_task(_unduck_later())

    # ── PC mic pause (phone-mic-is-live input hygiene) ───────────────────────
    # The callback gate (`_phone_active`) already keeps PC frames out of
    # Gemini; pausing the stream too stops the OS mic from feeding a listener
    # that can't act on it, frees the callback thread, and lets other apps
    # take the device on hosts that grant exclusive access.

    # Consecutive 1 s polls without phone-mic audio before the watchdog
    # force-resumes PC mic/speakers (covers missed/never-sent callbacks).
    PHONE_MIC_RESUME_POLLS = 3

    async def _watch_phone_mic(self) -> None:
        """Level-triggered safety net for the phone-mic duck/pause state.

        The callbacks are edge-triggered on WebSocket events. A phone whose
        tab froze (screen lock, Android memory pressure) stops sending audio
        WITHOUT a clean close — TCP may take minutes to notice the dead peer,
        so callback(False) never fires and the PC mic/speakers would stay
        muted forever (seen live 2026-09-22). Liveness is time-based: a live
        phone voice session streams silence frames continuously, so 'no audio
        for PHONE_MIC_RESUME_POLLS consecutive polls' can only mean the phone
        is really gone. This watcher reconciles every second: streaming →
        paused+ducked; idle → resumed+unducked. A revived phone re-fires
        callback(True) on its first frame and re-pauses instantly."""
        misses = 0
        while True:
            await asyncio.sleep(1.0)
            try:
                active = self._phone_mic_active_now()
            except Exception:
                continue
            if active:
                misses = 0
                if self._mic_pause_state() or not self._pc_speaker_ducked:
                    self._set_speaker_duck(True)
                continue
            misses += 1
            if misses >= self.PHONE_MIC_RESUME_POLLS and (
                    self._mic_pause_state() or self._pc_speaker_ducked):
                misses = 0
                self._pc_speaker_ducked = False
                self._resume_pc_mic()

    def _pause_pc_mic(self) -> None:
        with self._pc_mic_lock:
            if self._pc_mic_paused:
                return
            self._pc_mic_paused = True
            st = self._pc_mic_stream
        if st is not None:
            try:
                st.stop()
                print("[JARVIS] 🎙️ PC mic paused — phone microphone is live")
                self.ui.write_log("SYS: PC mic paused — phone microphone is live.")
                try:
                    self.ui.set_mic_paused(True)
                except Exception:
                    pass
            except Exception as e:
                print(f"[JARVIS] ⚠️ PC mic pause failed: {e}")

    def _resume_pc_mic(self) -> None:
        with self._pc_mic_lock:
            if not self._pc_mic_paused:
                return
            self._pc_mic_paused = False
            st = self._pc_mic_stream
        if st is not None:
            try:
                st.start()
                print("[JARVIS] 🎙️ PC mic resumed")
                self.ui.write_log("SYS: PC mic resumed.")
                try:
                    self.ui.set_mic_paused(False)
                except Exception:
                    pass
            except Exception as e:
                print(f"[JARVIS] ⚠️ PC mic resume failed: {e}")
                # Leave the flag down; the callback gate still guards Gemini
                # via _phone_active, so worst case is a silent-but-open stream.

    def _mic_pause_state(self) -> bool:
        """Thread-safe pause flag read for the mic callback and the HUD."""
        with self._pc_mic_lock:
            return self._pc_mic_paused

    def _phone_mic_active_now(self) -> bool:
        """Cheap poll of dashboard phone-mic liveness from the PortAudio
        callback thread. False when the dashboard is absent (PC-only use)."""
        d = self._dashboard
        try:
            return d is not None and d.phone_mic_active()
        except Exception:
            return False

    # ── dashboard command relay ─────────────────────────────────────────────

    # ── Hermes bridge (Telegram / WhatsApp in, same channel out) ────────────

    @staticmethod
    def _parse_inbound(text: str):
        """Split a normalized dashboard message into (channel, body, file_path).
        Non-bridge messages return (None, text, None) and flow unchanged."""
        m = re.match(r"^\[INBOUND ([^\]]+)\]\s*", text)
        if not m:
            return None, text, None
        channel = m.group(1).strip()
        body = text[m.end():].strip()
        file_path = None
        fm = re.search(r"^\[FILE: (.+)\]\s*$", body, flags=re.MULTILINE)
        if fm:
            file_path = fm.group(1).strip()
            body = (body[:fm.start()] + body[fm.end():]).strip()
        return channel, body, file_path

    # Telegram bot credentials from ~/.hermes/.env, resolved once and cached.
    # The reply arm used to shell out to `hermes send` for every answer, which
    # pays ~3.3s of interpreter startup before a single byte leaves the box and
    # measured 6.5-17s per reply; the bot API does the same POST in ~1.5s.
    # Cached because a reply must never block on re-reading a file, and these
    # values rotate on the order of days. `None` until the first lookup.
    _tg_creds: tuple[str, str] | None = None

    @classmethod
    def _telegram_creds(cls) -> tuple[str, str] | None:
        """(bot_token, chat_id) read from Hermes' env file, else None.

        Same two variables `hermes send` and the curl fallback in
        scripts/local_model_report.sh use. Returns None for any failure, which
        just routes the reply back through the CLI.
        """
        if cls._tg_creds is not None:
            return cls._tg_creds
        try:
            vals: dict[str, str] = {}
            for line in (Path.home() / ".hermes" / ".env").read_text(
                    encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip().strip('"').strip("'")
            token = vals.get("TELEGRAM_BOT_TOKEN", "")
            chat = (vals.get("TELEGRAM_CHAT_ID")
                    or vals.get("TELEGRAM_HOME_CHANNEL") or "")
            if not token or not chat:
                return None
        except Exception:
            return None
        cls._tg_creds = (token, chat)
        return cls._tg_creds

    @staticmethod
    def _telegram_direct(token: str, chat_id: str, text: str) -> bool:
        """One sendMessage POST. True on HTTP 200, False on anything else."""
        import urllib.request as _req
        import urllib.parse as _parse
        body = _parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = _req.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with _req.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        except Exception as e:
            print(f"[Hermes] ⚠️ direct telegram send failed: {e}")
            return False

    def _hermes_send(self, text: str, target: str,
                     attach: str | None = None) -> bool:
        """Fire-and-forget delivery. Safe to call from any thread; never raises.
        Returns True when the platform confirmed the message.

        Telegram goes straight to the bot API — the same request `hermes send`
        makes, minus the per-reply process startup that dominated reply latency.
        Anything else (WhatsApp, thread-scoped targets, missing creds) falls
        through to the CLI, which stays the single source of truth for routing.
        """
        import subprocess as _sp
        import os as _os
        from pathlib import Path as _Path

        body = text[:3500]
        parts = target.split(":")
        # platform:chat_id:thread_id keeps its thread only via the CLI, so it is
        # not eligible for the fast path.
        if not attach and parts[0] == "telegram" and len(parts) <= 2:
            creds = self._telegram_creds()
            if creds:
                token, home_chat = creds
                chat = parts[1] if len(parts) == 2 and parts[1] else home_chat
                if self._telegram_direct(token, chat, body):
                    return True
                print("[Hermes] falling back to CLI delivery")

        try:
            args = ["hermes", "send", "-t", target, body]
            if attach:
                args += ["-f", attach]
            proc = _sp.run(
                args, capture_output=True, text=True, timeout=30,
                env={"PATH": _os.pathsep.join([
                         str(_Path.home() / ".local" / "bin"),
                         "/usr/local/bin", "/usr/bin", "/bin"]),
                     "HOME": str(_Path.home())})
            ok = proc.returncode == 0 and "sent" in (proc.stdout or "").lower()
            if not ok:
                print(f"[Hermes] ⚠️ send to {target} failed: "
                      f"{(proc.stderr or proc.stdout or f'exit {proc.returncode}').strip()[:140]}")
            return ok
        except FileNotFoundError:
            print("[Hermes] ⚠️ CLI not found — reply not delivered")
            return False
        except Exception as e:
            print(f"[Hermes] ⚠️ send error: {e}")
            return False

    async def _hermes_reply(self, text: str, target: str | None = None) -> None:
        """Deliver the answer to the channel the inbound message came from,
        then clear the bridge state. `target` overrides the slot (used by the
        lost-reply notifier, which captures the channel before clearing it)."""
        channel = target or self._inbound_channel
        rid = self._active_req_id
        self._inbound_channel = None
        self._inbound_had_file = False
        self._inbound_req_ts = 0.0
        if not channel:
            return
        # Phase 7 · bridge pipeline events: the answer exists (generated)
        # and then the real delivery verdict — True only when the channel
        # confirmed. Never raises into the reply path.
        try:
            from dashboard.hermes_events import emit as _emit_event
            _emit_event(self._dashboard, "response_generated",
                        channel=channel, chars=len(text))
        except Exception:
            pass
        ok = await asyncio.to_thread(self._hermes_send, text, channel)
        if rid:
            print(f"[REQ {rid}] DELIVERED ok={str(bool(ok)).lower()} "
                  f"channel={channel}")
        try:
            from dashboard.hermes_events import emit as _emit_event
            _emit_event(self._dashboard, "response_sent", channel=channel,
                        ok=bool(ok),
                        detail=("delivered" if ok
                                else "delivery failed — see logs"))
        except Exception:
            pass

    def _notify_inbound_lost(self, reason: str) -> None:
        """A pending messaging-channel request will never get its answer (the
        turn was interrupted or lost to a reconnect). Clear the slot and send
        ONE honest failure notice — permanent silence was the failure mode
        this replaces, and a stale slot would misroute the NEXT voice answer
        to the chat. Never raises; safe from any async context."""
        channel = self._inbound_channel
        if not channel:
            return
        self._inbound_channel = None
        self._inbound_had_file = False
        self._inbound_req_ts = 0.0
        print(f"[Hermes] ⚠️ pending reply lost ({reason}) — notifying {channel}")
        if self._active_req_id:
            _resp_tracker().update(self._active_req_id, _RS_FAILED,
                                   error=f"reply lost: {reason}")
            self._active_req_id = ""
        try:
            asyncio.get_event_loop().create_task(self._hermes_reply(
                "⚠️ I was interrupted before I could reply — please send "
                "that again.", target=channel))
        except Exception:
            pass

    async def _wait_session_ready(self, timeout_s: float) -> bool:
        """Bounded wait for the Live session to (re)appear. True when ready.
        Kept as its own method so the dashboard command loop stays readable
        and tests can shrink the wait via _CMD_SESSION_WAIT_S."""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while time.monotonic() < deadline:
            if self.session:
                return True
            await asyncio.sleep(0.1)
        return bool(self.session)

    async def _process_dashboard_commands(self) -> None:
        # One message at a time: after sending a command, wait for its answer
        # turn to complete before the next queued command is generated.
        # WITHOUT this, a second message from another channel overwrote the
        # single inbound-reply slot and the first answer went to the wrong
        # chat (or nowhere). The wait is bounded — a wedged turn releases the
        # loop after the timeout, so the queue can never deadlock.
        while True:
            try:
                item = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                # Element shapes: plain string (phone app, bridge, TradingView),
                # (text, conv_id) tuple (RIO chat), or a requeued
                # [text, conv_tag, tries] list (see the no-session path below).
                tries = 0
                if isinstance(item, list):
                    text, conv_tag, tries = item
                elif isinstance(item, tuple):
                    text, conv_tag = item[0], item[1]
                else:
                    text, conv_tag = item, ""
                if not text:
                    continue
                # Response lifecycle: every command gets a correlation id and
                # a state trail in the log (core/response_state.py).
                rid = _resp_tracker().start(
                    channel=(self._parse_inbound(text)[0] or "dashboard"),
                    source="command queue")
                _resp_tracker().update(rid, _RS_PROCESSING)
                # Wait (bounded) for the session to become ready after a wake
                # or a reconnect. Module constants so tests can shrink them.
                if not await self._wait_session_ready(_CMD_SESSION_WAIT_S):
                    # Session down (long reconnect). Silently dropping the
                    # message lost user input; requeue at the BACK, bounded,
                    # so a real outage can neither loop forever nor grow the
                    # queue unbounded.
                    if tries < _CMD_MAX_REQUEUES:
                        self._dashboard._command_queue.put_nowait(
                            [text, conv_tag, tries + 1])
                        print(f"[Dashboard] no session — requeued "
                              f"({tries + 1}/{_CMD_MAX_REQUEUES})")
                    else:
                        print(f"[Dashboard] dropped command after "
                              f"{tries} requeues (session down)")
                        _resp_tracker().update(rid, _RS_FAILED,
                                               error="session unavailable")
                    continue
                # Hermes bridge: detect a normalized inbound message and
                # remember where to send the answer (cleared on reply).
                channel, body, file_path = self._parse_inbound(text)
                if channel:
                    self._inbound_channel = channel
                    self._inbound_had_file = bool(file_path)
                    # The model must know the file's local path so the
                    # knowledge_base / file_processor tools can act on it.
                    text = (f"{body}\n[Saved file: {file_path}]"
                            if file_path else body)
                    self.ui.write_log(f"[Hermes:{channel}] {body[:80]}")
                # A remote command is deliberate control and the phone user
                # has no desktop WAKE button — so it wakes JARVIS if asleep.
                if self._wake_enabled and not self._awake:
                    self.wake(reason="remote command")
                # Conversation tag rides with THIS turn only; the journal
                # consumes it at the next turn_complete (see _receive_audio).
                # The old set-around-send of the journal's module global
                # always lost the race with the executor — no row was ever
                # stamped with a conversation id.
                self._pending_conv = conv_tag
                self._active_req_id = rid
                self._dash_turn_done.clear()
                # Reply gate timestamp: only a turn that completes AFTER this
                # moment may be delivered to the messaging channel.
                self._inbound_req_ts = time.monotonic()
                _resp_tracker().update(rid, _RS_GENERATING, model="gemini-live")
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": text}]},
                    turn_complete=True,
                )
                if not channel:
                    self.ui.write_log(f"[Web]: {text}")
                # One message, one answer: hold the queue until this turn's
                # bookkeeping (journal, broadcast, channel reply) is done.
                try:
                    await asyncio.wait_for(
                        self._dash_turn_done.wait(), timeout=_CMD_TURN_WAIT_S)
                except asyncio.TimeoutError:
                    print("[Dashboard] ⚠️ turn wait timeout — proceeding with "
                          "the next queued command")
                    _resp_tracker().update(rid, _RS_COMPLETED,
                                           finish="wait_timeout")
                    self._pending_conv = ""
                    self._active_req_id = ""
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise      # never swallow cancellation (shutdown safety)
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                if self._active_req_id:
                    _resp_tracker().update(self._active_req_id, _RS_FAILED,
                                           error=str(e)[:120])
                    self._active_req_id = ""
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()
        self._reconnect_event = asyncio.Event()
        # Consecutive network-timeout counter for the self-heal below; reset
        # on every successful connect.
        self._net_fail_streak = 0

        # 2026-09-30 self-heal canary: probes the default executor that
        # getaddrinfo (DNS for every reconnect) rides on. A healthy pool
        # returns a trivial job in milliseconds; a pool whose workers are all
        # pinned by hung blocking jobs stalls it. See the net-fail branch of
        # the connect loop for the wedge it catches.
        async def _executor_canary(timeout_s: float = 5.0) -> None:
            await asyncio.wait_for(
                self._loop.run_in_executor(None, lambda: None), timeout_s)

        # The loop's default executor is load-bearing beyond the obvious
        # to_thread calls: asyncio itself runs getaddrinfo (DNS) on it before
        # every reconnect. Its default size is min(32, cpu+4) — here that is
        # 4 workers, shared by every blocking call in the app (LLM calls,
        # actions, journal writes). A handful of unbounded HTTPS calls can pin
        # ALL of them; DNS then queues forever behind the hung jobs and every
        # reconnect times out with "timed out during opening handshake" while
        # the network is actually fine (seen 2026-09-20 and 2026-09-21).
        # Generous sizing makes one wedged job an inconvenience, not an
        # outage; the timeouts below stop jobs from wedging for hours.
        self._loop.set_default_executor(
            concurrent.futures.ThreadPoolExecutor(
                thread_name_prefix="jarvis-worker", max_workers=24))

        # 2026-09-30: if the Qt main loop dies (window closed, QApplication
        # quit) while this daemon thread is mid-reconnect-backoff, the
        # interpreter begins teardown and this loop can never recover — it
        # just spins "Transient network error"/"interpreter shutdown" every
        # few seconds on a dead executor. Watch for the main thread and bail
        # out the moment it's gone, so systemd (Restart=on-failure) brings a
        # clean process back instead of a zombie.
        _parent = threading.main_thread()
        def _watch_parent():
            import os
            while _parent.is_alive():
                time.sleep(1)
            print("[JARVIS] 💀 Main thread gone — exiting for clean systemd restart")
            os._exit(1)
        threading.Thread(target=_watch_parent, name="jarvis-parent-watch",
                         daemon=True).start()

        # ── Wire the shared core services to the interface ───────────────────
        # The confirmation gate is useless without a way to ask, and a memory
        # trim is invisible without a way to say so. Both are bound once here
        # rather than passed down through every action signature.
        confirm_gate.bind(
            show = self.ui.show_confirm,
            hide = self.ui.hide_confirm,
            log  = self.ui.write_log,
        )
        set_trim_notifier(self.ui.write_log)
        set_journal_notifier(self.ui.write_log)   # evolution + journal notices → HUD

        # Tell the device picker the exact rates the streams open at, from the
        # constants that actually open them — so it can never list a device that
        # cannot be opened at them.
        audio_devices.configure(SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE)

        # Enumerate audio devices off-thread. The settings drawer must never pay
        # for host-API enumeration on the Qt thread.
        audio_devices.prefetch()

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            # Double-voice fix: duck PC speakers while the phone mic streams
            # (see _play() — reply plays on the phone, not out of the PC too).
            self._dashboard.set_phone_mic_callback(self._on_phone_mic_state)
            # Mark-LIV: the phone's settings panel can toggle push-to-talk.
            self._dashboard.set_ptt_callback(self.set_push_to_talk)
            # RIO Home/Chat mute buttons — same ui.muted gate as the HUD.
            self._dashboard.set_mute_callback(self.set_mic_muted,
                                              lambda: bool(self.ui.muted))
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
            # Safety net: reconcile duck/pause even when the phone's socket
            # dies without a clean close (frozen tab) — see _watch_phone_mic.
            asyncio.create_task(self._watch_phone_mic())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                _resumed_with = self._resume_handle is not None
                config = self._build_config()
                # 2026-09-28 diagnostics: which surface + which handle this
                # connect carries — the rejection correlation needs it.
                print(f"[JARVIS] 🔌 connect: api_version={'v1alpha' if self._enhanced_live else 'v1beta'}, "
                      f"handle={'None (fresh)' if self._resume_handle is None else str(self._resume_handle)[:14] + '…'}")

                # Fresh client on every reconnect — avoids stale HTTP session state
                # v1alpha carries proactive audio; if it gets rejected we fall
                # back to v1beta.
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={
                        "api_version": "v1alpha" if self._enhanced_live else "v1beta",
                        # Same rationale as _save_session_summary: unbounded
                        # REST calls pin default-executor workers (DNS rides
                        # that pool) → reconnects starve.
                        "timeout": 120_000,  # ms
                    },
                )

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False
                    self._goaway_pending       = False
                    self._goaway_deadline      = 0.0
                    # A messaging request whose answer turn was lost to this
                    # reconnect must not silently bind its reply to the next
                    # unrelated voice answer — and its chat must not wait
                    # forever. Clear the slot, notify the chat once.
                    if self._inbound_channel:
                        self._notify_inbound_lost("reconnect")
                    self._pending_conv = ""

                    print("[JARVIS] Connected.")
                    # Backoff exists to survive outages — once we are actually
                    # connected, the next failure must start the climb from
                    # scratch, not inherit the last outage's penalty.
                    self._conn_backoff = 0
                    self._last_connect_err = None
                    self._net_fail_streak = 0
                    if _resumed_with:
                        # Say it plainly: the difference between "it reconnected"
                        # and "it reconnected and still knows what we were doing"
                        # is the whole point, and it is invisible otherwise.
                        self.ui.write_log("SYS: Reconnected — conversation restored.")
                        # Same conversation, new WebSocket: the uptime clock
                        # keeps running across rotations. (The clock itself is
                        # stamped at the first user turn below; a resumed
                        # connect never touches it.)
                    else:
                        # A connect without a resumption handle is a genuinely
                        # new conversation (first connect of the process, a
                        # rejected handle, a voice change) — restart the clock.
                        self._conversation_started = None

                    # Wake word: if enabled, come up ASLEEP (mic gated, silent)
                    # until the user says "Hey Jarvis" or taps wake in the UI.
                    if self._wake_enabled:
                        self._ensure_wake_detector()
                        self._awake = False
                        self.ui.set_state("SLEEPING")
                        self.ui.write_log("SYS: RIO online — sleeping. Say 'Hey Jarvis' to wake me.")
                    else:
                        self._awake = True
                        self.ui.set_state("LISTENING")
                        self.ui.write_log("SYS: RIO online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    self._reconnect_event.clear()  # ignore requests from before this session
                    tg.create_task(self._watch_reconnect())
                    tg.create_task(self._watch_goaway_deadline())
                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_background_monitor())
                    tg.create_task(self._run_camera_watch())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_sleep_watch())
                    tg.create_task(self._run_trading_learner())
                    tg.create_task(self._run_market_desk())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — fires once per process launch (if enabled).
                    # Skipped in wake-word mode: it comes up asleep, and a briefing
                    # would mean talking while "asleep".
                    if not self._briefing_sent and get_brief_enabled() and self._awake:
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                # SIGINT lands on the runner thread at an arbitrary await.
                # Raising it out of run() lets asyncio.run() tear the loop
                # down while the dashboard is STILL serving — handlers then
                # schedule on the dead default executor and the executor-
                # crash guard turns a plain Ctrl+C into SystemExit(1):
                # systemd (Restart=on-failure) resurrects the app (observed
                # 2026-10-12). Handle it HERE instead, inside the loop, where
                # the dashboard can be stopped BEFORE the executor dies —
                # exactly the window-close pipeline, same one-shot guard.
                print("\n🔴 Shutting down (Ctrl+C)…")
                await self.graceful_shutdown("sigint")
                return
            except SystemExit:
                raise
            except BaseException as e:
                # Executor already torn down (run loop escaped and asyncio
                # shut down, e.g. after the 2026-09-23 05:54 wedged session):
                # retrying schedules onto a dead executor and spins at ~50%
                # CPU forever — reconnect is impossible from here. Bail out
                # nonzero so systemd (Restart=on-failure) brings a clean
                # process back within seconds.
                # _is_executor_shutdown, not str(e): the TaskGroup wraps the
                # RuntimeError in a group whose str() carries none of the
                # message — the old check never fired and the loop spun at
                # 3–6s intervals after the UI was already dead (2026-09-30).
                if _is_executor_shutdown(e):
                    print("[JARVIS] 💀 Executor shutdown — exiting for clean systemd restart")
                    raise SystemExit(1)
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                # Voluntary reconnect (voice change) — not an error. Rebuild the
                # session immediately with no backoff and no scary logs.
                if _is_reconnect_signal(e):
                    print("[JARVIS] Voluntary reconnect requested.")
                    if not _keep_context_of(e):
                        # A deliberate clean slate (voice change) — drop the
                        # handle so the next connect really does start empty.
                        self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                # A resumption handle the server will not accept — expired, or
                # belonging to a session it has since dropped. Without this, the
                # same dead handle would be replayed on every retry and the
                # assistant would never come back at all: the feature meant to
                # survive a reconnect would be the thing preventing one. Drop it
                # once and let the next attempt start clean.
                if _resumed_with and (
                    "resum" in str(e).lower()
                    or "handle" in str(e).lower()
                    or "INVALID_ARGUMENT" in str(e)
                    or "NOT_FOUND" in str(e)
                    # Google's WS close codes arrive as bare numbers — "1008
                    # None. BidiGenerateContent session not found" contains no
                    # NOT_FOUND and no INVALID_ARGUMENT. 1008 "session not
                    # found" is exactly what a poisoned handle replays as: the
                    # session it points at is gone server-side (force-closed
                    # when a GoAway grace expires, or after a 1011 keepalive
                    # death), and without these markers the same dead handle
                    # was replayed on every retry, forever (2026-09-27: five
                    # identical attempts 3s apart, then ^C).
                    or "session not found" in str(e).lower()
                    or "1008" in str(e)
                ):
                    # The reason MATTERS (dead-session reaping vs expired
                    # handle vs protocol mismatch lead to different fixes),
                    # but a full traceback here is noise. One compact line —
                    # 2026-09-28: every rotation since 03:54 was rejected and
                    # this line is the only way to see what the server said.
                    print(f"[JARVIS] 🔗 Resumption handle rejected — starting a fresh session ({str(e)[:200]}) "
                          f"[sent handle: {str(self._resume_handle)[:14]}…]")
                    self.ui.write_log("SYS: Could not restore the conversation — starting fresh.")
                    # Graceful degradation: fold the recent conversation tail
                    # into the fresh session's system instruction (see
                    # _carry_context in _build_config). In-proc turns only —
                    # a process restart has nothing to carry.
                    if self._session_log:
                        tail = self._session_log[-12:]
                        self._carry_context = ("\n".join(tail))[-1800:]
                    self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                # Quota exhausted on the CURRENT key (2026-10-01): try the
                # next configured key before backing off. Deliberately placed
                # BEFORE the handle-rejection branch below — a 429 that kills
                # the socket is not a dead handle, and the key must move
                # together with the handle it is pinned to. In a multi-key
                # setup this turns "out of quota until midnight" into an
                # instant hop; with a single key it degrades to the plain
                # backoff retry, exactly as before.
                if _is_quota_error(e):
                    if _rotate_api_key():
                        print(f"[JARVIS] 🔑 429 on current key — rotating to key #{_gemini_key_index % max(1, len(_load_gemini_keys()))} "
                              f"(handle travels with it; retrying now)")
                        self.ui.write_log("SYS: Quota exhausted — switching to the next API key.")
                        self._conn_backoff = 0
                    else:
                        print("[JARVIS] 🔑 429 and no alternate key configured — backing off")
                        self._conn_backoff = min(max(getattr(self, "_conn_backoff", 3) * 2, 3), 60)
                    continue

                err_str = str(e)
                # Transient network blips (a cancelled connect, a dropped
                # handshake) are expected and retried below — a 20-line
                # traceback for a 3s retry buries the signal. One compact line
                # instead; every other error keeps the full traceback.
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    print(f"[JARVIS] ⏳ Transient network error ({type(e).__name__}) — retrying.")
                else:
                    print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                    traceback.print_exc()

                # Proactive audio rejected by the server (preview API drift) —
                # drop it and reconnect with the plain config.
                if self._enhanced_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "proactiv" in err_str.lower()
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                ):
                    self._enhanced_live = False
                    self.ui.write_log(
                        "SYS: Proactive audio unavailable — reconnecting without it."
                    )
                    continue

                # Invalid API key — stop hammering the API, prompt re-configuration.
                # With several keys configured, try the next one first: the
                # failure may be this key alone (revoked, wrong project), and
                # prompting for a new key that would then be the 7th of 7 is
                # a dead end. Only when every key has been tried does the
                # reconfiguration prompt fire.
                # 2026-10-14: bare "1007" was REMOVED from this branch — a
                # 1007 "Request contains an invalid argument" is a rejected
                # request (e.g. a bad tool-response payload), not a key
                # problem, and prompting for reconfiguration mid-conversation
                # wedged the assistant for a full tool cycle. 1007s that are
                # genuinely about the key carry "api key"/"unauthenticated"
                # in the reason and still land here; every other 1007 is
                # handled above as an invalid-argument reconnect.
                if ("API key not valid" in err_str
                        or "unauthenticated" in err_str.lower()
                        or ("1007" in err_str and "api key" in err_str.lower())):
                    if _rotate_api_key():
                        print(f"[JARVIS] 🔑 Key rejected — rotating to key #{_gemini_key_index % max(1, len(_load_gemini_keys()))}")
                        self.ui.write_log("SYS: API key rejected — switching to the next configured key.")
                        self._conn_backoff = 0
                        continue
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    self._conn_backoff = 3
                    continue

                # 2026-09-30: a second executor-shutdown shape — this one
                # unwrapped (websockets resolving DNS directly on the loop),
                # where even str(e) carries the message but the old grep-miss
                # meant this bare RuntimeError fell into the "not transient"
                # branch, spun at 3–6s, and printed a full traceback every
                # time. Belt-and-braces to the bailout above.
                if _is_executor_shutdown(e):
                    print("[JARVIS] 💀 Executor shutdown — exiting for clean systemd restart")
                    raise SystemExit(1)

                # Network / timeout errors — back off and retry
                if is_net_err:
                    # max(..., 3): after a success-reset (or a voluntary
                    # reconnect) the counter is 0 — doubling must floor at the
                    # 3s base or every retry would fire instantly, forever.
                    _conn_backoff = min(max(getattr(self, "_conn_backoff", 3) * 2, 3), 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Connection failed — retrying in {_conn_backoff}s. "
                        "(a VPN may be required)"
                    )

                    # Self-heal (2026-09-30): when the network is actually
                    # fine, THIS process is the outage — getaddrinfo rides the
                    # loop's default executor, and hung blocking jobs can pin
                    # ALL its workers, so every connect "times out" forever
                    # while curl succeeds (seen 2026-09-20/21; again today:
                    # 9h of retries while a fresh process connected in 0.6s).
                    # Retrying never recovers from that, and Restart=on-failure
                    # never fires because the process doesn't crash. Two
                    # stages, so an unnecessary restart is the LAST resort:
                    #   1. canary — a trivial executor job. If it stalls, the
                    #      pool is the wedge: rebuild it and wipe the streak.
                    #   2. evidence gate — repeated net-failures on a HEALTHY
                    #      canary mean the wedge is elsewhere in-proc: exit
                    #      nonzero so systemd (Restart=on-failure) brings a
                    #      clean process back — same contract as the
                    #      executor-shutdown bailout above.
                    self._net_fail_streak += 1
                    try:
                        await _executor_canary()
                    except Exception:
                        print("[JARVIS] 🔧 Default executor wedged (canary stalled) — rebuilding it")
                        try:
                            self._loop.set_default_executor(
                                concurrent.futures.ThreadPoolExecutor(
                                    thread_name_prefix="jarvis-worker",
                                    max_workers=24))
                        except RuntimeError:
                            # Loop already closed — nothing left to heal.
                            raise SystemExit(1)
                        self._net_fail_streak = 0
                    if self._net_fail_streak >= 5:
                        print(f"[JARVIS] 💀 {self._net_fail_streak} consecutive connect "
                              "timeouts on a healthy executor — exiting for clean "
                              "systemd restart")
                        raise SystemExit(1)
                else:
                    # A connect failure that is not transient must not hammer
                    # at 3s forever (2026-09-27: an identical 1008 five times,
                    # full traceback each, then ^C). When the same error
                    # repeats, escalate the backoff like a network outage and
                    # stop re-printing the traceback — the first one carries
                    # the signal; every repeat after that is one compact line.
                    _err_key = err_str[:200]
                    if getattr(self, "_last_connect_err", None) == _err_key:
                        # getattr, not a bare read: _conn_backoff is born on
                        # first assignment, never in __init__ — a first-ever
                        # connect failure reaching this branch must not crash
                        # the run loop with AttributeError.
                        self._conn_backoff = min(max(getattr(self, "_conn_backoff", 3) * 2, 3), 60)
                        print(f"[JARVIS] Same connect error repeating — retrying in {self._conn_backoff}s")
                    else:
                        self._conn_backoff = 3
                    self._last_connect_err = _err_key
            finally:
                self.session = None
                # Only save if there was a real conversation (≥3 turns).
                # Reset HERE, not only inside _save_session_summary: that runs
                # as a detached create_task, so the swap-back of the cleared
                # list races the next session's appends and can silently
                # discard the first turns of the fresh conversation.
                if len(self._session_log) >= 3:
                    async def _post_session():
                        await self._save_session_summary()
                        try:
                            await self._evolve_memory_pass()
                        except Exception as _evo:
                            print(f"[Evolution] ⚠️ post-session pass failed: {_evo}")
                    asyncio.create_task(_post_session())
                self._session_log = []

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

    # ── window-close shutdown (Objective: close RIO ⇒ close the app) ────────
    # Closing the RIO desktop window is the user's stop button for the whole
    # assistant. Stages, in the app's own lifecycle order:
    #   1. stop accepting input (dashboard intake + voice)
    #   2. cancel the session TaskGroup workers (audio, camera, monitors,
    #      proactive, trading-learner, market desk — each already handles
    #      CancelledError per its own reconnect/exit contract)
    #   3. flush the conversation journal + save the session summary
    #   4. stop the dashboard server (uvicorn :8000 + :8001 alias)
    #   5. close the RIO window process group (owned via its profile dir)
    #   6. bounded wait → exit 0 (so systemd Restart=on-failure does NOT
    #      resurrect a deliberately closed app; crashes still restart)
    async def _dashboard_shutdown(self) -> None:
        """Signal uvicorn (both servers) to exit their serve() loops."""
        import uvicorn as _uv
        from dashboard.server import DashboardServer as _DS
        d = self._dashboard
        # The class registry holds the servers started in serve(); ask uvicorn
        # for a graceful exit via its should_exit flag (documented control).
        for srv in [_DS.live, d]:
            pass
        # uvicorn Server instances are reachable via the running event loop's
        # tasks — signal any uvicorn.Server to stop.
        loop = asyncio.get_running_loop()
        for t in asyncio.all_tasks(loop):
            cb = getattr(t, "get_coro", None)
            coro = cb() if cb else None
            name = f"{coro.__name__ if coro else ''}"
            if "serve" in name.lower():
                t.cancel()

    # One-shot + idempotent: a second trigger can never restart the pipeline.
    async def graceful_shutdown(self, stage_name: str = "window") -> None:
        if getattr(self, "_shutting_down", False):
            return
        self._shutting_down = True
        _SHUTDOWN_TIMEOUT_S = 8.0
        print(f"[JARVIS] 🔻 Graceful shutdown ({stage_name}) — stopping workers…")

        # 1) no new inputs
        self._quit = True
        try:
            if self._dashboard is not None:
                self._dashboard._wake_callback = None
        except Exception:
            pass

        # 2) session TaskGroup workers + live session
        try:
            if self.session is not None:
                await self.session.close()
        except Exception:
            pass
        self.session = None

        # 3) flush memory BEFORE killing the loop's executors (journal appends
        #    run on the default executor — order matters; B1 lesson). The 5s
        #    budget covers ONLY the summary LLM call.
        try:
            await asyncio.wait_for(self._save_session_summary(), timeout=5.0)
        except Exception as e:
            print(f"[JARVIS] shutdown: session summary not saved ({e})")
        # 3b) Evolution consolidation OUTSIDE the summary's budget — when due
        #     it can run ~80s (2026-10-12: ~6000 turns through an LLM). It is
        #     awaited, not fire-and-forgotten, so the pass (and its gate
        #     commit in maybe_evolve) completes before harder exit stages get
        #     going. The hard-exit timer is armed only at stage 6, so this
        #     gets its full time here.
        try:
            _evolved = await self._evolve_memory_pass()
            if _evolved:
                print(f"[JARVIS] shutdown: {_evolved}")
        except Exception as e:
            print(f"[JARVIS] shutdown: evolution pass failed ({e})")

        # 4) stop the dashboard servers (main :8000 + alias :8001). The FastAPI
        # app holds the listening sockets; there is no server-side shutdown
        # hook, so the exit below tears them down with the process — bounded,
        # not blind: this call gives uvicorn a chance to finish in-flight
        # requests first.
        try:
            if self._dashboard is not None:
                await asyncio.wait_for(self._dashboard_shutdown(), timeout=4.0)
        except Exception as e:
            print(f"[JARVIS] shutdown: dashboard stop interrupted ({e})")

        # 5) close the RIO window child (owned via profile-dir match)
        try:
            import rio_window as _rw
            _rw.close_rio_window()
        except Exception:
            pass
        # NOTE: the consolidation pass itself lives in step 3b above, where it
        # cannot fall inside the summary's 5s wait_for. (Objective 4.6:
        # timeouts for graceful shutdown.)

        # 6) bounded exit — non-daemon threads must not hang the interpreter
        #    (B1); flush logs, then hard-exit 0 exactly once.
        def _hard_exit() -> None:
            print("[JARVIS] ✅ Shutdown complete — exiting 0.")
            try:
                sys.stdout.flush(); sys.stderr.flush()
            except Exception:
                pass
            _os._exit(0)
        threading.Timer(_SHUTDOWN_TIMEOUT_S, _hard_exit).start()
        _hard_exit()

class _RioWindowUI:
    """JarvisUI-compatible stand-in for RIO-as-default-window mode.
    Same public surface the rest of main.py already programs against
    (write_log, set_state, muted, on_text_command, wait_for_api_key, root, …)
    but:
      * write_log/set_state feed a status event stream that the RIO window
        polls — instead of painting a PyQt HUD nobody sees.
      * root.mainloop() blocks on a quit-flag poll — the real UI is the
        Chrome --app window, which is its own process; when this mainloop
        returns, the process exits.
      * _app.quit() flips that flag (SIGINT path) — same effect.
      * WINDOW-CLOSE SHUTDOWN: the mainloop also watches the RIO window
        process (owned via its dedicated Chrome profile dir). Once the window
        has actually been up, its disappearance is the user closing the app →
        triggers JarvisLive.graceful_shutdown() exactly once on the runner
        loop. This is what makes "close the RIO window" stop the assistant.
    """

    def __init__(self):
        self._quit = False
        self._log_lines: list[str] = []          # bounded status ring
        self._state = "IDLE"
        self._lock = threading.Lock()
        self._shutdown_started = False            # one-shot guard (Objective 4.5)

        class _NoRoot:
            def mainloop(self, *a, **kw):
                # Qt event loop NOT started (no QApplication exists here);
                # park the main thread on a plain sleep loop until quit.
                # + window-close watch (only meaningful in RIO-window mode).
                import rio_window as _rw
                host = self._host
                seen_window = False
                grace = 60.0                      # window must appear within 60s
                deadline = time.monotonic() + grace
                while not host._quit:
                    try:
                        up = _rw.window_running()
                    except Exception:
                        up = seen_window          # probe failure ≠ user closed
                    if up:
                        seen_window = True
                        deadline = time.monotonic() + grace
                    elif seen_window:
                        # the window the user had open is gone → they closed RIO
                        if host._on_window_closed and not host._shutdown_started:
                            host._shutdown_started = True
                            print("[RIO window] closed by user — shutting the "
                                  "assistant down cleanly…")
                            host._on_window_closed()
                        return                     # mainloop ends; process exits
                    elif time.monotonic() > deadline:
                        # window never appeared (headless/cert trouble): do NOT
                        # kill the assistant — it still serves phone/Hermes.
                        pass
                    time.sleep(0.5)
            def protocol(self, *a, **kw):
                pass
            _host = None  # set below

        self.root = _NoRoot()
        self.root._host = self
        self._app = _NoApp()
        self._app._host_ref = self                     # so quit() flips _quit
        self._on_window_closed = None                  # set by main()

    def request_shutdown(self) -> None:
        """Window gone → flip the quit flag (idempotent like _app.quit())."""
        self._quit = True

    # ── surface the rest of main.py already calls ────────────────────
    def write_log(self, text: str) -> None:
        with self._lock:
            self._log_lines.append(str(text))
            if len(self._log_lines) > 200:
                del self._log_lines[:-200]
        print(text, flush=True)

    def set_state(self, state: str) -> None:
        self._state = str(state)

    @property
    def state(self) -> str:
        return self._state

    def recent_lines(self, n: int = 12) -> list[str]:
        with self._lock:
            return list(self._log_lines[-n:])

    # No-ops / benign values — nothing HUD-specific should leak
    muted = False
    current_file = None
    def show_content(self, *_a, **_k): pass
    def set_audio_level(self, *_a, **_k): pass
    def set_mic_paused(self, *_a, **_k): pass
    def start_camera_stream(self): pass
    def stop_camera_stream(self): pass
    def show_camera_frame(self, *_a, **_k): pass
    def show_confirm(self, *_a, **_k): pass
    def hide_confirm(self): pass
    def prompt_reconfig(self): pass
    def notify_phone_connected(self): pass
    def push_visemes(self, *_a, **_k): pass
    def wait_for_api_key(self): pass
    # Callback slots — JarvisLive assigns these; defaults are no-ops so an
    # unassigned slot still does nothing dangerous.
    on_text_command        = None
    on_remote_clicked      = None
    on_interrupt           = None
    on_voice_change        = None
    on_audio_device_change = None
    on_wake_toggle         = None
    on_wake_manual         = None
    on_wake_install        = None
    on_push_to_talk        = None
    get_plugins            = None
    get_plugin_settings    = None
    uptime_getter          = None
    wake_is_ready          = None
    wake_get_state         = None
    ptt_hold               = None

    # JarvisUI.root.mainloop is called from main(); _app.quit() from the
    # SIGINT handler maps onto it via the flag above.


class _NoApp:
    """QApplication.quit-shim: sets the host's quit flag."""
    def __init__(self):
        self._host_ref = None
    def quit(self):
        if self._host_ref is not None:
            self._host_ref._quit = True


def main():
    # JARVIS_RIO_WINDOW=1 (default): RIO's web UI becomes the desktop window
    # and the old PyQt HUD is not shown at all — no flash, no overlap (Phase:
    # "make RIO the default desktop UI"). The voice loop, dashboard, Hermes
    # and every agent run exactly as before; only the window surface swaps.
    # Set JARVIS_RIO_WINDOW=0 to restore the old HUD as the window surface.
    _rio_window_mode = False
    try:
        _rio_window_mode = _os.environ.get("JARVIS_RIO_WINDOW", "1").strip() != "0"
    except Exception:
        pass

    if _rio_window_mode:
        ui = _RioWindowUI()
    else:
        ui = JarvisUI("face.png")

    # Ctrl+C must not raise KeyboardInterrupt inside an arbitrary Qt slot:
    # the exception escapes the slot, Qt treats it as fatal and the process
    # dies with abort() + core dump (2026-09-27: ^C during a timer repaint →
    # "Aborted (core dumped)"). Catch the signal instead: quit the Qt loop
    # cleanly and let the daemon threads die with the process.
    import signal as _signal
    # SIGINT → the SAME graceful pipeline the window close uses, NOT
    # ui._app.quit(): the old quit path unwound asyncio.run() while the
    # dashboard was still serving, in-flight handlers scheduled on the dead
    # default executor, and the executor-crash guard turned a plain Ctrl+C
    # into SystemExit(1) — which systemd (Restart=on-failure) treats as a
    # crash and RESURRECTS the app (observed 2026-10-12). In RIO-window
    # mode the app-quit flag does nothing anyway (no Qt loop), so route the
    # signal straight into the runner's loop where shutdown is orchestrated
    # (stop intake → close session → flush memory → stop dashboard →
    # bounded exit 0, all under graceful_shutdown's one-shot guard).
    _hold = {"jarvis": None}

    def _sigint(_sig, _frm):
        print("\n🔴 Shutting down (SIGINT)…")
        jarvis = _hold.get("jarvis")   # _hold hoisted above this handler
        if jarvis is not None:
            loop = jarvis._loop
            if loop is not None and not loop.is_closed():
                try:
                    asyncio.run_coroutine_threadsafe(
                        jarvis.graceful_shutdown("sigint"), loop)
                    return
                except Exception:
                    pass
        # Runner not up yet (or loop already gone): nothing to stop
        # gracefully — flip the quit flag and let main() exit; graceful 
        # paths below also handle the not-yet-connected case.
        ui.request_shutdown()
    try:
        _signal.signal(_signal.SIGINT, _sigint)
    except ValueError:
        pass  # handler not installable off the main thread — ignore
    # Python-level handlers only run when the interpreter executes bytecode;
    # Qt's C++ event loop can starve them. A no-op timer hands control back
    # ~10×/s so the SIGINT handler is actually reached. Skipped in RIO-window
    # mode: there is no Qt event loop, and _NoRoot.mainloop polls the quit
    # flag itself.
    if not _rio_window_mode:
        from PyQt6.QtCore import QTimer as _QTimer
        _sig_timer = _QTimer()
        _sig_timer.timeout.connect(lambda: None)
        _sig_timer.start(100)

    # (RIO window mode: the opener below discovers the live DashboardServer
    # through the DashboardServer.live registry — set in serve() — so no
    # holder plumbing is needed between JarvisLive and the opener.)

    def runner():
        ui.wait_for_api_key()
        # Session-aware input: on Wayland, pyautogui's clicks/typing are
        # ignored by the compositor. Patch them to ydotool (kernel uinput)
        # when available; screenshots are routed through the Wayland capture
        # path. No-op on X11.
        try:
            from core.input_bridge import apply_patch
            apply_patch()
        except Exception as _e:
            print(f"[Input] ⚠️  input bridge failed: {_e}")
        # LLM failover: when the Gemini free tier hits quota, auxiliary text
        # calls (search, news, summaries, code helper…) continue on OpenRouter.
        try:
            from core.llm_router import install_router
            install_router()
        except Exception as _e:
            print(f"[LLM] ⚠️  failover router failed to install: {_e}")
        jarvis = JarvisLive(ui)
        _hold["jarvis"] = jarvis
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")
            # Route SIGINT through the SAME graceful pipeline the window close
            # uses. asyncio.run() alone would tear down the loop's default
            # executor while uvicorn is still serving — in-flight handlers
            # (rio_health) and workers (_run_system_monitor) then schedule on
            # the dead executor and raise "cannot schedule new futures after
            # shutdown"; the run-loop's executor-crash guard folds that into
            # SystemExit(1) and systemd (Restart=on-failure) treats a plain
            # Ctrl+C as a crash and RESURRECTS the app (observed 2026-10-12).
            # graceful_shutdown already stops intake, closes the live session,
            # flushes memory, awaits the evolution pass, STOPS THE DASHBOARD
            # first, and hard-exits 0 under a timer — idempotent and safe to
            # call even if a window-close shutdown already ran.
            loop = jarvis._loop
            if loop is not None and not loop.is_closed():
                try:
                    asyncio.run_coroutine_threadsafe(
                        jarvis.graceful_shutdown("sigint"), loop)
                except Exception:
                    pass
            raise SystemExit(0)

    def _on_window_closed():
        """User closed the RIO desktop window → run graceful_shutdown() on
        the runner's event loop. Runs on the MAIN thread (mainloop watcher):
        asyncio.run_coroutine_threadsafe is the thread-safe bridge into the
        runner loop. Idempotent — the one-shot guard lives in both the UI
        (_shutdown_started) and graceful_shutdown itself."""
        jarvis = _hold.get("jarvis")
        if jarvis is None:
            # runner still starting up (or already gone): nothing to stop
            # gracefully — exit now with code 0 so the service doesn't restart.
            print("[JARVIS] 🔻 Window closed before the assistant was up — exit 0.")
            _os._exit(0)
        loop = jarvis._loop
        if loop is None or loop.is_closed():
            _os._exit(0)
        fut = asyncio.run_coroutine_threadsafe(
            jarvis.graceful_shutdown("window"), loop)
        # keep the main thread alive so shutdown logs + the bounded hard-exit
        # timer (inside graceful_shutdown) can run: block briefly, the timer
        # ends the process either way.
        try:
            fut.result(timeout=15.0)
        except Exception:
            pass
        time.sleep(10.0)      # let the 8s bounded timer fire os._exit(0)

    threading.Thread(target=runner, daemon=True).start()

    if _rio_window_mode:
        # Open the RIO desktop window as soon as the dashboard is live. A
        # daemon thread so a slow backend never blocks the SIGINT path; the
        # opener itself refuses to open a dead window (returns 2) and logs,
        # and it discovers the live DashboardServer via the class registry.
        import rio_window as _rw
        ui._on_window_closed = _on_window_closed
        def _opener():
            from dashboard.server import DashboardServer as _DS
            for _ in range(120):                    # ≤60 s for dashboard.serve()
                if _DS.live is not None:
                    break
                time.sleep(0.5)
            _rw.open_rio_window(dashboard=_DS.live, wait_secs=20.0)
        threading.Thread(target=_opener, daemon=True).start()

    ui.root.mainloop()

if __name__ == "__main__":
    main()