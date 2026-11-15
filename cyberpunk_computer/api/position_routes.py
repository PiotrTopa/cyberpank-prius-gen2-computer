"""
REST front of the position feeder (backend.position_feeder):

    GET  /api/v1/position        feeder status (source, connected to mapd, counts, fix age)
                                 plus, REST only, ``raw_fix`` (newest raw fix: lat, lon,
                                 hacc_m, age_s, source) and ``pack_file`` (the map pack
                                 file roadsense loads: path, size_bytes, mtime)
    POST /api/v1/position/synthetic  {"on": true|false}: start / stop a run-time
                                 synthetic drive (test only; refused with 409
                                 {"detail": {"reason": ...}} unless parked and
                                 roadsense running, 403 when the control is off)
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
import os
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request


DEFAULT_PACK = "/var/lib/roadsense/maps/current.rsmap"


def pack_file(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Size and date of the map pack roadsense's mapd loads (the pack's
    ``map.pack`` carries region and OSM date but no size); None when absent."""
    path = path or os.environ.get("BACKEND_ROADSENSE_MAP_PACK") or DEFAULT_PACK
    try:
        real = os.path.realpath(path)
        st = os.stat(real)
    except OSError:
        return None
    return {"path": path, "file": os.path.basename(real), "size_bytes": st.st_size,
            "mtime": round(st.st_mtime, 1)}


def register(app, feeder, prefix: str, require_auth) -> None:
    """Mount the routes (``prefix`` = ``/api/v1/position``)."""

    @app.get(prefix, dependencies=[Depends(require_auth)])
    async def position_status() -> Dict[str, Any]:
        st = feeder.status()
        st["raw_fix"] = feeder.raw_fix()
        st["pack_file"] = pack_file()
        return st

    @app.post(prefix + "/synthetic", dependencies=[Depends(require_auth)])
    async def position_synthetic(request: Request) -> Dict[str, Any]:
        try:
            body = json.loads((await request.body()).decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("on"), bool):
            raise HTTPException(status_code=400, detail={"reason": "bad_request",
                                                         "message": 'body {"on": true|false} expected'})
        if not body["on"]:
            feeder.stop_synthetic("stopped")
            return feeder.status()
        ok, why = feeder.start_synthetic(by="dashboard")
        if not ok:
            code = 403 if why in ("control_disabled", "configured") else 409
            raise HTTPException(status_code=code, detail={"reason": why})
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
