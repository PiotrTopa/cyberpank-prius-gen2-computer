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
    GET  /api/v1/bean/frames          last BEAN body-bus frames (?since=seq&limit=n)
    GET  /api/v1/roadsense[/...]      roadsense perception API front (see
                                      api.roadsense_proxy; only when wired)
    *    /api/v1/camera/preview[/...]  on-demand camera preview: lease + frame.jpg
                                      (api.camera_preview_routes; only when wired)
    GET  /api/v1/position             position feeder status (api.position_routes;
    POST /api/v1/position/gnss        head-unit GNSS fixes; only when the feeder is on)

    GET  /roadsense/                  roadsense status page for browsers (same login)
    GET|POST /login, /logout, /auth/pair, GET /auth/status, POST /auth/revoke-all
                                      browser sessions + pairing links (api.auth)

With a token configured every route except /health and the login/pairing
pages needs credentials: a trusted peer address (backend default: loopback),
``Authorization: Bearer <token>``, or a session cookie (docs/AUTH.md).
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
from .auth import AuthConfig, Authenticator, AuthMiddleware
from .auth import register_routes as register_auth_routes
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
    roadsense_url: Optional[str] = None,
    bean_frames=None,
    camera_preview=None,
    position_feeder=None,
    auth_trusted: Optional[str] = None,
    auth_state_dir: Optional[str] = None,
    extra_routes=None,
) -> FastAPI:
    """Build the FastAPI app wired to the bridge and metrics database.

    ``auth_trusted``: comma-separated networks let in without credentials
    (none by default here; the backend passes loopback). ``auth_state_dir``:
    where sessions and pairing codes persist (None = memory only).
    """
    app = FastAPI(title="CyberPunk Prius Backend", version="1.0.0")
    reads = _ReadConnections(db)

    # ── auth ─────────────────────────────────────────────────────────────────

    auth = Authenticator.from_config(
        AuthConfig(token=auth_token, trusted=auth_trusted or "", state_dir=auth_state_dir))
    app.state.auth = auth
    app.add_middleware(AuthMiddleware, auth=auth)

    def require_auth(request: Request) -> None:
        # The middleware already gated the request; this keeps every route
        # protected on its own should the app ever be mounted without it.
        if request.scope.get("prius_auth") or auth.authorize(request) is not None:
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    def ws_authorized(websocket: WebSocket) -> bool:
        return bool(websocket.scope.get("prius_auth")) or auth.authorize(websocket) is not None

    register_auth_routes(app, auth)

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

    # ── BEAN frames (ring kept out of the 1 Hz state publish) ───────────────

    @app.get(API_PREFIX + "/bean/frames", dependencies=[Depends(require_auth)])
    async def bean_frames_endpoint(
        since: int = Query(0, ge=0, description="return frames with seq > since"),
        limit: int = Query(200, ge=1, le=200),
    ) -> Dict[str, Any]:
        if bean_frames is None:
            raise HTTPException(status_code=503, detail="BEAN monitor not wired")
        return bean_frames(since, limit)

    # ── websocket stream ─────────────────────────────────────────────────────

    @app.websocket(API_PREFIX + "/stream")
    async def stream(websocket: WebSocket) -> None:
        if not ws_authorized(websocket):
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

    # ── roadsense front (same computer, loopback-only upstream) ─────────────

    if roadsense_url:
        from .roadsense_proxy import ProxyConfig, register as register_roadsense

        register_roadsense(
            app,
            ProxyConfig(base_url=roadsense_url),
            API_PREFIX + "/roadsense",
            require_auth,
            ws_authorized,
            ui_prefix="/roadsense",
        )

    # ── on-demand camera preview (backend.camera_preview) ───────────────────

    if camera_preview is not None:
        from .camera_preview_routes import register as register_camera_preview

        register_camera_preview(app, camera_preview, API_PREFIX + "/camera/preview", require_auth)

    # ── position feeder -> roadsense mapd (backend.position_feeder) ─────────

    if position_feeder is not None:
        from .position_routes import register as register_position

        register_position(app, position_feeder, API_PREFIX + "/position", require_auth)

    # ── all-ECU diagnostics (scan reports, clear log, ECU registry) ─────────

    from ..diag.api import register as register_diag

    register_diag(app, API_PREFIX + "/diag", require_auth)

    # ── further route groups (e.g. /alerts), mounted before the dashboard ───
    # ``extra_routes``: callables (app, API_PREFIX, require_auth).

    for add_routes in extra_routes or ():
        add_routes(app, API_PREFIX, require_auth)

    # ── dashboard ────────────────────────────────────────────────────────────

    dashboard_path = Path(__file__).parent.parent.parent / "dashboard" / "dist"
    if dashboard_path.exists():
        import mimetypes

        mimetypes.add_type("application/manifest+json", ".webmanifest")
        app.mount("/", StaticFiles(directory=str(dashboard_path), html=True), name="dashboard")

    return app
