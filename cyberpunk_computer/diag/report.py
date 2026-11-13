"""
Scan report files and the clear log.

``<dir>/diag_report_YYYYmmdd_HHMMSS.json``  one file per completed scan
``<dir>/latest.json``                        copy of the newest report
``<dir>/clear_log.jsonl``                    one line per DTC clear (manual or
                                             automatic) with the codes and the
                                             freeze frame read before it

The directory defaults to ``data/diag`` next to the metrics database and can be
moved with the ``PRIUS_DIAG_DIR`` environment variable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REPORT_RE = re.compile(r"^diag_report_\d{8}_\d{6}(?:_\d+)?\.json$")
CLEAR_LOG = "clear_log.jsonl"
KEEP_REPORTS = 200


_active_dir: Optional[str] = None


def default_dir() -> str:
    return os.environ.get("PRIUS_DIAG_DIR") or os.path.join("data", "diag")


def set_active_dir(directory: str) -> None:
    """Remember the directory the running backend writes to (for the HTTP routes)."""
    global _active_dir
    _active_dir = directory


def active_dir() -> str:
    return _active_dir or default_dir()


def write_report(directory: str, report: Dict[str, Any]) -> str:
    """Write ``report`` and return the file name (not the full path)."""
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(report.get("ts") or time.time()))
    name = "diag_report_%s.json" % stamp
    n = 1
    while os.path.exists(os.path.join(directory, name)):
        n += 1
        name = "diag_report_%s_%d.json" % (stamp, n)
    text = json.dumps(report, indent=1, ensure_ascii=False, default=str)
    _atomic_write(os.path.join(directory, name), text)
    _atomic_write(os.path.join(directory, "latest.json"), text)
    _prune(directory)
    return name


def list_reports(directory: str) -> List[Dict[str, Any]]:
    """Newest first: ``[{"name", "size", "mtime"}]``."""
    try:
        names = [n for n in os.listdir(directory) if REPORT_RE.match(n)]
    except OSError:
        return []
    out = []
    for n in sorted(names, reverse=True):
        try:
            st = os.stat(os.path.join(directory, n))
        except OSError:
            continue
        out.append({"name": n, "size": st.st_size, "mtime": st.st_mtime})
    return out


def read_report(directory: str, name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Load one report by file name, or the newest one. ``None`` when missing.
    ``name`` must be a report file name — no path components."""
    if name is None:
        reports = list_reports(directory)
        if not reports:
            return None
        name = reports[0]["name"]
    if not REPORT_RE.match(name):
        return None
    try:
        with open(os.path.join(directory, name), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def append_clear(directory: str, record: Dict[str, Any]) -> bool:
    """Append one clear record to the clear log."""
    try:
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, CLEAR_LOG), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return True
    except OSError:
        logger.exception("could not append to the clear log in %s", directory)
        return False


def read_clears(directory: str, limit: int = 200) -> List[Dict[str, Any]]:
    """Last ``limit`` clear records, oldest first."""
    try:
        with open(os.path.join(directory, CLEAR_LOG), "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _prune(directory: str) -> None:
    for entry in list_reports(directory)[KEEP_REPORTS:]:
        try:
            os.remove(os.path.join(directory, entry["name"]))
        except OSError:
            pass
