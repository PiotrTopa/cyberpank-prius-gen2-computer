"""
Gateway Adapter - Bridge between Gateway protocol and State Store.

This adapter:
1. Receives raw messages from Gateway (AVC-LAN, CAN, serial)
2. Decodes and classifies messages
3. Dispatches appropriate Actions to the Store
4. Listens for UI actions and sends commands to Gateway

Flow:
    Gateway -> GatewayAdapter -> Store (via Actions)
    Store (UI Actions) -> GatewayAdapter -> Gateway
"""

import logging
import time
from typing import Callable, Optional, Dict, Any, List
from dataclasses import dataclass
from enum import Enum, auto

from ..state.store import Store, StateSlice
from ..state.actions import (
    Action, ActionType, ActionSource, BatchAction,
    SetVolumeAction, SetBassAction, SetMidAction, SetTrebleAction,
    SetBalanceAction, SetFaderAction, SetMuteAction,
    SetTargetTempAction, SetFanSpeedAction, SetACAction, SetAutoModeAction,
    SetRecirculationAction, SetAirDirectionAction, SetOutsideTempAction,
    SetReadyModeAction, SetParkModeAction, SetICERunningAction,
    SetThrottlePositionAction, SetBrakePressedAction, SetFuelLevelAction,
    SetFuelFlowAction, SetEnergyFlowFlagsAction, SetBatteryMaxTempAction,
    SetBatterySOCAction, SetChargingStateAction,
    SetConnectionStateAction,
    SetSpeedAction, SetRPMAction, SetICECoolantTempAction, SetInverterTempAction,
    SetBatteryVoltageAction, SetBatteryCurrentAction, SetBatteryTempAction,
    SetBatteryDeltaSOCAction, SetGearAction,
    AVCButtonPressAction, AVCTouchEventAction,
)
from ..comm.avc_decoder import (
    AVCDecoder, AVCMessage, AVCMessageType,
    parse_button_event, parse_touch_event,
    ButtonEvent, TouchEvent
)
from ..comm.avc_commands import AVCCommandGenerator
from ..comm.can_decoder import CANDecoder, CANMessageType
from ..state.app_state import GearPosition

logger = logging.getLogger(__name__)


class MessageType(Enum):
    """Types of messages from Gateway."""
    GATEWAY_READY = auto()
    GATEWAY_ERROR = auto()
    AVC_LAN = auto()
    CAN = auto()
    UNKNOWN = auto()


@dataclass
class GatewayMessage:
    """Parsed gateway message."""
    type: MessageType
    timestamp: float
    raw: dict
    decoded: Optional[Any] = None


class GatewayAdapter:
    """
    Adapter between Gateway and State Store.
    
    Responsibilities:
    - Parse incoming gateway messages (NDJSON)
    - Dispatch state updates to Store
    - Generate outgoing commands from UI actions
    - Handle protocol-specific encoding/decoding
    
    Usage:
        store = Store()
        adapter = GatewayAdapter(store)
        
        # Process incoming message
        adapter.process_message(raw_json_dict)
        
        # Adapter automatically handles UI->Gateway via middleware
    """
    
    def __init__(self, store: Store, send_callback: Optional[Callable[[dict], None]] = None):
        """
        Initialize adapter.
        
        Args:
            store: Application state store
            send_callback: Function to send commands to Gateway
        """
        self._store = store
        self._send_callback = send_callback
        
        # Protocol decoders/encoders
        self._avc_decoder = AVCDecoder()
        self._avc_commands = AVCCommandGenerator()
        self._can_decoder = CANDecoder()
        
        # Message logging callback
        self._message_log_callback: Optional[Callable[[GatewayMessage, str], None]] = None
        
        # Statistics
        self._stats = {
            "messages_received": 0,
            "avc_messages": 0,
            "can_messages": 0,
            "errors": 0,
        }
        
        # Analysis mode for reverse engineering
        self._analysis_mode = False
        self._last_seq = None  # Track sequence for context logging
        
        # Register as middleware to handle UI->Gateway
        store.add_middleware(self._handle_outgoing_action)
        
    def set_send_callback(self, callback: Callable[[dict], None]) -> None:
        """Set callback for sending commands to Gateway."""
        self._send_callback = callback
    
    def set_analysis_mode(self, enabled: bool) -> None:
        """
        Enable/disable analysis mode for reverse engineering.
        
        In analysis mode, the adapter prints detailed information about:
        - Button press events with raw codes
        - Touch events with coordinates
        - Energy-related packets (A00->258, 210->490)
        
        Args:
            enabled: True to enable analysis mode
        """
        self._analysis_mode = enabled
        
    def set_message_log_callback(
        self, 
        callback: Callable[[GatewayMessage, str], None]
    ) -> None:
        """
        Set callback for message logging/debugging.
        
        Args:
            callback: Function(message, direction) where direction is "IN" or "OUT"
        """
        self._message_log_callback = callback
        
    def process_message(self, raw: dict) -> None:
        """
        Process a raw gateway message.
        
        Args:
            raw: Raw JSON message dict from Gateway
        """
        self._stats["messages_received"] += 1
        
        # Classify message type
        msg_type = self._classify_message(raw)
        
        message = GatewayMessage(
            type=msg_type,
            timestamp=time.time(),
            raw=raw
        )
        
        # Log if callback set
        if self._message_log_callback:
            self._message_log_callback(message, "IN")
        
        # Route to appropriate handler
        if msg_type == MessageType.GATEWAY_READY:
            self._handle_gateway_ready(raw)
        elif msg_type == MessageType.AVC_LAN:
            self._handle_avc_message(raw, message)
        elif msg_type == MessageType.CAN:
            self._handle_can_message(raw)
        elif msg_type == MessageType.GATEWAY_ERROR:
            self._handle_gateway_error(raw)
            
    def _classify_message(self, raw: dict) -> MessageType:
        """Classify message type from raw data."""
        msg_id = raw.get("id")
        
        # id=0: System/status messages
        if msg_id == 0:
            d = raw.get("d", {})
            if isinstance(d, dict):
                if "GATEWAY_READY" in d.get("msg", ""):
                    return MessageType.GATEWAY_READY
                if "error" in d.get("msg", "").lower():
                    return MessageType.GATEWAY_ERROR
            return MessageType.UNKNOWN
        
        # id=1: CAN messages
        if msg_id == 1:
            return MessageType.CAN
        
        # id=2: AVC-LAN messages
        if msg_id == 2:
            return MessageType.AVC_LAN
        
        return MessageType.UNKNOWN
    
    def _handle_gateway_ready(self, raw: dict) -> None:
        """Handle GATEWAY_READY message."""
        d = raw.get("d", {})
        version = d.get("ver", "unknown")
        can_ready = d.get("can") == "CAN_READY"
        
        logger.info(f"Gateway ready: v{version}, CAN={can_ready}")
        
        self._store.dispatch(SetConnectionStateAction(
            connected=True,
            gateway_version=version,
            source=ActionSource.GATEWAY
        ))
        
    def _handle_gateway_error(self, raw: dict) -> None:
        """Handle gateway error message."""
        self._stats["errors"] += 1
        d = raw.get("d", {})
        error_msg = d.get("msg", "Unknown error")
        logger.error(f"Gateway error: {error_msg}")
        
    def _handle_avc_message(self, raw: dict, gateway_msg: GatewayMessage) -> None:
        """
        Handle AVC-LAN message.
        
        Decodes the message and dispatches appropriate actions.
        """
        self._stats["avc_messages"] += 1
        
        # Decode AVC message
        avc_msg = self._avc_decoder.decode_message(raw)
        if not avc_msg:
            return
            
        gateway_msg.decoded = avc_msg
        
        # Get sequence number for analysis mode
        seq = raw.get("seq")
        
        # Classify and extract data
        classification = self._avc_decoder.classify_message(avc_msg)
        
        # Dispatch actions based on message type
        actions = self._avc_to_actions(avc_msg, classification, seq)
        
        if actions:
            if len(actions) == 1:
                self._store.dispatch(actions[0])
            else:
                self._store.dispatch(BatchAction(actions, ActionSource.GATEWAY))
    
    def _avc_to_actions(
        self, 
        msg: AVCMessage, 
        classification: str,
        seq: Optional[int] = None
    ) -> List[Action]:
        """
        Convert AVC message to state actions.
        
        Args:
            msg: Decoded AVC message
            classification: Message classification string
            seq: Message sequence number (for analysis mode logging)
            
        Returns:
            List of actions to dispatch
        """
        actions: List[Action] = []
        data = msg.data
        
        # Volume messages (typically from 0x190 -> 0x110)
        if "volume" in classification.lower() and len(data) >= 2:
            volume = data[1] & 0x3F  # 6-bit volume
            actions.append(SetVolumeAction(volume, ActionSource.GATEWAY))

        # Climate Status (0x130 Status Broadcast) - NEW
        if msg.msg_type == AVCMessageType.CLIMATE_STATUS:
            # Ambient/Outside Temp
            ambient = msg.values.get("ambient_temp_c")
            if ambient is not None:
                actions.append(SetOutsideTempAction(ambient, ActionSource.GATEWAY))
            
            # AC Modes
            # TODO: Add ActionType.SET_CLIMATE_MODE logic here once Mode enum is shared or mapped
            # For now just recirc and AC on/off might be extractable
            recirc = msg.values.get("recirc")
            if recirc is not None:
                 actions.append(SetRecirculationAction(recirc, ActionSource.GATEWAY))

        # Climate messages (0x10C -> 0x310) (Legacy/Alternative)
        elif msg.master_addr == 0x10C and msg.slave_addr == 0x310:
            if len(data) >= 8:
                # Parse climate data (based on Prius Gen 2 protocol analysis)
                # Pattern: [00, 00, 00, 00, b4, b5, b6, b7]
                # 
                # Message types identified:
                #   - b6=0x90 (144): Outside temperature message
                #     b4 = outside temp encoded, very stable (values 8-9 consistently)
                #   - b6=0x00 or b6=0x60: Target/set temperature message  
                #
                # Outside temperature (only when b6=0x90, indicating temp message)
                # Using byte4 which shows higher inertia (smoother changes) than byte5
                # Empirically derived formula: temp_C = (byte4 - 18) / 2
                # Examples from real data:
                #   byte4=8, b6=0x90 → (8-18)/2 = -5°C (user drove in -4°C)
                #   byte4=9, b6=0x90 → (9-18)/2 = -4.5°C
                if data[6] == 0x90 and data[4] > 0:
                    outside_temp = (data[4] - 18) / 2.0
                    actions.append(SetOutsideTempAction(outside_temp, ActionSource.GATEWAY))
        
        # Analysis mode: Energy data candidate (A00 -> 258)
        # 32-byte packets with variable data in bytes 12-31
        elif msg.master_addr == 0xA00 and msg.slave_addr == 0x258:
            if self._analysis_mode:
                data_hex = " ".join(f"{b:02X}" for b in data)
                seq_str = f"[{seq:>4}]" if seq is not None else ""
                print(f"[NRG] {seq_str} A00->258 ({len(data)}B): {data_hex}", flush=True)
                
                # Detailed byte analysis for longer packets
                if len(data) >= 20:
                    # Show variable bytes that might contain energy data
                    var_bytes = data[12:] if len(data) > 12 else []
                    if var_bytes:
                        var_hex = " ".join(f"{b:02X}" for b in var_bytes)
                        print(f"      Variable bytes[12+]: {var_hex}", flush=True)
        
        # Analysis mode: ICE status (210 -> 490)
        # Byte[2]: C1=OFF, C8=RUNNING
        elif msg.master_addr == 0x210 and msg.slave_addr == 0x490:
            if self._analysis_mode:
                data_hex = " ".join(f"{b:02X}" for b in data)
                seq_str = f"[{seq:>4}]" if seq is not None else ""
                ice_status = "UNKNOWN"
                if len(data) > 2:
                    if data[2] == 0xC1:
                        ice_status = "OFF"
                    elif data[2] == 0xC8:
                        ice_status = "RUNNING"
                    else:
                        ice_status = f"0x{data[2]:02X}"
                print(f"[ICE] {seq_str} 210->490 ICE={ice_status}: {data_hex}", flush=True)
                
        # Power/READY state (0x400 heartbeat responses)
        elif "heartbeat" in classification.lower():
            # Heartbeat indicates communication is alive
            # Connection state already updated on message receive
            pass
            
        # Button press (040 -> 200)
        elif classification == "button_press":
            btn = parse_button_event(data)
            if btn:
                # Log button press to console
                status = "PRESS" if btn.is_press else "RELEASE"
                data_hex = " ".join(f"{b:02X}" for b in btn.raw_data)
                
                if self._analysis_mode:
                    seq_str = f"[{seq:>4}]" if seq is not None else ""
                    print(f"[BTN] {seq_str} {status}: {btn.button_name} (code=0x{btn.button_code:04X}, mod=0x{btn.modifier:02X}, suffix=0x{btn.suffix:02X})", flush=True)
                    print(f"      Raw: {data_hex}", flush=True)
                else:
                    print(f"[BTN] {status}: {btn.button_name} (code=0x{btn.button_code:04X}, suffix=0x{btn.suffix:02X}) [{data_hex}]", flush=True)
                
                # Dispatch action
                actions.append(AVCButtonPressAction(
                    button_code=btn.button_code,
                    modifier=btn.modifier,
                    suffix=btn.suffix,
                    is_press=btn.is_press,
                    raw_data=btn.raw_data,
                    button_name=btn.button_name,
                    source=ActionSource.GATEWAY
                ))

        # Touch event (000 -> 114)
        elif classification == "touch_event":
            touch = parse_touch_event(data)
            if touch:
                # Log touch event to console
                data_hex = " ".join(f"{b:02X}" for b in touch.raw_data)
                
                if self._analysis_mode:
                    seq_str = f"[{seq:>4}]" if seq is not None else ""
                    print(f"[TCH] {seq_str} {touch.touch_type.upper()}: x={touch.x:>3}, y={touch.y:>3} (conf={touch.confidence})", flush=True)
                    print(f"      Raw: {data_hex}", flush=True)
                else:
                    print(f"[TCH] {touch.touch_type.upper()}: x={touch.x}, y={touch.y} (conf={touch.confidence}) [{data_hex}]", flush=True)
                
                # Dispatch action
                actions.append(AVCTouchEventAction(
                    x=touch.x,
                    y=touch.y,
                    touch_type=touch.touch_type,
                    raw_data=touch.raw_data,
                    source=ActionSource.GATEWAY
                ))
        
        return actions
    
    def _handle_can_message(self, raw: dict) -> None:
        """
        Handle CAN message using CANDecoder.
        
        CAN messages provide real-time vehicle data:
        - Speed, RPM
        - Battery SOC
        - Power flow
        - Gear position
        """
        self._stats["can_messages"] += 1
        
        # Use CAN decoder
        msg = self._can_decoder.decode(raw)
        if not msg:
            return
        
        # Convert decoded message to actions
        actions = self._can_message_to_actions(msg)
        
        if actions:
            if len(actions) == 1:
                self._store.dispatch(actions[0])
            else:
                self._store.dispatch(BatchAction(actions, ActionSource.GATEWAY))
    
    def _can_message_to_actions(self, msg) -> List[Action]:
        """
        Convert decoded CAN message to state actions.
        
        Args:
            msg: Decoded CANMessage from CANDecoder
            
        Returns:
            List of actions to dispatch
        """
        actions: List[Action] = []
        
        # HV Battery State of Charge (from 0x03B)
        if msg.msg_type == CANMessageType.HV_BATTERY:
            soc = msg.values.get("soc")
            # Only update SOC if value is in realistic range (10-95%)
            # Prius keeps SOC between ~40-80% normally, but allow wider range
            if soc is not None and 10 <= soc <= 95:
                actions.append(SetBatterySOCAction(soc / 100.0, ActionSource.GATEWAY))
            
            # Battery voltage if available
            voltage = msg.values.get("voltage")
            if voltage is not None:
                actions.append(SetBatteryVoltageAction(voltage, ActionSource.GATEWAY))
            
            # Battery current if available (from 0x03B)
            current = msg.values.get("current")
            if current is not None:
                actions.append(SetBatteryCurrentAction(current, ActionSource.GATEWAY))
            
            # Battery temperature if available
            temp = msg.values.get("battery_temp")
            if temp is not None:
                actions.append(SetBatteryTempAction(temp, ActionSource.GATEWAY))
        
        # HV Battery Power Flow (from 0x3CB)
        elif msg.msg_type == CANMessageType.HV_BATTERY_POWER:
            # SOC from 0x3CB (primary source, more reliable)
            soc = msg.values.get("soc")
            if soc is not None:
                actions.append(SetBatterySOCAction(soc / 100.0, ActionSource.GATEWAY))
            
            # Delta SOC - difference between min/max cell blocks (for diagnostics)
            # TODO: Delta SOC requires SOLICITED OBD2 query to ECU 0x7E2 with PID 21CF
            #       See docs/TODO_SOLICITED_OBD2.md for implementation details
            #       Formula: delta_soc = 0.01 * Byte_G (range 0-60%)
            #       Current state: 0x3CB does NOT contain delta SOC (byte 2 is SOC high byte)
            delta_soc = msg.values.get("delta_soc")
            if delta_soc is not None:
                actions.append(SetBatteryDeltaSOCAction(delta_soc, ActionSource.GATEWAY))
            
            is_charging = msg.values.get("is_charging", False)
            power_kw = msg.values.get("power_kw", 0)
            
            # Determine charging/discharging with some threshold
            charging = is_charging and abs(power_kw) > 0.5
            discharging = not is_charging and abs(power_kw) > 0.5
            
            actions.append(SetChargingStateAction(
                charging=charging, 
                discharging=discharging,
                source=ActionSource.GATEWAY
            ))
            
            # Battery temperature from 0x3CB
            # Data shows values 13-18°C which are realistic and stable
            # This is the PRIMARY and ONLY reliable source for battery temperature
            temp = msg.values.get("battery_temp")
            if temp is not None and -20 <= temp <= 60:
                actions.append(SetBatteryTempAction(temp, ActionSource.GATEWAY))
            
            # Max Battery Temp (Byte 5)
            temp_max = msg.values.get("battery_temp_max")
            if temp_max is not None:
                actions.append(SetBatteryMaxTempAction(temp_max, ActionSource.GATEWAY))
        
        # Energy Flow (0x3B6)
        elif msg.msg_type == CANMessageType.ENERGY_FLOW:
            # Bytes 5 & 6 are raw
            b5 = msg.values.get("flow_byte_5", 0)
            b6 = msg.values.get("flow_byte_6", 0)
            
            # Decoding bitmask (Docs: Byte 5 & 6)
            # No Flow (0x00)
            # Engine -> Wheels: 0x08 ?? (Need verification)
            # Battery -> Motor: 0x10 ??
            # Motor -> Battery: 0x20 ??
            # Engine -> Battery: 0x40 ??
            
            # For now, simplistic mapping based on observation or standard bits
            # Usually these are individual bits.
            # Assuming:
            # Bit 0 (0x01) or 1?
            # Let's trust the decoder if it had logic, but I implemented raw pass.
            
            # Implementation Strategy: 
            # We will interpret common bits. If uncertain, we might need a dedicated tool to find them.
            # But the user doc says: "Engine -> Wheels" ... "Battery -> Motor".
            # Reference: Gen2 energy flow is often:
            # Byte 5:
            # 0x0C = Engine to Wheels ?
            # 0x10 = Battery to Wheel (Electric)
            # 0x20 = Regen
            # 0x40 = Engine to Battery (Charge)
            
            # Let's try:
            # Engine->Wheels: 0x04 or 0x08 or 0x80
            # Common Toyota:
            # 0->1 (Batt->Motor): 0x80?
            # 1->2 (Motor->Batt): 0x20?
            
            # Let's map based on what I put in app_state.py as flags using simple heuristic
            # If doc says "Byte 5 and 6 act as bitmask", and we have 0x3B6.
            
            # Let's map what we can.
            actions.append(SetEnergyFlowFlagsAction(
                engine_to_wheels=bool(b5 & 0x08), # Guess
                battery_to_motor=bool(b5 & 0x10), # Guess
                motor_to_battery=bool(b5 & 0x20), # Guess
                engine_to_battery=bool(b5 & 0x40), # Guess
                battery_to_wheels=bool(b5 & 0x80), # Guess
                source=ActionSource.GATEWAY
            ))

        # Fuel Consumption (0x520)
        elif msg.msg_type == CANMessageType.FUEL_CONSUMPTION:
            injector_time = msg.values.get("injector_time", 0)
            
            # Calculate Fuel Flow (L/h)
            # Factor 0.0001 derived from user feedback (55L/100km @ 75km/h was 10x too high)
            # Corrected logic assumes injector_time * 0.0001 = L/h
            if injector_time > 0:
                 flow_rate = injector_time * 0.0001
                 actions.append(SetFuelFlowAction(flow_rate, ActionSource.GATEWAY))
            else:
                 actions.append(SetFuelFlowAction(0.0, ActionSource.GATEWAY))

        # HV Battery Temperature (from 0x348) - DISABLED
        # Analysis shows 0x348 byte 2 is NOT a temperature value:
        # - 59% of readings decode as -40°C (byte=0x00, invalid marker)
        # - Rest range from -40 to 82°C with wild jumps (impossible for battery)
        # - Causes display instability showing 42°C, 35°C, -3°C etc.
        # Use 0x3CB exclusively for stable battery temperature
        elif msg.msg_type == CANMessageType.HV_BATTERY_TEMP:
            # Do NOT send temperature actions from 0x348 - data is unreliable
            pass
        
        # Inverter Temperature
        # TODO: Inverter temperature requires SOLICITED OBD2 query to ECU 0x7E2 with PID 21C3
        #       See docs/TODO_SOLICITED_OBD2.md for implementation details
        #       Formula: MG1_Temp = Byte_Y - 40, MG2_Temp = Byte_Z - 40
        #       Current state: NO unsolicited CAN source available
        elif msg.msg_type == CANMessageType.INVERTER_TEMP:
            temp = msg.values.get("inverter_temp")
            if temp is not None and -10 <= temp <= 80:
                actions.append(SetInverterTempAction(temp, ActionSource.GATEWAY))
        
        # Vehicle Speed (from 0x03A or 0x0B4)
        elif msg.msg_type == CANMessageType.VEHICLE_SPEED:
            speed = msg.values.get("speed_kph") or msg.values.get("speed")
            if speed is not None and speed < 300:  # Sanity check
                actions.append(SetSpeedAction(speed, ActionSource.GATEWAY))
        
        # Coolant Temperature only (from 0x039)
        # NOTE: RPM from 0x039 is unreliable - it shows values when ICE is off
        elif msg.msg_type == CANMessageType.ENGINE_RPM:
            # Coolant temperature from 0x039 byte 0 (direct °C, no offset)
            # Only update if in realistic range for running engine
            coolant_temp = msg.values.get("coolant_temp")
            if coolant_temp is not None and 40 <= coolant_temp <= 120:
                actions.append(SetICECoolantTempAction(coolant_temp, ActionSource.GATEWAY))
        
        # Engine Status and RPM from 0x038 (PRIMARY RPM SOURCE)
        # RPM from 0x038 byte 1 is reliable: 0 = ICE off, >0 = ICE running
        elif msg.msg_type == CANMessageType.ENGINE_STATUS:
            # RPM from 0x038 - this is the reliable source
            rpm = msg.values.get("rpm")
            if rpm is not None:
                actions.append(SetRPMAction(rpm, ActionSource.GATEWAY))
            
            # ICE running status (determined by RPM byte > 0 in decoder)
            ice_running = msg.values.get("ice_running")
            if ice_running is not None:
                actions.append(SetICERunningAction(ice_running, ActionSource.GATEWAY))
                actions.append(SetReadyModeAction(True, ActionSource.GATEWAY))
        
        # Pedals
        elif msg.msg_type == CANMessageType.PEDAL_POSITION:
            if "throttle" in msg.values:
                actions.append(SetThrottlePositionAction(msg.values["throttle"], ActionSource.GATEWAY))
            if "brake" in msg.values:
                actions.append(SetBrakePressedAction(msg.values["brake"], ActionSource.GATEWAY))
        
        # Fuel Level
        elif msg.msg_type == CANMessageType.FUEL_LEVEL:
            if "fuel_level" in msg.values:
                actions.append(SetFuelLevelAction(msg.values["fuel_level"], ActionSource.GATEWAY))
        
        # Climate Data (Ambient Temp)
        elif msg.msg_type == CANMessageType.CLIMATE_DATA:
            ambient = msg.values.get("ambient_temp")
            if ambient is not None and -50 <= ambient <= 80:
                actions.append(SetOutsideTempAction(ambient, ActionSource.GATEWAY))
        
        # Gear Position
        elif msg.msg_type == CANMessageType.GEAR_POSITION:
            gear_str = msg.values.get("gear")
            if gear_str:
                gear = GearPosition.PARK
                if gear_str == "R":
                    gear = GearPosition.REVERSE
                elif gear_str == "N":
                    gear = GearPosition.NEUTRAL
                elif gear_str == "D":
                    gear = GearPosition.DRIVE
                elif gear_str == "B":
                    gear = GearPosition.B
                elif gear_str == "P":
                    gear = GearPosition.PARK
                
                actions.append(SetGearAction(gear, ActionSource.GATEWAY))
                # Legacy SetParkModeAction is not needed as SetGearAction handles the specific gear
                # and Store updates state.vehicle.gear accordingly.
                # is_parked property is derived from gear state.
                
        return actions
    
    def _can_to_actions(self, can_id: int, data: List[int]) -> List[Action]:
        """
        Convert CAN message to state actions (legacy method).
        
        Deprecated: Use _can_message_to_actions with CANDecoder instead.
        
        Args:
            can_id: CAN message ID
            data: CAN data bytes
            
        Returns:
            List of actions to dispatch
        """
        actions: List[Action] = []
        
        # Example CAN ID mappings for Prius Gen 2
        # 0x3C8: HV Battery SOC
        if can_id == 0x3C8 and len(data) >= 5:
            soc_raw = data[4]
            soc = soc_raw / 255.0  # Normalize to 0-1
            actions.append(SetBatterySOCAction(soc, ActionSource.GATEWAY))
            
        # 0x3CB: Battery power flow
        elif can_id == 0x3CB and len(data) >= 4:
            # Positive = charging, Negative = discharging
            power_raw = (data[0] << 8) | data[1]
            if power_raw > 0x7FFF:
                power_raw -= 0x10000  # Convert to signed
            
            charging = power_raw > 100  # Some threshold
            discharging = power_raw < -100
            actions.append(SetChargingStateAction(
                charging=charging, 
                discharging=discharging,
                source=ActionSource.GATEWAY
            ))
        
        return actions
    
    def _handle_outgoing_action(self, action: Action, store: Store) -> None:
        """
        Middleware: Handle actions that should go to Gateway.
        
        Only processes actions with source=UI (user-initiated).
        """
        if action.source != ActionSource.UI:
            return
        
        # Generate gateway command
        command = self._action_to_command(action)
        
        if command:
            # Always log if callback is set (even without send_callback)
            if self._message_log_callback:
                msg = GatewayMessage(
                    type=MessageType.AVC_LAN,
                    timestamp=time.time(),
                    raw=command
                )
                self._message_log_callback(msg, "OUT")
            
            # Send only if we have a real gateway connection
            if self._send_callback:
                self._send_callback(command)
            else:
                import json
                raw_json = json.dumps(command)
                logger.debug(f"No send callback, logged but not sent: {action}\n  → NDJSON: {raw_json}")
    
    def _action_to_command(self, action: Action) -> Optional[dict]:
        """
        Convert state action to Gateway command.
        
        Args:
            action: Action from UI
            
        Returns:
            Gateway command dict or None
        """
        cmd = None
        
        # Audio commands
        if isinstance(action, SetVolumeAction):
            cmd = self._avc_commands.set_volume(action.volume)
        elif isinstance(action, SetBassAction):
            cmd = self._avc_commands.set_bass(action.bass)
        elif isinstance(action, SetMidAction):
            cmd = self._avc_commands.set_mid(action.mid)
        elif isinstance(action, SetTrebleAction):
            cmd = self._avc_commands.set_treble(action.treble)
        elif isinstance(action, SetBalanceAction):
            cmd = self._avc_commands.set_balance(action.balance)
        elif isinstance(action, SetFaderAction):
            cmd = self._avc_commands.set_fader(action.fader)
        elif isinstance(action, SetMuteAction):
            cmd = self._avc_commands.mute_toggle()
        
        # Climate commands
        elif isinstance(action, SetTargetTempAction):
            cmd = self._avc_commands.set_target_temp(action.temp)
        elif isinstance(action, SetFanSpeedAction):
            cmd = self._avc_commands.set_fan_speed(action.speed)
        elif isinstance(action, SetACAction):
            cmd = self._avc_commands.climate_ac_toggle()
        elif isinstance(action, SetAutoModeAction):
            cmd = self._avc_commands.climate_auto_toggle()
        elif isinstance(action, SetRecirculationAction):
            cmd = self._avc_commands.climate_recirc_toggle()
        elif isinstance(action, SetAirDirectionAction):
            cmd = self._avc_commands.set_air_direction(action.direction)
        
        # Convert AVCLANCommand to gateway format if we got a command
        if cmd is not None:
            return cmd.to_gateway_format()
        return None
    
    @property
    def stats(self) -> Dict[str, int]:
        """Get message statistics."""
        return self._stats.copy()
