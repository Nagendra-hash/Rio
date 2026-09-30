"""
dashboard/server.py — JARVIS Local HTTP Dashboard

Plain HTTP on port 8000 (no SSL warnings, no firewall issues).
Security at the application layer: AES-256-CBC with session-key-derived key.
CryptoJS is auto-downloaded once and served locally — no CDN needed after that.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import hashlib
import json
import re
import secrets
import socket
import string
import time
from pathlib import Path

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

# python-multipart is required for file uploads — optional dependency
_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR    = Path(__file__).resolve().parent.parent
STATIC_DIR  = Path(__file__).parent / "static"
PORT        = 8000
MAX_UPLOAD_MB = 500

# Everything HTML — and the service-worker script / manifest — is served with
# strict no-cache headers. The dashboard UI changes (bug fixes, new buttons)
# more often than the phone's cache would notice, and a stale page is painful
# to diagnose: the phone just keeps showing the old UI (or an old SW that
# never updates). HTML is a few KB, so there is nothing worth caching anyway.
# Immutable-ish assets (icons, vendored crypto.js/lwc) keep default caching —
# the SW also caches icons, which the PWA E2E test locks down.
_NO_CACHE = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _qr_png_b64(data: str) -> str | None:
    """Render text as a base64 PNG QR code (data-URI). Optional dependency —
    returns None when qrcode/Pillow is missing; the UI then shows only the link."""
    try:
        import io
        import qrcode
        buf = io.BytesIO()
        qrcode.make(data).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        BASE_DIR / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return BASE_DIR / "uploads"


UPLOADS_DIR = _make_uploads_dir()

def _get_gemini_key() -> str | None:
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return _json.load(f).get("gemini_api_key")
    except Exception:
        return None

_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

# ── AES-256-CBC ───────────────────────────────────────────────────────────────
_AES_SALT = b'JARVIS-DASHBOARD-v1'


def _derive_key(session_key: str) -> bytes:
    """SHA-256(sessionKey‖salt) → 32-byte AES-256 key (microseconds, no PBKDF2 needed)."""
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    """Decrypt base64(IV[16] ‖ ciphertext) with AES-256-CBC + PKCS7."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw      = base64.b64decode(enc_b64)
    iv, ct   = raw[:16], raw[16:]
    dec      = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded   = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode('utf-8')


# ── CryptoJS (auto-download once, served locally) ─────────────────────────────
_CRYPTOJS_CDN  = ("https://cdnjs.cloudflare.com/ajax/libs/"
                  "crypto-js/4.2.0/crypto-js.min.js")
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"

# TradingView Lightweight Charts standalone build, vendored from the
# lightweight-charts wheel (no CDN dependency at chart time).
_LWC_FILE = STATIC_DIR / "lightweight-charts.standalone.js"


def dashboard_url() -> str:
    """Best-effort LAN URL of the dashboard ('' when the IP can't be
    determined) — plugins deep-link pages like /chart through this."""
    try:
        ip = _local_ip()
    except Exception:
        return ""
    return f"http://{ip}:{PORT}" if ip else ""


# ── Standalone live-chart page (TradingView Lightweight Charts v4) ───────────
# JARVIS deep-links /chart?symbol=…&method=… from trading answers. Authed via
# sessionStorage like the main app; data from /api/chart-setup, re-polled
# every 30s so the last candle, EMAs and the setup levels stay live.
_CHART_PAGE_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JARVIS Live Chart</title>
<style>
  body{margin:0;background:#131722;color:#d1d4dc;font-family:'Segoe UI',sans-serif}
  header{display:flex;align-items:center;gap:10px;padding:8px 12px;flex-wrap:wrap}
  header .sym{font-weight:700;font-size:17px;color:#fff}
  header .meta{color:#787b86;font-size:12px}
  .badge{padding:2px 10px;border-radius:10px;font-size:12px;font-weight:700}
  .LONG{background:rgba(38,166,154,.18);color:#26a69a}
  .SHORT{background:rgba(239,83,80,.18);color:#ef5350}
  .FLAT{background:rgba(120,123,134,.18);color:#787b86}
  .spacer{flex:1}
  a.tv{color:#2962ff;font-size:12px;text-decoration:none;border:1px solid #2962ff;
       padding:3px 10px;border-radius:6px}
  #chart{width:100%;height:calc(100vh - 44px)}
  #err{display:none;margin:8px 12px;color:#ef5350;font-size:13px}
</style></head>
<body>
<header>
  <span class="sym" id="sym">—</span>
  <span class="badge" id="dir">…</span>
  <span class="meta" id="meta"></span>
  <span class="spacer"></span>
  <a class="tv" id="tvlink" target="_blank" rel="noopener">TradingView ↗</a>
</header>
<div id="err"></div>
<div id="chart"></div>
<script src="/static/lightweight-charts.standalone.js"></script>
<script>
const qs   = new URLSearchParams(location.search);
const SYM  = (qs.get('symbol') || 'BTC-USD').toUpperCase();
const METHOD = (qs.get('method') || 'swing').toLowerCase();
const token = sessionStorage.getItem('jarvis_token');
if (!token) location.replace('/login');

// PWA glue (deep-linked from shortcuts & JARVIS answers)
if ('serviceWorker' in navigator && window.isSecureContext) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

document.getElementById('sym').textContent = SYM;
document.getElementById('tvlink').href = tvUrl(SYM, METHOD);

function tvUrl(sym, method) {
  let s = sym;
  if (sym.endsWith('.NS')) s = 'NSE:' + sym.slice(0, -3);
  else if (sym.endsWith('.BO')) s = 'BSE:' + sym.slice(0, -3);
  else if (/-USD$/.test(sym)) s = 'BINANCE:' + sym.replace('-USD', 'USDT');
  const iv = method === 'intraday' ? '15' : 'D';
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(s)}&interval=${iv}`;
}
function toTs(t) {
  return t.length === 10
    ? Math.floor(new Date(t + 'T00:00:00Z').getTime() / 1000)
    : Math.floor(new Date(t + 'Z').getTime() / 1000);
}

const chart = LightweightCharts.createChart(document.getElementById('chart'), {
  layout: { background: { type: 'solid', color: '#131722' }, textColor: '#d1d4dc' },
  grid:   { vertLines: { color: '#1e222d' }, horzLines: { color: '#1e222d' } },
  timeScale: { timeVisible: true, borderColor: '#2a2e39', rightOffset: 8 },
  rightPriceScale: { borderColor: '#2a2e39' },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
});
function fit() { chart.applyOptions({ width: window.innerWidth, height: window.innerHeight - 44 }); }
fit(); addEventListener('resize', fit);

const candles = chart.addCandlestickSeries({
  upColor: '#26a69a', downColor: '#ef5350', wickColor: '#778',
  borderVisible: false,
});
const volume = chart.addHistogramSeries({ priceScaleId: '', priceFormat: { type: 'volume' } });
volume.priceScale().applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
function emaSeries(color, width) {
  return chart.addLineSeries({ color, lineWidth: width || 1,
    priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
}
const emaF = emaSeries('#42a5f5'), emaM = emaSeries('#ffa726'),
      emaS = emaSeries('#ab47bc'), ema200 = emaSeries('#787b86'),
      trail = emaSeries('#4dd0e1', 1);
trail.applyOptions({ lineStyle: LightweightCharts.LineStyle.Dashed });

let priceLines = [];
function drawPlan(plan) {
  priceLines.forEach(l => candles.removePriceLine(l));
  priceLines = [];
  if (plan.entry != null)
    priceLines.push(candles.createPriceLine({ price: plan.entry, color: '#2962ff',
      lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Solid,
      axisLabelVisible: true, title: 'ENTRY' }));
  if (plan.stop != null)
    priceLines.push(candles.createPriceLine({ price: plan.stop, color: '#ef5350',
      lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true, title: 'STOP' }));
  (plan.targets || []).forEach((t, i) =>
    priceLines.push(candles.createPriceLine({ price: t, color: '#fdd835',
      lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted,
      axisLabelVisible: true, title: 'TP' + (i + 1) + ' (' + plan.targets_r[i] + 'R)' })));
}

function setData(p) {
  const s = p.series;
  const bars  = s.time.map((t, i) => ({ time: toTs(t), open: s.open[i], high: s.high[i],
                                        low: s.low[i], close: s.close[i] }));
  const vols  = s.time.map((t, i) => ({ time: toTs(t), value: s.volume[i],
    color: s.close[i] >= s.open[i] ? 'rgba(38,166,154,.45)' : 'rgba(239,83,80,.45)' }));
  const line = (arr) => s.time.map((t, i) => ({ time: toTs(t), value: arr[i] }));
  candles.setData(bars); volume.setData(vols);
  emaF.setData(line(s.ema_f)); emaM.setData(line(s.ema_m)); emaS.setData(line(s.ema_s));
  ema200.setData(line(s.ema200)); trail.setData(line(s.trail));
  drawPlan(p);
  const dirEl = document.getElementById('dir');
  dirEl.textContent = p.direction; dirEl.className = 'badge ' + p.direction;
  document.getElementById('meta').textContent =
    `${p.price} · ${p.market_label} · ${p.interval} · score ${p.score}`;
}

function updateData(p) {
  const s = p.series, n = s.time.length - 1;
  const t = toTs(s.time[n]);
  candles.update({ time: t, open: s.open[n], high: s.high[n], low: s.low[n], close: s.close[n] });
  volume.update({ time: t, value: s.volume[n],
    color: s.close[n] >= s.open[n] ? 'rgba(38,166,154,.45)' : 'rgba(239,83,80,.45)' });
  emaF.update({ time: t, value: s.ema_f[n] }); emaM.update({ time: t, value: s.ema_m[n] });
  emaS.update({ time: t, value: s.ema_s[n] }); ema200.update({ time: t, value: s.ema200[n] });
  trail.update({ time: t, value: s.trail[n] });
  drawPlan(p);
}

let _unreachable = false;
async function poll(first) {
  try {
    const r = await fetch(`/api/chart-setup?symbol=${encodeURIComponent(SYM)}&method=${METHOD}`,
                          { headers: { 'Authorization': 'Bearer ' + token } });
    if (r.status === 401) { location.replace('/login'); return; }
    const p = await r.json();
    if (p.error) throw new Error(p.error);
    (first ? setData : updateData)(p);
    document.getElementById('err').style.display = 'none';
    _unreachable = false;
  } catch (e) {
    const el = document.getElementById('err');
    el.style.display = 'block';
    if (e instanceof TypeError) {           // fetch-level failure — PC/phone offline
      _unreachable = true;
      el.textContent = navigator.onLine
        ? "Can't reach JARVIS — is the PC asleep or the tunnel down? Retrying…"
        : 'Phone is offline — reconnect Wi‑Fi or mobile data. Retrying…';
    } else {
      el.textContent = 'Data unavailable: ' + e.message;
    }
  }
}
poll(true);
setInterval(() => poll(false), 30000);
// Connectivity back (or PC came back up) → refresh immediately, not on the
// next 30 s tick.
addEventListener('online', () => { if (_unreachable) poll(false); });
</script>
</body></html>"""


def _ensure_network_access(port: int) -> None:
    """Cross-platform, best-effort: open port in the OS firewall for LAN access.

    Runs in a background thread — never blocks uvicorn startup.

    Windows : writes a .bat file, runs it elevated via Windows ShellExecuteW
              (native UAC dialog, guaranteed to appear). One-time setup.
    macOS   : osascript admin dialog if the Application Firewall is on.
    Linux   : pkexec GUI → sudo -n → prints manual command as fallback.
    """
    import sys, subprocess, os, tempfile, threading

    # ── Windows ──────────────────────────────────────────────────────────────
    if sys.platform == "win32":
        import ctypes, time

        port_rule = f"JARVIS Dashboard Port {port}"
        prog_rule  = "JARVIS Dashboard Python"
        py_exe     = sys.executable

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        def _network_is_public() -> bool:
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-NetConnectionProfile | "
                     "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                     "Measure-Object).Count"],
                    capture_output=True, text=True, timeout=6,
                )
                return r.stdout.strip() not in ("", "0")
            except Exception:
                return False

        need_port    = not _netsh_rule_exists(port_rule)
        need_prog    = not _netsh_rule_exists(prog_rule)
        need_private = _network_is_public()

        if not need_port and not need_prog and not need_private:
            return  # already fully configured

        # Build a .bat file — netsh + powershell, runs fast when elevated
        bat_lines = ["@echo off"]
        if need_private:
            bat_lines.append(
                'powershell -NoProfile -NonInteractive -Command "'
                'Get-NetConnectionProfile | '
                "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                'Set-NetConnectionProfile -NetworkCategory Private"'
            )
        if need_port:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{port_rule}" protocol=TCP dir=in '
                f'localport={port} action=allow'
            )
        if need_prog:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{prog_rule}" dir=in action=allow '
                f'program="{py_exe}" enable=yes'
            )

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="jarvis_fw_")
        try:
            os.write(fd, bat_body.encode("mbcs"))   # Windows cmd.exe expects ANSI
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            return

        # ── Try running directly (succeeds when already admin) ────────────────
        try:
            r = subprocess.run(
                [bat_path], capture_output=True, timeout=8, shell=True
            )
            if r.returncode == 0:
                print(f"[Dashboard] Firewall configured for port {port}.")
                try:
                    os.unlink(bat_path)
                except Exception:
                    pass
                return
        except Exception:
            pass

        # ── ShellExecuteW: native UAC elevation (most reliable on Windows) ────
        # ShellExecuteW with verb "runas" always shows the UAC dialog regardless
        # of UAC level settings. Non-blocking — uvicorn is already running.
        print("[Dashboard] One-time network setup required.")
        print("[Dashboard] >>> A Windows security dialog will appear — click 'Yes' <<<")
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,       # hwnd  (no parent window)
                "runas",    # verb  (request elevation)
                bat_path,   # file  (our .bat)
                None,       # params
                None,       # working dir
                0,          # SW_HIDE (run without a visible cmd window)
            )
            if int(ret) > 32:
                # ShellExecuteW returns immediately; bat finishes in ~1 second.
                # Sleep briefly so the rules are in place before the first retry.
                time.sleep(2)
                print(f"[Dashboard] Network setup complete — port {port} is open.")
                print("[Dashboard] Refresh your phone browser to connect.")
            else:
                print("[Dashboard] Setup was not allowed.")
                print("[Dashboard] Phone connections may fail until JARVIS is run as Administrator.")
        except Exception as e:
            print(f"[Dashboard] Firewall setup error: {e}")
        finally:
            # Cleanup after the bat has had time to run
            def _cleanup(path: str) -> None:
                time.sleep(5)
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, args=(bat_path,), daemon=True).start()
        return

    # ── macOS ─────────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        fw_ctl = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        try:
            r = subprocess.run(
                [fw_ctl, "--getglobalstate"], capture_output=True, text=True, timeout=5,
            )
            if "disabled" in r.stdout.lower():
                return  # firewall off — nothing to do

            py = sys.executable
            listed = subprocess.run(
                [fw_ctl, "--listapps"], capture_output=True, text=True, timeout=5,
            )
            if py in listed.stdout:
                return  # already allowed

            print("[Dashboard] One-time network setup — enter your password in the macOS dialog.")
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{fw_ctl} --add {py} && {fw_ctl} --unblockapp {py}"'
                 f' with administrator privileges'],
                timeout=60,
            )
        except Exception:
            pass  # macOS firewall is off by default — silent failure is fine
        return

    # ── Linux ─────────────────────────────────────────────────────────────────
    def _privileged(cmd: list[str]) -> bool:
        for prefix in (["pkexec"], ["sudo", "-n"]):
            try:
                r = subprocess.run(prefix + cmd, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    try:  # ufw
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        if "active" in r.stdout.lower():
            if _privileged(["ufw", "allow", f"{port}/tcp"]):
                print(f"[Dashboard] ufw: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo ufw allow {port}/tcp")
            return
    except FileNotFoundError:
        pass

    try:  # firewalld
        r = subprocess.run(
            ["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5,
        )
        if "running" in r.stdout.lower():
            ok = (_privileged(["firewall-cmd", "--add-port", f"{port}/tcp", "--permanent"])
                  and _privileged(["firewall-cmd", "--reload"]))
            if ok:
                print(f"[Dashboard] firewalld: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo firewall-cmd --add-port={port}/tcp --permanent && sudo firewall-cmd --reload")
            return
    except FileNotFoundError:
        pass

    try:  # iptables (not persistent but works until reboot)
        r = subprocess.run(["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5)
        if r.returncode == 0:
            if _privileged(["iptables", "-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]):
                print(f"[Dashboard] iptables: port {port} opened.")
            else:
                print(f"[Dashboard] Run manually:  sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT")
    except FileNotFoundError:
        pass  # no iptables means firewall is probably off — nothing to do


def _ensure_crypto_js() -> None:
    if _CRYPTOJS_FILE.exists():
        return
    try:
        import urllib.request
        print("[Dashboard] Downloading CryptoJS (one-time setup)…")
        urllib.request.urlretrieve(_CRYPTOJS_CDN, str(_CRYPTOJS_FILE))
        print("[Dashboard] CryptoJS cached — will serve locally from now on.")
    except Exception as e:
        print(f"[Dashboard] CryptoJS download failed: {e}")
        print(f"[Dashboard] Encryption will fall back to CDN load on client.")


_ensure_crypto_js()


# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the best LAN-facing IPv4 address, no internet required."""
    # Method 1: route trick (fast, works when internet is available)
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Method 2: hostname resolution (works offline on most systems)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Method 3: enumerate all interfaces (fully offline, no external deps)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# ── DashboardServer ───────────────────────────────────────────────────────────

def _tv_now() -> str:
    """Timestamp for trading-activity entries (local time, ISO)."""
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


class DashboardServer:

    def __init__(self):
        self._ip                          = _local_ip()
        self._tokens: set[str]            = set()
        self._token_keys: dict[str, str]  = {}   # auth_token → session_key
        self._aes_cache:  dict[str, bytes]= {}   # session_key → AES bytes
        self._clients: set[WebSocket]     = set()
        # Phone presence & control:
        #   _phone_clients — /ws sockets that declared {role: "phone"}; the
        #     targets for phone_device commands (open WhatsApp, call, …).
        #   _phone_audio_q — PCM batches of JARVIS's voice mirrored from the
        #     speaker path so the phone HEARS replies, not just reads text.
        self._phone_clients: set[WebSocket]   = set()
        self._phone_audio_q: asyncio.Queue    = asyncio.Queue(maxsize=100)
        self._audio_sender_task               = None
        self._loop                            = None   # app loop (set on first ws accept)
        self._history: list[dict]         = []
        self._ws_seq                       = 0    # monotonic broadcast seq (resume-after-freeze)
        self._boot_id                      = secrets.token_hex(8)  # server lifetime id
        self._command_queue               = asyncio.Queue()
        self._wake_callback               = None
        self._ptt_callback                = None   # (enable: bool) -> str scope (Mark-LIV)
        self._connect_callback            = None
        self._pending_keys: dict[str, float] = {}
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key}
        # Device sessions survive restarts: the phone's device_token is the
        # credential that lets it skip the QR dance entirely, so losing it on
        # every app restart re-broke pairing constantly (2026-09-21). Stored
        # under config/ next to the other credentials.
        self._devices_file = BASE_DIR / "config" / "dashboard_devices.json"
        self._load_devices()
        self._phone_audio_queue: asyncio.Queue    = asyncio.Queue(maxsize=200)
        # Phone-mic liveness for audio ducking: while a phone streams mic
        # audio, the PC speaker should go quiet — otherwise JARVIS's voice
        # comes out of the PC speakers, is picked up by the phone's mic (and
        # the PC's own), and everyone hears the double-voice echo. Time-based
        # (no audio for PHONE_MIC_IDLE_S = inactive) so no explicit connect/
        # disconnect handshake is needed and a stalled phone self-heals.
        self._phone_mic_last: float  = 0.0
        self._phone_mic_active_cb = None   # set by the app (duck PC speakers)
        self._uploads_dir                 = UPLOADS_DIR
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        # Optional static token — set in the dashboard settings (or by hand in
        # api_keys.json). Lets scripts/cURL hit the API without the QR dance.
        # Loaded lazily per-request by _token_ok() so an edit to the config file
        # applies without a restart.
        self._static_token: str | None    = None
        self._static_token_mtime: float   = 0.0
        # Trading activity ring buffers — webhook alert history and the
        # kill-switch log, served to the dashboard's trading panel.
        self._tv_alerts: list[dict]       = []   # newest last, capped
        self._kill_log: list[dict]        = []
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    def new_key(self, expiry_secs: int = 600) -> str:
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys[key] = now + expiry_secs
        return key

    # ── device-session persistence ────────────────────────────────────────

    def _load_devices(self) -> None:
        try:
            data = json.loads(self._devices_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._device_sessions = {k: v for k, v in data.items()
                                         if isinstance(v, dict)
                                         and v.get("session_key")}
        except Exception:
            self._device_sessions = {}

    def _save_devices(self) -> None:
        try:
            self._devices_file.parent.mkdir(parents=True, exist_ok=True)
            self._devices_file.write_text(
                json.dumps(self._device_sessions, indent=1), encoding="utf-8")
            # Device tokens are bearer-equivalent credentials — keep them
            # readable by the owner only, like api_keys.json should be.
            import os as _os
            _os.chmod(self._devices_file, 0o600)
        except Exception as e:
            print(f"[Dashboard] ⚠️ could not persist device sessions: {e}")

    @staticmethod
    def _is_link_preview_bot(user_agent: str) -> bool:
        """Messaging apps pre-fetch shared links to render a preview. Those
        fetchers hit /auto-login before the human does and would burn the
        one-time key — the user then taps a dead link. Serve them a neutral
        preview page and leave the key untouched."""
        ua = (user_agent or "").lower()
        return any(sig in ua for sig in (
            "whatsapp", "telegrambot", "slackbot", "discordbot",
            "facebookexternalhit", "twitterbot", "linkedinbot",
            "skypeuripreview", "signal", "imessage", "applebot",
            "google", "bingpreview", "preview", "bot", "spider", "crawl"))

    @staticmethod
    def _ssl_enabled() -> bool:
        certs = BASE_DIR / "config" / "certs"
        return (certs / "jarvis.key").exists() and (certs / "jarvis.crt").exists()

    def get_url(self) -> str:
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{PORT}"

    def get_manual_url(self) -> str:
        """URL for manual browser entry. When HTTPS active, points to alias port (also HTTPS)."""
        if self._ssl_enabled():
            return f"{self._ip}:{PORT + 1}"
        return f"{self._ip}:{PORT}"

    def _aes_key(self, session_key: str) -> bytes:
        if session_key not in self._aes_cache:
            self._aes_cache[session_key] = _derive_key(session_key)
        return self._aes_cache[session_key]

    def _current_static_token(self) -> str:
        """The configured static token, re-read when the config file changes.

        Cached because every API request would otherwise hit the disk; the
        mtime check keeps a hand-edit to api_keys.json (or a save from the
        desktop UI) effective without a restart. The path comes from
        config_manager itself — deriving it here a second time would drift
        the moment config_manager changes how it locates the file."""
        try:
            from memory.config_manager import get_dashboard_token, CONFIG_FILE
            mtime = CONFIG_FILE.stat().st_mtime
        except OSError:
            self._static_token = None
            return ""
        if self._static_token is None or mtime != self._static_token_mtime:
            self._static_token = get_dashboard_token() or None
            self._static_token_mtime = mtime
        return self._static_token or ""

    def _token_ok(self, tok: str) -> bool:
        """A bearer/query token is valid if it is a paired session token OR
        the configured static token (when one is set)."""
        if not tok:
            return False
        if tok in self._tokens:
            return True
        st = self._current_static_token()
        return bool(st) and secrets.compare_digest(tok, st)

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        sk = self._token_keys.get(token)
        if not sk:
            return None
        try:
            return _decrypt_cbc(self._aes_key(sk), enc_b64)
        except Exception:
            return None

    # ── trading activity ring buffers ────────────────────────────────────

    def _record_tv_alert(self, alert: dict) -> None:
        """Append a TradingView webhook alert to the history (newest last).
        Never raises — alert logging must not break alert handling."""
        try:
            self._tv_alerts.append(alert)
            if len(self._tv_alerts) > 200:
                self._tv_alerts = self._tv_alerts[-200:]
        except Exception:
            pass

    # ── callbacks ────────────────────────────────────────────────────────

    PHONE_MIC_IDLE_S = 2.0   # no phone-mic audio for this long → mic inactive

    def set_phone_mic_callback(self, fn) -> None:
        """Register fn(active: bool) fired when the phone mic starts/stops.
        Used by the app to duck PC speaker playback (double-voice fix)."""
        self._phone_mic_active_cb = fn

    def phone_mic_active(self) -> bool:
        """True while the phone microphone is actually streaming audio."""
        return (time.time() - self._phone_mic_last) < self.PHONE_MIC_IDLE_S

    def _fire_phone_mic(self, active: bool) -> None:
        """Deliver the phone-mic state change to the app thread. Called from
        the asyncio loop; the callback itself must be thread-safe (the app
        posts to its own Qt/main loop). Never raises."""
        cb = self._phone_mic_active_cb
        if cb is None:
            return
        try:
            cb(active)
        except Exception as e:
            print(f"[Dashboard] phone-mic callback failed: {e}")

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_ptt_callback(self, fn) -> None:
        """(enable: bool) -> str — returns the scope actually achieved
        ("global" | "window" | "off"). When unset, toggles still persist to
        config and take effect on the next app restart."""
        self._ptt_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    # ── broadcast ────────────────────────────────────────────────────────

    async def broadcast(self, msg: dict) -> None:
        # Monotonic sequence: a phone that lost its socket (Android freezes
        # background tabs) reconnects with /ws?since=<seq> and gets only the
        # messages it missed instead of a duplicated backlog.
        self._ws_seq += 1
        msg = dict(msg)
        msg["seq"] = self._ws_seq
        self._history.append(msg)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    # ── phone voice mirror & device control ────────────────────────────────

    def _ensure_audio_sender(self) -> None:
        """Start the PCM sender loop once (idempotent). Runs only while a
        dashboard client is connected."""
        if self._audio_sender_task is None or self._audio_sender_task.done():
            self._audio_sender_task = asyncio.create_task(self._audio_sender())

    async def _audio_sender(self) -> None:
        """Push mirrored JARVIS audio to every connected dashboard client.
        Binary frames: 24 kHz, 16-bit, mono PCM (what Gemini Live returns).
        A client that can't keep up (2 s per ~200 ms batch) is dropped, never
        allowed to stall JARVIS's own playback."""
        q = self._phone_audio_q
        while True:
            chunk = await q.get()
            for ws in list(self._clients):
                try:
                    await asyncio.wait_for(ws.send_bytes(chunk), timeout=2.0)
                except Exception:
                    self._clients.discard(ws)

    async def broadcast_phone_cmd(self, cmd: dict) -> int:
        """Deliver a device-control command to connected phone dashboards.
        Returns how many phones actually received it — the honest number the
        phone_device action reports to the user."""
        delivered = 0
        for ws in list(self._phone_clients):
            try:
                await asyncio.wait_for(ws.send_json(cmd), timeout=2.0)
                delivered += 1
            except Exception:
                self._phone_clients.discard(ws)
                self._clients.discard(ws)
        return delivered

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return self._token_ok(tok)

        # serve CryptoJS from local cache, fallback to CDN redirect
        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _CRYPTOJS_FILE.exists():
                return FileResponse(str(_CRYPTOJS_FILE),
                                    media_type="application/javascript")
            from fastapi.responses import RedirectResponse
            return RedirectResponse(_CRYPTOJS_CDN)

        # ── PWA: manifest, service worker, icons ────────────────────────────
        # Served at the root scope so the service worker can control "/" and
        # the manifest is discoverable from any host (LAN IP or tunnel URL).
        @app.get("/manifest.webmanifest")
        async def serve_manifest():
            mf = STATIC_DIR / "manifest.webmanifest"
            if not mf.exists():
                return JSONResponse({"error": "manifest missing"}, status_code=404)
            return FileResponse(str(mf), media_type="application/manifest+json",
                                headers=_NO_CACHE)

        @app.get("/sw.js")
        async def serve_sw():
            sw = STATIC_DIR / "sw.js"
            if not sw.exists():
                return JSONResponse({"error": "sw missing"}, status_code=404)
            return FileResponse(str(sw), media_type="application/javascript",
                                headers={**_NO_CACHE, "Service-Worker-Allowed": "/"})

        @app.get("/static/icons/{filename}")
        async def serve_icon(filename: str):
            safe = re.sub(r'[^A-Za-z0-9._-]', '', filename)
            path = STATIC_DIR / "icons" / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), media_type="image/png",
                                headers={"Cache-Control": "public, max-age=604800"})

        # serve the local CA so phones/PCs can trust the self-signed HTTPS cert
        @app.get("/ca.crt")
        async def serve_ca():
            ca = BASE_DIR / "config" / "certs" / "ca.crt"
            if not ca.exists():
                return JSONResponse({"error": "No CA certificate (HTTPS not enabled)"},
                                    status_code=404)
            return FileResponse(str(ca), media_type="application/x-x509-ca-cert",
                                filename="jarvis-local-ca.crt")

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html, headers=_NO_CACHE)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            # Auth is handled client-side via sessionStorage bearer token.
            # Server-side header auth can't work here because browser navigations
            # don't send custom headers (location.href doesn't carry Authorization).
            html = (self._app_html
                    .replace("__IP__", self._ip)
                    .replace("__PORT__", str(PORT)))
            return HTMLResponse(html, headers=_NO_CACHE)

        @app.post("/login")
        async def login(req: Request):
            body    = await req.json()
            entered = str(body.get("pin", "")).strip().upper()
            now     = time.time()
            if entered in self._pending_keys and self._pending_keys[entered] > now:
                del self._pending_keys[entered]          # one-time use
                tok = secrets.token_urlsafe(32)
                self._tokens.add(tok)
                self._token_keys[tok] = entered
                self._aes_key(entered)                   # pre-derive & cache
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
                # Bearer token in response body — no cookies needed (works on any browser/HTTP)
                return JSONResponse({"ok": True, "token": tok})
            return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                status_code=401)

        @app.get("/auto-login")
        async def auto_login(key: str = "", request: Request = None):
            """QR code target — validates one-time key, creates session, redirects phone.

            Link-preview fetchers (WhatsApp/Telegram/…) get a neutral preview
            page and never consume the key; the pending-key store now also
            survives restarts indirectly by being re-minted — but paired
            devices don't need this endpoint at all anymore (device-login)."""
            ua = request.headers.get("user-agent", "") if request else ""
            if self._is_link_preview_bot(ua):
                return HTMLResponse(
                    """<!DOCTYPE html>\n<html><head><meta charset="UTF-8">"""
                    """<meta property="og:title" content="JARVIS Remote">"""
                    """<meta property="og:description" content="Tap to open and pair with JARVIS.">"""
                    """</head><body style="background:#07090f;color:#dde3ed;">"""
                    """<p>JARVIS remote pairing link — open it on your phone to connect.</p>"""
                    """</body></html>""", headers=_NO_CACHE)
            now = time.time()
            if not key or key not in self._pending_keys or self._pending_keys[key] <= now:
                return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
  h2{color:#f87171;margin-bottom:12px}p{color:#5e6a7e;font-size:14px}
</style></head>
<body><div><h2>Link Expired</h2>
<p>Press <strong style="color:#dde3ed">Remote Control</strong> in JARVIS to get a new QR code.\nIf JARVIS was restarted, a fresh code is required.</p>
</div></body></html>""", headers=_NO_CACHE)

            del self._pending_keys[key]
            tok     = secrets.token_urlsafe(32)
            dev_tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = key
            self._aes_key(key)
            self._device_sessions[dev_tok] = {"session_key": key,
                                              "paired_at": now}
            self._save_devices()

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Remote connection established via QR code."}
            ))

            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body>
<script>
  sessionStorage.setItem('jarvis_token','{tok}');
  sessionStorage.setItem('jarvis_key','{key}');
  localStorage.setItem('jarvis_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to JARVIS…</p>
</body></html>""", headers=_NO_CACHE)

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            dev_tok = (body.get("device_token") or "").strip()
            if not dev_tok or dev_tok not in self._device_sessions:
                return JSONResponse({"ok": False}, status_code=401)
            session_key = self._device_sessions[dev_tok]["session_key"]
            tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = session_key
            self._aes_key(session_key)
            self._save_devices()
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            return JSONResponse({"ok": True, "token": tok, "key": session_key})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Invalidate all persistent device tokens (admin action)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            count = len(self._device_sessions)
            self._device_sessions.clear()
            self._save_devices()
            return JSONResponse({"ok": True, "revoked": count})

        # ── Phone pairing (dashboard-side UI for the existing QR flow) ────────

        @app.post("/api/phone/pair")
        async def phone_pair(req: Request):
            """Mint a one-time pairing key for a phone: QR + universal link.
            Reuses the same one-time key store as the desktop 'Remote Control'
            QR flow, so /auto-login works unchanged."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            key    = self.new_key(expiry_secs=600)   # 10 min — scanning takes time
            url    = self.get_url()
            link   = f"{url}/auto-login?key={key}"
            return JSONResponse({
                "ok": True, "key": key, "url": url,
                "manual_url": self.get_manual_url(), "link": link,
                "expires_in": 600, "qr": _qr_png_b64(link),
            })

        @app.get("/api/phone/status")
        async def phone_status(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            now = time.time()
            self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
            return JSONResponse({
                "ok": True,
                "paired_devices": len(self._device_sessions),
                "pending_keys": len(self._pending_keys),
                "url": self.get_url(),
                "manual_url": self.get_manual_url(),
            })

        # ── Hermes bridge: file + text from Telegram/WhatsApp ────────────
        # The ONE normalized entry point for external channels (spec §8):
        # Hermes' gateway forwards each document + caption here, regardless
        # of platform, and Jarvis sees one uniform message:
        #     [INBOUND telegram:12345] remember this document
        #     [FILE: /path/to/upload.pdf]
        # Auth is the same bearer token every other endpoint uses, so only
        # the owner's Hermes can trigger ingestion (spec §17).
        if _UPLOAD_OK:
            @app.post("/api/message")
            async def message_inbound(req: Request,
                                      file: UploadFile | None = None):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)
                form = await req.form()
                text = str(form.get("text") or "").strip()
                channel = str(form.get("channel") or "telegram").strip().lower()
                chat_id = str(form.get("chat_id") or "").strip()
                if not text and file is None:
                    return JSONResponse({"error": "nothing to deliver"},
                                        status_code=400)

                saved_name = ""
                if file is not None and getattr(file, "filename", ""):
                    safe = _safe_filename(file.filename)
                    dest = self._uploads_dir / safe
                    stem, suffix = Path(safe).stem, Path(safe).suffix
                    counter = 1
                    while dest.exists():
                        dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                        counter += 1
                    size = 0
                    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                    try:
                        with open(dest, "wb") as fout:
                            while True:
                                chunk = await file.read(65536)
                                if not chunk:
                                    break
                                size += len(chunk)
                                if size > max_bytes:
                                    fout.close()
                                    dest.unlink(missing_ok=True)
                                    return JSONResponse(
                                        {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                        status_code=413)
                                fout.write(chunk)
                        saved_name = dest.name
                    except Exception as exc:
                        try:
                            dest.unlink(missing_ok=True)
                        except Exception:
                            pass
                        return JSONResponse({"error": str(exc)}, status_code=500)

                tag = f"[INBOUND {channel}:{chat_id}]" if chat_id \
                    else f"[INBOUND {channel}]"
                parts = [tag, text.strip()]
                if saved_name:
                    parts.append(f"[FILE: {self._uploads_dir / saved_name}]")
                await self._command_queue.put("\n".join(p for p in parts if p))
                if self._wake_callback:
                    self._wake_callback()
                return JSONResponse({"ok": True, "file": saved_name,
                                     "queued": True})
        else:
            @app.post("/api/message")
            async def message_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File handling requires: pip install python-multipart"},
                    status_code=503)

        # ── RAG retrieval (page-cited) ────────────────────────────────────
        # Direct, stateless access to JARVIS' knowledge library for local
        # agents (Hermes). Same index the knowledge_base action serves, so
        # citations always carry source + page. Loopback-friendly: the token
        # is the credential.
        @app.post("/api/knowledge/search")
        async def knowledge_search(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "invalid JSON"}, status_code=400)
            query = str(body.get("query") or "").strip()
            if not query:
                return JSONResponse({"error": "empty query"}, status_code=400)
            top_k = max(1, min(int(body.get("top_k") or 5), 12))
            collection = (str(body.get("collection") or "").strip() or None)
            try:
                from core import rag
                result = rag.search(query, top_k=top_k, collection=collection)
            except Exception as exc:
                return JSONResponse({"error": str(exc)}, status_code=500)
            return JSONResponse(result)

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body  = await req.json()
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc   = body.get("enc", "")
            if enc:
                text = self._decrypt(token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if text:
                await self._command_queue.put(text)
                if self._wake_callback:
                    self._wake_callback()
            return JSONResponse({"ok": True})

        # ── TradingView alert webhook ─────────────────────────────────────
        # TradingView webhooks cannot send custom headers, so the shared
        # static dashboard token doubles as the webhook secret and travels
        # in the alert body. Set it in dashboard ⚙ Settings, then use:
        #   http://<this-pc-ip>:8000/api/tradingview
        # as the alert's webhook URL with a message like:
        #   {"secret":"<token>","command":"buy BTCUSDT"}
        #   {"secret":"<token>","action":"trade","symbol":"SOLUSDT",
        #    "direction":"LONG","stop_loss":95.0,"targets":[100,105],
        #    "position_size":50}
        @app.post("/api/tradingview")
        async def tradingview_webhook(req: Request):
            try:
                body = await req.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                return JSONResponse({"error": "JSON object expected"}, status_code=400)

            secret = str(body.get("secret") or "")
            st = self._current_static_token()
            if not st or not secrets.compare_digest(secret, st):
                # Deliberately vague — no hint about what the correct value is.
                # Logged too: a burst of these usually means a leaked secret.
                self._record_tv_alert({"ts": _tv_now(), "status": "rejected",
                                       "detail": "bad secret"})
                return JSONResponse({"error": "Unauthorized"}, status_code=401)

            symbol  = str(body.get("symbol") or "").upper().strip()
            action  = str(body.get("action") or "").lower().strip()
            command_text = str(body.get("command") or "").strip()

            # Path A: natural-language command → JARVIS hears/executes it.
            if command_text:
                self._record_tv_alert({"ts": _tv_now(), "status": "queued",
                                       "detail": f"command: {command_text[:80]}",
                                       "symbol": symbol})
                await self._command_queue.put(command_text)
                if self._wake_callback:
                    self._wake_callback()
                return JSONResponse({"ok": True, "routed": "command", "text": command_text})

            # Path B: structured alert → straight to the live trader.
            if action in ("buy", "sell", "long", "short", "close", "trade"):
                try:
                    from plugins.binance_trader import run as bt_run
                    if action == "close":
                        if not symbol:
                            return JSONResponse({"error": "symbol required to close"}, status_code=400)
                        result = bt_run({"action": "close", "symbol": symbol})
                    else:
                        direction = ("LONG" if action in ("buy", "long", "trade") else "SHORT")
                        result = bt_run({
                            "action": "trade",
                            "symbol": symbol,
                            "direction": direction,
                            "entry_price": body.get("entry_price"),
                            "stop_loss": body.get("stop_loss"),
                            "targets": body.get("targets") or [],
                            "position_size": body.get("position_size"),
                            "signal_score": body.get("signal_score"),
                            "rules_votes": {"source": "tradingview_webhook"},
                        })
                    self._record_tv_alert({"ts": _tv_now(), "status": "trade",
                                           "detail": str(result)[:120], "symbol": symbol})
                    return JSONResponse({"ok": True, "routed": "trade", "result": str(result)[:400]})
                except Exception as e:
                    self._record_tv_alert({"ts": _tv_now(), "status": "error",
                                           "detail": str(e)[:120], "symbol": symbol})
                    return JSONResponse({"error": f"trade failed: {e}"}, status_code=500)

            return JSONResponse(
                {"error": "no usable alert — include 'command' or action/symbol"},
                status_code=400,
            )

        # ── Trading activity: alert history, kill log, kill button ────────
        @app.get("/api/trading/activity")
        async def trading_activity(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from plugins.binance_trader import _load_jsonl, _TRADE_LOG
            fills = _load_jsonl(_TRADE_LOG)[-100:]      # recent trade/kill records
            return JSONResponse({
                "alerts": self._tv_alerts[-100:],
                "fills": fills,
                "kills": self._kill_log[-20:],
            })

        @app.post("/api/trading/kill")
        async def trading_kill(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            confirm = str(body.get("confirm") or "").strip().upper()
            if confirm != "KILL":
                return JSONResponse(
                    {"error": "Send {\"confirm\":\"KILL\"} to arm the kill switch"},
                    status_code=400)
            from plugins.binance_trader import kill_switch, kill_switch_result_text
            summary = kill_switch(reason="dashboard button")
            self._kill_log.append({"ts": _tv_now(),
                                   "flattened": len(summary.get("flattened", [])),
                                   "failed": len(summary.get("failed_flattens", []))
                                             + len(summary.get("failed_cancels", [])),
                                   "summary": summary})
            await self.broadcast({"type": "kill_switch", "summary": summary})
            return JSONResponse({"ok": True, "report": kill_switch_result_text(summary)})

        # ── Settings: push-to-talk (Mark-LIV hotkey) ─────────────────────────

        @app.get("/api/settings/push-to-talk")
        async def ptt_get(req: Request):
            """Current push-to-talk state: saved flag + whether the hotkey
            hook is actually live right now."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from memory.config_manager import get_push_to_talk_enabled
            enabled = bool(get_push_to_talk_enabled())
            try:
                from core.hotkey import chord_label
                label = chord_label()
            except Exception:
                label = None
            # `live` = the running app registered its hook, so a toggle takes
            # effect immediately rather than on the next restart.
            return JSONResponse({
                "enabled": enabled,
                "live": self._ptt_callback is not None,
                "chord": label,
            })

        @app.post("/api/settings/push-to-talk")
        async def ptt_set(req: Request):
            """Enable/disable push-to-talk. Persists to config immediately;
            when the app-side hook is registered the change is live too."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from memory.config_manager import (get_push_to_talk_enabled,
                                               save_push_to_talk_enabled)
            body = await req.json()
            want = bool(body.get("enabled"))
            already = bool(get_push_to_talk_enabled())
            scope = None
            if self._ptt_callback is not None and want != already:
                try:
                    scope = self._ptt_callback(want)
                except Exception as e:
                    save_push_to_talk_enabled(already)
                    return JSONResponse({"ok": False, "error": str(e)},
                                        status_code=500)
            save_push_to_talk_enabled(want)
            return JSONResponse({"ok": True, "enabled": want, "scope": scope})

        # ── Settings: one-shot Gemini client (Mark-LIV core/gemini.py) ───────

        @app.get("/api/settings/gemini-client")
        async def gemini_client_get(req: Request):
            """Status of the one-shot Gemini client: ladder, key state and
            which rungs are cooling down after a 429."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import gemini
                status = gemini.status()
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)},
                                    status_code=500)
            return JSONResponse({"ok": True, **status})

        @app.post("/api/settings/gemini-client")
        async def gemini_client_post(req: Request):
            """Actions: {"action": "refresh-key"} re-reads the key from disk;
            {"action": "clear-cooldowns"} forgets 429 cooldowns now."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            action = str(body.get("action") or "")
            try:
                from core import gemini
                if action == "refresh-key":
                    gemini.api_key(refresh=True)
                elif action == "clear-cooldowns":
                    gemini.clear_cooldowns()
                else:
                    return JSONResponse({"ok": False,
                                         "error": "unknown action"},
                                        status_code=400)
                status = gemini.status()
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)},
                                    status_code=500)
            return JSONResponse({"ok": True, **status})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        # ── Settings: NewsAPI key ──────────────────────────────────────────

        @app.get("/api/settings/newsapi")
        async def newsapi_get(req: Request):
            """Return the masked key + whether it is set."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from memory.config_manager import get_newsapi_key
            key = get_newsapi_key()
            masked = (key[:8] + "…" + key[-4:]) if len(key) > 14 else ("•" * len(key) if key else "")
            return JSONResponse({"set": bool(key), "masked": masked})

        @app.post("/api/settings/newsapi")
        async def newsapi_set(req: Request):
            """Save (or clear, with an empty string) the NewsAPI key."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from memory.config_manager import save_newsapi_key
            body = await req.json()
            key = (body.get("key") or "").strip()
            save_newsapi_key(key)
            return JSONResponse({"ok": True, "set": bool(key)})

        @app.post("/api/settings/newsapi/test")
        async def newsapi_test(req: Request):
            """Live-test the key against NewsAPI top-headlines (no save).
            Accepts {"key": "..."} — or uses the stored one when key is empty."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            import json as _json
            from pathlib import Path as _Path
            body = await req.json()
            key = (body.get("key") or "").strip()
            if not key:
                try:
                    key = (_json.loads(
                        (_Path(BASE_DIR) / "config" / "api_keys.json")
                        .read_text(encoding="utf-8")
                    ).get("newsapi_key") or "").strip()
                except Exception:
                    key = ""
            if not key:
                return JSONResponse({"ok": False, "error": "No key provided or stored"})
            try:
                import requests as _requests
                r = _requests.get(
                    "https://newsapi.org/v2/top-headlines",
                    params={"country": "us", "pageSize": 1, "apiKey": key},
                    timeout=8,
                )
                try:
                    b = r.json()
                except ValueError:
                    b = {}
                if r.status_code == 200 and b.get("status") == "ok":
                    n = b.get("totalResults", 0)
                    return JSONResponse({"ok": True, "message": f"Key works — {n} headlines available"})
                msg = (b.get("message") or r.text or "").strip()[:160]
                return JSONResponse({"ok": False, "error": f"NewsAPI {r.status_code}: {msg}"})
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)[:160]})

        # ── Settings: dashboard static token ─────────────────────────────────

        @app.get("/api/settings/dashboard-token")
        async def dashtoken_get(req: Request):
            """Whether a static token is set (never echo the token itself)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            tok = self._current_static_token()
            masked = (tok[:4] + "…" + tok[-2:]) if len(tok) > 8 else ("•" * len(tok) if tok else "")
            return JSONResponse({"set": bool(tok), "masked": masked})

        @app.post("/api/settings/dashboard-token")
        async def dashtoken_set(req: Request):
            """Set (or clear, with an empty string) the static dashboard token.

            Takes effect immediately (cache is dropped here). QR-paired
            sessions are independent credentials — rotating this token does
            not log a paired phone out."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from memory.config_manager import save_dashboard_token
            body = await req.json()
            tok = (body.get("token") or "").strip()
            if tok and len(tok) < 8:
                return JSONResponse(
                    {"ok": False, "error": "Token too short — use at least 8 characters"},
                    status_code=400,
                )
            save_dashboard_token(tok)
            self._static_token = None      # force re-read on next request
            return JSONResponse({"ok": True, "set": bool(tok)})

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not self._token_ok(tok):
                await websocket.close(code=4001)
                return
            await websocket.accept()
            try:
                while True:
                    data = await websocket.receive_bytes()
                    was_active = self.phone_mic_active()
                    self._phone_mic_last = time.time()
                    if not was_active:
                        asyncio.create_task(self.broadcast(
                            {"type": "sys", "text": "Phone microphone live."}
                        ))
                        if self._phone_mic_active_cb:
                            self._fire_phone_mic(True)
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))
                if self._phone_mic_active_cb:
                    self._fire_phone_mic(False)

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name                          # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                safe = _safe_filename(file.filename or "upload")
                dest = self._uploads_dir / safe
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                return JSONResponse(
                                    {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": str(exc)}, status_code=500)

                asyncio.create_task(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.get("/static/lightweight-charts.standalone.js")
        async def serve_lwc():
            if _LWC_FILE.exists():
                return FileResponse(str(_LWC_FILE),
                                    media_type="application/javascript")
            return JSONResponse({"error": "chart library not vendored"},
                                status_code=404)

        @app.get("/api/chart-setup")
        async def chart_setup(req: Request, symbol: str = "BTC-USD",
                              method: str = "swing"):
            """Full live-chart payload: OHLCV + EMA/trail series + the trade
            plan (entry/stop/targets/levels) for one symbol."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from plugins.trading_strategy import (_trade_plan_dict,
                                                  _method, canonical_symbol)
            try:
                plan = _trade_plan_dict(
                    canonical_symbol(symbol),
                    _method({"method": method}), None, None)
            except Exception as e:
                return JSONResponse(
                    {"error": f"Data unavailable for {symbol}: {e}"},
                    status_code=502)
            return JSONResponse(plan)

        @app.get("/chart", response_class=HTMLResponse)
        async def chart_page():
            """Standalone interactive live chart (TradingView-style, browser
            rendered) — deep-linked from JARVIS answers, authed like '/'."""
            return HTMLResponse(_CHART_PAGE_HTML, headers=_NO_CACHE)

        @app.get("/api/files")
        async def list_files(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            files = []
            try:
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    files.append({"name": f.name, "size": f.stat().st_size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, token: str = ""):
            # Auth via query param — browser <a download> can't send custom headers
            tok = token.strip()
            if not self._token_ok(tok):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = re.sub(r'[/\\\\]', '', filename)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            # Images render inline (chart PNGs are meant to be viewed on the
            # phone); everything else keeps the attachment download behavior.
            inline_type = {".png": "image/png", ".jpg": "image/jpeg",
                           ".jpeg": "image/jpeg", ".gif": "image/gif",
                           ".webp": "image/webp"}.get(path.suffix.lower())
            if inline_type:
                return FileResponse(str(path), media_type=inline_type,
                                    headers={"Content-Disposition":
                                             f"inline; filename=\"{safe}\""})
            return FileResponse(str(path), filename=safe)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = "", since: int = 0,
                        boot: str = ""):
            tok = token.strip()
            # Record the running loop BEFORE accept: broadcast users (tests,
            # plugins) read _loop to schedule work — a just-connected client
            # must never race a not-yet-recorded loop.
            self._loop = asyncio.get_running_loop()
            # Accept BEFORE rejecting: a close frame after the handshake is
            # delivered to the browser with its code (4001) intact, while a
            # close before accept surfaces as an opaque handshake failure —
            # the client could never tell "stale token" from "PC down".
            await websocket.accept()
            if not self._token_ok(tok):
                await websocket.close(code=4001)
                return
            self._clients.add(websocket)
            self._ensure_audio_sender()
            # A `since` gap refers to THIS boot's history. A different boot id
            # means the server restarted: the client's baseline is meaningless
            # (and its seq counter has restarted from 1), so replay the recent
            # backlog like a fresh connection instead of a filtered gap.
            if (boot or "").strip() and boot.strip() != self._boot_id:
                since = 0
            backlog = (self._history[-50:] if since <= 0
                       else [e for e in self._history if e.get("seq", 0) > since])
            try:
                await websocket.send_json({"type": "boot",
                                           "boot_id": self._boot_id,
                                           "seq": self._ws_seq})
            except Exception:
                pass
            for entry in backlog:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    data = await websocket.receive_json()
                    mtype = data.get("type")
                    if mtype == "hello":
                        # Phone pages announce themselves so phone_device
                        # commands land on the handset, not a desktop tab.
                        if data.get("role") == "phone":
                            self._phone_clients.add(websocket)
                        continue
                    if mtype == "ping":
                        # Staleness probe — the page pings when the tab comes
                        # back to the foreground and force-reconnects when no
                        # pong (or any message) arrives within a few seconds.
                        try:
                            await websocket.send_json({"type": "pong"})
                        except Exception:
                            pass
                        continue
                    if mtype == "command":
                        enc = data.get("enc", "")
                        t   = self._decrypt(tok, enc) if enc else (data.get("text") or "").strip()
                        if t:
                            await self._command_queue.put(t)
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)
                self._phone_clients.discard(websocket)

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    def _port_in_use(self, port: int) -> bool:
        """True if something is already listening on `port`. Probed with a real
        bind (SO_REUSEADDR matches uvicorn's own bind flags) so we find out
        BEFORE uvicorn does — otherwise it aborts with a raw
        "[Errno 98] error while attempting to bind" and the only hint that
        JARVIS is already running is buried in the traceback."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return False
            except OSError:
                return True

    async def _serve_alias(self) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        if self._port_in_use(PORT + 1):
            print(f"[Dashboard] Port {PORT + 1} already in use — skipping alias server.")
            return
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT + 1)
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT + 1, log_level="warning",
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
        )
        print(f"[Dashboard] Manual entry:  {self._ip}:{PORT + 1}  (type in browser, accept cert once)")
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        # Firewall setup runs in a thread — uvicorn starts immediately,
        # no waiting for UAC dialogs or subprocess timeouts.
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT)

        if self._port_in_use(PORT):
            print(f"[Dashboard] ⚠️  Port {PORT} already in use — another JARVIS "
                  "instance is probably running. Dashboard/phone control stays "
                  "with that process; voice session continues normally.")
            return

        use_ssl  = self._ssl_enabled()
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"

        if use_ssl:
            asyncio.create_task(self._serve_alias())

        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT, log_level="warning",
            **({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)} if use_ssl else {}),
        )

        proto = "https" if use_ssl else "http"
        print(f"[Dashboard] {proto}://{self._ip}:{PORT}")
        print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")
        await uvicorn.Server(cfg).serve()
