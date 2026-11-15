"""Operator MFD hold (mfd_hold command): kept across backend restarts with
its expiry, never across a phone reboot, never against the protections."""
import json
from types import SimpleNamespace
from unittest import mock

from cyberpunk_computer.backend.mfd_power import MfdPowerConfig, MfdPowerManager, OperatorHold


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _hold(path, clock, boot="boot-a", max_s=1800.0):
    return OperatorHold(str(path), max_s, clock=clock, boot_id=lambda: boot)


def _pb(**kw):
    d = dict(undervoltage=False, shutdown_requested=False, shutdown_reason="", pm_state="normal")
    d.update(kw)
    return SimpleNamespace(**d)


def test_hold_runs_out_after_max(tmp_path):
    clk = Clock()
    h = _hold(tmp_path / "h.json", clk)
    h.set(True)
    assert h.check(_pb())
    clk.t += 1799.0
    assert h.check(_pb())
    clk.t += 1.0
    assert not h.check(_pb())
    assert not (tmp_path / "h.json").exists()


def test_restart_restores_remaining_time_not_a_fresh_hold(tmp_path):
    clk = Clock()
    p = tmp_path / "run" / "h.json"           # directory created on demand
    _hold(p, clk).set(True)
    clk.t += 600.0                             # backend down / restarting meanwhile
    h2 = _hold(p, clk)
    left = h2.restore()
    assert left == 1200.0
    assert h2.check(_pb())
    clk.t += 1199.0
    assert h2.check(_pb())
    clk.t += 1.0
    assert not h2.check(_pb())                 # the original 30 min, not 30 more


def test_release_removes_the_file(tmp_path):
    clk = Clock()
    p = tmp_path / "h.json"
    h = _hold(p, clk)
    h.set(True)
    assert p.exists()
    h.set(False)
    assert not p.exists()
    assert _hold(p, clk).restore() == 0.0


def test_not_restored_after_a_phone_reboot(tmp_path):
    clk = Clock()
    p = tmp_path / "h.json"
    _hold(p, clk, boot="boot-a").set(True)
    h2 = _hold(p, Clock(50.0), boot="boot-b")  # new boot: other id, clock restarted
    assert h2.restore() == 0.0
    assert not h2.check(_pb())
    assert not p.exists()


def test_not_restored_without_a_boot_id(tmp_path):
    clk = Clock()
    p = tmp_path / "h.json"
    _hold(p, clk, boot="").set(True)
    assert _hold(p, clk, boot="").restore() == 0.0


def test_not_restored_when_expired_or_too_long_or_garbage(tmp_path):
    clk = Clock()
    p = tmp_path / "h.json"
    _hold(p, clk).set(True)
    clk.t += 1801.0
    assert _hold(p, clk).restore() == 0.0
    assert not p.exists()
    p.write_text(json.dumps({"boot_id": "boot-a", "until_boottime": clk.t + 5000.0}))
    assert _hold(p, clk).restore() == 0.0       # more than the 30 min max: refused
    p.write_text("{not json")
    assert _hold(p, clk).restore() == 0.0
    assert not p.exists()
    p.write_text(json.dumps({"boot_id": "boot-a", "until_boottime": True}))
    assert _hold(p, clk).restore() == 0.0


def test_protections_drop_a_hold(tmp_path):
    for pb in (_pb(undervoltage=True), _pb(shutdown_requested=True, shutdown_reason="uv"),
               _pb(pm_state="shutdown"), _pb(pm_state="sleep")):
        clk = Clock()
        p = tmp_path / "h.json"
        h = _hold(p, clk)
        h.set(True)
        assert OperatorHold.blocked_by(pb)
        assert not h.check(pb)
        assert not p.exists()                  # and it does not come back after a restart
        assert _hold(p, clk).restore() == 0.0
    assert OperatorHold.blocked_by(_pb()) == ""
    assert OperatorHold.blocked_by(_pb(pm_state="")) == ""   # not reported yet
    assert OperatorHold.blocked_by(None) == ""


def test_restored_hold_drops_on_undervoltage_at_once(tmp_path):
    clk = Clock()
    p = tmp_path / "h.json"
    _hold(p, clk).set(True)
    h2 = _hold(p, clk)
    assert h2.restore() > 0
    assert not h2.check(_pb(undervoltage=True))


def test_in_memory_hold_without_file():
    clk = Clock()
    h = OperatorHold("", 1800.0, clock=clk, boot_id=lambda: "x")
    h.set(True)
    assert h.check(None)
    assert h.restore() == 0.0


def test_unwritable_file_keeps_the_hold_in_memory(tmp_path):
    clk = Clock()
    blocker = tmp_path / "f"
    blocker.write_text("")
    h = _hold(blocker / "h.json", clk)       # parent is a file: cannot be written
    h.set(True)
    assert h.check(_pb())


def test_default_file_is_on_tmpfs():
    assert MfdPowerConfig().hold_file.startswith("/run/")
    assert MfdPowerConfig().hold_s == 1800.0


def test_restored_hold_keeps_the_running_board_on_not_grace(tmp_path):
    """After a backend restart with the key off, the manager adopts the
    running board as ON (no dark standby) when the restored hold is the
    early demand; without it, GRACE + dark standby as before."""
    clk = Clock()
    p = tmp_path / "h.json"
    _hold(p, clk).set(True)

    def manager(spawned):
        def runner(cmd, timeout=10):
            return mock.Mock(returncode=0, stdout="")
        return MfdPowerManager(
            MfdPowerConfig(tick_s=5.0), runner=runner, spawner=spawned.append,
            iface_exists=lambda iface: True, clock=lambda: 100.0,
            controller=mock.Mock(), iface_configured=lambda iface, cidr: True)

    h = _hold(p, clk)
    h.restore()
    spawned = []
    m = manager(spawned)
    m.tick(False, early=h.check(_pb()), why="operator hold")
    assert m.status()["state"] == "on"
    assert not any("SIGSTOP" in " ".join(map(str, c)) for c in spawned)

    spawned2 = []
    m2 = manager(spawned2)
    m2.tick(False, early=False)
    assert m2.status()["state"] == "grace"
    assert any("SIGSTOP" in " ".join(map(str, c)) for c in spawned2)
