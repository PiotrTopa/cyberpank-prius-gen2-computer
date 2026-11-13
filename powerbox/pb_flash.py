#!/usr/bin/env python3
"""Update powerbox firmware files over the NDJSON link (firmware >= 2.2.0).

No REPL, no Ctrl-C: the firmware keeps running (WDT fed, POCO button released)
while each file is streamed with "put_begin"/"put"/"put_end", verified on the
board (size + sha256) and swapped in; then "reboot". This tool sends the host
heartbeat itself (the firmware only transmits while it hears one, and its wake
ladder would press the POCO button without it), so nothing else may hold the
port: on the POCO stop prius-backend first (prius-flash-powerbox does that).

    pb_flash.py [--port DEV] [--no-reboot] FILE [FILE ...]

A file is stored under its basename (main.py, boot.py, pcf8574.py, ...).
"""

import argparse
import base64
import hashlib
import json
import os
import queue
import sys
import threading
import time

import serial

DEFAULT_PORT = "/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_503359277a7c699f-if00"
CHUNK = 384          # raw bytes per "put" (512 base64 chars)
WINDOW = 8           # chunks in flight before waiting for the last ack


class Link:
    def __init__(self, port):
        self.port = port
        self.rx = queue.Queue()
        self.lock = threading.Lock()
        self.ser = None
        self.n = 0
        self.stop = False
        self._open()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def _open(self, timeout=30):
        t0 = time.time()
        while True:
            try:
                self.ser = serial.Serial(self.port, 115200, timeout=0.2)
                return
            except (serial.SerialException, OSError):
                if time.time() - t0 > timeout:
                    raise
                time.sleep(0.2)

    def send(self, d):
        line = json.dumps({"id": 0, "d": d}, separators=(",", ":")) + "\n"
        with self.lock:
            try:
                self.ser.write(line.encode())
            except (serial.SerialException, OSError):
                pass

    def _heartbeat(self):
        while not self.stop:
            self.n += 1
            self.send({"a": "hb", "n": self.n})
            time.sleep(0.5)

    def _reader(self):
        buf = b""
        while not self.stop:
            try:
                chunk = self.ser.read(4096)
            except (serial.SerialException, OSError):
                time.sleep(0.5)               # board rebooting: reopen
                try:
                    self.ser.close()
                    self._open(timeout=60)
                except Exception:
                    pass
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == 0:
                    self.rx.put(msg.get("d") or {})

    def wait(self, pred, timeout):
        t_end = time.time() + timeout
        while time.time() < t_end:
            try:
                d = self.rx.get(timeout=max(0.05, t_end - time.time()))
            except queue.Empty:
                break
            if d.get("msg") == "ERROR" and d.get("code") == "PUT":
                raise RuntimeError("board: PUT %s" % d.get("detail"))
            if pred(d):
                return d
        raise TimeoutError("no reply")


def put_file(link, path):
    data = open(path, "rb").read()
    name = os.path.basename(path)
    sha = hashlib.sha256(data).hexdigest()
    link.send({"a": "put_begin", "name": name, "size": len(data), "sha": sha})
    link.wait(lambda d: d.get("msg") == "PUT" and d.get("st") == "ready", 10)
    chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]
    seq = 0
    while seq < len(chunks):
        last = min(seq + WINDOW, len(chunks))
        for k in range(seq, last):
            link.send({"a": "put", "seq": k, "b64": base64.b64encode(chunks[k]).decode()})
        link.wait(lambda d, k=last - 1: d.get("msg") == "PUT" and d.get("seq") == k, 15)
        seq = last
        print("\r  %s %d/%d" % (name, min(seq * CHUNK, len(data)), len(data)), end="", flush=True)
    link.send({"a": "put_end"})
    link.wait(lambda d: d.get("msg") == "PUT" and d.get("st") == "ok", 15)
    print("\r  %s %d bytes ok sha256 %s" % (name, len(data), sha[:12]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--no-reboot", action="store_true")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()
    link = Link(a.port)
    link.send({"a": "whoami"})
    ident = link.wait(lambda d: d.get("msg") == "IDENT", 10)
    print("board: %s %s" % (ident.get("role"), ident.get("ver")))
    if ident.get("role") != "powerbox":
        sys.exit("not a powerbox")
    # main.py first: a new firmware file (bean_rx.py in 2.3.0, bean_tx.py in
    # 2.4.0) is only accepted once the main.py that lists it in OTA_FILES is
    # running, so a name refusal after main.py went in triggers one reboot
    # and a retry.
    files = sorted(a.files, key=lambda f: os.path.basename(f) != "main.py")
    sent_main = False
    retried = False
    i = 0
    while i < len(files):
        try:
            put_file(link, files[i])
        except RuntimeError as e:
            if "PUT name" not in str(e) or not sent_main or retried:
                raise
            print("\n  %s refused by the running firmware; rebooting into the new "
                  "main.py and retrying" % os.path.basename(files[i]))
            reboot(link)
            retried = True
            continue
        sent_main = sent_main or os.path.basename(files[i]) == "main.py"
        i += 1
    if a.no_reboot:
        return
    reboot(link)


def reboot(link):
    link.send({"a": "reboot"})
    link.wait(lambda d: d.get("ack") == "reboot", 10)
    print("rebooting...")
    time.sleep(2)
    t_end = time.time() + 60
    while time.time() < t_end:
        link.send({"a": "whoami"})
        try:
            d = link.wait(lambda d: d.get("msg") in ("IDENT", "POWERBOX_READY"), 2)
            print("back: %s %s" % (d.get("role", "powerbox"), d.get("ver")))
            return
        except TimeoutError:
            pass
    sys.exit("board did not come back within 60 s")


if __name__ == "__main__":
    main()
