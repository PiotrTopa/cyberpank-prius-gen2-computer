"""
Port Abstractions - Input and Output port interfaces.

These abstractions enable pluggable IO implementations:
- Production: Serial UART to RP2040 Gateway
- Development: File replay + console logging
- Testing: Mock ports for unit tests

All communication flows through these interfaces, ensuring the
Virtual Twin is completely decoupled from physical hardware.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Any, Dict
from enum import Enum, auto
import time


# ─────────────────────────────────────────────────────────────────────────────
# Device ID Constants
# ─────────────────────────────────────────────────────────────────────────────

DEVICE_SYSTEM = 0           # Gateway control/status
DEVICE_CAN = 1              # Vehicle CAN bus
DEVICE_AVC = 2              # Toyota AVC-LAN
DEVICE_SATELLITE_BASE = 100 # RS485 satellites start here

# Known satellite device IDs
DEVICE_SATELLITE_DRL = 106       # DRL controller (output)
DEVICE_SATELLITE_LIGHT_SENSOR = 107  # Rain/light sensor (input)
DEVICE_VFD = 110                 # VFD display satellite (output)


# ─────────────────────────────────────────────────────────────────────────────
# Message Types
# ─────────────────────────────────────────────────────────────────────────────

class MessageCategory(Enum):
    """High-level message category."""
    SYSTEM = auto()      # Gateway status/control
    CAN = auto()         # CAN bus message
    AVC_LAN = auto()     # AVC-LAN message
    SATELLITE = auto()   # RS485 satellite
    UNKNOWN = auto()


@dataclass
class RawMessage:
    """
    Raw message from any input source.
    
    This is the universal message format that bridges all input sources
    (serial, file replay, mock) to the ingress controller.
    
    Attributes:
        device_id: Source device (0=system, 1=CAN, 2=AVC, 100+=satellites)
        timestamp: When message was received (Unix timestamp)
        data: Raw payload data (protocol-specific structure)
        sequence: Optional sequence number from source
        category: High-level message category
    """
    device_id: int
    timestamp: float
    data: Dict[str, Any]
    sequence: Optional[int] = None
    category: MessageCategory = MessageCategory.UNKNOWN
    
    def __post_init__(self):
        """Auto-detect category from device_id if not set."""
        if self.category == MessageCategory.UNKNOWN:
            if self.device_id == DEVICE_SYSTEM:
                self.category = MessageCategory.SYSTEM
            elif self.device_id == DEVICE_CAN:
                self.category = MessageCategory.CAN
            elif self.device_id == DEVICE_AVC:
                self.category = MessageCategory.AVC_LAN
            elif self.device_id >= DEVICE_SATELLITE_BASE:
                self.category = MessageCategory.SATELLITE
    
    @classmethod
    def from_gateway_json(cls, raw: dict) -> "RawMessage":
        """
        Create RawMessage from gateway JSON format.
        
        Expected format: {"id": N, "d": {...}, "seq": N, "ts": N}
        """
        device_id = raw.get("id", 0)
        data = raw.get("d", {})
        sequence = raw.get("seq")
        timestamp = raw.get("ts", time.time() * 1000) / 1000.0  # Convert ms to seconds
        
        return cls(
            device_id=device_id,
            timestamp=timestamp,
            data=data if isinstance(data, dict) else {"raw": data},
            sequence=sequence
        )


@dataclass
class OutgoingCommand:
    """
    Command to send to hardware via output port.
    
    This is the universal command format that the egress controller
    uses to send commands through any output port implementation.
    
    Attributes:
        device_id: Target device (1=CAN, 2=AVC, 100+=satellites)
        command_type: Command identifier string
        payload: Command data (protocol-specific)
        priority: Higher priority commands are sent first
        timestamp: When command was created
    """
    device_id: int
    command_type: str
    payload: Dict[str, Any]
    priority: int = 0
    timestamp: float = field(default_factory=time.time)
    
    def to_gateway_json(self) -> dict:
        """
        Convert to gateway JSON format.
        
        Returns: {"id": N, "cmd": "...", "d": {...}}
        """
        return {
            "id": self.device_id,
            "cmd": self.command_type,
            "d": self.payload
        }


# ─────────────────────────────────────────────────────────────────────────────
# Input Port Interface
# ─────────────────────────────────────────────────────────────────────────────

class InputPort(ABC):
    """
    Abstract input port for receiving messages.
    
    Implementations:
    - SerialInputPort: Production serial/UART
    - FileInputPort: Development file replay
    - MockInputPort: Testing
    
    Usage:
        port = SerialInputPort("/dev/ttyACM0")
        port.start()
        
        while running:
            msg = port.poll()
            if msg:
                ingress.process(msg)
    """
    
    @abstractmethod
    def start(self) -> bool:
        """
        Start the input port.
        
        Returns:
            True if started successfully
        """
        pass
    
    @abstractmethod
    def stop(self) -> None:
        """Stop the input port and cleanup resources."""
        pass
    
    @abstractmethod
    def poll(self) -> Optional[RawMessage]:
        """
        Poll for the next available message.
        
        Non-blocking. Returns None if no message available.
        
        Returns:
            RawMessage if available, None otherwise
        """
        pass
    
    @abstractmethod
    def is_connected(self) -> bool:
        """
        Check if the port is connected/active.
        
        Returns:
            True if port is ready to receive
        """
        pass
    
    @property
    def name(self) -> str:
        """Human-readable port name for logging."""
        return self.__class__.__name__


# ─────────────────────────────────────────────────────────────────────────────
# Output Port Interface
# ─────────────────────────────────────────────────────────────────────────────

class OutputPort(ABC):
    """
    Abstract output port for sending commands.
    
    Implementations:
    - SerialOutputPort: Production serial/UART
    - LogOutputPort: Development console logging
    - MockOutputPort: Testing
    
    Usage:
        port = SerialOutputPort("/dev/ttyACM0")
        
        command = OutgoingCommand(
            device_id=DEVICE_AVC,
            command_type="set_volume",
            payload={"volume": 35}
        )
        port.send(command)
    """
    
    @abstractmethod
    def send(self, command: OutgoingCommand) -> bool:
        """
        Send a command.
        
        Args:
            command: Command to send
            
        Returns:
            True if command was queued/sent successfully
        """
        pass
    
    @abstractmethod
    def is_connected(self) -> bool:
        """
        Check if the port can send commands.
        
        Returns:
            True if port is ready to send
        """
        pass
    
    @property
    def name(self) -> str:
        """Human-readable port name for logging."""
        return self.__class__.__name__


# ─────────────────────────────────────────────────────────────────────────────
# Composite Port for Bidirectional Communication
# ─────────────────────────────────────────────────────────────────────────────

class BidirectionalPort(InputPort, OutputPort):
    """
    Combined input and output port.
    
    Some implementations (like serial) are naturally bidirectional.
    This interface combines both for convenience.
    """
    pass
