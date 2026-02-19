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
    STEERING_ANGLE = auto()     # Steering angle (0x025)
    ACCELERATION = auto()       # Lateral/longitudinal acceleration (0x022, 0x023)
    WHEEL_PULSES = auto()       # Wheel pulse counters (0x0B1, 0x0B3)
    HEADLIGHT_STATUS = auto()   # Headlight state (0x57F)
    SOC_BARS_EVENT = auto()     # MFD SOC bars & events (0x529)
    YAW_RATE = auto()           # Yaw rate / vehicle dynamics (0x03A)
    # Solicited response types (OBD-II style)
    SOLICITED_RESPONSE = auto()        # Generic solicited response
    SOLICITED_ENGINE = auto()          # Response from Engine ECU (0x7E8)
    SOLICITED_HYBRID = auto()          # Response from Hybrid ECU (0x7EA)
    SOLICITED_HV_BATTERY = auto()      # Response from HV Battery ECU (0x7EB)


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
    0x03A: "YAW_RATE",             # Yaw rate / vehicle dynamics
    0x0B4: "VEHICLE_SPEED_ALT",    # Vehicle speed alternative
    0x120: "GEAR_POSITION",        # PRND gear position
    
    # Vehicle dynamics
    0x022: "LATERAL_ACCEL",        # Lateral + longitudinal acceleration
    0x023: "LONGITUDINAL_ACCEL",   # Longitudinal acceleration (alt)
    0x025: "STEERING_ANGLE",       # Steering wheel angle
    0x0B1: "FRONT_WHEEL_PULSES",   # Front wheel pulse counters
    0x0B3: "REAR_WHEEL_PULSES",    # Rear wheel pulse counters
    
    # Status & events
    0x529: "SOC_BARS_EVENT",       # MFD SOC bars, EV mode, warnings
    0x57F: "HEADLIGHT_STATUS",     # Headlight state
    
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
    
    # Solicited OBD-II/CAN Responses (Protocol v2.8.0)
    # These are responses to queries sent in Normal CAN mode
    0x7E8: "OBD2_RESP_ENGINE",     # Response from Engine ECU (0x7E0)
    0x7E9: "OBD2_RESP_TRANS",      # Response from Transmission ECU
    0x7EA: "OBD2_RESP_HYBRID",     # Response from Hybrid ECU (0x7E2)
    0x7EB: "OBD2_RESP_HV_BATTERY", # Response from HV Battery ECU (0x7E3)
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
        # Alternative unsolicited RPM sources identified but not yet implemented:
        #   - CAN 0x348 byte2 * 25: r=0.986, MAE=53 RPM
        #   - CAN 0x3C8 byte2 * 32: r=0.977, MAE=38 RPM
        elif can_id == 0x038 and len(data) >= 7:
            msg.msg_type = CANMessageType.ENGINE_STATUS
            
            # Byte 1: ICE running indicator only (0 = off, >0 = running)
            # RPM value is NOT extracted — use solicited OBD-II PID 010C
            rpm_byte = data[1]
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
        
        # 0x4CE: Battery compartment intake air temp (NOT outside ambient)
        # Requires OBD-II PID 0x46 for real ambient - see docs/prius_can.md
        
        # 0x540: Status flags (NOT inverter temperature)
        # Inverter temps require SOLICITED PID 21C3 to ECU 0x7E2
        # See docs/TODO_SOLICITED_OBD2.md
        
        # 0x5CC: Outside / Ambient Temperature (broadcast, unsolicited)
        # Observed: 3 bytes [24, xx, xx] where byte 0 = temp + 40 offset
        # Formula: ambient_temp = byte0 - 40 (°C)
        # Cross-validated against AVC-LAN 10C->310 outside temp messages
        elif can_id == 0x5CC and len(data) >= 1:
            msg.msg_type = CANMessageType.CLIMATE_DATA
            msg.values["ambient_temp"] = data[0] - 40
        
        # ---------------------------------------------------
        # VEHICLE DYNAMICS
        # ---------------------------------------------------
        
        # 0x025: Steering Angle (13ms period)
        # Docs: (256*A+B) unsigned, 12-bit signed interpretation
        # Straight-ahead offset is vehicle-specific (needs calibration)
        elif can_id == 0x025 and len(data) >= 2:
            msg.msg_type = CANMessageType.STEERING_ANGLE
            raw = (data[0] << 8) | data[1]
            # 12-bit signed: values > 2047 are negative
            if raw > 2047:
                raw -= 4096
            msg.values["steering_angle_raw"] = raw
            # Approximate degrees (scaling ~0.1 deg/count typical for Toyota)
            msg.values["steering_angle"] = raw * 0.1
        
        # 0x022: Lateral + Longitudinal Acceleration (13ms period)
        # Docs: (256*A+B) - 0x0200 for each axis
        elif can_id == 0x022 and len(data) >= 4:
            msg.msg_type = CANMessageType.ACCELERATION
            lat_raw = (data[0] << 8) | data[1]
            lon_raw = (data[2] << 8) | data[3]
            msg.values["lateral_accel_raw"] = lat_raw - 0x200
            msg.values["longitudinal_accel_raw"] = lon_raw - 0x200
            # TODO: Scale factor to m/s² needs real-world calibration
        
        # 0x023: Longitudinal Acceleration alternative (13ms period)
        elif can_id == 0x023 and len(data) >= 4:
            msg.msg_type = CANMessageType.ACCELERATION
            lon_raw = (data[0] << 8) | data[1]
            msg.values["longitudinal_accel_alt_raw"] = lon_raw - 0x200
        
        # 0x0B1: Front Wheel Pulses (13ms period, 185 pulses/rev)
        elif can_id == 0x0B1 and len(data) >= 4:
            msg.msg_type = CANMessageType.WHEEL_PULSES
            msg.values["front_right_pulses"] = (data[0] << 8) | data[1]
            msg.values["front_left_pulses"] = (data[2] << 8) | data[3]
            msg.values["wheel_position"] = "front"
        
        # 0x0B3: Rear Wheel Pulses (13ms period, 185 pulses/rev)
        elif can_id == 0x0B3 and len(data) >= 4:
            msg.msg_type = CANMessageType.WHEEL_PULSES
            msg.values["rear_right_pulses"] = (data[0] << 8) | data[1]
            msg.values["rear_left_pulses"] = (data[2] << 8) | data[3]
            msg.values["wheel_position"] = "rear"
        
        # 0x03A: Yaw Rate / Vehicle Dynamics (13ms period, highest-volume undecoded)
        # b0:b1 as 9-bit signed shows small values -7 to +27
        # Likely yaw rate or rotational velocity sensor
        elif can_id == 0x03A and len(data) >= 5:
            msg.msg_type = CANMessageType.YAW_RATE
            # 16-bit raw, offset by 0x200 (same pattern as 0x022)
            yaw_raw = (data[0] << 8) | data[1]
            msg.values["yaw_rate_raw"] = yaw_raw - 0x200
            # b4: status flags (dominant values 0x24, 0x34)
            msg.values["yaw_status"] = data[4]
        
        # ---------------------------------------------------
        # HEADLIGHTS & EVENTS
        # ---------------------------------------------------
        
        # 0x57F: Headlight Status (1050ms period)
        # Byte B (data[1]) bits 3-5 encode light state:
        #   0x00 = OFF, 0x10 = Parking lights, 0x30 = Low beam, 0x38 = High beam
        # Byte D (data[3]) bit 7: DRL/auto headlight sensor active
        # Byte A (data[0]): constant 0x68 (status/multiplexer)
        # Byte C (data[2]): constant 0x10 (status flag)
        elif can_id == 0x57F and len(data) >= 4:
            msg.msg_type = CANMessageType.HEADLIGHT_STATUS
            light_byte = data[1]
            msg.values["headlight_raw"] = light_byte
            msg.values["parking_lights"] = bool(light_byte & 0x10)
            msg.values["low_beam"] = bool(light_byte & 0x20) and bool(light_byte & 0x10)
            msg.values["high_beam"] = bool(light_byte & 0x08)
            msg.values["drl_active"] = bool(data[3] & 0x80)
            # Derive state string
            if light_byte == 0x38:
                msg.values["headlight_state"] = "HIGH"
            elif light_byte == 0x30:
                msg.values["headlight_state"] = "LOW"
            elif light_byte == 0x10:
                msg.values["headlight_state"] = "PARK"
            else:
                msg.values["headlight_state"] = "OFF"
        
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
        
        # ---------------------------------------------------
        # SOLICITED OBD-II RESPONSES (Gateway Protocol v2.8.0)
        # ---------------------------------------------------
        # These are responses from ECUs to solicited queries.
        # Format (single-frame): [Length, Mode+0x40, PID, DataBytes...]
        # Format (ISO-TP reassembled): [Mode+0x40, PID/Count, DataBytes...]
        # Minimum 2 bytes for ISO-TP 0-DTC response: [0x43, 0x00]
        
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
        elif len(data) >= 3:
            # Single-frame format: [Length, Mode, PID, Data...]
            mode = data[1]
            pid = data[2]
            payload = data[3:] if len(data) > 3 else []
        else:
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
        
        # Mode 0x61 = response to Mode 0x21 (Toyota Extended)
        elif mode == 0x61:
            if pid == 0xF3 and len(payload) >= 1:  # Injector Time
                msg.values["injector_time_ms"] = 0.128 * payload[0]
        
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
        elif len(data) >= 3:
            # Single-frame format: [Length, Mode, PID, Data...]
            mode = data[1]
            pid = data[2]
            payload = data[3:] if len(data) > 3 else []
        else:
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
        - Bytes 6-7: MG1 RPM = ((G*256)+H)-16383
        - Bytes 8-9: MG1 Torque = (I*256+J)/8 - 500
        - Byte 24: MG1 Inverter Temp = Y - 40
        - Byte 25: MG2 Inverter Temp = Z - 40
        - Byte 26: MG2 Motor Temp = AA - 40
        - Byte 27: MG1 Motor Temp = AB - 40
        """
        # Store payload length for debugging
        msg.values["pid_21c3_payload_len"] = len(payload)
        
        if len(payload) >= 2:
            msg.values["mg2_rpm"] = ((payload[0] * 256) + payload[1]) - 16383
        
        if len(payload) >= 4:
            msg.values["mg2_torque"] = ((payload[2] * 256) + payload[3]) / 8 - 500
        
        if len(payload) >= 8:
            msg.values["mg1_rpm"] = ((payload[6] * 256) + payload[7]) - 16383
        
        if len(payload) >= 10:
            msg.values["mg1_torque"] = ((payload[8] * 256) + payload[9]) / 8 - 500
        
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
        elif len(data) >= 3:
            # Single-frame format: [Length, Mode, PID, Data...]
            mode = data[1]
            pid = data[2]
            payload = data[3:] if len(data) > 3 else []
        else:
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
        - Bytes 3-4: Battery Power = (256*D+E)/100 - 327.68 kW
        - Bytes 5+: Block voltages in pairs
        """
        if len(payload) >= 1:
            msg.values["battery_soc_21ce"] = 0.5 * payload[0]
        
        if len(payload) >= 3:
            msg.values["battery_current_21ce"] = ((payload[1] * 256) + payload[2]) / 100 - 327.68
        
        if len(payload) >= 5:
            msg.values["battery_power_kw_21ce"] = ((payload[3] * 256) + payload[4]) / 100 - 327.68
        
        # Decode block voltages (14 blocks, 2 bytes each starting at byte 5)
        block_voltages = []
        for i in range(14):
            offset = 5 + (i * 2)
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
        Decode PID 21CF - Battery temps and delta SOC.
        
        - Bytes 0-1: Battery Air Intake Temp = (256*A+B)/100 - 327.68
        - Byte 3: Aux Battery Voltage = (0.2*D) - 25.6
        - Byte 4: Charge Limit = E - 64 kW
        - Byte 5: Discharge Limit = F - 64 kW
        - Byte 6: Delta SOC = 0.01 * G (%)
        - Byte 7: Fan Speed (0-6)
        """
        if len(payload) >= 2:
            msg.values["battery_air_intake_temp"] = ((payload[0] * 256) + payload[1]) / 100 - 327.68
        
        if len(payload) >= 4:
            msg.values["aux_battery_voltage_21cf"] = (0.2 * payload[3]) - 25.6
        
        if len(payload) >= 5:
            msg.values["charge_limit_kw"] = payload[4] - 64
        
        if len(payload) >= 6:
            msg.values["discharge_limit_kw"] = payload[5] - 64
        
        if len(payload) >= 7:
            # This is the REAL delta SOC (not from unsolicited 0x3CB)
            msg.values["delta_soc"] = 0.01 * payload[6]
        
        if len(payload) >= 8:
            msg.values["battery_fan_speed"] = payload[7]
    
    def _decode_pid_21d0(self, msg: CANMessage, payload: list[int]) -> None:
        """
        Decode PID 21D0 - Internal resistance and voltage delta.
        
        Contains internal resistance for blocks 1-14 and NiMH voltage delta.
        """
        # Internal resistance values (14 blocks, 1 byte each)
        resistances = []
        for i in range(min(14, len(payload))):
            resistance_ohm = 0.001 * payload[i]  # 0-10 Ohm
            resistances.append(resistance_ohm)
        
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
