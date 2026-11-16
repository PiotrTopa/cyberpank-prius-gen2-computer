"""
Per-signal source priority with staleness fallback.

Several values reach the backend from more than one bus: passively heard
(BEAN body bus, broadcast CAN, AVC-LAN) and solicited (OBD-II requests the
gateway sends onto the car's CAN bus). The owner's rule (2026-11-14): a value
that passive listening gives at a rate equal or close to the solicited one
comes from the passive source, and the solicited request for it is dropped
while the passive source is alive, so we put less traffic on the car's bus.

:class:`SourceArbiter` holds one :class:`SignalPolicy` per signal: an ordered
list of sources (best first), each with a freshness window. A source is used
when no better source has delivered within its window:

    passive fresh  -> use it; worse sources are ignored
    passive stale  -> the next source (e.g. the solicited reply) is used

Every ingest path asks ``arbiter.offer(signal, source)`` before it dispatches
the value; the call also records that the source is alive.

:class:`SlotSuspender` turns the arbiter's view into gateway subscription
changes: a solicited slot that exists only for one signal is unsubscribed
after the passive source has been fresh for ``suspend_after_s`` and
re-subscribed the moment the passive source goes stale.

The measured rates and agreement behind the default policies are in the
module-level comments of :data:`DEFAULT_POLICIES`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

PASSIVE = "passive"
SOLICITED = "solicited"


@dataclass(frozen=True)
class Source:
    """One source of a signal. ``max_age_s``: how long after its last value
    the source still counts as alive (a few of its periods)."""
    name: str
    max_age_s: float
    kind: str = PASSIVE


@dataclass(frozen=True)
class SignalPolicy:
    """Sources of one signal, best first.

    ``solicited_slots``: gateway subscription slots that exist only to fetch
    this signal; they may be suspended while a passive source of the signal
    is fresh (see :class:`SlotSuspender`). Leave empty when the slot carries
    other values too.
    """
    signal: str
    sources: Tuple[Source, ...]
    solicited_slots: Tuple[int, ...] = ()
    suspend_after_s: float = 10.0
    note: str = ""

    def source(self, name: str) -> Optional[Source]:
        for s in self.sources:
            if s.name == name:
                return s
        return None


class SourceArbiter:
    """Decides, per signal, which source's values reach the state."""

    def __init__(self, policies: Iterable[SignalPolicy] = (),
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._policies: Dict[str, SignalPolicy] = {}
        self._seen: Dict[Tuple[str, str], float] = {}
        self._fresh_since: Dict[Tuple[str, str], float] = {}
        self._probes: Dict[str, Callable[[str], bool]] = {}
        for p in policies:
            self.add_policy(p)

    def add_policy(self, policy: SignalPolicy) -> None:
        self._policies[policy.signal] = policy

    def set_probe(self, source: str, fn: Optional[Callable[[str], bool]]) -> None:
        """Delegate the freshness of ``source`` to ``fn(signal) -> bool``
        (for an ingest path that already tracks its own freshness, e.g. the
        AVC-LAN ingest); ``offer`` calls for it are then only recorded."""
        if fn is None:
            self._probes.pop(source, None)
        else:
            self._probes[source] = fn

    def policy(self, signal: str) -> Optional[SignalPolicy]:
        return self._policies.get(signal)

    @property
    def policies(self) -> List[SignalPolicy]:
        return list(self._policies.values())

    def _now(self, now: Optional[float]) -> float:
        return self._clock() if now is None else now

    def offer(self, signal: str, source: str, now: Optional[float] = None) -> bool:
        """Record that ``source`` delivered ``signal`` now; True when its
        value should be used (no better source is fresh).

        Signals without a policy, and sources the policy does not list, are
        always used (the arbiter only arbitrates what it knows)."""
        now = self._now(now)
        policy = self._policies.get(signal)
        key = (signal, source)
        spec = policy.source(source) if policy else None
        if spec is not None:
            last = self._seen.get(key)
            if last is None or (now - last) > spec.max_age_s:
                self._fresh_since[key] = now
        self._seen[key] = now
        if policy is None or spec is None:
            return True
        for s in policy.sources:
            if s.name == source:
                return True
            if self._fresh(signal, s, now):
                return False
        return True

    def _fresh(self, signal: str, spec: Source, now: float) -> bool:
        probe = self._probes.get(spec.name)
        if probe is not None:
            try:
                return bool(probe(signal))
            except Exception:
                logger.exception("Source probe %s failed", spec.name)
                return False
        t = self._seen.get((signal, spec.name))
        return t is not None and (now - t) <= spec.max_age_s

    def fresh(self, signal: str, source: str, now: Optional[float] = None) -> bool:
        policy = self._policies.get(signal)
        spec = policy.source(source) if policy else None
        if spec is None:
            return False
        return self._fresh(signal, spec, self._now(now))

    def active(self, signal: str, now: Optional[float] = None) -> Optional[str]:
        """The best source that is fresh right now (None: all stale)."""
        policy = self._policies.get(signal)
        if policy is None:
            return None
        now = self._now(now)
        for s in policy.sources:
            if self._fresh(signal, s, now):
                return s.name
        return None

    def passive_fresh(self, signal: str, now: Optional[float] = None) -> bool:
        """Is any passive source of ``signal`` fresh right now?"""
        policy = self._policies.get(signal)
        if policy is None:
            return False
        now = self._now(now)
        return any(s.kind == PASSIVE and self._fresh(signal, s, now) for s in policy.sources)

    def passive_fresh_for(self, signal: str, now: Optional[float] = None) -> float:
        """Seconds the best fresh passive source has been continuously fresh
        (0.0 when no passive source is fresh)."""
        policy = self._policies.get(signal)
        if policy is None:
            return 0.0
        now = self._now(now)
        best = 0.0
        for s in policy.sources:
            if s.kind == PASSIVE and s.name not in self._probes and self._fresh(signal, s, now):
                since = self._fresh_since.get((signal, s.name), now)
                best = max(best, now - since)
        return best

    def status(self, now: Optional[float] = None) -> Dict[str, dict]:
        """``signal -> {"active": name|None, "ages": {source: age_s|None}}``."""
        now = self._now(now)
        out: Dict[str, dict] = {}
        for sig, p in self._policies.items():
            ages = {}
            for s in p.sources:
                t = self._seen.get((sig, s.name))
                ages[s.name] = None if t is None else round(now - t, 2)
            out[sig] = {"active": self.active(sig, now), "ages": ages}
        return out


class SlotSuspender:
    """Suspends solicited gateway slots while a passive source covers them.

    ``unsubscribe(slot)`` / ``subscribe(slot)`` send the gateway commands.
    A slot is suspended once every signal that names it has had a fresh
    passive source for its ``suspend_after_s``; it is resumed as soon as one
    of them has none. After a resume the slot stays subscribed for at least
    ``resume_hold_s`` so an intermittent passive source cannot make it flap.
    """

    def __init__(self, arbiter: SourceArbiter,
                 unsubscribe: Callable[[int], object],
                 subscribe: Callable[[int], object],
                 resume_hold_s: float = 30.0,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self._arbiter = arbiter
        self._unsub = unsubscribe
        self._sub = subscribe
        self.resume_hold_s = resume_hold_s
        self._clock = clock or arbiter._clock
        self.suspended: Set[int] = set()
        self._resumed_at: Dict[int, float] = {}
        self.enabled = True

    def _slot_signals(self) -> Dict[int, List[SignalPolicy]]:
        out: Dict[int, List[SignalPolicy]] = {}
        for p in self._arbiter.policies:
            for slot in p.solicited_slots:
                out.setdefault(slot, []).append(p)
        return out

    def wanted(self, now: Optional[float] = None) -> Set[int]:
        """Slots that should be suspended right now."""
        now = self._clock() if now is None else now
        want: Set[int] = set()
        if not self.enabled:
            return want
        for slot, policies in self._slot_signals().items():
            covered = all(self._arbiter.passive_fresh_for(p.signal, now) >= p.suspend_after_s
                          for p in policies)
            if slot in self.suspended:
                # stay suspended while any passive source is still fresh
                covered = all(self._arbiter.passive_fresh(p.signal, now) for p in policies)
            elif covered and (now - self._resumed_at.get(slot, -1e18)) < self.resume_hold_s:
                covered = False
            if covered:
                want.add(slot)
        return want

    def tick(self, now: Optional[float] = None) -> Tuple[Set[int], Set[int]]:
        """Apply changes; returns (newly suspended, resumed)."""
        now = self._clock() if now is None else now
        want = self.wanted(now)
        newly = want - self.suspended
        resumed = self.suspended - want
        for slot in sorted(newly):
            logger.info("Solicited slot %d suspended: passive source fresh (%s)", slot,
                        ", ".join(p.signal for p in self._slot_signals().get(slot, [])))
            self._unsub(slot)
        for slot in sorted(resumed):
            logger.info("Solicited slot %d resumed: passive source stale", slot)
            self._resumed_at[slot] = now
            self._sub(slot)
        self.suspended = want
        return newly, resumed

    def reset(self) -> None:
        """Forget suspensions (after a gateway reboot every slot is gone and
        the full setup is re-sent without the suspended ones)."""
        self.suspended = set()


# ─────────────────────────────────────────────────────────────────────────────
# Default policies (measured on the 2026-11-13 IG-ON drive: phone trip
# recording + BEAN capture of the same 19 minutes; docs/BEAN_SIGNALS.md).
#
#   signal        source            period   agreement
#   rpm           BEAN FE:26        0.50 s   vs OBD 010C: MAE 19 rpm (16 at
#                                            +0.25 s), p95 62; OBD vs 21C3
#                                            actual: MAE 19 -> same quality
#                 OBD 010C (slot 0) 0.50 s   (resolution 0.25 rpm; FE:26 0.2)
#   coolant       CAN 0x039         0.18 s   1 °C, no floor
#                 BEAN FE:2C        1.0 s    0.5 °C, reads +1.1 °C vs 0x039,
#                                            floor 16 °C (raw 0x20)
#   outside_temp  AVC E0 5D F7      1.0 s    verbatim copy of FE:CD
#                 BEAN FE:CD        1.0 s    = AVC (x-48)
#   gear          CAN 0x120         0.26 s
#                 BEAN FE:40        1.0 s    relayed 45-110 ms after CAN
#   lights        CAN 0x57F         1.0 s    = FE:7F verbatim (gateway copy)
#                 BEAN FE:7F        1.0 s
#   soc_bars      AVC E4 5F B8 / CAN 0x529 1.0 s / BEAN FE:29 1.0 s (same bytes)
#   climate       AVC E0 5D F3/F5   5 s+chg  verbatim copies of FE:FC / 52:FD
#                 BEAN FE:FC/52:FD  5 s+chg
#   engine_running rpm (FE:26/OBD)  0.50 s   agrees with OBD rpm>400 99.5 %
#                 BEAN 98:27        1.0 s    98.8 %
#                 BEAN 62:20        0.92 s   presence 98.8 %
#                 CAN 0x520/0x038   0.92 s   0x520 presence 98.5 % (0x038 b1: 68 %)
#   fuel_word     CAN 0x520 b1-2    0.92 s   = BEAN 62:20 word (289/300 frames
#                 BEAN 62:20        0.92 s   match the neighbouring 0x520)
#   cruise_main   BEAN 62:C8 b1 10  1 s+chg  first (2026-11-16 cruise drive)
#                 OBD 21D3 D 0x0F   1.0 s    ~0.6 s after 62:C8
#                 CAN 0x5C8 b2 10   0.4 s    content ~3 s late, missed a 1 s off/on
#   cruise_active OBD 21D3 D 0x40   1.0 s    at the poll
#                 CAN 0x120 b4 80   0.26 s   ~2 s behind 21D3 at engage/cancel
# ─────────────────────────────────────────────────────────────────────────────

#: Gateway subscription slot of the OBD 010C (engine rpm) request
#: (comm.subscription_groups.CORE_GROUP).
SLOT_OBD_RPM = 0

DEFAULT_POLICIES: Tuple[SignalPolicy, ...] = (
    SignalPolicy("rpm", (Source("bean", 2.0), Source("obd", 3.0, SOLICITED)),
                 solicited_slots=(SLOT_OBD_RPM,),
                 note="BEAN FE:26 (0.5 s) replaces OBD 010C (0.5 s)"),
    SignalPolicy("coolant", (Source("can", 2.0), Source("bean", 3.0),
                             Source("obd", 5.0, SOLICITED))),
    SignalPolicy("outside_temp", (Source("avc", 15.0), Source("bean", 3.0))),
    SignalPolicy("gear", (Source("can", 2.0), Source("bean", 3.0))),
    SignalPolicy("lights", (Source("can", 3.0), Source("bean", 3.0))),
    SignalPolicy("soc_bars", (Source("avc", 15.0), Source("can", 3.0), Source("bean", 3.0))),
    SignalPolicy("climate", (Source("avc", 15.0), Source("bean", 12.0))),
    # Engine running (comm.engine_gate): rpm first; CAN 0x038 b1 alone is
    # not reliable (0 for 32 % of the running time at low load).
    SignalPolicy("engine_running", (Source("rpm", 2.0), Source("bean", 3.0),
                                    Source("bean_inj", 2.5), Source("can", 2.0),
                                    Source("avc", 15.0)),
                 note="rpm > 400 / 98:27 / 62:20 / 0x520 presence / AVC B9"),
    # Fuel quantity per injection: CAN 0x520 b1-2 = BEAN 62:20 (same word).
    SignalPolicy("fuel_word", (Source("can", 2.5), Source("bean", 3.0))),
    # Cruise (io.cruise_fusion): the solicited 21D3 beats the late CAN copies.
    SignalPolicy("cruise_main", (Source("bean", 3.0), Source("obd", 3.0, SOLICITED),
                                 Source("can", 3.0))),
    SignalPolicy("cruise_active", (Source("obd", 3.0, SOLICITED), Source("can", 2.0))),
)


def default_arbiter(clock: Callable[[], float] = time.monotonic) -> SourceArbiter:
    return SourceArbiter(DEFAULT_POLICIES, clock=clock)
