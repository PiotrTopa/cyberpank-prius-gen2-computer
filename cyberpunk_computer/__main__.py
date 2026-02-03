"""
Application entry point.

Usage:
    python -m cyberpunk_computer [options]

Options:
    --dev           Enable development mode (keyboard input, debug info)
    --scale N       Display scale factor (1, 2, or 4) [default: 1]
    --fullscreen    Run in fullscreen mode
    --port PORT     Serial port for Gateway connection
    --test          Enable test mode with mock events (keyboard control)
    --replay FILE   Replay log file (NDJSON format, supports AVC-LAN and CAN)

Examples:
    python -m cyberpunk_computer --dev --scale 2
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


def setup_logging(dev_mode: bool = False, production_mode: bool = False) -> None:
    """Configure logging for the application."""
    # Determine log level
    if production_mode:
        level = logging.INFO
    elif dev_mode:
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
    
    logging.info("Logging initialized")


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
    
    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    args = parse_args()
    
    # Setup logging first
    setup_logging(dev_mode=args.dev, production_mode=args.production)
    
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
    
    # Build configuration from arguments
    config = Config(
        dev_mode=args.dev,
        scale_factor=scale,
        fullscreen=fullscreen,
        gateway_port=args.port,
        gateway_enabled=not args.no_gateway and not args.test and not args.replay
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
            log_commands=True
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
            serial_baudrate=1_000_000,
            serial_auto_reconnect=args.production,  # Enable auto-reconnect in production
            serial_reconnect_delay=2.0,
            enable_vfd_satellite=True,
            verbose=args.dev,
            log_commands=args.dev  # Only log commands in dev mode
        )
        virtual_twin = create_virtual_twin(twin_config)
        mode_str = "PRODUCTION" if args.production else "STANDARD"
        logger.info(f"Created Virtual Twin in {mode_str} mode")
    
    # Create and run application
    app = Application(config)
    
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
