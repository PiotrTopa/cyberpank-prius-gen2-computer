"""
On-demand camera preview for the dashboard ("preview lease").

The dashboard is reached over LTE/WireGuard, so the preview is a slow series
of downscaled JPEG stills (about 1 frame/s), never a video stream, and it only
exists while somebody is watching:

    POST   /api/v1/camera/preview/lease            start (or renew with lease_id)
    DELETE /api/v1/camera/preview/lease/{id}       stop
    POST   /api/v1/camera/preview/lease/{id}/release   stop (sendBeacon form)
    GET    /api/v1/camera/preview                  status
    GET    /api/v1/camera/preview/frame.jpg?w=&q=  latest frame

(routes: ``api.camera_preview_routes``). A lease lives ``ttl_s`` (15 s) unless
renewed. While at least one lease is alive the camera is made available.

Grace and cap (POCO/POWER.md, "Dashboard camera preview"): when the last
lease ends *softly* (released without ``stop``, TTL expiry, session cap) the
camera stays exactly as it is for ``grace_s`` (180 s: hold on, reader
running) so a resume is instant; then everything returns to what it would
have been without a preview. A user STOP (``release(..., stop=True)``),
under-voltage and shutdown end it at once; with the stack serving there is
nothing to keep. A *viewing* starts with an explicit START and ends when the
camera is released; its leases (renewals and ``resume=True`` leases) all end
at START + ``max_session_s`` (10 min), so camera-on time per viewing is at
most ``max_session_s`` + one ``grace_s`` without a new START.

Where the frames come from depends on the roadsense manager
(``backend.roadsense_power``), which owns the camera hub (powerbox relay ch4)
and the fvision USB DVR camera (1902:3241) on it:

* **stack running** (ACC on: camera_on / starting / running / grace /
  stopping): nothing is powered or started for the preview. Frames are the
  stack's own ``/v1/frame.jpg`` (the camera's JPEG that ``camerad-dvr`` writes
  as ``preview.jpg``), fetched through the roadsense HTTP boundary
  (``api.roadsense_proxy.fetch``) at most every ``stack_min_interval_s`` and
  shared between clients. A second block-device reader is impossible here:
  ``camerad-dvr`` opens the disk ``O_EXCL``.
* **stack down** (car off): the lease sets the manager's *preview hold*: the
  manager enters PREVIEW (relay ch4 on, no roadsense unit, no ``full`` CPU
  profile) and this module reads frames itself with :class:`DvrReader`, the
  same read-only protocol as ``camerad-dvr`` (info sector at a fixed LBA,
  frame at the video LBA, check word; roadsense docs/HUIYING_DVR_PROTOCOL.md)
  but at ``capture_interval_s`` (2 frames/s) instead of 20, with ``O_EXCL``
  too. If ACC comes on during the preview, the manager calls
  :meth:`CameraPreviewService.release_device` right before ``systemctl
  start`` and the preview switches to the stack's frames. When the last lease
  ends the hold is dropped and the manager cuts relay ch4 (unless the stack
  wants the camera by then).

Progress (``AppState.roadsense.camera_preview.phase``): ``off`` (no lease),
``powering`` (waiting for the manager / relay), ``enumerating`` (no 1902:3241
block device yet, or it vanished: the dashcam re-enumerates ~30 s after
power-on), ``waiting_for_frame`` (device open, info sector still empty/zero or
no fresh frame; for the stack: roadsense starting), ``live``, ``error``
(reason in ``reason``; under-voltage, or no camera/frame within the
timeouts — the reader keeps trying).

Safety: no preview-driven power-up while the 12 V under-voltage protection is
active (a lease is refused, a running direct preview is ended); at most
``max_leases`` concurrent leases; :meth:`CameraPreviewService.shutdown` on
backend exit drops every lease, closes the device and cuts a preview-only
relay ch4.

Downscaling uses Pillow's JPEG draft mode (DCT-domain 1/2, 1/4, 1/8 decode,
then one small resize and re-encode). Pillow is ``py3-pillow`` in
POCO/packages.txt; without it frames are served full size
(``X-Preview-Scaled: 0``).

Thread model: the API (asyncio thread) only touches the lease table and the
frame caches under a lock; :meth:`CameraPreviewService.tick` (engine thread)
applies leases to the manager, runs the reader and publishes the status.
"""

from __future__ import annotations

import errno
import hashlib
import io
import logging
import mmap
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

# ── phases ───────────────────────────────────────────────────────────────────
PHASE_OFF = "off"
PHASE_POWERING = "powering"
PHASE_ENUMERATING = "enumerating"
PHASE_WAITING = "waiting_for_frame"
PHASE_LIVE = "live"
PHASE_ERROR = "error"
PHASE_GRACE = "grace"     # no lease, camera kept on for a quick resume

SOURCE_STACK = "stack"
SOURCE_DIRECT = "direct"

# Manager states in which the roadsense stack (camerad-dvr) owns the camera.
STACK_STATES = ("camera_on", "starting", "running", "grace", "stopping")
PREVIEW_STATE = "preview"

# ── DVR protocol constants (roadsense docs/HUIYING_DVR_PROTOCOL.md) ─────────
DVR_VID, DVR_PID = 0x1902, 0x3241
SECTOR = 512
MAX_FRAME = 0x4B000
AUDIO_LEN = 0x2000
KICK_EXTRA = 0x1400
LAYOUT_A = (0x380, 0x391)   # (info LBA, video LBA) when partition 1 starts at LBA 8
LAYOUT_B = (0x300, 0x311)
_BUF = 1 << 20


class PreviewRefused(Exception):
    """A lease request cannot be granted (``status`` = HTTP code)."""

    def __init__(self, reason: str, status: int, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.status = status
        self.detail = detail or reason


class FrameUnavailable(Exception):
    """No frame to serve right now (``status`` 409 without a lease, else 503)."""

    def __init__(self, phase: str, reason: str, status: int = 503) -> None:
        super().__init__(reason)
        self.phase = phase
        self.reason = reason
        self.status = status


@dataclass
class CameraPreviewConfig:
    """Knobs (``BACKEND_CAMERA_PREVIEW_*`` env vars, see :meth:`from_env`)."""
    enabled: bool = True
    ttl_s: float = 15.0              # lease lifetime without renewal
    max_session_s: float = 600.0     # cap on lease time per viewing, then press again
    grace_s: float = 180.0           # camera kept on after a soft end (0 = off)
    max_leases: int = 8
    capture_interval_s: float = 0.5  # direct reader: one frame per this
    stale_s: float = 5.0             # a frame older than this is not "live"
    enumerate_timeout_s: float = 90.0  # no device this long -> phase error (keeps trying)
    frame_timeout_s: float = 60.0      # no valid frame this long -> phase error (keeps trying)
    stack_min_interval_s: float = 0.25  # shared cache of the stack's frame
    default_width: int = 640
    default_quality: int = 70
    min_width: int = 160
    max_width: int = 1280

    @classmethod
    def from_env(cls, env=os.environ) -> "CameraPreviewConfig":
        def f(name: str, default: float) -> float:
            try:
                return float(env.get(name, "") or default)
            except ValueError:
                return default
        c = cls()
        c.enabled = (env.get("BACKEND_CAMERA_PREVIEW_ENABLED", "1").strip().lower()
                     in ("1", "true", "yes", "on"))
        c.ttl_s = max(3.0, f("BACKEND_CAMERA_PREVIEW_TTL_S", c.ttl_s))
        c.max_session_s = max(30.0, f("BACKEND_CAMERA_PREVIEW_MAX_SESSION_S", c.max_session_s))
        c.grace_s = max(0.0, f("BACKEND_CAMERA_PREVIEW_GRACE_S", c.grace_s))
        c.capture_interval_s = max(0.1, f("BACKEND_CAMERA_PREVIEW_CAPTURE_S", c.capture_interval_s))
        return c


# ═════════════════════════════════════════════════════════════════════════════
# Leases
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class Lease:
    lease_id: str
    created: float      # monotonic
    expires: float      # monotonic
    cap: float = float("inf")   # monotonic: renewals never go past this


class LeaseTable:
    """Thread-safe lease bookkeeping (pure; clock injectable)."""

    def __init__(self, ttl_s: float, max_session_s: float, max_leases: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self.max_session_s = max_session_s
        self.max_leases = max_leases
        self._clock = clock
        self._lock = threading.Lock()
        self._leases: Dict[str, Lease] = {}
        self._ended: Dict[str, str] = {}   # recently expired id -> reason (bounded)
        # How the most recently ended lease ended: expired | max_session |
        # released | stop | cleared (read by the service for the grace rule).
        self.last_end: Optional[str] = None

    def _expire_locked(self, now: float) -> None:
        for lid in [k for k, v in self._leases.items() if v.expires <= now]:
            v = self._leases.pop(lid)
            capped = v.expires >= v.cap - 1e-6
            self._ended[lid] = "max_session" if capped else "expired"
            self.last_end = self._ended[lid]
        while len(self._ended) > 64:
            self._ended.pop(next(iter(self._ended)))

    def acquire(self, lease_id: Optional[str] = None,
                cap_at: Optional[float] = None) -> Tuple[Lease, bool]:
        """New lease (``lease_id`` None) or renewal; returns ``(lease, created)``.
        A new lease is capped at ``cap_at`` (default: now + ``max_session_s``).

        Raises :class:`PreviewRefused`: 410 ``expired`` (unknown/expired id),
        410 ``max_session`` (renewal past the hard cap; the lease is dropped),
        429 ``too_many_leases``.
        """
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            if lease_id:
                lease = self._leases.get(lease_id)
                end = lease.cap if lease is not None else 0.0
                if lease is not None and now >= end:
                    del self._leases[lease_id]
                    lease = None
                    self._ended[lease_id] = "max_session"
                    self.last_end = "max_session"
                if lease is None:
                    if self._ended.get(lease_id) == "max_session":
                        raise PreviewRefused("max_session", 410,
                                             "maximum session length reached (%.0f s); press again"
                                             % self.max_session_s)
                    raise PreviewRefused("expired", 410, "lease expired or unknown; start a new one")
                lease.expires = min(now + self.ttl_s, end)
                return lease, False
            if len(self._leases) >= self.max_leases:
                raise PreviewRefused("too_many_leases", 429,
                                     "too many preview sessions (max %d)" % self.max_leases)
            cap = (now + self.max_session_s) if cap_at is None else cap_at
            if now >= cap:
                raise PreviewRefused("max_session", 410,
                                     "maximum session length reached (%.0f s); press again"
                                     % self.max_session_s)
            lid = secrets.token_hex(8)
            lease = Lease(lid, now, min(now + self.ttl_s, cap), cap)
            self._leases[lid] = lease
            return lease, True

    def release(self, lease_id: str, how: str = "released") -> bool:
        with self._lock:
            ok = self._leases.pop(lease_id, None) is not None
            if ok:
                self.last_end = how
            return ok

    def clear(self, how: str = "cleared") -> int:
        with self._lock:
            n = len(self._leases)
            self._leases.clear()
            if n:
                self.last_end = how
            return n

    def count(self) -> int:
        with self._lock:
            self._expire_locked(self._clock())
            return len(self._leases)

    def has(self, lease_id: str) -> bool:
        with self._lock:
            self._expire_locked(self._clock())
            return lease_id in self._leases

    def describe(self, lease: Lease) -> dict:
        now = self._clock()
        return {
            "lease_id": lease.lease_id,
            "ttl_s": self.ttl_s,
            "expires_in_s": round(max(0.0, lease.expires - now), 1),
            "max_session_s": self.max_session_s,
            "session_left_s": round(max(0.0, lease.cap - now), 1),
        }


# ═════════════════════════════════════════════════════════════════════════════
# Power adapters
# ═════════════════════════════════════════════════════════════════════════════

class ManagerPreviewPower:
    """Camera power through the roadsense manager's preview hold (normal case)."""

    def __init__(self, manager) -> None:
        self.manager = manager

    def set_hold(self, on: bool) -> None:
        self.manager.set_preview_hold(on)

    def stack_state(self) -> str:
        return self.manager.state

    def health(self) -> Optional[dict]:
        return getattr(self.manager, "_health", None)

    def shutdown(self) -> None:
        self.manager.shutdown_preview()


class RelayPreviewPower:
    """roadsense manager disabled (bench): drive the camera hub relay directly
    and restore its previous desired state when the preview ends."""

    def __init__(self, camera) -> None:
        self.camera = camera
        self._held = False
        self._prev: Optional[bool] = None

    def set_hold(self, on: bool) -> None:
        if on and not self._held:
            self._prev = getattr(self.camera, "desired", None)
            self.camera.set(True)
            self._held = True
        elif not on and self._held:
            if self._prev is not True:
                self.camera.set(False)
            self._held = False

    def stack_state(self) -> str:
        return PREVIEW_STATE if self._held else "off"

    def health(self) -> Optional[dict]:
        return None

    def shutdown(self) -> None:
        self.set_hold(False)


# ═════════════════════════════════════════════════════════════════════════════
# Direct reader (fvision / Buildwin USB DVR, mass storage)
# ═════════════════════════════════════════════════════════════════════════════

def is_an_num(v: int) -> bool:
    """isAnNum(): integrity check of the u16 at info+0x30."""
    lo, hi = v & 0xFF, (v >> 8) & 0xFF
    return lo == ((((bin(hi & 0x1F).count("1")) ^ 0x0C) << 3) | ((hi >> 5) ^ 0x0B)) & 0xFF


def _u16(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 2], "little")


def _u32(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 4], "little")


@dataclass
class InfoHeader:
    flags: int
    vsize: int
    vseq: int
    check: int
    playback: bool
    valid: bool


def parse_info(b: bytes) -> InfoHeader:
    flags = _u16(b, 0)
    vsize = _u32(b, 0x0C)
    alen = _u32(b, 0x14)
    valid = (alen in (0, AUDIO_LEN) and 0 < vsize <= MAX_FRAME and flags != 0
             and is_an_num(_u16(b, 0x30)))
    return InfoHeader(flags, vsize, _u32(b, 0x10), _u32(b, 0x2C), bool(flags & 0x8000), valid)


def jpeg_length(d: bytes) -> int:
    """Length up to and including the first EOI after SOS, or 0 (camerad-dvr)."""
    i, n = 2, len(d)
    while i + 3 < n:
        if d[i] != 0xFF:
            return 0
        m = d[i + 1]
        ln = (d[i + 2] << 8) | d[i + 3]
        if m == 0xDA:
            j = d.find(b"\xff\xd9", i + 2 + ln)
            return j + 2 if j >= 0 else 0
        i += 2 + ln
    return 0


def terminate_jpeg(d: bytes) -> Optional[bytes]:
    """Trim to EOI (or append one); None unless it starts with SOI."""
    if len(d) < 4 or d[:2] != b"\xff\xd8":
        return None
    n = jpeg_length(d)
    return d[:n] if n else d + b"\xff\xd9"


def _usb_id_of(sys_path: str) -> Optional[Tuple[int, int]]:
    p = sys_path
    while len(p) > 1:
        try:
            with open(os.path.join(p, "idVendor")) as fv, open(os.path.join(p, "idProduct")) as fp:
                return int(fv.read().strip(), 16), int(fp.read().strip(), 16)
        except (OSError, ValueError):
            p = os.path.dirname(p)
    return None


def find_dvr_blocks(sys_block: str = "/sys/block") -> List[str]:
    """Every whole-disk block device whose USB parent is 1902:3241 (as
    ``camerad-dvr --auto``), in name order. After the dashcam's disconnect a
    stale node (sdg) can linger next to the new one (sdh): the reader tries
    them all, on every attempt."""
    try:
        names = sorted(os.listdir(sys_block))
    except OSError:
        return []
    out = []
    for name in names:
        real = os.path.realpath(os.path.join(sys_block, name))
        if _usb_id_of(real) == (DVR_VID, DVR_PID):
            out.append("/dev/" + name)
    return out


def find_dvr_block(sys_block: str = "/sys/block") -> Optional[str]:
    """First 1902:3241 whole disk, or None."""
    found = find_dvr_blocks(sys_block)
    return found[0] if found else None


class BlockIO:
    """O_DIRECT access to the raw disk: page-aligned buffer, sector multiples."""

    def __init__(self) -> None:
        self._buf = mmap.mmap(-1, _BUF)

    def open(self, path: str) -> int:
        return os.open(path, os.O_RDONLY | os.O_DIRECT | os.O_EXCL | os.O_CLOEXEC)

    def pread(self, fd: int, size: int, offset: int) -> bytes:
        size = (size + SECTOR - 1) // SECTOR * SECTOR
        if size > _BUF:
            raise OSError(errno.EINVAL, "read too large")
        view = memoryview(self._buf)[:size]
        try:
            got = os.preadv(fd, [view], offset)
            if got != size:
                raise OSError(errno.EIO, "short read %d/%d" % (got, size))
            return bytes(view)
        finally:
            view.release()

    def close(self, fd: int) -> None:
        os.close(fd)


@dataclass
class Frame:
    jpeg: bytes
    seq: int
    t_mono: float
    t_wall: float
    etag: str
    source: str


def make_etag(data: bytes) -> str:
    return '"%s"' % hashlib.sha1(data).hexdigest()[:16]


class DvrReader:
    """Low-rate, read-only frame grabber on its own thread.

    ``status()`` -> ``(phase, reason, phase_since_monotonic)``; ``latest()``
    -> newest :class:`Frame` or None. Every external effect is injectable.
    """

    ATTEMPT_LOG_S = 10.0   # while the camera is missing, one log line per this

    def __init__(self, capture_interval_s: float = 0.5,
                 find_device: Callable[[], Union[None, str, Sequence[str]]] = find_dvr_blocks,
                 io_: Optional[object] = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 retry_s: float = 1.0) -> None:
        self.capture_interval_s = capture_interval_s
        self.find_device = find_device
        self.io = io_ if io_ is not None else BlockIO()
        self._clock = clock
        self._wall = wall
        self.retry_s = retry_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fd: Optional[int] = None
        self._path: Optional[str] = None
        self._layout = LAYOUT_B
        self._phase = PHASE_ENUMERATING
        self._reason = "looking for the camera"
        self._since = clock()
        self._frame: Optional[Frame] = None
        self._last_seq: Optional[int] = None
        self._last_new = clock()
        self.frames = 0
        self.reopens = 0
        self.attempts = 0                 # open attempts since the last good open
        self._attempt_log_t = -1e9

    # ── public ───────────────────────────────────────────────────────────

    def running(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        with self._lock:
            self._frame = None
            self._set_phase_locked(PHASE_ENUMERATING, "looking for the camera")
        self._thread = threading.Thread(target=self._run, name="camera-preview", daemon=True)
        self._thread.start()
        logger.info("camera preview: direct reader started")

    def stop(self, timeout: float = 3.0) -> None:
        """Stop the thread and close the device (so camerad-dvr can open it)."""
        if self._thread is None:
            self._close()
            return
        self._stop.set()
        self._thread.join(timeout)
        if self._thread.is_alive():
            logger.error("camera preview: reader thread did not stop in %.1fs", timeout)
        self._thread = None
        self._close()
        with self._lock:
            self._frame = None
        logger.info("camera preview: direct reader stopped")

    def status(self) -> Tuple[str, str, float]:
        with self._lock:
            return self._phase, self._reason, self._since

    def latest(self) -> Optional[Frame]:
        with self._lock:
            return self._frame

    # ── internals ────────────────────────────────────────────────────────

    def _set_phase_locked(self, phase: str, reason: str) -> None:
        if phase != self._phase:
            self._since = self._clock()
            logger.info("camera preview: reader %s (%s)", phase, reason)
        self._phase = phase
        self._reason = reason

    def _set_phase(self, phase: str, reason: str) -> None:
        with self._lock:
            self._set_phase_locked(phase, reason)

    def _close(self) -> None:
        if self._fd is not None:
            try:
                self.io.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def _candidates(self) -> List[str]:
        found = self.find_device()
        if not found:
            return []
        return [found] if isinstance(found, str) else list(found)

    def _try_open(self, path: str) -> Tuple[bool, str, str]:
        """``(ok, phase, why)`` for one candidate node."""
        try:
            fd = self.io.open(path)
        except OSError as exc:
            why = ("%s busy (camerad-dvr or a mount holds it)" % path
                   if exc.errno == errno.EBUSY else "open %s: %s" % (path, exc.strerror or exc))
            return False, PHASE_ENUMERATING, why
        try:
            mbr = self.io.pread(fd, SECTOR, 0)
        except OSError as exc:
            # Node present but not readable yet (just re-enumerated) or stale.
            self.io.close(fd)
            return False, PHASE_ENUMERATING, "%s MBR read failed: %s" % (path, exc.strerror or exc)
        if mbr[510:512] != b"\x55\xaa":
            self.io.close(fd)
            return False, PHASE_WAITING, "no MBR on %s yet" % path
        self._layout = LAYOUT_A if _u32(mbr, 0x1C6) == 8 else LAYOUT_B
        self._fd, self._path = fd, path
        return True, PHASE_WAITING, ""

    def _open(self) -> bool:
        """Re-scan sysfs and try every 1902:3241 disk (called once per
        ``retry_s`` while the camera is missing)."""
        self.attempts += 1
        paths = self._candidates()
        if not paths:
            phase, why = PHASE_ENUMERATING, "no 1902:3241 block device yet"
        else:
            phase, why = PHASE_ENUMERATING, ""
            for path in paths:
                ok, ph, w = self._try_open(path)
                if ok:
                    if self.attempts > 1:
                        logger.info("camera preview: camera back on %s after %d attempt(s)",
                                    path, self.attempts)
                    self.attempts = 0
                    self._last_seq = None
                    self._last_new = self._clock()
                    self.reopens += 1
                    self._set_phase(PHASE_WAITING, "camera open (%s), waiting for the first frame"
                                    % path)
                    return True
                phase, why = ph, (why + "; " if why else "") + w
        now = self._clock()
        if now - self._attempt_log_t >= self.ATTEMPT_LOG_S:
            self._attempt_log_t = now
            logger.info("camera preview: open attempt %d: %s", self.attempts, why)
        self._set_phase(phase, why)
        return False

    def _io_failed(self, what: str, exc: OSError) -> None:
        logger.info("camera preview: %s failed (%s): reopening", what, exc)
        self._close()
        self._set_phase(PHASE_ENUMERATING, "camera gone (%s), re-enumerating" % what)

    def step(self) -> float:
        """One read cycle; returns the seconds to wait before the next."""
        if self._fd is None and not self._open():
            return self.retry_s
        info_lba, video_lba = self._layout
        try:
            info = self.io.pread(self._fd, SECTOR, info_lba * SECTOR)
        except OSError as exc:
            self._io_failed("info read", exc)
            return self.retry_s
        h = parse_info(info)
        now = self._clock()
        if not h.valid:
            # Right after power-on the info sector (and frame area) reads as
            # zeros for a while: wait, without hammering the device.
            self._set_phase(PHASE_WAITING, "camera info sector empty (starting up)")
            self._stale_check(now, reopen=False)
            return 0.2
        if self._last_seq is not None and h.vseq == self._last_seq:
            if not h.playback and now - self._last_new > 0.4:
                try:   # the app's dummy "kick" read for a stalled sequence
                    self.io.pread(self._fd, (h.vsize & ~0x1FF) + KICK_EXTRA, video_lba * SECTOR)
                except OSError as exc:
                    self._io_failed("kick read", exc)
                    return self.retry_s
            self._stale_check(now)
            return 0.05
        for _ in range(3):
            try:
                f = self.io.pread(self._fd, (h.vsize & ~0x1FF) + 0x200, video_lba * SECTOR)
            except OSError as exc:
                self._io_failed("frame read", exc)
                return self.retry_s
            off = ((h.vsize - 8) >> 2) * 4
            if h.playback or _u32(f, off) == h.check:
                break
            # frame buffer being rewritten: re-read the info sector for a new one
            try:
                h = parse_info(self.io.pread(self._fd, SECTOR, info_lba * SECTOR))
            except OSError as exc:
                self._io_failed("info read", exc)
                return self.retry_s
            if not h.valid:
                return 0.05
        else:
            return 0.05
        jpeg = terminate_jpeg(f[:h.vsize])
        self._last_seq = h.vseq
        self._last_new = now
        if jpeg is None:
            self._set_phase(PHASE_WAITING, "frame area not a JPEG yet (starting up)")
            return 0.2
        frame = Frame(jpeg, h.vseq, now, self._wall(), make_etag(jpeg), SOURCE_DIRECT)
        with self._lock:
            self._frame = frame
            self.frames += 1
            self._set_phase_locked(PHASE_LIVE, "")
        return self.capture_interval_s

    def _stale_check(self, now: float, reopen: bool = True) -> None:
        """No new frame for a while: not live any more; much longer (with the
        sequence frozen, not while the info sector is still empty): reopen."""
        idle = now - self._last_new
        if idle > 3.0:
            with self._lock:
                if self._phase == PHASE_LIVE:
                    self._set_phase_locked(PHASE_WAITING, "camera stopped delivering frames")
        if reopen and idle > 15.0 and self._fd is not None:
            logger.info("camera preview: no new frame for %.0fs, reopening", idle)
            self._close()
            self._set_phase(PHASE_ENUMERATING, "no frames for %.0f s, reopening" % idle)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                wait = self.step()
            except Exception:  # noqa: BLE001 - never die silently
                logger.exception("camera preview: reader step failed")
                self._close()
                self._set_phase(PHASE_ERROR, "reader crashed (see log), retrying")
                wait = self.retry_s
            self._stop.wait(max(0.01, wait))
        self._close()


# ═════════════════════════════════════════════════════════════════════════════
# Downscaling
# ═════════════════════════════════════════════════════════════════════════════

Scaler = Callable[[bytes, int, int], Optional[bytes]]


def make_pillow_scaler() -> Optional[Scaler]:
    """JPEG -> smaller JPEG with Pillow's draft (DCT-domain) decode; None
    when Pillow is not installed."""
    try:
        from PIL import Image  # type: ignore
    except Exception:  # noqa: BLE001 - ImportError or a broken install
        return None

    def scale(jpeg: bytes, width: int, quality: int) -> Optional[bytes]:
        im = Image.open(io.BytesIO(jpeg))
        if im.width <= width:
            return None
        height = max(1, round(im.height * width / im.width))
        im.draft("RGB", (width, height))
        im = im.convert("RGB")
        if im.width != width:
            im = im.resize((width, height), Image.BILINEAR)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality)
        return out.getvalue()

    return scale


# ═════════════════════════════════════════════════════════════════════════════
# Service
# ═════════════════════════════════════════════════════════════════════════════

StackFetch = Callable[[], "object"]   # async () -> (status, content_type, body)


class CameraPreviewService:
    """Leases + power hold + frame sources; see the module docstring."""

    def __init__(self, config: CameraPreviewConfig, power,
                 undervoltage: Callable[[], bool] = lambda: False,
                 publish: Optional[Callable[[dict], None]] = None,
                 reader: Optional[DvrReader] = None,
                 stack_fetch: Optional[StackFetch] = None,
                 scaler: Optional[Scaler] = None,
                 use_pillow: bool = True,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.config = config
        self.power = power
        self._undervoltage = undervoltage
        self._publish = publish
        self._clock = clock
        self._wall = wall
        self.leases = LeaseTable(config.ttl_s, config.max_session_s, config.max_leases, clock)
        self.reader = reader or DvrReader(config.capture_interval_s, clock=clock, wall=wall)
        self._stack_fetch = stack_fetch
        self.scaler = scaler if scaler is not None else (make_pillow_scaler() if use_pillow else None)
        self._lock = threading.Lock()
        self._held = False
        self._started_at: Optional[float] = None   # wall clock of the first lease
        self._end_reason: Optional[str] = None
        self._stack_frame: Optional[Frame] = None
        self._stack_seq = 0
        self._stack_error: Optional[str] = None
        self._stack_fetch_lock = None             # asyncio.Lock, made lazily on the API loop
        self._scaled: Dict[Tuple[str, int, int], bytes] = {}
        self._last_published: Optional[dict] = None
        self._last_tick = -1e9
        # Viewing = from the first explicit START until the camera is released.
        # Resumed leases share its cap; grace keeps the camera after a soft end.
        self._viewing_cap: Optional[float] = None   # monotonic
        self._grace_until: Optional[float] = None   # monotonic
        self._grace_reason: Optional[str] = None
        self._stop_now = False                      # explicit STOP while in grace

    # ── API side (asyncio thread) ─────────────────────────────────────────

    def lease(self, lease_id: Optional[str] = None, resume: bool = False) -> dict:
        """Start or renew a lease. Raises :class:`PreviewRefused`.

        ``resume`` (no ``lease_id``): continue the current viewing (a lease
        that lapsed, a reload, a tab coming back) — refused unless a viewing
        exists (leases active or grace running: never a cold start) and only
        up to the viewing's cap. A plain new lease is an explicit START: it
        gets a fresh ``max_session_s`` cap.
        """
        if not self.config.enabled:
            raise PreviewRefused("disabled", 503, "camera preview disabled")
        if lease_id:
            lease, created = self.leases.acquire(lease_id)
        else:
            if self._undervoltage() and not self._stack_serves():
                raise PreviewRefused("undervoltage", 409,
                                     "12 V under-voltage protection active: no camera power-up")
            now = self._clock()
            with self._lock:
                viewing = self._viewing_cap
                if resume:
                    if viewing is None:
                        raise PreviewRefused("not_resumable", 409,
                                             "no preview to resume (press start)")
                    cap = viewing
                else:
                    cap = now + self.config.max_session_s
            lease, created = self.leases.acquire(None, cap_at=cap)
            with self._lock:
                if not resume:
                    self._viewing_cap = max(self._viewing_cap or 0.0, cap)
                self._end_reason = None
                self._stop_now = False
                if self._started_at is None:
                    self._started_at = self._wall()
                was_grace = self._grace_until is not None
            logger.info("camera preview: lease %s %s (%d active)%s", lease.lease_id,
                        "resumed" if resume else "started", self.leases.count(),
                        " — camera kept on by the grace, no power-up" if was_grace else "")
        out = self.leases.describe(lease)
        out.update(self.status_brief())
        out["resumed"] = bool(resume and not lease_id)
        return out

    def release(self, lease_id: Optional[str], stop: bool = False) -> bool:
        """End a lease. ``stop`` = explicit user STOP: when it is the last
        lease the camera is released at once (no grace); with no lease left
        it also ends a running grace. Otherwise the end is "soft" (page
        hidden / closed / navigated away) and the grace keeps the camera."""
        ok = bool(lease_id) and self.leases.release(lease_id, "stop" if stop else "released")
        if ok:
            logger.info("camera preview: lease %s %s (%d left)", lease_id,
                        "stopped" if stop else "released", self.leases.count())
        if stop and self.leases.count() == 0:
            with self._lock:
                self._stop_now = True
        return ok

    def _stack_serves(self) -> bool:
        return self.power.stack_state() in STACK_STATES

    def source(self) -> Optional[str]:
        st = self.power.stack_state()
        if st in STACK_STATES:
            return SOURCE_STACK
        if st == PREVIEW_STATE:
            return SOURCE_DIRECT
        return None

    def phase(self) -> Tuple[str, str]:
        """``(phase, reason)`` right now."""
        if self.leases.count() == 0:
            with self._lock:
                reason = self._end_reason or ""
                grace = self._grace_until
                why = self._grace_reason
            if grace is None and self._grace_pending():
                grace, why = self._clock() + self.config.grace_s, self.leases.last_end
            if grace is not None:
                return PHASE_GRACE, "camera kept on for a resume (last lease %s)" % (why or "ended")
            return (PHASE_ERROR, reason) if reason == "undervoltage" else (PHASE_OFF, reason)
        return self.camera_phase()

    def _grace_pending(self) -> bool:
        """The last lease just ended softly and the engine tick (<= 0.2 s)
        has not started the grace yet: report it already, so a page reloading
        in that instant sees ``grace`` and resumes."""
        return (self._held and self.config.grace_s > 0 and not self._stop_now
                and self.leases.last_end not in ("stop", "undervoltage", "shutdown")
                and not self._stack_serves())

    def camera_phase(self) -> Tuple[str, str]:
        """Phase of the camera path itself (leases aside)."""
        st = self.power.stack_state()
        now = self._clock()
        if st in STACK_STATES:
            if st in ("camera_on", "starting"):
                return PHASE_WAITING, "roadsense starting"
            with self._lock:
                fr, err = self._stack_frame, self._stack_error
            if fr is not None and now - fr.t_mono <= self.config.stale_s:
                return PHASE_LIVE, ""
            return PHASE_WAITING, err or "waiting for the roadsense frame"
        if st == PREVIEW_STATE:
            if not self.reader.running():
                return PHASE_POWERING, "camera hub on, starting the reader"
            phase, reason, since = self.reader.status()
            if phase == PHASE_ENUMERATING and now - since > self.config.enumerate_timeout_s:
                return PHASE_ERROR, "camera not found for %.0f s (%s)" % (now - since, reason)
            if phase == PHASE_WAITING and now - since > self.config.frame_timeout_s:
                return PHASE_ERROR, "no frame for %.0f s (%s)" % (now - since, reason)
            if phase == PHASE_LIVE:
                fr = self.reader.latest()
                if fr is None or now - fr.t_mono > self.config.stale_s:
                    return PHASE_WAITING, "frame stale"
            return phase, reason
        if self._undervoltage():
            return PHASE_ERROR, "undervoltage"
        return PHASE_POWERING, "powering the camera hub"

    def status_brief(self) -> dict:
        phase, reason = self.phase()
        return {"phase": phase, "reason": reason, "source": self.source(),
                "leases": self.leases.count()}

    async def _fetch_stack_frame(self) -> Optional[Frame]:
        import asyncio
        if self._stack_fetch_lock is None:
            self._stack_fetch_lock = asyncio.Lock()
        async with self._stack_fetch_lock:
            with self._lock:
                fr = self._stack_frame
            now = self._clock()
            if fr is not None and now - fr.t_mono < self.config.stack_min_interval_s:
                return fr
            if self._stack_fetch is None:
                return None
            try:
                status, _ctype, body = await self._stack_fetch()
            except Exception as exc:  # noqa: BLE001 - stack down / starting
                with self._lock:
                    self._stack_error = "roadsense frame unavailable: %s" % exc
                return fr
            data = terminate_jpeg(body) if status == 200 else None
            if data is None:
                with self._lock:
                    self._stack_error = "roadsense has no frame yet (HTTP %s)" % status
                return fr
            etag = make_etag(data)
            with self._lock:
                if fr is not None and fr.etag == etag:
                    # Same picture again: camerad-dvr is not updating preview.jpg.
                    self._stack_error = "roadsense frame not updating"
                    return fr
                self._stack_seq += 1
                fr = Frame(data, self._stack_seq, self._clock(), self._wall(), etag, SOURCE_STACK)
                self._stack_frame = fr
                self._stack_error = None
            return fr

    async def frame(self, width: Optional[int] = None, quality: Optional[int] = None,
                    lease_id: Optional[str] = None) -> Tuple[bytes, Dict[str, str]]:
        """Latest frame as ``(jpeg, headers)``. Raises :class:`FrameUnavailable`."""
        if self.leases.count() == 0 or (lease_id and not self.leases.has(lease_id)):
            raise FrameUnavailable(PHASE_OFF, "no active preview lease", 409)
        src = self.source()
        fr: Optional[Frame] = None
        if src == SOURCE_STACK:
            fr = await self._fetch_stack_frame()
        elif src == SOURCE_DIRECT:
            fr = self.reader.latest()
        now = self._clock()
        if fr is None or now - fr.t_mono > self.config.stale_s:
            phase, reason = self.phase()
            if phase == PHASE_LIVE:
                phase, reason = PHASE_WAITING, "frame stale"
            raise FrameUnavailable(phase, reason or "no frame yet")
        width = self.config.default_width if width is None else width
        quality = self.config.default_quality if quality is None else quality
        data, scaled = await self._scaled_jpeg(fr, width, quality)
        headers = {
            "ETag": fr.etag if not scaled else '"%s-%d-%d"' % (fr.etag.strip('"'), width, quality),
            "Cache-Control": "no-store",
            "X-Frame-Age": "%.2f" % max(0.0, now - fr.t_mono),
            "X-Frame-Time": "%.3f" % fr.t_wall,
            "X-Frame-Seq": str(fr.seq),
            "X-Frame-Source": fr.source,
            "X-Preview-Phase": PHASE_LIVE,
            "X-Preview-Scaled": "1" if scaled else "0",
        }
        return data, headers

    async def _scaled_jpeg(self, fr: Frame, width: int, quality: int) -> Tuple[bytes, bool]:
        if self.scaler is None or width >= self.config.max_width:
            return fr.jpeg, False
        key = (fr.etag, width, quality)
        with self._lock:
            hit = self._scaled.get(key)
        if hit is not None:
            return hit, True
        import asyncio
        try:
            out = await asyncio.to_thread(self.scaler, fr.jpeg, width, quality)
        except Exception:  # noqa: BLE001 - a corrupt frame: serve it unscaled
            logger.debug("camera preview: downscale failed", exc_info=True)
            out = None
        if not out:
            return fr.jpeg, False
        with self._lock:
            if any(k[0] != fr.etag for k in self._scaled) or len(self._scaled) >= 4:
                self._scaled = {}       # keep the variants of the newest frame only
            self._scaled[key] = out
        return out, True

    # ── engine side ───────────────────────────────────────────────────────

    def release_device(self) -> None:
        """roadsense manager hook: the stack is about to start; let go of the
        camera's block device (``camerad-dvr`` opens it ``O_EXCL``)."""
        if self.reader.running():
            logger.info("camera preview: releasing the camera device for roadsense")
        self.reader.stop()

    TICK_S = 0.2

    def tick(self, force: bool = False) -> None:
        """Apply leases to the power hold and the reader; publish the status.
        Self-limits to :attr:`TICK_S` (the engine loop runs at 50 Hz)."""
        now = self._clock()
        if not force and now - self._last_tick < self.TICK_S:
            return
        self._last_tick = now
        active = self.leases.count() > 0
        uv = self._undervoltage()
        with self._lock:
            in_grace = self._grace_until is not None
            stop_now, self._stop_now = self._stop_now, False
        force_off = False
        if (active or in_grace or self._held) and uv and not self._stack_serves():
            n = self.leases.clear("undervoltage")
            logger.warning("camera preview: 12 V under-voltage protection active: "
                           "ending %d preview lease(s)%s, camera hub released", n,
                           " and the grace" if in_grace else "")
            with self._lock:
                self._end_reason = "undervoltage"
                self._grace_until = None
            active = in_grace = False
            force_off = True
        if force_off:
            want_hold = False
        elif active:
            want_hold = True
            if in_grace:
                with self._lock:
                    self._grace_until = None
                logger.info("camera preview: grace ended by a new lease (camera still on)")
        elif self._held:
            # Last lease gone (or in grace): grace unless an explicit STOP,
            # grace disabled, or nothing to keep (the stack has the camera).
            how = self.leases.last_end
            if not in_grace:
                if (how == "stop" or stop_now or self.config.grace_s <= 0
                        or self._stack_serves()):
                    want_hold = False
                else:
                    want_hold = True
                    with self._lock:
                        self._grace_until = now + self.config.grace_s
                        self._grace_reason = how
                    logger.info("camera preview: last lease %s: camera kept on for %.0f s "
                                "(grace) for a resume", how or "ended", self.config.grace_s)
            else:
                with self._lock:
                    until = self._grace_until or now
                if stop_now or now >= until or self._stack_serves():
                    want_hold = False
                    logger.info("camera preview: grace over (%s)",
                                "stop" if stop_now else "roadsense has the camera"
                                if self._stack_serves() else "%.0f s elapsed" % self.config.grace_s)
                else:
                    want_hold = True
        else:
            want_hold = False
        if want_hold != self._held:
            self.power.set_hold(want_hold)
            self._held = want_hold
            if not want_hold:
                with self._lock:
                    self._started_at = None
                    self._stack_frame = None
                    self._scaled = {}
                    self._viewing_cap = None
                    self._grace_until = None
                    self._grace_reason = None
                    if self._end_reason is None:
                        self._end_reason = "stopped" if (stop_now or
                                                         self.leases.last_end == "stop") else "ended"
        want_reader = want_hold and self.power.stack_state() == PREVIEW_STATE
        if want_reader and not self.reader.running():
            self.reader.start()
        elif not want_reader and self.reader.running():
            self.reader.stop()
        self._emit()

    def status(self) -> dict:
        phase, reason = self.phase()
        now = self._clock()
        with self._lock:
            started = self._started_at
            grace = self._grace_until
            cap = self._viewing_cap
        held = self._held
        if grace is None and self.leases.count() == 0 and self._grace_pending():
            grace = now + self.config.grace_s
        return {
            "enabled": self.config.enabled,
            "active": self.leases.count() > 0,
            "phase": phase,
            "reason": reason or None,
            # what the camera path is doing (also during the grace)
            "camera_phase": self.camera_phase()[0] if held or self.leases.count() else PHASE_OFF,
            "source": self.source() if (self.leases.count() or grace is not None) else None,
            "leases": self.leases.count(),
            "started_at": started,
            "grace": grace is not None,
            "grace_left_s": int(max(0.0, grace - now) + 0.999) if grace is not None else None,
            "session_left_s": int(max(0.0, cap - now)) // 10 * 10 if cap is not None else None,
            "ttl_s": self.config.ttl_s,
            "max_session_s": self.config.max_session_s,
            "grace_s": self.config.grace_s,
            "scaling": self.scaler is not None,
        }

    def _emit(self) -> None:
        st = self.status()
        if st == self._last_published:
            return
        self._last_published = st
        if self._publish is not None:
            try:
                self._publish(st)
            except Exception:
                logger.exception("camera preview: status publish failed")

    def shutdown(self) -> None:
        """Backend exit: drop every lease, close the device, cut a preview-only
        camera hub."""
        n = self.leases.clear("shutdown")
        with self._lock:
            self._grace_until = None
            self._viewing_cap = None
        if n:
            logger.info("camera preview: shutdown, %d lease(s) dropped", n)
        try:
            self.reader.stop()
        except Exception:
            logger.exception("camera preview: reader stop failed")
        try:
            self.power.shutdown()
        except Exception:
            logger.exception("camera preview: power release failed")
        self._held = False


# ═════════════════════════════════════════════════════════════════════════════
# Wiring (called from BackendService.build)
# ═════════════════════════════════════════════════════════════════════════════

def wire_camera_preview(service, twin, env=os.environ) -> Optional[CameraPreviewService]:
    """Build the preview service for a :class:`BackendService`; None when
    disabled or when there is no way to power the camera."""
    cfg = CameraPreviewConfig.from_env(env)
    if not cfg.enabled:
        logger.info("camera preview disabled (BACKEND_CAMERA_PREVIEW_ENABLED=0)")
        return None
    manager = getattr(service, "roadsense_power", None)
    pp = getattr(service, "port_power", None)
    if manager is not None:
        power = ManagerPreviewPower(manager)
    elif pp is not None:
        power = RelayPreviewPower(pp.camera)
    else:
        logger.info("camera preview not wired: no roadsense manager and no port power")
        return None

    stack_fetch = None
    if manager is not None:
        from ..api.roadsense_proxy import FRAME_PATH, ProxyConfig, fetch
        rs = manager.config
        proxy = ProxyConfig(base_url="http://%s:%d" % (rs.api_host, rs.api_port))

        async def stack_fetch():
            return await fetch(proxy, FRAME_PATH)

    def undervoltage() -> bool:
        pb = twin.store.state.powerbox
        return bool(pb.undervoltage or pb.shutdown_requested)

    def publish(status: dict) -> None:
        from ..state.actions import SetRoadsenseStateAction
        twin.store.dispatch(SetRoadsenseStateAction(camera_preview=status))

    svc = CameraPreviewService(cfg, power, undervoltage=undervoltage, publish=publish,
                               stack_fetch=stack_fetch)
    if manager is not None:
        manager.preview_release = svc.release_device
    logger.info("camera preview wired (power via %s, ttl %.0fs, max %.0fs, downscale %s)",
                "roadsense manager" if manager is not None else "relay ch4",
                cfg.ttl_s, cfg.max_session_s,
                "Pillow" if svc.scaler is not None else "unavailable (full-size frames)")
    return svc
