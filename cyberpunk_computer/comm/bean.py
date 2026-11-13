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

Known IDs (docs/BEAN_PROTOCOL.md §4): ALL of these are UNVERIFIED on the
NHW20 Prius. They come from other Toyota/Lexus models (RX300, IS300) or from
the bean_tools NHW20 dashboard and must be confirmed with captures.
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


def decode_known(mes: str, data_hex: str) -> Dict[str, Any]:
    """Named values for the few known MES-IDs. UNVERIFIED on the Prius.

    0x2C coolant = D0/2 C (RX300/IS300/BeanMPX car; NHW20 values plausible)
    0x40 shift   = D0 bit7 P, bit6 R, bit5 N, bit4 D (RX300; NHW20 P/R/N/D seen)
    0x24 speed   = D0 km/h (RX300/IS300; only 0 seen on the NHW20)
    0xCD ambient = D0 - 48 C (IS300/RX300 scaling)
    0x7F lamps   = b0.4 clearance/tail, b0.3 high beam, b2.7 door (bean_tools)
    0x3F odometer= D0..D2 24-bit km (bean_tools "mileage")
    """
    try:
        data = bytes.fromhex(data_hex or "")
    except ValueError:
        return {}
    mes = (mes or "").upper()
    out: Dict[str, Any] = {}
    d0 = _b(data, 0)
    if d0 is None:
        return out
    if mes == "2C":
        out["coolant_c"] = d0 / 2.0
    elif mes == "40":
        names = [n for bit, n in ((0x80, "P"), (0x40, "R"), (0x20, "N"), (0x10, "D")) if d0 & bit]
        out["shift"] = names[0] if len(names) == 1 else ("" if not names else "/".join(names))
        out["shift_raw"] = d0
    elif mes == "24":
        out["speed_kmh"] = d0
    elif mes == "CD":
        out["ambient_c"] = d0 - 48
    elif mes == "7F":
        out["lamp_tail"] = bool(d0 & 0x10)
        out["lamp_high_beam"] = bool(d0 & 0x08)
        b2 = _b(data, 2)
        if b2 is not None:
            out["door_open"] = bool(b2 & 0x80)
    elif mes == "3F" and len(data) >= 3:
        out["odometer_km"] = (data[0] << 16) | (data[1] << 8) | data[2]
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
        for name, value in decode_known(f["mes"], f["data"]).items():
            out[name] = {"value": value, "ts": f["ts"], "src": "%s:%s" % (f["dst"], f["mes"])}
    return out
