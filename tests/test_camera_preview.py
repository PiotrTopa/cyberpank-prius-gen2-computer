"""On-demand dashboard camera preview (backend.camera_preview +
api.camera_preview_routes + the roadsense manager's preview hold).

Covers: lease table (several clients, expiry, renewal, hard max length,
cooldown-free restart), the manager's PREVIEW state (relay ch4 without the
stack; never stops a running stack; never keeps ch4 on after the lease with
ACC off; ACC transitions in both directions; release hook before the stack
start), the service (under-voltage refusal and end, shutdown release), the
direct DVR reader against a synthetic block device (zero period,
re-enumeration, EBUSY, check word, kick, stale frames) and the REST surface
over a real uvicorn (frame downscale, 503 phases, 409 without a lease, ETag
304, validation, sendBeacon release).
"""

import asyncio
import errno
import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from cyberpunk_computer.backend import camera_preview as cp
from cyberpunk_computer.backend.camera_preview import (
    PHASE_ENUMERATING,
    PHASE_ERROR,
    PHASE_LIVE,
    PHASE_OFF,
    PHASE_POWERING,
    PHASE_WAITING,
    CameraPreviewConfig,
    CameraPreviewService,
    DvrReader,
    Frame,
    LeaseTable,
    ManagerPreviewPower,
    PreviewRefused,
    RelayPreviewPower,
    make_etag,
    parse_info,
    terminate_jpeg,
)
from cyberpunk_computer.backend.roadsense_power import (
    STATE_CAMERA_ON,
    STATE_GRACE,
    STATE_OFF,
    STATE_PREVIEW,
    STATE_RUNNING,
    STATE_STARTING,
    STATE_STOPPING,
    RoadsenseConfig,
    RoadsenseManager,
)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ═════════════════════════════════════════════════════════════════════════════
# Lease table
# ═════════════════════════════════════════════════════════════════════════════

class TestLeaseTable:
    def mk(self, **kw):
        clk = Clock()
        args = dict(ttl_s=15.0, max_session_s=600.0, max_leases=3)
        args.update(kw)
        return LeaseTable(clock=clk, **args), clk

    def test_two_clients_expire_independently(self):
        t, clk = self.mk()
        a, created = t.acquire()
        assert created
        clk.t += 10
        b, _ = t.acquire()
        assert t.count() == 2
        clk.t += 6                       # a: 16 s without renewal
        assert t.count() == 1
        assert not t.has(a.lease_id) and t.has(b.lease_id)
        clk.t += 10
        assert t.count() == 0

    def test_renewal_extends_and_expired_id_is_refused(self):
        t, clk = self.mk()
        a, _ = t.acquire()
        for _ in range(5):
            clk.t += 10
            again, created = t.acquire(a.lease_id)
            assert again is a and not created
        assert t.count() == 1
        clk.t += 16
        with pytest.raises(PreviewRefused) as e:
            t.acquire(a.lease_id)
        assert e.value.status == 410 and e.value.reason == "expired"
        b, created = t.acquire()         # press again: no cooldown
        assert created and b.lease_id != a.lease_id

    def test_max_session_hard_cap(self):
        t, clk = self.mk(max_session_s=60.0)
        a, _ = t.acquire()
        for _ in range(5):
            clk.t += 10
            t.acquire(a.lease_id)
        assert t.describe(a)["expires_in_s"] == 10.0   # renewal capped at the session end
        clk.t += 10
        assert t.count() == 0                          # ends exactly at the cap
        with pytest.raises(PreviewRefused) as e:
            t.acquire(a.lease_id)
        assert e.value.status == 410 and e.value.reason == "max_session"
        b, created = t.acquire()
        assert created

    def test_too_many_and_release(self):
        t, _ = self.mk(max_leases=2)
        a, _ = t.acquire()
        t.acquire()
        with pytest.raises(PreviewRefused) as e:
            t.acquire()
        assert e.value.status == 429
        assert t.release(a.lease_id) is True
        assert t.release(a.lease_id) is False
        t.acquire()


# ═════════════════════════════════════════════════════════════════════════════
# Roadsense manager preview hold
# ═════════════════════════════════════════════════════════════════════════════

class FakeCamera:
    def __init__(self):
        self.calls = []
        self.on = None

    @property
    def desired(self):
        return self.on

    def set(self, on):
        self.calls.append(on)
        self.on = on


class R:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


class MgrHarness:
    def __init__(self, target_active=False, api=None, **cfg):
        self.clock = Clock()
        self.target_active = target_active
        self.api = api
        self.systemctl = []
        self.camera = FakeCamera()
        self.released = 0
        kw = dict(tick_s=5.0, camera_settle_s=8.0, grace_s=30.0, start_timeout_s=60.0,
                  stop_timeout_s=40.0, api_dead_restart_s=0.0)
        kw.update(cfg)
        self.m = RoadsenseManager(
            RoadsenseConfig(units=("roadsense-camerad-dvr.service", "roadsense-api.service"), **kw),
            runner=self.run, camera=self.camera,
            health_fetch=lambda url, timeout: self.api,
            thermals=lambda: {}, clock=self.clock, async_probe=False,
            preview_release=self.on_release)

    def on_release(self):
        self.released += 1

    def run(self, cmd, timeout=10):
        self.systemctl.append(cmd[1:])
        verb = cmd[1]
        if verb == "is-active":
            st = "active" if self.target_active else "inactive"
            return R(0, "\n".join(st for _ in cmd[2:]) + "\n")
        if verb in ("start", "restart"):
            self.target_active = True
        elif verb == "stop":
            self.target_active = False
        return R(0)

    def tick(self, acc, dt=5.0):
        self.clock.t += dt
        self.m.tick(acc)

    def verbs(self):
        return [c[0] for c in self.systemctl if c[0] != "is-active"]

    @property
    def state(self):
        return self.m.state


class TestManagerPreviewHold:
    def test_hold_with_acc_off_powers_camera_only(self):
        h = MgrHarness()
        h.tick(False)
        assert h.state == STATE_OFF and h.camera.on is False
        h.m.set_preview_hold(True)
        h.m.tick(False)                  # acts at once (no 5 s wait)
        assert h.state == STATE_PREVIEW
        assert h.camera.on is True
        assert h.verbs() == []           # no systemctl start
        assert h.m.is_off()              # low CPU profile allowed, no "full"
        assert not h.m.is_active()
        assert h.m.holds_camera()        # manual set_relay ch4 refused
        assert h.m.status()["preview_hold"] is True
        h.m.set_preview_hold(False)
        h.m.tick(False)
        assert h.state == STATE_OFF and h.camera.on is False
        assert not h.m.holds_camera()

    def test_acc_on_during_preview_starts_stack_after_release(self):
        h = MgrHarness()
        h.m.set_preview_hold(True)
        h.tick(False)
        assert h.state == STATE_PREVIEW
        h.tick(True)
        assert h.released == 1           # reader let go before the start
        assert h.state == STATE_STARTING
        assert h.verbs() == ["start"]
        assert h.camera.on is True
        assert False not in h.camera.calls[1:]   # [0] = reconcile at the first tick
        h.api = {"camera_fps": 19.5}
        h.tick(True)
        assert h.state == STATE_RUNNING
        # lease ends while the stack runs: nothing changes
        h.m.set_preview_hold(False)
        h.tick(True)
        assert h.state == STATE_RUNNING and h.camera.on is True
        assert h.verbs() == ["start"]
        # key off: normal grace/stop, camera cut (no hold any more)
        h.tick(False)
        assert h.state == STATE_GRACE
        h.tick(False, dt=31)
        assert h.state == STATE_STOPPING
        h.tick(False)
        assert h.state == STATE_OFF and h.camera.on is False

    def test_preview_never_disturbs_running_stack(self):
        h = MgrHarness(target_active=True, api={})
        h.tick(True)
        assert h.state == STATE_RUNNING
        calls_before = list(h.camera.calls)
        h.m.set_preview_hold(True)
        for _ in range(5):
            h.tick(True)
        h.m.set_preview_hold(False)
        h.tick(True)
        assert h.state == STATE_RUNNING
        assert h.verbs() == []           # no stop/restart/start
        assert h.camera.calls == calls_before
        assert h.released == 0

    def test_acc_off_during_preview_keeps_camera_until_lease_end(self):
        h = MgrHarness(target_active=True, api={})
        h.tick(True)
        h.m.set_preview_hold(True)
        h.tick(False)
        assert h.state == STATE_GRACE
        h.tick(False, dt=31)
        assert h.state == STATE_STOPPING
        h.tick(False)
        assert h.state == STATE_PREVIEW  # stack down, camera kept for the preview
        assert h.camera.on is True and h.camera.calls[-1] is True
        assert h.verbs() == ["stop"]
        h.m.set_preview_hold(False)      # last lease gone
        h.m.tick(False)
        assert h.state == STATE_OFF and h.camera.on is False

    def test_camera_on_settle_aborted_by_key_off_with_hold(self):
        h = MgrHarness()
        h.tick(True)
        assert h.state == STATE_CAMERA_ON
        h.m.set_preview_hold(True)
        h.tick(False)
        assert h.state == STATE_PREVIEW and h.camera.on is True
        assert h.verbs() == []

    def test_shutdown_cuts_preview_only_camera(self):
        h = MgrHarness()
        h.m.set_preview_hold(True)
        h.tick(False)
        h.m.shutdown_preview()
        assert h.state == STATE_OFF and h.camera.on is False
        assert h.m.preview_hold is False

    def test_shutdown_leaves_running_stack_alone(self):
        h = MgrHarness(target_active=True, api={})
        h.tick(True)
        h.m.set_preview_hold(True)
        h.m.shutdown_preview()
        assert h.state == STATE_RUNNING and h.camera.on is True


# ═════════════════════════════════════════════════════════════════════════════
# Service (fake reader) + manager
# ═════════════════════════════════════════════════════════════════════════════

JPEG = b"\xff\xd8\xff\xdb\x00\x04\x00\x00\xff\xda\x00\x02DATA\xff\xd9"


class FakeReader:
    def __init__(self, clock):
        self.clock = clock
        self._running = False
        self.phase = (PHASE_ENUMERATING, "no device", clock())
        self.frame = None
        self.starts = self.stops = 0

    def running(self):
        return self._running

    def start(self):
        self._running = True
        self.starts += 1

    def stop(self, timeout=3.0):
        self._running = False
        self.stops += 1

    def status(self):
        return self.phase

    def latest(self):
        return self.frame

    def deliver(self, data=JPEG, seq=1):
        self.frame = Frame(data, seq, self.clock(), 0.0, make_etag(data), cp.SOURCE_DIRECT)
        self.phase = (PHASE_LIVE, "", self.clock())


class SvcHarness:
    def __init__(self, target_active=False, api=None, uv=False, stack_body=JPEG, **cfg):
        self.mh = MgrHarness(target_active=target_active, api=api)
        self.clock = self.mh.clock
        self.uv = uv
        self.published = []
        self.reader = FakeReader(self.clock)
        self.stack_body = stack_body
        self.stack_fetches = 0
        cfg.setdefault("grace_s", 0.0)     # immediate release; TestGrace sets it
        conf = CameraPreviewConfig(**cfg)

        async def fetch():
            self.stack_fetches += 1
            if self.stack_body is None:
                raise ConnectionRefusedError("down")
            return 200, "image/jpeg", self.stack_body

        self.svc = CameraPreviewService(
            conf, ManagerPreviewPower(self.mh.m), undervoltage=lambda: self.uv,
            publish=self.published.append, reader=self.reader, stack_fetch=fetch,
            scaler=lambda jpeg, w, q: b"\xff\xd8SCALED%d\xff\xd9" % w, clock=self.clock)
        self.mh.m.preview_release = self.svc.release_device

    def step(self, acc, dt=1.0):
        """One engine iteration: preview tick, then the manager tick."""
        self.clock.t += dt
        self.svc.tick(force=True)
        self.mh.m.tick(acc)
        self.svc.tick(force=True)

    def frame(self, **kw):
        return asyncio.run(self.svc.frame(**kw))


class TestService:
    def test_direct_preview_lifecycle_acc_off(self):
        h = SvcHarness()
        h.step(False, dt=5)
        lease = h.svc.lease()
        assert lease["ttl_s"] == 15.0 and lease["max_session_s"] == 600.0
        assert lease["phase"] == PHASE_POWERING
        h.step(False)
        assert h.mh.state == STATE_PREVIEW and h.mh.camera.on is True
        assert h.reader.running()
        assert h.svc.phase()[0] == PHASE_ENUMERATING
        with pytest.raises(cp.FrameUnavailable) as e:
            h.frame()
        assert e.value.status == 503 and e.value.phase == PHASE_ENUMERATING
        h.reader.deliver()
        data, hdr = h.frame(width=320)
        assert data == b"\xff\xd8SCALED320\xff\xd9"
        assert hdr["X-Frame-Source"] == "direct" and hdr["X-Preview-Scaled"] == "1"
        data, hdr = h.frame(width=1280)        # full size requested: original
        assert data == JPEG and hdr["X-Preview-Scaled"] == "0"
        h.svc.tick(force=True)
        assert h.published[-1]["phase"] == PHASE_LIVE
        assert h.published[-1]["source"] == "direct"
        # no renewal: TTL expiry returns everything
        h.step(False, dt=16)
        assert not h.reader.running()
        assert h.mh.state == STATE_OFF and h.mh.camera.on is False
        assert h.published[-1]["phase"] == PHASE_OFF and h.published[-1]["active"] is False
        with pytest.raises(cp.FrameUnavailable) as e:
            h.frame()
        assert e.value.status == 409            # no fetching while not previewing

    def test_delete_releases_immediately_and_restart_has_no_cooldown(self):
        h = SvcHarness()
        lid = h.svc.lease()["lease_id"]
        h.step(False)
        assert h.mh.state == STATE_PREVIEW
        assert h.svc.release(lid)
        h.step(False)
        assert h.mh.state == STATE_OFF and h.mh.camera.on is False
        h.svc.lease()
        h.step(False)
        assert h.mh.state == STATE_PREVIEW and h.mh.camera.on is True

    def test_two_clients_last_one_turns_off(self):
        h = SvcHarness()
        a = h.svc.lease()["lease_id"]
        b = h.svc.lease()["lease_id"]
        h.step(False)
        h.svc.release(a)
        h.step(False)
        assert h.mh.state == STATE_PREVIEW
        h.svc.release(b)
        h.step(False)
        assert h.mh.state == STATE_OFF

    def test_max_session_ends_preview(self):
        h = SvcHarness(max_session_s=60.0)
        lid = h.svc.lease()["lease_id"]
        for _ in range(5):
            h.step(False, dt=10)
            h.svc.lease(lid)
        h.step(False, dt=10)
        with pytest.raises(PreviewRefused) as e:
            h.svc.lease(lid)
        assert e.value.reason == "max_session"
        h.step(False)
        assert h.mh.state == STATE_OFF and h.mh.camera.on is False

    def test_acc_on_during_direct_preview_switches_to_stack(self):
        h = SvcHarness()
        lid = h.svc.lease()["lease_id"]
        h.step(False)
        assert h.reader.running()
        h.reader.deliver()
        h.step(True, dt=5)
        assert h.mh.released == 0                 # the service is the hook now
        assert not h.reader.running() and h.reader.stops >= 1
        assert h.mh.state == STATE_STARTING
        assert h.svc.phase() == (PHASE_WAITING, "roadsense starting")
        h.mh.api = {}
        h.step(True, dt=5)
        assert h.mh.state == STATE_RUNNING
        data, hdr = h.frame(width=1280)
        assert data == JPEG and hdr["X-Frame-Source"] == "stack"
        assert h.svc.phase()[0] == PHASE_LIVE
        # lease ends: stack untouched
        h.svc.release(lid)
        h.step(True, dt=5)
        assert h.mh.state == STATE_RUNNING and h.mh.camera.on is True
        assert h.mh.verbs() == ["start"]

    def test_stack_running_preview_is_free_and_cached(self):
        h = SvcHarness(target_active=True, api={})
        h.step(True, dt=5)
        assert h.mh.state == STATE_RUNNING
        h.svc.lease()
        h.step(True)
        assert not h.reader.running()             # no second reader
        h.frame()
        h.frame()                                  # within stack_min_interval_s: cached
        assert h.stack_fetches == 1
        h.clock.t += 1
        h.stack_body = JPEG.replace(b"DATA", b"DAT2")
        _, hdr = h.frame()
        assert h.stack_fetches == 2 and hdr["X-Frame-Seq"] == "2"
        assert h.mh.verbs() == []

    def test_stack_frame_unavailable_is_503(self):
        h = SvcHarness(target_active=True, api={}, stack_body=None)
        h.step(True, dt=5)
        h.svc.lease()
        with pytest.raises(cp.FrameUnavailable) as e:
            h.frame()
        assert e.value.status == 503 and e.value.phase == PHASE_WAITING
        assert "roadsense frame unavailable" in e.value.reason

    def test_acc_off_during_stack_preview_moves_to_direct(self):
        h = SvcHarness(target_active=True, api={})
        h.step(True, dt=5)
        lid = h.svc.lease()["lease_id"]
        h.step(False, dt=5)
        assert h.mh.state == STATE_GRACE and not h.reader.running()
        for _ in range(4):
            h.svc.lease(lid)
            h.step(False, dt=10)
        assert h.mh.state == STATE_PREVIEW
        assert h.reader.running() and h.mh.camera.on is True
        h.svc.release(lid)
        h.step(False)
        assert h.mh.state == STATE_OFF and h.mh.camera.on is False
        assert not h.reader.running()

    def test_undervoltage_refuses_power_up(self):
        h = SvcHarness(uv=True)
        with pytest.raises(PreviewRefused) as e:
            h.svc.lease()
        assert e.value.status == 409 and e.value.reason == "undervoltage"
        h.step(False)
        assert h.mh.state == STATE_OFF and h.mh.camera.calls[-1:] != [True]

    def test_undervoltage_allows_viewing_running_stack(self):
        h = SvcHarness(target_active=True, api={}, uv=True)
        h.step(True, dt=5)
        h.svc.lease()
        h.step(True)
        assert h.svc.leases.count() == 1

    def test_undervoltage_during_direct_preview_ends_it(self):
        h = SvcHarness()
        h.svc.lease()
        h.step(False)
        assert h.mh.state == STATE_PREVIEW
        h.uv = True
        h.step(False)
        assert h.svc.leases.count() == 0
        assert h.mh.state == STATE_OFF and h.mh.camera.on is False
        assert h.published[-1]["phase"] == PHASE_ERROR
        assert h.published[-1]["reason"] == "undervoltage"

    def test_enumerate_timeout_reports_error(self):
        h = SvcHarness(enumerate_timeout_s=10.0, frame_timeout_s=10.0)
        lid = h.svc.lease()["lease_id"]
        h.step(False, dt=0.5)
        h.reader.phase = (PHASE_ENUMERATING, "no 1902:3241 block device yet", h.clock())
        h.clock.t += 9
        assert h.svc.phase()[0] == PHASE_ENUMERATING
        h.clock.t += 2
        phase, reason = h.svc.phase()
        assert phase == PHASE_ERROR and "camera not found" in reason
        h.svc.lease(lid)
        h.reader.phase = (PHASE_WAITING, "camera info sector empty", h.clock())
        h.clock.t += 11
        assert h.svc.leases.count() == 1        # renewed 11 s ago
        assert h.svc.phase()[0] == PHASE_ERROR

    def test_shutdown_releases_everything(self):
        h = SvcHarness()
        h.svc.lease()
        h.step(False)
        assert h.mh.state == STATE_PREVIEW and h.reader.running()
        h.svc.shutdown()
        assert h.svc.leases.count() == 0
        assert not h.reader.running()
        assert h.mh.state == STATE_OFF and h.mh.camera.on is False

    def test_relay_power_restores_previous_state(self):
        cam = FakeCamera()
        p = RelayPreviewPower(cam)
        p.set_hold(True)
        assert cam.on is True and p.stack_state() == STATE_PREVIEW
        p.set_hold(False)
        assert cam.on is False and p.stack_state() == STATE_OFF
        cam.set(True)                              # operator had it on
        p.set_hold(True)
        p.set_hold(False)
        assert cam.on is True

    def test_disabled_config(self):
        h = SvcHarness(enabled=False)
        with pytest.raises(PreviewRefused) as e:
            h.svc.lease()
        assert e.value.status == 503


# ═════════════════════════════════════════════════════════════════════════════
# Direct reader against a synthetic DVR block device
# ═════════════════════════════════════════════════════════════════════════════

AN_NUM_OK = 0x006B    # hi 0 -> lo 0x6B passes isAnNum


def info_sector(size, seq, check, flags=0x0200, an=AN_NUM_OK, alen=0):
    b = bytearray(512)
    b[0:2] = flags.to_bytes(2, "little")
    b[0x0C:0x10] = size.to_bytes(4, "little")
    b[0x10:0x14] = seq.to_bytes(4, "little")
    b[0x14:0x18] = alen.to_bytes(4, "little")
    b[0x2C:0x30] = check.to_bytes(4, "little")
    b[0x30:0x32] = an.to_bytes(2, "little")
    return bytes(b)


class FakeDvr:
    """Sector-addressed fake of the camera: MBR (layout A), info + video."""

    def __init__(self):
        self.present = True
        self.busy = False
        self.zero = False          # power-on period: info + frame all zeros
        self.fail_reads = False    # device vanished under an open fd
        self.seq = 0
        self.frame = JPEG
        self.bad_check = 0         # next N frame reads carry a wrong check word
        self.reads = []
        self.opens = 0
        self.closes = 0
        mbr = bytearray(512)
        mbr[0x1C6:0x1CA] = (8).to_bytes(4, "little")
        mbr[510:512] = b"\x55\xaa"
        self.mbr = bytes(mbr)

    def find(self):
        return "/dev/sdz" if self.present else None

    # io_ protocol
    def open(self, path):
        if self.busy:
            raise OSError(errno.EBUSY, "Device or resource busy")
        self.opens += 1
        return 7

    def close(self, fd):
        self.closes += 1

    def _payload(self):
        size = len(self.frame) + 6
        data = self.frame + b"\x00" * 6        # trailing stale bytes after EOI
        data = data + b"\x00" * ((size + 3) // 4 * 4 + 8 - len(data))
        off = ((size - 8) >> 2) * 4
        check = int.from_bytes(data[off:off + 4], "little")
        return size, data, check

    def pread(self, fd, size, offset):
        self.reads.append((offset // 512, size))
        if self.fail_reads:
            raise OSError(errno.EIO, "Input/output error")
        lba = offset // 512
        if lba == 0:
            return self.mbr if not self.zero else bytes(512)
        if lba == 0x380:
            if self.zero:
                return bytes(512)
            vsize, _, check = self._payload()
            return info_sector(vsize, self.seq, check)
        if lba == 0x391:
            if self.zero:
                return bytes(size)
            vsize, data, check = self._payload()
            if self.bad_check:
                self.bad_check -= 1
                data = bytearray(data)
                off = ((vsize - 8) >> 2) * 4
                data[off:off + 4] = (check ^ 0xFFFFFFFF).to_bytes(4, "little")
                data = bytes(data)
            return (data + bytes(size))[:size]
        return bytes(size)


def mk_reader(dvr, clock=None):
    clock = clock or Clock()
    return DvrReader(capture_interval_s=0.5, find_device=dvr.find, io_=dvr,
                     clock=clock, wall=lambda: 1.7e9), clock


class TestDvrReader:
    def test_parse_and_terminate(self):
        h = parse_info(info_sector(1000, 5, 0xABCD))
        assert h.valid and h.vsize == 1000 and h.vseq == 5 and h.check == 0xABCD
        assert not parse_info(bytes(512)).valid                     # zeros
        assert not parse_info(info_sector(1000, 5, 0, an=0x1234)).valid
        assert not parse_info(info_sector(0x4B001, 5, 0)).valid
        assert terminate_jpeg(bytes(64)) is None
        assert terminate_jpeg(JPEG + b"junk") == JPEG
        assert terminate_jpeg(b"\xff\xd8\xff\xdb\x00\x04\x00\x00\xff\xda\x00\x02AB").endswith(b"\xff\xd9")

    def test_enumerating_then_zero_period_then_live(self):
        dvr = FakeDvr()
        dvr.present = False
        r, clk = mk_reader(dvr)
        r.step()
        assert r.status()[0] == PHASE_ENUMERATING
        dvr.present, dvr.zero = True, True
        r.step()                                   # MBR zero -> not opened
        assert r.status()[0] == PHASE_WAITING and dvr.closes == 1
        dvr.zero = False
        r.step()                                   # opens + first frame (seq 0)
        assert r.status()[0] == PHASE_LIVE and r.latest().seq == 0
        dvr.zero = True                            # info sector zero again
        r.step()
        assert r.status()[0] == PHASE_WAITING and r.latest().seq == 0
        dvr.zero = False
        dvr.seq = 1
        wait = r.step()
        assert r.status()[0] == PHASE_LIVE
        assert r.latest().jpeg == JPEG and r.latest().seq == 1
        assert wait == 0.5
        assert all(lba in (0, 0x380, 0x391) for lba, _ in dvr.reads)   # fixed LBAs only

    def test_reenumeration_reopens(self):
        dvr = FakeDvr()
        dvr.seq = 1
        r, clk = mk_reader(dvr)
        r.step(); r.step()
        assert r.status()[0] == PHASE_LIVE
        dvr.fail_reads = True                      # camera re-enumerates
        r.step()
        assert r.status()[0] == PHASE_ENUMERATING and dvr.closes == 1
        dvr.fail_reads, dvr.present = False, False
        r.step()
        assert r.status()[0] == PHASE_ENUMERATING
        dvr.present = True
        dvr.seq = 2
        r.step(); r.step()
        assert r.status()[0] == PHASE_LIVE and r.latest().seq == 2
        assert dvr.opens == 2

    def test_long_zero_period_keeps_device_open(self):
        dvr = FakeDvr()
        r, clk = mk_reader(dvr)
        dvr.zero = True
        orig = dvr.pread                           # MBR readable, info zero
        dvr.pread = lambda fd, size, off: (dvr.mbr if off == 0 else orig(fd, size, off))
        r.step()
        since = r.status()[2]
        for _ in range(10):
            clk.t += 3
            r.step()
        phase, _, since2 = r.status()
        assert phase == PHASE_WAITING and since2 == since   # frame_timeout can fire
        assert dvr.opens == 1 and dvr.closes == 0

    def test_busy_device_is_reported(self):
        dvr = FakeDvr()
        dvr.busy = True
        r, _ = mk_reader(dvr)
        r.step()
        phase, reason, _ = r.status()
        assert phase == PHASE_ENUMERATING and "busy" in reason

    def test_check_word_mismatch_retries(self):
        dvr = FakeDvr()
        dvr.seq = 3
        dvr.bad_check = 1
        r, _ = mk_reader(dvr)
        r.step(); r.step()
        assert r.latest() is not None and r.latest().seq == 3
        dvr.seq = 4
        dvr.bad_check = 5                          # never consistent
        r.step()
        assert r.latest().seq == 3                 # dropped, old frame kept

    def test_stalled_sequence_kicks_then_goes_stale_then_reopens(self):
        dvr = FakeDvr()
        dvr.seq = 1
        r, clk = mk_reader(dvr)
        r.step(); r.step()
        assert r.status()[0] == PHASE_LIVE
        clk.t += 1.0
        n = len(dvr.reads)
        r.step()                                   # same seq -> kick read
        assert any(sz == (len(JPEG) + 6 & ~0x1FF) + 0x1400 for _, sz in dvr.reads[n:])
        clk.t += 3.0
        r.step()
        assert r.status()[0] == PHASE_WAITING
        clk.t += 12.0
        r.step()
        assert r.status()[0] == PHASE_ENUMERATING and dvr.closes == 1

    def test_thread_start_stop_closes_device(self):
        dvr = FakeDvr()
        dvr.seq = 1
        r = DvrReader(capture_interval_s=0.05, find_device=dvr.find, io_=dvr)
        r.start()
        for _ in range(100):
            if r.latest() is not None:
                break
            time.sleep(0.01)
        assert r.latest() is not None
        r.stop()
        assert not r.running() and dvr.closes >= 1 and r.latest() is None


# ═════════════════════════════════════════════════════════════════════════════
# Real Pillow scaler (only where Pillow is installed)
# ═════════════════════════════════════════════════════════════════════════════

def test_pillow_scaler_downscales():
    PIL = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    PIL.new("RGB", (1280, 720), (10, 200, 30)).save(buf, "JPEG", quality=90)
    scale = cp.make_pillow_scaler()
    out = scale(buf.getvalue(), 320, 60)
    im = PIL.open(io.BytesIO(out))
    assert im.size == (320, 180)
    assert scale(buf.getvalue(), 1280, 60) is None     # no upscaling


def test_scaler_absent_without_pillow(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **kw):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no PIL")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)
    assert cp.make_pillow_scaler() is None


# ═════════════════════════════════════════════════════════════════════════════
# REST surface (real FastAPI app under uvicorn)
# ═════════════════════════════════════════════════════════════════════════════

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def api():
    import uvicorn
    from cyberpunk_computer.api import app as app_mod

    h = SvcHarness(max_session_s=60.0)

    class Bridge:
        def attach_loop(self, loop):
            pass

        def latest_snapshot(self):
            return {"state": {}}

    app = app_mod.create_app(Bridge(), db=object(), camera_preview=h.svc)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", loop="asyncio"))
    server.install_signal_handlers = lambda: None
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield h, "http://127.0.0.1:%d/api/v1/camera/preview" % port
    server.should_exit = True
    t.join(5)


def _req(method, url, body=None, headers=None):
    if isinstance(body, bytes):           # sendBeacon-like raw text body
        data, hdrs = body, {"Content-Type": "text/plain;charset=UTF-8"}
    else:
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"} if data is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


class TestRest:
    def test_lease_frame_release_flow(self, api):
        h, base = api
        st, _, body = _req("GET", base + "/frame.jpg")
        assert st == 409                                     # no lease, no frames
        st, _, body = _req("POST", base + "/lease")
        assert st == 200
        lease = json.loads(body)
        lid = lease["lease_id"]
        assert lease["phase"] == PHASE_POWERING and lease["ttl_s"] == 15.0
        h.step(False)
        st, hdr, body = _req("GET", base + "/frame.jpg?w=320")
        assert st == 503
        assert hdr["Retry-After"] == "1"
        assert json.loads(body)["detail"]["phase"] == PHASE_ENUMERATING
        h.reader.deliver()
        st, hdr, body = _req("GET", base + "/frame.jpg?w=320&lease=" + lid)
        assert st == 200 and hdr["content-type"] == "image/jpeg"
        assert body == b"\xff\xd8SCALED320\xff\xd9"
        assert float(hdr["X-Frame-Age"]) >= 0 and hdr["X-Frame-Source"] == "direct"
        etag = hdr["ETag"]
        st, _, _ = _req("GET", base + "/frame.jpg?w=320", headers={"If-None-Match": etag})
        assert st == 304
        st, _, body = _req("GET", base + "/frame.jpg")      # default width 640
        assert body == b"\xff\xd8SCALED640\xff\xd9"
        # renew
        st, _, body = _req("POST", base + "/lease", {"lease_id": lid})
        assert st == 200 and json.loads(body)["lease_id"] == lid
        st, _, body = _req("GET", base)
        status = json.loads(body)
        assert status["active"] is True and status["phase"] == PHASE_LIVE
        # sendBeacon form
        st, _, body = _req("POST", base + "/lease/%s/release" % lid, b"")
        assert st == 200 and json.loads(body)["released"] is True
        st, _, body = _req("DELETE", base + "/lease/" + lid)
        assert st == 200 and json.loads(body)["released"] is False
        h.step(False)
        assert h.mh.state == STATE_OFF and h.mh.camera.on is False

    def test_delete(self, api):
        h, base = api
        lid = json.loads(_req("POST", base + "/lease", {})[2])["lease_id"]
        st, _, body = _req("DELETE", base + "/lease/" + lid)
        assert st == 200 and json.loads(body) == {"released": True, "stop": False, "leases": 0}

    def test_validation(self, api):
        h, base = api
        st, _, body = _req("POST", base + "/lease", {"lease_id": "../../etc"})
        assert st == 400 and json.loads(body)["detail"]["reason"] == "bad_lease_id"
        st, _, _ = _req("POST", base + "/lease", [1, 2])
        assert st in (400, 422)
        st, _, body = _req("POST", base + "/lease", {"lease_id": "abcdef0123456789"})
        assert st == 410 and json.loads(body)["detail"]["reason"] == "expired"
        _req("POST", base + "/lease")
        st, _, _ = _req("GET", base + "/frame.jpg?w=10")
        assert st == 422
        st, _, _ = _req("GET", base + "/frame.jpg?w=5000")
        assert st == 422
        st, _, _ = _req("GET", base + "/frame.jpg?q=5")
        assert st == 422
        st, _, _ = _req("GET", base + "/frame.jpg?lease=zz")
        assert st == 400
        st, _, body = _req("GET", base + "/frame.jpg?lease=00ff")
        assert st == 409                                     # unknown lease

    def test_undervoltage_and_max_session(self, api):
        h, base = api
        h.uv = True
        st, _, body = _req("POST", base + "/lease")
        assert st == 409 and json.loads(body)["detail"]["reason"] == "undervoltage"
        h.uv = False
        lid = json.loads(_req("POST", base + "/lease")[2])["lease_id"]
        for _ in range(6):
            h.clock.t += 10
            st, _, body = _req("POST", base + "/lease", {"lease_id": lid})
        assert st == 410 and json.loads(body)["detail"]["reason"] == "max_session"



# ═════════════════════════════════════════════════════════════════════════════
# Grace (camera kept on after a soft end) + resume
# ═════════════════════════════════════════════════════════════════════════════

class TestGrace:
    def live(self, **kw):
        kw.setdefault("grace_s", 180.0)
        h = SvcHarness(**kw)
        lid = h.svc.lease()["lease_id"]
        h.step(False)
        h.reader.deliver()
        assert h.mh.state == STATE_PREVIEW and h.svc.phase()[0] == PHASE_LIVE
        return h, lid

    def test_soft_release_keeps_camera_and_resume_is_instant(self):
        h, lid = self.live()
        relay_calls = list(h.mh.camera.calls)
        h.svc.release(lid)                       # page hidden / pagehide beacon
        h.step(False)
        st = h.svc.status()
        assert st["phase"] == cp.PHASE_GRACE and st["grace"] is True
        assert st["grace_left_s"] == 180 and st["active"] is False
        assert st["camera_phase"] == PHASE_LIVE
        assert h.mh.state == STATE_PREVIEW and h.reader.running()
        assert h.published[-1]["phase"] == cp.PHASE_GRACE
        with pytest.raises(cp.FrameUnavailable):  # still no frames without a lease
            h.frame()
        h.step(False, dt=100)
        h.reader.deliver(seq=2)
        r = h.svc.lease(resume=True)             # tab visible again
        assert r["resumed"] is True and r["phase"] == PHASE_LIVE
        h.step(False)
        assert h.svc.status()["grace"] is False
        assert h.reader.starts == 1              # never restarted
        assert h.mh.camera.calls == relay_calls  # no relay toggling at all
        data, _ = h.frame(width=1280)
        assert data == JPEG

    def test_grace_reported_before_the_next_tick(self):
        # A reload's beacon release and the new page's status GET can land
        # within one engine tick: the status must already say grace.
        h, lid = self.live()
        h.svc.release(lid)
        st = h.svc.status()
        assert st["phase"] == cp.PHASE_GRACE and st["grace"] is True
        assert h.svc.lease(resume=True)["resumed"] is True

    def test_ttl_expiry_grace_then_off(self):
        h, lid = self.live()
        h.step(False, dt=16)                     # lease lapsed (tab closed, no beacon)
        h.reader.deliver(seq=2)                  # the fake camera keeps delivering
        assert h.svc.status()["phase"] == cp.PHASE_GRACE
        assert h.svc.status()["camera_phase"] == PHASE_LIVE
        h.step(False, dt=170)
        assert h.mh.state == STATE_PREVIEW
        h.step(False, dt=11)                     # 181 s of grace
        assert h.mh.state == STATE_OFF and h.mh.camera.on is False
        assert not h.reader.running()
        assert h.svc.status()["phase"] == PHASE_OFF
        with pytest.raises(PreviewRefused) as e:  # no cold auto-start
            h.svc.lease(resume=True)
        assert e.value.status == 409 and e.value.reason == "not_resumable"

    def test_explicit_stop_is_immediate(self):
        h, lid = self.live()
        h.svc.release(lid, stop=True)
        h.step(False)
        assert h.mh.state == STATE_OFF and h.mh.camera.on is False
        assert h.svc.status()["grace"] is False

    def test_stop_during_grace_ends_it(self):
        h, lid = self.live()
        h.svc.release(lid)
        h.step(False)
        assert h.svc.status()["grace"]
        assert h.svc.release(lid, stop=True) is False    # lease already gone
        h.step(False)
        assert h.mh.state == STATE_OFF and h.mh.camera.on is False

    def test_stop_by_one_viewer_leaves_the_other(self):
        h, a = self.live()
        b = h.svc.lease()["lease_id"]
        h.svc.release(a, stop=True)
        h.step(False)
        assert h.mh.state == STATE_PREVIEW and h.svc.leases.count() == 1
        h.svc.release(b)                         # soft: grace
        h.step(False)
        assert h.svc.status()["grace"] is True

    def test_cap_bounds_lease_time_and_grace_adds_one_period(self):
        h, lid = self.live(max_session_s=60.0)
        t0 = h.clock.t
        h.step(False, dt=5)
        h.svc.release(lid)                       # reload at +6 s
        h.step(False, dt=2)
        lid = h.svc.lease(resume=True)["lease_id"]
        assert lid
        r = h.svc.lease(lid)
        assert r["session_left_s"] <= 60 - 8    # resumed lease shares the viewing cap
        while True:
            h.step(False, dt=10)
            try:
                h.svc.lease(lid)
            except PreviewRefused as e:
                assert e.reason == "max_session"
                break
        cap_end = h.clock.t
        assert cap_end - t0 <= 60 + 10
        h.step(False)
        assert h.svc.status()["phase"] == cp.PHASE_GRACE    # one grace after the cap
        with pytest.raises(PreviewRefused) as e:             # auto-resume refused
            h.svc.lease(resume=True)
        assert e.value.reason == "max_session"
        h.step(False, dt=181)
        assert h.mh.state == STATE_OFF and h.mh.camera.on is False
        assert h.clock.t - t0 <= 60 + 180 + 15

    def test_explicit_start_during_grace_after_cap_is_instant_and_fresh(self):
        h, lid = self.live(max_session_s=60.0)
        for _ in range(7):
            h.step(False, dt=10)
            try:
                h.svc.lease(lid)
            except PreviewRefused:
                break
        h.step(False)
        assert h.svc.status()["grace"]
        h.reader.deliver(seq=9)
        r = h.svc.lease()                        # user presses START again
        assert r["session_left_s"] == 60.0 and r["phase"] == PHASE_LIVE
        assert h.reader.starts == 1

    def test_undervoltage_during_grace_cuts_now(self):
        h, lid = self.live()
        h.svc.release(lid)
        h.step(False)
        assert h.svc.status()["grace"]
        h.uv = True
        h.step(False)
        assert h.mh.state == STATE_OFF and h.mh.camera.on is False
        assert h.svc.status()["phase"] == PHASE_ERROR
        h.uv = False
        with pytest.raises(PreviewRefused):
            h.svc.lease(resume=True)

    def test_shutdown_during_grace_cuts_now(self):
        h, lid = self.live()
        h.svc.release(lid)
        h.step(False)
        h.svc.shutdown()
        assert h.mh.state == STATE_OFF and h.mh.camera.on is False
        assert not h.reader.running()

    def test_no_grace_when_stack_has_the_camera(self):
        h = SvcHarness(target_active=True, api={}, grace_s=180.0)
        h.step(True, dt=5)
        lid = h.svc.lease()["lease_id"]
        h.step(True)
        h.svc.release(lid)
        h.step(True)
        assert h.svc.status()["grace"] is False and h.svc.status()["phase"] == PHASE_OFF
        assert h.mh.m.preview_hold is False
        assert h.mh.state == STATE_RUNNING and h.mh.verbs() == []

    def test_acc_on_during_grace_hands_over_and_ends_grace(self):
        h, lid = self.live()
        h.svc.release(lid)
        h.step(False)
        h.step(True, dt=5)
        assert h.mh.state == STATE_STARTING and not h.reader.running()
        h.step(True)
        assert h.svc.status()["grace"] is False
        assert h.mh.m.preview_hold is False
        h.mh.api = {}
        h.step(True, dt=5)
        assert h.mh.state == STATE_RUNNING


class TestReenumeration:
    """Car log 2026-11-13: live at +7 s, info read EIO at ~+29 s (dashcam
    re-enumerates), node changes name; the reader must find the new one."""

    def test_disappear_new_node_recover(self):
        dvr = FakeDvr()
        dvr.seq = 1
        nodes = {"list": ["/dev/sdg"]}
        stale = {"/dev/sdg"}
        opened = []
        r = DvrReader(capture_interval_s=0.5, find_device=lambda: list(nodes["list"]),
                      io_=dvr, clock=Clock(), wall=lambda: 1.7e9)
        real_open = dvr.open

        def open_(path):
            opened.append(path)
            if path in stale and dvr.fail_reads:
                raise OSError(errno.ENODEV, "No such device")
            return real_open(path)
        dvr.open = open_
        r.step(); r.step()
        assert r.status()[0] == PHASE_LIVE
        dvr.fail_reads = True                    # disconnect under the open fd
        r.step()
        assert r.status()[0] == PHASE_ENUMERATING
        nodes["list"] = []                       # gone from sysfs
        r.step(); r.step()
        assert r.status()[0] == PHASE_ENUMERATING and r.attempts == 2
        nodes["list"] = ["/dev/sdg", "/dev/sdh"]  # stale sdg lingers, new sdh
        real_pread = dvr.pread

        def pread(fd, size, off):
            if dvr.fail_reads and opened[-1] == "/dev/sdh" and pread.flaky > 0:
                pread.flaky -= 1                 # node present, reads fail a few s
                raise OSError(errno.EIO, "Input/output error")
            if opened[-1] == "/dev/sdh":
                dvr.fail_reads = False
            return real_pread(fd, size, off)
        pread.flaky = 2
        dvr.pread = pread
        r.step()
        assert r.status()[0] == PHASE_ENUMERATING
        assert "MBR read failed" in r.status()[1]
        r.step()
        assert r.status()[0] == PHASE_ENUMERATING
        dvr.seq = 2
        r.step()                                 # sdg ENODEV, sdh opens + frame
        assert r._path == "/dev/sdh"
        assert r.status()[0] == PHASE_LIVE and r.latest().seq == 2
        assert r.attempts == 0
        assert opened.count("/dev/sdg") >= 3     # every attempt re-scans and retries

    def test_find_dvr_blocks_lists_all(self, tmp_path):
        sysb = tmp_path / "block"
        sysb.mkdir()
        for name, vid in (("sda", "1234"), ("sdg", "1902"), ("sdh", "1902")):
            dev = tmp_path / "devices" / name / "usb"
            dev.mkdir(parents=True)
            (dev / "idVendor").write_text(vid)
            (dev / "idProduct").write_text("3241")
            disk = dev / "host" / name
            disk.mkdir(parents=True)
            (sysb / name).symlink_to(disk)
        assert cp.find_dvr_blocks(str(sysb)) == ["/dev/sdg", "/dev/sdh"]
        assert cp.find_dvr_block(str(sysb)) == "/dev/sdg"


@pytest.fixture()
def grace_api():
    import uvicorn
    from cyberpunk_computer.api import app as app_mod

    h = SvcHarness(grace_s=180.0)

    class Bridge:
        def attach_loop(self, loop):
            pass

        def latest_snapshot(self):
            return {"state": {}}

    app = app_mod.create_app(Bridge(), db=object(), camera_preview=h.svc)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", loop="asyncio"))
    server.install_signal_handlers = lambda: None
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield h, "http://127.0.0.1:%d/api/v1/camera/preview" % port
    server.should_exit = True
    t.join(5)


class TestRestGrace:
    def test_status_with_and_without_slash(self, grace_api):
        h, base = grace_api
        for url in (base, base + "/"):
            st, _, body = _req("GET", url)
            assert st == 200 and json.loads(body)["phase"] == PHASE_OFF

    def test_soft_release_grace_resume_and_hard_stop(self, grace_api):
        h, base = grace_api
        st, _, body = _req("POST", base + "/lease", {"resume": True})
        assert st == 409 and json.loads(body)["detail"]["reason"] == "not_resumable"
        st, _, body = _req("POST", base + "/lease", {"resume": "yes"})
        assert st == 400
        lid = json.loads(_req("POST", base + "/lease")[2])["lease_id"]
        h.step(False)
        h.reader.deliver()
        _req("POST", base + "/lease/%s/release" % lid, b"")      # beacon = soft
        h.step(False)
        st, _, body = _req("GET", base + "/")
        d = json.loads(body)
        assert d["phase"] == "grace" and d["grace_left_s"] == 180 and d["camera_phase"] == "live"
        st, _, body = _req("POST", base + "/lease", {"resume": True})
        d = json.loads(body)
        assert st == 200 and d["resumed"] is True and d["phase"] == "live"
        st, _, body = _req("DELETE", base + "/lease/%s?stop=1" % d["lease_id"])
        assert json.loads(body) == {"released": True, "stop": True, "leases": 0}
        h.step(False)
        assert h.mh.state == STATE_OFF and h.mh.camera.on is False
