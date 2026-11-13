"""
ISO 15765-2 (ISO-TP) framing for the diagnostics engine — pure functions and a
receive-side reassembler, no I/O.

Two addressing formats are used on the NHW20 diagnostic ids:

* **normal** addressing (0x7E0/0x7E8, 0x7B0/0x7B8, ...): the PCI is byte 0.
* **extended** addressing (Toyota 0x750 / 0x758, "sub-address"): byte 0 is
  the ECU's sub-address (N_TA), the PCI starts at byte 1. A single frame then
  carries at most 6 payload bytes, a first frame 5, a consecutive frame 6.

Only single-frame *requests* are produced: every diagnostic request the engine
sends is 1..3 bytes long. Responses may be multi-frame.

Frame types (high nibble of the PCI byte)::

    0x0N            single frame, N payload bytes
    0x1L LL         first frame, 12-bit total length LLL
    0x2S            consecutive frame, sequence number S (1..15, 0, 1, ...)
    0x30 BS STmin   flow control (0x30 continue, 0x31 wait, 0x32 overflow)
"""

from __future__ import annotations

from typing import Optional

PCI_SF = 0x0
PCI_FF = 0x1
PCI_CF = 0x2
PCI_FC = 0x3

CAN_DL = 8


class IsoTpError(ValueError):
    """A frame or payload cannot be encoded."""


def max_single_frame(sub_addr: Optional[int] = None) -> int:
    """Payload bytes that fit one single frame."""
    return 6 if sub_addr is not None else 7


def encode_single(payload: bytes, sub_addr: Optional[int] = None, pad: int = 0x00) -> bytes:
    """Build the 8-byte single frame for ``payload``.

    Raises :class:`IsoTpError` when the payload does not fit one frame (the
    engine never sends multi-frame requests).
    """
    payload = bytes(payload)
    limit = max_single_frame(sub_addr)
    if not 1 <= len(payload) <= limit:
        raise IsoTpError("request of %d bytes does not fit a single frame (max %d)"
                         % (len(payload), limit))
    head = bytes([sub_addr & 0xFF]) if sub_addr is not None else b""
    frame = head + bytes([len(payload)]) + payload
    return frame + bytes([pad & 0xFF]) * (CAN_DL - len(frame))


def flow_control(sub_addr: Optional[int] = None, block_size: int = 0, st_min: int = 0,
                 pad: int = 0x00) -> bytes:
    """Build a flow-control "continue to send" frame."""
    head = bytes([sub_addr & 0xFF]) if sub_addr is not None else b""
    frame = head + bytes([0x30, block_size & 0xFF, st_min & 0xFF])
    return frame + bytes([pad & 0xFF]) * (CAN_DL - len(frame))


class Reassembler:
    """Receive side of one ISO-TP message.

    Feed raw CAN payloads (up to 8 bytes) with :meth:`feed`; the return value
    names what the frame was:

    ``"single"``       complete single-frame message (``payload`` is set)
    ``"first"``        first frame accepted — the caller must send flow control
    ``"consecutive"``  consecutive frame accepted, more expected
    ``"done"``         last consecutive frame — message complete
    ``"duplicate"``    repeat of the frame just consumed (two listeners on the
                       bus deliver every frame twice) — ignored
    ``"ignored"``      not for this message (wrong sub-address, flow control
                       from someone else, garbage, frame after completion)
    ``"error"``        sequence error; ``error`` says why, the message is dead

    ``rx_addr`` is the first byte expected on extended-addressed responses;
    ``None`` with ``extended=True`` accepts any first byte and records it.
    """

    def __init__(self, extended: bool = False, rx_addr: Optional[int] = None,
                 max_len: int = 4095) -> None:
        self.extended = extended
        self.rx_addr = rx_addr
        self.max_len = max_len
        self.reset()

    def reset(self) -> None:
        self.payload: Optional[bytes] = None
        self.error: Optional[str] = None
        self.total = 0
        self._buf = bytearray()
        self._next_seq = 0
        self._last_frame: Optional[bytes] = None
        self.frames = 0
        self.seen_addr: Optional[int] = None

    # ── state ────────────────────────────────────────────────────────────

    @property
    def complete(self) -> bool:
        return self.payload is not None

    @property
    def in_progress(self) -> bool:
        return self.total > 0 and self.payload is None and self.error is None

    @property
    def partial(self) -> bytes:
        """Bytes received so far of an unfinished multi-frame message."""
        return bytes(self._buf)

    @property
    def remaining(self) -> int:
        return max(0, self.total - len(self._buf)) if self.total else 0

    # ── feed ─────────────────────────────────────────────────────────────

    def feed(self, frame: bytes) -> str:
        frame = bytes(frame)
        off = 1 if self.extended else 0
        if len(frame) < off + 1:
            return "ignored"
        if self.extended:
            if self.rx_addr is not None and frame[0] != (self.rx_addr & 0xFF):
                return "ignored"
        if self.error is not None:
            return "ignored"
        if self._last_frame is not None and frame == self._last_frame:
            return "duplicate"
        if self.complete:
            return "ignored"

        pci = frame[off]
        kind = pci >> 4
        body = frame[off + 1:]

        if kind == PCI_SF:
            n = pci & 0x0F
            if self.in_progress or n == 0 or n > len(body):
                return "ignored"
            self.payload = bytes(body[:n])
            self.total = n
            return self._accept(frame, "single")

        if kind == PCI_FF:
            if len(body) < 1:
                return "ignored"
            total = ((pci & 0x0F) << 8) | body[0]
            if self.in_progress:
                # A second first frame mid-message: the ECU restarted. Only a
                # different one is an error; the identical one was caught above.
                self.error = "unexpected first frame"
                return "error"
            if total <= max_single_frame(0 if self.extended else None) or total > self.max_len:
                return "ignored"
            self.total = total
            self._buf = bytearray(body[1:][:total])
            self._next_seq = 1
            return self._accept(frame, "first")

        if kind == PCI_CF:
            if not self.in_progress:
                return "ignored"
            seq = pci & 0x0F
            if seq != self._next_seq:
                if seq == ((self._next_seq - 1) & 0x0F):
                    return "duplicate"
                self.error = "sequence error: got %d, expected %d" % (seq, self._next_seq)
                return "error"
            need = self.total - len(self._buf)
            self._buf += body[:need]
            self._next_seq = (self._next_seq + 1) & 0x0F
            if len(self._buf) >= self.total:
                self.payload = bytes(self._buf)
                return self._accept(frame, "done")
            return self._accept(frame, "consecutive")

        # Flow control or reserved PCI: nothing a response reassembler wants.
        return "ignored"

    def _accept(self, frame: bytes, what: str) -> str:
        self._last_frame = frame
        self.frames += 1
        if self.extended:
            self.seen_addr = frame[0]
        return what
