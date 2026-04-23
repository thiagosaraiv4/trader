#!/usr/bin/env bash
# =============================================================================
# watchdog.sh — Bot Watchdog + Keep-Alive for GitHub Codespaces
# =============================================================================
#
# Features:
#   1. Auto-restarts the bot if it crashes
#   2. Sends periodic keep-alive pings to prevent Codespace idle timeout
#   3. Logs all events to /workspaces/pasta-gemini-trade/logs/watchdog.log
#   4. Saves PID for external monitoring
#
# Usage:
#   ./scripts/watchdog.sh start   — Start bot + watchdog (background)
#   ./scripts/watchdog.sh stop    — Graceful shutdown
#   ./scripts/watchdog.sh status  — Check if running
#   ./scripts/watchdog.sh logs    — Tail bot logs
# =============================================================================

set -euo pipefail

WORKDIR="/workspaces/pasta-gemini-trade"
LOG_DIR="${WORKDIR}/logs"
BOT_LOG="${LOG_DIR}/bot.log"
WATCHDOG_LOG="${LOG_DIR}/watchdog.log"
PID_FILE="${LOG_DIR}/watchdog.pid"
BOT_PID_FILE="${LOG_DIR}/bot.pid"

# Keep-alive interval (seconds) — ping every 10 minutes to prevent idle timeout
KEEPALIVE_INTERVAL=600
# Max restart attempts before giving up
MAX_RESTARTS=50
# Delay between restarts (seconds)
RESTART_DELAY=10

mkdir -p "${LOG_DIR}"

log_msg() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $1" >> "${WATCHDOG_LOG}"
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $1"
}

start_bot() {
    cd "${WORKDIR}"
    python -u headless_main.py >> "${BOT_LOG}" 2>&1 &
    local pid=$!
    echo "${pid}" > "${BOT_PID_FILE}"
    log_msg "BOT_STARTED pid=${pid}"
    echo "${pid}"
}

keepalive_loop() {
    # Sends minimal activity to prevent Codespace idle detection
    while true; do
        sleep "${KEEPALIVE_INTERVAL}"
        # Touch a file and do a minimal git operation to register activity
        touch "${LOG_DIR}/.keepalive"
        log_msg "KEEPALIVE_PING"
    done
}

do_start() {
    # Check if already running
    if [ -f "${PID_FILE}" ]; then
        local old_pid
        old_pid=$(cat "${PID_FILE}")
        if kill -0 "${old_pid}" 2>/dev/null; then
            echo "Watchdog already running (pid=${old_pid}). Use 'stop' first."
            exit 1
        fi
    fi

    log_msg "========================================="
    log_msg "WATCHDOG_STARTING"
    log_msg "========================================="

    # Save watchdog PID (the subshell below)
    echo $$ > "${PID_FILE}"

    # Start keep-alive in background
    keepalive_loop &
    local keepalive_pid=$!
    log_msg "KEEPALIVE_STARTED pid=${keepalive_pid}"

    # Main watchdog loop — restart bot on crash
    local restart_count=0

    while [ "${restart_count}" -lt "${MAX_RESTARTS}" ]; do
        local bot_pid
        bot_pid=$(start_bot)

        # Wait for bot to exit
        set +e
        wait "${bot_pid}"
        local exit_code=$?
        set -e

        log_msg "BOT_EXITED code=${exit_code} restarts=${restart_count}"

        # If clean exit (0 or SIGTERM=143), don't restart
        if [ "${exit_code}" -eq 0 ] || [ "${exit_code}" -eq 143 ]; then
            log_msg "CLEAN_EXIT — not restarting"
            break
        fi

        restart_count=$((restart_count + 1))
        log_msg "RESTARTING in ${RESTART_DELAY}s (attempt ${restart_count}/${MAX_RESTARTS})"
        sleep "${RESTART_DELAY}"
    done

    if [ "${restart_count}" -ge "${MAX_RESTARTS}" ]; then
        log_msg "MAX_RESTARTS_REACHED — giving up"
    fi

    # Cleanup
    kill "${keepalive_pid}" 2>/dev/null || true
    rm -f "${PID_FILE}" "${BOT_PID_FILE}"
    log_msg "WATCHDOG_STOPPED"
}

do_stop() {
    if [ ! -f "${BOT_PID_FILE}" ]; then
        echo "No bot PID file found."
        exit 1
    fi

    local bot_pid
    bot_pid=$(cat "${BOT_PID_FILE}")

    log_msg "STOP_REQUESTED bot_pid=${bot_pid}"

    # Send SIGTERM for graceful shutdown (saves session)
    if kill -TERM "${bot_pid}" 2>/dev/null; then
        echo "Sent SIGTERM to bot (pid=${bot_pid}). Waiting for graceful shutdown..."
        # Wait up to 30 seconds
        for i in $(seq 1 30); do
            if ! kill -0 "${bot_pid}" 2>/dev/null; then
                echo "Bot stopped gracefully."
                break
            fi
            sleep 1
        done

        # Force kill if still running
        if kill -0 "${bot_pid}" 2>/dev/null; then
            echo "Force killing bot..."
            kill -9 "${bot_pid}" 2>/dev/null || true
        fi
    else
        echo "Bot process not found (pid=${bot_pid})"
    fi

    # Kill watchdog
    if [ -f "${PID_FILE}" ]; then
        local wd_pid
        wd_pid=$(cat "${PID_FILE}")
        kill -TERM "${wd_pid}" 2>/dev/null || true
        # Kill all children (keepalive loop)
        pkill -P "${wd_pid}" 2>/dev/null || true
    fi

    rm -f "${PID_FILE}" "${BOT_PID_FILE}"
    log_msg "ALL_STOPPED"
    echo "All processes stopped."
}

do_status() {
    echo "=== Trading Bot Status ==="

    if [ -f "${BOT_PID_FILE}" ]; then
        local bot_pid
        bot_pid=$(cat "${BOT_PID_FILE}")
        if kill -0 "${bot_pid}" 2>/dev/null; then
            echo "✅ Bot:      RUNNING (pid=${bot_pid})"
        else
            echo "❌ Bot:      DEAD (stale pid=${bot_pid})"
        fi
    else
        echo "❌ Bot:      NOT RUNNING"
    fi

    if [ -f "${PID_FILE}" ]; then
        local wd_pid
        wd_pid=$(cat "${PID_FILE}")
        if kill -0 "${wd_pid}" 2>/dev/null; then
            echo "✅ Watchdog: RUNNING (pid=${wd_pid})"
        else
            echo "❌ Watchdog: DEAD (stale pid=${wd_pid})"
        fi
    else
        echo "❌ Watchdog: NOT RUNNING"
    fi

    if [ -f "${BOT_LOG}" ]; then
        echo ""
        echo "=== Last 5 bot log lines ==="
        tail -5 "${BOT_LOG}"
    fi

    if [ -f "${WATCHDOG_LOG}" ]; then
        echo ""
        echo "=== Last 5 watchdog events ==="
        tail -5 "${WATCHDOG_LOG}"
    fi
}

do_logs() {
    if [ -f "${BOT_LOG}" ]; then
        tail -f "${BOT_LOG}"
    else
        echo "No bot log found at ${BOT_LOG}"
    fi
}

# =============================================================================
# CLI Router
# =============================================================================
case "${1:-start}" in
    start)
        do_start
        ;;
    stop)
        do_stop
        ;;
    status)
        do_status
        ;;
    logs)
        do_logs
        ;;
    *)
        echo "Usage: $0 {start|stop|status|logs}"
        exit 1
        ;;
esac
