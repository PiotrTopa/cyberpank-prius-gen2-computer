"""
ECU registry of the NHW20 (Prius Gen 2) for the diagnostics engine.

What the research established (docs/TOYOTA_DIAGNOSTICS.md, sections 1-2):

* Only three ECUs do DTC communication over the DLC3 CAN on this car: the ECM,
  the HV control ECU and the battery ECU (Toyota repair manual, CAN
  communication section: "The ECM, hybrid vehicle control ECU, and battery ECU
  use the CAN communication system to perform DTC communication instead of the
  conventional communication line (SIL)").
* The skid control ECU, power steering ECU, airbag sensor, transmission control
  ECU and — through the gateway ECU — every BEAN body ECU are diagnosed over
  SIL (DLC3 pin 7, K-line). The car computer has no K-line interface, so they
  are listed with ``bus="kline"`` and ``reachable=False``.
* The CAN ids later Toyotas use for those ECUs (0x7B0, 0x7A1, 0x780, 0x7C0,
  0x7C4, 0x727, 0x750 + sub-address) are kept as **probes**: a single
  tester-present frame each, sent only when a scan asks for ``probe`` (or names
  the ECU). Nobody has reported an answer on an NHW20; a probe costs one frame
  and settles it for this car.

``confidence`` uses the labels of the document: primary / multi / single /
inferred / unverified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import services


@dataclass(frozen=True)
class ReadSpec:
    """One read request of a scan: what to send and how to read the answer."""
    request: bytes
    label: str
    status: str                                 # DTC status flag or ident field name
    parse: Callable[[bytes], Any]
    alt: Optional[bytes] = None                 # tried once when the ECU rejects the format


@dataclass(frozen=True)
class Ecu:
    key: str
    name: str
    tx_id: int
    rx_id: int
    sub_addr: Optional[int] = None              # 0x750-style first-byte sub-address
    obd: bool = False                           # answers OBD modes 01/02/03/07/04
    toyota_dtc: bool = False                    # try Toyota service 0x13 (experimental on CAN)
    toyota_ident: bool = False                  # try Toyota service 0x1A 88 01
    bus: str = "can"                            # "can" | "kline" (SIL, DLC3 pin 7) | "bean"
    reachable: bool = True                      # False: no request is sent in a normal scan
    probe: bool = False                         # CAN address unverified on this car
    confidence: str = "primary"
    dict_key: str = ""                          # ECU key of the DTC dictionary
    legacy_name: str = ""                       # name on the MFD DTC screen
    note: str = ""
    silent_limit: int = 1                       # unanswered requests before the ECU is given up
    force_transport: Optional[str] = None       # "gw" | "host"
    items: Dict[str, bytes] = field(default_factory=dict, hash=False, compare=False)
    decoders: Dict[str, Callable[[bytes], Any]] = field(default_factory=dict, hash=False, compare=False)

    @property
    def transport(self) -> str:
        if self.force_transport:
            return self.force_transport
        return "host" if self.sub_addr is not None else "gw"

    @property
    def rx_sub_addr(self) -> Optional[int]:
        """First byte expected on a sub-addressed response (same as the request's)."""
        return self.sub_addr

    @property
    def protocol_label(self) -> str:
        parts = []
        if self.obd:
            parts.append("OBD (ISO 15765-4)")
        if self.toyota_dtc or self.toyota_ident:
            parts.append("Toyota enhanced (KWP2000 services)")
        if not parts:
            parts.append("K-line (SIL)" if self.bus == "kline" else "unknown")
        return " + ".join(parts)

    def dtc_reads(self, kinds: Tuple[str, ...]) -> List[ReadSpec]:
        out: List[ReadSpec] = []
        if self.obd:
            if "stored" in kinds:
                out.append(ReadSpec(services.obd_dtc_request(services.OBD_STORED_DTC), "obd 03",
                                    "stored", services.obd_parse_dtcs))
            if "pending" in kinds:
                out.append(ReadSpec(services.obd_dtc_request(services.OBD_PENDING_DTC), "obd 07",
                                    "pending", services.obd_parse_dtcs))
        if self.toyota_dtc and "stored" in kinds:
            out.append(ReadSpec(services.kwp_dtc_request(), "toyota 13", "stored",
                                services.kwp_parse_dtcs, alt=bytes([services.KWP_READ_DTC, 0xFF, 0x00])))
        return out

    def ident_reads(self) -> List[ReadSpec]:
        out: List[ReadSpec] = []
        if self.obd:
            out.append(ReadSpec(services.obd_vin_request(), "obd 09 02", "vin", services.obd_parse_vin))
            out.append(ReadSpec(services.obd_calid_request(), "obd 09 04", "calibration_ids",
                                services.obd_parse_calids))
        if self.toyota_ident:
            out.append(ReadSpec(services.kwp_ident_request(), "toyota 1A 88 01", "toyota_id",
                                services.kwp_parse_ident))
        return out

    def probe_follow_up(self) -> List[ReadSpec]:
        """Reads tried on a probe address only after it answered tester-present."""
        return [
            ReadSpec(services.kwp_dtc_request(), "toyota 13", "stored", services.kwp_parse_dtcs,
                     alt=bytes([services.KWP_READ_DTC, 0xFF, 0x00])),
        ]

    def clear_requests(self) -> List[Tuple[str, bytes]]:
        """Clear services known to apply to this ECU (label, request).

        Only OBD mode 04 is established for the NHW20's CAN ECUs. Toyota's
        0x14 is not sent: its form on this car is unverified.
        """
        if self.obd and self.reachable:
            return [("obd 04", services.obd_clear_request())]
        return []


# ── data-list decoders (payload = full response: 61 <id> <data...>) ──────────

def _strip2(fn: Callable[[List[int]], Any]) -> Callable[[bytes], Any]:
    return lambda payload: fn(list(payload[2:]))


def _lazy(name: str) -> Callable[[bytes], Any]:
    """Decoder that reuses the existing solicited-CAN parsers (comm.solicited_can)."""
    def decode(payload: bytes) -> Any:
        from ..comm import solicited_can as sc
        return getattr(sc, name)(list(payload[2:]))
    return decode


def _mon_status(payload: bytes) -> Any:
    return services.obd_parse_monitor_status(payload)


_ENGINE_ITEMS = {
    "monitor_status": bytes([0x01, 0x01]),
    "coolant_temp": bytes([0x01, 0x05]),
    "short_fuel_trim": bytes([0x01, 0x06]),
    "long_fuel_trim": bytes([0x01, 0x07]),
    "rpm": bytes([0x01, 0x0C]),
    "speed": bytes([0x01, 0x0D]),
    "distance_since_clear": bytes([0x01, 0x31]),
    "warmups_since_clear": bytes([0x01, 0x30]),
    "vin": bytes([0x09, 0x02]),
}
_ENGINE_DECODERS: Dict[str, Callable[[bytes], Any]] = {
    "monitor_status": _mon_status,
    "coolant_temp": lambda p: {"coolant_temp_c": p[2] - 40},
    "short_fuel_trim": lambda p: {"short_fuel_trim_pct": round((p[2] - 128) * 100 / 128, 1)},
    "long_fuel_trim": lambda p: {"long_fuel_trim_pct": round((p[2] - 128) * 100 / 128, 1)},
    "rpm": lambda p: {"rpm": ((p[2] << 8) | p[3]) / 4},
    "speed": lambda p: {"speed_kmh": p[2]},
    "distance_since_clear": lambda p: {"km_since_clear": (p[2] << 8) | p[3]},
    "warmups_since_clear": lambda p: {"warmups_since_clear": p[2]},
    "vin": lambda p: {"vin": services.obd_parse_vin(p)},
}

_HV_ITEMS = {
    "hybrid_system": bytes([0x21, 0xC3]),
    "hybrid_additional": bytes([0x21, 0xC4]),
    "cruise_control": bytes([0x21, 0xD3]),
    "monitor_status": bytes([0x01, 0x01]),
}
_HV_DECODERS = {
    "hybrid_system": _lazy("_hybrid_21c3_parser"),
    "hybrid_additional": _lazy("_hybrid_21c4_parser"),
    "cruise_control": _lazy("_hybrid_21d3_parser"),
    "monitor_status": _mon_status,
}

_BATT_ITEMS = {
    "battery_detail": bytes([0x21, 0xCE]),
    "battery_temps": bytes([0x21, 0xCF]),
    "battery_resistance": bytes([0x21, 0xD0]),
    "monitor_status": bytes([0x01, 0x01]),
}
_BATT_DECODERS = {
    "battery_detail": _lazy("_battery_21ce_parser"),
    "battery_resistance": _lazy("_battery_21d0_parser"),
    "monitor_status": _mon_status,
}

_KLINE = ("Diagnosed over SIL (DLC3 pin 7, K-line) on the NHW20 — not on the DLC3 CAN. "
          "Needs a K-line interface; see docs/TOYOTA_DIAGNOSTICS.md section 8.")
_BEAN = ("BEAN body ECU: a tester reaches it through the gateway ECU's SIL port "
         "(DLC3 pin 7). Not reachable over CAN on the NHW20.")
_PROBE = (" The CAN id is the one later Toyotas use; no NHW20 answer has been reported. "
          "A scan with kind 'probe' sends one tester-present frame to find out.")


def _probe(key: str, name: str, tx: int, dict_key: str, legacy: str, note: str,
           sub: Optional[int] = None, rx: Optional[int] = None) -> Ecu:
    return Ecu(key=key, name=name, tx_id=tx, rx_id=rx if rx is not None else tx + 8, sub_addr=sub,
               bus="kline", reachable=False, probe=True, confidence="unverified",
               dict_key=dict_key, legacy_name=legacy, note=note + _PROBE)


_ECU_LIST: List[Ecu] = [
    Ecu(key="engine", name="Engine (ECM)", tx_id=0x7E0, rx_id=0x7E8, obd=True, toyota_dtc=True,
        toyota_ident=True, confidence="primary", dict_key="engine", legacy_name="ENGINE",
        note="OBD modes verified by captures; Toyota service 0x13 on CAN is an unverified read attempt.",
        items=_ENGINE_ITEMS, decoders=_ENGINE_DECODERS),
    Ecu(key="hv", name="Hybrid vehicle control ECU", tx_id=0x7E2, rx_id=0x7EA, obd=True,
        toyota_dtc=True, toyota_ident=True, confidence="primary", dict_key="hv", legacy_name="HYBRID",
        note="Mode 03/07 list only the codes that light CHK ENG; codes that light only the master "
             "warning (e.g. P0851/P0852 P position switch, P3102) need the Toyota read (0x13, unverified on CAN).",
        items=_HV_ITEMS, decoders=_HV_DECODERS),
    Ecu(key="battery", name="HV battery ECU", tx_id=0x7E3, rx_id=0x7EB, obd=True, toyota_dtc=True,
        toyota_ident=True, confidence="primary", dict_key="battery", legacy_name="HV_BATT",
        note="OBD modes and 0x21 data verified; Toyota service 0x13 on CAN is an unverified read attempt.",
        items=_BATT_ITEMS, decoders=_BATT_DECODERS),

    _probe("brake", "Skid control ECU (ABS / VSC / ECB)", 0x7B0, "brake", "BRAKE", _KLINE),
    _probe("eps", "Power steering ECU (EPS)", 0x7A1, "eps", "EPS", _KLINE),
    _probe("airbag", "Airbag sensor assembly (SRS)", 0x780, "airbag", "AIRBAG", _KLINE),
    _probe("transmission", "Transmission control ECU (P lock)", 0x727, "transmission", "TRANS",
           _KLINE + " BEAN node; has its own SIL pin (T4-20)."),
    _probe("meter", "Combination meter", 0x7C0, "meter", "METER", _BEAN),
    _probe("ac", "A/C amplifier", 0x7C4, "ac", "AC", _BEAN),
    _probe("body", "Body ECU (main body)", 0x750, "body", "BODY", _BEAN, sub=0x40, rx=0x758),
    _probe("gateway", "Gateway ECU (multiplex DTCs B12xx)", 0x750, "gateway", "GATEWAY",
           _BEAN + " Sub-address 0x5F is the 'central gateway' of 2016+ Toyotas.", sub=0x5F, rx=0x758),

    Ecu(key="power_source", name="Power source control ECU (push-button start)", tx_id=0,
        rx_id=0, bus="bean", reachable=False, confidence="primary", dict_key="power_source",
        legacy_name="POWER", note=_BEAN + " No CAN address or sub-address is known for it."),
    Ecu(key="smart_key", name="Smart key ECU", tx_id=0, rx_id=0, bus="bean", reachable=False,
        confidence="primary", dict_key="smart_key", legacy_name="SMART",
        note=_BEAN + " No CAN address or sub-address is known for it."),
    Ecu(key="immobiliser", name="Transponder key ECU (immobiliser)", tx_id=0, rx_id=0,
        bus="bean", reachable=False, confidence="primary", dict_key="immobiliser",
        legacy_name="IMMO", note=_BEAN + " No CAN address or sub-address is known for it."),
]

ECUS: Dict[str, Ecu] = {e.key: e for e in _ECU_LIST}
SCAN_ORDER: List[str] = [e.key for e in _ECU_LIST]

#: ECUs a default scan talks to.
CAN_ECUS: List[str] = [e.key for e in _ECU_LIST if e.reachable]
#: ECUs a ``probe`` scan additionally sends one frame to.
PROBE_ECUS: List[str] = [e.key for e in _ECU_LIST if e.probe]
