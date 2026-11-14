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
    climate_remote: Optional[object] = None   # comm.avc.ClimateRemote (AVC-LAN climate buttons)
    avc_scheduler: Optional[object] = None    # comm.avc.ThreadScheduler shared by AVC senders
    _enable_solicited: bool = False  # Whether to send solicited CAN subscriptions

    # Solicited subscription watchdog (all monotonic seconds)
    _SOLICITED_TIMEOUT: float = 15.0   # no solicited reply for this long -> re-setup
    _RESUB_COOLDOWN: float = 30.0      # base spacing between watchdog re-setups
    _RESUB_COOLDOWN_MAX: float = 300.0 # backoff cap while the ECUs stay silent
    _GATEWAY_ALIVE_WINDOW: float = 5.0 # a GW_HB this recent = gateway is up

    def __post_init__(self) -> None:
        """Initialize mutable solicited-setup state."""
        import threading
        self._setup_lock = threading.Lock()
        self._setup_pending: Optional[str] = None   # reason, set from any thread
        self._last_setup_mono: float = 0.0
        self._setup_count: int = 0
        self._resub_count: int = 0                  # consecutive watchdog re-setups
        self._last_resub_mono: float = 0.0
        # Passive-source preference: solicited slots whose only signal is
        # being heard passively (BEAN) are unsubscribed while that lasts
        # (comm.source_priority.SlotSuspender).
        from ..comm.source_priority import SlotSuspender
        self._slot_suspender = SlotSuspender(
            self.ingress.sources, self._send_unsub_slot, self._send_sub_slot)
    
    def start(self) -> bool:
        """Start all components."""
        result = self.ingress.start()
        
        # Solicited CAN setup goes out on the engine thread (update()) once
        # the gateway port is actually open. The serial connect hook requests
        # it on every (re)open and the ingress on every gateway (re)boot, so a
        # gateway that is unpowered at backend start (key-off, pending
        # placeholder path) is configured the moment it appears.
        if result and self._enable_solicited and self.output_port.is_connected():
            self.request_solicited_setup("start")
        
        return result

    # ── solicited CAN (gateway) setup lifecycle ─────────────────────────

    def request_solicited_setup(self, reason: str) -> None:
        """Ask for the CAN mode + subscriptions to be (re)sent on the next
        engine tick. Thread-safe; repeated requests coalesce."""
        if not self._enable_solicited:
            return
        with self._setup_lock:
            if self._setup_pending is None:
                self._setup_pending = reason
        logger.info("Solicited CAN setup requested (%s)", reason)

    def _slot_payload(self, slot: int) -> Optional[dict]:
        """The subscription payload of a core or ENABLED toggleable slot."""
        from ..comm.subscription_groups import CORE_GROUP, TOGGLEABLE_GROUPS
        from ..persistence import get_settings
        for s in CORE_GROUP.slots:
            if s.slot == slot:
                return s.payload
        ds = get_settings().data_sources
        for g in TOGGLEABLE_GROUPS:
            for s in g.slots:
                if s.slot == slot:
                    return s.payload if ds.is_group_enabled(g.key) else None
        return None

    def _send_unsub_slot(self, slot: int) -> bool:
        from .ports import OutgoingCommand, DEVICE_CAN
        if not self.output_port.is_connected():
            return False
        return self.output_port.send(OutgoingCommand(
            device_id=DEVICE_CAN, command_type="unsub", payload={"a": "unsub", "slot": slot}))

    def _send_sub_slot(self, slot: int) -> bool:
        from .ports import OutgoingCommand, DEVICE_CAN
        payload = self._slot_payload(slot)
        if payload is None or not self.output_port.is_connected():
            return False
        return self.output_port.send(OutgoingCommand(
            device_id=DEVICE_CAN, command_type="sub", payload=payload))

    def _init_solicited_subscriptions(self, clear_first: bool = False) -> int:
        """
        Send the solicited CAN setup: normal mode, core drivetrain slots, the
        toggleable groups the user enabled. Returns the number of commands
        queued (0 when the port is down — the connect hook re-requests then).

        No sleeps: the serial writer preserves order and the gateway handles
        each line synchronously, and this runs on the engine thread.
        """
        from .ports import OutgoingCommand, DEVICE_CAN
        from ..comm.subscription_groups import CORE_GROUP, TOGGLEABLE_GROUPS
        from ..persistence import get_settings

        if not self.output_port.is_connected():
            logger.warning("Solicited CAN setup skipped: gateway port not connected")
            return 0

        def send(command_type: str, payload: dict) -> bool:
            return self.output_port.send(OutgoingCommand(
                device_id=DEVICE_CAN, command_type=command_type, payload=payload))

        sent = 0
        if clear_first:
            sent += send("unsub", {"a": "unsub", "slot": "all"})
        # Switch to normal (active) CAN mode
        sent += send("mode", {"a": "mode", "m": "normal"})
        
        # Always subscribe core drivetrain (minus the slots a fresh passive
        # source currently covers; the suspender re-adds them when it goes stale)
        suspended = self._slot_suspender.suspended
        for slot_def in CORE_GROUP.slots:
            if slot_def.slot not in suspended:
                sent += send("sub", slot_def.payload)
        
        # Subscribe toggleable groups based on user settings
        ds = get_settings().data_sources
        enabled_names = []
        for group in TOGGLEABLE_GROUPS:
            if ds.is_group_enabled(group.key):
                for slot_def in group.slots:
                    if slot_def.slot not in suspended:
                        sent += send("sub", slot_def.payload)
                enabled_names.append(group.key)
            else:
                logger.debug("Skipping disabled group: %s", group.key)
        
        core_labels = ", ".join(s.label for s in CORE_GROUP.slots)
        logger.info(
            "Solicited CAN setup sent: %d commands (core: %s | enabled: %s)",
            sent, core_labels, ", ".join(enabled_names) or "none",
        )
        return sent

    def _solicited_tick(self) -> None:
        """Engine-thread half of the lifecycle: perform a requested setup, and
        re-setup when the gateway is alive but the subscriptions went quiet
        (ECUs lost the diagnostic session, gateway reboot we did not see, a
        dropped line at setup time)."""
        import time as _time
        now = _time.monotonic()
        stats = self.ingress.stats

        with self._setup_lock:
            reason, self._setup_pending = self._setup_pending, None
        if reason is None and self.output_port.is_connected():
            # passive-source preference: suspend / resume single-signal slots
            try:
                self._slot_suspender.tick()
            except Exception as e:
                logger.error("Solicited slot suspender failed: %s", e)
        if reason is not None:
            n = self._init_solicited_subscriptions(clear_first=False)
            if n:
                self._setup_count += 1
                self._last_setup_mono = now
            else:
                # Port went away between the request and the tick: the next
                # connect hook requests again. Nothing to retry here.
                pass
            return

        # A solicited reply since the last re-setup proves the link works.
        if stats.last_solicited_mono > self._last_resub_mono:
            self._resub_count = 0

        gateway_alive = (now - stats.last_heartbeat_mono) < self._GATEWAY_ALIVE_WINDOW \
            if stats.last_heartbeat_mono else False
        if not gateway_alive or not self.output_port.is_connected():
            return
        reference = max(self._last_setup_mono, stats.last_solicited_mono)
        if reference <= 0.0:
            return      # nothing sent yet (a request is on its way)
        silent_for = now - reference
        if silent_for < self._SOLICITED_TIMEOUT:
            return
        cooldown = min(self._RESUB_COOLDOWN * (2 ** self._resub_count), self._RESUB_COOLDOWN_MAX)
        if (now - self._last_resub_mono) < cooldown:
            return

        self._last_resub_mono = now
        self._resub_count += 1
        logger.warning(
            "Solicited watchdog: gateway alive but no solicited reply for %.0fs — "
            "re-sending CAN setup (attempt #%d, next in >=%.0fs)",
            silent_for, self._resub_count,
            min(self._RESUB_COOLDOWN * (2 ** self._resub_count), self._RESUB_COOLDOWN_MAX),
        )
        try:
            if self._init_solicited_subscriptions(clear_first=True):
                self._setup_count += 1
                self._last_setup_mono = now
        except Exception as e:
            logger.error(f"Solicited watchdog re-setup failed: {e}")

    @property
    def solicited_status(self) -> dict:
        return {
            "enabled": self._enable_solicited,
            "setups": self._setup_count,
            "watchdog_resubs": self._resub_count,
            "pending": self._setup_pending,
            "suspended_slots": sorted(self._slot_suspender.suspended),
            "sources": self.ingress.sources.status(),
        }
    
    def subscribe_group(self, group) -> None:
        """Subscribe to all slots in a subscription group.
        
        Called by the Data Sources screen when the user enables a group.
        
        Args:
            group: SubscriptionGroup instance from comm.subscription_groups
        """
        from .ports import OutgoingCommand, DEVICE_CAN
        
        for slot_def in group.slots:
            if slot_def.slot in self._slot_suspender.suspended:
                continue        # covered by a passive source right now
            self.output_port.send(OutgoingCommand(
                device_id=DEVICE_CAN,
                command_type="sub",
                payload=slot_def.payload,
            ))
        logger.info("Subscribed group: %s (%d slots)", group.key, len(group.slots))
    
    def unsubscribe_group(self, group) -> None:
        """Unsubscribe from all slots in a subscription group.
        
        Called by the Data Sources screen when the user disables a group.
        
        Args:
            group: SubscriptionGroup instance from comm.subscription_groups
        """
        from .ports import OutgoingCommand, DEVICE_CAN
        
        for slot_def in group.slots:
            self.output_port.send(OutgoingCommand(
                device_id=DEVICE_CAN,
                command_type="unsub",
                payload={"a": "unsub", "slot": slot_def.slot},
            ))
        logger.info("Unsubscribed group: %s (%d slots)", group.key, len(group.slots))

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
        if self.avc_scheduler is not None:
            try:
                self.avc_scheduler.stop()
            except Exception:
                pass
        if self.comm_logger:
            self.comm_logger.stop()
    
    def update(self) -> int:
        """
        Process pending messages.
        
        Call this in the main loop.
        
        Returns:
            Number of messages processed
        """
        count = self.ingress.update()

        # Solicited setup / watchdog (engine thread)
        if self._enable_solicited:
            self._solicited_tick()

        return count


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
    
    # Chart data collection (continuous, screen-independent)
    from ..state.rules.chart_data import ChartDataRule
    rules_engine.register(ChartDataRule())
    
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
    
    # AVC-LAN climate remote control (verified button frames, AVC_MAP.md):
    # UI climate actions -> egress -> ClimateRemote -> press/release frames.
    # In replay/dev mode the output port only logs ("[WOULD SEND]").
    from ..comm.avc import ClimateRemote, ThreadScheduler
    from .ports import OutgoingCommand as _OC, DEVICE_AVC as _DEV_AVC

    def _avc_send(device_id: int, payload: dict) -> bool:
        return output_port.send(_OC(device_id=device_id, command_type="avc_tx",
                                    payload=payload, include_cmd=False))

    avc_scheduler = ThreadScheduler(name="avc-climate")
    climate_remote = ClimateRemote(_avc_send, avc_scheduler)
    ingress.avc.add_update_listener(climate_remote.on_update)
    egress.set_climate_remote(climate_remote)

    # Add auto-save middleware for user preference changes
    from .persistence_middleware import create_persistence_middleware
    store.add_middleware(create_persistence_middleware(settings_mgr))
    
    twin = VirtualTwin(
        store=store,
        input_port=input_port,
        output_port=output_port,
        ingress=ingress,
        egress=egress,
        rules_engine=rules_engine,
        mode=config.mode,
        comm_logger=comm_logger,
        climate_remote=climate_remote,
        avc_scheduler=avc_scheduler,
        _enable_solicited=enable_solicited
    )

    if enable_solicited:
        # Re-send the solicited setup whenever the gateway (re)appears or
        # (re)boots: serial (re)open, GATEWAY_READY banner, GW_HB uptime reset.
        if isinstance(input_port, SerialPort):
            input_port.add_connect_hook(
                lambda: twin.request_solicited_setup("gateway serial connected"))
        ingress.add_gateway_reset_listener(
            lambda reason: twin.request_solicited_setup("gateway reset: " + reason))

    return twin


def _create_production_io(config: VirtualTwinConfig):
    """Create production serial IO with optional UDP for satellites."""
    serial_config = SerialConfig(
        port=config.serial_port,
        baudrate=config.serial_baudrate,
        auto_reconnect=config.serial_auto_reconnect,
        reconnect_delay=config.serial_reconnect_delay,
        keepalive_ping='{"id":0,"d":{"a":"ping"}}\n'
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
