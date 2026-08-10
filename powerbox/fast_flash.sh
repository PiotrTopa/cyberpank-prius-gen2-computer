#!/bin/sh
# fast_flash.sh — flash powerbox firmware racing the chronic hub/CDC wedge.
#
# The ActionStar hub goes catatonic after minutes: children stay enumerated but
# every transfer EPROTOs, and even the hub's port-power control stops acting.
# A driver unbind/bind of the hub heals it. So, all inside one healthy window:
#   1. heal:  unbind/bind hub 1-1
#   2. cut:   real VBUS off/on on port 5 (verified by DEVNUM CHANGE — while
#             VBUS is off the kernel keeps the stale device node, so "node
#             disappears" is the WRONG check; the proof of a real cut is a
#             new devnum after re-power)
#   3. catch: spam Ctrl-C through the 3 s safe-boot => REPL, no WDT, quiet
#             link (reopen the tty by devnum — the pre-cut handle is stale)
#   4. flash: cp pcf8574.py + main.py, verify, reset
# Run on prius as user (sudo -n available). Caller restarts the hb daemon.
set -u
BYID=/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_503359277a7c699f-if00
SYSDEV=/sys/bus/usb/devices/1-1.5
M="$HOME/.local/bin/mpremote"

echo "== 1 heal: hub 1-1 unbind/bind =="
sudo -n sh -c "echo 1-1 > /sys/bus/usb/drivers/usb/unbind" 2>&1
sleep 2
sudo -n sh -c "echo 1-1 > /sys/bus/usb/drivers/usb/bind" 2>&1
i=0
while [ $i -lt 40 ] && [ ! -e "$BYID" ]; do sleep 0.5; i=$((i+1)); done
[ -e "$BYID" ] || { echo "FATAL: powerbox absent after hub heal"; exit 1; }
echo "healed: $(readlink -f "$BYID")"
sleep 1

echo "== 2 cut: VBUS off/on p5 (verify by devnum change) =="
DEVNUM_BEFORE="$(cat "$SYSDEV/devnum" 2>/dev/null || echo '?')"
sudo -n uhubctl -f -l 1-1 -p 5 -a off 2>&1 | grep -E "Port 5|Sent"
sleep 3
sudo -n uhubctl -f -l 1-1 -p 5 -a on 2>&1 | grep -E "Port 5|Sent"
i=0
DEVNUM_AFTER="$DEVNUM_BEFORE"
while [ $i -lt 40 ]; do
    DEVNUM_AFTER="$(cat "$SYSDEV/devnum" 2>/dev/null || echo "$DEVNUM_BEFORE")"
    [ "$DEVNUM_AFTER" != "$DEVNUM_BEFORE" ] && break
    sleep 0.25; i=$((i+1))
done
if [ "$DEVNUM_AFTER" = "$DEVNUM_BEFORE" ]; then
    echo "WARN: devnum unchanged ($DEVNUM_BEFORE) - cut ineffective, falling through"
else
    echo "devnum $DEVNUM_BEFORE -> $DEVNUM_AFTER: real cut confirmed, MCU cold-booted"
fi

echo "== 3 catch safe-boot REPL =="
sudo -n python3 - "$BYID" <<'PY'
import serial, sys, time, os
byid = sys.argv[1]
t0 = time.time(); s = None; node = None
# The board is mid-re-enumeration: keep (re)opening the CURRENT node and spam
# Ctrl-C. Reopen on any error — a handle from before re-enumeration is stale.
buf = b""
end = time.time() + 15
while time.time() < end:
    try:
        if s is None:
            node = os.path.realpath(byid)
            s = serial.Serial(node, 115200, timeout=0.05)
            print("port open at t=%.1fs; node=%s" % (time.time() - t0, node))
        s.write(b"\r\x03")
        buf += s.read(300)
        if b">>>" in buf[-40:]:
            break
    except Exception:
        try:
            if s: s.close()
        except Exception:
            pass
        s = None
        time.sleep(0.1)
if s is None:
    print("FATAL: device never became openable"); raise SystemExit(1)
s.close()
print("tail:", buf[-150:])
print("REPL_CAUGHT" if b">>>" in buf else "REPL_UNCONFIRMED")
PY

NODE="$(readlink -f "$BYID")"
sudo -n chmod a+rw "$NODE"

echo "== 4 flash =="
"$M" resume connect "$BYID" fs cp "$HOME/powerbox/pcf8574.py" :pcf8574.py 2>&1 | tail -1
"$M" resume connect "$BYID" fs cp "$HOME/powerbox/main.py" :main.py 2>&1 | tail -1
"$M" resume connect "$BYID" exec \
  "import os; f=open('main.py'); v=[l for l in f if l.startswith('VERSION')]; f.close(); print('on-device:', v[0].strip() if v else 'NO VERSION', '| files:', os.listdir())" 2>&1 | tail -1
"$M" resume connect "$BYID" reset >/dev/null 2>&1 || true
echo "DONE - reset sent, firmware rebooting"
