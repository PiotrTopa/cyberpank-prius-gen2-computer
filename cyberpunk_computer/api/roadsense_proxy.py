"""
roadsense front: the backend re-publishes the roadsense perception API.

Single-computer layout: the roadsense stack (github.com/PiotrTopa/roadsense,
MIT, plus the AGPL sign provider roadsense-signs) runs on this same phone with
its API bound to loopback (``ROADSENSE_API_HOST=127.0.0.1``, port 8090). Clients
(dashboard, MFD, phone apps) read roadsense through the backend instead:

    GET /api/v1/roadsense                     index: upstream URL + endpoint list
    GET /api/v1/roadsense/<name>              JSON passthrough (health, road,
                                              signs, signs/history, env,
                                              speed_limit, rules, lights, camera)
    GET /api/v1/roadsense/frame.jpg           current preview JPEG
    GET /api/v1/roadsense/preview             MJPEG overlay stream
    WS  /api/v1/roadsense/ws                  every daemon message (?topics=...)

The boundary is HTTP only: this module speaks roadsense's documented HTTP/WS
contract (roadsense docs/ARCHITECTURE.md, "api") and imports nothing from
either roadsense repository. Endpoints are whitelisted (no open proxy), bodies
are size-capped and the number of concurrent MJPEG relays is bounded so a
forgotten browser tab cannot eat the backend's CPU core or memory budget.

The HTTP client is a tiny asyncio HTTP/1.0 reader (stdlib only: the backend
venv deliberately carries no extra HTTP client). roadsense's aiohttp server
answers HTTP/1.0 with ``Connection: close``, so the body runs to EOF.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, HTTPException, Request, WebSocket
from fastapi.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)

# name under /api/v1/roadsense/ -> roadsense path. Only these are forwarded.
JSON_ENDPOINTS: Dict[str, str] = {
    "health": "/v1/health",
    "road": "/v1/road",
    "signs": "/v1/signs",
    "signs/history": "/v1/signs/history",
    "env": "/v1/env",
    "speed_limit": "/v1/speed_limit",
    "rules": "/v1/rules",
    "lights": "/v1/lights",
    "camera": "/v1/camera",
}
# Query parameters passed through per endpoint (everything else is dropped).
ALLOWED_QUERY: Dict[str, Tuple[str, ...]] = {
    "signs/history": ("limit",),
}
FRAME_PATH = "/v1/frame.jpg"
PREVIEW_PATH = "/preview"
WS_PATH = "/ws"
WS_TOPICS = ("road", "signs", "env", "speed_limit", "rules", "lights", "camera", "calib")


class RoadsenseUnavailable(Exception):
    """roadsense did not answer (stack stopped, starting, or wedged)."""


class RoadsenseBadResponse(Exception):
    """roadsense answered with something unusable (status, size, framing)."""


@dataclass
class ProxyConfig:
    base_url: str = "http://127.0.0.1:8090"
    timeout_s: float = 3.0
    max_body_bytes: int = 4 * 1024 * 1024
    max_preview_streams: int = 2
    chunk_bytes: int = 32 * 1024

    @property
    def host_port(self) -> Tuple[str, int]:
        u = urlsplit(self.base_url)
        return (u.hostname or "127.0.0.1", u.port or 80)

    @property
    def ws_url(self) -> str:
        host, port = self.host_port
        return "ws://%s:%d%s" % (host, port, WS_PATH)


def build_query(name: str, params: Dict[str, str]) -> str:
    """Keep only the whitelisted query parameters of ``name`` (urlencoded)."""
    allowed = ALLOWED_QUERY.get(name, ())
    kept = [(k, params[k]) for k in allowed if k in params]
    return ("?" + urlencode(kept)) if kept else ""


def ws_topics(raw: Optional[str]) -> Optional[str]:
    """Sanitise a ``topics`` filter: known topic names only, comma separated."""
    if not raw:
        return None
    names = [t.strip() for t in raw.split(",") if t.strip() in WS_TOPICS]
    return ",".join(dict.fromkeys(names)) or None


async def _open(cfg: ProxyConfig, path: str):
    """Send ``GET path`` and parse the response head.

    Returns ``(status, headers, reader, writer)``; the caller owns the writer.
    """
    host, port = cfg.host_port
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=cfg.timeout_s)
    except (OSError, asyncio.TimeoutError) as exc:
        raise RoadsenseUnavailable("connect %s:%d: %s" % (host, port, exc or "timeout"))
    try:
        writer.write(("GET %s HTTP/1.0\r\nHost: %s:%d\r\nUser-Agent: prius-backend\r\n"
                      "Connection: close\r\n\r\n" % (path, host, port)).encode("ascii"))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=cfg.timeout_s)
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError,
            asyncio.LimitOverrunError) as exc:
        writer.close()
        raise RoadsenseUnavailable("read head %s: %s" % (path, exc or "timeout"))
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        writer.close()
        raise RoadsenseBadResponse("bad status line %r" % lines[0][:80])
    headers: Dict[str, str] = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return int(parts[1]), headers, reader, writer


async def fetch(cfg: ProxyConfig, path: str) -> Tuple[int, str, bytes]:
    """GET ``path`` from roadsense; returns ``(status, content_type, body)``."""
    status, headers, reader, writer = await _open(cfg, path)
    try:
        body = b""
        while True:
            chunk = await asyncio.wait_for(reader.read(cfg.chunk_bytes), timeout=cfg.timeout_s)
            if not chunk:
                break
            body += chunk
            if len(body) > cfg.max_body_bytes:
                raise RoadsenseBadResponse("body over %d bytes" % cfg.max_body_bytes)
    except (OSError, asyncio.TimeoutError) as exc:
        raise RoadsenseUnavailable("read body %s: %s" % (path, exc or "timeout"))
    finally:
        writer.close()
    return status, headers.get("content-type", "application/octet-stream"), body


async def open_stream(cfg: ProxyConfig, path: str) -> Tuple[int, str, AsyncIterator[bytes]]:
    """GET a streaming ``path`` (MJPEG); returns ``(status, content_type, chunks)``.

    The iterator closes the upstream connection when the client goes away
    (generator close) or the upstream ends.
    """
    status, headers, reader, writer = await _open(cfg, path)

    async def chunks() -> AsyncIterator[bytes]:
        try:
            while True:
                # A live MJPEG stream sends a frame every ~50-100 ms; a long
                # silence means the stack stopped under us.
                chunk = await asyncio.wait_for(reader.read(cfg.chunk_bytes),
                                               timeout=max(10.0, cfg.timeout_s))
                if not chunk:
                    return
                yield chunk
        except (OSError, asyncio.TimeoutError):
            return
        finally:
            writer.close()

    return status, headers.get("content-type", "application/octet-stream"), chunks()


def index(cfg: ProxyConfig, prefix: str) -> Dict[str, object]:
    eps: List[str] = [prefix + "/" + n for n in JSON_ENDPOINTS]
    eps += [prefix + "/frame.jpg", prefix + "/preview", prefix + "/ws"]
    return {"upstream": cfg.base_url, "endpoints": eps, "ws_topics": list(WS_TOPICS)}


def register(app, cfg: ProxyConfig, prefix: str, require_auth, ws_authorized) -> None:
    """Mount the roadsense front routes on a FastAPI ``app``."""
    previews = {"n": 0}

    def _unavailable(exc: Exception) -> HTTPException:
        return HTTPException(status_code=503, detail="roadsense unavailable: %s" % exc)

    @app.get(prefix, dependencies=[Depends(require_auth)])
    async def rs_index() -> Dict[str, object]:
        return index(cfg, prefix)

    @app.get(prefix + "/frame.jpg", dependencies=[Depends(require_auth)])
    async def rs_frame() -> Response:
        try:
            status, ctype, body = await fetch(cfg, FRAME_PATH)
        except RoadsenseUnavailable as exc:
            raise _unavailable(exc)
        except RoadsenseBadResponse as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return Response(content=body, status_code=status, media_type=ctype,
                        headers={"Cache-Control": "no-store"})

    @app.get(prefix + "/preview", dependencies=[Depends(require_auth)])
    async def rs_preview() -> StreamingResponse:
        if previews["n"] >= cfg.max_preview_streams:
            raise HTTPException(status_code=429,
                                detail="too many preview streams (max %d)" % cfg.max_preview_streams)
        try:
            status, ctype, chunks = await open_stream(cfg, PREVIEW_PATH)
        except RoadsenseUnavailable as exc:
            raise _unavailable(exc)
        except RoadsenseBadResponse as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        previews["n"] += 1

        async def body() -> AsyncIterator[bytes]:
            try:
                async for c in chunks:
                    yield c
            finally:
                previews["n"] -= 1
                await chunks.aclose()

        return StreamingResponse(body(), status_code=status, media_type=ctype,
                                 headers={"Cache-Control": "no-store"})

    @app.websocket(prefix + "/ws")
    async def rs_ws(websocket: WebSocket) -> None:
        if not ws_authorized(websocket):
            await websocket.close(code=1008)
            return
        url = cfg.ws_url
        topics = ws_topics(websocket.query_params.get("topics"))
        if topics:
            url += "?" + urlencode({"topics": topics})
        try:
            upstream = await asyncio.wait_for(_ws_connect(url), timeout=cfg.timeout_s)
        except Exception as exc:  # noqa: BLE001 - stack down / refused
            await websocket.accept()
            await websocket.close(code=1013, reason="roadsense unavailable")
            logger.debug("roadsense ws connect failed: %s", exc)
            return
        await websocket.accept()

        async def down() -> None:      # roadsense -> client
            async for msg in upstream:
                if isinstance(msg, bytes):
                    await websocket.send_bytes(msg)
                else:
                    await websocket.send_text(msg)

        async def up() -> None:        # client -> nothing (read-only relay); detect close
            while True:
                m = await websocket.receive()
                if m.get("type") == "websocket.disconnect":
                    return

        tasks = [asyncio.ensure_future(down()), asyncio.ensure_future(up())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            try:
                await upstream.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    @app.get(prefix + "/{name:path}", dependencies=[Depends(require_auth)])
    async def rs_json(name: str, request: Request) -> Response:
        path = JSON_ENDPOINTS.get(name.strip("/"))
        if path is None:
            raise HTTPException(status_code=404, detail="unknown roadsense endpoint %r" % name)
        path += build_query(name.strip("/"), dict(request.query_params))
        try:
            status, ctype, body = await fetch(cfg, path)
        except RoadsenseUnavailable as exc:
            raise _unavailable(exc)
        except RoadsenseBadResponse as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return Response(content=body, status_code=status,
                        media_type=ctype.split(";")[0] or "application/json")


async def _ws_connect(url: str):
    """Open a client WebSocket with whichever API the installed websockets has."""
    try:
        from websockets.asyncio.client import connect  # websockets >= 13
    except ImportError:  # pragma: no cover - older websockets
        from websockets import connect  # type: ignore
    return await connect(url, max_size=8 * 1024 * 1024, open_timeout=5)
