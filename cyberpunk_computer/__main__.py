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
import sys

from .config import Config
from .core.app import Application


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
        default=1,
        help="Display scale factor (1, 2, or 4)"
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
    
    # Build configuration from arguments
    config = Config(
        dev_mode=args.dev,
        scale_factor=args.scale,
        fullscreen=args.fullscreen,
        gateway_port=args.port,
        gateway_enabled=not args.no_gateway
    )
    
    # If dev mode, default to 2x scale if not specified
    if args.dev and args.scale == 1:
        config.scale_factor = 2
    
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
