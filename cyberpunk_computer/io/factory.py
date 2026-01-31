"""
Virtual Twin Factory - Creates and configures the complete Virtual Twin system.

This module provides factory functions to create properly configured
instances of all Virtual Twin components based on the execution mode.

Modes:
- Production: Serial IO to RP2040 Gateway
- Development: File replay + console logging
- Test: Mock IO for unit tests
"""

import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from .ports import InputPort, OutputPort
from .file_io import FileInputPort
from .serial_io import SerialPort, SerialConfig
from .mock_io import MockInputPort, MockOutputPort, LogOutputPort
from .ingress import IngressController
from .egress import EgressController

from ..state.store import Store
from ..state.rules import RulesEngine
from ..state.rules.park_speed import ParkSpeedRule
from ..state.rules.fuel_consumption import FuelConsumptionRule
from ..state.rules.active_fuel import ActiveFuelRule

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
    
    # Serial config (production mode)
    serial_port: str = "/dev/ttyACM0"
    serial_baudrate: int = 1_000_000
    
    # File config (development mode)
    replay_file: Optional[str] = None
    playback_speed: float = 1.0
    playback_loop: bool = False
    
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
    
    def start(self) -> bool:
        """Start all components."""
        return self.ingress.start()
    
    def stop(self) -> None:
        """Stop all components."""
        self.ingress.stop()
    
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
    
    # Create store
    store = Store(verbose=config.verbose)
    
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
    
    # Create rules engine
    rules_engine = RulesEngine(store)
    rules_engine.register(ParkSpeedRule())
    rules_engine.register(FuelConsumptionRule())
    rules_engine.register(ActiveFuelRule())
    
    # Set up logging if enabled
    if config.log_commands:
        egress.set_command_log_callback(_log_command)
    
    return VirtualTwin(
        store=store,
        input_port=input_port,
        output_port=output_port,
        ingress=ingress,
        egress=egress,
        rules_engine=rules_engine,
        mode=config.mode
    )


def _create_production_io(config: VirtualTwinConfig):
    """Create production serial IO."""
    serial_config = SerialConfig(
        port=config.serial_port,
        baudrate=config.serial_baudrate
    )
    
    # Use bidirectional serial port
    serial_port = SerialPort(serial_config)
    
    return serial_port, serial_port


def _create_development_io(config: VirtualTwinConfig):
    """Create development file replay + logging IO."""
    # Input: file replay or mock
    if config.replay_file:
        input_port = FileInputPort(
            filepath=config.replay_file,
            speed=config.playback_speed,
            loop=config.playback_loop
        )
    else:
        input_port = MockInputPort()
    
    # Output: logging to console
    output_port = LogOutputPort(prefix="[WOULD SEND]")
    
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
