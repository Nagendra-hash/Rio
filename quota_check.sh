#!/usr/bin/env bash
# Verifies whether the Gemini free-tier daily quota has reset.
# Gemini counters reset at LOCAL midnight and at midnight Pacific Time —
# the systemd timer (systemd/jarvis-quota-check.timer) fires 30 min after
# each. Results append to /tmp/gemini_quota_check.log.
cd "$(dirname "$0")"

LOG=/tmp/gemini_quota_check.log
ts() { date '+%Y-%m-%d %H:%M:%S'; }

RES=$(.venv/bin/python - <<'PY'
import json
from google import genai
key = json.load(open('config/api_keys.json'))['gemini_api_key']
try:
    c = genai.Client(api_key=key)
    c.models.generate_content(model='gemini-flash-latest', contents='ping')
    print('RESET')
except Exception as e:
    s = str(e)
    if '429' in s or 'RESOURCE_EXHAUSTED' in s.upper():
        print('STILL_EXHAUSTED')
    else:
        print('OTHER:' + type(e).__name__ + ':' + s[:100])
PY
)

case "$RES" in
  RESET)
    echo "[$(ts)] ✅ Gemini quota RESET — primary LLM live again (failover idle)" >> "$LOG" ;;
  STILL_EXHAUSTED)
    echo "[$(ts)] ⏳ Gemini still quota-exhausted — OpenRouter failover remains active" >> "$LOG" ;;
  *)
    echo "[$(ts)] ⚠️  quota check unexpected: $RES" >> "$LOG" ;;
esac

# ── Flight-URL regression (tests/test_flight_url.py) ─────────────────────────
# Guards against the tfs= blob / hardcoded-route bug ever returning to the
# flight_finder URL builder (2026-09-21 incident: every search silently loaded
# Istanbul→London). Offline tier only — no browser, no extra API spend, so it
# rides along free inside this twice-daily timer. A failure is logged but must
# NOT fail the quota check's own exit status.
REG_LOG=$(.venv/bin/python tests/test_flight_url.py 2>&1); REG_RC=$?
case $REG_RC in
  0) echo "[$(ts)] ✅ flight URL regression clean" >> "$LOG" ;;
  1) echo "[$(ts)] ❌ flight URL REGRESSION — tfs/hardcoded-route bug is back:" >> "$LOG"
     echo "$REG_LOG" | sed 's/^/    /' >> "$LOG" ;;
  *) echo "[$(ts)] ⚠️  flight URL test could not run (rc=$REG_RC):" >> "$LOG"
     echo "$REG_LOG" | tail -3 | sed 's/^/    /' >> "$LOG" ;;
esac
