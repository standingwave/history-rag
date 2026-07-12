#!/bin/sh
# Kick the launchd refresh and follow it to completion, then print the run's
# stats block. Waits on the driver's `refresh:` summary line — the last thing
# a tick prints, after backup and sync finish.
#   tools/kick.sh [label] [log]
LABEL="${1:-com.user.history-index}"
LOG="${2:-/tmp/history-index.log}"

before=$(grep -c '^refresh:' "$LOG" 2>/dev/null || echo 0)
launchctl kickstart "gui/$(id -u)/$LABEL" || exit 1
printf 'kicked %s, waiting' "$LABEL"
while [ "$(grep -c '^refresh:' "$LOG" 2>/dev/null || echo 0)" -le "$before" ]; do
    printf .
    sleep 2
done
echo
tail -12 "$LOG"
