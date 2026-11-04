"""Tests for backend.port_power — hub port power controllers.

Covers the two hard-won bring-up rules:
  * VBUS-off first, data-off right after the disconnect (tick()); data-on
    before VBUS-on (bus-poisoning avoidance, order fixed 2026-11-04);
  * lossy relay commands converge via enforce() against the "rly" telemetry.
"""

from unittest import mock

from cyberpunk_computer.backend.port_power import (
    HubPortPower,
    PortPowerConfig,
    RelayPortPower,
    UhubctlPortPower,
    parse_uhubctl_port_power,
)


UHUBCTL_OUTPUT = """\
Current status for hub 2 [1d6b:0003 xHCI Host Controller, USB 3.00, 1 ports, nops]
  Port 1: 02a0 power 5gbps Rx.Detect
Current status for hub 1-1 [2101:8500 Action Star USB2.0 Hub, USB 2.00, 5 ports, ganged]
  Port 1: 0503 power highspeed enable connect [2101:8501 Action Star USB HID]
  Port 2: 0100 power
  Port 3: 0000 off
  Port 4: 0503 power highspeed enable connect [0bda:2838 Realtek]
  Port 5: 0103 power enable connect [2e8a:0005 MicroPython Board]
Current status for hub 1 [1d6b:0002 xHCI Host Controller, USB 2.00, 1 ports, nops]
  Port 1: 0503 power highspeed enable connect [2101:8500 Action Star]
"""


class TestParseUhubctl:
    def test_powered_port(self):
        assert parse_uhubctl_port_power(UHUBCTL_OUTPUT, "1-1", 2) is True
        assert parse_uhubctl_port_power(UHUBCTL_OUTPUT, "1-1", 5) is True

    def test_cut_port(self):
        assert parse_uhubctl_port_power(UHUBCTL_OUTPUT, "1-1", 3) is False

    def test_unknown_hub(self):
        assert parse_uhubctl_port_power(UHUBCTL_OUTPUT, "9-9", 2) is None

    def test_does_not_match_other_hub_sections(self):
        # "Port 1" exists in three hubs; only hub 1-1's line must be used.
        assert parse_uhubctl_port_power(UHUBCTL_OUTPUT, "1-1", 1) is True


def _runner_recorder(calls, returncode=0, stdout=""):
    def run(cmd, timeout=10):
        calls.append(cmd)
        return mock.Mock(returncode=returncode, stdout=stdout)
    return run


class TestUhubctlPortPower:
    def test_set_and_enforce(self):
        calls = []
        c = UhubctlPortPower("1-1", 5, runner=_runner_recorder(calls, stdout=UHUBCTL_OUTPUT))
        assert c.set(False)
        assert c.desired is False
        # Telemetry says port 5 is ON -> enforce re-applies OFF.
        c.enforce()
        offs = [cmd for cmd in calls if "off" in cmd]
        assert len(offs) == 2

    def test_read(self):
        c = UhubctlPortPower("1-1", 3, runner=_runner_recorder([], stdout=UHUBCTL_OUTPUT))
        assert c.read() is False


class TestRelayPortPower:
    def _make(self, relays_holder, sent, uhub_calls, clock_holder,
              fresh_holder=None, present_holder=None, stdout=""):
        return RelayPortPower(
            "sdr", relay_ch=2, hub="1-1", data_port=4,
            send_relay=lambda ch, on: (sent.append((ch, on)), True)[1],
            get_relays=lambda: relays_holder["v"],
            runner=_runner_recorder(uhub_calls, stdout=stdout),
            clock=lambda: clock_holder["t"],
            telemetry_fresh=(None if fresh_holder is None
                             else lambda: fresh_holder["v"]),
            device_present=(None if present_holder is None
                            else lambda: present_holder["v"]),
        )

    def test_off_sequence_vbus_first_then_data_after_disconnect(self):
        relays = {"v": (0, 1, 0, 0)}
        sent, uhub, clockh = [], [], {"t": 0.0}
        present = {"v": True}
        c = self._make(relays, sent, uhub, clockh, present_holder=present)
        c.set(False)
        assert sent == [(2, False)]
        assert uhub == []                   # data stays on until the device drops
        assert c.off_pending
        clockh["t"] = 0.5
        c.tick()
        assert uhub == []
        present["v"] = False                # kernel saw the disconnect
        c.tick()
        assert len(uhub) == 1 and "off" in uhub[0]
        assert not c.off_pending
        c.tick()
        assert len(uhub) == 1               # once

    def test_off_sequence_data_off_after_timeout(self):
        relays = {"v": (0, 1, 0, 0)}
        sent, uhub, clockh = [], [], {"t": 0.0}
        present = {"v": True}               # never disconnects (back-fed?)
        c = self._make(relays, sent, uhub, clockh, present_holder=present)
        c.set(False)
        clockh["t"] = 2.9
        c.tick()
        assert uhub == []
        clockh["t"] = 3.0
        c.tick()
        assert len(uhub) == 1 and "off" in uhub[0]

    def test_on_cancels_pending_data_off(self):
        relays = {"v": (0, 1, 0, 0)}
        sent, uhub, clockh = [], [], {"t": 0.0}
        present = {"v": True}
        c = self._make(relays, sent, uhub, clockh, present_holder=present)
        c.set(False)
        c.set(True)                         # wanted again right away
        clockh["t"] = 10.0
        c.tick()
        assert [u for u in uhub if "off" in u] == []
        assert uhub and "on" in uhub[0]

    def test_on_sequence_data_first_then_vbus(self):
        relays = {"v": (0, 0, 0, 0)}
        sent, uhub, clockh = [], [], {"t": 0.0}
        order = []
        c = RelayPortPower(
            "sdr", relay_ch=2, hub="1-1", data_port=4,
            send_relay=lambda ch, on: (order.append(("relay", on)), True)[1],
            get_relays=lambda: relays["v"],
            runner=lambda cmd, timeout=10: (order.append(("data", cmd[-1])),
                                            mock.Mock(returncode=0, stdout=""))[1],
            clock=lambda: clockh["t"],
        )
        c.set(True)
        assert order == [("data", "on"), ("relay", True)]

    def test_enforce_does_not_rush_the_data_off(self):
        """enforce() must not treat a port whose OFF is still pending as
        data-drifted (uhubctl still shows it powered) — tick() finishes it."""
        relays = {"v": (0, 0, 0, 0)}            # VBUS already off per mirror
        sent, uhub, clockh = [], [], {"t": 0.0}
        present = {"v": True}
        c = self._make(relays, sent, uhub, clockh, present_holder=present,
                       stdout=UHUBCTL_OUTPUT)  # port 4 reads "power" (enabled)
        c.set(False)
        clockh["t"] = 1.0
        c.enforce()
        assert uhub == [] and sent == [(2, False)]
        present["v"] = False
        clockh["t"] = 2.0
        c.enforce()
        assert len(uhub) == 1 and "off" in uhub[0]
        assert sent == [(2, False)]

    def test_no_data_drift_on_an_empty_port_of_a_ganged_hub(self):
        """After a clean OFF the top hub keeps reporting "power" on the dark
        port (ganged hub, 2026-11-04). With no device on the port that is not
        drift and must not even cost a uhubctl read; a device re-appearing
        on the dark port is drift and re-applies the sequence."""
        relays = {"v": (0, 0, 0, 0)}
        sent, uhub, clockh = [], [], {"t": 0.0}
        present = {"v": True}
        c = self._make(relays, sent, uhub, clockh, present_holder=present,
                       stdout=UHUBCTL_OUTPUT)  # port 4 reads "power" forever
        c.set(False)
        present["v"] = False
        clockh["t"] = 1.0
        c.tick()
        assert len(uhub) == 1 and "off" in uhub[0]   # OFF finished
        clockh["t"] = 20.0
        c.enforce()
        clockh["t"] = 40.0
        c.enforce()
        assert sent == [(2, False)]                  # no re-apply
        assert len(uhub) == 1                        # no `uhubctl -f` either
        present["v"] = True                          # zombie back on the port
        clockh["t"] = 60.0
        c.enforce()
        assert sent == [(2, False), (2, False)]      # re-applied
        assert c.off_pending

    def test_read_maps_channel(self):
        relays = {"v": (0, 1, 0, 0)}  # ch2 powered
        c = self._make(relays, [], [], {"t": 0.0})
        assert c.read() is True
        relays["v"] = (0, 0, 0, 0)
        assert c.read() is False
        relays["v"] = None
        assert c.read() is None

    def test_enforce_reapplies_on_drift(self):
        relays = {"v": (0, 0, 0, 0)}  # actual OFF
        sent, uhub, clockh = [], [], {"t": 0.0}
        c = self._make(relays, sent, uhub, clockh)
        c.set(True)          # lossy command "lost": telemetry stays OFF
        assert len(sent) == 1
        clockh["t"] = 3.0
        c.enforce()          # within min interval -> no re-send yet
        assert len(sent) == 1
        clockh["t"] = 6.0
        c.enforce()          # drift + interval passed -> re-apply
        assert len(sent) == 2
        relays["v"] = (0, 1, 0, 0)  # command landed
        clockh["t"] = 12.0
        c.enforce()          # converged -> no more sends
        assert len(sent) == 2

    def test_enforce_suspends_vbus_drift_while_telemetry_stale(self):
        """A wedged powerbox link must not drive endless relay re-issues.

        The ``rly`` mirror freezes at its last value while the USB-CDC link is
        wedged, but commands still go out — so an ungated enforce() re-issues
        forever (relay chatter + enumeration storm, 2026-08-31).
        """
        relays = {"v": (0, 0, 0, 0)}   # mirror frozen at all-off
        sent, uhub, clockh = [], [], {"t": 0.0}
        fresh = {"v": True}
        c = self._make(relays, sent, uhub, clockh, fresh_holder=fresh)
        c.set(True)
        assert len(sent) == 1

        # Link wedges: mirror can no longer be trusted -> no re-issue, ever.
        fresh["v"] = False
        for t in (6.0, 12.0, 60.0, 600.0):
            clockh["t"] = t
            c.enforce()
        assert len(sent) == 1, "stale telemetry must not trigger re-application"

        # Frames resume and confirm the command never landed -> converge again.
        fresh["v"] = True
        clockh["t"] = 700.0
        c.enforce()
        assert len(sent) == 2

    def test_enforce_noop_without_desired_or_telemetry(self):
        relays = {"v": None}
        sent = []
        c = self._make(relays, sent, [], {"t": 100.0})
        c.enforce()  # no desired state yet
        assert sent == []
        c._desired = True
        c._last_apply = 0.0
        c.enforce()  # telemetry unknown -> no blind re-send
        assert sent == []


class TestHubPortPower:
    def test_build_wires_channels(self):
        hub = HubPortPower.build(
            PortPowerConfig(),
            send_relay=lambda ch, on: True,
            get_relays=lambda: (0, 0, 0, 0),
        )
        # fw 2.1 board: relay N = channel N. ch3 = CAN hub (gateway + panda),
        # ch2 = MFD Pi, ch4 = camera hub; ch1 (restart) is never a port.
        assert hub.gateway.relay_ch == 3
        assert hub.gateway.data_port == 2
        assert hub.mfd.relay_ch == 2
        assert hub.mfd.data_port == 3
        assert hub.camera.relay_ch == 4
        assert hub.camera.data_port == 4
        assert hub.powerbox.port == 5
        assert 1 not in {c.relay_ch for c in hub.relay_ports}

    def test_tick_all_finishes_pending_offs_and_survives_errors(self):
        hub = HubPortPower.build(
            PortPowerConfig(),
            send_relay=lambda ch, on: True,
            get_relays=lambda: (None, 0, 0, 0, 0, 0, 0, 0),
            runner=_runner_recorder([]),
        )
        hub.gateway.tick = mock.Mock(side_effect=RuntimeError("boom"))
        hub.camera._device_present = lambda: False
        hub.camera.set(False)
        assert hub.camera.off_pending
        hub.tick_all()                      # must not raise
        assert not hub.camera.off_pending   # camera's data-off was finished

    def test_enforce_all_survives_errors(self):
        hub = HubPortPower.build(
            PortPowerConfig(),
            send_relay=lambda ch, on: True,
            get_relays=lambda: (0, 0, 0, 0),
        )
        hub.gateway.enforce = mock.Mock(side_effect=RuntimeError("boom"))
        hub.mfd.enforce = mock.Mock()
        hub.camera.enforce = mock.Mock()
        hub.enforce_all()  # must not raise
        hub.mfd.enforce.assert_called_once()
        hub.camera.enforce.assert_called_once()

    def test_restart_relay_can_never_back_a_port(self):
        import pytest
        with pytest.raises(ValueError):
            RelayPortPower("x", relay_ch=1, hub="1-1", data_port=2,
                           send_relay=lambda ch, on: True, get_relays=lambda: None)
        with pytest.raises(ValueError):
            HubPortPower.build(PortPowerConfig(camera_relay_ch=1),
                               send_relay=lambda ch, on: True, get_relays=lambda: None)
        with pytest.raises(ValueError):
            HubPortPower.build(PortPowerConfig(camera_relay_ch=3),  # duplicate of gateway
                               send_relay=lambda ch, on: True, get_relays=lambda: None)

    def test_reassert_all_after_powerbox_boot(self):
        """After a powerbox (re)boot every NO relay is released: the recorded
        desired states must be pushed again regardless of the mirror."""
        sent = []
        hub = HubPortPower.build(
            PortPowerConfig(),
            send_relay=lambda ch, on: (sent.append((ch, on)), True)[1],
            get_relays=lambda: (None, 1, 1, 0, 0, 0, 0, 0),  # stale mirror: all fine
            runner=_runner_recorder([]),
        )
        hub.gateway.set(True)
        hub.mfd.set(True)
        sent.clear()
        assert hub.reassert_all() == 2          # camera has no desired state
        assert sorted(sent) == [(2, True), (3, True)]

    def test_read_treats_null_mirror_entry_as_unknown(self):
        hub = HubPortPower.build(
            PortPowerConfig(),
            send_relay=lambda ch, on: True,
            get_relays=lambda: (None, True, None, False, False, False, False, False),
        )
        assert hub.mfd.read() is True
        assert hub.gateway.read() is None
        assert hub.camera.read() is False
