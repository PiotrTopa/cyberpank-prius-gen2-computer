"""
Communication module.

Handles serial communication with the Gateway using NDJSON protocol.
"""

from .protocol import parse_message, create_message

__all__ = ["parse_message", "create_message"]
