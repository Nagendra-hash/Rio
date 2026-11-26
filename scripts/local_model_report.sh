#!/usr/bin/env bash
# local_model_report.sh — periodic Hermes/JARVIS health report to Telegram,
# drafted by the LOCAL model (Qwen2.5-Coder-3B GGUF, core/llm_local.py) when
# RAM allows — proving the local rung end-to-end on every run.
#
# Design notes (mirrors hermes_failover_test.sh / backup_notify.sh):
#   * flock -n so overlapping runs (slow model load) can't pile up
#   * cron-safe PATH + XDG_RUNTIME_DIR for `systemctl --user` probes
#   * the local-model draft is BEST-EFFORT and hard-bounded by `timeout`:
#     a cold GGUF load takes ~150s on this box; if it stalls or RAM is low,
#     the plain (probe-only) report still goes out. Deliverability > prose.
#   * send via `hermes send -t telegram` (project convention), curl fallback
#
# Usage: local_model_report.sh [--no-llm]     # --no-llm: skip model drafting
# Cron (daily 09:15):
#   15 9 * * * ... flock -n /tmp/jarvis-local-report.lock \
#     /home/nagendra/Mark-III/scripts/local_model_report.sh >> \
#     /home/nagendra/backups/local_report.log 2>&1

LOG="${HOME}/backups/local_report.log"
NO_LLM=0
[ "${1:-}" = "--no-llm" ] && NO_LLM=1

export PATH="${HOME}/.local/bin:${HOME}/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"

PY="${HOME}/Mark-III/.venv/bin/python"
[ -x "$PY" ] || PY=python3

# ── 1. probes + (best-effort) local-model draft ──────────────────────────────
# Python prints the final message on stdout. LLM drafting is skipped when RAM
# is tight (core/llm_local.ram_ok) or the model file is missing; the plain
# report always includes the local rung's own status either way.
MSG="$(timeout 420 "$PY" - <<PYEOF
import sys
sys.path.insert(0, "${HOME}/Mark-III")
from actions.hermes_health import hermes_health
from core import llm_local

plain = hermes_health({})

status = llm_local.status()
tail = (f"\\n🤖 Local rung: {'configured' if status['configured'] else 'MISSING'}"
        f", RAM free {status['ram_available_mb']} MB"
        f" (min {status['min_ram_mb']}), model {'loaded' if status['model_loaded'] else 'not loaded'}")

draft = None
if status['configured'] and status['ram_ok']:
    try:
        draft = llm_local.generate(
            "Condense this health report into a short Telegram message "
            "(max 8 lines, one emoji per line, plain text). Keep every fact, "
            "change nothing, add nothing.\\n\\n" + plain + tail,
            system="You are a terse status reporter. Output only the message.",
            max_tokens=220, temperature=0.2)
    except Exception as e:
        draft = None
        tail += f", draft failed: {str(e)[:60]}"
elif status['configured']:
    tail += " — RAM too low to draft with the model, sending probe text"
else:
    tail += " — model file missing"

out = (draft + "\\n— drafted by local Qwen2.5-Coder-3B (Q4_K_M)") if draft else (plain + tail)
print(out)
PYEOF
)" 2>>"$LOG"
RC=$?

# ── 2. fallbacks: plain report without the model, then a hard-failure note ───
if [ $RC -ne 0 ] || [ -z "$MSG" ]; then
  echo "[report] llm path failed rc=$RC — retrying probe-only" >> "$LOG"
  MSG="$("$PY" - <<PYEOF
import sys
sys.path.insert(0, "${HOME}/Mark-III")
from actions.hermes_health import hermes_health
print(hermes_health({}) + "\\n⚠️ local-model draft failed — probe-only report")
PYEOF
)" 2>>"$LOG" || MSG="🔴 local_model_report: probes AND draft both failed (rc=$RC) — check ~/backups/local_report.log"
fi

# ── 3. deliver: hermes send first, curl fallback ─────────────────────────────
if command -v hermes >/dev/null 2>&1; then
  if hermes send -t telegram "$MSG" >>"$LOG" 2>&1; then
    echo "[report] $(date '+%F %T') sent via hermes ($(echo "$MSG" | wc -l) lines)" >> "$LOG"
    exit 0
  fi
  echo "[report] hermes send failed — curl fallback" >> "$LOG"
fi

ENV_FILE="${HOME}/.hermes/.env"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  CHAT="${TELEGRAM_CHAT_ID:-${TELEGRAM_HOME_CHANNEL:-}}"
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "$CHAT" ]; then
    HTTP="$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' -X POST \
      "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${CHAT}" --data-urlencode "text=${MSG}" 2>/dev/null || echo 000)"
    echo "[report] $(date '+%F %T') sent via curl http=$HTTP" >> "$LOG"
    exit 0
  fi
fi
echo "[report] $(date '+%F %T') UNDELIVERED: no hermes CLI and no Telegram creds" >> "$LOG"
exit 1
