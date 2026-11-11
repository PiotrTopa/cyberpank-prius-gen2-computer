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
    SetPowerboxIgnitionAction,
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


class TestAccOffDebouncer:
    """ACC ON->OFF edges are held ``hold_s``; ON passes at once; a glitch is dropped."""

    def _mk(self, hold=3.0):
        from cyberpunk_computer.io.powerbox import AccOffDebouncer
        clock = {"t": 100.0}
        return AccOffDebouncer(hold, clock=lambda: clock["t"]), clock

    @staticmethod
    def _ign(acc, batt=True):
        return SetPowerboxIgnitionAction(acc_on=acc, batt_present=batt)

    def test_boot_with_key_off_passes_immediately(self):
        d, _ = self._mk()
        out = d.filter([self._ign(False)])
        assert [a.acc_on for a in out] == [False]
        assert not d.pending

    def test_off_edge_is_held_then_released(self):
        d, clock = self._mk(3.0)
        d.filter([self._ign(True)])
        out = d.filter([self._ign(False, batt=False)])
        assert [a.acc_on for a in out] == [True]       # held: still "on" downstream
        assert out[0].batt_present is False             # ...but batt keeps flowing
        assert d.pending
        clock["t"] = 102.9
        assert d.due() is None
        clock["t"] = 103.0
        rel = d.due()
        assert rel is not None and rel.acc_on is False and rel.batt_present is False
        assert not d.pending
        # Repeats of the OFF (STATUS frames) now pass straight through.
        assert [a.acc_on for a in d.filter([self._ign(False)])] == [False]

    def test_glitch_back_on_is_dropped(self):
        d, clock = self._mk(3.0)
        d.filter([self._ign(True)])
        d.filter([self._ign(False)])
        clock["t"] = 101.0
        out = d.filter([self._ign(True)])
        assert [a.acc_on for a in out] == [True]
        assert not d.pending
        clock["t"] = 110.0
        assert d.due() is None                          # nothing left to release

    def test_disabled_passes_everything(self):
        d, _ = self._mk(0.0)
        d.filter([self._ign(True)])
        assert [a.acc_on for a in d.filter([self._ign(False)])] == [False]

    def test_other_actions_untouched(self):
        d, _ = self._mk()
        other = SetPowerboxTelemetryAction(voltage=12.0)
        assert d.filter([other]) == [other]

    def test_register_with_filter_wraps_handlers(self):
        from cyberpunk_computer.io.powerbox import (
            register_powerbox_ingress, DEVICE_POWERBOX_EVENTS)
        handlers = {}
        ingress = type("I", (), {"register_satellite_handler":
                                  lambda self, dev, h: handlers.__setitem__(dev, h)})()
        d, _ = self._mk(3.0)
        register_powerbox_ingress(ingress, action_filter=d.filter)
        handlers[DEVICE_POWERBOX_EVENTS]({"event": "acc_on"})
        out = handlers[DEVICE_POWERBOX_EVENTS]({"event": "acc_off"})
        assert [a.acc_on for a in out] == [True] and d.pending


class TestBean:
    """fw 2.2.3 BEAN transceiver bench: command, ack mirror, REST validation."""

    def test_build_and_caps(self):
        from cyberpunk_computer.io.powerbox import build_bean_command
        assert build_bean_command("status").payload == {"a": "bean", "op": "status"}
        assert build_bean_command("tx", v=1, ms=500).payload == \
            {"a": "bean", "op": "tx", "v": 1, "ms": 500}
        assert build_bean_command("square", hz=20000, ms=1000).payload["hz"] == 20000
        assert build_bean_command("loop").payload == {"a": "bean", "op": "loop"}
        for op, kw in (("tx", {"ms": 501}), ("tx", {"v": 2}), ("pulse", {"us": 2001}),
                       ("loop", {"n": 0}), ("loop", {"n": 201}), ("square", {"hz": 99}),
                       ("square", {"hz": 20001}), ("square", {"ms": 1001}),
                       ("tx", {"us": 10}), ("blast", {}), ("tx", {"v": True}),
                       ("tx", {"ms": 1.5})):
            with pytest.raises(ValueError):
                build_bean_command(op, **kw)

    def test_commander_send_bean(self):
        sent = []

        class Port:
            def send(self, cmd):
                sent.append(cmd.payload)
                return True

        c = PowerboxCommander(output_port=Port())
        assert c.send_bean("pulse", us=100)
        assert c.send_bean("tx", ms=9999) is False      # refused, not sent
        assert sent == [{"a": "bean", "op": "pulse", "us": 100}]

    def test_ack_mirrored_into_state(self):
        from cyberpunk_computer.api.serialization import serialize_state
        store = Store()
        (a,) = parse_powerbox_system({"ack": "bean", "op": "loop", "n": 20,
                                      "dom_avg": 12, "timeouts": 0})
        store.dispatch(a)
        bean = store.state.powerbox.bean
        assert bean["op"] == "loop" and bean["dom_avg"] == 12 and "ack" not in bean
        assert isinstance(bean["ts"], float)
        js = serialize_state(store.state)
        assert js["powerbox"]["bean"]["op"] == "loop"

    def test_pmevent_echo_is_not_reduced(self):
        assert parse_powerbox_system({"msg": "PMEVENT", "ev": "bean", "op": "status"}) == []

    def test_bean_error_mirrored(self):
        store = Store()
        (a,) = parse_powerbox_system({"msg": "ERROR", "code": "BEAN_ARGS",
                                      "detail": "ms=900 not in 1..500"})
        store.dispatch(a)
        assert store.state.powerbox.bean["error"] == "BEAN_ARGS"
        assert parse_powerbox_system({"msg": "ERROR", "code": "RELAY"}) == []

    def test_rest_command_validation(self):
        from cyberpunk_computer.api.commands import COMMANDS, CommandError, build_command
        a = build_command("powerbox_bean", {"op": "square", "hz": "5000", "ms": 200})
        assert type(a).__name__ == "PowerboxBeanAction"
        assert a.op == "square" and a.params == {"hz": 5000, "ms": 200}
        assert build_command("powerbox_bean", {"op": "status"}).params == {}
        for params in ({}, {"op": "nope"}, {"op": "tx", "ms": 501},
                       {"op": "pulse", "us": 0}, {"op": "loop", "hz": 5}):
            with pytest.raises(CommandError):
                build_command("powerbox_bean", params)
        assert "op" in COMMANDS["powerbox_bean"]["params"]

    def test_service_routes_ui_action_to_commander(self):
        """The backend middleware turns PowerboxBeanAction into send_bean."""
        import inspect
        from cyberpunk_computer.backend import service
        src = inspect.getsource(service)
        assert '"PowerboxBeanAction"' in src and "send_bean(action.op, **action.params)" in src


class TestAccSim:
    def test_command_payload(self):
        from cyberpunk_computer.io.powerbox import build_acc_sim_command
        assert build_acc_sim_command(True).payload == {"a": "acc_sim", "v": 1}
        assert build_acc_sim_command(False).payload == {"a": "acc_sim", "v": 0}
        assert build_acc_sim_command(None).payload == {"a": "acc_sim", "v": None}

    def test_commander(self):
        sent = []

        class Port:
            def send(self, cmd):
                sent.append(cmd.payload)
                return True

        assert PowerboxCommander(output_port=Port()).set_acc_sim(None)
        assert sent == [{"a": "acc_sim", "v": None}]

    def test_status_flag_and_ack_mirrored(self):
        store = Store()
        store.dispatch(parse_powerbox_system(_status(acc_sim=1))[0])
        assert store.state.powerbox.acc_sim is True
        store.dispatch(parse_powerbox_system(_status())[0])   # flag absent = off
        assert store.state.powerbox.acc_sim is False
        (a,) = parse_powerbox_system({"ack": "acc_sim", "v": 0, "pin": 1})
        store.dispatch(a)
        assert store.state.powerbox.acc_sim is True
        (a,) = parse_powerbox_system({"ack": "acc_sim", "v": None, "pin": 1})
        store.dispatch(a)
        assert store.state.powerbox.acc_sim is False

    def test_rest_command(self):
        from cyberpunk_computer.api.commands import COMMANDS, CommandError, build_command
        assert build_command("powerbox_acc_sim", {"on": True}).on is True
        assert build_command("powerbox_acc_sim", {"on": "false"}).on is False
        assert build_command("powerbox_acc_sim", {"on": None}).on is None
        assert build_command("powerbox_acc_sim", {}).on is None
        with pytest.raises(CommandError):
            build_command("powerbox_acc_sim", {"on": "maybe"})
        assert "BENCH ONLY" in COMMANDS["powerbox_acc_sim"]["description"]
