#!/usr/bin/env bash
# backup_notify.sh — send the weekly backup summary to Telegram.
# Args: <export_rc> <offsite_rc> <offsite_skipped:yes|no>
# Credentials: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from ~/.hermes/.env.
# Never crashes the caller — set -euo pipefail is intentionally NOT used here.
LOG="${HOME}/backups/cron.log"
ENV_FILE="${HOME}/.hermes/.env"

EXPORT_RC="$1"; OFFSITE_RC="$2"; SKIPPED="$3"

# Stage table
if [ "$EXPORT_RC" -eq 0 ]; then S1="✅ local export OK"; else S1="❌ local export FAILED (rc=$EXPORT_RC)"; fi
if [ "$SKIPPED" = yes ]; then
  S2="⏭️ offsite upload SKIPPED (export failed)"
elif [ "$OFFSITE_RC" -eq 0 ]; then
  S2="✅ offsite upload OK"
else
  S2="❌ offsite upload FAILED (rc=$OFFSITE_RC)"
fi

if [ "$EXPORT_RC" -eq 0 ] && [ "$OFFSITE_RC" -eq 0 ]; then
  HEAD="🟢 JARVIS weekly backup: OK"
else
  HEAD="🔴 JARVIS weekly backup: FAILURE"
fi

# Detail = last 20 lines of the cron log (prune/usage/error info)
TAIL_TXT="$(tail -n 20 "$LOG" 2>/dev/null | sed 's/```/[/g' || true)"
[ -n "$TAIL_TXT" ] && DETAIL="

\`\`\`
$TAIL_TXT
\`\`\`" || DETAIL=""

MSG="$HEAD
$S1
$S2$DETAIL"

if [ ! -f "$ENV_FILE" ]; then
  echo "[notify] $ENV_FILE missing — cannot send Telegram" >&2
  exit 0
fi
# shellcheck disable=SC1090
. "$ENV_FILE"
[ -n "${TELEGRAM_BOT_TOKEN:-}" ] || { echo "[notify] no TELEGRAM_BOT_TOKEN" >&2; exit 0; }
CHAT="${TELEGRAM_CHAT_ID:-${TELEGRAM_HOME_CHANNEL:-}}"
[ -n "$CHAT" ] || { echo "[notify] no TELEGRAM_CHAT_ID" >&2; exit 0; }

curl -sS --max-time 30 -o /dev/null -w "%{http_code}" \
  -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT}" \
  --data-urlencode "text=${MSG}" \
  > /tmp/bk_notify_rc 2>/dev/null
HTTP="$(cat /tmp/bk_notify_rc 2>/dev/null || echo 000)"
rm -f /tmp/bk_notify_rc
echo "[notify] telegram http=$HTTP" >> "$LOG"
