"""
Communication Logger - Records all Gateway communication for replay.

Provides file-based logging of all incoming and outgoing messages
with timestamps. The log format is compatible with FileInputPort
for replay.

Usage:
    logger = CommLogger("logs/session_2026-02-04.ndjson")
    logger.start()
    
    # Hook into ingress/egress
    ingress.set_message_log_callback(logger.log_incoming)
    egress.set_command_log_callback(logger.log_outgoing)
    
    # ... run application ...
    
    logger.stop()
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from typing import Optional, Any

from .ports import RawMessage, OutgoingCommand

logger = logging.getLogger(__name__)


@dataclass
class LogConfig:
    """Configuration for communication logger."""
    directory: str = "logs"
    prefix: str = "comm"
    buffer_size: int = 100
    flush_interval: float = 1.0  # Seconds
    include_system: bool = True
    include_can: bool = True
    include_avc: bool = True
    include_satellite: bool = True
    include_outgoing: bool = True


class CommLogger:
    """
    Logs all communication to NDJSON file for later replay.
    
    Log format:
    {"ts": 12345, "dir": "IN", "id": 1, "d": {...}}
    {"ts": 12346, "dir": "OUT", "id": 2, "d": {...}}
    
    Where:
    - ts: Timestamp in milliseconds since logger start
    - dir: Direction - "IN" for received, "OUT" for sent
    - id: Device ID
    - d: Message data
    """
    
    def __init__(
        self, 
        filepath: Optional[str] = None,
        config: Optional[LogConfig] = None
    ):
        """
        Initialize communication logger.
        
        Args:
            filepath: Explicit file path, or None to auto-generate
            config: Logger configuration
        """
        self._config = config or LogConfig()
        
        if filepath:
            self._filepath = Path(filepath)
        else:
            self._filepath = self._generate_filepath()
        
        self._file = None
        self._start_time = 0.0
        self._running = False
        
        # Write queue for async logging
        self._queue: Queue = Queue()
        self._writer_thread: Optional[threading.Thread] = None
        
        # Statistics
        self._messages_logged = 0
        self._bytes_written = 0
    
    @property
    def filepath(self) -> Path:
        """Get log file path."""
        return self._filepath
    
    @property
    def is_running(self) -> bool:
        """Check if logger is running."""
        return self._running
    
    @property
    def messages_logged(self) -> int:
        """Total messages logged."""
        return self._messages_logged
    
    @property
    def bytes_written(self) -> int:
        """Total bytes written to the log file."""
        return self._bytes_written
    
    def _generate_filepath(self) -> Path:
        """Generate timestamped filepath."""
        directory = Path(self._config.directory)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self._config.prefix}_{timestamp}.ndjson"
        return directory / filename
    
    def start(self) -> bool:
        """
        Start the logger.
        
        Creates the log file and starts the writer thread.
        
        Returns:
            True if started successfully
        """
        if self._running:
            return True
        
        try:
            # Create directory if needed
            self._filepath.parent.mkdir(parents=True, exist_ok=True)
            
            # Open file
            self._file = open(self._filepath, 'w', encoding='utf-8')
            
            # Write header comment
            header = {
                "meta": "comm_log",
                "version": "1.0",
                "created": datetime.now().isoformat(),
                "config": {
                    "include_system": self._config.include_system,
                    "include_can": self._config.include_can,
                    "include_avc": self._config.include_avc,
                    "include_satellite": self._config.include_satellite,
                    "include_outgoing": self._config.include_outgoing,
                }
            }
            self._file.write(json.dumps(header) + "\n")
            self._file.flush()
            
            self._start_time = time.time()
            self._running = True
            
            # Start writer thread
            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                daemon=True,
                name="CommLogger-Writer"
            )
            self._writer_thread.start()
            
            logger.info(f"Communication logger started: {self._filepath}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start comm logger: {e}")
            return False
    
    def stop(self) -> None:
        """Stop the logger and flush remaining data."""
        if not self._running:
            return
        
        self._running = False
        
        # Wait for writer to finish
        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=2.0)
        
        # Flush remaining items
        self._flush_queue()
        
        # Write footer
        if self._file:
            footer = {
                "meta": "end",
                "messages": self._messages_logged,
                "bytes": self._bytes_written,
                "duration_s": time.time() - self._start_time
            }
            self._file.write(json.dumps(footer) + "\n")
            self._file.close()
            self._file = None
        
        logger.info(f"Communication logger stopped. {self._messages_logged} messages logged.")
    
    def log_incoming(self, msg: RawMessage, direction: str = "IN") -> None:
        """
        Log an incoming message.
        
        This is the callback for IngressController.
        
        Args:
            msg: Raw message from gateway
            direction: Direction marker (always "IN" for incoming)
        """
        if not self._running:
            return
        
        # Filter by device type
        if msg.device_id == 0 and not self._config.include_system:
            return
        if msg.device_id == 1 and not self._config.include_can:
            return
        if msg.device_id == 2 and not self._config.include_avc:
            return
        if msg.device_id >= 6 and not self._config.include_satellite:
            return
        
        entry = self._create_entry(
            direction="IN",
            device_id=msg.device_id,
            data=msg.data,
            timestamp_ms=msg.timestamp,
            sequence=msg.sequence
        )
        
        self._queue.put(entry)
    
    def log_outgoing(self, cmd: OutgoingCommand, direction: str = "OUT") -> None:
        """
        Log an outgoing command.
        
        This is the callback for EgressController.
        
        Args:
            cmd: Outgoing command
            direction: Direction marker (always "OUT" for outgoing)
        """
        if not self._running or not self._config.include_outgoing:
            return
        
        entry = self._create_entry(
            direction="OUT",
            device_id=cmd.device_id,
            data=cmd.data
        )
        
        self._queue.put(entry)
    
    def log_raw(
        self, 
        direction: str, 
        device_id: int, 
        data: Any,
        timestamp_ms: Optional[int] = None
    ) -> None:
        """
        Log a raw message directly.
        
        Args:
            direction: "IN" or "OUT"
            device_id: Device ID
            data: Message data
            timestamp_ms: Optional gateway timestamp
        """
        if not self._running:
            return
        
        entry = self._create_entry(
            direction=direction,
            device_id=device_id,
            data=data,
            timestamp_ms=timestamp_ms
        )
        
        self._queue.put(entry)
    
    def _create_entry(
        self,
        direction: str,
        device_id: int,
        data: Any,
        timestamp_ms: Optional[int] = None,
        sequence: Optional[int] = None
    ) -> dict:
        """Create a log entry."""
        # Use local timestamp if not provided
        if timestamp_ms is None:
            timestamp_ms = int((time.time() - self._start_time) * 1000)
        
        entry = {
            "ts": timestamp_ms,
            "dir": direction,
            "id": device_id,
            "d": data
        }
        
        if sequence is not None:
            entry["seq"] = sequence
        
        return entry
    
    def _writer_loop(self) -> None:
        """Background thread for writing to file."""
        buffer = []
        last_flush = time.time()
        
        while self._running or not self._queue.empty():
            try:
                entry = self._queue.get(timeout=0.1)
                buffer.append(entry)
                
                # Flush if buffer full or time elapsed
                if len(buffer) >= self._config.buffer_size or \
                   time.time() - last_flush >= self._config.flush_interval:
                    self._write_buffer(buffer)
                    buffer = []
                    last_flush = time.time()
                    
            except Empty:
                # Flush remaining on timeout
                if buffer:
                    self._write_buffer(buffer)
                    buffer = []
                    last_flush = time.time()
            except Exception as e:
                logger.error(f"Writer loop error: {e}")
    
    def _write_buffer(self, buffer: list) -> None:
        """Write buffered entries to file."""
        if not self._file or not buffer:
            return
        
        try:
            for entry in buffer:
                line = json.dumps(entry, separators=(',', ':')) + "\n"
                self._file.write(line)
                self._bytes_written += len(line)
                self._messages_logged += 1
            
            self._file.flush()
            
        except Exception as e:
            logger.error(f"Failed to write log buffer: {e}")
    
    def _flush_queue(self) -> None:
        """Flush all remaining items from queue."""
        buffer = []
        while True:
            try:
                entry = self._queue.get_nowait()
                buffer.append(entry)
            except Empty:
                break
        
        if buffer:
            self._write_buffer(buffer)


class CommLoggerManager:
    """
    Manages communication logging across the application.
    
    Singleton-style manager that can be enabled/disabled via config.
    """
    
    _instance: Optional[CommLogger] = None
    _enabled: bool = False
    
    @classmethod
    def enable(
        cls, 
        filepath: Optional[str] = None,
        config: Optional[LogConfig] = None
    ) -> CommLogger:
        """
        Enable communication logging.
        
        Args:
            filepath: Optional explicit filepath
            config: Logger configuration
            
        Returns:
            The CommLogger instance
        """
        if cls._instance and cls._instance.is_running:
            cls._instance.stop()
        
        cls._instance = CommLogger(filepath, config)
        cls._instance.start()
        cls._enabled = True
        
        return cls._instance
    
    @classmethod
    def disable(cls) -> None:
        """Disable communication logging."""
        if cls._instance:
            cls._instance.stop()
            cls._instance = None
        cls._enabled = False
    
    @classmethod
    def get(cls) -> Optional[CommLogger]:
        """Get the current logger instance."""
        return cls._instance
    
    @classmethod
    def is_enabled(cls) -> bool:
        """Check if logging is enabled."""
        return cls._enabled and cls._instance is not None
