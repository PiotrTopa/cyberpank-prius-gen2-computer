"""MFD manager last resort: restart mfd.service when the board is up and awake
but its frontend holds no connection to the backend's ZMQ PUB port."""
import socket
from unittest import mock

from cyberpunk_computer.backend.mfd_power import MfdPowerConfig, MfdPowerManager


def _manager(conn, clockh, reach, spawned, **cfg):
    def runner(cmd, timeout=10):
        if cmd and cmd[0] == "ping":
            return mock.Mock(returncode=0 if reach["v"] else 1, stdout="")
        return mock.Mock(returncode=0, stdout="")
    return MfdPowerManager(
        MfdPowerConfig(tick_s=5.0, **cfg),
        runner=runner,
        spawner=lambda cmd: spawned.append(list(cmd)),
        iface_exists=lambda iface: True,
        clock=lambda: clockh["t"],
        controller=mock.Mock(),
        iface_configured=lambda iface, cidr: True,
        frontend_connected=lambda: conn["v"],
    )


def _restarts(spawned):
    return [c for c in spawned if "try-restart mfd.service" in c[-1]]


def _run(m, clockh, acc, seconds):
    end = clockh["t"] + seconds
    while clockh["t"] < end:
        clockh["t"] += 5.0
        m.tick(acc)


def test_restart_after_window_then_rate_limited():
    conn, clockh, reach, spawned = {"v": False}, {"t": 100.0}, {"v": True}, []
    m = _manager(conn, clockh, reach, spawned)
    m.tick(True)                                   # adopt -> ON
    assert m.state == "on"
    _run(m, clockh, True, m.config.frontend_restart_s - 10)
    assert _restarts(spawned) == []
    _run(m, clockh, True, 20)
    assert len(_restarts(spawned)) == 1
    cmd = _restarts(spawned)[0]
    assert cmd[:2] == ["sudo", "ssh"] and cmd[-2] == "piotr@192.168.100.2"
    # still missing: no second restart inside the minimum interval
    _run(m, clockh, True, m.config.frontend_restart_min_interval_s - 30)
    assert len(_restarts(spawned)) == 1
    _run(m, clockh, True, 60)
    assert len(_restarts(spawned)) == 2
    assert m.status()["frontend_restarts"] == 2


def test_connected_frontend_is_never_restarted():
    conn, clockh, reach, spawned = {"v": True}, {"t": 100.0}, {"v": True}, []
    m = _manager(conn, clockh, reach, spawned)
    m.tick(True)
    _run(m, clockh, True, 3600)
    assert _restarts(spawned) == []


def test_short_gaps_reset_the_window():
    conn, clockh, reach, spawned = {"v": False}, {"t": 100.0}, {"v": True}, []
    m = _manager(conn, clockh, reach, spawned)
    m.tick(True)
    for _ in range(10):
        _run(m, clockh, True, m.config.frontend_restart_s - 30)
        conn["v"] = True
        _run(m, clockh, True, 5)
        conn["v"] = False
    assert _restarts(spawned) == []


def test_never_in_dark_standby_unreachable_or_unknown():
    # dark grace (key off): the UI is frozen on purpose
    conn, clockh, reach, spawned = {"v": False}, {"t": 100.0}, {"v": True}, []
    m = _manager(conn, clockh, reach, spawned)
    m.tick(False)                                  # adopt -> GRACE, dark
    assert m.state == "grace"
    _run(m, clockh, False, 900)
    assert _restarts(spawned) == []
    # unreachable board (the power-cycle watchdog owns that case)
    conn, clockh, reach, spawned = {"v": False}, {"t": 100.0}, {"v": False}, []
    m = _manager(conn, clockh, reach, spawned, unreachable_recover_s=0)
    m.tick(True)
    _run(m, clockh, True, 900)
    assert _restarts(spawned) == []
    # probe cannot tell
    conn, clockh, reach, spawned = {"v": None}, {"t": 100.0}, {"v": True}, []
    m = _manager(conn, clockh, reach, spawned)
    m.tick(True)
    _run(m, clockh, True, 900)
    assert _restarts(spawned) == []


def test_window_restarts_after_restore_from_dark():
    conn, clockh, reach, spawned = {"v": False}, {"t": 100.0}, {"v": True}, []
    m = _manager(conn, clockh, reach, spawned)
    m.tick(False)                                  # GRACE, dark
    _run(m, clockh, False, 600)
    _run(m, clockh, True, m.config.frontend_restart_s - 20)   # ACC back -> ON
    assert m.state == "on" and _restarts(spawned) == []
    _run(m, clockh, True, 40)
    assert len(_restarts(spawned)) == 1


def test_disabled_with_zero():
    conn, clockh, reach, spawned = {"v": False}, {"t": 100.0}, {"v": True}, []
    m = _manager(conn, clockh, reach, spawned, frontend_restart_s=0)
    m.tick(True)
    _run(m, clockh, True, 3600)
    assert _restarts(spawned) == []


def test_default_probe_sees_an_established_connection():
    """/proc/net/tcp probe against a real loopback connection."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    m = MfdPowerManager(MfdPowerConfig(board_ip="127.0.0.1", frontend_port=port),
                        runner=lambda *a, **k: None, spawner=lambda c: None,
                        controller=mock.Mock())
    assert m._default_frontend_connected() is False
    cli = socket.create_connection(("127.0.0.1", port))
    acc, _ = srv.accept()
    try:
        assert m._default_frontend_connected() is True
        other = MfdPowerManager(MfdPowerConfig(board_ip="192.0.2.7", frontend_port=port),
                                runner=lambda *a, **k: None, spawner=lambda c: None,
                                controller=mock.Mock())
        assert other._default_frontend_connected() is False
    finally:
        cli.close()
        acc.close()
        srv.close()
