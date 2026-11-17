"""Body signals checked on the car 2026-11-17 (docs/BEAN_SIGNALS.md §4.1, §6):
per-door state (BEAN FE:B6 / CAN 0x5B6), the back door told apart by the
opener pulse (FE:83 b3 0x04), headlamp flash, climate hard keys, and the
turn-signal intent tracker (no bus source on this car)."""

from cyberpunk_computer.alerts.inputs import build_inputs
from cyberpunk_computer.comm.bean import decode_known
from cyberpunk_computer.comm.can_decoder import CANDecoder, CANMessageType
from cyberpunk_computer.io.turn_signal import HOLD_S, STALE_S, TurnSignalTracker

from tests.test_bean_signals import feed_bean, feed_can, make_ingress


# ── decoders ────────────────────────────────────────────────────────────────

def test_fe_b6_each_door():
    # 2026-11-17 13:28-13:29: driver, rear (x2) and back door share 04, passenger 40
    for b1, want in ((0x80, (True, False, False)), (0x40, (False, True, False)),
                     (0x04, (False, False, True)), (0x00, (False, False, False))):
        v = decode_known("B6", "C1%02X" % b1, "FE")
        assert (v["driver_door_open"], v["passenger_door_open"], v["rear_door_open"]) == want


def test_fe_83_back_door_opener_and_flash_bits():
    assert decode_known("83", "6000000C", "FE")["back_door_opener"] is True
    assert decode_known("83", "60000008", "FE")["back_door_opener"] is False
    v = decode_known("7F", "281000000000", "FE")          # flash with the lamps off
    assert v["lamp_high_beam"] and v["lamp_head"] and not v["lamp_tail"]


def test_climate_hard_keys():
    v = decode_known("FC", "CA8000000020", "FE")          # rear defogger key
    assert v["climate_rear_defogger"] and not v["climate_front_defrost"]
    v = decode_known("FC", "520000000040", "FE")          # front defrost key
    assert v["climate_front_defrost"] and not v["climate_auto"] and v["climate_ac"]
    assert decode_known("ED", "6A20DF0000", "52")["front_defrost_mode"] is True
    assert decode_known("ED", "6A807F0000", "52")["front_defrost_mode"] is False
    # steering pad: 52:FD 05 -> 09 = 18.0 -> 20.0 °C in 0.5 °C steps
    assert [decode_known("FD", "%02X38" % r, "52")["climate_target_c"] for r in range(5, 10)] == \
        [18.0, 18.5, 19.0, 19.5, 20.0]


def test_can_5b6_is_fe_b6():
    m = CANDecoder().decode({"i": "0x5B6", "d": [0xE4, 0xC1, 0x80]})
    assert m.msg_type == CANMessageType.BODY_DOORS
    assert (m.values["driver_door_open"], m.values["passenger_door_open"],
            m.values["rear_door_open"], m.values["doors_locked"]) == (True, False, False, False)
    v = CANDecoder().decode({"i": "0x5B6", "d": [0x64, 0x00, 0x44]}).values
    assert (v["passenger_door_open"], v["rear_door_open"], v["doors_locked"]) == (True, True, True)


# ── into the state ──────────────────────────────────────────────────────────

def test_doors_reach_vehicle_state():
    store, ing, clk = make_ingress()
    feed_bean(ing, [("FE", "B6", "C180")])
    v = store.state.vehicle
    assert (v.driver_door_open, v.passenger_door_open, v.rear_door_open, v.doors_locked) == \
        (True, False, False, False)
    assert v.back_door_open is False
    feed_bean(ing, [("FE", "B6", "C140")])
    v = store.state.vehicle
    assert (v.driver_door_open, v.passenger_door_open) == (False, True)


def test_back_door_needs_the_opener_pulse():
    store, ing, clk = make_ingress()
    # a rear side door: the shared bit rises with no opener pulse
    feed_bean(ing, [("FE", "83", "60000008"), ("FE", "B6", "C104")])
    v = store.state.vehicle
    assert v.rear_door_open is True and v.back_door_open is False
    feed_bean(ing, [("FE", "B6", "C100")])
    assert store.state.vehicle.rear_door_open is False
    # the back door: opener pulse 0.2 s before the shared bit
    feed_bean(ing, [("FE", "83", "6000000C"), ("FE", "B6", "C104"), ("FE", "83", "60000008")])
    v = store.state.vehicle
    assert v.rear_door_open is True and v.back_door_open is True
    feed_bean(ing, [("FE", "B6", "C100")])
    assert store.state.vehicle.back_door_open is False


def test_can_5b6_fills_doors_while_bean_is_silent():
    store, ing, clk = make_ingress()
    feed_can(ing, 0x5B6, [0x64, 0xC1, 0x40])
    v = store.state.vehicle
    assert v.passenger_door_open is True and v.back_door_open is None


# ── turn signal intent ──────────────────────────────────────────────────────

def _blink(tr, side, t0, n, period=0.7, duty=0.5, step=0.05):
    """Feed ``n`` blink periods of one side (or 'both') sampled every ``step``."""
    out = []
    t = t0
    while t < t0 + n * period:
        on = ((t - t0) % period) < duty * period
        left = on if side in ("left", "both") else False
        right = on if side in ("right", "both") else False
        tr.observe(left, right, t, "test")
        out.append((t, tr.intent(t)))
        t += step
    return out, t


def test_turn_signal_steady_through_the_dark_phase():
    tr = TurnSignalTracker()
    seq, t = _blink(tr, "left", 0.0, 5)
    assert {i for _, i in seq} == {"left"}
    tr.observe(True, False, t, "test")            # last lit sample
    tr.observe(False, False, t + 0.05, "test")
    assert tr.intent(t + HOLD_S * 0.9) == "left"
    assert tr.intent(t + HOLD_S + 0.1) is None


def test_hazard_and_side_change():
    tr = TurnSignalTracker()
    seq, t = _blink(tr, "both", 0.0, 4)
    assert {i for _, i in seq} == {"hazard"}
    tr2 = TurnSignalTracker()
    _, t = _blink(tr2, "left", 0.0, 3)
    seq, _ = _blink(tr2, "right", t, 3)
    assert "hazard" not in {i for _, i in seq}
    assert seq[-1][1] == "right"


def test_turn_signal_actions_and_staleness():
    tr = TurnSignalTracker()
    acts = tr.observe(True, False, 0.0, "tap")
    assert acts and acts[0].vehicle["turn_signal"] == "left"
    assert acts[0].vehicle["turn_lamp_left"] is True and acts[0].vehicle["turn_signal_source"] == "tap"
    assert tr.observe(True, False, 0.05, "tap") == []                 # nothing changed
    acts = tr.tick(STALE_S + 0.5)
    assert acts[0].vehicle == {"turn_signal": None, "turn_lamp_left": None,
                               "turn_lamp_right": None, "turn_signal_source": None}


def test_turn_signal_reaches_state_and_alert_inputs():
    store, ing, clk = make_ingress()
    inp = build_inputs(store.state, None, None, now=0.0, wall=0.0)
    assert inp.turn_signal == "unknown"                              # no source on this car
    tr = TurnSignalTracker()
    for a in tr.observe(False, True, 0.0, "tap"):
        store.dispatch(a)
    v = store.state.vehicle
    assert (v.turn_signal, v.turn_lamp_right, v.turn_signal_source) == ("right", True, "tap")
    assert build_inputs(store.state, None, None, now=0.0, wall=0.0).turn_signal == "right"
    for a in tr.observe(False, False, 5.0, "tap"):
        store.dispatch(a)
    assert build_inputs(store.state, None, None, now=0.0, wall=0.0).turn_signal == "off"
