#!/usr/bin/env bash
# =============================================================================
# install_bots.sh — Run every coin's paper bot in ONE process (service
# "trading-bots"), instead of one service per coin. Same strategy, same
# accounts and files; ~100 MB per coin less memory on the 1 GB VM.
#
#   bash scripts/install_bots.sh BTC ETH SOL BNB XLM TRX NEAR DOGE AVAX
#
# Each coin keeps its paper account and files: BTC at the repository root
# (session.json, live_bars.csv, logs/bot.log), the others in bots/<coin>/ and
# logs/bot-<coin>.log. logs/bots.log has every coin's lines.
# The per-coin services (trading-bot, trading-bot-<coin>) are stopped and
# disabled first, so no coin ever runs twice. Run it again with another
# list to add or remove coins (a removed coin keeps its files).
#
#   sudo systemctl status trading-bots
#   tail -f logs/bots.log
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
SERVICE=trading-bots

if [ "$#" -eq 0 ]; then
    echo "Usage: bash scripts/install_bots.sh BTC ETH SOL ..."
    exit 1
fi
if [ "${RUN_USER}" = "root" ]; then
    echo "Run as your normal user (e.g. ubuntu), not root."
    exit 1
fi
if [ ! -x "${REPO_DIR}/.venv/bin/python" ]; then
    echo "Run scripts/install_vm.sh first (it creates .venv)."
    exit 1
fi

COINS=""
for RAW in "$@"; do
    COIN="$(echo "${RAW}" | tr '[:lower:]' '[:upper:]')"
    if ! [[ "${COIN}" =~ ^[A-Z0-9]{2,10}$ ]]; then
        echo "Invalid coin '${RAW}' (use symbols like BTC ETH)."
        exit 1
    fi
    COINS="${COINS:+${COINS},}${COIN}"
done
mkdir -p "${REPO_DIR}/logs" "${REPO_DIR}/bots"

# One process per coin → stop them (each saves its session on SIGTERM)
for UNIT in $(systemctl list-unit-files --type=service --no-legend 'trading-bot.service' 'trading-bot-*.service' 2>/dev/null | awk '{print $1}'); do
    echo "==> Stopping ${UNIT} (its coin moves into ${SERVICE})"
    sudo systemctl disable --now "${UNIT}" || true
    case "${UNIT}" in
        trading-bot-*) sudo rm -f "/etc/systemd/system/${UNIT}" ;;
    esac
done

echo "==> ${SERVICE}: ${COINS}"
sudo tee "/etc/systemd/system/${SERVICE}.service" >/dev/null <<UNIT
[Unit]
Description=Trading bots ${COINS} (headless paper trading, one process)
After=network-online.target
Wants=network-online.target

[Service]
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${REPO_DIR}/.venv/bin/python -u ${REPO_DIR}/headless_main.py --coins ${COINS}
StandardOutput=append:${REPO_DIR}/logs/bots.log
StandardError=append:${REPO_DIR}/logs/bots.log
Restart=on-failure
RestartSec=10
# Exit code 2 = invalid strategy.json or no coin could start
RestartPreventExitStatus=2
KillSignal=SIGTERM
TimeoutStopSec=60
MemoryMax=500M

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE}"
sudo systemctl restart "${SERVICE}"
sleep 8
sudo systemctl --no-pager status "${SERVICE}" | head -5
echo
grep -a '"event": "bots_started"\|coin skipped\|coin NOT started' "${REPO_DIR}/logs/bots.log" | tail -3 || true
echo
echo "==> Logs: tail -f ${REPO_DIR}/logs/bots.log   (por moeda: logs/bot.log, logs/bot-<moeda>.log)"
