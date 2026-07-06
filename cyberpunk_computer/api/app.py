"""
FastAPI application for the backend network API.

Surface (versioned under /api/v1), designed for the future Android dashboard:

    GET  /health                      liveness + link/connection summary
    GET  /api/v1/state                latest full state snapshot
    WS   /api/v1/stream               live state snapshots (latest-only)
    GET  /api/v1/metrics/catalog      available signals + commands
    GET  /api/v1/metrics              time-series history (auto/raw/1m/1h/1d)
    GET  /api/v1/events               discrete power/vehicle events
    POST /api/v1/commands/{name}      send a command to the vehicle/twin

All /api/v1 routes (and the WS) require a Bearer token when one is configured.
Blocking SQLite reads run in a thread pool (per-thread connections) so the event
loop is never blocked.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from ..metrics import MetricsDatabase
from .bridge import StoreBridge
from .commands import CommandError, UnknownCommand, command_catalog
from .serialization import serialize_events, serialize_series

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


class _ReadConnections:
    """Lazily open one read-only SQLite connection per worker thread."""

    def __init__(self, db: MetricsDatabase) -> None:
        self._db = db
        self._local = threading.local()

    def get(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._db.connect()
            self._local.conn = conn
        return conn


def create_app(
    bridge: StoreBridge,
    db: MetricsDatabase,
    auth_token: Optional[str] = None,
    on_startup=None,
) -> FastAPI:
    """Build the FastAPI app wired to the bridge and metrics database."""
    app = FastAPI(title="CyberPunk Prius Backend", version="1.0.0")
    reads = _ReadConnections(db)

    # ── auth ─────────────────────────────────────────────────────────────────

    def require_auth(request: Request) -> None:
        if not auth_token:
            return
        header = request.headers.get("authorization", "")
        if not _token_ok(header, auth_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # ── lifecycle ────────────────────────────────────────────────────────────

    @app.on_event("startup")
    async def _startup() -> None:
        bridge.attach_loop(asyncio.get_running_loop())
        if on_startup is not None:
            on_startup()

    # ── health ───────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        snap = bridge.latest_snapshot()
        connected = None
        if snap is not None:
            connected = snap.get("state", {}).get("connection", {}).get("connected")
        return {
            "status": "ok",
            "ts": time.time(),
            "has_state": snap is not None,
            "gateway_connected": connected,
        }

    # ── state ────────────────────────────────────────────────────────────────

    @app.get(API_PREFIX + "/state", dependencies=[Depends(require_auth)])
    async def get_state() -> Dict[str, Any]:
        snap = bridge.latest_snapshot()
        if snap is None:
            raise HTTPException(status_code=503, detail="state not available yet")
        return snap

    # ── metrics catalog ──────────────────────────────────────────────────────

    @app.get(API_PREFIX + "/metrics/catalog", dependencies=[Depends(require_auth)])
    async def metrics_catalog() -> Dict[str, Any]:
        from ..metrics import SIGNALS

        present = await asyncio.to_thread(lambda: db.signals_present(reads.get()))
        present_set = set(present)
        signals = [
            {
                "name": s.name,
                "unit": s.unit,
                "description": s.description,
                "has_data": s.name in present_set,
            }
            for s in SIGNALS
        ]
        return {"signals": signals, "commands": command_catalog()}

    # ── metrics series ───────────────────────────────────────────────────────

    @app.get(API_PREFIX + "/metrics", dependencies=[Depends(require_auth)])
    async def metrics(
        signal: str = Query(..., description="signal name"),
        from_: Optional[float] = Query(None, alias="from", description="unix start seconds"),
        to: Optional[float] = Query(None, description="unix end seconds"),
        res: Optional[str] = Query(None, description="auto|raw|1m|1h|1d"),
    ) -> Dict[str, Any]:
        end = to if to is not None else time.time()
        start = from_ if from_ is not None else end - 3600.0
        if start >= end:
            raise HTTPException(status_code=400, detail="'from' must be before 'to'")
        resolution = None if (res in (None, "", "auto")) else res
        if resolution is not None and resolution not in ("raw", "1m", "1h", "1d"):
            raise HTTPException(status_code=400, detail="res must be auto|raw|1m|1h|1d")
        chosen = resolution or db.pick_resolution(start, end)
        points = await asyncio.to_thread(
            lambda: db.query_series(reads.get(), signal, start, end, resolution)
        )
        return {
            "signal": signal,
            "from": start,
            "to": end,
            "resolution": chosen,
            "points": serialize_series(points),
        }

    # ── events ───────────────────────────────────────────────────────────────

    @app.get(API_PREFIX + "/events", dependencies=[Depends(require_auth)])
    async def events(
        from_: Optional[float] = Query(None, alias="from"),
        to: Optional[float] = Query(None),
        type: Optional[str] = Query(None, description="filter by event type"),
        limit: int = Query(200, ge=1, le=5000),
    ) -> Dict[str, Any]:
        end = to if to is not None else time.time()
        start = from_ if from_ is not None else end - 86400.0
        rows = await asyncio.to_thread(
            lambda: db.query_events(reads.get(), start, end, type, limit)
        )
        return {"from": start, "to": end, "events": serialize_events(rows)}

    # ── commands ─────────────────────────────────────────────────────────────

    @app.get(API_PREFIX + "/commands", dependencies=[Depends(require_auth)])
    async def list_commands() -> Dict[str, Any]:
        return {"commands": command_catalog()}

    @app.post(API_PREFIX + "/commands/{name}", dependencies=[Depends(require_auth)])
    async def post_command(name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            action = bridge.submit_command(name, params or {})
        except UnknownCommand:
            raise HTTPException(status_code=404, detail=f"unknown command '{name}'")
        except CommandError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        return {"accepted": True, "command": name, "action": type(action).__name__}

    # ── websocket stream ─────────────────────────────────────────────────────

    @app.websocket(API_PREFIX + "/stream")
    async def stream(websocket: WebSocket) -> None:
        if auth_token and not _ws_authorized(websocket, auth_token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await websocket.accept()
        client = bridge.register_client()
        try:
            while True:
                envelope = await client.get()
                await websocket.send_json(envelope)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.debug("WebSocket stream closed", exc_info=True)
        finally:
            bridge.unregister_client(client)

    # ── dashboard ────────────────────────────────────────────────────────────

    dashboard_path = Path(__file__).parent.parent.parent / "dashboard" / "dist"
    if dashboard_path.exists():
        app.mount("/", StaticFiles(directory=str(dashboard_path), html=True), name="dashboard")

    return app


def _ws_authorized(websocket: WebSocket, expected: str) -> bool:
    """Accept the token from either the Authorization header or ?token=."""
    if _token_ok(websocket.headers.get("authorization", ""), expected):
        return True
    token = websocket.query_params.get("token") or ""
    return _token_ok("Bearer " + token, expected)


def _token_ok(authorization_header: str, expected: str) -> bool:
    """Constant-time check of a 'Bearer <token>' header against expected."""
    import hmac

    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1].strip(), expected)
