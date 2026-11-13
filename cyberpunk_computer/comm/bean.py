"""
Toyota BEAN (body bus) helpers for the backend: frame records from the
powerbox receiver (fw 2.3.0, powerbox/bean_rx.py), the per-message table and
the few known message IDs.

Wire record from the firmware (device 203, ``{"t":ms,"bf":[...],"st":{...}}``):

    [t_ms, pri, "DST", "MES", "DATAHEX", rsp, code]      (+ ", 1" = our own)

``t_ms`` is the firmware ticks_ms of the frame's SOF (wraps at 2^30), ``t``
the firmware ticks_ms when the line was sent; ``rsp`` the two raw RSP bits
(0..3, ``01`` = ACK; null if the frame broke before RSP); ``code`` 0 = ok,
else :data:`ERROR_NAMES`. For failed frames DATAHEX holds every byte after
MES that arrived (it may include the CRC byte). An 8th element 1 (fw >=
2.4.0) marks a frame, a fragment or the edge-test pulse sent by the
powerbox's own transmitter (``"own": True`` in the frame dict); such frames
stay in the frame ring and the capture log but not in the message table.

Known IDs (:func:`decode_known`): the door / lock / lamp / key-window group
is CONFIRMED on this NHW20 with the car OFF (docs/BEAN_FINDINGS.md,
docs/BEAN_ANALYSIS.md). The IG-ON group (coolant, ambient, speed, rpm, shift,
P-lock, power-source state) is carried over from JP dumps and other
Toyota/Lexus models (RX300, IS300, bean_tools) and stays UNVERIFIED on this
car until captured with the key on.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

ERROR_NAMES = ("ok", "crc", "stuff", "len", "frm", "trunc", "ovf")
TICKS_PERIOD_MS = 1 << 30
MAX_MESSAGES = 256            # message-table cap (oldest-seen dropped beyond)


def frame_from_wire(rec: list, line_t: Optional[int], recv_ts: float) -> Optional[Dict[str, Any]]:
    """Firmware record -> frame dict with a wall-clock ``ts`` (seconds).

    The wall time is ``recv_ts`` minus the age the firmware reports
    (``line_t - t_ms`` modulo the 2^30 ms tick period)."""
    if not isinstance(rec, (list, tuple)) or len(rec) < 7:
        return None
    t_ms, pri, dst, mes, data, rsp, code = rec[:7]
    ts = recv_ts
    try:
        if line_t is not None and t_ms is not None:
            age = (int(line_t) - int(t_ms)) % TICKS_PERIOD_MS
            if age < 600_000:
                ts = recv_ts - age / 1000.0
    except (TypeError, ValueError):
        pass
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = 4
    out = {
        "ts": round(ts, 3),
        "t_ms": t_ms,
        "pri": pri,
        "dst": str(dst or "").upper(),
        "mes": str(mes or "").upper(),
        "data": str(data or "").upper(),
        "rsp": rsp,
        "ok": code == 0,
        "err": None if code == 0 else (ERROR_NAMES[code] if 0 < code < len(ERROR_NAMES) else str(code)),
    }
    if len(rec) > 7 and rec[7]:
        out["own"] = True
    return out


def _b(data: bytes, i: int) -> Optional[int]:
    return data[i] if len(data) > i else None


def decode_known(mes: str, data_hex: str, dst: Optional[str] = None) -> Dict[str, Any]:
    """Named values decoded from a BEAN frame's MES id + data (and, where it
    matters to avoid a collision, its DST).

    CONFIRMED on the owner's NHW20 with the car OFF (docs/BEAN_FINDINGS.md,
    docs/BEAN_ANALYSIS.md):
      FE:B6  door lock state (b0) + door-open courtesy switches (b1)
      FE:E1  lock/unlock EVENT (b2), by source: 04/08 remote fob, 10/20 smart
      FE:7F  any-door-open indicator (b2 bit7); lamps (b0, car ON)
      FE:83  interior dome / luggage lamp (b3); key/ACC/IG flags (b0, car ON)
      FE:D8  driver-door smart-key check window (b1 bit0, ~30 s)
      ..:3F  odometer, 24-bit km (52:3F on this car, owner-confirmed exact)

    Carried over from JP dumps / other Toyota-Lexus models (RX300, IS300,
    bean_tools) and UNVERIFIED on this car until captured with the key on:
      FE:2C coolant (x/2), FE:CD ambient (x-48), FE:24 speed, FE:26 rpm,
      FE:40 shift, FE:50 P-lock, FE:62 power-source state.

    NB: FE:E1 is a STATUS REPORT the body ECU broadcasts after it acts, not a
    command — see docs/BEAN_FINDINGS.md (there is no door-lock command on this
    bus; the request reaches the body ECU on a dedicated serial line).
    """
    try:
        data = bytes.fromhex(data_hex or "")
    except ValueError:
        return {}
    mes = (mes or "").upper()
    dst = (dst or "").upper()
    out: Dict[str, Any] = {}
    d0, d1, d2, d3 = _b(data, 0), _b(data, 1), _b(data, 2), _b(data, 3)

    # Odometer rides a directed frame on this car (52:3F); accept any DST.
    if mes == "3F":
        if len(data) >= 3:
            out["odometer_km"] = (data[0] << 16) | (data[1] << 8) | data[2]
        return out

    # The rest are body-bus broadcasts (DST 0xFE). Guard on DST so a directed
    # frame that happens to reuse a MES byte is not mis-decoded.
    if dst not in ("", "FE") or d0 is None:
        return out

    if mes == "B6":                         # [C] door lock + door-open state
        out["doors_locked"] = (d0 == 0x00)
        out["driver_door_unlocked"] = bool(d0 & 0x01)
        out["other_doors_unlocked"] = bool(d0 & 0xC0)
        out["lock_state_raw"] = d0
        if d1 is not None:
            out["driver_door_open"] = bool(d1 & 0x80)
            out["passenger_door_open"] = bool(d1 & 0x40)
            out["rear_door_open"] = bool(d1 & 0x04)
            out["any_door_open"] = bool(d1 & 0xC4)
    elif mes == "E1":                       # [C] lock/unlock event (b2)
        if d2:
            out["lock_event"] = {0x04: "remote_unlock", 0x08: "remote_lock",
                                 0x10: "smart_unlock", 0x20: "smart_lock"}.get(
                                     d2, "0x%02X" % d2)
            out["lock_event_raw"] = d2
    elif mes == "7F":                       # [C] any-door-open (b2); lamps (b0)
        out["lamp_tail"] = bool(d0 & 0x10)
        out["lamp_head"] = bool(d0 & 0x20)
        out["lamp_high_beam"] = bool(d0 & 0x08)
        if d2 is not None:
            out["door_open"] = bool(d2 & 0x80)
    elif mes == "83":                       # [C] dome/luggage lamp (b3); flags (b0)
        if d3 is not None:
            out["dome_lamp"] = bool(d3 & 0x08)
            out["luggage_lamp"] = bool(d3 & 0x04)
        out["key_inserted"] = bool(d0 & 0x02)
        out["acc_on"] = bool(d0 & 0x20)
        out["ig_on"] = bool(d0 & 0x40)
    elif mes == "D8":                       # [C] driver-door key window (b1)
        if d1 is not None:
            out["driver_key_window"] = bool(d1 & 0x01)
    elif mes == "62":                       # [I] power-source state (JP-derived)
        out["in_park"] = bool(d0 & 0x20)
        out["key_present"] = bool(d0 & 0x40)
        out["power_on"] = bool(d0 & 0x10)
        out["power_switch_pressed"] = bool(d0 & 0x08)
    elif mes == "2C":                       # [X] coolant
        out["coolant_c"] = d0 / 2.0
    elif mes == "CD":                       # [X] ambient (car ON)
        out["ambient_c"] = d0 - 48
    elif mes == "24":                       # [X] vehicle speed
        out["speed_kmh"] = d0
    elif mes == "26" and len(data) >= 2:    # [X] engine rpm (RX300 scaling)
        out["engine_rpm"] = round((data[0] * 256 + data[1]) / 5.12)
    elif mes == "40":                       # [X]/[C-JP] shift position
        names = [n for bit, n in ((0x80, "P"), (0x40, "R"), (0x20, "N"), (0x10, "D")) if d0 & bit]
        out["shift"] = names[0] if len(names) == 1 else ("" if not names else "/".join(names))
        out["shift_raw"] = d0
    elif mes == "50":                       # [I] P-lock state
        out["park_engaged"] = bool(d0 & 0x80)
    return out


def update_messages(table: Mapping[str, Mapping], frames: List[Mapping]) -> Dict[str, Dict]:
    """New message table: "DST:MES" -> {dst, mes, pri, count, last_ts, data,
    changes, period_ms}. Only CRC-good frames count. ``period_ms`` is an
    EWMA of the inter-arrival time (retransmissions and bursts included)."""
    out = {k: dict(v) for k, v in table.items()}
    for f in frames:
        if not f.get("ok"):
            continue
        key = "%s:%s" % (f["dst"], f["mes"])
        e = out.get(key)
        if e is None:
            if len(out) >= MAX_MESSAGES:
                oldest = min(out, key=lambda k: out[k].get("last_ts", 0))
                del out[oldest]
            out[key] = {"dst": f["dst"], "mes": f["mes"], "pri": f["pri"], "count": 1,
                        "last_ts": f["ts"], "data": f["data"], "changes": 0,
                        "period_ms": None}
            continue
        dt_ms = (f["ts"] - e["last_ts"]) * 1000.0
        if dt_ms > 0:
            p = e.get("period_ms")
            e["period_ms"] = round(dt_ms if p is None else p + 0.2 * (dt_ms - p), 1)
        if f["data"] != e["data"]:
            e["changes"] += 1
        e["count"] += 1
        e["last_ts"] = f["ts"]
        e["data"] = f["data"]
        e["pri"] = f["pri"]
    return out


def update_decoded(decoded: Mapping[str, Mapping], frames: List[Mapping]) -> Dict[str, Dict]:
    """``name -> {"value", "ts", "src": "DST:MES"}`` from CRC-good frames."""
    out = dict(decoded)
    for f in frames:
        if not f.get("ok"):
            continue
        for name, value in decode_known(f["mes"], f["data"], f.get("dst")).items():
            out[name] = {"value": value, "ts": f["ts"], "src": "%s:%s" % (f["dst"], f["mes"])}
    return out
