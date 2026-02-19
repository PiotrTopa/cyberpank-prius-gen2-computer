"""
Virtual Twin Factory - Creates and configures the complete Virtual Twin system.

This module provides factory functions to create properly configured
instances of all Virtual Twin components based on the execution mode.

Modes:
- Production: Serial IO to RP2040 Gateway
- Development: File replay + console logging + UDP to satellites
- Test: Mock IO for unit tests
"""

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, List, Tuple

from .ports import InputPort, OutputPort
from .file_io import FileInputPort
from .serial_io import SerialPort, SerialConfig
from .mock_io import MockInputPort, MockOutputPort, LogOutputPort
from .udp_output import UDPOutputPort, MultiOutputPort
from .comm_logger import CommLogger, LogConfig, CommLoggerManager
from .ingress import IngressController
from .egress import EgressController
from .vfd_output import register_vfd_handlers

from ..state.store import Store
from ..state.rules import RulesEngine
from ..state.rules.park_speed import ParkSpeedRule
from ..state.rules.fuel_consumption import FuelConsumptionRule
from ..state.rules.trip_fuel import TripFuelConsumptionRule
from ..state.rules.active_fuel import ActiveFuelRule
from ..state.rules.vfd_display import VFDDisplayRule

logger = logging.getLogger(__name__)


class ExecutionMode(Enum):
    """Application execution mode."""
    PRODUCTION = auto()   # Real hardware
    DEVELOPMENT = auto()  # File replay + logging
    TEST = auto()         # Mock IO


@dataclass
class VirtualTwinConfig:
    """Configuration for Virtual Twin system."""
    mode: ExecutionMode = ExecutionMode.DEVELOPMENT
    
    # Logging configuration
    log_state_changes: bool = False  # Log [STATE] messages
    
    # Serial config (production mode)
    serial_port: str = "/dev/ttyACM0"
    serial_baudrate: int = 1_000_000
    serial_auto_reconnect: bool = True
    serial_reconnect_delay: float = 2.0
    
    # File config (development mode)
    replay_file: Optional[str] = None
    playback_speed: float = 1.0
    playback_loop: bool = False
    
    # UDP satellite config (development mode)
    enable_vfd_satellite: bool = True
    vfd_udp_host: str = "localhost"
    vfd_udp_port: int = 5110
    
    # Communication logging for replay (NEW in v2.8.0)
    enable_comm_logging: bool = False  # Enable logging all comm for replay
    comm_log_dir: Optional[str] = None  # Directory for log files
    comm_log_devices: Optional[List[int]] = None  # Filter by device (None = all)
    
    # Solicited CAN mode (v2.8.0) - enables inverter temp, delta SOC, etc.
    enable_solicited_can: bool = True  # Auto-subscribe to key PIDs on connect
    
    # Logging
    verbose: bool = False
    log_commands: bool = True


@dataclass
class VirtualTwin:
    """
    Complete Virtual Twin system.
    
    Contains all components needed for the Virtual Twin architecture.
    """
    store: Store
    input_port: InputPort
    output_port: OutputPort
    ingress: IngressController
    egress: EgressController
    rules_engine: RulesEngine
    mode: ExecutionMode
    comm_logger: Optional[CommLogger] = None  # Communication logger for replay
    _enable_solicited: bool = False  # Whether to send solicited CAN subscriptions
    
    def start(self) -> bool:
        """Start all components."""
        result = self.ingress.start()
        
        # Initialize solicited CAN subscriptions after connection
        if result and self._enable_solicited:
            self._init_solicited_subscriptions()
        
        return result
    
    def _init_solicited_subscriptions(self) -> None:
        """
        Initialize solicited CAN subscriptions for key PIDs.
        
        This enables RPM, SOC and inverter temperature from solicited queries.
        Uses ISO-TP multi-frame reassembly for PIDs that return >7 bytes (Gateway v2.9.0+).
        """
        from .ports import OutgoingCommand, DEVICE_CAN
        
        logger.info("Initializing solicited CAN subscriptions...")
        
        # Switch to normal (active) CAN mode
        self.output_port.send(OutgoingCommand(
            device_id=DEVICE_CAN,
            command_type="mode",
            payload={"a": "mode", "m": "normal"}
        ))
        
        # Wait briefly for mode switch
        import time
        time.sleep(0.1)
        
        # Subscribe to key PIDs:
        
        # Slot 0: Engine ECU 0x7E0, PID 010C (RPM) @ 500ms
        # IMPORTANT: Use direct ECU address 0x7E0, NOT broadcast 0x7DF!
        # Broadcasting to 0x7DF queries ALL ECUs which floods the bus and
        # can trigger diagnostic sessions in ECUs, causing the master warning triangle.
        # Also reduced polling rate from 200ms to 500ms to be less aggressive.
        self.output_port.send(OutgoingCommand(
            device_id=DEVICE_CAN,
            command_type="sub",
            payload={
                "a": "sub",
                "slot": 0,
                "i": "0x7E0",
                "d": [0x02, 0x01, 0x0C, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 500,
                "t": 200,
                "r": ["0x7E8"]
            }
        ))
        
        # Slot 1: Hybrid ECU 0x7E2, PID 21CF (delta SOC) @ 2000ms
        # Single-frame response from 0x7EA
        # Reduced rate - delta SOC doesn't change fast
        self.output_port.send(OutgoingCommand(
            device_id=DEVICE_CAN,
            command_type="sub",
            payload={
                "a": "sub",
                "slot": 1,
                "i": "0x7E2",
                "d": [0x02, 0x21, 0xCF, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 2000,
                "t": 200,
                "r": ["0x7EA"]
            }
        ))
        
        # Slot 2: Hybrid ECU 0x7E2, PID 21C3 (inverter temps, MG data) @ 1000ms
        # ISO-TP multi-frame response (31+ bytes) from 0x7EA
        # Reduced rate from 500ms - less bus load, still fast enough for temps
        self.output_port.send(OutgoingCommand(
            device_id=DEVICE_CAN,
            command_type="sub",
            payload={
                "a": "sub",
                "slot": 2,
                "i": "0x7E2",
                "d": [0x02, 0x21, 0xC3, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 1000,
                "t": 500,
                "r": ["0x7EA"],
                "isotp": True
            }
        ))
        
        # Slot 3: HV Battery ECU 0x7E3, PID 21CE (block voltages) @ 2000ms
        # ISO-TP multi-frame response (33+ bytes) from 0x7EB
        # Contains 14 block voltages for delta-V / battery health monitoring
        self.output_port.send(OutgoingCommand(
            device_id=DEVICE_CAN,
            command_type="sub",
            payload={
                "a": "sub",
                "slot": 3,
                "i": "0x7E3",
                "d": [0x02, 0x21, 0xCE, 0x00, 0x00, 0x00, 0x00, 0x00],
                "int": 2000,
                "t": 500,
                "r": ["0x7EB"],
                "isotp": True
            }
        ))
        
        logger.info("Solicited CAN subscriptions initialized (4 slots: RPM, SOC, INV temp, block V)")
        
        # Wait for writer thread to actually send the commands
        import time
        time.sleep(0.5)
        
        # Log serial stats to confirm commands were sent
        if hasattr(self.output_port, '_ports'):
            # MultiOutputPort
            for p in self.output_port._ports:
                if hasattr(p, 'stats'):
                    logger.info(f"Serial stats after sub init: {p.stats}")
        elif hasattr(self.output_port, 'stats'):
            logger.info(f"Serial stats after sub init: {self.output_port.stats}")
    
    def _cleanup_solicited_subscriptions(self) -> None:
        """
        Clean up solicited CAN subscriptions and return to listen-only mode.
        
        CRITICAL: Must be called on shutdown to prevent the gateway from
        continuing to send OBD-II queries after our program exits.
        Leaving the MCP2515 in normal mode with active subscriptions will
        keep transmitting on the CAN bus, causing potential ECU issues.
        """
        from .ports import OutgoingCommand, DEVICE_CAN
        
        logger.info("Cleaning up solicited CAN subscriptions...")
        
        # Unsubscribe all slots
        self.output_port.send(OutgoingCommand(
            device_id=DEVICE_CAN,
            command_type="unsub",
            payload={"a": "unsub", "slot": "all"}
        ))
        
        import time
        time.sleep(0.05)
        
        # Switch back to listen-only mode (passive sniffing)
        self.output_port.send(OutgoingCommand(
            device_id=DEVICE_CAN,
            command_type="mode",
            payload={"a": "mode", "m": "listen"}
        ))
        
        logger.info("CAN returned to listen-only mode")
    
    def stop(self) -> None:
        """Stop all components."""
        # Clean up solicited subscriptions FIRST (before closing serial)
        if self._enable_solicited:
            try:
                self._cleanup_solicited_subscriptions()
            except Exception as e:
                logger.warning(f"Error cleaning up solicited subscriptions: {e}")
        
        self.ingress.stop()
        if self.comm_logger:
            self.comm_logger.stop()
    
    def update(self) -> int:
        """
        Process pending messages.
        
        Call this in the main loop.
        
        Returns:
            Number of messages processed
        """
        return self.ingress.update()


def create_virtual_twin(config: VirtualTwinConfig) -> VirtualTwin:
    """
    Create a complete Virtual Twin system.
    
    Args:
        config: Configuration for the system
        
    Returns:
        Configured VirtualTwin instance
    """
    logger.info(f"Creating Virtual Twin in {config.mode.name} mode")
    
    # Create store with persisted user preferences
    from ..persistence import get_settings
    settings_mgr = get_settings()
    initial_state = settings_mgr.build_initial_app_state()
    store = Store(initial_state=initial_state, verbose=config.log_state_changes)
    logger.info("Store initialized with persisted user preferences")
    
    # Create IO ports based on mode
    if config.mode == ExecutionMode.PRODUCTION:
        input_port, output_port = _create_production_io(config)
    elif config.mode == ExecutionMode.DEVELOPMENT:
        input_port, output_port = _create_development_io(config)
    else:  # TEST
        input_port, output_port = _create_test_io(config)
    
    # Create controllers
    ingress = IngressController(store, input_port)
    egress = EgressController(store, output_port)
    
    # Create rules engine with core rules
    rules_engine = RulesEngine(store)
    rules_engine.register(ParkSpeedRule())
    rules_engine.register(FuelConsumptionRule())
    rules_engine.register(TripFuelConsumptionRule())
    rules_engine.register(ActiveFuelRule())
    
    # Register VFD satellite support
    if config.enable_vfd_satellite:
        # Add VFD display rule (computes VFD state from vehicle state)
        rules_engine.register(VFDDisplayRule())
        
        # Register VFD output handlers (sends VFD state to satellite)
        register_vfd_handlers(egress)
        logger.info("VFD satellite support enabled")
    
    # Set up logging if enabled
    if config.log_commands:
        egress.set_command_log_callback(_log_command)
    
    # Set up communication logging for replay
    comm_logger = None
    if config.enable_comm_logging:
        # Parse device filter into LogConfig booleans
        include_can = True
        include_avc = True
        include_system = True
        include_satellite = True
        
        if config.comm_log_devices:
            device_set = set(config.comm_log_devices)
            include_system = 0 in device_set
            include_can = 1 in device_set
            include_avc = 2 in device_set
            include_satellite = any(d >= 100 for d in device_set)
        
        log_config = LogConfig(
            directory=config.comm_log_dir or "logs",
            include_system=include_system,
            include_can=include_can,
            include_avc=include_avc,
            include_satellite=include_satellite
        )
        comm_logger = CommLogger(config=log_config)  # filepath=None for auto-generate
        comm_logger.start()
        
        # Wire up ingress message logging
        ingress.add_message_log_callback(comm_logger.log_incoming)
        
        # Wire up egress command logging (in addition to console logging)
        egress.set_message_log_callback(comm_logger.log_outgoing)
        
        logger.info(f"Communication logging enabled, writing to: {comm_logger.filepath}")
    
    # Determine if solicited CAN should be enabled
    # Only for production/serial mode, not for replay
    enable_solicited = (
        config.enable_solicited_can and 
        config.mode == ExecutionMode.PRODUCTION
    )
    
    # Add auto-save middleware for user preference changes
    from .persistence_middleware import create_persistence_middleware
    store.add_middleware(create_persistence_middleware(settings_mgr))
    
    return VirtualTwin(
        store=store,
        input_port=input_port,
        output_port=output_port,
        ingress=ingress,
        egress=egress,
        rules_engine=rules_engine,
        mode=config.mode,
        comm_logger=comm_logger,
        _enable_solicited=enable_solicited
    )


def _create_production_io(config: VirtualTwinConfig):
    """Create production serial IO with optional UDP for satellites."""
    serial_config = SerialConfig(
        port=config.serial_port,
        baudrate=config.serial_baudrate,
        auto_reconnect=config.serial_auto_reconnect,
        reconnect_delay=config.serial_reconnect_delay
    )
    
    # Input: Serial port
    serial_port = SerialPort(serial_config)
    input_port = serial_port
    
    # Output: Serial + UDP for satellite development/debugging
    outputs: List[OutputPort] = []
    
    # Primary output: Serial to Gateway
    outputs.append(serial_port)
    
    # Secondary output: UDP for satellites (allows monitoring on dev machine)
    if config.enable_vfd_satellite:
        udp_output = _create_vfd_udp_output(config)
        outputs.append(udp_output)
        logger.info(f"Production mode: UDP mirror enabled for VFD at {config.vfd_udp_host}:{config.vfd_udp_port}")
    
    # Combine outputs
    if len(outputs) > 1:
        output_port = MultiOutputPort(outputs)
    else:
        output_port = outputs[0]
    
    return input_port, output_port


def _create_vfd_udp_output(config: VirtualTwinConfig) -> UDPOutputPort:
    """Create UDP output port for VFD satellite."""
    udp_output = UDPOutputPort()
    udp_output.add_target(
        config.vfd_udp_host,
        config.vfd_udp_port,
        device_ids={110}  # VFD device ID
    )
    return udp_output


def _create_development_io(config: VirtualTwinConfig):
    """Create development file replay + logging + UDP IO."""
    # Input: file replay or mock
    if config.replay_file:
        input_port = FileInputPort(
            filepath=config.replay_file,
            speed=config.playback_speed,
            loop=config.playback_loop
        )
    else:
        input_port = MockInputPort()
    
    # Output: combine logging and UDP for satellites
    outputs: List[OutputPort] = []
    
    # Conditionally add console logging for commands
    if config.log_commands:
        log_output = LogOutputPort(prefix="[WOULD SEND]")
        outputs.append(log_output)
    
    # UDP output for VFD satellite
    if config.enable_vfd_satellite:
        udp_output = _create_vfd_udp_output(config)
        outputs.append(udp_output)
        logger.info(f"Development mode: UDP output enabled for VFD at {config.vfd_udp_host}:{config.vfd_udp_port}")
    
    # Use multi-output if we have multiple outputs
    if len(outputs) > 1:
        output_port = MultiOutputPort(outputs)
    else:
        output_port = outputs[0]
    
    return input_port, output_port


def _create_test_io(config: VirtualTwinConfig):
    """Create test mock IO."""
    input_port = MockInputPort()
    output_port = MockOutputPort()
    return input_port, output_port


def _log_command(command, direction: str) -> None:
    """Default command logging callback."""
    logger.debug(f"[{direction}] {command}")


# ─────────────────────────────────────────────────────────────────────────────
# Convenience functions for common configurations
# ─────────────────────────────────────────────────────────────────────────────

def create_production_twin(
    serial_port: str = "/dev/ttyACM0",
    verbose: bool = False
) -> VirtualTwin:
    """
    Create a Virtual Twin for production use.
    
    Args:
        serial_port: Serial port path
        verbose: Enable verbose logging
    """
    config = VirtualTwinConfig(
        mode=ExecutionMode.PRODUCTION,
        serial_port=serial_port,
        verbose=verbose
    )
    return create_virtual_twin(config)


def create_development_twin(
    replay_file: Optional[str] = None,
    speed: float = 1.0,
    verbose: bool = True
) -> VirtualTwin:
    """
    Create a Virtual Twin for development/testing.
    
    Args:
        replay_file: Path to NDJSON replay file
        speed: Playback speed multiplier
        verbose: Enable verbose logging
    """
    config = VirtualTwinConfig(
        mode=ExecutionMode.DEVELOPMENT,
        replay_file=replay_file,
        playback_speed=speed,
        verbose=verbose
    )
    return create_virtual_twin(config)


def create_test_twin(verbose: bool = False) -> VirtualTwin:
    """
    Create a Virtual Twin for unit testing.
    
    Returns a twin with mock IO for programmatic control.
    """
    config = VirtualTwinConfig(
        mode=ExecutionMode.TEST,
        verbose=verbose,
        log_commands=False
    )
    return create_virtual_twin(config)
