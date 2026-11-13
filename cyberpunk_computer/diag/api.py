"""HTTP routes of the diagnostics engine (mounted by api.app.create_app).

    GET /api/v1/diag/report[?name=...]   newest (or named) scan report, full JSON
    GET /api/v1/diag/reports             list of stored reports, newest first
    GET /api/v1/diag/clears[?limit=N]    clear log (manual + automatic clears)
    GET /api/v1/diag/ecus                the ECU registry (static)

Live results are in the state (``state.diagnostics.ecus`` / ``.scan`` /
``.autoclear``); commands are the ``diag_*`` entries of POST /commands/{name}.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query

from . import report as reportmod
from .ecus import ECUS


def register(app: FastAPI, prefix: str, require_auth: Callable[..., None]) -> None:
    deps = [Depends(require_auth)]

    def directory() -> str:
        return reportmod.active_dir()

    @app.get(prefix + "/report", dependencies=deps)
    async def diag_report(name: Optional[str] = Query(None, description="report file name")) -> Dict[str, Any]:
        rep = await asyncio.to_thread(reportmod.read_report, directory(), name)
        if rep is None:
            raise HTTPException(status_code=404, detail="no diagnostics report" if name is None
                                else "no such report")
        return rep

    @app.get(prefix + "/reports", dependencies=deps)
    async def diag_reports() -> Dict[str, Any]:
        return {"reports": await asyncio.to_thread(reportmod.list_reports, directory())}

    @app.get(prefix + "/clears", dependencies=deps)
    async def diag_clears(limit: int = Query(200, ge=1, le=2000)) -> Dict[str, Any]:
        return {"clears": await asyncio.to_thread(reportmod.read_clears, directory(), limit)}

    @app.get(prefix + "/ecus", dependencies=deps)
    async def diag_ecus() -> Dict[str, Any]:
        return {"ecus": [
            {"key": e.key, "name": e.name,
             "address": "0x%03X" % e.tx_id if e.tx_id else None,
             "response_address": "0x%03X" % e.rx_id if e.rx_id else None,
             "sub_address": None if e.sub_addr is None else "0x%02X" % e.sub_addr,
             "bus": e.bus, "reachable": e.reachable, "probe": e.probe,
             "protocol": e.protocol_label, "confidence": e.confidence,
             "items": sorted(e.items), "can_clear": bool(e.clear_requests()), "note": e.note}
            for e in ECUS.values()]}
