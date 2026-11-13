"""
Diagnostic services: request builders, response parsers and the safety
allow-list. Pure functions over byte strings — no I/O, no state.

Two service families are used (docs/TOYOTA_DIAGNOSTICS.md, section 3):

* **OBD** (ISO 15031-5 / SAE J1979 over ISO 15765-4): modes 01, 02, 03, 07, 09
  read; 04 clears. Answered by the emission-related ECUs (ECM, HV control ECU,
  battery ECU). Mode 03/07 list emission-related codes only — the codes that
  light CHK ENG — which is why a mode 03 scan can be empty while the master
  warning is on.
* **Toyota enhanced** (ISO 14230-3 / KWP2000 service ids carried on CAN):
  0x13 read DTCs, 0x12 freeze frame, 0x14 clear, 0x1A ECU identification,
  0x21 read data by local identifier, 0x3E tester present.

Every request the engine transmits passes :func:`check_request`; anything that
is not on the read-only list (or, with an explicit flag, the clear list) is
refused before it reaches the transport.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import dtc as dtcmod

# ── service ids ──────────────────────────────────────────────────────────────

OBD_CURRENT_DATA = 0x01
OBD_FREEZE_FRAME = 0x02
OBD_STORED_DTC = 0x03
OBD_CLEAR_DTC = 0x04
OBD_PENDING_DTC = 0x07
OBD_VEHICLE_INFO = 0x09
OBD_PERMANENT_DTC = 0x0A

KWP_READ_FREEZE_FRAME = 0x12
KWP_READ_DTC = 0x13
KWP_CLEAR_DTC = 0x14
KWP_READ_DTC_STATUS = 0x17
KWP_READ_DTC_BY_STATUS = 0x18
KWP_READ_ECU_ID = 0x1A
KWP_READ_LOCAL_ID = 0x21
KWP_TESTER_PRESENT = 0x3E

NEGATIVE_RESPONSE = 0x7F

#: Services that only read. Sending them changes nothing in the ECU.
READ_ONLY_SIDS = frozenset({
    OBD_CURRENT_DATA, OBD_FREEZE_FRAME, OBD_STORED_DTC, OBD_PENDING_DTC,
    OBD_VEHICLE_INFO, OBD_PERMANENT_DTC,
    KWP_READ_FREEZE_FRAME, KWP_READ_DTC, KWP_READ_DTC_STATUS,
    KWP_READ_DTC_BY_STATUS, KWP_READ_ECU_ID, KWP_READ_LOCAL_ID,
    KWP_TESTER_PRESENT,
})

#: Services that erase stored diagnostic information. Only with ``allow_clear``.
CLEAR_SIDS = frozenset({OBD_CLEAR_DTC, KWP_CLEAR_DTC})

#: Never sent, whatever the caller asks: session control, ECU reset, security
#: access, communication control, write / IO-control / routine / programming
#: services, OBD mode 08 (control of on-board systems).
FORBIDDEN_SIDS = frozenset({
    0x08, 0x10, 0x11, 0x20, 0x27, 0x28, 0x2C, 0x2E, 0x2F, 0x30, 0x31, 0x32, 0x33,
    0x34, 0x35, 0x36, 0x37, 0x38, 0x3B, 0x3D, 0x83, 0x84, 0x85, 0x86, 0x87,
    0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9,
})

NRC_NAMES = {
    0x10: "general reject",
    0x11: "service not supported",
    0x12: "sub-function not supported / invalid format",
    0x13: "incorrect message length",
    0x21: "busy, repeat request",
    0x22: "conditions not correct",
    0x23: "routine not complete",
    0x31: "request out of range",
    0x33: "security access denied",
    0x78: "response pending",
    0x7E: "sub-function not supported in active session",
    0x7F: "service not supported in active session",
}
NRC_RESPONSE_PENDING = 0x78


class DiagSafetyError(ValueError):
    """The request is not allowed by the safety policy."""


def check_request(payload: bytes, allow_clear: bool = False) -> None:
    """Raise :class:`DiagSafetyError` unless ``payload`` may be transmitted."""
    payload = bytes(payload)
    if not payload:
        raise DiagSafetyError("empty request")
    sid = payload[0]
    if sid in FORBIDDEN_SIDS:
        raise DiagSafetyError("service 0x%02X is never sent (not a read-only service)" % sid)
    if sid in CLEAR_SIDS:
        if not allow_clear:
            raise DiagSafetyError("service 0x%02X erases DTCs: explicit confirmation required" % sid)
        return
    if sid not in READ_ONLY_SIDS:
        raise DiagSafetyError("service 0x%02X is not on the read-only allow-list" % sid)


# ── generic response handling ────────────────────────────────────────────────

def negative_code(request: bytes, response: bytes) -> Optional[int]:
    """NRC when ``response`` is the negative response to ``request``."""
    if len(response) >= 3 and response[0] == NEGATIVE_RESPONSE and response[1] == request[0]:
        return response[2]
    return None


def is_positive(request: bytes, response: bytes) -> bool:
    """Positive response: the service id echoed with bit 6 set."""
    return bool(response) and response[0] == ((request[0] + 0x40) & 0xFF)


def matches(request: bytes, response: bytes) -> bool:
    """True when ``response`` belongs to ``request`` (positive or negative)."""
    return is_positive(request, response) or negative_code(request, response) is not None


def nrc_text(nrc: int) -> str:
    return "0x%02X %s" % (nrc, NRC_NAMES.get(nrc, "negative response"))


def hexs(data: bytes) -> str:
    return bytes(data).hex(" ").upper()


# ── OBD (SAE J1979) ──────────────────────────────────────────────────────────

def obd_dtc_request(mode: int) -> bytes:
    return bytes([mode])


def obd_parse_dtcs(response: bytes) -> List[str]:
    """Mode 03 / 07 / 0A response on CAN: ``<mode+0x40> <count> <hi lo>...``.

    The count byte is not trusted (some ECUs pad with 0x0000 pairs); every
    non-zero pair after it is decoded.
    """
    if len(response) < 2:
        return []
    return [code for code, _ in dtcmod.decode_list(response[2:], 2)]


def obd_monitor_status_request() -> bytes:
    return bytes([OBD_CURRENT_DATA, 0x01])


def obd_parse_monitor_status(response: bytes) -> Optional[Dict[str, Any]]:
    """Mode 01 PID 01: MIL state, number of confirmed emission DTCs, readiness.

    ``41 01 A B C D``: A bit 7 = MIL commanded on, A bits 6-0 = DTC count;
    B bits 0-2 = misfire/fuel/components monitors supported, bits 4-6 = those
    monitors NOT complete; C = non-continuous monitors supported, D = those
    monitors NOT complete (spark-ignition layout).
    """
    if len(response) < 6 or response[0] != 0x41 or response[1] != 0x01:
        return None
    a, b, c, d = response[2:6]
    names = ["catalyst", "heated_catalyst", "evap", "secondary_air", "ac_refrigerant",
             "o2_sensor", "o2_heater", "egr"]
    incomplete = [n for i, n in enumerate(names) if (c >> i) & 1 and (d >> i) & 1]
    complete = [n for i, n in enumerate(names) if (c >> i) & 1 and not (d >> i) & 1]
    cont = ["misfire", "fuel_system", "components"]
    incomplete += [n for i, n in enumerate(cont) if (b >> i) & 1 and (b >> (i + 4)) & 1]
    complete += [n for i, n in enumerate(cont) if (b >> i) & 1 and not (b >> (i + 4)) & 1]
    return {"mil": bool(a & 0x80), "dtc_count": a & 0x7F,
            "monitors_complete": complete, "monitors_incomplete": incomplete}


def obd_pid_request(pid: int) -> bytes:
    return bytes([OBD_CURRENT_DATA, pid & 0xFF])


def obd_freeze_request(pid: int, frame: int = 0) -> bytes:
    return bytes([OBD_FREEZE_FRAME, pid & 0xFF, frame & 0xFF])


#: Freeze-frame PIDs read before a clear: (pid, name, unit, decoder).
FREEZE_PIDS: List[Tuple[int, str, str, Any]] = [
    (0x03, "fuel_system_status", "", lambda d: d[0]),
    (0x04, "engine_load", "%", lambda d: round(d[0] * 100 / 255, 1)),
    (0x05, "coolant_temp", "degC", lambda d: d[0] - 40),
    (0x06, "short_fuel_trim", "%", lambda d: round((d[0] - 128) * 100 / 128, 1)),
    (0x07, "long_fuel_trim", "%", lambda d: round((d[0] - 128) * 100 / 128, 1)),
    (0x0C, "rpm", "rpm", lambda d: ((d[0] << 8) | d[1]) / 4),
    (0x0D, "speed", "km/h", lambda d: d[0]),
    (0x0F, "intake_air_temp", "degC", lambda d: d[0] - 40),
    (0x10, "maf", "g/s", lambda d: ((d[0] << 8) | d[1]) / 100),
    (0x11, "throttle", "%", lambda d: round(d[0] * 100 / 255, 1)),
]


def obd_parse_freeze_dtc(response: bytes) -> Optional[str]:
    """Mode 02 PID 02 (``42 02 <frame> <hi> <lo>``): the code that stored the
    freeze frame; ``None`` when no frame is stored (0x0000)."""
    if len(response) < 5 or response[0] != 0x42 or response[1] != 0x02:
        return None
    return dtcmod.decode(response[3], response[4])


def obd_parse_freeze_value(pid: int, response: bytes) -> Optional[Any]:
    """Decode one mode 02 PID value (``42 <pid> <frame> <data>``)."""
    if len(response) < 4 or response[0] != 0x42 or response[1] != pid:
        return None
    for p, _name, _unit, fn in FREEZE_PIDS:
        if p == pid:
            try:
                return fn(response[3:])
            except IndexError:
                return None
    return None


def obd_parse_pid_value(pid: int, response: bytes) -> Optional[bytes]:
    """Data bytes of a mode 01 response (``41 <pid> <data>``)."""
    if len(response) < 3 or response[0] != 0x41 or response[1] != pid:
        return None
    return bytes(response[2:])


def obd_vin_request() -> bytes:
    return bytes([OBD_VEHICLE_INFO, 0x02])


def obd_parse_vin(response: bytes) -> Optional[str]:
    """Mode 09 PID 02 on CAN: ``49 02 <count> <17 ASCII>``."""
    if len(response) < 4 or response[0] != 0x49 or response[1] != 0x02:
        return None
    text = bytes(b for b in response[3:] if 0x20 <= b < 0x7F).decode("ascii", "replace").strip()
    return text or None


def obd_calid_request() -> bytes:
    return bytes([OBD_VEHICLE_INFO, 0x04])


def obd_parse_calids(response: bytes) -> List[str]:
    """Mode 09 PID 04: ``49 04 <count> <16 ASCII bytes per calibration id>``."""
    if len(response) < 4 or response[0] != 0x49 or response[1] != 0x04:
        return []
    body = response[3:]
    out = []
    for i in range(0, len(body), 16):
        text = bytes(b for b in body[i:i + 16] if 0x20 <= b < 0x7F).decode("ascii", "replace").strip()
        if text:
            out.append(text)
    return out


def obd_clear_request() -> bytes:
    return bytes([OBD_CLEAR_DTC])


# ── Toyota enhanced (KWP2000 service ids on CAN) ─────────────────────────────

def kwp_dtc_request(sub: Optional[int] = None) -> bytes:
    """Service 0x13 (readDiagnosticTroubleCodes). Toyota testers send it without
    a group parameter; ``sub`` appends one byte for the variants documented in
    docs/TOYOTA_DIAGNOSTICS.md."""
    return bytes([KWP_READ_DTC]) if sub is None else bytes([KWP_READ_DTC, sub & 0xFF])


def kwp_parse_dtcs(response: bytes) -> List[str]:
    """Service 0x13 positive response.

    Two layouts exist and the response does not say which one it is:

    * ISO 14230-3: ``53 <count> <hi lo>...``
    * Toyota K-line captures: ``53 <hi lo> <hi lo> ...`` without a count,
      unused slots zero-filled (``53 01 30 00 00 00 00`` = P0130)

    The body length decides: an odd number of bytes after 0x53 means a count
    byte followed by pairs, an even number means pairs only. Zero pairs are
    filler in both.
    """
    if len(response) < 1 or response[0] != 0x53:
        return []
    body = response[1:]
    if len(body) % 2:
        body = body[1:]
    return [code for code, _ in dtcmod.decode_list(body, 2)]


def kwp_clear_request() -> bytes:
    """Service 0x14 (clearDiagnosticInformation), Toyota form without a group."""
    return bytes([KWP_CLEAR_DTC])


def kwp_ident_request(option: int = 0x88, extra: bytes = b"\x01") -> bytes:
    """Service 0x1A (readEcuIdentification)."""
    return bytes([KWP_READ_ECU_ID, option & 0xFF]) + bytes(extra)


def kwp_parse_ident(response: bytes) -> Optional[Dict[str, Any]]:
    """Service 0x1A response: ``5A <option> ...``; printable runs are returned
    as strings (calibration / part numbers), the raw bytes always."""
    if len(response) < 2 or response[0] != 0x5A:
        return None
    body = bytes(response[2:])
    strings: List[str] = []
    run = bytearray()
    for b in body + b"\x00":
        if 0x20 <= b < 0x7F:
            run.append(b)
        else:
            if len(run) >= 4:
                strings.append(run.decode("ascii"))
            run = bytearray()
    return {"option": response[1], "raw": body.hex().upper(), "strings": strings}


def kwp_local_id_request(local_id: int) -> bytes:
    return bytes([KWP_READ_LOCAL_ID, local_id & 0xFF])


def kwp_tester_present() -> bytes:
    return bytes([KWP_TESTER_PRESENT])


def parse_hex_request(text: str) -> bytes:
    """``"21C3"`` / ``"21 c3"`` / ``"0x21 0xC3"`` -> bytes. ``ValueError`` on junk."""
    cleaned = text.replace("0x", "").replace("0X", "").replace(" ", "").replace(",", "")
    if not cleaned or len(cleaned) % 2:
        raise ValueError("not a hex byte string: %r" % (text,))
    return bytes.fromhex(cleaned)
