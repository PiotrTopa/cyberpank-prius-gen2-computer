"""
Ingress Controller - Bridges InputPort to Virtual Twin State.

The Ingress Controller is responsible for:
1. Polling messages from the InputPort
2. Decoding protocol-specific data (AVC-LAN, CAN, RS485)
3. Converting messages to domain Actions
4. Dispatching Actions to the Store

This provides a clean separation between IO and state management.
"""

import logging
import time
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass

from .ports import (
    InputPort, RawMessage, MessageCategory,
    DEVICE_SYSTEM, DEVICE_CAN, DEVICE_AVC, DEVICE_SATELLITE_BASE
)
from ..state.store import Store
from ..state.actions import (
    Action, ActionSource, BatchAction,
    SetVolumeAction, SetBassAction, SetMidAction, SetTrebleAction,
    SetBalanceAction, SetFaderAction, SetMuteAction,
    SetTargetTempAction, SetFanSpeedAction, SetACAction, SetAutoModeAction,
    SetRecirculationAction, SetOutsideTempAction,
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
)
from ..comm.can_decoder import CANDecoder, CANMessageType
from ..state.app_state import GearPosition

logger = logging.getLogger(__name__)


@dataclass
class IngressStats:
    """Statistics for ingress processing."""
    messages_received: int = 0
    messages_processed: int = 0
    avc_messages: int = 0
    can_messages: int = 0
    satellite_messages: int = 0
    system_messages: int = 0
    errors: int = 0
    last_message_time: float = 0.0


class IngressController:
    """
    Ingress controller - bridges InputPort to Virtual Twin.
    
    Usage:
        store = Store()
        input_port = FileInputPort("recording.ndjson")
        
        ingress = IngressController(store, input_port)
        ingress.start()
        
        # In main loop:
        ingress.update()  # Process pending messages
    """
    
    def __init__(self, store: Store, input_port: InputPort):
        """
        Initialize ingress controller.
        
        Args:
            store: Virtual Twin state store
            input_port: Input source for raw messages
        """
        self._store = store
        self._input_port = input_port
        
        # Protocol decoders
        self._avc_decoder = AVCDecoder()
        self._can_decoder = CANDecoder()
        
        # Satellite message handlers (device_id -> handler)
        self._satellite_handlers: Dict[int, Callable[[dict], List[Action]]] = {}
        
        # Statistics
        self._stats = IngressStats()
        
        # Analysis/debug mode
        self._analysis_mode = False
        
        # Message logging callback
        self._message_log_callback: Optional[Callable[[RawMessage, str], None]] = None
    
    @property
    def stats(self) -> IngressStats:
        """Get ingress statistics."""
        return self._stats
    
    @property
    def input_port(self) -> InputPort:
        """Get the input port."""
        return self._input_port
    
    def set_analysis_mode(self, enabled: bool) -> None:
        """Enable/disable analysis mode for debugging."""
        self._analysis_mode = enabled
    
    def set_message_log_callback(
        self, 
        callback: Callable[[RawMessage, str], None]
    ) -> None:
        """Set callback for message logging (called with message and "IN")."""
        self._message_log_callback = callback
    
    def register_satellite_handler(
        self,
        device_id: int,
        handler: Callable[[dict], List[Action]]
    ) -> None:
        """
        Register a handler for a specific satellite device.
        
        Args:
            device_id: Satellite device ID (100+)
            handler: Function that takes message data and returns Actions
        """
        if device_id < DEVICE_SATELLITE_BASE:
            raise ValueError(f"Satellite device ID must be >= {DEVICE_SATELLITE_BASE}")
        self._satellite_handlers[device_id] = handler
        logger.info(f"Registered satellite handler for device {device_id}")
    
    def start(self) -> bool:
        """
        Start the ingress controller.
        
        Starts the input port if not already started.
        
        Returns:
            True if started successfully
        """
        if not self._input_port.is_connected():
            if not self._input_port.start():
                logger.error("Failed to start input port")
                return False
        
        logger.info(f"Ingress controller started with {self._input_port.name}")
        return True
    
    def stop(self) -> None:
        """Stop the ingress controller and input port."""
        self._input_port.stop()
        logger.info("Ingress controller stopped")
    
    def update(self, max_messages: int = 100) -> int:
        """
        Process pending messages from input port.
        
        Should be called each frame/tick of the main loop.
        
        Args:
            max_messages: Maximum messages to process per update
            
        Returns:
            Number of messages processed
        """
        processed = 0
        
        while processed < max_messages:
            msg = self._input_port.poll()
            if msg is None:
                break
            
            self._process_message(msg)
            processed += 1
        
        return processed
    
    def _process_message(self, msg: RawMessage) -> None:
        """
        Process a single raw message.
        
        Routes to appropriate decoder based on device ID.
        """
        self._stats.messages_received += 1
        self._stats.last_message_time = time.time()
        
        # Log if callback set
        if self._message_log_callback:
            self._message_log_callback(msg, "IN")
        
        try:
            # Route based on message category
            if msg.category == MessageCategory.SYSTEM:
                self._handle_system_message(msg)
            elif msg.category == MessageCategory.CAN:
                self._handle_can_message(msg)
            elif msg.category == MessageCategory.AVC_LAN:
                self._handle_avc_message(msg)
            elif msg.category == MessageCategory.SATELLITE:
                self._handle_satellite_message(msg)
            else:
                logger.debug(f"Unknown message category: {msg}")
            
            self._stats.messages_processed += 1
            
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            self._stats.errors += 1
    
    def _handle_system_message(self, msg: RawMessage) -> None:
        """Handle system/gateway control messages."""
        self._stats.system_messages += 1
        
        data = msg.data
        message_text = data.get("msg", "")
        
        if "GATEWAY_READY" in message_text:
            version = data.get("ver", "unknown")
            can_ready = data.get("can") == "CAN_READY"
            
            logger.info(f"Gateway ready: v{version}, CAN={can_ready}")
            
            self._store.dispatch(SetConnectionStateAction(
                connected=True,
                gateway_version=version,
                source=ActionSource.GATEWAY
            ))
        
        elif "error" in message_text.lower():
            logger.error(f"Gateway error: {message_text}")
    
    def _handle_can_message(self, msg: RawMessage) -> None:
        """Handle CAN bus messages."""
        self._stats.can_messages += 1
        
        # Decode using CAN decoder
        decoded = self._can_decoder.decode(msg.data)
        if not decoded:
            return
        
        # Convert to actions
        actions = self._can_message_to_actions(decoded)
        
        if actions:
            self._dispatch_actions(actions)
    
    def _handle_avc_message(self, msg: RawMessage) -> None:
        """Handle AVC-LAN messages."""
        self._stats.avc_messages += 1
        
        # Decode using AVC decoder
        avc_msg = self._avc_decoder.decode_message(msg.data)
        if not avc_msg:
            return
        
        # Get classification
        classification = self._avc_decoder.classify_message(avc_msg)
        
        # Convert to actions
        actions = self._avc_to_actions(avc_msg, classification, msg.sequence)
        
        if actions:
            self._dispatch_actions(actions)
    
    def _handle_satellite_message(self, msg: RawMessage) -> None:
        """Handle RS485 satellite messages."""
        self._stats.satellite_messages += 1
        
        device_id = msg.device_id
        
        # Check for registered handler
        handler = self._satellite_handlers.get(device_id)
        if handler:
            try:
                actions = handler(msg.data)
                if actions:
                    self._dispatch_actions(actions)
            except Exception as e:
                logger.error(f"Satellite handler error for device {device_id}: {e}")
        else:
            logger.debug(f"No handler for satellite device {device_id}")
    
    def _dispatch_actions(self, actions: List[Action]) -> None:
        """Dispatch a list of actions to the store."""
        if len(actions) == 1:
            self._store.dispatch(actions[0])
        elif len(actions) > 1:
            self._store.dispatch(BatchAction(actions, ActionSource.GATEWAY))
    
    # ─────────────────────────────────────────────────────────────────────────
    # AVC-LAN to Actions conversion
    # ─────────────────────────────────────────────────────────────────────────
    
    def _avc_to_actions(
        self, 
        msg: AVCMessage, 
        classification: str,
        seq: Optional[int] = None
    ) -> List[Action]:
        """Convert AVC message to state actions."""
        actions: List[Action] = []
        data = msg.data
        
        # Volume messages
        if "volume" in classification.lower() and len(data) >= 2:
            volume = data[1] & 0x3F
            actions.append(SetVolumeAction(volume, ActionSource.GATEWAY))
        
        # Climate Status
        if msg.msg_type == AVCMessageType.CLIMATE_STATUS:
            ambient = msg.values.get("ambient_temp_c")
            if ambient is not None:
                actions.append(SetOutsideTempAction(ambient, ActionSource.GATEWAY))
            
            recirc = msg.values.get("recirc")
            if recirc is not None:
                actions.append(SetRecirculationAction(recirc, ActionSource.GATEWAY))
        
        # Climate messages (0x10C -> 0x310)
        elif msg.master_addr == 0x10C and msg.slave_addr == 0x310:
            if len(data) >= 8:
                if data[6] == 0x90 and data[4] > 0:
                    outside_temp = (data[4] - 18) / 2.0
                    actions.append(SetOutsideTempAction(outside_temp, ActionSource.GATEWAY))
        
        # Button press
        elif classification == "button_press":
            btn = parse_button_event(data)
            if btn:
                if self._analysis_mode:
                    status = "PRESS" if btn.is_press else "RELEASE"
                    seq_str = f"[{seq:>4}]" if seq is not None else ""
                    print(f"[BTN] {seq_str} {status}: {btn.button_name} (code=0x{btn.button_code:04X})")
                
                actions.append(AVCButtonPressAction(
                    button_code=btn.button_code,
                    modifier=btn.modifier,
                    suffix=btn.suffix,
                    is_press=btn.is_press,
                    raw_data=btn.raw_data,
                    button_name=btn.button_name,
                    source=ActionSource.GATEWAY
                ))
        
        # Touch event
        elif classification == "touch_event":
            touch = parse_touch_event(data)
            if touch:
                if self._analysis_mode:
                    seq_str = f"[{seq:>4}]" if seq is not None else ""
                    print(f"[TCH] {seq_str} {touch.touch_type.upper()}: x={touch.x}, y={touch.y}")
                
                actions.append(AVCTouchEventAction(
                    x=touch.x,
                    y=touch.y,
                    touch_type=touch.touch_type,
                    raw_data=touch.raw_data,
                    source=ActionSource.GATEWAY
                ))
        
        # Capture debug bytes for manual correlation
        # 0x110→0x490: MFD status/flow arrows (8 bytes)
        # 0xA00→0x258: SOC/energy broadcast (32 bytes)
        if (msg.master_addr == 0x110 and msg.slave_addr == 0x490) or \
           (msg.master_addr == 0xA00 and msg.slave_addr == 0x258):
            from ..state.actions import AVCDebugBytesAction
            actions.append(AVCDebugBytesAction(
                master_addr=msg.master_addr,
                slave_addr=msg.slave_addr,
                data=list(data),
                source=ActionSource.GATEWAY
            ))
        
        return actions
    
    # ─────────────────────────────────────────────────────────────────────────
    # CAN to Actions conversion
    # ─────────────────────────────────────────────────────────────────────────
    
    def _can_message_to_actions(self, msg) -> List[Action]:
        """Convert decoded CAN message to state actions."""
        actions: List[Action] = []
        
        # HV Battery State of Charge (from 0x03B)
        if msg.msg_type == CANMessageType.HV_BATTERY:
            soc = msg.values.get("soc")
            if soc is not None and 10 <= soc <= 95:
                actions.append(SetBatterySOCAction(soc / 100.0, ActionSource.GATEWAY))
            
            voltage = msg.values.get("voltage")
            if voltage is not None:
                actions.append(SetBatteryVoltageAction(voltage, ActionSource.GATEWAY))
            
            current = msg.values.get("current")
            if current is not None:
                actions.append(SetBatteryCurrentAction(current, ActionSource.GATEWAY))
            
            temp = msg.values.get("battery_temp")
            if temp is not None:
                actions.append(SetBatteryTempAction(temp, ActionSource.GATEWAY))
        
        # HV Battery Power Flow (from 0x3CB)
        elif msg.msg_type == CANMessageType.HV_BATTERY_POWER:
            soc = msg.values.get("soc")
            if soc is not None:
                actions.append(SetBatterySOCAction(soc / 100.0, ActionSource.GATEWAY))
            
            delta_soc = msg.values.get("delta_soc")
            if delta_soc is not None:
                actions.append(SetBatteryDeltaSOCAction(delta_soc, ActionSource.GATEWAY))
            
            is_charging = msg.values.get("is_charging", False)
            power_kw = msg.values.get("power_kw", 0)
            
            charging = is_charging and abs(power_kw) > 0.5
            discharging = not is_charging and abs(power_kw) > 0.5
            
            actions.append(SetChargingStateAction(
                charging=charging,
                discharging=discharging,
                source=ActionSource.GATEWAY
            ))
            
            temp = msg.values.get("battery_temp")
            if temp is not None and -20 <= temp <= 60:
                actions.append(SetBatteryTempAction(temp, ActionSource.GATEWAY))
            
            temp_max = msg.values.get("battery_temp_max")
            if temp_max is not None:
                actions.append(SetBatteryMaxTempAction(temp_max, ActionSource.GATEWAY))
        
        # Energy Flow (0x3B6)
        elif msg.msg_type == CANMessageType.ENERGY_FLOW:
            b5 = msg.values.get("flow_byte_5", 0)
            actions.append(SetEnergyFlowFlagsAction(
                engine_to_wheels=bool(b5 & 0x08),
                battery_to_motor=bool(b5 & 0x10),
                motor_to_battery=bool(b5 & 0x20),
                engine_to_battery=bool(b5 & 0x40),
                battery_to_wheels=bool(b5 & 0x80),
                source=ActionSource.GATEWAY
            ))
        
        # Fuel Consumption (0x520)
        elif msg.msg_type == CANMessageType.FUEL_CONSUMPTION:
            injector_time = msg.values.get("injector_time", 0)
            # Prius Gen 2 1.5L 1NZ-FXE Engine Fuel Flow
            # Based on observed data: 0x520 bytes 1-2 as 16-bit Big Endian
            # - ICE off: 0
            # - Idle (1000 RPM): ~200-400 → ~0.6-1.2 L/h
            # - Light load: ~500-700 → ~2-3 L/h
            # - Moderate: ~800-1000 → ~4-6 L/h
            # - High load: ~1100-1200 → ~7-9 L/h
            # - WOT: Can reach 20-30 L/h (off-scale for normal driving)
            #
            # Empirical scaling factor: 0.01 gives reasonable results
            # Adjust threshold and multiplier based on real-world testing
            if injector_time > 50:  # Filter noise
                # Linear scaling with offset adjustment
                # Typical: 200-1200 maps to ~0.6-9 L/h
                flow_rate = injector_time * 0.008  # 1000 * 0.008 = 8 L/h
                flow_rate = max(0.0, min(30.0, flow_rate))  # Clamp 0-30 L/h
            else:
                flow_rate = 0.0
            actions.append(SetFuelFlowAction(flow_rate, ActionSource.GATEWAY))
        
        # Vehicle Speed
        elif msg.msg_type == CANMessageType.VEHICLE_SPEED:
            speed = msg.values.get("speed_kph") or msg.values.get("speed")
            if speed is not None and speed < 300:
                actions.append(SetSpeedAction(speed, ActionSource.GATEWAY))
        
        # Engine status (0x039 - coolant temp only)
        elif msg.msg_type == CANMessageType.ENGINE_RPM:
            coolant_temp = msg.values.get("coolant_temp")
            if coolant_temp is not None and 40 <= coolant_temp <= 120:
                actions.append(SetICECoolantTempAction(coolant_temp, ActionSource.GATEWAY))
        
        # Engine Status (0x038 - RPM and running state)
        elif msg.msg_type == CANMessageType.ENGINE_STATUS:
            rpm = msg.values.get("rpm")
            if rpm is not None:
                actions.append(SetRPMAction(rpm, ActionSource.GATEWAY))
            
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
                gear_map = {
                    "R": GearPosition.REVERSE,
                    "N": GearPosition.NEUTRAL,
                    "D": GearPosition.DRIVE,
                    "B": GearPosition.B,
                    "P": GearPosition.PARK,
                }
                gear = gear_map.get(gear_str, GearPosition.PARK)
                actions.append(SetGearAction(gear, ActionSource.GATEWAY))
        
        return actions
