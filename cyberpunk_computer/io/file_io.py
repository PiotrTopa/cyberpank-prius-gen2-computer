"""
File-based Input Port - Replays recorded Gateway data from file.

Provides the same InputPort interface but reads from a file
with timing based on recorded timestamps.

Usage:
    port = FileInputPort("assets/data/recording.ndjson")
    port.start()
    
    while running:
        msg = port.poll()
        if msg:
            process(msg)
"""

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import List, Optional, Callable

from .ports import InputPort, RawMessage

logger = logging.getLogger(__name__)


class PlaybackState(Enum):
    """Playback state."""
    STOPPED = auto()
    PLAYING = auto()
    PAUSED = auto()


@dataclass
class LogEntry:
    """A single entry from the log file."""
    timestamp_ms: int       # Original timestamp from file (ms)
    relative_time: float    # Time since start (seconds)
    raw_line: str           # Original line
    raw_dict: dict          # Parsed JSON
    message: RawMessage     # Converted message


class FileInputPort(InputPort):
    """
    File-based input port that replays recorded messages.
    
    Supports both realtime playback (with timing) and instant mode.
    """
    
    def __init__(
        self, 
        filepath: str, 
        speed: float = 1.0,
        loop: bool = False,
        realtime: bool = True
    ):
        """
        Initialize file input port.
        
        Args:
            filepath: Path to NDJSON log file
            speed: Playback speed multiplier (1.0 = realtime, 0 = instant)
            loop: Loop playback when reaching end
            realtime: If True, respect timestamps; if False, return all immediately
        """
        self.filepath = Path(filepath)
        self.speed = speed
        self.loop = loop
        self.realtime = realtime
        
        # Loaded entries
        self._entries: List[LogEntry] = []
        
        # Playback state
        self._state = PlaybackState.STOPPED
        self._position = 0
        self._start_time = 0.0
        self._pause_time = 0.0
        self._pause_offset = 0.0
        
        # Statistics
        self._messages_played = 0
        
        # Callbacks for playback events
        self._on_position_change: Optional[Callable[[int, int], None]] = None
    
    @property
    def state(self) -> PlaybackState:
        """Get current playback state."""
        return self._state
    
    @property
    def position(self) -> int:
        """Current playback position (entry index)."""
        return self._position
    
    @property
    def total_entries(self) -> int:
        """Total number of entries in file."""
        return len(self._entries)
    
    @property
    def progress(self) -> float:
        """Playback progress (0.0 to 1.0)."""
        if not self._entries:
            return 0.0
        return self._position / len(self._entries)
    
    @property
    def duration(self) -> float:
        """Total duration in seconds."""
        if not self._entries:
            return 0.0
        return self._entries[-1].relative_time
    
    @property
    def total_duration(self) -> float:
        """Total duration in seconds (alias for duration)."""
        return self.duration
    
    @property
    def current_time(self) -> float:
        """Current playback time in seconds."""
        if not self._entries or self._position >= len(self._entries):
            return self.duration
        return self._entries[self._position].relative_time
    
    @property
    def current_playback_time(self) -> float:
        """Current playback time in seconds (alias for current_time)."""
        return self.current_time
    
    def get_status(self) -> str:
        """Get human-readable playback status."""
        state_str = {
            PlaybackState.STOPPED: "STOPPED",
            PlaybackState.PLAYING: "PLAYING",
            PlaybackState.PAUSED: "PAUSED",
        }.get(self._state, "UNKNOWN")
        
        return f"[{state_str}] Position: {self._position}/{len(self._entries)}"
    
    def set_position_callback(self, callback: Callable[[int, int], None]) -> None:
        """Set callback for position changes: callback(position, total)."""
        self._on_position_change = callback
    
    def load(self) -> bool:
        """
        Load the log file.
        
        Returns:
            True if file loaded successfully
        """
        if not self.filepath.exists():
            logger.error(f"File not found: {self.filepath}")
            return False
        
        self._entries = []
        first_ts = None
        
        try:
            with open(self.filepath, 'r') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError as e:
                        logger.warning(f"Line {line_num}: Invalid JSON: {e}")
                        continue
                    
                    # Extract timestamp
                    ts = data.get("ts", 0)
                    if first_ts is None:
                        first_ts = ts
                    
                    relative_time = (ts - first_ts) / 1000.0  # ms to seconds
                    
                    # Create RawMessage
                    raw_msg = RawMessage.from_gateway_json(data)
                    
                    entry = LogEntry(
                        timestamp_ms=ts,
                        relative_time=relative_time,
                        raw_line=line,
                        raw_dict=data,
                        message=raw_msg
                    )
                    self._entries.append(entry)
            
            logger.info(f"Loaded {len(self._entries)} entries from {self.filepath}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to load file: {e}")
            return False
    
    def start(self) -> bool:
        """Start playback."""
        if not self._entries:
            if not self.load():
                return False
        
        self._state = PlaybackState.PLAYING
        self._position = 0
        self._start_time = time.time()
        self._pause_offset = 0.0
        self._messages_played = 0
        
        logger.info(f"Started playback: {len(self._entries)} entries, {self.duration:.1f}s duration")
        return True
    
    def stop(self) -> None:
        """Stop playback."""
        self._state = PlaybackState.STOPPED
        self._position = 0
    
    def pause(self) -> None:
        """Pause playback."""
        if self._state == PlaybackState.PLAYING:
            self._state = PlaybackState.PAUSED
            self._pause_time = time.time()
    
    def resume(self) -> None:
        """Resume playback from current position."""
        if self._state == PlaybackState.PLAYING:
            return  # Already playing
        
        if not self._entries:
            return
        
        if self._state == PlaybackState.PAUSED:
            # Account for pause duration
            self._pause_offset += time.time() - self._pause_time
        else:
            # STOPPED state - start from current position (not from 0)
            if self._position < len(self._entries):
                entry = self._entries[self._position]
                self._start_time = time.time() - (entry.relative_time / self.speed)
            else:
                # At end, start from beginning
                self._position = 0
                self._start_time = time.time()
            self._pause_offset = 0.0
        
        self._state = PlaybackState.PLAYING
    
    def toggle(self) -> None:
        """Toggle play/pause."""
        if self._state == PlaybackState.PLAYING:
            self.pause()
        else:
            self.resume()
    
    def seek(self, position: int) -> None:
        """
        Seek to specific position.
        
        Args:
            position: Entry index to seek to
        """
        if not self._entries:
            return
        
        position = max(0, min(position, len(self._entries) - 1))
        self._position = position
        
        # Always recalculate start time based on new position
        # This ensures timing is correct when we resume
        entry = self._entries[position]
        self._start_time = time.time() - (entry.relative_time / self.speed)
        self._pause_offset = 0.0
        
        # If we were paused, reset pause time to now so resume works correctly
        if self._state == PlaybackState.PAUSED:
            self._pause_time = time.time()
        
        if self._on_position_change:
            self._on_position_change(self._position, len(self._entries))
    
    def seek_time(self, seconds: float) -> None:
        """Seek to specific time in seconds."""
        for i, entry in enumerate(self._entries):
            if entry.relative_time >= seconds:
                self.seek(i)
                return
        # Seek to end
        self.seek(len(self._entries) - 1)
    
    def poll(self) -> Optional[RawMessage]:
        """
        Poll for next message.
        
        Returns messages that are due based on playback time.
        """
        if self._state != PlaybackState.PLAYING:
            return None
        
        if self._position >= len(self._entries):
            if self.loop:
                self._position = 0
                self._start_time = time.time()
                self._pause_offset = 0.0
            else:
                self._state = PlaybackState.STOPPED
                return None
        
        entry = self._entries[self._position]
        
        if self.realtime and self.speed > 0:
            # Calculate elapsed playback time
            elapsed = (time.time() - self._start_time - self._pause_offset) * self.speed
            
            # Check if this entry is due
            if entry.relative_time > elapsed:
                return None
        
        # Return this entry's message
        self._position += 1
        self._messages_played += 1
        
        if self._on_position_change:
            self._on_position_change(self._position, len(self._entries))
        
        return entry.message
    
    def is_connected(self) -> bool:
        """File input is 'connected' when file is loaded."""
        return len(self._entries) > 0
    
    @property
    def name(self) -> str:
        return f"FileInputPort({self.filepath.name})"
    
    # ─────────────────────────────────────────────────────────────────────────
    # Additional helpers for development
    # ─────────────────────────────────────────────────────────────────────────
    
    def step_forward(self, count: int = 1) -> Optional[RawMessage]:
        """
        Step forward N entries (ignoring timing).
        
        Useful for debugging/development.
        """
        if self._position >= len(self._entries):
            return None
        
        # Return the current entry
        entry = self._entries[self._position]
        self._position = min(self._position + count, len(self._entries))
        
        if self._on_position_change:
            self._on_position_change(self._position, len(self._entries))
        
        return entry.message
    
    def step_backward(self, count: int = 1) -> Optional[RawMessage]:
        """Step backward N entries."""
        self._position = max(0, self._position - count)
        
        if self._on_position_change:
            self._on_position_change(self._position, len(self._entries))
        
        if self._position < len(self._entries):
            return self._entries[self._position].message
        return None
    
    def get_entries_in_range(self, start: int, end: int) -> List[LogEntry]:
        """Get entries in index range (for preview/analysis)."""
        return self._entries[start:end]
