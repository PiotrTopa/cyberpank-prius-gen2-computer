"""End-to-end over a pseudo-terminal: SerialPort hooks + NavEmulator as the
backend wires them (configure on connect, reply from the reader thread)."""

import json
import os
import select
import time

import pytest

serial = pytest.importorskip("serial")

from cyberpunk_computer.backend.service import BackendConfig, BackendService
from cyberpunk_computer.io.serial_io import SerialConfig, SerialPort


def read_lines(fd, timeout=1.0, want=1):
    buf, out, end = b"", [], time.time() + timeout
    while time.time() < end and len(out) < want:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            buf += os.read(fd, 4096)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    out.append(json.loads(line))
    return out


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pty")
def test_configure_on_connect_and_fast_reply():
    master, slave = os.openpty()
    port = SerialPort(SerialConfig(port=os.ttyname(slave), baudrate=1_000_000, timeout=0.05))
    svc = BackendService(BackendConfig())
    svc._wire_nav_emulator(port)
    try:
        assert port.start()
        cfg = read_lines(master, want=3)
        assert cfg == [
            {"id": 2, "d": {"a": "auto", "slot": 0, "on": True, "m": "110", "s": "FFF",
                            "d": ["12", "01", "00"],
                            "tx": {"m": "178", "s": "110", "c": 15,
                                   "d": ["00", "01", "12", "10", "58", "24", "85"]}}},
            {"id": 2, "d": {"a": "auto", "slot": 1, "on": True, "m": "110", "s": "FFF",
                            "d": ["12", "01", "01"],
                            "tx": {"m": "178", "s": "110", "c": 15,
                                   "d": "00 01 12 12 58 28 29 32 60 74 A4 56".split()}}},
            {"id": 0, "d": {"a": "avc_cfg", "ack": "178"}},
        ]
        t0 = time.time()
        os.write(master, (json.dumps({"id": 2, "ts": 1, "d": {
            "m": "110", "s": "178", "c": 15, "d": ["00", "12", "58", "8F"]}}) + "\n").encode())
        reply = read_lines(master, want=1)
        dt = time.time() - t0
        assert reply == [{"id": 2, "d": {"m": "178", "s": "110", "c": 15,
                                         "d": "00 58 12 9F 02 F9 D1 13 0A 58 A6 9A 80 80".split()}}]
        assert dt < 0.2
    finally:
        svc.nav_emulator.enabled = False
        svc._nav_scheduler.stop()
        port.stop()
        os.close(master)
        os.close(slave)
