"""Solicited poll budget (2026-11-17): what the gateway transmits onto the
car's CAN, which polls a broadcast replaces, the reply byte offsets."""

from cyberpunk_computer.comm.can_decoder import CANDecoder
from cyberpunk_computer.comm.solicited_can import reply_payload, _hybrid_21d3_parser
from cyberpunk_computer.comm.source_priority import (
    DEFAULT_POLICIES, SLOT_OBD_AMBIENT_TEMP, SLOT_OBD_INTAKE_TEMP, SLOT_OBD_RPM,
    SlotSuspender, SourceArbiter,
)
from cyberpunk_computer.comm.subscription_groups import ALL_GROUPS, with_padding


def _slots():
    return {s.slot: s.payload for g in ALL_GROUPS for s in g.slots}


def _tx_rate(skip=()):
    """(requests/s, flow-control frames/s) of the subscribed slots."""
    req = fc = 0.0
    for slot, p in _slots().items():
        if slot in skip:
            continue
        req += 1000.0 / p["int"]
        if p.get("isotp"):
            fc += 1000.0 / p["int"]
    return req, fc


def test_transmit_budget_with_broadcasts_alive():
    covered = (SLOT_OBD_RPM, SLOT_OBD_INTAKE_TEMP, SLOT_OBD_AMBIENT_TEMP)
    req, fc = _tx_rate(skip=covered)
    assert req + fc < 4.5            # was 8.6 frames/s until 2026-11-16
    assert 2.9 < req < 3.1 and 1.2 < fc < 1.4
    # every broadcast source down: the fallback polls come back
    req_all, fc_all = _tx_rate()
    assert req_all + fc_all < 6.5    # was 10.6


def test_polls_that_exist_nowhere_else_keep_their_rate():
    s = _slots()
    assert s[6]["d"][1:3] == [0x21, 0xD3] and s[6]["int"] == 1000      # cruise set speed
    assert s[3]["d"][1:3] == [0x21, 0xCE] and s[3]["int"] <= 3000      # block-resistance pairs <= 3.5 s
    assert s[2]["d"][1:3] == [0x21, 0xC3] and s[2]["int"] <= 2000
    for slot in (2, 3, 4, 5, 8):     # every multi-frame reply is reassembled by the gateway
        assert s[slot].get("isotp") is True


def test_air_temperature_slots_are_suspended_by_0x3cf():
    t = [0.0]
    arb = SourceArbiter(DEFAULT_POLICIES, clock=lambda: t[0])
    unsub, sub = [], []
    sus = SlotSuspender(arb, unsub.append, sub.append, resume_hold_s=30.0)
    for k in range(120):             # 0x3CF every 0.25 s for 30 s
        t[0] = k * 0.25
        arb.offer("intake_temp", "can")
        arb.offer("ambient_air", "can")
        sus.tick()
    assert set(unsub) >= {SLOT_OBD_INTAKE_TEMP, SLOT_OBD_AMBIENT_TEMP} and not sub
    # the broadcast goes silent: both polls return
    t[0] += 5.0
    sus.tick()
    assert set(sub) == {SLOT_OBD_INTAKE_TEMP, SLOT_OBD_AMBIENT_TEMP}
    # a fresh OBD reply is used while the broadcast is stale, ignored otherwise
    assert arb.offer("ambient_air", "obd")
    arb.offer("ambient_air", "can")
    assert not arb.offer("ambient_air", "obd")


def test_reply_payload_offsets_single_frame_and_reassembled():
    # non-ISO-TP subscription: the CAN frame, [PCI, 61, D3, A, B, C, D, E]
    single = [0x07, 0x61, 0xD3, 52, 60, 0, 0x8F, 0]
    # ISO-TP subscription: reassembled, [61, C3, A, B, ...]
    multi = [0x61, 0xC3, 0x3F, 0xFF, 0x0F, 0xA0] + [0] * 33
    assert reply_payload(single) == [52, 60, 0, 0x8F, 0]
    assert reply_payload(multi)[:4] == [0x3F, 0xFF, 0x0F, 0xA0] and len(reply_payload(multi)) == 37
    assert reply_payload([]) == []
    assert _hybrid_21d3_parser(reply_payload(single))["cruise_set_speed"] == 60


def test_decoder_reads_both_reply_shapes_alike():
    """End to end on replies from the 2026-11-16 trip log: slot 2 (21C3,
    reassembled) and the same data as a single frame would carry it."""
    dec = CANDecoder()
    c3 = [97, 195, 63, 255, 15, 160, 0, 0, 63, 255, 15, 160, 0, 0, 0, 0, 0, 0, 0, 13, 140, 55, 145,
          0, 128, 0, 74, 74, 70, 71, 112, 0, 130, 120, 120, 76, 78, 1, 128]
    m = dec.decode({"a": "sub", "slot": 2, "i": "0x7EA", "d": c3})
    assert m.values["obd2_pid"] == 0xC3
    assert m.values["mg1_inverter_temp"] == 74 - 40 and m.values["mg2_inverter_temp"] == 74 - 40
    assert m.values["mg2_rpm"] == 0x3FFF - 16383
    d3_frame = dec.decode({"a": "sub", "slot": 6, "i": "0x7EA", "d": [7, 0x61, 0xD3, 52, 60, 0, 0x8F, 0]})
    d3_isotp = dec.decode({"a": "resp", "i": "0x7EA", "d": [0x61, 0xD3, 52, 60, 0, 0x8F, 0]})
    assert d3_frame.values["cruise_set_speed"] == d3_isotp.values["cruise_set_speed"] == 60
    # a consecutive frame heard passively is not a reply
    cf = dec.decode({"i": "0x7EA", "d": [0x21, 0, 0, 63, 223, 15, 160, 0]})
    assert "mg2_rpm" not in cf.values and "cruise_set_speed" not in cf.values


def test_request_padding_is_opt_in():
    p = _slots()[0]
    assert with_padding(p, None) is p
    padded = with_padding(p, 0x55)
    assert padded["d"] == [0x02, 0x01, 0x0C, 0x55, 0x55, 0x55, 0x55, 0x55]
    assert p["d"] == [0x02, 0x01, 0x0C, 0, 0, 0, 0, 0] and padded["slot"] == 0
