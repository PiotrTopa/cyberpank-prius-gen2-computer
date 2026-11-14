"""
Fuel model of the 1NZ-FXE (Prius NHW20): constants and pure helpers.

Fuel flow
---------
CAN 0x520 bytes 1-2 (big-endian word; BEAN 62:20 carries the same word) is
the engine ECU's fuel quantity PER INJECTION, not a flow. The flow therefore
scales with engine speed::

    flow [L/h] = FUEL_FLOW_K * word * rpm

``FUEL_FLOW_K`` = 2.69e-6 L/h per (count * rpm) makes the integrated flow
equal the combination meter's own trip computer (BEAN 52:90 x true speed,
52:A2 x 52:76) on the 2026-11-13 drive; fitted on the instantaneous 52:90
values it is 2.82e-6. Uncertainty about +-5 %. The fit rests on one 2.4 km /
18 min drive, mostly near 1300 rpm: K at high rpm / highway load is not
verified, and the meter itself reads 14 % above the fuel derived from MAF and
lambda, so the absolute scale is open until a brim-to-brim petrol fill
(docs/prius_can.md, open gaps).

Override at start-up with the environment variable ``PRIUS_FUEL_FLOW_K``
(float), or per ingress with ``IngressController.fuel_flow_k``.

Fuel basis
----------
Every litre figure (ours and the meter's) is PETROL-EQUIVALENT: the engine
ECU meters injector quantity as if the fuel were petrol. A sequential LPG
system drives its injectors from the petrol injector pulses, so on LPG the
figures stay petrol-equivalent and LPG litres would need a volume factor.
No input tells the backend which fuel is active, so no factor is applied:
``LPG_VOLUME_FACTOR`` is None ("not applied"). Setting it (environment
``PRIUS_LPG_VOLUME_FACTOR``) only records the value; it is applied nowhere
until an active-fuel input exists.
"""

from __future__ import annotations

import os
from typing import Optional


def _env_float(name: str, default: Optional[float]) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


#: L/h per (0x520 word count x engine rpm). See the module docstring.
FUEL_FLOW_K_DEFAULT = 2.69e-6
FUEL_FLOW_K: float = _env_float("PRIUS_FUEL_FLOW_K", FUEL_FLOW_K_DEFAULT)

#: Engine counts as running above this rpm (BEAN FE:26 / OBD 010C read 0
#: when stopped; spin-down passes through 100-400 rpm within a second).
ENGINE_RUNNING_RPM = 400

#: Upper clamp of the computed flow (L/h); the 1NZ-FXE at full load is ~10.
MAX_FLOW_LH = 30.0

#: 0x520 / 62:20 frames come only while the engine runs (period ~0.92 s);
#: after this long without one the computed flow is 0.
FLOW_STALE_S = 2.5

#: Basis of every litre figure in the state.
FUEL_BASIS = "petrol_equivalent"

#: LPG litres per petrol-equivalent litre. None = not applied (no LPG input).
LPG_VOLUME_FACTOR: Optional[float] = _env_float("PRIUS_LPG_VOLUME_FACTOR", None)

#: Display cap of the instantaneous L/100 km (the meter's own 52:90 reaches
#: ~196 at walking pace; the raw value stays in ``meter_consumption_l100km``).
DISPLAY_CAP_L100KM = 99.9

#: Speed below which our own L/100 km is not computed (L/h is shown).
MIN_SPEED_L100KM = 5.0


def fuel_flow_lh(word: int, rpm: Optional[float], k: Optional[float] = None) -> float:
    """Fuel flow (L/h) from the 0x520 per-injection word and engine rpm."""
    if not word or not rpm or word <= 0 or rpm <= 0:
        return 0.0
    flow = (FUEL_FLOW_K if k is None else k) * word * rpm
    return max(0.0, min(MAX_FLOW_LH, flow))
