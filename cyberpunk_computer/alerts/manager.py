"""
Alert manager: candidates -> fired / shown / cleared alerts.

Per alert type the manager keeps a small runtime and applies, in order:

1. **mode**: ``off`` ignores the type; ``shadow`` and ``on`` both run the
   rules below and log every fire (with the inputs that fired it);
2. **gating**: true speed range, gear, READY, car powered off;
3. **debounce**: the condition must hold ``debounce_s`` before it fires;
4. **latch / re-arm**: after a fire the type stays latched on the event key
   until the condition is absent for ``clear_after_s`` (or the key changes);
5. **suppression**: ``cooldown_s`` after a fire, ``max_per_drive``, a tap
   (acknowledge: that event only), a long press (the rest of the drive);
6. **arbitration** (mode ``on``, kind ``alert``): one alert is shown at a time:
   highest severity, then priority, then the newest; a shown alert stays at
   least ``min_show_s`` (unless a warning pre-empts a lesser one), and clears
   when its condition is gone, after ``max_show_s`` or on acknowledge.

A drive starts when READY (or IG) comes on after at least ``drive_gap_s``
without it; per-drive counters and mutes reset then (the speed-limit
warning comes back on at every drive, as ISA must).

Thread model: :meth:`tick` runs on the backend engine thread; dashboard calls
(:meth:`set_mode`, :meth:`status`, :meth:`acknowledge`) come from the API
thread and take the lock.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .inputs import AlertInputs
from .model import MODES, SEVERITIES, AlertFunction, AlertSpec, Candidate, EvalContext
from .persist import AlertLog, AlertSettings
from .sinks import NotificationSink, NullNotificationSink, NullSoundSink, SoundSink

logger = logging.getLogger(__name__)


@dataclass
class _Runtime:
    spec: AlertSpec
    cand_since: Optional[float] = None
    cand: Optional[Candidate] = None
    absent_since: Optional[float] = None
    latched_key: Optional[str] = None      # fired on this key; waits for the condition to clear
    latched: bool = False
    fired_at: Optional[float] = None
    active: bool = False                   # fired and not cleared (shown or waiting to be shown)
    shown_at: Optional[float] = None
    shown_total: float = 0.0
    drive_count: int = 0
    session_count: int = 0
    acked_keys: Set[str] = field(default_factory=set)
    muted_drive: bool = False
    last_entry: Optional[Dict[str, Any]] = None
    blocked_reason: Optional[str] = None   # why mode "on" is refused (enable_check)


class AlertManager:
    REPUBLISH_S = 2.0          # the shown alert is re-sent this often (MFD reconnects)
    SAVE_EVERY_S = 30.0

    def __init__(self, functions: List[AlertFunction], settings: Optional[AlertSettings] = None,
                 log: Optional[AlertLog] = None, sound: Optional[SoundSink] = None,
                 notify: Optional[NotificationSink] = None,
                 publish: Optional[Callable[[Dict[str, Any]], None]] = None,
                 actuator: Optional[Callable[[str, str, Dict[str, Any]], bool]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 drive_gap_s: float = 60.0) -> None:
        self.functions = list(functions)
        self.settings = settings or AlertSettings(None)
        self.log = log or AlertLog(None)
        self.sound = sound or NullSoundSink()
        self.notify = notify or NullNotificationSink()
        self._publish = publish
        self._actuator = actuator
        self._clock = clock
        self._wall = wall
        self.drive_gap_s = drive_gap_s
        self._lock = threading.RLock()
        self.specs: Dict[str, AlertSpec] = {}
        self.rt: Dict[str, _Runtime] = {}
        self._fn_of: Dict[str, AlertFunction] = {}
        for fn in self.functions:
            for spec in fn.specs:
                if spec.id in self.specs:
                    raise ValueError("duplicate alert id %r" % spec.id)
                if spec.function != fn.name:
                    spec = _with_function(spec, fn.name)
                self.specs[spec.id] = spec
                self.rt[spec.id] = _Runtime(spec)
                self._fn_of[spec.id] = fn
        self.drive_id = 0
        self._powered_since: Optional[float] = None
        self._unpowered_since: Optional[float] = None
        self.drive_started_at: Optional[float] = None
        self.odo_m = 0.0
        self._last_t: Optional[float] = None
        self.shown: Optional[str] = None
        self._shown_seq = 0
        self._last_publish = -1e9
        self._last_save = clock()
        self._dirty = False
        self.last_inputs: Optional[AlertInputs] = None
        self.ticks = 0
        self.errors = 0

    # ── modes ────────────────────────────────────────────────────────────────

    def mode(self, alert_id: str) -> str:
        spec = self.specs.get(alert_id)
        if spec is None:
            return "off"
        m = self.settings.mode(alert_id) or spec.default_mode
        if m not in MODES:
            m = spec.default_mode
        if m == "on" and spec.enable_check is not None and spec.enable_check() is not None:
            return "shadow"           # refused while its required input is unavailable
        return m

    def set_mode(self, alert_id: str, mode: str, by: str = "api") -> Tuple[bool, Optional[str]]:
        """Change and persist a type's mode. Returns (ok, reason)."""
        if alert_id not in self.specs:
            return False, "unknown alert id"
        if mode not in MODES:
            return False, "mode must be one of %s" % ", ".join(MODES)
        spec = self.specs[alert_id]
        if mode == "on" and spec.enable_check is not None:
            why = spec.enable_check()
            if why is not None:
                return False, why
        with self._lock:
            old = self.mode(alert_id)
            self.settings.set_mode(alert_id, mode)
            self.settings.save()
            rt = self.rt[alert_id]
            if mode != "on" and self.shown == alert_id:
                self._clear(rt, self._clock(), "mode")
            self.log.append({"ts": self._wall(), "event": "mode", "id": alert_id,
                             "from": old, "to": mode, "by": by})
        return True, None

    # ── drive bookkeeping ────────────────────────────────────────────────────

    def _drive(self, inp: AlertInputs, now: float) -> None:
        on = inp.ready or inp.ig
        if on:
            if self._powered_since is None:
                gap = None if self._unpowered_since is None else now - self._unpowered_since
                self._powered_since = now
                if self.drive_id == 0 or gap is None or gap >= self.drive_gap_s:
                    self._new_drive(inp, now)
            self._unpowered_since = None
        else:
            if self._powered_since is not None:
                self._unpowered_since = now
            self._powered_since = None

    def _new_drive(self, inp: AlertInputs, now: float) -> None:
        self.drive_id += 1
        self.drive_started_at = now
        for rt in self.rt.values():
            rt.drive_count = 0
            rt.muted_drive = False
            rt.acked_keys.clear()
        ctx = EvalContext(self, now)
        for fn in self.functions:
            try:
                fn.on_drive_start(inp, ctx)
            except Exception:  # noqa: BLE001
                logger.exception("alert function %s: drive start failed", fn.name)
        self.log.append({"ts": self._wall(), "event": "drive", "drive": self.drive_id})

    # ── main loop ────────────────────────────────────────────────────────────

    def tick(self, inp: AlertInputs) -> None:
        now = inp.now
        with self._lock:
            self.ticks += 1
            if self._last_t is not None and inp.real_kmh is not None:
                dt = min(1.0, max(0.0, now - self._last_t))
                self.odo_m += inp.real_kmh / 3.6 * dt
            self._last_t = now
            self.last_inputs = inp
            self._drive(inp, now)
            ctx = EvalContext(self, now)
            results: Dict[str, Optional[Candidate]] = {}
            for fn in self.functions:
                try:
                    out = fn.evaluate(inp, ctx) or {}
                except Exception:  # noqa: BLE001 - one bad function never stops the rest
                    self.errors += 1
                    logger.exception("alert function %s failed", fn.name)
                    out = {}
                for aid in (s.id for s in fn.specs):
                    results[aid] = out.get(aid)
            for aid, rt in self.rt.items():
                self._step(rt, results.get(aid), inp, now)
            self._arbitrate(inp, now)
            if self._dirty and now - self._last_save >= self.SAVE_EVERY_S:
                self._last_save = now
                self._dirty = False
                self.settings.save()

    def _gated(self, spec: AlertSpec, inp: AlertInputs) -> Optional[str]:
        """Reason the type is gated off now, or None."""
        if spec.require_off:
            return "car on" if inp.powered else None
        if spec.require_ready and not (inp.ready or inp.ig):
            return "not ready"
        if spec.gears is not None and inp.gear not in spec.gears:
            return "gear"
        if spec.min_kmh is not None and (inp.real_kmh is None or inp.real_kmh < spec.min_kmh):
            return "slow"
        if spec.max_kmh is not None and (inp.real_kmh is None or inp.real_kmh > spec.max_kmh):
            return "fast"
        return None

    def _step(self, rt: _Runtime, cand: Optional[Candidate], inp: AlertInputs, now: float) -> None:
        spec = rt.spec
        mode = self.mode(spec.id)
        rt.blocked_reason = spec.enable_check() if spec.enable_check is not None else None
        if mode == "off":
            if rt.active:
                self._clear(rt, now, "mode")
            rt.cand_since = rt.cand = None
            rt.latched = False
            return
        if cand is not None and self._gated(spec, inp) is not None:
            cand = None
        if cand is None:
            rt.cand_since = None
            if rt.absent_since is None:
                rt.absent_since = now
            if now - rt.absent_since >= spec.clear_after_s:
                if rt.latched:
                    rt.latched = False
                    rt.latched_key = None
                held = (self.shown == spec.id and rt.shown_at is not None
                        and now - rt.shown_at < spec.min_show_s)
                if rt.active and not held:
                    self._clear(rt, now, "condition")
            return
        rt.absent_since = None
        rt.cand = cand
        if rt.cand_since is None:
            rt.cand_since = now
        if rt.latched and cand.key is not None and cand.key != rt.latched_key:
            rt.latched = False            # a new event (e.g. a new limit) re-arms the type
            if rt.active and self.shown != spec.id:
                rt.active = False
        if rt.latched:
            return
        if now - rt.cand_since < spec.debounce_s:
            return
        if rt.fired_at is not None and now - rt.fired_at < spec.cooldown_s:
            return
        if spec.max_per_drive is not None and rt.drive_count >= spec.max_per_drive:
            return
        if rt.muted_drive or (cand.key is not None and cand.key in rt.acked_keys):
            return
        self._fire(rt, cand, inp, now, mode)

    def _entry(self, rt: _Runtime, event: str, inp: Optional[AlertInputs], **extra) -> Dict[str, Any]:
        spec = rt.spec
        e: Dict[str, Any] = {"ts": round(self._wall(), 3), "event": event, "id": spec.id,
                             "kind": spec.kind, "mode": self.mode(spec.id), "drive": self.drive_id}
        e.update(extra)
        if inp is not None:
            e["inputs"] = inp.compact()
        return e

    def _fire(self, rt: _Runtime, cand: Candidate, inp: AlertInputs, now: float, mode: str) -> None:
        spec = rt.spec
        rt.latched = True
        rt.latched_key = cand.key
        rt.fired_at = now
        rt.drive_count += 1
        rt.session_count += 1
        self.settings.add_count(spec.id)
        self._dirty = True
        text = cand.text or spec.text
        entry = self._entry(rt, "fire", inp, text=text, key=cand.key,
                            severity=cand.severity or spec.severity, evidence=cand.evidence)
        rt.last_entry = self.log.append(entry)
        if mode != "on":
            return
        if spec.kind == "alert":
            rt.active = True
            rt.shown_at = None
            rt.shown_total = 0.0
        elif spec.kind == "event":
            try:
                self.notify.notify(entry)
            except Exception:  # noqa: BLE001
                logger.exception("notification sink failed")

    def _clear(self, rt: _Runtime, now: float, why: str) -> None:
        if not rt.active:
            return
        rt.active = False
        was_shown = self.shown == rt.spec.id
        if was_shown:
            self.shown = None
            try:
                self.sound.stop(rt.spec.id)
            except Exception:  # noqa: BLE001
                logger.exception("sound sink stop failed")
        if rt.shown_at is not None or why == "ack":
            dur = (now - rt.shown_at) if rt.shown_at is not None else 0.0
            self.log.append(self._entry(rt, "clear", None, why=why, shown_s=round(dur, 1)))
        rt.shown_at = None
        if was_shown:
            self._publish_now(now)

    # ── arbitration and publishing ───────────────────────────────────────────

    @staticmethod
    def _rank(rt: _Runtime) -> Tuple[int, int, float]:
        sev = SEVERITIES.get((rt.cand.severity if rt.cand and rt.cand.severity else None)
                             or rt.spec.severity, 1)
        return (sev, rt.spec.priority, rt.fired_at or 0.0)

    def _arbitrate(self, inp: AlertInputs, now: float) -> None:
        cur = self.rt.get(self.shown) if self.shown else None
        if cur is not None:
            if not cur.active:
                self.shown = None
                cur = None
            elif cur.spec.max_show_s is not None and cur.shown_at is not None \
                    and now - cur.shown_at >= cur.spec.max_show_s:
                self._clear(cur, now, "timeout")
                cur = None
            elif self._gated(cur.spec, inp) is not None:
                self._clear(cur, now, "gated")
                cur = None
        waiting = [rt for rt in self.rt.values() if rt.active and rt.spec.id != self.shown]
        if not waiting:
            if cur is not None and now - self._last_publish >= self.REPUBLISH_S:
                self._publish_now(now)
            return
        best = max(waiting, key=self._rank)
        if cur is not None:
            held = cur.shown_at is not None and now - cur.shown_at < cur.spec.min_show_s
            better = self._rank(best) > self._rank(cur)
            preempt = SEVERITIES.get(best.spec.severity, 1) > SEVERITIES.get(cur.spec.severity, 1)
            if not better or (held and not preempt):
                if now - self._last_publish >= self.REPUBLISH_S:
                    self._publish_now(now)
                return
            # a stronger alert takes the line; the weaker one goes back to waiting
            cur.shown_total += now - (cur.shown_at or now)
            cur.shown_at = None
            self.shown = None
        self._show(best, now)

    def _show(self, rt: _Runtime, now: float) -> None:
        self.shown = rt.spec.id
        first = rt.shown_at is None and rt.shown_total == 0.0
        rt.shown_at = now
        self._shown_seq += 1
        if first:
            snd = (rt.cand.sound if rt.cand and rt.cand.sound else None) or rt.spec.sound
            if snd:
                try:
                    self.sound.play(snd, rt.spec.severity, rt.spec.id)
                except Exception:  # noqa: BLE001
                    logger.exception("sound sink failed")
            self.log.append(self._entry(rt, "show", None, sound=snd))
        self._publish_now(now)

    def current(self) -> Optional[Dict[str, Any]]:
        """The alert on the MFD now, as published (None = nothing shown)."""
        rt = self.rt.get(self.shown) if self.shown else None
        if rt is None:
            return None
        spec = rt.spec
        c = rt.cand
        text = (rt.last_entry or {}).get("text") or spec.text
        if c is not None and c.text and rt.latched:
            text = c.text            # live text (e.g. the real speed) while the condition holds
        out = {"id": spec.id, "seq": self._shown_seq, "text": text,
               "severity": (c.severity if c and c.severity else spec.severity),
               "icon": spec.icon, "sound": spec.sound, "ack": spec.ackable,
               "advisory": spec.advisory, "ttl": 5.0}
        if c is not None and c.chip:
            out["chip"] = dict(c.chip)
        return out

    def _publish_now(self, now: float) -> None:
        self._last_publish = now
        if self._publish is None:
            return
        try:
            self._publish({"alert": self.current()})
        except Exception:  # noqa: BLE001
            logger.exception("alert publish failed")

    # ── driver input ─────────────────────────────────────────────────────────

    def acknowledge(self, alert_id: Optional[str] = None, long: bool = False) -> bool:
        """Tap = this event is acknowledged (no repeat until it re-arms);
        long press = the type is muted for the rest of the drive."""
        with self._lock:
            aid = alert_id or self.shown
            rt = self.rt.get(aid) if aid else None
            if rt is None or not rt.active or not rt.spec.ackable:
                return False
            now = self._clock()
            if rt.latched_key is not None:
                rt.acked_keys.add(rt.latched_key)
            if long:
                rt.muted_drive = True
            self.log.append(self._entry(rt, "ack", None, long=long))
            self._clear(rt, now, "ack")
            return True

    # ── automation / notes ───────────────────────────────────────────────────

    def actuate(self, alert_id: str, action: str, evidence: Dict[str, Any]) -> bool:
        rt = self.rt.get(alert_id)
        if rt is None:
            return False
        mode = self.mode(alert_id)
        if mode == "off":
            return False
        executed = False
        if mode == "on" and self._actuator is not None:
            try:
                executed = bool(self._actuator(alert_id, action, evidence))
            except Exception:  # noqa: BLE001
                logger.exception("actuator failed for %s/%s", alert_id, action)
        inp = self.last_inputs
        rt.drive_count += 1
        rt.session_count += 1
        self.settings.add_count(alert_id)
        self._dirty = True
        rt.last_entry = self.log.append(self._entry(rt, "action", inp, action=action,
                                                    executed=executed, evidence=evidence))
        return executed

    def note(self, alert_id: str, what: str, evidence: Dict[str, Any]) -> None:
        rt = self.rt.get(alert_id)
        if rt is None or self.mode(alert_id) == "off":
            return
        self.log.append(self._entry(rt, "note", None, what=what, evidence=evidence))

    def stat(self, name: str, data: Dict[str, Any]) -> None:
        self.log.append({"ts": round(self._wall(), 3), "event": "stat", "id": name,
                         "drive": self.drive_id, "data": data})

    # ── dashboard ────────────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        with self._lock:
            counts = self.settings.counts()
            types = []
            for aid, rt in self.rt.items():
                spec = rt.spec
                d = spec.public()
                d.update({
                    "mode": self.mode(aid),
                    "mode_setting": self.settings.mode(aid) or spec.default_mode,
                    "on_blocked": rt.blocked_reason,
                    "fires_total": counts.get(aid, 0),
                    "fires_drive": rt.drive_count,
                    "fires_session": rt.session_count,
                    "active": rt.active, "shown": self.shown == aid,
                    "muted_drive": rt.muted_drive,
                    "last": rt.last_entry,
                })
                types.append(d)
            fn_status = {}
            for fn in self.functions:
                try:
                    fn_status[fn.name] = fn.status()
                except Exception:  # noqa: BLE001
                    fn_status[fn.name] = {"error": "status failed"}
            return {"types": types, "shown": self.current(), "drive": self.drive_id,
                    "functions": fn_status, "sound": self.sound.status(),
                    "notify": self.notify.status(), "ticks": self.ticks, "errors": self.errors,
                    "log": {"recent": len(self.log.recent), "written": self.log.written,
                            "dropped": self.log.dropped}}


def _with_function(spec: AlertSpec, name: str) -> AlertSpec:
    from dataclasses import replace
    return replace(spec, function=name)
