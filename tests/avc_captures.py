"""Helpers to read the gateway-v3 AVC captures (cyberpunk-prius-gateway repo).

Set ``PRIUS_CAPTURES`` to the ``captures`` directory if it is not at the
default location; tests that need a capture are skipped when it is missing.
"""

from __future__ import annotations

import calendar
import json
import os
import time
from pathlib import Path
from typing import Iterator, Optional, Tuple

import pytest

from cyberpunk_computer.io.file_io import FileInputPort

CAPTURES = Path(os.environ.get(
    "PRIUS_CAPTURES", "/home/piotr/projects/cyberpunk-prius-gateway/captures"))


def capture(rel: str) -> Path:
    p = CAPTURES / rel
    if not p.exists():
        pytest.skip(f"capture not available: {p}")
    return p


def records(path: Path) -> Iterator[Tuple[float, dict]]:
    """Yield (host epoch, gateway object) for every gateway line."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            obj = FileInputPort.parse_capture_record(rec)
            if obj is not None and isinstance(rec.get("ht"), (int, float)):
                yield float(rec["ht"]), obj


def avc_records(path: Path) -> Iterator[Tuple[float, dict]]:
    """Yield (host epoch, ID 2 ``d`` payload) for AVC frames only."""
    for ht, obj in records(path):
        d = obj.get("d")
        if obj.get("id") == 2 and isinstance(d, dict) and "m" in d:
            yield ht, d


def tx_records(path: Path) -> Iterator[Tuple[float, dict]]:
    """Yield (host epoch, payload) for the emulator's own ``{"tx":..}`` lines."""
    with open(path) as f:
        for line in f:
            rec = json.loads(line)
            if "tx" in rec:
                yield float(rec["ht"]), rec["tx"]


CEST = 2 * 3600   # all capture notes are CEST (UTC+2 in October 2026)


def local_epoch(day: str, hms: str) -> float:
    """CEST wall-clock time of a capture day -> epoch."""
    return calendar.timegm(time.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S")) - CEST


def hms(t: float) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(t + CEST))
