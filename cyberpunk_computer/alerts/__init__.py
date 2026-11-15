"""
Driver-alert framework and smart functions (docs/ALERTS.md, docs/FEATURES.md).

The system only informs the driver and controls comfort functions (climate
over AVC-LAN). Nothing in this package acts on steering, braking or
acceleration, and nothing transmits on BEAN or CAN.
"""

from .model import MODES, AlertFunction, AlertSpec, Candidate, EvalContext  # noqa: F401
