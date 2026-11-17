# hu-common.sh — shared helpers for the head-unit ADB tools (sourced, bash).
#
# The head unit (MediaTek Autochips AC8227L, Android 8.1) shows up on the car
# computer as USB 0e8d:201c ("9520") at 1-1.4.2, behind the camera hub 1-1.4
# (VIA 2109:2817, powerbox relay ch4), and only while ACC is on.
# docs/HEADUNIT_INTEGRATION.md has the facts and the on-site checklist.
#
# Environment overrides:
#   ADB             adb binary (default: adb)
#   HU_SERIAL       adb serial (default: the first USB device with id 0e8d:201c;
#                   known unit: MJU8DY59WS8LMB85). "emulator-5554" etc. also work.
#   HU_VIDPID       USB id of the head unit in adb mode (default 0e8d:201c; the
#                   vendor part is what the tools match)
#   HU_HUB_PATH     sysfs name of the camera hub (default 1-1.4)
#   HU_DATA         where captures go (default ~/headunit)
#   PRIUS_API       backend REST base (default http://127.0.0.1:8080)
#   PRIUS_API_TOKEN backend bearer token (else ~/.config/prius/api-token, else
#                   `sudo -n` read of /etc/prius/backend-auth.env, else none)

ADB="${ADB:-adb}"
HU_VIDPID="${HU_VIDPID:-0e8d:201c}"
HU_HUB_PATH="${HU_HUB_PATH:-1-1.4}"
HU_DATA="${HU_DATA:-$HOME/headunit}"
PRIUS_API="${PRIUS_API:-http://127.0.0.1:8080}"
HU_TIMEOUT="${HU_TIMEOUT:-8}"

# exit codes shared by all tools
E_ABSENT=3        # no head unit on USB / adb sees no device
E_UNAUTH=4        # adb: unauthorized (RSA prompt not accepted yet)
E_OFFLINE=5       # adb: offline / other state
E_REFUSED=6       # a write was asked for without the explicit flag

hu_log()  { printf '%s\n' "$*" >&2; }
hu_die()  { local code="$1"; shift; hu_log "hu: $*"; exit "$code"; }

hu_have_adb() { command -v "$ADB" >/dev/null 2>&1; }

# Ensure the adb server runs (quietly); fails only when adb is missing.
hu_adb_server() {
    hu_have_adb || hu_die 2 "adb not installed (apk add android-tools)"
    timeout "$HU_TIMEOUT" "$ADB" start-server >/dev/null 2>&1 || true
}

# Print "<sysfs-name> <vid:pid> <product> <serial> <speed>" for every USB device of the
# head unit's vendor (HU_VIDPID's vendor part: 0e8d). Any product id matches, because the
# id changes with the USB function set (adb 201c; rndis,adb another one).
hu_usb_find() {
    local d v p want="${HU_VIDPID%%:*}"
    for d in /sys/bus/usb/devices/*; do
        [ -f "$d/idVendor" ] || continue
        v=$(cat "$d/idVendor" 2>/dev/null); p=$(cat "$d/idProduct" 2>/dev/null)
        [ "$v" = "$want" ] || continue
        printf '%s %s %s %s %s\n' "$(basename "$d")" "$v:$p" \
            "$(tr ' ' _ < "$d/product" 2>/dev/null || echo '?')" \
            "$(cat "$d/serial" 2>/dev/null || echo '?')" \
            "$(cat "$d/speed" 2>/dev/null || echo '?')"
    done
}

# Camera hub presence: "<vid:pid>" when $HU_HUB_PATH exists, empty otherwise.
hu_hub_id() {
    local d="/sys/bus/usb/devices/$HU_HUB_PATH"
    [ -f "$d/idVendor" ] && printf '%s:%s\n' "$(cat "$d/idVendor")" "$(cat "$d/idProduct")"
}

# Backend token (never printed). Empty when none is reachable.
hu_api_token() {
    if [ -n "${PRIUS_API_TOKEN:-}" ]; then printf '%s' "$PRIUS_API_TOKEN"; return; fi
    if [ -r "$HOME/.config/prius/api-token" ]; then head -n1 "$HOME/.config/prius/api-token"; return; fi
    sudo -n sed -n 's/^BACKEND_AUTH_TOKEN=//p' /etc/prius/backend-auth.env 2>/dev/null | head -n1
}

# GET a backend REST path; prints the body; non-zero on failure.
hu_api_get() {
    local tok; tok=$(hu_api_token)
    if [ -n "$tok" ]; then
        curl -fsS -m 4 -H "Authorization: Bearer $tok" "$PRIUS_API$1"
    else
        curl -fsS -m 4 "$PRIUS_API$1"
    fi
}

# Resolve the adb serial of the head unit into HU_SERIAL (exported) if not set.
hu_resolve_serial() {
    [ -n "${HU_SERIAL:-}" ] && return 0
    local line
    line=$(hu_usb_find | head -n1)
    if [ -n "$line" ]; then
        set -- $line
        [ "$4" != "?" ] && HU_SERIAL="$4"
    fi
    export HU_SERIAL
}

# adb state of the head unit: device | unauthorized | offline | absent | <other>
hu_adb_state() {
    hu_resolve_serial
    local out
    if [ -n "${HU_SERIAL:-}" ]; then
        out=$(timeout "$HU_TIMEOUT" "$ADB" devices 2>/dev/null | awk -v s="$HU_SERIAL" '$1==s {print $2}')
    else
        # no serial known: accept exactly one USB-attached adb device
        out=$(timeout "$HU_TIMEOUT" "$ADB" devices -l 2>/dev/null | awk 'NR>1 && /usb:/ {print $2}' | head -n1)
    fi
    printf '%s\n' "${out:-absent}"
}

# adb device selector for the head unit (-s SERIAL, or -d = the only USB device)
hu_sel() { hu_resolve_serial; if [ -n "${HU_SERIAL:-}" ]; then HU_SEL=(-s "$HU_SERIAL"); else HU_SEL=(-d); fi; }

# adb with the head unit selected
hu_adb() { hu_sel; "$ADB" "${HU_SEL[@]}" "$@"; }

# Exit with a clear message unless the head unit is reachable over adb.
hu_require() {
    hu_adb_server
    local st; st=$(hu_adb_state)
    case "$st" in
        device) return 0 ;;
        unauthorized) hu_die $E_UNAUTH "head unit is UNAUTHORIZED: accept the 'Allow USB debugging' prompt on its screen (tick 'Always allow'), or see docs/HEADUNIT_INTEGRATION.md §7 for the adb_keys route" ;;
        absent)
            if [ -n "$(hu_usb_find)" ]; then
                hu_die $E_ABSENT "head unit is on USB but adb does not list it (udev permissions? try: adb kill-server; hu-status)"
            elif [ -z "$(hu_hub_id)" ]; then
                hu_die $E_ABSENT "head unit not present: camera hub $HU_HUB_PATH is off (relay ch4 off / roadsense stack not in PREVIEW or running)"
            else
                hu_die $E_ABSENT "head unit not present: camera hub is up but no $HU_VIDPID (ACC off = head unit unpowered, or cable on its other port)"
            fi ;;
        *) hu_die $E_OFFLINE "head unit adb state '$st' (try: adb kill-server; replug / ACC cycle)" ;;
    esac
}

# Quote one string for a remote POSIX shell.
hu_q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# Run a shell command on the head unit as shell user / as root (su 0).
hu_sh() { hu_adb shell "$*"; }
hu_su() { hu_adb shell "su 0 sh -c $(hu_q "$*")"; }

hu_ts() { date +%Y%m%d-%H%M%S; }
