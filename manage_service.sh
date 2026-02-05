#!/bin/bash

# Script to manage the systemd service for Lobs Orchestrator

SERVICE_NAME="lobs-orchestrator.service"
SERVICE_PATH="$(pwd)/$SERVICE_NAME"
SYSTEMD_PATH="/etc/systemd/system/$SERVICE_NAME"

function install() {
    echo "Installing service..."
    sudo cp "$SERVICE_PATH" "$SYSTEMD_PATH"
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    echo "Service installed and enabled."
}

function start() {
    echo "Starting service..."
    sudo systemctl start "$SERVICE_NAME"
}

function stop() {
    echo "Stopping service..."
    sudo systemctl stop "$SERVICE_NAME"
}

function restart() {
    echo "Restarting service..."
    sudo systemctl restart "$SERVICE_NAME"
}

function status() {
    sudo systemctl status "$SERVICE_NAME"
}

function logs() {
    journalctl -u "$SERVICE_NAME" -f
}

case "$1" in
    install) install ;;
    start) start ;;
    stop) stop ;;
    restart) restart ;;
    status) status ;;
    logs) logs ;;
    *) echo "Usage: $0 {install|start|stop|restart|status|logs}" ;;
esac
