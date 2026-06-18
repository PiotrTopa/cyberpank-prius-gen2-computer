"""
Frontend entry point — pygame UI driven by the backend network API.

Usage:
    python -m cyberpunk_computer.frontend --host 10.200.0.5 [--port 8080] [options]

The UI code is identical to the local app; only the data source differs: instead
of a local VirtualTwin talking to serial hardware, a RemoteTwin streams state
from the headless backend and forwards UI actions back as REST commands.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from ..config import Config
from ..core.app import Application
from . import RemoteTwin


def _setup_logging(verbose: int) -> None:
    level = logging.DEBUG if verbose >= 1 else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="cyberpunk_computer.frontend",
        description="Remote pygame frontend for the CyberPunk Prius backend.",
    )
    parser.add_argument("--host", required=True, help="backend host (IP or name)")
    parser.add_argument("--port", type=int, default=8080, help="backend API port")
    parser.add_argument(
        "--token",
        default=None,
        help="backend bearer token (or set BACKEND_AUTH_TOKEN)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="REST fallback poll interval / WS read timeout (seconds)",
    )
    parser.add_argument("--scale", type=int, choices=[1, 2, 4], default=2, help="display scale")
    parser.add_argument("--fullscreen", action="store_true", help="run fullscreen")
    parser.add_argument("--dev", action="store_true", help="development mode (keyboard input)")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="verbose logging")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    config = Config(
        dev_mode=args.dev,
        scale_factor=args.scale,
        fullscreen=args.fullscreen,
        gateway_enabled=False,
    )

    twin = RemoteTwin(
        host=args.host,
        port=args.port,
        token=args.token or os.environ.get("BACKEND_AUTH_TOKEN"),
        poll_interval=args.poll_interval,
    )

    app = Application(config)
    app.set_virtual_twin(twin)
    logger.info("Frontend connected to backend %s:%d", args.host, args.port)

    try:
        app.run()
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        twin.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
