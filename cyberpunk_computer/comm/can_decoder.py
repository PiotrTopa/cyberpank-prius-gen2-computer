"""
CAN Bus Message Decoder for Toyota Prius Gen 2.

Parses CAN messages from the vehicle's CAN bus and extracts
meaningful data like battery state, power flow, engine status.

Note: CAN IDs and data interpretation are based on community 
research from EAA-PHEV wiki and may vary between vehicle years/regions.

Broadcast ids decoded here (scales checked on this car, confidence in
docs/prius_can.md §2: C confirmed, P probable, G third-party guess):
- 0x022: yaw rate, two channels (C; scale P)      0x023: accelerations (P)
- 0x025: steering angle (C)                       0x030: brake pedal (C)
- 0x038: ICE status, B = ICE power request (P)    0x039: coolant (C)
- 0x03A: E bit 4 accelerator pressed (P)          0x03B: HV current/voltage (C)
- 0x0B1/0x0B3: wheel speeds (C)                   0x0B4: speed, E bit 0 moving
- 0x120: gear, E bit 7 cruise engaged (C)         0x230: distance counter (P), brake
- 0x244: pedal (G), E,F MG2 rpm (C)               0x348 / 0x3C8: ICE rpm (P)
- 0x3CA: true speed km/h (C)                      0x3CB: SOC, limits, temps (C)
- 0x3CD: pack voltage (P)                         0x3CF: outside/intake air (P)
- 0x484: coolant (P)                              0x5xx: gateway copies of BEAN
The last byte of every 0x022..0x3CF frame is a Toyota checksum, not data.
Not decoded: 0x3C9, 0x4C1..0x4D1 (0x4CE is a constant index/config frame,
not a temperature), 0x020, 0x03E, 0x262, 0x423, 0x602.

Solicited replies (0x7E8/0x7EA/0x7EB): docs/prius_can.md §5-7.
"""

import logging
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum, auto

logger = logging.getLogger(__name__)


# 0x025 steering wheel angle: 12-bit signed (low nibble of A, then B) x 1.5 deg,
# positive = left. Scale verified on the car (2026-11-13 drive: +-650..660 deg
# at the stops, r = 0.95 with the rear wheel-speed yaw); sign = Toyota
# convention. The high nibble of A is not part of the angle. Shared by the
# state (dynamics.steering_angle) and backend/position_feeder.py.
STEERING_DEG_PER_COUNT = 1.5


def steering_counts(data: bytes) -> int:
    """0x025 bytes -> signed 12-bit count (low nibble of A, then B)."""
    raw = ((data[0] & 0x0F) << 8) | data[1]
    return raw - 4096 if raw > 2047 else raw


def decode_steering_deg(data: bytes) -> Optional[float]:
    """0x025 bytes -> steering wheel angle in degrees, positive = left."""
    if len(data) < 2:
        return None
    return steering_counts(data) * STEERING_DEG_PER_COUNT


class CANMessageType(Enum):
    """Types of CAN messages we can decode."""
    UNKNOWN = auto()
    HV_BATTERY = auto()         # Battery state (SOC, voltage, current)
    HV_BATTERY_POWER = auto()   # Battery power flow, CCL/CDL
    HV_BATTERY_TEMP = auto()    # Battery temperature
    ENGINE_STATUS = auto()      # Engine running state, coolant temp
    VEHICLE_SPEED = auto()      # Vehicle speed
    ENGINE_RPM = auto()         # Engine RPM
    GEAR_POSITION = auto()      # Transmission gear
    INVERTER_TEMP = auto()      # Inverter/motor temperatures
    SYSTEM_STATUS = auto()      # Various status messages
    PEDAL_POSITION = auto()     # Throttle/Brake pedals
    FUEL_LEVEL = auto()         # Fuel level
    CLIMATE_DATA = auto()       # Climate/Temperature data
    ENERGY_FLOW = auto()        # Energy flow arrows (0x3B6)
    FUEL_CONSUMPTION = auto()   # Fuel consumption (0x520)
    STEERING_ANGLE = auto()     # Steering angle (0x025)
    ACCELERATION = auto()       # Longitudinal/lateral acceleration (0x023)
    WHEEL_SPEED = auto()        # Individual wheel speed (0x0B1, 0x0B3)
    HEADLIGHT_STATUS = auto()   # Headlight state (0x57F)
    SOC_BARS_EVENT = auto()     # MFD SOC bars & events (0x529)
    YAW_RATE = auto()           # Yaw rate, two channels (0x022)
    CRUISE_CONTROL = auto()     # Cruise control main switch (0x5C8)
    AIR_TEMPS = auto()          # Outside / intake air temperature (0x3CF)
    DISTANCE = auto()           # Distance counter + brake flag (0x230)
    ICE_SPEED = auto()          # ICE rpm, secondary sources (0x348, 0x3C8)
    POWERTRAIN_FLAGS = auto()   # Accelerator pressed (0x03A)
    VEHICLE_SPEED_KMH = auto()  # True speed, integer km/h (0x3CA)
    COOLANT_ALT = auto()        # Coolant, second source (0x484)
    BODY_DOORS = auto()         # Door lock + door switches (0x5B6 = BEAN FE:B6)
    # Solicited response types (OBD-II style)
    SOLICITED_RESPONSE = auto()        # Generic solicited response
    SOLICITED_ENGINE = auto()          # Response from Engine ECU (0x7E8)
    SOLICITED_HYBRID = auto()          # Response from Hybrid ECU (0x7EA)
    SOLICITED_HV_BATTERY = auto()      # Response from HV Battery ECU (0x7EB)
    SOLICITED_AIRBAG = auto()          # Response from Airbag ECU (0x788)


@dataclass
class CANMessage:
    """
    Decoded CAN message.
    
    Attributes:
        can_id: Raw CAN ID (11-bit or 29-bit)
        is_extended: True if 29-bit extended frame
        data: Raw data bytes
        msg_type: Decoded message type
        values: Extracted values (type-specific)
        timestamp: Gateway timestamp (ms)
        sequence: Message sequence number
    """
    can_id: int
    is_extended: bool
    data: list[int]
    msg_type: CANMessageType = CANMessageType.UNKNOWN
    values: dict = field(default_factory=dict)
    timestamp: int | None = None
    sequence: int | None = None


# Known Prius Gen 2 CAN IDs (11-bit standard frames): a label per id, for
# tools and logs only. Meanings and confidence: docs/prius_can.md §2-3.
KNOWN_CAN_IDS = {
    0x022: "YAW_RATE",                 # two channels, ~0.25 deg/s per count
    0x023: "ACCELERATION",             # two channels at +-45 deg (long/lat)
    0x025: "STEERING_ANGLE",
    0x030: "BRAKE_PEDAL",
    0x038: "ICE_STATUS",               # B = ICE power request
    0x039: "ICE_COOLANT",
    0x03A: "POWERTRAIN_FLAGS",         # E bit 4 accelerator pressed
    0x03B: "HV_BATTERY_CURRENT_VOLTAGE",
    0x0B1: "FRONT_WHEEL_SPEED",
    0x0B3: "REAR_WHEEL_SPEED",
    0x0B4: "VEHICLE_SPEED",            # + E bit 0 moving
    0x120: "GEAR_CRUISE",              # gear, E bit 7 cruise engaged
    0x230: "DISTANCE_COUNTER",         # A,B ~103 counts/m; D bit 2 brake
    0x244: "PEDAL_MG2_RPM",            # G pedal, E,F MG2 rpm signed
    0x348: "ICE_SPEED",                # C = rpm / 25 (NOT a pack temperature)
    0x3C8: "ICE_SPEED_2",              # C = rpm / 32 (NOT an SOC)
    0x3CA: "VEHICLE_SPEED_KMH",        # C true speed, km/h
    0x3CB: "HV_BATTERY_SOC_LIMITS",
    0x3CD: "HV_BATTERY_FAULT_VOLTAGE",
    0x3CF: "AIR_TEMPS",
    0x484: "ICE_COOLANT_2",
    0x520: "FUEL_INJECTION",           # gateway copy -> BEAN 62:20
    0x529: "SOC_BARS_EVENT",
    0x57F: "LAMPS_DOOR",
    0x5A4: "FUEL_TANK",
    0x5B6: "BODY_DOORS",               # gateway copy of BEAN FE:B6
    0x5C8: "CRUISE_MAIN",              # gateway copy of BEAN 62:C8
    0x5CC: "BEAN_98CC",
    # Solicited replies
    0x788: "OBD2_RESP_AIRBAG",
    0x7E8: "OBD2_RESP_ENGINE",
    0x7E9: "OBD2_RESP_TRANS",
    0x7EA: "OBD2_RESP_HYBRID",
    0x7EB: "OBD2_RESP_HV_BATTERY",
}

# ── Calibrated broadcast scales (2026-11-17 drive, docs/prius_can.md §2) ──
#: 0x022 yaw rate: rad/s per count (P: against the rear wheel-speed yaw with
#: a 1.48 m track, slope 0.97-0.98, r 0.95); positive = turning left (C).
YAW_RAD_S_PER_COUNT = 0.0044
YAW_DEG_S_PER_COUNT = YAW_RAD_S_PER_COUNT * 57.29577951308232
#: Standstill offsets of the two channels (counts) measured 2026-11-17; the
#: ingress re-estimates them at every standstill.
YAW_STANDSTILL_OFFSETS = (-2.0, -7.0)
#: 0x023 accelerations (P): longitudinal = k_lon x (P - Q), positive =
#: speeding up; lateral = k_lat x (P + Q), positive = towards the left.
ACCEL_LON_MS2_PER_COUNT = 0.020
ACCEL_LAT_MS2_PER_COUNT = 0.025
#: 0x230 distance counter, counts per metre of 0x0B4 distance (P).
DISTANCE_COUNTS_PER_M = 102.8
#: 0x038 B: ICE power request, kW per count (P, r 0.92 against 21C3 K,L).
ICE_POWER_KW_PER_COUNT = 0.23
#: ICE rpm per count: 0x348 C and 0x3C8 C (P, r 0.99 against BEAN FE:26).
ICE_RPM_PER_COUNT_348 = 25
ICE_RPM_PER_COUNT_3C8 = 32


def _s16(hi: int, lo: int) -> int:
    v = (hi << 8) | lo
    return v - 0x10000 if v & 0x8000 else v


def decode_yaw_counts(data) -> Optional[tuple]:
    """0x022 bytes -> (channel 1, channel 2) raw counts ((256A+B) - 0x200,
    (256C+D) - 0x200), before the standstill offset; None if short."""
    if len(data) < 4:
        return None
    return ((data[0] << 8 | data[1]) - 0x200, (data[2] << 8 | data[3]) - 0x200)


def decode_accel_counts(data) -> Optional[tuple]:
    """0x023 bytes -> (P, Q) raw counts of the two +-45 deg channels."""
    if len(data) < 4:
        return None
    return ((data[0] << 8 | data[1]) - 0x200, (data[2] << 8 | data[3]) - 0x200)


def _is_single_frame(data) -> bool:
    """ISO-TP single frame: PCI 0x01..0x07 and at least that many bytes
    (the relayed frame is 8 bytes; PCI 1 = a mode byte only, e.g. 44)."""
    return len(data) >= 3 and 1 <= data[0] <= 7 and len(data) >= 1 + data[0]


class CANDecoder:
    """
    Decodes CAN bus messages from Prius Gen 2.
    
    Usage:
        decoder = CANDecoder()
        msg = decoder.decode(raw_data)
        if msg and msg.msg_type == CANMessageType.HV_BATTERY:
            print(f"Battery SOC: {msg.values.get('soc', 0)}%")
    """
    
    def __init__(self):
        """Initialize decoder."""
        self._stats = {
            "total": 0,
            "decoded": 0,
            "unknown": 0,
            "by_type": {}
        }
    
    @property
    def stats(self) -> dict:
        """Get decoder statistics."""
        return self._stats.copy()
    
    def decode(self, raw: dict) -> CANMessage | None:
        """
        Decode a raw CAN message from gateway.
        
        Args:
            raw: Raw message dict with 'i' (CAN ID) and 'd' (data bytes)
            
        Returns:
            Decoded CANMessage or None if invalid
            
        Example raw format:
            {"i": "0x3C8", "d": [0, 0, 0, 128, 50, 0, 0, 0]}
        """
        self._stats["total"] += 1
        
        # Handle both nested and flat formats
        # Nested: {"d": {"i": "0x3C8", "d": [...]}}
        # Flat: {"i": "0x3C8", "d": [...]}
        if "d" in raw and isinstance(raw["d"], dict) and "i" in raw["d"]:
            d = raw["d"]
        else:
            d = raw
        
        can_id_str = d.get("i")
        data = d.get("d", [])
        
        if not can_id_str:
            return None
        
        # Parse CAN ID
        try:
            can_id = int(can_id_str, 16)
        except ValueError:
            return None
        
        # Check if extended frame (29-bit)
        is_extended = can_id > 0x7FF
        
        # Convert data to integers if needed
        if data and isinstance(data[0], str):
            try:
                data = [int(b, 16) for b in data]
            except ValueError:
                data = []
        
        # Create base message
        msg = CANMessage(
            can_id=can_id,
            is_extended=is_extended,
            data=data,
            timestamp=raw.get("ts"),
            sequence=raw.get("seq")
        )
        
        # Try to decode based on CAN ID
        if not is_extended:
            self._decode_standard_frame(msg)
        
        # Update stats
        if msg.msg_type == CANMessageType.UNKNOWN:
            self._stats["unknown"] += 1
        else:
            self._stats["decoded"] += 1
            type_name = msg.msg_type.name
            self._stats["by_type"][type_name] = self._stats["by_type"].get(type_name, 0) + 1
        
        return msg
    
    def _decode_standard_frame(self, msg: CANMessage) -> None:
        """Decode a standard 11-bit CAN frame based on Prius Gen 2 specs."""
        
        can_id = msg.can_id
        data = msg.data
        
        # ---------------------------------------------------
        # HYBRID VEHICLE BATTERY (HV ECU)
        # Based on EAA-PHEV wiki documentation
        # ---------------------------------------------------
        
        # 0x03B: Battery Current and Voltage (8ms period)
        # Format: [Current_Hi, Current_Lo (12-bit signed), Voltage_Hi, Voltage_Lo (16-bit), Checksum]
        # Current: 12-bit signed, [0.1 A], >0 = discharge, <0 = charge
        # Voltage: 16-bit unsigned, [1 V]
        if can_id == 0x03B and len(data) >= 5:
            msg.msg_type = CANMessageType.HV_BATTERY
            
            # Current: bytes 0-1, 12-bit signed (top 4 bits of byte 0 may be flags)
            # Format from wiki: 0F80h = -128d = charging at 12.8 A
            current_raw = ((data[0] & 0x0F) << 8) | data[1]
            # Sign extend from 12 bits
            if current_raw > 0x7FF:
                current_raw -= 0x1000
            current_amps = current_raw * 0.1
            
            # Voltage: bytes 2-3, 16-bit unsigned [1 V] (wiki: 00DCh = 220 V,
            # 0100h = 256 V). Byte 3 alone wraps at 256 V: under hard
            # regeneration the pack goes to ~260 V, which then read as 3-4 V
            # and made the power ~0 kW exactly when it was highest (seen in
            # the car's own metrics, 2026-11: V = 3 at -68 A).
            voltage = (data[2] << 8) | data[3]

            msg.values["current"] = current_amps
            msg.values["voltage"] = voltage
            msg.values["is_charging"] = current_amps < 0
            msg.values["power_kw"] = (voltage * current_amps) / 1000.0
        
        # 0x3CB: SOC, Current Limits, Temperatures (100ms period)
        # Format: [CDL, CCL, DeltaSOC, SOC, Temp1, Temp2, Checksum]
        # SOC: byte 3, unsigned [0.5%]
        # Temps: bytes 4-5, signed [°C]
        elif can_id == 0x3CB and len(data) >= 7:
            msg.msg_type = CANMessageType.HV_BATTERY_POWER
            
            # Discharge Current Limit (CDL): byte 0 [A]
            msg.values["cdl"] = data[0]
            
            # Charge Current Limit (CCL): byte 1 [A]  
            msg.values["ccl"] = data[1]
            
            # Delta SOC: Previously thought byte 2.
            # Actually byte 2 is SOC High byte (usually 0).
            # True Formula: (256*Byte2 + Byte3)/2
            # Since Byte 2 is 0, just Byte3/2.
            # 
            # TODO: Real Delta SOC requires SOLICITED OBD2 query to ECU 0x7E2 with PID 21CF
            #       See docs/ARCHITECTURE.md §3 for implementation details
            #       Formula: delta_soc = 0.01 * Byte_G (range 0-60%)
            # 
            # Ignoring Byte 2 as "delta" to avoid confusion - it's NOT delta SOC.
            # msg.values["delta_soc"] = data[2] * 0.5
            
            # SOC: byte 3 [0.5%]
            soc = data[3] * 0.5
            msg.values["soc"] = min(100.0, soc)
            msg.values["soc_raw"] = data[3]
            
            # Temperature 1: byte 4, signed [°C] - average/lowest
            temp1 = data[4]
            if temp1 > 127:
                temp1 -= 256
            msg.values["battery_temp"] = temp1
            msg.values["battery_temp_avg"] = temp1
            
            # Temperature 2: byte 5, signed [°C] - MAX TEMP per docs
            # Note: Byte 5 in 0x3CB is Temp2 (Highest/Intake)
            temp2 = data[5]
            if temp2 > 127:
                temp2 -= 256
            msg.values["battery_temp2"] = temp2
            msg.values["battery_temp_max"] = temp2
        
        # 0x3B6: Energy Flow (Energy Monitor)
        # Bytes 5 & 6 are bitmasks
        elif can_id == 0x3B6 and len(data) >= 7:
            msg.msg_type = CANMessageType.ENERGY_FLOW
            msg.values["flow_engine_to_wheels"] = bool(data[5] & 0x01) # Example bit, need validation
            msg.values["flow_battery_to_motor"] = bool(data[5] & 0x02) # Example bit
            # Docs say: No Flow 0x00. 
            # We will perform rough bit extraction based on common patterns or just pass raw
            # For now, let's just pass the raw bytes 5 and 6 so state can interpret
            msg.values["flow_byte_5"] = data[5]
            msg.values["flow_byte_6"] = data[6]

        # 0x520: Fuel Consumption
        # Byte 0: Constant (0xA4 = 164) - status/multiplexer
        # Byte 1: Page/multiplier (0-4 observed) - slow incrementing
        # Byte 2: Primary value (0-255) - rapidly varying
        # Interpretation: (Byte1 * 256) + Byte2 gives total injector time
        # Range observed: 0 (off) to ~1200 (high load)
        elif can_id == 0x520 and len(data) >= 3:
            msg.msg_type = CANMessageType.FUEL_CONSUMPTION
            # Standard Big Endian with Byte 1 as multiplier
            injector_time = (data[1] * 256) + data[2]
            msg.values["injector_time"] = injector_time



        # 0x244: accelerator pedal G (third-party, 0..0xC8) and, in E,F, the
        # MG2 speed in rpm, signed 16-bit, negative in reverse (Car
        # 2026-11-17: slope 1.00, r 0.999 against 21C3 MG2 speed).
        elif can_id == 0x244 and len(data) >= 7:
            msg.msg_type = CANMessageType.PEDAL_POSITION
            msg.values["throttle"] = data[6]
            msg.values["mg2_rpm"] = _s16(data[4], data[5])
            
        # 0x030: Brake Pedal Position
        # Eq: E (Byte 4) | Range 0-127
        elif can_id == 0x030 and len(data) >= 5:
            msg.msg_type = CANMessageType.PEDAL_POSITION
            msg.values["brake"] = data[4]
            
        # 0x5A4: Fuel Tank Level
        # Eq: B (Byte 1) | Range 0-45 L
        elif can_id == 0x5A4 and len(data) >= 2:
            msg.msg_type = CANMessageType.FUEL_LEVEL
            msg.values["fuel_level"] = data[1]

        # 0x3C8 (70 ms): C = ICE rpm / 32 (P, r 0.99 against BEAN FE:26,
        # 2026-11-17). Not an SOC (the old "soc_alt" label). D unknown.
        elif can_id == 0x3C8 and len(data) >= 4:
            msg.msg_type = CANMessageType.ICE_SPEED
            msg.values["ice_rpm"] = data[2] * ICE_RPM_PER_COUNT_3C8
            msg.values["rpm_source"] = "can3c8"

        # 0x348 (44 ms): C = ICE rpm / 25 (P, r 0.99 against FE:26); A bit 2
        # and B bits 0/5/6 switch with the ICE running (P). NOT a pack
        # temperature (the old "C - 40" label).
        elif can_id == 0x348 and len(data) >= 3:
            msg.msg_type = CANMessageType.ICE_SPEED
            msg.values["ice_rpm"] = data[2] * ICE_RPM_PER_COUNT_348
            msg.values["rpm_source"] = "can"

        # 0x3CA (108 ms): C = true vehicle speed, km/h, integer (C: slope
        # 1.01, r 0.994 against 0x0B4). Fallback speed source.
        elif can_id == 0x3CA and len(data) >= 3:
            msg.msg_type = CANMessageType.VEHICLE_SPEED_KMH
            msg.values["speed_kph"] = float(data[2])

        # 0x484 (1.1 s): B = coolant, 0.626 x B - 0.6 °C (P, r 0.9998
        # against 0x039); a third coolant source. C follows engine load (G).
        elif can_id == 0x484 and len(data) >= 2:
            msg.msg_type = CANMessageType.COOLANT_ALT
            msg.values["coolant_temp"] = round(0.626 * data[1] - 0.6, 1)

        # 0x230 (28 ms): A,B = 16-bit distance counter, wraps, ~102.8 counts
        # per metre (P; counts up in reverse too); D bit 2 = brake pressed
        # (C: 85 edges, 99 % at a BEAN FE:29 brake edge). F unknown.
        elif can_id == 0x230 and len(data) >= 4:
            msg.msg_type = CANMessageType.DISTANCE
            msg.values["distance_counter"] = (data[0] << 8) | data[1]
            msg.values["brake_flag"] = bool(data[3] & 0x04)
        
        # 0x3CD: Fault Code and Voltage (100ms period)
        # Format: [FaultCode_Hi, FaultCode_Lo, Voltage_Hi, Voltage_Lo, Checksum]
        elif can_id == 0x3CD and len(data) >= 5:
            msg.msg_type = CANMessageType.HV_BATTERY
            
            # Fault code: bytes 0-1
            fault_code = (data[0] << 8) | data[1]
            msg.values["fault_code"] = fault_code
            
            # Bytes C,D = pack voltage in V (P, 2026-11-16 data crunch: median
            # difference against 0x03B 1 V, r 0.90 at 0.2 s alignment). The
            # fallback voltage source while 0x03B (8 ms) is stale.
            msg.values["pack_voltage_3cd"] = (data[2] << 8) | data[3]

        # 0x3CF (139 ms): byte B = outside air °C, byte C = intake air °C,
        # both without offset (2026-11-16 data crunch against OBD 0146 and
        # 010F: median difference 0, r 0.975 / 0.988 over 4..10 °C outside).
        # Nothing at or below 0 °C has been recorded yet, so a byte of 0 or
        # with bit 7 set is reported as "unverified": the value is decoded as
        # signed, and the ingress then keeps the OBD poll of that value alive.
        elif can_id == 0x3CF and len(data) >= 3:
            msg.msg_type = CANMessageType.AIR_TEMPS
            for key, b in (("ambient_temp", data[1]), ("intake_air_temp", data[2])):
                msg.values[key] = b - 256 if b & 0x80 else b
                msg.values[key + "_verified"] = 0 < b < 0x80

        # ---------------------------------------------------
        # ENGINE & INVERTER
        # ---------------------------------------------------
        
        # 0x038: ICE Running Status
        # Observed: [C8, 0D, 08, 00, 00, 00, 1C] when running
        #           [C0, 00, 08, 00, 00, 00, 07] when ICE off (most common: 4093 occurrences)
        #           [C0, 07, 08, 00, 00, 00, 0E] when running (solicited RPM = 1302)
        # Byte 0: Status flags - bit 6 is NOT reliable for ICE on/off detection
        # Byte 1: RPM-correlated value (range 0-118), but NOT a reliable RPM source:
        #         - byte1*64 formula is entirely wrong (r=0.49 correlation only)
        #         - byte1=0 when engine IS running 27% of the time
        #         - However, byte1 > 0 reliably indicates ICE is running
        #         RPM is obtained from solicited OBD-II PID 010C instead.
        # Byte 2: Status/flag field (mostly 12 when running, 8 when off)
        # Byte 6: Changes with RPM - possibly checksum or low bits
        #
        # Byte 1 (2026-11-17): ICE power request, ~0.23 kW per count (P, r 0.92
        # against 21C3 K,L). Secondary rpm sources: 0x348 C, 0x3C8 C.
        elif can_id == 0x038 and len(data) >= 7:
            msg.msg_type = CANMessageType.ENGINE_STATUS
            
            # Byte 1 > 0 only with the ICE turning (engine gate evidence)
            rpm_byte = data[1]
            msg.values["ice_running"] = rpm_byte > 0
            msg.values["ice_power_request_kw"] = round(rpm_byte * ICE_POWER_KW_PER_COUNT, 2)
            
        # 0x039: Coolant Temperature (RPM from this message is NOT reliable)
        # Observed cold start: [254/255 → 1 → 2 → ... → 38 → 39 → ... → 90]
        # Observed warm engine: [54-90 decimal]
        # Byte 0: Direct °C (no offset). 254/255 = ECU sentinel (sensor not ready).
        # Byte 2: Previously thought to be RPM, but shows non-zero values (8-12)
        #         even when ICE is confirmed OFF by 0x038 byte1=0
        #         DO NOT USE byte 2 for RPM - use 0x038 instead
        elif can_id == 0x039 and len(data) >= 4:
            msg.msg_type = CANMessageType.ENGINE_RPM
            
            # Byte 0: Coolant temperature - direct value in °C (no offset)
            msg.values["coolant_temp"] = data[0]
            
            # Note: byte 2 is NOT used for RPM anymore
            # RPM comes from 0x038 byte 1 which properly shows 0 when ICE is off
        
        # 0x4CE: constant `0F 00 01 01 00 00 ...` on every drive (2026-11-16):
        # an index/configuration frame like 0x4C1..0x4D1, not a temperature.
        # Outside and intake air temperature are on 0x3CF.
        
        # 0x540: Status flags (NOT inverter temperature)
        # Inverter temps require SOLICITED PID 21C3 to ECU 0x7E2
        # See docs/ARCHITECTURE.md §3
        
        # 0x5CC: NOT a temperature. It is the Toyota gateway's CAN copy of
        # BEAN 98:CC: byte 0 is the BEAN header (0x24), bytes 1-2 the BEAN
        # data verbatim (docs/BEAN_SIGNALS.md §5, survey 2026-11-14). The old
        # "ambient = byte0 - 40" decoded the header and always gave -4 °C.
        # 98:CC is a slow 10-bit value (0x1FA-0x203) that falls as the engine
        # bay warms; its meaning is unknown, so only the raw value is kept.
        elif can_id == 0x5CC and len(data) >= 3:
            msg.msg_type = CANMessageType.SYSTEM_STATUS
            msg.values["bean_98cc_raw"] = ((data[1] << 8) | data[2]) & 0x3FF
        
        # 0x5C8: gateway CAN copy of BEAN 62:C8 ([BEAN header 0x24][62:C8
        # data], §5 of docs/BEAN_SIGNALS.md). Byte 2 bit 4 (0x10) = cruise
        # MAIN SWITCH on (C, 2026-11-16/17; NOT a cancel flag). It leads
        # BEAN 62:C8 by 40-60 ms (2026-11-17). Byte 0 bit 7 (A4 instead of
        # 24) is the gateway's immediate-copy header on the first frame
        # after a change: not a signal, ignored.
        elif can_id == 0x5C8 and len(data) >= 3:
            msg.msg_type = CANMessageType.CRUISE_CONTROL
            msg.values["cruise_main_switch"] = bool(data[2] & 0x10)
            msg.values["cruise_status_byte"] = data[0]
        
        # ---------------------------------------------------
        # VEHICLE DYNAMICS
        # ---------------------------------------------------
        
        # 0x025: Steering wheel angle (13 ms period), see decode_steering_deg
        elif can_id == 0x025 and len(data) >= 2:
            msg.msg_type = CANMessageType.STEERING_ANGLE
            raw = steering_counts(data)
            msg.values["steering_angle_raw"] = raw
            msg.values["steering_angle"] = raw * STEERING_DEG_PER_COUNT
        
        # 0x022 (13 ms): YAW RATE, two redundant channels (C: yaw, not an
        # acceleration; scale P), see decode_yaw_counts / YAW_*. The
        # standstill offset is removed by the ingress (io.dynamics_ingest).
        elif can_id == 0x022 and len(data) >= 4:
            msg.msg_type = CANMessageType.YAW_RATE
            y1, y2 = decode_yaw_counts(data)
            msg.values["yaw_raw_1"] = y1
            msg.values["yaw_raw_2"] = y2

        # 0x023 (13 ms): acceleration sensor, two channels at +-45 deg (P):
        # longitudinal = 0.020 (P - Q), lateral = 0.025 (P + Q) m/s²
        # (positive = speeding up / towards the left); includes road grade.
        elif can_id == 0x023 and len(data) >= 4:
            msg.msg_type = CANMessageType.ACCELERATION
            p, q = decode_accel_counts(data)
            msg.values["accel_raw_p"] = p
            msg.values["accel_raw_q"] = q
            msg.values["longitudinal_accel_ms2"] = round(ACCEL_LON_MS2_PER_COUNT * (p - q), 3)
            msg.values["lateral_accel_ms2"] = round(ACCEL_LAT_MS2_PER_COUNT * (p + q), 3)
        
        # 0x0B1: Front Wheel Speed (13ms period)
        # Bytes 0-1: front right wheel speed * 100 (km/h)
        # Bytes 2-3: front left wheel speed * 100 (km/h)
        elif can_id == 0x0B1 and len(data) >= 4:
            msg.msg_type = CANMessageType.WHEEL_SPEED
            msg.values["right_speed_kph"] = ((data[0] << 8) | data[1]) * 0.01
            msg.values["left_speed_kph"] = ((data[2] << 8) | data[3]) * 0.01
            msg.values["wheel_position"] = "front"
        
        # 0x0B3: Rear Wheel Speed (13ms period)
        # Bytes 0-1: rear right wheel speed * 100 (km/h)
        # Bytes 2-3: rear left wheel speed * 100 (km/h)
        elif can_id == 0x0B3 and len(data) >= 4:
            msg.msg_type = CANMessageType.WHEEL_SPEED
            msg.values["right_speed_kph"] = ((data[0] << 8) | data[1]) * 0.01
            msg.values["left_speed_kph"] = ((data[2] << 8) | data[3]) * 0.01
            msg.values["wheel_position"] = "rear"
        
        # 0x03A (8 ms): NOT a yaw rate (r 0.01 with the wheel-speed yaw). E
        # bit 4 = accelerator pressed (P, 2026-11-17: 0x244 G > 0 in all
        # 6,576 samples with it set). E bit 0 + F: a 9-bit voltage ~ pack
        # voltage - 11.5 V (P, not used).
        elif can_id == 0x03A and len(data) >= 5:
            msg.msg_type = CANMessageType.POWERTRAIN_FLAGS
            msg.values["accelerator_pressed"] = bool(data[4] & 0x10)
        
        # ---------------------------------------------------
        # HEADLIGHTS & EVENTS
        # ---------------------------------------------------
        
        # 0x57F: Lamps + door (1 s period). The Toyota gateway's CAN copy of
        # BEAN FE:7F: data[0] = BEAN header (0x68), data[1..] = FE:7F bytes
        # 0.. verbatim (docs/BEAN_SIGNALS.md §5, survey 2026-11-14).
        #   data[1] (FE:7F b0): 0x10 tail/parking, 0x20 low beam, 0x01 front
        #           fog (all confirmed); 0x08 high beam (public decode, never
        #           seen set on this car yet)
        #   data[2] (FE:7F b1): 0x08 instrument illumination (follows tail)
        #   data[3] (FE:7F b2): 0x80 A DOOR IS OPEN (was mis-read as "DRL")
        elif can_id == 0x57F and len(data) >= 4:
            msg.msg_type = CANMessageType.HEADLIGHT_STATUS
            light_byte = data[1]
            msg.values["headlight_raw"] = light_byte
            msg.values["parking_lights"] = bool(light_byte & 0x10)
            msg.values["low_beam"] = bool(light_byte & 0x20)
            msg.values["high_beam"] = bool(light_byte & 0x08)
            msg.values["fog_front"] = bool(light_byte & 0x01)
            msg.values["illumination"] = bool(data[2] & 0x08)
            msg.values["door_open"] = bool(data[3] & 0x80)
            msg.values["headlight_state"] = headlight_state(light_byte)
        
        # 0x5B6: door lock + door switches, the gateway's CAN copy of BEAN
        # FE:B6 (IG only): data[0] = header (0x64; 0xE4 = immediate copy),
        # data[1] = FE:B6 b0 (lock state, 0x00 = all locked), data[2] = FE:B6
        # b1: 0x80 driver, 0x40 front passenger, 0x04 a rear door or the
        # back door (one shared bit). Each door checked 2026-11-17 (C).
        elif can_id == 0x5B6 and len(data) >= 3:
            msg.msg_type = CANMessageType.BODY_DOORS
            msg.values["lock_state_raw"] = data[1]
            msg.values["doors_locked"] = data[1] == 0x00
            msg.values["driver_door_open"] = bool(data[2] & 0x80)
            msg.values["passenger_door_open"] = bool(data[2] & 0x40)
            msg.values["rear_door_open"] = bool(data[2] & 0x04)

        # 0x529: SOC Bars & Event Messages (1000ms period, immediate on event)
        # Byte A (data[0]) bit 7: event flag
        # Byte B (data[1]) bits 2,4,6: general problem (red triangle)
        # Byte B (data[1]) bit 3: not in park / driver door open
        # Byte D (data[3]) bits 0-2: MFD SOC bars (0-8)
        # Byte E (data[4]) bit 6: EV mode active
        # Byte F (data[5]) bits 5-7: EV mode denied reason
        elif can_id == 0x529 and len(data) >= 6:
            msg.msg_type = CANMessageType.SOC_BARS_EVENT
            msg.values["event_flag"] = bool(data[0] & 0x80)
            msg.values["warning_triangle"] = bool(data[1] & 0x54)  # bits 2,4,6
            msg.values["door_park_warning"] = bool(data[1] & 0x08)
            msg.values["soc_bars"] = data[3] & 0x07
            msg.values["ev_mode_active"] = bool(data[4] & 0x40)
            msg.values["ev_mode_denied"] = (data[5] >> 5) & 0x07
        
        # ---------------------------------------------------
        # VEHICLE SPEED
        # ---------------------------------------------------
        
        # 0x0B4: Vehicle Speed Alternative
        # Observed: [00, 00, 00, 00, 00-01, 00-1D, 00-FF, 00-FF]
        elif can_id == 0x0B4 and len(data) >= 8:
            msg.msg_type = CANMessageType.VEHICLE_SPEED
            
            # Bytes 5-6: [00-1D, 00-FF] = [0-29, 0-255]
            # Combined gives 0-7679, if /100 = 0-76 km/h (plausible)
            speed_raw = (data[5] << 8) | data[6]
            msg.values["speed_kph"] = speed_raw * 0.01
            
            # E bit 0 = moving: set at 7-14 km/h, cleared at <= 2 km/h (P,
            # 22 edges on 2026-11-17). H is the checksum.
            msg.values["moving"] = bool(data[4] & 0x01)
        
        # 0x120: Gear Position
        # Observed: [00, 00, 00, 00, 10-90, 20-23, 00-04, 59-E0]
        elif can_id == 0x120 and len(data) >= 8:
            msg.msg_type = CANMessageType.GEAR_POSITION
            
            # Byte 5: Gear Position (lower nibble)
            # 0=P, 1=R, 2=N, 3=D, 4=B
            gear_val = data[5] & 0x0F
            
            if gear_val == 0:
                msg.values["gear"] = "P"
            elif gear_val == 1:
                msg.values["gear"] = "R"
            elif gear_val == 2:
                msg.values["gear"] = "N"
            elif gear_val == 3:
                msg.values["gear"] = "D"
            elif gear_val == 4:
                msg.values["gear"] = "B"
            else:
                msg.values["gear"] = "?"
            msg.values["gear_raw"] = gear_val
            # Byte E (4) bit 7: cruise engaged (C, every engage/cancel of
            # 2026-11-16/17). It is the first source of the engaged state:
            # 21D3 D bit 6 follows 0.04-1.1 s later (poll phase). Byte H is
            # the Toyota checksum (its bit 7 only follows E), not a signal.
            msg.values["cruise_active"] = bool(data[4] & 0x80)
        
        # ---------------------------------------------------
        # SOLICITED OBD-II RESPONSES (Gateway Protocol v2.8.0)
        # ---------------------------------------------------
        # These are responses from ECUs to solicited queries.
        # Format (single-frame): [Length, Mode+0x40, PID, DataBytes...]
        # Format (ISO-TP reassembled): [Mode+0x40, PID/Count, DataBytes...]
        # Minimum 2 bytes for ISO-TP 0-DTC response: [0x43, 0x00]
        
        # 0x788: Airbag ECU Response (from 0x780) — DTC-only
        elif can_id == 0x788 and len(data) >= 2:
            msg.msg_type = CANMessageType.SOLICITED_AIRBAG
            self._decode_obd2_response(msg, data)
        
        # 0x7E8: Engine ECU Response (from 0x7E0)
        elif can_id == 0x7E8 and len(data) >= 2:
            msg.msg_type = CANMessageType.SOLICITED_ENGINE
            self._decode_obd2_response(msg, data)
        
        # 0x7EA: Hybrid ECU Response (from 0x7E2)
        # Contains inverter temps, MG1/MG2 data, etc.
        elif can_id == 0x7EA and len(data) >= 2:
            msg.msg_type = CANMessageType.SOLICITED_HYBRID
            self._decode_hybrid_response(msg, data)
        
        # 0x7EB: HV Battery ECU Response (from 0x7E3)
        # Contains detailed battery data, cell voltages, delta SOC
        elif can_id == 0x7EB and len(data) >= 2:
            msg.msg_type = CANMessageType.SOLICITED_HV_BATTERY
            self._decode_hv_battery_response(msg, data)
    
    def _decode_obd2_response(self, msg: CANMessage, data: list[int]) -> None:
        """
        Decode standard OBD-II response from Engine ECU (0x7E8).
        
        Supports two formats:
        1. Single-frame: [Length, Mode+0x40, PID, Data...] (8 bytes max)
        2. ISO-TP reassembled: [Mode+0x40, PID, Data...] (no length byte, up to 64 bytes)
        
        Detection: If data[0] >= 0x40, it's ISO-TP format (all positive OBD-II
                   response modes are 0x40+request_mode: 0x41, 0x43, 0x47, 0x61, etc.)
                   If data[0] <= 0x07, it's single-frame with PCI length byte.
        """
        if len(data) < 2:
            return
        
        # Detect format: ISO-TP reassembled starts with mode response (>= 0x40),
        # single-frame starts with PCI length byte (0x01-0x07)
        if data[0] >= 0x40:
            # ISO-TP format: [Mode, PID/Count, Data...]
            mode = data[0]
            pid = data[1] if len(data) > 1 else 0
            payload = data[2:] if len(data) > 2 else []
        elif _is_single_frame(data):
            # Single-frame format: [Length, Mode, PID, Data...]
            mode = data[1]
            pid = data[2]
            payload = data[3:1 + data[0]] if data[0] >= 3 else []
        else:
            # ISO-TP first / consecutive / flow-control frame (e.g. the
            # panda's passive copy of a multi-frame reply): not a reply on
            # its own. Reading it as [len, mode, pid] decoded consecutive
            # frames whose bytes happened to be 61 C3 as a 21C3 reply.
            return
        
        msg.values["obd2_mode"] = mode
        msg.values["obd2_pid"] = pid
        msg.values["obd2_raw"] = payload
        
        # Mode 0x41 = response to Mode 0x01 (Current Data)
        if mode == 0x41:
            if pid == 0x05 and payload:  # Coolant Temp
                msg.values["coolant_temp"] = payload[0] - 40
            elif pid == 0x0C and len(payload) >= 2:  # RPM
                msg.values["rpm"] = ((payload[0] * 256) + payload[1]) / 4
            elif pid == 0x0D and payload:  # Vehicle Speed
                msg.values["speed_kph"] = payload[0]
            elif pid == 0x0F and payload:  # Intake Air Temp
                msg.values["intake_air_temp"] = payload[0] - 40
            elif pid == 0x11 and payload:  # Throttle Position
                msg.values["throttle_position"] = (payload[0] * 100) / 255
            elif pid == 0x42 and len(payload) >= 2:  # Aux Battery Voltage
                msg.values["aux_battery_voltage"] = ((payload[0] * 256) + payload[1]) / 1000
            elif pid == 0x46 and payload:  # Ambient Air Temp
                msg.values["ambient_temp"] = payload[0] - 40
            elif pid == 0x04 and payload:  # Engine Load
                msg.values["engine_load"] = payload[0]
            elif pid == 0x0E and payload:  # Timing Advance
                msg.values["timing_advance"] = (payload[0] / 2) - 64
            elif pid == 0x10 and len(payload) >= 2:  # MAF Flow
                msg.values["maf_flow"] = ((payload[0] * 256) + payload[1]) / 100
            elif pid == 0x24 and len(payload) >= 4:  # Lambda + O2 Sensor
                msg.values["lambda_ratio"] = 0.0000305 * ((payload[0] * 256) + payload[1])
                msg.values["o2_sensor_voltage"] = 0.000122 * ((payload[2] * 256) + payload[3])
            elif pid == 0x31 and len(payload) >= 2:  # Odometer since DTC Clear
                msg.values["odometer_dtc_clear"] = (payload[0] * 256) + payload[1]
            elif pid == 0x33 and payload:  # Barometric Pressure
                msg.values["barometric_pressure"] = payload[0]
        
        # Mode 0x61 = response to Mode 0x21 (Toyota Extended)
        elif mode == 0x61:
            if pid == 0xF3 and len(payload) >= 3:  # Injector Time
                # Spec: 0.128 * C where C = byte 2 (payload index 2)
                msg.values["injector_time_ms"] = 0.128 * payload[2]
        
        # Mode 0x43 = response to Mode 0x03 (Stored DTCs)
        elif mode == 0x43:
            # Format: [43, count, DTC1_hi, DTC1_lo, DTC2_hi, DTC2_lo, ...]
            # 'pid' here is actually the DTC count byte
            msg.values["dtc_mode"] = 0x03
            msg.values["dtc_count"] = pid  # First byte after mode is count
            msg.values["dtc_raw"] = [pid] + list(payload)  # Full payload for parsing
        
        # Mode 0x47 = response to Mode 0x07 (Pending DTCs)
        elif mode == 0x47:
            msg.values["dtc_mode"] = 0x07
            msg.values["dtc_count"] = pid
            msg.values["dtc_raw"] = [pid] + list(payload)
        
        # Mode 0x44 = response to Mode 0x04 (Clear DTCs confirmation)
        elif mode == 0x44:
            msg.values["dtc_cleared"] = True
    
    def _decode_hybrid_response(self, msg: CANMessage, data: list[int]) -> None:
        """
        Decode response from Hybrid ECU (0x7EA).
        
        Handles Toyota extended diagnostic mode (0x21) responses.
        PIDs 21C3, 21C4 contain comprehensive hybrid system data.
        
        Supports two formats:
        1. Single-frame: [Length, Mode, PID, Data...] (8 bytes max)
        2. ISO-TP reassembled: [Mode, PID, Data...] (no length byte, up to 64 bytes)
        
        Detection: If data[0] >= 0x40, it's ISO-TP format (mode response byte).
                   If data[0] <= 0x07, it's single-frame with PCI length byte.
        """
        if len(data) < 2:
            return
        
        # Detect format: ISO-TP reassembled starts with mode (>= 0x40),
        # single-frame starts with PCI length byte (0x01-0x07)
        if data[0] >= 0x40:
            # ISO-TP format: [Mode, PID, Data...]
            mode = data[0]
            pid = data[1] if len(data) > 1 else 0
            payload = data[2:] if len(data) > 2 else []
        elif _is_single_frame(data):
            # Single-frame format: [Length, Mode, PID, Data...]
            mode = data[1]
            pid = data[2]
            payload = data[3:1 + data[0]] if data[0] >= 3 else []
        else:
            # ISO-TP first / consecutive / flow-control frame (e.g. the
            # panda's passive copy of a multi-frame reply): not a reply on
            # its own. Reading it as [len, mode, pid] decoded consecutive
            # frames whose bytes happened to be 61 C3 as a 21C3 reply.
            return
        
        msg.values["obd2_mode"] = mode
        msg.values["obd2_pid"] = pid
        msg.values["payload_length"] = len(payload)
        
        # Mode 0x61 = response to Mode 0x21 (Toyota Extended)
        if mode == 0x61:
            if pid == 0xC3:  # Comprehensive hybrid data
                self._decode_pid_21c3(msg, payload)
            elif pid == 0xC4:  # Additional hybrid data
                self._decode_pid_21c4(msg, payload)
            elif pid == 0xD3:  # Cruise control data
                self._decode_pid_21d3(msg, payload)
        
        # Mode 0x43 = DTC response from Hybrid ECU
        elif mode == 0x43:
            msg.values["dtc_mode"] = 0x03
            msg.values["dtc_count"] = pid
            msg.values["dtc_raw"] = [pid] + list(payload)
        
        elif mode == 0x47:
            msg.values["dtc_mode"] = 0x07
            msg.values["dtc_count"] = pid
            msg.values["dtc_raw"] = [pid] + list(payload)
    
    def _decode_pid_21c3(self, msg: CANMessage, payload: list[int]) -> None:
        """
        Decode PID 21C3 - Comprehensive hybrid system data.
        
        This is a multi-frame response. Key data positions:
        - Bytes 0-1: MG2 RPM = ((A*256)+B)-16383
        - Bytes 2-3: MG2 Torque = (C*256+D)/8 - 500
        - Byte 4: Regen Brake Torque Actual = 4 * E (Nm)
        - Byte 5: Regen Brake Torque Request = 4 * F (Nm)
        - Bytes 6-7: MG1 RPM = ((G*256)+H)-16383
        - Bytes 8-9: MG1 Torque = (I*256+J)/8 - 500
        - Bytes 10-11 (K,L): ICE Power Request = ((256*K)+L)/100 kW
        - Bytes 12-13 (M,N): Engine Speed Target = (M*256)+N RPM
        - Bytes 14-15 (O,P): Engine Speed Actual = (O*256)+P RPM
        - Byte 16 (Q): (not documented)
        - Byte 17 (R): Master Cylinder Torque = (4 * R) - 512 Nm
        - Byte 18 (S): SOC = (100 * S) / 255 %
        - Byte 19 (T): WOUT HV Batt to Converter = 320 * T Watts = 0.32 * T kW
        - Byte 20 (U): WIN HV Batt to Converter = (320*U - 40800) / 1000 kW
        - Bytes 21-22 (V,W): Discharge Request = (256*V+W) - 20480 Watts
        - Byte 23 (X): Drive Condition ID (0-6)
        - Byte 24 (Y): MG1 Inverter Temp = Y - 40
        - Byte 25 (Z): MG2 Inverter Temp = Z - 40
        - Byte 26 (AA): MG2 Motor Temp = AA - 40
        - Byte 27 (AB): MG1 Motor Temp = AB - 40
        - Byte 28 (AC): HV Voltage = 2 * AC (V)
        - Byte 30 (AE): HV Current = 2 * AE - 256 (A)
        - Bytes 31-35 (AF-AJ): Shift Sensors (0.019608 * byte = 0-5V)
        
        Note: Cruise control data — use PID 21D3 instead.
        """
        # Store payload length for debugging
        msg.values["pid_21c3_payload_len"] = len(payload)
        
        if len(payload) >= 2:
            msg.values["mg2_rpm"] = ((payload[0] * 256) + payload[1]) - 16383
        
        if len(payload) >= 4:
            msg.values["mg2_torque"] = ((payload[2] * 256) + payload[3]) / 8 - 500
        
        # Regen brake torque (bytes 4-5)
        if len(payload) >= 5:
            msg.values["regen_torque_actual"] = 4 * payload[4]
        
        if len(payload) >= 6:
            msg.values["regen_torque_request"] = 4 * payload[5]
        
        if len(payload) >= 8:
            msg.values["mg1_rpm"] = ((payload[6] * 256) + payload[7]) - 16383
        
        if len(payload) >= 10:
            msg.values["mg1_torque"] = ((payload[8] * 256) + payload[9]) / 8 - 500
        
        # ICE Power Request (bytes 10-11, K,L): ((256*K)+L)/100 kW
        if len(payload) >= 12:
            msg.values["ice_power_request_kw"] = ((payload[10] * 256) + payload[11]) / 100
        
        # Engine speed target/actual (bytes 12-15)
        if len(payload) >= 14:
            msg.values["ice_rpm_target"] = (payload[12] * 256) + payload[13]
        
        if len(payload) >= 16:
            msg.values["ice_rpm_actual"] = (payload[14] * 256) + payload[15]
        
        # Master cylinder torque (byte 17)
        if len(payload) >= 18:
            msg.values["master_cylinder_torque"] = (4 * payload[17]) - 512
        
        # SOC (byte 18)
        if len(payload) >= 19:
            msg.values["soc_percent"] = (100 * payload[18]) / 255
        
        # Battery power flow (bytes 19-20)
        if len(payload) >= 20:
            msg.values["wout_kw"] = 0.32 * payload[19]
        
        if len(payload) >= 21:
            # WIN: spec says "U - 40800" (Watts). Interpreting as (320*U - 40800)/1000 kW
            # to match WOUT's 320 scale factor. Gives 0.32*U - 40.8 kW.
            msg.values["win_kw"] = 0.32 * payload[20] - 40.8
        
        # Discharge Request to Adjust SOC (bytes 21-22, V,W): (256*V+W) - 20480 Watts
        if len(payload) >= 23:
            msg.values["discharge_request_w"] = (payload[21] * 256 + payload[22]) - 20480
        
        # Drive condition ID (byte 23, X)
        if len(payload) >= 24:
            msg.values["drive_condition"] = payload[23]
        
        # Inverter and motor temperatures
        if len(payload) >= 25:
            msg.values["mg1_inverter_temp"] = payload[24] - 40
        
        if len(payload) >= 26:
            mg2_inv_temp = payload[25] - 40
            msg.values["mg2_inverter_temp"] = mg2_inv_temp
            # Use MG2 inverter temp as the primary inverter_temp value
            msg.values["inverter_temp"] = mg2_inv_temp
        
        if len(payload) >= 27:
            msg.values["mg2_motor_temp"] = payload[26] - 40
        
        if len(payload) >= 28:
            msg.values["mg1_motor_temp"] = payload[27] - 40
        
        # HV Battery voltage (byte 28): 2 * AC
        if len(payload) >= 29:
            msg.values["hv_voltage_21c3"] = 2 * payload[28]
        
        # HV Battery current (byte 30): 2 * AE - 256
        if len(payload) >= 31:
            msg.values["hv_current_21c3"] = 2 * payload[30] - 256
        
        # Note: Cruise control data — use PID 21D3 for proper cruise control
        # with active/switch flags.
        
        # Shift Sensors (bytes 31-35, AF-AJ): 0.019608 * byte = 0-5V
        if len(payload) >= 32:
            msg.values["shift_sensor_main"] = 0.019608 * payload[31]
        if len(payload) >= 33:
            msg.values["shift_sensor_sub"] = 0.019608 * payload[32]
        if len(payload) >= 34:
            msg.values["shift_sensor_select_main"] = 0.019608 * payload[33]
        if len(payload) >= 35:
            msg.values["shift_sensor_select_sub"] = 0.019608 * payload[34]
        if len(payload) >= 36:
            msg.values["shift_sensor_position"] = 0.019608 * payload[35]
    
    def _decode_pid_21c4(self, msg: CANMessage, payload: list[int]) -> None:
        """
        Decode PID 21C4 - Additional hybrid data.
        
        - Byte 2: Accelerator Pedal = (100*C)/255
        - Byte 3: VL-Voltage Before Boost = 2 * D
        - Byte 4: VH-Voltage After Boost = 2 * E
        - Byte 5: Converter Temp = F - 40
        """
        if len(payload) >= 3:
            msg.values["accelerator_percent"] = (100 * payload[2]) / 255
        
        if len(payload) >= 4:
            msg.values["voltage_before_boost"] = 2 * payload[3]
        
        if len(payload) >= 5:
            msg.values["voltage_after_boost"] = 2 * payload[4]
        
        if len(payload) >= 6:
            msg.values["converter_temp"] = payload[5] - 40
        
        # Bytes A,B (payload[0:2]): Engine request bitmasks
        if len(payload) >= 2:
            msg.values["engine_requests_a"] = payload[0]
            msg.values["engine_requests_b"] = payload[1]
        
        # Byte G (payload[6]): Crank Position = 0.706 * G
        if len(payload) >= 7:
            msg.values["crank_position"] = 0.706 * payload[6]
        
        # Byte H (payload[7]): System Main Relay Status bits 0,1,2
        if len(payload) >= 8:
            msg.values["system_relay_1"] = bool(payload[7] & 0x01)
            msg.values["system_relay_2"] = bool(payload[7] & 0x02)
            msg.values["system_relay_3"] = bool(payload[7] & 0x04)
        
        # Byte P (payload[15]): Aircon Consumption Power = 0.019608 * P
        if len(payload) >= 16:
            msg.values["aircon_power_kw"] = 0.019608 * payload[15]
    
    def _decode_pid_21d3(self, msg: CANMessage, payload: list[int]) -> None:
        """PID 21D3 cruise control; layout in
        comm.solicited_can._hybrid_21d3_parser (decoded 2026-11-16)."""
        from .solicited_can import _hybrid_21d3_parser
        msg.values.update(_hybrid_21d3_parser(payload))
    
    def _decode_hv_battery_response(self, msg: CANMessage, data: list[int]) -> None:
        """
        Decode response from HV Battery ECU (0x7EB).
        
        Handles PIDs 21CE (detailed battery data) and 21CF (temps, delta SOC).
        
        Supports two formats:
        1. Single-frame: [Length, Mode, PID, Data...] (8 bytes max)
        2. ISO-TP reassembled: [Mode, PID, Data...] (no length byte, up to 64 bytes)
        
        Detection: If data[0] >= 0x40, it's ISO-TP format (mode response byte).
                   If data[0] <= 0x07, it's single-frame with PCI length byte.
        """
        if len(data) < 2:
            return
        
        # Detect format: ISO-TP reassembled starts with mode (>= 0x40),
        # single-frame starts with PCI length byte (0x01-0x07)
        if data[0] >= 0x40:
            # ISO-TP format: [Mode, PID, Data...]
            mode = data[0]
            pid = data[1] if len(data) > 1 else 0
            payload = data[2:] if len(data) > 2 else []
        elif _is_single_frame(data):
            # Single-frame format: [Length, Mode, PID, Data...]
            mode = data[1]
            pid = data[2]
            payload = data[3:1 + data[0]] if data[0] >= 3 else []
        else:
            # ISO-TP first / consecutive / flow-control frame (e.g. the
            # panda's passive copy of a multi-frame reply): not a reply on
            # its own. Reading it as [len, mode, pid] decoded consecutive
            # frames whose bytes happened to be 61 C3 as a 21C3 reply.
            return
        
        msg.values["obd2_mode"] = mode
        msg.values["obd2_pid"] = pid
        msg.values["payload_length"] = len(payload)
        
        # Mode 0x61 = response to Mode 0x21
        if mode == 0x61:
            if pid == 0xCE:  # Battery detail data
                self._decode_pid_21ce(msg, payload)
            elif pid == 0xCF:  # Battery temps and delta SOC
                self._decode_pid_21cf(msg, payload)
            elif pid == 0xD0:  # Internal resistance and voltage delta
                self._decode_pid_21d0(msg, payload)
        
        # Mode 0x43 = DTC response from HV Battery ECU
        elif mode == 0x43:
            msg.values["dtc_mode"] = 0x03
            msg.values["dtc_count"] = pid
            msg.values["dtc_raw"] = [pid] + list(payload)
        
        elif mode == 0x47:
            msg.values["dtc_mode"] = 0x07
            msg.values["dtc_count"] = pid
            msg.values["dtc_raw"] = [pid] + list(payload)
    
    def _decode_pid_21ce(self, msg: CANMessage, payload: list[int]) -> None:
        """
        Decode PID 21CE - HV Battery detailed data.
        
        - Byte 0: SOC = 0.5 * A
        - Bytes 1-2: Battery Current = (256*B+C)/100 - 327.68
        - Bytes 3+: Block voltages in pairs (14 blocks, 2 bytes each)
        """
        if len(payload) >= 1:
            msg.values["battery_soc_21ce"] = 0.5 * payload[0]
        
        if len(payload) >= 3:
            msg.values["battery_current_21ce"] = ((payload[1] * 256) + payload[2]) / 100 - 327.68
        
        # Decode block voltages (14 blocks, 2 bytes each starting at byte 3)
        block_voltages = []
        for i in range(14):
            offset = 3 + (i * 2)
            if len(payload) > offset + 1:
                voltage = ((payload[offset] * 256) + payload[offset + 1]) / 100 - 327.68
                block_voltages.append(voltage)
        
        if block_voltages:
            msg.values["block_voltages"] = block_voltages
            msg.values["block_voltage_min"] = min(block_voltages)
            msg.values["block_voltage_max"] = max(block_voltages)
            msg.values["block_voltage_delta"] = max(block_voltages) - min(block_voltages)
    
    def _decode_pid_21cf(self, msg: CANMessage, payload: list[int]) -> None:
        """
        Decode PID 21CF - Battery temps, fan, and delta SOC.
        
        Per prius_can.md spec:
        - Bytes 0-1 (A,B): Battery Air Intake Temp = (256*A+B)/100 - 327.68
        - Byte 2 (C): VMF Fan Motor Voltage = (0.2*C) - 25.6 V
        - Byte 3 (D): Aux Battery Voltage = (0.2*D) - 25.6 V
        - Byte 4 (E): HV Battery Charge = E - 64 kW
        - Byte 5 (F): HV Battery Discharge = F - 64 kW
        - Byte 6 (G): Delta SOC = 0.01 * G (%)
        - Byte 8 (I): Fan Speed (0-6)
        - Bytes 10-11 (K,L): Battery Temp 1 = (256*K+L)/100 - 327.68
        - Bytes 12-13 (M,N): Battery Temp 2 = (256*M+N)/100 - 327.68
        - Bytes 14-15 (O,P): Battery Temp 3 = (256*O+P)/100 - 327.68
        """
        if len(payload) >= 2:
            msg.values["battery_air_intake_temp"] = ((payload[0] * 256) + payload[1]) / 100 - 327.68
        
        # VMF Fan Motor Voltage (byte 2)
        if len(payload) >= 3:
            msg.values["fan_motor_voltage"] = (0.2 * payload[2]) - 25.6
        
        if len(payload) >= 4:
            msg.values["aux_battery_voltage_21cf"] = (0.2 * payload[3]) - 25.6
        
        if len(payload) >= 5:
            msg.values["charge_limit_kw"] = payload[4] - 64
        
        if len(payload) >= 6:
            msg.values["discharge_limit_kw"] = payload[5] - 64
        
        if len(payload) >= 7:
            # This is the REAL delta SOC (not from unsolicited 0x3CB)
            msg.values["delta_soc"] = 0.01 * payload[6]
        
        # Fan Speed — byte I (index 8), NOT byte 7 (H)
        if len(payload) >= 9:
            msg.values["battery_fan_speed"] = payload[8]
        
        # Battery temperatures 1-3 (bytes 10-15)
        if len(payload) >= 12:
            msg.values["battery_temp_1"] = ((payload[10] * 256) + payload[11]) / 100 - 327.68
        if len(payload) >= 14:
            msg.values["battery_temp_2"] = ((payload[12] * 256) + payload[13]) / 100 - 327.68
        if len(payload) >= 16:
            msg.values["battery_temp_3"] = ((payload[14] * 256) + payload[15]) / 100 - 327.68
    
    def _decode_pid_21d0(self, msg: CANMessage, payload: list[int]) -> None:
        """
        Decode PID 21D0 - Internal resistance, accumulated times, voltage delta.
        
        Per prius_can.md spec:
        - Byte 0 (A): Block Count (0-14)
        - Bytes 1-2 (B,C): Accumulated Time of Battery LOW (sec)
        - Bytes 3-4 (D,E): Accumulated Time of DC Inhibit (sec)
        - Bytes 5-6 (F,G): Accumulated Time of Battery Too High (sec)
        - Bytes 7-8 (H,I): Accumulated Time of Hot Temperature (sec)
        - Bytes 9-10 (J,K): Block Lowest Volt = (2.56*J + 0.01*K) - 327.68
        - Byte 11 (L): Block # with Min V
        - Bytes 12-13 (M,N): Block Highest Volt = (2.56*M + 0.01*N) - 327.68
        - Byte 14 (O): Block # with Max V
        - Bytes 15-28 (P-AC): Internal Resistance R01-R14 = 0.001 * byte
        """
        # Block count (byte 0)
        if len(payload) >= 1:
            msg.values["block_count"] = payload[0]
        
        # Accumulated times (bytes 1-8, 2 bytes each)
        if len(payload) >= 3:
            msg.values["accum_time_battery_low"] = (payload[1] * 256) + payload[2]
        if len(payload) >= 5:
            msg.values["accum_time_dc_inhibit"] = (payload[3] * 256) + payload[4]
        if len(payload) >= 7:
            msg.values["accum_time_battery_high"] = (payload[5] * 256) + payload[6]
        if len(payload) >= 9:
            msg.values["accum_time_hot_temp"] = (payload[7] * 256) + payload[8]
        
        # Voltage delta data (bytes 9-14)
        if len(payload) >= 11:
            msg.values["block_voltage_lowest"] = (2.56 * payload[9]) + (0.01 * payload[10]) - 327.68
        if len(payload) >= 12:
            msg.values["block_min_v_index"] = payload[11]
        if len(payload) >= 14:
            msg.values["block_voltage_highest"] = (2.56 * payload[12]) + (0.01 * payload[13]) - 327.68
        if len(payload) >= 15:
            msg.values["block_max_v_index"] = payload[14]
        
        # NiMH Volt Delta (derived)
        if len(payload) >= 14:
            lowest = (2.56 * payload[9]) + (0.01 * payload[10]) - 327.68
            highest = (2.56 * payload[12]) + (0.01 * payload[13]) - 327.68
            msg.values["nimh_volt_delta"] = highest - lowest
        
        # Internal resistance values R01-R14 (bytes 15-28, 1 byte each)
        resistances = []
        for i in range(14):
            offset = 15 + i
            if len(payload) > offset:
                resistances.append(0.001 * payload[offset])
        
        if resistances:
            msg.values["block_resistances"] = resistances


class CANStateTracker:
    """
    Tracks CAN bus state and generates state change events.
    
    Maintains current values and detects changes that should
    trigger UI updates or other actions.
    """
    
    def __init__(self):
        """Initialize state tracker."""
        self._state = {
            "battery_soc": None,        # 0-100%
            "battery_power": None,      # kW (+ = discharge, - = charge)
            "is_charging": None,        # True if regenerating/charging
            "vehicle_speed": None,      # km/h
            "ice_running": None,        # Engine on/off
        }
        self._decoder = CANDecoder()
        self._change_callbacks: list[callable] = []
    
    @property
    def state(self) -> dict:
        """Get current state snapshot."""
        return self._state.copy()
    
    def on_change(self, callback: callable) -> None:
        """Register a callback for state changes."""
        self._change_callbacks.append(callback)
    
    def update(self, raw: dict) -> dict | None:
        """
        Process a CAN message and update state.
        
        Args:
            raw: Raw gateway message dict
            
        Returns:
            Dict of changed values, or None if no changes
        """
        msg = self._decoder.decode(raw)
        if not msg:
            return None
        
        changes = {}
        
        if msg.msg_type == CANMessageType.HV_BATTERY:
            soc = msg.values.get("soc")
            if soc is not None and soc != self._state["battery_soc"]:
                self._state["battery_soc"] = soc
                changes["battery_soc"] = soc
        
        elif msg.msg_type == CANMessageType.HV_BATTERY_POWER:
            power = msg.values.get("power_kw")
            is_charging = msg.values.get("is_charging")
            
            if power is not None and power != self._state["battery_power"]:
                self._state["battery_power"] = power
                changes["battery_power"] = power
            
            if is_charging is not None and is_charging != self._state["is_charging"]:
                self._state["is_charging"] = is_charging
                changes["is_charging"] = is_charging
        
        elif msg.msg_type == CANMessageType.VEHICLE_SPEED:
            speed = msg.values.get("speed_kph")
            if speed is not None and speed != self._state["vehicle_speed"]:
                self._state["vehicle_speed"] = speed
                changes["vehicle_speed"] = speed
        
        # Notify callbacks
        if changes:
            for callback in self._change_callbacks:
                try:
                    callback(changes)
                except Exception as e:
                    logger.error(f"State change callback error: {e}")
        
        return changes if changes else None


# Utility functions

def headlight_state(lamp_byte: int) -> str:
    """"HIGH" / "LOW" / "PARK" / "OFF" from the FE:7F lamp byte (= CAN
    0x57F data[1]); the fog bit (0x01) does not change the state."""
    if lamp_byte & 0x08:
        return "HIGH"
    if lamp_byte & 0x20:
        return "LOW"
    if lamp_byte & 0x10:
        return "PARK"
    return "OFF"


def parse_can_id(can_id_str: str) -> tuple[int, bool]:
    """
    Parse CAN ID string to integer and extended flag.
    
    Args:
        can_id_str: CAN ID as hex string (e.g., "0x3C8" or "0xC9893DE")
        
    Returns:
        Tuple of (can_id: int, is_extended: bool)
    """
    can_id = int(can_id_str, 16)
    is_extended = can_id > 0x7FF
    return can_id, is_extended


def format_can_data(data: list[int]) -> str:
    """Format CAN data bytes as hex string."""
    return " ".join(f"{b:02X}" for b in data)
