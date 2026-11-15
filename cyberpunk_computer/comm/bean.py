"""
Toyota BEAN (body bus) helpers for the backend: frame records from the
powerbox receiver (fw 2.3.0, powerbox/bean_rx.py), the per-message table and
the known message IDs.

Wire record from the firmware (device 203, ``{"t":ms,"bf":[...],"st":{...}}``):

    [t_ms, pri, "DST", "MES", "DATAHEX", rsp, code]      (+ ", 1" = our own)

``t_ms`` is the frame's SOF time on the firmware's frame clock (wraps at
2^30), ``t`` the firmware ticks_ms when the line was sent; ``rsp`` the two raw
RSP bits (0..3, ``01`` = ACK; null if the frame broke before RSP); ``code`` 0
= ok, else :data:`ERROR_NAMES`. For failed frames DATAHEX holds every byte
after MES that arrived (it may include the CRC byte). An 8th element 1 (fw >=
2.4.0) marks a frame, a fragment or the edge-test pulse sent by the
powerbox's own transmitter (``"own": True`` in the frame dict); such frames
stay in the frame ring and the capture log but not in the message table.

Frame clock: fw <= 2.4.0 accumulated the frame clock from decoded run
lengths, which lost the length of every bus sleep longer than ~9 min (an
OverflowError at the first edge after it, see powerbox/README.md) and 0.5 us
per dominant run. Its ``t_ms`` therefore jumps by an arbitrary amount
(mod 2^30 us) at a wake-up and runs slow while the bus is busy; on the
2026-11-13 drive it was 236.4 s ahead of ``t``. fw >= 2.4.1 anchors frame
times to the edge stamps, so ``t - t_ms`` is the frame's real age (well
under a second) and :func:`frame_from_wire` uses it directly. For older
firmware :class:`BeanClock` maps ``t_ms`` onto the monotonic receive clock
with a drift-tracking lower-envelope estimate of ``recv - t_ms``.

Known IDs (:func:`decode_known`), per docs/BEAN_SIGNALS.md §4 (ID map;
survey 2026-11-14). Keys starting with ``prov_`` are PROBABLE decodes
(survey confidence "P"): they are kept for display/logging only and nothing
in the backend may depend on them. Everything else is CONFIRMED ("C") on
this NHW20 (or carried over and marked where it is not).
"""

from __future__ import annotations

import collections
from typing import Any, Deque, Dict, List, Mapping, Optional

ERROR_NAMES = ("ok", "crc", "stuff", "len", "frm", "trunc", "ovf")
TICKS_PERIOD_MS = 1 << 30
MAX_MESSAGES = 256            # message-table cap (oldest-seen dropped beyond)

#: An age from the firmware's line time ``t`` is trusted only in this range
#: (seconds). A pre-2.4.1 frame clock can be minutes off ``t`` (the age then
#: wraps to ~12 days) and the estimator takes over.
MAX_LINE_AGE_S = 5.0


class BeanClock:
    """Maps the firmware frame clock (``t_ms``) onto the receive clock.

    Every frame satisfies ``recv >= T_true + min_latency`` and
    ``T_true = t_ms/1000 + offset(t)`` with a slowly drifting offset. The
    lower envelope of ``recv - t_ms/1000`` therefore tracks
    ``offset + min_latency``. The estimator keeps the minimum per
    ``bucket_s`` bucket (the last ``keep`` buckets, none older than
    ``keep * bucket_s``), fits a line through the minima for the drift, and
    lowers the line until it touches the lowest minimum, so it is a lower
    envelope, not an average. A frame then gets
    ``min(recv, t_ms/1000 + offset(recv))``.

    The receive clock must be MONOTONIC (``time.monotonic()``): a wall-clock
    step (NTP after the RTC-less phone boots) would otherwise look like a
    frame-clock jump. ``t_ms`` wrap-around (2^30 ms) is unwrapped. The
    estimate restarts when a batch's offset leaves the estimate by more than
    ``reset_s`` in either direction (firmware reboot, or a pre-2.4.1 frame
    clock jumping at a bus wake-up): keeping the old, lower buckets after an
    upward jump would date every frame too early by the jump. Buckets older
    than ``keep * bucket_s`` are dropped, so the drift line is never
    extrapolated across a parked gap.
    """

    def __init__(self, bucket_s: float = 10.0, keep: int = 12, reset_s: float = 5.0) -> None:
        self.bucket_s = bucket_s
        self.keep = keep
        self.reset_s = reset_s
        self._buckets: Deque[List[float]] = collections.deque()   # [bucket_id, x, min_off]
        self._last_raw: Optional[int] = None
        self._epoch = 0
        self.resets = 0

    def _unwrap(self, t_ms: int) -> float:
        t_ms = int(t_ms) % TICKS_PERIOD_MS
        if self._last_raw is not None and t_ms < self._last_raw \
                and self._last_raw - t_ms > TICKS_PERIOD_MS // 2:
            self._epoch += 1
        self._last_raw = t_ms
        return (self._epoch * TICKS_PERIOD_MS + t_ms) / 1000.0

    def reset(self) -> None:
        self._buckets.clear()
        self._last_raw = None
        self._epoch = 0
        self.resets += 1

    def observe(self, recv_ts: float, t_ms_list: List[int]) -> List[float]:
        """Feed one received batch (``recv_ts`` on the monotonic receive
        clock); returns the frame times (unwrapped s) of ``t_ms_list`` for
        :meth:`to_wall`."""
        tf = [self._unwrap(t) for t in t_ms_list]
        if not tf:
            return tf
        off = min(recv_ts - x for x in tf)
        window = self.keep * self.bucket_s
        while self._buckets and recv_ts - self._buckets[0][1] > window:
            self._buckets.popleft()
        est = self.offset(recv_ts)
        if est is not None and abs(off - est) > self.reset_s:
            # The frame clock jumped (firmware restart, pre-2.4.1 wake-up
            # jump): start over, keeping this batch's frame times in the
            # new epoch.
            self.reset()
            tf = [self._unwrap(t) for t in t_ms_list]
            off = min(recv_ts - x for x in tf)
        bid = int(recv_ts // self.bucket_s)
        if self._buckets and self._buckets[-1][0] == bid:
            b = self._buckets[-1]
            if off < b[2]:
                b[1], b[2] = recv_ts, off
        else:
            self._buckets.append([bid, recv_ts, off])
            while len(self._buckets) > self.keep:
                self._buckets.popleft()
        return tf

    def offset(self, at: float) -> Optional[float]:
        """Estimated ``wall - frame_clock`` at receive time ``at``."""
        n = len(self._buckets)
        if n == 0:
            return None
        if n < 3:
            return min(b[2] for b in self._buckets)
        xs = [b[1] for b in self._buckets]
        ys = [b[2] for b in self._buckets]
        mx = sum(xs) / n
        my = sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = 0.0 if sxx <= 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        slope = max(-0.01, min(0.01, slope))      # clocks never drift 1 %
        # lower envelope: shift the line down to the lowest bucket minimum
        icpt = min(y - slope * (x - mx) for x, y in zip(xs, ys))
        return icpt + slope * (at - mx)

    def to_wall(self, frame_s: float, recv_ts: float) -> float:
        off = self.offset(recv_ts)
        if off is None:
            return recv_ts
        return min(recv_ts, frame_s + off)


def frame_from_wire(rec: list, line_t: Optional[int], recv_ts: float,
                    frame_s: Optional[float] = None,
                    clock: Optional[BeanClock] = None,
                    recv_mono: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Firmware record -> frame dict with a wall-clock ``ts`` (seconds).

    The wall time is ``recv_ts`` minus the age the firmware reports
    (``line_t - t_ms`` modulo the 2^30 ms tick period) when that age is
    plausible (0..:data:`MAX_LINE_AGE_S`); otherwise ``clock`` (a
    :class:`BeanClock` that has already observed this batch on the receive
    clock ``recv_mono`` (default ``recv_ts``), ``frame_s`` = its unwrapped
    frame time) maps ``t_ms``; without either it is ``recv_ts``."""
    if not isinstance(rec, (list, tuple)) or len(rec) < 7:
        return None
    t_ms, pri, dst, mes, data, rsp, code = rec[:7]
    ts = recv_ts
    try:
        age = None
        if line_t is not None and t_ms is not None:
            age = (int(line_t) - int(t_ms)) % TICKS_PERIOD_MS
        if age is not None and age <= MAX_LINE_AGE_S * 1000:
            ts = recv_ts - age / 1000.0
        elif clock is not None and frame_s is not None:
            rc = recv_ts if recv_mono is None else recv_mono
            ts = recv_ts - (rc - clock.to_wall(frame_s, rc))
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


def frames_from_batch(recs: List[list], line_t: Optional[int], recv_ts: float,
                      clock: Optional[BeanClock] = None,
                      recv_mono: Optional[float] = None) -> List[Dict[str, Any]]:
    """All frames of one firmware line, timed through ``clock`` (fed on the
    monotonic receive clock ``recv_mono``; ``ts`` stays wall clock)."""
    rc = recv_ts if recv_mono is None else recv_mono
    good = [r for r in recs if isinstance(r, (list, tuple)) and len(r) >= 7]
    frame_s: List[Optional[float]] = [None] * len(good)
    if clock is not None:
        idx = [i for i, r in enumerate(good) if isinstance(r[0], int)]
        try:
            tf = clock.observe(rc, [good[i][0] for i in idx])
            for i, x in zip(idx, tf):
                frame_s[i] = x
        except (TypeError, ValueError):
            pass
    out = []
    for r, fs in zip(good, frame_s):
        f = frame_from_wire(r, line_t, recv_ts, frame_s=fs, clock=clock, recv_mono=rc)
        if f is not None:
            out.append(f)
    return out


def _b(data: bytes, i: int) -> Optional[int]:
    return data[i] if len(data) > i else None


def _w(data: bytes, i: int) -> Optional[int]:
    return (data[i] << 8) | data[i + 1] if len(data) > i + 1 else None


#: FE:FB climate button bits (= the AVC-LAN climate button frame the gateway
#: relays; comm.avc.decoder.CLIMATE_BUTTONS has the same layout).
def _climate_buttons(data: bytes) -> tuple:
    from .avc.decoder import CLIMATE_BUTTONS
    b6 = tuple(data[:6]) + (0,) * (6 - min(6, len(data)))
    return tuple(name for name, code in CLIMATE_BUTTONS.items()
                 if any(code[i] and (b6[i] & code[i]) == code[i] for i in range(6)))


#: FE:FC byte 0 / 1 / 5 = AVC ``E0 5D F3`` b1 / b2 / b6 (the gateway copies
#: FE:FC verbatim onto AVC-LAN), decoded with the same rules.
_VENT = {0x80: "face_foot", 0x40: "foot", 0x20: "windscreen_foot"}

#: Coolant floor of FE:2C: the gateway clamps the value at raw 0x20 = 16 °C
#: (0x039 read 6-15 °C while FE:2C held 0x20). The floor is "<= 16 °C".
COOLANT_FLOOR_RAW = 0x20


def decode_known(mes: str, data_hex: str, dst: Optional[str] = None) -> Dict[str, Any]:
    """Named values decoded from a BEAN frame's MES id + data (and DST, which
    selects the directed frames; ``dst`` None/"" accepts any DST).

    CONFIRMED on the owner's NHW20 (docs/BEAN_SIGNALS.md §4):
      FE:B6  door lock state (b0) + door-open courtesy switches (b1)
      FE:E1  lock/unlock EVENT (b2), by source: 04/08 remote fob, 10/20 smart
      FE:7F  lamps (b0: 10 tail, 20 low beam, 01 front fog), b1 08
             illumination, b2 80 a door open
      FE:83  ACC (b0 20) / IG (b0 40); dome lamp (b3 08)
      FE:D8  driver-door smart-key check window (b1 bit0, ~30 s)
      ..:3F  odometer, 24-bit km (52:3F on this car)
      FE:29  b2: 08 brake pedal, bits 0-2 MFD SOC bars
      98:27  b0 08 = ICE stopped while READY
      52:ED  compressor target rpm (b3-4), b0 40 compressor running, b0 20
             A/C switch
      98:EC  actual compressor rpm (b0-1)
      52:76  trip distance, 0.1 km (TRUE distance, not the odometer's)
      52:90  instantaneous consumption 0.1 L/100 km over TRUE speed
             (FFFE = stopped with the ICE running, 0000 = no fuel: ICE off/EV
             or fuel cut)
      52:A2  average consumption 0.1 L/100 km since the meter's trip reset
             (frozen while parked)
      62:20  fuel quantity per injection (word b0-1, = CAN 0x520 b1-2; sent
             only while the ICE runs)
      FE:24  DISPLAYED speed (km/h, the speedometer value, ~12 % over 0x0B4)
      FE:26  engine rpm = word / 5.12
      FE:2C  coolant x/2 °C, clamped at 16 °C (raw 0x20 = "<= 16 °C")
      FE:CD  outside temperature x - 48 (offset confirmed, slope untested)
      FE:FB  climate button frame; FE:FC climate status; 52:FD target temp
      62:6E  key-not-in-vehicle warning (b0 80) / beep (b1 80)
      FE:40  shift (80 P, 40 R, 10 D, 00 in transit)

    PROBABLE (``prov_`` keys; display only, nothing may depend on them):
      52:ED b1 80 A/C request for ICE run (cabin heat); 52:6D b0 cabin
      temperature raw; FE:29 b2 80 READY;
      FE:50 P-lock bits; 98:EC b5 HV bus voltage (V ~ 1.9 x + 102);
      98:27 b0 01 + b1 FF READY.

    Placeholder (no meaning yet; recorded raw so a cruise-on drive yields
    data): 62:C8 cruise -> ``cruise_raw``.

    NB: FE:E1 is a STATUS REPORT the body ECU broadcasts after it acts, not a
    command — see docs/BEAN_SIGNALS.md §7.
    """
    try:
        data = bytes.fromhex(data_hex or "")
    except ValueError:
        return {}
    mes = (mes or "").upper()
    dst = (dst or "").upper()
    out: Dict[str, Any] = {}
    d0, d1, d2, d3 = _b(data, 0), _b(data, 1), _b(data, 2), _b(data, 3)

    def is_dst(*ids: str) -> bool:
        return dst == "" or dst in ids

    # Odometer rides a directed frame on this car (52:3F); accept any DST.
    if mes == "3F":
        if len(data) >= 3:
            out["odometer_km"] = (data[0] << 16) | (data[1] << 8) | data[2]
        return out
    if d0 is None:
        return out

    # ── directed frames (DST = a node address) ──────────────────────────────
    if mes == "ED" and is_dst("52") and len(data) >= 5:      # A/C amp -> HV ECU
        out["compressor_target_rpm"] = _w(data, 3)             # [C]
        out["compressor_running"] = bool(d0 & 0x40)            # [C]
        out["ac_switch_on"] = bool(d0 & 0x20)                  # [C]
        out["prov_ac_engine_heat_request"] = bool(d1 & 0x80)   # [P]
        return out
    if mes == "EC" and is_dst("98") and len(data) >= 6:      # gateway -> A/C amp
        out["compressor_rpm"] = _w(data, 0)                    # [C]
        out["prov_hv_bus_voltage_v"] = round(1.9 * data[5] + 102, 1)   # [P]
        out["prov_hv_bus_voltage_raw"] = data[5]
        return out
    if mes == "27" and is_dst("98") and len(data) >= 2:
        out["ice_stopped"] = bool(d0 & 0x08)                   # [C] while READY
        out["prov_hv_ready_9827"] = bool(d0 & 0x01) and d1 == 0xFF     # [P]
        return out
    if mes == "76" and is_dst("52") and len(data) >= 2:       # meter trip distance
        out["trip_distance_km"] = round(_w(data, 0) * 0.1, 1)  # [C] 0.1 km
        return out
    if mes == "90" and is_dst("52") and len(data) >= 2:       # instantaneous L/100 km
        w = _w(data, 0)
        if w == 0xFFFE:                                        # [C] stopped, ICE on
            out["consumption_l100km"] = None
            out["consumption_stopped"] = True
        else:
            out["consumption_l100km"] = round(w * 0.1, 1)
            out["consumption_stopped"] = False
        return out
    if mes == "A2" and is_dst("52") and len(data) >= 2:       # [C] average L/100 km
        out["avg_consumption_l100km"] = round(_w(data, 0) * 0.1, 1)
        return out
    if mes == "20" and is_dst("62") and len(data) >= 2:       # [C] fuel per injection
        out["injection_word"] = _w(data, 0)
        return out
    if mes == "6D" and is_dst("52"):                          # [P] cabin temp raw
        out["prov_cabin_temp_raw"] = d0
        return out
    if mes == "FD" and is_dst("52"):                          # [C] target temp
        out["climate_target_c"] = None if d0 == 0x39 else 15.5 + 0.5 * d0
        out["climate_target_raw"] = d0
        return out
    if mes == "6E" and is_dst("62") and len(data) >= 2:       # [C] key-out warning
        out["key_out_warning"] = bool(d0 & 0x80)
        out["key_out_beep"] = bool(d1 & 0x80)
        return out
    if mes == "C8" and is_dst("62"):                          # placeholder: cruise
        out["cruise_raw"] = data.hex().upper()
        return out

    # The rest are body-bus broadcasts (DST 0xFE). Guard on DST so a directed
    # frame that happens to reuse a MES byte is not mis-decoded.
    if dst not in ("", "FE"):
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
    elif mes == "7F":                       # [C] lamps (b0), illumination (b1), door (b2)
        out["lamp_tail"] = bool(d0 & 0x10)
        out["lamp_head"] = bool(d0 & 0x20)          # low beam
        out["lamp_fog_front"] = bool(d0 & 0x01)
        out["lamp_high_beam"] = bool(d0 & 0x08)     # [unconfirmed: never seen set]
        if d1 is not None:
            out["illumination"] = bool(d1 & 0x08)
        if d2 is not None:
            out["door_open"] = bool(d2 & 0x80)
    elif mes == "83":                       # [C] ACC/IG (b0), dome/luggage lamp (b3)
        if d3 is not None:
            out["dome_lamp"] = bool(d3 & 0x08)
            out["luggage_lamp"] = bool(d3 & 0x04)
        out["key_inserted"] = bool(d0 & 0x02)       # never set on this smart-start car
        out["acc_on"] = bool(d0 & 0x20)
        out["ig_on"] = bool(d0 & 0x40)
    elif mes == "D8":                       # [C] driver-door key window (b1)
        if d1 is not None:
            out["driver_key_window"] = bool(d1 & 0x01)
    elif mes == "62":                       # [P] power-source state
        out["in_park"] = bool(d0 & 0x20)
        out["key_present"] = bool(d0 & 0x40)
        out["power_on"] = bool(d0 & 0x10)
        out["power_switch_pressed"] = bool(d0 & 0x08)
    elif mes == "29" and d2 is not None:    # [C] gateway copy of CAN 0x529
        out["brake_switch"] = bool(d2 & 0x08)
        out["soc_bars"] = d2 & 0x07
        out["prov_hv_ready"] = bool(d2 & 0x80)      # [P]
    elif mes == "2C":                       # [C] coolant x/2, floor 16 °C
        if d0 <= COOLANT_FLOOR_RAW:
            out["coolant_at_floor"] = True          # "<= 16 °C": not a measurement
        else:
            out["coolant_c"] = d0 / 2.0
            out["coolant_at_floor"] = False
    elif mes == "CD":                       # [C] outside temperature
        out["outside_temp_c"] = d0 - 48
    elif mes == "24":                       # [C] displayed (speedometer) speed
        out["display_speed_kmh"] = d0
    elif mes == "26" and len(data) >= 2:    # [C] engine rpm
        out["engine_rpm"] = round((data[0] * 256 + data[1]) / 5.12)
    elif mes == "40":                       # [C] shift position
        names = [n for bit, n in ((0x80, "P"), (0x40, "R"), (0x20, "N"), (0x10, "D")) if d0 & bit]
        out["shift"] = names[0] if len(names) == 1 else ("" if not names else "/".join(names))
        out["shift_raw"] = d0
    elif mes == "50":                       # [P] P-position ECU
        out["prov_p_locked"] = bool(d0 & 0x80)
        out["prov_out_of_p"] = bool(d0 & 0x08)
        out["prov_p_ecu_on"] = bool(d0 & 0x04)
        out["prov_power_off_handshake"] = bool(d0 & 0x02)
    elif mes == "FB":                       # [C] climate button frame (momentary)
        buttons = _climate_buttons(data)
        if buttons:
            out["climate_buttons"] = buttons
    elif mes == "FC" and len(data) >= 6:    # [C] climate status (= AVC E0 5D F3)
        b0, b1, b5 = data[0], data[1], data[5]
        fan = b5 >> 5
        out["climate_on"] = fan != 0
        out["climate_fan"] = fan
        out["climate_auto"] = bool(b0 & 0x80)
        out["climate_fresh_air"] = bool(b0 & 0x40)
        out["climate_recirc"] = bool(b0 & 0x20)
        out["climate_front_defrost"] = bool(b0 & 0x10)
        out["climate_rear_defogger"] = bool(b0 & 0x08)
        out["climate_ac"] = bool(b0 & 0x02)
        out["climate_vent"] = "face" if (b1 == 0 and b0 & 0x01) else _VENT.get(b1)
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
