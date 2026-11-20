"""
Panda CAN source — unsolicited (broadcast) CAN frames with millisecond latency.

The backend reads the vehicle bus through TWO independent flows:

* **comma.ai panda** (this module, USB ``bbaa:ddcc``): passive listener for
  every broadcast frame (0x03B battery, 0x3CB SOC, 0x0B4 speed, steering,
  wheel speeds, ...). Frames go straight from the USB reader thread into the
  decoder and the store (``IngressController.ingest_broadcast_can``), without
  the engine-tick queue, so a frame reaches the state within a millisecond or
  two of leaving the bus.
* **RP2040 gateway** (``serial_io`` + ``factory.VirtualTwin``): solicited
  traffic only — periodic OBD/UDS subscriptions, one-shot requests, DTC scans.
  Gateway firmware 3.x forwards no passive frames unless its ``sniff`` mode is
  on, and ``sniff`` disables ``req``/``sub``, so one device cannot do both.

Safety: the panda is put in **SAFETY_SILENT** (mode 0) on every connect. In
that mode the CAN controllers run in bus-monitoring mode: no transmissions, no
ACK bits, no error frames. Nothing in this module writes to the CAN TX endpoint.

Host dependency: ``libusb-1.0`` via :mod:`ctypes` (the shared library ships
with postmarketOS; no pyusb/libusb1/panda Python package is needed). The
backend runs as root, so ``/dev/bus/usb`` is writable; a non-root deployment
needs a udev rule for ``bbaa:ddcc``.

Panda USB protocol (verified against commaai/panda ``python/__init__.py`` and
``board/main_comms.h``, 2026-11):

* bulk IN endpoint 0x81 streams ``CANPacket_t`` records. Two generations:
  **v4 (2023+)**: 6-byte header ``[dlc<<4 | bus<<1 | fd, w0, w1, w2, w3,
  checksum]`` with ``w = addr<<3 | extended<<2 | returned<<1 | rejected``
  little-endian, then ``dlc_to_len[dlc]`` data bytes; XOR of header[0:5]+data
  equals header[5]; a record may straddle two bulk transfers. **v2 (2021-
  2022, e.g. fw ``DEV-f120999e-DEBUG`` found on the car's panda 2026-11-05)**:
  every 64-byte USB packet starts with a packet counter byte, records have the
  same 5-byte header without checksum, the tail of a record continues in the
  next USB packet of the same transfer. The generation is told apart by the
  ``0xdd`` reply: 2 bytes ``[health_version, can_packet_version]`` (old) or
  the 4-byte sha256 hashes (new).
* vendor control requests (bmRequestType 0x40 out / 0xC0 in):
  ``0xdc`` set_safety_mode(mode, param), ``0xe7`` set_power_save(on),
  ``0xf3`` heartbeat(engaged), ``0xc0`` reset communications,
  ``0xd2`` health, ``0xdd`` (health, can) packet versions, ``0xd6`` fw
  version string, ``0xd0`` serial.
* the firmware drops to SILENT **and enters power save (CAN transceivers
  off)** when the host heartbeat stops for 2 s (ignition off) / 5 s, so the
  reader sends ``0xf3`` every second and re-asserts power-save off when the
  health flags say it was entered.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ── protocol constants ───────────────────────────────────────────────────────

PANDA_VIDS = (0xBBAA, 0x3801)
PANDA_PID = 0xDDCC          # application firmware (0xDDEE = DFU bootloader)

EP_CAN_IN = 0x81            # bulk IN: CAN RX stream
BULK_READ_SIZE = 16384      # same as the reference library

REQUEST_OUT = 0x40          # host->device | vendor | device
REQUEST_IN = 0xC0           # device->host | vendor | device

REQ_RESET_COMMS = 0xC0
REQ_SERIAL = 0xD0
REQ_HEALTH = 0xD2
REQ_VERSION = 0xD6
REQ_SAFETY_MODE = 0xDC
REQ_PACKET_VERSIONS = 0xDD
REQ_POWER_SAVE = 0xE7
REQ_HEARTBEAT = 0xF3

SAFETY_SILENT = 0           # bus-monitoring mode, no ACKs, no TX
SAFETY_NOOUTPUT = 19        # ACKs frames, no TX (NOT used: not fully passive)

CANPACKET_HEAD_SIZE = 6
DLC_TO_LEN = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)

# board/health.h (commaai/panda master, 2026-11): sha256(file)[:4] little-endian
# is what the firmware returns as its health packet version (0xdd). The struct
# is only decoded when the device reports exactly this version.
HEALTH_PACKET_VERSION = 0xD330EEAC
HEALTH_STRUCT = struct.Struct("<IHHIIIIIHBBHBHBBHHHHB")
# board/health.h at f120999e (2022-09-15): HEALTH_PACKET_VERSION 9, 54 bytes.
HEALTH_V9_STRUCT = struct.Struct("<IIIIIIIIBBBBBBHBBBHIfB")
CONTROL_TIMEOUT_MS = 5000
SAFETY_MODE_TIMEOUT_MS = 15000   # fw f120999e re-inits all CAN cores: 2.9 s measured
HEALTH_FLAG_IGNITION_LINE = 1 << 0
HEALTH_FLAG_IGNITION_CAN = 1 << 1
HEALTH_FLAG_POWER_SAVE_ENABLED = 1 << 3
HEALTH_FLAG_HEARTBEAT_LOST = 1 << 4

# libusb error codes (libusb.h)
LIBUSB_SUCCESS = 0
LIBUSB_ERROR_IO = -1
LIBUSB_ERROR_ACCESS = -3
LIBUSB_ERROR_NO_DEVICE = -4
LIBUSB_ERROR_NOT_FOUND = -5
LIBUSB_ERROR_BUSY = -6
LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_OVERFLOW = -8
LIBUSB_ERROR_PIPE = -9
LIBUSB_ERROR_INTERRUPTED = -10
LIBUSB_ERROR_NO_MEM = -11
LIBUSB_ERROR_NOT_SUPPORTED = -12
LIBUSB_ERROR_OTHER = -99


# ── pure helpers (unit-tested) ───────────────────────────────────────────────

def xor_checksum(data: bytes) -> int:
    res = 0
    for b in data:
        res ^= b
    return res


def unpack_can_buffer(buf: bytes) -> Tuple[List[Tuple[int, bytes, int]], bytes]:
    """Split a bulk-IN byte stream into ``(can_id, data, bus)`` records.

    Returns the records and the unconsumed tail (a record cut by the transfer
    boundary; prepend it to the next read). A checksum mismatch raises
    ``ValueError`` — the caller drops the buffer and resynchronises on the
    next transfer (records never straddle more than one boundary).
    """
    out: List[Tuple[int, bytes, int]] = []
    pos = 0
    n = len(buf)
    while n - pos >= CANPACKET_HEAD_SIZE:
        h0 = buf[pos]
        data_len = DLC_TO_LEN[h0 >> 4]
        end = pos + CANPACKET_HEAD_SIZE + data_len
        if end > n:
            break
        if xor_checksum(buf[pos:end]) != 0:
            raise ValueError("CAN packet checksum incorrect at offset %d" % pos)
        w = buf[pos + 1] | (buf[pos + 2] << 8) | (buf[pos + 3] << 16) | (buf[pos + 4] << 24)
        bus = (h0 >> 1) & 0x7
        if (w >> 1) & 0x1:      # returned (our own TX echoed) — never in SILENT
            bus += 128
        if w & 0x1:             # rejected by the safety model
            bus += 192
        out.append((w >> 3, bytes(buf[pos + CANPACKET_HEAD_SIZE:end]), bus))
        pos = end
    return out, bytes(buf[pos:])


def unpack_can_buffer_v2(dat: bytes) -> List[Tuple[int, bytes, int]]:
    """2021-2022 framing (reference ``python/__init__.py`` at f120999e): one
    bulk transfer = 64-byte USB packets, each ``[counter, payload...]``; the
    5-byte record header is the v4 one without the checksum byte; a record
    may continue into the next packet of the SAME transfer."""
    ret: List[Tuple[int, bytes, int]] = []
    counter = 0
    tail = b""
    for i in range(0, len(dat), 64):
        if dat[i] != counter:
            raise ValueError("USB packet counter mismatch (%d != %d)" % (dat[i], counter))
        counter += 1
        chunk = tail + dat[i + 1:i + 64]
        tail = b""
        pos = 0
        n = len(chunk)
        while pos < n:
            data_len = DLC_TO_LEN[chunk[pos] >> 4]
            end = pos + 5 + data_len
            if end > n:
                tail = chunk[pos:]
                break
            h0 = chunk[pos]
            w = chunk[pos + 1] | (chunk[pos + 2] << 8) | (chunk[pos + 3] << 16) | (chunk[pos + 4] << 24)
            bus = (h0 >> 1) & 0x7
            if (w >> 1) & 0x1:
                bus += 128
            if w & 0x1:
                bus += 192
            ret.append((w >> 3, bytes(chunk[pos + 5:end]), bus))
            pos = end
    return ret


def coalesce_frames(frames: Sequence[Tuple[int, bytes, int]]
                    ) -> Tuple[List[Tuple[int, bytes, int]], int]:
    """Backlog batch -> newest frame of each (id, bus), in arrival order of
    those newest frames. Ids from 0x700 up (diagnostic requests and replies,
    multi-frame sequences) are all kept. Returns (frames, dropped count)."""
    last: Dict[Tuple[int, int], int] = {}
    for i, (can_id, _payload, bus) in enumerate(frames):
        if can_id < 0x700:
            last[(can_id, bus)] = i
    out = [fr for i, fr in enumerate(frames)
           if fr[0] >= 0x700 or last[(fr[0], fr[2])] == i]
    return out, len(frames) - len(out)


def pack_can_buffer_v2(frames: Sequence[Tuple[int, bytes, int]]) -> bytes:
    """Inverse of :func:`unpack_can_buffer_v2` (tests)."""
    body = bytearray()
    for addr, data, bus in frames:
        dlc = DLC_TO_LEN.index(len(data))
        w = (addr << 3) | ((1 if addr >= 0x800 else 0) << 2)
        body += bytes(((dlc << 4) | ((bus & 0x7) << 1), w & 0xFF, (w >> 8) & 0xFF,
                       (w >> 16) & 0xFF, (w >> 24) & 0xFF)) + bytes(data)
    out = bytearray()
    counter = 0
    for i in range(0, len(body), 63):
        out.append(counter)
        out += body[i:i + 63]
        counter += 1
    return bytes(out)


def pack_can_buffer(frames: Sequence[Tuple[int, bytes, int]]) -> bytes:
    """Inverse of :func:`unpack_can_buffer` (tests, replay tooling)."""
    out = bytearray()
    for addr, data, bus in frames:
        dlc = DLC_TO_LEN.index(len(data))
        w = (addr << 3) | ((1 if addr >= 0x800 else 0) << 2)
        head = bytearray((
            (dlc << 4) | ((bus & 0x7) << 1),
            w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF, (w >> 24) & 0xFF,
        ))
        head.append(xor_checksum(bytes(head) + bytes(data)))
        out += head + bytes(data)
    return bytes(out)


@dataclass
class PandaHealth:
    uptime_s: int
    voltage_mv: int
    current_ma: int
    rx_buffer_overflow: int
    faults: int
    flags: int
    safety_mode: int
    safety_param: int
    temperature_c: int

    @property
    def power_save(self) -> bool:
        return bool(self.flags & HEALTH_FLAG_POWER_SAVE_ENABLED)

    @property
    def heartbeat_lost(self) -> bool:
        return bool(self.flags & HEALTH_FLAG_HEARTBEAT_LOST)

    @property
    def ignition(self) -> bool:
        return bool(self.flags & (HEALTH_FLAG_IGNITION_LINE | HEALTH_FLAG_IGNITION_CAN))


def parse_health(raw: bytes, layout: str = "v16") -> Optional[PandaHealth]:
    """``layout``: "v16" = board/health.h on master (48 bytes, flag bits);
    "v9" = f120999e (54 bytes, one byte per flag)."""
    if layout == "v9":
        if len(raw) < HEALTH_V9_STRUCT.size:
            return None
        f = HEALTH_V9_STRUCT.unpack(raw[:HEALTH_V9_STRUCT.size])
        flags = ((HEALTH_FLAG_IGNITION_LINE if f[8] else 0)
                 | (HEALTH_FLAG_IGNITION_CAN if f[9] else 0)
                 | (HEALTH_FLAG_POWER_SAVE_ENABLED if f[16] else 0)
                 | (HEALTH_FLAG_HEARTBEAT_LOST if f[17] else 0))
        return PandaHealth(
            uptime_s=f[0], voltage_mv=f[1], current_ma=f[2],
            rx_buffer_overflow=f[3],      # can_rx_errs_pkt: the only rx health counter in v9
            faults=f[7], flags=flags, safety_mode=f[13], safety_param=f[14],
            temperature_c=0,
        )
    if len(raw) < HEALTH_STRUCT.size:
        return None
    f = HEALTH_STRUCT.unpack(raw[:HEALTH_STRUCT.size])
    return PandaHealth(
        uptime_s=f[0], voltage_mv=f[1], current_ma=f[2],
        rx_buffer_overflow=f[6], faults=f[7], flags=f[8],
        safety_mode=f[10], safety_param=f[11], temperature_c=f[20],
    )


# ── libusb-1.0 via ctypes ────────────────────────────────────────────────────

class UsbError(OSError):
    def __init__(self, code: int, what: str):
        super().__init__(code, "%s: libusb error %d (%s)" % (what, code, _error_name(code)))
        self.code = code


_LIB: Optional[ctypes.CDLL] = None


def _load_libusb() -> ctypes.CDLL:
    global _LIB
    if _LIB is not None:
        return _LIB
    names = ["libusb-1.0.so.0", "libusb-1.0.so"]
    found = ctypes.util.find_library("usb-1.0")
    if found:
        names.insert(0, found)
    err: Optional[Exception] = None
    for name in names:
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError as e:
            err = e
    else:
        raise OSError("libusb-1.0 not found (%s)" % err)
    c = ctypes
    lib.libusb_init.argtypes = [c.POINTER(c.c_void_p)]
    lib.libusb_init.restype = c.c_int
    lib.libusb_exit.argtypes = [c.c_void_p]
    lib.libusb_exit.restype = None
    lib.libusb_open_device_with_vid_pid.argtypes = [c.c_void_p, c.c_uint16, c.c_uint16]
    lib.libusb_open_device_with_vid_pid.restype = c.c_void_p
    lib.libusb_close.argtypes = [c.c_void_p]
    lib.libusb_close.restype = None
    lib.libusb_set_auto_detach_kernel_driver.argtypes = [c.c_void_p, c.c_int]
    lib.libusb_set_auto_detach_kernel_driver.restype = c.c_int
    lib.libusb_claim_interface.argtypes = [c.c_void_p, c.c_int]
    lib.libusb_claim_interface.restype = c.c_int
    lib.libusb_release_interface.argtypes = [c.c_void_p, c.c_int]
    lib.libusb_release_interface.restype = c.c_int
    lib.libusb_control_transfer.argtypes = [
        c.c_void_p, c.c_uint8, c.c_uint8, c.c_uint16, c.c_uint16,
        c.c_void_p, c.c_uint16, c.c_uint,
    ]
    lib.libusb_control_transfer.restype = c.c_int
    lib.libusb_bulk_transfer.argtypes = [
        c.c_void_p, c.c_ubyte, c.c_void_p, c.c_int, c.POINTER(c.c_int), c.c_uint,
    ]
    lib.libusb_bulk_transfer.restype = c.c_int
    lib.libusb_get_device.argtypes = [c.c_void_p]
    lib.libusb_get_device.restype = c.c_void_p
    lib.libusb_get_bus_number.argtypes = [c.c_void_p]
    lib.libusb_get_bus_number.restype = c.c_uint8
    lib.libusb_get_device_address.argtypes = [c.c_void_p]
    lib.libusb_get_device_address.restype = c.c_uint8
    try:
        lib.libusb_error_name.argtypes = [c.c_int]
        lib.libusb_error_name.restype = c.c_char_p
    except AttributeError:
        pass
    _LIB = lib
    return lib


def _error_name(code: int) -> str:
    try:
        lib = _load_libusb()
        name = lib.libusb_error_name(code)
        return name.decode() if name else str(code)
    except Exception:
        return str(code)


def libusb_available() -> bool:
    try:
        _load_libusb()
        return True
    except OSError:
        return False


class LibusbPanda:
    """Minimal synchronous handle on one panda (interface 0).

    All calls happen on the reader thread; the synchronous libusb API releases
    the GIL for the duration of each transfer.
    """

    def __init__(self, vids: Sequence[int] = PANDA_VIDS, pid: int = PANDA_PID):
        self._lib = _load_libusb()
        self._ctx = ctypes.c_void_p()
        rc = self._lib.libusb_init(ctypes.byref(self._ctx))
        if rc != 0:
            raise UsbError(rc, "libusb_init")
        self._handle: Optional[ctypes.c_void_p] = None
        self._vids = tuple(vids)
        self._pid = pid
        self.location = ""

    # -- lifecycle --
    def open(self) -> bool:
        """Open + claim the first panda found. False when none is attached."""
        for vid in self._vids:
            h = self._lib.libusb_open_device_with_vid_pid(self._ctx, vid, self._pid)
            if h:
                break
        else:
            return False
        handle = ctypes.c_void_p(h)
        self._lib.libusb_set_auto_detach_kernel_driver(handle, 1)
        rc = self._lib.libusb_claim_interface(handle, 0)
        if rc != 0:
            self._lib.libusb_close(handle)
            raise UsbError(rc, "claim interface 0")
        self._handle = handle
        try:
            dev = self._lib.libusb_get_device(handle)
            self.location = "bus %03d dev %03d" % (
                self._lib.libusb_get_bus_number(dev), self._lib.libusb_get_device_address(dev))
        except Exception:
            self.location = "?"
        return True

    def close(self) -> None:
        h, self._handle = self._handle, None
        if h is not None:
            try:
                self._lib.libusb_release_interface(h, 0)
            except Exception:
                pass
            try:
                self._lib.libusb_close(h)
            except Exception:
                pass

    def exit(self) -> None:
        self.close()
        if self._ctx:
            self._lib.libusb_exit(self._ctx)
            self._ctx = ctypes.c_void_p()

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    # -- transfers --
    def control_out(self, request: int, value: int = 0, index: int = 0,
                    timeout_ms: int = CONTROL_TIMEOUT_MS) -> None:
        rc = self._lib.libusb_control_transfer(
            self._handle, REQUEST_OUT, request, value, index, None, 0, timeout_ms)
        if rc < 0:
            raise UsbError(rc, "control 0x%02x" % request)

    def control_in(self, request: int, value: int = 0, index: int = 0, length: int = 64,
                   timeout_ms: int = CONTROL_TIMEOUT_MS) -> bytes:
        buf = (ctypes.c_ubyte * length)()
        rc = self._lib.libusb_control_transfer(
            self._handle, REQUEST_IN, request, value, index, buf, length, timeout_ms)
        if rc < 0:
            raise UsbError(rc, "control 0x%02x" % request)
        return bytes(buf[:rc])

    def bulk_read(self, endpoint: int = EP_CAN_IN, length: int = BULK_READ_SIZE,
                  timeout_ms: int = 20) -> bytes:
        """One bulk IN transfer. Empty bytes on timeout."""
        buf = (ctypes.c_ubyte * length)()
        got = ctypes.c_int(0)
        rc = self._lib.libusb_bulk_transfer(
            self._handle, endpoint, buf, length, ctypes.byref(got), timeout_ms)
        if rc == LIBUSB_ERROR_TIMEOUT:
            # A timed-out transfer may still have moved some bytes.
            return bytes(buf[:got.value]) if got.value else b""
        if rc != 0:
            raise UsbError(rc, "bulk read 0x%02x" % endpoint)
        return bytes(buf[:got.value])


# ── the source thread ────────────────────────────────────────────────────────

FrameCallback = Callable[[int, bytes, int, float, float], None]
LinkCallback = Callable[[bool, dict], None]


@dataclass
class PandaCanConfig:
    enabled: bool = True
    bus: Optional[int] = None          # accept frames from this panda bus only (None = any)
    # SAFETY_SILENT is the only mode for the car. SAFETY_NOOUTPUT (19) makes
    # the panda ACK frames (still no TX from the host): bench use only, when
    # the gateway is the sole other node and nothing would ACK its frames.
    safety_mode: int = SAFETY_SILENT
    heartbeat_s: float = 1.0           # firmware power-saves after 2 s without one
    health_poll_s: float = 5.0
    reconnect_s: float = 2.0           # hub comes up ~9 s after relay ch3 (panda last)
    read_timeout_ms: int = 20
    # Pace of the bulk reads (L48). The car's panda firmware (f120999e, 2022)
    # answers a bulk IN at once with an EMPTY transfer when its CAN queue is
    # empty, so read_timeout_ms never applies and an unpaced loop spins: 4500
    # transfers/s and 38 % of an A55 core on the bench with 1 frame/s (L48-B,
    # 2026-11-20). A read that returns less than a full transfer waits out the
    # rest of this period; a full transfer (a backlog) is followed by the next
    # read at once. 5 ms adds 0..5 ms to a frame's way into the state, far below
    # the 0.1 s state rate limit of the fast ids; the panda queues thousands of
    # frames, ~8 per period at the Prius's 1650 frames/s. 0 = unpaced (as before).
    poll_period_s: float = 0.005
    # Hint for the whole process: the CPython GIL switch interval bounds how
    # long a busy engine thread can delay the reader after a transfer returns.
    switch_interval_s: Optional[float] = 0.002
    # A bulk transfer with more frames than this is a backlog (the reader
    # fell behind the bus, about 1650 frames/s on the Prius): only the newest
    # frame of each id below 0x700 is passed on, so the backlog is worked off
    # in one pass instead of being replayed seconds late. 0 = never coalesce.
    coalesce_over: int = 64


@dataclass
class PandaCanStats:
    connected: bool = False
    location: str = ""
    serial: str = ""
    fw_version: str = ""
    health_version: Optional[int] = None
    health_decodable: bool = False
    health_layout: str = ""            # "v9" | "v16" | ""
    packet_format: str = ""            # "v2" | "v4" | "" (not yet known)
    can_rx_errs: Optional[int] = None  # v9 only: CAN rx error counter (bitrate/termination hint)
    safety_mode: Optional[int] = None
    power_save: Optional[bool] = None
    heartbeat_lost: Optional[bool] = None
    ignition: Optional[bool] = None
    voltage_mv: Optional[int] = None
    rx_buffer_overflow: Optional[int] = None
    connects: int = 0
    frames: int = 0
    frames_filtered: int = 0           # dropped by the bus filter / rejected flag
    bulk_reads: int = 0
    bytes: int = 0
    checksum_errors: int = 0
    usb_errors: int = 0
    power_save_fixes: int = 0
    safety_fixes: int = 0
    last_frame_mono: float = 0.0
    last_frame_wall: float = 0.0
    last_error: str = ""
    bus_counts: Dict[int, int] = field(default_factory=dict)
    coalesced: int = 0                 # frames dropped as older copies in a backlog batch
    batch_max: int = 0                 # largest bulk transfer, frames
    busy_s: float = 0.0                # time spent handling frames (reader thread)


class PandaCanSource:
    """Background reader: panda bulk IN -> ``on_frame(can_id, data, bus, t_wall, t_mono)``.

    ``on_frame`` runs on the reader thread for every accepted frame (so it must
    be fast and thread-safe — ``IngressController.ingest_broadcast_can`` is).
    ``on_link(connected, info)`` runs on the same thread at link edges.
    """

    def __init__(self, on_frame: FrameCallback, on_link: Optional[LinkCallback] = None,
                 config: Optional[PandaCanConfig] = None,
                 usb_factory: Callable[[], "LibusbPanda"] = LibusbPanda,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 on_every_frame: Optional[FrameCallback] = None):
        self.config = config or PandaCanConfig()
        self._on_frame = on_frame
        # Cheap tap that must see every frame, backlog or not (wheel speeds
        # for the position feeder); runs before the backlog coalescing.
        self._on_every_frame = on_every_frame
        self._on_link = on_link
        self._usb_factory = usb_factory
        self._clock = clock
        self._wall = wall
        self._stats = PandaCanStats()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._usb: Optional[LibusbPanda] = None
        self._tail = b""
        self._fps_window_t = 0.0
        self._fps_window_n = 0
        self.fps: float = 0.0

    # -- public --
    def start(self) -> bool:
        if self._thread is not None:
            return True
        if not libusb_available():
            logger.error("Panda CAN source disabled: libusb-1.0 not available")
            return False
        if self.config.switch_interval_s:
            try:
                import sys
                if sys.getswitchinterval() > self.config.switch_interval_s:
                    sys.setswitchinterval(self.config.switch_interval_s)
            except Exception:
                pass
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="PandaCanReader", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None

    def status(self) -> PandaCanStats:
        with self._lock:
            import copy
            s = copy.copy(self._stats)
            s.bus_counts = dict(self._stats.bus_counts)
            return s

    # -- reader thread --
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._connect():
                    self._stop.wait(self.config.reconnect_s)
                    continue
                self._read_loop()
            except UsbError as e:
                self._note_error(str(e))
                logger.warning("Panda link lost: %s", e)
            except Exception:
                logger.exception("Panda reader error")
                self._note_error("exception")
            finally:
                self._disconnect()
            if not self._stop.is_set():
                self._stop.wait(self.config.reconnect_s)

    def _connect(self) -> bool:
        usb = self._usb_factory()
        try:
            if not usb.open():
                usb.exit()
                return False
        except UsbError as e:
            self._note_error(str(e))
            logger.warning("Panda present but not usable: %s", e)
            usb.exit()
            return False
        self._usb = usb
        self._tail = b""
        mode = self.config.safety_mode
        # Passive first, everything else after. set_safety_mode re-inits the
        # CAN cores (2.9 s on fw f120999e), hence its own timeout.
        usb.control_out(REQ_SAFETY_MODE, mode, 0, SAFETY_MODE_TIMEOUT_MS)
        usb.control_out(REQ_POWER_SAVE, 0, 0)
        usb.control_out(REQ_HEARTBEAT, 0, 0)
        usb.control_out(REQ_RESET_COMMS, 0, 0)
        serial = fw = ""
        health_version = None
        layout = ""
        try:
            serial = usb.control_in(REQ_SERIAL, 0, 0, 0x20).split(b"\x00", 1)[0].decode("ascii", "replace")
            fw = usb.control_in(REQ_VERSION, 0, 0, 0x40).split(b"\x00", 1)[0].decode("ascii", "replace")
            vers = usb.control_in(REQ_PACKET_VERSIONS, 0, 0, 16)
            if len(vers) >= 8:
                health_version = struct.unpack_from("<I", vers, 0)[0]
                if health_version == HEALTH_PACKET_VERSION:
                    layout = "v16"
            elif len(vers) >= 1:
                health_version = vers[0]
                if health_version == 9:
                    layout = "v9"
        except UsbError as e:
            logger.debug("Panda identity read failed: %s", e)
        with self._lock:
            s = self._stats
            s.connected = True
            s.connects += 1
            s.location = usb.location
            s.serial = serial
            s.fw_version = fw
            s.health_version = health_version
            s.health_layout = layout
            s.health_decodable = bool(layout)
            s.packet_format = ""
            s.safety_mode = mode
            s.power_save = False
            s.last_error = ""
        if mode != SAFETY_SILENT:
            logger.warning("Panda safety mode %d requested (NOT silent: the panda ACKs frames) — "
                           "bench use only", mode)
        logger.info("Panda connected (%s, serial %s, fw %s): safety mode %d, power save off, "
                    "health %s", usb.location, serial or "?", fw or "?", mode,
                    layout or "version %r (not decoded)" % (health_version,))
        self._emit_link(True)
        return True

    def _disconnect(self) -> None:
        usb, self._usb = self._usb, None
        was = False
        with self._lock:
            was = self._stats.connected
            self._stats.connected = False
        if usb is not None:
            try:
                usb.exit()
            except Exception:
                pass
        if was:
            logger.info("Panda disconnected")
            self._emit_link(False)

    def _read_loop(self) -> None:
        cfg = self.config
        usb = self._usb
        assert usb is not None
        next_hb = self._clock()           # heartbeat due immediately after connect
        next_health = self._clock() + cfg.health_poll_s
        period = cfg.poll_period_s
        while not self._stop.is_set():
            t_read = self._clock()
            data = usb.bulk_read(EP_CAN_IN, BULK_READ_SIZE, cfg.read_timeout_ms)
            if data:
                self._handle_bytes(data)
            now = self._clock()
            if now >= next_hb:
                usb.control_out(REQ_HEARTBEAT, 0, 0)
                if now - next_hb > 1.5 * cfg.heartbeat_s:
                    # We stalled long enough for the firmware to power-save;
                    # bring the transceivers back without waiting for health.
                    usb.control_out(REQ_POWER_SAVE, 0, 0)
                    with self._lock:
                        self._stats.power_save_fixes += 1
                next_hb = now + cfg.heartbeat_s
            if now >= next_health:
                next_health = now + cfg.health_poll_s
                self._poll_health(usb)
            if period > 0 and len(data) < BULK_READ_SIZE:
                rest = t_read + period - self._clock()
                if rest > 0:
                    self._stop.wait(rest)

    def _handle_bytes(self, data: bytes) -> None:
        t_mono = self._clock()
        t_wall = self._wall()
        with self._lock:
            fmt = self._stats.packet_format
        if not fmt:
            fmt = self._detect_format(data)
            with self._lock:
                self._stats.packet_format = fmt
            logger.info("Panda CAN packet format: %s", fmt)
        try:
            if fmt == "v2":
                frames = unpack_can_buffer_v2(data)
            else:
                buf = self._tail + data if self._tail else data
                frames, self._tail = unpack_can_buffer(buf)
        except (ValueError, IndexError):
            self._tail = b""
            with self._lock:
                self._stats.checksum_errors += 1
            return
        bus_filter = self.config.bus
        tap = self._on_every_frame
        kept: List[Tuple[int, bytes, int]] = []
        filtered = 0
        for fr in frames:
            bus = fr[2]
            if bus >= 128 or (bus_filter is not None and bus != bus_filter):
                filtered += 1
                continue
            kept.append(fr)
            if tap is not None:
                try:
                    tap(fr[0], fr[1], bus, t_wall, t_mono)
                except Exception:
                    logger.exception("Panda frame tap failed (id 0x%03X)", fr[0])
        accepted = len(kept)
        coalesced = 0
        over = self.config.coalesce_over
        if over and accepted > over:
            kept, coalesced = coalesce_frames(kept)
        for can_id, payload, bus in kept:
            try:
                self._on_frame(can_id, payload, bus, t_wall, t_mono)
            except Exception:
                logger.exception("Panda frame callback failed (id 0x%03X)", can_id)
        busy = self._clock() - t_mono
        with self._lock:
            s = self._stats
            s.bulk_reads += 1
            s.bytes += len(data)
            s.frames += accepted
            s.frames_filtered += filtered
            s.coalesced += coalesced
            s.busy_s += busy
            if accepted > s.batch_max:
                s.batch_max = accepted
            for _, _, bus in frames:
                s.bus_counts[bus] = s.bus_counts.get(bus, 0) + 1
            if accepted:
                s.last_frame_mono = t_mono
                s.last_frame_wall = t_wall
        # 1 s rolling frames-per-second figure for the status mirror.
        self._fps_window_n += accepted
        if t_mono - self._fps_window_t >= 1.0:
            span = t_mono - self._fps_window_t
            self.fps = self._fps_window_n / span if self._fps_window_t else 0.0
            self._fps_window_t = t_mono
            self._fps_window_n = 0

    @staticmethod
    def _detect_format(data: bytes) -> str:
        """v2 transfers carry 0,1,2,... at every 64-byte boundary; a v4 stream
        parses with zero checksums. Decided once per connection on the first
        non-empty transfer."""
        try:
            frames, rest = unpack_can_buffer(data)
            if frames and not rest:
                return "v4"
        except ValueError:
            pass
        counters_ok = all(data[i] == i // 64 for i in range(0, len(data), 64))
        if counters_ok:
            try:
                unpack_can_buffer_v2(data)
                return "v2"
            except (ValueError, IndexError):
                pass
        return "v4"

    def _poll_health(self, usb: LibusbPanda) -> None:
        with self._lock:
            decodable = self._stats.health_decodable
            layout = self._stats.health_layout
            mode = self.config.safety_mode
        if not decodable:
            return
        try:
            raw = usb.control_in(REQ_HEALTH, 0, 0, 64)
        except UsbError as e:
            if e.code in (LIBUSB_ERROR_NO_DEVICE, LIBUSB_ERROR_IO, LIBUSB_ERROR_PIPE):
                raise
            logger.debug("Panda health read failed: %s", e)
            return
        h = parse_health(raw, layout)
        if h is None:
            return
        fixes = []
        if h.power_save:
            usb.control_out(REQ_POWER_SAVE, 0, 0)
            fixes.append("power save was on")
        if h.safety_mode != mode:
            usb.control_out(REQ_SAFETY_MODE, mode, 0, SAFETY_MODE_TIMEOUT_MS)
            fixes.append("safety mode was %d" % h.safety_mode)
        with self._lock:
            s = self._stats
            s.safety_mode = mode if fixes else h.safety_mode
            s.power_save = False            # off after the fix, or was off
            s.heartbeat_lost = h.heartbeat_lost
            s.ignition = h.ignition
            s.voltage_mv = h.voltage_mv
            if layout == "v9":
                s.can_rx_errs = h.rx_buffer_overflow
            else:
                s.rx_buffer_overflow = h.rx_buffer_overflow
            if h.power_save:
                s.power_save_fixes += 1
            if h.safety_mode != mode:
                s.safety_fixes += 1
        if fixes:
            logger.warning("Panda health: %s — re-asserted safety mode %d + power save off",
                           "; ".join(fixes), mode)

    def _note_error(self, what: str) -> None:
        with self._lock:
            self._stats.usb_errors += 1
            self._stats.last_error = what

    def _emit_link(self, up: bool) -> None:
        if self._on_link is None:
            return
        try:
            s = self.status()
            self._on_link(up, {"serial": s.serial, "fw": s.fw_version, "location": s.location})
        except Exception:
            logger.exception("Panda link callback failed")
