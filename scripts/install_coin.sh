#!/usr/bin/env bash
# =============================================================================
# install_coin.sh — Run trend_v4 on more coins, one paper bot per coin, next
# to the BTC bot (trading-bot). Needs scripts/install_vm.sh first.
#
# Each coin gets its own service, folder and log, and its own US$ 10,000
# paper account:
#   service  trading-bot-eth
#   folder   bots/eth/   (session.json, live_bars.csv, archive/)
#   log      logs/bot-eth.log
# All coins trade the repository's strategy.json (same parameters as BTC);
# put a strategy.json inside bots/<coin>/ to give one coin its own.
#
#   bash scripts/install_coin.sh ETH SOL BNB
#   sudo systemctl status trading-bot-eth
#   tail -f logs/bot-eth.log
#   sudo systemctl disable --now trading-bot-eth   # stop one coin
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"

if [ "$#" -eq 0 ]; then
    echo "Usage: bash scripts/install_coin.sh ETH [SOL BNB ...]"
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
mkdir -p "${REPO_DIR}/logs"

for RAW in "$@"; do
    COIN="$(echo "${RAW}" | tr '[:lower:]' '[:upper:]')"
    if ! [[ "${COIN}" =~ ^[A-Z0-9]{2,10}$ ]] || [ "${COIN}" = "BTC" ]; then
        echo "Skipping '${RAW}': use a coin symbol like ETH (BTC is trading-bot)."
        continue
    fi
    ID="$(echo "${COIN}" | tr '[:upper:]' '[:lower:]')"
    SERVICE="trading-bot-${ID}"
    DATA_DIR="${REPO_DIR}/bots/${ID}"
    mkdir -p "${DATA_DIR}"

    echo "==> ${COIN}/USDT → ${SERVICE} (bots/${ID}, logs/bot-${ID}.log)"
    sudo tee "/etc/systemd/system/${SERVICE}.service" >/dev/null <<UNIT
[Unit]
Description=Trading bot ${COIN}/USDT (headless paper trading)
After=network-online.target
Wants=network-online.target

[Service]
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=TRADER_SYMBOL=${COIN}/USDT
Environment=TRADER_DATA_DIR=${DATA_DIR}
ExecStart=${REPO_DIR}/.venv/bin/python -u ${REPO_DIR}/headless_main.py
StandardOutput=append:${REPO_DIR}/logs/bot-${ID}.log
StandardError=append:${REPO_DIR}/logs/bot-${ID}.log
Restart=on-failure
RestartSec=10
# Exit code 2 = invalid strategy.json or a session of another coin
RestartPreventExitStatus=2
KillSignal=SIGTERM
TimeoutStopSec=40
# Keep the BTC bot safe on a 1 GB VM
MemoryMax=300M

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable --now "${SERVICE}"
done

sleep 5
for RAW in "$@"; do
    ID="$(echo "${RAW}" | tr '[:upper:]' '[:lower:]')"
    [ "${ID}" = "btc" ] && continue
    printf '%-20s %s\n' "trading-bot-${ID}" "$(systemctl is-active "trading-bot-${ID}" 2>/dev/null || true)"
done
echo
echo "==> Logs: tail -f ${REPO_DIR}/logs/bot-<moeda>.log   (o painel mostra todas as moedas)"
