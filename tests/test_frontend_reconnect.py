"""MFD frontend link self-healing (frontend/client.py BackendClient).

Incident 2026-11-17: the phone (backend) lost power and rebooted; the Pi kept
its SUB connection to the dead phone ESTABLISHED (no FIN, no RST: the peer
just vanished) and never reconnected; ``systemctl restart mfd`` fixed it at
once. A SUB socket never sends after its subscription, so without heartbeats
nothing ever probes the dead connection.

The cases below run a fake backend in a separate process (so it can be
SIGSTOPped / SIGKILLed) and, for the "peer vanished" case, a TCP proxy in front
of it that can black-hole its connections without closing them, which is what
a power loss of the far end looks like from the Pi.
"""
from __future__ import annotations

import os
import select
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest

zmq = pytest.importorskip("zmq")

from cyberpunk_computer.frontend import client as client_mod  # noqa: E402
from cyberpunk_computer.frontend.client import BackendClient  # noqa: E402

SERVER = textwrap.dedent(r"""
    import json, sys, time, zmq
    pub_port, rep_port, tag = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB); pub.bind("tcp://127.0.0.1:%d" % pub_port)
    rep = ctx.socket(zmq.REP); rep.bind("tcp://127.0.0.1:%d" % rep_port)
    print("ready", flush=True)
    n = 0
    while True:
        if rep.poll(100):
            rep.recv(); rep.send_string(json.dumps({"status": "ok"}))
        n += 1
        pub.send_string(json.dumps({"type": "state", "state": {"tag": tag, "n": n}}))
""")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(pub_port: int, rep_port: int, tag: str) -> subprocess.Popen:
    deadline = time.monotonic() + 10
    while True:   # the port of a just-killed server may need a moment
        p = subprocess.Popen([sys.executable, "-c", SERVER, str(pub_port), str(rep_port), tag],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        line = p.stdout.readline()
        if line.strip() == "ready":
            return p
        p.kill()
        p.wait()
        if time.monotonic() > deadline:
            raise RuntimeError("fake backend did not start")
        time.sleep(0.2)


class BlackholeProxy:
    """TCP forwarder 127.0.0.1:listen -> 127.0.0.1:target.

    ``power_loss()``: every existing connection stops forwarding in both
    directions but stays open (no FIN, no RST), and new connections are
    black-holed too. ``power_back()``: new connections forward again; the old
    ones stay dead forever, like TCP state that died with the far end.
    """

    def __init__(self, listen_port: int, target_port: int) -> None:
        self.target_port = target_port
        self._lsock = socket.socket()
        self._lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._lsock.bind(("127.0.0.1", listen_port))
        self._lsock.listen(16)
        self._epoch = 0
        self._down = False
        self._stop = False
        self._held = []
        threading.Thread(target=self._accept, daemon=True).start()

    def power_loss(self) -> None:
        self._down = True

    def power_back(self) -> None:
        self._epoch += 1
        self._down = False

    def close(self) -> None:
        self._stop = True
        self._lsock.close()
        for s in self._held:
            try:
                s.close()
            except OSError:
                pass

    def _alive(self, epoch: int) -> bool:
        return not self._down and epoch == self._epoch and not self._stop

    def _accept(self) -> None:
        while not self._stop:
            try:
                c, _ = self._lsock.accept()
            except OSError:
                return
            self._held.append(c)
            epoch = self._epoch
            if self._down:
                continue          # SYN answered by "nobody": held open, silent
            try:
                u = socket.create_connection(("127.0.0.1", self.target_port), timeout=2)
            except OSError:
                c.close()
                continue
            self._held.append(u)
            threading.Thread(target=self._pump, args=(c, u, epoch), daemon=True).start()

    def _pump(self, a, b, epoch) -> None:
        while not self._stop:
            r, _, _ = select.select([a, b], [], [], 0.2)
            if not self._alive(epoch):
                return            # silent from now on, sockets left open
            for s in r:
                try:
                    data = s.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    for x in (a, b):
                        try:
                            x.close()
                        except OSError:
                            pass
                    return
                (b if s is a else a).sendall(data)


class _Rx:
    def __init__(self):
        self.last = None
        self.lock = threading.Lock()

    def __call__(self, state):
        with self.lock:
            self.last = (time.monotonic(), dict(state))

    def wait_tag(self, tag: str, timeout: float) -> float:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            with self.lock:
                last = self.last
            if last and last[1].get("tag") == tag and last[0] > t0 - 0.5:
                return time.monotonic() - t0
            time.sleep(0.05)
        raise AssertionError("no state with tag %r within %.0fs" % (tag, timeout))


# Designed recovery: heartbeat timeout (6 s) + reconnect (<=2 s); the
# envelope watchdog (10 s) is the backstop. Budget with margin:
RECOVERY_S = 15.0


@pytest.fixture
def procs():
    started = []
    yield started
    for p in started:
        try:
            os.kill(p.pid, signal.SIGCONT)
        except OSError:
            pass
        p.kill()
        p.wait()


@pytest.mark.parametrize("layer", ["both", "transport_only", "watchdog_only"])
def test_reconnects_after_peer_vanished_without_fin(procs, monkeypatch, layer):
    """Phone power loss: the connection is neither closed nor reset. Each
    self-healing layer must recover on its own: the transport heartbeat with
    the watchdog disabled, and the watchdog with heartbeats/keepalive off
    (the pre-fix transport, which stayed stuck forever)."""
    kw = {}
    if layer == "transport_only":
        kw = {"rebuild_after": 1e9}
    elif layer == "watchdog_only":
        monkeypatch.setattr(client_mod, "apply_liveness_options", lambda sock: None)
    pub_back, rep_back = _free_port(), _free_port()
    pub_front, rep_front = _free_port(), _free_port()
    procs.append(_start_server(pub_back, rep_back, "A"))
    proxy_pub = BlackholeProxy(pub_front, pub_back)
    proxy_rep = BlackholeProxy(rep_front, rep_back)
    rx = _Rx()
    c = BackendClient("127.0.0.1", pub_front, rep_front, on_state=rx, **kw)
    try:
        c.start()
        rx.wait_tag("A", 10)
        assert c.send_command("noop")
        proxy_pub.power_loss()
        proxy_rep.power_loss()
        time.sleep(3)
        # the "phone" reboots: a new backend process, same endpoint
        procs[0].kill()
        procs[0].wait()
        procs.append(_start_server(pub_back, rep_back, "B"))
        proxy_pub.power_back()
        proxy_rep.power_back()
        took = rx.wait_tag("B", RECOVERY_S)
        print("recovered after %.1fs (%s)" % (took, layer))
        assert took < RECOVERY_S
        if layer == "transport_only":
            assert c.counters["rebuilds"] == 0
        # the command channel recovers as well (at most one lost command)
        oks = [c.send_command("noop") for _ in range(3)]
        assert oks[-1]
    finally:
        c.stop()
        proxy_pub.close()
        proxy_rep.close()


def test_reconnects_after_server_frozen_then_replaced(procs):
    """Old backend SIGSTOPped (its kernel keeps the TCP state alive but nothing
    answers), later SIGKILLed, and a new process binds the same endpoint."""
    pub, rep = _free_port(), _free_port()
    procs.append(_start_server(pub, rep, "A"))
    rx = _Rx()
    c = BackendClient("127.0.0.1", pub, rep, on_state=rx)
    try:
        c.start()
        rx.wait_tag("A", 10)
        os.kill(procs[0].pid, signal.SIGSTOP)
        time.sleep(8)
        assert not c.connected
        os.kill(procs[0].pid, signal.SIGKILL)
        procs[0].wait()
        procs.append(_start_server(pub, rep, "B"))
        assert rx.wait_tag("B", RECOVERY_S) < RECOVERY_S
        assert c.connected
    finally:
        c.stop()


def test_reconnects_after_clean_restart(procs):
    pub, rep = _free_port(), _free_port()
    procs.append(_start_server(pub, rep, "A"))
    rx = _Rx()
    c = BackendClient("127.0.0.1", pub, rep, on_state=rx)
    try:
        c.start()
        rx.wait_tag("A", 10)
        procs[0].terminate()
        procs[0].wait()
        time.sleep(2)
        procs.append(_start_server(pub, rep, "B"))
        assert rx.wait_tag("B", RECOVERY_S) < 5.0
    finally:
        c.stop()


def test_watchdog_rebuilds_sockets_with_backoff(monkeypatch):
    """No envelope at all: the client rebuilds its sockets after
    ``rebuild_after`` and then backs off, forever, without blocking stop()."""
    pub, rep = _free_port(), _free_port()     # nothing listens
    c = BackendClient("127.0.0.1", pub, rep, rebuild_after=0.6, rebuild_max=2.0)
    try:
        c.start()
        time.sleep(4.5)
        n = c.counters["rebuilds"]
        # 0.6, then 1.2, then 2.0 (cap), 2.0 ... -> 2..4 rebuilds in 4.5 s
        assert 2 <= n <= 4
    finally:
        t0 = time.monotonic()
        c.stop()
        assert time.monotonic() - t0 < 3.0


def test_heartbeat_envelope_keeps_link_up():
    """A heartbeat envelope counts as proof of life but is not a state."""
    got = []
    c = BackendClient("127.0.0.1", 1, 2, on_state=got.append)
    c._handle_raw('{"type": "hb", "ts": 1}')
    assert got == [] and c.counters["decode_errors"] == 0


def test_idle_backend_server_keeps_the_client_linked():
    """Real ZmqServer with no state/events: its heartbeat envelope keeps the
    client connected and its watchdog quiet; commands still go through."""
    from cyberpunk_computer.backend.zmq_server import ZmqServer

    class _Bridge:
        def __init__(self):
            self.cmds = []

        def submit_command(self, name, params):
            self.cmds.append(name)

    pub, rep = _free_port(), _free_port()
    bridge = _Bridge()
    srv = ZmqServer(bridge, host="127.0.0.1", pub_port=pub, rep_port=rep)
    srv.start()
    c = BackendClient("127.0.0.1", pub, rep, rebuild_after=3.0)
    try:
        c.start()
        time.sleep(5.0)
        assert c.connected and c.counters["hb_rx"] >= 3
        assert c.counters["rebuilds"] == 0
        assert c.link_age_s() < 2.0
        assert c.send_command("noop") and bridge.cmds == ["noop"]
        assert srv.stats["hb_pub"] >= 3
    finally:
        c.stop()
        srv.stop()
