"""Raw messages (trip-recorder format) -> collector events.

One decoder for the live tap and for the backfill: both feed the messages the
ingress hands to its message-log callbacks, i.e. exactly what the trip recorder
writes. Scalings: docs/prius_can.md sections 2, 5, 6, 7.

Message shapes (``device_id``, ``data``):

    1   {"i": "0x03B", "d": [...], "bus": 0}                  panda broadcast frame
    1   {"a": "sub"|"resp", "i": "0x7EB", "d": [...]}         solicited answer
    201 {"v": 11.86, "ma": 245.5, "mah": 464.3, "bmp2_t": 4.9} powerbox INA219 + ambient
    202 {"acc": true}                                           powerbox ignition sense
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .model import N_BLOCKS

DEVICE_CAN = 1
DEVICE_POWERBOX = 200
DEVICE_POWERBOX_POWER = 201
DEVICE_POWERBOX_EVENTS = 202

# Broadcast ids the collector uses (strings as the panda path formats them).
BROADCAST_IDS = frozenset(("0x03B", "0x3CB", "0x529", "0x0B4"))
SPEED_DECIMATE = 16      # 0x0B4 arrives at ~33 Hz; every 16th frame is enough


def wanted(device_id: int, data: Dict[str, Any]) -> bool:
    """Cheap pre-filter for the live tap (runs on the ingress threads)."""
    if device_id == DEVICE_CAN:
        return data.get("i") in BROADCAST_IDS or data.get("a") in ("sub", "resp")
    return device_id in (DEVICE_POWERBOX_POWER, DEVICE_POWERBOX_EVENTS)


def _s16(hi: int, lo: int) -> float:
    return (hi * 256 + lo) / 100.0 - 327.68


def iso_payload(data: List[int]) -> Optional[List[int]]:
    """Service payload [mode, pid, ...] of a solicited answer, or None.

    Reassembled ISO-TP answers start with the mode byte (>= 0x40); single frames
    with the PCI length (1..7). A lone ISO-TP *first frame* (0x10 nn ...: the
    gateway subscription ran without ``isotp``) yields the first 6 payload
    bytes: enough for the leading fields of 21CF.
    """
    if not data or len(data) < 3:
        return None
    b0 = data[0]
    if b0 >= 0x40:
        return list(data)
    if 1 <= b0 <= 7:
        return list(data[1:1 + b0])
    if (b0 & 0xF0) == 0x10 and len(data) >= 8:
        return list(data[2:8])
    return None


def decode_21ce(p: List[int]) -> Optional[dict]:
    """p = payload after 61 CE."""
    if len(p) < 3 + 2 * N_BLOCKS:
        return None
    volts = [_s16(p[3 + 2 * k], p[4 + 2 * k]) for k in range(N_BLOCKS)]
    if not all(5.0 < v < 25.0 for v in volts):
        return None
    return {"soc": 0.5 * p[0], "i": _s16(p[1], p[2]), "v": volts}


def decode_21cf(p: List[int]) -> dict:
    """p = payload after 61 CF; fields beyond the received length are absent."""
    out: dict = {}
    n = len(p)
    if n >= 2:
        t = _s16(p[0], p[1])
        if -40.0 <= t <= 90.0:
            out["intake"] = t
    if n >= 3:
        out["fan_v"] = 0.2 * p[2] - 25.6
    if n >= 4:
        out["aux_v"] = 0.2 * p[3] - 25.6
    if n >= 5:
        out["chg_kw"] = p[4] - 64
    if n >= 6:
        out["dis_kw"] = p[5] - 64
    if n >= 7:
        out["dsoc"] = 0.01 * p[6]
    if n >= 9:
        out["fan"] = p[8]
    for k, name in ((10, "tb1"), (12, "tb2"), (14, "tb3")):
        if n >= k + 2:
            t = _s16(p[k], p[k + 1])
            if -40.0 <= t <= 90.0:
                out[name] = t
    return out


def decode_21d0(p: List[int]) -> Optional[dict]:
    if len(p) < 15 + N_BLOCKS:
        return None
    return {
        "t_low": p[1] * 256 + p[2], "t_dcinh": p[3] * 256 + p[4],
        "t_high": p[5] * 256 + p[6], "t_hot": p[7] * 256 + p[8],
        "r": [p[15 + k] for k in range(N_BLOCKS)],     # milliohm
    }


class FrameDecoder:
    """Feeds a collector from raw messages. Not thread-safe (one caller)."""

    def __init__(self, collector) -> None:
        self.c = collector
        self._speed_n = 0
        self.counts: Dict[str, int] = {}

    def _count(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def feed(self, t: float, device_id: int, data: Dict[str, Any]) -> None:
        if device_id == DEVICE_CAN:
            cid = data.get("i")
            d = data.get("d")
            if not isinstance(d, list):
                return
            if data.get("a") in ("sub", "resp"):
                if not data.get("err"):
                    self._solicited(t, cid, d)
            else:
                self._broadcast(t, cid, d)
        elif device_id == DEVICE_POWERBOX_POWER:
            v = data.get("v")
            if isinstance(v, (int, float)):
                self._count("pb")
                self.c.powerbox(t, float(v), data.get("ma"), data.get("mah"), data.get("bmp2_t"))
        elif device_id == DEVICE_POWERBOX_EVENTS:
            if "acc" in data:
                self.c.acc(t, bool(data["acc"]))

    def _broadcast(self, t: float, cid: Any, d: List[int]) -> None:
        c = self.c
        if cid == "0x03B":
            if len(d) >= 4:
                raw = ((d[0] & 0x0F) << 8) | d[1]
                if raw > 0x7FF:
                    raw -= 0x1000
                v = (d[2] << 8) | d[3]
                if 100 <= v <= 400:
                    self._count("pack")
                    c.pack(t, float(v), raw * 0.1)
        elif cid == "0x3CB":
            if len(d) >= 6:
                t1 = d[4] - 256 if d[4] > 127 else d[4]
                t2 = d[5] - 256 if d[5] > 127 else d[5]
                self._count("ecu")
                c.ecu(t, (d[2] * 256 + d[3]) / 2.0, d[1], d[0], t1, t2)
        elif cid == "0x0B4":
            self._speed_n += 1
            if self._speed_n % SPEED_DECIMATE == 0 and len(d) >= 7:
                c.speed(t, (d[5] * 256 + d[6]) * 0.01)
        elif cid == "0x529":
            if len(d) >= 4:
                c.ready(t, bool(d[3] & 0x80))

    def _solicited(self, t: float, cid: Any, d: List[int]) -> None:
        p = iso_payload(d)
        if p is None or len(p) < 2:
            return
        c = self.c
        mode, pid, body = p[0], p[1], p[2:]
        if cid == "0x7EB" and mode == 0x61:
            if pid == 0xCE:
                r = decode_21ce(body)
                if r is not None:
                    self._count("21ce")
                    c.blocks(t, r["soc"], r["i"], r["v"])
            elif pid == 0xCF:
                r = decode_21cf(body)
                if r:
                    self._count("21cf_full" if "tb1" in r else "21cf_first")
                    c.cf(t, r)
            elif pid == 0xD0:
                r = decode_21d0(body)
                if r is not None:
                    self._count("21d0")
                    c.d0(t, r)
        elif cid == "0x7E8" and mode == 0x41:
            if pid == 0x42 and len(body) >= 2:
                c.obd(t, "aux_v", (body[0] * 256 + body[1]) / 1000.0)
            elif pid == 0x0C and len(body) >= 2:
                c.obd(t, "rpm", (body[0] * 256 + body[1]) / 4.0)
            elif pid == 0x46 and len(body) >= 1:
                c.obd(t, "ambient", body[0] - 40.0)
        elif cid == "0x7EA" and mode == 0x61 and pid == 0xC3 and len(body) >= 26:
            # 21C3 bytes Y / Z: MG1 / MG2 inverter temperature
            c.obd(t, "inv1", body[24] - 40.0)
            c.obd(t, "inv2", body[25] - 40.0)
