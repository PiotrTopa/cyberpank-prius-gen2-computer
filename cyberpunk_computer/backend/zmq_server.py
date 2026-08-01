"""
ZmqServer — ZeroMQ transport for the headless backend.

* PUB on ``pub_port`` (default 8081): JSON envelopes, two kinds —
  ``{"type":"state","ts":..,"state":{...}}`` (latest-only, throttled to
  ``state_min_interval``) and ``{"type":"event","ts":..,"name":..,"data":{...}}``
  (FIFO, never displaced by state frames).
* REP on ``rep_port`` (default 8082): ``{"command": name, "params": {...}}`` →
  ``{"status":"ok"}`` on ACCEPTANCE (the command is queued to the engine
  thread via StoreBridge; execution is asynchronous) or
  ``{"status":"error","reason":...}``.

Threading: ``enqueue_state`` / ``enqueue_event`` are thread-safe and
non-blocking (engine thread calls them); all socket IO happens on the private
server thread. State and events use SEPARATE queues so a burst of state
frames can never drop an event — events are the channel HID/feedback
reactions ride on.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Optional

import zmq

from ..api.bridge import StoreBridge

logger = logging.getLogger(__name__)


class ZmqServer:
    def __init__(
        self,
        bridge: StoreBridge,
        host: str = "0.0.0.0",
        pub_port: int = 8081,
        rep_port: int = 8082,
        state_min_interval: float = 1.0,
    ):
        self._bridge = bridge
        self.host = host
        self.pub_port = pub_port
        self.rep_port = rep_port
        self.state_min_interval = state_min_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_box: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        self._event_box: "queue.Queue[dict]" = queue.Queue(maxsize=64)
        self._last_state_ts = 0.0
        # Telemetry for logs/debugging.
        self.stats = {"state_pub": 0, "event_pub": 0, "commands": 0,
                      "command_errors": 0, "events_dropped": 0}

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="zmq-server", daemon=True)
        self._thread.start()
        logger.info("ZMQ server starting: PUB %s:%d, REP %s:%d, state interval %.1fs",
                    self.host, self.pub_port, self.host, self.rep_port,
                    self.state_min_interval)

    def stop(self, timeout=3.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("ZMQ server stopped (stats: %s)", self.stats)

    # ── producers (engine thread) ────────────────────────────────────────────

    def enqueue_state(self, envelope: dict):
        """Queue a state envelope, latest-only + rate-limited."""
        now = time.time()
        if now - self._last_state_ts < self.state_min_interval:
            return
        self._last_state_ts = now
        try:
            self._state_box.put_nowait(envelope)
        except queue.Full:
            try:
                self._state_box.get_nowait()
            except queue.Empty:
                pass
            try:
                self._state_box.put_nowait(envelope)
            except queue.Full:
                pass

    def enqueue_event(self, envelope: dict):
        """Queue an event envelope (FIFO; independent of the state slot)."""
        try:
            self._event_box.put_nowait(envelope)
        except queue.Full:
            self.stats["events_dropped"] += 1
            logger.warning("ZMQ event queue full — dropping %s",
                           envelope.get("name"))

    # ── server thread ────────────────────────────────────────────────────────

    def _run(self):
        ctx = zmq.Context.instance()
        pub_sock = ctx.socket(zmq.PUB)
        pub_sock.setsockopt(zmq.SNDHWM, 16)
        pub_sock.bind(f"tcp://{self.host}:{self.pub_port}")

        rep_sock = ctx.socket(zmq.REP)
        rep_sock.bind(f"tcp://{self.host}:{self.rep_port}")

        poller = zmq.Poller()
        poller.register(rep_sock, zmq.POLLIN)

        while not self._stop.is_set():
            socks = dict(poller.poll(timeout=50))  # ms
            if rep_sock in socks:
                self._handle_request(rep_sock)

            # Events first (never starved by state), then the state slot.
            while True:
                try:
                    envelope = self._event_box.get_nowait()
                except queue.Empty:
                    break
                pub_sock.send_string(json.dumps(envelope))
                self.stats["event_pub"] += 1
                logger.info("ZMQ event published: %s", envelope.get("name"))
            try:
                envelope = self._state_box.get_nowait()
                pub_sock.send_string(json.dumps(envelope))
                self.stats["state_pub"] += 1
            except queue.Empty:
                pass

        pub_sock.close(linger=0)
        rep_sock.close(linger=0)

    def _handle_request(self, rep_sock) -> None:
        try:
            msg = rep_sock.recv_string()
        except Exception as e:
            logger.warning("ZMQ REP recv error: %s", e)
            return
        reply = {"status": "error", "reason": "invalid request"}
        try:
            req = json.loads(msg)
            cmd = req.get("command")
            params = req.get("params", {})
            if cmd:
                self._bridge.submit_command(cmd, params)
                self.stats["commands"] += 1
                logger.info("ZMQ command accepted: %s %s", cmd, params)
                reply = {"status": "ok"}
            else:
                reply = {"status": "error", "reason": "missing command"}
        except Exception as e:
            self.stats["command_errors"] += 1
            logger.warning("ZMQ command error: %s", e)
            reply = {"status": "error", "reason": str(e)}
        try:
            rep_sock.send_string(json.dumps(reply))
        except Exception:
            logger.exception("ZMQ REP reply failed")
