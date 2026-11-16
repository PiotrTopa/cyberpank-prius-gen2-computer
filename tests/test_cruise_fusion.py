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


def test_bean_62c8_beats_21d3_and_5c8_for_the_main_switch():
    store, ing, clk = make_ingress()
    feed_bean(ing, [("62", "C8", "0000")])          # BEAN: main off (just switched)
    d3(ing, 50, 0, 0, 0x8F)                         # 21D3 still says on: ignored
    feed_can(ing, 0x5C8, [0x24, 0x00, 0x10])        # 0x5C8 lags: ignored
    assert store.state.vehicle.cruise_main_switch is False
    clk.t += 3.5                                    # BEAN stale -> 21D3 takes over
    d3(ing, 50, 0, 0, 0x8F)
    assert store.state.vehicle.cruise_main_switch is True


def test_0x120_engaged_only_when_21d3_is_stale():
    store, ing, clk = make_ingress()
    gear_d = [0, 0, 0, 0, 0x10, 0x23, 0x04, 0x60]
    gear_d_cruise = [0, 0, 0, 0, 0x90, 0x23, 0x04, 0xE0]
    d3(ing, 42, 42, 70, 0x8F)                       # 21D3: cancelled
    feed_can(ing, 0x120, gear_d_cruise)             # 0x120 still engaged (2 s late)
    assert store.state.vehicle.cruise_active is False
    clk.t += 3.5                                    # no 21D3 poll any more
    feed_can(ing, 0x120, gear_d_cruise)
    assert store.state.vehicle.cruise_active is True
    feed_can(ing, 0x120, gear_d)
    assert store.state.vehicle.cruise_active is False


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
