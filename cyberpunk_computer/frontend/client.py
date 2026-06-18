"""
BackendClient — frontend-side link to the headless backend's network API.

Runs a daemon thread that keeps a live WebSocket subscription to
``/api/v1/stream`` and pushes each decoded state snapshot to a callback. If the
``websocket-client`` package is unavailable or the socket drops, it transparently
falls back to REST polling of ``/api/v1/state`` so the frontend still updates
(just at a lower rate). Outgoing commands are POSTed with the stdlib only.

The callback receives the raw serialized state dict; deserialization into an
AppState happens on the consumer (main/pygame) thread, mirroring the engine's
single-threaded model.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

StateCallback = Callable[[dict], None]


class BackendClient:
    def __init__(
        self,
        host: str,
        port: int = 8080,
        token: Optional[str] = None,
        on_state: Optional[StateCallback] = None,
        poll_interval: float = 1.0,
        connect_timeout: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._on_state = on_state
        self._poll_interval = poll_interval
        self._connect_timeout = connect_timeout
        self._http_base = f"http://{host}:{port}"
        self._ws_url = f"ws://{host}:{port}/api/v1/stream"
        if token:
            self._ws_url += f"?token={token}"
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = threading.Event()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="backend-client", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ── commands (called from the consumer thread) ───────────────────────────

    def send_command(self, name: str, params: Optional[Dict] = None) -> bool:
        """POST a command to the backend. Returns True on HTTP 2xx."""
        url = f"{self._http_base}/api/v1/commands/{name}"
        body = json.dumps(params or {}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self._token:
            req.add_header("Authorization", "Bearer " + self._token)
        try:
            with urllib.request.urlopen(req, timeout=self._connect_timeout) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            logger.warning("Command %s rejected: HTTP %s", name, exc.code)
            return False
        except Exception as exc:
            logger.warning("Command %s failed: %s", name, exc)
            return False

    # ── background loop ──────────────────────────────────────────────────────

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            ok = self._try_websocket()
            if not ok:
                # WS unavailable/closed: fall back to a polling pass.
                self._poll_once()
            if self._stop.is_set():
                break
            time.sleep(min(backoff, 5.0))
            backoff = min(backoff * 1.5, 5.0) if not self._connected.is_set() else 1.0

    def _try_websocket(self) -> bool:
        """Open a WS and pump messages until it closes. Returns False if WS unusable."""
        try:
            import websocket  # type: ignore  (websocket-client)
        except ImportError:
            return False
        try:
            ws = websocket.create_connection(self._ws_url, timeout=self._connect_timeout)
        except Exception as exc:
            logger.debug("WebSocket connect failed: %s", exc)
            return False
        logger.info("Connected to backend WebSocket %s", self._ws_url)
        self._connected.set()
        try:
            ws.settimeout(self._poll_interval)
            while not self._stop.is_set():
                try:
                    raw = ws.recv()
                except Exception:
                    # timeout or closed
                    if self._stop.is_set():
                        break
                    # Probe liveness with a ping; break on failure.
                    try:
                        ws.ping()
                        continue
                    except Exception:
                        break
                if not raw:
                    break
                self._handle_raw(raw)
            return True
        finally:
            self._connected.clear()
            try:
                ws.close()
            except Exception:
                pass

    def _poll_once(self) -> None:
        """REST fallback: fetch the latest state snapshot a single time."""
        url = f"{self._http_base}/api/v1/state"
        req = urllib.request.Request(url)
        if self._token:
            req.add_header("Authorization", "Bearer " + self._token)
        try:
            with urllib.request.urlopen(req, timeout=self._connect_timeout) as resp:
                if 200 <= resp.status < 300:
                    self._connected.set()
                    self._handle_raw(resp.read().decode())
                    return
        except urllib.error.HTTPError as exc:
            logger.debug("State poll HTTP %s", exc.code)
        except Exception as exc:
            logger.debug("State poll failed: %s", exc)
        self._connected.clear()

    def _handle_raw(self, raw) -> None:
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError):
            return
        state = envelope.get("state") if isinstance(envelope, dict) else None
        if state is not None and self._on_state is not None:
            self._on_state(state)
