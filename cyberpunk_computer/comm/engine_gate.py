"""
Engine-running gate: is the ICE turning, from the most reliable fresh source.

CAN 0x038 b1 > 0 is NOT an engine-running flag: on the 2026-11-13 drive it
read 0 for 32 % of the time the engine was turning (low load, e.g. 1300 rpm
warm-up). The AVC-LAN energy-monitor engine arrow (B9) has the same meaning
("engine delivering power"). The gate uses, best first (signal
``engine_running`` in the shared :class:`SourceArbiter`):

  source    evidence                                         fresh for
  rpm       engine rpm > ENGINE_RUNNING_RPM (BEAN FE:26, or  2.0 s
            OBD 010C while BEAN is stale; the value the rpm
            arbitration accepted)
  bean      BEAN 98:27 b0: 0x01 READY, 0x08 ICE stopped      3.0 s
            -> running = READY and not stopped
  bean_inj  BEAN 62:20 present (sent only while the engine   2.5 s
            runs) -> running
  can       CAN 0x520 present (sent only while the engine    2.0 s
            runs) or 0x038 b1 > 0 -> running; 0x038 frames
            arriving with 0x520 silent > 2 s -> stopped
  avc       AVC B9 engine arrow (probe: AVC energy fresh)    AVC window

Agreement with OBD rpm > 400 on that drive: FE:26 99.5 %, 98:27 98.8 %,
0x520 presence 98.5 %, 0x038 b1 68 %.

With no source fresh the gate gives no decision and the state keeps its
last value.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from ..state.fuel_model import ENGINE_RUNNING_RPM
from .source_priority import SourceArbiter

SIGNAL = "engine_running"

#: 0x520 counts as present this long after its last frame (period ~0.92 s).
CAN_INJ_PRESENT_S = 2.0


class EngineRunningGate:
    def __init__(self, arbiter: SourceArbiter, rpm_threshold: float = ENGINE_RUNNING_RPM) -> None:
        self._arb = arbiter
        self.rpm_threshold = rpm_threshold
        self._value: Dict[str, bool] = {}
        self._t_520: Optional[float] = None
        #: last rpm the rpm arbitration accepted (also before it reaches the
        #: store, for a fuel word in the same batch)
        self.rpm: Optional[float] = None

    def _now(self, now: Optional[float]) -> float:
        return self._arb._clock() if now is None else now

    def _set(self, source: str, running: bool, now: Optional[float]) -> None:
        self._value[source] = bool(running)
        self._arb.offer(SIGNAL, source, self._now(now))

    # ── observations ──────────────────────────────────────────────────────
    def observe_rpm(self, rpm: float, now: Optional[float] = None) -> None:
        """An rpm value the rpm arbitration accepted (BEAN or OBD)."""
        self.rpm = rpm
        self._set("rpm", rpm is not None and rpm > self.rpm_threshold, now)

    def observe_bean_9827(self, b0: int, now: Optional[float] = None) -> None:
        self._set("bean", bool(b0 & 0x01) and not (b0 & 0x08), now)

    def observe_bean_6220(self, now: Optional[float] = None) -> None:
        self._set("bean_inj", True, now)

    def observe_can_520(self, now: Optional[float] = None) -> None:
        now = self._now(now)
        self._t_520 = now
        self._set("can", True, now)

    def observe_can_038(self, b1_nonzero: bool, now: Optional[float] = None) -> None:
        now = self._now(now)
        inj = self._t_520 is not None and (now - self._t_520) <= CAN_INJ_PRESENT_S
        self._set("can", inj or bool(b1_nonzero), now)

    def observe_avc(self, engine_flag: bool) -> None:
        """AVC freshness is a probe on the AVC ingest; only the value is kept."""
        self._value["avc"] = bool(engine_flag)

    # ── decision ──────────────────────────────────────────────────────────
    def decide(self, now: Optional[float] = None) -> Tuple[Optional[bool], Optional[str]]:
        """(running, source) from the best fresh source; (None, None) when
        no source is fresh."""
        now = self._now(now)
        src = self._arb.active(SIGNAL, now)
        if src is None or src not in self._value:
            return None, None
        return self._value[src], src
