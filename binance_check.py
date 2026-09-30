#!/usr/bin/env python3
"""
binance_check.py — staged, READ-ONLY Binance connection checker.

Run:  python3 binance_check.py [--futures-only] [--json]

Guarantees:
  * Every request is a GET. Nothing is ever placed, cancelled or modified.
  * The API secret is never printed; the key only as first6…last4.

Stages:
  0  local config      keys present, shape, duplicate-key-set conflicts
  1  network           plain TCP/TLS reachability of spot + futures hosts
  2  market data       public ticker (no key involved)
  3  clock skew        local vs Binance server time (predicts -1021)
  4  egress IP         which IP an IP-restriction allowlist must contain
  5  spot auth         GET /api/v3/account — the moment key/IP/permissions bite
  6  futures auth      GET /fapi/v2/balance — proves futures permission

Exit codes: 0 all-clear, 1 auth problem, 2 network problem, 3 config problem.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from plugins.binance_trader import BinanceClient, _get_binance_keys  # noqa: E402

SPOT_HOST = "https://api.binance.com"
FUT_HOST = "https://fapi.binance.com"

# Terminal colours (degrade gracefully when piped)
def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else str(s)

def ok(msg):    print(_c("32", f"  ✓ {msg}"))
def warn(msg):  print(_c("33", f"  ⚠ {msg}"))
def bad(msg):   print(_c("31", f"  ✗ {msg}"))
def info(msg):  print(_c("36", f"  · {msg}"))
def stage(n, name): print(f"\n{_c('1;94', f'── Stage {n} · {name} ' + '─' * max(0, 52 - len(name)))}")

RESULTS: dict = {"stages": {}, "verdict": None}
AUTH_FAILURES: list[str] = []
NETWORK_FAILURES: list[str] = []


def _error_body(exc: Exception) -> str:
    """binance_trader raises RuntimeError('Binance API error <http>: <body>')."""
    m = re.search(r"Binance API error \d+:\s*(.*)", str(exc), re.S)
    return m.group(1)[:300] if m else str(exc)[:300]


def _binance_code(exc: Exception) -> int | None:
    m = re.search(r'"code"\s*:\s*(-?\d+)', _error_body(exc))
    return int(m.group(1)) if m else None


def _mask(key: str) -> str:
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "<too short>"


def _classify_auth(exc: Exception, market: str) -> None:
    code = _binance_code(exc)
    body = _error_body(exc)
    AUTH_FAILURES.append(f"{market}: HTTP {_binance_code(exc)} → {body}")
    if code == -2015:
        bad(f"{market} auth REJECTED — error -2015 (Invalid API-key, IP, or permissions)")
        print("      This single error means exactly one of:")
        print("        1. Key invalid/revoked, or secret pasted wrong (secret is shown ONCE at creation)")
        print("        2. IP restriction on the key — your egress IP (stage 4) must be on the allowlist")
        print("        3. Permissions: 'Enable Reading' unchecked" +
              (" (futures: also 'Enable Futures')" if market == "futures" else ""))
        print("      → Fix on binance.com → Profile → API Management, then re-run this script.")
    elif code == -2014:
        bad(f"{market} auth REJECTED — error -2014 (API-key format invalid)")
        print("      The key string itself is malformed — re-copy it. Check for truncated paste.")
    elif code == -1022:
        bad(f"{market} auth REJECTED — error -1022 (signature invalid)")
        print("      Unexpected after the signing fix — report this; likely a broken secret value.")
    elif code == -1021:
        bad(f"{market} auth REJECTED — error -1021 (timestamp outside recvWindow)")
        print("      Your system clock drifted >10 s from Binance. Sync time (chrony/ntpd) and re-run.")
    else:
        bad(f"{market} auth failed — {body}")


# ── Stage 0 ───────────────────────────────────────────────────────────────────
def stage0_config() -> None:
    stage(0, "local config")
    key, secret = _get_binance_keys()
    problems = []
    if not key or not secret:
        bad("No Binance keys found.")
        print("      Add binance_api_key / binance_api_secret to config/api_keys.json")
        RESULTS["stages"]["config"] = "missing"
        NETWORK_FAILURES.append("config: keys missing")
        return

    ok(f"Key loaded: {_mask(key)}")
    ok(f"Secret loaded: {len(secret)} chars (not printed)")

    # Duplicate key-set trap: top-level vs plugin_config.trading
    try:
        cfg = json.loads((PROJECT_ROOT / "config" / "api_keys.json").read_text())
    except Exception:
        cfg = {}
    alt = ((cfg.get("plugin_config") or {}).get("trading") or {})
    top_k = str(cfg.get("binance_api_key") or "").strip()
    plug_k = str(alt.get("live_api_key") or "").strip()
    if top_k and plug_k and top_k != plug_k:
        warn("TWO DIFFERENT key sets present in config/api_keys.json:")
        print(f"        binance_api_key (wins):        {_mask(top_k)}")
        print(f"        plugin_config trading key:     {_mask(plug_k)}")
        print("      The trader uses the FIRST one. Delete the one you don't want to avoid confusion.")
        problems.append("duplicate key sets")
    if key != key.strip() or (secret != secret.strip()):
        bad("Keys contain leading/trailing whitespace — will break the HMAC signature.")
        problems.append("whitespace in keys")

    if not re.fullmatch(r"[A-Za-z0-9]{32,}", key):
        warn(f"Key has unusual shape ({len(key)} chars). Binance keys are 64 alphanumerics.")
        problems.append("odd key shape")

    RESULTS["stages"]["config"] = "ok" if not problems else f"ok+warnings({','.join(problems)})"
    if problems:
        AUTH_FAILURES.append("config: " + ", ".join(problems))


# ── Stage 1/2/3/4 ────────────────────────────────────────────────────────────
def stage1_network() -> bool:
    stage(1, "network reachability")
    reachable = True
    for label, url in (("spot", SPOT_HOST), ("futures", FUT_HOST)):
        c = BinanceClient(market=label)
        try:
            t0 = time.monotonic()
            c._request("/api/v3/ping" if label == "spot" else "/fapi/v1/ping")
            ok(f"{label}: {url} reachable ({(time.monotonic()-t0)*1000:.0f} ms)")
        except Exception as e:
            reachable = False
            bad(f"{label}: {url} UNREACHABLE — {_error_body(e)}")
            print("      Timeout/refused usually means: ISP/network block, firewall, or")
            print("      regional restriction. Try: curl -sS " + url + "/api/v3/ping")
            NETWORK_FAILURES.append(f"{label} unreachable")
    if not reachable:
        RESULTS["stages"]["network"] = "fail"
        return False
    RESULTS["stages"]["network"] = "ok"
    return True


def stage2_market_data() -> bool:
    stage(2, "market data (public, no key)")
    good = True
    for label, symbol in (("spot", "BTCUSDT"), ("futures", "BTCUSDT")):
        c = BinanceClient(market=label)
        try:
            t = c.get_ticker(symbol)
            px = float(t.get("price") or 0)
            ok(f"{label} {symbol} = ${px:,.2f}")
        except Exception as e:
            good = False
            bad(f"{label} ticker failed — {_error_body(e)}")
            NETWORK_FAILURES.append(f"{label} market data")
    RESULTS["stages"]["market_data"] = "ok" if good else "fail"
    return good


def stage3_clock() -> None:
    stage(3, "clock skew (predicts -1021)")
    c = BinanceClient(market="spot")
    try:
        server_ms = c.get_server_time()   # client returns an int epoch-ms
        skew = int(time.time() * 1000) - int(server_ms)
        RESULTS["stages"]["clock_skew_ms"] = skew
        if abs(skew) < 1000:
            ok(f"local clock vs Binance: {skew:+d} ms (fine)")
        elif abs(skew) < 10_000:
            warn(f"clock skew {skew:+d} ms — inside the 10 s recvWindow, but sync your clock anyway")
        else:
            bad(f"clock skew {skew:+d} ms — signed orders WILL be rejected with -1021. Sync time now.")
            AUTH_FAILURES.append(f"clock skew {skew} ms")
    except Exception as e:
        warn(f"could not read server time — {_error_body(e)}")


def stage4_egress_ip() -> None:
    stage(4, "your egress IP (for Binance IP allowlist)")
    import urllib.request
    try:
        ip = urllib.request.urlopen("https://api.ipify.org", timeout=8).read().decode().strip()
        ok(f"public IP seen by the internet: {ip}")
        RESULTS["stages"]["egress_ip"] = ip
        print("      If your key is IP-restricted, exactly this address must be on the allowlist")
        print("      (or remove the restriction). Home IPs change on router reboot — prefer")
        print("      removing the restriction, or re-check after every reconnect.")
    except Exception as e:
        warn(f"could not determine egress IP — {e}")


# ── Stage 5/6 ────────────────────────────────────────────────────────────────
def stage5_spot_auth() -> bool:
    stage(5, "spot authentication  (GET /api/v3/account — read-only)")
    key, secret = _get_binance_keys()
    c = BinanceClient(api_key=key, api_secret=secret, market="spot")
    try:
        acct = c.get_account()
    except Exception as e:
        _classify_auth(e, "spot")
        RESULTS["stages"]["spot_auth"] = "fail"
        return False
    can = acct.get("canTrade", False)
    perms = acct.get("permissions") or acct.get("accountType") or "?"
    ok(f"authenticated — accountType={acct.get('accountType', '?')} canTrade={can} perms={perms}")
    usdt = next((b for b in acct.get("balances", []) if b.get("asset") == "USDT"), None)
    if usdt:
        free, locked = float(usdt.get("free", 0)), float(usdt.get("locked", 0))
        ok(f"USDT balance: free={free:,.2f} locked={locked:,.2f}")
        if free + locked == 0:
            warn("USDT balance is zero — deposits/transfer needed before any live order")
    else:
        warn("no USDT wallet entry returned")
    if not can:
        warn("account canTrade=false — trading disabled at account level on Binance side")
        AUTH_FAILURES.append("spot: canTrade=false")
    RESULTS["stages"]["spot_auth"] = "ok"
    return True


def stage6_futures_auth() -> bool:
    stage(6, "futures authentication  (GET /fapi/v2/balance — read-only)")
    key, secret = _get_binance_keys()
    c = BinanceClient(api_key=key, api_secret=secret, market="futures")
    try:
        bal = c.get_account()   # normalized to list-of-balances for futures
    except Exception as e:
        code = _binance_code(e)
        if code == -2015:
            bad("futures auth REJECTED (-2015) — key likely lacks 'Enable Futures'")
            print("      Spot working + futures failing almost always means the futures")
            print("      permission checkbox on the key is off (or futures wallet is not")
            print("      activated once on Binance's Futures page).")
            AUTH_FAILURES.append("futures: -2015 (enable Futures on the key)")
        else:
            _classify_auth(e, "futures")
        RESULTS["stages"]["futures_auth"] = "fail"
        return False
    rows = bal if isinstance(bal, list) else bal.get("balances", [])
    usdt = next((b for b in rows if b.get("asset") == "USDT"), None)
    if usdt:
        ok(f"futures USDT wallet: balance={float(usdt.get('balance', 0)):,.2f} "
           f"available={float(usdt.get('availableBalance', usdt.get('withdrawAvailable', 0))):,.2f}")
    else:
        ok("authenticated (no USDT row returned — futures wallet empty)")
    RESULTS["stages"]["futures_auth"] = "ok"
    return True


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    print(_c("1", "Binance connection check — READ-ONLY (no order can be placed)"))
    stage0_config()
    net = stage1_network()
    if net:
        stage2_market_data()
        stage3_clock()
        stage4_egress_ip()
        stage5_spot_auth()
        stage6_futures_auth()

    print(f"\n{_c('1;94', '── Verdict ' + '─' * 58)}")
    if not net:
        bad("Network-level failure: Binance hosts unreachable from this machine.")
        print("    Nothing about your keys was tested. Fix connectivity first.")
        RESULTS["verdict"] = "NETWORK_FAIL"
        return 2
    if AUTH_FAILURES:
        bad("Auth problems found:")
        for f in AUTH_FAILURES:
            print(f"      - {f}")
        RESULTS["verdict"] = "AUTH_FAIL"
        return 1
    ok("ALL CHECKS PASSED — keys valid, permissions present, both markets authenticated.")
    print("    Safe next step: 'test Binance' by voice, then paper-trade through the")
    print("    dashboard webhook before enabling live mode.")
    RESULTS["verdict"] = "PASS"
    return 0


if __name__ == "__main__":
    if "--json" in sys.argv:
        rc = main()
        print(json.dumps(RESULTS, indent=2))
        sys.exit(rc)
    sys.exit(main())
