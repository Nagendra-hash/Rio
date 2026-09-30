#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# jarvis-rotation-smoke.sh — post-rotation smoke check for JARVIS's Live session
#
# The timer (systemd/jarvis-rotation-smoke.timer) fires this every 5 minutes.
# Each run scans ONLY the jarvis-gui journald lines since the previous run
# (cursor file), so every GoAway rotation is examined exactly once:
#
#   rotation + ❌ Recv / google.genai.errors / websockets.exceptions /
#   handle rejection / same-connect-error loop
#     → offending lines into the log file + exit 1 (unit shows failed)
#   clean rotation (including the 2026-09-28 safety-net path: the server
#   force-closed first and the recv net caught it) → "OK" log line
#
# Failure markers are session-scoped on purpose. A bare "Traceback" line in
# the journal is usually a TOOL failure (browser_control, actions/* print
# their own) and must not fail the rotation check — the markers above only
# match the google-genai / websockets session-death shapes.
#
# Cursor discipline: the cursor file advances only after a rotation-free
# window or a COMPLETED rotation. A GoAway seen without its reconnect stays
# unpinned, so the next run re-examines it and keeps failing until JARVIS
# actually comes back — the alarm you want while the session is dead.
#
# Env overrides (used by tests/test_rotation_smoke.py):
#   JARVIS_SMOKE_JOURNALCTL   journalctl binary/stub
#   JARVIS_SMOKE_SYSTEMCTL    systemctl binary/stub
#   JARVIS_SMOKE_CURSOR_FILE  cursor state file
#   JARVIS_SMOKE_LOG          human-readable log file
#   JARVIS_SMOKE_SEED_WINDOW  --since window for the first seed (default 5 min)
#   JARVIS_SMOKE_ENV_FILE     Telegram creds file (default ~/.hermes/.env,
#                             backup_notify.sh convention: TELEGRAM_BOT_TOKEN /
#                             TELEGRAM_CHAT_ID or TELEGRAM_HOME_CHANNEL)
#   JARVIS_SMOKE_LASTFAIL_FILE cooldown state (default /tmp/jarvis_rotation_smoke.lastfail)
#   JARVIS_SMOKE_NOTIFY_COOLDOWN  minutes between pings for the SAME failure
#                             (default 60; 0 = ping every run)
#   JARVIS_SMOKE_TG_API       Telegram API base (stub-able in tests)
# ─────────────────────────────────────────────────────────────────────────────

JCTL="${JARVIS_SMOKE_JOURNALCTL:-journalctl}"
SYSCTL="${JARVIS_SMOKE_SYSTEMCTL:-systemctl}"
CURSOR_FILE="${JARVIS_SMOKE_CURSOR_FILE:-/tmp/jarvis_rotation_smoke.cursor}"
LOG="${JARVIS_SMOKE_LOG:-/tmp/jarvis_rotation_smoke.log}"
SEED_WINDOW="${JARVIS_SMOKE_SEED_WINDOW:--5 min}"
ENV_FILE="${JARVIS_SMOKE_ENV_FILE:-${HOME}/.hermes/.env}"
LASTFAIL_FILE="${JARVIS_SMOKE_LASTFAIL_FILE:-/tmp/jarvis_rotation_smoke.lastfail}"
NOTIFY_COOLDOWN_MIN="${JARVIS_SMOKE_NOTIFY_COOLDOWN:-60}"
TG_API="${JARVIS_SMOKE_TG_API:-https://api.telegram.org}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
note() { echo "[$(ts)] $*" >> "$LOG"; }

# ── the classifier ───────────────────────────────────────────────────────────
# Single awk pass over one window; mawk-safe (no length(array), no gawk-isms).
# stdin: window body WITHOUT cursor lines. stdout: verdict lines.
# exit: 0 healthy / 1 regression. Exposed via --analyze (below) so
# tests/test_rotation_smoke.py can drive it with journal fixtures — the exact
# code path the real run uses, no bash/journalctl mocking required.
analyze() {
  awk '
    /GoAway received/ { inrot = 1; rot++ }
    /\[JARVIS\] Connected\./ { if (inrot) { inrot = 0; done++ } }
    inrot && (/❌ Recv: / || /google\.genai\.errors/ || /websockets\.exceptions/) { f[fcount++] = $0 }
    inrot && /handle rejected/ { rej_in_rot++ }
    !inrot && /handle rejected/ { hcount++ }
    /Same connect error repeating/ { scount++ }
    END {
      if (rot == 0 && hcount == 0 && fcount == 0 && scount == 0) { print "no rotation in window"; exit 0 }
      printf "rotations=%d completed=%d\n", rot, done + 0
      for (i = 0; i < fcount; i++) print "FAIL: " f[i]
      # 2026-09-28: ONE rejection inside a rotation that then completes is the
      # EXPECTED steady state (upstream server reaps content-bearing session
      # handles; JARVIS falls back to a fresh session carrying the recent
      # conversation tail). That is degraded service, not an incident — no
      # page. Two or more rejections inside ONE rotation is a rejection loop.
      if (rej_in_rot == 1) print "DEGRADED: handle rejected once — fresh session with carried context (upstream bug, handled)"
      if (rej_in_rot > 1) print "FAIL: " rej_in_rot " handle rejections within a single rotation — rejection loop"
      if (hcount > 0) print "FAIL: " hcount " resumption handle rejection(s) outside any rotation — poisoned-handle loop"
      if (scount > 0) print "FAIL: " scount " repeats of the stuck-retry marker (same connect error repeating)"
      if (fcount > 0 || rej_in_rot > 1 || hcount > 0 || scount > 0) exit 1
      if (done + 0 < rot) { print "INCOMPLETE: GoAway seen without a completing reconnect"; exit 1 }
      if (rej_in_rot == 1) exit 0
      print "OK: rotation(s) completed clean"
      exit 0
    }
  '
}

# Test hook: analyze a fixture on stdin, print the verdict, exit with it.
if [ "${1:-}" = "--analyze" ]; then
  analyze
  exit $?
fi

# ── failure notifications ────────────────────────────────────────────────────
# A failing window stays unpinned (re-examined next run), so without a cooldown
# one incident would ping every 5 minutes until JARVIS recovers. The first two
# verdict lines identify the failure; identical failures within the cooldown
# window are logged but not re-sent. A DIFFERENT failure always pings.
notify_failure() {
  local verdict="$1" sig now when old_sig http chat
  # Signature = first two verdict lines with volatile digits (journal
  # timestamps, rotation counts) stripped — otherwise "handle rejected at
  # 04:15:43" vs "…04:24:46" would defeat the cooldown and ping every 5 min.
  sig=$(printf '%s\n' "$verdict" | head -n 2 | tr -d '0-9' | tr '\n' ' ')
  now=$(date +%s)
  if [ -f "$LASTFAIL_FILE" ] && [ "$NOTIFY_COOLDOWN_MIN" -gt 0 ] 2>/dev/null; then
    when=$(cut -d'|' -f1 "$LASTFAIL_FILE" 2>/dev/null)
    old_sig=$(cut -d'|' -f2- "$LASTFAIL_FILE" 2>/dev/null)
    if [ "$sig" = "$old_sig" ] && [ -n "$when" ] \
       && [ $((now - when)) -lt $((NOTIFY_COOLDOWN_MIN * 60)) ]; then
      note "🔕 smoke failure repeat (cooldown ${NOTIFY_COOLDOWN_MIN}m): $sig"
      return 0
    fi
  fi
  printf '%s|%s' "$now" "$sig" > "$LASTFAIL_FILE"

  # Desktop toast — best effort. From a systemd --user unit the session bus
  # address may be absent; derive it from the UID (same shape loginctl uses).
  if command -v notify-send >/dev/null 2>&1; then
    [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] || \
      export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
    notify-send --urgency=critical --expire-time=20000 \
      "🔴 JARVIS rotation smoke" "$verdict" 2>/dev/null || true
    note "🖥️ desktop toast sent"
  fi

  # Telegram — backup_notify.sh convention: source ~/.hermes/.env, sendMessage
  # via curl. Missing creds are a silent skip: a notification channel must
  # never be the thing that fails a health check.
  if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    chat="${TELEGRAM_CHAT_ID:-${TELEGRAM_HOME_CHANNEL:-}}"
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "$chat" ]; then
      http=$(curl -sS --max-time 15 -o /dev/null -w "%{http_code}" \
        -X POST "${TG_API}/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${chat}" \
        --data-urlencode "text=🔴 JARVIS rotation smoke — session death markers in journald

$verdict" \
        2>/dev/null || echo 000)
      note "📣 telegram http=$http"
    fi
  fi
}

# Test hook: run the notify path against a verdict, with stub-able channels.
if [ "${1:-}" = "--notify" ]; then
  shift
  notify_failure "$*"
  exit 0
fi

# JARVIS stopped → nothing to check (also stops a stuck cursor from nagging
# every 5 min while the unit is intentionally off).
state=$("$SYSCTL" --user is-active jarvis-gui.service 2>/dev/null)
if [ "$state" != "active" ]; then
  exit 0
fi

# ── collect the window ───────────────────────────────────────────────────────
last=""
[ -f "$CURSOR_FILE" ] && last=$(tr -d '[:space:]' < "$CURSOR_FILE")

if [ -z "$last" ]; then
  # First run ever (or state wiped): seed the cursor without judging history —
  # a --since window would rescan old incidents and cry wolf once. A quiet
  # window yields no cursor line at all, so stay silent and retry next run.
  log=$("$JCTL" --user -u jarvis-gui --no-pager --show-cursor --since "$SEED_WINDOW" 2>/dev/null) || log=""
  new_cursor=$(printf '%s\n' "$log" | awk '/^-- cursor: /{c=$0} END{print c}' | sed 's/^-- cursor: //')
  if [ -z "$new_cursor" ]; then
    exit 0                       # quiet journal — nothing to pin yet
  fi
  printf '%s' "$new_cursor" > "$CURSOR_FILE"
  note "🧭 cursor seeded — watching from the next window on"
  exit 0
fi

if ! log=$("$JCTL" --user -u jarvis-gui --no-pager --show-cursor --after-cursor "$last" 2>/dev/null); then
  # Stale cursor (journal rotated/vacuumed past it) — fall back to a recent
  # window rather than silently going blind.
  log=$("$JCTL" --user -u jarvis-gui --no-pager --show-cursor --since "-15 min" 2>/dev/null) || log=""
fi
[ -z "$log" ] && exit 0    # nothing new since the last run

new_cursor=$(printf '%s\n' "$log" | awk '/^-- cursor: /{c=$0} END{print c}' | sed 's/^-- cursor: //')
body=$(printf '%s\n' "$log" | grep -v '^-- cursor: ')

# ── analyze the window ───────────────────────────────────────────────────────
verdict=$(printf '%s\n' "$body" | analyze)
rc=$?

case $rc in
  0) case $verdict in
       "no rotation in window") ;;          # idle — stay silent, no spam
       *) note "✅ smoke: $verdict" ;;
     esac ;;
  *) note "❌ smoke: $verdict"
     notify_failure "$verdict" ;;
esac

# ── cursor advance: only after a resolved window ─────────────────────────────
completed=$(printf '%s\n' "$body" | awk '/GoAway received/{g=1} g && /\[JARVIS\] Connected\./{print "y"; exit}')
if [ -n "$completed" ] || ! printf '%s\n' "$body" | grep -qF "GoAway received"; then
  [ -n "$new_cursor" ] && printf '%s' "$new_cursor" > "$CURSOR_FILE"
fi

exit $rc
