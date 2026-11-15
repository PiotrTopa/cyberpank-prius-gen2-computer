"""
9. Parked-car guardian (events only; no new battery drain).

Runs on what the backend already receives while the car is off: BEAN frames
(the body bus wakes on events; the powerbox receiver is on permanent power),
the powerbox INA219 telemetry, and the alert tick that is running anyway. No
polling, no wake-ups, no transmit.

Car off = not READY, not IG, not ACC. Per parking (one parking = one power-off)
each type is recorded at most once:

``left_unlocked``     doors not locked (BEAN FE:B6 b0) 5 min after power-off
``door_open_parked``  a door / the hatch open (FE:B6 b1, FE:7F b2) for 2 min
``lamps_left_on``     tail or low-beam bit (FE:7F b0) still set 1 min after
                      power-off (the bit stays set while the switch is on;
                      the car has no chime frame for it)
``low_12v``           resting voltage below 12.2 V for 10 min, from 30 min
                      after power-off (INA219 on the computer's own 12 V feed:
                      includes the computer's draw and the wiring drop)
``intrusion``         a door opens while the doors are locked and no unlock
                      event (FE:E1 b2) came in the minute before

The BEAN values are the last frames heard: a bus that went to sleep keeps its
last state, which is exactly the "left on / left open" case.

Delivery: the notification sink (alerts/sinks.py, null implementation: no push
channel exists) in mode ``on``; always the alert log and the dashboard.
Recommended push channel: a small outbound notifier on the phone over LTE
(ntfy-style HTTP push to a self-hosted topic on the WireGuard hub), which works
when the laptop is off; the laptop's WhatsApp bridge is reachable only while
the laptop runs, so at most a secondary sink.
"""

from __future__ import annotations

import collections
from typing import Deque, Dict, Optional

from ..inputs import AlertInputs
from ..model import AlertFunction, AlertSpec, Candidate, EvalContext

UNLOCKED = "left_unlocked"
DOOR = "door_open_parked"
LAMPS = "lamps_left_on"
LOW12 = "low_12v"
INTRUSION = "intrusion"
UNLOCK_EVENTS = ("remote_unlock", "smart_unlock")


def _ev_spec(aid: str, title: str, text: str, icon: str, desc: str, needs) -> AlertSpec:
    return AlertSpec(id=aid, title=title, text=text, kind="event", severity="notice", icon=icon,
                     require_off=True, clear_after_s=5.0, cooldown_s=0.0, max_show_s=None,
                     min_show_s=0.0, ackable=False, description=desc, needs=needs)


class GuardianFunction(AlertFunction):
    name = "parked_guardian"

    def __init__(self, tuning, settings=None):
        t = self.t = tuning.guardian
        self.specs = (
            _ev_spec(UNLOCKED, "Car left unlocked", "LEFT UNLOCKED", "door",
                     "Doors not locked %.0f min after power-off." % (t.unlocked_after_s / 60),
                     ("bean doors_locked (FE:B6)",)),
            _ev_spec(DOOR, "Door or hatch left open", "DOOR OPEN", "door",
                     "A door / the hatch open %.0f min after power-off." % (t.door_open_after_s / 60),
                     ("bean any_door_open (FE:B6), door_open (FE:7F)",)),
            _ev_spec(LAMPS, "Lamps left on", "LAMPS ON", "info",
                     "Tail or low-beam bit still set %.0f min after power-off." % (t.lamps_after_s / 60),
                     ("bean lamp_tail / lamp_head (FE:7F)",)),
            _ev_spec(LOW12, "Low 12 V resting voltage", "12V LOW", "info",
                     "Below %.1f V for %.0f min, from %.0f min after power-off (INA219)."
                     % (t.low_12v_v, t.low_12v_hold_s / 60, t.low_12v_rest_s / 60),
                     ("powerbox.system_voltage (INA219)",)),
            _ev_spec(INTRUSION, "Door opened while locked", "DOOR OPENED (LOCKED)", "door",
                     "A door opened while locked with no unlock event in the minute before.",
                     ("bean doors_locked, any_door_open, lock_event (FE:E1)",)),
        )
        self.off_since: Optional[float] = None
        self.parking = 0
        self.fired: set = set()
        self.done: set = set()
        self.low_since: Optional[float] = None
        self.prev_door: Optional[bool] = None
        self.intrusion_key: Optional[str] = None
        self.events: Deque[Dict] = collections.deque(maxlen=50)

    def evaluate(self, inp: AlertInputs, ctx: EvalContext) -> Dict[str, Optional[Candidate]]:
        t = self.t
        now = inp.now
        out: Dict[str, Optional[Candidate]] = {k: None for k in (UNLOCKED, DOOR, LAMPS, LOW12, INTRUSION)}
        if inp.powered:
            self.off_since = None
            self.low_since = None
            self.prev_door = inp.door_open
            return out
        if self.off_since is None:
            self.off_since = now
            self.parking += 1
            self.fired = set()
            self.done = set()
            self.intrusion_key = None
        off = now - self.off_since
        ev = {"off_s": round(off), "locked": inp.doors_locked, "door_open": inp.door_open,
              "doors": list(inp.door_open_which), "tail": inp.lamp_tail, "head": inp.lamp_head,
              "v12": inp.v12, "lock_event": inp.lock_event}
        conds: Dict[str, Optional[str]] = {k: None for k in out}
        if inp.bean_frames:
            if off >= t.unlocked_after_s and inp.doors_locked is False:
                conds[UNLOCKED] = "LEFT UNLOCKED"
            if off >= t.door_open_after_s and inp.door_open:
                conds[DOOR] = "%s OPEN" % ("/".join(inp.door_open_which).upper() or "DOOR")
            if off >= t.lamps_after_s and (inp.lamp_tail or inp.lamp_head):
                conds[LAMPS] = "LAMPS ON"
            # intrusion: door closed -> open edge while locked, no recent unlock
            if self.prev_door is False and inp.door_open and inp.doors_locked:
                recent_unlock = (inp.lock_event in UNLOCK_EVENTS and inp.lock_event_ts is not None
                                 and inp.wall - inp.lock_event_ts <= t.intrusion_unlock_window_s)
                if not recent_unlock:
                    self.intrusion_key = "park%d@%d" % (self.parking, int(now))
            if self.intrusion_key is not None and not inp.door_open:
                self.intrusion_key = None
            if self.intrusion_key is not None:
                conds[INTRUSION] = "DOOR OPENED (LOCKED)"
            self.prev_door = inp.door_open
        if inp.v12 is not None and inp.powerbox_fresh and off >= t.low_12v_rest_s:
            if inp.v12 < t.low_12v_v:
                self.low_since = self.low_since or now
                if now - self.low_since >= t.low_12v_hold_s:
                    conds[LOW12] = "12V LOW %.2fV" % inp.v12
            else:
                self.low_since = None
        for aid, text in conds.items():
            key = self.intrusion_key if aid == INTRUSION else "park%d" % self.parking
            if text is None:
                if any(f[0] == aid for f in self.fired):
                    self.done.update(f for f in self.fired if f[0] == aid)
                continue
            if (aid, key) in self.done:
                continue                 # once per parking (per intrusion edge)
            if (aid, key) not in self.fired:
                self.fired.add((aid, key))
                self.events.append({"ts": round(inp.wall, 1), "id": aid, "text": text})
            out[aid] = Candidate(text=text, key=key, evidence=ev)
        return out

    def status(self):
        import time as _t
        return {"off_for_s": None if self.off_since is None else round(_t.monotonic() - self.off_since),
                "parking": self.parking, "events": list(self.events)}
