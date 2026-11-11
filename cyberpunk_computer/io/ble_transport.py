"""
BLE satellite transport — the backend half of ``prius-bled`` (docs/SATELLITES.md §4).

The backend never touches BlueZ: ``prius-bled`` (root daemon on the phone) owns
the adapter and exposes a Unix control socket speaking NDJSON. This module is
the client of that socket. It deliberately imports nothing BlueZ/D-Bus related
so the backend (and the tests) run on any host; when bled is not running the
transport simply reports ``available=False`` and keeps retrying.

Threading: one daemon IO thread owns the socket (connect with backoff
0.5 s → 10 s, read events, write queued commands). Events become
:class:`LinkEvent` objects on a thread-safe queue that the engine loop drains
through :meth:`BleTransport.poll`; every public method only touches locked
state or queues, so none of them blocks the engine loop.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import select
import socket
import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

from .satellite_transport import (
    TRANSPORT_BLE,
    DiscoveredDevice,
    LinkEvent,
    SatelliteTransport,
    TransportStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_SOCKET = "/run/prius/bled.sock"
BACKOFF_MIN_S = 0.5
BACKOFF_MAX_S = 10.0
SCAN_SECONDS = 5
SCAN_MIN_GAP_S = 60.0          # discover() without force not more often than this
DISCOVERY_TTL_S = 600.0
READ_MAX = 1 << 20             # bled -> backend line cap (a scan list can be long)


def _fmt_interval(ms) -> str:
    """10000 -> "10 s", 1000 -> "1 s", 250 -> "250 ms" (dashboard detail)."""
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return ""
    if ms >= 1000 and ms % 1000 == 0:
        return "%d s" % (ms // 1000)
    if ms >= 1000:
        return "%.1f s" % (ms / 1000.0)
    return "%d ms" % ms


class BleTransport(SatelliteTransport):
    """SatelliteTransport over the ``prius-bled`` control socket.

    ``rediscover_s``: when set, :meth:`tick` starts a scan on that cadence and
    on the bled-reachable edge; by default the registry drives rediscovery by
    calling :meth:`discover` (the 60 s gap still applies unless forced).
    """

    name = TRANSPORT_BLE

    def __init__(self, socket_path: str = DEFAULT_SOCKET, *,
                 scan_seconds: int = SCAN_SECONDS,
                 scan_min_gap_s: float = SCAN_MIN_GAP_S,
                 discovery_ttl_s: float = DISCOVERY_TTL_S,
                 rediscover_s: Optional[float] = None,
                 backoff_min_s: float = BACKOFF_MIN_S,
                 backoff_max_s: float = BACKOFF_MAX_S):
        self.socket_path = socket_path
        self.scan_seconds = scan_seconds
        self.scan_min_gap_s = scan_min_gap_s
        self.discovery_ttl_s = discovery_ttl_s
        self.rediscover_s = rediscover_s
        self.backoff_min_s = backoff_min_s
        self.backoff_max_s = backoff_max_s

        self._events: "queue.Queue[LinkEvent]" = queue.Queue()
        self._out: "queue.Queue[bytes]" = queue.Queue()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._wake_r, self._wake_w = -1, -1

        # state below is guarded by _lock
        self._connected = False
        self._hello: dict = {}
        self._bled_status: dict = {}
        self._links: Dict[str, DiscoveredDevice] = {}     # addr -> linked device
        self._scan: Dict[str, DiscoveredDevice] = {}      # addr -> last scan sighting
        self._last_discovery = 0.0                        # time.time() of the last scan result
        self._last_scan_request = 0.0                     # time.monotonic()
        self._scan_pending = False
        self._was_available = False
        self._last_auto_scan = 0.0
        # desired central role (None = never set); re-sent after every bled hello
        self._central: Optional[Tuple[bool, Tuple[str, ...]]] = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        self._thread = threading.Thread(target=self._run, name="ble-transport", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._wake()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=2.0)
        for fd in (self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._wake_r = self._wake_w = -1

    # ------------------------------------------------------------------ engine API
    def tick(self, now: float) -> None:
        if not self.rediscover_s:
            return
        with self._lock:
            avail = self._connected
            edge = avail and not self._was_available
            self._was_available = avail
        mono = time.monotonic()
        if avail and (edge or mono - self._last_auto_scan >= self.rediscover_s):
            self._last_auto_scan = mono
            self.discover(force=edge)

    def poll(self) -> Optional[LinkEvent]:
        try:
            return self._events.get_nowait()
        except queue.Empty:
            return None

    def send(self, address: str, line: str) -> bool:
        addr = address.upper()
        with self._lock:
            if not self._connected or addr not in self._links:
                return False
        return self._command({"cmd": "send", "addr": addr, "line": line.rstrip("\n")})

    def disconnect(self, address: str) -> None:
        self._command({"cmd": "disconnect", "addr": address.upper()})

    def set_pairable(self, seconds: float) -> None:
        self._command({"cmd": "pairable", "seconds": max(0, int(round(seconds)))})

    def set_advertisement(self, interval_ms: int, flags: int) -> None:
        self._command({"cmd": "adv", "interval_ms": int(interval_ms), "flags": int(flags) & 0xFFFF})

    def set_central(self, enabled: bool, addresses: Sequence[str] = ()) -> None:
        """Central role on/off + the enrolled beacon addresses to connect to.
        Remembered and re-sent after every (re)connect to bled, so a bled or
        backend restart converges without the registry noticing."""
        want = (bool(enabled), tuple(sorted({str(a).upper() for a in addresses})))
        with self._lock:
            self._central = want
        self._send_central(want)

    def _send_central(self, want: Tuple[bool, Tuple[str, ...]]) -> None:
        self._command({"cmd": "central", "enabled": want[0], "addresses": list(want[1])})

    def request_status(self) -> None:
        self._command({"cmd": "status"})

    def discover(self, force: bool = False) -> None:
        mono = time.monotonic()
        with self._lock:
            if not self._connected:
                return
            if not force and (self._scan_pending or
                              mono - self._last_scan_request < self.scan_min_gap_s):
                return
            self._last_scan_request = mono
            self._scan_pending = True
        self._command({"cmd": "scan", "seconds": self.scan_seconds})

    def discovered(self) -> List[DiscoveredDevice]:
        now = time.time()
        with self._lock:
            linked = list(self._links.values())
            seen = {d.address for d in linked}
            fresh = [d for a, d in self._scan.items()
                     if a not in seen and now - d.last_seen <= self.discovery_ttl_s]
        return linked + sorted(fresh, key=lambda d: -d.last_seen)

    def status(self) -> TransportStatus:
        with self._lock:
            connected = self._connected and bool(self._hello)   # usable once bled said hello
            st = dict(self._bled_status)
            adv = st.get("adv") or self._hello.get("adv") or {}
            last = self._last_discovery
            central = st.get("central")
            if not isinstance(central, dict):   # older bled / no status yet: what we asked for
                central = {"enabled": bool(self._central and self._central[0])}
        seen = len(self.discovered())
        if not connected:
            return TransportStatus(self.name, available=False, state="down",
                                   detail="bled unreachable", last_discovery=last,
                                   devices_seen=seen)
        powered = st.get("powered", True)
        if not powered:
            state, detail = "down", "adapter off"
        elif st.get("pairable"):
            state, detail = "pairing", _fmt_interval(adv.get("interval_ms"))
        elif st.get("advertising", adv.get("active", False)):
            state, detail = "advertising", _fmt_interval(adv.get("interval_ms"))
        else:
            state, detail = "down", "not advertising"
        if powered and central.get("enabled"):
            detail = "%s, central" % detail if detail else "central"
        return TransportStatus(self.name, available=bool(powered), state=state, detail=detail,
                               last_discovery=last, devices_seen=seen)

    # ------------------------------------------------------------------ internals
    def _command(self, obj: dict) -> bool:
        with self._lock:
            if not self._connected:
                return False
        self._out.put((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))
        self._wake()
        return True

    def _wake(self) -> None:
        if self._wake_w >= 0:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass

    def _emit(self, ev: LinkEvent) -> None:
        ev.ts = ev.ts or time.time()
        self._events.put(ev)

    def _run(self) -> None:
        backoff = self.backoff_min_s
        unreachable_logged = False
        while not self._stop.is_set():
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.socket_path)
            except OSError as exc:
                sock.close()
                if not unreachable_logged:
                    logger.warning("BLE: bled unreachable at %s (%s); retrying", self.socket_path, exc)
                    unreachable_logged = True
                self._sleep(backoff)
                backoff = min(self.backoff_max_s, backoff * 2)
                continue
            backoff = self.backoff_min_s
            unreachable_logged = False
            logger.info("BLE: connected to bled at %s", self.socket_path)
            try:
                self._session(sock)
            finally:
                sock.close()
                self._on_disconnected()
            if not self._stop.is_set():
                logger.warning("BLE: lost bled connection; reconnecting")
                self._sleep(self.backoff_min_s)

    def _sleep(self, seconds: float) -> None:
        if self._wake_r < 0:
            self._stop.wait(seconds)
            return
        try:
            select.select([self._wake_r], [], [], seconds)
            self._drain_wake()
        except (OSError, ValueError):
            self._stop.wait(seconds)

    def _drain_wake(self) -> None:
        try:
            while os.read(self._wake_r, 4096):
                pass
        except (BlockingIOError, OSError):
            pass

    def _session(self, sock: socket.socket) -> None:
        # Drop commands queued for a previous bled instance: they referred to its state.
        while True:
            try:
                self._out.get_nowait()
            except queue.Empty:
                break
        with self._lock:
            self._connected = True
        buf = bytearray()
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([sock, self._wake_r], [], [], 1.0)
            except (OSError, ValueError):
                return
            if self._wake_r in r:
                self._drain_wake()
            try:
                while True:
                    data = self._out.get_nowait()
                    sock.sendall(data)
            except queue.Empty:
                pass
            except OSError:
                return
            if sock in r:
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[:nl])
                    del buf[: nl + 1]
                    self._handle_line(line)
                if len(buf) > READ_MAX:
                    logger.warning("BLE: oversize line from bled; reconnecting")
                    return

    def _handle_line(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            logger.warning("BLE: bad line from bled: %r", raw[:120])
            return
        if not isinstance(msg, dict):
            return
        if "ack" in msg:
            if not msg.get("ok", False):
                logger.warning("BLE: bled %s failed: %s", msg.get("ack"), msg.get("error"))
                if msg.get("ack") == "scan":
                    with self._lock:
                        self._scan_pending = False
            return
        ev = msg.get("ev")
        if ev == "hello":
            with self._lock:
                self._hello = msg
                central = self._central
            logger.info("BLE: bled hello adapter=%s address=%s", msg.get("adapter"), msg.get("address"))
            if central is not None:
                self._send_central(central)
        elif ev == "status":
            self._on_status(msg)
        elif ev == "link":
            self._on_link(msg)
        elif ev == "data":
            addr = str(msg.get("addr", "")).upper()
            line = msg.get("line")
            if addr and isinstance(line, str):
                with self._lock:
                    dev = self._links.get(addr)
                    if dev is not None:
                        dev.last_seen = time.time()
                self._emit(LinkEvent("data", self.name, address=addr, line=line))
        elif ev == "scan":
            self._on_scan(msg)

    def _on_link(self, msg: dict) -> None:
        addr = str(msg.get("addr", "")).upper()
        if not addr:
            return
        extra = {k: msg[k] for k in ("bonded", "mtu") if k in msg}
        name = str(msg.get("name") or "")
        if msg.get("state") == "up":
            self._link_up(addr, name, extra)
        elif msg.get("state") == "down":
            self._link_down(addr)

    def _link_up(self, addr: str, name: str, extra: dict) -> None:
        with self._lock:
            known = addr in self._links
            dev = self._links.get(addr) or DiscoveredDevice(self.name, addr)
            dev.name = name or dev.name
            dev.linked = True
            dev.last_seen = time.time()
            dev.extra.update(extra)
            self._links[addr] = dev
        if not known:
            self._emit(LinkEvent("up", self.name, address=addr, name=dev.name, extra=dict(extra)))

    def _link_down(self, addr: str) -> None:
        with self._lock:
            dev = self._links.pop(addr, None)
        if dev is not None:
            self._emit(LinkEvent("down", self.name, address=addr, name=dev.name))

    def _on_status(self, msg: dict) -> None:
        with self._lock:
            self._bled_status = msg
            current = set(self._links)
        links = [l for l in msg.get("links", []) if isinstance(l, dict) and l.get("addr")]
        reported = {str(l["addr"]).upper() for l in links}
        # Reconcile: after a bled restart/reconnect the status is the truth.
        for addr in current - reported:
            self._link_down(addr)
        for l in links:
            self._link_up(str(l["addr"]).upper(), str(l.get("name") or ""),
                          {k: l[k] for k in ("bonded", "mtu") if k in l})

    def _on_scan(self, msg: dict) -> None:
        now = time.time()
        fresh: List[DiscoveredDevice] = []
        with self._lock:
            for d in msg.get("devices", []):
                if not isinstance(d, dict) or not d.get("addr"):
                    continue
                addr = str(d["addr"]).upper()
                dev = DiscoveredDevice(self.name, addr, name=str(d.get("name") or ""),
                                       rssi=d.get("rssi"), last_seen=now,
                                       linked=addr in self._links,
                                       extra={"uuids": list(d.get("uuids") or [])})
                self._scan[addr] = dev
                fresh.append(dev)
            self._last_discovery = now
            self._scan_pending = False
            for addr in [a for a, d in self._scan.items() if now - d.last_seen > self.discovery_ttl_s]:
                del self._scan[addr]
        self._emit(LinkEvent("discovery", self.name, devices=tuple(fresh), ts=now))

    def _on_disconnected(self) -> None:
        with self._lock:
            self._connected = False
            self._hello = {}
            self._bled_status = {}
            self._scan_pending = False
            addrs = list(self._links)
        for addr in addrs:
            self._link_down(addr)
