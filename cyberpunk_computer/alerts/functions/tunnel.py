"""
5. Tunnel auto-recirculation (automation; the only actuation in the framework).

Input: scene ``map.tunnel`` = roadsense's fused tunnel fact (``in``,
``entry_m``, ``src``: map / vision / map+vision). Entry when the map says the
car is in a tunnel or the portal is within 120 m (map usable, match >= 0.8,
held 1 s), or vision alone says tunnel for 3 s (envd label ``tunnel`` with
confidence >= 0.5, roadsense's own rule). Exit when the fact is false for 5 s.

Action (mode ``on`` only; ``shadow`` logs what it would press):

* press recirculation only if the climate status (AVC E0 5D F3, fresh within
  15 s) says the system is on and the intake is FRESH, front defrost is off,
  and it is not cold with the A/C off (fogging risk); the AVC climate remote
  presses only when the live status differs (double check);
* restore FRESH 20 s after the exit, only if the status still shows our
  recirculation and nobody touched the climate;
* step back for the rest of the tunnel when the driver touches the climate
  (any status change that our press does not explain, or a BEAN FE:FB climate
  key frame outside the grace window after our press); never press again in
  that tunnel and never restore;
* no confirmation of our press within 6 s -> step back (no retries);
* rate limits: 30 s between our presses, at most 12 per hour; never keep our
  recirculation longer than 10 min (restored then even inside a tunnel).
"""

from __future__ import annotations

import collections
from typing import Deque, Dict, Optional, Tuple

from ..inputs import AlertInputs
from ..model import AlertFunction, AlertSpec, Candidate, EvalContext

ID = "tunnel_recirc"


def _touch_tuple(c: Dict) -> Tuple:
    base = (c.get("recirculation"), c.get("fresh_air"), c.get("ac_on"), c.get("auto_mode"),
            c.get("defrost"), c.get("rear_defogger"), c.get("system_on"), c.get("target_temp"))
    if not c.get("auto_mode"):            # in AUTO the ECU moves fan and vents itself
        base += (c.get("fan_speed"), c.get("air_direction"))
    return base


class TunnelRecircFunction(AlertFunction):
    name = "tunnel_recirc"

    def __init__(self, tuning, settings=None):
        self.t = tuning.tunnel
        self.specs = (AlertSpec(
            id=ID, title="Tunnel auto-recirculation", text="RECIRC: TUNNEL", kind="automation",
            severity="info", icon="recirc",
            description="Recirculation on in tunnels (only from fresh air with the climate on), "
                        "fresh air restored after the exit; steps back when the driver touches the "
                        "climate. AVC-LAN climate remote.",
            needs=("scene map.tunnel (fused)", "climate.* (AVC E0 5D F3)", "climate.outside_temp",
                   "bean climate_buttons (FE:FB)")),)
        self.state = "idle"
        self.map_since: Optional[float] = None
        self.vis_since: Optional[float] = None
        self.out_since: Optional[float] = None
        self.unknown_since: Optional[float] = None
        self.inside = False
        self.press_t: Optional[float] = None
        self.press_wall: Optional[float] = None
        self.confirmed = False
        self.snapshot: Optional[Tuple] = None
        self.exit_t: Optional[float] = None
        self.presses: Deque[float] = collections.deque(maxlen=64)
        self.tunnels = 0
        self.last: Optional[str] = None

    # ── tunnel detection ─────────────────────────────────────────────────────

    def _raw(self, inp: AlertInputs) -> Tuple[Optional[bool], Optional[bool]]:
        """(map says in / portal close, vision says in); None = unknown."""
        tn = inp.tunnel
        if tn is None or not inp.scene_fresh:
            return None, None
        map_ok = bool(tn.get("map_usable")) and (inp.map_conf or 0.0) >= self.t.min_match_conf
        src = str(tn.get("src") or "")
        inside = bool(tn.get("in"))
        entry = tn.get("entry_m")
        m = map_ok and ((inside and "map" in src)
                        or (isinstance(entry, (int, float)) and 0 <= entry <= self.t.entry_m))
        v = inside and ("vision" in src)
        return m, v

    def _detect(self, inp: AlertInputs, now: float) -> Optional[bool]:
        m, v = self._raw(inp)
        if m is None:
            if self.unknown_since is None:
                self.unknown_since = now
            return None
        self.unknown_since = None
        self.map_since = (self.map_since or now) if m else None
        self.vis_since = (self.vis_since or now) if v else None
        on = ((self.map_since is not None and now - self.map_since >= self.t.map_in_stable_s)
              or (self.vis_since is not None and now - self.vis_since >= self.t.vision_stable_s))
        if on:
            self.out_since = None
            self.inside = True
        elif not m and not v:
            self.out_since = self.out_since or now
            if now - self.out_since >= self.t.exit_stable_s:
                self.inside = False
        return self.inside

    # ── guards ───────────────────────────────────────────────────────────────

    def _skip_reason(self, inp: AlertInputs, now: float) -> Optional[str]:
        c = inp.climate
        t = self.t
        if not inp.climate_live:
            return "no climate status"
        if not c.get("system_on"):
            return "climate off"
        if c.get("recirculation") or not c.get("fresh_air"):
            return "already recirculating"
        if t.skip_if_defrost and c.get("defrost"):
            return "front defrost on"
        if inp.outside_c is not None and inp.outside_c < t.skip_below_c and not c.get("ac_on"):
            return "cold with A/C off (fogging risk)"
        if inp.real_kmh is None or inp.real_kmh < t.min_speed_kmh:
            return "slow"
        if self.presses and now - self.presses[-1] < t.min_press_interval_s:
            return "rate limit (interval)"
        if sum(1 for p in self.presses if now - p < 3600.0) >= t.max_presses_per_hour:
            return "rate limit (per hour)"
        return None

    def _touched(self, inp: AlertInputs, now: float) -> Optional[str]:
        if self.press_t is not None and now - self.press_t < self.t.touch_grace_s:
            return None
        bts = inp.climate_buttons_ts
        if (bts is not None and self.press_wall is not None
                and bts > self.press_wall + self.t.touch_grace_s):
            return "climate key pressed (BEAN FE:FB)"
        if self.snapshot is not None and _touch_tuple(inp.climate) != self.snapshot:
            return "climate status changed"
        return None

    def _evidence(self, inp: AlertInputs, extra: Dict = None) -> Dict:
        c = inp.climate
        ev = {"tunnel": dict(inp.tunnel or {}), "map_conf": inp.map_conf, "recirc": c.get("recirculation"),
              "fresh": c.get("fresh_air"), "ac": c.get("ac_on"), "defrost": c.get("defrost"),
              "outside_c": inp.outside_c, "real_kmh": inp.real_kmh}
        if extra:
            ev.update(extra)
        return ev

    # ── state machine ────────────────────────────────────────────────────────

    def evaluate(self, inp: AlertInputs, ctx: EvalContext) -> Dict[str, Optional[Candidate]]:
        now = inp.now
        mode = ctx.mode(ID)
        if mode == "off":
            self.state = "idle"
            return {}
        inside = self._detect(inp, now)
        st = self.state
        if st == "idle":
            if inside:
                self.tunnels += 1
                why = self._skip_reason(inp, now)
                if why is not None:
                    ctx.note(ID, "skipped: %s" % why, self._evidence(inp))
                    self.last = "skipped: %s" % why
                    self.state = "skipped"
                    return {}
                self.presses.append(now)
                executed = ctx.actuate(ID, "recirc_on", self._evidence(inp))
                self.press_t, self.press_wall = now, inp.wall
                self.confirmed, self.snapshot = False, None
                self.state = "ours" if executed else "virtual"
                self.last = "recirc on" + ("" if executed else " (shadow)")
            return {}
        if st in ("skipped", "stepped_back"):
            if inside is False:
                self.state = "idle"
            return {}
        if st in ("ours", "restore_ours"):
            c = inp.climate
            if not self.confirmed:
                if c.get("recirculation") and not c.get("fresh_air"):
                    self.confirmed = True
                    self.snapshot = _touch_tuple(c)
                elif now - (self.press_t or now) > self.t.feedback_timeout_s:
                    ctx.note(ID, "no confirmation of recirculation: stepped back", self._evidence(inp))
                    self.last = "no feedback"
                    self.state = "stepped_back" if inside else "idle"
                    return {}
            else:
                why = self._touched(inp, now)
                if why is not None:
                    ctx.note(ID, "driver touched the climate (%s): stepped back" % why, self._evidence(inp))
                    self.last = "stepped back"
                    self.state = "stepped_back" if inside else "idle"
                    return {}
        holding = st in ("ours", "virtual")
        capped = self.press_t is not None and now - self.press_t >= self.t.max_recirc_s
        if holding:
            stale = self.unknown_since is not None and now - self.unknown_since > 60.0
            if capped or inside is False or stale:
                self.exit_t = now
                self.state = "restore_ours" if st == "ours" else "restore_virtual"
                if capped:
                    self.exit_t = now - self.t.restore_after_s          # restore at once
            return {}
        # restore_*: waiting after the exit
        if inside and not capped:
            self.state = "ours" if st == "restore_ours" else "virtual"   # chained tunnels
            return {}
        if now - (self.exit_t or now) >= self.t.restore_after_s:
            if st == "restore_ours" and not (inp.climate.get("recirculation") and not inp.climate.get("fresh_air")):
                ctx.note(ID, "restore skipped: intake no longer recirculation", self._evidence(inp))
                self.last = "restore skipped"
            else:
                executed = ctx.actuate(ID, "recirc_off", self._evidence(inp, {"capped": capped}))
                self.presses.append(now)
                self.last = "fresh restored" + ("" if executed else " (shadow)")
            self.state = "idle" if not inside else "skipped"
            self.press_t = None
            self.snapshot = None
        return {}

    def status(self):
        import time as _t
        now = _t.monotonic()
        return {"state": self.state, "inside": self.inside, "tunnels": self.tunnels, "last": self.last,
                "presses_last_hour": sum(1 for p in self.presses if now - p < 3600.0)}
