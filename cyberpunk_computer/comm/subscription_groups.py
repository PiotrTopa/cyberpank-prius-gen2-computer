"""
CAN Subscription Group Definitions.

Single source of truth for all solicited CAN PID subscriptions.
Defines logical groups of OBD-II/diagnostic PIDs that can be
individually enabled/disabled by the user via the Data Sources screen.

Core drivetrain PIDs (RPM, hybrid, cruise) are always active.

Intervals (2026-11-17): every request is a frame the gateway transmits onto
the car's powertrain CAN, and an ISO-TP reply costs a second one (the flow
control frame). A value that is broadcast is not polled (rpm: BEAN FE:26;
intake and ambient air: CAN 0x3CF; these slots are suspended while the
broadcast is fresh, comm.source_priority), and a poll runs no faster than
its consumer needs:

    21D3  1 s   cruise set speed, exists nowhere else
    21CE  2 s   block voltages; the block-resistance estimate pairs samples
                at most 3.5 s apart (docs/BATTERY_HEALTH.md)
    21C3  2 s   inverter / motor temperatures, torques
    21C4, 0104, 0110, 0124   5 s
    21CF 10 s   battery temperatures, fan, delta SOC (10 s table of the
                battery collector; 0x3CB carries two temperatures at 0.1 s)
    21D0, 0142, 010F  30 s;  0133, 0146, 0131  60 s

Nominal transmit rate with the broadcasts alive: 3.0 requests/s + 1.3 flow
controls/s = 4.3 frames/s (before: 5.9 + 2.7 = 8.6 frames/s).
"""

from dataclasses import dataclass
from typing import List, Dict, Any


@dataclass
class SubscriptionSlot:
    """A single CAN subscription slot with its gateway payload."""
    slot: int
    label: str
    payload: Dict[str, Any]


@dataclass
class SubscriptionGroup:
    """A logical group of related CAN subscriptions."""
    key: str
    label: str
    description: str
    default_enabled: bool
    toggleable: bool
    slots: List[SubscriptionSlot]


def with_padding(payload: Dict[str, Any], pad: int | None) -> Dict[str, Any]:
    """The subscription payload with its unused request bytes set to ``pad``.

    A request is a single ISO-TP frame ``[n, b1..bn, padding]``. The slots
    above pad with 0x00, which makes the frame mostly dominant bits on the
    wire; 0x55 / 0xAA / 0xCC are the customary alternatives and ECUs ignore
    the padding. ``pad`` None returns the payload unchanged (the default:
    ``BACKEND_OBD_PAD`` unset). Bench/drive experiment for the gateway's
    transmit errors, see docs/prius_can.md §9.
    """
    if pad is None:
        return payload
    d = list(payload.get("d") or [])
    n = d[0] if d else 0
    if not (1 <= n <= 7) or len(d) != 8:
        return payload
    out = dict(payload)
    out["d"] = d[:1 + n] + [pad & 0xFF] * (7 - n)
    return out


# ── Core Drivetrain (always ON, not user-toggleable) ─────────────────

CORE_GROUP = SubscriptionGroup(
    key="core",
    label="CORE DRIVETRAIN",
    description="RPM, load, hybrid, cruise (always on)",
    default_enabled=True,
    toggleable=False,
    slots=[
        SubscriptionSlot(
            slot=0, label="Engine RPM",
            payload={
                "a": "sub", "slot": 0,
                "i": "0x7E0",
                "d": [0x02, 0x01, 0x0C, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 500, "t": 200, "r": ["0x7E8"],
            },
        ),
        SubscriptionSlot(
            slot=1, label="Engine Load",
            payload={
                "a": "sub", "slot": 1,
                "i": "0x7E0",
                "d": [0x02, 0x01, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 5000, "t": 200, "r": ["0x7E8"],
            },
        ),
        SubscriptionSlot(
            slot=2, label="Hybrid Comprehensive",
            payload={
                "a": "sub", "slot": 2,
                "i": "0x7E2",
                "d": [0x02, 0x21, 0xC3, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 2000, "t": 500, "r": ["0x7EA"], "isotp": True,
            },
        ),
        SubscriptionSlot(
            slot=6, label="Cruise Control",
            payload={
                "a": "sub", "slot": 6,
                "i": "0x7E2",
                "d": [0x02, 0x21, 0xD3, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 1000, "t": 200, "r": ["0x7EA"],
            },
        ),
    ],
)


# ── Toggleable Groups ────────────────────────────────────────────────

BATTERY_CELLS_GROUP = SubscriptionGroup(
    key="battery_cells",
    label="BATTERY CELLS",
    description="Block voltages, resistance, temps, fan, delta SOC",
    default_enabled=True,
    toggleable=True,
    slots=[
        SubscriptionSlot(
            slot=3, label="Block Voltages (21CE)",
            payload={
                "a": "sub", "slot": 3,
                "i": "0x7E3",
                "d": [0x02, 0x21, 0xCE, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 2000, "t": 500, "r": ["0x7EB"], "isotp": True,
            },
        ),
        SubscriptionSlot(
            slot=4, label="Battery Temps/Fan (21CF)",
            payload={
                "a": "sub", "slot": 4,
                "i": "0x7E3",
                "d": [0x02, 0x21, 0xCF, 0x00, 0x00, 0x00, 0x00, 0x00],
                # The answer is 18 bytes (first frame 10 12 61 CF ...): without
                # ISO-TP the gateway returned the first frame only and nothing
                # after the intake temperature was ever decoded.
                "int": 10000, "t": 500, "r": ["0x7EB"], "isotp": True,
            },
        ),
        SubscriptionSlot(
            slot=5, label="Block Resistances (21D0)",
            payload={
                "a": "sub", "slot": 5,
                "i": "0x7E3",
                "d": [0x02, 0x21, 0xD0, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 30000, "t": 500, "r": ["0x7EB"], "isotp": True,
            },
        ),
    ],
)

HYBRID_EXTENDED_GROUP = SubscriptionGroup(
    key="hybrid_extended",
    label="HYBRID EXTENDED",
    description="A/C compressor power, converter temp",
    default_enabled=True,
    toggleable=True,
    slots=[
        SubscriptionSlot(
            slot=8, label="Hybrid Additional (21C4)",
            payload={
                "a": "sub", "slot": 8,
                "i": "0x7E2",
                "d": [0x02, 0x21, 0xC4, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 5000, "t": 500, "r": ["0x7EA"], "isotp": True,
            },
        ),
    ],
)

ENGINE_SENSORS_GROUP = SubscriptionGroup(
    key="engine_sensors",
    label="ENGINE SENSORS",
    description="Intake temp, MAF air flow, lambda/O2",
    default_enabled=True,
    toggleable=True,
    slots=[
        SubscriptionSlot(
            slot=9, label="Intake Air Temp (010F)",
            payload={
                "a": "sub", "slot": 9,
                "i": "0x7E0",
                "d": [0x02, 0x01, 0x0F, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 30000, "t": 200, "r": ["0x7E8"],
            },
        ),
        SubscriptionSlot(
            slot=10, label="MAF Air Flow (0110)",
            payload={
                "a": "sub", "slot": 10,
                "i": "0x7E0",
                "d": [0x02, 0x01, 0x10, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 5000, "t": 200, "r": ["0x7E8"],
            },
        ),
        SubscriptionSlot(
            slot=11, label="Lambda / O2 (0124)",
            payload={
                "a": "sub", "slot": 11,
                "i": "0x7E0",
                "d": [0x02, 0x01, 0x24, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 5000, "t": 200, "r": ["0x7E8"],
            },
        ),
    ],
)

AUX_BATTERY_GROUP = SubscriptionGroup(
    key="aux_battery",
    label="12V BATTERY",
    description="Auxiliary / 12V control module voltage",
    default_enabled=True,
    toggleable=True,
    slots=[
        SubscriptionSlot(
            slot=7, label="Aux Voltage (0142)",
            payload={
                "a": "sub", "slot": 7,
                "i": "0x7E0",
                "d": [0x02, 0x01, 0x42, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 30000, "t": 200, "r": ["0x7E8"],
            },
        ),
    ],
)

ENVIRONMENT_GROUP = SubscriptionGroup(
    key="environment",
    label="ENVIRONMENT",
    description="Barometric pressure, ambient air temp",
    default_enabled=True,
    toggleable=True,
    slots=[
        SubscriptionSlot(
            slot=13, label="Barometric Pressure (0133)",
            payload={
                "a": "sub", "slot": 13,
                "i": "0x7E0",
                "d": [0x02, 0x01, 0x33, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 60000, "t": 200, "r": ["0x7E8"],
            },
        ),
        SubscriptionSlot(
            slot=14, label="Ambient Air Temp (0146)",
            payload={
                "a": "sub", "slot": 14,
                "i": "0x7E0",
                "d": [0x02, 0x01, 0x46, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 60000, "t": 200, "r": ["0x7E8"],
            },
        ),
    ],
)

ODOMETER_GROUP = SubscriptionGroup(
    key="odometer",
    label="ODOMETER",
    description="Distance since DTC clear",
    default_enabled=True,
    toggleable=True,
    slots=[
        SubscriptionSlot(
            slot=12, label="Distance Since DTC (0131)",
            payload={
                "a": "sub", "slot": 12,
                "i": "0x7E0",
                "d": [0x02, 0x01, 0x31, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 60000, "t": 200, "r": ["0x7E8"],
            },
        ),
    ],
)


# ── Aggregated Lists ─────────────────────────────────────────────────

# All toggleable groups in display order
TOGGLEABLE_GROUPS: List[SubscriptionGroup] = [
    BATTERY_CELLS_GROUP,
    HYBRID_EXTENDED_GROUP,
    ENGINE_SENSORS_GROUP,
    AUX_BATTERY_GROUP,
    ENVIRONMENT_GROUP,
    ODOMETER_GROUP,
]

# All groups (core + toggleable)
ALL_GROUPS: List[SubscriptionGroup] = [CORE_GROUP] + TOGGLEABLE_GROUPS

# Default enabled state for each toggleable group
DEFAULT_ENABLED: Dict[str, bool] = {
    g.key: g.default_enabled for g in TOGGLEABLE_GROUPS
}
