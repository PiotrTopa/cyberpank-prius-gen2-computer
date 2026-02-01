"""
VFD Receiver - NDJSON message receiver for UDP and Serial.

Receives messages from the main CyberPunk Computer application.
"""

import json
import socket
import threading
import logging
from typing import Optional, Callable
from queue import Queue, Empty

logger = logging.getLogger(__name__)


class UDPReceiver:
    """
    UDP-based NDJSON receiver for development.
    
    Listens on a UDP port for NDJSON messages.
    """
    
    def __init__(self, port: int = 5110, host: str = "0.0.0.0"):
        """
        Initialize UDP receiver.
        
        Args:
            port: UDP port to listen on
            host: Host address to bind to
        """
        self.port = port
        self.host = host
        
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._message_queue: Queue = Queue()
        
        # Callback for received messages
        self._on_message: Optional[Callable[[dict], None]] = None
    
    def start(self) -> None:
        """Start the UDP receiver."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.settimeout(0.1)  # 100ms timeout for graceful shutdown
        self._socket.bind((self.host, self.port))
        
        self._running = True
        self._thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._thread.start()
        
        logger.info(f"UDP receiver started on {self.host}:{self.port}")
    
    def stop(self) -> None:
        """Stop the UDP receiver."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._socket:
            self._socket.close()
        logger.info("UDP receiver stopped")
    
    def set_message_callback(self, callback: Callable[[dict], None]) -> None:
        """Set callback for received messages."""
        self._on_message = callback
    
    def poll(self) -> Optional[dict]:
        """Poll for next available message."""
        try:
            return self._message_queue.get_nowait()
        except Empty:
            return None
    
    def _receive_loop(self) -> None:
        """Background receive loop."""
        buffer = ""
        
        while self._running:
            try:
                data, addr = self._socket.recvfrom(4096)
                buffer += data.decode('utf-8')
                
                # Process complete lines
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    if line.strip():
                        try:
                            message = json.loads(line)
                            self._message_queue.put(message)
                            if self._on_message:
                                self._on_message(message)
                        except json.JSONDecodeError as e:
                            logger.warning(f"Invalid JSON: {e}")
                            
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"Receive error: {e}")


class SerialReceiver:
    """
    Serial/RS485 NDJSON receiver for production.
    
    Receives messages from RS485 bus via serial port.
    """
    
    def __init__(self, port: str, baudrate: int = 115200):
        """
        Initialize serial receiver.
        
        Args:
            port: Serial port path (e.g., /dev/ttyUSB0, COM3)
            baudrate: Serial baudrate
        """
        self.port = port
        self.baudrate = baudrate
        
        self._serial = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._message_queue: Queue = Queue()
        self._on_message: Optional[Callable[[dict], None]] = None
    
    def start(self) -> None:
        """Start the serial receiver."""
        try:
            import serial
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=0.1
            )
            
            self._running = True
            self._thread = threading.Thread(target=self._receive_loop, daemon=True)
            self._thread.start()
            
            logger.info(f"Serial receiver started on {self.port} @ {self.baudrate}")
        except ImportError:
            raise RuntimeError("pyserial not installed. Run: pip install pyserial")
        except Exception as e:
            raise RuntimeError(f"Failed to open serial port: {e}")
    
    def stop(self) -> None:
        """Stop the serial receiver."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._serial:
            self._serial.close()
        logger.info("Serial receiver stopped")
    
    def set_message_callback(self, callback: Callable[[dict], None]) -> None:
        """Set callback for received messages."""
        self._on_message = callback
    
    def poll(self) -> Optional[dict]:
        """Poll for next available message."""
        try:
            return self._message_queue.get_nowait()
        except Empty:
            return None
    
    def _receive_loop(self) -> None:
        """Background receive loop."""
        buffer = ""
        
        while self._running:
            try:
                if self._serial.in_waiting > 0:
                    data = self._serial.read(self._serial.in_waiting)
                    buffer += data.decode('utf-8')
                    
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        if line.strip():
                            try:
                                message = json.loads(line)
                                self._message_queue.put(message)
                                if self._on_message:
                                    self._on_message(message)
                            except json.JSONDecodeError as e:
                                logger.warning(f"Invalid JSON: {e}")
                                
            except Exception as e:
                if self._running:
                    logger.error(f"Serial receive error: {e}")


class DemoReceiver:
    """
    Demo receiver that generates simulated data.
    
    For testing without a connected main application.
    """
    
    def __init__(self):
        """Initialize demo receiver."""
        self._message_queue: Queue = Queue()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._on_message: Optional[Callable[[dict], None]] = None
        
        # Simulation state
        self._time = 0.0
        self._ice_running = False
    
    def start(self) -> None:
        """Start demo data generation."""
        import time
        
        self._running = True
        self._thread = threading.Thread(target=self._generate_loop, daemon=True)
        self._thread.start()
        
        logger.info("Demo receiver started")
    
    def stop(self) -> None:
        """Stop demo receiver."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        logger.info("Demo receiver stopped")
    
    def set_message_callback(self, callback: Callable[[dict], None]) -> None:
        """Set callback for received messages."""
        self._on_message = callback
    
    def poll(self) -> Optional[dict]:
        """Poll for next available message."""
        try:
            return self._message_queue.get_nowait()
        except Empty:
            return None
    
    def _generate_loop(self) -> None:
        """Generate demo data."""
        import time
        import math
        
        # Send initial config
        config_msg = {"id": 110, "d": {"t": "C", "tb": 60, "bri": 100}}
        self._message_queue.put(config_msg)
        
        # Send initial state
        state_msg = {"id": 110, "d": {"t": "S", "fuel": "PTR", "gear": "D", "rdy": True}}
        self._message_queue.put(state_msg)
        
        while self._running:
            self._time += 0.05
            
            # Simulate driving patterns
            phase = self._time % 30.0  # 30 second cycle
            
            if phase < 10:
                # Accelerating
                mg_power = 0.3 + 0.2 * math.sin(phase * 0.5)
                speed = min(0.8, phase / 12.0)
                fuel_flow = 0.4
                brake = 0.0
                ice_running = True
            elif phase < 15:
                # Coasting
                mg_power = 0.05
                speed = 0.7
                fuel_flow = 0.1
                brake = 0.0
                ice_running = False
            elif phase < 22:
                # Braking / Regen
                mg_power = -0.5 + 0.2 * math.sin(phase * 0.8)
                speed = max(0.1, 0.7 - (phase - 15) / 10.0)
                fuel_flow = 0.0
                brake = 0.4
                ice_running = False
            else:
                # Stopped, charging
                mg_power = -0.2
                speed = 0.0
                fuel_flow = 0.0
                brake = 0.0
                ice_running = True
            
            # SOC slowly fluctuates
            soc = 0.55 + 0.1 * math.sin(self._time * 0.1)
            
            # Generate energy message
            energy_msg = {
                "id": 110,
                "d": {
                    "t": "E",
                    "mg": round(mg_power, 2),
                    "fl": round(fuel_flow, 2),
                    "br": round(brake, 2),
                    "spd": round(speed, 2),
                    "soc": round(soc, 2),
                    "ptr": 25,
                    "lpg": 42,
                    "ice": ice_running
                }
            }
            
            self._message_queue.put(energy_msg)
            if self._on_message:
                self._on_message(energy_msg)
            
            time.sleep(0.05)  # 20Hz update rate
