#!/usr/bin/env bash
# hermes_failover_test.sh — weekly chaos test of Hermes' LLM failover chain.
#
# Why: OpenRouter retired the primary :free model mid-flight on 2026-09-24 and
# turns died until it was re-pointed. This test proves the chain actually
# catches such a failure BEFORE a real outage needs it:
#
#   1. Injects a bogus primary model id into ~/.hermes/config.yaml
#      (guaranteed 404 — same failure shape as a retired :free model)
#   2. Runs one real `hermes -z` turn — must recover via the fallback chain
#      (FreeLLMAPI router → OpenRouter ×2 → Gemini)
#   3. Restores the config (trap: restore happens on EVERY exit path)
#   4. Telegrams 🟢/🔴 result + router-journal evidence via `hermes send`
#      (messaging path only — no LLM — so delivery never depends on the chain)
#
# Safety:
#   * flock — never overlaps itself or the Sunday 04:00 backup window (07:00)
#   * leftover-recovery: a state marker survives crashes; next run (or this
#     run's preamble) restores the config first
#   * no gateway restart needed: `hermes -z` reads config per-process
#   * worst case during the ~1-4 min test window a live Telegram/WhatsApp turn
#     sees the bogus primary and — by design — fails over exactly like this
#     test does
#
# Cron (installed on this machine, Sundays 07:00 IST — after UTC-midnight quota
# reset and well clear of the 04:00 backup):
#   0 7 * * 0 ... flock -n /tmp/hermes-failover-test.lock
#     /home/nagendra/Mark-III/scripts/hermes_failover_test.sh \
#     >> /home/nagendra/backups/failover_test.log 2>&1
#
# Exit codes: 0 pass, 1 fail, 2 environment error (skipped, not a chain failure).
set -uo pipefail

HERMES_HOME="${HOME}/.hermes"
CONFIG="${HERMES_HOME}/config.yaml"
STATE_DIR="${HERMES_HOME}/failover-test.state"
STATE_CONFIG="${STATE_DIR}/config.yaml.bak"
STATE_MARKER="${STATE_DIR}/test-in-progress"
BOGUS_MODEL="bogus/failover-test-model-xyz"
TURN_TIMEOUT_S=240
LOG_TAG="[failover-test]"

log() { printf '%s %s\n' "$LOG_TAG" "$*"; }
now() { date -Is; }

# Cron-safe PATH (hermes CLI + its node tooling), same convention as
# hermes_skill_batch.sh / hermes_task.py.
export PATH="${HOME}/.local/bin:${HOME}/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin"
# journalctl --user needs the user bus from cron contexts.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"

command -v hermes >/dev/null || { log "hermes CLI not found — environment error"; exit 2; }
[ -f "$CONFIG" ] || { log "config missing: $CONFIG"; exit 2; }
mkdir -p "$STATE_DIR" || exit 2

TELEGRAM_BODY=""
send_telegram() {
  # Delivery is bot-token based; works with no LLM and no running gateway.
  hermes send -t telegram "$1" >/dev/null 2>&1 || log "Telegram send failed (non-fatal to restore logic)"
}

restore_config() {
  [ -f "$STATE_MARKER" ] || return 0
  if cp "$STATE_CONFIG" "$CONFIG" 2>/dev/null; then
    rm -f "$STATE_MARKER" "$STATE_CONFIG"
    log "$(now) config restored from state copy"
  else
    log "$(now) FATAL: config restore FAILED — manual fix: cp ${STATE_CONFIG} ${CONFIG}"
  fi
}
trap restore_config EXIT INT TERM

# ── 0. Leftover recovery: a previous run may have crashed mid-test ──────────
PRE_NOTE=""
if [ -f "$STATE_MARKER" ]; then
  restore_config
  PRE_NOTE="⚠ previous test run crashed mid-injection; config was restored now. "
fi

# ── 1. Baseline: primary line must be exactly one, and not already bogus ────
N_DEFAULT=$(grep -c '^  default: ' "$CONFIG" || true)
if [ "$N_DEFAULT" != "1" ]; then
  log "config has ${N_DEFAULT} '  default:' lines (expected 1) — skipping to avoid corrupting config"
  send_telegram "🟡 Hermes failover test SKIPPED: config.yaml layout changed (${N_DEFAULT} primary lines). Script needs an update before it can test safely."
  exit 2
fi
if grep -q '^  default: '"${BOGUS_MODEL}"'$' "$CONFIG"; then
  log "config already contains the bogus model (stale state) — restoring first"
  restore_config
fi

# ── 2. Inject the guaranteed-404 primary ────────────────────────────────────
cp "$CONFIG" "$STATE_CONFIG" && touch "$STATE_MARKER"
ORIG_MODEL=$(sed -n 's/^  default: *//p' "$CONFIG")
sed -i 's|^  default: .*|  default: '"${BOGUS_MODEL}"'|' "$CONFIG"
if ! grep -q "$BOGUS_MODEL" "$CONFIG"; then
  log "injection failed — aborting (config untouched? verifying)"
  restore_config
  exit 2
fi
log "$(now) bogus primary injected (was: ${ORIG_MODEL})"

# ── 3. Run one real turn; the ONLY way it can succeed is via fallback ───────
T0=$(date +%s)
OUT=$(timeout "$TURN_TIMEOUT_S" hermes -z "Reply with exactly: FAILOVER_OK" 2>&1)
RC=$?
T1=$(date +%s)
ELAPSED=$((T1 - T0))
restore_config   # restore immediately — trap is only a safety net
log "$(now) turn finished rc=${RC} elapsed=${ELAPSED}s"

# ── 4. Evidence from the router journal (test window) ───────────────────────
EVIDENCE=$(journalctl --user -u freellmapi --since "@$((T0 - 1))" --until "@$((T1 + 1))" --no-pager 2>/dev/null \
  | grep -E '\[Proxy\].*(start|ok|fail)' | tail -6)
[ -n "$EVIDENCE" ] || EVIDENCE="(no router activity seen in the test window — check that freellmapi is actually in the chain)"

# ── 5. Verdict ───────────────────────────────────────────────────────────────
PASS=no
if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q "FAILOVER_OK"; then
  PASS=yes
fi

# Config really back? (belt and braces — restored copy must parse + not bogus)
RESTORED_OK=yes
python3 - "$CONFIG" "$BOGUS_MODEL" <<'PYEOF' || RESTORED_OK=no
import sys, yaml
cfg_path, bogus = sys.argv[1], sys.argv[2]
cfg = yaml.safe_load(open(cfg_path))
model = (cfg.get("model") or {}).get("default", "")
sys.exit(0 if model and bogus not in model else 1)
PYEOF

if [ "$PASS" = yes ]; then
  VERDICT="🟢 *Hermes failover test — PASSED*"
  DETAIL="• Bogus primary (404) → chain caught it
• Recovery time: ${ELAPSED}s (timeout ${TURN_TIMEOUT_S}s)
• Reply: FAILOVER_OK"
else
  VERDICT="🔴 *Hermes failover test — FAILED*"
  DETAIL="• exit=${RC} elapsed=${ELAPSED}s
• output tail: $(printf '%s' "$OUT" | tail -2 | tr '\n' ' ' | cut -c1-300)"
fi

RESTORE_NOTE="✅ config restored (primary: ${ORIG_MODEL})"
[ "$RESTORED_OK" = yes ] || RESTORE_NOTE="🚨 CONFIG RESTORE FAILED — fix ${CONFIG} manually (primary should be: ${ORIG_MODEL})"

TELEGRAM_BODY="${VERDICT}

${PRE_NOTE}${DETAIL}

${EVIDENCE}

Chain: ling-3.0-flash-sante → FreeLLMAPI → OpenRouter×2 → Gemini.
${RESTORE_NOTE}"
send_telegram "$TELEGRAM_BODY"

log "$(now) verdict: PASS=${PASS} restored_ok=${RESTORED_OK}"
[ "$PASS" = yes ] && [ "$RESTORED_OK" = yes ] && exit 0
exit 1
