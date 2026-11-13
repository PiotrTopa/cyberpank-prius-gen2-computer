"""
NHW20 DTC dictionary — loader for ``data/dtc_nhw20.json``.

The data file is generated from the Toyota repair manual RM1076E, section 05
(see docs/TOYOTA_DIAGNOSTICS.md, "Code dictionary", and
``tools/diag_dictionary/``). One code can exist in several systems (U0100 is
set by the HV control ECU and by the battery ECU with different meanings), so a
lookup takes the reporting ECU's dictionary key and prefers that entry.

Unknown codes are not an error: :func:`annotate` returns the raw code with an
empty description.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, List, Optional

_DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "dtc_nhw20.json")
_lock = threading.Lock()
_cache: Optional[Dict[str, Any]] = None


def load(path: Optional[str] = None) -> Dict[str, Any]:
    """Return the parsed dictionary document (cached for the default path)."""
    global _cache
    if path is not None:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    with _lock:
        if _cache is None:
            try:
                with open(_DATA_FILE, "r", encoding="utf-8") as f:
                    _cache = json.load(f)
            except (OSError, ValueError):
                _cache = {"meta": {}, "codes": {}}
        return _cache


def entries(code: str) -> List[Dict[str, Any]]:
    """All dictionary entries of ``code`` (one per system that defines it)."""
    return list(load().get("codes", {}).get(code.upper(), []))


def lookup(code: str, ecu: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Best entry for ``code`` as reported by ``ecu`` (a dictionary ECU key).

    The entry of the reporting ECU wins; otherwise the first entry is returned
    with ``"other_ecu": True`` so a caller can show that the text comes from a
    different system's chart.
    """
    found = entries(code)
    if not found:
        return None
    if ecu is not None:
        for e in found:
            if e.get("ecu") == ecu:
                return e
    out = dict(found[0])
    if ecu is not None:
        out["other_ecu"] = True
    return out


def annotate(code: str, ecu: Optional[str] = None, inf: Optional[str] = None) -> Dict[str, Any]:
    """JSON-friendly annotation of one code for the state / report.

    Keys: ``code``, ``description``, ``system``, ``manual_page``, ``manual_url``,
    ``trouble_area`` (list), ``known`` and, when the dictionary says so,
    ``master_warning`` ("all" / "some" / "none" of the code's detail codes light
    the master warning lamp, HV control ECU only) and ``detail`` for a Toyota
    INF (information) code.
    """
    out: Dict[str, Any] = {
        "code": code.upper(), "description": "", "system": "", "manual_page": None,
        "manual_url": None, "trouble_area": [], "known": False,
    }
    e = lookup(code, ecu)
    if e is None:
        return out
    out.update(
        description=e.get("description", ""),
        system=e.get("system", ""),
        manual_page=e.get("page"),
        manual_url=e.get("url"),
        trouble_area=list(e.get("trouble_area", [])),
        known=True,
    )
    if e.get("other_ecu"):
        out["other_ecu"] = True
    if e.get("sub"):
        out["toyota_code"] = e["sub"]
    if "master_warning" in e:
        out["master_warning"] = e["master_warning"]
    infs = e.get("inf") or {}
    if inf is not None and str(inf) in infs:
        d = infs[str(inf)]
        out["detail"] = {
            "inf": str(inf),
            "detection": d.get("detection", ""),
            "master_warning": d.get("master_warning"),
            "chk_eng": d.get("chk_eng"),
            "manual_page": d.get("page"),
        }
    elif infs:
        out["inf_codes"] = sorted(infs)
    return out


def stats() -> Dict[str, int]:
    codes = load().get("codes", {})
    return {
        "codes": len(codes),
        "entries": sum(len(v) for v in codes.values()),
        "inf_codes": sum(len(e.get("inf", {})) for v in codes.values() for e in v),
    }
