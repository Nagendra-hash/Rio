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
import logging
import os
import re
import secrets
import socket
import string
import time
import uuid
from datetime import datetime
from pathlib import Path

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Query
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
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

BASE_DIR = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)
# JARVIS_HOME (pendrive mode): relocates everything writable. Set here, at the
# top of the module, before any function below computes a path.
import os as _os_home

_home = _os_home.environ.get("JARVIS_HOME", "").strip()
if _home:
    BASE_DIR = Path(_home).expanduser().resolve()
STATIC_DIR = Path(__file__).parent / "static"
PORT = 8000
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

_LWC_FILE = STATIC_DIR / "lightweight-charts.standalone.js"


# ── duplicate-event guard (core/dedup.py) ────────────────────────────────────
# One process-wide DedupGuard behind every intake endpoint (WS /ws command,
# POST /api/command, POST /api/message, POST /api/tradingview). An explicit
# message id (mid / msg_id) always wins; without one, a short content hash
# scoped to the channel is used — long enough to absorb a hook retry or a
# double-POST, short enough (5 s) that deliberately re-sent identical text
# goes through. Fail-open: if the guard ever errors, the message proceeds.
def _dedup_acquire(channel: str, event_id: str, text: str) -> bool:
    try:
        from core.dedup import shared_guard

        ok = shared_guard().acquire(channel, event_id, text)
        if not ok:
            print(
                f"[Dashboard] dedup: replay suppressed channel={channel} "
                f"id={(event_id or '')[:24] or 'hash'}"
            )
        return ok
    except Exception:
        return True


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


def _news_settings_deps():
    """Resolve the news-key helpers, or None when unavailable.

    Imported lazily and defensively: the dashboard is documented as optional
    and must still boot when memory/ or actions/ can't be imported. The routes
    that use these return a clear 503 in that case instead of failing the
    whole app at import time.
    """
    try:
        from memory.config_manager import (
            get_news_key,
            save_news_key,
            mask_secret,
        )
        from actions.web_search import _newsapi_key_valid

        return get_news_key, save_news_key, mask_secret, _newsapi_key_valid
    except Exception:
        return None


def _delta_settings_deps():
    """Resolve the Delta credential helpers, or None when unavailable.

    Same defensive contract as `_news_settings_deps`: the dashboard must boot
    even when memory/ can't be imported, so the routes return 503 instead of
    failing at startup.
    """
    try:
        from memory.config_manager import (
            get_delta_api_key,
            get_delta_api_secret,
            save_delta_api_credentials,
            mask_secret,
        )

        return (
            get_delta_api_key,
            get_delta_api_secret,
            save_delta_api_credentials,
            mask_secret,
        )
    except Exception:
        return None


_KEY_CHARS = [
    c
    for c in (string.ascii_uppercase + string.digits)
    if c not in ("O", "I", "L", "0", "1")
]

# ── AES-256-CBC ───────────────────────────────────────────────────────────────
_AES_SALT = b"JARVIS-DASHBOARD-v1"


def _derive_key(session_key: str) -> bytes:
    """SHA-256(sessionKey‖salt) → 32-byte AES-256 key (microseconds, no PBKDF2 needed)."""
    return hashlib.sha256(session_key.encode("utf-8") + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    """Decrypt base64(IV[16] ‖ ciphertext) with AES-256-CBC + PKCS7."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad

    raw = base64.b64decode(enc_b64)
    iv, ct = raw[:16], raw[16:]
    dec = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


# ── CryptoJS (auto-download once, served locally) ─────────────────────────────
_CRYPTOJS_CDN = (
    "https://cdnjs.cloudflare.com/ajax/libs/" "crypto-js/4.2.0/crypto-js.min.js"
)
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"


def _ensure_network_access(port: int) -> None:
    """Cross-platform, best-effort: open port in the OS firewall for LAN access.

    Runs in a background thread — never blocks uvicorn startup.

    Windows : writes a .bat file, runs it elevated via Windows ShellExecuteW
              (native UAC dialog, guaranteed to appear). One-time setup.
    macOS   : osascript admin dialog if the Application Firewall is on.
    Linux   : pkexec GUI → sudo -n → prints manual command as fallback.
    """
    import sys
    import subprocess
    import tempfile
    import threading

    # ── Windows ──────────────────────────────────────────────────────────────
    if sys.platform == "win32":
        import ctypes
        import time

        port_rule = f"JARVIS Dashboard Port {port}"
        prog_rule = "JARVIS Dashboard Python"
        py_exe = sys.executable

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = subprocess.run(
                    [
                        "netsh",
                        "advfirewall",
                        "firewall",
                        "show",
                        "rule",
                        f"name={name}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        def _network_is_public() -> bool:
            try:
                r = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        "(Get-NetConnectionProfile | "
                        "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                        "Measure-Object).Count",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=6,
                )
                return r.stdout.strip() not in ("", "0")
            except Exception:
                return False

        need_port = not _netsh_rule_exists(port_rule)
        need_prog = not _netsh_rule_exists(prog_rule)
        need_private = _network_is_public()

        if not need_port and not need_prog and not need_private:
            return  # already fully configured

        # Build a .bat file — netsh + powershell, runs fast when elevated
        bat_lines = ["@echo off"]
        if need_private:
            bat_lines.append(
                'powershell -NoProfile -NonInteractive -Command "'
                "Get-NetConnectionProfile | "
                "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                'Set-NetConnectionProfile -NetworkCategory Private"'
            )
        if need_port:
            bat_lines.append(
                f"netsh advfirewall firewall add rule "
                f'name="{port_rule}" protocol=TCP dir=in '
                f"localport={port} action=allow"
            )
        if need_prog:
            bat_lines.append(
                f"netsh advfirewall firewall add rule "
                f'name="{prog_rule}" dir=in action=allow '
                f'program="{py_exe}" enable=yes'
            )

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="jarvis_fw_")
        try:
            os.write(fd, bat_body.encode("mbcs"))  # Windows cmd.exe expects ANSI
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            return

        # ── Try running directly (succeeds when already admin) ────────────────
        try:
            r = subprocess.run([bat_path], capture_output=True, timeout=8, shell=True)
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
                None,  # hwnd  (no parent window)
                "runas",  # verb  (request elevation)
                bat_path,  # file  (our .bat)
                None,  # params
                None,  # working dir
                0,  # SW_HIDE (run without a visible cmd window)
            )
            if int(ret) > 32:
                # ShellExecuteW returns immediately; bat finishes in ~1 second.
                # Sleep briefly so the rules are in place before the first retry.
                time.sleep(2)
                print(f"[Dashboard] Network setup complete — port {port} is open.")
                print("[Dashboard] Refresh your phone browser to connect.")
            else:
                print("[Dashboard] Setup was not allowed.")
                print(
                    "[Dashboard] Phone connections may fail until JARVIS is run as Administrator."
                )
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
                [fw_ctl, "--getglobalstate"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if "disabled" in r.stdout.lower():
                return  # firewall off — nothing to do

            py = sys.executable
            listed = subprocess.run(
                [fw_ctl, "--listapps"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if py in listed.stdout:
                return  # already allowed

            print(
                "[Dashboard] One-time network setup — enter your password in the macOS dialog."
            )
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'do shell script "{fw_ctl} --add {py} && {fw_ctl} --unblockapp {py}"'
                    f" with administrator privileges",
                ],
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
            ["firewall-cmd", "--state"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if "running" in r.stdout.lower():
            ok = _privileged(
                ["firewall-cmd", "--add-port", f"{port}/tcp", "--permanent"]
            ) and _privileged(["firewall-cmd", "--reload"])
            if ok:
                print(f"[Dashboard] firewalld: port {port} allowed.")
            else:
                print(
                    f"[Dashboard] Run manually:  sudo firewall-cmd --add-port={port}/tcp --permanent && sudo firewall-cmd --reload"
                )
            return
    except FileNotFoundError:
        pass

    try:  # iptables (not persistent but works until reboot)
        r = subprocess.run(
            ["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5
        )
        if r.returncode == 0:
            if _privileged(
                [
                    "iptables",
                    "-A",
                    "INPUT",
                    "-p",
                    "tcp",
                    "--dport",
                    str(port),
                    "-j",
                    "ACCEPT",
                ]
            ):
                print(f"[Dashboard] iptables: port {port} opened.")
            else:
                print(
                    f"[Dashboard] Run manually:  sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT"
                )
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
        print("[Dashboard] Encryption will fall back to CDN load on client.")


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


def _ensure_certs() -> bool:
    """
    Make sure config/certs holds a TLS key pair, generating a self-signed one the
    first time the dashboard runs.

    The pair is deliberately NOT shipped in the repository. A private key that
    every user downloads is the same as having no private key at all: anyone can
    present a certificate that matches it. Generating locally gives each install
    its own key, costs about a second, and happens exactly once.

    Returns True when a usable pair exists afterwards; False leaves the caller on
    plain HTTP, which still works — the QR code simply encodes http:// instead.
    """
    certs = BASE_DIR / "config" / "certs"
    key_p = certs / "jarvis.key"
    crt_p = certs / "jarvis.crt"
    if key_p.exists() and crt_p.exists():
        return True

    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("[Dashboard] cryptography not installed — serving over plain HTTP.")
        print("[Dashboard] For HTTPS run:  pip install cryptography")
        return False

    try:
        certs.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        who = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS Dashboard"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS"),
            ]
        )

        # The SAN has to cover every address the phone might use: the LAN IP the
        # QR code encodes, plus localhost when testing on the machine itself.
        alt = [
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        ]
        try:
            lan = _local_ip()
            if not lan.startswith("127."):
                alt.append(x509.IPAddress(ipaddress.IPv4Address(lan)))
        except Exception:
            pass  # no LAN address resolvable — localhost entries still work

        # Timezone-aware UTC: datetime.utcnow() is deprecated from Python 3.12 on,
        # and the builder normalises aware values to UTC itself.
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(who)
            .issuer_name(who)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .sign(key, hashes.SHA256())
        )

        key_p.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        crt_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        try:
            import os as _os

            _os.chmod(key_p, 0o600)  # best effort — largely a no-op on Windows
        except Exception:
            pass

        print(
            f"[Dashboard] Generated a self-signed certificate for this machine: {certs}"
        )
        return True
    except Exception as e:
        print(
            f"[Dashboard] Certificate generation failed ({e}) — serving over plain HTTP."
        )
        return False


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _read_frontend(name: str) -> str:
    """Read from dashboard/frontend/ source directory (for app.html with inline JS)."""
    return (Path(__file__).parent / "frontend" / name).read_text(encoding="utf-8")


# ── DashboardServer ───────────────────────────────────────────────────────────


def _tv_now() -> str:
    """Timestamp for trading-activity entries (local time, ISO)."""
    return datetime.now().isoformat(timespec="seconds")


_CHART_PAGE_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RIO Live Chart</title>
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

// PWA glue (deep-linked from shortcuts & RIO answers)
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
        ? "Can't reach RIO — is the PC asleep or the tunnel down? Retrying…"
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


class DashboardServer:
    # Live-instance registry for same-process consumers (rio_window.py needs
    # the running server to mint a pairing key). Set in serve(); None before
    # that. One server per process — a second instantiation would overwrite,
    # which mirrors what the single-instance wrapper already prevents.
    live: "DashboardServer | None" = None

    def __init__(self):
        self._ip = _local_ip()
        self._tokens: set[str] = set()
        self._token_keys: dict[str, str] = {}  # auth_token → session_key
        self._aes_cache: dict[str, bytes] = {}  # session_key → AES bytes
        self._clients: set[WebSocket] = set()
        self._history: list[dict] = []
        self._command_queue = asyncio.Queue()
        self._wake_callback = None
        self._connect_callback = None
        self._phone_mic_callback = None
        self._ptt_callback = None
        self._mute_setter = None  # RIO mic-mute → ui.muted
        self._mute_getter = None
        self._pending_keys: dict[str, float] = {}
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key}
        self._devices_file: Path | None = None  # optional persistent store
        self._static_token: str | None = None  # mtime-cached dashboard token
        self._static_token_mtime: float = 0.0
        self._tv_alerts: list[dict] = []  # TradingView alert history
        self._kill_log: list[dict] = []
        self._phone_audio_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._ws_seq: int = 0  # broadcast sequence
        self._boot_id: str = uuid.uuid4().hex[:12]
        self._uploads_dir = UPLOADS_DIR
        self._login_html = _read("login.html")
        self._app_html = _read_frontend("app.html")
        self._loop: asyncio.AbstractEventLoop | None = None
        # The static API token the user configured (config/api_keys.json →
        # dashboard_auth_token, written by the settings UI and read by
        # config_manager.get_dashboard_token) is a VALID credential, and
        # _auth() below only accepts what is in self._tokens — which used to
        # start EMPTY and only ever gained QR-pairing session tokens. So the
        # documented static token was rejected with 401 forever, and every
        # non-browser client that used it (scripts/jarvis_inbox.py, and with it
        # the whole Hermes/Telegram bridge: jarvis-pdf-forward and
        # jarvis-google-bridge) could never deliver a message.
        #
        # Seeding it here restores what the config file already promises. The
        # token is user-chosen and gitignored; when it is absent (the default)
        # nothing is added and auth behaves exactly as before.
        try:
            from memory.config_manager import get_dashboard_token

            static_tok = (get_dashboard_token() or "").strip()
            if static_tok:
                self._tokens.add(static_tok)
        except Exception:
            pass
        self.app = self._build_app()

    def _load_devices(self) -> None:
        """Load persistent device sessions from the devices file."""
        if self._devices_file and self._devices_file.exists():
            try:
                import json

                data = json.loads(self._devices_file.read_text())
                if isinstance(data, dict):
                    self._device_sessions = data
            except Exception:
                self._device_sessions = {}

    def _save_devices(self) -> None:
        """Save device sessions to the devices file."""
        if self._devices_file:
            try:
                import json

                self._devices_file.write_text(json.dumps(self._device_sessions))
            except Exception:
                pass

    # ── one-time key management ───────────────────────────────────────────

    def new_key(self, expiry_secs: int = 600) -> str:
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        key = "".join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys[key] = now + expiry_secs
        return key

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

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        sk = self._token_keys.get(token)
        if not sk:
            return None
        try:
            return _decrypt_cbc(self._aes_key(sk), enc_b64)
        except Exception:
            return None

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

    # ── trading activity ring buffers ─────────────────────

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

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_phone_mic_callback(self, fn) -> None:
        """Main loop registers its phone-mic duck/pause hook here.

        main.py has called this since the Mark-LIV ducking work, but the
        setter itself was never ported into this server rewrite — the
        AttributeError killed the whole dashboard at startup
        ('[Dashboard] Disabled: …'), silently taking phone-audio down with
        it. Same defensive style as set_wake_callback: hooks are optional.
        """
        self._phone_mic_callback = fn
        # The ws/phone-audio handler flips this as streams open/close; main.py
        # additionally polls phone_mic_active() from its playback loop.
        self._phone_mic_on = False

    def set_ptt_callback(self, fn) -> None:
        """Push-to-talk toggle hook from the phone settings panel."""
        self._ptt_callback = fn

    def set_mute_callback(self, setter, getter=None) -> None:
        """Mic-mute hooks from the RIO UI (Home / Chat mute buttons).

        setter(muted: bool) → flips the live session's mic gate (main.py sets
        `ui.muted`, the same switch the HUD mute button uses — the audio
        callback stops sending PC-mic audio to the session).
        getter() → bool, the current state; without a getter the RIO UI is
        told the state is unknown (it then renders an honest N/A).
        """
        self._mute_setter = setter
        self._mute_getter = getter

    def phone_mic_active(self) -> bool:
        """True while the phone mic is streaming into the live session.

        main.py's playback loop polls this to duck PC speakers while the
        phone user is talking. The setter for the internal flag arrives via
        the _phone_mic_callback; without a phone connected this is simply
        always False (PC-only behaviour, as before ducking existed).
        """
        return bool(getattr(self, "_phone_mic_on", False))

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    # ── broadcast ────────────────────────────────────────────────────────

    def _next_seq(self) -> int:
        """Monotonic sequence for broadcast entries so /ws clients can resume
        with ?since=<seq> instead of re-receiving (and re-appending) the same
        history after every reconnect. Written from the event loop."""
        self._ws_seq += 1
        return self._ws_seq

    async def broadcast(self, msg: dict) -> None:
        msg["seq"] = self._next_seq()
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

    async def broadcast_transient(self, msg: dict) -> None:
        """Fan out to connected clients WITHOUT recording to _history.

        Used for high-frequency ephemeral frames — viseme mouth levels and
        live assistant state for the RIO 3D face. History IS the WS replay;
        at ~12 Hz these frames would flood the last-50 replay and push real
        messages out of it, so they live only in the moment."""
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        # ── OpenTelemetry instrumentation ─────────────────────────────────────
        # Adds trace context to all requests, exports spans/metrics.
        # Disable with OTEL_PYTHON_DISABLED=1
        try:
            from core.telemetry import instrument_fastapi

            instrument_fastapi(app)
        except Exception:
            pass  # never break the dashboard on observability failure

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not tok:
                # Element auth (<img src=MJPEG>, <audio src=mp3>) cannot send
                # headers — same query-token contract the /ws websocket uses.
                tok = (req.query_params.get("token") or "").strip()
            return self._token_ok(tok)

        # serve CryptoJS from local cache, fallback to CDN redirect
        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _CRYPTOJS_FILE.exists():
                return FileResponse(
                    str(_CRYPTOJS_FILE), media_type="application/javascript"
                )
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
            return FileResponse(
                str(mf), media_type="application/manifest+json", headers=_NO_CACHE
            )

        @app.get("/sw.js")
        async def serve_sw():
            sw = STATIC_DIR / "sw.js"
            if not sw.exists():
                return JSONResponse({"error": "sw missing"}, status_code=404)
            return FileResponse(
                str(sw),
                media_type="application/javascript",
                headers={**_NO_CACHE, "Service-Worker-Allowed": "/"},
            )

        @app.get("/static/icons/{filename}")
        async def serve_icon(filename: str):
            safe = re.sub(r"[^A-Za-z0-9._-]", "", filename)
            path = STATIC_DIR / "icons" / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(
                str(path),
                media_type="image/png",
                headers={"Cache-Control": "public, max-age=604800"},
            )

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html, headers=_NO_CACHE)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            # Auth is handled client-side via sessionStorage bearer token.
            # Server-side header auth can't work here because browser navigations
            # don't send custom headers (location.href doesn't carry Authorization).
            html = self._app_html.replace("__IP__", self._ip).replace(
                "__PORT__", str(PORT)
            )
            return HTMLResponse(html, headers=_NO_CACHE)

        @app.post("/login")
        async def login(req: Request):
            body = await req.json()
            entered = str(body.get("pin", "")).strip().upper()
            now = time.time()
            if entered in self._pending_keys and self._pending_keys[entered] > now:
                del self._pending_keys[entered]  # one-time use
                tok = secrets.token_urlsafe(32)
                self._tokens.add(tok)
                self._token_keys[tok] = entered
                self._aes_key(entered)  # pre-derive & cache
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(
                    self.broadcast(
                        {"type": "sys", "text": "Remote connection established."}
                    )
                )
                # Bearer token in response body — no cookies needed (works on any browser/HTTP)
                return JSONResponse({"ok": True, "token": tok})
            return JSONResponse(
                {"ok": False, "error": "Invalid or expired key"}, status_code=401
            )

        @app.get("/auto-login")
        async def auto_login(key: str = "", to: str = ""):
            """QR code target — validates one-time key, creates session, redirects phone."""
            now = time.time()
            if (
                not key
                or key not in self._pending_keys
                or self._pending_keys[key] <= now
            ):
                return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
  h2{color:#f87171;margin-bottom:12px}p{color:#5e6a7e;font-size:14px}
</style></head>
<body><div><h2>Link Expired</h2>
<p>Press <strong style="color:#dde3ed">Remote Control</strong> in RIO to get a new QR code.</p>
</div></body></html>""")

            del self._pending_keys[key]
            tok = secrets.token_urlsafe(32)
            dev_tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = key
            self._aes_key(key)
            self._device_sessions[dev_tok] = {"session_key": key}
            self._save_devices()

            # Optional landing route for the auto-login handoff (RIO desktop
            # window passes to=/home). Only real RIO shell routes are allowed
            # — anything else falls back to '/' so `to=` can never act as an
            # open redirect or inject into the HTML template (single quote
            # would break the JS string). Same-origin relative path only.
            _ALLOWED_DESTS = (
                "/",
                "/home",
                "/news",
                "/chat",
                "/agents",
                "/workflows",
                "/ops",
                "/brain",
                "/memory",
                "/files",
                "/hermes",
                "/missions",
                "/activity",
                "/system",
                "/settings",
                "/tools",
                "/video",
            )  # — same list rio_api.register mounts, kept literal so
            #   this guard has no import-order dependency
            _dest = to if to in _ALLOWED_DESTS else "/"

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(
                self.broadcast(
                    {
                        "type": "sys",
                        "text": "Remote connection established via QR code.",
                    }
                )
            )

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
  setTimeout(function(){{location.replace('{_dest}')}},400);
</script>
<p>Connecting to RIO…</p>
</body></html>""")

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
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(
                self.broadcast(
                    {"type": "sys", "text": "Known device reconnected automatically."}
                )
            )
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
                self._record_tv_alert(
                    {"ts": _tv_now(), "status": "rejected", "detail": "bad secret"}
                )
                return JSONResponse({"error": "Unauthorized"}, status_code=401)

            symbol = str(body.get("symbol") or "").upper().strip()
            action = str(body.get("action") or "").lower().strip()
            command_text = str(body.get("command") or "").strip()

            # Path A: natural-language command → JARVIS hears/executes it.
            if command_text:
                # Webhook replays (TradingView re-delivering the same alert)
                # must not become repeated model turns; channel-scoped hash.
                if not _dedup_acquire("tradingview", "", command_text):
                    self._record_tv_alert(
                        {
                            "ts": _tv_now(),
                            "status": "duplicate-dropped",
                            "detail": f"command: {command_text[:80]}",
                            "symbol": symbol,
                        }
                    )
                    return JSONResponse({"ok": True, "routed": "duplicate"})
                self._record_tv_alert(
                    {
                        "ts": _tv_now(),
                        "status": "queued",
                        "detail": f"command: {command_text[:80]}",
                        "symbol": symbol,
                    }
                )
                await self._command_queue.put(command_text)
                if self._wake_callback:
                    self._wake_callback()
                return JSONResponse(
                    {"ok": True, "routed": "command", "text": command_text}
                )

            # Path B: structured alert → straight to the live trader.
            if action in ("buy", "sell", "long", "short", "close", "trade"):
                try:
                    from plugins.binance_trader import run as bt_run

                    if action == "close":
                        if not symbol:
                            return JSONResponse(
                                {"error": "symbol required to close"}, status_code=400
                            )
                        result = bt_run({"action": "close", "symbol": symbol})
                    else:
                        direction = (
                            "LONG" if action in ("buy", "long", "trade") else "SHORT"
                        )
                        result = bt_run(
                            {
                                "action": "trade",
                                "symbol": symbol,
                                "direction": direction,
                                "entry_price": body.get("entry_price"),
                                "stop_loss": body.get("stop_loss"),
                                "targets": body.get("targets") or [],
                                "position_size": body.get("position_size"),
                                "signal_score": body.get("signal_score"),
                                "rules_votes": {"source": "tradingview_webhook"},
                            }
                        )
                    self._record_tv_alert(
                        {
                            "ts": _tv_now(),
                            "status": "trade",
                            "detail": str(result)[:120],
                            "symbol": symbol,
                        }
                    )
                    return JSONResponse(
                        {"ok": True, "routed": "trade", "result": str(result)[:400]}
                    )
                except Exception as e:
                    self._record_tv_alert(
                        {
                            "ts": _tv_now(),
                            "status": "error",
                            "detail": str(e)[:120],
                            "symbol": symbol,
                        }
                    )
                    return JSONResponse(
                        {"error": f"trade failed: {e}"}, status_code=500
                    )

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

            fills = _load_jsonl(_TRADE_LOG)[-100:]  # recent trade/kill records
            return JSONResponse(
                {
                    "alerts": self._tv_alerts[-100:],
                    "fills": fills,
                    "kills": self._kill_log[-20:],
                }
            )

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
                    {"error": 'Send {"confirm":"KILL"} to arm the kill switch'},
                    status_code=400,
                )
            from plugins.binance_trader import kill_switch, kill_switch_result_text

            summary = kill_switch(reason="dashboard button")
            self._kill_log.append(
                {
                    "ts": _tv_now(),
                    "flattened": len(summary.get("flattened", [])),
                    "failed": len(summary.get("failed_flattens", []))
                    + len(summary.get("failed_cancels", [])),
                    "summary": summary,
                }
            )
            await self.broadcast({"type": "kill_switch", "summary": summary})
            return JSONResponse(
                {"ok": True, "report": kill_switch_result_text(summary)}
            )

        @app.post("/api/phone/pair")
        async def phone_pair(req: Request):
            """Mint a one-time pairing key for a phone: QR + universal link.
            Reuses the same one-time key store as the desktop 'Remote Control'
            QR flow, so /auto-login works unchanged."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            key = self.new_key(expiry_secs=600)  # 10 min — scanning takes time
            url = self.get_url()
            link = f"{url}/auto-login?key={key}"
            return JSONResponse(
                {
                    "ok": True,
                    "key": key,
                    "url": url,
                    "manual_url": self.get_manual_url(),
                    "link": link,
                    "expires_in": 600,
                    "qr": _qr_png_b64(link),
                }
            )

        @app.get("/api/phone/status")
        async def phone_status(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            now = time.time()
            self._pending_keys = {
                k: v for k, v in self._pending_keys.items() if v > now
            }
            return JSONResponse(
                {
                    "ok": True,
                    "paired_devices": len(self._device_sessions),
                    "pending_keys": len(self._pending_keys),
                    "url": self.get_url(),
                    "manual_url": self.get_manual_url(),
                }
            )

        @app.post("/api/voice/command")
        async def voice_command(req: Request):
            """Process voice command from phone/dashboard."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "Invalid JSON"}, status_code=400)

            transcript = (body.get("transcript") or "").strip()
            session_id = (body.get("session_id") or "default").strip()
            auto_confirm = bool(body.get("auto_confirm", False))

            if not transcript:
                return JSONResponse({"error": "Empty transcript"}, status_code=400)

            # Import and execute voice command handler
            try:
                from actions.voice_command import voice_command_handler
                result = await voice_command_handler({
                    "transcript": transcript,
                    "session_id": session_id,
                    "auto_confirm": auto_confirm,
                    "return_audio": True
                })
                return JSONResponse(result)
            except Exception as e:
                logger.error(f"Voice command error: {e}")
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc = body.get("enc", "")
            if enc:
                text = self._decrypt(token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if text:
                conv = str(body.get("conv") or "").strip()[:64]
                # Duplicate-event guard: same client message id (or same text
                # within the guard's short window) enqueued once only — a
                # double-POST from a double-click or a WS/HTTP fallback race
                # must not become two model turns.
                mid = str(body.get("mid") or body.get("message_id") or "").strip()[:128]
                if not _dedup_acquire("command", mid, text):
                    return JSONResponse({"ok": True, "duplicate": True})
                await self._command_queue.put((text, conv) if conv else text)
                if self._wake_callback:
                    self._wake_callback()
            return JSONResponse({"ok": True})

        # ── Hermes bridge intake ────────────────────────────────────────
        # The one normalized entry point from messaging platforms into Jarvis.
        # scripts/jarvis_inbox.py multipart-POSTs {channel, chat_id, text, file}
        # here; we rebuild the [INBOUND channel:chat] tag that main.py's
        # _parse_inbound splits back apart, so the reply arm can return the
        # answer on the same channel it arrived on. Intent decisions stay in
        # Jarvis — this layer only normalizes and enqueues.

        if _UPLOAD_OK:

            @app.post("/api/message")
            async def api_message(
                req: Request,
                channel: str = FastAPIFile(None),
                chat_id: str = FastAPIFile(None),
                text: str = FastAPIFile(None),
                msg_id: str = FastAPIFile(None),
                file: UploadFile = FastAPIFile(None),
            ):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                channel = (channel or "telegram").strip() or "telegram"
                chat_id = (chat_id or "").strip()
                text = (text or "").strip()

                saved = None
                if file is not None and file.filename:
                    safe = _safe_filename(file.filename)
                    dest = self._uploads_dir / safe
                    stem, suffix = Path(safe).stem, Path(safe).suffix
                    counter = 1
                    while dest.exists():
                        dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                        counter += 1
                    size = 0
                    try:
                        with open(dest, "wb") as fout:
                            while True:
                                chunk = await file.read(65536)
                                if not chunk:
                                    break
                                size += len(chunk)
                                if size > MAX_UPLOAD_MB * 1024 * 1024:
                                    fout.close()
                                    dest.unlink(missing_ok=True)
                                    return JSONResponse(
                                        {
                                            "error": "File too large "
                                            f"(max {MAX_UPLOAD_MB} MB)"
                                        },
                                        status_code=413,
                                    )
                                fout.write(chunk)
                    except Exception as exc:
                        try:
                            dest.unlink(missing_ok=True)
                        except Exception:
                            pass
                        return JSONResponse({"error": str(exc)}, status_code=500)
                    saved = str(dest)

                if not text and not saved:
                    return JSONResponse({"error": "empty message"}, status_code=400)

                target = f"{channel}:{chat_id}" if chat_id else channel

                # ── Phase 7 · bridge pipeline events ──────────────
                # The two stages that belong to THIS request: it arrived
                # (message_received) and intake handling started
                # (processing). Journaled to memory/hermes_events.jsonl and
                # pushed on the existing /ws — only for messages that really
                # landed here; a stage that never fires emits nothing.
                try:
                    from dashboard.hermes_events import emit as _emit_event

                    _emit_event(
                        self,
                        "message_received",
                        channel=target,
                        platform=channel,
                        chat=chat_id,
                        text=text,
                        file=(Path(saved).name if saved else None),
                    )
                    _emit_event(
                        self,
                        "processing",
                        channel=target,
                        platform=channel,
                        chat=chat_id,
                    )
                except Exception:
                    pass

                # ── Orchestrator observe point (Phase 1) ────────────────
                # Shadow-mode by default: classify + journal the turn, then
                # proceed EXACTLY as before. Wrapped so it can never break
                # the Hermes intake path. JARVIS_ORCHESTRATOR=on/off changes
                # the mode; see orchestrator/integration.py.
                try:
                    from orchestrator import integration as _orch_integration

                    _verdict = _orch_integration.observe_user_turn(
                        text,
                        channel=channel,
                        chat_id=chat_id,
                        attachments=[saved] if saved else None,
                    )
                    # Phase 7: what the router selected for this turn —
                    # the verdict the intake already computes (task type,
                    # confidence, mode). Nothing extra is classified here.
                    try:
                        _res = (_verdict or {}).get("result")
                        _data = getattr(_res, "data", None) or {}
                        _cls = _data.get("classification") or {}
                        _agent = (
                            _cls.get("task_type")
                            or _data.get("task_type")
                            or getattr(_res, "agent", "")
                            or ""
                        )
                        _conf = _cls.get("confidence")
                        _detail = (
                            f"confidence {_conf:.2f}"
                            if isinstance(_conf, (int, float))
                            else str(_cls.get("reason") or "")
                        )[:160]
                        _emit_event(
                            self,
                            "agent_selected",
                            channel=target,
                            platform=channel,
                            chat=chat_id,
                            agent=(_agent or "unclassified"),
                            mode=(_verdict or {}).get("mode", ""),
                            detail=_detail,
                        )
                    except Exception:
                        pass
                except Exception:
                    pass

                line = f"[INBOUND {target}] {text}".rstrip()
                if saved:
                    line += f"\n[FILE: {saved}]"
                # Duplicate-event guard (Hermes bridge): a hook retry or a
                # webhook replay re-POSTs the same payload within seconds —
                # an explicit msg_id (when the sender supplies one) or the
                # channel-scoped content hash absorbs it. Distinct messages
                # from the same chat are untouched (different text or >5s).
                msg_id = str(msg_id or "").strip()[:128]
                if not _dedup_acquire(
                    target, msg_id, text + (f"|{saved}" if saved else "")
                ):
                    return JSONResponse({"ok": True, "duplicate": True})
                await self._command_queue.put(line)

                import datetime

                asyncio.create_task(
                    self.broadcast(
                        {
                            "type": "log",
                            "speaker": "user",
                            "text": f"[{target}] {text}"
                            if text
                            else f"[{target}] 📎 {Path(saved).name}",
                            "ts": datetime.datetime.now().isoformat(),
                        }
                    )
                )
                if self._wake_callback:
                    self._wake_callback()
                return JSONResponse(
                    {"ok": True, "file": Path(saved).name if saved else None}
                )
        else:

            @app.post("/api/message")
            async def api_message_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        @app.get("/api/orchestrator")
        async def orchestrator_status(req: Request):
            """Phase 1 orchestrator status: mode + registered agents."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from orchestrator import integration as _orch_integration

                orch = _orch_integration.shared_orchestrator()
                import os as _os

                mode = (
                    (_os.environ.get("JARVIS_ORCHESTRATOR") or "shadow").strip().lower()
                )
                return JSONResponse(
                    {
                        "ok": True,
                        "mode": mode if mode in {"on", "shadow", "off"} else "shadow",
                        "agents": orch.registry.describe(),
                        "pending_approvals": len(orch.approvals),
                    }
                )
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=200)

        # ── RAG service REST surface ─────────────────────────────────────
        # Upload, index, query, update, forget — every call goes through
        # core/rag_service (ACL + verdicts), never straight to core/rag.
        # Auth stays here (_auth); this module only maps HTTP → service.
        try:
            from dashboard.rag_api import register as _rag_register

            _rag_register(app, _auth)
        except Exception as _rag_exc:
            print(f"[Dashboard] RAG API unavailable: {_rag_exc}")

        # ── Memory REST surface ────────────────────────────────────────────
        try:
            from dashboard.memory_api import register as _mem_register
            _mem_register(app, _auth)
        except Exception as _mem_exc:
            print(f"[Dashboard] Memory API unavailable: {_mem_exc}")

        # ── Autonomy REST surface (Phase 20) ─────────────────────────
        # Emergency stop, approvals, scheduled tasks, audit — HTTP maps
        # onto orchestrator/autonomy; enforcement lives there.
        try:
            from dashboard.autonomy_api import register as _auto_register

            _auto_register(app, _auth)
        except Exception as _auto_exc:
            print(f"[Dashboard] Autonomy API unavailable: {_auto_exc}")

        # ── RIO shell (Phase 1 — UI foundation) ────────────────────────
        # Serves the top-level RIO routes + /rio/assets/* and one
        # real-probe endpoint /api/rio/health. Same mount pattern as the
        # RAG and autonomy surfaces above; _auth is the shared bearer gate.
        try:
            from dashboard.rio_api import register as _rio_register

            _rio_register(app, _auth)
        except Exception as _rio_exc:
            print(f"[Dashboard] RIO shell unavailable: {_rio_exc}")

        # ── Hermes Control Center (Phase 7) ──────────────────────────
        # Status · stats · recent · events for the Telegram/WhatsApp
        # bridge. Same mount pattern as the surfaces above; _auth is the
        # shared bearer gate.
        try:
            from dashboard.hermes_api import register as _hermes_register

            _hermes_register(app, _auth)
        except Exception as _hermes_exc:
            print(f"[Dashboard] Hermes API unavailable: {_hermes_exc}")

        # -- System Control Center (Phase 8) ---------------------------
        # Metrics / services / activity / notifications. Same mount
        # pattern as the surfaces above; the journal watcher pushes
        # live updates over the existing /ws broadcast.
        try:
            from dashboard.system_api import register as _sys_register
            from dashboard.system_api import start_watcher as _sys_watch

            _sys_register(app, _auth)
            _sys_watch(self)
        except Exception as _sys_exc:
            print(f"[Dashboard] System API unavailable: {_sys_exc}")

        # -- Trading Center + Mission Center (Phase 9) ------------------
        # Read-through over the existing trading stack (MarketData /
        # TradingAgent / PaperBroker) and the autonomy engine. Same
        # mount pattern; _auth is the shared bearer gate.
        try:
            from dashboard.trading_api import register as _tr_register

            _tr_register(app, _auth)
        except Exception as _tr_exc:
            print(f"[Dashboard] Trading API unavailable: {_tr_exc}")

        # -- Liquidity sweep / reversal engine (read-only strategy) ------
        # Zones, state machine, confirmation checklist and risk proposal.
        # There is no order endpoint on this surface.
        try:
            from dashboard.liquidity_api import register as _liq_register

            _liq_register(app, _auth)
        except Exception as _liq_exc:
            print(f"[Dashboard] Liquidity API unavailable: {_liq_exc}")

        # -- PAPER trading account (virtual only) -----------------------
        # Account/positions/performance + start/stop/reset. There is no
        # order, broker or live-switch route on this surface.
        try:
            from dashboard.paper_api import register as _paper_register

            _paper_register(app, _auth)
        except Exception as _paper_exc:
            print(f"[Dashboard] Paper API unavailable: {_paper_exc}")

        # -- Continuous trading monitor (health + signal ledger) --------
        # Reports whether the background worker and market subscription are
        # genuinely running. Read-only; no order route exists.
        try:
            from dashboard.monitor_api import register as _mon_register

            _mon_register(app, _auth)
        except Exception as _mon_exc:
            print(f"[Dashboard] Monitor API unavailable: {_mon_exc}")

        # -- Backtest research lab (Phase 22, research only) ------------
        # Jobs/champions/reports over orchestrator/agents/backtest. There
        # is no order or broker endpoint on this surface by design.
        try:
            from dashboard.backtest_api import register as _bt_register

            _bt_register(app, _auth)
        except Exception as _bt_exc:
            print(f"[Dashboard] Backtest API unavailable: {_bt_exc}")

        # -- Universal Repository Agent (tools / skills) ----------------
        # Install + lifecycle for managed GitHub repos under ~/JarvisTools;
        # all mutations run via the detached job store, never in-request.
        try:
            from dashboard.repo_api import register as _repo_register

            _repo_register(app, _auth)
        except Exception as _repo_exc:
            print(f"[Dashboard] Repo API unavailable: {_repo_exc}")

        # -- Video Creation Agent (Video Studio) --------------------------
        # Jobs run via the detached worker; file serving is whitelisted to
        # known output roots; progress is the store's real phase/progress.
        try:
            from dashboard.video_api import register as _video_register

            _video_register(app, _auth)
        except Exception as _video_exc:
            print(f"[Dashboard] Video API unavailable: {_video_exc}")

        # -- Voice Command Analytics & History (Phase 4) ---------------
        # Stats, history, trends, hourly distribution, sessions, wake word
        # training. Same mount pattern as the surfaces above; _auth is the
        # shared bearer gate.
        try:
            from dashboard.voice_api import register as _voice_register

            _voice_register(app, _auth)
        except Exception as _voice_exc:
            print(f"[Dashboard] Voice API unavailable: {_voice_exc}")

        # ── Svelte Frontend (StaticFiles) ────────────────────────────────────────
        # Mount the Svelte build output (dashboard/static) for assets
        if STATIC_DIR.exists():
            app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
            app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")

        # PWA files served at root paths (referenced by app.html)
        from fastapi.responses import FileResponse

        @app.get("/sw.js")
        async def sw_js():
            return FileResponse(str(STATIC_DIR / "sw.js"), headers=_NO_CACHE)

        @app.get("/manifest.webmanifest")
        async def manifest_webmanifest():
            return FileResponse(
                str(STATIC_DIR / "manifest.webmanifest"), headers=_NO_CACHE
            )

        @app.get("/favicon.svg")
        async def favicon_svg():
            return FileResponse(str(STATIC_DIR / "favicon.svg"), headers=_NO_CACHE)

        # ── News key (CurrentsAPI) ────────────────────────────────────────────
        # The dashboard ⚙ Settings panel needs these to store the key that
        # actions/web_search.py reads via get_news_key(). GET reports only
        # whether a key exists plus a masked preview — the real key is never
        # echoed back, so a stolen session can't read it out of the UI.

        @app.get("/api/settings/newsapi")
        async def newsapi_get(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            deps = _news_settings_deps()
            if deps is None:
                return JSONResponse(
                    {"error": "News backend unavailable"}, status_code=503
                )
            get_news_key, _save, mask_secret, _valid = deps
            key = get_news_key()
            return JSONResponse(
                {
                    "set": bool(key),
                    "masked": mask_secret(key),
                }
            )

        @app.post("/api/settings/newsapi")
        async def newsapi_save(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            deps = _news_settings_deps()
            if deps is None:
                return JSONResponse(
                    {"error": "News backend unavailable"}, status_code=503
                )
            _get, save_news_key, _mask, _newsapi_key_valid = deps
            body = await req.json()
            key = str(body.get("key") or "").strip()
            if not key:
                return JSONResponse({"ok": False, "error": "No key supplied"})
            # Reject a key CurrentsAPI won't accept, rather than storing it and
            # letting every later news query silently fall through to Gemini/DDG.
            ok, msg = _newsapi_key_valid(key)
            if not ok:
                return JSONResponse({"ok": False, "error": msg})
            try:
                save_news_key(key)
            except Exception as e:
                return JSONResponse({"ok": False, "error": f"Could not save: {e}"})
            return JSONResponse({"ok": True, "message": msg})

        @app.post("/api/settings/newsapi/test")
        async def newsapi_test(req: Request):
            """Validate a key WITHOUT storing it (the TEST button)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            deps = _news_settings_deps()
            if deps is None:
                return JSONResponse(
                    {"error": "News backend unavailable"}, status_code=503
                )
            _get, _save, _mask, _newsapi_key_valid = deps
            body = await req.json()
            ok, msg = _newsapi_key_valid(str(body.get("key") or ""))
            return JSONResponse({"ok": ok, "message" if ok else "error": msg})

        # ── Settings: Delta Exchange India credentials ───────────────────────
        # Optional and read-only. Delta's public data needs no key; saving a
        # pair only marks the venue configured for /api/rio/trading/delta/status.
        # Env vars (DELTA_API_KEY / DELTA_API_SECRET) win over what is stored
        # here — the response says which source is live. Secrets are never
        # echoed back, only masked previews.

        @app.get("/api/settings/delta")
        async def delta_settings_get(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            deps = _delta_settings_deps()
            if deps is None:
                return JSONResponse(
                    {"error": "Delta settings backend unavailable"}, status_code=503
                )
            get_key, get_secret, _save, mask_secret = deps
            key, secret = get_key(), get_secret()
            try:
                from orchestrator.agents.delta_exchange import (
                    DEFAULT_BASE_URL,
                    credentials_source,
                )

                source = credentials_source()
                base_url = (
                    _os_home.environ.get("DELTA_BASE_URL", "").strip()
                    or DEFAULT_BASE_URL
                )
            except Exception:
                source, base_url = "none", ""
            return JSONResponse(
                {
                    "ok": True,
                    "set": bool(key and secret),
                    "key_set": bool(key),
                    "secret_set": bool(secret),
                    "masked_key": mask_secret(key),
                    "masked_secret": mask_secret(secret),
                    "source": source,
                    "base_url": base_url,
                    "read_only": True,
                    "mode": "public data + paper only",
                }
            )

        @app.post("/api/settings/delta")
        async def delta_settings_save(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            deps = _delta_settings_deps()
            if deps is None:
                return JSONResponse(
                    {"error": "Delta settings backend unavailable"}, status_code=503
                )
            _get_key, _get_secret, save, _mask = deps
            body = await req.json()
            if bool(body.get("clear")):
                save("", "")
                return JSONResponse(
                    {
                        "ok": True,
                        "set": False,
                        "message": "Stored Delta credentials cleared.",
                    }
                )
            key = str(body.get("key") or "").strip()
            secret = str(body.get("secret") or "").strip()
            if not key or not secret:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "Both the API key and the API secret are required.",
                    }
                )
            try:
                save(key, secret)
            except Exception as e:
                return JSONResponse({"ok": False, "error": f"Could not save: {e}"})
            return JSONResponse(
                {
                    "ok": True,
                    "set": True,
                    "message": "Delta credentials saved. Public data works "
                    "without them; live trading stays disabled.",
                }
            )

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
            return JSONResponse(
                {
                    "enabled": enabled,
                    "live": self._ptt_callback is not None,
                    "chord": label,
                }
            )

        @app.post("/api/settings/push-to-talk")
        async def ptt_set(req: Request):
            """Enable/disable push-to-talk. Persists to config immediately;
            when the app-side hook is registered the change is live too."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from memory.config_manager import (
                get_push_to_talk_enabled,
                save_push_to_talk_enabled,
            )

            body = await req.json()
            want = bool(body.get("enabled"))
            already = bool(get_push_to_talk_enabled())
            scope = None
            if self._ptt_callback is not None and want != already:
                try:
                    scope = self._ptt_callback(want)
                except Exception as e:
                    save_push_to_talk_enabled(already)
                    return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
            save_push_to_talk_enabled(want)
            return JSONResponse({"ok": True, "enabled": want, "scope": scope})

        # ── Settings: assistant mic mute (RIO Home / Chat mute buttons) ─────
        # The same switch the HUD mute button flips: `ui.muted` gates the PC
        # mic in main.py's audio callback, so a muted assistant stops SENDING
        # your voice to the live session. Session-scoped — it resets to live
        # when the assistant restarts (the HUD behaves the same way).

        @app.get("/api/settings/mic-mute")
        async def mic_mute_get(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            muted = None
            if self._mute_getter is not None:
                try:
                    muted = bool(self._mute_getter())
                except Exception:
                    muted = None
            return JSONResponse(
                {
                    "muted": muted,  # None = assistant not running
                    "live": self._mute_setter is not None,
                }
            )

        @app.post("/api/settings/mic-mute")
        async def mic_mute_set(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._mute_setter is None:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "Assistant session not running — the mic state "
                        "can only be changed while RIO is live.",
                    },
                    status_code=503,
                )
            body = await req.json()
            want = bool(body.get("muted"))
            try:
                scope = self._mute_setter(want)
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
            return JSONResponse({"ok": True, "muted": want, "scope": scope})

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
                return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
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
                    return JSONResponse(
                        {"ok": False, "error": "unknown action"}, status_code=400
                    )
                status = gemini.status()
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
            return JSONResponse({"ok": True, **status})

        @app.get("/api/settings/dashboard-token")
        async def dashtoken_get(req: Request):
            """Whether a static token is set (never echo the token itself)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            tok = self._current_static_token()
            masked = (
                (tok[:4] + "…" + tok[-2:])
                if len(tok) > 8
                else ("•" * len(tok) if tok else "")
            )
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
                    {
                        "ok": False,
                        "error": "Token too short — use at least 8 characters",
                    },
                    status_code=400,
                )
            save_dashboard_token(tok)
            self._static_token = None  # force re-read on next request
            return JSONResponse({"ok": True, "set": bool(tok)})

        # ── Admin Config API (for Svelte frontend) ───────────────────────────
        @app.get("/api/admin/config")
        async def admin_config_get(req: Request):
            """Get full config for admin panel."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from config.settings import get_settings, get_api_config

                s = get_settings()
                cfg = get_api_config()
                # Build the config shape expected by frontend
                return JSONResponse(
                    {
                        "env": s.env,
                        "logLevel": s.log_level,
                        "llm": {
                            "routerMode": s.llm.router_mode,
                            "geminiApiKeys": ",".join(
                                k.get_secret_value()
                                for k in s.llm.gemini_api_keys_parsed
                            ),
                            "nvidiaEnabled": s.llm.nvidia_enabled,
                            "nvidiaApiKey": s.llm.nvidia_api_key.get_secret_value()
                            if s.llm.nvidia_api_key
                            else "",
                            "openrouterApiKeys": ",".join(
                                k.get_secret_value()
                                for k in s.llm.openrouter_api_keys_parsed
                            ),
                            "ollamaEnabled": s.llm.ollama_enabled,
                            "ollamaModels": ",".join(s.llm.ollama_models_parsed),
                        },
                        "ui": {
                            "assistantName": s.ui.assistant_name,
                            "userName": s.ui.user_name,
                            "voice": s.ui.voice,
                            "wakeWordEnabled": s.ui.wake_word_enabled,
                            "pushToTalkEnabled": s.ui.push_to_talk_enabled,
                            "thinkingEnabled": s.ui.thinking_enabled,
                            "webglHud": False,  # from settings if needed
                        },
                        "trading": {
                            "paperEnabled": s.trading.paper_enabled,
                            "deltaTestnet": s.trading.delta_testnet,
                        },
                        "agentGates": {
                            "CODING_AGENT": s.agents.coding_agent,
                            "RESEARCH_AGENT": s.agents.research_agent,
                            "TRADING_AGENT": s.agents.trading_agent,
                            "RAG_AGENT": s.agents.rag_agent,
                            "SYSTEM_AGENT": s.agents.system_agent,
                            "EMAIL_AGENT": s.agents.email_agent,
                            "CALENDAR_AGENT": s.agents.calendar_agent,
                            "HEALTH_AGENT": s.agents.health_agent,
                            "BRIEFING_AGENT": s.agents.briefing_agent,
                            "MEAL_AGENT": s.agents.meal_agent,
                            "TRAVEL_AGENT": s.agents.travel_agent,
                            "FINANCE_AGENT": s.agents.finance_agent,
                            "STUDY_AGENT": s.agents.study_agent,
                            "METAQUEST_AGENT": s.agents.metaquest_agent,
                            "PLAN_AGENT": s.agents.plan_agent,
                            "LEAD_AGENT": s.agents.lead_agent,
                            "LEGAL_AGENT": s.agents.legal_agent,
                            "MARKETING_AGENT": s.agents.marketing_agent,
                            "SELF_EVAL_AGENT": s.agents.self_eval_agent,
                            "SQL_AGENT": s.agents.sql_agent,
                            "NEWS_AGENT": s.agents.news_agent,
                            "REPLICA_AGENT": s.agents.replica_agent,
                            "YOUTUBE_AGENT": s.agents.youtube_agent,
                            "BACKTEST_AGENT": s.agents.backtest_agent,
                        },
                    }
                )
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.put("/api/admin/config")
        async def admin_config_put(req: Request):
            """Update config from admin panel."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from config.settings import save_api_config

                body = await req.json()
                # Convert frontend format to save_api_config format
                cfg = {}
                if "llm" in body:
                    llm = body["llm"]
                    if "geminiApiKeys" in llm:
                        cfg["gemini_api_key"] = (
                            llm["geminiApiKeys"].split(",")[0].strip()
                            if llm["geminiApiKeys"]
                            else ""
                        )
                    if "nvidiaApiKey" in llm and llm["nvidiaApiKey"]:
                        cfg["nvidia_api_key"] = llm["nvidiaApiKey"]
                    if "nvidiaEnabled" in llm:
                        cfg["nvidia_nim_enabled"] = llm["nvidiaEnabled"]
                    if "openrouterApiKeys" in llm:
                        cfg["openrouter_api_keys"] = [
                            k.strip()
                            for k in llm["openrouterApiKeys"].split(",")
                            if k.strip()
                        ]
                    if "ollamaEnabled" in llm:
                        cfg["ollama_enabled"] = llm["ollamaEnabled"]
                    if "routerMode" in llm:
                        cfg["router_mode"] = llm["routerMode"]
                    if "liveModel" in llm and llm["liveModel"]:
                        cfg["live_model"] = llm["liveModel"]
                    if "geminiDailyCallBudget" in llm:
                        cfg["gemini_daily_call_budget"] = llm["geminiDailyCallBudget"]
                if "ui" in body:
                    cfg["ui"] = body["ui"]
                if "trading" in body:
                    cfg["trading"] = body["trading"]
                if "hermes" in body:
                    cfg["hermes"] = body["hermes"]
                if "agentGates" in body:
                    cfg["agentGates"] = body["agentGates"]
                if "env" in body:
                    cfg["env"] = body["env"]
                if "logLevel" in body:
                    cfg["logLevel"] = body["logLevel"]
                if cfg:
                    save_api_config(cfg)
                return JSONResponse({"ok": True})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/admin/agents/{name}/gate")
        async def admin_agent_gate(req: Request, name: str):
            """Toggle agent gate (reads from env, returns current state)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            # Agent gates are controlled via env vars (JARVIS_{NAME}_AGENT)
            # This endpoint just reports current state; actual toggle requires restart
            env_key = f"JARVIS_{name.upper()}_AGENT"
            current = os.environ.get(env_key, "").lower() in ("1", "true", "yes", "on")
            return JSONResponse(
                {
                    "ok": True,
                    "enabled": current,
                    "note": "Agent gates require restart to change",
                }
            )

        # ── Usage & Budget API (Cloud LLM Token Tracking) ────────────────────────
        @app.get("/api/usage/summary")
        async def usage_summary(req: Request):
            """Get token usage summary across all providers."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from orchestrator.model_router.budget import BudgetManager
                from orchestrator.model_router.usage import UsageJournal

                usage = UsageJournal()
                budget = BudgetManager(usage_journal=usage)
                return JSONResponse(budget.get_all_status())
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/usage/detail")
        async def usage_detail(
            req: Request,
            provider: str = "",
            days: int = 7,
        ):
            """Get detailed token usage by provider and date."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                import json
                from datetime import date, timedelta
                from pathlib import Path
                import os

                _home = os.environ.get("JARVIS_HOME", "").strip()
                base = Path(_home).expanduser().resolve() if _home else BASE_DIR
                usage_path = base / "memory" / "llm_usage.jsonl"

                if not usage_path.exists():
                    return JSONResponse({"data": [], "summary": {}})

                cutoff = date.today() - timedelta(days=days)
                results = []
                summary = {}

                for line in usage_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue

                    # Filter by provider
                    if provider and rec.get("provider") != provider:
                        continue

                    # Filter by date
                    ts = rec.get("ts", "")
                    try:
                        rec_date = date.fromisoformat(ts[:10])
                        if rec_date < cutoff:
                            continue
                    except Exception:
                        continue

                    prov = rec.get("provider", "unknown")
                    tokens = rec.get("total_tokens", 0) or rec.get("in_tokens", 0) + rec.get("out_tokens", 0)

                    results.append({
                        "timestamp": ts,
                        "provider": prov,
                        "model": rec.get("model", ""),
                        "task": rec.get("task", ""),
                        "ok": rec.get("ok", False),
                        "latency_s": rec.get("latency_s", 0),
                        "prompt_tokens": rec.get("in_tokens", 0),
                        "completion_tokens": rec.get("out_tokens", 0),
                        "total_tokens": tokens,
                        "cost_usd": rec.get("cost", 0),
                    })

                    if prov not in summary:
                        summary[prov] = {"requests": 0, "total_tokens": 0, "cost_usd": 0.0}
                    summary[prov]["requests"] += 1
                    summary[prov]["total_tokens"] += tokens
                    summary[prov]["cost_usd"] += rec.get("cost", 0.0)

                return JSONResponse({"data": results, "summary": summary})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/usage/budget")
        async def usage_budget(req: Request):
            """Get current budget status and limits for all providers."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from orchestrator.model_router.budget import BudgetManager
                from orchestrator.model_router.usage import UsageJournal

                usage = UsageJournal()
                budget = BudgetManager(usage_journal=usage)
                return JSONResponse({
                    "providers": budget.get_all_status(),
                    "config": budget._config,
                })
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/usage/budget")
        async def usage_budget_update(req: Request):
            """Update budget limits for providers."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from orchestrator.model_router.budget import BudgetManager
                from orchestrator.model_router.usage import UsageJournal

                body = await req.json()
                usage = UsageJournal()
                budget = BudgetManager(usage_journal=usage)

                # Update daily limits
                if "daily_limits" in body:
                    for prov, limit in body["daily_limits"].items():
                        budget._config.setdefault("daily_limits", {})[prov] = int(limit)

                # Update monthly limits
                if "monthly_limits" in body:
                    for prov, limit in body["monthly_limits"].items():
                        budget._config.setdefault("monthly_limits", {})[prov] = int(limit)

                # Update per-request cap
                if "per_request_cap" in body:
                    budget._config["per_request_cap"] = int(body["per_request_cap"])

                # Update alert thresholds
                if "alert_thresholds" in body:
                    budget._config["alert_thresholds"] = body["alert_thresholds"]

                # Update hard block threshold
                if "hard_block_at" in body:
                    budget._config["hard_block_at"] = float(body["hard_block_at"])

                # Update exclude_local
                if "exclude_local" in body:
                    budget._config["exclude_local"] = bool(body["exclude_local"])

                budget.save()
                return JSONResponse({"ok": True, "config": budget._config})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/usage/router-status")
        async def usage_router_status(req: Request):
            """Get the ModelRouter status including model chain and health."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from orchestrator.model_router.router import ModelRouter
                from orchestrator.model_router.usage import UsageJournal
                from orchestrator.model_router.budget import BudgetManager

                router = ModelRouter(
                    usage=UsageJournal(),
                    budget=BudgetManager(),
                )
                return JSONResponse(router.status())
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/model_router/freellmapi_health")
        async def freellmapi_health(req: Request):
            """FreeLLMAPI gateway diagnostics — connection, model count, errors.
            Never returns the API key."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from orchestrator.model_router.router import ModelRouter
                from orchestrator.model_router.usage import UsageJournal
                from orchestrator.model_router.budget import BudgetManager

                router = ModelRouter(
                    usage=UsageJournal(),
                    budget=BudgetManager(),
                )
                return JSONResponse(router.get_freellmapi_gateway_status())
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/model_router/test_connection")
        async def test_connection(req: Request):
            """Test a specific provider by running its probe. Returns (ok, reason)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
                provider = (body.get("provider") or "").strip().lower()
                from orchestrator.model_router.router import ModelRouter
                from orchestrator.model_router.usage import UsageJournal
                from orchestrator.model_router.budget import BudgetManager

                router = ModelRouter(
                    usage=UsageJournal(),
                    budget=BudgetManager(),
                )
                adapter = router.adapters.get(provider)
                if adapter is None:
                    return JSONResponse({
                        "ok": False,
                        "reason": f"unknown provider: {provider!r}",
                    })
                ok, reason = adapter.probe()
                return JSONResponse({"ok": ok, "reason": reason})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/model_router/providers")
        async def list_providers(req: Request):
            """List all configured providers with their health status."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from orchestrator.model_router.router import ModelRouter
                from orchestrator.model_router.usage import UsageJournal
                from orchestrator.model_router.budget import BudgetManager

                router = ModelRouter(
                    usage=UsageJournal(),
                    budget=BudgetManager(),
                )
                # Run probes for each known provider to get fresh status
                providers_info = {}
                for pname in sorted(router.adapters.keys()):
                    adapter = router.adapters[pname]
                    configured = adapter.is_configured()
                    ok, reason = (False, "not probed")
                    if configured:
                        ok, reason = adapter.probe()
                    providers_info[pname] = {
                        "configured": configured,
                        "healthy": ok,
                        "reason": reason,
                    }
                return JSONResponse(providers_info)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/model_router/set_task_pref")
        async def set_task_pref(req: Request):
            """Persist a task->provider preference into config/api_keys.json."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                import json as _json
                body = await req.json()
                task_value = str(body.get("task") or "").strip()
                provider = str(body.get("provider") or "").strip()
                if not task_value or not provider:
                    return JSONResponse({"error": "task and provider required"}, status_code=400)
                cfg_path = Path(__file__).resolve().parents[2] / "config" / "api_keys.json"
                try:
                    cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                except Exception:
                    cfg = {}
                prefs = cfg.setdefault("model_task_prefs", {})
                prefs[task_value] = provider
                cfg_path.write_text(_json.dumps(cfg, indent=4), encoding="utf-8")
                return JSONResponse({"ok": True})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/model_router/task_prefs")
        async def get_task_prefs(req: Request):
            """Return current task->provider preferences."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                import json as _json
                cfg_path = Path(__file__).resolve().parents[2] / "config" / "api_keys.json"
                try:
                    cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                except Exception:
                    cfg = {}
                return JSONResponse(cfg.get("model_task_prefs", {}))
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # ── Conversation Journal / Replay API ────────────────────────────────
        @app.get("/api/journal")
        async def journal_get(
            req: Request,
            limit: int = 50,
            before: str = "",
            agent: str = "",
            status: str = "",
        ):
            """Get conversation journal entries for replay."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from memory.conversation_journal import _iter_journal
                from datetime import datetime

                recs = _iter_journal()
                # Filter
                if before:
                    try:
                        cutoff = datetime.fromisoformat(before)
                        recs = [
                            r
                            for r in recs
                            if datetime.fromisoformat(r.get("ts", "")) < cutoff
                        ]
                    except Exception:
                        pass
                # Apply limit (newest first)
                recs = recs[-limit:]
                # Transform to JournalEntry format
                entries = []
                for i, r in enumerate(recs):
                    entries.append(
                        {
                            "turnId": f"{r.get('ts', '')}-{i}",
                            "timestamp": r.get("ts", ""),
                            "userText": r.get("text", "")
                            if r.get("speaker") == "user"
                            else "",
                            "agent": "jarvis",
                            "status": "COMPLETED",
                            "latencyMs": 0,
                            "tokensUsed": 0,
                            "traceId": None,
                        }
                    )
                return JSONResponse(entries)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/trace/{trace_id}")
        async def trace_get(req: Request, trace_id: str):
            """Get trace details for a specific trace ID."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            # For now, return mock trace data - integrate with actual tracing later
            return JSONResponse(
                {
                    "traceId": trace_id,
                    "spans": [],
                }
            )

        @app.post("/api/orchestrator/replay")
        async def orchestrator_replay(req: Request):
            """Replay an orchestrator turn."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            turn_id = body.get("turn_id")
            # Implement replay logic here
            return JSONResponse({"ok": True, "turn_id": turn_id})

        # ── Metrics WebSocket ────────────────────────────────────────────────
        @app.websocket("/metrics/ws")
        async def metrics_ws(websocket: WebSocket, token: str = ""):
            """WebSocket for real-time metrics streaming."""
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            try:
                while True:
                    # Send metrics every second
                    import psutil

                    cpu = psutil.cpu_percent(interval=1)
                    mem = psutil.virtual_memory().percent
                    disk = psutil.disk_usage("/").percent
                    await websocket.send_json(
                        {
                            "type": "metrics",
                            "payload": {
                                "cpu": {"value": cpu, "unit": "%"},
                                "memory": {"value": mem, "unit": "%"},
                                "disk": {"value": disk, "unit": "%"},
                            },
                        }
                    )
            except WebSocketDisconnect:
                pass
            except Exception:
                pass

        # ── Backtest API ─────────────────────────────────────────────────────
        @app.get("/api/backtest/jobs")
        async def backtest_jobs(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from orchestrator.agents.backtest_agent import list_jobs

                jobs = list_jobs()
                return JSONResponse(jobs)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/backtest/report/{job_id}")
        async def backtest_report(req: Request, job_id: str):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from orchestrator.agents.backtest_agent import get_report

                report = get_report(job_id)
                return JSONResponse({"report": report})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # ── Trading API ──────────────────────────────────────────────────────
        @app.get("/api/trading/state")
        async def trading_state(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from plugins.binance_trader import get_paper_state

                state = get_paper_state()
                return JSONResponse(state)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/trading/positions")
        async def trading_positions(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from plugins.binance_trader import get_paper_positions

                positions = get_paper_positions()
                return JSONResponse(positions)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._phone_mic_on = True  # polled by main.py (ducking)
            if self._phone_mic_callback:
                try:
                    self._phone_mic_callback(True)
                except Exception:
                    pass
            asyncio.create_task(
                self.broadcast({"type": "sys", "text": "Phone microphone live."})
            )
            try:
                while True:
                    data = await websocket.receive_bytes()
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                self._phone_mic_on = False
                if self._phone_mic_callback:
                    try:
                        self._phone_mic_callback(False)
                    except Exception:
                        pass
                asyncio.create_task(
                    self.broadcast({"type": "sys", "text": "Phone microphone stopped."})
                )

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name  # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
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
                                    {
                                        "error": f"File too large (max {MAX_UPLOAD_MB} MB)"
                                    },
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": str(exc)}, status_code=500)

                asyncio.create_task(
                    self.broadcast(
                        {
                            "type": "file_received",
                            "name": dest.name,
                            "size": size,
                            "saved_to": str(self._uploads_dir),
                        }
                    )
                )
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
                return FileResponse(str(_LWC_FILE), media_type="application/javascript")
            return JSONResponse(
                {"error": "chart library not vendored"}, status_code=404
            )

        @app.get("/api/chart-setup")
        async def chart_setup(
            req: Request, symbol: str = "BTC-USD", method: str = "swing"
        ):
            """Full live-chart payload: OHLCV + EMA/trail series + the trade
            plan (entry/stop/targets/levels) for one symbol."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from plugins.trading_strategy import (
                _trade_plan_dict,
                _method,
                canonical_symbol,
            )

            try:
                plan = _trade_plan_dict(
                    canonical_symbol(symbol), _method({"method": method}), None, None
                )
            except Exception as e:
                return JSONResponse(
                    {"error": f"Data unavailable for {symbol}: {e}"}, status_code=502
                )
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
            if not tok or tok not in self._tokens:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = re.sub(r"[/\\]", "", filename)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = "", since: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._clients.add(websocket)
            self._loop = asyncio.get_running_loop()
            # Resume protocol: the client tracks (boot_id, seq) and reconnects
            # with ?since=<last seq>. Same boot → replay only what it missed;
            # a `since` from another (older) boot, or a client from the future,
            # falls back to the standard recent-history replay. Every connect
            # also gets a boot frame so the client can detect a server restart.
            try:
                _since = max(0, int(since or 0))
            except ValueError:
                _since = 0
            if _since and (_since >= self._ws_seq or _since < self._ws_seq - 300):
                _since = 0  # seq space unknown to this boot — full replay
            try:
                await websocket.send_json(
                    {"type": "boot", "boot_id": self._boot_id, "seq": self._ws_seq}
                )
            except Exception:
                pass
            for entry in self._history[-50:]:
                if _since and entry.get("seq", 0) <= _since:
                    continue
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    data = await websocket.receive_json()
                    if data.get("type") == "command":
                        enc = data.get("enc", "")
                        t = (
                            self._decrypt(tok, enc)
                            if enc
                            else (data.get("text") or "").strip()
                        )
                        if t:
                            # RIO chat attaches its conversation id so the
                            # assistant journals this turn into the right
                            # conversation (tuple form; plain text otherwise).
                            conv = str(data.get("conv") or "").strip()[:64]
                            # Duplicate-event guard (core/dedup.py): a WS send
                            # that races its own HTTP fallback, or a client
                            # retry that reuses the same message id, must not
                            # become a second model turn. `mid` is the client's
                            # per-message id; without one a short content hash
                            # scoped to this channel covers double-POSTs.
                            mid = str(data.get("mid") or "").strip()[:128]
                            if not _dedup_acquire("command", mid, t):
                                print(
                                    f"[Dashboard] duplicate command dropped "
                                    f"(ws, mid={mid[:24] or '-'})"
                                )
                                continue
                            await self._command_queue.put((t, conv) if conv else t)
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        # SPA fallback: serve index.html for all non-API, non-static routes
        # This MUST come AFTER all API routes are registered (registered last)
        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            # Skip API routes, WS routes, static assets, and known dashboard routes
            if (
                full_path.startswith("api/")
                or full_path.startswith("ws")
                or full_path.startswith("static/")
                or full_path.startswith("assets/")
                or full_path in ("login", "chart", "auto-login")
                or full_path.startswith("static/icons/")
                or full_path.startswith("uploads/")
            ):
                return JSONResponse({"error": "Not found"}, status_code=404)

            # Serve the Svelte index.html (app.html is the old template, we use the new build)
            index_path = STATIC_DIR / "index.html"
            if index_path.exists():
                return FileResponse(str(index_path), headers=_NO_CACHE)
            # Fallback to old app.html if new build not found
            html = self._app_html.replace("__IP__", self._ip).replace(
                "__PORT__", str(PORT)
            )
            return HTMLResponse(html, headers=_NO_CACHE)

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    async def _serve_alias(self) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        ssl_key = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT + 1)
        cfg = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=PORT + 1,
            log_level="warning",
            ssl_keyfile=str(ssl_key),
            ssl_certfile=str(ssl_cert),
        )
        print(
            f"[Dashboard] Manual entry:  {self._ip}:{PORT + 1}  (type in browser, accept cert once)"
        )
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        DashboardServer.live = self  # for same-process consumers
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print(
                "[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography"
            )
            return

        # Firewall setup runs in a thread — uvicorn starts immediately,
        # no waiting for UAC dialogs or subprocess timeouts.
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT)

        # Generate the TLS pair on first run so no private key ships in the repo.
        _ensure_certs()

        use_ssl = self._ssl_enabled()
        ssl_key = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"

        if use_ssl:
            asyncio.create_task(self._serve_alias())

        cfg = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=PORT,
            log_level="warning",
            **(
                {"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)}
                if use_ssl
                else {}
            ),
        )

        proto = "https" if use_ssl else "http"
        print(f"[Dashboard] {proto}://{self._ip}:{PORT}")
        print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")
        await uvicorn.Server(cfg).serve()
