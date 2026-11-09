"""
Wi-Fi satellite transport — satellites that join the car's ``prius`` AP.

See ``docs/SATELLITES.md`` §4 (Wi-Fi). Pure stdlib sockets, one IO thread:

* **UDP 8787** (discovery). Satellites broadcast
  ``{"prius_sat":1,"fp":…,"name":…,"port":8788}`` every 10 s; each beacon
  refreshes a :class:`DiscoveredDevice` with address ``"<ip>:<port>"``. A
  ``{"prius_car":1}`` probe (no ``car_fp`` key) is answered unicast with
  ``{"prius_car":1,"car_fp":…,"port":8788}``. :meth:`discover` broadcasts the
  same announcement so listening satellites can beacon/connect right away.
* **TCP 8788** (sessions), NDJSON lines both ways. The car may connect to a
  satellite's port (:meth:`connect`, driven by the registry's policy) or
  accept a satellite connecting to the car. A session whose peer stayed
  silent for ``dead_s`` (45 s) is closed; the registry sends the 15 s
  ``ping`` keep-alives because only it knows when a link is authenticated
  (nothing but ``auth`` may be sent before ``auth_ok``).

Why a thread: sockets must never block the 50 Hz engine loop, and the
transport must not touch the store. The IO thread hands :class:`LinkEvent`\\ s
to :meth:`poll` through a thread-safe queue; :meth:`send` / :meth:`connect` /
:meth:`disconnect` post commands back and wake the selector via a socketpair.

AP awareness: with ``iface`` set (``wlan0`` on the phone) the transport is
``available`` only while that interface carries ``ap_addr`` (10.42.0.1, NM
shared mode); otherwise (wlan0 is a home-Wi-Fi client, or down) it reports
``ap-down``, ignores beacons and refuses sessions. With ``iface`` empty (dev,
P52) it is available whenever its sockets are bound. Listening on a specific
unicast address does not receive broadcasts on Linux, so the default bind is
``0.0.0.0`` and ``iface`` (SO_BINDTODEVICE, best effort) scopes it.
"""

from __future__ import annotations

import errno
import json
import logging
import queue
import selectors
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .satellite_transport import (
    DiscoveredDevice,
    LinkEvent,
    SatelliteTransport,
    TransportStatus,
    TRANSPORT_WIFI,
)

logger = logging.getLogger(__name__)

DEFAULT_UDP_PORT = 8787
DEFAULT_TCP_PORT = 8788
SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)
SIOCGIFADDR = 0x8915


def iface_ipv4(iface: str) -> Optional[str]:
    """IPv4 address of ``iface`` (Linux ioctl), or None."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        return None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = struct.pack("256s", iface.encode()[:15])
        res = fcntl.ioctl(s.fileno(), SIOCGIFADDR, packed)
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return None
    finally:
        s.close()


def parse_address(address: str) -> Tuple[str, int]:
    host, _, port = address.rpartition(":")
    return host, int(port)


@dataclass
class _Session:
    sock: socket.socket
    address: str
    outbound: bool
    connecting: bool = False
    started: float = 0.0
    last_rx: float = 0.0
    rx: bytearray = field(default_factory=bytearray)
    tx: bytearray = field(default_factory=bytearray)
    announced: bool = False      # "up" event emitted


class WifiTransport(SatelliteTransport):
    """SatelliteTransport over UDP beacons + TCP NDJSON sessions."""

    name = TRANSPORT_WIFI
    keepalive_s = 15.0           # registry pings authenticated links this often

    def __init__(
        self,
        bind: str = "0.0.0.0",
        udp_port: int = DEFAULT_UDP_PORT,
        tcp_port: int = DEFAULT_TCP_PORT,
        iface: str = "",
        ap_addr: str = "10.42.0.1",
        broadcast_addr: str = "255.255.255.255",
        dead_s: float = 45.0,
        discovery_ttl_s: float = 600.0,
        connect_timeout_s: float = 5.0,
        max_line: int = 16384,
        iface_check_s: float = 2.0,
    ):
        self.bind = bind
        self.udp_port = udp_port
        self.tcp_port = tcp_port
        self.iface = iface or ""
        self.ap_addr = ap_addr or ""
        self.broadcast_addr = broadcast_addr
        self.dead_s = dead_s
        self.discovery_ttl_s = discovery_ttl_s
        self.connect_timeout_s = connect_timeout_s
        self.max_line = max_line
        self.iface_check_s = iface_check_s
        self.car_fp: Optional[str] = None

        self._events: "queue.Queue[LinkEvent]" = queue.Queue()
        self._cmds: "queue.Queue[tuple]" = queue.Queue()
        self._lock = threading.Lock()
        self._devices: Dict[str, DiscoveredDevice] = {}
        self._linked: Dict[str, bool] = {}        # address -> session established
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._sel: Optional[selectors.BaseSelector] = None
        self._udp: Optional[socket.socket] = None
        self._tcp: Optional[socket.socket] = None
        self._wake_r: Optional[socket.socket] = None
        self._wake_w: Optional[socket.socket] = None
        self._sessions: Dict[int, _Session] = {}   # IO thread only
        self._state = "init"
        self._detail = ""
        self._ap_up = not self.iface
        self._last_discovery = 0.0
        self._discovery_due: Optional[float] = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def set_car_fp(self, car_fp: Optional[str]) -> None:
        self.car_fp = car_fp

    def start(self) -> bool:
        if self._thread is not None:
            return True
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._bind_device(udp)
            udp.bind((self.bind, self.udp_port))
            udp.setblocking(False)
            tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._bind_device(tcp)
            tcp.bind((self.bind, self.tcp_port))
            tcp.listen(8)
            tcp.setblocking(False)
        except OSError as exc:
            self._state, self._detail = "error", f"bind failed: {exc}"
            logger.error("Wi-Fi satellite transport: %s", self._detail)
            return False
        self._udp, self._tcp = udp, tcp
        # Report the real ports (tests bind port 0).
        self.udp_port = udp.getsockname()[1]
        self.tcp_port = tcp.getsockname()[1]
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        sel = selectors.DefaultSelector()
        sel.register(udp, selectors.EVENT_READ, "udp")
        sel.register(tcp, selectors.EVENT_READ, "listen")
        sel.register(self._wake_r, selectors.EVENT_READ, "wake")
        self._sel = sel
        self._stop.clear()
        self._refresh_ap(force=True)
        self._thread = threading.Thread(target=self._run, name="sat-wifi", daemon=True)
        self._thread.start()
        logger.info("Wi-Fi satellite transport listening on %s udp/%d tcp/%d%s",
                    self.bind, self.udp_port, self.tcp_port,
                    f" (iface {self.iface}, AP {self.ap_addr})" if self.iface else "")
        return True

    def _bind_device(self, sock: socket.socket) -> None:
        if not self.iface:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, self.iface.encode())
        except OSError as exc:  # needs CAP_NET_RAW; harmless without
            logger.debug("SO_BINDTODEVICE %s failed: %s", self.iface, exc)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._wake()
        self._thread.join(timeout=2.0)
        self._thread = None
        for sock in (self._udp, self._tcp, self._wake_r, self._wake_w):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._udp = self._tcp = self._wake_r = self._wake_w = None
        if self._sel is not None:
            self._sel.close()
            self._sel = None
        self._state = "down"

    def tick(self, now: float) -> None:
        return None  # all housekeeping runs in the IO thread

    def poll(self) -> Optional[LinkEvent]:
        try:
            return self._events.get_nowait()
        except queue.Empty:
            return None

    # ── engine-side API ─────────────────────────────────────────────────────

    def _wake(self) -> None:
        if self._wake_w is not None:
            try:
                self._wake_w.send(b"\0")
            except OSError:
                pass

    def send(self, address: str, line: str) -> bool:
        with self._lock:
            if not self._linked.get(address):
                return False
        data = line if line.endswith("\n") else line + "\n"
        self._cmds.put(("send", address, data.encode("utf-8")))
        self._wake()
        return True

    def connect(self, address: str) -> bool:
        """Open a session to a satellite's ``ip:port`` (registry policy decides when)."""
        if not self._ap_up or self._thread is None:
            return False
        with self._lock:
            if address in self._linked:
                return True
        self._cmds.put(("connect", address))
        self._wake()
        return True

    def disconnect(self, address: str) -> None:
        self._cmds.put(("disconnect", address))
        self._wake()

    def is_linked(self, address: str) -> bool:
        with self._lock:
            return bool(self._linked.get(address))

    def discover(self, force: bool = False) -> None:
        self._cmds.put(("announce",))
        self._wake()

    def discovered(self) -> List[DiscoveredDevice]:
        now = time.time()
        with self._lock:
            out = []
            for addr, dev in list(self._devices.items()):
                linked = bool(self._linked.get(addr))
                if not linked and (now - dev.last_seen) > self.discovery_ttl_s:
                    del self._devices[addr]
                    continue
                out.append(DiscoveredDevice(
                    transport=self.name, address=addr, name=dev.name, rssi=None,
                    last_seen=dev.last_seen, linked=linked, fp=dev.fp,
                    extra=dict(dev.extra)))
        out.sort(key=lambda d: (not d.linked, -d.last_seen))
        return out

    def status(self) -> TransportStatus:
        if self._state in ("error", "down", "init"):
            available, state = False, self._state
        elif not self._ap_up:
            available, state = False, "ap-down"
        else:
            available, state = True, "listening"
        with self._lock:
            n = sum(1 for v in self._linked.values() if v)
        detail = self._detail or (f"{n} session(s)" if available else "")
        return TransportStatus(
            transport=self.name, available=available, state=state, detail=detail,
            last_discovery=self._last_discovery, devices_seen=len(self.discovered()))

    # ── IO thread ───────────────────────────────────────────────────────────

    def _emit(self, kind: str, address: str = "", **kw) -> None:
        self._events.put(LinkEvent(kind=kind, transport=self.name, address=address,
                                   ts=time.time(), **kw))

    def _refresh_ap(self, force: bool = False) -> None:
        if not self.iface:
            self._ap_up = True
        else:
            addr = iface_ipv4(self.iface)
            up = addr is not None and (not self.ap_addr or addr == self.ap_addr)
            if up != self._ap_up or force:
                logger.info("Wi-Fi satellite transport: %s %s (%s)", self.iface,
                            "AP up" if up else "AP down", addr or "no IPv4")
            self._ap_up = up
            if not up:
                for sess in list(self._sessions.values()):
                    self._close(sess, "ap down")
        if self._state in ("init",) or self._state == "listening":
            self._state = "listening"

    def _run(self) -> None:
        next_ap_check = time.monotonic() + self.iface_check_s
        while not self._stop.is_set():
            try:
                events = self._sel.select(timeout=0.5)
            except (OSError, ValueError):
                break
            for key, mask in events:
                tag = key.data
                try:
                    if tag == "wake":
                        try:
                            while self._wake_r.recv(256):
                                pass
                        except (BlockingIOError, InterruptedError):
                            pass
                    elif tag == "udp":
                        self._on_udp()
                    elif tag == "listen":
                        self._on_accept()
                    elif isinstance(tag, _Session):
                        self._on_session(tag, mask)
                except Exception:
                    logger.exception("Wi-Fi transport IO error")
            self._drain_cmds()
            now = time.monotonic()
            if now >= next_ap_check:
                next_ap_check = now + self.iface_check_s
                self._refresh_ap()
            self._housekeeping(now)
        for sess in list(self._sessions.values()):
            self._close(sess, "stopped")

    def _housekeeping(self, now: float) -> None:
        for sess in list(self._sessions.values()):
            if sess.connecting:
                if now - sess.started > self.connect_timeout_s:
                    self._close(sess, "connect timeout")
            elif now - sess.last_rx > self.dead_s:
                self._close(sess, "dead (no traffic)")
        if self._discovery_due is not None and now >= self._discovery_due:
            self._discovery_due = None
            self._last_discovery = time.time()
            self._emit("discovery", devices=tuple(self.discovered()))

    def _drain_cmds(self) -> None:
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                return
            op = cmd[0]
            if op == "send":
                sess = self._find(cmd[1])
                if sess is not None and not sess.connecting:
                    sess.tx.extend(cmd[2])
                    self._update_interest(sess)
            elif op == "connect":
                self._do_connect(cmd[1])
            elif op == "disconnect":
                sess = self._find(cmd[1])
                if sess is not None:
                    self._close(sess, "disconnect requested")
            elif op == "announce":
                self._announce()

    def _find(self, address: str) -> Optional[_Session]:
        for sess in self._sessions.values():
            if sess.address == address:
                return sess
        return None

    def _announce(self) -> None:
        self._discovery_due = time.monotonic() + 2.0
        if not self._ap_up or self._udp is None:
            return
        msg = json.dumps({"prius_car": 1, "car_fp": self.car_fp, "port": self.tcp_port}).encode()
        try:
            self._udp.sendto(msg + b"\n", (self.broadcast_addr, self.udp_port))
        except OSError as exc:
            logger.debug("Wi-Fi announce failed: %s", exc)

    def _on_udp(self) -> None:
        while True:
            try:
                data, (ip, port) = self._udp.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if not self._ap_up:
                continue
            try:
                msg = json.loads(data.decode("utf-8").strip() or "{}")
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("prius_sat") == 1:
                try:
                    sport = int(msg.get("port") or DEFAULT_TCP_PORT)
                except (TypeError, ValueError):
                    continue
                fp = msg.get("fp")
                address = f"{ip}:{sport}"
                with self._lock:
                    dev = self._devices.get(address)
                    if dev is None:
                        dev = DiscoveredDevice(transport=self.name, address=address)
                        self._devices[address] = dev
                        logger.info("Wi-Fi satellite beacon %s (%s, fp=%s)",
                                    address, msg.get("name"), fp)
                    dev.name = str(msg.get("name") or "")[:32]
                    dev.fp = str(fp) if isinstance(fp, str) else None
                    dev.last_seen = time.time()
                    dev.extra = {"beacon": True}
            elif msg.get("prius_car") == 1 and "car_fp" not in msg:
                reply = json.dumps({"prius_car": 1, "car_fp": self.car_fp,
                                    "port": self.tcp_port}).encode()
                try:
                    self._udp.sendto(reply + b"\n", (ip, port))
                except OSError as exc:
                    logger.debug("Wi-Fi probe reply to %s failed: %s", ip, exc)

    def _register(self, sess: _Session, events: int) -> None:
        self._sel.register(sess.sock, events, sess)
        self._sessions[sess.sock.fileno()] = sess

    def _update_interest(self, sess: _Session) -> None:
        ev = selectors.EVENT_READ
        if sess.tx or sess.connecting:
            ev |= selectors.EVENT_WRITE
        try:
            self._sel.modify(sess.sock, ev, sess)
        except (KeyError, ValueError, OSError):
            pass

    def _mark_up(self, sess: _Session) -> None:
        sess.announced = True
        sess.last_rx = time.monotonic()
        with self._lock:
            self._linked[sess.address] = True
            dev = self._devices.get(sess.address)
            if dev is None:
                dev = DiscoveredDevice(transport=self.name, address=sess.address)
                self._devices[sess.address] = dev
            dev.last_seen = time.time()
        self._emit("up", sess.address, extra={"outbound": sess.outbound})

    def _on_accept(self) -> None:
        while True:
            try:
                conn, (ip, port) = self._tcp.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if not self._ap_up:
                conn.close()
                continue
            conn.setblocking(False)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sess = _Session(conn, f"{ip}:{port}", outbound=False,
                            started=time.monotonic())
            self._register(sess, selectors.EVENT_READ)
            logger.info("Wi-Fi satellite session from %s", sess.address)
            self._mark_up(sess)

    def _do_connect(self, address: str) -> None:
        if self._find(address) is not None or not self._ap_up:
            return
        try:
            host, port = parse_address(address)
        except ValueError:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        self._bind_device(sock)
        err = sock.connect_ex((host, port))
        if err not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK):
            sock.close()
            self._emit("down", address, extra={"why": f"connect: {errno.errorcode.get(err, err)}"})
            return
        sess = _Session(sock, address, outbound=True, connecting=True,
                        started=time.monotonic())
        with self._lock:
            self._linked[address] = False
        self._register(sess, selectors.EVENT_READ | selectors.EVENT_WRITE)

    def _on_session(self, sess: _Session, mask: int) -> None:
        if sess.connecting and mask & selectors.EVENT_WRITE:
            err = sess.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err:
                self._close(sess, f"connect: {errno.errorcode.get(err, err)}")
                return
            sess.connecting = False
            sess.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            logger.info("Wi-Fi satellite session to %s", sess.address)
            self._mark_up(sess)
            self._update_interest(sess)
        if mask & selectors.EVENT_READ and not sess.connecting:
            try:
                chunk = sess.sock.recv(4096)
            except (BlockingIOError, InterruptedError):
                chunk = None
            except OSError as exc:
                self._close(sess, f"recv: {exc}")
                return
            if chunk is not None:
                if not chunk:
                    self._close(sess, "peer closed")
                    return
                sess.last_rx = time.monotonic()
                sess.rx.extend(chunk)
                while True:
                    idx = sess.rx.find(b"\n")
                    if idx < 0:
                        break
                    raw = bytes(sess.rx[:idx]).strip()
                    del sess.rx[:idx + 1]
                    if not raw:
                        continue
                    try:
                        line = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    self._emit("data", sess.address, line=line)
                if len(sess.rx) > self.max_line:
                    self._close(sess, "line too long")
                    return
        if mask & selectors.EVENT_WRITE and sess.tx and not sess.connecting:
            try:
                n = sess.sock.send(sess.tx)
                del sess.tx[:n]
            except (BlockingIOError, InterruptedError):
                pass
            except OSError as exc:
                self._close(sess, f"send: {exc}")
                return
            self._update_interest(sess)

    def _close(self, sess: _Session, why: str) -> None:
        fd = None
        for k, v in list(self._sessions.items()):
            if v is sess:
                fd = k
        if fd is not None:
            del self._sessions[fd]
        try:
            self._sel.unregister(sess.sock)
        except (KeyError, ValueError, OSError):
            pass
        # Flush what is queued (e.g. auth_fail) best-effort before closing.
        if sess.tx and not sess.connecting:
            try:
                sess.sock.setblocking(True)
                sess.sock.settimeout(0.2)
                sess.sock.sendall(bytes(sess.tx))
            except OSError:
                pass
        try:
            sess.sock.close()
        except OSError:
            pass
        with self._lock:
            self._linked.pop(sess.address, None)
        logger.info("Wi-Fi satellite session %s closed: %s", sess.address, why)
        self._emit("down", sess.address, extra={"why": why, "was_up": sess.announced})
