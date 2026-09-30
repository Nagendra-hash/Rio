#!/bin/bash
# Zombie-session watcher v2: re-reads the pid file EVERY loop (the app restarts
# often), samples socket state + recent log lines, appends to /tmp/zombie_watch.log
echo "$(date '+%H:%M:%S') watcher v2 started" >> /tmp/zombie_watch.log
while true; do
  TS=$(date '+%H:%M:%S')
  APP=$(cat /tmp/jarvis.pid 2>/dev/null)
  if [ -z "$APP" ] || ! kill -0 "$APP" 2>/dev/null; then
    echo "$TS APP DEAD (pid=$APP)" >> /tmp/zombie_watch.log
    sleep 15; continue
  fi
  CW=$(ss -tnp 2>/dev/null | grep "pid=$APP" | grep -c CLOSE-WAIT)
  EST=$(ss -tnp 2>/dev/null | grep "pid=$APP" | grep -c ESTAB)
  SENDQ=$(ss -tnp 2>/dev/null | grep "pid=$APP" | awk '{s+=$2} END {print s+0}')
  RECVQ=$(ss -tnp 2>/dev/null | grep "pid=$APP" | awk '{s+=$1} END {print s+0}')
  STALL=$(tail -30 /tmp/jarvis_live.log 2>/dev/null | grep -c "stalled\|send error\|Reconnecting")
  echo "$TS pid=$APP estab=$EST close_wait=$CW sendq=$SENDQ recvq=$RECVQ stall_lines=$STALL" >> /tmp/zombie_watch.log
  sleep 30
done
