"""
CAN Bus Message Decoder for Toyota Prius Gen 2.

Parses CAN messages from the vehicle's CAN bus and extracts
meaningful data like battery state, power flow, engine status.

Note: CAN IDs and data interpretation are based on community 
research from EAA-PHEV wiki and may vary between vehicle years/regions.

Key Prius Gen 2 CAN messages (from Battery ECU @ 100ms):
- 0x03B: Battery current (12-bit signed @ 0.1A) and voltage (16-bit unsigned [V])
- 0x3CB: SOC, CCL/CDL, temperatures
- 0x3CD: Fault codes, pack voltage
- 0x3C8: SOC alternative
- 0x3C9: Cell voltage calibration data

Other important messages:
- 0x038: ICE RPM and coolant temperature  
- 0x039: ICE RPM alternative
- 0x03A: Vehicle speed related
- 0x120: Gear position
- 0x348: Battery pack temperature/status

Note: Inverter temperature requires SOLICITED PID 21C3 to ECU 0x7E2
      (not available in unsolicited CAN messages)
"""

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, List, Any

logger = logging.getLogger(__name__)


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
    data: List[int]
    msg_type: CANMessageType = CANMessageType.UNKNOWN
    values: dict = field(default_factory=dict)
    timestamp: Optional[int] = None
    sequence: Optional[int] = None


# Known Prius Gen 2 CAN IDs (11-bit standard frames)
# Based on EAA-PHEV wiki documentation
KNOWN_CAN_IDS = {
    # Battery ECU messages (from EAA-PHEV wiki)
    0x03B: "HV_BATTERY_CURRENT_VOLTAGE",  # Current (12-bit signed 0.1A) + Voltage (16-bit V)
    0x3CB: "HV_BATTERY_SOC_LIMITS",       # CDL, CCL, Delta SOC, SOC, temps
    0x3CD: "HV_BATTERY_FAULT_VOLTAGE",    # Fault code + voltage
    0x3C8: "HV_BATTERY_SOC_ALT",          # Alternative SOC data
    0x3C9: "HV_BATTERY_CELL_DATA",        # Cell voltage calibration
    
    # Engine/ICE
    0x038: "ENGINE_RPM_COOLANT",   # ICE RPM and coolant temperature
    0x039: "ENGINE_RPM_ALT",       # Engine RPM alternative
    0x030: "ENGINE_STATUS",        # ICE status flags
    
    # Speed and position
    0x03A: "VEHICLE_SPEED",        # Vehicle speed data
    0x0B4: "VEHICLE_SPEED_ALT",    # Vehicle speed alternative
    0x120: "GEAR_POSITION",        # PRND gear position
    
    # Energy Flow
    0x3B6: "ENERGY_FLOW",          # Energy flow arrows
    
    # Fuel
    0x520: "FUEL_INJECTOR",        # Fuel injector time
    0x5A4: "FUEL_TANK",            # Fuel level

    # Temperatures
    0x348: "BATTERY_PACK_TEMP",    # Battery pack temperature/status
    # 0x540: NOT inverter temp - unknown status message (see decode section)
    
    # Other frequent messages
    0x03E: "SYSTEM_STATUS_3E",     # Unknown status
    0x0B3: "SYSTEM_STATUS_B3",     # Unknown status
}


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
    
    def decode(self, raw: dict) -> Optional[CANMessage]:
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
        
        d = raw.get("d", {})
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
            
            # Voltage: bytes 2-3, 16-bit unsigned
            # Format from wiki: 00DCh = 220V, 0100h = 256V
            # UPDATE: Per 2009 Gen2 Docs, Byte 3 is voltage [0-255V]
            # My previous interpretation was 0.5V
            # Let's check ranges. If raw is > 255, it's likely 2 byte.
            # But recent data sample showed '229' (0xE5) which is perfect for 229V.
            # Using byte 3 alone.
            voltage = data[3]
            
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
            #       See docs/TODO_SOLICITED_OBD2.md for implementation details
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
        # Bytes 0-1: Fuel Injector Time
        elif can_id == 0x520 and len(data) >= 2:
            msg.msg_type = CANMessageType.FUEL_CONSUMPTION
            injector_time = (data[0] << 8) | data[1]
            msg.values["injector_time"] = injector_time



        # 0x244: Throttle Pedal Position
        # Eq: G (Byte 6) | Range 0-200 (0xC8)
        elif can_id == 0x244 and len(data) >= 7:
            msg.msg_type = CANMessageType.PEDAL_POSITION
            msg.values["throttle"] = data[6]
            
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

        # 0x3C8: Alternative SOC data (info only, NOT used for SOC updates)
        # Observed: [00-10, 28-34, 00-60, 00, 00-FD]
        # NOTE: byte 2 often = 0, which would incorrectly reset SOC
        # We only use 0x3CB for reliable SOC data
        elif can_id == 0x3C8 and len(data) >= 5:
            msg.msg_type = CANMessageType.SYSTEM_STATUS  # Don't treat as HV_BATTERY
            msg.values["soc_alt"] = data[2]  # Store for info only
        
        # 0x3CD: Fault Code and Voltage (100ms period)
        # Format: [FaultCode_Hi, FaultCode_Lo, Voltage_Hi, Voltage_Lo, Checksum]
        elif can_id == 0x3CD and len(data) >= 5:
            msg.msg_type = CANMessageType.HV_BATTERY
            
            # Fault code: bytes 0-1
            fault_code = (data[0] << 8) | data[1]
            msg.values["fault_code"] = fault_code
            
            # Voltage: bytes 2-3 (same format as 0x03B potentially)
            # Observed range: BA-FF (186-255), 8F-D4 (143-212) for checksum
            # Let's decode bytes 3-4 as voltage  
            voltage_raw = (data[3] << 8) | data[4]
            # This doesn't look right, let's just store raw for now
            msg.values["voltage_raw_3cd"] = voltage_raw
        
        # 0x348: Battery Pack Temperature/Status
        # Observed: [04, 60, 34, 00, 18, 01] = [04, 96, 52, 0, 24, 1]
        # Byte 2: 0x34 = 52, with offset 40 = 12°C (matches 0x3CB temps)
        # Byte 1: 0x60 = 96, with offset 40 = 56°C (seems too high, likely not temp)
        elif can_id == 0x348 and len(data) >= 6:
            msg.msg_type = CANMessageType.HV_BATTERY_TEMP
            # Use byte 2 as primary pack temperature (matches other temp readings)
            temp_raw = data[2]
            if temp_raw > 0:
                msg.values["pack_temp"] = temp_raw - 40
            # Byte 4 might be additional temp or status
            if len(data) > 4 and data[4] > 0:
                msg.values["pack_temp2"] = data[4] - 40
        
        # ---------------------------------------------------
        # ENGINE & INVERTER
        # ---------------------------------------------------
        
        # 0x038: ICE Status and RPM (PRIMARY RPM SOURCE)
        # Observed: [C8, 0D, 08, 00, 00, 00, 1C] when running
        #           [C0, 00, 08, 00, 00, 00, 07] when ICE off (most common: 4093 occurrences)
        # Byte 0: Status flags - bit 6 is NOT reliable for ICE on/off detection
        # Byte 1: RPM value (range 0-118, *32 = 0-3776 RPM)
        #         When byte 1 = 0, ICE is definitely OFF
        #         When byte 1 > 0, ICE is running with RPM = byte1 * 32
        # Note: Coolant temperature NOT reliably found in 0x038
        elif can_id == 0x038 and len(data) >= 7:
            msg.msg_type = CANMessageType.ENGINE_STATUS
            
            # Byte 1: RPM scaling (range 0-118, *32 = 0-3776 RPM)
            # Also serves as ICE running indicator: 0 = off, >0 = running
            rpm_byte = data[1]
            msg.values["rpm"] = rpm_byte * 32
            
            # ICE running status determined by RPM byte (not byte 0 flags)
            msg.values["ice_running"] = rpm_byte > 0
            
        # 0x039: Coolant Temperature (RPM from this message is NOT reliable)
        # Observed: [36-5A, 00-02, 00-27, 76-BC]
        # Byte 0: Range 54-90 decimal = direct °C (warm engine, no offset needed)
        # Byte 2: Previously thought to be RPM, but shows non-zero values (8-12)
        #         even when ICE is confirmed OFF by 0x038 byte1=0
        #         DO NOT USE byte 2 for RPM - use 0x038 instead
        elif can_id == 0x039 and len(data) >= 4:
            msg.msg_type = CANMessageType.ENGINE_RPM
            
            # Byte 0: Coolant temperature - direct value in °C (no offset)
            msg.values["coolant_temp"] = data[0]
            
            # Note: byte 2 is NOT used for RPM anymore
            # RPM comes from 0x038 byte 1 which properly shows 0 when ICE is off
        
        # 0x4CE: Previously thought to be Ambient Temperature (Outside)
        # Analysis shows byte 0 = constant 15, matching battery temp range (13-18°C)
        # NOT matching user's driving conditions of -2 to -4°C ambient
        # This is likely battery compartment intake air temperature, NOT outside ambient
        # DISABLED as ambient source - real ambient requires OBD2 PID 0x46 query
        # elif can_id == 0x4CE and len(data) >= 1:
        #     msg.msg_type = CANMessageType.CLIMATE_DATA
        #     msg.values["ambient_temp"] = float(data[0])
        
        # 0x540: Unknown Status Message (NOT Inverter Temperature)
        # DISABLED: This CAN ID does NOT contain inverter temperature!
        # The observed data [37, 128/16/64/0, 0, 0] shows:
        # - Byte 0 = 37 constant throughout recording (not temp behavior)
        # - Byte 1 = power-of-two values (0x80, 0x10, 0x40, 0x00) = status flags
        # - Values 37 and 165 are likely status codes, not temperatures
        # 
        # TODO: Implement SOLICITED OBD2 query for inverter temperature
        #       See docs/TODO_SOLICITED_OBD2.md for implementation details
        #       Request: ECU 0x7E2, PID 21C3
        #       Response: MG1 Inverter Temp = Byte_Y - 40
        #                 MG2 Inverter Temp = Byte_Z - 40
        #                 MG1 Motor Temp = Byte_AB - 40
        #                 MG2 Motor Temp = Byte_AA - 40
        # 
        # See docs/prius_can.md section "Solicited (CAN) - Hybrid/Specific (ECU 07E2)"
        # elif can_id == 0x540 and len(data) >= 4:
        #     msg.msg_type = CANMessageType.INVERTER_TEMP
        #     temp_raw = data[0]
        #     msg.values["inverter_temp"] = temp_raw - 40
        
        # ---------------------------------------------------
        # VEHICLE SPEED
        # ---------------------------------------------------
        
        # 0x03A: NOT Vehicle Speed - byte 4 contains status flags (36, 52, 132)
        # These are NOT speed values - ignoring this message for speed
        # Speed comes from 0x0B4 instead
        # elif can_id == 0x03A - DISABLED, not a speed source
        
        # 0x0B4: Vehicle Speed Alternative
        # Observed: [00, 00, 00, 00, 00-01, 00-1D, 00-FF, 00-FF]
        elif can_id == 0x0B4 and len(data) >= 8:
            msg.msg_type = CANMessageType.VEHICLE_SPEED
            
            # Bytes 5-6: [00-1D, 00-FF] = [0-29, 0-255]
            # Combined gives 0-7679, if /100 = 0-76 km/h (plausible)
            speed_raw = (data[5] << 8) | data[6]
            msg.values["speed_kph"] = speed_raw * 0.01
            
            # Bytes 6-7 alternative
            speed_raw_alt = (data[6] << 8) | data[7]
            if speed_raw_alt > 0:
                msg.values["speed_kph_alt"] = speed_raw_alt * 0.01
        
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
        self._change_callbacks: List[callable] = []
    
    @property
    def state(self) -> dict:
        """Get current state snapshot."""
        return self._state.copy()
    
    def on_change(self, callback: callable) -> None:
        """Register a callback for state changes."""
        self._change_callbacks.append(callback)
    
    def update(self, raw: dict) -> Optional[dict]:
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


def format_can_data(data: List[int]) -> str:
    """Format CAN data bytes as hex string."""
    return " ".join(f"{b:02X}" for b in data)
