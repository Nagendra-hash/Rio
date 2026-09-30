#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# jarvis-live-model-watch.sh — auto-recovery watchdog for the Live model
#
# Why: 2026-09-30 gemini-3.1-flash-live-preview had a server-side incident
# (every audio session died with 1011, speech never transcribed) and JARVIS
# was switched to gemini-2.5-flash-native-audio-latest via "live_model" in
# config/api_keys.json. Nobody should have to remember to probe 3.1 and flip
# back by hand — this timer does it.
#
# The timer (systemd/jarvis-live-model-watch.timer) fires every 30 minutes.
# Each run:
#   1. Reads the CURRENT live_model from config/api_keys.json.
#   2. If a CANDIDATE model is configured (default: gemini-3.1-flash-live-
#      preview), probes it with scripts/diag_live_speech_health.py --probe.
#      Probing the model JARVIS is already ON would prove nothing and would
#      disturb the live session's quota for no actionable result.
#   3. Candidate still broken  → log, reset the success streak, exit 0.
#   4. Candidate healthy       → require RECOVERY_HITS consecutive healthy
#      probes (default 2, i.e. ~1h of stability) before flipping, so a
#      one-off good probe during a flapping incident doesn't switch JARVIS
#      onto a model that dies an hour later. On flip: update
#      config/api_keys.json, restart jarvis-gui.service, verify it came
#      back up, and notify (desktop toast + Telegram, same channels as
#      jarvis-rotation-smoke.sh). A flip that fails to come back healthy
#      rolls the config back and says so loudly.
#
# Env overrides (mirror jarvis-rotation-smoke.sh):
#   JARVIS_LMW_CONFIG        api_keys.json path
#   JARVIS_LMW_CANDIDATE     model to probe for recovery (default 3.1)
#   JARVIS_LMW_STATE_FILE    state file (default /tmp/jarvis_live_model_watch.state)
#   JARVIS_LMW_LOG           human-readable log file
#   JARVIS_LMW_PROBE         probe script/binary (stub-able in tests)
#   JARVIS_LMW_SYSTEMCTL     systemctl binary/stub
#   JARVIS_LMW_RECOVERY_HITS consecutive healthy probes required (default 2)
#   JARVIS_LMW_NOTIFY        1 = notify on flip/rollback (default 1)
#   JARVIS_LMW_ENV_FILE      Telegram creds file (default ~/.hermes/.env)
#   JARVIS_LMW_TG_API        Telegram API base (stub-able in tests)
# ─────────────────────────────────────────────────────────────────────────────

CONFIG="${JARVIS_LMW_CONFIG:-$HOME/Mark-LIII/config/api_keys.json}"
CANDIDATE="${JARVIS_LMW_CANDIDATE:-models/gemini-3.1-flash-live-preview}"
STATE_FILE="${JARVIS_LMW_STATE_FILE:-/tmp/jarvis_live_model_watch.state}"
LOG="${JARVIS_LMW_LOG:-/tmp/jarvis_live_model_watch.log}"
PROBE="${JARVIS_LMW_PROBE:-$HOME/Mark-LIII/.venv/bin/python $HOME/Mark-LIII/scripts/diag_live_speech_health.py}"
SYSCTL="${JARVIS_LMW_SYSTEMCTL:-systemctl}"
RECOVERY_HITS="${JARVIS_LMW_RECOVERY_HITS:-2}"
NOTIFY="${JARVIS_LMW_NOTIFY:-1}"
ENV_FILE="${JARVIS_LMW_ENV_FILE:-${HOME}/.hermes/.env}"
TG_API="${JARVIS_LMW_TG_API:-https://api.telegram.org}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
note() { echo "[$(ts)] $*" >> "$LOG"; }

# ── notifications (same channels as jarvis-rotation-smoke.sh) ────────────────
notify() {
  local title="$1" body="$2" http chat
  [ "$NOTIFY" = "1" ] || return 0
  if command -v notify-send >/dev/null 2>&1; then
    [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] || \
      export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
    notify-send --urgency=critical --expire-time=20000 "$title" "$body" 2>/dev/null || true
  fi
  if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    chat="${TELEGRAM_CHAT_ID:-${TELEGRAM_HOME_CHANNEL:-}}"
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "$chat" ]; then
      http=$(curl -sS --max-time 15 -o /dev/null -w "%{http_code}" \
        -X POST "${TG_API}/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${chat}" \
        --data-urlencode "text=$title

$body" \
        2>/dev/null || echo 000)
      note "📣 telegram http=$http"
    fi
  fi
}

# Test hooks: notify path against a canned message.
if [ "${1:-}" = "--notify" ]; then
  shift
  notify "$@"
  exit 0
fi

# ── read the current model ───────────────────────────────────────────────────
current=$(python3 - "$CONFIG" <<'PY'
import json, sys
try:
    m = (json.load(open(sys.argv[1])) or {}).get("live_model")
    print((m or "").strip())
except Exception:
    print("")
PY
)
if [ -z "$current" ]; then
  note "⚠️ cannot read live_model from $CONFIG — skipping (config unchanged since before the switch?)"
  exit 0
fi

# JARVIS back on the candidate already (flipped manually)? Clear state, done.
if [ "$current" = "$CANDIDATE" ]; then
  rm -f "$STATE_FILE"
  exit 0    # silent — steady state, no spam
fi

# ── probe the candidate ──────────────────────────────────────────────────────
if out=$($PROBE --probe "$CANDIDATE" --json 2>/dev/null); then
  verdict="healthy"
else
  verdict="broken"
fi
note "probe $CANDIDATE: $verdict ${out:+| $out}"

if [ "$verdict" != "healthy" ]; then
  rm -f "$STATE_FILE"          # streak resets on any failure
  exit 0
fi

# ── healthy probe: count the streak ──────────────────────────────────────────
hits=0
[ -f "$STATE_FILE" ] && hits=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
case "$hits" in (*[!0-9]*|"") hits=0;; esac
hits=$((hits + 1))
if [ "$hits" -lt "$RECOVERY_HITS" ]; then
  printf '%s' "$hits" > "$STATE_FILE"
  note "✅ candidate healthy $hits/$RECOVERY_HITS — waiting for more confirmations"
  exit 0
fi

# ── flip! update config, restart, verify, roll back on failure ───────────────
note "🎉 candidate healthy $hits/$RECOVERY_HITS — switching live_model back to $CANDIDATE"
cp "$CONFIG" "$CONFIG.bak-lmw" || { note "❌ cannot back up config — NOT flipping"; exit 1; }
python3 - "$CONFIG" "$CANDIDATE" <<'PY' || { note "❌ config write failed — NOT flipping"; exit 1; }
import json, sys
p, model = sys.argv[1], sys.argv[2]
cfg = json.load(open(p))
cfg["live_model"] = model
json.dump(cfg, open(p, "w"), indent=2)
PY

"$SYSCTL" --user restart jarvis-gui.service
sleep 15
state=$("$SYSCTL" --user is-active jarvis-gui.service 2>/dev/null)
if [ "$state" != "active" ]; then
  # Reset the streak AND the backup: the next cycle must re-earn the flip
  # with fresh consecutive confirmations (2026-09-30 dry-run: without this,
  # the persisted 2/2 state made every later run re-flip on ONE probe).
  note "💀 jarvis-gui not active after flip — ROLLING BACK to $current"
  cp "$CONFIG.bak-lmw" "$CONFIG"
  rm -f "$STATE_FILE" "$CONFIG.bak-lmw"
  "$SYSCTL" --user restart jarvis-gui.service
  notify "🔴 JARVIS model flip FAILED" \
    "Switched live_model to $CANDIDATE but the service did not come back. Rolled back to $current — check journalctl --user -u jarvis-gui"
  exit 1
fi
rm -f "$STATE_FILE" "$CONFIG.bak-lmw"
note "✅ flipped live_model → $CANDIDATE; jarvis-gui active"
notify "🟢 JARVIS Live model restored" \
  "gemini-3.1-flash-live-preview passed $RECOVERY_HITS consecutive speech probes. live_model switched back and jarvis-gui restarted cleanly."
exit 0
