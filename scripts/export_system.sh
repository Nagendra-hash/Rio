#!/usr/bin/env bash
# export_system.sh — back up JARVIS + Hermes so a new machine runs as-is.
#
# Backs up the five locations that carry the whole setup (see readme.md,
# "Hermes Agent Integration"):
#   1. ~/Mark-III          JARVIS code, config/keys, memory, journals
#   2. ~/.hermes            Hermes agent, config, persona, skills, state DBs
#   3. ~/freellmapi         FreeLLMAPI router (LLM pool): code, build, SQLite
#                           (provider keys + the ENCRYPTION_KEY they depend
#                           on — losing .env loses the stored keys)
#   4. ~/.local (subset)    user tools (tesseract, ocrmypdf, hermes, uv),
#                           uv Python runtimes, tessdata
#   5. ~/.config/systemd/user  service units (hermes-gateway, jarvis-gui,
#                           freellmapi, …)
# plus `crontab -l` → jarvis-crontab-<stamp>.txt and
# `pip list --user` → pip-user-requirements-<stamp>.txt (slim archives skip
# the 3.6 GB pip user site-packages; restore uses this file to reinstall
# exactly what was there — ocrmypdf included).
#
# Usage:
#   ./scripts/export_system.sh                    # slim: skips rebuildable
#     # venvs/node_modules/.git/caches (~1.5 GB archive, minutes)
#   ./scripts/export_system.sh --full             # byte-for-byte (~8.5 GB)
#   ./scripts/export_system.sh --full --to=/mnt/backup
#   ./scripts/export_system.sh --to=~/backups --prune=30   # also delete
#     # backups (and their .sha256/.manifest/pip/crontab companions) older
#     # than 30 days — the newest archive is ALWAYS kept
#   ./scripts/export_system.sh --prune-only=30 --to=~/backups  # just prune,
#     # no new archive (used by tests and manual cleanup)
#
# Services are STOPPED during export (SQLite safety) and RESTARTED after —
# including a JARVIS that was launched manually (cwd-based detection, not
# just unit stops). Output: <to>/jarvis-hermes-backup-<host>-<date>.tar.zst
# (+ .manifest.txt, .sha256). Restore with scripts/restore_system.sh.
set -euo pipefail

MODE="slim"; TO=""; PRUNE_DAYS=0; PRUNE_ONLY=no
while [ $# -gt 0 ]; do
  case "$1" in
    --full) MODE="full" ;;
    --to=*) TO="${1#--to=}" ;;
    --prune=*) PRUNE_DAYS="${1#--prune=}" ;;
    --prune-only=*) PRUNE_DAYS="${1#--prune-only=}"; PRUNE_ONLY=yes ;;
    *) echo "Unknown arg: $1 (use --full, --to=DIR, --prune=DAYS, --prune-only=DAYS)" >&2; exit 2 ;;
  esac
  shift
done
TO="${TO:-$HOME}"

USER_NAME="$(id -un)"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
STAMP="$(date +%Y%m%d-%H%M%S)"
HOST_TAG="$(hostname -s)"
ARCHIVE="$TO/jarvis-hermes-backup-$HOST_TAG-$STAMP.tar.zst"
MANIFEST="$ARCHIVE.manifest.txt"
SHA="$ARCHIVE.sha256"

log() { printf '\033[1;36m[export]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[export] ⚠️  %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m[export] FATAL: %s\033[0m\n' "$*" >&2; exit 1; }

# ── Prune old backups ───────────────────────────────────────────────────────
# Deletes jarvis-hermes-backup-*.tar.zst (plus .sha256/.manifest.txt) and the
# per-run pip-user-requirements-*/jarvis-crontab-* companions older than
# PRUNE_DAYS. The newest archive always survives, even if ancient.
prune() {
  [ "$PRUNE_DAYS" -gt 0 ] 2>/dev/null || { warn "--prune needs a positive day count, got '$PRUNE_DAYS'"; return 0; }
  local newest removed=0 kept=0 f x
  newest="$(ls -1t "$TO"/jarvis-hermes-backup-*.tar.zst 2>/dev/null | head -1 || true)"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ "$f" = "$newest" ]; then kept=$((kept+1)); continue; fi
    if [ -n "$(find "$f" -mtime +"$PRUNE_DAYS" -print -quit 2>/dev/null)" ]; then
      rm -f "$f" "$f.sha256" "$f.manifest.txt"
      removed=$((removed+1))
    else
      kept=$((kept+1))
    fi
  done < <(ls -1tr "$TO"/jarvis-hermes-backup-*.tar.zst 2>/dev/null || true)
  while IFS= read -r x; do
    [ -n "$x" ] || continue
    rm -f "$x"; removed=$((removed+1))
  done < <(find "$TO" -maxdepth 1 \( -name 'pip-user-requirements-*.txt' -o -name 'jarvis-crontab-*.txt' \) \
             -mtime +"$PRUNE_DAYS" -print 2>/dev/null)
  log "prune: removed $removed old backup file(s), kept $kept archive(s) (>${PRUNE_DAYS}d; newest always kept)"
}

if [ "$PRUNE_ONLY" = yes ]; then
  mkdir -p "$TO"; [ -w "$TO" ] || die "cannot write to $TO"
  prune
  exit 0
fi

# ── Pre-flight ──────────────────────────────────────────────────────────────
command -v zstd >/dev/null || die "zstd not installed (sudo apt install zstd)"
[ -d "$HOME_DIR/Mark-III" ] || die "$HOME_DIR/Mark-III not found — run from the JARVIS machine"
[ -d "$HOME_DIR/.hermes" ]   || die "$HOME_DIR/.hermes not found"
mkdir -p "$TO"; [ -w "$TO" ] || die "cannot write to $TO"
FREE_K=$(df -k --output=avail "$TO" | tail -1)
[ "$FREE_K" -gt 4000000 ] || warn "less than 4 GB free at $TO — slim export may not fit"

log "mode=$MODE  user=$USER_NAME  home=$HOME_DIR  → $ARCHIVE"

# ── Stop services (SQLite WAL safety) ───────────────────────────────────────
# freellmapi is included: its router holds freeapi.db (SQLite) open.
UNITS=(hermes-gateway jarvis-gui jarvis-tv-tunnel hermes-skill-batch freellmapi)
log "stopping user services: ${UNITS[*]}"
for u in "${UNITS[@]}"; do systemctl --user stop "$u.service" 2>/dev/null || true; done

# JARVIS may also run MANUALLY (cwd = Mark-III, cmdline "python main.py" —
# invisible to pkill -f patterns). Kill by working directory instead.
for p in $(pgrep python 2>/dev/null || true); do
  cwd=$(readlink "/proc/$p/cwd" 2>/dev/null || true)
  if [ "$cwd" = "$HOME_DIR/Mark-III" ]; then
    log "stopping manually-launched JARVIS (pid $p)"
    kill "$p" 2>/dev/null || true
  fi
done
sleep 3

restore_services() {
  log "restarting services on THIS machine…"
  systemctl --user daemon-reload 2>/dev/null || true
  for u in "${UNITS[@]}"; do
    # Type=oneshot units (hermes-skill-batch installs skills for many minutes)
    # must NOT block this loop — `systemctl start` waits for oneshot completion.
    if [ "$(systemctl --user show -p Type --value "$u.service" 2>/dev/null)" = "oneshot" ]; then
      systemctl --user start --no-block "$u.service" 2>/dev/null || true
    else
      systemctl --user start "$u.service" 2>/dev/null || true
    fi
  done
  # Restart checks: a service that silently failed to come back up would
  # otherwise only surface as a mysterious outage hours later. Oneshots are
  # judged on state (activating = fine, it is working through ExecStart).
  sleep 3
  for u in "${UNITS[@]}"; do
    _type=$(systemctl --user show -p Type --value "$u.service" 2>/dev/null)
    if [ "$_type" = "oneshot" ]; then
      # A finished oneshot ends up INACTIVE (no RemainAfterExit), so judge by
      # state + Result: active/activating = running, inactive+success = done.
      _state=$(systemctl --user show -p ActiveState --value "$u.service" 2>/dev/null)
      _result=$(systemctl --user show -p Result --value "$u.service" 2>/dev/null)
      case "$_state" in
        activating) log "restart check: $u launched (async oneshot — completes on its own)" ;;
        active)     log "restart check: $u active (oneshot still running its ExecStart)" ;;
        inactive)
          if [ "$_result" = "success" ]; then
            log "restart check: $u ran and completed (oneshot)"
          else
            warn "restart check: $u did NOT complete (Result=$_result) — journalctl --user -u $u -n 20"
          fi ;;
        *)          warn "restart check: $u FAILED to start (state=$_state, Result=$_result) — journalctl --user -u $u -n 20" ;;
      esac
    elif systemctl --user is-active --quiet "$u.service" 2>/dev/null; then
      log "restart check: $u active"
    else
      warn "restart check: $u FAILED to start — journalctl --user -u $u -n 20"
    fi
  done
  log "NOTE: if JARVIS ran manually before (not via jarvis-gui unit), relaunch it:"
  log "      cd $HOME_DIR/Mark-III && .venv/bin/python main.py &"
}
trap restore_services EXIT INT TERM

# ── SQLite integrity (report only; a corrupt db would fail on the new box) ──
if command -v sqlite3 >/dev/null; then
  while IFS= read -r db; do
    if ! sqlite3 "$db" "PRAGMA quick_check;" >/dev/null 2>&1; then
      warn "CORRUPT DB (review before trusting backup): $db"
    fi
  done < <(find "$HOME_DIR/.hermes" "$HOME_DIR/Mark-III" "$HOME_DIR/freellmapi" -maxdepth 4 -name "*.db" 2>/dev/null)
else
  warn "sqlite3 CLI not installed — DBs unchecked (sudo apt install sqlite3)"
fi

# ── pip --user inventory (slim archives skip site-packages; restore uses this)
if command -v pip >/dev/null; then
  PIP_FILE="$TO/pip-user-requirements-$STAMP.txt"
  pip list --user --format=freeze > "$PIP_FILE" 2>/dev/null \
    && log "pip --user inventory: $PIP_FILE ($(grep -c . "$PIP_FILE") packages)" \
    || warn "pip list --user failed — user-site packages must be installed manually"
else
  PIP_FILE=""
  warn "pip not found — user-site packages must be installed manually"
fi

# ── Exclusions ──────────────────────────────────────────────────────────────
# Slim drops everything restore_system.sh can rebuild: both venvs (Mark-III
# 2.2G, hermes 728M), hermes node_modules 403M, hermes .git 831M, pip user
# site-packages 3.6G. The uv Python RUNTIME is always kept — venvs and
# rebuilds reference it by absolute path.
if [ "$MODE" = "slim" ]; then
  EXCL=(--exclude='Mark-III/.venv'
        --exclude='freellmapi/node_modules'
        --exclude='freellmapi/.git'
        --exclude='.hermes/hermes-agent/venv'
        --exclude='.hermes/hermes-agent/node_modules'
        --exclude='.hermes/hermes-agent/.git'
        --exclude='.hermes/cache'
        --exclude='.hermes/audio_cache'
        --exclude='.hermes/image_cache'
        --exclude='.hermes/sandboxes'
        --exclude='.hermes/pending'
        --exclude='.hermes/pending_messages'
        --exclude='.local/lib/python3.10'  # pip --user site-packages (rebuilt from pip-user inventory; .local/lib keeps the compiled OCR libs)
        --exclude='.local/lib/python3.12'
        --exclude='.hermes/logs'
        --exclude='.hermes/gateway.sock'
        --exclude='.hermes/gateway.pid'
        --exclude='.hermes/*.lock')
else
  EXCL=(--exclude='.hermes/cache'
        --exclude='.hermes/audio_cache'
        --exclude='.hermes/image_cache'
        --exclude='.hermes/sandboxes'
        --exclude='.hermes/logs'
        --exclude='.hermes/gateway.sock'
        --exclude='.hermes/gateway.pid'
        --exclude='.hermes/*.lock')
fi

log "creating archive — on an HDD expect 5–15 min; do not interrupt…"
tar -C "$HOME_DIR" "${EXCL[@]}" \
    -cf - \
    "Mark-III" \
    ".hermes" \
    "freellmapi" \
    ".config/systemd/user" \
    ".local/bin" \
    ".local/lib" \
    ".local/share/tessdata" \
    ".local/share/uv/python" \
  | zstd -q -T0 -3 -o "$ARCHIVE"

# crontab lives outside all four trees
if crontab -l > "$TO/jarvis-crontab-$STAMP.txt" 2>/dev/null; then
  CRON_MSG="crontab:  $TO/jarvis-crontab-$STAMP.txt"
else
  CRON_MSG="crontab:  (none on this machine)"
  rm -f "$TO/jarvis-crontab-$STAMP.txt"
fi

# ── Manifest + checksum ─────────────────────────────────────────────────────
{
  echo "jarvis-hermes backup"
  echo "created:  $(date -Is)"
  echo "mode:     $MODE (slim skips rebuildable venvs/node_modules/.git/caches)"
  echo "user:     $USER_NAME   home: $HOME_DIR"
  echo "host:     $HOST_TAG"
  echo "pip-user: ${PIP_FILE:+$(basename "$PIP_FILE")}"
  echo
  echo "RESTORE ON TARGET:  scripts/restore_system.sh $(basename "$ARCHIVE")"
} > "$MANIFEST"
sha256sum "$ARCHIVE" > "$SHA"

log "✅ backup complete: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
log "   manifest: $MANIFEST"
log "   checksum: $SHA"
log "   $CRON_MSG"

[ "$PRUNE_DAYS" -gt 0 ] 2>/dev/null && prune
true
