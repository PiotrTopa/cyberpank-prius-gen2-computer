"""
Serial IO - Production serial/UART port implementations.

These implementations communicate with the RP2040 Gateway over USB CDC serial.

Usage:
    port = SerialPort("/dev/ttyACM0")
    port.start()
    
    while running:
        msg = port.poll()
        if msg:
            ingress.process(msg)
        
        port.send(command)
"""

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional, Any

from .ports import (
    InputPort, OutputPort, BidirectionalPort,
    RawMessage, OutgoingCommand
)

logger = logging.getLogger(__name__)

# Try to import pyserial
try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False
    logger.warning("pyserial not available - serial IO will not work")


@dataclass
class SerialConfig:
    """Serial port configuration."""
    port: str = "/dev/ttyACM0"  # Default Linux USB CDC port
    baudrate: int = 1_000_000
    timeout: float = 0.1


class SerialPort(BidirectionalPort):
    """
    Bidirectional serial port for Gateway communication.
    
    Implements both InputPort and OutputPort for serial UART.
    Runs background threads for non-blocking read/write.
    """
    
    def __init__(self, config: Optional[SerialConfig] = None):
        """
        Initialize serial port.
        
        Args:
            config: Port configuration
        """
        self.config = config or SerialConfig()
        
        self._serial: Optional["serial.Serial"] = None
        self._connected = False
        self._running = False
        
        # Message queues
        self._rx_queue: queue.Queue[RawMessage] = queue.Queue()
        self._tx_queue: queue.Queue[OutgoingCommand] = queue.Queue()
        
        # Background threads
        self._reader_thread: Optional[threading.Thread] = None
        self._writer_thread: Optional[threading.Thread] = None
        
        # Statistics
        self._stats = {
            "rx_messages": 0,
            "tx_messages": 0,
            "rx_errors": 0,
            "tx_errors": 0,
        }
    
    @property
    def stats(self) -> dict:
        """Get IO statistics."""
        return self._stats.copy()
    
    def start(self) -> bool:
        """
        Start serial communication.
        
        Returns:
            True if connection established
        """
        if not SERIAL_AVAILABLE:
            logger.error("pyserial not available")
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
                daemon=True,
                name="SerialReader"
            )
            self._reader_thread.start()
            
            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                daemon=True,
                name="SerialWriter"
            )
            self._writer_thread.start()
            
            logger.info(f"Serial port opened: {self.config.port}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to open serial port: {e}")
            return False
    
    def stop(self) -> None:
        """Stop serial communication and cleanup."""
        self._running = False
        
        # Wait for threads to finish
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=1.0)
        
        if self._serial:
            try:
                self._serial.close()
            except Exception as e:
                logger.error(f"Error closing serial port: {e}")
            self._serial = None
        
        self._connected = False
        logger.info("Serial port closed")
    
    def poll(self) -> Optional[RawMessage]:
        """
        Poll for next received message.
        
        Returns:
            RawMessage if available, None otherwise
        """
        try:
            return self._rx_queue.get_nowait()
        except queue.Empty:
            return None
    
    def send(self, command: OutgoingCommand) -> bool:
        """
        Queue a command for sending.
        
        Args:
            command: Command to send
            
        Returns:
            True if queued successfully
        """
        if not self._connected:
            return False
        
        self._tx_queue.put(command)
        return True
    
    def is_connected(self) -> bool:
        """Check if serial port is connected."""
        return self._connected
    
    @property
    def name(self) -> str:
        return f"SerialPort({self.config.port})"
    
    def _reader_loop(self) -> None:
        """Background thread for reading from serial."""
        while self._running and self._serial:
            try:
                line = self._serial.readline()
                if not line:
                    continue
                
                # Decode and parse
                try:
                    text = line.decode('utf-8', errors='ignore').strip()
                    if not text:
                        continue
                    
                    data = json.loads(text)
                    msg = RawMessage.from_gateway_json(data)
                    self._rx_queue.put(msg)
                    self._stats["rx_messages"] += 1
                    
                except json.JSONDecodeError as e:
                    logger.debug(f"Invalid JSON from serial: {e}")
                    self._stats["rx_errors"] += 1
                    
            except Exception as e:
                if self._running:
                    logger.error(f"Serial reader error: {e}")
                    self._stats["rx_errors"] += 1
    
    def _writer_loop(self) -> None:
        """Background thread for writing to serial."""
        while self._running and self._serial:
            try:
                command = self._tx_queue.get(timeout=0.1)
                
                # Convert to JSON and send
                json_data = command.to_gateway_json()
                line = json.dumps(json_data) + "\n"
                
                if self._serial:
                    self._serial.write(line.encode('utf-8'))
                    self._stats["tx_messages"] += 1
                    
            except queue.Empty:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"Serial writer error: {e}")
                    self._stats["tx_errors"] += 1


# Separate classes for when only input or output is needed

class SerialInputPort(InputPort):
    """
    Read-only serial input port.
    
    Wraps SerialPort for cases where only input is needed.
    """
    
    def __init__(self, port: str = "/dev/ttyACM0", baudrate: int = 1_000_000):
        config = SerialConfig(port=port, baudrate=baudrate)
        self._port = SerialPort(config)
    
    def start(self) -> bool:
        return self._port.start()
    
    def stop(self) -> None:
        self._port.stop()
    
    def poll(self) -> Optional[RawMessage]:
        return self._port.poll()
    
    def is_connected(self) -> bool:
        return self._port.is_connected()
    
    @property
    def name(self) -> str:
        return f"SerialInputPort({self._port.config.port})"


class SerialOutputPort(OutputPort):
    """
    Write-only serial output port.
    
    Wraps SerialPort for cases where only output is needed.
    Note: In practice, serial is bidirectional, so this starts
    the full serial connection but only exposes send().
    """
    
    def __init__(self, port: str = "/dev/ttyACM0", baudrate: int = 1_000_000):
        config = SerialConfig(port=port, baudrate=baudrate)
        self._port = SerialPort(config)
        self._started = False
    
    def start(self) -> bool:
        """Start the underlying serial connection."""
        self._started = self._port.start()
        return self._started
    
    def stop(self) -> None:
        """Stop the underlying serial connection."""
        self._port.stop()
        self._started = False
    
    def send(self, command: OutgoingCommand) -> bool:
        if not self._started:
            # Auto-start on first send
            if not self.start():
                return False
        return self._port.send(command)
    
    def is_connected(self) -> bool:
        return self._port.is_connected()
    
    @property
    def name(self) -> str:
        return f"SerialOutputPort({self._port.config.port})"
