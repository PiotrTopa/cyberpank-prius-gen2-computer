"""Secondary POCO power manager (backend.poco2_power): power button through the
powerbox, clean shutdown / restart over SSH, ACC follower, operator modes."""

import pytest

from cyberpunk_computer.api.commands import CommandError, build_command
from cyberpunk_computer.backend.poco2_power import (
    MODE_AUTO,
    MODE_OFF,
    MODE_ON,
    STATE_BOOT_FAILED,
    STATE_BOOTING,
    STATE_GRACE,
    STATE_OFF,
    STATE_ON,
    STATE_SHUTTING_DOWN,
    STATE_UNKNOWN,
    Poco2PowerConfig,
    Poco2PowerManager,
)


class R:
    def __init__(self, rc=0):
        self.returncode = rc
        self.stdout = ""
        self.stderr = ""


class Rig:
    """A fake phone: ``up`` answers ping, ``ssh`` answers SSH; a power-off or
    restart over SSH takes effect at once unless ``deaf`` is set."""

    def __init__(self, up=False, mode=MODE_AUTO, **cfg):
        self.t = 1000.0
        self.up = up
        self.ssh = True
        self.deaf = False
        self.link = True          # powerbox link (button presses get through)
        self.presses = []
        self.spawned = []
        self.alive = []
        self.published = []
        self.boots_on_press = True
        self.cfg = Poco2PowerConfig(mode=mode, **cfg)
        self.m = Poco2PowerManager(
            self.cfg, press=self._press, publish=self.published.append,
            report_alive=self.alive.append, runner=self._run, spawner=self._spawn,
            clock=lambda: self.t, wall=lambda: self.t)

    def _press(self, ms):
        if not self.link:
            return False
        self.presses.append(ms)
        return True

    def _run(self, cmd, timeout=10):
        if cmd[0] == "ping":
            return R(0 if self.up else 1)
        if cmd[0] == "ssh":
            return R(0 if (self.up and self.ssh) else 255)
        raise AssertionError(cmd)

    def _spawn(self, cmd):
        self.spawned.append(cmd)
        if not self.deaf and cmd[-1] in ("poweroff", "reboot"):
            self.up = False

    def tick(self, acc, n=1, dt=5.0, **kw):
        for _ in range(n):
            self.m.tick(acc, **kw)
            self.t += dt

    def run(self, acc, seconds, **kw):
        self.tick(acc, n=int(seconds / 5.0) + 1, **kw)


def test_startup_adopts_a_running_phone():
    r = Rig(up=True)
    assert r.m.state == STATE_UNKNOWN
    r.tick(True)
    assert r.m.state == STATE_ON
    assert r.presses == []
    assert r.m.status()["ssh_ok"] is True


def test_startup_takes_silence_as_off_then_boots_with_acc():
    r = Rig(up=False)
    r.tick(True, n=3)
    assert r.m.state == STATE_OFF
    r.tick(True)
    assert r.m.state == STATE_BOOTING and r.presses == [6000]
    r.t += 40
    r.up = True
    r.tick(True)
    assert r.m.state == STATE_ON
    assert r.presses == [6000]
    assert r.m.status()["last_boot_s"] == pytest.approx(45, abs=6)


def test_unknown_acc_holds_everything():
    r = Rig(up=False)
    r.tick(False, n=10, acc_known=False)
    assert r.m.state == STATE_OFF and r.presses == []
    r.up = True
    r.run(False, 120, acc_known=False)
    assert r.m.state == STATE_ON and r.spawned == []      # adopted, not shut down


def test_key_off_grace_then_clean_shutdown_and_current_drop():
    r = Rig(up=True, grace_s=60)
    r.tick(True, current_a=0.30)
    r.tick(False, current_a=0.30)
    assert r.m.state == STATE_GRACE and r.spawned == []
    r.run(False, 50, current_a=0.31)
    assert r.m.state == STATE_GRACE
    r.run(False, 15, current_a=0.32)
    assert r.m.state == STATE_SHUTTING_DOWN
    assert len(r.spawned) == 1
    cmd = r.spawned[0]
    assert cmd[0] == "ssh" and cmd[-4:] == ["sudo", "-n", "systemctl", "poweroff"]
    assert "user@10.42.0.94" in cmd and "/etc/prius/poco2_ssh_key" in cmd
    r.run(False, 20, current_a=0.22)
    assert r.m.state == STATE_OFF
    assert r.presses == []                # the button never switches it off
    r.run(False, 30, current_a=0.21)
    st = r.m.status()
    assert st["i_before_off_a"] == pytest.approx(0.31, abs=0.011)
    assert st["i_after_off_a"] == pytest.approx(0.21, abs=0.011)
    assert st["off_drop_a"] == pytest.approx(0.10, abs=0.021)
    assert st["last_shutdown_s"] is not None


def test_key_back_during_grace_keeps_it_up():
    r = Rig(up=True, grace_s=60)
    r.tick(True)
    r.tick(False, n=3)
    assert r.m.state == STATE_GRACE
    r.tick(True)
    assert r.m.state == STATE_ON and r.spawned == []


def test_shutdown_is_resent_while_the_phone_still_answers():
    r = Rig(up=True, mode=MODE_OFF)
    r.deaf = True
    r.tick(False, n=2)
    assert r.m.state == STATE_SHUTTING_DOWN
    r.run(False, 70)
    assert r.m.state == STATE_SHUTTING_DOWN
    assert 3 <= len(r.spawned) <= 4
    assert r.presses == []
    r.deaf = False
    r.run(False, 60)
    assert r.m.state == STATE_OFF


def test_mode_off_skips_the_grace_and_mode_on_boots_with_key_off():
    r = Rig(up=True, grace_s=600)
    r.tick(True)
    r.m.set_mode("off")
    r.tick(True)
    assert r.m.state == STATE_SHUTTING_DOWN
    r.run(True, 30)
    assert r.m.state == STATE_OFF and r.presses == []
    r.m.set_mode("on")
    r.tick(False)
    assert r.m.state == STATE_BOOTING and r.presses == [6000]
    r.up = True
    r.tick(False)
    assert r.m.state == STATE_ON
    r.run(False, 300)
    assert r.m.state == STATE_ON and len(r.spawned) == 1    # only the earlier power-off
    with pytest.raises(ValueError):
        r.m.set_mode("maybe")


def test_boot_retries_then_fails_then_new_round():
    r = Rig(up=False, boot_timeout_s=30, boot_tries=3, boot_retry_s=120)
    r.tick(True, n=4)
    assert r.presses == [6000]
    r.run(True, 100)
    assert r.presses == [6000, 6000, 6000]
    assert r.m.state == STATE_BOOT_FAILED
    assert "3 presses" in r.m.status()["last_error"]
    r.run(True, 60)
    assert r.m.state == STATE_BOOT_FAILED and len(r.presses) == 3
    r.run(True, 80)
    assert r.m.state == STATE_BOOTING and len(r.presses) >= 4
    r.up = True
    r.tick(True)
    assert r.m.state == STATE_ON and r.m.status()["last_error"] == ""


def test_no_press_without_powerbox_link():
    r = Rig(up=False)
    r.link = False
    r.tick(True, n=6)
    assert r.m.state == STATE_OFF and r.presses == []
    assert "not sent" in r.m.status()["last_error"]
    r.link = True
    r.tick(True)
    assert r.m.state == STATE_BOOTING


def test_ping_without_ssh_is_not_on():
    r = Rig(up=False, boot_timeout_s=30)
    r.tick(True, n=4)
    r.up, r.ssh = True, False
    r.run(True, 20)
    assert r.m.state == STATE_BOOTING
    r.ssh = True
    r.tick(True)
    assert r.m.state == STATE_ON
    assert r.presses == [6000]            # a phone that answers ping is not pressed again


def test_unreachable_while_on_forces_one_restart():
    r = Rig(up=True, lost_recover_s=60, recover_min_interval_s=600, boot_timeout_s=600)
    r.tick(True)
    r.up = False
    r.run(True, 50)
    assert r.m.state == STATE_ON and r.presses == []
    r.run(True, 20)
    assert r.m.state == STATE_BOOTING and r.presses == [12000]
    assert r.m.status()["forced_restarts"] == 1
    r.up = True
    r.tick(True)
    assert r.m.state == STATE_ON


def test_unreachable_recovery_can_be_disabled():
    r = Rig(up=True, lost_recover_s=0)
    r.tick(True)
    r.up = False
    r.run(True, 900)
    assert r.m.state == STATE_ON and r.presses == []


def test_went_silent_while_not_wanted_counts_as_off():
    r = Rig(up=True, grace_s=600)
    r.tick(True)
    r.tick(False)
    assert r.m.state == STATE_GRACE
    r.up = False                          # switched off by hand
    r.run(False, 30)
    assert r.m.state == STATE_OFF and r.spawned == [] and r.presses == []


def test_clean_restart_waits_for_the_phone_to_go_down():
    r = Rig(up=True)
    r.deaf = True                         # keeps answering for a while after the request
    r.tick(True)
    r.m.request_restart()
    r.tick(True)
    assert r.spawned[-1][-1] == "reboot"
    assert r.m.state == STATE_BOOTING
    r.tick(True, n=2)
    assert r.m.state == STATE_BOOTING     # still up: not taken as the new boot
    r.up = False
    r.tick(True, n=2)
    r.up = True
    r.tick(True)
    assert r.m.state == STATE_ON and r.presses == []


def test_forced_restart_uses_the_long_press():
    r = Rig(up=True)
    r.tick(True)
    r.m.request_restart(force=True)
    r.tick(True)
    assert r.presses == [12000] and r.m.state == STATE_BOOTING


def test_protection_overrides_mode_on_without_grace():
    r = Rig(up=True, mode=MODE_ON, grace_s=600)
    r.tick(True)
    r.tick(True, inhibit=True)
    assert r.m.state == STATE_SHUTTING_DOWN
    r.run(True, 30, inhibit=True)
    assert r.m.state == STATE_OFF and r.presses == []


def test_alive_is_reported_to_the_powerbox():
    r = Rig(up=True)
    r.run(True, 70)
    assert r.alive[0] is True and r.alive.count(True) >= 2
    r.up = False
    r.tick(True)
    assert r.alive[-1] is False


def test_status_is_published_on_change_only():
    r = Rig(up=True)
    r.run(True, 60)
    n = len(r.published)
    r.run(True, 60)
    assert len(r.published) <= n + 13     # last_ok_ping moves; nothing else
    assert r.published[-1]["state"] == STATE_ON


def test_commands():
    a = build_command("poco2_mode", {"mode": "ON"})
    assert type(a).__name__ == "Poco2ModeAction" and a.mode == "on"
    with pytest.raises(CommandError):
        build_command("poco2_mode", {"mode": "sleep"})
    assert build_command("poco2_restart", {}).force is False
    assert build_command("poco2_restart", {"force": True}).force is True


def test_store_mirrors_the_status():
    from cyberpunk_computer.state.actions import SetPoco2StateAction
    from cyberpunk_computer.state.store import Store

    store = Store()
    store.dispatch(SetPoco2StateAction(enabled=True, host="10.42.0.94", state="on", mode="auto"))
    p2 = store.state.poco2
    assert (p2.enabled, p2.host, p2.state, p2.mode) == (True, "10.42.0.94", "on", "auto")


def test_mfd_early_demand_starts_it_without_acc_and_holds_the_grace():
    """BEAN entry / operator hold (the MFD's ``early``) wants the phone like ACC."""
    r = Rig(up=False, grace_s=60)
    r.tick(False, n=3, early=False)
    assert r.m.state == STATE_OFF
    r.tick(False, early=True)
    assert r.m.state == STATE_BOOTING and r.presses == [6000]
    r.t += 40
    r.up = True
    r.tick(False, early=True)
    assert r.m.state == STATE_ON
    # demand ends: the same key-off grace, then the clean shutdown
    r.tick(False, n=3, early=False)
    assert r.m.state == STATE_GRACE and r.spawned == []
    r.run(False, 70, early=False)
    assert r.m.state in (STATE_SHUTTING_DOWN, STATE_OFF)
    assert r.spawned


# ── the Wi-Fi AP gate (2026-11-23: started with the AP suspended, never seen) ──

def test_no_press_while_the_ap_is_down_then_press_once_it_is_up():
    r = Rig(up=False, mode=MODE_ON)
    r.tick(False, n=4, net_up=False, net_age_s=0.0)        # UNKNOWN: waits, no press
    assert r.m.state == STATE_UNKNOWN and r.presses == []
    assert r.m.needs_network()
    r.tick(False, n=80, net_up=True, net_age_s=400.0)      # AP settled: off, then press
    assert r.presses and r.m.state in (STATE_BOOTING, STATE_ON)


def test_unknown_is_not_taken_as_off_before_the_ap_has_settled():
    r = Rig(up=False)
    r.tick(False, n=10, net_up=True, net_age_s=60.0)       # AP just came back
    assert r.m.state == STATE_UNKNOWN
    r.up = True                                            # the running phone reconnects
    r.tick(False, net_up=True, net_age_s=120.0)
    assert r.m.state == STATE_ON                           # adopted, not lost


def test_wanted_off_phone_waits_for_the_ap():
    r = Rig(up=False)
    r.tick(False, n=4)                                     # -> OFF (AP up, default)
    assert r.m.state == STATE_OFF
    assert not r.m.needs_network()
    r.m.set_mode(MODE_ON)
    r.tick(False, n=3, net_up=False, net_age_s=0.0)
    assert r.presses == [] and r.m.needs_network()
    r.tick(False, net_up=True, net_age_s=5.0)
    assert len(r.presses) == 1 and r.m.state == STATE_BOOTING


def test_boot_timeout_does_not_run_while_the_ap_is_down():
    r = Rig(up=False, mode=MODE_ON)
    r.boots_on_press = False
    r.tick(False, n=4)
    r.tick(False)
    assert r.m.state == STATE_BOOTING and len(r.presses) == 1
    r.tick(False, n=100, net_up=False, net_age_s=0.0)      # 500 s without the AP
    assert len(r.presses) == 1 and r.m.state == STATE_BOOTING
