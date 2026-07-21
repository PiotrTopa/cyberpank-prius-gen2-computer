"""
Tests for the RS485 satellite subsystem: OUT2 wake-lock power rules, the
priority job queue (FIFO within priority), presence tracking and the
config-re-push supervisor.
"""

import time

import pytest

from cyberpunk_computer.state.store import Store, StateSlice
from cyberpunk_computer.state.actions import (
    ActionSource,
    EnqueueSatelliteCommandAction,
    SatellitePowerHoldAction,
    SetPowerboxIgnitionAction,
    SetPowerboxPowerStatusAction,
    SetPowerboxUndervoltageAction,
    UpdateSatelliteNodeAction,
)
from cyberpunk_computer.state.rules.satellite_power import (
    HOLDER_ACC,
    HOLDER_QUEUE,
    SatelliteAccHoldRule,
    SatellitePowerRule,
)
from cyberpunk_computer.backend.satellites import (
    PRIORITY_CONFIG,
    PRIORITY_SCHEDULED,
    PeriodicJobSpec,
    SatelliteJob,
    SatelliteJobQueue,
    SatelliteScheduler,
    SatelliteSupervisor,
    command_job,
)


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeOutputPort:
    def __init__(self):
        self.sent = []

    def send(self, cmd) -> bool:
        self.sent.append(cmd)
        return True


def make_store() -> Store:
    return Store()


def run_rule(rule, store, old_state):
    """Evaluate a rule against the store's current state (like the engine)."""
    rule.evaluate(old_state, store.state, store)


# ─────────────────────────────────────────────────────────────────────────────
# State model / reducers
# ─────────────────────────────────────────────────────────────────────────────

class TestSatelliteReducers:
    def test_power_hold_acquire_release(self):
        store = make_store()
        store.dispatch(SatellitePowerHoldAction("acc", acquire=True))
        assert store.state.satellites.power_holders == frozenset({"acc"})
        store.dispatch(SatellitePowerHoldAction("queue", acquire=True))
        assert store.state.satellites.power_holders == frozenset({"acc", "queue"})
        store.dispatch(SatellitePowerHoldAction("acc", acquire=False))
        assert store.state.satellites.power_holders == frozenset({"queue"})

    def test_node_seen_marks_online_and_bumps_boot_id(self):
        store = make_store()
        store.dispatch(UpdateSatelliteNodeAction(107, seen=True))
        node = store.state.satellites.nodes[107]
        assert node.online and node.boot_id == 1
        assert not node.config_synced

        # Traffic while already online must NOT bump boot_id again.
        store.dispatch(UpdateSatelliteNodeAction(107, seen=True))
        assert store.state.satellites.nodes[107].boot_id == 1

        # Offline -> online again = a reboot: boot_id bumps, sync flag clears.
        store.dispatch(UpdateSatelliteNodeAction(107, config_synced=True))
        store.dispatch(UpdateSatelliteNodeAction(107, online=False))
        store.dispatch(UpdateSatelliteNodeAction(107, seen=True))
        node = store.state.satellites.nodes[107]
        assert node.boot_id == 2 and not node.config_synced


# ─────────────────────────────────────────────────────────────────────────────
# Power rules
# ─────────────────────────────────────────────────────────────────────────────

class TestSatelliteAccHoldRule:
    def test_acc_edge_acquires_and_releases(self):
        store = make_store()
        rule = SatelliteAccHoldRule()

        old = store.state
        store.dispatch(SetPowerboxIgnitionAction(acc_on=True, batt_present=True))
        run_rule(rule, store, old)
        assert HOLDER_ACC in store.state.satellites.power_holders

        old = store.state
        store.dispatch(SetPowerboxIgnitionAction(acc_on=False, batt_present=True))
        run_rule(rule, store, old)
        assert HOLDER_ACC not in store.state.satellites.power_holders


class TestSatellitePowerRule:
    def test_on_immediate_off_lingers(self):
        store = make_store()
        clock = FakeClock()
        calls = []
        rule = SatellitePowerRule(set_out2=calls.append, linger_s=10.0, clock=clock)

        # Acquire -> ON immediately.
        old = store.state
        store.dispatch(SatellitePowerHoldAction("acc", acquire=True))
        run_rule(rule, store, old)
        assert calls == [True]
        assert store.state.satellites.power_requested is True

        # Release -> nothing yet (linger).
        old = store.state
        store.dispatch(SatellitePowerHoldAction("acc", acquire=False))
        run_rule(rule, store, old)
        assert calls == [True]

        # Re-acquire within linger -> stays ON, no OFF ever sent (no flap).
        old = store.state
        store.dispatch(SatellitePowerHoldAction("queue", acquire=True))
        run_rule(rule, store, old)
        store.dispatch(SatellitePowerHoldAction("queue", acquire=False))
        run_rule(rule, store, store.state)
        clock.advance(5.0)
        run_rule(rule, store, store.state)
        assert calls == [True]

        # Past the linger -> OFF.
        clock.advance(6.0)
        run_rule(rule, store, store.state)
        assert calls == [True, False]
        assert store.state.satellites.power_requested is False

    def test_reasserts_on_mirror_mismatch(self):
        store = make_store()
        clock = FakeClock()
        calls = []
        rule = SatellitePowerRule(set_out2=calls.append, linger_s=10.0,
                                  retry_s=5.0, clock=clock)

        old = store.state
        store.dispatch(SatellitePowerHoldAction("acc", acquire=True))
        run_rule(rule, store, old)
        assert calls == [True]

        # Powerbox says OUT2 is off (e.g. rebooted) -> re-send after retry_s.
        store.dispatch(SetPowerboxPowerStatusAction(out2=False))
        run_rule(rule, store, store.state)
        assert calls == [True]  # within retry window: no spam
        clock.advance(6.0)
        run_rule(rule, store, store.state)
        assert calls == [True, True]

    def test_gateway_bonded_to_rail_edges_only(self):
        store = make_store()
        clock = FakeClock()
        out2_calls = []
        gw_calls = []
        rule = SatellitePowerRule(set_out2=out2_calls.append, linger_s=10.0,
                                  retry_s=5.0, clock=clock,
                                  set_gateway=gw_calls.append)

        # ON edge: both rail and gateway commanded.
        old = store.state
        store.dispatch(SatellitePowerHoldAction("acc", acquire=True))
        run_rule(rule, store, old)
        assert out2_calls == [True]
        assert gw_calls == [True]

        # Mirror-mismatch reassert re-sends OUT2 but NOT the gateway
        # (gateway convergence is owned by the backend enforcement poll).
        store.dispatch(SetPowerboxPowerStatusAction(out2=False))
        clock.advance(6.0)
        run_rule(rule, store, store.state)
        assert out2_calls == [True, True]
        assert gw_calls == [True]

        # OFF edge after linger: both commanded off.
        store.dispatch(SetPowerboxPowerStatusAction(out2=True))
        old = store.state
        store.dispatch(SatellitePowerHoldAction("acc", acquire=False))
        run_rule(rule, store, old)
        clock.advance(11.0)
        run_rule(rule, store, store.state)
        assert out2_calls == [True, True, False]
        assert gw_calls == [True, False]


# ─────────────────────────────────────────────────────────────────────────────
# Job queue
# ─────────────────────────────────────────────────────────────────────────────

def make_queue(store=None, port=None, clock=None):
    store = store or make_store()
    port = port or FakeOutputPort()
    clock = clock or FakeClock()
    return SatelliteJobQueue(store, output_port=port, clock=clock), store, port, clock


def rail_up(store):
    store.dispatch(SetPowerboxPowerStatusAction(out2=True))


class TestSatelliteJobQueue:
    def test_priority_order_fifo_within_priority(self):
        queue, store, port, clock = make_queue()
        rail_up(store)
        order = []

        def job(name, prio):
            return SatelliteJob(name=name, priority=prio,
                                start=lambda ctx, n=name: order.append(n))

        queue.submit(job("scheduled", PRIORITY_SCHEDULED))
        queue.submit(job("normal-1", 50))
        queue.submit(job("config", PRIORITY_CONFIG))
        queue.submit(job("normal-2", 50))

        for _ in range(10):
            queue.tick()
        assert order == ["config", "normal-1", "normal-2", "scheduled"]

    def test_holder_acquired_while_busy_released_when_drained(self):
        queue, store, port, clock = make_queue()
        rail_up(store)
        queue.submit(command_job(107, {"a": "read"}))
        assert HOLDER_QUEUE in store.state.satellites.power_holders

        for _ in range(5):
            queue.tick()
        assert HOLDER_QUEUE not in store.state.satellites.power_holders
        assert len(port.sent) == 1
        assert port.sent[0].device_id == 107

    def test_waits_for_rail_and_required_node(self):
        queue, store, port, clock = make_queue()
        started = []
        queue.submit(SatelliteJob(
            name="needs-107", requires_online=(107,),
            start=lambda ctx: started.append(1),
        ))
        # Rail down, node offline -> must not start.
        store.dispatch(SetPowerboxPowerStatusAction(out2=False))
        for _ in range(3):
            queue.tick()
        assert not started

        rail_up(store)
        queue.tick()
        assert not started  # node still offline

        store.dispatch(UpdateSatelliteNodeAction(107, seen=True))
        queue.tick()
        assert started

    def test_ready_timeout_fails_job(self):
        queue, store, port, clock = make_queue()
        store.dispatch(SetPowerboxPowerStatusAction(out2=False))
        results = []
        queue.submit(SatelliteJob(
            name="doomed", start=lambda ctx: None,
            ready_timeout_s=20.0, on_done=results.append,
        ))
        queue.tick()          # pops -> wait_ready
        clock.advance(25.0)
        queue.tick()
        assert results == [False]
        assert queue.depth == 0

    def test_undervoltage_drops_queue_and_releases_holder(self):
        queue, store, port, clock = make_queue()
        queue.submit(command_job(107, {"a": "read"}))
        assert HOLDER_QUEUE in store.state.satellites.power_holders
        store.dispatch(SetPowerboxUndervoltageAction(True))
        queue.tick()
        assert queue.depth == 0
        assert HOLDER_QUEUE not in store.state.satellites.power_holders

    def test_poll_job_runs_until_done(self):
        queue, store, port, clock = make_queue()
        rail_up(store)
        polls = []

        def poll(ctx):
            polls.append(1)
            return len(polls) >= 3

        results = []
        queue.submit(SatelliteJob(name="poller", start=lambda ctx: None,
                                  poll=poll, on_done=results.append))
        for _ in range(6):
            queue.tick()
        assert len(polls) == 3 and results == [True]


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler + supervisor
# ─────────────────────────────────────────────────────────────────────────────

class TestSatelliteScheduler:
    def test_periodic_submission_and_no_pileup(self):
        clock = FakeClock()
        queue, store, port, _ = make_queue(clock=clock)
        sched = SatelliteScheduler(queue, store, clock=clock)
        sched.add(PeriodicJobSpec(
            name="sched:poll", interval_s=300.0,
            factory=lambda: command_job(107, {"a": "status"}),
        ))

        clock.advance(301.0)
        sched.tick()
        assert queue.depth == 1
        sched.tick()  # same cycle: job still queued -> no duplicate
        assert queue.depth == 1

    def test_skips_on_undervoltage(self):
        clock = FakeClock()
        queue, store, port, _ = make_queue(clock=clock)
        sched = SatelliteScheduler(queue, store, clock=clock)
        sched.add(PeriodicJobSpec(
            name="sched:poll", interval_s=300.0,
            factory=lambda: command_job(107, {"a": "status"}),
        ))
        store.dispatch(SetPowerboxUndervoltageAction(True))
        clock.advance(301.0)
        sched.tick()
        assert queue.depth == 0


class TestSatelliteSupervisor:
    def test_config_repushed_after_reboot(self):
        queue, store, port, clock = make_queue()
        rail_up(store)
        sup = SatelliteSupervisor(
            queue, store, desired_configs={110: {"t": "C", "br": 80}})
        sup.seed()
        assert store.state.satellites.nodes[110].desired_config == {"t": "C", "br": 80}

        # Node comes online -> config push job submitted and executed.
        store.dispatch(UpdateSatelliteNodeAction(110, seen=True))
        sup.tick()
        for _ in range(5):
            queue.tick()
        assert any(c.device_id == 110 and c.payload == {"t": "C", "br": 80}
                   for c in port.sent)
        assert store.state.satellites.nodes[110].config_synced

        # Reboot (offline -> seen again) -> re-push.
        sent_before = len(port.sent)
        store.dispatch(UpdateSatelliteNodeAction(110, online=False))
        store.dispatch(UpdateSatelliteNodeAction(110, seen=True))
        sup.tick()
        for _ in range(5):
            queue.tick()
        assert len(port.sent) == sent_before + 1

    def test_stale_node_marked_offline(self):
        queue, store, port, clock = make_queue()
        sup = SatelliteSupervisor(queue, store, offline_after_s=15.0)
        store.dispatch(UpdateSatelliteNodeAction(107, seen=True))
        assert store.state.satellites.nodes[107].online

        # Fake staleness by rewriting last_seen far in the past.
        import dataclasses
        node = store.state.satellites.nodes[107]
        store._state = dataclasses.replace(
            store._state,
            satellites=dataclasses.replace(
                store._state.satellites,
                nodes={107: dataclasses.replace(node, last_seen=time.time() - 60)},
            ),
        )
        sup.tick()
        assert not store.state.satellites.nodes[107].online

    def test_rail_down_marks_nodes_offline(self):
        queue, store, port, clock = make_queue()
        sup = SatelliteSupervisor(queue, store, offline_after_s=15.0)
        store.dispatch(UpdateSatelliteNodeAction(107, seen=True))
        store.dispatch(SetPowerboxPowerStatusAction(out2=False))
        sup.tick()
        assert not store.state.satellites.nodes[107].online


# ─────────────────────────────────────────────────────────────────────────────
# API command builders
# ─────────────────────────────────────────────────────────────────────────────

class TestApiCommands:
    def test_satellite_send_builds_enqueue_action(self):
        from cyberpunk_computer.api.commands import build_command
        action = build_command(
            "satellite_send",
            {"device_id": 107, "payload": {"a": "read"}, "priority": 30},
        )
        assert isinstance(action, EnqueueSatelliteCommandAction)
        assert action.device_id == 107 and action.priority == 30

    def test_satellite_power_hold_builds_manual_holder(self):
        from cyberpunk_computer.api.commands import build_command
        action = build_command("satellite_power_hold", {"name": "garage", "on": True})
        assert isinstance(action, SatellitePowerHoldAction)
        assert action.holder == "manual:garage" and action.acquire

    def test_satellite_send_rejects_bad_device(self):
        from cyberpunk_computer.api.commands import build_command, CommandError
        with pytest.raises(CommandError):
            build_command("satellite_send", {"device_id": 201, "payload": {}})
