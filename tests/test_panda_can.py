"""io.panda_can — the broadcast CAN source (comma.ai panda over libusb/ctypes).

Protocol facts are checked against the upstream reference (commaai/panda
python/__init__.py + board/main_comms.h, 2026-11): packet framing, the
SAFETY_SILENT + power-save-off + heartbeat connect sequence, the health
struct, and the reconnect behaviour when the device drops off the bus.
"""

import struct
import threading
import time

import pytest

from cyberpunk_computer.io import panda_can as pc
from cyberpunk_computer.io.panda_can import (
    HEALTH_PACKET_VERSION, HEALTH_STRUCT, HEALTH_FLAG_POWER_SAVE_ENABLED,
    LIBUSB_ERROR_NO_DEVICE, REQ_HEARTBEAT, REQ_HEALTH, REQ_PACKET_VERSIONS,
    REQ_POWER_SAVE, REQ_RESET_COMMS, REQ_SAFETY_MODE, REQ_SERIAL, REQ_VERSION,
    SAFETY_SILENT, PandaCanConfig, PandaCanSource, UsbError,
    pack_can_buffer, unpack_can_buffer, parse_health,
)


# ── framing ──────────────────────────────────────────────────────────────────

def test_roundtrip_standard_and_extended():
    frames = [
        (0x03B, bytes([0x0F, 0x80, 0x00, 0xE5, 0x00]), 0),
        (0x3CB, bytes(range(7)), 1),
        (0x18DAF110, bytes([1, 2, 3, 4, 5, 6, 7, 8]), 2),
        (0x0B4, bytes(8), 0),
    ]
    buf = pack_can_buffer(frames)
    out, rest = unpack_can_buffer(buf)
    assert rest == b""
    assert out == frames


def test_reference_layout_header_bits():
    # Hand-built header per the reference: dlc<<4 | bus<<1, w = addr<<3.
    addr, data, bus = 0x3CB, bytes([0xAA] * 7), 2
    w = addr << 3
    head = bytes([(7 << 4) | (bus << 1), w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF, (w >> 24) & 0xFF])
    cs = 0
    for b in head + data:
        cs ^= b
    out, rest = unpack_can_buffer(head + bytes([cs]) + data)
    assert out == [(addr, data, bus)]


def test_record_split_across_transfers():
    buf = pack_can_buffer([(0x03B, bytes(5), 0), (0x0B4, bytes(8), 0)])
    cut = 6 + 5 + 3           # inside the second record's header/data
    out1, rest = unpack_can_buffer(buf[:cut])
    assert [f[0] for f in out1] == [0x03B]
    assert len(rest) == 3
    out2, rest2 = unpack_can_buffer(rest + buf[cut:])
    assert [f[0] for f in out2] == [0x0B4]
    assert rest2 == b""


def test_returned_and_rejected_flags_map_to_bus_offsets():
    buf = bytearray(pack_can_buffer([(0x100, bytes(2), 0)]))
    buf[1] |= 0x2             # returned
    buf[5] ^= 0x2             # keep checksum valid
    out, _ = unpack_can_buffer(bytes(buf))
    assert out[0][2] == 128
    buf = bytearray(pack_can_buffer([(0x100, bytes(2), 0)]))
    buf[1] |= 0x1             # rejected
    buf[5] ^= 0x1
    out, _ = unpack_can_buffer(bytes(buf))
    assert out[0][2] == 192


def test_checksum_error_raises():
    buf = bytearray(pack_can_buffer([(0x03B, bytes(5), 0)]))
    buf[-1] ^= 0xFF
    with pytest.raises(ValueError):
        unpack_can_buffer(bytes(buf))


# ── health ───────────────────────────────────────────────────────────────────

def make_health(flags=0, safety_mode=SAFETY_SILENT, voltage=12345):
    vals = [1234, voltage, 100, 0, 0, 0, 7, 0, flags, 0, safety_mode, 0, 0, 0, 0, 0, 0, 0, 0, 0, 41]
    return HEALTH_STRUCT.pack(*vals)


def test_parse_health():
    h = parse_health(make_health(flags=HEALTH_FLAG_POWER_SAVE_ENABLED, safety_mode=19))
    assert h.uptime_s == 1234 and h.voltage_mv == 12345
    assert h.power_save and h.safety_mode == 19 and h.rx_buffer_overflow == 7
    assert h.temperature_c == 41
    assert parse_health(b"\x00" * 10) is None
    assert HEALTH_STRUCT.size == 48


# ── source thread with a fake USB handle ─────────────────────────────────────

class FakeUsb:
    """Stands in for LibusbPanda: scripted bulk reads, recorded controls."""

    def __init__(self, script, present=True, health=None, versions=None):
        self.script = list(script)        # items: bytes | Exception
        self.present = present
        self.controls = []
        self.location = "bus 001 dev 014"
        self.health = health
        self.versions = versions if versions is not None else struct.pack("<II", HEALTH_PACKET_VERSION, 0)
        self.closed = False
        self._open = False

    def open(self):
        self._open = self.present
        return self.present

    def close(self):
        self._open = False

    def exit(self):
        self.closed = True

    @property
    def is_open(self):
        return self._open

    def control_out(self, request, value=0, index=0, timeout_ms=500):
        self.controls.append(("out", request, value, index))

    def control_in(self, request, value=0, index=0, length=64, timeout_ms=500):
        self.controls.append(("in", request, value, index))
        if request == REQ_SERIAL:
            return b"1f0042000c50484e52343020"
        if request == REQ_VERSION:
            return b"v24.2.3-release"
        if request == REQ_PACKET_VERSIONS:
            return self.versions
        if request == REQ_HEALTH:
            return self.health or b""
        return b""

    def bulk_read(self, endpoint=0x81, length=16384, timeout_ms=20):
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        time.sleep(timeout_ms / 1000.0)
        return b""


def run_source(usbs, frames_expected, cfg=None, timeout=3.0):
    got = []
    links = []
    done = threading.Event()

    def on_frame(can_id, data, bus, t_wall, t_mono):
        got.append((can_id, data, bus))
        if len(got) >= frames_expected:
            done.set()

    def on_link(up, info):
        links.append(up)

    pool = list(usbs)

    def factory():
        return pool.pop(0) if pool else FakeUsb([], present=False)

    src = PandaCanSource(on_frame, on_link, config=cfg or PandaCanConfig(reconnect_s=0.01, read_timeout_ms=2),
                         usb_factory=factory)
    # libusb itself is not needed with a fake handle.
    pc_available = pc.libusb_available
    pc.libusb_available = lambda: True
    try:
        assert src.start()
        done.wait(timeout)
    finally:
        pc.libusb_available = pc_available
    return src, got, links


def test_connect_sequence_is_silent_first_then_frames_flow():
    data = pack_can_buffer([(0x03B, bytes([0x0F, 0x80, 0, 0xE5, 0]), 0), (0x0B4, bytes(8), 1)])
    usb = FakeUsb([data])
    src, got, links = run_source([usb], 2)
    st = src.status()
    src.stop()
    outs = [c for c in usb.controls if c[0] == "out"]
    # First thing on the wire: SAFETY_SILENT (mode 0), then power save off,
    # heartbeat, comms reset. Nothing ever goes to the CAN TX endpoint.
    assert outs[0] == ("out", REQ_SAFETY_MODE, SAFETY_SILENT, 0)
    assert outs[1] == ("out", REQ_POWER_SAVE, 0, 0)
    assert outs[2] == ("out", REQ_HEARTBEAT, 0, 0)
    assert outs[3] == ("out", REQ_RESET_COMMS, 0, 0)
    assert [g[0] for g in got] == [0x03B, 0x0B4]
    assert got[1][2] == 1
    assert st.connected and st.frames == 2 and st.serial.startswith("1f00")
    assert st.fw_version == "v24.2.3-release" and st.health_decodable
    assert st.bus_counts == {0: 1, 1: 1}
    assert links[:1] == [True]           # stop() appends the final False


def test_bus_filter_and_rejected_frames_are_dropped():
    buf = bytearray(pack_can_buffer([(0x100, bytes(2), 0)]))
    buf[1] |= 0x1
    buf[5] ^= 0x1             # rejected flag -> bus 192
    data = pack_can_buffer([(0x03B, bytes(5), 0), (0x03B, bytes(5), 2)]) + bytes(buf)
    usb = FakeUsb([data])
    src, got, _ = run_source([usb], 1, cfg=PandaCanConfig(bus=2, reconnect_s=0.01, read_timeout_ms=2))
    time.sleep(0.05)
    src.stop()
    assert got == [(0x03B, bytes(5), 2)]
    assert src.status().frames_filtered == 2


def test_device_drop_reconnects_and_reports_link_edges():
    first = FakeUsb([pack_can_buffer([(0x03B, bytes(5), 0)]), UsbError(LIBUSB_ERROR_NO_DEVICE, "bulk")])
    second = FakeUsb([pack_can_buffer([(0x0B4, bytes(8), 0)])])
    src, got, links = run_source([first, second], 2)
    st = src.status()
    src.stop()
    assert [g[0] for g in got] == [0x03B, 0x0B4]
    assert links[:3] == [True, False, True]
    assert st.connects == 2 and st.usb_errors >= 1 and first.closed


def test_absent_device_keeps_retrying_without_frames():
    src, got, links = run_source([FakeUsb([], present=False)], 1, timeout=0.1)
    src.stop()
    assert got == [] and links == []
    assert src.status().connected is False


def test_heartbeat_and_health_fix_power_save():
    health = make_health(flags=HEALTH_FLAG_POWER_SAVE_ENABLED, safety_mode=19)
    usb = FakeUsb([], health=health)
    cfg = PandaCanConfig(reconnect_s=0.01, read_timeout_ms=2, heartbeat_s=0.02, health_poll_s=0.03)
    src, got, _ = run_source([usb], 1, cfg=cfg, timeout=0.25)
    st = src.status()
    src.stop()
    outs = [c for c in usb.controls if c[0] == "out"]
    hbs = [c for c in outs if c[1] == REQ_HEARTBEAT]
    assert len(hbs) >= 3, "heartbeat must repeat (firmware power-saves after 2 s without one)"
    # Health said power save on + mode 19: both re-asserted.
    after_health = outs[outs.index(("out", REQ_HEARTBEAT, 0, 0)) + 1:]
    assert ("out", REQ_POWER_SAVE, 0, 0) in after_health
    assert ("out", REQ_SAFETY_MODE, SAFETY_SILENT, 0) in after_health
    assert st.power_save_fixes >= 1 and st.safety_fixes >= 1
    assert st.safety_mode == SAFETY_SILENT and st.power_save is False


def test_unknown_health_version_is_not_decoded():
    usb = FakeUsb([], versions=struct.pack("<II", 0x12345678, 0))
    cfg = PandaCanConfig(reconnect_s=0.01, read_timeout_ms=2, health_poll_s=0.02)
    src, _, _ = run_source([usb], 1, cfg=cfg, timeout=0.1)
    src.stop()
    assert not src.status().health_decodable
    assert all(c[1] != REQ_HEALTH for c in usb.controls)


def test_checksum_error_resyncs():
    bad = bytearray(pack_can_buffer([(0x03B, bytes(5), 0)]))
    bad[-1] ^= 0x55
    good = pack_can_buffer([(0x0B4, bytes(8), 0)])
    usb = FakeUsb([bytes(bad), good])
    src, got, _ = run_source([usb], 1)
    src.stop()
    assert [g[0] for g in got] == [0x0B4]
    assert src.status().checksum_errors == 1


# ── 2022-era firmware (f120999e, found on the car's panda 2026-11-05) ─────────

def test_v2_framing_roundtrip_and_detection():
    from cyberpunk_computer.io.panda_can import pack_can_buffer_v2, unpack_can_buffer_v2
    frames = [(0x03B, bytes([1, 2, 3, 4, 5]), 0), (0x0B4, bytes(range(8)), 1)] * 20   # > 64 B
    buf = pack_can_buffer_v2(frames)
    assert buf[0] == 0 and buf[64] == 1
    assert unpack_can_buffer_v2(buf) == frames
    assert PandaCanSource._detect_format(buf) == "v2"
    assert PandaCanSource._detect_format(pack_can_buffer(frames)) == "v4"
    bad = bytearray(buf)
    bad[64] = 7
    with pytest.raises(ValueError):
        unpack_can_buffer_v2(bytes(bad))


def test_health_v9_layout_from_live_capture():
    # Raw 0xd2 reply of fw DEV-f120999e-DEBUG (54 bytes), probe 3 on 2026-11-05.
    raw = bytes.fromhex("920a0000d10f00000000000000000000038b3c00000000000000000000000000"
                        "04000100000001000000010100" "00000000" "3bda503b" "00")
    h = parse_health(raw, "v9")
    assert h.uptime_s == 2706 and h.voltage_mv == 4049
    assert h.safety_mode == SAFETY_SILENT and h.power_save is False
    assert h.heartbeat_lost is True and h.ignition is True
    assert parse_health(raw[:40], "v9") is None


def test_old_firmware_connect_uses_v9_health_and_v2_frames():
    from cyberpunk_computer.io.panda_can import pack_can_buffer_v2
    health = bytes.fromhex("920a0000d10f00000000000000000000038b3c00000000000000000000000000"
                           "04000100000001000001010100" "00000000" "3bda503b" "00")  # power save on
    usb = FakeUsb([pack_can_buffer_v2([(0x3CB, bytes(7), 0)])], health=health,
                  versions=bytes([9, 2]))
    cfg = PandaCanConfig(reconnect_s=0.01, read_timeout_ms=2, health_poll_s=0.02)
    src, got, _ = run_source([usb], 1, cfg=cfg, timeout=0.3)
    time.sleep(0.08)
    st = src.status()
    src.stop()
    assert got == [(0x3CB, bytes(7), 0)]
    assert st.packet_format == "v2" and st.health_layout == "v9" and st.health_decodable
    assert st.power_save_fixes >= 1
    # set_safety_mode gets the long timeout (2.9 s measured on this firmware).
    assert any(c[0] == "out" and c[1] == REQ_SAFETY_MODE for c in usb.controls)


def test_bench_safety_mode_is_opt_in_and_logged():
    usb = FakeUsb([])
    cfg = PandaCanConfig(reconnect_s=0.01, read_timeout_ms=2, safety_mode=19)
    src, _, _ = run_source([usb], 1, cfg=cfg, timeout=0.05)
    src.stop()
    assert ("out", REQ_SAFETY_MODE, 19, 0) in usb.controls
    assert PandaCanConfig().safety_mode == SAFETY_SILENT
