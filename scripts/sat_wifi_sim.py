#!/usr/bin/env python3
"""
sat_wifi_sim — a fake Wi-Fi satellite for bench-testing the car's WifiTransport.

Speaks docs/SATELLITES.md §4 (Wi-Fi) + §3 (auth) with a provisioning blob from
``prius-satkey new NAME --transport wifi -o blob.json``:

* broadcasts ``{"prius_sat":1,"fp":…,"name":…,"port":P}`` to UDP 8787 every
  10 s, so the car dials it (the car dials enrolled fingerprints, or any
  beacon while the pairing window is open);
* listens on TCP ``P`` (default 8788 — use another port when the backend runs
  on the same host) for the car's session; ``--dial HOST:PORT`` instead
  connects to the car's 8788 itself;
* answers the car's ``auth`` challenge, verifies ``auth_ok`` (mutual auth),
  then sends ``{"a":"status","ver":…,"temp":…}`` every ``--report-s`` s,
  pings every 15 s, answers ``ping`` with ``pong`` and acks any other command.

Usage (bench, backend on the same P52)::

    prius-satkey --dir ~/satkeys init
    prius-satkey --dir ~/satkeys new garage --transport wifi -o ~/satkeys/garage.json
    BACKEND_SATELLITE_KEYCHAIN_DIR=~/satkeys python -m cyberpunk_computer.backend ...
    python3 scripts/sat_wifi_sim.py --blob ~/satkeys/garage.json --port 8790 \
        --beacon-addr 127.0.0.1
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import sys
import threading
import time
import types

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _load_auth():
    """Import satellite_auth without the package __init__ chain (no backend deps)."""
    pkg = os.path.join(REPO, "cyberpunk_computer")
    for name, path in (("cyberpunk_computer", pkg),
                       ("cyberpunk_computer.io", os.path.join(pkg, "io")),
                       ("cyberpunk_computer.backend", os.path.join(pkg, "backend"))):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.__path__ = [path]
            sys.modules[name] = mod
    import importlib
    return importlib.import_module("cyberpunk_computer.backend.satellite_auth")


auth_mod = _load_auth()


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Session:
    def __init__(self, sock: socket.socket, peer: str, blob: dict, args):
        self.sock = sock
        self.peer = peer
        self.client = auth_mod.SatelliteAuthClient(blob)
        self.args = args
        self.alive = True
        self.lock = threading.Lock()

    def send(self, payload: dict) -> None:
        obj = {"d": payload}
        if self.client.authenticated:
            obj = {"id": self.client.sat_id, "d": payload}
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self.lock:
            self.sock.sendall(data)

    def run(self) -> None:
        threading.Thread(target=self._ticker, daemon=True).start()
        buf = b""
        try:
            while self.alive:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    if raw.strip():
                        self._on_line(raw.decode("utf-8", "replace"))
        except OSError as exc:
            log(f"[{self.peer}] socket error: {exc}")
        finally:
            self.alive = False
            self.sock.close()
            log(f"[{self.peer}] session closed")

    def _on_line(self, line: str) -> None:
        try:
            obj = json.loads(line)
        except ValueError:
            log(f"[{self.peer}] non-JSON: {line!r}")
            return
        payload = obj.get("d", obj) if isinstance(obj, dict) else {}
        a = payload.get("a")
        if not self.client.authenticated:
            try:
                if a == "auth":
                    log(f"[{self.peer}] challenge from car {payload.get('car')}")
                    self.send(self.client.respond(payload))
                elif a in ("auth_ok", "auth_fail"):
                    sid = self.client.check_result(payload)
                    log(f"[{self.peer}] AUTHENTICATED as sat_id {sid} (car verified)")
                    self.send({"a": "status", "ver": "sim-1", "temp": 21.0})
                else:
                    log(f"[{self.peer}] ignoring pre-auth {a!r}")
            except auth_mod.SatelliteAuthError as exc:
                log(f"[{self.peer}] AUTH FAILED: {exc}")
                self.alive = False
                self.sock.close()
            return
        if a == "ping":
            self.send({"a": "pong"})
        elif a == "pong":
            pass
        else:
            log(f"[{self.peer}] command: {payload}")
            self.send({"a": "ack", "cmd": a})

    def _ticker(self) -> None:
        last_ping = last_report = time.monotonic()
        while self.alive:
            time.sleep(0.5)
            now = time.monotonic()
            try:
                # Pings flow both ways (also while held for pairing) so neither
                # side declares the session dead after 45 s of silence.
                if now - last_ping >= 15:
                    last_ping = now
                    if self.client.authenticated:
                        self.send({"a": "ping"})
                    else:
                        with self.lock:
                            self.sock.sendall(b'{"d":{"a":"ping"}}\n')
                if self.client.authenticated and now - last_report >= self.args.report_s:
                    last_report = now
                    self.send({"a": "status", "ver": "sim-1",
                               "temp": round(20 + random.random() * 3, 1)})
            except OSError:
                return


def beacon_loop(blob: dict, args, port: int) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    msg = json.dumps({"prius_sat": 1, "fp": blob["fp"], "name": blob["name"],
                      "port": port}).encode() + b"\n"
    while True:
        try:
            s.sendto(msg, (args.beacon_addr, args.beacon_port))
        except OSError as exc:
            log(f"beacon failed: {exc}")
        time.sleep(args.beacon_s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--blob", required=True, help="provisioning blob (prius-satkey new … -o FILE)")
    ap.add_argument("--port", type=int, default=8788, help="TCP port to accept the car on")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--dial", default=None, help="connect to the car at HOST:PORT instead")
    ap.add_argument("--beacon-addr", default="255.255.255.255")
    ap.add_argument("--beacon-port", type=int, default=8787)
    ap.add_argument("--beacon-s", type=float, default=10.0)
    ap.add_argument("--no-beacon", action="store_true")
    ap.add_argument("--report-s", type=float, default=30.0)
    args = ap.parse_args()

    if not sys.modules["cyberpunk_computer.backend.satellite_keychain"].crypto_available():
        sys.exit("sat_wifi_sim needs the 'cryptography' package (use the system python3)")
    with open(os.path.expanduser(args.blob)) as fh:
        blob = json.load(fh)
    if "privkey" not in blob:
        sys.exit("blob has no privkey (use the output of 'prius-satkey new')")
    log(f"satellite {blob['name']} id {blob['sat_id']} fp {blob['fp']} car {blob['car_fp']}")

    if args.dial:
        host, _, port = args.dial.rpartition(":")
        while True:
            try:
                sock = socket.create_connection((host, int(port)), timeout=5)
                sock.settimeout(None)
                log(f"connected to car {args.dial}")
                Session(sock, args.dial, blob, args).run()
            except OSError as exc:
                log(f"dial {args.dial} failed: {exc}")
            time.sleep(5)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(4)
    log(f"listening on {args.bind}:{args.port}")
    if not args.no_beacon:
        threading.Thread(target=beacon_loop, args=(blob, args, args.port), daemon=True).start()
    while True:
        conn, (ip, port) = srv.accept()
        log(f"car session from {ip}:{port}")
        threading.Thread(target=Session(conn, f"{ip}:{port}", blob, args).run,
                         daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
