#!/usr/bin/env bash
# =============================================================================
# install_maker.sh — Run the paper maker bot (maker_bot.py) as a systemd
# service "maker-bot", next to trading-bot. Needs scripts/install_vm.sh first
# (it creates .venv). No API key, no orders: public market data only.
#
#   bash scripts/install_maker.sh
#   tail -f logs/maker.log
#   .venv/bin/python run_maker_report.py
#   sudo systemctl stop maker-bot          # stop
#   sudo systemctl disable --now maker-bot # stop and do not start on boot
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
SERVICE=maker-bot

if [ ! -x "${REPO_DIR}/.venv/bin/python" ]; then
    echo "Run scripts/install_vm.sh first (it creates .venv)."
    exit 1
fi
mkdir -p "${REPO_DIR}/logs"

sudo tee "/etc/systemd/system/${SERVICE}.service" >/dev/null <<UNIT
[Unit]
Description=Paper maker bot (BTC/FDUSD, simulated orders)
After=network-online.target
Wants=network-online.target

[Service]
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${REPO_DIR}/.venv/bin/python -u ${REPO_DIR}/maker_bot.py
StandardOutput=append:${REPO_DIR}/logs/maker.log
StandardError=append:${REPO_DIR}/logs/maker.log
Restart=on-failure
RestartSec=30
# Exit code 2 = symbol not available: do not loop
RestartPreventExitStatus=2
KillSignal=SIGTERM
TimeoutStopSec=30
# Keep the main bot safe on a 1 GB VM
MemoryMax=300M

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE}"
sleep 5
sudo systemctl --no-pager status "${SERVICE}" | head -5
echo
echo "==> Logs: tail -f ${REPO_DIR}/logs/maker.log"
echo "==> Report: ${REPO_DIR}/.venv/bin/python ${REPO_DIR}/run_maker_report.py"
