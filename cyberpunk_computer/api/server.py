"""
ApiServer — run the FastAPI app under uvicorn in a dedicated thread so the
engine main loop keeps ownership of its own thread.

The engine composes this with a StoreBridge: the bridge's event loop is attached
on FastAPI startup (see app.create_app), after which engine-thread snapshots can
be fanned out to WebSocket clients.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import uvicorn

from ..metrics import MetricsDatabase
from .app import create_app
from .bridge import StoreBridge

logger = logging.getLogger(__name__)


class ApiServer:
    """Own a uvicorn server running on a background thread."""

    def __init__(
        self,
        bridge: StoreBridge,
        db: MetricsDatabase,
        host: str = "0.0.0.0",
        port: int = 8080,
        auth_token: Optional[str] = None,
        log_level: str = "info",
    ) -> None:
        self._app = create_app(bridge, db, auth_token=auth_token)
        config = uvicorn.Config(
            self._app,
            host=host,
            port=port,
            log_level=log_level,
            access_log=False,
            # Single worker: the bridge holds one event loop reference.
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        # Let our own SIGINT handling in the backend drive shutdown.
        self._server.install_signal_handlers = lambda: None
        self._thread: Optional[threading.Thread] = None
        self._host = host
        self._port = port

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.run, name="api-server", daemon=True
        )
        self._thread.start()
        logger.info("API server starting on http://%s:%d", self._host, self._port)

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._server.should_exit = True
        self._thread.join(timeout=timeout)
        self._thread = None
        logger.info("API server stopped")
