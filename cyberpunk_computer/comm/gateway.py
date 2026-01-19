"""
Gateway connection manager.

Handles serial communication with the RP2040 Gateway.
"""

import threading
import queue
from typing import Optional, Callable, Any
from dataclasses import dataclass

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

from .protocol import Message, parse_message, create_message


@dataclass
class GatewayConfig:
    """Gateway connection configuration."""
    port: str = "/dev/ttyACM0"  # Default Linux USB CDC port
    baudrate: int = 1_000_000
    timeout: float = 0.1


class GatewayConnection:
    """
    Manages serial connection to the Gateway.
    
    Provides async-style message handling with a receive queue
    and background reader thread.
    """
    
    def __init__(self, config: Optional[GatewayConfig] = None):
        """
        Initialize the gateway connection.
        
        Args:
            config: Connection configuration
        """
        self.config = config or GatewayConfig()
        
        self._serial: Optional["serial.Serial"] = None
        self._connected = False
        
        # Message queues
        self._rx_queue: queue.Queue[Message] = queue.Queue()
        self._tx_queue: queue.Queue[str] = queue.Queue()
        
        # Background threads
        self._reader_thread: Optional[threading.Thread] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._running = False
        
        # Message handlers by device ID
        self._handlers: dict[int, list[Callable[[Message], None]]] = {}
    
    @property
    def connected(self) -> bool:
        """Check if connected to Gateway."""
        return self._connected
    
    def connect(self) -> bool:
        """
        Establish connection to the Gateway.
        
        Returns:
            True if connection successful
        """
        if not SERIAL_AVAILABLE:
            print("Warning: pyserial not available, running in mock mode")
            return False
        
        try:
            self._serial = serial.Serial(
                port=self.config.port,
                baudrate=self.config.baudrate,
                timeout=self.config.timeout
            )
            self._connected = True
            self._running = True
            
            # Start background threads
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                daemon=True
            )
            self._reader_thread.start()
            
            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                daemon=True
            )
            self._writer_thread.start()
            
            return True
            
        except Exception as e:
            print(f"Failed to connect to Gateway: {e}")
            return False
    
    def disconnect(self) -> None:
        """Disconnect from the Gateway."""
        self._running = False
        
        if self._serial:
            self._serial.close()
            self._serial = None
        
        self._connected = False
    
    def send(self, device_id: int, data: Any) -> None:
        """
        Send a message to the Gateway.
        
        Args:
            device_id: Target device ID
            data: Payload data
        """
        message = create_message(device_id, data)
        self._tx_queue.put(message)
    
    def receive(self, timeout: float = 0.0) -> Optional[Message]:
        """
        Receive a message from the queue.
        
        Args:
            timeout: Timeout in seconds (0 = non-blocking)
        
        Returns:
            Message if available, None otherwise
        """
        try:
            if timeout > 0:
                return self._rx_queue.get(timeout=timeout)
            else:
                return self._rx_queue.get_nowait()
        except queue.Empty:
            return None
    
    def register_handler(
        self, 
        device_id: int, 
        handler: Callable[[Message], None]
    ) -> None:
        """
        Register a handler for messages from a specific device.
        
        Args:
            device_id: Device ID to handle
            handler: Callback function
        """
        if device_id not in self._handlers:
            self._handlers[device_id] = []
        self._handlers[device_id].append(handler)
    
    def process_messages(self) -> int:
        """
        Process all pending messages through handlers.
        
        Call this in the main loop to handle incoming messages.
        
        Returns:
            Number of messages processed
        """
        count = 0
        while True:
            message = self.receive()
            if message is None:
                break
            
            # Call registered handlers
            handlers = self._handlers.get(message.device_id, [])
            for handler in handlers:
                try:
                    handler(message)
                except Exception as e:
                    print(f"Handler error: {e}")
            
            count += 1
        
        return count
    
    def _reader_loop(self) -> None:
        """Background thread for reading from serial."""
        while self._running and self._serial:
            try:
                line = self._serial.readline()
                if line:
                    message = parse_message(line.decode('utf-8', errors='ignore'))
                    if message:
                        self._rx_queue.put(message)
            except Exception as e:
                if self._running:
                    print(f"Reader error: {e}")
    
    def _writer_loop(self) -> None:
        """Background thread for writing to serial."""
        while self._running and self._serial:
            try:
                message = self._tx_queue.get(timeout=0.1)
                if self._serial:
                    self._serial.write(message.encode('utf-8'))
            except queue.Empty:
                continue
            except Exception as e:
                if self._running:
                    print(f"Writer error: {e}")
