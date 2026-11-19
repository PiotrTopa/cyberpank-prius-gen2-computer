"""USB tree watch and the MFD watchdog's hold-off during whole-tree drops."""
from unittest import mock

from cyberpunk_computer.backend.mfd_power import MfdPowerConfig, MfdPowerManager
from cyberpunk_computer.backend.usb_tree import UsbTreeWatch


def _watch(clockh, devh, **kw):
    return UsbTreeWatch(poll_s=1.0, clock=lambda: clockh["t"],
                        read_devnum=lambda hub: devh["v"], **kw)


def test_reenumeration_and_absence_are_counted_once():
    clockh, devh = {"t": 100.0}, {"v": "2"}
    w = _watch(clockh, devh)
    w.poll()
    assert w.total_resets == 0 and w.last_reset_age() is None
    clockh["t"] += 1; devh["v"] = None          # hub gone
    w.poll()
    assert w.total_resets == 1 and w.last_reset_age() == 0.0
    clockh["t"] += 1; w.poll()                  # still gone: not counted again
    assert w.total_resets == 1
    clockh["t"] += 1; devh["v"] = "19"          # back with a new devnum
    w.poll()
    assert w.total_resets == 1                  # same event
    assert w.last_reset_age() == 2.0
    clockh["t"] += 1; devh["v"] = "28"          # left and came back between polls
    w.poll()
    assert w.total_resets == 2


def test_storm_flag_follows_the_window():
    clockh, devh = {"t": 0.0}, {"v": "1"}
    w = _watch(clockh, devh, storm_n=3, storm_window_s=600.0)
    w.poll()
    for n in range(2, 5):
        clockh["t"] += 60; devh["v"] = str(n); w.poll()
    assert w.storm()
    clockh["t"] += 601
    assert not w.storm()


def _mfd(clockh, ping_ok, tree_age):
    def runner(cmd, timeout=10):
        rc = 0 if (cmd and cmd[0] == "ping" and ping_ok["v"]) or (cmd and cmd[0] != "ping") else 1
        return mock.Mock(returncode=rc, stdout="")
    ctl = mock.Mock()
    m = MfdPowerManager(
        MfdPowerConfig(tick_s=5.0, unreachable_recover_s=45.0),
        runner=runner, spawner=lambda cmd: None,
        iface_exists=lambda iface: True, clock=lambda: clockh["t"],
        controller=ctl, iface_configured=lambda i, c: True,
        frontend_connected=lambda: True,
        tree_reset_age=lambda: tree_age["v"],
    )
    return m, ctl


def _run(m, clockh, seconds):
    for _ in range(int(seconds / 5)):
        clockh["t"] += 5.0
        m.tick(True)


def test_no_power_cycle_while_the_tree_keeps_dropping():
    clockh, ping, tree = {"t": 1000.0}, {"v": True}, {"v": None}
    m, ctl = _mfd(clockh, ping, tree)
    m.tick(True)                                 # adopt -> ON
    assert m.status()["state"] == "on"
    ping["v"] = False
    tree["v"] = 10.0                             # a tree drop 10 s ago, every tick
    _run(m, clockh, 300)
    assert m.status()["power_cycles"] == 0


def test_power_cycle_once_the_tree_is_quiet_and_backoff_doubles():
    clockh, ping, tree = {"t": 1000.0}, {"v": True}, {"v": None}
    m, ctl = _mfd(clockh, ping, tree)
    m.tick(True)
    ping["v"] = False
    _run(m, clockh, 55)                          # 45 s window + tick slack
    assert m.status()["power_cycles"] == 1
    # back in ON after the boot (ping still dead): next window is 90 s
    ping["v"] = True; _run(m, clockh, 10)
    ping["v"] = False
    before = m.status()["power_cycles"]
    assert m.status()["watchdog_cycles_in_row"] == 0   # a good ping reset it
    _run(m, clockh, 55)
    assert m.status()["power_cycles"] == before + 1
    # no good ping since: the window doubles
    m._enter("on")                               # boot "done" without a ping
    _run(m, clockh, 60)
    assert m.status()["power_cycles"] == before + 1
    _run(m, clockh, 40)
    assert m.status()["power_cycles"] == before + 2
