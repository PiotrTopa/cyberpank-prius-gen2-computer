"""
NDJSON Protocol handling.

Provides serialization and deserialization for the Gateway protocol.
"""

import json
from typing import Any, Optional
from dataclasses import dataclass


# Device IDs
DEVICE_SYSTEM = 0
DEVICE_CAN = 1
DEVICE_AVCLAN = 2
DEVICE_SATELLITE_BASE = 6  # Satellites are 6-255


@dataclass
class Message:
    """
    A parsed NDJSON message from the Gateway.
    
    Attributes:
        device_id: Device ID / channel
        data: Payload data
        timestamp: Gateway timestamp (ms), optional
        sequence: Sequence counter, optional
    """
    device_id: int
    data: Any
    timestamp: Optional[int] = None
    sequence: Optional[int] = None


def parse_message(line: str) -> Optional[Message]:
    """
    Parse a single NDJSON line from the Gateway.
    
    Args:
        line: Raw line from serial port
    
    Returns:
        Parsed Message or None if invalid
    
    Example:
        >>> parse_message('{"id":1,"ts":2200,"d":{"i":"0x2C4","d":[0,0,12,55]}}')
        Message(device_id=1, data={'i': '0x2C4', 'd': [0, 0, 12, 55]}, timestamp=2200)
    """
    try:
        obj = json.loads(line.strip())
        
        if "id" not in obj or "d" not in obj:
            return None
        
        return Message(
            device_id=obj["id"],
            data=obj["d"],
            timestamp=obj.get("ts"),
            sequence=obj.get("seq")
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def create_message(device_id: int, data: Any) -> str:
    """
    Create an NDJSON message to send to the Gateway.
    
    Args:
        device_id: Target device ID
        data: Payload data
    
    Returns:
        NDJSON line (with newline terminator)
    
    Example:
        >>> create_message(1, {"i": "0x5A0", "d": [128, 1]})
        '{"id":1,"d":{"i":"0x5A0","d":[128,1]}}\\n'
    """
    message = {
        "id": device_id,
        "d": data
    }
    return json.dumps(message, separators=(',', ':')) + "\n"


def create_can_message(can_id: int | str, data: list[int], extended: bool = False) -> str:
    """
    Create a CAN bus message.
    
    Args:
        can_id: CAN ID (int or hex string like "0x5A0")
        data: Data bytes (0-8)
        extended: True for 29-bit extended frames
    
    Returns:
        NDJSON line ready to send
    """
    payload = {
        "i": can_id if isinstance(can_id, str) else f"0x{can_id:03X}",
        "d": data
    }
    if extended:
        payload["e"] = True
    
    return create_message(DEVICE_CAN, payload)


def create_avclan_message(
    master: str,
    slave: str,
    control: int,
    data: list[str]
) -> str:
    """
    Create an AVC-LAN message.
    
    Args:
        master: Master address (12-bit hex string)
        slave: Slave address (12-bit hex string)
        control: Control flag (4-bit)
        data: Data bytes as hex strings
    
    Returns:
        NDJSON line ready to send
    """
    payload = {
        "m": master,
        "s": slave,
        "c": control,
        "d": data
    }
    return create_message(DEVICE_AVCLAN, payload)


def create_satellite_message(satellite_id: int, data: Any) -> str:
    """
    Create a message for an RS485 satellite.
    
    Args:
        satellite_id: Satellite address (6-255)
        data: Payload data (format depends on satellite)
    
    Returns:
        NDJSON line ready to send
    """
    if satellite_id < DEVICE_SATELLITE_BASE:
        raise ValueError(f"Satellite ID must be >= {DEVICE_SATELLITE_BASE}")
    
    return create_message(satellite_id, data)
