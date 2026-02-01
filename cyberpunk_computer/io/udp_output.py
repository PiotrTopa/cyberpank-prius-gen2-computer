"""
UDP Output Port - Development output sink for satellites.

Broadcasts NDJSON messages via UDP to connected satellites and dev tools.
Used in development mode alongside file replay for input.
"""

import json
import socket
import logging
import time
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field

from .ports import OutputPort, OutgoingCommand

logger = logging.getLogger(__name__)


@dataclass
class UDPTarget:
    """A UDP broadcast target."""
    host: str
    port: int
    device_ids: Set[int] = field(default_factory=set)  # Empty = all devices


class UDPOutputPort(OutputPort):
    """
    UDP-based output port for development.
    
    Broadcasts NDJSON messages to configured UDP targets.
    Each satellite can listen on its own port.
    
    Usage:
        # Create port
        port = UDPOutputPort()
        
        # Add VFD satellite target (device 110 on port 5110)
        port.add_target("localhost", 5110, device_ids={110})
        
        # Add debug monitor (all devices on port 5000)
        port.add_target("localhost", 5000)
        
        # Send command
        cmd = OutgoingCommand(device_id=110, command_type="E", payload={...})
        port.send(cmd)
    """
    
    def __init__(self, broadcast: bool = False):
        """
        Initialize UDP output port.
        
        Args:
            broadcast: If True, use broadcast socket (requires network permission)
        """
        self._socket: Optional[socket.socket] = None
        self._targets: List[UDPTarget] = []
        self._broadcast = broadcast
        self._stats = {
            'messages_sent': 0,
            'bytes_sent': 0,
            'errors': 0,
            'last_send_time': 0.0,
        }
        
        self._create_socket()
    
    def _create_socket(self) -> None:
        """Create the UDP socket."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self._broadcast:
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    
    def add_target(
        self, 
        host: str, 
        port: int, 
        device_ids: Optional[Set[int]] = None
    ) -> None:
        """
        Add a UDP target for message routing.
        
        Args:
            host: Target hostname or IP
            port: Target UDP port
            device_ids: Set of device IDs to route to this target.
                       If None or empty, all messages are sent.
        """
        target = UDPTarget(
            host=host,
            port=port,
            device_ids=device_ids or set()
        )
        self._targets.append(target)
        logger.info(f"Added UDP target: {host}:{port} (devices: {device_ids or 'all'})")
    
    def remove_target(self, host: str, port: int) -> bool:
        """
        Remove a UDP target.
        
        Returns:
            True if target was found and removed
        """
        for i, target in enumerate(self._targets):
            if target.host == host and target.port == port:
                del self._targets[i]
                logger.info(f"Removed UDP target: {host}:{port}")
                return True
        return False
    
    def send(self, command: OutgoingCommand) -> bool:
        """
        Send command to matching UDP targets.
        
        The command is converted to NDJSON and sent to all targets
        that either have no device filter or include this device ID.
        
        Args:
            command: Command to send
            
        Returns:
            True if sent to at least one target
        """
        if not self._socket or not self._targets:
            return False
        
        # Convert to NDJSON
        message = {
            "id": command.device_id,
            "d": command.payload
        }
        data = (json.dumps(message, separators=(',', ':')) + "\n").encode('utf-8')
        
        sent_count = 0
        
        for target in self._targets:
            # Check if this target wants this device's messages
            if target.device_ids and command.device_id not in target.device_ids:
                continue
            
            try:
                self._socket.sendto(data, (target.host, target.port))
                sent_count += 1
                self._stats['bytes_sent'] += len(data)
                logger.debug(f"UDP sent {len(data)} bytes to {target.host}:{target.port}")
            except Exception as e:
                logger.warning(f"Failed to send to {target.host}:{target.port}: {e}")
                self._stats['errors'] += 1
        
        if sent_count > 0:
            self._stats['messages_sent'] += 1
            self._stats['last_send_time'] = time.time()
            return True
        
        return False
    
    def send_raw(self, device_id: int, data: dict) -> bool:
        """
        Send raw data dict to targets (convenience method).
        
        Args:
            device_id: Target device ID
            data: Data payload
            
        Returns:
            True if sent successfully
        """
        command = OutgoingCommand(
            device_id=device_id,
            command_type="raw",
            payload=data
        )
        return self.send(command)
    
    def is_connected(self) -> bool:
        """Check if port is ready to send."""
        return self._socket is not None and len(self._targets) > 0
    
    @property
    def name(self) -> str:
        """Port name for logging."""
        target_count = len(self._targets)
        return f"UDPOutputPort({target_count} targets)"
    
    @property
    def stats(self) -> dict:
        """Get send statistics."""
        return self._stats.copy()
    
    def close(self) -> None:
        """Close the socket."""
        if self._socket:
            self._socket.close()
            self._socket = None


class MultiOutputPort(OutputPort):
    """
    Composite output port that sends to multiple output ports.
    
    Useful for combining serial output (production) with UDP output (dev tools).
    
    Usage:
        serial_port = SerialOutputPort("/dev/ttyACM0")
        udp_port = UDPOutputPort()
        udp_port.add_target("localhost", 5110, {110})
        
        multi = MultiOutputPort([serial_port, udp_port])
        multi.send(command)  # Sends to both
    """
    
    def __init__(self, ports: Optional[List[OutputPort]] = None):
        """
        Initialize multi-output port.
        
        Args:
            ports: List of output ports to combine
        """
        self._ports: List[OutputPort] = ports or []
    
    def add_port(self, port: OutputPort) -> None:
        """Add an output port."""
        self._ports.append(port)
        logger.info(f"Added output port: {port.name}")
    
    def send(self, command: OutgoingCommand) -> bool:
        """
        Send command to all ports.
        
        Returns True if at least one port succeeded.
        """
        success = False
        for port in self._ports:
            try:
                if port.send(command):
                    success = True
            except Exception as e:
                logger.warning(f"Port {port.name} send failed: {e}")
        return success
    
    def is_connected(self) -> bool:
        """Check if any port is connected."""
        return any(port.is_connected() for port in self._ports)
    
    @property
    def name(self) -> str:
        """Port name for logging."""
        names = [p.name for p in self._ports]
        return f"MultiOutputPort({', '.join(names)})"
