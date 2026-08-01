#!/usr/bin/env python3
"""Bring-up harness for the powerbox USB-port relays (PCF8574, firmware >= 1.7.0).

Runs ON PRIUS while prius-backend is STOPPED. The daemon owns the powerbox
serial port and, critically, keeps sending the POCO heartbeat ({"a":"hb"})
every 2 s — without it the powerbox would decide the POCO is dead ~15 s after
the backend stops and start pressing its power button (escalating to a 35 s
forced power-cycle). Do NOT run relay tests with the backend stopped any other
way.

Usage:
    sudo systemctl stop prius-backend
    sudo chmod a+rw /dev/ttyACM0        # reset on re-enumeration
    nohup python3 relay_bringup_test.py daemon >/tmp/relay-daemon.out 2>&1 &

    python3 relay_bringup_test.py cmd 1 on     # energise relay ch1
    python3 relay_bringup_test.py cmd 1 off
    python3 relay_bringup_test.py cmd whoami   # any bare action word
    python3 relay_bringup_test.py cmd quit     # stop the daemon

    tail -f /tmp/relay-log                     # acks / errors / rly changes
    sudo systemctl restart prius-backend       # when done

The daemon logs every ack/ERROR/READY/IDENT/SENSOR_OK frame and any STATUS
whose "rly" array changed, timestamped, to /tmp/relay-log.
"""

import json
import os
import subprocess
import sys
import threading
import time

import serial  # system pyserial

PORT = "/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_503359277a7c699f-if00"
BAUD = 115200
CMD_FILE = "/tmp/relay-cmd"
LOG_FILE = "/tmp/relay-log"


def send(s, payload):
    s.write((json.dumps({"id": 0, "d": payload}) + "\n").encode())
    s.flush()


def daemon():
    log = open(LOG_FILE, "a", buffering=1)

    def logline(text):
        log.write("%s %s\n" % (time.strftime("%H:%M:%S"), text))

    logline("daemon start")

    stop = threading.Event()
    shared = {"s": None}

    def open_port():
        """(Re)open the powerbox port via the by-id path, waiting for it to
        (re)appear. The board WDT-resets ~30 s after boot when nobody drains
        its CDC, so after every re-enumeration we must reopen the NEW node."""
        while True:
            try:
                node = os.path.realpath(PORT)
                if os.path.exists(node) and os.path.exists(PORT):
                    subprocess.run(["sudo", "-n", "chmod", "a+rw", node],
                                   capture_output=True, timeout=10)
                    s = serial.Serial(node, BAUD, timeout=0.3)
                    logline("OPEN %s" % node)
                    return s
            except Exception as e:
                logline("OPEN_RETRY %s" % e)
            time.sleep(0.5)

    def hb_loop():
        n = 0
        while not stop.is_set():
            s = shared["s"]
            if s is not None:
                try:
                    send(s, {"a": "hb", "n": n})
                except Exception:
                    pass  # RX loop handles reopen
            n = (n + 1) & 0xFF
            stop.wait(2.0)

    threading.Thread(target=hb_loop, daemon=True).start()

    open(CMD_FILE, "w").close()
    shared["s"] = open_port()
    try:
        send(shared["s"], {"a": "whoami"})
    except Exception:
        pass
    last_rly = None

    while True:
        s = shared["s"]
        # ── serial RX ──
        try:
            raw = s.readline()
        except Exception as e:
            logline("RX_ERR %s -- reopening" % e)
            shared["s"] = None
            try:
                s.close()
            except Exception:
                pass
            shared["s"] = open_port()
            try:
                send(shared["s"], {"a": "whoami"})
            except Exception:
                pass
            continue
        if raw:
            try:
                d = json.loads(raw.decode(errors="replace"))["d"]
            except (ValueError, KeyError, TypeError):
                d = None
            if isinstance(d, dict):
                msg = d.get("msg")
                if msg == "STATUS":
                    rly = d.get("rly")
                    if rly != last_rly:
                        last_rly = rly
                        logline("RLY %s (full: %s)" % (rly, json.dumps(d)))
                elif d.get("ack") or msg in (
                    "ERROR", "SENSOR_OK", "POWERBOX_READY", "IDENT", "I2C_OK",
                ):
                    logline(json.dumps(d))

        # ── command spool ──
        try:
            cmd = open(CMD_FILE).read().strip()
        except OSError:
            cmd = ""
        if not cmd:
            continue
        open(CMD_FILE, "w").close()
        for line in cmd.splitlines():
            line = line.strip()
            if not line:
                continue
            logline("CMD %s" % line)
            if line == "quit":
                stop.set()
                logline("daemon exit")
                return
            parts = line.split()
            try:
                if len(parts) == 2 and parts[0].isdigit():
                    send(s, {"a": "relay", "ch": int(parts[0]),
                             "on": parts[1].lower() in ("1", "on", "true")})
                else:
                    send(s, {"a": parts[0]})
            except Exception as e:
                logline("TX_ERR %s" % e)


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "daemon":
        daemon()
    elif len(sys.argv) >= 3 and sys.argv[1] == "cmd":
        with open(CMD_FILE, "w") as f:
            f.write(" ".join(sys.argv[2:]))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
