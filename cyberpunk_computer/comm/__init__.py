"""
Communication module.

Handles serial communication with the Gateway using NDJSON protocol.
Provides AVC-LAN message decoding, state management, and command generation.
Also provides CAN bus message decoding for vehicle data.

Supports Gateway Protocol v2.8.0 with solicited CAN mode.

AVC-LAN: the backend uses :mod:`cyberpunk_computer.comm.avc` (gateway fw 3.x,
verified map in cyberpunk-prius-gateway/captures/2026-10-07/AVC_MAP.md).
``avc_decoder`` / ``avc_state`` are LEGACY decoders fitted to gateway fw 2.x
output (bit-misaligned re-readings of the bus, Flerchinger-era guesses); they
remain only because the pygame UI's debug screens still import them. Do not
use them for new code.
"""

from .protocol import (
    parse_message, create_message, Message, 
    DEVICE_AVCLAN, DEVICE_CAN, DEVICE_SYSTEM,
    # CAN solicited mode functions
    create_can_request, create_can_subscription, create_can_unsubscribe,
    create_can_mode_switch, create_can_list_subs,
    create_obd2_request, create_obd2_subscription,
)
from .avc_decoder import (
    AVCMessage,
    AVCDecoder,
    DeviceType,
    DeviceInfo,
    LogicDeviceID,
    DEVICE_ADDRESSES,
    # Parsing helpers
    parse_touch_event,
    parse_button_event,
    parse_audio_status,
    parse_volume_status,
    TouchEvent,
    ButtonEvent,
    AudioParamStatus,
    HEARTBEAT_BUTTON_CODES,
)
from .avc_state import (
    AVCStateManager,
    AVCEventType,
    AudioSource,
    DisplayMode,
    ClimateMode,
    AudioState,
    ClimateState,
    VehicleState,
    DisplayState,
)
from .can_decoder import (
    CANDecoder,
    CANMessage,
    CANMessageType,
    CANStateTracker,
)
from .solicited_can import (
    SolicitedCANManager,
    get_manager as get_solicited_manager,
    ECUAddress,
    PIDDefinition,
    # Common PID definitions
    PID_ENGINE_COOLANT_TEMP,
    PID_ENGINE_RPM,
    PID_VEHICLE_SPEED,
    PID_HYBRID_COMPREHENSIVE,
    PID_HV_BATTERY_DETAIL,
    PID_HV_BATTERY_TEMPS,
)

__all__ = [
    # Protocol
    "parse_message",
    "create_message",
    "Message",
    "DEVICE_AVCLAN",
    "DEVICE_CAN",
    "DEVICE_SYSTEM",
    # CAN solicited mode
    "create_can_request",
    "create_can_subscription",
    "create_can_unsubscribe",
    "create_can_mode_switch",
    "create_can_list_subs",
    "create_obd2_request",
    "create_obd2_subscription",
    # AVC Decoder
    "AVCMessage",
    "AVCDecoder",
    "DeviceType",
    "DeviceInfo",
    "LogicDeviceID",
    "DEVICE_ADDRESSES",
    # Parsing helpers
    "parse_touch_event",
    "parse_button_event",
    "parse_audio_status",
    "parse_volume_status",
    "TouchEvent",
    "ButtonEvent",
    "AudioParamStatus",
    "HEARTBEAT_BUTTON_CODES",
    # AVC State
    "AVCStateManager",
    "AVCEventType",
    "AudioSource",
    "DisplayMode",
    "ClimateMode",
    "AudioState",
    "ClimateState",
    "VehicleState",
    "DisplayState",
    # CAN Decoder
    "CANDecoder",
    "CANMessage",
    "CANMessageType",
    "CANStateTracker",
    # Solicited CAN
    "SolicitedCANManager",
    "get_solicited_manager",
    "ECUAddress",
    "PIDDefinition",
    "PID_ENGINE_COOLANT_TEMP",
    "PID_ENGINE_RPM",
    "PID_VEHICLE_SPEED",
    "PID_HYBRID_COMPREHENSIVE",
    "PID_HV_BATTERY_DETAIL",
    "PID_HV_BATTERY_TEMPS",
]
