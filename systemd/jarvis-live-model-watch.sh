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
#   JARVIS_LMW_INCIDENTS     incident JSONL timeline (default memory/incidents.jsonl)
#   JARVIS_LMW_INCIDENT_ID   incident id stamped on records (default live-20260930-3.1)
# ─────────────────────────────────────────────────────────────────────────────

CONFIG="${JARVIS_LMW_CONFIG:-$HOME/Mark-LIII/config/api_keys.json}"
CANDIDATE="${JARVIS_LMW_CANDIDATE:-models/gemini-3.1-flash-live-preview}"
STATE_FILE="${JARVIS_LMW_STATE_FILE:-/tmp/jarvis_live_model_watch.state}"
LOG="${JARVIS_LMW_LOG:-/tmp/jarvis_live_model_watch.log}"
# Durable incident timeline: every probe outcome is appended here as one JSON
# object per line (memory/incidents.jsonl), so the 2026-09-30 Live-voice
# incident's full history survives /tmp cleanups and can be replayed later.
# Dash-only default (not :-): an explicitly EMPTY value must disable incident
# logging (opt-out), not fall back to the default path.
INCIDENTS="${JARVIS_LMW_INCIDENTS-$HOME/Mark-LIII/memory/incidents.jsonl}"
INCIDENT_ID="${JARVIS_LMW_INCIDENT_ID:-live-20260930-3.1}"
PROBE="${JARVIS_LMW_PROBE:-$HOME/Mark-LIII/.venv/bin/python $HOME/Mark-LIII/scripts/diag_live_speech_health.py}"
SYSCTL="${JARVIS_LMW_SYSTEMCTL:-systemctl}"
RECOVERY_HITS="${JARVIS_LMW_RECOVERY_HITS:-2}"
NOTIFY="${JARVIS_LMW_NOTIFY:-1}"
ENV_FILE="${JARVIS_LMW_ENV_FILE:-${HOME}/.hermes/.env}"
TG_API="${JARVIS_LMW_TG_API:-https://api.telegram.org}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
note() { echo "[$(ts)] $*" >> "$LOG"; }

# Append a machine-readable record to the incident log. Best-effort by design:
# a broken log path must never affect probing or the flip logic, so everything
# runs inside a guarded python heredoc whose failures land in the watch log.
# $1 = event name; $2 = optional CONTROLLED JSON literal (bash never
# interpolates probe output into it). The probe's own --json verdict arrives
# via $LMW_OUT and is parsed in python (last {…} line wins) instead.
record_incident() {
  [ -n "$INCIDENTS" ] || return 0
  LMW_EVENT="$1" LMW_FIELDS="${2:-}" LMW_OUT="${out:-}" \
    LMW_ID="$INCIDENT_ID" LMW_MODEL="$CANDIDATE" \
    python3 - "$INCIDENTS" >>"$LOG" 2>&1 <<'PY' || true
import datetime, json, os, sys
try:
    rec = {
        "event": os.environ["LMW_EVENT"],
        "id": os.environ["LMW_ID"],
        "model": os.environ["LMW_MODEL"],
        "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    raw = os.environ.get("LMW_OUT", "").strip()
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                rec["probe"] = json.loads(line)
                break
            except ValueError:
                pass
    if "probe" not in rec and raw:
        rec["probe_output"] = raw[:300]
    if os.environ.get("LMW_FIELDS"):
        rec.update(json.loads(os.environ["LMW_FIELDS"]))
    path = sys.argv[1]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
except Exception as e:  # logging must never break the probe cycle
    print(f"incident-log append failed: {type(e).__name__}: {e}")
PY
}

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
  record_incident probe_broken
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
  record_incident probe_healthy "{\"streak\": $hits, \"required\": $RECOVERY_HITS}"
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
  record_incident flip_rolled_back "{\"rolled_back_to\": \"$current\"}"
  exit 1
fi
rm -f "$STATE_FILE" "$CONFIG.bak-lmw"
note "✅ flipped live_model → $CANDIDATE; jarvis-gui active"
record_incident recovered "{\"streak\": $hits, \"required\": $RECOVERY_HITS, \"flipped_to\": \"$CANDIDATE\"}"
notify "🟢 JARVIS Live model restored" \
  "gemini-3.1-flash-live-preview passed $RECOVERY_HITS consecutive speech probes. live_model switched back and jarvis-gui restarted cleanly."
exit 0
