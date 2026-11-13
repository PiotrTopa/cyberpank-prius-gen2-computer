"""ISO-TP framing of the diagnostics engine (cyberpunk_computer.diag.isotp)."""

import pytest

from cyberpunk_computer.diag import isotp
from cyberpunk_computer.diag.sim import segment


def test_single_frame_normal_addressing():
    # Worked example (2009 Gen 2 capture): request 21 C3 to the HV ECU.
    assert isotp.encode_single(b"\x21\xC3") == bytes([0x02, 0x21, 0xC3, 0, 0, 0, 0, 0])
    assert isotp.encode_single(b"\x03") == bytes([0x01, 0x03, 0, 0, 0, 0, 0, 0])


def test_single_frame_extended_addressing():
    # 0x750-style: sub-address first, PCI second (TPMS tools: 2A 02 21 30).
    assert isotp.encode_single(b"\x21\x30", sub_addr=0x2A) == bytes([0x2A, 0x02, 0x21, 0x30, 0, 0, 0, 0])
    assert isotp.max_single_frame(None) == 7 and isotp.max_single_frame(0x40) == 6


def test_single_frame_too_long_is_refused():
    with pytest.raises(isotp.IsoTpError):
        isotp.encode_single(bytes(8))
    with pytest.raises(isotp.IsoTpError):
        isotp.encode_single(bytes(7), sub_addr=0x40)
    with pytest.raises(isotp.IsoTpError):
        isotp.encode_single(b"")


def test_flow_control_frames():
    assert isotp.flow_control() == bytes([0x30, 0, 0, 0, 0, 0, 0, 0])
    assert isotp.flow_control(block_size=1) == bytes([0x30, 1, 0, 0, 0, 0, 0, 0])
    # the raw flow control a 0x750 tool sends: 2A 30 00 00 ...
    assert isotp.flow_control(sub_addr=0x2A) == bytes([0x2A, 0x30, 0, 0, 0, 0, 0, 0])


def test_reassemble_single():
    r = isotp.Reassembler()
    assert r.feed(bytes([0x02, 0x43, 0x00, 0, 0, 0, 0, 0])) == "single"
    assert r.payload == b"\x43\x00" and r.complete


def test_reassemble_multi_frame_normal():
    payload = bytes([0x61, 0xC3]) + bytes(range(37))          # 39 bytes: FF "10 27 61 C3 ..."
    frames = segment(payload)
    assert frames[0][:4] == bytes([0x10, 0x27, 0x61, 0xC3])
    r = isotp.Reassembler()
    assert r.feed(frames[0]) == "first" and r.in_progress and r.total == 39
    for f in frames[1:-1]:
        assert r.feed(f) == "consecutive"
    assert r.feed(frames[-1]) == "done"
    assert r.payload == payload


def test_reassemble_multi_frame_extended():
    payload = bytes([0x53, 0x04]) + bytes([0xA2, 0x78, 0xA2, 0x81, 0x92, 0x10, 0x92, 0x60])
    frames = segment(payload, sub_addr=0x40)
    assert frames[0][0] == 0x40 and frames[0][1] == 0x10 and frames[0][2] == len(payload)
    r = isotp.Reassembler(extended=True, rx_addr=0x40)
    assert [r.feed(f) for f in frames] == ["first", "done"]
    assert r.payload == payload and r.seen_addr == 0x40


def test_extended_wrong_sub_address_is_ignored():
    r = isotp.Reassembler(extended=True, rx_addr=0x40)
    assert r.feed(bytes([0x5F, 0x02, 0x7E, 0x00, 0, 0, 0, 0])) == "ignored"
    assert not r.complete
    assert r.feed(bytes([0x40, 0x01, 0x7E, 0, 0, 0, 0, 0])) == "single"


def test_sequence_wraps_after_15():
    payload = bytes(range(256)) * 1
    frames = segment(payload[:200])
    r = isotp.Reassembler()
    out = [r.feed(f) for f in frames]
    assert out[0] == "first" and out[-1] == "done" and r.payload == payload[:200]
    assert len(frames) > 17                      # the 4-bit sequence number wrapped


def test_out_of_order_is_an_error():
    frames = segment(bytes(30))
    r = isotp.Reassembler()
    r.feed(frames[0])
    assert r.feed(frames[2]) == "error"
    assert "sequence" in r.error
    assert r.feed(frames[1]) == "ignored"        # dead after an error
    assert r.partial == bytes(6)


def test_duplicate_frames_are_dropped():
    frames = segment(bytes(range(20)))
    r = isotp.Reassembler()
    assert r.feed(frames[0]) == "first"
    assert r.feed(frames[0]) == "duplicate"
    assert r.feed(frames[1]) == "consecutive"
    assert r.feed(frames[1]) == "duplicate"
    assert r.feed(frames[2]) == "done"
    assert r.feed(frames[2]) == "duplicate"
    assert r.payload == bytes(range(20))


def test_garbage_is_ignored():
    r = isotp.Reassembler()
    assert r.feed(b"") == "ignored"
    assert r.feed(bytes([0x00, 1, 2, 3])) == "ignored"               # SF length 0
    assert r.feed(bytes([0x07, 1, 2])) == "ignored"                  # SF longer than the frame
    assert r.feed(bytes([0x30, 0, 0, 0, 0, 0, 0, 0])) == "ignored"   # flow control
    assert r.feed(bytes([0x21, 1, 2, 3, 4, 5, 6, 7])) == "ignored"   # CF without FF
    assert r.feed(bytes([0x10, 0x05, 1, 2, 3, 4, 5, 6])) == "ignored"  # FF that would fit a SF
    assert r.feed(bytes([0xF0, 0, 0, 0, 0, 0, 0, 0])) == "ignored"
    assert not r.complete and r.error is None
    assert r.feed(bytes([0x01, 0x44, 0, 0, 0, 0, 0, 0])) == "single"
    assert r.feed(bytes([0x01, 0x45, 0, 0, 0, 0, 0, 0])) == "ignored"  # after completion


def test_unexpected_first_frame_mid_message():
    frames = segment(bytes(30))
    other = segment(bytes([1]) * 30)
    r = isotp.Reassembler()
    r.feed(frames[0])
    assert r.feed(other[0]) == "error"


def test_remaining_and_reset():
    frames = segment(bytes(20))
    r = isotp.Reassembler()
    r.feed(frames[0])
    assert r.remaining == 14
    r.reset()
    assert not r.in_progress and r.partial == b""
