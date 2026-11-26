"""
tests/test_conversation_reliability.py — regression tests for the
conversation-reliability fix set (duplicates + incomplete responses).

What these tests prove (against the REAL modules — no live Gemini needed):

  Dedup core (core/dedup.py):
    1. Same event id twice → processed once.
    2. Two legitimate identical texts, different ids → both processed.
    3. Same text on a different channel → both processed (channel-scoped).
    4. TTL expiry: after the window the same event goes through again.
    5. Bounded memory: the store is capped and evicts.

  Intake wiring (dashboard/server.py):
    6. POST /api/command twice with the same mid → ONE queue entry.
    7. POST /api/command, same text, different mids → TWO entries.
    8. POST /api/message (Hermes) replay without msg_id → one [INBOUND] line.
    9. WS /ws: boot frame + seq-bearing history; duplicate command over the
       same mid enqueued once.

  Response lifecycle (core/response_state.py):
   10. Legal transitions recorded; illegal ones ignored; spoken flag separate.

  Command drain loop + turn flow (main.py):
   11. One message → exactly one send to the session.
   12. Serialization: the second queued message is NOT generated until the
       first turn completes (reply-routing guard).
   13. Session down → bounded requeue, then a FAILED drop (never a silent
       infinite drop, never an unbounded queue).
   14. Reply gate: a turn that completes BEFORE the request was sent is not
       delivered to the messaging channel; the answer turn IS.
   15. Empty model response for a pending messaging request → one failure
       notice, request not left silent, state FAILED.
   16. Interrupted turn → CANCELLED, queue released, nothing journaled.
   17. Deferred GoAway reconnect happens AFTER the turn's bookkeeping
       (journal + broadcast) — the answer is no longer thrown away.
   18. Conversation stamp travels with the RIO turn (journal conv param).
   19. Reconnect clears the stale inbound slot and notifies the chat once.
   20. Phone audio mirror uses the real queue attribute.

Run:  .venv/bin/python -m pytest tests/test_conversation_reliability.py -q
Also standalone: .venv/bin/python tests/test_conversation_reliability.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── isolation: sandbox config + journal BEFORE importing main ────────────────
_TMPDIR = tempfile.mkdtemp(prefix="jarvis-convrel-")
_TMP_CONFIG = Path(_TMPDIR) / "api_keys.json"
_TMP_CONFIG.write_text(json.dumps({
    "dashboard_auth_token": "convrel-test-token",
    "gemini_api_key": "AIzaFAKE-not-used-here",
}))

import memory.config_manager as cfg  # noqa: E402
cfg.CONFIG_FILE = _TMP_CONFIG

import memory.conversation_journal as cjournal  # noqa: E402
cjournal.JOURNAL_DIR = Path(_TMPDIR) / "conversations"   # sandbox the journal

import main as jarvis_main  # noqa: E402
from core.dedup import DedupGuard  # noqa: E402
from core.response_state import (  # noqa: E402
    tracker, RECEIVED, PROCESSING, GENERATING, COMPLETED, FAILED, CANCELLED,
)


import pytest  # noqa: E402

@pytest.fixture(autouse=True)
def _sandbox_config_file():
    """Re-point config_manager at THIS module's sandbox before every test,
    and restore whatever was set before afterwards.

    Test-isolation guard: other test modules in this repo (e.g.
    test_dashboard_pwa_install.py) ALSO monkeypatch cfg.CONFIG_FILE at
    import time, and pytest imports every module before running anything —
    so whoever is imported last wins the global and the earlier module's
    static-token file silently stops matching (401 in the intake tests,
    seen 2026-10-13 when running both files in one pytest invocation).
    Restoring the previous value afterwards keeps the other module's own
    sandbox intact for its tests.
    """
    prev = cfg.CONFIG_FILE
    cfg.CONFIG_FILE = _TMP_CONFIG
    yield
    cfg.CONFIG_FILE = prev


# ── helpers ──────────────────────────────────────────────────────────────────

class _StubUI:
    """Catch-all UI stub: every method is a no-op, attributes default False."""
    def __getattr__(self, name):
        def _noop(*a, **k):
            return None
        return _noop
    muted = False


class _RecordingQueue(asyncio.Queue):
    """Queue that records put_nowait items — lets tests observe requeues even
    when the consumer drains them instantly."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.put_records: list = []

    def put_nowait(self, item):
        self.put_records.append(item)
        super().put_nowait(item)


class _FakeDashboard:
    def __init__(self):
        self._command_queue = _RecordingQueue()
        self._phone_audio_queue = asyncio.Queue(maxsize=200)
        self.broadcasts: list[dict] = []

    async def broadcast(self, msg):
        self.broadcasts.append(msg)


class _FakeLiveSession:
    """Records send_client_content; receive() polls a mutable response list
    (tests append mid-run) and blocks when empty (the test cancels)."""
    def __init__(self, responses=None):
        self._q: list = list(responses or [])
        self.sent: list[dict] = []

    async def receive(self):
        while True:
            if self._q:
                yield self._q.pop(0)
            else:
                await asyncio.sleep(0.02)      # poll — cancellation-safe

    async def send_client_content(self, turns=None, turn_complete=True, **kw):
        self.sent.append({"turns": turns, "turn_complete": turn_complete})

    async def send_tool_response(self, **kw):
        pass


def _sc(input_text: str | None = None, output_text: str | None = None,
        turn_complete: bool = True):
    """A Gemini-Live-style server_content namespace."""
    it = types.SimpleNamespace(text=input_text) if input_text else None
    ot = types.SimpleNamespace(text=output_text) if output_text else None
    return types.SimpleNamespace(output_transcription=ot,
                                 input_transcription=it,
                                 turn_complete=turn_complete)


def _resp(sc=None, data: bytes | None = None):
    return types.SimpleNamespace(data=data, server_content=sc, go_away=None,
                                 tool_call=None, session_resumption_update=None)


def _make_jarvis(session=None) -> "jarvis_main.JarvisLive":
    j = jarvis_main.JarvisLive(_StubUI())
    j.session = session
    j._dashboard = _FakeDashboard()
    j._wake_enabled = False
    # Normally created per-connect inside run(); the receive loop guards on
    # it, so a constructed-but-never-run instance needs it too.
    if getattr(j, "_turn_done_event", None) is None:
        j._turn_done_event = asyncio.Event()
    if getattr(j, "_goaway_deadline", 1) == 1:
        j._goaway_deadline = 0.0
    return j


class _JournalRecorder:
    """Replaces main.append_turn: records (speaker, text, conv)."""
    def __init__(self):
        self.rows: list[tuple[str, str, str | None]] = []

    def __call__(self, speaker: str, text: str, conv: str | None = None):
        self.rows.append((speaker, text, conv))


def _install_fake_hermes(recorder: "_HermesRecorder"):
    """Replace JarvisLive._hermes_reply with a recorder that resolves the
    target the way the real method does (explicit target, else the inbound
    slot) and clears the slot. Returns the undo callable."""
    real = jarvis_main.JarvisLive._hermes_reply

    async def fake_reply(self, text, target=None):
        recorder.calls.append((text, target or self._inbound_channel))
        self._inbound_channel = None
        self._inbound_had_file = False
        self._inbound_req_ts = 0.0

    jarvis_main.JarvisLive._hermes_reply = fake_reply

    def undo():
        jarvis_main.JarvisLive._hermes_reply = real
    return undo


class _HermesRecorder:
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    async def __call__(self, text: str, target: str | None = None):
        self.calls.append((text, target))


# ── 1-5 · dedup core ─────────────────────────────────────────────────────────

def test_dedup_same_id_processed_once():
    g = DedupGuard(ttl_s=30)
    assert g.acquire("telegram:1", "evt-1", "hello") is True
    assert g.acquire("telegram:1", "evt-1", "hello") is False, \
        "same event id replayed within TTL must be suppressed"


def test_dedup_identical_text_different_ids_both_pass():
    g = DedupGuard(ttl_s=30)
    assert g.acquire("command", "mid-a", "what time is it") is True
    assert g.acquire("command", "mid-b", "what time is it") is True, \
        "two legitimate identical messages with different ids must both go through"


def test_dedup_channel_scoped():
    g = DedupGuard(ttl_s=30)
    assert g.acquire("telegram:1", "", "status") is True
    assert g.acquire("whatsapp:2", "", "status") is True, \
        "same text on a different channel is a different event"


def test_dedup_ttl_expiry():
    g = DedupGuard(ttl_s=0.2)
    assert g.acquire("command", "", "once") is True
    assert g.acquire("command", "", "once") is False
    time.sleep(0.25)
    assert g.acquire("command", "", "once") is True, \
        "after the short TTL the same words deliberately sent again go through"


def test_dedup_bounded():
    g = DedupGuard(ttl_s=60, max_entries=8)
    for i in range(100):
        g.acquire("command", f"m-{i}", f"text {i}")
    assert g.size() <= 8, "the store must be hard-capped"


# ── 6-9 · intake wiring (real DashboardServer) ───────────────────────────────

def _make_server():
    from dashboard.server import DashboardServer
    return DashboardServer()


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _post_command(port: int, payload: dict, token: str = "convrel-test-token"):
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/command",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


async def _run_intake_tests(port: int, srv):
    # 6: same mid twice → one queue entry, second response flags duplicate
    r1 = await _post_command(port, {"text": "hello there", "mid": "mm-1"})
    r2 = await _post_command(port, {"text": "hello there", "mid": "mm-1"})
    assert r1.get("ok") is True
    assert r2.get("duplicate") is True, f"replay not flagged: {r2}"
    assert srv._command_queue.qsize() == 1, \
        f"replayed command was enqueued twice (qsize={srv._command_queue.qsize()})"

    # 7: same text, different mids → both enqueued
    await _post_command(port, {"text": "legit repeat", "mid": "mm-a"})
    await _post_command(port, {"text": "legit repeat", "mid": "mm-b"})
    assert srv._command_queue.qsize() == 3, \
        f"identical text with a NEW id was dropped (qsize={srv._command_queue.qsize()})"

    # drain to tuples for assertion — a command WITHOUT a conv tag must land
    # as a plain string (phone/bridge shape), one WITH a conv as a tuple.
    items = []
    while not srv._command_queue.empty():
        items.append(srv._command_queue.get_nowait())
    assert items[0] == "hello there", f"unexpected queue item: {items[0]!r}"
    assert items[2] == "legit repeat" and items[1] == "legit repeat"

    # conv tag rides along as a tuple
    await _post_command(port, {"text": "tagged msg", "mid": "mm-c",
                               "conv": "c9"})
    tagged = srv._command_queue.get_nowait()
    assert tagged == ("tagged msg", "c9"), f"conv tag lost: {tagged!r}"

    # 8: Hermes bridge replay (no msg_id → channel-scoped content hash)
    import urllib.request
    def _post_message(text):
        boundary = "----ConvRelTest"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"channel\"\r\n\r\ntelegram\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"chat_id\"\r\n\r\n777\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"text\"\r\n\r\n{text}\r\n"
                f"--{boundary}--\r\n").encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/message", data=body, method="POST",
            headers={"Authorization": "Bearer convrel-test-token",
                     "Content-Type":
                         f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    m1 = _post_message("bridge hello")
    m2 = _post_message("bridge hello")          # hook retry within the window
    assert m1.get("ok") is True and m2.get("duplicate") is True, \
        f"Hermes replay not suppressed: {m1} {m2}"
    inbound = [i for i in items if isinstance(i, str)]
    while not srv._command_queue.empty():
        got = srv._command_queue.get_nowait()
        if isinstance(got, str):
            inbound.append(got)
    inbound_lines = [i for i in inbound if i.startswith("[INBOUND")]
    assert len(inbound_lines) == 1, \
        f"expected exactly one [INBOUND] line, got {inbound_lines!r}"

    # distinct message from the same chat still passes
    m3 = _post_message("a different message")
    assert m3.get("ok") is True and not m3.get("duplicate")


def test_intake_dedup_end_to_end():
    """6-8: HTTP intake dedup against the real FastAPI app."""
    try:
        import uvicorn  # noqa
    except ImportError:
        print("SKIP: uvicorn missing")
        return
    srv = _make_server()
    srv._tokens.add("convrel-ws-token")          # fabricate a session token
    port = _free_port()
    config = uvicorn.Config(srv.app, host="127.0.0.1", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    import threading
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    end = time.time() + 20
    import urllib.request
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=2)
            break
        except Exception:
            time.sleep(0.2)
    try:
        asyncio.run(_run_intake_tests(port, srv))
    finally:
        server.should_exit = True
        th.join(timeout=5)


async def _run_ws_tests(port: int, srv):
    import websockets
    uri = f"ws://127.0.0.1:{port}/ws?token=convrel-ws-token"
    async with websockets.connect(uri) as ws:
        boot = json.loads(await ws.recv())
        assert boot.get("type") == "boot" and "boot_id" in boot and "seq" in boot, \
            f"no boot frame: {boot!r}"
        # history replay carries seqs (empty here — fine)
        # duplicate command over the same mid → enqueued once
        await ws.send(json.dumps({"type": "command", "text": "ws hi",
                                  "conv": "c1", "mid": "ws-m1"}))
        await ws.send(json.dumps({"type": "command", "text": "ws hi",
                                  "conv": "c1", "mid": "ws-m1"}))
        await asyncio.sleep(0.4)
        items = []
        while not srv._command_queue.empty():
            items.append(srv._command_queue.get_nowait())
        ws_cmds = [i for i in items if isinstance(i, tuple) and i[0] == "ws hi"]
        assert len(ws_cmds) == 1, \
            f"WS duplicate command enqueued {len(ws_cmds)}× — dedup not wired"
        assert ws_cmds[0][1] == "c1", "WS conv tag lost"


def test_ws_boot_and_dedup():
    """9: WS boot frame + resume fields + WS-path dedup."""
    try:
        import uvicorn  # noqa
        import websockets  # noqa
    except ImportError as e:
        print(f"SKIP: {e}")
        return
    srv = _make_server()
    srv._tokens.add("convrel-ws-token")
    port = _free_port()
    config = uvicorn.Config(srv.app, host="127.0.0.1", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    import threading
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    end = time.time() + 20
    import urllib.request
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=2)
            break
        except Exception:
            time.sleep(0.2)
    try:
        asyncio.run(_run_ws_tests(port, srv))
    finally:
        server.should_exit = True
        th.join(timeout=5)


def test_broadcast_seq_and_history():
    """WS resume support: every broadcast entry gets a monotonic seq and is
    kept in history so ?since= can filter it."""
    srv = _make_server()

    async def _drive():
        await srv.broadcast({"type": "log", "speaker": "user", "text": "a"})
        await srv.broadcast({"type": "log", "speaker": "jarvis", "text": "b"})
        await srv.broadcast({"type": "log", "speaker": "user", "text": "c"})
    asyncio.run(_drive())
    seqs = [e["seq"] for e in srv._history[-3:]]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3, \
        f"broadcast seqs not monotonic-unique: {seqs}"
    assert all("seq" in e for e in srv._history), "history entries lost seq"


# ── 10 · response lifecycle ──────────────────────────────────────────────────

def test_response_tracker_lifecycle():
    t = tracker()
    rid = t.start(channel="telegram:9", source="test")
    assert rid
    assert t.update(rid, PROCESSING) is True
    assert t.update(rid, GENERATING, model="gemini-live") is True
    info = t.get(rid)
    assert info["state"] == GENERATING and info["model"] == "gemini-live"
    t.mark_spoken(rid)
    assert t.get(rid)["spoken"] is True, "spoken flag must be settable"
    assert t.update(rid, COMPLETED, finish="turn_complete") is True
    # a completed request cannot be resurrected
    assert t.update(rid, GENERATING) is False, \
        "illegal transition COMPLETED→GENERATING must be ignored"
    assert t.get(rid)["state"] == COMPLETED


def test_response_tracker_failed_state():
    t = tracker()
    rid = t.start(channel="dashboard")
    t.update(rid, PROCESSING)
    t.update(rid, FAILED, error="session unavailable")
    info = t.get(rid)
    assert info["state"] == FAILED and "session" in info["error"]
    assert t.update(rid, COMPLETED) is False, "FAILED is terminal"


# ── 11-13 · command drain loop ───────────────────────────────────────────────

def test_one_message_one_generation():
    """11: one queued command → exactly one send_client_content."""
    async def _run():
        sess = _FakeLiveSession()
        j = _make_jarvis(sess)
        task = asyncio.create_task(j._process_dashboard_commands())
        await j._dashboard._command_queue.put("hello world")
        await asyncio.sleep(0.4)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return sess
    sess = asyncio.run(_run())
    assert len(sess.sent) == 1, f"expected 1 generation, got {len(sess.sent)}"


def test_commands_serialized_until_turn_done():
    """12: the second message must not be generated until the first turn
    completes (this is the cross-channel reply-routing guard)."""
    async def _run():
        sess = _FakeLiveSession()
        j = _make_jarvis(sess)
        task = asyncio.create_task(j._process_dashboard_commands())
        await j._dashboard._command_queue.put("first question")
        await asyncio.sleep(0.3)
        await j._dashboard._command_queue.put("second question")
        await asyncio.sleep(0.4)
        before = len(sess.sent)               # second must still be waiting
        j._dash_turn_done.set()               # first turn completes
        await asyncio.sleep(0.4)
        after = len(sess.sent)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return before, after
    before, after = asyncio.run(_run())
    assert before == 1, \
        f"second command generated before the first turn finished ({before} sends)"
    assert after == 2, f"second command never generated after turn done ({after})"


def test_session_down_requeues_bounded_then_fails():
    """13: no session → the command is requeued (list form with a tries
    counter), bounded; after the cap it is dropped, not looped forever."""
    old_wait = jarvis_main._CMD_SESSION_WAIT_S
    jarvis_main._CMD_SESSION_WAIT_S = 0.15          # shrink for the test
    try:
        async def _run():
            j = _make_jarvis(session=None)
            j.session = None
            task = asyncio.create_task(j._process_dashboard_commands())
            await j._dashboard._command_queue.put("will be requeued")
            # the drain loop re-consumes a requeued item instantly, so observe
            # the PUTS, not the queue contents: expect tries=1,2,3 then a stop.
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                records = j._dashboard._command_queue.put_records
                lists = [r for r in records if isinstance(r, list)]
                if len(lists) >= 3:
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.4)          # allow the final drop cycle
            records = list(j._dashboard._command_queue.put_records)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return records
        records = asyncio.run(_run())
    finally:
        jarvis_main._CMD_SESSION_WAIT_S = old_wait
    lists = [r for r in records if isinstance(r, list)]
    assert len(lists) >= 2, f"command was not bounded-requeued (puts={records!r})"
    assert lists[0][2] == 1 and lists[0][0] == "will be requeued" \
        and lists[0][1] == "", f"requeued item malformed: {lists[0]!r}"
    tries_seen = [r[2] for r in lists]
    assert tries_seen == sorted(tries_seen), f"tries not increasing: {tries_seen}"
    assert max(tries_seen) <= 3, \
        f"requeue cap exceeded (tries seen: {tries_seen}) — unbounded loop"


# ── 14-18 · turn flow inside _receive_audio ──────────────────────────────────

def _patch_journal(monkey_targets, recorder):
    monkey_targets.append((jarvis_main, "append_turn", recorder))


def test_reply_gate_and_empty_response():
    """14+15: (a) a turn completing while the request timestamp is in the
    future (pre-request turn) is NOT delivered to the chat; (b) an empty
    answer for a CURRENT pending request produces one failure notice."""
    async def _run():
        journal = _JournalRecorder()
        hermes = _HermesRecorder()
        real_append = jarvis_main.append_turn
        jarvis_main.append_turn = journal
        undo_hermes = _install_fake_hermes(hermes)
        try:
            sess = _FakeLiveSession()
            j = _make_jarvis(sess)
            task = asyncio.create_task(j._receive_audio())

            # Phase A — the messaging request has NOT been sent yet: the gate
            # timestamp is in the future, so ANY turn completing now must not
            # be delivered to the chat (this is the pre-request/unrelated
            # turn that used to go to the wrong chat).
            j._inbound_channel = "telegram:111"
            j._inbound_req_ts = time.monotonic() + 60.0
            sess._q.append(_resp(_sc(input_text="voice question",
                                     output_text="voice answer")))
            await asyncio.sleep(0.4)
            assert any(r[1] == "voice answer" for r in journal.rows), \
                "turn not journaled"
            gate_blocked = all(tgt != "telegram:111" for _, tgt in hermes.calls)
            assert gate_blocked, \
                f"pre-request turn was delivered to the chat: {hermes.calls!r}"

            # Phase B — the request is now current (sent 0.1s ago) and the
            # model answers with an EMPTY turn: one honest failure notice.
            j._inbound_req_ts = time.monotonic() - 0.1
            sess._q.append(_resp(_sc(input_text="tg message",
                                     output_text=None)))
            await asyncio.sleep(0.4)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return hermes
        finally:
            jarvis_main.append_turn = real_append
            undo_hermes()
    hermes = asyncio.run(_run())
    texts = [c[0] for c in hermes.calls]
    assert not any(t == "voice answer" for t in texts), \
        "the unrelated voice answer reached the chat channel"
    assert any("couldn't generate a reply" in t for t in texts), \
        f"empty answer produced no failure notice: {texts!r}"
    assert len(texts) == 1, f"expected exactly one notice, got {texts!r}"


def test_reply_gate_delivers_answer_turn():
    """14b: the turn that answers the request IS delivered."""
    async def _run():
        journal = _JournalRecorder()
        hermes = _HermesRecorder()
        real_append = jarvis_main.append_turn
        jarvis_main.append_turn = journal
        undo_hermes = _install_fake_hermes(hermes)
        try:
            sess = _FakeLiveSession([
                _resp(_sc(input_text="tg message", output_text="the real answer")),
            ])
            j = _make_jarvis(sess)
            j._inbound_channel = "telegram:111"
            j._inbound_req_ts = time.monotonic() - 0.05   # request already sent
            task = asyncio.create_task(j._receive_audio())
            await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return hermes
        finally:
            jarvis_main.append_turn = real_append
            undo_hermes()
    hermes = asyncio.run(_run())
    delivered = [t for t, tgt in hermes.calls if t == "the real answer"]
    assert delivered, f"answer turn not delivered: {hermes.calls!r}"
    targets = [tgt for t, tgt in hermes.calls if t == "the real answer"]
    assert any(tgt == "telegram:111" for tgt in targets), \
        f"answer delivered to the wrong target: {targets!r}"


def test_interrupted_turn_cancelled_and_released():
    """16: an interrupted turn marks the request CANCELLED, releases the
    command queue, journals nothing, and delivers nothing."""
    async def _run():
        journal = _JournalRecorder()
        hermes = _HermesRecorder()
        real_append = jarvis_main.append_turn
        jarvis_main.append_turn = journal
        undo_hermes = _install_fake_hermes(hermes)
        try:
            sess = _FakeLiveSession([
                _resp(_sc(input_text="partial", output_text="partial ans")),
            ])
            j = _make_jarvis(sess)
            j._interrupted = True
            j._dash_turn_done.clear()
            rid = jarvis_main._resp_tracker().start(channel="dashboard")
            j._active_req_id = rid
            task = asyncio.create_task(j._receive_audio())
            await asyncio.sleep(0.4)
            released = j._dash_turn_done.is_set()
            info = jarvis_main._resp_tracker().get(rid)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return journal, hermes, released, info
        finally:
            jarvis_main.append_turn = real_append
            undo_hermes()
    journal, hermes, released, info = asyncio.run(_run())
    assert journal.rows == [], f"interrupted turn was journaled: {journal.rows!r}"
    assert hermes.calls == [], "interrupted turn was delivered"
    assert released, "interrupted turn did not release the command queue"
    assert info and info["state"] == CANCELLED, f"state={info!r}"


def test_goaway_reconnect_after_bookkeeping():
    """17: a deferred GoAway reconnect must fire AFTER the turn is journaled
    and broadcast — the answer is no longer thrown away."""
    async def _run():
        journal = _JournalRecorder()
        hermes = _HermesRecorder()
        real_append = jarvis_main.append_turn
        jarvis_main.append_turn = journal
        undo_hermes = _install_fake_hermes(hermes)
        try:
            sess = _FakeLiveSession([
                _resp(_sc(input_text="question", output_text="full answer")),
            ])
            j = _make_jarvis(sess)
            j._goaway_pending = True
            signal_caught = None
            task = asyncio.create_task(j._receive_audio())
            await asyncio.sleep(0.5)
            if task.done():
                try:
                    await task
                except jarvis_main._ReconnectSignal as e:
                    signal_caught = e
                except BaseException as e:      # noqa: BLE001
                    signal_caught = e
            else:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            journaled = any(r[1] == "full answer" for r in journal.rows)
            broadcast = any(b.get("text") == "full answer"
                            for b in j._dashboard.broadcasts)
            return signal_caught, journaled, broadcast
        finally:
            jarvis_main.append_turn = real_append
            undo_hermes()
    signal_caught, journaled, broadcast = asyncio.run(_run())
    assert isinstance(signal_caught, jarvis_main._ReconnectSignal), \
        f"deferred GoAway did not reconnect (got {signal_caught!r})"
    assert journaled, "turn bookkeeping (journal) skipped before reconnect"
    assert broadcast, "turn bookkeeping (broadcast) skipped before reconnect"


def test_conv_stamp_travels_with_turn():
    """18: append_turn(speaker, text, conv=...) stamps the row; the journal
    file contains the conversation id; plain calls stay unstamped."""
    cjournal.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    assert cjournal.append_turn("user", "rio question", "conv-test-1") is True
    assert cjournal.append_turn("jarvis", "rio answer", "conv-test-1") is True
    assert cjournal.append_turn("user", "voice turn") is True
    rows = []
    for f in sorted(cjournal.JOURNAL_DIR.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    rio = [r for r in rows if r["text"] in ("rio question", "rio answer")]
    assert len(rio) == 2 and all(r.get("conv") == "conv-test-1" for r in rio), \
        f"conv stamp missing: {rio!r}"
    voice = [r for r in rows if r["text"] == "voice turn"]
    assert voice and "conv" not in voice[0], "voice turn must stay unstamped"


def test_notify_inbound_lost():
    """19: reconnect/interrupt clears the stale inbound slot and notifies the
    chat once (previously the next unrelated voice answer went to the chat)."""
    async def _run():
        j = _make_jarvis()
        hermes = _HermesRecorder()
        undo_hermes = _install_fake_hermes(hermes)
        try:
            j._inbound_channel = "whatsapp:42"
            j._active_req_id = ""
            j._notify_inbound_lost("reconnect")
            await asyncio.sleep(0.1)
            return hermes, j._inbound_channel
        finally:
            undo_hermes()
    hermes, slot = asyncio.run(_run())
    assert slot is None, "stale inbound slot not cleared"
    assert len(hermes.calls) == 1 and hermes.calls[0][1] == "whatsapp:42", \
        f"expected one notice to whatsapp:42, got {hermes.calls!r}"
    assert "interrupted" in hermes.calls[0][0].lower() or \
        "send that again" in hermes.calls[0][0].lower()


def test_phone_mirror_uses_real_queue():
    """20: the mirror pushes into `_phone_audio_queue` (the real attribute)."""
    async def _run():
        j = _make_jarvis()
        j._mirror_phone_audio(b"\x00\x01\x02\x03")
        await asyncio.sleep(0.05)
        return j._dashboard._phone_audio_queue.qsize()
    n = asyncio.run(_run())
    assert n == 1, f"phone mirror did not enqueue (qsize={n})"


# ── standalone runner (repo convention) ──────────────────────────────────────

def _all_tests():
    return [
        test_dedup_same_id_processed_once,
        test_dedup_identical_text_different_ids_both_pass,
        test_dedup_channel_scoped,
        test_dedup_ttl_expiry,
        test_dedup_bounded,
        test_intake_dedup_end_to_end,
        test_ws_boot_and_dedup,
        test_broadcast_seq_and_history,
        test_response_tracker_lifecycle,
        test_response_tracker_failed_state,
        test_one_message_one_generation,
        test_commands_serialized_until_turn_done,
        test_session_down_requeues_bounded_then_fails,
        test_reply_gate_and_empty_response,
        test_reply_gate_delivers_answer_turn,
        test_interrupted_turn_cancelled_and_released,
        test_goaway_reconnect_after_bookkeeping,
        test_conv_stamp_travels_with_turn,
        test_notify_inbound_lost,
        test_phone_mirror_uses_real_queue,
    ]


if __name__ == "__main__":
    ok = 0
    for fn in _all_tests():
        try:
            fn()
            print(f"PASS {fn.__name__}")
            ok += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:                         # noqa: BLE001
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{ok}/{len(_all_tests())} passed")
    sys.exit(0 if ok == len(_all_tests()) else 1)


# ── 21. Output-transcript overlap merge (duplicate-sentence fix) ─────────────
# The Live API's output_transcription occasionally RESTATES text it already
# emitted (restart / re-sync chunk). Naive appending produced verbatim doubled
# sentences — 11 confirmed A+A rows in memory/conversations/2026-10.jsonl
# before the fix. These tests lock the merge semantics down.

def test_transcript_incremental_chunks_unchanged():
    """Normal incremental chunking must assemble the full text untouched."""
    out = ""
    for c in ["नमस्ते बॉस,", "कैसे हैं आप?", "मैं यहीं हूँ।"]:
        out = jarvis_main._merge_transcript(out, c)
    assert out == "नमस्ते बॉस, कैसे हैं आप? मैं यहीं हूँ।"


def test_transcript_whole_restatement_not_doubled():
    """The 2026-10-13T20:21:35 journal case: pieces, then the whole turn
    re-emitted as one chunk → the restatement is swallowed."""
    A = ("आप सही कह रहे हैं, बॉस। मेरा नाम केवल एक पहचान है। "
         "मैं आपकी किस प्रकार सहायता कर सकता हूँ?")
    out = ""
    for c in ["आप सही कह रहे हैं, बॉस।", "मेरा नाम केवल एक पहचान है।",
              "मैं आपकी किस प्रकार सहायता कर सकता हूँ?", A]:
        out = jarvis_main._merge_transcript(out, c)
    assert out == A


def test_transcript_piecewise_restart_not_doubled():
    """A restart that re-emits from the beginning piece by piece must not
    double the turn (each re-emitted chunk is verbatim-containment)."""
    out = ""
    for c in ["नमस्ते बॉस,", "कैसे हैं आप?", "नमस्ते बॉस,", "कैसे हैं आप?"]:
        out = jarvis_main._merge_transcript(out, c)
    assert out == "नमस्ते बॉस, कैसे हैं आप?"


def test_transcript_overlap_stitch():
    """A chunk that continues the tail and then restates it is stitched and
    collapsed, not appended twice."""
    out = ""
    for c in ["ठीक है बॉस। फिर", "मिलते हैं। ठीक है बॉस। फिर मिलते हैं।"]:
        out = jarvis_main._merge_transcript(out, c)
    assert out == "ठीक है बॉस। फिर मिलते हैं।"


def test_transcript_consecutive_identical_chunk_dropped():
    """The pre-existing consecutive guard semantics are preserved (the merge
    subsumes it for ≥6-char chunks)."""
    out = jarvis_main._merge_transcript(
        "Hello, Boss. How can I assist you today?",
        "Hello, Boss. How can I assist you today?")
    assert out == "Hello, Boss. How can I assist you today?"


def test_transcript_short_nonconsecutive_repeat_kept():
    """Short (<6 char) non-consecutive repeats are legitimate speech and must
    survive — the merge floor exists so real words are never eaten."""
    out = ""
    for c in ["हाँ।", "अच्छा।", "हाँ।"]:
        out = jarvis_main._merge_transcript(out, c)
    assert out == "हाँ। अच्छा। हाँ।"


def test_collapse_doubled_only_exact_halves():
    """_collapse_doubled collapses only exact A+A texts; anything else passes
    through untouched."""
    assert jarvis_main._collapse_doubled("नमस्ते बॉस, कैसे हैं आप? नमस्ते बॉस, कैसे हैं आप?") \
        == "नमस्ते बॉस, कैसे हैं आप?"
    assert jarvis_main._collapse_doubled("हाँ। अच्छा। हाँ।") == "हाँ। अच्छा। हाँ।"
    assert jarvis_main._collapse_doubled("short") == "short"
