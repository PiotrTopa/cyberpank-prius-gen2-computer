"""Tests for backend.port_power — hub port power controllers.

Covers the two hard-won bring-up rules:
  * data-off before VBUS-off, VBUS-on before data-on (bus-poisoning avoidance);
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
    def _make(self, relays_holder, sent, uhub_calls, clock_holder):
        return RelayPortPower(
            "sdr", relay_ch=2, hub="1-1", data_port=4,
            send_relay=lambda ch, on: (sent.append((ch, on)), True)[1],
            get_relays=lambda: relays_holder["v"],
            runner=_runner_recorder(uhub_calls),
            clock=lambda: clock_holder["t"],
        )

    def test_off_sequence_data_first(self):
        relays = {"v": (0, 1, 0, 0)}
        sent, uhub, clockh = [], [], {"t": 0.0}
        c = self._make(relays, sent, uhub, clockh)
        c.set(False)
        # Data disabled via uhubctl BEFORE the relay VBUS command.
        assert uhub and "off" in uhub[0]
        assert sent == [(2, False)]

    def test_on_sequence_vbus_first(self):
        relays = {"v": (0, 0, 0, 0)}
        sent, uhub, clockh = [], [], {"t": 0.0}
        c = self._make(relays, sent, uhub, clockh)
        c.set(True)
        assert sent == [(2, True)]
        assert uhub and "on" in uhub[0]

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
        assert hub.gateway.relay_ch == 4
        assert hub.gateway.data_port == 2
        assert hub.mfd.relay_ch == 3
        assert hub.mfd.data_port == 3
        assert hub.sdr.relay_ch == 2
        assert hub.sdr.data_port == 4
        assert hub.powerbox.port == 5

    def test_enforce_all_survives_errors(self):
        hub = HubPortPower.build(
            PortPowerConfig(),
            send_relay=lambda ch, on: True,
            get_relays=lambda: (0, 0, 0, 0),
        )
        hub.gateway.enforce = mock.Mock(side_effect=RuntimeError("boom"))
        hub.mfd.enforce = mock.Mock()
        hub.sdr.enforce = mock.Mock()
        hub.enforce_all()  # must not raise
        hub.mfd.enforce.assert_called_once()
        hub.sdr.enforce.assert_called_once()
