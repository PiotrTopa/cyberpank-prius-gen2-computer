"""
Solicited CAN Query Manager.

Handles OBD-II style request-response queries and periodic subscriptions
for obtaining vehicle data that isn't available in unsolicited CAN broadcasts.

Based on Gateway Protocol v2.8.0 with solicited mode support.

Key Prius Gen2 PIDs that require solicited queries:
- 0x7E0 (Engine ECU): Standard OBD-II PIDs (0x01xx)
- 0x7E2 (Hybrid ECU): Toyota-specific PIDs (0x21xx)
- 0x7E3 (Battery ECU): HV Battery detailed data (0x21xx)

References:
- docs/PROTOCOL.md - Gateway communication protocol
- docs/prius_can.md - Prius-specific CAN/OBD-II codes
- docs/TODO_SOLICITED_OBD2.md - Implementation notes
"""

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Any

logger = logging.getLogger(__name__)


# =============================================================================
# Constants - Prius Gen2 ECU Addresses
# =============================================================================

class ECUAddress(Enum):
    """Toyota Prius Gen2 ECU request/response addresses."""
    # Request addresses (TX)
    OBD2_BROADCAST = 0x7DF     # OBD-II broadcast (any ECU)
    TRANSMISSION = 0x727
    MAIN_BODY = 0x750
    AIRBAG = 0x780
    PRECRASH = 0x781
    DISTANCE = 0x790
    PRECRASH2 = 0x791
    STEERING_ASSIST = 0x7A1
    PARK_ASSIST = 0x7A2
    ABS_BRAKE = 0x7B0
    INSTRUMENT = 0x7C0
    AIR_CONDITIONER = 0x7C4
    NAVIGATION = 0x7D0
    ENGINE = 0x7E0             # Engine Controls (standard OBD-II + Toyota extended)
    HYBRID = 0x7E2             # Hybrid System (MG1/MG2, inverter temps, etc.)
    HV_BATTERY = 0x7E3         # HV Battery (detailed cell data) - response on 0x7EB
    
    # Response addresses (RX)
    ENGINE_RESP = 0x7E8        # Engine response
    HYBRID_RESP = 0x7EA        # Hybrid response
    HV_BATTERY_RESP = 0x7EB    # HV Battery response


# OBD-II Modes
OBD2_MODE_CURRENT_DATA = 0x01       # Mode 01: Current data
OBD2_MODE_FREEZE_FRAME = 0x02       # Mode 02: Freeze frame data
OBD2_MODE_DTC = 0x03                # Mode 03: Stored DTCs
OBD2_MODE_CLEAR_DTC = 0x04          # Mode 04: Clear DTCs
OBD2_MODE_TEST_RESULTS = 0x06       # Mode 06: Test results
OBD2_MODE_PENDING_DTC = 0x07        # Mode 07: Pending DTCs
OBD2_MODE_SPECIAL_CTRL = 0x08       # Mode 08: Special control mode
OBD2_MODE_VEHICLE_INFO = 0x09       # Mode 09: Vehicle information
TOYOTA_EXTENDED_MODE = 0x21         # Toyota extended diagnostic mode


# =============================================================================
# PID Definitions
# =============================================================================

@dataclass
class PIDDefinition:
    """
    Definition of an OBD-II/CAN PID.
    
    Attributes:
        ecu: Target ECU address
        mode: OBD-II mode (0x01, 0x21, etc.)
        pid: PID within the mode
        name: Human-readable name
        unit: Unit of measurement
        formula: Lambda to convert raw bytes to value
        byte_count: Expected response data bytes (excluding header)
        interval_ms: Recommended polling interval
        description: Detailed description
    """
    ecu: int
    mode: int
    pid: int
    name: str
    unit: str
    formula: Callable[[list[int]], Any]
    byte_count: int = 1
    interval_ms: int = 1000
    description: str = ""
    
    @property
    def request_data(self) -> list[int]:
        """Build request data bytes [length, mode, pid, 0, 0, 0, 0, 0]."""
        return [0x02, self.mode, self.pid, 0x00, 0x00, 0x00, 0x00, 0x00]
    
    @property
    def response_ecu(self) -> int:
        """Expected response ECU address (request + 8)."""
        return self.ecu + 0x08


# =============================================================================
# Standard OBD-II PIDs (Mode 01 - Engine ECU 0x7E0)
# =============================================================================

# Formula helpers
def _coolant_temp(d: list[int]) -> float:
    """Coolant/intake temp: A - 40 °C."""
    return d[0] - 40 if d else 0

def _rpm(d: list[int]) -> float:
    """Engine RPM: ((A*256)+B)/4."""
    return ((d[0] * 256) + d[1]) / 4 if len(d) >= 2 else 0

def _speed(d: list[int]) -> int:
    """Vehicle speed: A km/h."""
    return d[0] if d else 0

def _throttle_pos(d: list[int]) -> float:
    """Throttle position: A*100/255 %."""
    return (d[0] * 100) / 255 if d else 0

def _fuel_level(d: list[int]) -> float:
    """Fuel level: A*100/255 %."""
    return (d[0] * 100) / 255 if d else 0

def _maf_flow(d: list[int]) -> float:
    """MAF air flow: ((A*256)+B)/100 g/s."""
    return ((d[0] * 256) + d[1]) / 100 if len(d) >= 2 else 0

def _timing_advance(d: list[int]) -> float:
    """Timing advance: (A/2)-64 degrees."""
    return (d[0] / 2) - 64 if d else 0

def _aux_battery_voltage(d: list[int]) -> float:
    """Aux battery voltage: ((A*256)+B)/1000 V."""
    return ((d[0] * 256) + d[1]) / 1000 if len(d) >= 2 else 0

def _engine_load(d: list[int]) -> float:
    """Engine load: A %."""
    return d[0] if d else 0

def _fuel_trim(d: list[int]) -> float:
    """Fuel trim: 0.7812 * (A-128) %."""
    return 0.7812 * (d[0] - 128) if d else 0


# Standard OBD-II PIDs
PID_ENGINE_COOLANT_TEMP = PIDDefinition(
    ecu=ECUAddress.ENGINE.value,
    mode=OBD2_MODE_CURRENT_DATA,
    pid=0x05,
    name="ice_coolant_temp",
    unit="°C",
    formula=_coolant_temp,
    byte_count=1,
    interval_ms=1000,
    description="Engine coolant temperature"
)

PID_ENGINE_RPM = PIDDefinition(
    ecu=ECUAddress.ENGINE.value,
    mode=OBD2_MODE_CURRENT_DATA,
    pid=0x0C,
    name="rpm",
    unit="RPM",
    formula=_rpm,
    byte_count=2,
    interval_ms=200,
    description="Engine RPM"
)

PID_VEHICLE_SPEED = PIDDefinition(
    ecu=ECUAddress.ENGINE.value,
    mode=OBD2_MODE_CURRENT_DATA,
    pid=0x0D,
    name="speed_kmh",
    unit="km/h",
    formula=_speed,
    byte_count=1,
    interval_ms=200,
    description="Vehicle speed"
)

PID_INTAKE_AIR_TEMP = PIDDefinition(
    ecu=ECUAddress.ENGINE.value,
    mode=OBD2_MODE_CURRENT_DATA,
    pid=0x0F,
    name="intake_air_temp",
    unit="°C",
    formula=_coolant_temp,
    byte_count=1,
    interval_ms=2000,
    description="Intake air temperature"
)

PID_THROTTLE_POSITION = PIDDefinition(
    ecu=ECUAddress.ENGINE.value,
    mode=OBD2_MODE_CURRENT_DATA,
    pid=0x11,
    name="throttle_position",
    unit="%",
    formula=_throttle_pos,
    byte_count=1,
    interval_ms=200,
    description="Throttle position"
)

PID_AUX_BATTERY_VOLTAGE = PIDDefinition(
    ecu=ECUAddress.ENGINE.value,
    mode=OBD2_MODE_CURRENT_DATA,
    pid=0x42,
    name="aux_battery_voltage",
    unit="V",
    formula=_aux_battery_voltage,
    byte_count=2,
    interval_ms=5000,
    description="12V auxiliary battery voltage"
)

PID_AMBIENT_AIR_TEMP = PIDDefinition(
    ecu=ECUAddress.ENGINE.value,
    mode=OBD2_MODE_CURRENT_DATA,
    pid=0x46,
    name="ambient_temp",
    unit="°C",
    formula=_coolant_temp,
    byte_count=1,
    interval_ms=5000,
    description="Ambient air temperature"
)

PID_ENGINE_LOAD = PIDDefinition(
    ecu=ECUAddress.ENGINE.value,
    mode=OBD2_MODE_CURRENT_DATA,
    pid=0x04,
    name="engine_load",
    unit="%",
    formula=_engine_load,
    byte_count=1,
    interval_ms=500,
    description="Calculated engine load"
)

PID_TIMING_ADVANCE = PIDDefinition(
    ecu=ECUAddress.ENGINE.value,
    mode=OBD2_MODE_CURRENT_DATA,
    pid=0x0E,
    name="timing_advance",
    unit="°",
    formula=_timing_advance,
    byte_count=1,
    interval_ms=500,
    description="Ignition timing advance relative to #1 cylinder"
)

PID_MAF_FLOW = PIDDefinition(
    ecu=ECUAddress.ENGINE.value,
    mode=OBD2_MODE_CURRENT_DATA,
    pid=0x10,
    name="maf_flow",
    unit="g/s",
    formula=_maf_flow,
    byte_count=2,
    interval_ms=500,
    description="MAF air flow rate"
)


# =============================================================================
# Toyota Hybrid PIDs (Mode 0x21 - Hybrid ECU 0x7E2)
# =============================================================================

def _hybrid_21c3_parser(d: list[int]) -> dict[str, Any]:
    """
    Parse PID 21C3 multi-frame response (Hybrid system comprehensive data).
    
    This is a multi-frame response with ~30 bytes of data.
    Byte positions are 0-indexed from the start of data (after headers).
    
    Reference: docs/prius_can.md Section 6
    """
    result = {}
    
    if len(d) < 28:
        logger.warning(f"21C3 response too short: {len(d)} bytes")
        return result
    
    # MG2 Revolution (bytes 0-1): ((256*A)+B)-16383
    result["mg2_rpm"] = ((d[0] * 256) + d[1]) - 16383
    
    # MG2 Torque (bytes 2-3): (256*C+D)/8 - 500
    result["mg2_torque"] = ((d[2] * 256) + d[3]) / 8 - 500
    
    # Regen Brake Torque Actual (byte 4): 4 * E
    result["regen_torque_actual"] = 4 * d[4]
    
    # Regen Brake Torque Request (byte 5): 4 * F
    result["regen_torque_request"] = 4 * d[5]
    
    # MG1 Revolution (bytes 6-7): ((256*G)+H)-16383
    result["mg1_rpm"] = ((d[6] * 256) + d[7]) - 16383
    
    # MG1 Torque (bytes 8-9): (256*I+J)/8 - 500
    result["mg1_torque"] = ((d[8] * 256) + d[9]) / 8 - 500
    
    # Engine Speed Target (bytes 12-13): (256*M)+N
    if len(d) > 13:
        result["ice_rpm_target"] = (d[12] * 256) + d[13]
    
    # Engine Speed Actual (bytes 14-15): (256*O)+P
    if len(d) > 15:
        result["ice_rpm_actual"] = (d[14] * 256) + d[15]
    
    # Master Cylinder Torque (byte 17): (4 * R) - 512
    if len(d) > 17:
        result["master_cylinder_torque"] = (4 * d[17]) - 512
    
    # SOC (byte 18): (100*S)/255
    if len(d) > 18:
        result["soc_percent"] = (100 * d[18]) / 255
    
    # WOUT HV Batt to Converter (byte 19): 320 * T kW
    if len(d) > 19:
        result["wout_kw"] = 0.32 * d[19]  # 320W = 0.32kW per unit
    
    # WIN HV Batt to Converter (byte 20): U - 40800 (in Watts, as kW offset)
    # Actually: (U - 128) * 0.32 kW (approximate based on similar fields)
    if len(d) > 20:
        result["win_kw"] = (d[20] - 128) * 0.32
    
    # MG1 Inverter Temp (byte 24): Y - 40
    if len(d) > 24:
        result["mg1_inverter_temp"] = d[24] - 40
    
    # MG2 Inverter Temp (byte 25): Z - 40
    if len(d) > 25:
        result["mg2_inverter_temp"] = d[25] - 40
    
    # Motor Temp No2/MG2 (byte 26): AA - 40
    if len(d) > 26:
        result["mg2_motor_temp"] = d[26] - 40
    
    # Motor Temp No1/MG1 (byte 27): AB - 40
    if len(d) > 27:
        result["mg1_motor_temp"] = d[27] - 40
    
    # HV Battery Voltage (byte 28): 2 * AC
    if len(d) > 28:
        result["hv_voltage"] = 2 * d[28]
    
    # HV Battery Current (byte 30): 2 * AE - 256
    if len(d) > 30:
        result["hv_current"] = 2 * d[30] - 256
    
    return result


def _hybrid_21c4_parser(d: list[int]) -> dict[str, Any]:
    """
    Parse PID 21C4 response (Additional hybrid data).
    
    Reference: docs/prius_can.md Section 6
    """
    result = {}
    
    if len(d) < 6:
        return result
    
    # Accelerator Pedal Angle (byte 2): (100*C)/255
    result["accelerator_percent"] = (100 * d[2]) / 255
    
    # VL-Voltage Before Boosted (byte 3): 2 * D
    result["voltage_before_boost"] = 2 * d[3]
    
    # VH-Voltage After Boosted (byte 4): 2 * E
    # Note: This can exceed 255*2=510V, might use different formula for high voltage
    result["voltage_after_boost"] = 2 * d[4]
    
    # Converter Temperature (byte 5): F - 40
    result["converter_temp"] = d[5] - 40
    
    return result


PID_HYBRID_COMPREHENSIVE = PIDDefinition(
    ecu=ECUAddress.HYBRID.value,
    mode=TOYOTA_EXTENDED_MODE,
    pid=0xC3,
    name="hybrid_system",
    unit="multi",
    formula=_hybrid_21c3_parser,
    byte_count=30,  # Multi-frame
    interval_ms=500,
    description="Comprehensive hybrid system data (MG1/MG2 RPM, torque, temps)"
)

PID_HYBRID_ADDITIONAL = PIDDefinition(
    ecu=ECUAddress.HYBRID.value,
    mode=TOYOTA_EXTENDED_MODE,
    pid=0xC4,
    name="hybrid_additional",
    unit="multi",
    formula=_hybrid_21c4_parser,
    byte_count=16,
    interval_ms=500,
    description="Additional hybrid data (pedal, voltages, converter temp)"
)


# =============================================================================
# HV Battery PIDs (Mode 0x21 - Battery ECU 0x7E3 -> Response on 0x7EB)
# =============================================================================

def _battery_21ce_parser(d: list[int]) -> dict[str, Any]:
    """
    Parse PID 21CE response (HV Battery detailed data).
    
    Reference: docs/prius_can.md Section 7
    Layout: SOC(1) + Current(2) + 14 Block Voltages(28) = 31 bytes
    """
    result = {}
    
    if len(d) < 3:
        return result
    
    # SOC (byte 0): 0.5 * A
    result["battery_soc"] = 0.5 * d[0]
    
    # HV Battery Current (bytes 1-2): (256*B+C)/100 - 327.68
    result["battery_current"] = ((d[1] * 256) + d[2]) / 100 - 327.68
    
    # Block voltages follow in pairs (bytes 3+)
    # Block 1: (256*D+E)/100 - 327.68, etc.
    block_voltages = []
    for i in range(14):
        offset = 3 + (i * 2)
        if len(d) > offset + 1:
            voltage = ((d[offset] * 256) + d[offset + 1]) / 100 - 327.68
            block_voltages.append(voltage)
    
    if block_voltages:
        result["block_voltages"] = block_voltages
        result["block_voltage_min"] = min(block_voltages)
        result["block_voltage_max"] = max(block_voltages)
        result["block_voltage_delta"] = max(block_voltages) - min(block_voltages)
    
    return result


def _battery_21cf_parser(d: list[int]) -> dict[str, Any]:
    """
    Parse PID 21CF response (HV Battery temps and delta SOC).
    
    Reference: docs/prius_can.md Section 7
    """
    result = {}
    
    if len(d) < 7:
        return result
    
    # Battery Air Intake Temp (bytes 0-1): (256*A+B)/100 - 327.68
    result["battery_air_intake_temp"] = ((d[0] * 256) + d[1]) / 100 - 327.68
    
    # Auxiliary Battery Voltage (byte 3): (0.2*D) - 25.6
    result["aux_battery_voltage"] = (0.2 * d[3]) - 25.6
    
    # Charge Limit (byte 4): E - 64 kW
    result["charge_limit_kw"] = d[4] - 64
    
    # Discharge Limit (byte 5): F - 64 kW
    result["discharge_limit_kw"] = d[5] - 64
    
    # Delta SOC (byte 6): 0.01 * G (%)
    result["delta_soc"] = 0.01 * d[6]
    
    # Fan Speed (byte 7 if present)
    if len(d) > 7:
        result["fan_speed"] = d[7]
    
    return result


PID_HV_BATTERY_DETAIL = PIDDefinition(
    ecu=ECUAddress.HV_BATTERY.value,
    mode=TOYOTA_EXTENDED_MODE,
    pid=0xCE,
    name="hv_battery_detail",
    unit="multi",
    formula=_battery_21ce_parser,
    byte_count=33,  # Multi-frame
    interval_ms=2000,
    description="HV Battery detailed data (SOC, current, block voltages)"
)

PID_HV_BATTERY_TEMPS = PIDDefinition(
    ecu=ECUAddress.HV_BATTERY.value,
    mode=TOYOTA_EXTENDED_MODE,
    pid=0xCF,
    name="hv_battery_temps",
    unit="multi",
    formula=_battery_21cf_parser,
    byte_count=12,
    interval_ms=2000,
    description="HV Battery temperatures and delta SOC"
)


# =============================================================================
# DTC Helpers
# =============================================================================

# DTC first-nibble prefix map: 0=P0, 1=P1, 2=P2, 3=P3, 4=C0, 5=C1, 6=C2, 7=C3,
#                                8=B0, 9=B1, A=B2, B=B3, C=U0, D=U1, E=U2, F=U3
DTC_PREFIX = {
    0x0: "P0", 0x1: "P1", 0x2: "P2", 0x3: "P3",
    0x4: "C0", 0x5: "C1", 0x6: "C2", 0x7: "C3",
    0x8: "B0", 0x9: "B1", 0xA: "B2", 0xB: "B3",
    0xC: "U0", 0xD: "U1", 0xE: "U2", 0xF: "U3",
}

# ECU names for display
ECU_NAMES = {
    0x7E8: "ENGINE",
    0x7EA: "HYBRID",
    0x7EB: "HV_BATT",
}

# ECUs to scan for DTCs
DTC_ECUS = [
    (ECUAddress.ENGINE.value, "ENGINE"),
    (ECUAddress.HYBRID.value, "HYBRID"),
    (ECUAddress.HV_BATTERY.value, "HV_BATT"),
]


def parse_dtc_bytes(byte_high: int, byte_low: int) -> str | None:
    """
    Parse a 2-byte DTC into standard OBD-II code string.
    
    Format: [PPPP PPPP] [PPPP PPPP]
    First nibble of byte_high determines prefix (P/C/B/U + digit).
    Remaining 12 bits are the code number.
    
    Returns: e.g. "P0171" or None if both bytes are 0x00.
    """
    if byte_high == 0x00 and byte_low == 0x00:
        return None
    
    prefix_nibble = (byte_high >> 4) & 0x0F
    prefix = DTC_PREFIX.get(prefix_nibble, "P0")
    code_num = ((byte_high & 0x0F) << 8) | byte_low
    return f"{prefix}{code_num:03X}"


def parse_dtc_response(data: list[int], ecu_name: str) -> list[tuple[str, str]]:
    """
    Parse DTC response payload (after mode byte).
    
    Mode 03 response format:
    - Single-frame: [count, DTC1_hi, DTC1_lo, DTC2_hi, DTC2_lo, ...]
    - ISO-TP reassembled: same but possibly longer
    
    Returns: List of (dtc_code, ecu_name) tuples.
    """
    if not data:
        return []
    
    dtcs = []
    # First byte is DTC count (for Mode 43 response)
    # DTCs start at byte 1, each is 2 bytes
    i = 1
    while i + 1 < len(data):
        code = parse_dtc_bytes(data[i], data[i + 1])
        if code:
            dtcs.append((code, ecu_name))
        i += 2
    
    return dtcs


# =============================================================================
# Subscription Manager
# =============================================================================

@dataclass
class Subscription:
    """Active subscription information."""
    slot: int
    pid_def: PIDDefinition
    interval_ms: int
    active: bool = True
    last_response_ts: int | None = None
    error_count: int = 0


class SolicitedCANManager:
    """
    Manages solicited CAN queries and subscriptions.
    
    Coordinates with the Gateway to send OBD-II style queries and
    process responses.
    """
    
    # Maximum subscription slots (Gateway limit)
    MAX_SLOTS = 16
    
    # Pre-defined subscription profiles
    PROFILE_DASHBOARD = [
        # Fast-updating data for dashboard display
        (PID_ENGINE_RPM, 200),
        (PID_VEHICLE_SPEED, 200),
        (PID_ENGINE_COOLANT_TEMP, 1000),
        (PID_HYBRID_COMPREHENSIVE, 500),  # Inverter temps, MG data
    ]
    
    PROFILE_ENERGY_MONITOR = [
        # Energy monitoring focused
        (PID_HYBRID_COMPREHENSIVE, 500),
        (PID_HV_BATTERY_TEMPS, 2000),
        (PID_HV_BATTERY_DETAIL, 5000),
    ]
    
    PROFILE_BATTERY_HEALTH = [
        # Detailed battery health data
        (PID_HV_BATTERY_DETAIL, 2000),
        (PID_HV_BATTERY_TEMPS, 2000),
    ]
    
    def __init__(self):
        """Initialize the manager."""
        self._subscriptions: dict[int, Subscription] = {}
        self._send_callback: Callable[[int, dict | None, None]] = None
        self._response_handlers: dict[str, list[Callable[[str, Any], None]]] = {}
        self._mode = "listen"  # Current CAN mode
    
    def set_send_callback(self, callback: Callable[[int, dict], None]) -> None:
        """
        Set the callback for sending messages to the gateway.
        
        Args:
            callback: Function(device_id, data) to send messages
        """
        self._send_callback = callback
    
    def register_handler(
        self, 
        pid_name: str, 
        handler: Callable[[str, Any], None]
    ) -> None:
        """
        Register a handler for PID responses.
        
        Args:
            pid_name: PID name (e.g., "hybrid_system")
            handler: Callback function(pid_name, parsed_value)
        """
        if pid_name not in self._response_handlers:
            self._response_handlers[pid_name] = []
        self._response_handlers[pid_name].append(handler)
    
    def switch_mode(self, mode: str) -> None:
        """
        Switch CAN operating mode.
        
        Args:
            mode: "normal" or "listen"
        """
        if self._send_callback:
            self._send_callback(1, {"a": "mode", "m": mode})
            self._mode = mode
            logger.info(f"Switching CAN mode to: {mode}")
    
    def subscribe(
        self, 
        slot: int, 
        pid_def: PIDDefinition, 
        interval_ms: int | None = None
    ) -> bool:
        """
        Create a subscription for periodic polling.
        
        Args:
            slot: Subscription slot (0-15)
            pid_def: PID definition to poll
            interval_ms: Override interval (uses pid_def.interval_ms if None)
        
        Returns:
            True if subscription sent successfully
        """
        if slot >= self.MAX_SLOTS:
            logger.error(f"Invalid slot: {slot} (max {self.MAX_SLOTS - 1})")
            return False
        
        if not self._send_callback:
            logger.error("No send callback registered")
            return False
        
        interval = interval_ms or pid_def.interval_ms
        
        # Build subscription message
        # Use longer timeout + isotp flag for multi-frame PIDs (>7 bytes response)
        needs_isotp = pid_def.byte_count > 7
        timeout = 500 if needs_isotp else 100
        
        msg = {
            "a": "sub",
            "slot": slot,
            "i": f"0x{pid_def.ecu:03X}",
            "d": pid_def.request_data,
            "r": [f"0x{pid_def.response_ecu:03X}"],
            "int": interval,
            "t": timeout
        }
        if needs_isotp:
            msg["isotp"] = True
        
        self._send_callback(1, msg)
        
        self._subscriptions[slot] = Subscription(
            slot=slot,
            pid_def=pid_def,
            interval_ms=interval
        )
        
        logger.info(f"Subscribed slot {slot}: {pid_def.name} @ {interval}ms")
        return True
    
    def unsubscribe(self, slot: int) -> None:
        """Unsubscribe from a slot."""
        if self._send_callback:
            self._send_callback(1, {"a": "unsub", "slot": slot})
            if slot in self._subscriptions:
                del self._subscriptions[slot]
            logger.info(f"Unsubscribed slot {slot}")
    
    def unsubscribe_all(self) -> None:
        """Unsubscribe from all slots."""
        if self._send_callback:
            self._send_callback(1, {"a": "unsub", "slot": "all"})
            self._subscriptions.clear()
            logger.info("Unsubscribed from all slots")
    
    def query_once(self, pid_def: PIDDefinition, timeout_ms: int = 100) -> None:
        """
        Send a single one-time query.
        
        Args:
            pid_def: PID to query
            timeout_ms: Response timeout
        """
        if not self._send_callback:
            return
        
        msg = {
            "a": "req",
            "i": f"0x{pid_def.ecu:03X}",
            "d": pid_def.request_data,
            "r": [f"0x{pid_def.response_ecu:03X}"],
            "t": timeout_ms
        }
        
        self._send_callback(1, msg)
        logger.debug(f"Sent single query: {pid_def.name}")
    
    def request_dtc_scan(self, mode: int = OBD2_MODE_DTC) -> None:
        """
        Send DTC scan requests to all known ECUs.
        
        Mode 03 = stored DTCs, Mode 07 = pending DTCs.
        Responses are handled via process_dtc_response().
        
        Args:
            mode: OBD-II mode (0x03 for stored, 0x07 for pending)
        """
        if not self._send_callback:
            return
        
        for ecu_addr, ecu_name in DTC_ECUS:
            msg = {
                "a": "req",
                "i": f"0x{ecu_addr:03X}",
                "d": [0x01, mode, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                "r": [f"0x{ecu_addr + 8:03X}"],
                "t": 500,
                "isotp": True  # DTCs may need multi-frame
            }
            self._send_callback(1, msg)
            logger.info(f"DTC scan (mode 0x{mode:02X}) sent to {ecu_name} (0x{ecu_addr:03X})")
    
    def request_clear_dtcs(self) -> None:
        """
        Send clear DTCs request (Mode 04) to Engine ECU.
        
        WARNING: This clears all stored DTCs and resets MIL lamp.
        """
        if not self._send_callback:
            return
        
        msg = {
            "a": "req",
            "i": f"0x{ECUAddress.ENGINE.value:03X}",
            "d": [0x01, OBD2_MODE_CLEAR_DTC, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
            "r": [f"0x{ECUAddress.ENGINE_RESP.value:03X}"],
            "t": 500
        }
        self._send_callback(1, msg)
        logger.info("Clear DTCs (Mode 04) sent to Engine ECU")
    
    def apply_profile(self, profile: list[tuple]) -> None:
        """
        Apply a subscription profile.
        
        Args:
            profile: List of (PIDDefinition, interval_ms) tuples
        """
        # First clear all existing subscriptions
        self.unsubscribe_all()
        
        # Ensure we're in normal mode for queries
        self.switch_mode("normal")
        
        # Subscribe to each PID in the profile
        for slot, (pid_def, interval) in enumerate(profile):
            if slot >= self.MAX_SLOTS:
                logger.warning(f"Profile exceeds max slots, truncating")
                break
            self.subscribe(slot, pid_def, interval)
    
    def process_response(self, data: dict) -> dict[str, Any | None]:
        """
        Process a CAN response from the gateway.
        
        Args:
            data: Response data dict with 'a', 'i', 'd', and optionally 'slot'
        
        Returns:
            Parsed values dict or None
        """
        action = data.get("a")
        
        # Handle subscription responses
        if action == "sub":
            slot = data.get("slot")
            if slot is not None and slot in self._subscriptions:
                sub = self._subscriptions[slot]
                return self._parse_response(sub.pid_def, data)
        
        # Handle single query responses
        elif action == "resp":
            if "err" in data:
                logger.warning(f"Query error: {data['err']}")
                return None
            
            # Try to match response to a known PID by ECU
            can_id = data.get("i", "")
            if isinstance(can_id, str):
                can_id = int(can_id, 16)
            
            # Find matching PID def
            for sub in self._subscriptions.values():
                if sub.pid_def.response_ecu == can_id:
                    return self._parse_response(sub.pid_def, data)
        
        return None
    
    def _parse_response(
        self, 
        pid_def: PIDDefinition, 
        data: dict
    ) -> dict[str, Any | None]:
        """Parse a response using the PID's formula."""
        raw_data = data.get("d", [])
        
        if not raw_data:
            return None
        
        # Skip OBD-II header bytes: [length, mode+0x40, pid, ...]
        # Data starts at byte index 3
        if len(raw_data) > 3:
            payload = raw_data[3:]
        else:
            payload = raw_data
        
        try:
            result = pid_def.formula(payload)
            
            # Notify handlers
            handlers = self._response_handlers.get(pid_def.name, [])
            for handler in handlers:
                try:
                    handler(pid_def.name, result)
                except Exception as e:
                    logger.error(f"Handler error for {pid_def.name}: {e}")
            
            return {pid_def.name: result}
            
        except Exception as e:
            logger.error(f"Parse error for {pid_def.name}: {e}")
            return None
    
    def get_active_subscriptions(self) -> list[Subscription]:
        """Get list of active subscriptions."""
        return [s for s in self._subscriptions.values() if s.active]


# =============================================================================
# Convenience Functions
# =============================================================================

def create_obd2_request(mode: int, pid: int, ecu: int = 0x7DF) -> dict:
    """
    Create an OBD-II request message.
    
    Args:
        mode: OBD-II mode (e.g., 0x01 for current data)
        pid: PID within the mode
        ecu: Target ECU (0x7DF for broadcast, or specific ECU)
    
    Returns:
        Request data dict for gateway
    """
    return {
        "a": "req",
        "i": f"0x{ecu:03X}",
        "d": [0x02, mode, pid, 0x00, 0x00, 0x00, 0x00, 0x00],
        "t": 100
    }


def create_subscription(
    slot: int,
    ecu: int,
    mode: int,
    pid: int,
    interval_ms: int = 1000,
    timeout_ms: int = 100
) -> dict:
    """
    Create a subscription request message.
    
    Args:
        slot: Subscription slot (0-15)
        ecu: Target ECU address
        mode: OBD-II mode
        pid: PID
        interval_ms: Polling interval
        timeout_ms: Response timeout
    
    Returns:
        Subscription data dict for gateway
    """
    return {
        "a": "sub",
        "slot": slot,
        "i": f"0x{ecu:03X}",
        "d": [0x02, mode, pid, 0x00, 0x00, 0x00, 0x00, 0x00],
        "r": [f"0x{ecu + 8:03X}"],
        "int": interval_ms,
        "t": timeout_ms
    }


# Module-level manager instance
_manager: SolicitedCANManager | None = None


def get_manager() -> SolicitedCANManager:
    """Get or create the global solicited CAN manager."""
    global _manager
    if _manager is None:
        _manager = SolicitedCANManager()
    return _manager
