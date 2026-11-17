"""
Turn-signal intent from the raw turn-lamp levels.

No bus on this car carries the turn signals (docs/BEAN_SIGNALS.md §6,
2026-11-17 test): with IG on, left/right blinking changed no BEAN frame and
no broadcast CAN frame, and hazards with the car off did not even wake BEAN.
A source therefore has to report the raw lamp levels (a hard-wired tap of
the two lamp circuits, a satellite, or vision). This class turns those blink
levels into a steady driver intent that consumers (roadsense: which junction
arm / exit) can read without seeing the blink:

* ``vehicle.turn_lamp_left`` / ``turn_lamp_right``: raw lamp level as last
  reported (they blink); None = no source;
* ``vehicle.turn_signal``: ``"left"`` / ``"right"`` / ``"hazard"`` / None.
  A side stays signalled for :data:`HOLD_S` after its lamp was last lit, so
  the dark half of the blink does not drop it. Both sides lit together
  (rising edges within :data:`PAIR_S`) = hazard; a left -> right change is
  not mistaken for hazards while the old side's hold runs out;
* ``vehicle.turn_signal_source``: who reported the lamps; None when no
  source has reported for :data:`STALE_S` (then everything is None:
  unknown, not "off").
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..state.actions import Action, SetPassiveSignalsAction

#: A side counts as signalled for this long after its lamp was last seen
#: lit. ECE R6/R48: 90 +- 30 flashes/min, so the dark phase is at most
#: ~0.7 s (60/min, 30 % duty); 1.0 s bridges it with margin.
HOLD_S = 1.0
#: Both lamps switching on within this window = hazard (one flasher drives
#: both sides at once for hazards).
PAIR_S = 0.25
#: No lamp report for this long -> the whole signal is unknown (None).
STALE_S = 2.0


class TurnSignalTracker:
    def __init__(self, hold_s: float = HOLD_S, pair_s: float = PAIR_S,
                 stale_s: float = STALE_S) -> None:
        self.hold_s = hold_s
        self.pair_s = pair_s
        self.stale_s = stale_s
        self._lamp: Dict[str, Optional[bool]] = {"left": None, "right": None}
        self._lit_t: Dict[str, Optional[float]] = {"left": None, "right": None}
        self._rise_t: Dict[str, Optional[float]] = {"left": None, "right": None}
        self._seen: Optional[float] = None
        self._source: Optional[str] = None
        self._sent: Dict[str, Any] = {}

    # ── inputs ───────────────────────────────────────────────────────────
    def observe(self, left: Optional[bool], right: Optional[bool], t: float,
                source: str) -> List[Action]:
        """One report of the lamp levels at time ``t`` (monotonic s)."""
        for side, lit in (("left", left), ("right", right)):
            if lit is None:
                continue
            if lit:
                if not self._lamp[side]:
                    self._rise_t[side] = t
                self._lit_t[side] = t
            self._lamp[side] = bool(lit)
        self._seen = t
        self._source = source
        return self._emit(t)

    def tick(self, t: float) -> List[Action]:
        """Let holds run out and a silent source go stale."""
        return self._emit(t)

    # ── derived ──────────────────────────────────────────────────────────
    def _active(self, side: str, t: float) -> bool:
        lit = self._lit_t[side]
        return bool(self._lamp[side]) or (lit is not None and t - lit <= self.hold_s)

    def intent(self, t: float) -> Optional[str]:
        """``"left"`` / ``"right"`` / ``"hazard"`` / None at time ``t``."""
        if self._seen is None or t - self._seen > self.stale_s:
            return None
        left, right = self._active("left", t), self._active("right", t)
        if left and right:
            rl, rr = self._rise_t["left"], self._rise_t["right"]
            if rl is not None and rr is not None and abs(rl - rr) <= self.pair_s:
                return "hazard"
            # a side change: the newer side wins while the old hold runs out
            return "left" if (rl or 0.0) >= (rr or 0.0) else "right"
        if left:
            return "left"
        if right:
            return "right"
        return None

    def _emit(self, t: float) -> List[Action]:
        if self._seen is None or t - self._seen > self.stale_s:
            fields = {"turn_signal": None, "turn_lamp_left": None,
                      "turn_lamp_right": None, "turn_signal_source": None}
        else:
            fields = {"turn_signal": self.intent(t), "turn_lamp_left": self._lamp["left"],
                      "turn_lamp_right": self._lamp["right"], "turn_signal_source": self._source}
        changed = {k: v for k, v in fields.items() if k not in self._sent or self._sent[k] != v}
        if not changed:
            return []
        self._sent.update(changed)
        return [SetPassiveSignalsAction(vehicle=changed)]
