"""
Backend wiring of the alert framework (``BackendService`` calls into this).

    store (engine thread) ──middleware──▶ seen markers, MFD acknowledge
    roadsense scene feed (its thread) ──on_scene──▶ latest scene
    engine loop ──tick() at <= 10 Hz──▶ build_inputs ─▶ AlertManager.tick
                                            │
          ┌─────────────────────────────────┼───────────────────────────────┐
          ▼                                 ▼                               ▼
    event "alert" (ZMQ + websocket)   alerts_log.jsonl + metrics       climate press over
    for the MFD banner                ``events`` rows (type alert.*)   AVC-LAN (automation
                                                                       types in mode on only)

The only actions this module can dispatch are the AVC-LAN climate
recirculation set (``SetRecirculationAction``, routed by the egress to the
verified climate remote, which presses only when the live status differs).
Nothing is sent on BEAN or CAN.

Environment:

    BACKEND_ALERTS=0           disable the framework (default on)
    BACKEND_ALERTS_DIR         settings, log, tuning (default: alerts/ next to the metrics DB)
    BACKEND_ALERTS_TUNING      a tuning JSON file (alerts/tuning.py)
    BACKEND_ALERTS_HZ          evaluation rate, 1..20 (default 10)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

from .functions import default_functions
from .inputs import build_inputs
from .manager import AlertManager
from .persist import AlertLog, AlertSettings
from .sinks import make_notification_sink, make_sound_sink
from .tuning import AlertTuning, load as load_tuning

logger = logging.getLogger(__name__)

EVENT_NAME = "alert"
ACK_DEVICE = "mfd"
ACK_EVENT = "alert_ack"

#: automation action name -> what it does (whitelist; nothing else is executable)
ACTIONS = {
    "recirc_on": "climate intake to recirculation (AVC-LAN climate remote)",
    "recirc_off": "climate intake back to fresh air (AVC-LAN climate remote)",
}


def alerts_dir(db_path: Optional[str]) -> Optional[str]:
    env = os.environ.get("BACKEND_ALERTS_DIR")
    if env:
        return env
    if not db_path or db_path == ":memory:":
        return None
    return os.path.join(os.path.dirname(os.path.abspath(db_path)) or ".", "alerts")


class AlertService:
    def __init__(self, store, directory: Optional[str] = None,
                 tuning: Optional[AlertTuning] = None,
                 publish_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 db_events: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
                 hz: float = 10.0, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, functions=None) -> None:
        self.store = store
        self.dir = directory
        self.tuning = tuning or load_tuning(directory)
        self._clock = clock
        self._wall = wall
        self._period = 1.0 / max(1.0, min(20.0, hz))
        self._last_tick = -1e9
        self._publish_event = publish_event
        self.settings = AlertSettings(directory)
        self.log = AlertLog(directory, db_writer=db_events)
        self.sound = make_sound_sink(os.environ.get("BACKEND_ALERTS_SOUND"))
        self.notify = make_notification_sink(os.environ.get("BACKEND_ALERTS_NOTIFY"))
        fns = functions if functions is not None else default_functions(self.tuning, self.settings)
        self.manager = AlertManager(fns, self.settings, self.log, self.sound, self.notify,
                                    publish=self._on_publish, actuator=self._actuate,
                                    clock=clock, wall=wall)
        self._scene_lock = threading.Lock()
        self._scene: Optional[Mapping] = None
        self._scene_t: Optional[float] = None
        self.scenes = 0
        self._seen: Dict[str, Any] = {"hybrid_seq": 0}
        self._avc_frame_time = None
        self._bean_frames = None
        self.actions_executed = 0

    # ── wiring ───────────────────────────────────────────────────────────────

    def attach(self) -> None:
        self.store.add_middleware(self._middleware)
        self.log.start()
        logger.info("alert framework: %d types (%s), dir %s", len(self.manager.specs),
                    ", ".join("%s=%s" % (a, self.manager.mode(a)) for a in self.manager.specs),
                    self.dir or "(memory only)")

    def stop(self) -> None:
        self.settings.save()
        self.log.stop()

    def on_scene(self, scene: Mapping) -> None:
        """Called on the roadsense scene feed thread for every published scene."""
        with self._scene_lock:
            self._scene = scene
            self._scene_t = self._clock()
            self.scenes += 1

    def _middleware(self, action, _store) -> None:
        name = type(action).__name__
        if name == "SetFuelLevelAction":
            self._seen["fuel"] = True
        elif name == "SetDrivetrainTorquesAction":
            self._seen["hybrid_seq"] = self._seen.get("hybrid_seq", 0) + 1
        elif name == "InputEventAction" and getattr(action, "event", "") == ACK_EVENT:
            val = getattr(action, "value", None)
            aid, long = None, False
            if isinstance(val, dict):
                aid, long = val.get("id"), bool(val.get("long"))
            elif isinstance(val, str):
                aid = val
            self.manager.acknowledge(aid, long=long)

    def _climate_marker(self, state) -> None:
        now = self._clock()
        avc_t = getattr(getattr(state, "avc", None), "last_frame_time", None)
        if avc_t != self._avc_frame_time:
            self._avc_frame_time = avc_t
            if getattr(state.climate, "status_source", None) == "avc":
                self._seen["climate_t"] = now
        frames = getattr(getattr(state, "bean", None), "frames_total", None)
        if frames != self._bean_frames:
            self._bean_frames = frames
            if getattr(state.climate, "status_source", None) == "bean":
                self._seen["climate_t"] = now

    # ── loop ─────────────────────────────────────────────────────────────────

    def tick(self, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._last_tick < self._period:
            return
        self._last_tick = now
        state = self.store.state
        self._climate_marker(state)
        with self._scene_lock:
            scene, st = self._scene, self._scene_t
        age = None if st is None else now - st
        inp = build_inputs(state, scene, age, now, self._wall(), self._seen)
        self.manager.tick(inp)

    # ── outputs ──────────────────────────────────────────────────────────────

    def _on_publish(self, payload: Dict[str, Any]) -> None:
        if self._publish_event is not None:
            self._publish_event(EVENT_NAME, payload)

    def _actuate(self, alert_id: str, action: str, evidence: Dict[str, Any]) -> bool:
        if action not in ACTIONS:
            logger.warning("alert %s asked for unknown action %r (refused)", alert_id, action)
            return False
        from ..state.actions import ActionSource, SetRecirculationAction
        on = action == "recirc_on"
        logger.info("alert %s: %s (%s)", alert_id, action, ACTIONS[action])
        self.store.dispatch(SetRecirculationAction(on, source=ActionSource.UI))
        self.actions_executed += 1
        return True

    # ── dashboard ────────────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        st = self.manager.status()
        with self._scene_lock:
            st_t = self._scene_t
        st["scene"] = {"received": self.scenes,
                       "age_s": None if st_t is None else round(self._clock() - st_t, 2)}
        st["actions"] = {"executed": self.actions_executed, "allowed": ACTIONS}
        st["dir"] = self.dir
        return st


def db_event_writer(db) -> Callable[[List[Dict[str, Any]]], None]:
    """Metrics ``events`` rows for fires, actions and acknowledges (own
    connection, opened lazily on the log writer thread)."""
    holder: Dict[str, Any] = {}

    def write(batch: List[Dict[str, Any]]) -> None:
        rows = []
        for e in batch:
            ev = e.get("event")
            if ev not in ("fire", "action", "ack", "mode", "stat"):
                continue
            detail = {"mode": e.get("mode"), "text": e.get("text"), "key": e.get("key"),
                      "action": e.get("action"), "executed": e.get("executed"),
                      "to": e.get("to"), "drive": e.get("drive"), "data": e.get("data")}
            rows.append((float(e.get("ts") or time.time()), "alert.%s.%s" % (ev, e.get("id")),
                         json.dumps({k: v for k, v in detail.items() if v is not None},
                                    separators=(",", ":"))))
        if not rows:
            return
        conn = holder.get("conn")
        if conn is None:
            conn = holder["conn"] = db.connect()
        db.write_events(conn, rows)

    return write


def wire(service, twin, bridge, zmq_srv, db, db_path: Optional[str]) -> Optional[AlertService]:
    """Build and attach the framework for the backend (``BackendService``)."""
    if os.environ.get("BACKEND_ALERTS", "1").strip().lower() in ("0", "false", "off", "no"):
        logger.info("alert framework disabled (BACKEND_ALERTS=0)")
        return None
    d = alerts_dir(db_path)

    def publish(name: str, data: Dict[str, Any]) -> None:
        bridge.push_event(name, data)

    try:
        hz = float(os.environ.get("BACKEND_ALERTS_HZ", "10"))
    except ValueError:
        hz = 10.0
    svc = AlertService(twin.store, d, publish_event=publish,
                       db_events=db_event_writer(db) if db is not None else None, hz=hz)
    svc.attach()
    return svc
