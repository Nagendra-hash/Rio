#!/usr/bin/env bash
# restore_system.sh — restore a jarvis-hermes backup onto THIS machine.
#
# Usage:
#   ./scripts/restore_system.sh /path/to/jarvis-hermes-backup-<host>-<date>.tar.zst [--home=/some/path]
#
# --home=PATH extracts into an ISOLATED directory instead of $HOME — used for
# restore drills (nothing touches the live system; systemd/cron steps are
# skipped and venvs are built but services are NOT started).
#
# Two scenarios:
#   A. same username + home layout → extract, verify DBs, install units, done.
#   B. different username/home     → additionally rewrite every baked
#      /home/<olduser> path (code refs, pyvenv.cfg, systemd units, launchers)
#      and rebuild venvs from the bundled uv runtime (slim archives carry no
#      venvs and no pip user site-packages).
#
# The old home is detected from the archive manifest (user: field, written at
# export time) with a pyvenv.cfg fallback — NOT from archive paths, since the
# tree is stored relative to $HOME.
#
# Existing dirs are never clobbered: they are moved aside to <name>.pre-restore-<ts>.
set -euo pipefail

ARCHIVE=""; TARGET_HOME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --home=*) TARGET_HOME="${1#--home=}" ;;
    *) ARCHIVE="$1" ;;
  esac
  shift
done
[ -n "$ARCHIVE" ] && [ -f "$ARCHIVE" ] || { echo "usage: $0 <backup.tar.zst> [--home=/isolated/path]" >&2; exit 2; }
SHA="${ARCHIVE}.sha256"
MANIFEST="${ARCHIVE}.manifest.txt"

log()  { printf '\033[1;36m[restore]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[restore] ⚠️  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[restore] FATAL: %s\033[0m\n' "$*" >&2; exit 1; }

command -v zstd    >/dev/null || die "zstd missing (sudo apt install zstd)"
command -v python3 >/dev/null || die "python3 missing"

# Drill mode: isolated target, no systemd/cron side effects.
DRILL=no
if [ -n "$TARGET_HOME" ]; then
  DRILL=yes
  mkdir -p "$TARGET_HOME"
  log "DRILL MODE: restoring into isolated $TARGET_HOME (no systemd/cron changes)"
fi

# A REAL restore REPLACES live directories (Mark-LIII, .hermes, .local/…).
# 2026-09-23 drill incident: an accidental non-drill run moved the live home
# aside while services were still running. Never proceed by accident.
if [ "$DRILL" = no ] && [ "$CONFIRM_RESTORE" != "yes" ]; then
  cat >&2 <<'EOF'

  ⚠️  This will REPLACE live directories in $HOME (Mark-LIII, .hermes,
      .local/bin + .local/lib, systemd units). Existing data is moved aside,
      not deleted, but services MUST be stopped first.

  Proceed for real:   CONFIRM_RESTORE=yes $0 <archive>
  Safe drill instead: TARGET_HOME=/tmp/fakehome $0 <archive>

EOF
  exit 1
fi

# ── Verify checksum ─────────────────────────────────────────────────────────
if [ -f "$SHA" ]; then
  log "verifying sha256…"
  ( cd "$(dirname "$ARCHIVE")" && sha256sum -c "$(basename "$SHA")" --quiet ) \
    || die "checksum mismatch — archive damaged"
  log "checksum OK"
else
  warn "no .sha256 next to archive — skipping integrity check"
fi
[ -f "$MANIFEST" ] && { log "manifest:"; sed 's/^/    /' "$MANIFEST"; }

# ── App-dir name detection (audit fix 2026-10-08) ───────────────────────────
# Archives made before 2026-09 contain the checkout as ./Mark-LIII/…; exports
# from the renamed checkout pack ./Mark-III/…. Probe once and use it everywhere
# below, so both archive generations restore identically.
APP_DIR=""
for _cand in Mark-III Mark-LIII; do
  if tar --use-compress-program=unzstd -xOf "$ARCHIVE" "./$_cand/requirements.txt" >/dev/null 2>&1; then
    APP_DIR="$_cand"
    break
  fi
done
[ -n "$APP_DIR" ] || die "archive contains neither Mark-III/ nor Mark-LIII/ — not a JARVIS export"
log "archive app dir: $APP_DIR"

# ── Old→new home detection ──────────────────────────────────────────────────
NEW_USER="$(id -un)"
NEW_HOME="${TARGET_HOME:-$(getent passwd "$NEW_USER" | cut -d: -f6)}"
OLD_HOME=""
if [ -f "$MANIFEST" ] && grep -q '^user:' "$MANIFEST"; then
  OLD_USER_MF=$(sed -n 's/^user:[[:space:]]*\([^ ]*\).*$/\1/p' "$MANIFEST" | head -1)
  [ -n "$OLD_USER_MF" ] && OLD_HOME="$(getent passwd "$OLD_USER_MF" | cut -d: -f6 || true)"
fi
if [ -z "$OLD_HOME" ] || [ "$OLD_HOME" = "$NEW_HOME" ]; then
  # fallback: read the baked home out of the archived JARVIS venv config
  OLD_HOME=$(tar --use-compress-program=unzstd -xOf "$ARCHIVE" "./$APP_DIR/.venv/pyvenv.cfg" 2>/dev/null \
    | awk '/^home =/{print $3; exit}' \
    | awk -F'/\\.local' '{print $1}') || OLD_HOME=""
fi
OLD_HOME="${OLD_HOME:-$NEW_HOME}"
log "old home: $OLD_HOME    new home: $NEW_HOME"

# ── Extract ─────────────────────────────────────────────────────────────────
if [ "$DRILL" = no ]; then
  if systemctl --user is-active --quiet hermes-gateway 2>/dev/null \
     || systemctl --user is-active --quiet jarvis-gui 2>/dev/null; then
    die "hermes-gateway/jarvis-gui are still RUNNING — stop them first: systemctl --user stop hermes-gateway jarvis-gui"
  fi
  for d in "$APP_DIR" ".hermes" "freellmapi" ".config/systemd/user" ".local/bin" ".local/lib" ".local/share/tessdata" ".local/share/uv/python"; do
    if [ -e "$NEW_HOME/$d" ]; then
      BAK="$NEW_HOME/$(echo "$d" | tr '/' '_').pre-restore-$(date +%s)"
      warn "$d exists — moving aside to $(basename "$BAK")"
      mv "$NEW_HOME/$d" "$BAK"
    fi
  done
fi

log "extracting (long step)…"
tar --use-compress-program=unzstd -xf "$ARCHIVE" -C "$NEW_HOME"
log "extracted."

# ── Archive completeness (slim sanity) ─────────────────────────────────────
MISSING=""
[ -d "$NEW_HOME/.local/share/uv/python" ] || MISSING="$MISSING uv-runtime"
[ -f "$NEW_HOME/$APP_DIR/requirements.txt" ] || MISSING="$MISSING $APP_DIR/requirements.txt"
[ -d "$NEW_HOME/.hermes/hermes-agent" ] || MISSING="$MISSING hermes-agent"
[ -n "$MISSING" ] && warn "archive is missing:$MISSING — restore will be incomplete"

# ── Path patching ───────────────────────────────────────────────────────────
if [ "$OLD_HOME" != "$NEW_HOME" ]; then
  log "rewriting baked paths: $OLD_HOME → $NEW_HOME"
  OLD_Q=$(printf '%s' "$OLD_HOME" | sed 's/[.[\*^$/]/\\&/g')
  NEW_Q=$(printf '%s' "$NEW_HOME" | sed 's/[&/]/\\&/g')
  N=0
  while IFS= read -r -d '' f; do
    sed -i "s/$OLD_Q/$NEW_Q/g" "$f" && N=$((N+1))
  done < <(grep -rlZ --binary-files=without-match "$OLD_HOME" \
        "$NEW_HOME/$APP_DIR/actions" \
        "$NEW_HOME/$APP_DIR/plugins" \
        "$NEW_HOME/$APP_DIR/scripts" \
        "$NEW_HOME/.config/systemd/user" \
        "$NEW_HOME/.local/bin" 2>/dev/null || true)
  # venv configs + hermes launcher(s)
  find "$NEW_HOME/$APP_DIR/.venv" "$NEW_HOME/.hermes/hermes-agent/venv" \
       -maxdepth 1 -name pyvenv.cfg -exec sed -i "s/$OLD_Q/$NEW_Q/g" {} + 2>/dev/null || true
  log "patched $N files + venv configs."

  # ELF RUNPATH fix: compiled binaries (.local/bin) and shared libs
  # (.local/lib: libtesseract, libleptonica, libtiff, libjpeg…) bake the
  # build-time home into their RUNPATH; sed on ELF would corrupt them, so
  # rewrite via patchelf to location-independent $ORIGIN paths.
  if command -v patchelf >/dev/null 2>&1; then
    log "fixing ELF RUNPATHs → $ORIGIN (portable across homes)…"
    while IFS= read -r -d '' f; do
      patchelf --set-rpath '$ORIGIN/../lib' "$f" 2>/dev/null || true
    done < <(find "$NEW_HOME/.local/bin" -maxdepth 1 -type f -print0 2>/dev/null)
    while IFS= read -r -d '' f; do
      patchelf --set-rpath '$ORIGIN' "$f" 2>/dev/null || true
    done < <(find -L "$NEW_HOME/.local/lib" -maxdepth 1 -name '*.so*' -type f -print0 2>/dev/null)
    log "RUNPATHs fixed."
  else
    warn "patchelf not found — tesseract/libjpeg may fail to load libraries on a new home."
    warn "fix later: python3 -m pip install --user patchelf; then rerun this script."
  fi
else
  log "same home — no path rewrite needed."
fi

# ── Python runtime for venv rebuilds ────────────────────────────────────────
# Prefer the runtime bundled in the archive; fall back to any uv-managed
# cpython-3.12 already on this machine; last resort: system python3.
UV_PY="$(ls -d "$NEW_HOME"/.local/share/uv/python/cpython-3.12*/bin/python3.12 2>/dev/null | head -1 || true)"
if [ -z "$UV_PY" ]; then
  UV_PY="$(ls -d "$HOME"/.local/share/uv/python/cpython-3.12*/bin/python3.12 2>/dev/null | head -1 || true)"
  [ -n "$UV_PY" ] && warn "archive has no uv runtime — using this machine's: $UV_PY"
fi
if [ -z "$UV_PY" ]; then
  UV_PY="$(command -v python3 || true)"
  [ -n "$UV_PY" ] && warn "no cpython-3.12 runtime anywhere — falling back to system python3 ($UV_PY); package wheels may differ"
fi

# ── Rebuild venvs (slim archives ship without them) ─────────────────────────
if [ ! -x "$NEW_HOME/$APP_DIR/.venv/bin/python" ]; then
  [ -n "$UV_PY" ] || die "$APP_DIR venv missing and no runtime to rebuild it"
  log "rebuilding $APP_DIR venv (this downloads packages — needs network)…"
  "$UV_PY" -m venv "$NEW_HOME/$APP_DIR/.venv"
  "$NEW_HOME/$APP_DIR/.venv/bin/pip" install -q --upgrade pip
  if [ -f "$NEW_HOME/$APP_DIR/requirements.txt" ]; then
    "$NEW_HOME/$APP_DIR/.venv/bin/pip" install -q -r "$NEW_HOME/$APP_DIR/requirements.txt" \
      || warn "some requirements failed — rerun: ~/$APP_DIR/.venv/bin/pip install -r ~/$APP_DIR/requirements.txt"
  fi
else
  log "$APP_DIR venv present in archive — checking it runs…"
  "$NEW_HOME/$APP_DIR/.venv/bin/python" -c "import sys; assert sys.prefix != sys.base_prefix" \
    || warn "$APP_DIR venv broken — delete ~/.venv and re-run this script"
fi

if [ ! -x "$NEW_HOME/.hermes/hermes-agent/venv/bin/python" ] && [ -d "$NEW_HOME/.hermes/hermes-agent" ]; then
  [ -n "$UV_PY" ] || die "hermes venv missing and no runtime to rebuild it"
  log "rebuilding hermes venv (editable hermes-agent install)…"
  "$UV_PY" -m venv "$NEW_HOME/.hermes/hermes-agent/venv"
  "$NEW_HOME/.hermes/hermes-agent/venv/bin/pip" install -q --upgrade pip
  "$NEW_HOME/.hermes/hermes-agent/venv/bin/pip" install -q -e "$NEW_HOME/.hermes/hermes-agent" \
    || warn "editable install failed — cd ~/.hermes/hermes-agent && venv/bin/pip install -e ."
else
  log "hermes venv present in archive."
fi

# ── FreeLLMAPI router (freellmapi.service) ─────────────────────────────
# Ships with server/dist + client/dist + SQLite data; slim archives skip
# node_modules (restore runs npm install). Node 20+ must already exist.
if [ -d "$NEW_HOME/freellmapi" ]; then
  if ! command -v node >/dev/null 2>&1; then
    warn "node not found — install Node.js 20+ before starting freellmapi.service"
  elif [ ! -f "$NEW_HOME/freellmapi/server/dist/index.js" ]; then
    log "building freellmapi (server + client dist missing)…"
    (cd "$NEW_HOME/freellmapi" && npm install --no-audit --no-fund -q \
       && npm run build -q) \
      || warn "freellmapi build failed — run it manually: cd ~/freellmapi && npm install && npm run build"
  elif [ ! -d "$NEW_HOME/freellmapi/node_modules" ]; then
    log "reinstalling freellmapi node_modules (skipped in slim backup)…"
    (cd "$NEW_HOME/freellmapi" && npm install --no-audit --no-fund -q) \
      || warn "npm install failed — run it manually: cd ~/freellmapi && npm install"
  fi
  if [ ! -f "$NEW_HOME/freellmapi/.env" ]; then
    warn "freellmapi/.env missing — provider keys in its SQLite are DECRYPTABLE only with the original ENCRYPTION_KEY"
  fi
else
  warn "freellmapi/ not in archive — install manually if the LLM router is wanted"
fi

# ── pip --user essentials (slim archives skip user site-packages) ───────────
# ocrmypdf must be importable/runnable for the pdf-ocr skill; anything else in
# pip-user-requirements-*.txt can be reinstalled with one command (printed in
# the checklist — a blind bulk install could take an hour on torch etc.).
if ! "$NEW_HOME/.local/bin/ocrmypdf" --version >/dev/null 2>&1 \
   && ! python3 -m pip show ocrmypdf >/dev/null 2>&1; then
  log "installing ocrmypdf (pip --user, small)…"
  python3 -m pip install -q --user ocrmypdf 2>/dev/null \
    || warn "ocrmypdf install failed — run: python3 -m pip install --user ocrmypdf"
fi
PIP_REQ="$(ls "$(dirname "$ARCHIVE")"/pip-user-requirements-*.txt 2>/dev/null | head -1 || true)"
[ -n "$PIP_REQ" ] && warn "full pip --user inventory available: $PIP_REQ"
[ -n "$PIP_REQ" ] && warn "restore it if needed:  python3 -m pip install --user -r \"$PIP_REQ\"  (large: torch/tensorflow — optional)"

# ── DB integrity ────────────────────────────────────────────────────────────
# (uses python3's sqlite3 module — the sqlite3 CLI is often not installed)
log "checking restored SQLite databases…"
python3 - "$NEW_HOME" "${APP_DIR}" <<'PYEOF' || warn "one or more databases are CORRUPT — see list above"
import glob, os, sqlite3, sys
new_home, app_dir = sys.argv[1], sys.argv[2]
bad = checked = 0
for root in (".hermes", app_dir):
    for db in glob.glob(os.path.join(new_home, root, "**", "*.db"), recursive=True):
        checked += 1
        try:
            r = sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute("PRAGMA quick_check").fetchone()[0]
        except Exception as e:
            r = f"unreadable: {e}"
        if r != "ok":
            print(f"  CORRUPT: {db} → {r}")
            bad += 1
print(f"  {checked} databases checked, {bad} corrupt")
sys.exit(1 if bad else 0)
PYEOF

# ── Systemd + linger + cron (skipped in drill mode) ─────────────────────────
if [ "$DRILL" = no ]; then
  log "installing systemd user units…"
  systemctl --user daemon-reload 2>/dev/null || warn "daemon-reload failed — run it after first login"
  loginctl enable-linger "$NEW_USER" 2>/dev/null || warn "enable-linger failed (services would stop at logout)"
  CF=$(ls "$(dirname "$ARCHIVE")"/jarvis-crontab-*.txt 2>/dev/null | head -1 || true)
  [ -n "$CF" ] && warn "crontab backup: $CF — review then load:  crontab \"$CF\""
else
  log "drill: skipping systemd/cron steps (isolated target)"
fi

# ── Final checklist ─────────────────────────────────────────────────────────
CRON_LINE=""
[ "$DRILL" = no ] && CRON_LINE="$(ls "$(dirname "$ARCHIVE")"/jarvis-crontab-*.txt 2>/dev/null | head -1 || true)"
cat <<EOF

════════════════════════════════════════════════════════════════
  RESTORE DONE ${DRILL:+(DRILL — nothing live was touched)}
  Restored into: $NEW_HOME

  Remaining manual steps ${DRILL:+(for a REAL restore)}:

  1. System packages NOT inside the archive:
     sudo apt install -y poppler-utils ghostscript zstd sqlite3 patchelf \\
       portaudio19-dev alsa-utils pulseaudio libnotify-bin xdotool scrot
     (+ display stack & Chrome for the JARVIS GUI)

  2. Smoke test (each must succeed):
     ~/.local/bin/tesseract --version
     ~/.local/bin/ocrmypdf --version
     ~/$APP_DIR/.venv/bin/python -c "import google.genai"
     ~/.hermes/hermes-agent/venv/bin/python -c "import tools.read_extract"
     hermes skills list                        # pdf-ocr must appear

  3. Start:
     systemctl --user start hermes-gateway jarvis-gui freellmapi
     journalctl --user -u hermes-gateway -f
     curl -s http://127.0.0.1:3001/v1/models -H "Authorization: Bearer $(grep FREELLMAPI_API_KEY ~/.hermes/.env | cut -d= -f2)" | head -c 80   # router sanity

  4. Cron:  crontab "${CRON_LINE:-<no crontab file found>}"

  Notes:
  • API keys travel inside the backup ($APP_DIR/config/api_keys.json,
    ~/.hermes/.env) — treat the archive as a SECRET.
  • Slim backups ship without pip user site-packages; ocrmypdf was reinstalled
    automatically if missing. Full inventory: ${PIP_REQ:-<none captured>}
  • Audio/video devices may need re-selection in JARVIS settings on new
    hardware (config/dashboard_devices.json, audio device picker).
════════════════════════════════════════════════════════════════
EOF
