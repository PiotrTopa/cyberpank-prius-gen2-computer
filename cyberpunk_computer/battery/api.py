"""REST routes of the battery-health module (mounted by api.app.create_app).

    GET  /api/v1/battery/summary            precomputed indicators, trends, heat maps, lists
    GET  /api/v1/battery/rides/{id}         one ride: features + decimated series
    GET  /api/v1/battery/rests/{id}         one rest: features + 12 V series
    GET  /api/v1/battery/export/{name}.csv  rides | blocks | rests (CSV)
    GET  /api/v1/battery/export/ride/{id}.csv  raw block series of one ride
    GET  /api/v1/battery/export/battery.db  consistent copy of the database
    POST /api/v1/battery/recompute          rebuild the summary now (worker thread)

All read-only except ``recompute`` (which only schedules work at low priority).
503 while the module is not running.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any, Callable, Dict

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response

from . import export, service, summary


def register(app: FastAPI, prefix: str, require_auth: Callable[..., None]) -> None:
    deps = [Depends(require_auth)]

    def svc():
        s = service.active()
        if s is None:
            raise HTTPException(status_code=503, detail="battery module not running")
        return s

    @app.get(prefix + "/summary", dependencies=deps)
    async def battery_summary() -> Dict[str, Any]:
        s = await asyncio.to_thread(svc().get_summary)
        if s is None:
            raise HTTPException(status_code=503, detail="summary not built yet")
        return s

    @app.get(prefix + "/rides/{rid}", dependencies=deps)
    async def battery_ride(rid: int) -> Dict[str, Any]:
        store = svc().store

        def load():
            row = store.get("rides", rid)
            if row is None:
                return None
            rec = store.record(row)
            row.pop("raw", None)
            b = ((store.get_cache("summary") or {}).get("blocks") or {}).get("arrhenius_b")
            return summary.ride_detail(row, rec, arr_b=b or summary.R_ARRHENIUS_B_PRIOR)
        out = await asyncio.to_thread(load)
        if out is None:
            raise HTTPException(status_code=404, detail="no such ride")
        return out

    @app.get(prefix + "/rests/{rid}", dependencies=deps)
    async def battery_rest(rid: int) -> Dict[str, Any]:
        store = svc().store

        def load():
            row = store.get("rests", rid)
            if row is None:
                return None
            rec = store.record(row)
            row.pop("raw", None)
            return summary.rest_detail(row, rec)
        out = await asyncio.to_thread(load)
        if out is None:
            raise HTTPException(status_code=404, detail="no such rest")
        return out

    @app.get(prefix + "/export/battery.db", dependencies=deps)
    async def battery_export_db():
        store = svc().store
        fd, path = tempfile.mkstemp(prefix="battery-", suffix=".db")
        os.close(fd)
        await asyncio.to_thread(store.backup_to, path)
        from starlette.background import BackgroundTask
        return FileResponse(path, media_type="application/vnd.sqlite3", filename="battery.db",
                            background=BackgroundTask(os.unlink, path))

    @app.get(prefix + "/export/ride/{rid}.csv", dependencies=deps)
    async def battery_export_ride(rid: int):
        store = svc().store

        def load():
            row = store.get("rides", rid)
            rec = store.record(row) if row else None
            return export.ride_series_csv(rec) if rec else None
        text = await asyncio.to_thread(load)
        if text is None:
            raise HTTPException(status_code=404, detail="no raw record for this ride")
        return Response(text, media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="battery_ride_%d.csv"' % rid})

    @app.get(prefix + "/export/{name}.csv", dependencies=deps)
    async def battery_export(name: str):
        fn = export.EXPORTS.get(name)
        if fn is None:
            raise HTTPException(status_code=404, detail="exports: " + ", ".join(sorted(export.EXPORTS)))
        text = await asyncio.to_thread(fn, svc().store)
        return Response(text, media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="battery_%s.csv"' % name})

    @app.post(prefix + "/recompute", dependencies=deps)
    async def battery_recompute() -> Dict[str, Any]:
        svc().request_recompute()
        return {"accepted": True}
