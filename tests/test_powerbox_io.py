"""Powerbox wire-format tests for firmware 2.1 (STATUS, telemetry, commands)."""

import pytest

from cyberpunk_computer.io.powerbox import (
    BUTTON_FORCE_MS,
    BUTTON_POWER_ON_MS,
    POCO2_BUTTON_CONNECTED,
    PowerboxCommander,
    build_button_command,
    build_fan_command,
    build_hard_restart_command,
    build_poco2_alive_command,
    build_relay_command,
    build_wake_command,
    parse_power_telemetry,
    parse_powerbox_system,
)
from cyberpunk_computer.state.actions import (
    SetPowerboxConnectionAction,
    SetPowerboxPowerStatusAction,
    SetPowerboxTelemetryAction,
)
from cyberpunk_computer.state.app_state import (
    RELAY_CH_CAM_HUB,
    RELAY_CH_CAN_HUB,
    RELAY_CH_MFD,
    RELAY_CH_RESTART,
    RELAY_CH_RS485,
)
from cyberpunk_computer.state.store import Store


def _status(**extra):
    # fw 2.1: rly index 0 (relay 1 = powerbox restart relay) is always null.
    d = {"msg": "STATUS", "hb": 7, "acc": 1, "poco": 1, "p2": 0, "pm": "normal",
         "rc": 1, "up": 123, "fan": 40, "rpm": 2100,
         "rly": [None, 1, 0, 0, 1, 0, 0, 0], "rly_fix": 2, "rly_err": 0}
    d.update(extra)
    return d


class TestStatusParsing:
    def test_status_fields(self):
        (a,) = parse_powerbox_system(_status())
        assert isinstance(a, SetPowerboxPowerStatusAction)
        assert a.poco_alive is True
        assert a.poco2_alive is False
        assert a.pm_state == "normal"
        assert a.hb == 7
        assert a.fan_pct == 40
        assert a.fan_rpm == 2100
        assert a.relays == (None, True, False, False, True, False, False, False)
        assert a.reset_cause == 1 and a.uptime_s == 123
        assert a.relay_fix_count == 2 and a.relay_err_count == 0

    def test_channel_map_matches_board(self):
        """Board relay N = firmware channel N (fw 2.1): ch2 MFD, ch3 CAN hub,
        ch4 camera hub, ch5 RS485; ch1 = restart relay, never a load."""
        assert (RELAY_CH_RESTART, RELAY_CH_MFD, RELAY_CH_CAN_HUB, RELAY_CH_CAM_HUB,
                RELAY_CH_RS485) == (1, 2, 3, 4, 5)
        (a,) = parse_powerbox_system(_status(rly=[None, 1, 1, 0, 0, 0, 0, 0]))
        assert a.relays[RELAY_CH_MFD - 1] is True
        assert a.relays[RELAY_CH_CAN_HUB - 1] is True
        assert a.relays[RELAY_CH_CAM_HUB - 1] is False
        assert a.relays[RELAY_CH_RESTART - 1] is None

    def test_status_without_rly_leaves_relays_untouched(self):
        store = Store()
        store.dispatch(parse_powerbox_system(_status())[0])
        d = _status(rly_err=3)
        del d["rly"]                          # firmware: read-back failed
        store.dispatch(parse_powerbox_system(d)[0])
        pb = store.state.powerbox
        assert pb.relays == (None, True, False, False, True, False, False, False)
        assert pb.relay_err_count == 3

    def test_boot_banner_counts_boots_and_reset_cause(self):
        store = Store()
        (a,) = parse_powerbox_system({"msg": "POWERBOX_READY", "ver": "2.1.0",
                                      "role": "powerbox", "rc": 3})
        assert isinstance(a, SetPowerboxConnectionAction)
        assert a.boot is True and a.reset_cause == 3
        store.dispatch(a)
        store.dispatch(a)
        assert store.state.powerbox.boot_count == 2
        assert store.state.powerbox.reset_cause == 3
        # The periodic IDENT re-announce is NOT a boot.
        (b,) = parse_powerbox_system({"msg": "IDENT", "role": "powerbox", "ver": "2.1.0"})
        assert b.boot is False
        store.dispatch(b)
        assert store.state.powerbox.boot_count == 2

    def test_null_rs485_entry_leaves_rail_unknown(self):
        store = Store()
        store.dispatch(parse_powerbox_system(_status())[0])
        assert store.state.powerbox.rs485_power is True
        store.dispatch(parse_powerbox_system(
            _status(rly=[None, 1, 0, 0, None, 0, 0, 0]))[0])
        assert store.state.powerbox.rs485_power is None

    def test_reducer_derives_rs485_rail_from_relays(self):
        store = Store()
        store.dispatch(parse_powerbox_system(_status())[0])
        pb = store.state.powerbox
        assert pb.relays[RELAY_CH_RS485 - 1] is True
        assert pb.rs485_power is True
        assert pb.fan_duty_pct == 40.0
        assert pb.fan_rpm == 2100
        assert pb.poco2_alive is False
        assert pb.pm_state == "normal"
        store.dispatch(parse_powerbox_system(_status(rly=[0, 0, 0, 0, 0, 0, 0, 0]))[0])
        assert store.state.powerbox.rs485_power is False

    def test_old_four_channel_board_leaves_rail_unknown(self):
        store = Store()
        store.dispatch(parse_powerbox_system(_status(rly=[0, 1, 0, 0]))[0])
        assert store.state.powerbox.relays == (False, True, False, False)
        assert store.state.powerbox.rs485_power is None

    def test_sleep_and_shutdown_messages(self):
        (a,) = parse_powerbox_system({"msg": "SLEEP", "reason": "poco_down"})
        assert a.pm_state == "sleep"
        (a,) = parse_powerbox_system({"msg": "SHUTDOWN", "reason": "undervoltage 9.8V"})
        assert a.pm_state == "shutdown"

    def test_error_message_is_logged_not_reduced(self):
        assert parse_powerbox_system({"msg": "ERROR", "code": "OUT_REMOVED"}) == []


class TestTelemetryParsing:
    def test_fan_fields_ride_in_telemetry(self):
        actions = parse_power_telemetry(
            {"v": 12.4, "ma": 350.0, "mah": 1.5, "fan_pct": 55, "fan_rpm": 3000})
        assert any(isinstance(a, SetPowerboxTelemetryAction) and a.voltage == 12.4
                   for a in actions)
        st = [a for a in actions if isinstance(a, SetPowerboxPowerStatusAction)]
        assert len(st) == 1 and st[0].fan_pct == 55 and st[0].fan_rpm == 3000

    def test_telemetry_without_fan_has_no_status(self):
        actions = parse_power_telemetry({"v": 12.4})
        assert len(actions) == 1 and isinstance(actions[0], SetPowerboxTelemetryAction)


class TestCommands:
    def test_button_defaults_to_main_poco_power_on(self):
        cmd = build_button_command()
        assert cmd.payload == {"a": "button", "poco": 1, "ms": BUTTON_POWER_ON_MS}
        assert BUTTON_POWER_ON_MS == 6000 and BUTTON_FORCE_MS == 35000

    def test_button_secondary(self):
        assert build_button_command(300, poco=2).payload == {"a": "button", "poco": 2, "ms": 300}

    def test_fan_is_percent_and_clamped(self):
        assert build_fan_command(42.4).payload == {"a": "fan", "pct": 42}
        assert build_fan_command(140).payload["pct"] == 100
        assert build_fan_command(-3).payload["pct"] == 0

    def test_wake_and_p2(self):
        assert build_wake_command().payload == {"a": "wake", "poco": 1}
        assert build_wake_command(2).payload == {"a": "wake", "poco": 2}
        assert build_poco2_alive_command(True).payload == {"a": "p2", "alive": True}

    def test_relay_command_refuses_restart_channel(self):
        with pytest.raises(ValueError):
            build_relay_command(RELAY_CH_RESTART, True)
        with pytest.raises(ValueError):
            build_relay_command(9, True)
        assert build_relay_command(RELAY_CH_CAN_HUB, False).payload == \
            {"a": "relay", "ch": 3, "on": False}

    def test_hard_restart_is_a_dedicated_command(self):
        assert build_hard_restart_command().payload == {"a": "hard_restart"}

    def test_commander_never_sends_relay_1(self):
        sent = []

        class Port:
            def send(self, cmd):
                sent.append(cmd.payload)
                return True

        c = PowerboxCommander(output_port=Port())
        assert c.set_relay(RELAY_CH_RESTART, True) is False
        assert sent == []
        assert c.hard_restart()
        assert sent == [{"a": "hard_restart"}]

    def test_api_set_relay_rejects_restart_channel(self):
        from cyberpunk_computer.api.commands import COMMANDS, CommandError
        with pytest.raises(CommandError):
            COMMANDS["set_relay"]["builder"]({"channel": 1, "on": True})
        a = COMMANDS["set_relay"]["builder"]({"channel": 4, "on": True})
        assert a.channel == 4 and a.on is True
        assert type(COMMANDS["powerbox_hard_restart"]["builder"]({})).__name__ == \
            "PowerboxHardRestartAction"

    def test_secondary_button_is_known_disconnected(self):
        assert POCO2_BUTTON_CONNECTED is False

    def test_commander_routes_rs485_rail_to_relay_5(self):
        sent = []

        class Port:
            def send(self, cmd):
                sent.append(cmd.payload)
                return True

        c = PowerboxCommander(output_port=Port())
        assert c.set_rs485_power(True)
        assert sent == [{"a": "relay", "ch": RELAY_CH_RS485, "on": True}]
        assert not hasattr(c, "set_out")
