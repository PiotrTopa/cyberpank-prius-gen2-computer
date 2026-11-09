"""
Satellite job queue — serialized command execution + rail power orchestration.

The RS485 satellites hang off the gateway on the relay-switched rail (relay
ch5, ``powerbox.rs485_power``). Any work
that needs a satellite (scheduled sensor reads, event-driven commands, config
pushes, API requests) is expressed as a :class:`SatelliteJob` and submitted to
the single :class:`SatelliteJobQueue`. The queue:

    * orders jobs by ``priority`` (lower first), FIFO within a priority level;
    * holds the ``"queue"`` rail wake-lock (see rules/satellite_power.py) while
      any job is pending or running, so back-to-back jobs — a temperature read
      finishing while a light-control job and an event command arrive — share
      ONE rail power-up with no flapping (the power rule adds a linger before
      the final OFF);
    * per job, waits for the rail to actually come up (powerbox ``rs485_power``
      mirror) and for the ``requires_online`` satellites to report presence
      before calling ``start``; then polls ``poll`` until done or timeout;
    * mirrors depth + active job into ``SatellitesState`` for observability;
    * on sustained 12 V under-voltage cancels pending jobs and releases the
      hold, so offline (key-off) satellite work can never drain the battery.

Multi-transport (docs/SATELLITES.md §5): the SatelliteRegistry owns one
queue per transport. The RS485 queue is exactly the queue described above
(rail wake-lock, ``powerbox.rs485_power``). BLE/Wi-Fi queues are built with
``use_rail=False`` and a ``transport_ready`` callback: they hold no wake-lock
and only wait for the medium plus the job's ``requires_online`` nodes (for
those transports ``online`` implies an authenticated link); their
``JobContext.send`` goes through the registry instead of the gateway port.

Executed from the backend engine loop (`tick()` — same thread as store
dispatches), no asyncio needed. RS485 is half-duplex, so serializing jobs is
also a bus-correctness win.

The :class:`SatelliteScheduler` produces periodic jobs (e.g. "every 5 min power
the satellites and read the light sensor"), and :class:`SatelliteSupervisor`
handles presence aging + automatic config re-push after a satellite restart.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from ..io.ports import OutgoingCommand, OutputPort, SATELLITE_NAMES
from ..state.store import Store
from ..state.app_state import AppState
from ..state.actions import (
    ActionSource,
    SatellitePowerHoldAction,
    SetSatelliteQueueAction,
    UpdateSatelliteNodeAction,
)
from ..state.rules.satellite_power import HOLDER_QUEUE

logger = logging.getLogger(__name__)

# Priorities (lower runs first; FIFO within a level)
PRIORITY_CONFIG = 10    # config re-push after satellite boot — before user jobs
PRIORITY_EVENT = 30     # event-driven commands (e.g. remote start prep)
PRIORITY_NORMAL = 50    # default / API commands
PRIORITY_SCHEDULED = 70 # periodic background sensor reads


class JobContext:
    """What a job gets to work with while executing."""

    def __init__(
        self,
        store: Store,
        output_port: Optional[OutputPort],
        sender: Optional[Callable[[int, dict, int], bool]] = None,
    ):
        self.store = store
        self.output_port = output_port
        # Non-RS485 queues: sender(sat_id, payload, priority) via the registry.
        self._sender = sender

    @property
    def state(self) -> AppState:
        return self.store.state

    def send(self, device_id: int, payload: dict, priority: int = 50) -> bool:
        """Send one NDJSON command to a satellite (gateway for RS485, the
        registry's transport routing for BLE/Wi-Fi)."""
        if self._sender is not None:
            try:
                return bool(self._sender(device_id, payload, priority))
            except Exception:
                logger.exception("Satellite send to %d failed", device_id)
                return False
        if self.output_port is None:
            logger.warning("Satellite send to %d dropped: no output port", device_id)
            return False
        cmd = OutgoingCommand(
            device_id=device_id,
            command_type="satellite",
            payload=payload,
            priority=priority,
        )
        try:
            return bool(self.output_port.send(cmd))
        except Exception:
            logger.exception("Satellite send to %d failed", device_id)
            return False


@dataclass
class SatelliteJob:
    """One unit of satellite work.

    ``start`` is called once the satellite rail is up and every ``requires_online``
    node is present. If ``poll`` is None the job completes right after
    ``start``; otherwise ``poll`` is called each engine tick until it returns
    True (done) or ``timeout_s`` elapses (failed).
    """
    name: str
    start: Callable[[JobContext], None]
    poll: Optional[Callable[[JobContext], bool]] = None
    priority: int = PRIORITY_NORMAL
    requires_online: Tuple[int, ...] = ()
    ready_timeout_s: float = 20.0   # max wait for rail + satellites to come up
    timeout_s: float = 30.0         # max run time after start
    on_done: Optional[Callable[[bool], None]] = None  # called with success flag


def command_job(
    device_id: int,
    payload: dict,
    name: str = "",
    priority: int = PRIORITY_NORMAL,
    requires_online: Tuple[int, ...] = (),
) -> SatelliteJob:
    """Build a fire-one-command job (the common case)."""
    return SatelliteJob(
        name=name or f"cmd:{device_id}",
        start=lambda ctx: ctx.send(device_id, payload),
        priority=priority,
        requires_online=requires_online,
    )


class SatelliteJobQueue:
    """Priority queue (FIFO within priority) + tick-driven executor."""

    _IDLE = "idle"
    _WAIT_READY = "wait_ready"
    _RUNNING = "running"

    def __init__(
        self,
        store: Store,
        output_port: Optional[OutputPort] = None,
        clock: Callable[[], float] = time.monotonic,
        transport: str = "rs485",
        use_rail: bool = True,
        transport_ready: Optional[Callable[[], bool]] = None,
        sender: Optional[Callable[[int, dict, int], bool]] = None,
    ):
        self._store = store
        self._ctx = JobContext(store, output_port, sender=sender)
        self._clock = clock
        self.transport = transport
        self._use_rail = use_rail
        self._transport_ready = transport_ready
        self._lock = threading.Lock()
        self._heap: List[Tuple[int, int, SatelliteJob]] = []
        self._seq = itertools.count()  # FIFO tiebreaker within a priority
        self._phase = self._IDLE
        self._current: Optional[SatelliteJob] = None
        self._phase_since = 0.0
        self._holder_held = False

    # ── producers ────────────────────────────────────────────────────────────

    def submit(self, job: SatelliteJob) -> None:
        """Enqueue a job and (if needed) acquire the rail power hold."""
        with self._lock:
            heapq.heappush(self._heap, (job.priority, next(self._seq), job))
        logger.info("Satellite job queued: %s (prio=%d, depth=%d)",
                    job.name, job.priority, self.depth)
        self._acquire_holder()
        self._publish()

    def has(self, name: str) -> bool:
        """True if a job with this name is pending or running."""
        with self._lock:
            if self._current is not None and self._current.name == name:
                return True
            return any(j.name == name for _, _, j in self._heap)

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._heap) + (1 if self._current else 0)

    # ── power hold ───────────────────────────────────────────────────────────

    def _acquire_holder(self) -> None:
        if not self._use_rail:
            return
        if not self._holder_held:
            self._holder_held = True
            self._store.dispatch(SatellitePowerHoldAction(HOLDER_QUEUE, acquire=True))

    def _release_holder(self) -> None:
        # The power rule lingers before actually dropping the rail, so releasing
        # promptly here does NOT flap the rail between close jobs.
        if self._holder_held:
            self._holder_held = False
            self._store.dispatch(SatellitePowerHoldAction(HOLDER_QUEUE, acquire=False))

    # ── observability ────────────────────────────────────────────────────────

    def _publish(self) -> None:
        active = self._current.name if self._current else ""
        self._store.dispatch(SetSatelliteQueueAction(self.depth, active,
                                                     transport=self.transport))

    # ── executor ─────────────────────────────────────────────────────────────

    def _rail_up(self, state: AppState) -> bool:
        if not self._use_rail:
            # BLE/Wi-Fi: "rail" = the medium is usable (bled reachable, AP up).
            if self._transport_ready is None:
                return True
            try:
                return bool(self._transport_ready())
            except Exception:
                logger.exception("transport_ready for %s failed", self.transport)
                return False
        # Trust the powerbox STATUS mirror; when no powerbox is attached
        # (dev/replay) fall back to "requested" so jobs still run.
        rail = state.powerbox.rs485_power
        if rail is not None and state.powerbox.connected:
            return bool(rail)
        return bool(state.satellites.power_requested or not state.powerbox.connected)

    def _nodes_ready(self, state: AppState, job: SatelliteJob) -> bool:
        nodes = state.satellites.nodes
        return all(
            (n := nodes.get(dev)) is not None and n.online
            for dev in job.requires_online
        )

    def _finish(self, success: bool) -> None:
        job = self._current
        self._current = None
        self._phase = self._IDLE
        if job is not None:
            logger.log(logging.INFO if success else logging.WARNING,
                       "Satellite job %s: %s", "done" if success else "FAILED", job.name)
            if job.on_done is not None:
                try:
                    job.on_done(success)
                except Exception:
                    logger.exception("Satellite job %s on_done failed", job.name)
        self._publish()

    def tick(self) -> None:
        """Advance the executor by one step. Call from the engine loop."""
        state = self._store.state
        now = self._clock()

        # Battery protection: sustained under-voltage cancels everything.
        if state.powerbox.undervoltage:
            with self._lock:
                dropped = len(self._heap)
                self._heap.clear()
            if dropped or self._current:
                logger.warning("Under-voltage: dropping %d queued satellite job(s)%s",
                               dropped, " + active" if self._current else "")
                self._finish(False)
                self._release_holder()
            return

        if self._phase == self._IDLE:
            with self._lock:
                item = heapq.heappop(self._heap) if self._heap else None
            if item is None:
                self._release_holder()
                return
            self._acquire_holder()
            self._current = item[2]
            self._phase = self._WAIT_READY
            self._phase_since = now
            self._publish()
            return

        job = self._current
        if job is None:  # defensive
            self._phase = self._IDLE
            return

        if self._phase == self._WAIT_READY:
            if self._rail_up(state) and self._nodes_ready(state, job):
                try:
                    job.start(self._ctx)
                except Exception:
                    logger.exception("Satellite job %s start failed", job.name)
                    self._finish(False)
                    return
                if job.poll is None:
                    self._finish(True)
                else:
                    self._phase = self._RUNNING
                    self._phase_since = now
            elif (now - self._phase_since) >= job.ready_timeout_s:
                logger.warning(
                    "Satellite job %s: rail/nodes not ready after %.0fs "
                    "(rail_up=%s, requires=%s)",
                    job.name, job.ready_timeout_s,
                    self._rail_up(state), job.requires_online,
                )
                self._finish(False)
            return

        if self._phase == self._RUNNING:
            done = False
            try:
                done = bool(job.poll(self._ctx))
            except Exception:
                logger.exception("Satellite job %s poll failed", job.name)
                self._finish(False)
                return
            if done:
                self._finish(True)
            elif (now - self._phase_since) >= job.timeout_s:
                logger.warning("Satellite job %s timed out after %.0fs",
                               job.name, job.timeout_s)
                self._finish(False)


@dataclass
class PeriodicJobSpec:
    """A recurring satellite job (e.g. wake every 5 min and read sensors)."""
    name: str
    interval_s: float
    factory: Callable[[], SatelliteJob]
    run_when_acc_off: bool = True   # scheduled wakes are the key-off use case
    transport: str = "rs485"        # which transport queue runs it
    _last_run: float = field(default=0.0, repr=False)


class SatelliteScheduler:
    """Feeds periodic jobs into the queue on their intervals.

    Skips a cycle when the same-named job is still pending/running (no
    pile-up) and when the 12 V battery is under-voltage.
    """

    def __init__(
        self,
        queue: SatelliteJobQueue,
        store: Store,
        clock: Callable[[], float] = time.monotonic,
        queues: Optional[Dict[str, SatelliteJobQueue]] = None,
    ):
        self._queue = queue
        self._store = store
        self._clock = clock
        # Per-transport queues (registry); specs pick theirs by ``transport``.
        self._queues = queues
        self._specs: List[PeriodicJobSpec] = []

    def _queue_for(self, spec: PeriodicJobSpec) -> SatelliteJobQueue:
        if self._queues is not None and spec.transport in self._queues:
            return self._queues[spec.transport]
        return self._queue

    def add(self, spec: PeriodicJobSpec) -> None:
        self._specs.append(spec)
        logger.info("Satellite periodic job registered: %s every %.0fs",
                    spec.name, spec.interval_s)

    def tick(self) -> None:
        state = self._store.state
        if state.powerbox.undervoltage:
            return
        now = self._clock()
        for spec in self._specs:
            if spec.interval_s <= 0:
                continue
            if (now - spec._last_run) < spec.interval_s:
                continue
            if not spec.run_when_acc_off and not state.powerbox.acc_on:
                continue
            queue = self._queue_for(spec)
            if queue.has(spec.name):
                continue  # previous run still in flight
            spec._last_run = now
            job = spec.factory()
            job.name = spec.name
            queue.submit(job)


class SatelliteSupervisor:
    """Presence aging + automatic reconfiguration after a satellite restart.

    * Seeds the twin with the known satellites (SATELLITE_NAMES) and their
      persisted ``desired_config``.
    * Marks nodes offline when their traffic goes stale or when the satellite rail
      is known to be down (output-only satellites that never transmit simply
      stay offline in the twin — don't gate jobs on them).
    * When a node comes (back) online with ``config_synced == False`` (reducer
      clears it on every offline->online edge), submits a high-priority config
      job that pushes ``desired_config`` to the device and marks it synced.
    """

    def __init__(
        self,
        queue: SatelliteJobQueue,
        store: Store,
        desired_configs: Optional[Dict[int, dict]] = None,
        offline_after_s: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
        queue_for: Optional[Callable[[int], Optional[SatelliteJobQueue]]] = None,
    ):
        self._queue = queue
        # Registry: device id -> its transport's queue (None = skip).
        self._queue_for = queue_for
        self._store = store
        self._offline_after_s = offline_after_s
        self._desired = dict(desired_configs or {})
        self._clock = clock
        # node.last_seen is a wall-clock stamp (display). Presence ages on the
        # monotonic clock from the moment the stamp was last seen to change,
        # so an NTP step cannot mark every satellite offline.
        self._seen_marks: Dict[int, tuple] = {}

    def _seen_mono(self, dev: int, last_seen: float, now: float) -> float:
        mark = self._seen_marks.get(dev)
        if mark is None or (last_seen and last_seen != mark[0]):
            mark = (last_seen, now)
            self._seen_marks[dev] = mark
        return mark[1]

    def seed(self) -> None:
        """Publish the known-satellite registry + persisted configs as twin nodes."""
        for dev, name in SATELLITE_NAMES.items():
            self._store.dispatch(UpdateSatelliteNodeAction(
                dev, name=name,
                desired_config=self._desired.get(dev),
                source=ActionSource.INTERNAL,
            ))

    def tick(self) -> None:
        state = self._store.state
        now = self._clock()
        rail_down = state.powerbox.connected and state.powerbox.rs485_power is False
        for dev, node in state.satellites.nodes.items():
            wired = getattr(node, "transport", "rs485") == "rs485"
            # RS485 presence ages on traffic + rail. BLE/Wi-Fi presence follows
            # the authenticated link (the registry clears it on link down; the
            # transports run their own keep-alive), so no traffic ageing here.
            if wired:
                seen = self._seen_mono(dev, node.last_seen, now)
                if node.online and (
                    rail_down or (now - seen) > self._offline_after_s
                ):
                    self._store.dispatch(UpdateSatelliteNodeAction(
                        dev, online=False, linked=False, source=ActionSource.INTERNAL))
                    continue
            queue = self._queue if self._queue_for is None else self._queue_for(dev)
            if (
                queue is not None
                and node.online
                and not node.config_synced
                and node.desired_config
                and not queue.has(f"cfg:{dev}")
            ):
                self._submit_config_push(dev, dict(node.desired_config), queue)

    def _submit_config_push(self, device_id: int, config: dict,
                            queue: Optional[SatelliteJobQueue] = None) -> None:
        def start(ctx: JobContext) -> None:
            ctx.send(device_id, config, priority=80)
            ctx.store.dispatch(UpdateSatelliteNodeAction(
                device_id, config_synced=True, source=ActionSource.INTERNAL))

        logger.info("Satellite %d rebooted/appeared — re-pushing config", device_id)
        (queue or self._queue).submit(SatelliteJob(
            name=f"cfg:{device_id}",
            start=start,
            priority=PRIORITY_CONFIG,
            requires_online=(device_id,),
        ))
