"""
Egress Controller - Bridges Virtual Twin State to OutputPort.

The Egress Controller is responsible for:
1. Subscribing to state changes
2. Detecting which changes require hardware commands
3. Encoding commands for appropriate protocols (AVC-LAN, RS485)
4. Sending commands via the OutputPort

This provides a clean separation between state management and IO.
"""

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Callable, Any, Set

from .ports import OutputPort, OutgoingCommand, DEVICE_AVC, DEVICE_SATELLITE_BASE
from ..state.store import Store, StateSlice
from ..state.app_state import AppState
from ..state.actions import (
    Action, ActionType, ActionSource,
    SetVolumeAction, SetBassAction, SetMidAction, SetTrebleAction,
    SetBalanceAction, SetFaderAction, SetMuteAction,
    SetTargetTempAction, SetFanSpeedAction, SetACAction, SetAutoModeAction,
    SetRecirculationAction, SetAirDirectionAction,
)
from ..comm.avc_commands import AVCCommandGenerator

logger = logging.getLogger(__name__)


@dataclass
class EgressStats:
    """Statistics for egress processing."""
    commands_sent: int = 0
    avc_commands: int = 0
    satellite_commands: int = 0
    send_failures: int = 0
    last_command_time: float = 0.0


class EgressController:
    """
    Egress controller - bridges Virtual Twin to OutputPort.
    
    The controller operates in two modes:
    
    1. **Action-based (Middleware Mode)**: Listens to actions dispatched to
       the store and converts UI-sourced actions to commands. This is for
       user interactions like changing volume.
       
    2. **State-based (Rule Output Mode)**: Monitors specific state fields
       and sends commands when those states change. This is for computed
       outputs like DRL status from rules.
    
    Usage:
        store = Store()
        output_port = SerialOutputPort("/dev/ttyACM0")
        
        egress = EgressController(store, output_port)
        
        # Register state-based output handlers
        egress.register_output_handler(
            state_path="lights.drl_output_active",
            device_id=106,
            command_type="set_drl",
            payload_builder=lambda state: {"drl": state.lights.drl_output_active}
        )
    """
    
    def __init__(self, store: Store, output_port: OutputPort):
        """
        Initialize egress controller.
        
        Args:
            store: Virtual Twin state store
            output_port: Output destination for commands
        """
        self._store = store
        self._output_port = output_port
        
        # Command encoders
        self._avc_commands = AVCCommandGenerator()
        
        # Statistics
        self._stats = EgressStats()
        
        # Previous state for change detection
        self._prev_state: Optional[AppState] = None
        
        # State-based output handlers
        self._output_handlers: List[OutputHandler] = []
        
        # Satellite command encoders (device_id -> encoder function)
        self._satellite_encoders: Dict[int, Callable[[str, dict], dict]] = {}
        
        # Command logging callback
        self._command_log_callback: Optional[Callable[[OutgoingCommand, str], None]] = None
        
        # Register as middleware for action-based commands
        store.add_middleware(self._handle_action)
        
        # Subscribe to state changes for state-based outputs
        store.subscribe(StateSlice.ALL, self._on_state_change)
    
    @property
    def stats(self) -> EgressStats:
        """Get egress statistics."""
        return self._stats
    
    @property
    def output_port(self) -> OutputPort:
        """Get the output port."""
        return self._output_port
    
    def set_command_log_callback(
        self, 
        callback: Callable[[OutgoingCommand, str], None]
    ) -> None:
        """Set callback for command logging (called with command and "OUT")."""
        self._command_log_callback = callback
    
    def register_satellite_encoder(
        self,
        device_id: int,
        encoder: Callable[[str, dict], dict]
    ) -> None:
        """
        Register a command encoder for a satellite device.
        
        The encoder converts (command_type, payload) to wire format.
        
        Args:
            device_id: Satellite device ID (100+)
            encoder: Function(command_type, payload) -> wire_format_dict
        """
        if device_id < DEVICE_SATELLITE_BASE:
            raise ValueError(f"Satellite device ID must be >= {DEVICE_SATELLITE_BASE}")
        self._satellite_encoders[device_id] = encoder
        logger.info(f"Registered satellite encoder for device {device_id}")
    
    def register_output_handler(
        self,
        name: str,
        watched_slices: Set[StateSlice],
        should_send: Callable[[Optional[AppState], AppState], bool],
        build_command: Callable[[AppState], OutgoingCommand]
    ) -> None:
        """
        Register a state-based output handler.
        
        This allows Rules or other computed state to trigger hardware commands.
        
        Args:
            name: Handler name for logging
            watched_slices: State slices to monitor
            should_send: Function(old_state, new_state) -> True if command needed
            build_command: Function(state) -> OutgoingCommand to send
        """
        handler = OutputHandler(
            name=name,
            watched_slices=watched_slices,
            should_send=should_send,
            build_command=build_command
        )
        self._output_handlers.append(handler)
        logger.info(f"Registered output handler: {name}")
    
    def send_command(self, command: OutgoingCommand) -> bool:
        """
        Send a command directly.
        
        Args:
            command: Command to send
            
        Returns:
            True if sent successfully
        """
        # Log if callback set
        if self._command_log_callback:
            self._command_log_callback(command, "OUT")
        
        success = self._output_port.send(command)
        
        if success:
            self._stats.commands_sent += 1
            self._stats.last_command_time = time.time()
            
            if command.device_id == DEVICE_AVC:
                self._stats.avc_commands += 1
            elif command.device_id >= DEVICE_SATELLITE_BASE:
                self._stats.satellite_commands += 1
        else:
            self._stats.send_failures += 1
            logger.warning(f"Failed to send command: {command}")
        
        return success
    
    # ─────────────────────────────────────────────────────────────────────────
    # Action-based command generation (middleware)
    # ─────────────────────────────────────────────────────────────────────────
    
    def _handle_action(self, action: Action, store: Store) -> None:
        """
        Middleware: Handle actions that should generate commands.
        
        Only processes actions with source=UI (user-initiated).
        """
        # Only send commands for UI-initiated actions
        if action.source != ActionSource.UI:
            return
        
        command = self._action_to_command(action)
        if command:
            self.send_command(command)
    
    def _action_to_command(self, action: Action) -> Optional[OutgoingCommand]:
        """
        Convert a UI action to an outgoing command.
        
        Args:
            action: Action from UI
            
        Returns:
            OutgoingCommand or None if no command needed
        """
        avc_cmd = None
        
        # Audio commands
        if isinstance(action, SetVolumeAction):
            avc_cmd = self._avc_commands.set_volume(action.volume)
        elif isinstance(action, SetBassAction):
            avc_cmd = self._avc_commands.set_bass(action.bass)
        elif isinstance(action, SetMidAction):
            avc_cmd = self._avc_commands.set_mid(action.mid)
        elif isinstance(action, SetTrebleAction):
            avc_cmd = self._avc_commands.set_treble(action.treble)
        elif isinstance(action, SetBalanceAction):
            avc_cmd = self._avc_commands.set_balance(action.balance)
        elif isinstance(action, SetFaderAction):
            avc_cmd = self._avc_commands.set_fader(action.fader)
        elif isinstance(action, SetMuteAction):
            avc_cmd = self._avc_commands.mute_toggle()
        
        # Climate commands
        elif isinstance(action, SetTargetTempAction):
            avc_cmd = self._avc_commands.set_target_temp(action.temp)
        elif isinstance(action, SetFanSpeedAction):
            avc_cmd = self._avc_commands.set_fan_speed(action.speed)
        elif isinstance(action, SetACAction):
            avc_cmd = self._avc_commands.climate_ac_toggle()
        elif isinstance(action, SetAutoModeAction):
            avc_cmd = self._avc_commands.climate_auto_toggle()
        elif isinstance(action, SetRecirculationAction):
            avc_cmd = self._avc_commands.climate_recirc_toggle()
        elif isinstance(action, SetAirDirectionAction):
            avc_cmd = self._avc_commands.set_air_direction(action.direction)
        
        # Convert AVC command to OutgoingCommand
        if avc_cmd is not None:
            gateway_format = avc_cmd.to_gateway_format()
            return OutgoingCommand(
                device_id=DEVICE_AVC,
                command_type=action.type.name.lower(),
                payload=gateway_format
            )
        
        return None
    
    # ─────────────────────────────────────────────────────────────────────────
    # State-based command generation
    # ─────────────────────────────────────────────────────────────────────────
    
    def _on_state_change(self, state: AppState) -> None:
        """
        Handle state changes that may require commands.
        
        Checks registered output handlers and sends commands as needed.
        """
        if not self._output_handlers:
            self._prev_state = state
            return
        
        # Check each output handler
        for handler in self._output_handlers:
            try:
                if handler.should_send(self._prev_state, state):
                    command = handler.build_command(state)
                    self.send_command(command)
                    logger.debug(f"Output handler '{handler.name}' triggered command")
            except Exception as e:
                logger.error(f"Output handler '{handler.name}' error: {e}")
        
        self._prev_state = state


@dataclass
class OutputHandler:
    """
    Configuration for a state-based output handler.
    
    Output handlers monitor state changes and generate commands
    when specific conditions are met (e.g., computed DRL status changes).
    """
    name: str
    watched_slices: Set[StateSlice]
    should_send: Callable[[Optional[AppState], AppState], bool]
    build_command: Callable[[AppState], OutgoingCommand]


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions for creating common output handlers
# ─────────────────────────────────────────────────────────────────────────────

def create_satellite_output_handler(
    name: str,
    device_id: int,
    command_type: str,
    state_getter: Callable[[AppState], Any],
    watched_slices: Set[StateSlice],
    payload_key: str = "value"
) -> OutputHandler:
    """
    Create a simple output handler for a satellite device.
    
    Args:
        name: Handler name
        device_id: Satellite device ID
        command_type: Command type string
        state_getter: Function to get the relevant state value
        watched_slices: State slices to watch
        payload_key: Key to use in payload dict
        
    Returns:
        Configured OutputHandler
    """
    def should_send(old: Optional[AppState], new: AppState) -> bool:
        if old is None:
            return True  # Initial state
        return state_getter(old) != state_getter(new)
    
    def build_command(state: AppState) -> OutgoingCommand:
        value = state_getter(state)
        return OutgoingCommand(
            device_id=device_id,
            command_type=command_type,
            payload={payload_key: value}
        )
    
    return OutputHandler(
        name=name,
        watched_slices=watched_slices,
        should_send=should_send,
        build_command=build_command
    )
