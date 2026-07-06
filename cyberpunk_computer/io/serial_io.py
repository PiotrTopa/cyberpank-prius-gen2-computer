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
    auto_reconnect: bool = True  # Auto-reconnect on disconnect
    reconnect_delay: float = 2.0  # Seconds between reconnect attempts
    keepalive_ping: Optional[str] = None  # Ping message to send when idle


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
        self._serial_lock = threading.Lock()
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
            "reconnects": 0,
        }
        
        # Reconnection state
        self._last_reconnect_attempt = 0.0
    
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
            s = serial.Serial(
                port=self.config.port,
                baudrate=self.config.baudrate,
                timeout=self.config.timeout
            )
            with self._serial_lock:
                self._serial = s
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
        
        with self._serial_lock:
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
        while self._running:
            # Check if we need to reconnect
            if not self._connected and self.config.auto_reconnect:
                self._attempt_reconnect()
            
            with self._serial_lock:
                s = self._serial
            if s is None or not self._connected:
                time.sleep(0.1)
                continue
            
            try:
                line = s.readline()
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
                    
            except (serial.SerialException, OSError, TypeError) as e:
                # Connection lost - mark as disconnected
                if self._running:
                    logger.warning(f"Serial connection lost: {e}")
                    self._handle_disconnect()
                    
            except Exception as e:
                if self._running:
                    logger.error(f"Serial reader error: {e}", exc_info=True)
                    self._stats["rx_errors"] += 1
    
    def _writer_loop(self) -> None:
        """Background thread for writing to serial."""
        while self._running:
            with self._serial_lock:
                s = self._serial
            if s is None or not self._connected:
                # Drain queue while disconnected to prevent buildup
                try:
                    self._tx_queue.get_nowait()
                except queue.Empty:
                    pass
                time.sleep(0.1)
                continue
            
            try:
                command = self._tx_queue.get(timeout=2.0)
            except queue.Empty:
                # If TX queue is empty, send a ping to keep gateway awake (if configured)
                if self.config.keepalive_ping and s is not None and self._connected:
                    try:
                        s.write(self.config.keepalive_ping.encode('utf-8'))
                        s.flush()
                    except Exception:
                        pass
                continue
                
            try:
                # Convert to JSON and send
                json_data = command.to_gateway_json()
                line = json.dumps(json_data) + "\n"
                
                if s is not None and self._connected:
                    s.write(line.encode('utf-8'))
                    s.flush()
                    self._stats["tx_messages"] += 1
                    logger.debug(f"TX [{command.device_id}:{command.command_type}]: {line.strip()[:120]}")
                    
            except queue.Empty:
                continue
            except (serial.SerialException, OSError, TypeError) as e:
                # Connection lost
                if self._running:
                    logger.warning(f"Serial write failed: {e}")
                    self._handle_disconnect()
            except Exception as e:
                if self._running:
                    logger.error(f"Serial writer error: {e}")
                    self._stats["tx_errors"] += 1
    
    def _handle_disconnect(self) -> None:
        """Handle serial port disconnection."""
        self._connected = False
        with self._serial_lock:
            if self._serial:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
        logger.info("Serial port disconnected")
    
    def _attempt_reconnect(self) -> bool:
        """
        Attempt to reconnect to serial port.
        
        Returns:
            True if reconnection successful
        """
        now = time.time()
        if now - self._last_reconnect_attempt < self.config.reconnect_delay:
            return False
        
        self._last_reconnect_attempt = now
        
        try:
            logger.debug(f"Attempting to reconnect to {self.config.port}...")
            s = serial.Serial(
                port=self.config.port,
                baudrate=self.config.baudrate,
                timeout=self.config.timeout
            )
            with self._serial_lock:
                self._serial = s
            self._connected = True
            self._stats["reconnects"] += 1
            logger.info(f"Serial port reconnected successfully (attempt #{self._stats['reconnects']})")
            return True
            
        except Exception as e:
            logger.debug(f"Reconnection failed: {e}")
            return False

    def retarget(self, new_port: str) -> None:
        """
        Point this port at a different device path and force a reconnect.

        Used by USB hotplug discovery: when the powerbox/gateway re-enumerates
        on a new ``/dev/ttyACM*`` (or is plugged in for the first time after
        boot), the monitor calls ``retarget`` so the reader loop reconnects to
        the correct device instead of the stale path. Safe to call from another
        thread; the reader loop owns the actual (re)connect.
        """
        if new_port == self.config.port and self._connected:
            logger.debug(f"retarget no-op: already on {new_port}")
            return

        logger.info(f"Retargeting serial port {self.config.port} -> {new_port}")
        self.config.port = new_port
        # Drop any current connection and clear the backoff so the reader loop
        # reconnects to the new path on its next iteration.
        self._handle_disconnect()
        self._last_reconnect_attempt = 0.0

    def force_reconnect(self, attempt: int = 1) -> None:
        """Force the link down and trigger USB-level recovery.

        Used by the backend's link-staleness watchdog to recover a *silently
        wedged* USB-CDC link.  The MicroPython RP2040 CDC wedge is a host/link
        level IN-endpoint stall: the board keeps running and a plain
        close/reopen of the tty does NOT reset the MCU or re-enumerate the
        device on MicroPython, so it never clears the wedge.  The only thing
        that reliably recovers the link is re-enumerating the device via a
        parent-hub reset, so we escalate straight to the hub reset on the very
        first attempt instead of wasting a full staleness cycle (~20 s) on a
        reopen that cannot help.

        (A true VBUS power-cycle would be more forceful still, but it cold-boots
        the bus-powered powerbox and would collapse the self-latched OUT1 rail
        when ACC is off, killing the whole computer — hence the gentler
        driver-level unbind/bind here.)

        Safe to call from another thread; the reader loop owns the actual reopen.
        """
        import os

        logger.warning(
            f"Forcing serial reconnect on {self.config.port} "
            f"(staleness watchdog recovery, attempt #{attempt})"
        )

        tty_name = None
        try:
            real_port = os.path.realpath(self.config.port)
            tty_name = os.path.basename(real_port)
        except Exception:
            pass

        device_present = (
            tty_name is not None
            and os.path.exists(f"/sys/class/tty/{tty_name}/device")
        )

        reason = "gone from bus" if not device_present else "wedged link"
        hub_id = self._find_parent_hub_id(tty_name)
        if hub_id:
            logger.warning(
                "Device %s %s — resetting parent USB hub %s",
                self.config.port, reason, hub_id,
            )
            self._reset_usb_hub(hub_id)
        else:
            logger.error(
                "Device %s %s and cannot determine parent hub; "
                "manual intervention required.",
                self.config.port, reason,
            )

        # Drop the wedged handle so the reader loop reopens on its next
        # iteration.  After a hub reset the UsbSerialMonitor will detect the
        # re-enumerated device and call retarget().
        self._handle_disconnect()
        self._last_reconnect_attempt = time.time()

    # ------------------------------------------------------------------
    # USB hub helpers
    # ------------------------------------------------------------------

    # Cache the hub id so we can still reset it after the device disappears.
    _cached_hub_id: Optional[str] = None

    def _find_parent_hub_id(self, tty_name: Optional[str] = None) -> Optional[str]:
        """Return the sysfs bus-id of the parent USB hub (e.g. ``1-1``).

        Tries the live sysfs path first; falls back to a cached value from a
        previous successful lookup (the device may have already disappeared).
        """
        import os

        hub_id = None
        if tty_name:
            device_dir = f"/sys/class/tty/{tty_name}/device"
            if os.path.exists(device_dir):
                try:
                    # device_dir resolves to e.g. .../1-1.1:1.0
                    # parent of parent is the hub: .../1-1
                    iface_path = os.path.realpath(device_dir)      # .../1-1.1:1.0
                    usb_dev_path = os.path.dirname(iface_path)      # .../1-1.1
                    hub_path = os.path.dirname(usb_dev_path)         # .../1-1
                    hub_id = os.path.basename(hub_path)
                    # Validate: it should look like "1-1", not "usb1"
                    if hub_id.startswith("usb"):
                        hub_id = None
                except Exception:
                    pass

        if hub_id:
            self._cached_hub_id = hub_id
        elif self._cached_hub_id:
            hub_id = self._cached_hub_id
            logger.info("Using cached hub id %s (device already gone)", hub_id)

        return hub_id

    @staticmethod
    def _reset_usb_hub(hub_id: str) -> None:
        """Unbind and rebind a USB hub to force all downstream devices to
        re-enumerate.  Requires write access to ``/sys/bus/usb/drivers/usb/``
        (typically root or a udev rule granting access to the service user).
        """
        import subprocess

        unbind = f"/sys/bus/usb/drivers/usb/unbind"
        bind = f"/sys/bus/usb/drivers/usb/bind"

        try:
            # Try direct write first (works if running as root or with a
            # udev rule granting write permission).
            try:
                with open(unbind, "w") as f:
                    f.write(hub_id)
                # Dwell long enough for the downstream Full-Speed device to fully
                # drop off the bus before re-binding. Too short a gap leaves the
                # RP2040 half-enumerated and the re-bind fails with -32 (EPIPE),
                # forcing extra recovery cycles.
                time.sleep(1.5)
                with open(bind, "w") as f:
                    f.write(hub_id)
            except PermissionError:
                logger.info("Direct sysfs write failed, using sudo for hub reset")
                subprocess.run(
                    ["sudo", "sh", "-c", f"echo {hub_id} > {unbind}"],
                    check=True, timeout=5,
                )
                time.sleep(1.5)
                subprocess.run(
                    ["sudo", "sh", "-c", f"echo {hub_id} > {bind}"],
                    check=True, timeout=5,
                )
            # Give the hub and downstream devices time to re-enumerate.
            time.sleep(2.0)
            logger.info("USB hub %s reset completed", hub_id)
        except Exception as exc:
            logger.error("USB hub reset failed for %s: %s", hub_id, exc)

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
