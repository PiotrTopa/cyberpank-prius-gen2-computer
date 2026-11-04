"""MFD video-board power manager: host-side link maintenance."""
import os
from unittest import mock

import pytest

from cyberpunk_computer.backend.mfd_power import MfdPowerConfig, MfdPowerManager


def _manager(calls, clockh, configured, iface_exists=True):
    def runner(cmd, timeout=10):
        calls.append(list(cmd))
        return mock.Mock(returncode=0, stdout="")
    return MfdPowerManager(
        MfdPowerConfig(tick_s=5.0),
        runner=runner,
        spawner=lambda cmd: None,
        iface_exists=lambda iface: iface_exists,
        clock=lambda: clockh["t"],
        controller=mock.Mock(),
        iface_configured=lambda iface, cidr: configured["v"],
    )


def _adds(calls):
    return [c for c in calls if c[:4] == ["sudo", "ip", "addr", "add"]]


def test_host_link_is_reconfigured_only_when_missing():
    """Steady state costs no sudo: 217 `ip addr add` in 18 min (2026-11-04)
    came from re-applying every tick regardless of the link state."""
    calls, clockh, configured = [], {"t": 100.0}, {"v": False}
    m = _manager(calls, clockh, configured)
    m.tick(True)                       # adopts the running board -> ON
    assert m.status()["state"] == "on"
    assert len(_adds(calls)) == 1      # link was missing: configured once
    configured["v"] = True
    for _ in range(5):
        clockh["t"] += 5.0
        m.tick(True)
    assert len(_adds(calls)) == 1      # nothing to do while it is up
    configured["v"] = False            # gadget re-enumerated: address gone
    clockh["t"] += 5.0
    m.tick(True)
    assert len(_adds(calls)) == 2      # re-applied exactly once
    assert calls[-2][:4] == ["sudo", "ip", "link", "set"]   # then the ping


def test_host_link_kept_during_grace_too():
    calls, clockh, configured = [], {"t": 100.0}, {"v": True}
    m = _manager(calls, clockh, configured)
    m.tick(False)                      # adopt -> GRACE (key off)
    assert m.status()["state"] == "grace"
    clockh["t"] += 5.0
    m.tick(False)
    assert _adds(calls) == []
    configured["v"] = False
    clockh["t"] += 5.0
    m.tick(False)
    assert len(_adds(calls)) == 1


@pytest.mark.skipif(not os.path.exists("/sys/class/net/lo/flags"),
                    reason="needs Linux sysfs")
def test_default_probe_reads_flags_and_ip_output():
    calls = []
    out = {"v": "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever"}

    def runner(cmd, timeout=10):
        calls.append(list(cmd))
        return mock.Mock(returncode=0, stdout=out["v"])
    m = MfdPowerManager(MfdPowerConfig(), runner=runner, spawner=lambda c: None,
                        controller=mock.Mock())
    assert m._default_iface_configured("lo", "127.0.0.1/8") is True
    assert calls[-1][:2] == ["ip", "-o"] and "sudo" not in calls[-1]
    out["v"] = "1: lo    inet 127.0.0.2/8 scope host lo"
    assert m._default_iface_configured("lo", "127.0.0.1/8") is False
    assert m._default_iface_configured("no-such-iface0", "127.0.0.1/8") is False
