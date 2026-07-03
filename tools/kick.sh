#!/bin/sh
# Kick the launchd index refresh and follow it to completion, then print the
# run's per-source stats block. Replaces the kickstart / sleep / tail dance.
#   tools/kick.sh [label] [log]
LABEL="${1:-com.user.history-index}"
LOG="${2:-/tmp/history-index.log}"

before=$(grep -c '^done\.' "$LOG" 2>/dev/null || echo 0)
launchctl kickstart "gui/$(id -u)/$LABEL" || exit 1
printf 'kicked %s, waiting' "$LABEL"
while [ "$(grep -c '^done\.' "$LOG" 2>/dev/null || echo 0)" -le "$before" ]; do
    printf .
    sleep 2
done
echo
tail -9 "$LOG"
