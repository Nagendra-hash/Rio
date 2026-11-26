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

_HOME = Path.home()  # same convention as actions/hermes_task.py
_ENV_FILE = _HOME / ".hermes" / ".env"
_CONFIG_FILE = _HOME / ".hermes" / "config.yaml"
_PID_FILE = _HOME / ".hermes" / "gateway.pid"
# This install runs the gateway as a manual runtime (hermes gateway ensure /
# run), not as a systemd unit, so the unit's state is not the whole story.
_GATEWAY_STATE = _HOME / ".hermes" / "gateway_state.json"
_GATEWAY_LOCK = _HOME / ".hermes" / "gateway.lock"
_HERMES = shutil.which("hermes") or str(_HOME / ".local" / "bin" / "hermes")

_HTTP_TIMEOUT = 6  # seconds per HTTP probe
_CMD_TIMEOUT = 6  # seconds per subprocess probe
_DEEP_TIMEOUT = 120  # seconds for the full agent turn (deep=True)
_JOURNAL_WINDOW = "15 min"

# Gold API updatedAt older than this = the feed is serving a frozen snapshot
# even though HTTP says 200 (the silent failure mode behind stale prices).
_PRICE_FRESHNESS_MAX_S = 15 * 60


def _env() -> dict:
    """Env for subprocess probes: HOME/PATH like hermes_task.py, plus the
    XDG_RUNTIME_DIR that `systemctl --user` needs when inherited from a
    context without it."""
    env = dict(os.environ)
    env.setdefault("HOME", str(_HOME))
    env.setdefault(
        "PATH",
        os.pathsep.join(
            [
                str(_HOME / ".local" / "bin"),
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            ]
        ),
    )
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
    except Exception as e:  # DNS, timeout, refused…
        return False, str(e)[:80] or type(e).__name__


# ---------------------------------------------------------------- probes ----


def _probe_gateway() -> tuple[bool, str]:
    """systemd state first, then the gateway's own runtime.

    "service inactive" is not the same as "gateway down": a gateway started
    with `hermes gateway ensure` has no unit at all, and checking only systemd
    reported a perfectly healthy gateway as FAIL (observed 2026-11-16).
    """
    load_state, active_state, since = _systemd_gateway_state()
    if active_state == "active":
        return True, (f"running (systemd, since {since})" if since else "running (systemd)")

    runtime_ok, runtime_detail = _probe_gateway_runtime()
    if runtime_ok:
        return True, runtime_detail
    if load_state == "loaded" and active_state in ("inactive", "failed"):
        return False, f"service {active_state}; {runtime_detail}"
    if load_state == "not-found":
        return False, runtime_detail
    return False, runtime_detail


def _systemd_gateway_state() -> tuple[str, str, str]:
    """(LoadState, ActiveState, ActiveEnterTimestamp) — ("","","") on any error."""
    try:
        proc = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                "hermes-gateway",
                "--property=LoadState,ActiveState,ActiveEnterTimestamp",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=_CMD_TIMEOUT,
            env=_env(),
        )
        props = {}
        for line in (proc.stdout or "").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                props[key.strip()] = value.strip()
        return (
            props.get("LoadState", ""),
            props.get("ActiveState", ""),
            props.get("ActiveEnterTimestamp", ""),
        )
    except Exception:
        return "", "", ""


def _read_gateway_state() -> dict:
    """The gateway's own runtime record (state file, then the lock file)."""
    for path in (_GATEWAY_STATE, _GATEWAY_LOCK):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and data.get("pid"):
            return data
    return {}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except Exception:
        return False


def _pid_is_gateway(pid: int) -> bool:
    """True unless /proc proves otherwise (non-Linux stays permissive)."""
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except Exception:
        return True
    cmdline = raw.replace(b"\0", b" ").decode("utf-8", errors="ignore").lower()
    return "gateway" in cmdline and "hermes" in cmdline


def _probe_gateway_runtime() -> tuple[bool, str]:
    state = _read_gateway_state()
    if not state:
        return _probe_gateway_pidfile()
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if not pid:
        return False, "gateway state has no pid"
    if not _pid_alive(pid):
        return False, f"stale gateway state: recorded pid {pid} is gone"
    if not _pid_is_gateway(pid):
        return False, f"pid {pid} is alive but is not a Hermes gateway"

    notes = [f"running (pid {pid}, manual runtime)"]
    platforms = state.get("platforms") or {}
    telegram = (platforms.get("telegram") or {}).get("state")
    if telegram:
        notes.append(f"telegram {telegram}")
    whatsapp = (platforms.get("whatsapp") or {}).get("state")
    if whatsapp and whatsapp != "connected":
        notes.append(f"whatsapp {whatsapp}")
    return True, "; ".join(notes)


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
            [_HERMES, "--version"],
            capture_output=True,
            text=True,
            timeout=_CMD_TIMEOUT,
            env=_env(),
        )
        first = (proc.stdout or proc.stderr or "").strip().splitlines()
        return (
            proc.returncode == 0 and bool(first),
            first[0][:60] if first else f"exit {proc.returncode}",
        )
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
    # A transport flap that recovered is NOT an outage: the state machine
    # resets on the recovery line and reports the flap as a note.
    unresolved, recovered = _recent_gateway_telegram_errors()
    if unresolved:
        return False, (
            f"@{username} reachable, but the gateway has "
            f"{unresolved} unrecovered Telegram error(s) in "
            f"{_JOURNAL_WINDOW}"
        )
    if recovered:
        return True, (
            f"@{username} reachable; {recovered} transport flap(s) "
            f"recovered in {_JOURNAL_WINDOW} — polling is healthy"
        )
    return True, f"@{username} reachable, no gateway errors in {_JOURNAL_WINDOW}"


def _recent_gateway_telegram_errors() -> tuple[int, int]:
    """Return (unresolved_errors, recovered_flaps) from the gateway journal.

    Recovery-aware: a failure increments the unresolved count, while a
    recovery/healthy line resets it and records the flap. Counting every
    historical "failed" line would leave a permanent false ERROR after a
    transient DNS/transport failure that already healed.
    """
    try:
        proc = subprocess.run(
            [
                "journalctl",
                "--user",
                "-u",
                "hermes-gateway",
                "--since",
                f"-{_JOURNAL_WINDOW}",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=_CMD_TIMEOUT + 2,
            env=_env(),
        )
        unresolved = 0
        recovered = 0
        for line in (proc.stdout or "").splitlines():
            if "Telegram" not in line:
                continue
            low = line.lower()
            # Check recovery first: "reconnect successful" and "connected
            # (polling mode)" also contain failure-ish words.
            is_recovery = any(
                marker in low
                for marker in (
                    "connected to telegram",
                    "using sticky",
                    "recovered",
                    "recovery successful",
                    "recovery complete",
                    "polling again",
                    "polling is healthy",
                    "reconnect successful",
                    "reconnected",
                    "back online",
                )
            )
            if is_recovery:
                if unresolved:
                    recovered += unresolved
                    unresolved = 0
                continue
            is_failure = any(
                marker in low
                for marker in (
                    "error",
                    "failed",
                    "failure",
                    "reconnect",
                    "timeout",
                    "timed out",
                    "disconnected",
                    "getupdates",
                    "connecterror",
                )
            )
            if is_failure:
                unresolved += 1
        return unresolved, recovered
    except Exception:
        return 0, 0  # journal unavailable — don't fail the probe for it


def _probe_llm() -> tuple[bool, str]:
    primary, _fallbacks = _hermes_model_chain()
    prov = primary.get("provider") or ""
    model = primary.get("model") or ""
    if not prov:
        return False, "no primary model in ~/.hermes/config.yaml"
    ok, detail = _probe_provider(prov)
    label = f"{prov}"
    if model:
        label += f" ({model})"
    return (True, f"{label} — {detail}") if ok else (False, f"{label} — {detail}")


def _probe_fallback() -> tuple[bool, str]:
    _primary, fallbacks = _hermes_model_chain()
    if not fallbacks:
        return True, "no fallback configured (skip)"
    good, bad = [], []
    for fb in fallbacks:
        prov = fb.get("provider") or "?"
        ok, detail = _probe_provider(prov, fb.get("base_url") or "")
        tag = f"{prov}:{fb.get('model') or '?'}"
        (good if ok else bad).append(tag if ok else f"{tag} ({detail})")
    if not bad:
        return True, f"{len(good)} fallback(s) ready: {', '.join(good)}"
    if good:
        return True, (f"{len(good)}/{len(fallbacks)} fallback(s) ready "
                      f"({', '.join(good)}); down: {', '.join(bad)}")
    return False, f"no fallback reachable: {'; '.join(bad)}"


def _probe_price_sources() -> tuple[bool, str]:
    """Reachability + freshness of the endpoints the live-prices skill depends
    on (skills/live-prices — key-free GETs). HTTP 200 alone is not success: a
    feed can serve a frozen snapshot, so XAU updatedAt age is asserted and BTC
    is cross-checked across two independent sources (the stale-snippet mode)."""
    problems: list[str] = []
    binance_px = None
    gecko_px = None

    # 1. Binance spot ticker (crypto backbone). data-api.binance.vision is
    # Binance's official READ-ONLY market-data mirror — the same REST surface.
    # It matters here because api.binance.com is DPI-blocked by the home ISP
    # (Binance is PMLA-unregistered in India): curl/urllib hang on connect
    # while every other endpoint (Google, CoinGecko, OpenRouter) answers fine
    # — confirmed by trial on 2026-11-03, data-api.binance.vision answered
    # 4/4 while api.binance.com timed out on every attempt.
    ok, data = _http_get_json(
        "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT"
    )
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
            updated = datetime.strptime(
                str(data["updatedAt"]), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            age_s = (datetime.now(timezone.utc) - updated).total_seconds()
            if xau <= 0:
                problems.append("Gold API returned non-positive price")
            elif age_s > _PRICE_FRESHNESS_MAX_S:
                problems.append(f"Gold API stale ({age_s / 60:.0f} min old)")
        except (KeyError, TypeError, ValueError):
            problems.append(f"Gold API malformed: {str(data)[:50]}")

    # 3. CoinGecko (crypto fallback) + BTC cross-source consistency
    ok, data = _http_get_json(
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
    )
    if not ok:
        problems.append(f"CoinGecko unreachable: {data}")
    else:
        try:
            gecko_px = float(data["bitcoin"]["usd"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"CoinGecko malformed: {str(data)[:50]}")

    if (
        binance_px
        and gecko_px
        and abs(binance_px - gecko_px) / min(binance_px, gecko_px) > 0.05
    ):
        problems.append(
            f"BTC sources disagree: Binance {binance_px:.0f} vs CoinGecko {gecko_px:.0f}"
        )

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
    ok, data = _http_get_json(
        "http://127.0.0.1:3001/v1/models", headers={"Authorization": f"Bearer {key}"}
    )
    if not ok:
        return False, f"router unreachable (is freellmapi.service up?): {data}"
    n = len((data or {}).get("data", []))
    if n == 0:
        return False, "router up but catalog empty"
    return True, f"router OK ({n} models pooled)"




def _hermes_model_chain():
    try:
        import yaml
        cfg = yaml.safe_load(_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}, []
    model = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    primary = {
        "provider": str((model or {}).get("provider") or "").strip(),
        "model": str((model or {}).get("default") or (model or {}).get("name") or "").strip(),
    }
    fallbacks = []
    for entry in cfg.get("fallback_providers") or []:
        if isinstance(entry, dict):
            fallbacks.append({"provider": str(entry.get("provider") or "").strip(),
                              "model": str(entry.get("model") or "").strip(),
                              "base_url": str(entry.get("base_url") or "").strip()})
        elif isinstance(entry, str) and entry.strip():
            prov, _, mdl = entry.strip().partition(":")
            fallbacks.append({"provider": prov, "model": mdl, "base_url": ""})
    return primary, fallbacks


def _probe_provider(provider, base_url=""):
    prov = (provider or "").strip().lower()
    if prov in ("nvidia", "nim", "nvidia-nim", "build-nvidia", "nemotron"):
        key = _read_env_var("NVIDIA_API_KEY")
        if not key:
            return False, "no NVIDIA_API_KEY in ~/.hermes/.env"
        ok, data = _http_get_json(
            "https://integrate.api.nvidia.com/v1/models",
            headers={"Authorization": f"Bearer {key}"})
        if not ok:
            return False, f"NVIDIA NIM unreachable: {data}"
        n = len((data or {}).get("data", []))
        return True, f"NVIDIA NIM key valid ({n} models)"
    if prov in ("gemini", "google"):
        key = _read_env_var("GEMINI_API_KEY")
        if not key:
            return False, "no GEMINI_API_KEY in ~/.hermes/.env"
        ok, data = _http_get_json(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": key})
        if not ok:
            return False, f"Gemini unreachable: {data}"
        n = len((data or {}).get("models", []))
        return True, f"Gemini key valid ({n} models)"
    if prov in ("openrouter", "or"):
        key = _read_env_var("OPENROUTER_API_KEY")
        if not key:
            return False, "no OPENROUTER_API_KEY in ~/.hermes/.env"
        ok, data = _http_get_json(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {key}"})
        if not ok:
            return False, f"OpenRouter unreachable: {data}"
        return True, "OpenRouter key valid"
    url = (base_url or "http://localhost:11434/v1").rstrip("/") + "/models"
    ok, data = _http_get_json(url)
    if not ok:
        return False, f"{prov or 'local'} unreachable at {url}: {data}"
    n = len((data or {}).get("data", []))
    return True, f"{prov or 'local'} answering ({n} models)"


def _probe_rag():
    try:
        from core import gemini, rag
        if not callable(getattr(gemini, "embed", None)):
            return False, "core.gemini.embed missing"
        stats = rag.stats()
        docs = int(stats.get("documents") or 0)
        chunks = int(stats.get("chunks") or 0)
        if not docs:
            return True, "library empty (0 documents)"
        return True, f"{docs} documents / {chunks} chunks, embeddings OK"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:70]


def _probe_memory():
    try:
        from memory import memory_manager as mm
        entries = mm.all_entries_for_ui()
        return True, f"{len(entries)} stored facts, search available"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:70]


def _probe_agents():
    try:
        from orchestrator.autonomy import TaskStore
        tasks = TaskStore().list()
        enabled = sum(1 for t in tasks if t.get("enabled"))
        return True, f"{len(tasks)} autonomy task(s), {enabled} enabled"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:70]

def _probe_deep() -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [_HERMES, "-z", "Reply with exactly: HEALTH_OK"],
            capture_output=True,
            text=True,
            timeout=_DEEP_TIMEOUT,
            env=_env(),
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
        "Gateway": _probe_gateway,
        "Bridge CLI": _probe_bridge,
        "Telegram": _probe_telegram,
        "LLM primary": _probe_llm,
        "LLM fallback": _probe_fallback,
        "FreeLLMAPI router": _probe_freellmapi,
        "Price sources": _probe_price_sources,
        "RAG library": _probe_rag,
        "Memory": _probe_memory,
        "Agents": _probe_agents,
    }
    if deep:
        jobs["Agent loop (deep)"] = _probe_deep

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = dict(zip(jobs, pool.map(lambda fn: fn(), jobs.values())))

    passed = sum(1 for ok, _ in results.values() if ok)
    total = len(results)

    lines = [f"Hermes health — {passed}/{total} checks passed:"]
    for name, (ok, detail) in results.items():
        lines.append(f"{'OK ' if ok else 'FAIL'} {name}: {detail}")

    broken = [name for name, (ok, _) in results.items() if not ok]
    if not broken:
        verdict = "Verdict: Hermes is fully connected."
    elif "Gateway" in broken and len(broken) == total - 1:
        verdict = "Verdict: Hermes is DOWN — gateway not running. Start it with: systemctl --user start hermes-gateway"
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
                "description": "Optional: 'true' also runs one real end-to-end agent turn (~30s) to prove the full loop works.",
            }
        },
        "required": [],
    },
    "handler": hermes_health,
}
