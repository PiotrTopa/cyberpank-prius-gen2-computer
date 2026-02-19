"""
Application entry point.

Usage:
    python -m cyberpunk_computer [options]

Options:
    --dev               Enable development mode (keyboard input, debug info)
    -v, --verbose       Increase verbosity (-v=DEBUG, -vv=TRACE with IO details)
    --log-state         Enable [STATE] change logging
    --log-in            Log incoming messages from Gateway
    --log-out           Log outgoing commands to Gateway
    --filter-device IDs Filter messages by device (can,avc,sat,sys or IDs: 0=SYS,1=CAN,2=AVC,110=VFD)
    --scale N           Display scale factor (1, 2, or 4) [default: 1]
    --fullscreen        Run in fullscreen mode
    --port PORT         Serial port for Gateway connection
    --baudrate BAUD     Serial port baudrate [default: 1000000]
    --test              Enable test mode with mock events (keyboard control)
    --replay FILE       Replay log file (NDJSON format, supports AVC-LAN and CAN)

Examples:
    python -m cyberpunk_computer --dev --scale 2
    python -m cyberpunk_computer --dev --port COM9 --baudrate 115200
    python -m cyberpunk_computer --dev --log-in --filter-device avc --port COM9 --baudrate 115200
    python -m cyberpunk_computer --dev --log-state --port COM9 --baudrate 115200
    python -m cyberpunk_computer --dev -vv --port COM9 --baudrate 115200
    python -m cyberpunk_computer --replay assets/data/avc_lan.ndjson
    python -m cyberpunk_computer --replay assets/data/can_1.ndjson --dev
"""

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config
from .core.app import Application
from .io import (
    VirtualTwin, VirtualTwinConfig, ExecutionMode,
    create_virtual_twin
)


def setup_logging(dev_mode: bool = False, production_mode: bool = False, verbosity: int = 0) -> None:
    """
    Configure logging for the application.
    
    Args:
        dev_mode: Enable development mode
        production_mode: Enable production mode
        verbosity: Verbosity level (0=INFO, 1=DEBUG, 2=TRACE with IO details)
    """
    # Determine log level based on verbosity, not dev_mode
    if production_mode:
        level = logging.INFO
    elif verbosity >= 1:
        level = logging.DEBUG
    else:
        level = logging.INFO
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S'
    )
    
    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    
    # Root logger
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    
    # Add file handler in production mode
    if production_mode:
        log_dir = Path("/var/log/cyberpunk_computer")
        if log_dir.exists():
            log_file = log_dir / "app.log"
            try:
                file_handler = RotatingFileHandler(
                    log_file,
                    maxBytes=10 * 1024 * 1024,  # 10 MB
                    backupCount=5
                )
                file_handler.setLevel(level)
                file_handler.setFormatter(formatter)
                root.addHandler(file_handler)
                logging.info(f"File logging enabled: {log_file}")
            except Exception as e:
                logging.warning(f"Could not enable file logging: {e}")
    
    # Suppress noisy loggers based on verbosity level
    if verbosity == 0:
        # Default: Only show STATE changes, suppress DEBUG from all modules
        logging.getLogger('cyberpunk_computer.io').setLevel(logging.INFO)
        logging.getLogger('cyberpunk_computer.state.rules').setLevel(logging.INFO)
    elif verbosity == 1:
        # -v: Show DEBUG but suppress noisy IO details
        logging.getLogger('cyberpunk_computer.io.factory').setLevel(logging.INFO)
        logging.getLogger('cyberpunk_computer.io.udp_output').setLevel(logging.INFO)
        logging.getLogger('cyberpunk_computer.io.egress').setLevel(logging.INFO)
    # verbosity >= 2: Show everything at DEBUG level
    
    logging.info("Logging initialized")


def _parse_device_list(device_str: str) -> set:
    """
    Parse comma-separated device list string to set of device IDs.
    
    Supports names: sys, can, avc, sat, system, satellite
    Supports numeric IDs: 0, 1, 2, 110, etc.
    """
    devices = set()
    for part in device_str.lower().split(','):
        part = part.strip()
        if part == 'sys' or part == 'system':
            devices.add(0)
        elif part == 'can':
            devices.add(1)
        elif part == 'avc':
            devices.add(2)
        elif part == 'sat' or part == 'satellite':
            devices.add(110)  # VFD satellite ID
        elif part.isdigit():
            devices.add(int(part))
    return devices


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="CyberPunk Prius Gen 2 - Onboard Computer"
    )
    parser.add_argument(
        "--production",
        action="store_true",
        help="Enable production mode (for deployment to RPI)"
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Enable development mode"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (can be used multiple times: -v for DEBUG, -vv for TRACE)"
    )
    parser.add_argument(
        "--log-state",
        action="store_true",
        help="Enable state change logging (shows [STATE] messages)"
    )
    parser.add_argument(
        "--log-in",
        action="store_true",
        help="Log incoming messages from Gateway"
    )
    parser.add_argument(
        "--log-out",
        action="store_true",
        help="Log outgoing commands to Gateway"
    )
    parser.add_argument(
        "--filter-device",
        type=str,
        default=None,
        help="Filter messages by device ID (comma-separated: 0=SYSTEM,1=CAN,2=AVC,110+=SATELLITE or use names: can,avc,sat)"
    )
    parser.add_argument(
        "--scale",
        type=int,
        choices=[1, 2, 4],
        default=None,
        help="Display scale factor (1, 2, or 4). Default: 2 in dev mode, 1 otherwise"
    )
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="Run in fullscreen mode"
    )
    parser.add_argument(
        "--port",
        type=str,
        default=None,
        help="Serial port for Gateway connection"
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=1_000_000,
        help="Serial port baudrate (default: 1000000)"
    )
    parser.add_argument(
        "--no-gateway",
        action="store_true",
        help="Run without Gateway connection (UI only)"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Enable test mode with mock events (keyboard: 1-4 vehicle, +/- volume, [/] temp)"
    )
    parser.add_argument(
        "--replay",
        type=str,
        default=None,
        help="Replay log file (NDJSON format, supports AVC-LAN and CAN recordings)"
    )
    parser.add_argument(
        "--log-commands",
        action="store_true",
        help="Log all outgoing commands to console"
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Enable communication logging to file for later replay"
    )
    parser.add_argument(
        "--record-dir",
        type=str,
        default="./logs",
        help="Directory for communication log files (default: ./logs)"
    )
    parser.add_argument(
        "--record-devices",
        type=str,
        default=None,
        help="Only record specific devices (comma-separated: can,avc,sys,sat or numeric IDs)"
    )
    parser.add_argument(
        "--debug-solicited",
        action="store_true",
        help="Debug solicited CAN responses (0x7E8, 0x7EA, 0x7EB) - print to console"
    )
    parser.add_argument(
        "--no-solicited",
        action="store_true",
        help="Disable solicited CAN subscriptions (use only unsolicited data)"
    )
    
    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    args = parse_args()
    
    # Setup logging first
    setup_logging(dev_mode=args.dev, production_mode=args.production, verbosity=args.verbose)
    
    logger = logging.getLogger(__name__)
    logger.info("CyberPunk Prius Gen 2 - Onboard Computer starting...")
    
    # Determine scale factor
    if args.scale is not None:
        scale = args.scale
    elif args.production:
        scale = 1  # Native resolution for production
    elif args.dev:
        scale = 2  # Larger for development
    else:
        scale = 1
    
    # Determine fullscreen
    fullscreen = args.fullscreen or args.production
    
    # Parse device filter
    filter_devices = None
    if args.filter_device:
        filter_devices = _parse_device_list(args.filter_device)
        logger.info(f"Device filter: {filter_devices}")
    
    # Parse record device filter
    record_devices = None
    if args.record_devices:
        record_devices = list(_parse_device_list(args.record_devices))
        logger.info(f"Record device filter: {record_devices}")
    
    # Build configuration from arguments
    config = Config(
        dev_mode=args.dev,
        scale_factor=scale,
        fullscreen=fullscreen,
        gateway_port=args.port,
        gateway_enabled=not args.no_gateway and not args.test and not args.replay,
        log_incoming=args.log_in,
        log_outgoing=args.log_out,
        filter_devices=filter_devices,
        log_commands=args.log_commands
    )
    
    logger.info(f"Config: dev={config.dev_mode}, scale={config.scale_factor}, production={args.production}")
    
    # Create Virtual Twin based on mode
    virtual_twin = None
    if args.replay:
        # Development mode with file replay
        twin_config = VirtualTwinConfig(
            mode=ExecutionMode.DEVELOPMENT,
            replay_file=args.replay,
            playback_speed=1.0,
            verbose=args.dev,
            log_commands=True,
            log_state_changes=args.log_state
        )
        virtual_twin = create_virtual_twin(twin_config)
        logger.info(f"Created Virtual Twin in DEVELOPMENT mode with replay: {args.replay}")
        
        # Show keyboard shortcuts (using ASCII for Windows compatibility)
        print("""
    ===================================================================
    REPLAY MODE - Keyboard Shortcuts
    ===================================================================
    P         Play/Pause playback
    R         Restart from beginning
    S         Print message statistics
    
    J         Jump to row (enter row number)
    [/]       Step backward/forward 1 message
    -/+       Step backward/forward 10 messages
    
    --- Direction Filters ---
    V         Toggle ALL verbose logging (IN + OUT)
    I         Toggle incoming message logging only
    O         Toggle outgoing commands logging only
    T         Toggle STATE change logging
    
    --- Source Filters ---
    1         Toggle AVC-LAN messages
    2         Toggle CAN messages  
    3         Toggle RS485/Satellite messages
    0         Toggle ALL sources on/off
    
    --- Analysis Mode ---
    A         Toggle ANALYSIS mode (detailed reverse-engineering output)
              Shows: Button presses, Touch events, Energy packets
              (A00->258), ICE status (210->490)
    
    ESC       Exit application
    ===================================================================
""")
    elif args.production or (not args.no_gateway and not args.test):
        # Production mode with serial
        twin_config = VirtualTwinConfig(
            mode=ExecutionMode.PRODUCTION,
            serial_port=args.port or "/dev/ttyACM0",
            serial_baudrate=args.baudrate,
            serial_auto_reconnect=args.production,  # Enable auto-reconnect in production
            serial_reconnect_delay=2.0,
            enable_vfd_satellite=True,
            verbose=args.dev,
            log_commands=args.dev,  # Only log commands in dev mode
            log_state_changes=args.log_state,
            # Communication recording for replay
            enable_comm_logging=args.record,
            comm_log_dir=args.record_dir if args.record else None,
            comm_log_devices=record_devices,
            # Solicited CAN mode
            enable_solicited_can=not args.no_solicited
        )
        virtual_twin = create_virtual_twin(twin_config)
        mode_str = "PRODUCTION" if args.production else "STANDARD"
        logger.info(f"Created Virtual Twin in {mode_str} mode")
        
        # Enable solicited debug if requested
        if args.debug_solicited:
            virtual_twin.ingress.set_solicited_debug(True)
            print("\n[DEBUG] Solicited CAN debug enabled - showing 0x7E8/0x7EA/0x7EB responses\n")
        
        # Show record info if enabled
        if args.record:
            from pathlib import Path
            log_dir = Path(args.record_dir).resolve()
            logger.info(f"Communication recording enabled, logs in: {log_dir}")
            print(f"\n[RECORD] Saving communication log to: {log_dir}/")
            print("[RECORD] Use --replay <file> to replay later\n")
    
    # Create and run application
    app = Application(config)
    
    # Apply logging configuration
    if config.log_incoming:
        app._verbose_in = True
    if config.log_outgoing:
        app._verbose_out = True
    
    # Apply device filters
    if config.filter_devices is not None:
        # Disable all by default
        app._verbose_avc = False
        app._verbose_can = False
        app._verbose_sat = False
        app._verbose_sys = False
        
        # Enable only filtered devices
        if 0 in config.filter_devices:
            app._verbose_sys = True
        if 1 in config.filter_devices:
            app._verbose_can = True
        if 2 in config.filter_devices:
            app._verbose_avc = True
        if any(d >= 100 for d in config.filter_devices):
            app._verbose_sat = True
    
    # Connect Virtual Twin to app
    if virtual_twin:
        app.set_virtual_twin(virtual_twin)
    
    try:
        app.run()
    except KeyboardInterrupt:
        print("\nShutdown requested...")
    finally:
        app.cleanup()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
