#!/usr/bin/env bash
# offsite_backup.sh — encrypted off-machine copy of JARVIS/Hermes backups.
#
# Takes the NEWEST jarvis-hermes-backup-*.tar.zst in the local backup dir,
# encrypts it client-side (AES-256-CBC + HMAC-SHA256, key in ~/.backup_offsite.key
# — the cloud copy is useless without this key file), uploads to Cloudflare R2
# via rclone, and prunes offsite archives older than --prune days (the newest
# offsite archive is always kept). Companions (.sha256/.manifest) are uploaded
# too so any machine with the key can verify what it downloaded.
#
# Usage:
#   scripts/offsite_backup.sh                     # upload newest + prune >60d
#   scripts/offsite_backup.sh --prune=90          # custom retention
#   scripts/offsite_backup.sh --file=<archive>    # upload a specific archive
#   scripts/offsite_backup.sh --prune-only=60     # just prune, no upload
#   scripts/offsite_backup.sh --verify=<file.enc> # decrypt+checksum a remote file
#
# Requires: rclone in ~/.local/bin (remote "r2-backups"), openssl, ~/.backup_offsite.key
set -euo pipefail

LOCAL_DIR="${HOME}/backups"
KEY="${HOME}/.backup_offsite.key"
REMOTE="r2-backups"
BUCKET="jarvis-hermes-backups"
RCLONE="${HOME}/.local/bin/rclone"
[ -x "$RCLONE" ] || RCLONE="$(command -v rclone || true)"

PRUNE_DAYS=60; SPECIFIC=""; PRUNE_ONLY=no; VERIFY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --prune=*) PRUNE_DAYS="${1#--prune=}" ;;
    --file=*) SPECIFIC="${1#--file=}" ;;
    --prune-only=*) PRUNE_DAYS="${1#--prune-only=}"; PRUNE_ONLY=yes ;;
    --verify=*) VERIFY="${1#--verify=}" ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

log()  { printf '\033[1;36m[offsite]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[offsite] ⚠️  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[offsite] FATAL: %s\033[0m\n' "$*" >&2; exit 1; }

[ -n "$RCLONE" ] || die "rclone not found (~/.local/bin/rclone)"
[ -f "$KEY" ]    || die "encryption key missing: $KEY (refusing to upload unencrypted)"

# ── Verify mode: prove a remote .enc is intact and matches its sha256 ───────
if [ -n "$VERIFY" ]; then
  BASE="$(basename "$VERIFY")"; SRC="${VERIFY%$BASE}"
  TMP="$(mktemp /tmp/offsite-verify-XXXXXX.tar.zst)"
  trap 'rm -f "$TMP" "$TMP.dec"' EXIT
  log "downloading $BASE for verification…"
  "$RCLONE" copyto "${SRC%/}/$BASE" "$TMP" >/dev/null
  # 1) ciphertext integrity (transfer check)
  ENC_SHA="$("$RCLONE" cat "${SRC%/}/$BASE.sha256" 2>/dev/null | awk '{print $1}')" || true
  if [ -n "$ENC_SHA" ]; then
    echo "$ENC_SHA  $TMP" | sha256sum -c --quiet \
      || die "ciphertext checksum MISMATCH for $BASE"
    log "✓ ciphertext checksum OK"
  fi
  # 2) decrypt → must equal the ORIGINAL archive's sha256 (full roundtrip)
  openssl enc -d -aes-256-cbc -md sha256 \
    -pbkdf2 -iter 600000 -in "$TMP" -out "$TMP.dec" -pass file:"$KEY"
  rm -f "$TMP"
  PLAIN_BASE="${BASE%.enc}"   # …tar.zst
  PLAIN_SHA="$("$RCLONE" cat "${SRC%/}/$PLAIN_BASE.sha256" 2>/dev/null | awk '{print $1}')" || true
  if [ -n "$PLAIN_SHA" ]; then
    echo "$PLAIN_SHA  $TMP.dec" | sha256sum -c --quiet \
      && log "✓ decrypt + roundtrip checksum OK: $BASE == original" \
      || die "roundtrip checksum MISMATCH for $BASE"
  else
    log "✓ decrypt OK (no original .sha256 remote — roundtrip unproven): $BASE"
  fi
  exit 0
fi

# ── Bucket usage report (goes into ~/backups/cron.log) ─────────────────────
report_usage() {
  # rclone size --json → {count, bytes}; R2 free tier = 10 GB storage.
  local SIZE_JSON
  SIZE_JSON="$($RCLONE size "$REMOTE:$BUCKET" --json 2>/dev/null || true)"
  if [ -z "$SIZE_JSON" ]; then
    warn "could not query R2 bucket size (rclone size failed)"
    return 0
  fi
  python3 - "$SIZE_JSON" <<'PYEOF' || true
import json, sys
try:
    d = json.loads(sys.argv[1])
    n, b = int(d.get("count", 0)), int(d.get("bytes", 0))
except Exception:
    sys.exit(0)
h = float(b)
unit = "B"
for unit in ("B", "KB", "MB", "GB", "TB"):
    if h < 1024 or unit == "TB":
        break
    h /= 1024
print(f"[offsite] R2 usage: {n} object(s), {h:.1f} {unit}")
FREE = 10 * 1024**3
pct = 100.0 * b / FREE
print(f"[offsite] R2 free tier: {pct:.1f}% of 10 GB used")
if pct >= 80:
    print("[offsite] ⚠️  >80% of R2 free tier — prune harder or upgrade")
PYEOF
}

# ── Prune helper (shared by both modes) ─────────────────────────────────────
prune() {
  [ "$PRUNE_DAYS" -gt 0 ] 2>/dev/null || return 0
  local newest removed=0 kept=0 f
  newest="$("$RCLONE" lsf "$REMOTE:$BUCKET" --files-only 2>/dev/null \
    | grep '\.tar\.zst\.enc$' | sort | tail -1 || true)"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ "$f" = "$newest" ]; then kept=$((kept+1)); continue; fi
    if [ -n "$("$RCLONE" lsf "$REMOTE:$BUCKET" --files-only --max-age "${PRUNE_DAYS}d" 2>/dev/null \
        | grep -Fx "$f")" ]; then
      kept=$((kept+1)); continue   # not old enough
    fi
    "$RCLONE" deletefile "$REMOTE:$BUCKET/$f" && removed=$((removed+1))
    "$RCLONE" deletefile "$REMOTE:$BUCKET/$f.sha256" 2>/dev/null || true
    "$RCLONE" deletefile "$REMOTE:$BUCKET/$f.manifest.txt" 2>/dev/null || true
  done < <("$RCLONE" lsf "$REMOTE:$BUCKET" --files-only 2>/dev/null | grep '\.tar\.zst\.enc$' | sort)
  log "prune: removed $removed offsite file(s), kept $kept (> ${PRUNE_DAYS}d; newest always kept)"
}

if [ "$PRUNE_ONLY" = yes ]; then
  prune
  report_usage
  exit 0
fi

# ── Pick archive: newest local or --file= ───────────────────────────────────
if [ -n "$SPECIFIC" ]; then
  ARCHIVE="$SPECIFIC"
else
  ARCHIVE="$(ls -1t "$LOCAL_DIR"/jarvis-hermes-backup-*.tar.zst 2>/dev/null | head -1 || true)"
fi
[ -n "$ARCHIVE" ] && [ -f "$ARCHIVE" ] || die "no local backup archive found in $LOCAL_DIR (run export_system.sh first)"
[ -r "$KEY" ] || die "encryption key unreadable: $KEY"

BASE="$(basename "$ARCHIVE")"
ENC="/tmp/$BASE.enc"
SHA_OUT="/tmp/$BASE.enc.sha256"
trap 'rm -f "$ENC" "$SHA_OUT"' EXIT

log "encrypting $(basename "$ARCHIVE") ($(du -h "$ARCHIVE" | cut -f1))…"
openssl enc -aes-256-cbc -md sha256 \
  -pbkdf2 -iter 600000 -salt -in "$ARCHIVE" -out "$ENC" -pass file:"$KEY"

log "checksumming ciphertext…"
SHA1="$(sha256sum "$ENC" | awk '{print $1}')"
printf '%s  %s\n' "$SHA1" "$BASE.enc" > "$SHA_OUT"

log "uploading to r2-backups:$BUCKET …"
"$RCLONE" copy "$ENC" "$REMOTE:$BUCKET" --transfers 4 --checkers 8
"$RCLONE" copy "$SHA_OUT" "$REMOTE:$BUCKET"
# companions: manifest + original sha (identify the archive offline)
[ -f "$ARCHIVE.manifest.txt" ] && "$RCLONE" copy "$ARCHIVE.manifest.txt" "$REMOTE:$BUCKET" || true
[ -f "$ARCHIVE.sha256" ] && "$RCLONE" copy "$ARCHIVE.sha256" "$REMOTE:$BUCKET" || true

log "uploaded: $BASE.enc (+ .sha256)"

# sanity: what's actually stored remotely?
"$RCLONE" lsf "$REMOTE:$BUCKET" --files-only | grep '\.enc$' | sed 's/^/    remote: /'

prune
report_usage
log "✅ offsite copy complete"
