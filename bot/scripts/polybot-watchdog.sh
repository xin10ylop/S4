#!/usr/bin/env bash
# Watchdog for polybot: catches the case where the service dies silently
# (e.g. OOM-killed) without systemd noticing, or isn't running at all.
# Intended to be run periodically via polybot-watchdog.timer.
#
# Healthy: exits 0 with no output.
# Unhealthy: restarts polybot and appends a UTC-timestamped reason line to
# /root/S4/bot/data/watchdog.log.
set -euo pipefail

BOT_DIR="/root/S4/bot"
STATUS_FILE="${BOT_DIR}/data/status.json"
LOG_FILE="${BOT_DIR}/data/watchdog.log"
MAX_AGE_SEC=120
SERVICE_NAME="polybot"

# Guard: only run on the actual server deployment.
if [ ! -d "$BOT_DIR" ]; then
    exit 0
fi

log_and_restart() {
    local reason="$1"
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ${reason}" >> "$LOG_FILE"
    systemctl restart "$SERVICE_NAME"
}

if systemctl is-active --quiet "$SERVICE_NAME"; then
    if [ ! -f "$STATUS_FILE" ]; then
        log_and_restart "status.json missing while polybot is active; restarting"
        exit 0
    fi

    NOW_EPOCH=$(date -u +%s)
    FILE_EPOCH=$(stat -c %Y "$STATUS_FILE")
    AGE=$(( NOW_EPOCH - FILE_EPOCH ))

    if [ "$AGE" -gt "$MAX_AGE_SEC" ]; then
        log_and_restart "status.json stale (${AGE}s old, limit ${MAX_AGE_SEC}s) while polybot is active (hung process); restarting"
        exit 0
    fi
else
    log_and_restart "polybot service is not active; restarting"
    exit 0
fi

exit 0
