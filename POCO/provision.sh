#!/bin/sh
# provision.sh — install the Prius rootfs overlay into / and activate it.
#
# This is the "Dockerfile RUN" stage: it takes a staged rootfs mirror (a directory
# whose tree maps 1:1 onto the device filesystem) and applies it idempotently:
#   - copy every file to its path under / with the correct mode/owner,
#   - seed /etc/prius/* flag files if missing (never clobber live state),
#   - reload + enable the systemd units and NetworkManager connections.
#
# It is normally invoked by sync.sh after rsync, but can be run standalone on the
# phone for recovery. Re-running is safe.
#
#   sudo sh provision.sh <rootfs-dir> [--packages] [--usb]
#     --packages : apk add the package list at /tmp/packages.txt (needs network)
#     --usb      : apply USB host + VBUS (prius-usb host && prius-vbus apply --reboot)
set -eu

SRC="${1:-}"
[ -n "$SRC" ] && [ -d "$SRC" ] || { echo "usage: provision.sh <rootfs-dir> [--packages] [--usb]" >&2; exit 2; }
shift
[ "$(id -u)" = 0 ] || { echo "must run as root" >&2; exit 1; }

DO_PKGS=0; DO_USB=0
for a in "$@"; do
    case "$a" in
        --packages) DO_PKGS=1 ;;
        --usb)      DO_USB=1 ;;
        *) echo "unknown arg: $a" >&2; exit 2 ;;
    esac
done

log() { echo "provision: $*"; }

# --- 1. packages (optional; needs network) -----------------------------------
if [ "$DO_PKGS" = 1 ]; then
    if [ -f /tmp/packages.txt ]; then
        pkgs=$(grep -vE '^[[:space:]]*#|^[[:space:]]*$' /tmp/packages.txt | tr '\n' ' ')
        log "apk add: $pkgs"
        # shellcheck disable=SC2086
        apk add $pkgs
    else
        log "WARN: --packages set but /tmp/packages.txt missing; skipping"
    fi
fi

# --- 2. install the rootfs overlay -------------------------------------------
log "installing rootfs overlay from $SRC"
( cd "$SRC" && find . -type f ) | while IFS= read -r f; do
    rel=${f#./}
    dst="/$rel"
    case "$rel" in
        usr/local/sbin/*)                         mode=755 ;;
        etc/NetworkManager/system-connections/*)  mode=600 ;;
        *)                                        mode=644 ;;
    esac
    install -D -m "$mode" -o root -g root "$SRC/$rel" "$dst"
    # Strip CRLF from text assets that the kernel/systemd/NM parse directly.
    # A stray CR in a shebang (#!/bin/sh\r) makes the loader fail with 203/EXEC
    # ("bad interpreter: No such file or directory"). Files staged from a Windows
    # working tree can carry CRLF even with .gitattributes, so normalize on-device.
    case "$rel" in
        usr/local/sbin/*|etc/systemd/system/*|etc/NetworkManager/*|etc/prius/*)
            sed -i 's/\r$//' "$dst" ;;
    esac
    log "  $dst ($mode)"
done

# --- 3. seed flag files (create-if-missing; never clobber live state) ---------
mkdir -p /etc/prius
for kv in power-mode:full wifi-mode:off usb-mode:host; do
    k=${kv%%:*}; v=${kv#*:}
    if [ ! -f "/etc/prius/$k" ]; then
        echo "$v" > "/etc/prius/$k"
        log "seeded /etc/prius/$k = $v"
    fi
done

# Seed the backend service config once (never clobber a live token/port).
if [ ! -f /etc/prius/backend.env ]; then
    cat > /etc/prius/backend.env <<'EOF'
# Config for prius-backend.service (the headless board-computer backend).
BACKEND_REPO=/home/user/cyberpunk
BACKEND_GATEWAY_PORT=/dev/ttyACM0
BACKEND_BAUDRATE=1000000
BACKEND_API_HOST=0.0.0.0
BACKEND_API_PORT=8080
BACKEND_DB=/home/user/cyberpunk/data/metrics.db
BACKEND_TICK_HZ=50
# Bearer token required by the network API. Empty = open (LAN/VPN only!).
BACKEND_AUTH_TOKEN=
BACKEND_EXTRA_ARGS=

# Trip recording: rotating per-trip NDJSON logs (replay-compatible). 0=off.
BACKEND_RECORD=0
BACKEND_RECORD_DIR=/home/user/cyberpunk/logs/trips
# Segmentation: trip | session | continuous
BACKEND_RECORD_SEGMENTATION=trip
# What to record: all | comma list of system,can,avc,satellite,powerbox,outgoing
BACKEND_RECORD_INCLUDE=all
# Rotation limits (0 = unlimited for that dimension).
BACKEND_RECORD_MAX_FILES=60
BACKEND_RECORD_MAX_TOTAL_MB=512
BACKEND_RECORD_MAX_AGE_DAYS=30
EOF
    log "seeded /etc/prius/backend.env"
fi

# --- 4. activate systemd units + NetworkManager ------------------------------
log "reloading systemd + enabling units"
systemctl daemon-reload
for u in prius-power.service prius-power.path \
         prius-wifi.service prius-wifi.path \
         prius-netwatch.service prius-netwatch.timer \
         prius-backend.service \
         backlight-off.service; do
    systemctl enable "$u" >/dev/null 2>&1 || log "  (enable $u skipped)"
done
systemctl start prius-power.path prius-wifi.path prius-netwatch.timer >/dev/null 2>&1 || true

if command -v nmcli >/dev/null 2>&1; then
    log "reloading NetworkManager connections"
    nmcli connection reload >/dev/null 2>&1 || true
fi

# --- 5. USB host + VBUS (optional; reboots) ----------------------------------
if [ "$DO_USB" = 1 ]; then
    log "applying USB host + VBUS (will reboot)"
    prius-usb host || true
    prius-vbus apply --reboot || true
fi

log "done."
