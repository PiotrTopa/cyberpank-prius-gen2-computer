"""Secondary-POCO ACC-follower state machine (backend.poco2_power)."""

from cyberpunk_computer.backend.poco2_power import (
    STATE_BOOTING,
    STATE_GRACE,
    STATE_OFF,
    STATE_ON,
    STATE_SHUTTING_DOWN,
    Poco2PowerConfig,
    Poco2PowerManager,
)


class Harness:
    def __init__(self, reachable=False, **cfg):
        self.t = 1000.0
        self.reachable = reachable
        self.presses = []
        self.spawned = []
        self.alive_reports = []
        self.published = []
        config = Poco2PowerConfig(tick_s=5.0, boot_timeout_s=60.0, grace_s=100.0,
                                  shutdown_wait_s=30.0, max_boot_presses=2,
                                  unreachable_off_s=20.0, **cfg)

        class R:
            def __init__(self, rc):
                self.returncode = rc

        self.m = Poco2PowerManager(
            config,
            press=lambda ms: self.presses.append(ms) or True,
            publish=self.published.append,
            report_alive=lambda alive: self.alive_reports.append(alive) or True,
            runner=lambda cmd, timeout=10: R(0 if self.reachable else 1),
            spawner=self.spawned.append,
            clock=lambda: self.t,
        )

    def tick(self, acc, dt=5.0):
        self.t += dt
        self.m.tick(acc)

    @property
    def state(self):
        return self.m.status()["state"]


class TestPoco2PowerManager:
    def test_key_on_presses_and_waits_for_ping(self):
        h = Harness()
        h.tick(True)
        assert h.state == STATE_BOOTING
        assert h.presses == [6000]
        h.reachable = True
        h.tick(True)
        assert h.state == STATE_ON
        assert h.alive_reports[-1] is True
        assert h.m.status()["last_boot_s"] is not None

    def test_boot_timeout_presses_again_then_gives_up_until_key_cycle(self):
        h = Harness()
        h.tick(True)                      # press #1
        h.tick(True, dt=61)               # timeout -> press #2
        assert h.presses == [6000, 6000]
        assert h.state == STATE_BOOTING
        h.tick(True, dt=61)               # timeout -> give up
        assert h.state == STATE_OFF
        h.tick(True)                      # still key-on: no third press
        assert h.presses == [6000, 6000]
        h.tick(False)                     # key off clears the latch
        h.tick(True)
        assert h.presses == [6000, 6000, 6000]

    def test_key_off_grace_then_ssh_poweroff(self):
        h = Harness(reachable=True)
        h.tick(True)                      # adopt running board
        assert h.state == STATE_ON
        h.tick(False)
        assert h.state == STATE_GRACE
        h.tick(True)                      # key back during grace
        assert h.state == STATE_ON
        h.tick(False)
        h.tick(False, dt=101)             # grace expires
        assert h.state == STATE_SHUTTING_DOWN
        assert h.spawned and h.spawned[0][-2:] == ["sudo", "poweroff"]
        assert "user@" in h.spawned[0][-3]
        h.reachable = False
        h.tick(False)
        assert h.state == STATE_OFF
        assert h.alive_reports[-1] is False

    def test_startup_adopts_running_board_in_grace_when_key_off(self):
        h = Harness(reachable=True)
        h.tick(False)
        assert h.state == STATE_GRACE

    def test_unreachable_in_on_falls_back_to_off_and_reboots(self):
        h = Harness(reachable=True)
        h.tick(True)
        assert h.state == STATE_ON
        h.reachable = False
        h.tick(True)
        h.tick(True, dt=25)
        assert h.state == STATE_OFF
        h.tick(True)
        assert h.state == STATE_BOOTING and h.presses == [6000]
