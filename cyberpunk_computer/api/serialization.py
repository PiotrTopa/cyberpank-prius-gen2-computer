"""
Serialize the immutable AppState tree (and metrics rows) into JSON-friendly
structures for the network API.

AppState is a tree of frozen dataclasses containing enums, tuples and Optionals.
``dataclasses.asdict`` recurses dataclasses/containers but leaves Enum instances
in place, which are not JSON-serializable. :func:`to_jsonable` converts the whole
tree: dataclass -> dict, Enum -> ``.name``, tuple -> list, recursively.
"""

from __future__ import annotations

import dataclasses
import enum
import math
from typing import Any, Dict, Iterable

from ..metrics import AggrPoint, EventRow


def to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses/enums/tuples into JSON-friendly values."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        # JSON has no NaN/Infinity; emit null so clients can parse safely.
        return obj if math.isfinite(obj) else None
    if isinstance(obj, enum.Enum):
        return obj.name
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    # Fallback: stringify unknown objects rather than crashing the encoder.
    return str(obj)


def serialize_state(state: Any) -> Dict[str, Any]:
    """Convert an AppState into a plain JSON-serializable dict."""
    return to_jsonable(state)


def serialize_series(points: Iterable[AggrPoint]) -> list:
    """Convert metrics aggregate points into compact JSON rows."""
    return [
        {
            "t": p.bucket,
            "min": p.min,
            "max": p.max,
            "avg": p.avg,
            "last": p.last,
            "n": p.count,
        }
        for p in points
    ]


def serialize_events(events: Iterable[EventRow]) -> list:
    """Convert metrics event rows into JSON objects."""
    return [{"t": e.ts, "type": e.type, "detail": e.detail} for e in events]
