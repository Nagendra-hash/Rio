"""
hermes_health.py — live connectivity probe for the Hermes bridge.

Answers "is Hermes connected?" with *measurements*, not assumptions. Five
checks, run in parallel so the spoken answer lands fast even when a probe has
to time out:

  1. gateway   — systemd user service state (pidfile fallback on non-systemd)
  2. bridge    — the hermes CLI itself (version probe = binary + venv healthy)
  3. telegram  — Bot API getMe (proves token + network in one request), plus a
                 scan of the gateway journal for Telegram errors in the last
                 15 minutes (catches the DNS-outage failure mode: API fine but
                 the gateway's polling loop down)
  4. llm       — OpenRouter key endpoint (auth + reachability, no tokens burnt)
  5. fallback  — Gemini models endpoint, only when fallback_providers is set

An optional deep='true' runs one real `hermes -z` agent turn — the only probe
that exercises the full loop (CLI → agent → model → answer). Slow (~30 s), so
off by default.

Every probe owns its hard timeout and swallows its own exceptions: a health
check must never hang the audio loop or crash the tool dispatcher.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

_HOME        = "/home/nagendra"   # same convention as actions/hermes_task.py
_ENV_FILE    = Path(_HOME) / ".hermes" / ".env"
_CONFIG_FILE = Path(_HOME) / ".hermes" / "config.yaml"
_PID_FILE    = Path(_HOME) / ".hermes" / "gateway.pid"
_HERMES      = shutil.which("hermes") or f"{_HOME}/.local/bin/hermes"

_HTTP_TIMEOUT   = 6    # seconds per HTTP probe
_CMD_TIMEOUT    = 6    # seconds per subprocess probe
_DEEP_TIMEOUT   = 120  # seconds for the full agent turn (deep=True)
_JOURNAL_WINDOW = "15 min"

# Gold API updatedAt older than this = the feed is serving a frozen snapshot
# even though HTTP says 200 (the silent failure mode behind stale prices).
_PRICE_FRESHNESS_MAX_S = 15 * 60


def _env() -> dict:
    """Env for subprocess probes: HOME/PATH like hermes_task.py, plus the
    XDG_RUNTIME_DIR that `systemctl --user` needs when inherited from a
    context without it."""
    env = dict(os.environ)
    env.setdefault("HOME", _HOME)
    env.setdefault("PATH", f"{_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin")
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def _read_env_var(name: str) -> str | None:
    try:
        for line in _ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _http_get_json(url: str, headers: dict | None = None) -> tuple[bool, str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            # Full body: the Gemini /models list is ~50 KB — a fixed small read
            # cap truncates it mid-JSON and poisons the parse.
            body = r.read(2_000_000).decode("utf-8", errors="ignore")
        data = json.loads(body) if body.strip().startswith(("{", "[")) else {}
        return True, data
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:                     # DNS, timeout, refused…
        return False, str(e)[:80] or type(e).__name__


# ---------------------------------------------------------------- probes ----

def _probe_gateway() -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", "hermes-gateway"],
            capture_output=True, text=True, timeout=_CMD_TIMEOUT, env=_env(),
        )
        state = (proc.stdout or "").strip() or "unknown"
        if state == "active":
            since = ""
            try:
                p2 = subprocess.run(
                    ["systemctl", "--user", "show", "hermes-gateway",
                     "--property=ActiveEnterTimestamp", "--value"],
                    capture_output=True, text=True, timeout=_CMD_TIMEOUT, env=_env(),
                )
                since = (p2.stdout or "").strip()
            except Exception:
                pass
            return True, f"running (since {since})" if since else "running"
        if state in ("inactive", "failed"):
            return False, f"service {state}"
        return _probe_gateway_pidfile()
    except FileNotFoundError:
        return _probe_gateway_pidfile()
    except Exception as e:
        return _probe_gateway_pidfile()


def _probe_gateway_pidfile() -> tuple[bool, str]:
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True, f"running (pid {pid}, pidfile)"
    except Exception:
        return False, "gateway not running (no live pidfile)"


def _probe_bridge() -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [_HERMES, "--version"], capture_output=True, text=True,
            timeout=_CMD_TIMEOUT, env=_env(),
        )
        first = (proc.stdout or proc.stderr or "").strip().splitlines()
        return (proc.returncode == 0 and bool(first),
                first[0][:60] if first else f"exit {proc.returncode}")
    except FileNotFoundError:
        return False, "hermes CLI not found"
    except subprocess.TimeoutExpired:
        return False, "hermes CLI timed out"
    except Exception as e:
        return False, str(e)[:60]


def _probe_telegram() -> tuple[bool, str]:
    token = _read_env_var("TELEGRAM_BOT_TOKEN")
    if not token:
        return False, "no TELEGRAM_BOT_TOKEN in ~/.hermes/.env"
    ok, data = _http_get_json(f"https://api.telegram.org/bot{token}/getMe")
    if not ok:
        return False, f"Bot API unreachable: {data}"
    if not (isinstance(data, dict) and data.get("ok")):
        return False, f"bot token rejected: {data}"
    username = (data.get("result") or {}).get("username", "?")
    # API reachable ≠ gateway polling loop healthy. Check the journal for
    # fresh Telegram errors (tonight's DNS outage looked exactly like this).
    errs = _recent_gateway_telegram_errors()
    if errs:
        return False, f"@{username} reachable, but gateway logged {errs} Telegram error(s) in {_JOURNAL_WINDOW}"
    return True, f"@{username} reachable, no gateway errors in {_JOURNAL_WINDOW}"


def _recent_gateway_telegram_errors() -> int:
    try:
        proc = subprocess.run(
            ["journalctl", "--user", "-u", "hermes-gateway",
             "--since", f"-{_JOURNAL_WINDOW}", "--no-pager"],
            capture_output=True, text=True, timeout=_CMD_TIMEOUT + 2, env=_env(),
        )
        return sum(
            1 for line in (proc.stdout or "").splitlines()
            if "Telegram" in line and ("ERROR" in line.upper()
                                       or "failed" in line.lower()
                                       or "reconnect" in line.lower())
        )
    except Exception:
        return 0    # journal unavailable — don't fail the probe for it


def _probe_llm() -> tuple[bool, str]:
    key = _read_env_var("OPENROUTER_API_KEY")
    if not key:
        return False, "no OPENROUTER_API_KEY in ~/.hermes/.env"
    ok, data = _http_get_json("https://openrouter.ai/api/v1/key",
                              headers={"Authorization": f"Bearer {key}"})
    if not ok:
        return False, f"OpenRouter unreachable: {data}"
    return True, "OpenRouter key valid"


def _probe_fallback() -> tuple[bool, str]:
    try:
        configured = "fallback_providers:" in _CONFIG_FILE.read_text(
            encoding="utf-8", errors="ignore")
    except OSError:
        return False, "config.yaml unreadable"
    if not configured:
        return True, "no fallback configured (skip)"
    key = _read_env_var("GEMINI_API_KEY")
    if not key:
        return False, "fallback configured but no GEMINI_API_KEY"
    ok, data = _http_get_json("https://generativelanguage.googleapis.com/v1beta/models",
                              headers={"x-goog-api-key": key})
    if not ok:
        return False, f"Gemini fallback unreachable: {data}"
    n = len((data or {}).get("models", []))
    return True, f"Gemini fallback OK ({n} models)"


def _probe_price_sources() -> tuple[bool, str]:
    """Reachability + freshness of the endpoints the live-prices skill depends
    on (skills/live-prices — key-free GETs). HTTP 200 alone is not success: a
    feed can serve a frozen snapshot, so XAU updatedAt age is asserted and BTC
    is cross-checked across two independent sources (the stale-snippet mode)."""
    problems: list[str] = []
    binance_px = None
    gecko_px = None

    # 1. Binance spot ticker (crypto backbone)
    ok, data = _http_get_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
    if not ok:
        problems.append(f"Binance unreachable: {data}")
    else:
        try:
            binance_px = float(data["price"])
            if binance_px <= 0:
                problems.append("Binance returned non-positive price")
        except (KeyError, TypeError, ValueError):
            problems.append(f"Binance malformed: {str(data)[:50]}")

    # 2. Gold API (XAUUSD) — price + snapshot freshness
    ok, data = _http_get_json("https://api.gold-api.com/price/XAU")
    if not ok:
        problems.append(f"Gold API unreachable: {data}")
    else:
        try:
            xau = float(data["price"])
            updated = datetime.strptime(str(data["updatedAt"]), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age_s = (datetime.now(timezone.utc) - updated).total_seconds()
            if xau <= 0:
                problems.append("Gold API returned non-positive price")
            elif age_s > _PRICE_FRESHNESS_MAX_S:
                problems.append(f"Gold API stale ({age_s / 60:.0f} min old)")
        except (KeyError, TypeError, ValueError):
            problems.append(f"Gold API malformed: {str(data)[:50]}")

    # 3. CoinGecko (crypto fallback) + BTC cross-source consistency
    ok, data = _http_get_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")
    if not ok:
        problems.append(f"CoinGecko unreachable: {data}")
    else:
        try:
            gecko_px = float(data["bitcoin"]["usd"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"CoinGecko malformed: {str(data)[:50]}")

    if binance_px and gecko_px and abs(binance_px - gecko_px) / min(binance_px, gecko_px) > 0.05:
        problems.append(f"BTC sources disagree: Binance {binance_px:.0f} vs CoinGecko {gecko_px:.0f}")

    if problems:
        return False, "; ".join(problems)
    return True, "Binance, Gold API (fresh), CoinGecko live; BTC sources agree"


def _probe_freellmapi() -> tuple[bool, str]:
    """FreeLLMAPI router (localhost:3001) — Hermes' first fallback provider.
    A dead router silently degrades every turn to the direct chain, so it
    gets its own check: /v1/models with the configured key proves the
    service, the key, and the catalog in one request."""
    key = _read_env_var("FREELLMAPI_API_KEY")
    if not key:
        return False, "no FREELLMAPI_API_KEY in ~/.hermes/.env"
    ok, data = _http_get_json("http://127.0.0.1:3001/v1/models",
                              headers={"Authorization": f"Bearer {key}"})
    if not ok:
        return False, f"router unreachable (is freellmapi.service up?): {data}"
    n = len((data or {}).get("data", []))
    if n == 0:
        return False, "router up but catalog empty"
    return True, f"router OK ({n} models pooled)"


def _probe_deep() -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [_HERMES, "-z", "Reply with exactly: HEALTH_OK"],
            capture_output=True, text=True, timeout=_DEEP_TIMEOUT, env=_env(),
        )
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out:
            return True, f"agent turn OK: {out[:40]}"
        return False, (out or f"exit {proc.returncode}")[:80]
    except subprocess.TimeoutExpired:
        return False, "agent turn timed out"
    except Exception as e:
        return False, str(e)[:60]


# --------------------------------------------------------------- handler ----

def hermes_health(parameters: dict, player=None) -> str:
    parameters = parameters or {}
    deep = str(parameters.get("deep", "")).strip().lower() in ("true", "yes", "1")

    jobs = {
        "Gateway":   _probe_gateway,
        "Bridge CLI": _probe_bridge,
        "Telegram":  _probe_telegram,
        "LLM primary": _probe_llm,
        "LLM fallback": _probe_fallback,
        "FreeLLMAPI router": _probe_freellmapi,
        "Price sources": _probe_price_sources,
    }
    if deep:
        jobs["Agent loop (deep)"] = _probe_deep

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = dict(zip(jobs, pool.map(lambda fn: fn(), jobs.values())))

    passed = sum(1 for ok, _ in results.values() if ok)
    total  = len(results)

    lines = [f"Hermes health — {passed}/{total} checks passed:"]
    for name, (ok, detail) in results.items():
        lines.append(f"{'OK ' if ok else 'FAIL'} {name}: {detail}")

    broken = [name for name, (ok, _) in results.items() if not ok]
    if not broken:
        verdict = "Verdict: Hermes is fully connected."
    elif "Gateway" in broken and len(broken) == total - 1:
        verdict = f"Verdict: Hermes is DOWN — gateway not running. Start it with: systemctl --user start hermes-gateway"
    elif broken:
        verdict = f"Verdict: Hermes is partially connected — problem with: {', '.join(broken)}."
    lines.append(verdict)
    return "\n".join(lines)


TOOL = {
    "name": "hermes_health",
    "description": (
        "Live health check of the Hermes bridge: systemd gateway status, the "
        "hermes CLI, Telegram bot reachability (with recent gateway error "
        "scan), LLM backends (OpenRouter + configured fallback), and crypto/"
        "gold price-source reachability. Use when the user asks whether Hermes "
        "is connected/working, reports Hermes not responding, notices wrong or "
        "stale prices, or asks for a messaging/Telegram status."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "deep": {
                "type": "STRING",
                "description": "Optional: 'true' also runs one real end-to-end agent turn (~30s) to prove the full loop works."
            }
        },
        "required": []
    },
    "handler": hermes_health,
}
