#!/usr/bin/env bash
# =============================================================================
# install_dashboard.sh — Run the web panel (dashboard.py) as a systemd service
# "trader-panel", next to trading-bot. Needs scripts/install_vm.sh first (it
# creates .venv). Read-only: the panel never sends orders or changes the bots.
#
# Listens on 127.0.0.1:8080 only (not reachable from the internet). From your
# computer, open an SSH tunnel and browse http://localhost:8080 :
#   ssh -i CHAVE.key -L 8080:127.0.0.1:8080 ubuntu@IP_DA_VM
#
#   bash scripts/install_dashboard.sh
#   sudo systemctl status trader-panel
#   sudo systemctl disable --now trader-panel   # stop and do not start on boot
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
SERVICE=trader-panel

if [ ! -x "${REPO_DIR}/.venv/bin/python" ]; then
    echo "Run scripts/install_vm.sh first (it creates .venv)."
    exit 1
fi
mkdir -p "${REPO_DIR}/logs"

sudo tee "/etc/systemd/system/${SERVICE}.service" >/dev/null <<UNIT
[Unit]
Description=Trader web panel (read-only, localhost)
After=network-online.target
Wants=network-online.target

[Service]
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${REPO_DIR}/.venv/bin/python -u ${REPO_DIR}/dashboard.py --host 127.0.0.1 --port 8080
StandardOutput=append:${REPO_DIR}/logs/panel.log
StandardError=append:${REPO_DIR}/logs/panel.log
Restart=on-failure
RestartSec=10
# Exit code 2 = bad arguments/password: do not loop
RestartPreventExitStatus=2
KillSignal=SIGTERM
# Keep the trading bot safe on a 1 GB VM
MemoryMax=150M
# The panel only reads files: no new privileges, read-only system
NoNewPrivileges=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE}"
sleep 3
sudo systemctl --no-pager status "${SERVICE}" | head -5
echo
echo "==> No seu computador: ssh -i CHAVE.key -L 8080:127.0.0.1:8080 ${RUN_USER}@IP_DA_VM"
echo "==> Depois abra http://localhost:8080 no navegador."
