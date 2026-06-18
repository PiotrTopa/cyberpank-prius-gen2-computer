"""
Reconstruct the immutable AppState tree from the JSON the backend API emits.

The backend serializes AppState with api.serialization.to_jsonable:
    frozen dataclass -> dict, Enum -> .name, tuple -> list, NaN -> null.

This module reverses that, driven entirely by the dataclass field type hints, so
it stays correct as the state schema evolves. Unknown keys are ignored and missing
keys fall back to the dataclass default, which makes the frontend tolerant of
backend version skew.
"""

from __future__ import annotations

import dataclasses
import enum
import typing
from typing import Any, get_args, get_origin

from ..state.app_state import AppState


def deserialize_state(data: dict) -> AppState:
    """Build an AppState from a serialized state dict."""
    return _from_jsonable(data, AppState)


# Cache resolved type hints per dataclass (get_type_hints is relatively costly).
_HINTS_CACHE: dict[type, dict[str, Any]] = {}


def _type_hints(cls: type) -> dict[str, Any]:
    hints = _HINTS_CACHE.get(cls)
    if hints is None:
        hints = typing.get_type_hints(cls)
        _HINTS_CACHE[cls] = hints
    return hints


def _from_jsonable(value: Any, typ: Any) -> Any:
    """Convert ``value`` into ``typ`` using type hints."""
    if value is None:
        return None

    origin = get_origin(typ)

    # Optional[X] / Union[...] -> pick the first non-None member type.
    if origin is typing.Union:
        args = [a for a in get_args(typ) if a is not type(None)]
        if len(args) == 1:
            return _from_jsonable(value, args[0])
        # Heterogeneous union: best-effort, return as-is.
        return value

    # Nested dataclass.
    if dataclasses.is_dataclass(typ) and isinstance(typ, type):
        return _build_dataclass(value, typ)

    # Enum from its .name string.
    if isinstance(typ, type) and issubclass(typ, enum.Enum):
        if isinstance(value, str):
            try:
                return typ[value]
            except KeyError:
                return value
        return value

    # tuple (typed Tuple[...] or bare ``tuple``).
    if typ is tuple or origin is tuple:
        return tuple(value) if isinstance(value, (list, tuple)) else value

    # list (typed List[...] or bare ``list``).
    if typ is list or origin is list:
        return list(value) if isinstance(value, (list, tuple)) else value

    return value


def _build_dataclass(data: Any, cls: type) -> Any:
    if not isinstance(data, dict):
        # Cannot reconstruct; fall back to a default instance.
        return cls()
    hints = _type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue  # use the field default
        kwargs[f.name] = _from_jsonable(data[f.name], hints.get(f.name, Any))
    return cls(**kwargs)
