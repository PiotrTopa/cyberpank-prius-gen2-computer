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
DEVICE_SATELLITE_BASE = 100  # RS485 satellites start at 100 (matches io.ports)


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


# =============================================================================
# CAN Solicited Mode Functions (Protocol v2.8.0)
# =============================================================================

def create_can_request(
    can_id: int | str,
    data: list[int],
    response_ids: list[str] | None = None,
    timeout_ms: int = 100,
    extended: bool = False
) -> str:
    """
    Create a single CAN request-response query message.
    
    Args:
        can_id: Request CAN ID (e.g., 0x7DF for OBD-II broadcast)
        data: Request data bytes (max 8)
        response_ids: Expected response CAN IDs (default: 0x7E8-0x7EF)
        timeout_ms: Response timeout in milliseconds
        extended: True for 29-bit extended frames
    
    Returns:
        NDJSON line ready to send
    
    Example:
        >>> create_can_request(0x7DF, [2, 1, 12])  # Read RPM
    """
    payload = {
        "a": "req",
        "i": can_id if isinstance(can_id, str) else f"0x{can_id:03X}",
        "d": data,
        "t": timeout_ms
    }
    if response_ids:
        payload["r"] = response_ids
    if extended:
        payload["e"] = True
    
    return create_message(DEVICE_CAN, payload)


def create_can_subscription(
    slot: int,
    can_id: int | str,
    data: list[int],
    interval_ms: int = 1000,
    response_ids: list[str] | None = None,
    timeout_ms: int = 100,
    extended: bool = False
) -> str:
    """
    Create a CAN subscription for periodic polling.
    
    Args:
        slot: Subscription slot (0-15)
        can_id: Request CAN ID
        data: Request data bytes
        interval_ms: Polling interval in milliseconds
        response_ids: Expected response CAN IDs
        timeout_ms: Response timeout in milliseconds
        extended: True for 29-bit extended frames
    
    Returns:
        NDJSON line ready to send
    
    Example:
        >>> create_can_subscription(0, 0x7DF, [2, 1, 12], interval_ms=200)
    """
    payload = {
        "a": "sub",
        "slot": slot,
        "i": can_id if isinstance(can_id, str) else f"0x{can_id:03X}",
        "d": data,
        "int": interval_ms,
        "t": timeout_ms
    }
    if response_ids:
        payload["r"] = response_ids
    if extended:
        payload["e"] = True
    
    return create_message(DEVICE_CAN, payload)


def create_can_unsubscribe(slot: int | str = "all") -> str:
    """
    Create a CAN unsubscribe message.
    
    Args:
        slot: Slot to unsubscribe (0-15) or "all" for all slots
    
    Returns:
        NDJSON line ready to send
    """
    payload = {"a": "unsub", "slot": slot}
    return create_message(DEVICE_CAN, payload)


def create_can_list_subs() -> str:
    """
    Create a message to list active CAN subscriptions.
    
    Returns:
        NDJSON line ready to send
    """
    return create_message(DEVICE_CAN, {"a": "subs"})


def create_can_mode_switch(mode: str) -> str:
    """
    Create a CAN mode switch message.
    
    Args:
        mode: "normal" for active mode, "listen" for passive mode
    
    Returns:
        NDJSON line ready to send
    
    Warning:
        Switching to "listen" mode clears all active subscriptions.
    """
    if mode not in ("normal", "listen"):
        raise ValueError("Mode must be 'normal' or 'listen'")
    
    return create_message(DEVICE_CAN, {"a": "mode", "m": mode})


def create_obd2_request(mode: int, pid: int, ecu: int = 0x7DF) -> str:
    """
    Create a standard OBD-II request.
    
    Args:
        mode: OBD-II mode (e.g., 0x01 for current data)
        pid: PID within the mode
        ecu: Target ECU (0x7DF for broadcast, or specific like 0x7E0)
    
    Returns:
        NDJSON line ready to send
    
    Example:
        >>> create_obd2_request(0x01, 0x0C)  # Engine RPM
        >>> create_obd2_request(0x21, 0xC3, 0x7E2)  # Toyota hybrid data
    """
    # OBD-II data format: [length, mode, pid, 0, 0, 0, 0, 0]
    data = [0x02, mode, pid, 0x00, 0x00, 0x00, 0x00, 0x00]
    response_ids = [f"0x{ecu + 8:03X}"] if ecu != 0x7DF else None
    
    return create_can_request(ecu, data, response_ids)


def create_obd2_subscription(
    slot: int,
    mode: int,
    pid: int,
    interval_ms: int = 1000,
    ecu: int = 0x7DF
) -> str:
    """
    Create a periodic OBD-II subscription.
    
    Args:
        slot: Subscription slot (0-15)
        mode: OBD-II mode
        pid: PID
        interval_ms: Polling interval
        ecu: Target ECU
    
    Returns:
        NDJSON line ready to send
    """
    data = [0x02, mode, pid, 0x00, 0x00, 0x00, 0x00, 0x00]
    response_ids = [f"0x{ecu + 8:03X}"] if ecu != 0x7DF else None
    
    return create_can_subscription(slot, ecu, data, interval_ms, response_ids)

