#!/usr/bin/env bash
# =============================================================================
# watchdog.sh — Bot supervisor for GitHub Codespaces
# =============================================================================
#
# Features:
#   1. Auto-restarts the bot if it crashes
#   2. Graceful stop (SIGTERM → bot saves session.json before exiting)
#   3. Logs all events to <repo>/logs/watchdog.log
#   4. Saves PIDs for external monitoring
#
# NOTE: this does NOT keep the Codespace awake. Codespaces stop after the idle
# timeout (max 240 min, set at github.com/settings/codespaces) when nobody is
# interacting with them. The bot resumes from session.json on the next start.
#
# Usage:
#   ./scripts/watchdog.sh start   — Start bot + watchdog (foreground; use nohup/setsid for background)
#   ./scripts/watchdog.sh stop    — Graceful shutdown
#   ./scripts/watchdog.sh status  — Check if running
#   ./scripts/watchdog.sh logs    — Tail bot logs
# =============================================================================

set -euo pipefail

# Auto-detect workspace root (works regardless of repo/folder name)
WORKDIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${WORKDIR}/logs"
BOT_LOG="${LOG_DIR}/bot.log"
WATCHDOG_LOG="${LOG_DIR}/watchdog.log"
PID_FILE="${LOG_DIR}/watchdog.pid"
BOT_PID_FILE="${LOG_DIR}/bot.pid"

# Max consecutive restart attempts before giving up
MAX_RESTARTS=50
# A bot that ran at least this long (seconds) resets the restart counter
STABLE_RUN_SEC=600
# Delay between restarts (seconds)
RESTART_DELAY=10

BOT_PID=""
STOPPING=0

mkdir -p "${LOG_DIR}"

log_msg() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $1" >> "${WATCHDOG_LOG}"
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $1"
}

# True if $1 is a live PID whose command line contains $2.
# PID files survive Codespace restarts, so a bare `kill -0` could match an
# unrelated process that reused the PID.
pid_matches() {
    local pid="$1" pattern="$2"
    [ -n "${pid}" ] || return 1
    kill -0 "${pid}" 2>/dev/null || return 1
    tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null | grep -q -- "${pattern}"
}

read_pid() {
    cat "$1" 2>/dev/null || true
}

# Runs in the watchdog shell itself (no command substitution), so the bot is
# a child of this shell and `wait` works on it.
start_bot() {
    cd "${WORKDIR}"
    python -u headless_main.py >> "${BOT_LOG}" 2>&1 &
    BOT_PID=$!
    echo "${BOT_PID}" > "${BOT_PID_FILE}"
    log_msg "BOT_STARTED pid=${BOT_PID}"
}

on_stop_signal() {
    STOPPING=1
    log_msg "WATCHDOG_STOP_SIGNAL — forwarding SIGTERM to bot"
    if [ -n "${BOT_PID}" ]; then
        kill -TERM "${BOT_PID}" 2>/dev/null || true
    fi
}

do_start() {
    local old_pid
    old_pid=$(read_pid "${PID_FILE}")
    # After a Codespace restart the stale PID can even be our own
    if [ "${old_pid}" != "$$" ] && pid_matches "${old_pid}" "watchdog.sh"; then
        echo "Watchdog already running (pid=${old_pid}). Use 'stop' first."
        exit 1
    fi

    log_msg "========================================="
    log_msg "WATCHDOG_STARTING pid=$$"
    log_msg "========================================="

    echo $$ > "${PID_FILE}"
    trap on_stop_signal TERM INT

    local restart_count=0
    local exit_code started_at ran_for

    while [ "${STOPPING}" -eq 0 ] && [ "${restart_count}" -lt "${MAX_RESTARTS}" ]; do
        start_bot
        started_at=$(date +%s)

        # `wait` returns early when a trapped signal arrives; keep waiting
        # until the bot itself has exited so it can finish saving the session.
        set +e
        wait "${BOT_PID}"
        exit_code=$?
        while kill -0 "${BOT_PID}" 2>/dev/null; do
            wait "${BOT_PID}"
            exit_code=$?
        done
        set -e

        ran_for=$(( $(date +%s) - started_at ))
        log_msg "BOT_EXITED code=${exit_code} ran_for=${ran_for}s restarts=${restart_count}"

        # Stop requested, or clean exit (0 or SIGTERM=143): don't restart
        if [ "${STOPPING}" -eq 1 ] || [ "${exit_code}" -eq 0 ] || [ "${exit_code}" -eq 143 ]; then
            log_msg "CLEAN_EXIT — not restarting"
            break
        fi

        if [ "${ran_for}" -ge "${STABLE_RUN_SEC}" ]; then
            restart_count=0
        fi
        restart_count=$((restart_count + 1))
        log_msg "RESTARTING in ${RESTART_DELAY}s (attempt ${restart_count}/${MAX_RESTARTS})"
        sleep "${RESTART_DELAY}"
    done

    if [ "${restart_count}" -ge "${MAX_RESTARTS}" ]; then
        log_msg "MAX_RESTARTS_REACHED — giving up"
    fi

    rm -f "${PID_FILE}" "${BOT_PID_FILE}"
    log_msg "WATCHDOG_STOPPED"
}

do_stop() {
    local wd_pid bot_pid
    wd_pid=$(read_pid "${PID_FILE}")
    bot_pid=$(read_pid "${BOT_PID_FILE}")

    log_msg "STOP_REQUESTED watchdog_pid=${wd_pid:-none} bot_pid=${bot_pid:-none}"

    # Stop the watchdog first so it won't restart the bot; it forwards SIGTERM.
    if pid_matches "${wd_pid}" "watchdog.sh"; then
        kill -TERM "${wd_pid}" 2>/dev/null || true
    fi

    if pid_matches "${bot_pid}" "headless_main.py"; then
        kill -TERM "${bot_pid}" 2>/dev/null || true
        echo "Sent SIGTERM to bot (pid=${bot_pid}). Waiting for graceful shutdown..."
        for _ in $(seq 1 30); do
            pid_matches "${bot_pid}" "headless_main.py" || break
            sleep 1
        done
        if pid_matches "${bot_pid}" "headless_main.py"; then
            echo "Force killing bot..."
            kill -9 "${bot_pid}" 2>/dev/null || true
        else
            echo "Bot stopped gracefully."
        fi
    else
        echo "Bot process not running."
    fi

    # Give the watchdog a moment to log and clean up after the bot exits
    for _ in $(seq 1 5); do
        pid_matches "${wd_pid}" "watchdog.sh" || break
        sleep 1
    done

    rm -f "${PID_FILE}" "${BOT_PID_FILE}"
    log_msg "ALL_STOPPED"
    echo "All processes stopped."
}

do_status() {
    echo "=== Trading Bot Status ==="

    local bot_pid wd_pid
    bot_pid=$(read_pid "${BOT_PID_FILE}")
    wd_pid=$(read_pid "${PID_FILE}")

    if pid_matches "${bot_pid}" "headless_main.py"; then
        echo "✅ Bot:      RUNNING (pid=${bot_pid})"
    else
        echo "❌ Bot:      NOT RUNNING"
    fi

    if pid_matches "${wd_pid}" "watchdog.sh"; then
        echo "✅ Watchdog: RUNNING (pid=${wd_pid})"
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
