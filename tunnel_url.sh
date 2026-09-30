#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# tunnel_url.sh — report (and optionally save) the current public webhook URL
#
# The tunnel uses the account's free static domain (pinned in config/ngrok.yml),
# so the URL is stable across restarts AND reboots. This helper still exists so
# consumers never hardcode the domain, and to re-save the webhook file.
#
# Usage:
#   ./tunnel_url.sh            print  https://<sub>.ngrok-free.dev/api/tradingview
#   ./tunnel_url.sh --save     also write it to memory/trading/tv_webhook_url.txt
#
# Called by the systemd user unit (ExecStartPost --save) after every tunnel
# (re)start, so the file always holds the URL TradingView should be pointed at.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# Poll patiently — the ngrok session handshake can take 20s+ on a cold
# start (and the API answers with an empty tunnel list until it is ONLINE)
URL=""
for _ in $(seq 1 60); do
    URL="$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null |
        .venv/bin/python -c 'import json,sys
try:
    d = json.load(sys.stdin)
    ts = [t for t in d.get("tunnels", [])
          if t.get("proto") in ("http", "https")]
    print(ts[0]["public_url"].rstrip("/") if ts else "")
except Exception:
    pass' 2>/dev/null || true)"
    [ -n "$URL" ] && break
    sleep 0.5
done

if [ -z "$URL" ]; then
    echo "[tunnel_url] tunnel not reachable yet (is ngrok running?)" >&2
    exit 1
fi

echo "${URL}/api/tradingview"

if [ "${1:-}" = "--save" ]; then
    printf '%s/api/tradingview\n' "$URL" > memory/trading/tv_webhook_url.txt
fi
