"""
DTC number encoding shared by every diagnostic service used on the NHW20.

A trouble code travels as two bytes (SAE J2012 / ISO 15031-6)::

    byte 0: bits 7-6 system  (00 P powertrain, 01 C chassis, 10 B body, 11 U network)
            bits 5-4 first digit (0-3)
            bits 3-0 second digit
    byte 1: bits 7-4 third digit, bits 3-0 fourth digit

so ``0x04 0x20`` is P0420, ``0x63 0x00`` is C2300, ``0xA2 0x78`` is B2278 and
``0xC1 0x00`` is U0100. Digits above 9 are printed as hex letters (P0A80 is
``0x0A 0x80``).
"""

from __future__ import annotations

from typing import List, Optional, Tuple

_SYSTEM = "PCBU"


def decode(byte_hi: int, byte_lo: int) -> Optional[str]:
    """Two bytes -> code string, ``None`` for the 0x0000 filler."""
    byte_hi &= 0xFF
    byte_lo &= 0xFF
    if byte_hi == 0 and byte_lo == 0:
        return None
    return "%s%d%X%02X" % (_SYSTEM[byte_hi >> 6], (byte_hi >> 4) & 0x3, byte_hi & 0x0F, byte_lo)


def encode(code: str) -> Tuple[int, int]:
    """Code string -> two bytes. Raises ``ValueError`` on a malformed code."""
    code = code.strip().upper()
    if len(code) != 5 or code[0] not in _SYSTEM:
        raise ValueError("not a DTC: %r" % (code,))
    try:
        first = int(code[1], 16)
        rest = int(code[2:], 16)
    except ValueError:
        raise ValueError("not a DTC: %r" % (code,))
    if first > 3:
        raise ValueError("not a DTC: %r" % (code,))
    hi = (_SYSTEM.index(code[0]) << 6) | (first << 4) | (rest >> 8)
    return hi, rest & 0xFF


def is_code(text: object) -> bool:
    try:
        encode(str(text))
        return True
    except ValueError:
        return False


def decode_list(data: bytes, width: int = 2) -> List[Tuple[str, bytes]]:
    """Split ``data`` into ``width``-byte records and decode each record's first
    two bytes. Returns ``[(code, extra_bytes)]``; filler records (0x0000) are
    dropped, a trailing partial record is ignored."""
    out: List[Tuple[str, bytes]] = []
    data = bytes(data)
    for i in range(0, len(data) - width + 1, width):
        code = decode(data[i], data[i + 1])
        if code is not None:
            out.append((code, data[i + 2:i + width]))
    return out
