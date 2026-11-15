"""
Alert framework: the types every alert, automation and parked-car event share.

An *alert type* (:class:`AlertSpec`) is registered once with its id, text,
severity, icon, optional sound id and its timing / gating rules. A *function*
(:class:`AlertFunction`) owns the logic: on every evaluation it returns, per
alert id it provides, a :class:`Candidate` (the condition holds now, with the
inputs that made it hold) or ``None``. The manager (``alerts/manager.py``)
turns candidates into fired / shown / cleared alerts.

Mode per alert type (persisted, changeable from the dashboard):

    off     not evaluated into anything (the function may still run)
    shadow  evaluated and logged with its evidence; nothing is shown,
            sounded or actuated  (default for every type)
    on      logged, shown on the MFD, sounded (when a sound sink exists),
            actuated (automations), sent to the notification sink (events)

Kinds:

    alert       a driver notice on the MFD banner (and the sound channel)
    automation  a comfort action (climate over AVC-LAN); never shown as a
                banner, its actions are logged in shadow and executed in on
    event       a parked-car event: recorded, listed on the dashboard and
                handed to the notification sink; never on the MFD

Nothing here acts on steering, brakes or the drivetrain: the only actuation
path in the whole framework is :meth:`EvalContext.actuate`, and the service
maps it to AVC-LAN climate button actions only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple

MODES: Tuple[str, ...] = ("off", "shadow", "on")
SEVERITIES: Dict[str, int] = {"info": 0, "notice": 1, "warning": 2}
KINDS: Tuple[str, ...] = ("alert", "automation", "event")
DRIVE_GEARS: FrozenSet[str] = frozenset({"DRIVE", "B"})


@dataclass(frozen=True)
class AlertSpec:
    """One alert type. Timing values are seconds, speeds true km/h."""

    id: str
    title: str                         # dashboard name
    text: str                          # default one-line banner text (short, upper case)
    function: str = ""                 # owning function (filled in by the registry)
    kind: str = "alert"
    severity: str = "notice"
    priority: int = 50                 # higher wins within a severity
    icon: str = "info"                 # MFD icon id (ui/scene/alerts.py ICONS)
    sound: Optional[str] = None        # sound id for the sound sink (alerts/sinks.py)
    default_mode: str = "shadow"
    debounce_s: float = 0.0            # the condition must hold this long before it fires
    clear_after_s: float = 1.0         # absent this long -> cleared, and the type re-arms
    min_show_s: float = 3.0            # a shown alert stays at least this long
    max_show_s: Optional[float] = 8.0  # auto-clear after this long on screen (None: while it holds)
    cooldown_s: float = 30.0           # after a fire, no new fire for this long
    max_per_drive: Optional[int] = None
    min_kmh: Optional[float] = None    # gating on the TRUE speed
    max_kmh: Optional[float] = None
    gears: Optional[FrozenSet[str]] = None   # GearPosition names; None = any
    require_ready: bool = False
    require_off: bool = False          # car powered off (parked-car events)
    ackable: bool = True               # a tap acknowledges (per event), a long press mutes it for the drive
    advisory: bool = False             # labelled "advisory" everywhere
    description: str = ""
    needs: Tuple[str, ...] = ()        # the inputs it relies on (documentation, dashboard)
    # Returns a reason why mode "on" is refused right now (e.g. a required
    # input is not available), or None. Shadow is always allowed.
    enable_check: Optional[Callable[[], Optional[str]]] = None

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "text": self.text, "function": self.function,
            "kind": self.kind, "severity": self.severity, "priority": self.priority,
            "icon": self.icon, "sound": self.sound, "default_mode": self.default_mode,
            "advisory": self.advisory, "description": self.description, "needs": list(self.needs),
            "gating": {"min_kmh": self.min_kmh, "max_kmh": self.max_kmh,
                       "gears": sorted(self.gears) if self.gears else None,
                       "require_ready": self.require_ready, "require_off": self.require_off},
            "timing": {"debounce_s": self.debounce_s, "clear_after_s": self.clear_after_s,
                       "min_show_s": self.min_show_s, "max_show_s": self.max_show_s,
                       "cooldown_s": self.cooldown_s, "max_per_drive": self.max_per_drive},
        }


@dataclass
class Candidate:
    """The condition of an alert type holds now.

    ``key`` identifies the event (a limit segment, a stop, a curve): after a
    fire the type stays latched on that key until the condition clears or the
    key changes, and an acknowledge suppresses exactly that key.
    ``evidence`` = the inputs that made it hold (logged with the fire).
    """

    text: Optional[str] = None
    key: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)
    severity: Optional[str] = None
    sound: Optional[str] = None
    chip: Optional[Dict[str, Any]] = None   # extra MFD element while shown (e.g. the real speed)


class EvalContext:
    """What a function may ask the manager during one evaluation."""

    def __init__(self, manager, now: float):
        self._m = manager
        self.now = now

    @property
    def drive(self) -> int:
        return self._m.drive_id

    @property
    def odo_m(self) -> float:
        """Distance driven since the backend started, from the true speed (m)."""
        return self._m.odo_m

    def mode(self, alert_id: str) -> str:
        return self._m.mode(alert_id)

    def actuate(self, alert_id: str, action: str, evidence: Optional[Dict[str, Any]] = None) -> bool:
        """Ask for a comfort action on behalf of an automation type.

        Logged in shadow and on; executed only in ``on``. Returns True when it
        was executed (the caller then expects the car's feedback)."""
        return self._m.actuate(alert_id, action, evidence or {})

    def note(self, alert_id: str, what: str, evidence: Optional[Dict[str, Any]] = None) -> None:
        """Log a non-firing observation for a type (e.g. "stepped back")."""
        self._m.note(alert_id, what, evidence or {})

    def stat(self, name: str, data: Dict[str, Any]) -> None:
        """Log a statistic (e.g. the braking score of a stop): independent of
        any mode, never shown; goes to the alert log and the metrics events."""
        self._m.stat(name, data)


class AlertFunction:
    """Base of a function: provides one or more alert types."""

    name: str = ""
    specs: Tuple[AlertSpec, ...] = ()

    def evaluate(self, inp, ctx: EvalContext) -> Dict[str, Optional[Candidate]]:  # pragma: no cover
        raise NotImplementedError

    def on_drive_start(self, inp, ctx: EvalContext) -> None:
        """A new drive began (READY after a pause)."""

    def status(self) -> Dict[str, Any]:
        """Function state for the dashboard (statistics, current values)."""
        return {}
