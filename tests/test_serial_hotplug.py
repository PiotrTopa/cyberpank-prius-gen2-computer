"""SerialPort started while its device is absent must still connect later.

The backend starts the gateway SerialPort on a "pending" placeholder path when
the gateway is unpowered (key-off) and retargets it on USB hotplug. Before
2026-11-04 start() returned without spawning the reader thread when the open
failed, so retarget() flipped flags nobody acted on and the gateway stayed
deaf until the next backend restart.
"""
import threading
import time

import pytest

from cyberpunk_computer.io import serial_io
from cyberpunk_computer.io.serial_io import SerialConfig, SerialPort

pytestmark = pytest.mark.skipif(not serial_io.SERIAL_AVAILABLE,
                                reason="pyserial not installed")


class FakeSerial:
    def __init__(self, port, baudrate, timeout):
        self.port = port
        self.is_open = True

    def readline(self):
        time.sleep(0.01)
        return b""

    def write(self, data):
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False


@pytest.fixture
def present(monkeypatch):
    devices = set()

    def fake_serial(port, baudrate, timeout):
        if port not in devices:
            raise OSError(2, "could not open port %s" % port)
        return FakeSerial(port, baudrate, timeout)
    monkeypatch.setattr(serial_io.serial, "Serial", fake_serial)
    return devices


def _wait(pred, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_start_without_device_then_retarget_connects(present):
    p = SerialPort(SerialConfig(port="/dev/prius-gateway-pending",
                                reconnect_delay=0.05))
    try:
        assert p.start() is False
        assert not p.is_connected()
        assert p._reader_thread is not None and p._reader_thread.is_alive()
        time.sleep(0.2)
        assert not p.is_connected()            # placeholder never opens
        present.add("/dev/real-gateway")
        p.retarget("/dev/real-gateway")
        assert _wait(p.is_connected)
        assert p.config.port == "/dev/real-gateway"
    finally:
        p.stop()


def test_device_appearing_on_the_same_path_connects_too(present):
    """Key-off/key-on with a stable by-id path: no retarget, just a retry."""
    p = SerialPort(SerialConfig(port="/dev/by-id/gateway", reconnect_delay=0.05))
    try:
        assert p.start() is False
        present.add("/dev/by-id/gateway")
        assert _wait(p.is_connected)
    finally:
        p.stop()


def test_second_start_does_not_spawn_duplicate_threads(present):
    present.add("/dev/x")
    p = SerialPort(SerialConfig(port="/dev/x", reconnect_delay=0.05))
    try:
        before = threading.active_count()
        assert p.start() is True
        after_first = threading.active_count()
        assert p.start() is True
        assert threading.active_count() == after_first
        assert after_first - before >= 2
    finally:
        p.stop()


def test_no_auto_reconnect_keeps_the_old_contract(present):
    p = SerialPort(SerialConfig(port="/dev/none", auto_reconnect=False))
    assert p.start() is False
    assert p._reader_thread is None
