"""
BackendClient — frontend-side link to the headless backend over ZeroMQ.

* SUB to the backend's PUB (state + event envelopes).
* REQ/REP for commands (with timeout + socket recreation on failure — a REQ
  socket is a lockstep state machine and must be rebuilt after a lost reply).

Liveness: ``connected`` is TRUE only while envelopes have actually been
received within ``stale_after`` seconds — the backend publishes state at
~1 Hz (the POCO telemetry tick dispatches every second) plus a heartbeat
envelope when idle, so silence means the link (or backend) is really gone.
Transitions are logged at INFO; per-frame logging stays at DEBUG with a
periodic INFO summary so a headless deployment can prove the link works from
logs alone.

Self-healing (incident 2026-11-17: the phone lost power and rebooted; the
Pi's SUB connection stayed ESTABLISHED to the dead peer — no FIN, no RST — and
a SUB socket never sends after subscribing, so nothing probed it; the UI sat
on the last picture until ``systemctl restart mfd``):

1. Transport: ZMTP heartbeats (PING every ``HEARTBEAT_IVL``, peer dropped
   after ``HEARTBEAT_TIMEOUT``) and TCP keepalive on both sockets, bounded
   connect/handshake times and a fast reconnect, so a vanished peer is
   detected within ~6 s and libzmq reconnects by itself. The REQ socket is
   relaxed+correlated and still rebuilt after a timeout.
2. Watchdog: no envelope for ``rebuild_after`` seconds -> close both sockets,
   terminate the context and build everything new; repeated with backoff up
   to ``rebuild_max`` for as long as the silence lasts. Edges are logged.
3. ``link_age_s`` lets the UI show the outage instead of a frozen picture.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Dict, Optional

import zmq

# Shared with backend/zmq_server (a dependency-free module: importing the
# frontend package from the backend would pull in pygame/RemoteTwin).
from ..zmq_liveness import apply_liveness_options

logger = logging.getLogger(__name__)

StateCallback = Callable[[dict], None]
EventCallback = Callable[[str, dict], None]


class BackendClient:
    def __init__(
        self,
        host: str,
        pub_port: int = 8081,
        rep_port: int = 8082,
        token: Optional[str] = None,      # reserved; ZMQ transport is LAN-only
        on_state: Optional[StateCallback] = None,
        on_event: Optional[EventCallback] = None,
        poll_interval: float = 1.0,
        stale_after: float = 5.0,
        summary_interval: float = 30.0,
        rebuild_after: float = 10.0,
        rebuild_max: float = 60.0,
    ) -> None:
        self._host = host
        self._pub_port = pub_port
        self._rep_port = rep_port
        self._on_state = on_state
        self._on_event = on_event
        self._stale_after = stale_after
        self._summary_interval = summary_interval
        # Watchdog: first rebuild after rebuild_after s of silence, then the
        # interval doubles up to rebuild_max. State arrives at ~1 Hz, so 10 s
        # is ten missed frames; the transport heartbeat (6 s) normally heals
        # the link before the watchdog fires.
        self._rebuild_after = rebuild_after
        self._rebuild_max = max(rebuild_max, rebuild_after)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Private context: a rebuild terminates it and starts a fresh one.
        self._ctx = zmq.Context()
        self._req_lock = threading.Lock()
        self._req_sock = None
        self._last_rx = 0.0
        self._started_at = 0.0
        self._was_connected = False
        # Proof-of-life counters (read via stats()).
        self.counters = {"state_rx": 0, "event_rx": 0, "hb_rx": 0, "cmd_ok": 0,
                        "cmd_fail": 0, "decode_errors": 0, "rebuilds": 0}

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._started_at = time.monotonic()
        with self._req_lock:
            self._make_req_socket()
        self._thread = threading.Thread(target=self._run, name="backend-client-zmq", daemon=True)
        self._thread.start()
        logger.info("ZMQ BackendClient started: SUB tcp://%s:%d, REQ tcp://%s:%d",
                    self._host, self._pub_port, self._host, self._rep_port)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread_done = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            thread_done = not self._thread.is_alive()
            self._thread = None
        with self._req_lock:
            if self._req_sock:
                self._req_sock.close(linger=0)
                self._req_sock = None
            # term() blocks while any socket is open: skip it if the reader
            # thread (owner of the SUB socket) did not finish
            if thread_done:
                try:
                    self._ctx.term()
                except Exception:
                    pass
            self._ctx = zmq.Context()   # a later start() gets a live context
        logger.info("ZMQ BackendClient stopped (counters: %s)", self.counters)

    @property
    def connected(self) -> bool:
        """True only while envelopes are actually arriving (monotonic clock:
        the Pi has no RTC and its wall clock is stepped when the backend sets
        it after boot)."""
        return bool(self._last_rx) and (time.monotonic() - self._last_rx) < self._stale_after

    def link_age_s(self) -> Optional[float]:
        """Seconds since the last envelope (since start() before the first
        one); None when the client is not running."""
        ref = self._last_rx or self._started_at
        if not ref:
            return None
        return time.monotonic() - ref

    def stats(self) -> dict:
        return {
            **self.counters,
            "connected": self.connected,
            "last_rx_age_s": round(time.monotonic() - self._last_rx, 1)
            if self._last_rx else None,
        }

    # ── commands (any thread) ────────────────────────────────────────────────

    def _make_req_socket(self) -> None:
        """Caller holds ``_req_lock``."""
        sock = self._ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVTIMEO, 2000)
        sock.setsockopt(zmq.SNDTIMEO, 2000)
        # A lost reply must not wedge the lockstep state machine: relaxed lets
        # the next send go out, correlate drops a late reply to an old request.
        for name in ("REQ_RELAXED", "REQ_CORRELATE"):
            opt = getattr(zmq, name, None)
            if opt is not None:
                try:
                    sock.setsockopt(opt, 1)
                except zmq.ZMQError:
                    pass
        apply_liveness_options(sock)
        sock.connect(f"tcp://{self._host}:{self._rep_port}")
        self._req_sock = sock

    def send_command(self, name: str, params: Optional[Dict] = None) -> bool:
        """Send a command via REQ/REP. Returns True when the backend ACCEPTED
        it (execution is asynchronous on the engine thread)."""
        payload = {"command": name, "params": params or {}}
        with self._req_lock:
            if not self._req_sock:
                return False
            try:
                self._req_sock.send_string(json.dumps(payload))
                reply = json.loads(self._req_sock.recv_string())
                ok = reply.get("status") == "ok"
                if ok:
                    self.counters["cmd_ok"] += 1
                    logger.info("Command %s accepted (params=%s)", name, params or {})
                else:
                    self.counters["cmd_fail"] += 1
                    logger.warning("Command %s rejected: %s", name, reply.get("reason"))
                return ok
            except zmq.error.Again:
                self.counters["cmd_fail"] += 1
                logger.warning("Command %s failed: REQ timeout — rebuilding socket", name)
                self._rebuild_req_locked()
                return False
            except Exception as exc:
                self.counters["cmd_fail"] += 1
                logger.warning("Command %s failed: %s — rebuilding socket", name, exc)
                self._rebuild_req_locked()
                return False

    def _rebuild_req_locked(self) -> None:
        try:
            if self._req_sock is not None:
                self._req_sock.close(linger=0)
        except Exception:
            pass
        self._req_sock = None
        try:
            self._make_req_socket()
        except Exception as exc:
            logger.warning("REQ socket rebuild failed: %s", exc)

    # ── background loop ──────────────────────────────────────────────────────

    def _make_sub_socket(self):
        sub_sock = self._ctx.socket(zmq.SUB)
        sub_sock.setsockopt(zmq.SUBSCRIBE, b"")
        sub_sock.setsockopt(zmq.LINGER, 0)
        sub_sock.setsockopt(zmq.RCVTIMEO, 1000)
        apply_liveness_options(sub_sock)
        sub_sock.connect(f"tcp://{self._host}:{self._pub_port}")
        return sub_sock

    def _rebuild_all(self, sub_sock):
        """Watchdog step: close both sockets, terminate the context, build a
        new context and new sockets. Returns the new SUB socket."""
        try:
            sub_sock.close(linger=0)
        except Exception:
            pass
        with self._req_lock:
            try:
                if self._req_sock is not None:
                    self._req_sock.close(linger=0)
            except Exception:
                pass
            self._req_sock = None
            old = self._ctx
            try:
                old.term()
            except Exception:
                logger.debug("ZMQ context term failed", exc_info=True)
            self._ctx = zmq.Context()
            try:
                self._make_req_socket()
            except Exception as exc:
                logger.warning("REQ socket rebuild failed: %s", exc)
        return self._make_sub_socket()

    def _run(self) -> None:
        sub_sock = self._make_sub_socket()

        last_summary = time.monotonic()
        # watchdog: silence is measured from the last envelope or the last
        # (re)build, whichever is later
        last_build = time.monotonic()
        rebuild_ivl = self._rebuild_after
        rebuilds_in_outage = 0
        while not self._stop.is_set():
            try:
                raw = sub_sock.recv_string()
                self._last_rx = time.monotonic()
                self._handle_raw(raw)
            except zmq.error.Again:
                pass  # timeout — fall through to liveness/summary checks
            except Exception as exc:
                if not self._stop.is_set():
                    logger.debug("ZMQ SUB error: %s", exc)
                    time.sleep(0.2)

            now = time.monotonic()
            up = self.connected
            if up and not self._was_connected:
                if rebuilds_in_outage:
                    logger.info("Backend link UP (first envelope received; after %d "
                                "socket rebuild(s))", rebuilds_in_outage)
                else:
                    logger.info("Backend link UP (first envelope received)")
                self._was_connected = True
            elif not up and self._was_connected:
                logger.warning("Backend link DOWN: no envelope for %.1fs "
                               "(backend stopped or network lost)",
                               now - self._last_rx)
                self._was_connected = False
            if up:
                rebuild_ivl = self._rebuild_after
                rebuilds_in_outage = 0
            elif now - max(self._last_rx, last_build) >= rebuild_ivl \
                    and not self._stop.is_set():
                rebuilds_in_outage += 1
                self.counters["rebuilds"] += 1
                if rebuilds_in_outage == 1:
                    logger.warning("Backend link: no envelope for %.0fs — rebuilding the "
                                   "ZMQ sockets and context (repeats with backoff up to "
                                   "%.0fs until the link is back)", rebuild_ivl,
                                   self._rebuild_max)
                else:
                    logger.debug("Backend link: rebuild #%d", rebuilds_in_outage)
                try:
                    sub_sock = self._rebuild_all(sub_sock)
                except Exception as exc:
                    logger.warning("ZMQ rebuild failed: %s", exc)
                    try:
                        sub_sock = self._make_sub_socket()
                    except Exception:
                        time.sleep(1.0)
                last_build = time.monotonic()
                rebuild_ivl = min(rebuild_ivl * 2, self._rebuild_max)
            if (now - last_summary) >= self._summary_interval:
                last_summary = now
                logger.info("Link summary: %s", self.stats())

        try:
            sub_sock.close(linger=0)
        except Exception:
            pass

    def _handle_raw(self, raw) -> None:
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError):
            self.counters["decode_errors"] += 1
            return
        if not isinstance(envelope, dict):
            self.counters["decode_errors"] += 1
            return
        msg_type = envelope.get("type")
        if msg_type == "hb":
            self.counters["hb_rx"] += 1   # proof of life only (backend idle)
        elif msg_type == "state":
            state = envelope.get("state")
            if state is not None and self._on_state is not None:
                self.counters["state_rx"] += 1
                logger.debug("State frame #%d received", self.counters["state_rx"])
                self._on_state(state)
        elif msg_type == "event":
            name = envelope.get("name", "")
            data = envelope.get("data", {})
            if name and self._on_event is not None:
                self.counters["event_rx"] += 1
                if name == "touch":
                    # 10-15 Hz while a finger is down: keep the log quiet
                    logger.debug("Event received: %s %s", name, data)
                elif name == "roadsense_scene":
                    # 5-10 Hz while driving: never format the scene (the
                    # dict repr alone is ~0.6 kB per event on the Pi Zero)
                    pass
                else:
                    logger.info("Event received: %s %s", name, data)
                self._on_event(name, data)
