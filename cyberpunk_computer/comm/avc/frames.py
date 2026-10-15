"""
AVC-LAN frame model for gateway firmware 3.x.

Gateway line (ID 2), see cyberpunk-prius-gateway/PROTOCOL.md and
gateway-rp2350/README.md "Protocol: what changed":

    {"id":2,"ts":..,"d":{"m":"110","s":"178","c":15,"d":["00","21",...],
                         "b":1, "nak":1, "echo":1}}

``m``/``s`` are 12-bit hex addresses, ``d`` the data bytes as hex strings.
``b`` = broadcast, ``nak`` = an individual frame with an unacknowledged slot,
``echo`` = our own transmission seen back on the bus. Lines on ID 2 without
``m`` (e.g. the auto-reply slot acks ``{"auto":0,"on":true}``) are not frames.

Physical units (AVC_MAP.md): 110 = display (EMV), 1C6 = climate / info ECU
(logicals E0 climate, E4/E5 info), 178 = navigation ECU (logical 58).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

# Physical addresses (AVC_MAP.md, NAV_EMULATION_SPEC.md)
DISPLAY = 0x110
CLIMATE = 0x1C6
NAV = 0x178
BCAST_FFF = 0xFFF
BCAST_1FF = 0x1FF

# Control nibble used by every frame the display / nav / climate ECU send.
CTRL_DEFAULT = 15


def _hex_byte(v: Any) -> int:
    if isinstance(v, int):
        return v & 0xFF
    return int(str(v), 16) & 0xFF


def _hex_addr(v: Any) -> int:
    if isinstance(v, int):
        return v & 0xFFF
    return int(str(v), 16) & 0xFFF


@dataclass(frozen=True)
class AvcFrame:
    """One AVC-LAN frame as reported by the gateway."""

    master: int
    slave: int
    data: Tuple[int, ...]
    ctrl: int = CTRL_DEFAULT
    broadcast: bool = False
    nak: bool = False
    echo: bool = False

    @classmethod
    def from_gateway(cls, d: Dict[str, Any]) -> Optional["AvcFrame"]:
        """Build a frame from the ``d`` member of a gateway ID 2 line.

        Returns None for ID 2 lines that are not frames.
        """
        if not isinstance(d, dict) or "m" not in d or "s" not in d:
            return None
        try:
            return cls(
                master=_hex_addr(d["m"]),
                slave=_hex_addr(d["s"]),
                data=tuple(_hex_byte(b) for b in d.get("d", []) or []),
                ctrl=int(d.get("c", CTRL_DEFAULT)),
                broadcast=bool(d.get("b")),
                nak=bool(d.get("nak")),
                echo=bool(d.get("echo")),
            )
        except (TypeError, ValueError):
            return None

    @classmethod
    def make(cls, master: int, slave: int, data: str | Sequence[int], **kw) -> "AvcFrame":
        """Convenience constructor: ``AvcFrame.make(0x110, 0x178, "00 21 24 78 ...")``."""
        if isinstance(data, str):
            data = [int(x, 16) for x in data.split()]
        return cls(master=master, slave=slave, data=tuple(data), **kw)

    def startswith(self, prefix: Sequence[int]) -> bool:
        return self.data[: len(prefix)] == tuple(prefix)

    def hex(self) -> str:
        return " ".join(f"{b:02X}" for b in self.data)

    def __str__(self) -> str:  # pragma: no cover - debug helper
        flags = "".join(f" {k}" for k in ("broadcast", "nak", "echo") if getattr(self, k))
        return f"{self.master:03X}->{self.slave:03X} {self.hex()}{flags}"


def parse_hex(data: str | Iterable[int]) -> Tuple[int, ...]:
    if isinstance(data, str):
        return tuple(int(x, 16) for x in data.split())
    return tuple(int(x) & 0xFF for x in data)


def tx_payload(master: int, slave: int, data: str | Iterable[int],
               broadcast: bool = False, ctrl: int = CTRL_DEFAULT) -> Dict[str, Any]:
    """Gateway ``d`` payload for an AVC-LAN transmission (ID 2).

    Matches what ``gateway-rp2350/tools/nav_emu.py`` sends:
    ``{"m":"178","s":"110","c":15,"d":["00","58",...]}`` plus ``"b":true``
    for broadcasts.
    """
    d: Dict[str, Any] = {
        "m": f"{master:03X}",
        "s": f"{slave:03X}",
        "c": ctrl,
        "d": [f"{b:02X}" for b in parse_hex(data)],
    }
    if broadcast:
        d["b"] = True
    return d
