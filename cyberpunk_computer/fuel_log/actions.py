"""Store actions of the fuelling log (kept here, like diag.actions; only the
two ``ActionType`` members live in the shared enum)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..state.actions import Action, ActionSource, ActionType

#: ``FuelLogState`` fields a :class:`SetFuelLogStateAction` may set.
STATE_FIELDS = ("enabled", "entries", "figures", "defaults", "last_command", "count", "updated_at")


class FuelLogCommandAction(Action):
    """A fuelling-log request (REST / ZMQ / the MFD). The backend's
    FuelLogService middleware runs it; the reducer ignores it. The MFD's
    RemoteStore maps it onto the ``fuel_log_<op>`` command."""

    OPS = ("add", "undo", "edit_last")

    def __init__(self, op: str, params: Optional[Dict[str, Any]] = None,
                 source: ActionSource = ActionSource.UI):
        super().__init__(ActionType.FUEL_LOG_COMMAND, source)
        if op not in self.OPS:
            raise ValueError("fuel log op must be one of %s" % (self.OPS,))
        self.op = op
        self.params = dict(params or {})


class SetFuelLogStateAction(Action):
    """Publish the log into ``AppState.fuel_log``."""

    def __init__(self, fields: Dict[str, Any], source: ActionSource = ActionSource.INTERNAL):
        super().__init__(ActionType.SET_FUEL_LOG_STATE, source)
        unknown = [k for k in fields if k not in STATE_FIELDS]
        if unknown:
            raise ValueError("not a fuel log state field: %s" % ", ".join(unknown))
        self.fields = dict(fields)
