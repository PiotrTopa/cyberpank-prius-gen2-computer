"""
REST front of the on-demand camera preview (backend.camera_preview):

    GET    /api/v1/camera/preview                     status (phase, source, leases)
    POST   /api/v1/camera/preview/lease               {"lease_id"?: str} -> start / renew
    DELETE /api/v1/camera/preview/lease/{lease_id}    stop
    POST   /api/v1/camera/preview/lease/{lease_id}/release
                                                      stop; same as DELETE, for
                                                      navigator.sendBeacon (POST only)
    GET    /api/v1/camera/preview/frame.jpg?w=640&q=70[&lease=<id>]
                                                      latest frame (JPEG)

Lease answers: ``{lease_id, ttl_s, expires_in_s, max_session_s,
session_left_s, phase, reason, source, leases}``. Refusals carry
``{"detail": {"reason": ..., "message": ...}}``: 409 ``undervoltage``,
410 ``expired`` / ``max_session`` (press again), 429 ``too_many_leases``,
503 ``disabled``.

Frame answers: 200 ``image/jpeg`` with ``ETag``, ``X-Frame-Age`` (s),
``X-Frame-Time`` (wall clock), ``X-Frame-Seq``, ``X-Frame-Source``
(stack|direct), ``X-Preview-Scaled``; 304 on a matching ``If-None-Match``;
409 without an active lease; 503 + ``Retry-After`` and
``{"detail": {"phase", "reason"}}`` while the camera is coming up.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Body, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from ..backend.camera_preview import FrameUnavailable, PreviewRefused

_LEASE_ID_MAX = 64


def _valid_id(lease_id: Optional[str]) -> Optional[str]:
    if lease_id is None or lease_id == "":
        return None
    if not isinstance(lease_id, str) or len(lease_id) > _LEASE_ID_MAX or \
            not all(c in "0123456789abcdef" for c in lease_id):
        raise HTTPException(status_code=400, detail={"reason": "bad_lease_id",
                                                     "message": "lease_id must be a hex string"})
    return lease_id


def register(app, service, prefix: str, require_auth) -> None:
    """Mount the preview routes (``prefix`` = ``/api/v1/camera/preview``)."""
    cfg = service.config

    @app.get(prefix, dependencies=[Depends(require_auth)])
    async def preview_status() -> Dict[str, Any]:
        return service.status()

    @app.post(prefix + "/lease", dependencies=[Depends(require_auth)])
    async def preview_lease(body: Optional[Dict[str, Any]] = Body(None)) -> Dict[str, Any]:
        if body is not None and not isinstance(body, dict):
            raise HTTPException(status_code=400, detail={"reason": "bad_body",
                                                         "message": "JSON object expected"})
        lease_id = _valid_id((body or {}).get("lease_id"))
        try:
            return service.lease(lease_id)
        except PreviewRefused as exc:
            raise HTTPException(status_code=exc.status,
                                detail={"reason": exc.reason, "message": exc.detail})

    async def _release(lease_id: str) -> Dict[str, Any]:
        lid = _valid_id(lease_id)
        released = service.release(lid) if lid else False
        return {"released": released, "leases": service.leases.count()}

    @app.delete(prefix + "/lease/{lease_id}", dependencies=[Depends(require_auth)])
    async def preview_lease_delete(lease_id: str) -> Dict[str, Any]:
        return await _release(lease_id)

    @app.post(prefix + "/lease/{lease_id}/release", dependencies=[Depends(require_auth)])
    async def preview_lease_release(lease_id: str) -> Dict[str, Any]:
        return await _release(lease_id)

    @app.get(prefix + "/frame.jpg", dependencies=[Depends(require_auth)])
    async def preview_frame(
        request: Request,
        w: Optional[int] = Query(None, ge=cfg.min_width, le=cfg.max_width,
                                 description="output width in px (downscale only)"),
        q: Optional[int] = Query(None, ge=30, le=95, description="JPEG quality when scaled"),
        lease: Optional[str] = Query(None, description="lease id (optional check)"),
    ) -> Response:
        try:
            data, headers = await service.frame(w, q, _valid_id(lease))
        except FrameUnavailable as exc:
            hdrs = {"Cache-Control": "no-store", "X-Preview-Phase": exc.phase}
            if exc.status == 503:
                hdrs["Retry-After"] = "1"
            raise HTTPException(status_code=exc.status,
                                detail={"phase": exc.phase, "reason": exc.reason},
                                headers=hdrs)
        inm = request.headers.get("if-none-match")
        if inm and inm.strip() == headers["ETag"]:
            return Response(status_code=304, headers=headers)
        return Response(content=data, media_type="image/jpeg", headers=headers)
