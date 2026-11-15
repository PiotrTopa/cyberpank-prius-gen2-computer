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
#   sudo sh provision.sh <rootfs-dir> [--packages] [--usb] [--core]
#     --packages : apk add the package list at /tmp/packages.txt (needs network)
#     --usb      : apply USB host + VBUS (prius-usb host && prius-vbus apply --reboot)
#     --core     : backend + power + udev only. Skips the car network layer
#                  (NetworkManager connections, resolv.conf, prius-wifi,
#                  prius-clock-fallback) and the display units
#                  (prius-blank, backlight-off). For a phone whose networking
#                  is managed by hand (the bench POCO on home Wi-Fi) or that has
#                  no panel. Flag files and the backend env are seeded as usual.
set -eu

SRC="${1:-}"
[ -n "$SRC" ] && [ -d "$SRC" ] || { echo "usage: provision.sh <rootfs-dir> [--packages] [--usb]" >&2; exit 2; }
shift
[ "$(id -u)" = 0 ] || { echo "must run as root" >&2; exit 1; }

DO_PKGS=0; DO_USB=0; CORE=0
for a in "$@"; do
    case "$a" in
        --packages) DO_PKGS=1 ;;
        --usb)      DO_USB=1 ;;
        --core)     CORE=1 ;;
        *) echo "unknown arg: $a" >&2; exit 2 ;;
    esac
done

# Files that belong to the car network layer / the display, skipped by --core.
# prius-netwatch is installed but never enabled: prius-power starts its timer
# 20 min after key-off and stops it at key-on (user decision 2026-11-06; an
# always-on netwatch rebooted the phone mid-drive).
is_noncore() {
    case "$1" in
        etc/NetworkManager/*|etc/resolv.conf|etc/prius/sim-pin) return 0 ;;
        etc/systemd/system/prius-sim-unlock.service|usr/local/sbin/prius-sim-unlock) return 0 ;;
        etc/systemd/system/prius-wifi.*|etc/systemd/system/prius-clock-fallback.*) return 0 ;;
        etc/systemd/system/prius-lifeline.*|usr/local/sbin/prius-lifeline) return 0 ;;
        etc/systemd/system/prius-blank.service|etc/systemd/system/backlight-off.service) return 0 ;;
        usr/local/sbin/prius-wifi|usr/local/sbin/prius-clock-fallback|usr/local/sbin/prius-blankd) return 0 ;;
    esac
    return 1
}

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
    if [ "$CORE" = 1 ] && is_noncore "$rel"; then
        log "  skip $dst (--core)"
        continue
    fi
    case "$rel" in
        usr/local/sbin/*)                         mode=755 ;;
        etc/NetworkManager/system-connections/*)  mode=600 ;;
        etc/prius/sim-pin)                        mode=600 ;;
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
for kv in power-mode:full wifi-mode:ap usb-mode:host wifi-grace:20min netwatch-grace:20min \
          lifeline-hold:10min lifeline-fails:3; do
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
# API token: NOT here (this file is world-readable) but in
# /etc/prius/backend-auth.env (root 0600), created below / `prius-auth token new`.
# Peers let in without credentials (default loopback) and session state:
#BACKEND_AUTH_TRUSTED=127.0.0.0/8,::1
#BACKEND_AUTH_STATE_DIR=/var/lib/prius/auth
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

# MFD video board (Raspberry Pi Zero v1.3 on powerbox relay ch2, USB gadget
# link). Check the gadget's interface name on this host after cabling
# (`ip link`): cdc_ether names it usb0 unless udev renames by MAC.
BACKEND_MFD_ENABLED=1
BACKEND_MFD_IFACE=usb0
BACKEND_MFD_IP=192.168.100.2
BACKEND_MFD_SSH_USER=piotr

# roadsense perception stack on this same POCO (single computer): the backend
# powers the camera hub (relay ch4) and starts roadsense.target with ACC, stops
# it after the key-off grace. The stack's own API serves 8090 here (8080 is the
# backend); set with `API_PORT=8090 scripts/install-phone.sh` in the roadsense
# checkout. BACKEND_ROADSENSE_MODE: auto (follow ACC) | on | off.
BACKEND_ROADSENSE_ENABLED=1
BACKEND_ROADSENSE_API_PORT=8090
BACKEND_ROADSENSE_GRACE_S=30
BACKEND_ROADSENSE_MODE=auto
# Road scene for the MFD main screen (event roadsense_scene over ZMQ, read from
# the stack's /ws while it runs and the MFD is on; docs/MFD_MAIN_SCREEN.md 7).
BACKEND_ROADSENSE_SCENE=1
BACKEND_ROADSENSE_SCENE_HZ=10
# Position feeder -> roadsense mapd (CAN motion + head-unit GNSS; docs/POSITION_FEEDER.md).
# Off until mapd and a map pack are installed. SOURCE: headunit | none | synthetic (demo only).
BACKEND_POSITION_FEEDER=0
BACKEND_POSITION_SOURCE=headunit

# USB topology (main hub 1-1 = Terminus 1a40:0101, 4 ports, cabled 2026-11-04):
# port 1 powerbox, port 2 MFD Pi (relay ch2), port 3 CAN hub with the gateway +
# panda (relay ch3), port 4 VIA camera hub (relay ch4). The gateway sits one
# hub level down, so it is found by whoami probing (fallback), not by port.
BACKEND_HUB=1-1
BACKEND_USB_PORT_ROLES=1:powerbox
BACKEND_USB_PROBE_FALLBACK=1
BACKEND_PP_POWERBOX_PORT=1
BACKEND_PP_MFD_PORT=2
BACKEND_PP_GATEWAY_PORT=3
BACKEND_PP_CAMERA_PORT=4
BACKEND_MFD_PORT=2
EOF
    log "seeded /etc/prius/backend.env"
fi

# Network API token (docs/AUTH.md): generated once on the phone, root 0600,
# never clobbered (an empty BACKEND_AUTH_TOKEN= line there turns auth off).
# Copy it into the operator's vault from the dev box, e.g.
#   ssh <phone> 'sudo sed -n "s/^BACKEND_AUTH_TOKEN=//p" /etc/prius/backend-auth.env' \
#     | dotinfra vault set prius_backend_auth_token
if [ ! -e /etc/prius/backend-auth.env ]; then
    ( umask 077
      tok=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') || exit 1
      printf '# prius-backend API token (root 0600; prius-auth token new). docs/AUTH.md\nBACKEND_AUTH_TOKEN=%s\n' \
          "$tok" > /etc/prius/backend-auth.env ) \
        && log "generated /etc/prius/backend-auth.env (API token; store it in the vault)" \
        || log "  WARN: could not generate the API token"
fi
chmod 600 /etc/prius/backend-auth.env 2>/dev/null || true
chown root:root /etc/prius/backend-auth.env 2>/dev/null || true

# --- 4. activate systemd units + NetworkManager ------------------------------
log "reloading systemd + enabling units"
systemctl daemon-reload
# CPU/memory layout (prius.slice on cpu3; roadsense/system/user slices off it):
# slices pick up AllowedCPUs on daemon-reload; services move into prius.slice
# at their next (re)start.

# Firewall: the overlay ships /etc/nftables.d/50_prius_backend.nft (8080-8082
# accept + the prius_guard table limiting 8080/8081/8082/8090 to lo, wg*, usb*
# and the AP address); pmOS loads nftables with policy drop, so reload it when
# it is running. Validate first: a broken file would leave the old ruleset.
if systemctl is-active --quiet nftables 2>/dev/null; then
    if nft -c -f /etc/nftables.nft >/dev/null 2>&1; then
        systemctl try-restart nftables && log "reloaded nftables" || log "  (nftables reload failed)"
    else
        log "  WARN: nft -c rejects the ruleset; NOT reloading nftables"
    fi
fi

# Reload udev so the RP2040 ModemManager-ignore rule (99-prius-rp2040-nomm)
# takes effect without a reboot. Without this, MM keeps probing the powerbox/
# gateway CDC-ACM ttys until the next boot.
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules >/dev/null 2>&1 || log "  (udev reload skipped)"
    udevadm trigger --subsystem-match=tty >/dev/null 2>&1 || true
    log "reloaded udev rules"
fi
if [ "$CORE" = 1 ]; then
    UNITS="prius-power.service prius-power.path prius-backend.service prius-bled.service"
    PATHS="prius-power.path"
else
    UNITS="prius-power.service prius-power.path prius-wifi.service prius-wifi.path \
           prius-backend.service prius-bled.service backlight-off.service \
           prius-clock-fallback.timer prius-sim-unlock.service prius-lifeline.service"
    PATHS="prius-power.path prius-wifi.path prius-clock-fallback.timer"
fi
# netwatch is owned by prius-power (ACC off + grace); never enabled at boot.
systemctl disable prius-netwatch.timer >/dev/null 2>&1 || true
for u in $UNITS; do
    systemctl enable "$u" >/dev/null 2>&1 || log "  (enable $u skipped)"
done
# shellcheck disable=SC2086
systemctl start $PATHS >/dev/null 2>&1 || true
# prius-lifeline (uplink down => car AP up) is always on, not owned by prius-power;
# restart so a running loop picks up a new script.
[ "$CORE" = 1 ] || systemctl restart prius-lifeline.service >/dev/null 2>&1 || log "  (prius-lifeline start skipped)"

if [ "$CORE" = 0 ] && command -v nmcli >/dev/null 2>&1; then
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
