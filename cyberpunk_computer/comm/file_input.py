"""
File Input - Replays recorded Gateway data from file.

Provides the same interface as GatewayConnection but reads from
a file with timing based on recorded timestamps.

Usage:
    # Same interface as GatewayConnection
    input_source = FileInput("log.ndjson")
    input_source.start()
    
    while running:
        input_source.update()  # Call each frame
        msg = input_source.receive()
        if msg:
            process(msg)
"""

import time
import json
import logging
from typing import Optional, Callable, List
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from .protocol import Message, parse_message

logger = logging.getLogger(__name__)


class PlaybackState(Enum):
    """Playback state."""
    STOPPED = auto()
    PLAYING = auto()
    PAUSED = auto()


@dataclass
class LogEntry:
    """A single entry from the log file."""
    timestamp_ms: int  # Original timestamp from file (ms)
    relative_time: float  # Time since start (seconds)
    raw_line: str
    message: Optional[Message]


class FileInput:
    """
    File-based input that replays Gateway messages with timing.
    
    Implements same interface as GatewayConnection for easy swap.
    """
    
    def __init__(self, filepath: str, speed: float = 1.0):
        """
        Initialize file input.
        
        Args:
            filepath: Path to NDJSON log file
            speed: Playback speed multiplier (1.0 = realtime)
        """
        self.filepath = Path(filepath)
        self.speed = speed
        
        # Loaded entries
        self._entries: List[LogEntry] = []
        
        # Playback state
        self._state = PlaybackState.STOPPED
        self._position = 0
        self._start_time = 0.0
        self._pause_time = 0.0
        
        # Pending messages (ready to be received)
        self._pending: List[Message] = []
        
        # Message handlers (same interface as GatewayConnection)
        self._handlers: dict[int, list[Callable[[Message], None]]] = {}
        
        # Stats
        self._messages_played = 0
        
        # Verbose logging
        self._verbose = False
        self._log_callback: Optional[Callable[[Message, str], None]] = None
    
    @property
    def connected(self) -> bool:
        """Always 'connected' when file is loaded."""
        return len(self._entries) > 0
    
    @property
    def state(self) -> PlaybackState:
        """Get current playback state."""
        return self._state
    
    @property
    def position(self) -> int:
        """Current playback position."""
        return self._position
    
    @property
    def total_entries(self) -> int:
        """Total number of entries."""
        return len(self._entries)
    
    @property
    def progress(self) -> float:
        """Playback progress (0.0 - 1.0)."""
        if not self._entries:
            return 0.0
        return self._position / len(self._entries)
    
    @property
    def total_duration(self) -> float:
        """Total recording duration in seconds."""
        if not self._entries:
            return 0.0
        return self._entries[-1].relative_time
    
    @property
    def current_playback_time(self) -> float:
        """Current playback time in seconds."""
        if self._state != PlaybackState.PLAYING:
            # When paused/stopped, return time of current position
            if self._position < len(self._entries):
                return self._entries[self._position].relative_time
            elif self._entries:
                return self._entries[-1].relative_time
            return 0.0
        
        # When playing, calculate from elapsed time
        return (time.time() - self._start_time) * self.speed
    
    def load(self) -> int:
        """
        Load the log file.
        
        Returns:
            Number of entries loaded
        """
        self._entries = []
        base_timestamp: Optional[int] = None
        
        try:
            with open(self.filepath, 'r') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    
                    try:
                        # Parse the message
                        message = parse_message(line)
                        
                        # Get timestamp from raw JSON
                        obj = json.loads(line)
                        ts = obj.get('ts')
                        
                        # Set base timestamp from first valid timestamp
                        if ts is not None and base_timestamp is None:
                            base_timestamp = ts
                        
                        # Calculate relative time
                        if ts is not None and base_timestamp is not None:
                            rel_time = (ts - base_timestamp) / 1000.0
                        else:
                            rel_time = 0.0
                        
                        entry = LogEntry(
                            timestamp_ms=ts or 0,
                            relative_time=rel_time,
                            raw_line=line,
                            message=message
                        )
                        self._entries.append(entry)
                        
                    except json.JSONDecodeError as e:
                        logger.debug(f"Line {line_num}: Invalid JSON - {e}")
                    except Exception as e:
                        logger.debug(f"Line {line_num}: Error - {e}")
                        
            logger.info(f"Loaded {len(self._entries)} entries from {self.filepath}")
            return len(self._entries)
            
        except FileNotFoundError:
            logger.error(f"File not found: {self.filepath}")
            return 0
        except Exception as e:
            logger.error(f"Failed to load file: {e}")
            return 0
    
    def start(self, speed: float = None) -> None:
        """Start or resume playback."""
        if speed is not None:
            self.speed = speed
        
        if self._state == PlaybackState.PAUSED:
            # Resume from pause
            pause_duration = time.time() - self._pause_time
            self._start_time += pause_duration
        else:
            # Start from beginning
            self._position = 0
            self._start_time = time.time()
            self._messages_played = 0
        
        self._state = PlaybackState.PLAYING
        logger.debug("Playback started")
    
    def pause(self) -> None:
        """Pause playback."""
        if self._state == PlaybackState.PLAYING:
            self._state = PlaybackState.PAUSED
            self._pause_time = time.time()
            logger.debug("Playback paused")
    
    def stop(self) -> None:
        """Stop and reset playback."""
        self._state = PlaybackState.STOPPED
        self._position = 0
        logger.debug("Playback stopped")
    
    def toggle(self) -> None:
        """Toggle play/pause."""
        if self._state == PlaybackState.PLAYING:
            self.pause()
        else:
            self.start()
    
    def seek(self, position: int) -> None:
        """Seek to specific position."""
        self._position = max(0, min(len(self._entries) - 1, position))
        if self._position < len(self._entries):
            self._start_time = time.time() - (
                self._entries[self._position].relative_time / self.speed
            )
    
    def set_verbose(self, verbose: bool, 
                    callback: Optional[Callable[[Message, str], None]] = None) -> None:
        """
        Enable/disable verbose logging.
        
        Args:
            verbose: Enable verbose output
            callback: Optional callback for each message (msg, direction)
        """
        self._verbose = verbose
        self._log_callback = callback
    
    def update(self) -> None:
        """
        Update playback - call this every frame.
        
        Moves messages that are due to the pending queue.
        """
        if self._state != PlaybackState.PLAYING:
            return
        
        if self._position >= len(self._entries):
            self._state = PlaybackState.STOPPED
            logger.info("Playback complete")
            return
        
        # Calculate current playback time
        current_time = (time.time() - self._start_time) * self.speed
        
        # Process all entries up to current time
        while (self._position < len(self._entries) and
               self._entries[self._position].relative_time <= current_time):
            
            entry = self._entries[self._position]
            
            if entry.message:
                self._pending.append(entry.message)
                self._messages_played += 1
                
                # Verbose logging
                if self._verbose and self._log_callback:
                    self._log_callback(entry.message, "IN")
            
            self._position += 1
    
    def receive(self, timeout: float = 0.0) -> Optional[Message]:
        """
        Receive a message from the queue.
        
        Same interface as GatewayConnection.receive()
        
        Args:
            timeout: Ignored (for compatibility)
        
        Returns:
            Message if available, None otherwise
        """
        if self._pending:
            return self._pending.pop(0)
        return None
    
    def register_handler(
        self,
        device_id: int,
        handler: Callable[[Message], None]
    ) -> None:
        """
        Register a handler for messages from a specific device.
        
        Same interface as GatewayConnection.
        """
        if device_id not in self._handlers:
            self._handlers[device_id] = []
        self._handlers[device_id].append(handler)
    
    def process_messages(self) -> int:
        """
        Process all pending messages through handlers.
        
        Same interface as GatewayConnection.
        
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
                    logger.error(f"Handler error: {e}")
            
            count += 1
        
        return count
    
    def send(self, device_id: int, data) -> None:
        """
        Send a message (logs only in file mode).
        
        Args:
            device_id: Target device ID
            data: Payload data
        """
        if self._verbose:
            logger.info(f"[FILE] Would send to {device_id}: {data}")
    
    def get_status(self) -> str:
        """Get playback status string."""
        state_icons = {
            PlaybackState.STOPPED: "⏹",
            PlaybackState.PLAYING: "▶",
            PlaybackState.PAUSED: "⏸"
        }
        
        icon = state_icons.get(self._state, "?")
        pos = self._position
        total = len(self._entries)
        pct = int(self.progress * 100)
        
        return f"{icon} {pos}/{total} ({pct}%)"


def create_input_source(filepath: Optional[str] = None, 
                        serial_port: Optional[str] = None,
                        speed: float = 1.0):
    """
    Factory function to create appropriate input source.
    
    Args:
        filepath: Path to log file (for file input)
        serial_port: Serial port (for real Gateway)
        speed: Playback speed for file input
    
    Returns:
        FileInput or GatewayConnection instance
    """
    if filepath:
        source = FileInput(filepath, speed)
        source.load()
        return source
    else:
        from .gateway import GatewayConnection, GatewayConfig
        config = GatewayConfig(port=serial_port or "/dev/ttyACM0")
        return GatewayConnection(config)
