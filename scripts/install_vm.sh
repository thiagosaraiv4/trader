#!/usr/bin/env bash
# =============================================================================
# install_vm.sh — Install the headless bot as a systemd service on a Linux VM
# (tested target: Ubuntu 22.04/24.04, e.g. Oracle Cloud Always Free)
# =============================================================================
#
# Run from inside the cloned repo:
#   bash scripts/install_vm.sh
#
# What it does:
#   1. Installs python3-venv (apt)
#   2. Adds 1 GB of swap on small VMs (1 GB RAM micro instances)
#   3. Creates .venv and installs requirements.txt
#   4. Creates the systemd service "trading-bot": starts on boot, restarts on
#      crash, SIGTERM on stop (the bot saves session.json before exiting)
#
# Afterwards:
#   sudo systemctl status trading-bot     # running?
#   tail -f logs/bot.log                  # logs
#   sudo systemctl restart trading-bot    # after a git pull
#   sudo systemctl stop trading-bot       # stop
#
# Use systemd OR scripts/watchdog.sh on a machine — never both
# (a second instance exits anyway because of session.lock).
# =============================================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
SERVICE=trading-bot

if [ "${RUN_USER}" = "root" ]; then
    echo "Run as your normal user (the script calls sudo when needed)."
    exit 1
fi

echo "==> Repo: ${REPO_DIR} | user: ${RUN_USER}"

echo "==> Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git

# Small VMs (1 GB RAM) can run out of memory while installing pandas/numpy
mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
if [ "${mem_kb}" -lt 2000000 ] && ! swapon --show | grep -q .; then
    echo "==> Adding 1 GB swap (VM has $((mem_kb / 1024)) MB RAM)"
    sudo fallocate -l 1G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
fi

echo "==> Creating virtualenv and installing requirements"
python3 -m venv "${REPO_DIR}/.venv"
"${REPO_DIR}/.venv/bin/pip" install -q --upgrade pip
"${REPO_DIR}/.venv/bin/pip" install -q -r "${REPO_DIR}/requirements.txt"
mkdir -p "${REPO_DIR}/logs"

echo "==> Creating systemd service ${SERVICE}"
sudo tee "/etc/systemd/system/${SERVICE}.service" >/dev/null <<EOF
[Unit]
Description=Trading bot (headless paper trading)
After=network-online.target
Wants=network-online.target

[Service]
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${REPO_DIR}/.venv/bin/python -u ${REPO_DIR}/headless_main.py
StandardOutput=append:${REPO_DIR}/logs/bot.log
StandardError=append:${REPO_DIR}/logs/bot.log
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE}"

sleep 3
sudo systemctl --no-pager status "${SERVICE}" | head -5
echo
echo "==> Done. Logs: tail -f ${REPO_DIR}/logs/bot.log"
