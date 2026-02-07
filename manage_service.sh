#!/bin/bash
set -euo pipefail

SERVICE_NAME="lobs-orchestrator.service"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_PATH="$(pwd)/$SERVICE_NAME"
INSTALL_PATH="$SERVICE_DIR/$SERVICE_NAME"

function install() {
    echo "Installing user service..."

    mkdir -p "$SERVICE_DIR"
    cp "$SERVICE_PATH" "$INSTALL_PATH"

    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"

    echo "User service installed and enabled."
}

function start() {
    echo "Starting service..."
    systemctl --user start "$SERVICE_NAME"
}

function stop() {
    echo "Stopping service..."
    systemctl --user stop "$SERVICE_NAME"
}

function restart() {
    echo "Restarting service..."
    systemctl --user restart "$SERVICE_NAME"
}

function status() {
    systemctl --user status "$SERVICE_NAME"
}

function logs() {
    journalctl --user -u "$SERVICE_NAME" -f
}

case "${1:-}" in
    install) install ;;
    start) start ;;
    stop) stop ;;
    restart) restart ;;
    status) status ;;
    logs) logs ;;
    *)
        echo "Usage: $0 {install|start|stop|restart|status|logs}"
        exit 1
        ;;
esac
