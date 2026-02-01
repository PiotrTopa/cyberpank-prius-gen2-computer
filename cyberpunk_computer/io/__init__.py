"""
IO Package - Input/Output abstractions for Virtual Twin architecture.

This package provides abstract interfaces for data ingress and egress,
enabling different implementations for production, development, and testing.

Key components:
- ports: Abstract InputPort and OutputPort interfaces
- ingress: IngressController for updating Virtual Twin from inputs
- egress: EgressController for sending commands from Virtual Twin
- serial_io: Production serial/UART implementations
- file_io: Development file replay implementation
- mock_io: Testing mock implementations
- factory: Factory functions for creating complete Virtual Twin systems
"""

from .ports import (
    RawMessage,
    OutgoingCommand,
    MessageCategory,
    InputPort,
    OutputPort,
    BidirectionalPort,
    DEVICE_SYSTEM,
    DEVICE_CAN,
    DEVICE_AVC,
    DEVICE_SATELLITE_BASE,
    DEVICE_SATELLITE_DRL,
    DEVICE_SATELLITE_LIGHT_SENSOR,
    DEVICE_VFD,
)

from .ingress import IngressController
from .egress import EgressController, OutputHandler, create_satellite_output_handler

from .file_io import FileInputPort, PlaybackState
from .serial_io import SerialPort, SerialConfig, SerialInputPort, SerialOutputPort
from .mock_io import MockInputPort, MockOutputPort, LogOutputPort
from .udp_output import UDPOutputPort, MultiOutputPort
from .vfd_output import (
    register_vfd_handlers,
    create_vfd_energy_handler,
    create_vfd_state_handler,
    create_vfd_config_handler,
    create_all_vfd_handlers,
)

from .factory import (
    VirtualTwin,
    VirtualTwinConfig,
    ExecutionMode,
    create_virtual_twin,
    create_production_twin,
    create_development_twin,
    create_test_twin,
)

__all__ = [
    # Message types
    "RawMessage",
    "OutgoingCommand",
    "MessageCategory",
    
    # Port interfaces
    "InputPort",
    "OutputPort",
    "BidirectionalPort",
    
    # Port implementations
    "FileInputPort",
    "PlaybackState",
    "SerialPort",
    "SerialConfig",
    "SerialInputPort",
    "SerialOutputPort",
    "MockInputPort",
    "MockOutputPort",
    "LogOutputPort",
    "UDPOutputPort",
    "MultiOutputPort",
    
    # Controllers
    "IngressController",
    "EgressController",
    "OutputHandler",
    "create_satellite_output_handler",
    
    # VFD handlers
    "register_vfd_handlers",
    "create_vfd_energy_handler",
    "create_vfd_state_handler",
    "create_vfd_config_handler",
    "create_all_vfd_handlers",
    
    # Factory
    "VirtualTwin",
    "VirtualTwinConfig",
    "ExecutionMode",
    "create_virtual_twin",
    "create_production_twin",
    "create_development_twin",
    "create_test_twin",
    
    # Device constants
    "DEVICE_SYSTEM",
    "DEVICE_CAN", 
    "DEVICE_AVC",
    "DEVICE_SATELLITE_BASE",
    "DEVICE_SATELLITE_DRL",
    "DEVICE_SATELLITE_LIGHT_SENSOR",
    "DEVICE_VFD",
]
