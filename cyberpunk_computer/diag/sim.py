"""
ECU and gateway simulator for the diagnostics engine — tests and dry runs
(``scripts/diag_cli.py --sim``). Nothing here touches hardware.

:class:`SimGateway` behaves like gateway firmware 3.2.9's ``req`` action
(gateway-rp2350/src/can_sol.c): one job at a time, a request frame padded to 8
bytes, the reply line ``{"a":"resp","i":"0x7E8","d":[...]}`` or
``{"a":"resp","err":"TIMEOUT","i":...,"req":...}``. With ``"isotp": true`` it
reassembles a normal-addressed multi-frame response itself; without it the
first matching frame is returned raw and the job ends, exactly as the firmware
does.

:class:`SimEcu` is one ECU: a table ``request bytes -> response bytes`` (or a
callable), ISO-TP segmentation with normal or extended addressing, and a
flow-control model that honours BlockSize or ignores it (``honour_bs=False``).
"""

from __future__ import annotations

import collections
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple, Union

from . import dtc as dtcmod

Responder = Union[bytes, Callable[[bytes], Optional[bytes]]]


def segment(payload: bytes, sub_addr: Optional[int] = None, pad: int = 0x00) -> List[bytes]:
    """ISO-TP frames (8 bytes each) carrying ``payload``."""
    head = bytes([sub_addr]) if sub_addr is not None else b""
    room = 8 - len(head)
    frames: List[bytes] = []
    if len(payload) <= room - 1:
        frames.append(head + bytes([len(payload)]) + payload)
    else:
        n = len(payload)
        first = room - 2
        frames.append(head + bytes([0x10 | (n >> 8), n & 0xFF]) + payload[:first])
        seq, pos = 1, first
        while pos < n:
            frames.append(head + bytes([0x20 | seq]) + payload[pos:pos + room - 1])
            pos += room - 1
            seq = (seq + 1) & 0x0F
    return [f + bytes([pad]) * (8 - len(f)) for f in frames]


class SimEcu:
    """One simulated ECU."""

    def __init__(self, tx_id: int, rx_id: int, sub_addr: Optional[int] = None,
                 responses: Optional[Dict[bytes, Responder]] = None, honour_bs: bool = True,
                 negative_default: bool = True, alive: bool = True) -> None:
        self.tx_id, self.rx_id, self.sub_addr = tx_id, rx_id, sub_addr
        self.responses: Dict[bytes, Responder] = dict(responses or {})
        self.honour_bs = honour_bs
        self.negative_default = negative_default
        self.alive = alive
        self.requests: List[bytes] = []
        self._pending: List[bytes] = []          # consecutive frames not sent yet

    # request level ------------------------------------------------------

    def respond(self, request: bytes) -> Optional[bytes]:
        self.requests.append(bytes(request))
        if not self.alive:
            return None
        r = self.responses.get(bytes(request))
        if callable(r):
            r = r(bytes(request))
        if r is None and self.negative_default:
            return bytes([0x7F, request[0], 0x11])
        return r

    # frame level --------------------------------------------------------

    def on_frame(self, frame: bytes) -> List[bytes]:
        """A frame addressed to ``tx_id`` arrives; returns the frames sent back."""
        off = 0
        if self.sub_addr is not None:
            if frame[0] != self.sub_addr:
                return []
            off = 1
        pci = frame[off]
        kind = pci >> 4
        if kind == 0:
            n = pci & 0x0F
            if n == 0 or n > 7 - off:
                return []
            resp = self.respond(frame[off + 1:off + 1 + n])
            if resp is None:
                return []
            frames = segment(resp, self.sub_addr)
            self._pending = frames[1:]
            return frames[:1]
        if kind == 3 and self._pending:
            if (pci & 0x0F) != 0:
                self._pending = []
                return []
            bs = frame[off + 1]
            if bs == 0 or not self.honour_bs:
                out, self._pending = self._pending, []
            else:
                out, self._pending = self._pending[:bs], self._pending[bs:]
            return out
        return []


class SimGateway:
    """Gateway ``req`` engine plus the bus the simulated ECUs sit on."""

    def __init__(self, deliver: Callable[[Dict[str, Any]], None],
                 bus_tap: Optional[Callable[[int, bytes], None]] = None,
                 connected: bool = True) -> None:
        self.deliver = deliver
        self.bus_tap = bus_tap
        self.connected = connected
        self.ecus: List[SimEcu] = []
        self.queue: Deque[Dict[str, Any]] = collections.deque()
        self.sent: List[Dict[str, Any]] = []        # every req payload accepted
        self.frames_tx = 0
        self.drop_replies = False                   # simulate a dead gateway

    def add(self, ecu: SimEcu) -> SimEcu:
        self.ecus.append(ecu)
        return ecu

    # host side ----------------------------------------------------------

    def send(self, payload: Dict[str, Any]) -> bool:
        if not self.connected:
            return False
        self.sent.append(dict(payload))
        self.queue.append(dict(payload))
        return True

    def step(self) -> bool:
        """Run one queued job to completion. Returns False when idle."""
        if not self.queue:
            return False
        job = self.queue.popleft()
        self._run(job)
        return True

    def run_all(self) -> None:
        while self.step():
            pass

    # firmware model -----------------------------------------------------

    def _bus(self, can_id: int, frame: bytes) -> List[Tuple[int, bytes]]:
        """Put one frame on the bus; returns the frames ECUs answer with."""
        if self.bus_tap is not None:
            self.bus_tap(can_id, frame)
        out: List[Tuple[int, bytes]] = []
        for ecu in self.ecus:
            if ecu.tx_id == can_id:
                for f in ecu.on_frame(frame):
                    out.append((ecu.rx_id, f))
        if self.bus_tap is not None:
            for rid, f in out:
                self.bus_tap(rid, f)
        return out

    def _reply(self, data: Dict[str, Any]) -> None:
        if not self.drop_replies:
            self.deliver(data)

    def _run(self, job: Dict[str, Any]) -> None:
        tx = int(job["i"], 16) if isinstance(job["i"], str) else int(job["i"])
        frame = bytes(job["d"]) + bytes(8 - len(job["d"]))
        expect = [int(r, 16) if isinstance(r, str) else int(r) for r in job.get("r", [])] or \
            list(range(0x7E8, 0x7F0))
        use_isotp = bool(job.get("isotp")) or int(job.get("t", 100)) >= 300
        self.frames_tx += 1
        rx = [(i, f) for i, f in self._bus(tx, frame) if i in expect]

        def timeout() -> None:
            self._reply({"a": "resp", "err": "TIMEOUT", "i": "0x%X" % expect[0], "req": "0x%X" % tx})

        if not rx:
            timeout()
            return
        rid, first = rx[0]
        if not use_isotp:
            self._reply({"a": "resp", "i": "0x%X" % rid, "d": list(first)})
            return
        pci = first[0] >> 4
        if pci == 0:
            n = first[0] & 0x0F
            if n == 0 or n > 7:
                timeout()
                return
            self._reply({"a": "resp", "i": "0x%X" % rid, "d": list(first[1:1 + n])})
        elif pci == 1:
            total = ((first[0] & 0x0F) << 8) | first[1]
            buf = bytearray(first[2:8])
            self.frames_tx += 1
            fc = bytes([0x30, 0x00, 0x05, 0, 0, 0, 0, 0])
            for i, f in self._bus(tx, fc):
                if i == rid and (f[0] >> 4) == 2:
                    buf += f[1:8]
            if len(buf) < total:
                timeout()
                return
            self._reply({"a": "resp", "i": "0x%X" % rid, "d": list(buf[:total])})
        else:
            timeout()


# ── a simulated NHW20 ────────────────────────────────────────────────────────

class SimObdEcu(SimEcu):
    """OBD ECU with a code memory: modes 01 (PIDs 01, 0D), 02, 03, 07, 09, 04,
    optionally Toyota 0x13."""

    def __init__(self, tx_id: int, rx_id: int, stored: Optional[List[str]] = None,
                 pending: Optional[List[str]] = None, toyota: Optional[List[str]] = None,
                 vin: Optional[str] = None, speed: int = 0, refuse_clear: bool = False,
                 toyota_count_byte: bool = True, **kw: Any) -> None:
        super().__init__(tx_id, rx_id, **kw)
        self.stored = list(stored or [])
        self.pending = list(pending or [])
        self.toyota = toyota                      # None: service 0x13 not supported
        self.toyota_count_byte = toyota_count_byte
        self.vin = vin
        self.speed = speed
        self.refuse_clear = refuse_clear
        self.clears = 0
        self.freeze: Dict[int, bytes] = {0x04: b"\x80", 0x05: b"\x7D", 0x06: b"\x80", 0x07: b"\x8A",
                                         0x0C: b"\x12\xC0", 0x0D: b"\x32"}

    @staticmethod
    def _pairs(codes: List[str]) -> bytes:
        out = bytearray()
        for c in codes:
            out += bytes(dtcmod.encode(c))
        return bytes(out)

    def respond(self, request: bytes) -> Optional[bytes]:
        self.requests.append(bytes(request))
        if not self.alive:
            return None
        sid = request[0]
        if bytes(request) in self.responses:
            r = self.responses[bytes(request)]
            return r(bytes(request)) if callable(r) else r
        if request == b"\x01\x01":
            return bytes([0x41, 0x01, (0x80 if self.stored else 0) | len(self.stored), 0x07, 0x65, 0x00])
        if request == b"\x01\x0D":
            return bytes([0x41, 0x0D, self.speed])
        if sid == 0x03 and len(request) == 1:
            return bytes([0x43, len(self.stored)]) + self._pairs(self.stored)
        if sid == 0x07 and len(request) == 1:
            return bytes([0x47, len(self.pending)]) + self._pairs(self.pending)
        if sid == 0x02 and len(request) == 3:
            pid = request[1]
            if pid == 0x02:
                code = self.stored[0] if self.stored else None
                return bytes([0x42, 0x02, 0x00]) + (bytes(dtcmod.encode(code)) if code else b"\x00\x00")
            if pid == 0x00 and self.stored:
                mask = 0
                for q in list(self.freeze) + [0x02]:
                    mask |= 1 << (32 - q)
                return bytes([0x42, 0x00, 0x00]) + mask.to_bytes(4, "big")
            if pid in self.freeze and self.stored:
                return bytes([0x42, pid, 0x00]) + self.freeze[pid]
            return None                           # OBD: unsupported PID -> no answer
        if request == b"\x09\x02" and self.vin:
            return b"\x49\x02\x01" + self.vin.encode("ascii")
        if sid == 0x04 and len(request) == 1:
            if self.refuse_clear:
                return b"\x7F\x04\x22"
            self.clears += 1
            self.stored, self.pending = [], []
            if self.toyota is not None:
                self.toyota = []
            return b"\x44"
        if sid == 0x13 and self.toyota is not None:
            head = bytes([0x53, len(self.toyota)]) if self.toyota_count_byte else b"\x53"
            return head + self._pairs(self.toyota)
        if sid in (0x01, 0x02, 0x09):
            return None
        return bytes([0x7F, sid, 0x11]) if self.negative_default else None


def make_prius(deliver: Callable[[Dict[str, Any]], None],
               bus_tap: Optional[Callable[[int, bytes], None]] = None,
               scenario: str = "buttons_unplugged") -> SimGateway:
    """A simulated NHW20 behind a simulated gateway.

    ``buttons_unplugged`` models the working hypothesis for the car: mode 03
    lists nothing on the HV control ECU, the Toyota read (0x13) lists the
    master-warning-only codes P0852 and P3102; the ECM holds a confirmed P0420.
    ``clean`` has no codes. Only the three CAN ECUs exist; probe addresses stay
    silent, as the research predicts.
    """
    gw = SimGateway(deliver, bus_tap)
    vin = "JTDKB20U040000000"
    if scenario == "clean":
        gw.add(SimObdEcu(0x7E0, 0x7E8, vin=vin, toyota=[]))
        gw.add(SimObdEcu(0x7E2, 0x7EA, toyota=[]))
        gw.add(SimObdEcu(0x7E3, 0x7EB, toyota=[]))
    else:
        gw.add(SimObdEcu(0x7E0, 0x7E8, stored=["P0420"], vin=vin, toyota=["P0420"]))
        gw.add(SimObdEcu(0x7E2, 0x7EA, toyota=["P0852", "P3102"]))
        gw.add(SimObdEcu(0x7E3, 0x7EB, toyota=[]))
    return gw
