"""Cruise decode (2026-11-16 cruise drive): 21D3 layout, BEAN 62:C8 /
CAN 0x5C8 main switch, CAN 0x120 engaged, source priority and staleness."""

from cyberpunk_computer.comm.solicited_can import _hybrid_21d3_parser
from cyberpunk_computer.io.cruise_fusion import STALE_S

from cyberpunk_computer.io.ports import RawMessage

from test_bean_signals import feed_bean, feed_can, make_ingress


def d3(ing, a, b, c, d, e=0, passive=False):
    """A 0x7EA single-frame 21D3 reply as the gateway relays it (a
    subscription response), or its passive copy from the CAN stream."""
    data = {"i": "0x7EA", "d": [7, 0x61, 0xD3, a, b, c, d, e]}
    if not passive:
        data.update(a="sub", slot=6)
    ing._process_message(RawMessage(device_id=1, timestamp=0, data=data))


def test_21d3_layout_from_the_drive():
    # 21:17:05 on the drive: engaged at 52 km/h set, car at 51, cruise pulling
    v = _hybrid_21d3_parser([51, 52, 66, 0xCF, 7])
    assert v["cruise_vehicle_speed"] == 51 and v["cruise_set_speed"] == 52
    assert v["cruise_drive_request"] == 66
    assert v["cruise_main_switch"] and v["cruise_active"] and v["shift_d_position"]
    # main on, not engaged, resume speed kept after a cancel (21:17:17)
    v = _hybrid_21d3_parser([61, 63, 0, 0x8F, 0])
    assert v["cruise_main_switch"] and not v["cruise_active"] and v["cruise_set_speed"] == 63
    # main off, D
    v = _hybrid_21d3_parser([45, 0, 0, 0x80, 0])
    assert not v["cruise_main_switch"] and not v["cruise_active"]
    assert _hybrid_21d3_parser([1, 2]) == {}


def test_engage_cancel_resume_through_ingress():
    store, ing, clk = make_ingress()
    d3(ing, 50, 0, 0, 0x8F)                         # main on
    v = store.state.vehicle
    assert v.cruise_main_switch is True and not v.cruise_active
    assert v.cruise_set_speed is None and v.cruise_memory_speed is None
    clk.t += 1; d3(ing, 52, 52, 4, 0xCF)            # SET 52
    v = store.state.vehicle
    assert v.cruise_active and v.cruise_set_speed == 52 and v.cruise_drive_request == 4
    clk.t += 1; d3(ing, 63, 63, 0, 0x8F)            # cancel: set speed gone, resume speed kept
    v = store.state.vehicle
    assert not v.cruise_active and v.cruise_set_speed is None and v.cruise_memory_speed == 63
    assert v.cruise_drive_request == 0
    clk.t += 1; d3(ing, 32, 0, 0, 0x8F)             # slower than ~35 km/h: B cleared
    assert store.state.vehicle.cruise_memory_speed is None
    clk.t += 1; d3(ing, 40, 0, 0, 0x80)             # main off
    assert store.state.vehicle.cruise_main_switch is False


def test_5c8_main_bit_is_not_a_cancel():
    """The old decode read 0x5C8 b2 0x10 as "cancel" and forced cruise off."""
    store, ing, clk = make_ingress()
    d3(ing, 52, 52, 30, 0xCF)
    feed_can(ing, 0x5C8, [0x24, 0x00, 0x10])
    v = store.state.vehicle
    assert v.cruise_active and v.cruise_main_switch is True
    assert v.cruise_5c8_debug == "24 00 10"


def test_5c8_beats_bean_and_21d3_for_the_main_switch():
    """2026-11-17 (panda reader fixed): 0x5C8 leads BEAN 62:C8 by 40-60 ms;
    21D3 is the last fallback."""
    store, ing, clk = make_ingress()
    feed_can(ing, 0x5C8, [0xA4, 0x00, 0x00])        # CAN: main off (just switched)
    feed_bean(ing, [("62", "C8", "0010")])          # BEAN still says on: ignored
    d3(ing, 50, 0, 0, 0x8F)                         # 21D3 still says on: ignored
    assert store.state.vehicle.cruise_main_switch is False
    clk.t += 3.5                                    # CAN stale -> BEAN takes over
    feed_bean(ing, [("62", "C8", "0010")])
    assert store.state.vehicle.cruise_main_switch is True
    clk.t += 3.5                                    # CAN and BEAN stale -> 21D3
    d3(ing, 50, 0, 0, 0x80)
    assert store.state.vehicle.cruise_main_switch is False


GEAR_D = [0, 0, 0, 0, 0x10, 0x23, 0x04, 0x60]
GEAR_D_CRUISE = [0, 0, 0, 0, 0x90, 0x23, 0x04, 0xE0]


def test_0x120_engaged_first_21d3_confirms_set_speed():
    store, ing, clk = make_ingress()
    d3(ing, 50, 0, 0, 0x8F)                         # standby, no memory
    feed_can(ing, 0x120, GEAR_D_CRUISE)             # engage seen on 0x120 first
    v = store.state.vehicle
    assert v.cruise_active is True and v.cruise_set_speed is None   # not confirmed yet
    d3(ing, 50, 0, 0, 0x8F)                         # a poll from before the engage
    v = store.state.vehicle
    assert v.cruise_active is True and v.cruise_set_speed is None
    clk.t += 0.5
    feed_can(ing, 0x120, GEAR_D_CRUISE)
    d3(ing, 50, 50, 40, 0xCF)                       # 21D3 confirms: set speed shown
    v = store.state.vehicle
    assert v.cruise_active and v.cruise_set_speed == 50 and v.cruise_memory_speed == 50
    feed_can(ing, 0x120, GEAR_D)                    # cancel on 0x120
    v = store.state.vehicle
    assert v.cruise_active is False and v.cruise_set_speed is None


def test_0x120_stale_21d3_takes_over():
    store, ing, clk = make_ingress()
    feed_can(ing, 0x120, GEAR_D)
    d3(ing, 52, 52, 30, 0xCF)                       # 0x120 fresh and says not engaged
    assert store.state.vehicle.cruise_active is False
    clk.t += 1.5                                    # 0x120 silent (panda gone)
    d3(ing, 52, 52, 30, 0xCF)
    v = store.state.vehicle
    assert v.cruise_active is True and v.cruise_set_speed == 52


def test_memory_is_speed_at_cancel_and_cleared_below_33():
    """docs/CRUISE_CONTROL.md §5: memory = vehicle speed at the cancel (P),
    cleared at 21D3 A <= 32 (C)."""
    store, ing, clk = make_ingress()
    feed_can(ing, 0x0B4, speed_frame(60.4))
    feed_can(ing, 0x120, GEAR_D_CRUISE)
    d3(ing, 60, 66, 50, 0xCF)
    assert store.state.vehicle.cruise_set_speed == 66
    feed_can(ing, 0x0B4, speed_frame(63.7))
    feed_can(ing, 0x120, GEAR_D)                    # cancel at 63.7 km/h
    v = store.state.vehicle
    assert not v.cruise_active and v.cruise_memory_speed == 63
    d3(ing, 63, 63, 0, 0x8F)                        # the ECU agrees
    assert store.state.vehicle.cruise_memory_speed == 63
    for kmh in (40.0, 34.0, 33.2):
        clk.t += 0.3
        feed_can(ing, 0x0B4, speed_frame(kmh))
        assert store.state.vehicle.cruise_memory_speed == 63
    clk.t += 0.3
    feed_can(ing, 0x0B4, speed_frame(32.9))
    assert store.state.vehicle.cruise_memory_speed is None


def test_switch_bits_of_21d3_e():
    store, ing, clk = make_ingress()
    d3(ing, 53, 0, 0, 0x8F, e=0x10)                 # SET pressed
    v = store.state.vehicle
    assert v.cruise_switch_set_coast is True and v.cruise_switch_res_acc is False
    assert v.cruise_brake_inputs == 0
    d3(ing, 51, 53, 0, 0xCF, e=0x08)                # RES/ACC
    v = store.state.vehicle
    assert v.cruise_switch_set_coast is False and v.cruise_switch_res_acc is True
    d3(ing, 40, 53, 0, 0x8F, e=0x07)                # brake pedal: all three inputs
    assert store.state.vehicle.cruise_brake_inputs == 7
    vals = _hybrid_21d3_parser([0, 0, 0, 0x80, 0x02])  # power-off sample: no switch
    assert not vals["cruise_switch_set_coast"] and not vals["cruise_switch_res_acc"]
    assert vals["cruise_brake_inputs"] == 2


def speed_frame(kmh):
    w = int(round(kmh * 100))
    return [0, 0, 0, 0, 1 if kmh > 5 else 0, w >> 8, w & 0xFF, 0]


def test_everything_silent_clears_the_cruise_state():
    store, ing, clk = make_ingress()
    d3(ing, 52, 52, 30, 0xCF)
    ing.update()
    assert store.state.vehicle.cruise_active
    clk.t += STALE_S + 0.5
    ing.update()
    v = store.state.vehicle
    assert not v.cruise_active and v.cruise_main_switch is None
    assert v.cruise_set_speed is None and v.cruise_drive_request is None


def test_late_passive_copy_of_a_reply_is_ignored():
    """The gateway's passive stream ran 0..5 s behind the replies (2026-11-16):
    an old copy of a pre-SET reply must not cancel the engaged state."""
    store, ing, clk = make_ingress()
    d3(ing, 52, 52, 4, 0xCF)
    d3(ing, 53, 0, 0, 0x8F, passive=True)
    v = store.state.vehicle
    assert v.cruise_active and v.cruise_set_speed == 52
