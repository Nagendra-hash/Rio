#!/usr/bin/env bash
# weekly_backup.sh — cron entry: export → offsite upload → Telegram notify.
#
# Runs the full weekly chain, captures per-stage exit codes, appends detail
# to ~/backups/cron.log, then ALWAYS sends one Telegram summary (success or
# failure) so problems reach the phone, not just the log. Credentials come
# from ~/.hermes/.env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — never stored
# in this script.
#
# Test overrides (do not touch services):
#   EXPORT_CMD="true"  OFFSITE_CMD="true"  scripts/weekly_backup.sh   # success path
#   EXPORT_CMD="false"                     scripts/weekly_backup.sh   # failure path
set -u

HOME_DIR="${HOME}"
LOG="$HOME_DIR/backups/cron.log"
MARK="$(date '+%Y-%m-%d %H:%M:%S')"

EXPORT_CMD="${EXPORT_CMD:-$HOME_DIR/Mark-LIII/scripts/export_system.sh --to=$HOME_DIR/backups --prune=30}"
OFFSITE_CMD="${OFFSITE_CMD:-$HOME_DIR/Mark-LIII/scripts/offsite_backup.sh --prune=60}"

mkdir -p "$HOME_DIR/backups"
EXPORT_RC=0; OFFSITE_RC=0; OFFSITE_SKIPPED=no

{
  echo "════════ weekly backup $MARK ════════"
  eval "$EXPORT_CMD"
} >> "$LOG" 2>&1 || EXPORT_RC=$?

if [ "$EXPORT_RC" -eq 0 ]; then
  eval "$OFFSITE_CMD" >> "$LOG" 2>&1 || OFFSITE_RC=$?
else
  OFFSITE_SKIPPED=yes
fi

"$HOME_DIR/Mark-LIII/scripts/backup_notify.sh" "$EXPORT_RC" "$OFFSITE_RC" "$OFFSITE_SKIPPED" || true
exit $(( EXPORT_RC || OFFSITE_RC ))
