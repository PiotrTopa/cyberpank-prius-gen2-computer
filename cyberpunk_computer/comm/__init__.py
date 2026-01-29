"""
Communication module.

Handles serial communication with the Gateway using NDJSON protocol.
Provides AVC-LAN message decoding, state management, and command generation.
Also provides CAN bus message decoding for vehicle data.

Reference: Flerchinger, J.J. "AN IN-DEPTH LOOK AT THE TOYOTA AUDIO & VIDEO BUS (AVC-LAN)" 2006
"""

from .protocol import parse_message, create_message, Message, DEVICE_AVCLAN
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
from .avc_commands import (
    AVCLANCommand,
    AVCCommandGenerator,
    CommandQueue,
    AudioParamCode,
    # Value range constants
    BALANCE_MIN, BALANCE_MAX, BALANCE_CENTER,
    FADE_MIN, FADE_MAX, FADE_CENTER,
    TONE_MIN, TONE_MAX, TONE_CENTER,
)
from .can_decoder import (
    CANDecoder,
    CANMessage,
    CANMessageType,
    CANStateTracker,
)

__all__ = [
    # Protocol
    "parse_message",
    "create_message",
    "Message",
    "DEVICE_AVCLAN",
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
    # AVC Commands
    "AVCLANCommand",
    "AVCCommandGenerator",
    "CommandQueue",
    "AudioParamCode",
    # Value range constants
    "BALANCE_MIN", "BALANCE_MAX", "BALANCE_CENTER",
    "FADE_MIN", "FADE_MAX", "FADE_CENTER",
    "TONE_MIN", "TONE_MAX", "TONE_CENTER",
    # CAN Decoder
    "CANDecoder",
    "CANMessage",
    "CANMessageType",
    "CANStateTracker",
]
