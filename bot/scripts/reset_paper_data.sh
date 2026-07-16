#!/usr/bin/env bash
# Archive the current paper-trading ledger/logs and restart with a clean slate.
# Use after a config/strategy change so the paper record evaluates ONLY the
# current configuration (old data is preserved under data/archive_<UTC-stamp>/).
set -euo pipefail

if [ -d /root/S4/bot ]; then cd /root/S4/bot; else cd "$(dirname "$0")/.."; fi

STAMP=$(date -u +%Y%m%d_%H%M%SZ)
ARCH="data/archive_${STAMP}"

RUNNING=0
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet polybot; then
    RUNNING=1
    echo "-> stopping polybot"
    systemctl stop polybot
fi

mkdir -p "$ARCH"
for f in polybot.db fills.csv pnl.csv status.json polybot.log polybot.service.log; do
    if [ -f "data/$f" ]; then
        mv "data/$f" "$ARCH/"
        echo "   archived data/$f"
    fi
done
# rotated log backups too
for f in data/polybot.log.*; do
    [ -f "$f" ] && mv "$f" "$ARCH/" && echo "   archived $f"
done

if [ "$RUNNING" -eq 1 ]; then
    echo "-> starting polybot (fresh ledger)"
    systemctl start polybot
fi

echo
echo "Done. Old paper data preserved in: $(pwd)/$ARCH"
echo "Verify clean: venv/bin/python -m polybot.main pnl"
