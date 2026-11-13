"""
Backend wiring of the diagnostics engine: gateway output, ingress hooks, store
actions, persisted auto-clear settings, report files.

``wire_diagnostics(twin, ...)`` returns a :class:`Diagnostics` whose ``tick()``
the engine loop calls once per iteration.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, Optional

from . import report as reportmod
from .actions import DiagCommandAction, SetDiagStateAction
from .autoclear import AutoClearConfig, AutoClearMonitor, validate_config
from .engine import DEFAULT_KINDS, DiagConfig, DiagEngine

logger = logging.getLogger(__name__)


class Diagnostics:
    """The engine, the auto-clear monitor and their glue."""

    def __init__(self, engine: DiagEngine, monitor: AutoClearMonitor, report_dir: str,
                 dispatch: Callable[[Any], None]) -> None:
        self.engine = engine
        self.monitor = monitor
        self.report_dir = report_dir
        self._dispatch = dispatch
        self.last_message: Optional[str] = None

    def tick(self) -> None:
        self.monitor.tick()
        self.engine.tick()

    # ── commands (engine thread, from the store middleware) ───────────────

    def handle(self, action: DiagCommandAction) -> None:
        op, p = action.op, action.params
        eng = self.engine
        if op == "scan":
            kinds = list(p.get("kinds") or DEFAULT_KINDS)
            if p.get("probe") and "probe" not in kinds:
                kinds.append("probe")
            ok, msg = eng.start_scan(p.get("ecus"), kinds)
        elif op == "identify":
            ok, msg = eng.start_identify(p.get("ecus"))
        elif op == "read":
            ok, msg = eng.start_read(p["ecu"], p["item"])
        elif op == "clear":
            ok, msg = eng.start_clear(p["ecu"], confirm=p.get("confirm") is True,
                                      allow_moving=bool(p.get("allow_moving")))
        elif op == "cancel":
            eng.cancel()
            ok, msg = True, "cancelled"
        elif op == "autoclear_set":
            ok, msg = self.monitor.set_config(p)
        else:
            ok, msg = False, "unknown diagnostics op %r" % (op,)
        self.last_message = msg
        (logger.info if ok else logger.warning)("diag %s: %s", op, msg)
        if not ok:
            eng.scan["error"] = msg
            eng.mark_dirty()

    def middleware(self, action: Any, _store: Any) -> None:
        if isinstance(action, DiagCommandAction):
            try:
                self.handle(action)
            except Exception:
                logger.exception("diagnostics command %s failed", action.op)


def state_fields(state: Dict[str, Any]) -> Dict[str, Any]:
    """Engine snapshot -> ``DiagnosticsState`` fields."""
    return {
        "ecus": state.get("ecus"),
        "scan": state.get("scan"),
        "vehicle": state.get("vehicle"),
        "last_read": state.get("last_read"),
        "autoclear": state.get("autoclear"),
        "diag_stats": state.get("stats"),
    }


def _load_autoclear(known_ecus: Any) -> AutoClearConfig:
    try:
        from ..persistence import SettingsManager
        raw = dict(SettingsManager().settings.diagnostics.autoclear or {})
        raw = {k: v for k, v in raw.items()
               if k in ("enabled", "codes", "trigger", "speed_policy", "min_interval_s",
                        "max_per_day", "poll_s")}
        return validate_config(raw, known_ecus)
    except Exception:
        logger.exception("could not load the auto-clear settings; auto-clear stays off")
        return AutoClearConfig()


def _save_autoclear(cfg: Dict[str, Any]) -> None:
    from ..persistence import SettingsManager
    sm = SettingsManager()
    sm.settings.diagnostics.autoclear = dict(cfg)
    sm.save()


def wire_diagnostics(twin: Any, report_dir: Optional[str] = None, live: bool = True,
                     config: Optional[DiagConfig] = None) -> Diagnostics:
    """Create the engine for ``twin`` (io.factory.VirtualTwin) and hook it up.

    ``live=False`` (replay mode) keeps the commands and the state but never
    lets the auto-clear monitor poll.
    """
    from ..io.ports import DEVICE_CAN, OutgoingCommand
    from ..state.actions import SetPendingDTCsAction, SetStoredDTCsAction

    store = twin.store
    ingress = twin.ingress
    report_dir = report_dir or reportmod.default_dir()
    reportmod.set_active_dir(report_dir)

    def send_can(payload: Dict[str, Any]) -> bool:
        out = twin.output_port
        try:
            if hasattr(out, "is_connected") and not out.is_connected():
                return False
            return bool(out.send(OutgoingCommand(device_id=DEVICE_CAN, command_type="req",
                                                 payload=payload)))
        except Exception:
            logger.exception("diagnostics request could not be sent")
            return False

    def publish(state: Dict[str, Any]) -> None:
        store.dispatch(SetDiagStateAction(state_fields(state)))

    def vehicle_speed() -> Optional[float]:
        # Broadcast speed is only trusted while CAN traffic is fresh.
        try:
            age = time.monotonic() - ingress.stats.last_can_mono
        except Exception:
            return None
        if age > 5.0:
            return None
        return float(store.state.vehicle.speed_kmh)

    def on_scan_done(rep: Dict[str, Any]) -> Optional[str]:
        name = reportmod.write_report(report_dir, rep)
        kinds = rep.get("scan", {}).get("kinds", [])
        if "stored" in kinds or "pending" in kinds:
            # Keep the MFD DTC screen's fields in step: every code of every ECU.
            codes = engine.all_codes()
            stored = tuple((c, ecu) for c, ecu, st in codes if "pending" not in st or "stored" in st)
            pending = tuple((c, ecu) for c, ecu, st in codes if "pending" in st)
            mil = any(r.get("mil") for r in engine.results.values())
            store.dispatch(SetStoredDTCsAction(dtcs=stored, mil_on=bool(mil or stored)))
            store.dispatch(SetPendingDTCsAction(dtcs=pending))
        return name

    engine = DiagEngine(send_can, publish=publish, vehicle_speed=vehicle_speed,
                        config=config, on_scan_done=on_scan_done)

    def context() -> Dict[str, Any]:
        s = store.state
        v = s.vehicle
        return {
            "speed_kmh": v.speed_kmh, "ready": v.ready_mode, "ice_running": v.ice_running,
            "gear": getattr(v.gear, "name", str(v.gear)), "rpm": v.rpm,
            "km_since_previous_clear": getattr(v, "odometer_dtc_clear", None),
            "ice_coolant_temp": getattr(v, "ice_coolant_temp", None),
            "trip_fuel_consumed_l": getattr(v, "trip_fuel_consumed", None),
        }

    def log_clear(record: Dict[str, Any]) -> None:
        record.setdefault("context", context())
        reportmod.append_clear(report_dir, record)

    engine.on_clear = log_clear
    monitor = AutoClearMonitor(
        engine,
        config=_load_autoclear(engine.ecus.keys()) if live else AutoClearConfig(),
        save=_save_autoclear, log=log_clear, context=context,
        past_clears=reportmod.read_clears(report_dir),
        active=lambda: vehicle_speed() is not None,     # CAN traffic seen in the last 5 s
    )
    diag = Diagnostics(engine, monitor, report_dir, store.dispatch)
    if not live:
        monitor.tick = lambda now=None: None      # type: ignore[method-assign]

    store.add_middleware(diag.middleware)
    if hasattr(ingress, "set_diag_hooks"):
        ingress.set_diag_hooks(engine.on_gateway_message, engine.on_bus_frame)
    publish(engine.state())
    logger.info("Diagnostics engine wired (%d ECUs, reports in %s, auto-clear %s)",
                len(engine.ecus), os.path.abspath(report_dir),
                "on" if monitor.config.enabled else "off")
    return diag
