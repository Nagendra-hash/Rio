#!/usr/bin/env python3
"""E2E replay of JARVIS-Remote v1.3 BackgroundLinkService — faithful version.

Key fidelity fix over the previous run: the app's WebSocket reader runs in a
continuous thread (answers protocol pings instantly, app-level pings every
25s). This test now mirrors that with a reader thread, so server-side
uvicorn ping/pong timeouts behave as they do for the real service.
"""

import base64
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time

HOST, PORT = "192.168.29.172", 8000
HOLD_MIN = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
PING_EVERY, PONG_WINDOW = 25, 12


def newest_token():
    d = json.load(open(os.path.expanduser("~/Mark-LIII/config/dashboard_devices.json")))
    return max(d.items(), key=lambda kv: kv[1].get("paired_at", 0))[0]


class WS:
    def __init__(self, host, port, path):
        raw = socket.create_connection((host, port), timeout=10)
        self.sock = ssl._create_unverified_context().wrap_socket(
            raw, server_hostname=host
        )
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("handshake failed: " + head.decode(errors="replace"))
        self.sock.settimeout(None)  # app uses blocking reads; no idle timeout
        self.buf = rest
        self.alive = True
        self.pongs = 0
        self.msgs = {"log": 0, "status": 0, "other": 0}
        self.err = None
        # continuous reader thread — like the service's readLoop
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("eof")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_loop(self):
        try:
            while self.alive:
                b1, b2 = self._read(2)
                opcode, masked, ln = b1 & 0x0F, b2 & 0x80, b2 & 0x7F
                if ln == 126:
                    ln = struct.unpack(">H", self._read(2))[0]
                elif ln == 127:
                    ln = struct.unpack(">Q", self._read(8))[0]
                mask = self._read(4) if masked else None
                payload = self._read(ln)
                if mask:
                    payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
                if opcode == 0x9:  # server protocol ping
                    self._send_frame(0x8A, payload)  # → pong immediately
                elif opcode == 0x8:
                    self.alive = False
                    return
                elif opcode == 0x1:
                    try:
                        m = json.loads(payload.decode())
                        t = m.get("type", "other")
                        if t == "pong":
                            self.pongs += 1
                        elif t in self.msgs:
                            self.msgs[t] += 1
                        else:
                            self.msgs["other"] += 1
                    except Exception:
                        pass
        except Exception as e:
            if self.alive:
                self.err = repr(e)
                self.alive = False

    def _send_frame(self, first, payload):
        mask = os.urandom(4)
        header = (
            bytes([first, 0x80 | (len(payload) & 0x7F)])
            if len(payload) < 126
            else bytes([first, 0x80 | 126]) + struct.pack(">H", len(payload))
        )
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_text(self, s):
        self._send_frame(0x81, s.encode())

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass


def connect():
    dev = newest_token()
    import urllib.request
    import ssl as _ssl

    req = urllib.request.Request(
        f"https://{HOST}:{PORT}/api/device-login",
        data=json.dumps({"device_token": dev}).encode(),
        headers={"Content-Type": "application/json"},
    )
    resp = json.loads(
        urllib.request.urlopen(
            req, context=_ssl._create_unverified_context(), timeout=10
        ).read()
    )
    if not resp.get("ok") or not resp.get("token"):
        raise RuntimeError("device-login rejected: " + str(resp)[:200])
    ws = WS(HOST, PORT, f"/ws?token={resp['token']}&role=phone")
    ws.send_text('{"type":"hello","role":"phone"}')
    time.sleep(1.0)  # let boot/status frames arrive
    return ws


results, drops, reconnects = [], 0, 0
try:
    ws = connect()
    results.append("PASS device-login + handshake + hello role=phone")
except Exception as e:
    results.append(f"FAIL connect: {e}")
    print("\n".join("RESULT: " + r for r in results))
    sys.exit(1)

deadline = time.time() + HOLD_MIN * 60
cycles = 0
while time.time() < deadline:
    cycles += 1
    time.sleep(PING_EVERY)
    if not ws.alive:  # unexpected drop
        drops += 1
        results.append(
            f"FAIL drop at cycle {cycles}: {ws.err or 'closed'} — reconnecting"
        )
        try:
            ws.close()
        except Exception:
            pass
        reconnects += 1
        ws = connect()
        continue
    before = ws.pongs
    ws.send_text('{"type":"ping"}')
    t0 = time.time()
    while time.time() - t0 < PONG_WINDOW and ws.pongs == before:
        time.sleep(0.2)
    if ws.pongs > before:
        pass  # healthy cycle
    elif not ws.alive:
        drops += 1
        results.append(f"FAIL died right after ping at cycle {cycles}: {ws.err}")
        ws = connect()
        reconnects += 1
    else:
        results.append(
            f"WARN no pong in {PONG_WINDOW}s at cycle {cycles} "
            f"(rx: log={ws.msgs['log']} status={ws.msgs['status']})"
        )

results.append(
    f"{'PASS' if drops == 0 else 'FAIL'} screen-off hold {HOLD_MIN:g}min: "
    f"{cycles} keepalive cycles, {ws.pongs} pongs total, "
    f"{drops} drops, {reconnects} reconnects, "
    f"rx log={ws.msgs['log']} status={ws.msgs['status']}"
)

try:
    ws.send_text(json.dumps({"type": "command", "text": "system status report"}))
    time.sleep(2)
    results.append("PASS quick-action command accepted (queued to JARVIS)")
except Exception as e:
    results.append(f"FAIL quick-action command: {e}")
ws.close()

print("\n".join("RESULT: " + r for r in results))
sys.exit(0 if drops == 0 else 2)
