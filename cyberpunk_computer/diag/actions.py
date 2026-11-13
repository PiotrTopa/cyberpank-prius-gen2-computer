"""Store actions of the all-ECU diagnostics engine.

The classes live here (not in state/actions.py) so the diagnostics package
stays self-contained; only the two ``ActionType`` members are declared in the
shared enum.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..state.actions import Action, ActionSource, ActionType

#: ``DiagnosticsState`` fields a :class:`SetDiagStateAction` may set.
STATE_FIELDS = ("ecus", "scan", "vehicle", "last_read", "autoclear", "diag_stats")


class DiagCommandAction(Action):
    """A diagnostics request from the REST API / a frontend. The backend's
    diagnostics middleware (diag.wiring) runs it; the reducer ignores it."""

    OPS = ("scan", "identify", "read", "clear", "cancel", "autoclear_set")

    def __init__(self, op: str, params: Optional[Dict[str, Any]] = None,
                 source: ActionSource = ActionSource.UI):
        super().__init__(ActionType.DIAG_COMMAND, source)
        if op not in self.OPS:
            raise ValueError("diag op must be one of %s" % (self.OPS,))
        self.op = op
        self.params = dict(params or {})


class SetDiagStateAction(Action):
    """Publish diagnostics results into ``AppState.diagnostics``."""

    def __init__(self, fields: Dict[str, Any], source: ActionSource = ActionSource.GATEWAY):
        super().__init__(ActionType.SET_DIAG_STATE, source)
        unknown = [k for k in fields if k not in STATE_FIELDS]
        if unknown:
            raise ValueError("not a diagnostics state field: %s" % ", ".join(unknown))
        self.fields = dict(fields)
