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

if [ "${RUN_USER}" = "root" ]; then
    echo "Run as your normal user (e.g. ubuntu), not root: the panel must not run as root."
    exit 1
fi
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
Environment=PYTHONDONTWRITEBYTECODE=1
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
# The panel only reads files: the whole disk (home included) is read-only
# for it, so a bug in it cannot change the bots' code or data. The log file
# above is opened by systemd before these apply.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=true
RestrictSUIDSGID=true
LockPersonality=true
CapabilityBoundingSet=

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
