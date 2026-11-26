"""media_bus.py — JARVIS → dashboard playback handoff (YouTube/JioSaavan/…).

Actions in main.py's process (actions/media_play.py) resolve a request into a
playable payload and push() it here; the RIO UI long-polls GET
/api/rio/media/next and renders the right player. A worker thread cannot
await the server's asyncio loop, so delivery is a plain thread-safe queue —
the same honesty as hermes_events' journal fallback, just with a consumer.

push() also tries a live broadcast when a loop IS running (in-loop callers
get instant delivery; the poll is the safety net for worker threads).
"""
from __future__ import annotations

import threading
import time
from collections import deque

_lock = threading.Lock()
_queue: deque[dict] = deque(maxlen=8)
_current: dict | None = None        # what the UI should be playing now
_state: dict | None = None          # what the UI REPORTED it did with it
_cmd: dict | None = None            # pending transport command for the dock
_seq = 0


def push(payload: dict) -> dict:
    """Queue one play payload (dict with source/url/videoId/title/...)."""
    global _current, _state, _seq, _cmd
    with _lock:
        _seq += 1
        payload = dict(payload)
        payload["seq"] = _seq
        payload["ts"] = time.time()
        _queue.append(payload)
        _current = payload
        _state = None               # new payload, no word from the UI yet
        _cmd = None                 # a fresh play supersedes a pending
                                    # pause/resume (pause A → play B must
                                    # never pause B)
    _try_broadcast({"type": "media_event", "kind": "play", "media": payload})
    return payload


def stop() -> dict:
    """Clear the current media (UI stops the player).

    Also drains the queue: a pending payload that pops AFTER a stop (or
    after the speaker fallback took over) would start the dock late and
    double-play over the local player.

    Broadcast ONLY when something was actually cleared: a page that runs
    old JS answers a stop broadcast with its own POST /media/stop — if the
    server rebroadcast that too, the two sides would echo each other into a
    stop-storm that drains every play payload (observed 2026-10-17)."""
    global _current, _state
    with _lock:
        changed = _current is not None or bool(_queue)
        _current = None
        _state = None
        _queue.clear()
    if changed:
        _try_broadcast({"type": "media_event", "kind": "stop"})
    return {"ok": True}


def set_state(status: str, detail: str = "") -> dict:
    """The RIO dock reports what its player ACTUALLY did — playing, paused,
    blocked (Chrome refused autoplay), or error (stream failed). Without
    this, 'play' claims success it cannot see and now_playing guesses."""
    global _state
    with _lock:
        _state = {"status": str(status or "").strip().lower()[:16],
                  "detail": str(detail or "").strip()[:120],
                  "ts": time.time(),
                  "seq": _current.get("seq", 0) if _current else 0}
        return dict(_state)


def set_cmd(kind: str, value: str = "") -> dict:
    """Queue one transport command (pause / resume / stop) for the dock.

    Commands arrive from two callers: voice actions run in WORKER threads
    (no asyncio loop → the dock picks this up on its next poll, ≤ ~1 s) and
    controller/HTTP endpoints run IN the loop (the broadcast below lands
    instantly on every open page). The command is also stored so the poll
    fallback works when a page's WebSocket is down; the poll with cmd=1
    consumes it exactly once."""
    global _cmd
    kind = str(kind or "").strip().lower()
    if kind not in ("pause", "resume", "stop"):
        return {"ok": False, "error": f"unknown command '{kind}'"}
    with _lock:
        _cmd = {"kind": kind, "value": str(value or "")[:40],
                "ts": time.time()}
    _try_broadcast({"type": "media_event", "kind": kind,
                    "value": str(value or "")[:40]})
    return {"ok": True, "kind": kind}


def pop_cmd() -> dict | None:
    """Consume the pending transport command (one-shot).

    Commands older than 10 s are discarded, not delivered: an undelivered
    pause (page was closed) must never surface minutes later and pause a
    fresh song the user just started."""
    global _cmd
    with _lock:
        cmd, _cmd = _cmd, None
    if cmd and time.time() - cmd.get("ts", 0) > 10:
        return None
    return dict(cmd) if cmd else None


def state() -> dict | None:
    with _lock:
        return dict(_state) if _state else None


def current() -> dict | None:
    with _lock:
        return dict(_current) if _current else None


def pop(wait_s: float = 0.0) -> dict | None:
    """Consume the oldest queued payload; optionally block up to wait_s
    (the UI long-poll). Returns None when nothing arrived.

    A pending transport command also ends the long-poll immediately — a
    voice 'pause' must not sit out the remaining 4 s of a wait=4 poll when
    the WebSocket broadcast was unavailable."""
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        with _lock:
            if _queue:
                return _queue.popleft()
            if _cmd:
                return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.15)


def _try_broadcast(msg: dict) -> None:
    """Fast path: broadcast directly when called from the server's loop."""
    try:
        import asyncio
        from dashboard.server import DashboardServer
        srv = DashboardServer.live
        if srv is None:
            return
        loop = asyncio.get_running_loop()
        loop.create_task(srv.broadcast(msg))
    except Exception:
        pass        # worker thread or server down — the poll path covers it
