#!/bin/sh
# watch_and_flash.sh — wait for a physical unplug/replug of the powerbox, then
# flash v1.7.0 the instant it re-enumerates (fresh healthy link, before the
# wedge can return). Progress + result in /tmp/watch-flash.log.
#
# Ctrl-C works both in the 3 s safe-boot window (REPL, no WDT — stable) and in
# the main loop (KeyboardInterrupt -> sys.exit -> REPL, WDT may be armed -> the
# board resets after 8 s, goes through safe boot again, and a later try lands).
set -u
BYID=/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_503359277a7c699f-if00
M="$HOME/.local/bin/mpremote"
LOG=/tmp/watch-flash.log
exec >>"$LOG" 2>&1

stamp() { echo "$(date '+%H:%M:%S') $*"; }

nudge() {
    sudo -n python3 - "$BYID" <<'PY'
import serial, sys, time, os
byid = sys.argv[1]
try:
    s = serial.Serial(os.path.realpath(byid), 115200, timeout=0.05)
except Exception as e:
    print("nudge open fail:", e); raise SystemExit(1)
end = time.time() + 3.5
buf = b""
while time.time() < end:
    try:
        s.write(b"\r\x03"); buf += s.read(300)
    except Exception:
        pass
    time.sleep(0.05)
s.close()
print("nudge tail:", buf[-80:])
PY
}

stamp "watcher armed - waiting for powerbox UNPLUG"
while [ -e "$BYID" ]; do sleep 0.3; done
stamp "UNPLUG detected - waiting for replug"
while [ ! -e "$BYID" ]; do sleep 0.2; done
stamp "REPLUG detected"

for p in $(pgrep -f "relay_bringup_test.p[y]"); do kill "$p" 2>/dev/null; done

ok=0
try=1
while [ $try -le 10 ]; do
    sudo -n chmod a+rw "$(readlink -f "$BYID")" 2>/dev/null
    nudge
    if "$M" resume connect "$BYID" fs cp "$HOME/powerbox/pcf8574.py" :pcf8574.py 2>&1 \
       && "$M" resume connect "$BYID" fs cp "$HOME/powerbox/main.py" :main.py 2>&1; then
        ok=1
        stamp "copied on try $try"
        break
    fi
    stamp "try $try failed"
    try=$((try + 1))
    # If the board WDT-reset it re-enumerates: wait for the node to be back.
    i=0; while [ $i -lt 40 ] && [ ! -e "$BYID" ]; do sleep 0.3; i=$((i+1)); done
done

if [ $ok -eq 1 ]; then
    "$M" resume connect "$BYID" exec \
      "import os; f=open('main.py'); v=[l for l in f if l.startswith('VERSION')]; f.close(); print('on-device:', v[0].strip() if v else 'NO VERSION', '| files:', os.listdir())" 2>&1
    "$M" resume connect "$BYID" reset >/dev/null 2>&1
    stamp "RESULT: FLASHED OK - reset sent"
else
    stamp "RESULT: FLASH FAILED after 10 tries"
fi

sleep 7
sudo -n chmod a+rw "$(readlink -f "$BYID")" 2>/dev/null
cd "$HOME/powerbox" && nohup python3 relay_bringup_test.py daemon >/tmp/relay-daemon.out 2>&1 &
stamp "hb daemon restarted"
