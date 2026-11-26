#!/usr/bin/env bash
# Cold-boot verification for hermes-gateway (added 2026-10-04).
#
# Background: the 2026-10-04 08:44 boot timed out loading the telegram-platform
# plugin at the 10s default → "No adapter available for telegram" all morning →
# zero replies. Fixes under test:
#   1. config.yaml: plugins.load_timeout_seconds: 90
#   2. cron/scheduler_worker_env.py: pin runtime site-packages onto worker
#      PYTHONPATH (ruamel crash fix)
#
# Runs as a user systemd oneshot (hermes-coldboot-verify.service) after
# hermes-gateway.service at every boot. Report:
#   ~/.hermes/logs/coldboot-verify-<timestamp>.log
# Read it after boot:  cat ~/.hermes/logs/coldboot-verify-*.log
#
# All checks are scoped to the CURRENT gateway process generation via its
# ActiveEnterTimestamp, and only lines that BEGIN with a timestamp are scanned
# (traceback continuation lines have no timestamp and must not leak in).
set -u

LOG_DIR=/home/nagendra/.hermes/logs
LOG="$LOG_DIR/coldboot-verify-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$LOG_DIR"

{
  echo "=== hermes-gateway cold-boot verification — $(date) — boot $(uptime -s 2>/dev/null || echo '?') ==="

  # 1. Wait up to 240s for the gateway to go active.
  ok=0
  for _ in $(seq 1 48); do
    if systemctl --user is-active --quiet hermes-gateway.service 2>/dev/null; then ok=1; break; fi
    sleep 5
  done
  if [ "$ok" = 1 ]; then
    echo "PASS: hermes-gateway.service active"
  else
    echo "FAIL: hermes-gateway.service not active 240s after boot"
  fi

  GEN_TS=$(systemctl --user show hermes-gateway -p ActiveEnterTimestamp --value 2>/dev/null)
  GEN_CUTOFF=$(date -d "$GEN_TS" "+%Y-%m-%d %H:%M:%S" 2>/dev/null || echo "")
  GEN_DATE=$(date -d "$GEN_TS" "+%Y-%m-%d" 2>/dev/null || echo "")
  echo "current gateway generation started: ${GEN_TS:-unknown}"

  # Timestamped log lines from the current generation onward (agent.log has the
  # INFO lines the journal does not carry; continuation lines are excluded by
  # the anchored date grep).
  gen_lines() {
    grep -hE "^${GEN_DATE}" "$LOG_DIR/agent.log" "$LOG_DIR/gateway.log" 2>/dev/null \
      | awk -v c="$GEN_CUTOFF" 'c == "" || $0 >= c'
  }
  journal_since_gen() {
    journalctl --user -u hermes-gateway.service --since "${GEN_TS:-today}" --no-pager 2>/dev/null
  }

  # 2. Poll up to 150s for the telegram adapter to connect (cold-cache import
  #    can take a while even under the 90s deadline).
  connected=0
  for _ in $(seq 1 30); do
    if gen_lines | grep -qiE "Connected to Telegram \(polling mode\)|✓ telegram connected"; then
      connected=1; break
    fi
    sleep 5
  done

  echo "--- 3. telegram plugin load (90s timeout fix) ---"
  if { gen_lines; journal_since_gen; } | grep -iE "load timed out|No adapter available for telegram|register_platform\(\) after its load timed out"; then
    echo "FAIL: telegram plugin load timeout / missing adapter (see lines above)"
  else
    echo "PASS: no plugin-load timeout, no 'No adapter available for telegram'"
  fi

  echo "--- 4. telegram adapter connected ---"
  if [ "$connected" = 1 ]; then
    gen_lines | grep -iE "Connected to Telegram \(polling mode\)|✓ telegram connected" | head -2
    echo "PASS: telegram adapter connected"
  else
    echo "FAIL: telegram adapter never connected within 150s of gateway start"
  fi

  echo "--- 5. ruamel / import crashes (worker-env pin fix) ---"
  if { gen_lines; journal_since_gen; } | grep -iE "ModuleNotFoundError|No module named 'ruamel'"; then
    echo "FAIL: import crash in gateway/worker (see lines above)"
  else
    echo "PASS: no ModuleNotFoundError / ruamel crash"
  fi

  echo "--- 6. cron scheduler ---"
  if gen_lines | grep -qiE "In-process cron scheduler started"; then
    echo "PASS: in-process cron scheduler started"
  else
    echo "WARN: cron scheduler start line not found in this generation"
  fi

  echo "=== end — $(date) ==="
} >> "$LOG" 2>&1
