"""
Application entry point.

Usage:
    python -m cyberpunk_computer [options]

Options:
    --dev           Enable development mode (keyboard input, debug info)
    --scale N       Display scale factor (1, 2, or 4) [default: 1]
    --fullscreen    Run in fullscreen mode
    --port PORT     Serial port for Gateway connection
"""

import argparse
import logging
import sys

from .config import Config
from .core.app import Application


def setup_logging(dev_mode: bool = False) -> None:
    """Configure logging for the application."""
    level = logging.DEBUG if dev_mode else logging.INFO
    
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
    
    logging.info("Logging initialized")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="CyberPunk Prius Gen 2 - Onboard Computer"
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
    
    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    args = parse_args()
    
    # Setup logging first
    setup_logging(dev_mode=args.dev)
    
    logger = logging.getLogger(__name__)
    logger.info("CyberPunk Prius Gen 2 - Onboard Computer starting...")
    
    # Determine scale factor (default: 2 for dev mode, 1 otherwise)
    scale = args.scale if args.scale is not None else (2 if args.dev else 1)
    
    # Build configuration from arguments
    config = Config(
        dev_mode=args.dev,
        scale_factor=scale,
        fullscreen=args.fullscreen,
        gateway_port=args.port,
        gateway_enabled=not args.no_gateway
    )
    
    logger.info(f"Config: dev={config.dev_mode}, scale={config.scale_factor}")
    
    # Create and run application
    app = Application(config)
    
    try:
        app.run()
    except KeyboardInterrupt:
        print("\nShutdown requested...")
    finally:
        app.cleanup()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
