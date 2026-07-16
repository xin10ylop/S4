#!/usr/bin/env bash
# Deploy polybot to a fresh (or existing) Ubuntu server, running as root.
#
# What it does:
#   1. Clones the repo to /root/S4 (or pulls latest if already present).
#   2. Creates/refreshes a Python 3.11 venv at /root/S4/bot/venv and installs
#      bot/requirements.txt.
#   3. Installs, enables, and (re)starts the polybot systemd service.
#   4. Prints service status + recent logs.
#
# Usage:
#   POLYBOT_REPO_URL=<git-url> ./deploy_server.sh
# (POLYBOT_REPO_URL is only needed the first time, if /root/S4 doesn't exist yet.)
#
# Secrets: this script never touches secrets. Before flipping to LIVE mode,
# create /root/S4/bot/.env yourself (chmod 600) — see bot/README.md.
set -euo pipefail

REPO_DIR="/root/S4"
REPO_URL="${POLYBOT_REPO_URL:-}"
BRANCH="${POLYBOT_BRANCH:-claude/polymarket-btc-strategy-setup-balexr}"
SERVICE_NAME="polybot"
STATUS_PORT="${POLYBOT_PORT:-8899}"

# pick a python: explicit override, else newest available 3.x
if [ -n "${PYTHON_BIN:-}" ]; then
    :
elif command -v python3.12 >/dev/null 2>&1; then PYTHON_BIN=python3.12
elif command -v python3.11 >/dev/null 2>&1; then PYTHON_BIN=python3.11
else PYTHON_BIN=python3
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: this script expects to run as root (per bot/README.md)." >&2
    exit 1
fi

echo "== polybot deploy =="

if [ -d "$REPO_DIR/.git" ]; then
    echo "-> $REPO_DIR already exists, pulling latest ($BRANCH)"
    git -C "$REPO_DIR" fetch origin "$BRANCH"
    git -C "$REPO_DIR" checkout "$BRANCH"
    git -C "$REPO_DIR" pull --ff-only origin "$BRANCH"
else
    if [ -z "$REPO_URL" ]; then
        echo "ERROR: $REPO_DIR does not exist and POLYBOT_REPO_URL is not set." >&2
        echo "Re-run as: POLYBOT_REPO_URL=<git-url> $0" >&2
        echo "(or manually clone/copy the repo to $REPO_DIR and re-run)" >&2
        exit 1
    fi
    echo "-> cloning $REPO_URL ($BRANCH) to $REPO_DIR"
    git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$REPO_DIR"
fi

cd "$REPO_DIR/bot"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "ERROR: $PYTHON_BIN not found. Install Python 3.11 (e.g. 'apt-get install -y python3.11 python3.11-venv') and re-run." >&2
    exit 1
fi

echo "-> creating/refreshing venv at $REPO_DIR/bot/venv"
"$PYTHON_BIN" -m venv venv
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

mkdir -p data

if [ ! -f .env ]; then
    cat <<'EOF'
-> NOTE: bot/.env not found. PAPER mode (the default) does not need it.
   Before ever setting mode.paper: false in config.yaml, create bot/.env
   (chmod 600, NEVER committed) with:
     POLYBOT_LIVE=1
     POLYBOT_PK=<funded wallet private key>
     POLYBOT_FUNDER=<proxy wallet address; required for signature_type 1 or 2>
     POLYBOT_SIGNATURE_TYPE=<0=EOA, 1=email/magic proxy, 2=browser-wallet proxy>
EOF
fi

echo "-> installing systemd unit"
cp scripts/polybot.service /etc/systemd/system/${SERVICE_NAME}.service
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo "-> installing watchdog (restarts polybot if it hangs or dies silently)"
cp scripts/polybot-watchdog.service /etc/systemd/system/polybot-watchdog.service
cp scripts/polybot-watchdog.timer /etc/systemd/system/polybot-watchdog.timer
systemctl daemon-reload
systemctl enable --now polybot-watchdog.timer

sleep 2
echo
echo "== systemctl status =="
systemctl status "${SERVICE_NAME}" --no-pager -l || true

echo
echo "== recent logs =="
journalctl -u "${SERVICE_NAME}" -n 30 --no-pager || true

echo
echo "Deploy complete."
echo "Check status: curl -s http://127.0.0.1:${STATUS_PORT}/status | python3 -m json.tool"
echo "Or:           cd $REPO_DIR/bot && venv/bin/python -m polybot.main status"
