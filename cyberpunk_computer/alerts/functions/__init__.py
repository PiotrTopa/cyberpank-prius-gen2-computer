"""
The alert functions (one module each). ``default_functions`` builds the set the
backend runs; every type in it ships in mode ``shadow`` (FEATURES.md lists
them with status, inputs and gaps).
"""

from __future__ import annotations

from typing import List

from ..model import AlertFunction


def default_functions(tuning, settings) -> List[AlertFunction]:
    fns: List[AlertFunction] = []
    for factory in _FACTORIES:
        fns.append(factory(tuning, settings))
    return fns


from .curve import CurveFunction  # noqa: E402
from .eco import EcoCoachFunction  # noqa: E402
from .guardian import GuardianFunction  # noqa: E402
from .headway import HeadwayFunction  # noqa: E402
from .lane import LaneDepartureFunction  # noqa: E402
from .obstacle import ObstacleFunction  # noqa: E402
from .maintenance import MaintenanceFunction  # noqa: E402
from .speed_limit import SpeedLimitFunction  # noqa: E402
from .stop_go import StopGoFunction  # noqa: E402
from .tunnel import TunnelRecircFunction  # noqa: E402
from .winter import WinterFunction  # noqa: E402

# (tuning, settings) -> AlertFunction
_FACTORIES: list = [SpeedLimitFunction, StopGoFunction, HeadwayFunction, CurveFunction,
                    TunnelRecircFunction, EcoCoachFunction, WinterFunction, MaintenanceFunction,
                    GuardianFunction, LaneDepartureFunction, ObstacleFunction]
