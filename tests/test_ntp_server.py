"""SNTP server for the MFD Pi (backend/ntp_server.py)."""

import socket
import struct
import time

from cyberpunk_computer.backend.ntp_server import (
    NTP_EPOCH_OFFSET, SntpServer, build_reply, clock_quality,
)


def _request(t=1000.25, vn=4):
    pkt = bytearray(48)
    pkt[0] = (vn << 3) | 3
    pkt[2] = 6
    sec = int(t) + NTP_EPOCH_OFFSET
    struct.pack_into("!II", pkt, 40, sec, int((t % 1) * (1 << 32)))
    return bytes(pkt)


def _ts(b, off):
    sec, frac = struct.unpack_from("!II", b, off)
    return sec - NTP_EPOCH_OFFSET + frac / (1 << 32)


def test_reply_fields_when_synchronized():
    req = _request()
    r = build_reply(req, 1_794_700_000.5, 1_794_700_000.501, 0.05)
    assert len(r) == 48
    assert r[0] >> 6 == 0 and (r[0] >> 3) & 7 == 4 and r[0] & 7 == 4
    assert r[1] == 2                                   # stratum
    assert r[24:32] == req[40:48]                      # origin = client transmit
    assert abs(_ts(r, 32) - 1_794_700_000.5) < 1e-6
    assert abs(_ts(r, 40) - 1_794_700_000.501) < 1e-6
    disp = struct.unpack_from("!I", r, 8)[0] / 65536
    assert abs(disp - 0.05) < 1e-3


def test_unsynchronized_phone_says_leap_3():
    r = build_reply(_request(), 1.0e9, 1.0e9, None)
    assert r[0] >> 6 == 3 and r[1] == 0               # timesyncd discards it


def test_only_client_requests_answered():
    req = bytearray(_request())
    req[0] = (4 << 3) | 4                              # a server packet
    assert build_reply(bytes(req), 1.0, 1.0, 0.05) is None
    assert build_reply(b"\x23" * 10, 1.0, 1.0, 0.05) is None


def test_clock_quality_marks(tmp_path):
    ntp, https = tmp_path / "synchronized", tmp_path / "prius-clock-synced"
    assert clock_quality((str(ntp), str(https))) is None
    https.touch()
    assert clock_quality((str(ntp), str(https))) == 1.0
    ntp.touch()
    assert clock_quality((str(ntp), str(https))) == 0.05


def test_udp_round_trip_and_subnet_filter():
    srv = SntpServer(host="127.0.0.1", port=0, allow_net="127.0.0.0/8",
                     quality=lambda: 0.05)
    assert srv.start()
    try:
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.settimeout(2.0)
        t1 = time.time()
        c.sendto(_request(t1), ("127.0.0.1", srv.port_bound))
        r, _ = c.recvfrom(512)
        t4 = time.time()
        offset = ((_ts(r, 32) - t1) + (_ts(r, 40) - t4)) / 2
        assert abs(offset) < 0.05 and r[0] >> 6 == 0
        assert srv.served == 1
    finally:
        srv.stop()
    other = SntpServer(allow_net="192.168.100.0/24", quality=lambda: 0.05)
    assert other.handle(_request(), ("10.0.0.5", 123), 1.0) is None
    assert other.refused == 1
    assert other.handle(_request(), ("192.168.100.2", 123), 1.0) is not None


def test_allow_list_of_networks():
    srv = SntpServer(allow_net="192.168.100.0/24, 10.42.0.0/24", quality=lambda: 0.05)
    assert srv.handle(_request(), ("192.168.100.2", 123), 1.0) is not None
    assert srv.handle(_request(), ("10.42.0.94", 123), 1.0) is not None
    assert srv.handle(_request(), ("192.168.0.65", 123), 1.0) is None
    assert srv.refused == 1

