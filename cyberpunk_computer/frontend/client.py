"""
BackendClient — frontend-side link to the headless backend's network API using ZeroMQ.
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
        port: int = 8081, # pub_port
        token: Optional[str] = None,
        on_state: Optional[StateCallback] = None,
        on_event: Optional[EventCallback] = None,
        poll_interval: float = 1.0,
        connect_timeout: float = 5.0,
    ) -> None:
        self._host = host
        self._pub_port = port
        self._rep_port = 8082 # Fixed for now, or could be port + 1
        self._token = token
        self._on_state = on_state
        self._on_event = on_event
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._ctx = zmq.Context.instance()
        self._req_lock = threading.Lock()
        self._req_sock = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        
        # Setup REQ socket
        self._req_sock = self._ctx.socket(zmq.REQ)
        self._req_sock.setsockopt(zmq.LINGER, 0)
        # Timeout for recv
        self._req_sock.setsockopt(zmq.RCVTIMEO, 2000)
        self._req_sock.connect(f"tcp://{self._host}:{self._rep_port}")
        
        self._thread = threading.Thread(target=self._run, name="backend-client-zmq", daemon=True)
        self._thread.start()
        logger.info("ZMQ BackendClient started. SUB connected to %s:%d", self._host, self._pub_port)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
            
        with self._req_lock:
            if self._req_sock:
                self._req_sock.close()
                self._req_sock = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ── commands (called from the consumer thread) ───────────────────────────

    def send_command(self, name: str, params: Optional[Dict] = None) -> bool:
        """POST a command to the backend via ZMQ REQ."""
        if not self._req_sock:
            return False
            
        payload = {
            "command": name,
            "params": params or {}
        }
        
        with self._req_lock:
            try:
                self._req_sock.send_string(json.dumps(payload))
                reply = self._req_sock.recv_string()
                resp = json.loads(reply)
                success = resp.get("status") == "ok"
                if not success:
                    logger.warning("Command %s rejected: %s", name, resp.get("reason"))
                return success
            except zmq.error.Again:
                logger.warning("Command %s failed: REQ timeout", name)
                # Recover REQ socket state by recreating it
                self._req_sock.close(linger=0)
                self._req_sock = self._ctx.socket(zmq.REQ)
                self._req_sock.setsockopt(zmq.LINGER, 0)
                self._req_sock.setsockopt(zmq.RCVTIMEO, 2000)
                self._req_sock.connect(f"tcp://{self._host}:{self._rep_port}")
                return False
            except Exception as exc:
                logger.warning("Command %s failed: %s", name, exc)
                return False

    # ── background loop ──────────────────────────────────────────────────────

    def _run(self) -> None:
        sub_sock = self._ctx.socket(zmq.SUB)
        sub_sock.setsockopt(zmq.SUBSCRIBE, b"")
        sub_sock.setsockopt(zmq.LINGER, 0)
        sub_sock.setsockopt(zmq.RCVTIMEO, 2000) # 2s timeout to check stop flag periodically
        sub_sock.connect(f"tcp://{self._host}:{self._pub_port}")
        
        # We consider ourselves connected as long as we're running since ZMQ handles reconnects.
        # But to be precise, we can say connected if we received something recently.
        self._connected.set()
        
        frame_counter = 0

        while not self._stop.is_set():
            try:
                raw = sub_sock.recv_string()
                self._handle_raw(raw)
                frame_counter += 1
                logger.info(f"ZMQ SUB: Received state/event frame (total {frame_counter})")
                
            except zmq.error.Again:
                # Timeout, just loop back and check _stop
                continue
            except Exception as exc:
                if not self._stop.is_set():
                    logger.debug("ZMQ SUB error: %s", exc)

        sub_sock.close()
        self._connected.clear()

    def _handle_raw(self, raw) -> None:
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(envelope, dict):
            return
        msg_type = envelope.get("type")
        if msg_type == "state":
            state = envelope.get("state")
            if state is not None and self._on_state is not None:
                self._on_state(state)
        elif msg_type == "event":
            name = envelope.get("name", "")
            data = envelope.get("data", {})
            if name and self._on_event is not None:
                logger.info("ZMQ Event received! name=%s data=%s", name, data)
                self._on_event(name, data)
