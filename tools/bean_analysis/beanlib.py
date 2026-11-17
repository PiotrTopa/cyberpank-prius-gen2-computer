"""Shared loaders and helpers for BEAN capture analysis.

Inputs supported:
  * our captures: gzipped (or plain) NDJSON, format "bean-capture/1"
    (types header/mark/stats/frame; frame = {ts, t_ms, pri, dst, mes, data, rsp, ok})
  * the bean_tools NHW20 fixture: tests/fixtures/bean_nhw20_frames.txt.gz
    (whitespace columns: file t_ms pri ml dst mes data crc ack)

Times: frame time is reconstructed from the firmware millisecond counter t_ms
plus one constant offset (the minimum of ts - t_ms/1000 over the capture), because
the host-side ts is batched (several frames share a ts) while t_ms is exact.
"""
from __future__ import annotations

import datetime as _dt
import gzip
import json
from dataclasses import dataclass, field
from typing import Iterable

CET = _dt.timezone(_dt.timedelta(hours=1))


@dataclass
class Frame:
    t: float            # seconds (wall clock, unix) for our captures; seconds from file start for fixture
    pri: int
    dst: str
    mes: str
    data: bytes
    rsp: int            # our captures: 0..3 raw RSP bits; fixture: ack 0/1
    ok: bool = True
    src: str = ""       # file / capture name

    @property
    def key(self) -> str:
        return f"{self.dst}:{self.mes}"

    @property
    def hex(self) -> str:
        return self.data.hex().upper()


@dataclass
class Capture:
    name: str
    frames: list[Frame]
    marks: list[tuple[float, str]] = field(default_factory=list)
    stats: list[dict] = field(default_factory=list)


def _open(path: str):
    return (gzip.open(path, "rt", errors="replace") if path.endswith(".gz")
            else open(path, "rt", errors="replace"))


def _rows(path: str):
    """Records of a capture; NUL-filled tails (hard reset) and torn or
    garbage lines are skipped."""
    for l in _open(path):
        l = l.replace("\0", "").strip()
        if not l.startswith("{"):
            continue
        try:
            yield json.loads(l)
        except ValueError:
            continue


def load_ndjson(path: str) -> Capture:
    rows = list(_rows(path))
    fr = [r for r in rows if r.get("type") == "frame"]
    off = min(r["ts"] - r["t_ms"] / 1000.0 for r in fr) if fr else 0.0
    frames = [Frame(off + r["t_ms"] / 1000.0, int(r["pri"]), r["dst"].upper(), r["mes"].upper(),
                    bytes.fromhex(r["data"]), int(r["rsp"]), bool(r["ok"]), path) for r in fr]
    frames.sort(key=lambda f: f.t)
    marks = [(r["ts"], r.get("text", "")) for r in rows if r.get("type") == "mark"]
    stats = [r for r in rows if r.get("type") == "stats"]
    return Capture(path, frames, marks, stats)


def load_fixture(path: str) -> dict[str, Capture]:
    """bean_tools NHW20 fixture -> {dat file name: Capture} (t in s from first frame)."""
    caps: dict[str, list[Frame]] = {}
    for line in _open(path):
        if line.startswith("#") or not line.strip():
            continue
        fn, t_ms, pri, ml, dst, mes, data, crc, ack = line.split()
        caps.setdefault(fn, []).append(Frame(int(t_ms) / 1000.0, int(pri), dst.upper(), mes.upper(),
                                             bytes.fromhex(data), int(ack), True, fn))
    out = {}
    for fn, fr in caps.items():
        t0 = fr[0].t
        for f in fr:
            f.t -= t0
        out[fn] = Capture(fn, fr)
    return out


def load_any(path: str) -> list[Capture]:
    if path.endswith(".txt.gz") or path.endswith(".txt"):
        return list(load_fixture(path).values())
    return [load_ndjson(path)]


def owner_clock(t: float, hour: int = 16, date=(2026, 11, 13)) -> str:
    """Unix time -> owner's notation mm:ss(.s) after hh:00 CET (negative = before)."""
    base = _dt.datetime(*date, hour, 0, tzinfo=CET).timestamp()
    d = t - base
    sign = "-" if d < 0 else ""
    d = abs(d)
    return f"{sign}{int(d // 60):02d}:{d % 60:04.1f}"


def parse_owner_clock(s: str, hour: int = 16, date=(2026, 11, 13)) -> float:
    base = _dt.datetime(*date, hour, 0, tzinfo=CET).timestamp()
    m, sec = s.split(":")
    return base + int(m) * 60 + float(sec)


def wake_segments(frames: list[Frame], gap: float = 2.0) -> list[list[Frame]]:
    """Split a frame list into bus-awake periods separated by > gap seconds of silence."""
    segs: list[list[Frame]] = []
    for f in frames:
        if not segs or f.t - segs[-1][-1].t > gap:
            segs.append([])
        segs[-1].append(f)
    return segs


def group(frames: Iterable[Frame]) -> dict[str, list[Frame]]:
    g: dict[str, list[Frame]] = {}
    for f in frames:
        g.setdefault(f.key, []).append(f)
    return g


def node_key(f: Frame) -> str:
    """FE:AB / FE:C2 are per-node frames: split them by the first data byte."""
    if f.key in ("FE:AB", "FE:C2") and f.data:
        return f"{f.key}[{f.data[0]:02X}]"
    return f.key


def ascii_or_dot(b: bytes) -> str:
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b)
