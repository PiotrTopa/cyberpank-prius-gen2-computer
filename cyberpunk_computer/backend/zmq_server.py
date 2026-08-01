"""
ZmqServer - ZeroMQ based pub/sub and req/rep for the headless backend.
"""
from __future__ import annotations

import logging
import threading
import json
import time
import queue
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
    ):
        self._bridge = bridge
        self.host = host
        self.pub_port = pub_port
        self.rep_port = rep_port
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._outbox = queue.Queue(maxsize=1)
        self._last_state_ts = 0.0

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="zmq-server", daemon=True)
        self._thread.start()
        logger.info("ZMQ server starting PUB on %s:%d, REP on %s:%d", self.host, self.pub_port, self.host, self.rep_port)

    def stop(self, timeout=3.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("ZMQ server stopped")

    def enqueue_state(self, envelope: dict):
        # Keep only the latest
        try:
            self._outbox.put_nowait(envelope)
        except queue.Full:
            try:
                self._outbox.get_nowait()
                self._outbox.put_nowait(envelope)
            except queue.Empty:
                pass

    def enqueue_event(self, envelope: dict):
        try:
            self._outbox.put_nowait(envelope)
        except queue.Full:
            pass

    def _run(self):
        ctx = zmq.Context.instance()
        pub_sock = ctx.socket(zmq.PUB)
        pub_sock.bind(f"tcp://{self.host}:{self.pub_port}")

        rep_sock = ctx.socket(zmq.REP)
        rep_sock.bind(f"tcp://{self.host}:{self.rep_port}")

        poller = zmq.Poller()
        poller.register(rep_sock, zmq.POLLIN)

        while not self._stop.is_set():
            # 1. Process incoming commands
            socks = dict(poller.poll(timeout=10)) # 10ms
            if rep_sock in socks:
                try:
                    msg = rep_sock.recv_string()
                    req = json.loads(msg)
                    cmd = req.get("command")
                    params = req.get("params", {})
                    if cmd:
                        self._bridge.submit_command(cmd, params)
                        rep_sock.send_string(json.dumps({"status": "ok"}))
                    else:
                        rep_sock.send_string(json.dumps({"status": "error", "reason": "missing command"}))
                except Exception as e:
                    logger.warning("ZMQ REP error: %s", e)
                    try:
                        rep_sock.send_string(json.dumps({"status": "error", "reason": str(e)}))
                    except:
                        pass

            # 2. Publish outgoing state/events
            while not self._outbox.empty():
                try:
                    envelope = self._outbox.get_nowait()
                    pub_sock.send_string(json.dumps(envelope))
                except queue.Empty:
                    break

        pub_sock.close(linger=0)
        rep_sock.close(linger=0)
