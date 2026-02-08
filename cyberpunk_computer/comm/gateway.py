"""
Gateway connection manager.

Handles serial communication with the RP2040 Gateway.
Supports Gateway Protocol v2.8.0 with solicited CAN mode.
"""

import threading
import queue
import logging
from typing import Optional, Callable, Any
from dataclasses import dataclass, field

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

from .protocol import (
    Message, parse_message, create_message, DEVICE_CAN,
    create_can_request, create_can_subscription, create_can_unsubscribe,
    create_can_mode_switch, create_can_list_subs
)

logger = logging.getLogger(__name__)


@dataclass
class GatewayConfig:
    """Gateway connection configuration."""
    port: str = "/dev/ttyACM0"  # Default Linux USB CDC port
    baudrate: int = 1_000_000
    timeout: float = 0.1
    enable_solicited: bool = True  # Enable solicited CAN mode on connect


@dataclass
class CANSubscription:
    """Information about an active CAN subscription."""
    slot: int
    can_id: str
    interval_ms: int
    active: bool = True


class GatewayConnection:
    """
    Manages serial connection to the Gateway.
    
    Provides async-style message handling with a receive queue
    and background reader thread.
    
    Supports Gateway Protocol v2.8.0 features:
    - Passive CAN monitoring (listen-only mode)
    - Active CAN mode with request-response queries
    - Periodic CAN subscriptions for continuous data
    """
    
    # Maximum subscription slots
    MAX_CAN_SLOTS = 16
    
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
        
        # CAN mode and subscriptions
        self._can_mode: str = "listen"
        self._can_subscriptions: dict[int, CANSubscription] = {}
    
    @property
    def can_mode(self) -> str:
        """Current CAN operating mode ('listen' or 'normal')."""
        return self._can_mode
    
    @property
    def can_subscriptions(self) -> dict[int, CANSubscription]:
        """Active CAN subscriptions by slot."""
        return self._can_subscriptions.copy()

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
            
            # Handle system messages for mode/subscription confirmations
            if message.device_id == 0:
                self._handle_system_message(message)
            
            # Call registered handlers
            handlers = self._handlers.get(message.device_id, [])
            for handler in handlers:
                try:
                    handler(message)
                except Exception as e:
                    logger.error(f"Handler error: {e}")
            
            count += 1
        
        return count
    
    def _handle_system_message(self, message: Message) -> None:
        """Handle system messages (mode changes, subscription confirmations)."""
        data = message.data
        if not isinstance(data, dict):
            return
        
        msg_type = data.get("msg", "")
        
        if msg_type == "CAN_MODE":
            self._can_mode = data.get("m", "listen").lower()
            logger.info(f"CAN mode changed to: {self._can_mode}")
        
        elif msg_type == "SUB_OK":
            slot = data.get("slot")
            if slot is not None and slot in self._can_subscriptions:
                self._can_subscriptions[slot].active = True
                logger.debug(f"Subscription confirmed: slot {slot}")
        
        elif msg_type == "UNSUB_OK":
            slot = data.get("slot")
            if slot is not None and slot in self._can_subscriptions:
                del self._can_subscriptions[slot]
                logger.debug(f"Unsubscription confirmed: slot {slot}")
        
        elif msg_type == "UNSUB_ALL":
            self._can_subscriptions.clear()
            logger.info("All subscriptions cleared")
    
    # =========================================================================
    # CAN Solicited Mode Methods (Protocol v2.8.0)
    # =========================================================================
    
    def can_switch_mode(self, mode: str) -> None:
        """
        Switch CAN operating mode.
        
        Args:
            mode: "normal" for active participation, "listen" for passive
        
        Warning:
            Switching to "listen" mode clears all active subscriptions.
        """
        if mode not in ("normal", "listen"):
            raise ValueError("Mode must be 'normal' or 'listen'")
        
        self._tx_queue.put(create_can_mode_switch(mode))
        logger.info(f"Requesting CAN mode switch to: {mode}")
    
    def can_request(
        self,
        can_id: int | str,
        data: list[int],
        response_ids: list[str] | None = None,
        timeout_ms: int = 100
    ) -> None:
        """
        Send a single CAN request and wait for response.
        
        The response will be received through the normal message handler.
        
        Args:
            can_id: Request CAN ID (e.g., 0x7DF for OBD-II broadcast)
            data: Request data bytes
            response_ids: Expected response CAN IDs
            timeout_ms: Response timeout
        """
        self._tx_queue.put(create_can_request(can_id, data, response_ids, timeout_ms))
    
    def can_subscribe(
        self,
        slot: int,
        can_id: int | str,
        data: list[int],
        interval_ms: int = 1000,
        response_ids: list[str] | None = None,
        timeout_ms: int = 100
    ) -> bool:
        """
        Create a periodic CAN subscription.
        
        Args:
            slot: Subscription slot (0-15)
            can_id: Request CAN ID
            data: Request data bytes
            interval_ms: Polling interval in milliseconds
            response_ids: Expected response CAN IDs
            timeout_ms: Response timeout
        
        Returns:
            True if subscription request sent
        """
        if slot >= self.MAX_CAN_SLOTS:
            logger.error(f"Invalid slot {slot}, max is {self.MAX_CAN_SLOTS - 1}")
            return False
        
        can_id_str = can_id if isinstance(can_id, str) else f"0x{can_id:03X}"
        
        self._can_subscriptions[slot] = CANSubscription(
            slot=slot,
            can_id=can_id_str,
            interval_ms=interval_ms,
            active=False  # Will be set True on confirmation
        )
        
        self._tx_queue.put(create_can_subscription(
            slot, can_id, data, interval_ms, response_ids, timeout_ms
        ))
        
        logger.info(f"Subscribing slot {slot}: {can_id_str} @ {interval_ms}ms")
        return True
    
    def can_unsubscribe(self, slot: int | str = "all") -> None:
        """
        Unsubscribe from CAN polling.
        
        Args:
            slot: Slot to unsubscribe (0-15) or "all"
        """
        self._tx_queue.put(create_can_unsubscribe(slot))
        
        if slot == "all":
            logger.info("Unsubscribing from all slots")
        else:
            logger.info(f"Unsubscribing from slot {slot}")
    
    def can_list_subscriptions(self) -> None:
        """Request list of active subscriptions from gateway."""
        self._tx_queue.put(create_can_list_subs())
    
    def obd2_request(self, mode: int, pid: int, ecu: int = 0x7DF) -> None:
        """
        Send an OBD-II request.
        
        Args:
            mode: OBD-II mode (e.g., 0x01 for current data)
            pid: PID within the mode
            ecu: Target ECU (0x7DF for broadcast)
        """
        data = [0x02, mode, pid, 0x00, 0x00, 0x00, 0x00, 0x00]
        response_ids = [f"0x{ecu + 8:03X}"] if ecu != 0x7DF else None
        self.can_request(ecu, data, response_ids)
    
    def obd2_subscribe(
        self,
        slot: int,
        mode: int,
        pid: int,
        interval_ms: int = 1000,
        ecu: int = 0x7DF
    ) -> bool:
        """
        Create a periodic OBD-II subscription.
        
        Args:
            slot: Subscription slot (0-15)
            mode: OBD-II mode
            pid: PID
            interval_ms: Polling interval
            ecu: Target ECU
        
        Returns:
            True if subscription sent
        """
        data = [0x02, mode, pid, 0x00, 0x00, 0x00, 0x00, 0x00]
        response_ids = [f"0x{ecu + 8:03X}"] if ecu != 0x7DF else None
        return self.can_subscribe(slot, ecu, data, interval_ms, response_ids)
    
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
                    logger.error(f"Reader error: {e}")
    
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
                    logger.error(f"Writer error: {e}")
