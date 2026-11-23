"""
Per-connection shaping of the ``/api/v1/stream`` state feed.

The full state is ~30 KB and the engine publishes it at 1 Hz. A phone on LTE
that only shows the parked-car home view needs a few KB of it every few
seconds, so a client may opt in to three reductions (all off by default: a
client that sends nothing keeps the full 1 Hz snapshots):

    keys      top-level state sections to send (``powerbox,hosts,...``);
              absent or ``*`` = all sections
    interval  minimum seconds between state messages, clamped to 1..60
    delta     send only what changed since this connection's last message,
              as an RFC 7386 JSON merge patch under ``"patch"`` (the first
              message, and the first after a re-subscribe, is a full
              ``"state"`` of the selected sections)

The parameters come from the WebSocket query string and can be changed later
without reconnecting by sending ``{"type": "subscribe", ...}`` with the same
fields. A merge patch replaces lists whole and marks a removed key, or a value
that became null, with null; the client deletes such keys (reads as absent).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Mapping, Optional

MIN_INTERVAL_S = 1.0
MAX_INTERVAL_S = 60.0


def _parse_keys(raw: Any) -> Optional[FrozenSet[str]]:
    if raw is None:
        return None
    if isinstance(raw, str):
        items = [k.strip() for k in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(k).strip() for k in raw]
    else:
        return None
    items = [k for k in items if k]
    if not items or "*" in items:
        return None
    return frozenset(items)


def _parse_interval(raw: Any) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return MIN_INTERVAL_S
    if v != v:  # NaN
        return MIN_INTERVAL_S
    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, v))


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class StreamSubscription:
    """What one WebSocket client wants from the state feed."""

    keys: Optional[FrozenSet[str]] = None
    interval_s: float = MIN_INTERVAL_S
    delta: bool = False
    # Connection-local bookkeeping.
    last_sent: Optional[Dict[str, Any]] = field(default=None, repr=False)
    last_sent_t: float = field(default=float("-inf"), repr=False)

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> "StreamSubscription":
        sub = cls()
        sub.update(params)
        return sub

    def update(self, params: Mapping[str, Any]) -> None:
        """Apply new parameters; the next state message is a full resync."""
        if "keys" in params:
            self.keys = _parse_keys(params.get("keys"))
        if "interval" in params:
            self.interval_s = _parse_interval(params.get("interval"))
        if "delta" in params:
            self.delta = _parse_bool(params.get("delta"))
        self.last_sent = None
        self.last_sent_t = float("-inf")

    def select(self, state: Mapping[str, Any]) -> Dict[str, Any]:
        if self.keys is None:
            return dict(state)
        return {k: v for k, v in state.items() if k in self.keys}

    def shape(self, envelope: Mapping[str, Any], now: float) -> Optional[Dict[str, Any]]:
        """Turn a bridge envelope into the message for this client, or None to skip.

        Events always pass. State snapshots are rate-limited to ``interval_s``
        (monotonic ``now``), trimmed to ``keys`` and, with ``delta``, reduced
        to a merge patch against what this client last received.
        """
        if envelope.get("type") != "state":
            return dict(envelope)
        resync = self.last_sent is None
        if not resync and now - self.last_sent_t < self.interval_s - 0.05:
            return None
        selected = self.select(envelope.get("state") or {})
        if self.delta and not resync:
            patch = merge_patch(self.last_sent, selected)
            self.last_sent = selected
            self.last_sent_t = now
            return {"type": "state", "ts": envelope.get("ts"), "patch": patch or {}}
        self.last_sent = selected
        self.last_sent_t = now
        return {"type": "state", "ts": envelope.get("ts"), "state": selected}


def merge_patch(old: Mapping[str, Any], new: Mapping[str, Any]) -> Dict[str, Any]:
    """RFC 7386 merge patch turning ``old`` into ``new`` (empty dict = no change)."""
    patch: Dict[str, Any] = {}
    for k in old:
        if k not in new:
            patch[k] = None
    for k, nv in new.items():
        if k not in old:
            patch[k] = nv
            continue
        ov = old[k]
        if ov == nv:
            continue
        if isinstance(ov, dict) and isinstance(nv, dict):
            sub = merge_patch(ov, nv)
            if sub:
                patch[k] = sub
        else:
            patch[k] = nv
    return patch


def apply_merge_patch(target: Any, patch: Any) -> Any:
    """Reference RFC 7386 apply (used by the tests; the dashboard has its own)."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = apply_merge_patch(out.get(k), v)
    return out
