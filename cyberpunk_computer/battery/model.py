"""Constants, thresholds and the compact column codec of the battery module."""

from __future__ import annotations

import json
import struct
import sys
import zlib
from array import array
from typing import Dict, List, Optional, Sequence

SCHEMA_VERSION = 1      # database layout and raw-record layout
ANALYSIS_VERSION = 1    # feature definitions; a bump re-analyses from the raw records

N_BLOCKS = 14           # 28 modules read in pairs (168 cells, 12 cells per block)
NOMINAL_AH = 6.5        # NHW20 pack nominal capacity
NONE16 = -32768         # "no value" in an int16 column

# ── ride segmentation ───────────────────────────────────────────────────────
RIDE_GAP_S = 20.0           # HV frames silent this long -> the ride is over
RIDE_RESUME_S = 180.0       # an open ride found at start-up is continued if younger
SPOOL_INTERVAL_S = 60.0     # open records are written to the database this often
SLOW_PERIOD_S = 10.0        # slow series sampling
WAKE_PRE_S = 30.0           # 12 V samples kept before a ride starts
WAKE_POST_S = 90.0          # ... and after

# ── regimes of the block series (battery current, A; > 0 = discharge) ───────
I_REST = 3.0                # |I| <= this: "rest" sample
I_LOAD = 15.0               # I >= this: discharge sample; I <= -this: charge sample
STEP_MIN_DI = 8.0           # block-resistance step: |dI| between consecutive 21CE samples
STEP_MAX_DT = 3.5           # ... no further apart than this (s)
FAST_LAGS_S = (0.25, 1.0)   # pack-resistance pair lags on the 0x03B stream (s)
FAST_MIN_DI = 15.0          # ... minimum |dI| over the lag
I_REF = 50.0                # standard load for "deviation at load" figures (A)
OCV_MAX_I = 5.0             # key-on / key-off snapshot: |I| below this
OCV_WINDOW_S = 20.0         # ... within this of the ride start / end

# current histogram bin edges (A); bins are [-inf,e0), [e0,e1) ... [e_last, inf)
HIST_EDGES = (-80.0, -40.0, -20.0, -5.0, 5.0, 20.0, 40.0, 80.0)

# SOC window: the ECU steers to ~60 % and holds 40..80 % (displayed bars).
SOC_LOW = 42.0
SOC_HIGH = 78.0
SOC_JUMP_PCT = 1.5          # SOC change not explained by ampere-hours (within 20 s)

# ── normalisation priors (own estimates until learnt from the pack) ─────────
# R(T) = R25 * exp(B * (1/T - 1/298.15)); B in kelvin. A doubling of the
# resistance between 25 degC and 0 degC corresponds to B ~ 2260 K.
R_ARRHENIUS_B_PRIOR = 2260.0
T_REF_C = 25.0

# ── quality gates ───────────────────────────────────────────────────────────
MIN_RIDE_S = 60.0           # below: grade X (not analysed for health)
MIN_GOOD_S = 300.0          # below: grade C
MIN_STEPS_R = 15            # resistance metrics need this many current steps
MIN_BLK_FIT = 60            # block deviation fit needs this many 21CE samples
MIN_LOAD_SPAN_A = 20.0      # ... and this current span (p95 - p5)
COLD_C = 0.0                # below: resistance flagged "cold" (strongly non-linear)
MIN_REST_SD_H = 8.0         # self-discharge needs a rest at least this long

# ── 12 V battery ────────────────────────────────────────────────────────────
REST_MARKS_H = (0.5, 1.0, 2.0, 6.0, 12.0, 24.0, 48.0)
V12_LOW = 11.8              # low-voltage event threshold (V)
V12_CRIT = 11.5
LEAD_V_PER_SOC = 0.0115     # ~1.15 V between 0 % and 100 % (12 V lead-acid, rested)


def clamp16(x: float) -> int:
    v = int(round(x))
    return -32767 if v < -32767 else 32767 if v > 32767 else v


def opt16(x: Optional[float], scale: float = 1.0) -> int:
    return NONE16 if x is None else clamp16(x * scale)


# ── column codec ────────────────────────────────────────────────────────────
# A raw record is a dict of "tables"; a table is a dict of equally long integer
# columns. Encoding: per column delta coding, int16 or int32 little-endian, the
# whole record zlib-compressed behind a JSON header. Stable across platforms.

_MAGIC = b"BH1\n"


def _delta(col: Sequence[int]) -> List[int]:
    out = []
    prev = 0
    for v in col:
        out.append(v - prev)
        prev = v
    return out


def _undelta(col: Sequence[int]) -> List[int]:
    out = []
    acc = 0
    for v in col:
        acc += v
        out.append(acc)
    return out


def encode_record(meta: dict, tables: Dict[str, Dict[str, Sequence[int]]]) -> bytes:
    header = {"meta": meta, "tables": {}}
    chunks: List[bytes] = []
    for tname, cols in tables.items():
        tdesc = []
        for cname, col in cols.items():
            d = _delta(col)
            wide = any(v < -32768 or v > 32767 for v in d)
            arr = array("i" if wide else "h", d)
            if arr.itemsize not in (2, 4):          # exotic platform
                raise RuntimeError("unexpected array item size")
            if sys.byteorder != "little":
                arr.byteswap()
            b = arr.tobytes()
            tdesc.append([cname, 4 if wide else 2, len(col)])
            chunks.append(b)
        header["tables"][tname] = tdesc
    h = json.dumps(header, separators=(",", ":")).encode()
    body = struct.pack("<I", len(h)) + h + b"".join(chunks)
    return _MAGIC + zlib.compress(body, 6)


def decode_record(blob: bytes) -> tuple:
    """-> (meta, tables)."""
    if not blob.startswith(_MAGIC):
        raise ValueError("not a battery raw record")
    body = zlib.decompress(blob[len(_MAGIC):])
    (hl,) = struct.unpack_from("<I", body, 0)
    header = json.loads(body[4:4 + hl].decode())
    pos = 4 + hl
    tables: Dict[str, Dict[str, List[int]]] = {}
    for tname, tdesc in header["tables"].items():
        cols: Dict[str, List[int]] = {}
        for cname, size, n in tdesc:
            arr = array("i" if size == 4 else "h")
            arr.frombytes(body[pos:pos + size * n])
            pos += size * n
            if sys.byteorder != "little":
                arr.byteswap()
            cols[cname] = _undelta(arr)
        tables[tname] = cols
    return header["meta"], tables
