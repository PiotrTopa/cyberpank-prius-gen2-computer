"""
REST front of the position feeder (backend.position_feeder):

    GET  /api/v1/position        feeder status (source, connected to mapd, counts, fix age)
    POST /api/v1/position/gnss   raw fixes from the Android head unit: one JSON
                                 object or a JSON array of at most 20; answer
                                 {"accepted": n, "rejected": [{"index", "reason"}], "has_fix": bool}

The head-unit contract (fields, units, clock) is in docs/POSITION_FEEDER.md
section 4. Both routes need the backend's bearer token when one is set.
POST answers 503 ``{"detail": {"reason": "source_not_headunit"}}`` when the
feeder runs another source, 400 for a body that is not JSON / too many fixes;
a fix that fails validation is listed in ``rejected`` (status 200).
"""

from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request


def register(app, feeder, prefix: str, require_auth) -> None:
    """Mount the routes (``prefix`` = ``/api/v1/position``)."""

    @app.get(prefix, dependencies=[Depends(require_auth)])
    async def position_status() -> Dict[str, Any]:
        return feeder.status()

    @app.post(prefix + "/gnss", dependencies=[Depends(require_auth)])
    async def position_gnss(request: Request) -> Dict[str, Any]:
        src = feeder.source
        if not hasattr(src, "ingest"):
            raise HTTPException(status_code=503, detail={
                "reason": "source_not_headunit",
                "message": "the position feeder runs source %r" % src.name})
        raw = await request.body()
        if len(raw) > 64 * 1024:
            raise HTTPException(status_code=413, detail={"reason": "too_large"})
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HTTPException(status_code=400, detail={"reason": "bad_json",
                                                         "message": "JSON object or array expected"})
        if not isinstance(body, (dict, list)):
            raise HTTPException(status_code=400, detail={"reason": "bad_json",
                                                         "message": "JSON object or array expected"})
        try:
            return src.ingest(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"reason": "bad_request", "message": str(exc)})
