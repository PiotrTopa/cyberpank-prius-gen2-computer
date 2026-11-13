"""Service encode/decode, DTC numbers, safety allow-list, dictionary."""

import pytest

from cyberpunk_computer.diag import dictionary, dtc, services


# ── DTC numbers ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hi,lo,code", [
    (0x04, 0x20, "P0420"),
    (0x0A, 0x80, "P0A80"),
    (0x31, 0x02, "P3102"),
    (0x08, 0x52, "P0852"),
    (0x63, 0x00, "C2300"),
    (0x52, 0x71, "C1271"),      # Toyota K-line capture: 53 52 71 52 72 52 73
    (0xA2, 0x78, "B2278"),
    (0x92, 0x10, "B1210"),
    (0xC1, 0x00, "U0100"),
])
def test_dtc_decode_encode(hi, lo, code):
    assert dtc.decode(hi, lo) == code
    assert dtc.encode(code) == (hi, lo)


def test_dtc_filler_and_bad_codes():
    assert dtc.decode(0, 0) is None
    assert dtc.is_code("p0420") and not dtc.is_code("P042") and not dtc.is_code("X0420")
    assert not dtc.is_code("P4000") and not dtc.is_code("P0G20")
    assert dtc.decode_list(bytes([0x04, 0x20, 0x00, 0x00, 0x01, 0x71, 0xFF])) == [
        ("P0420", b""), ("P0171", b"")]


def test_matches_existing_decoder():
    from cyberpunk_computer.comm.solicited_can import parse_dtc_bytes
    for hi in range(0, 256, 7):
        for lo in (0x01, 0x20, 0x7F, 0xA5, 0xFF):
            assert dtc.decode(hi, lo) == parse_dtc_bytes(hi, lo)


# ── OBD ──────────────────────────────────────────────────────────────────────

def test_obd_dtc_lists():
    assert services.obd_dtc_request(0x03) == b"\x03"
    assert services.obd_parse_dtcs(bytes([0x43, 0x01, 0x04, 0x20])) == ["P0420"]
    assert services.obd_parse_dtcs(bytes([0x47, 0x00])) == []
    # count byte wrong / zero padded: still decoded pair by pair
    assert services.obd_parse_dtcs(bytes([0x43, 0x02, 0x04, 0x20, 0x00, 0x00, 0x01, 0x71])) == [
        "P0420", "P0171"]


def test_obd_monitor_status():
    st = services.obd_parse_monitor_status(bytes([0x41, 0x01, 0x81, 0x07, 0x65, 0x04]))
    assert st["mil"] is True and st["dtc_count"] == 1
    assert "catalyst" in st["monitors_complete"] and "evap" in st["monitors_incomplete"]
    assert services.obd_parse_monitor_status(bytes([0x41, 0x01, 0x00, 0x07, 0x65, 0x00]))["mil"] is False
    assert services.obd_parse_monitor_status(b"\x7F\x01\x11") is None


def test_obd_freeze_frame():
    assert services.obd_freeze_request(0x02) == bytes([0x02, 0x02, 0x00])
    assert services.obd_parse_freeze_dtc(bytes([0x42, 0x02, 0x00, 0x04, 0x20])) == "P0420"
    assert services.obd_parse_freeze_dtc(bytes([0x42, 0x02, 0x00, 0x00, 0x00])) is None
    assert services.obd_parse_freeze_value(0x0C, bytes([0x42, 0x0C, 0x00, 0x12, 0xC0])) == 1200.0
    assert services.obd_parse_freeze_value(0x05, bytes([0x42, 0x05, 0x00, 0x7D])) == 85
    assert services.obd_parse_freeze_value(0x07, bytes([0x42, 0x07, 0x00, 0x8A])) == 7.8
    assert services.obd_parse_freeze_value(0x0C, bytes([0x42, 0x0C, 0x00])) is None


def test_obd_vin_and_calid():
    vin = "JTDKB20U040123456"
    assert services.obd_parse_vin(b"\x49\x02\x01" + vin.encode()) == vin
    assert services.obd_parse_vin(b"\x7F\x09\x11") is None
    assert services.obd_parse_calids(b"\x49\x04\x01" + b"34715100".ljust(16, b"\x00")) == ["34715100"]


# ── Toyota enhanced ──────────────────────────────────────────────────────────

def test_kwp_dtc_layouts():
    assert services.kwp_dtc_request() == b"\x13"
    # ISO 14230-3 layout: count byte, then pairs
    assert services.kwp_parse_dtcs(bytes([0x53, 0x02, 0x08, 0x52, 0x31, 0x02])) == ["P0852", "P3102"]
    assert services.kwp_parse_dtcs(bytes([0x53, 0x00])) == []
    # Toyota K-line captures: no count byte, zero-filled slots
    assert services.kwp_parse_dtcs(bytes([0x53, 0x01, 0x30, 0x00, 0x00, 0x00, 0x00])) == ["P0130"]
    assert services.kwp_parse_dtcs(bytes([0x53, 0x52, 0x71, 0x52, 0x72, 0x52, 0x73])) == [
        "C1271", "C1272", "C1273"]
    assert services.kwp_parse_dtcs(bytes([0x7F, 0x13, 0x11])) == []


def test_kwp_ident():
    assert services.kwp_ident_request() == bytes([0x1A, 0x88, 0x01])       # opendbc: 1A 88 01 -> 5A 88 01
    out = services.kwp_parse_ident(bytes([0x5A, 0x88, 0x01]) + b"\x02" + b"89661-47100\x00\x00")
    assert out["option"] == 0x88 and out["strings"] == ["89661-47100"]
    assert services.kwp_parse_ident(b"\x7F\x1A\x12") is None


def test_negative_and_positive_matching():
    assert services.negative_code(b"\x13", b"\x7F\x13\x11") == 0x11
    assert services.negative_code(b"\x13", b"\x7F\x21\x11") is None
    assert services.is_positive(b"\x21\xC3", b"\x61\xC3\x00") and not services.is_positive(b"\x21", b"\x43")
    assert services.matches(b"\x04", b"\x7F\x04\x22") and services.matches(b"\x04", b"\x44")
    assert "conditions not correct" in services.nrc_text(0x22)


# ── safety allow-list ────────────────────────────────────────────────────────

@pytest.mark.parametrize("req", ["0101", "020200", "03", "07", "0902", "13", "13FF00", "1A8801",
                                 "21C3", "3E", "12", "17"])
def test_read_only_requests_pass(req):
    services.check_request(bytes.fromhex(req))


@pytest.mark.parametrize("req", ["1101", "1001", "2701", "3021020001", "3101", "3B01", "2E0001",
                                 "3400", "3600", "2801", "8500", "08", "A800"])
def test_forbidden_requests_never_pass(req):
    for allow in (False, True):
        with pytest.raises(services.DiagSafetyError):
            services.check_request(bytes.fromhex(req), allow_clear=allow)


def test_clear_needs_the_flag():
    for req in (b"\x04", b"\x14"):
        with pytest.raises(services.DiagSafetyError):
            services.check_request(req)
        services.check_request(req, allow_clear=True)
    with pytest.raises(services.DiagSafetyError):
        services.check_request(b"")
    with pytest.raises(services.DiagSafetyError):
        services.check_request(b"\x99")           # unknown service: not on the allow-list


def test_parse_hex_request():
    assert services.parse_hex_request("21 c3") == b"\x21\xC3"
    assert services.parse_hex_request("0x21,0xC3") == b"\x21\xC3"
    for bad in ("", "2", "zz", "21C"):
        with pytest.raises(ValueError):
            services.parse_hex_request(bad)


# ── dictionary ───────────────────────────────────────────────────────────────

def test_dictionary_loaded_from_the_manual():
    st = dictionary.stats()
    assert st["codes"] > 350 and st["inf_codes"] > 250


def test_dictionary_entries_for_the_suspects():
    c = dictionary.annotate("C2300", "transmission")
    assert c["known"] and c["manual_page"] == "05-931" and "ACT" in c["description"]
    assert c["manual_url"].endswith("m_05_0931.pdf")
    b = dictionary.annotate("B2278", "power_source")
    assert "power switch" in b["description"].lower() and "Power switch" in b["trouble_area"]
    p = dictionary.annotate("P0852", "hv", inf="580")
    assert p["master_warning"] == "all"
    assert p["detail"]["detection"] == "Open or +B short in P position switch circuit"
    assert p["detail"]["master_warning"] is True and p["detail"]["chk_eng"] is False
    assert dictionary.annotate("P0420", "engine")["manual_page"] == "05-139"
    assert dictionary.annotate("B1260", "gateway")["description"].startswith("Transmission control ECU")


def test_dictionary_prefers_the_reporting_ecu():
    hv = dictionary.lookup("U0100", "hv")
    batt = dictionary.lookup("U0100", "battery")
    assert hv["ecu"] == "hv" and batt["ecu"] == "battery" and hv["page"] != batt["page"]
    other = dictionary.annotate("C2300", "engine")
    assert other["known"] and other.get("other_ecu") is True


def test_unknown_code_is_shown_raw():
    a = dictionary.annotate("P1ABC", "engine")
    assert a == {"code": "P1ABC", "description": "", "system": "", "manual_page": None,
                 "manual_url": None, "trouble_area": [], "known": False}
    assert dictionary.lookup("B9999") is None
