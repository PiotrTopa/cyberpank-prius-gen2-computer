"""
BackendClient — frontend-side link to the headless backend over ZeroMQ.

* SUB to the backend's PUB (state + event envelopes).
* REQ/REP for commands (with timeout + socket recreation on failure — a REQ
  socket is a lockstep state machine and must be rebuilt after a lost reply).

Liveness: ``connected`` is TRUE only while envelopes have actually been
received within ``stale_after`` seconds — the backend publishes state at
~1 Hz, so silence means the link (or backend) is really gone. Transitions are
logged at INFO; per-frame logging stays at DEBUG with a periodic INFO summary
so a headless deployment can prove the link works from logs alone.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Dict, Optional

import zmq

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
    ) -> None:
        self._host = host
        self._pub_port = pub_port
        self._rep_port = rep_port
        self._on_state = on_state
        self._on_event = on_event
        self._stale_after = stale_after
        self._summary_interval = summary_interval
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ctx = zmq.Context.instance()
        self._req_lock = threading.Lock()
        self._req_sock = None
        self._last_rx = 0.0
        self._was_connected = False
        # Proof-of-life counters (read via stats()).
        self.counters = {"state_rx": 0, "event_rx": 0, "cmd_ok": 0,
                        "cmd_fail": 0, "decode_errors": 0}

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._make_req_socket()
        self._thread = threading.Thread(target=self._run, name="backend-client-zmq", daemon=True)
        self._thread.start()
        logger.info("ZMQ BackendClient started: SUB tcp://%s:%d, REQ tcp://%s:%d",
                    self._host, self._pub_port, self._host, self._rep_port)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        with self._req_lock:
            if self._req_sock:
                self._req_sock.close(linger=0)
                self._req_sock = None
        logger.info("ZMQ BackendClient stopped (counters: %s)", self.counters)

    @property
    def connected(self) -> bool:
        """True only while envelopes are actually arriving."""
        return (time.time() - self._last_rx) < self._stale_after

    def stats(self) -> dict:
        return {
            **self.counters,
            "connected": self.connected,
            "last_rx_age_s": round(time.time() - self._last_rx, 1)
            if self._last_rx else None,
        }

    # ── commands (any thread) ────────────────────────────────────────────────

    def _make_req_socket(self) -> None:
        sock = self._ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVTIMEO, 2000)
        sock.setsockopt(zmq.SNDTIMEO, 2000)
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
                self._req_sock.close(linger=0)
                self._make_req_socket()
                return False
            except Exception as exc:
                self.counters["cmd_fail"] += 1
                logger.warning("Command %s failed: %s", name, exc)
                return False

    # ── background loop ──────────────────────────────────────────────────────

    def _run(self) -> None:
        sub_sock = self._ctx.socket(zmq.SUB)
        sub_sock.setsockopt(zmq.SUBSCRIBE, b"")
        sub_sock.setsockopt(zmq.LINGER, 0)
        sub_sock.setsockopt(zmq.RCVTIMEO, 1000)
        sub_sock.connect(f"tcp://{self._host}:{self._pub_port}")

        last_summary = time.time()
        while not self._stop.is_set():
            try:
                raw = sub_sock.recv_string()
                self._last_rx = time.time()
                self._handle_raw(raw)
            except zmq.error.Again:
                pass  # timeout — fall through to liveness/summary checks
            except Exception as exc:
                if not self._stop.is_set():
                    logger.debug("ZMQ SUB error: %s", exc)

            now = time.time()
            up = self.connected
            if up and not self._was_connected:
                logger.info("Backend link UP (first envelope received)")
                self._was_connected = True
            elif not up and self._was_connected:
                logger.warning("Backend link DOWN: no envelope for %.1fs "
                               "(backend stopped or network lost)",
                               now - self._last_rx)
                self._was_connected = False
            if (now - last_summary) >= self._summary_interval:
                last_summary = now
                logger.info("Link summary: %s", self.stats())

        sub_sock.close(linger=0)

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
        if msg_type == "state":
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
                logger.info("Event received: %s %s", name, data)
                self._on_event(name, data)
