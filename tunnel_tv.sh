#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# tunnel_tv.sh — expose the JARVIS dashboard to TradingView via ngrok
#
# TradingView alerts fire from TradingView's cloud, which cannot reach a
# 192.168.x.x LAN address. This tunnel gives them a public https URL that
# forwards to the local dashboard (port 8000) — the /api/tradingview webhook
# then reaches JARVIS exactly like the local test did.
#
# Usage:    ./tunnel_tv.sh
# Setup:    1. free account  → https://dashboard.ngrok.com/signup
#           2. ~/.local/bin/ngrok config add-authtoken <YOUR_TOKEN>
#           3. ./tunnel_tv.sh  →  copy the https://…ngrok-free.app URL
#           4. TradingView alert → Webhook URL:  <that URL>/api/tradingview
#
# Env overrides:
#   NGROK_BIN       path to the ngrok binary     (default ~/.local/bin/ngrok)
#   NGROK_CONFIG    path to this tunnel config   (default config/ngrok.yml)
#   NGROK_AUTHTOKEN authtoken instead of the saved one
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

NGROK_BIN="${NGROK_BIN:-$HOME/.local/bin/ngrok}"
NGROK_CONFIG="${NGROK_CONFIG:-config/ngrok.yml}"
PORT=8000   # must match dashboard/server.py

# Bootstrap ngrok (user-local install, no sudo needed)
if [ ! -x "$NGROK_BIN" ]; then
    echo "[tunnel] ngrok not found — installing to ~/.local/bin …"
    mkdir -p "$HOME/.local/bin"
    curl -sSL -o /tmp/ngrok.tgz \
        https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz
    tar -xzf /tmp/ngrok.tgz -C "$HOME/.local/bin"
fi

# The free tier needs an authtoken — saved in ngrok's own config, this file,
# or passed via NGROK_AUTHTOKEN
EXTRA=()
if [ -n "${NGROK_AUTHTOKEN:-}" ]; then
    EXTRA+=(--authtoken "$NGROK_AUTHTOKEN")
elif ! grep -qs "authtoken:" "$NGROK_CONFIG" "$HOME/.config/ngrok/ngrok.yml" 2>/dev/null; then
    echo "[tunnel] ✗ no ngrok authtoken found."
    echo "    1. Sign up free: https://dashboard.ngrok.com/signup"
    echo "    2. $NGROK_BIN config add-authtoken <YOUR_TOKEN>"
    echo "       (or: NGROK_AUTHTOKEN=<YOUR_TOKEN> ./tunnel_tv.sh)"
    exit 1
fi

# Dashboard up? Probe plain HTTP and HTTPS (8000 serves TLS when
# config/certs/ exists — see make_certs.sh). Warn, don't block.
if ! curl -sk -o /dev/null "http://127.0.0.1:${PORT}/chart" \
   && ! curl -sk -o /dev/null "https://127.0.0.1:${PORT}/chart"; then
    echo "[tunnel] ⚠ nothing answering on port ${PORT} — start JARVIS (./run.sh)"
    echo "         so the dashboard is live before TradingView sends alerts."
fi

echo "[tunnel] starting ngrok tunnel 'tradingview' (config: ${NGROK_CONFIG})"
echo "[tunnel] static domain pinned in config/ngrok.yml — the public URL never changes."
echo "[tunnel] TradingView alert webhook (already saved):  $(cat memory/trading/tv_webhook_url.txt 2>/dev/null || echo '<URL>/api/tradingview')"
echo "[tunnel] secret must match dashboard_auth_token in config/api_keys.json"
echo "[tunnel] request inspector: http://localhost:4040   (Ctrl-C to stop)"
# Load BOTH configs: the user config holds the authtoken, the project config
# defines the tunnel. (A lone --config replaces the default config entirely,
# which is why the token was silently missing on first run — ERR_NGROK_4018.)
exec "$NGROK_BIN" start tradingview \
    --config="$HOME/.config/ngrok/ngrok.yml" \
    --config="$NGROK_CONFIG" ${EXTRA[@]+"${EXTRA[@]}"}
