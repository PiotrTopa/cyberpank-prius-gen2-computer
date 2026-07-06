"""Integration tests for serial port auto-recovery.

Tests the escalating USB recovery strategy in SerialPort.force_reconnect():

1. Device still on bus  → simple close + reopen (DTR toggle)
2. Device gone from bus → parent USB hub unbind/bind via sysfs

These tests mock the filesystem and subprocess calls so they run without
real hardware.
"""

import os
import time
import tempfile
import shutil
import threading
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeSysfs:
    """Create a temporary sysfs-like tree for a USB-CDC device.

    Layout::
        <tmpdir>/sys/class/tty/<tty_name>/device →
            <tmpdir>/sys/devices/1-1.1:1.0
        <tmpdir>/sys/devices/1-1.1:1.0/  (interface)
        <tmpdir>/sys/devices/1-1.1/       (usb device)
        <tmpdir>/sys/devices/1-1/          (hub)

    ``remove_device()`` deletes the tty symlink so the device appears to have
    disappeared from the kernel (simulates failed USB re-enumeration).
    """

    def __init__(self, tty_name: str = "ttyACM0", hub_id: str = "1-1"):
        self.root = tempfile.mkdtemp(prefix="fakesysfs_")
        self.tty_name = tty_name
        self.hub_id = hub_id
        self.usb_dev_id = f"{hub_id}.1"

        # Create the nested device path hierarchy (mirrors real sysfs):
        #   <root>/sys/devices/<hub_id>/<usb_dev_id>/<usb_dev_id>:1.0
        self.hub_dir = os.path.join(
            self.root, "sys", "devices", self.hub_id
        )
        self.usb_dev_dir = os.path.join(
            self.hub_dir, self.usb_dev_id
        )
        self.iface_dir = os.path.join(
            self.usb_dev_dir, f"{self.usb_dev_id}:1.0"
        )
        os.makedirs(self.iface_dir, exist_ok=True)

        # Create the tty class symlink
        tty_class = os.path.join(self.root, "sys", "class", "tty", tty_name)
        os.makedirs(tty_class, exist_ok=True)

        # device → interface
        device_link = os.path.join(tty_class, "device")
        os.symlink(self.iface_dir, device_link)

    def remove_device(self):
        """Simulate the device disappearing from the kernel."""
        tty_dir = os.path.join(
            self.root, "sys", "class", "tty", self.tty_name
        )
        if os.path.exists(tty_dir):
            shutil.rmtree(tty_dir)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture
def fake_sysfs():
    fs = FakeSysfs()
    yield fs
    fs.cleanup()


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

from cyberpunk_computer.io.serial_io import SerialPort, SerialConfig


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestForceReconnect:
    """Test force_reconnect escalation logic."""

    def _make_port(self, sysfs: FakeSysfs) -> SerialPort:
        """Create a SerialPort pointing at the fake device."""
        cfg = SerialConfig(port=f"/dev/{sysfs.tty_name}", baudrate=115200)
        port = SerialPort(cfg)
        return port

    def test_device_present_escalates_to_hub_reset(self, fake_sysfs):
        """Even when the tty device node still exists, force_reconnect should
        escalate straight to a hub reset: a MicroPython RP2040 CDC wedge is a
        host/link-level stall that a plain close/reopen cannot clear, so the
        device must be re-enumerated via the parent hub."""
        port = self._make_port(fake_sysfs)
        # Monkey-patch os.path.realpath to resolve our fake port name to
        # the fake sysfs tty name.
        tty_path = os.path.join(
            fake_sysfs.root, "sys", "class", "tty", fake_sysfs.tty_name
        )

        orig_exists = os.path.exists
        orig_realpath = os.path.realpath

        def patched_realpath(path):
            if path == port.config.port:
                return f"/dev/{fake_sysfs.tty_name}"
            return orig_realpath(path)

        def patched_exists(path):
            if path == f"/sys/class/tty/{fake_sysfs.tty_name}/device":
                return os.path.exists(
                    os.path.join(
                        fake_sysfs.root,
                        "sys", "class", "tty",
                        fake_sysfs.tty_name, "device",
                    )
                )
            return orig_exists(path)

        with mock.patch("os.path.realpath", side_effect=patched_realpath):
            with mock.patch("os.path.exists", side_effect=patched_exists):
                with mock.patch.object(port, "_handle_disconnect") as mock_disc:
                    with mock.patch.object(port, "_reset_usb_hub") as mock_hub:
                        port.force_reconnect()

                        mock_disc.assert_called_once()
                        mock_hub.assert_called_once()

    def test_device_gone_triggers_hub_reset(self, fake_sysfs):
        """When the tty device has disappeared from the kernel, force_reconnect
        should escalate to a hub reset using the cached hub ID."""
        port = self._make_port(fake_sysfs)

        # Prime the hub cache as if the device was seen at startup.
        port._cached_hub_id = fake_sysfs.hub_id

        # Remove the device from fake sysfs.
        fake_sysfs.remove_device()

        orig_exists = os.path.exists
        orig_realpath = os.path.realpath

        def patched_realpath(path):
            if path == port.config.port:
                return f"/dev/{fake_sysfs.tty_name}"
            return orig_realpath(path)

        def patched_exists(path):
            if path == f"/sys/class/tty/{fake_sysfs.tty_name}/device":
                return False
            return orig_exists(path)

        with mock.patch("os.path.realpath", side_effect=patched_realpath):
            with mock.patch("os.path.exists", side_effect=patched_exists):
                with mock.patch.object(port, "_handle_disconnect") as mock_disc:
                    with mock.patch.object(port, "_reset_usb_hub") as mock_hub:
                        port.force_reconnect()

                        mock_disc.assert_called_once()
                        mock_hub.assert_called_once_with(fake_sysfs.hub_id)

    def test_hub_id_cached_on_first_lookup(self, fake_sysfs):
        """_find_parent_hub_id should cache the hub ID and return it even
        after the device disappears."""
        port = self._make_port(fake_sysfs)

        orig_exists = os.path.exists
        orig_realpath = os.path.realpath

        fake_device_dir = os.path.join(
            fake_sysfs.root, "sys", "class", "tty",
            fake_sysfs.tty_name, "device",
        )

        def patched_exists(path):
            if path == f"/sys/class/tty/{fake_sysfs.tty_name}/device":
                return os.path.lexists(fake_device_dir)
            return orig_exists(path)

        def patched_realpath(path):
            if path == f"/sys/class/tty/{fake_sysfs.tty_name}/device":
                return orig_realpath(fake_device_dir)
            return orig_realpath(path)

        with mock.patch("os.path.exists", side_effect=patched_exists):
            with mock.patch("os.path.realpath", side_effect=patched_realpath):
                # First lookup — device present.
                hub_id = port._find_parent_hub_id(fake_sysfs.tty_name)
                assert hub_id == fake_sysfs.hub_id
                assert port._cached_hub_id == fake_sysfs.hub_id

        # Remove the device.
        fake_sysfs.remove_device()

        with mock.patch("os.path.exists", side_effect=patched_exists):
            with mock.patch("os.path.realpath", side_effect=patched_realpath):
                # Second lookup — device gone, should use cache.
                hub_id = port._find_parent_hub_id(fake_sysfs.tty_name)
                assert hub_id == fake_sysfs.hub_id

    def test_hub_reset_calls_sysfs_write(self):
        """_reset_usb_hub should write the hub ID to the sysfs unbind/bind
        files."""
        written = {}

        def fake_open(path, mode="r"):
            if mode == "w" and ("unbind" in path or "bind" in path):
                class FakeFile:
                    def __enter__(self):
                        return self
                    def __exit__(self, *a):
                        pass
                    def write(self, data):
                        written[path] = data
                return FakeFile()
            return open.__class__(path, mode)

        with mock.patch("builtins.open", side_effect=fake_open):
            with mock.patch("time.sleep"):
                SerialPort._reset_usb_hub("1-1")

        assert written.get("/sys/bus/usb/drivers/usb/unbind") == "1-1"
        assert written.get("/sys/bus/usb/drivers/usb/bind") == "1-1"

    def test_hub_reset_falls_back_to_sudo(self):
        """_reset_usb_hub should fall back to sudo when direct sysfs write
        fails with PermissionError."""
        def fake_open(path, mode="r"):
            if mode == "w" and ("unbind" in path or "bind" in path):
                raise PermissionError("no access")
            return open.__class__(path, mode)

        with mock.patch("builtins.open", side_effect=fake_open):
            with mock.patch("time.sleep"):
                with mock.patch("subprocess.run") as mock_run:
                    SerialPort._reset_usb_hub("1-1")

                    assert mock_run.call_count == 2
                    # First call: unbind
                    args0 = mock_run.call_args_list[0]
                    assert "unbind" in str(args0)
                    # Second call: bind
                    args1 = mock_run.call_args_list[1]
                    assert "bind" in str(args1)

    def test_device_gone_no_cache_logs_error(self, fake_sysfs):
        """When device is gone AND no cached hub ID exists, should log an
        error but not crash."""
        port = self._make_port(fake_sysfs)
        # No cached hub ID.
        port._cached_hub_id = None

        fake_sysfs.remove_device()

        orig_exists = os.path.exists
        orig_realpath = os.path.realpath

        def patched_realpath(path):
            if path == port.config.port:
                return f"/dev/{fake_sysfs.tty_name}"
            return orig_realpath(path)

        def patched_exists(path):
            if path == f"/sys/class/tty/{fake_sysfs.tty_name}/device":
                return False
            return orig_exists(path)

        with mock.patch("os.path.realpath", side_effect=patched_realpath):
            with mock.patch("os.path.exists", side_effect=patched_exists):
                with mock.patch.object(port, "_handle_disconnect"):
                    with mock.patch.object(port, "_reset_usb_hub") as mock_hub:
                        # Should not raise.
                        port.force_reconnect()
                        mock_hub.assert_not_called()


class TestMaybeRecoverPowerbox:
    """Test the service-level recovery orchestration."""

    def test_recovery_disabled_noop(self):
        """When powerbox_auto_recover is False, _maybe_recover_powerbox
        should be a no-op."""
        from cyberpunk_computer.backend.service import BackendService, BackendConfig

        cfg = BackendConfig(powerbox_auto_recover=False)
        svc = BackendService.__new__(BackendService)
        svc.config = cfg
        svc._powerbox_serial = mock.MagicMock()
        svc._pb_recover_last = 0.0
        svc._pb_recover_attempts = 0

        pb = mock.MagicMock()
        svc._maybe_recover_powerbox(pb, age=100.0)

        svc._powerbox_serial.force_reconnect.assert_not_called()

    def test_recovery_respects_cooldown(self):
        """Should not fire if cooldown hasn't elapsed."""
        from cyberpunk_computer.backend.service import BackendService, BackendConfig

        cfg = BackendConfig(powerbox_auto_recover=True, powerbox_recover_cooldown_s=30.0)
        svc = BackendService.__new__(BackendService)
        svc.config = cfg
        svc._powerbox_serial = mock.MagicMock()
        svc._pb_recover_last = time.time()  # just fired
        svc._pb_recover_attempts = 0

        pb = mock.MagicMock()
        svc._maybe_recover_powerbox(pb, age=100.0)

        svc._powerbox_serial.force_reconnect.assert_not_called()

    def test_recovery_fires_when_enabled(self):
        """When enabled and cooldown elapsed, should call force_reconnect."""
        from cyberpunk_computer.backend.service import BackendService, BackendConfig

        cfg = BackendConfig(powerbox_auto_recover=True, powerbox_recover_cooldown_s=1.0)
        svc = BackendService.__new__(BackendService)
        svc.config = cfg
        svc._powerbox_serial = mock.MagicMock()
        svc._pb_recover_last = 0.0  # long ago
        svc._pb_recover_attempts = 0

        pb = mock.MagicMock()
        svc._maybe_recover_powerbox(pb, age=100.0)

        svc._powerbox_serial.force_reconnect.assert_called_once()
        assert svc._pb_recover_attempts == 1
