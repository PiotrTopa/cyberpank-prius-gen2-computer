"""Time sync for the fvision / Buildwin USB DVR camera (1902:3241).

The vendor app sends ``CAM_SET_TIME`` (opcode 11) about 2 s after it connects.
A host that only reads never does, and the camera then leaves the USB bus once,
28 s after power-up, for about 5 s (measured 2026-11-17 on the car: 4 of 4
power-ups without the command dropped, 9 of 9 with it stayed connected;
roadsense docs/PHONE_DVR_DEPLOY.md "USB drop-outs after power-up").
``camerad-dvr`` sends it since roadsense a61e118; this module does the same for
the backend's own reader (dashboard preview with the stack down).

The command is one 512-byte sector written to the command sector of the file
``Android/data/com.fvision.camera/HWC`` on the camera's FAT volume. The sector
number is fixed per layout; before writing, :func:`hwc_covers` walks the FAT
and requires that sector to lie inside that file, so a card with another
layout is never written to.

Pure functions on a ``read(lba, nbytes) -> bytes`` callable (testable).
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

SECTOR = 512
OP_SET_TIME = 11
CMD_LBA = {0x380: 0x340, 0x300: 0x2C0}      # info LBA (layout A / B) -> command-write LBA
HWC_PATH = ("Android", "data", "com.fvision.camera", "HWC")
BOARD_V1 = 0xE9                              # info[0x34]: 16-bit command header

Read = Callable[[int, int], bytes]


def _u16(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 2], "little")


def _u32(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 4], "little")


def time_packet(board_v1: bool, t: Optional[time.struct_time] = None) -> bytes:
    """The 512-byte CAM_SET_TIME packet: header, then year LE16, month, day, h, m, s."""
    t = t or time.localtime()
    payload = bytes([t.tm_year & 0xFF, t.tm_year >> 8, t.tm_mon, t.tm_mday,
                     t.tm_hour, t.tm_min, t.tm_sec])
    head = bytes([OP_SET_TIME, 0, 7, 0]) if board_v1 else bytes([OP_SET_TIME, 7])
    return (head + payload).ljust(SECTOR, b"\0")


class _Fat:
    def __init__(self, read: Read) -> None:
        self.read = read
        mbr = read(0, SECTOR)
        if mbr[510:512] != b"\x55\xaa":
            raise ValueError("no MBR")
        self.part = _u32(mbr, 0x1C6)
        b = read(self.part, SECTOR)
        if b[510:512] != b"\x55\xaa":
            raise ValueError("no FAT boot sector")
        bps, self.spc, res, nf = _u16(b, 11), b[13], _u16(b, 14), b[16]
        rootent = _u16(b, 17)
        ts = _u16(b, 19) or _u32(b, 32)
        fsz = _u16(b, 22) or _u32(b, 36)
        if bps != SECTOR or self.spc == 0:
            raise ValueError("unexpected FAT geometry")
        self.fat_lba = self.part + res
        self.root_secs = (rootent * 32 + SECTOR - 1) // SECTOR
        self.root_lba = self.fat_lba + nf * fsz
        self.data_lba = self.root_lba + self.root_secs
        clusters = (ts - (self.data_lba - self.part)) // self.spc
        self.kind = 12 if clusters < 4085 else 16 if clusters < 65525 else 32
        self.root_clus = _u32(b, 44) if self.kind == 32 else 0

    def next(self, c: int) -> int:
        off = c * 2 if self.kind == 16 else c * 4 if self.kind == 32 else c + c // 2
        s = self.read(self.fat_lba + off // SECTOR, 2 * SECTOR)
        o = off % SECTOR
        if self.kind == 16:
            v = _u16(s, o)
            return -1 if v >= 0xFFF8 else v
        if self.kind == 32:
            v = _u32(s, o) & 0x0FFFFFFF
            return -1 if v >= 0x0FFFFFF8 else v
        v = _u16(s, o)
        v = v >> 4 if c & 1 else v & 0xFFF
        return -1 if v >= 0xFF8 else v

    def chain(self, c: int, limit: int = 4096) -> List[int]:
        out: List[int] = []
        while c >= 2 and len(out) < limit:
            out.append(c)
            c = self.next(c)
        return out

    def lba(self, c: int) -> int:
        return self.data_lba + (c - 2) * self.spc

    def read_dir(self, clus: int) -> bytes:
        if clus < 2:
            return self.read(self.root_lba, self.root_secs * SECTOR) if self.kind != 32 else b""
        return b"".join(self.read(self.lba(c), self.spc * SECTOR) for c in self.chain(clus, 64))

    def lookup(self, d: bytes, name: str) -> Tuple[int, bool]:
        """(first cluster, is_dir) of ``name`` (long or 8.3 name, case-insensitive); (-1, False) if absent."""
        want, lfn = name.lower(), ""
        for i in range(0, len(d) - 31, 32):
            e = d[i:i + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5:
                lfn = ""
                continue
            if e[11] == 0x0F:                 # long-name part, last part first
                part = ""
                for k in (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30):
                    ch = _u16(e, k)
                    if ch in (0, 0xFFFF):
                        break
                    part += chr(ch) if ch < 0x80 else "?"
                lfn = part + lfn
                continue
            if e[11] & 0x08:
                lfn = ""
                continue
            base = e[0:8].decode("latin-1").rstrip()
            ext = e[8:11].decode("latin-1").rstrip()
            short = base + ("." + ext if ext else "")
            hit = (lfn and lfn.lower() == want) or short.lower() == want
            lfn = ""
            if hit:
                hi = _u16(e, 20) << 16 if self.kind == 32 else 0
                return hi | _u16(e, 26), bool(e[11] & 0x10)
        return -1, False


def hwc_covers(read: Read, lba: int) -> bool:
    """True when the HWC command file exists on the volume and contains sector ``lba``."""
    try:
        f = _Fat(read)
        clus, is_dir = (f.root_clus if f.kind == 32 else 0), True
        for comp in HWC_PATH:
            if not is_dir:
                return False
            clus, is_dir = f.lookup(f.read_dir(clus), comp)
            if clus < 2:
                return False
        if is_dir:
            return False
        return any(f.lba(c) <= lba < f.lba(c) + f.spc for c in f.chain(clus))
    except (ValueError, OSError, IndexError):
        return False
