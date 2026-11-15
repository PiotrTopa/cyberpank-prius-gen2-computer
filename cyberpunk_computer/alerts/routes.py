"""
REST front of the alert framework (same authentication as every /api/v1 route):

    GET  /api/v1/alerts                    types (mode, counts, gating, last fire), the
                                           shown alert, function status, sinks
    GET  /api/v1/alerts/log?limit=&id=&event=   recent log entries, newest first
    GET  /api/v1/alerts/log/export?since=  the whole log as JSON lines (download)
    POST /api/v1/alerts/{id}/mode          {"mode": "off" | "shadow" | "on"}
                                           409 {"detail": {"reason"}} when "on" is refused
                                           (a required input is not available)
    POST /api/v1/alerts/ack                {"id": optional, "long": bool}
    POST /api/v1/alerts/service/{item}/done   {"km": optional (default: odometer now)}
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse


async def _body(request: Request) -> Dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail={"reason": "bad_json"})
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={"reason": "bad_json"})
    return body


def register(app, service, prefix: str, require_auth) -> None:
    """Mount the routes (``prefix`` = ``/api/v1/alerts``)."""
    m = service.manager

    @app.get(prefix, dependencies=[Depends(require_auth)])
    async def alerts_status() -> Dict[str, Any]:
        return service.status()

    @app.get(prefix + "/log", dependencies=[Depends(require_auth)])
    async def alerts_log(limit: int = Query(100, ge=1, le=500), id: Optional[str] = None,
                         event: Optional[str] = None) -> Dict[str, Any]:
        return {"entries": service.log.recent_entries(limit, id, event)}

    @app.get(prefix + "/log/export", dependencies=[Depends(require_auth)])
    async def alerts_export(since: Optional[float] = None):
        return StreamingResponse(service.log.iter_export(since), media_type="application/x-ndjson",
                                 headers={"Content-Disposition": 'attachment; filename="alerts_log.jsonl"'})

    @app.post(prefix + "/ack", dependencies=[Depends(require_auth)])
    async def alerts_ack(request: Request) -> Dict[str, Any]:
        body = await _body(request)
        ok = m.acknowledge(body.get("id"), long=bool(body.get("long")))
        return {"acknowledged": ok}

    @app.post(prefix + "/service/{item}/done", dependencies=[Depends(require_auth)])
    async def alerts_service_done(item: str, request: Request) -> Dict[str, Any]:
        body = await _body(request)
        fn = next((f for f in m.functions if hasattr(f, "mark_service_done")), None)
        if fn is None:
            raise HTTPException(status_code=404, detail={"reason": "no maintenance function"})
        km = body.get("km")
        if km is not None and (isinstance(km, bool) or not isinstance(km, (int, float))):
            raise HTTPException(status_code=400, detail={"reason": "km must be a number"})
        ok, why = fn.mark_service_done(item, km)
        if not ok:
            raise HTTPException(status_code=409, detail={"reason": why})
        service.settings.save()
        return {"ok": True, "service": fn.status().get("services")}

    @app.post(prefix + "/{alert_id}/mode", dependencies=[Depends(require_auth)])
    async def alerts_mode(alert_id: str, request: Request) -> Dict[str, Any]:
        body = await _body(request)
        if alert_id not in m.specs:
            raise HTTPException(status_code=404, detail={"reason": "unknown alert id"})
        ok, why = m.set_mode(alert_id, str(body.get("mode", "")), by="dashboard")
        if not ok:
            code = 400 if why and why.startswith("mode must") else 409
            raise HTTPException(status_code=code, detail={"reason": why})
        return {"id": alert_id, "mode": m.mode(alert_id)}
