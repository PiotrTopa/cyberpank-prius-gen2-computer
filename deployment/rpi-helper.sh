#!/usr/bin/env bash
# Quick helper script for common deployment operations

set -e

# Load .env if it exists
if [ -f "$(dirname "$0")/../.env" ]; then
    export $(grep -v '^#' "$(dirname "$0")/../.env" | xargs)
fi

HOST="${DEPLOY_HOST:-prius}"
USER="${DEPLOY_USER:-piotr}"
SERVICE="cyberpunk-computer"
REMOTE_PATH="/home/$USER/cyberpunk_computer"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_header() {
    echo -e "${BLUE}===================================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}===================================================${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

# Commands
cmd_status() {
    print_header "Service Status"
    ssh "$HOST" "sudo systemctl status $SERVICE" || true
}

cmd_logs() {
    print_header "Service Logs (Live)"
    ssh "$HOST" "sudo journalctl -u $SERVICE -f"
}

cmd_logs_recent() {
    print_header "Recent Logs (Last 50 lines)"
    ssh "$HOST" "sudo journalctl -u $SERVICE -n 50 --no-pager"
}

cmd_restart() {
    print_header "Restarting Service"
    ssh "$HOST" "sudo systemctl restart $SERVICE"
    print_success "Service restarted"
    sleep 2
    cmd_status
}

cmd_stop() {
    print_header "Stopping Service"
    ssh "$HOST" "sudo systemctl stop $SERVICE"
    print_success "Service stopped"
}

cmd_start() {
    print_header "Starting Service"
    ssh "$HOST" "sudo systemctl start $SERVICE"
    print_success "Service started"
    sleep 2
    cmd_status
}

cmd_deploy() {
    print_header "Deploying to $HOST"
    python deploy.py --restart
    print_success "Deployment complete"
}

cmd_deploy_full() {
    print_header "Full Deployment (with service install)"
    python deploy.py --install --restart --logs
}

cmd_shell() {
    print_header "Opening SSH Shell"
    ssh "$HOST"
}

cmd_test() {
    print_header "Running in Test Mode"
    print_warning "Service will be stopped. Press Ctrl+C to exit test mode."
    ssh "$HOST" "sudo systemctl stop $SERVICE && cd $REMOTE_PATH && ./venv/bin/python -m cyberpunk_computer --production"
}

cmd_setup() {
    print_header "Setting up OS Requirements"
    python deploy.py --setup-os --install
}

cmd_serial() {
    print_header "Checking Serial Port"
    ssh "$HOST" "ls -l /dev/ttyACM* 2>/dev/null || echo 'No serial device found'"
}

cmd_usb() {
    print_header "USB Devices"
    ssh "$HOST" "lsusb"
}

cmd_help() {
    cat << EOF
${BLUE}CyberPunk Computer - Deployment Helper${NC}

Usage: $0 <command>

Commands:
  ${GREEN}status${NC}          Show service status
  ${GREEN}logs${NC}            Follow live logs
  ${GREEN}recent${NC}          Show recent logs (last 50 lines)
  ${GREEN}restart${NC}         Restart service
  ${GREEN}start${NC}           Start service
  ${GREEN}stop${NC}            Stop service
  ${GREEN}deploy${NC}          Deploy code and restart
  ${GREEN}deploy-full${NC}     Full deployment (install service)
  ${GREEN}setup${NC}           Set up OS requirements (first-time install)
  ${GREEN}shell${NC}           Open SSH shell
  ${GREEN}test${NC}            Run manually (stop service first)
  ${GREEN}serial${NC}          Check serial port
  ${GREEN}usb${NC}             List USB devices
  ${GREEN}help${NC}            Show this help

Environment:
  DEPLOY_HOST         Target host (default: prius)

Examples:
  $0 status           # Check if service is running
  $0 deploy           # Deploy and restart
  $0 logs             # Watch logs live
  DEPLOY_HOST=rpi $0 status  # Use different host
EOF
}

# Main
case "${1:-help}" in
    status)
        cmd_status
        ;;
    logs)
        cmd_logs
        ;;
    recent)
        cmd_logs_recent
        ;;
    restart)
        cmd_restart
        ;;
    start)
        cmd_start
        ;;
    stop)
        cmd_stop
        ;;
    deploy)
        cmd_deploy
        ;;
    deploy-full)
        cmd_deploy_full
        ;;
    setup)
        cmd_setup
        ;;
    shell)
        cmd_shell
        ;;
    test)
        cmd_test
        ;;
    serial)
        cmd_serial
        ;;
    usb)
        cmd_usb
        ;;
    help|--help|-h)
        cmd_help
        ;;
    *)
        print_error "Unknown command: $1"
        echo
        cmd_help
        exit 1
        ;;
esac
