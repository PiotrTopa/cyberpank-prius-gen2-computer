"""
Mock IO - Testing implementations of Input and Output ports.

These implementations allow for programmatic control of inputs
and inspection of outputs during unit testing.

Usage:
    input_port = MockInputPort()
    output_port = MockOutputPort()
    
    # Inject test messages
    input_port.inject(RawMessage(...))
    
    # Run code under test...
    
    # Verify outputs
    assert output_port.sent_commands[-1] == expected_command
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional, Callable
import time

from .ports import InputPort, OutputPort, RawMessage, OutgoingCommand

logger = logging.getLogger(__name__)


class MockInputPort(InputPort):
    """
    Mock input port for testing.
    
    Allows programmatic injection of messages and verification
    of message processing.
    """
    
    def __init__(self):
        self._queue: deque[RawMessage] = deque()
        self._started = False
        self._injected_count = 0
        self._polled_count = 0
    
    def start(self) -> bool:
        """Start the mock port."""
        self._started = True
        logger.debug("MockInputPort started")
        return True
    
    def stop(self) -> None:
        """Stop the mock port."""
        self._started = False
        self._queue.clear()
    
    def poll(self) -> Optional[RawMessage]:
        """Poll for next injected message."""
        if not self._started or not self._queue:
            return None
        
        self._polled_count += 1
        return self._queue.popleft()
    
    def is_connected(self) -> bool:
        """Mock is always 'connected' when started."""
        return self._started
    
    @property
    def name(self) -> str:
        return "MockInputPort"
    
    # ─────────────────────────────────────────────────────────────────────────
    # Testing helpers
    # ─────────────────────────────────────────────────────────────────────────
    
    def inject(self, message: RawMessage) -> None:
        """
        Inject a message to be returned by next poll().
        
        Args:
            message: Message to inject
        """
        self._queue.append(message)
        self._injected_count += 1
    
    def inject_many(self, messages: List[RawMessage]) -> None:
        """Inject multiple messages."""
        for msg in messages:
            self.inject(msg)
    
    def inject_gateway_json(self, raw: dict) -> None:
        """Inject a message from gateway JSON format."""
        msg = RawMessage.from_gateway_json(raw)
        self.inject(msg)
    
    @property
    def pending_count(self) -> int:
        """Number of messages waiting to be polled."""
        return len(self._queue)
    
    @property
    def stats(self) -> dict:
        """Get statistics."""
        return {
            "injected": self._injected_count,
            "polled": self._polled_count,
            "pending": len(self._queue)
        }
    
    def reset(self) -> None:
        """Reset the mock to initial state."""
        self._queue.clear()
        self._injected_count = 0
        self._polled_count = 0


class MockOutputPort(OutputPort):
    """
    Mock output port for testing.
    
    Records all sent commands for verification.
    """
    
    def __init__(self):
        self._connected = True
        self._sent_commands: List[OutgoingCommand] = []
        self._on_send: Optional[Callable[[OutgoingCommand], None]] = None
    
    def send(self, command: OutgoingCommand) -> bool:
        """
        Record a sent command.
        
        Args:
            command: Command to record
            
        Returns:
            True (always succeeds in mock)
        """
        if not self._connected:
            return False
        
        self._sent_commands.append(command)
        logger.debug(f"MockOutputPort.send: {command}")
        
        if self._on_send:
            self._on_send(command)
        
        return True
    
    def is_connected(self) -> bool:
        """Check mock connection state."""
        return self._connected
    
    @property
    def name(self) -> str:
        return "MockOutputPort"
    
    # ─────────────────────────────────────────────────────────────────────────
    # Testing helpers
    # ─────────────────────────────────────────────────────────────────────────
    
    @property
    def sent_commands(self) -> List[OutgoingCommand]:
        """Get all sent commands."""
        return self._sent_commands
    
    @property
    def last_sent(self) -> Optional[OutgoingCommand]:
        """Get the last sent command."""
        return self._sent_commands[-1] if self._sent_commands else None
    
    @property
    def send_count(self) -> int:
        """Number of commands sent."""
        return len(self._sent_commands)
    
    def set_connected(self, connected: bool) -> None:
        """Set mock connection state."""
        self._connected = connected
    
    def set_on_send(self, callback: Callable[[OutgoingCommand], None]) -> None:
        """Set callback to be notified of sends."""
        self._on_send = callback
    
    def clear(self) -> None:
        """Clear sent commands history."""
        self._sent_commands.clear()
    
    def reset(self) -> None:
        """Reset to initial state."""
        self._sent_commands.clear()
        self._connected = True
        self._on_send = None
    
    def find_commands(
        self, 
        device_id: Optional[int] = None,
        command_type: Optional[str] = None
    ) -> List[OutgoingCommand]:
        """
        Find commands matching criteria.
        
        Args:
            device_id: Filter by device ID
            command_type: Filter by command type
            
        Returns:
            List of matching commands
        """
        result = self._sent_commands
        
        if device_id is not None:
            result = [c for c in result if c.device_id == device_id]
        
        if command_type is not None:
            result = [c for c in result if c.command_type == command_type]
        
        return result


class LogOutputPort(OutputPort):
    """
    Logging output port for development.
    
    Logs all commands to console/file instead of sending to hardware.
    Useful for development mode where we want to see what would be sent.
    """
    
    def __init__(
        self, 
        prefix: str = "[WOULD SEND]",
        log_level: int = logging.INFO
    ):
        """
        Initialize log output port.
        
        Args:
            prefix: Prefix for log messages
            log_level: Logging level to use
        """
        self._prefix = prefix
        self._log_level = log_level
        self._send_count = 0
    
    def send(self, command: OutgoingCommand) -> bool:
        """
        Log a command instead of sending.
        
        Args:
            command: Command to log
            
        Returns:
            True (always succeeds)
        """
        self._send_count += 1
        
        msg = (
            f"{self._prefix} device={command.device_id} "
            f"cmd={command.command_type} payload={command.payload}"
        )
        logger.log(self._log_level, msg)
        
        # Also print to stdout with clear formatting
        print(f"\n{self._prefix} ─────────────────────────────────────────")
        print(f"  Device:  {command.device_id}")
        print(f"  Command: {command.command_type}")
        print(f"  Payload: {command.payload}")
        print(f"────────────────────────────────────────────────────────\n")
        
        return True
    
    def is_connected(self) -> bool:
        """Log port is always 'connected'."""
        return True
    
    @property
    def name(self) -> str:
        return "LogOutputPort"
    
    @property
    def send_count(self) -> int:
        """Number of commands logged."""
        return self._send_count
