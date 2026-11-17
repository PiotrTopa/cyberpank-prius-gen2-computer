"""roadsense stack manager (backend.roadsense_power): ACC follower on the
single car computer — camera hub relay, systemctl start/stop, health probe,
operator modes, deferred low power profile hook."""

from cyberpunk_computer.backend.roadsense_power import (
    MODE_AUTO,
    MODE_OFF,
    MODE_ON,
    STATE_CAMERA_ON,
    STATE_GRACE,
    STATE_OFF,
    STATE_RUNNING,
    STATE_STARTING,
    STATE_STOPPING,
    RoadsenseConfig,
    RoadsenseManager,
)
from cyberpunk_computer.state.rules.power_management import PowerModeRule


class FakeCamera:
    def __init__(self):
        self.calls = []
        self.on = None

    def set(self, on):
        self.calls.append(on)
        self.on = on

    def read(self):
        return self.on


class R:
    def __init__(self, rc=0, out=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


class Harness:
    """Fake systemd: tracks the target state from start/stop/restart calls and
    answers is-active per unit; the API answers while ``api`` is a dict."""

    def __init__(self, target_active=False, api=None, **cfg):
        self.t = 1000.0
        self.target_active = target_active
        self.api = api                   # dict -> healthy, None -> down
        self.systemctl = []              # every systemctl argv
        self.published = []
        self.camera = FakeCamera()
        self.stop_lag = 0                # is-active polls answering "deactivating" after a stop
        self.temps = {"cpu0-thermal": 41.0, "gpu-top-thermal": 33.2, "cpu7-thermal": 44.5}
        kw = dict(tick_s=5.0, camera_settle_s=8.0, grace_s=30.0, start_timeout_s=60.0,
                  stop_timeout_s=40.0, api_dead_restart_s=100.0, restart_cooldown_s=200.0)
        kw.update(cfg)
        config = RoadsenseConfig(units=("roadsense-modeld.service", "roadsense-api.service"), **kw)
        self.m = RoadsenseManager(
            config,
            publish=self.published.append,
            runner=self.run,
            camera=self.camera,
            health_fetch=lambda url, timeout: self.api,
            thermals=lambda: dict(self.temps),
            clock=lambda: self.t,
            async_probe=False,
        )

    def run(self, cmd, timeout=10):
        self.systemctl.append(cmd[1:])
        verb = cmd[1]
        if verb == "is-active":
            units = cmd[2:]
            if self.stop_lag > 0 and not self.target_active:
                self.stop_lag -= 1
                return R(0, "\n".join("deactivating" for _ in units) + "\n")
            state = "active" if self.target_active else "inactive"
            return R(0 if self.target_active else 3, "\n".join(state for _ in units) + "\n")
        if verb in ("start", "restart"):
            self.target_active = True
            return R(0)
        if verb == "stop":
            self.target_active = False
            return R(0)
        return R(1)

    def tick(self, acc, dt=5.0, acc_known=True):
        self.t += dt
        self.m.tick(acc, acc_known=acc_known)

    @property
    def state(self):
        return self.m.state

    def verbs(self):
        return [c[0] for c in self.systemctl if c[0] != "is-active"]


class TestAccFollower:
    def test_key_on_powers_camera_then_starts_then_runs(self):
        h = Harness()
        h.tick(True)
        assert h.state == STATE_CAMERA_ON
        assert h.camera.calls == [True]
        assert h.verbs() == []                   # settle first
        h.tick(True, dt=8)
        assert h.state == STATE_STARTING
        assert h.verbs() == ["start"]
        assert ["start", "--no-block", "roadsense.target"] in h.systemctl
        h.api = {"fps": {"road": 12.5}, "camera_fps": 19.5, "model_ms": 78, "daemons": {"modeld": {"connected": True}}}
        h.tick(True)
        assert h.state == STATE_RUNNING
        st = h.m.status()
        assert st["api_ok"] is True
        assert st["health"]["camera_fps"] == 19.5
        assert st["health"]["daemons"] == {"modeld": True}
        assert st["units"] == {"roadsense-modeld.service": "active", "roadsense-api.service": "active"}
        assert st["hottest_zone"] == "cpu7-thermal" and st["hottest_c"] == 44.5
        assert st["temps"]["gpu-top-thermal"] == 33.2
        assert st["api_url"] == "http://127.0.0.1:8090"

    def test_key_off_grace_then_stop_then_camera_off(self):
        h = Harness(target_active=True, api={})
        h.tick(True)                              # adopt the running stack
        assert h.state == STATE_RUNNING
        assert h.camera.calls == [True]
        h.tick(False)
        assert h.state == STATE_GRACE
        assert h.verbs() == []
        h.tick(False, dt=10)
        assert h.state == STATE_GRACE             # 30 s grace not over
        h.tick(False, dt=25)
        assert h.state == STATE_STOPPING
        assert h.verbs() == ["stop"]
        assert h.camera.on is True                # camera stays until the units are down
        h.tick(False)
        assert h.state == STATE_OFF
        assert h.camera.calls == [True, False]
        assert h.m.is_off()

    def test_key_back_during_grace_stays_up(self):
        h = Harness(target_active=True, api={})
        h.tick(True)
        h.tick(False)
        assert h.state == STATE_GRACE
        h.tick(True)
        assert h.state == STATE_RUNNING
        assert h.verbs() == []                    # never stopped/started

    def test_startup_with_key_off_and_stack_down_cuts_camera(self):
        h = Harness()
        h.tick(False)
        assert h.state == STATE_OFF
        assert h.camera.calls == [False]
        assert h.verbs() == []

    def test_startup_key_off_stops_stray_stack_without_camera_power(self):
        """Units started by hand while parked (2026-11-15 09:58): a backend
        start with ACC off stops them at once and never switches ch4 on."""
        h = Harness(target_active=True, api={})
        h.stop_lag = 1                            # units still deactivating on the first poll
        h.tick(False)
        assert h.state == STATE_STOPPING
        assert h.verbs() == ["stop"]
        assert h.camera.calls == []
        h.tick(False)                             # units inactive
        assert h.state == STATE_OFF
        assert h.camera.calls == [False]
        assert h.verbs() == ["stop"]
        h.tick(False)
        h.tick(False)
        assert True not in h.camera.calls and h.verbs() == ["stop"]

    def test_startup_mode_off_stops_stray_stack_without_camera_power(self):
        h = Harness(target_active=True, api={}, mode=MODE_OFF)
        h.tick(True)
        assert h.verbs() == ["stop"]
        assert h.state == STATE_OFF
        assert h.camera.calls == [False]

    def test_startup_key_on_adopts_running_stack_without_camera_glitch(self):
        """Backend restart while driving: adopt, camera only asserted ON."""
        h = Harness(target_active=True, api={})
        h.camera.on = True                        # hub powered before the restart
        h.tick(True)
        assert h.state == STATE_RUNNING
        assert h.camera.calls == [True]
        assert h.verbs() == []                    # no stop / restart
        h.tick(True)
        assert h.state == STATE_RUNNING and h.camera.calls == [True]

    def test_startup_waits_for_first_acc_report_then_adopts(self):
        """ACC not reported yet after a restart: no decision, no relay."""
        h = Harness(target_active=True, api={}, acc_wait_s=20.0)
        h.camera.on = True
        h.tick(False, acc_known=False)
        h.tick(False, acc_known=False)
        assert h.state == STATE_OFF
        assert h.camera.calls == [] and h.verbs() == []
        h.tick(True)                              # first ACC report: on
        assert h.state == STATE_RUNNING
        assert h.camera.calls == [True] and h.verbs() == []

    def test_startup_without_acc_report_counts_as_off_after_wait(self):
        h = Harness(target_active=True, api={}, acc_wait_s=20.0)
        for _ in range(3):
            h.tick(False, acc_known=False)        # t = +5, +10, +15
        assert h.verbs() == []
        h.tick(False, acc_known=False)            # +20: give up waiting
        assert h.verbs() == ["stop"]
        assert h.camera.calls == [False]

    def test_startup_mode_on_adopts_with_key_off(self):
        h = Harness(target_active=True, api={}, mode=MODE_ON)
        h.tick(False, acc_known=False)            # mode on needs no ACC
        assert h.state == STATE_RUNNING
        assert h.camera.calls == [True] and h.verbs() == []

    def test_start_timeout_runs_degraded(self):
        h = Harness(start_timeout_s=20.0)
        h.tick(True)
        h.tick(True, dt=8)                        # start issued
        assert h.state == STATE_STARTING
        h.tick(True, dt=25)                       # API still silent
        assert h.state == STATE_RUNNING
        assert h.m.status()["api_ok"] is False

    def test_stop_timeout_gives_up_and_cuts_camera(self):
        h = Harness(target_active=True, api={}, stop_timeout_s=10.0, grace_s=0.0)
        h.tick(True)
        h.tick(False)                             # grace 0 -> stop immediately next tick
        h.tick(False)
        assert h.state == STATE_STOPPING

        # A unit that refuses to die: keep reporting active.
        def stubborn(cmd, timeout=10):
            h.systemctl.append(cmd[1:])
            if cmd[1] == "is-active":
                return R(0, "active\nactive\n")
            return R(0)
        h.m._run = stubborn
        h.tick(False, dt=5)
        assert h.state == STATE_STOPPING
        h.tick(False, dt=10)
        assert h.state == STATE_OFF
        assert "stop timeout" in h.m.status()["last_error"]
        assert h.camera.on is False


class TestWatchdogAndModes:
    def _running(self, **cfg):
        h = Harness(target_active=True, api={}, **cfg)
        h.tick(True)
        assert h.state == STATE_RUNNING
        return h

    def test_api_silence_restarts_target_once_per_cooldown(self):
        h = self._running()
        h.api = None
        h.tick(True)                              # silence starts
        h.tick(True, dt=50)
        assert h.verbs() == []
        h.tick(True, dt=60)                       # > 100 s silent
        assert h.verbs() == ["restart"]
        assert h.m.status()["restarts"] == 1
        h.tick(True, dt=120)                      # still silent, within cooldown
        assert h.verbs() == ["restart"]
        h.tick(True, dt=200)
        assert h.verbs() == ["restart", "restart"]

    def test_api_silence_watchdog_disabled(self):
        h = self._running(api_dead_restart_s=0.0)
        h.api = None
        for _ in range(10):
            h.tick(True, dt=60)
        assert h.verbs() == []

    def test_operator_restart(self):
        h = self._running()
        h.m.request_restart()
        h.tick(True)
        assert h.verbs() == ["restart"]
        h.tick(True)
        assert h.verbs() == ["restart"]           # one-shot

    def test_mode_on_runs_with_key_off(self):
        h = Harness()
        h.tick(False)
        assert h.state == STATE_OFF
        assert h.m.set_mode(MODE_ON) is True
        h.tick(False)
        assert h.state == STATE_CAMERA_ON
        h.tick(False, dt=8)
        assert h.state == STATE_STARTING
        h.api = {}
        h.tick(False)
        assert h.state == STATE_RUNNING
        assert h.m.status()["mode"] == MODE_ON and h.m.status()["desired"] is True
        h.m.set_mode(MODE_AUTO)
        h.tick(False)
        assert h.state == STATE_GRACE

    def test_mode_off_stops_without_grace_even_with_key_on(self):
        h = self._running()
        h.m.set_mode(MODE_OFF)
        h.tick(True)
        assert h.state == STATE_GRACE
        h.tick(True)                              # grace is 0 in mode off
        assert h.state == STATE_STOPPING
        h.tick(True)
        assert h.state == STATE_OFF
        h.tick(True, dt=60)
        assert h.state == STATE_OFF               # stays down while mode off
        h.m.set_mode(MODE_AUTO)
        h.tick(True)
        assert h.state == STATE_CAMERA_ON

    def test_invalid_mode_rejected(self):
        h = Harness()
        try:
            h.m.set_mode("sometimes")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
        assert h.m.set_mode(MODE_AUTO) is False   # unchanged

    def test_camera_relay_disabled_starts_directly(self):
        h = Harness(camera_relay=False)
        h.tick(True)
        assert h.state == STATE_STARTING
        assert h.camera.calls == []
        assert h.m.status()["camera_power"] is None

    def test_publish_on_change_and_periodic(self):
        h = self._running()
        n = len(h.published)
        h.tick(True)                              # nothing changed, < refresh period
        assert len(h.published) == n
        h.tick(True, dt=11)                       # periodic refresh
        assert len(h.published) == n + 1
        assert h.published[-1]["state"] == STATE_RUNNING

    def test_systemctl_failure_is_recorded_not_raised(self):
        h = Harness(camera_relay=False)

        def boom(cmd, timeout=10):
            raise OSError("no systemd here")
        h.m._run = boom
        h.tick(True)
        assert h.state == STATE_STARTING
        assert "no systemd here" in h.m.status()["last_error"]


class TestPowerModeRuleHold:
    class _Store:
        def __init__(self):
            self.dispatched = []

        def dispatch(self, a):
            self.dispatched.append(a)

    class _PB:
        def __init__(self, acc):
            self.acc_on = acc

    class _State:
        def __init__(self, acc):
            self.powerbox = TestPowerModeRuleHold._PB(acc)

    def test_low_held_while_roadsense_active(self):
        applied = []
        rule = PowerModeRule(apply_mode=applied.append, hold_low=lambda: True)
        store = self._Store()
        rule.evaluate(self._State(True), self._State(False), store)
        assert applied == []
        assert store.dispatched == []

    def test_full_never_held(self):
        applied = []
        rule = PowerModeRule(apply_mode=applied.append, hold_low=lambda: True)
        store = self._Store()
        rule.evaluate(self._State(False), self._State(True), store)
        assert applied == [True]
        assert store.dispatched[-1].mode == "full"

    def test_low_applied_when_not_held(self):
        applied = []
        rule = PowerModeRule(apply_mode=applied.append, hold_low=lambda: False)
        store = self._Store()
        rule.evaluate(self._State(True), self._State(False), store)
        assert applied == [False]
        assert store.dispatched[-1].mode == "low"


class TestServicePowerProfileHold:
    """BackendService._roadsense_power_tick: 'full' while the stack is meant to
    run, deferred 'low' once it is off with the key off."""

    class _Ctl:
        def __init__(self):
            self.modes = []

        def set_mode(self, mode):
            self.modes.append(mode)
            return True

        def set_for_ignition(self, acc):
            self.modes.append("full" if acc else "low")
            return True

    def _svc(self, acc, power_mode):
        from cyberpunk_computer.backend.service import BackendService
        from cyberpunk_computer.state.store import Store
        from cyberpunk_computer.state.actions import (
            SetPowerboxIgnitionAction, SetPowerboxPowerModeAction)
        svc = BackendService.__new__(BackendService)
        store = Store()
        store.dispatch(SetPowerboxIgnitionAction(acc_on=acc, batt_present=True))
        store.dispatch(SetPowerboxPowerModeAction(power_mode))

        class Twin:
            pass
        svc.twin = Twin()
        svc.twin.store = store
        svc.power_controller = self._Ctl()
        svc._low_mode_deferred = False
        svc._acc_seen = True                  # the ignition report above
        h = Harness(camera_relay=False)
        svc.roadsense_power = h.m
        return svc, h

    def test_full_held_while_active_then_low_after_stop(self):
        svc, h = self._svc(acc=False, power_mode="low")
        h.m.set_mode(MODE_ON)                 # operator keeps the stack up, key off
        h.t += 5
        svc._roadsense_power_tick()
        assert h.state == STATE_STARTING
        assert svc.power_controller.modes == ["full"]
        assert svc._low_mode_deferred is True
        h.m.set_mode(MODE_AUTO)               # back to following ACC (off)
        for _ in range(4):                    # grace 0? no: grace 30 -> stop -> off
            h.t += 20
            svc._roadsense_power_tick()
        assert h.m.is_off()
        assert svc.power_controller.modes[-1] == "low"
        assert svc._low_mode_deferred is False

    def test_orphan_full_with_key_off_goes_low(self):
        """Backend restarted during a roadsense grace: the flag still says
        "full", the stack is off, the key is off -> low, once."""
        svc, h = self._svc(acc=False, power_mode="full")
        assert h.m.is_off()
        svc._roadsense_power_tick()
        assert svc.power_controller.modes == ["low"]
        assert svc.twin.store.state.powerbox.power_mode == "low"
        svc._roadsense_power_tick()
        assert svc.power_controller.modes == ["low"]

    def test_orphan_full_waits_for_the_first_acc_report(self):
        svc, h = self._svc(acc=False, power_mode="full")
        svc._acc_seen = False
        svc._roadsense_power_tick()
        assert svc.power_controller.modes == []

    def test_full_with_key_on_is_left_alone(self):
        svc, h = self._svc(acc=True, power_mode="full")
        svc._roadsense_power_tick()
        assert "low" not in svc.power_controller.modes

    def test_hold_hook_arms_deferral_only_when_active(self):
        svc, h = self._svc(acc=True, power_mode="full")
        assert svc._roadsense_holds_low() is False
        h.t += 5
        svc._roadsense_power_tick()           # ACC on -> starting
        assert h.m.is_active()
        assert svc._roadsense_holds_low() is True
        assert svc._low_mode_deferred is True


class TestEarlyCamera:
    """Door unlocked before ACC: the camera hub is powered early, without the stack."""

    def test_early_demand_powers_the_camera_then_the_stack_starts_without_settle(self):
        h = Harness()
        h.tick(False)
        assert h.state == "off" and h.camera.read() is False
        h.m.set_early_camera(True)
        h.tick(False, dt=0.5)                       # reacts on the next loop pass
        assert h.state == "preview" and h.camera.read() is True
        assert h.verbs() == []                      # no unit is started for it
        h.tick(True)                                # the key: straight to the start, hub already up
        assert h.state == "starting" and h.verbs() == ["start"]
        h.api = {"fps": {}}
        h.tick(True)
        assert h.state == "running"

    def test_early_demand_without_a_key_ends_after_the_limit(self):
        h = Harness(early_camera_s=20.0)
        h.tick(False)
        h.m.set_early_camera(True)
        h.tick(False, dt=0.5)
        assert h.camera.read() is True
        for _ in range(3):
            h.m.set_early_camera(True)              # the demand is a level: repeated every pass
            h.tick(False)
        assert h.state == "preview"
        h.m.set_early_camera(True)
        h.tick(False, dt=10.0)                      # 25.5 s after the demand
        assert h.state == "off" and h.camera.read() is False
        h.m.set_early_camera(True)                  # the same demand does not bring it back
        h.tick(False)
        assert h.state == "off"
        h.m.set_early_camera(False)
        h.m.set_early_camera(True)                  # a new one does
        h.tick(False, dt=0.5)
        assert h.state == "preview"

    def test_early_demand_dropped_or_disabled(self):
        h = Harness()
        h.tick(False)
        h.m.set_early_camera(True)
        h.tick(False, dt=0.5)
        h.m.set_early_camera(False)
        h.tick(False)
        assert h.state == "off" and h.camera.read() is False
        h = Harness(early_camera_s=0.0)
        h.tick(False)
        h.m.set_early_camera(True)
        h.tick(False)
        assert h.state == "off" and h.camera.read() is False
