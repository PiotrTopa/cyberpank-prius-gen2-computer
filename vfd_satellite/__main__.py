"""
VFD Satellite Display - Entry Point.

Standalone VFD display simulator that receives data via NDJSON protocol.
"""

import argparse
import logging
import sys

import pygame

from . import __version__, __device_id__
from .state import VFDState
from .renderer import VFDRenderer
from .framebuffer import VFD_WIDTH, VFD_HEIGHT
from .receiver import UDPReceiver, SerialReceiver, DemoReceiver

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    """Main entry point for VFD satellite display."""
    parser = argparse.ArgumentParser(
        description=f"VFD Satellite Display v{__version__} (Device ID: {__device_id__})"
    )
    
    # Input source
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        "--udp", action="store_true",
        help="Use UDP receiver (development mode)"
    )
    input_group.add_argument(
        "--serial", type=str, metavar="PORT",
        help="Use serial/RS485 receiver (e.g., /dev/ttyUSB0, COM3)"
    )
    input_group.add_argument(
        "--demo", action="store_true",
        help="Use demo mode with simulated data"
    )
    
    # UDP options
    parser.add_argument(
        "--port", type=int, default=5110,
        help="UDP port to listen on (default: 5110)"
    )
    
    # Serial options
    parser.add_argument(
        "--baudrate", type=int, default=115200,
        help="Serial baudrate (default: 115200)"
    )
    
    # Display options
    parser.add_argument(
        "--scale", type=int, default=2, choices=[1, 2, 3, 4],
        help="Display scale factor (default: 2)"
    )
    parser.add_argument(
        "--fps", type=int, default=60,
        help="Target frame rate (default: 60)"
    )
    
    # Debug options
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose logging"
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Initialize pygame
    pygame.init()
    pygame.display.set_caption(f"VFD Satellite Display v{__version__}")
    
    # Create renderer
    renderer = VFDRenderer(scale=args.scale)
    width, height = renderer.get_size()
    
    # Create display
    screen = pygame.display.set_mode((width, height))
    clock = pygame.time.Clock()
    
    # Create state
    state = VFDState()
    
    # Create receiver based on arguments
    if args.serial:
        receiver = SerialReceiver(port=args.serial, baudrate=args.baudrate)
    elif args.udp:
        receiver = UDPReceiver(port=args.port)
    else:
        # Default to demo mode
        receiver = DemoReceiver()
        logger.info("No input specified, using demo mode")
    
    # Set up message callback
    def on_message(message: dict):
        state.process_message(message)
    
    receiver.set_message_callback(on_message)
    
    try:
        # Start receiver
        receiver.start()
        
        logger.info(f"VFD Satellite Display started (scale={args.scale}x, fps={args.fps})")
        logger.info(f"Display size: {VFD_WIDTH}x{VFD_HEIGHT} pixels")
        logger.info(f"Window size: {width}x{height} pixels")
        
        # Main loop
        running = True
        while running:
            # Handle events
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        running = False
                    elif event.key == pygame.K_q:
                        running = False
            
            # Process any pending messages
            while True:
                msg = receiver.poll()
                if msg is None:
                    break
                state.process_message(msg)
            
            # Update renderer from state
            renderer.update(state)
            
            # Render
            screen.fill((0, 0, 0))
            renderer.render(screen, 0, 0)
            
            # Update display
            pygame.display.flip()
            
            # Cap frame rate
            clock.tick(args.fps)
    
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        receiver.stop()
        pygame.quit()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
