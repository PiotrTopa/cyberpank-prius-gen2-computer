"""
Network API package — FastAPI service exposing the backend over HTTP/WebSocket.

Composition:
- :class:`StoreBridge` marshals commands (API->engine) and state snapshots
  (engine->API) across the single-threaded engine boundary.
- :func:`create_app` builds the FastAPI app (REST + WS).
- :class:`ApiServer` runs uvicorn on a background thread.
"""

from .bridge import StoreBridge
from .server import ApiServer
from .app import create_app
from .commands import CommandError, UnknownCommand, build_command, command_catalog

__all__ = [
    "StoreBridge",
    "ApiServer",
    "create_app",
    "build_command",
    "command_catalog",
    "CommandError",
    "UnknownCommand",
]
