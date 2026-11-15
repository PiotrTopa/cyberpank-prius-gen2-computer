"""
MFD alert layer: the one-line banner of the alert framework (backend
``cyberpunk_computer/alerts``, docs/ALERTS.md).

Contract (backend -> frontend), ZMQ event ``alert`` on the existing event
channel, sent on every change and re-sent every 2 s while something is shown:

    {"alert": {"id": "speed_limit", "seq": 7, "text": "LIMIT 50  REAL 62",
               "severity": "info|notice|warning", "icon": "limit", "sound": "notice",
               "ack": true, "advisory": false, "ttl": 5.0,
               "chip": {"real_kmh": 62}}            # optional
     }
    {"alert": null}                                  # nothing shown

A banner older than its ``ttl`` without a refresh is dropped (link lost: never
a stale warning on the glass).

Look (information first, no flashing): the banner takes the status strip's row
(194 x 21, the page dots stay), black, a 1 px frame and the text in the
severity colour (info cyan, notice amber, warning red), a drawn 1-bit 15 x 15
icon on the left, Terminus 14 text, an 8 px ``ADVISORY`` tag for advisory
alerts. The ``chip`` (real speed) is a small framed ``REAL 62`` under the limit
sign in the scene's top-right corner, home page only. Both are cached surfaces
re-drawn only when their content changes.

Acknowledge: a tap on the banner sends ``input_event`` (device ``mfd``, event
``alert_ack``, value ``{"id", "long": false}``), a long press the same with
``long: true`` (mute that type for the rest of the drive).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

EVENT = "alert"
RGB = Tuple[int, int, int]


class AlertFeed:
    """Latest-only hand-off of ``alert`` events (client thread -> main loop)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._alert: Optional[Dict[str, Any]] = None
        self._t = 0.0
        self.received = 0
        self.ack_fn: Optional[Callable[[str, bool], Any]] = None

    def push(self, name: str, data: Any) -> None:
        if name != EVENT or not isinstance(data, Mapping):
            return
        a = data.get("alert")
        with self._lock:
            self._alert = dict(a) if isinstance(a, Mapping) and a.get("text") else None
            self._t = self._clock()
            self.received += 1

    def current(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            a = self._alert
            if a is None:
                return None
            ttl = a.get("ttl") if isinstance(a.get("ttl"), (int, float)) else 5.0
            if self._clock() - self._t > ttl:
                return None
            return a

    def ack(self, long: bool = False) -> bool:
        a = self.current()
        if a is None or not a.get("ack", True) or self.ack_fn is None:
            return False
        try:
            self.ack_fn(str(a.get("id", "")), bool(long))
        except Exception:  # noqa: BLE001 - a lost ack is harmless
            return False
        with self._lock:
            self._alert = None             # hide at once; the backend confirms with null
            self.received += 1
        return True


ALERT_FEED = AlertFeed()


def attach(virtual_twin, feed: AlertFeed = ALERT_FEED) -> None:
    """Register the feed on a twin's event channel and give it a way to send
    acknowledgements (RemoteTwin: ``input_event`` command; local twin: the
    same InputEventAction dispatched into its store)."""
    if hasattr(virtual_twin, "add_event_callback"):
        virtual_twin.add_event_callback(feed.push)
    payload = lambda aid, long: {"id": aid, "long": long}  # noqa: E731
    if hasattr(virtual_twin, "send_input_event"):
        feed.ack_fn = lambda aid, long: virtual_twin.send_input_event("mfd", "alert_ack", payload(aid, long))
    elif getattr(virtual_twin, "store", None) is not None:
        def _local(aid: str, long: bool) -> None:
            from ...state.actions import ActionSource, InputEventAction
            virtual_twin.store.dispatch(InputEventAction("mfd", "alert_ack", payload(aid, long),
                                                         source=ActionSource.UI))
        feed.ack_fn = _local


# ── icons: 15 x 15, 1-bit, drawn with lines and rects only ───────────────────

def _icon(pg, surf, name: str, x: int, y: int, col: RGB) -> None:
    d = pg.draw
    if name == "limit":                   # limit sign: ring with two digits
        d.circle(surf, col, (x + 7, y + 7), 7, 2)
        d.rect(surf, col, (x + 4, y + 5, 3, 5), 1)
        d.rect(surf, col, (x + 8, y + 5, 3, 5), 1)
    elif name == "lead":                  # rear of a car with a gap bar under it
        d.rect(surf, col, (x + 2, y + 2, 11, 7), 1)
        d.line(surf, col, (x + 1, y + 9), (x + 13, y + 9))
        d.rect(surf, col, (x + 2, y + 10, 2, 2))
        d.rect(surf, col, (x + 11, y + 10, 2, 2))
        d.line(surf, col, (x + 3, y + 14), (x + 11, y + 14))
    elif name == "go":                    # car ahead moving away: chevron up
        d.rect(surf, col, (x + 3, y + 8, 9, 6), 1)
        d.lines(surf, col, False, [(x + 3, y + 5), (x + 7, y + 1), (x + 11, y + 5)])
    elif name == "light":                 # three-lamp signal, bottom lamp lit
        d.rect(surf, col, (x + 4, y, 7, 15), 1)
        d.circle(surf, col, (x + 7, y + 3), 1)
        d.circle(surf, col, (x + 7, y + 7), 1)
        d.circle(surf, col, (x + 7, y + 11), 2)
    elif name == "curve":                 # bending road arrow
        d.lines(surf, col, False, [(x + 3, y + 14), (x + 3, y + 7), (x + 6, y + 3), (x + 11, y + 3)])
        d.lines(surf, col, False, [(x + 9, y + 1), (x + 12, y + 3), (x + 9, y + 5)])
    elif name == "recirc":                # loop inside a cabin outline
        d.line(surf, col, (x, y + 12), (x + 14, y + 12))
        d.lines(surf, col, False, [(x + 1, y + 12), (x + 4, y + 5), (x + 11, y + 5), (x + 14, y + 12)])
        d.circle(surf, col, (x + 7, y + 9), 2, 1)
    elif name == "lift":                  # foot off a pedal: up arrow over a bar
        d.line(surf, col, (x + 7, y + 1), (x + 7, y + 9))
        d.lines(surf, col, False, [(x + 3, y + 5), (x + 7, y + 1), (x + 11, y + 5)])
        d.line(surf, col, (x + 2, y + 13), (x + 12, y + 13))
        d.line(surf, col, (x + 2, y + 14), (x + 12, y + 14))
    elif name == "snow":                  # snowflake
        d.line(surf, col, (x + 7, y), (x + 7, y + 14))
        d.line(surf, col, (x + 1, y + 3), (x + 13, y + 11))
        d.line(surf, col, (x + 1, y + 11), (x + 13, y + 3))
    elif name == "defog":                 # rear window with heat lines
        d.rect(surf, col, (x + 1, y + 3, 13, 9), 1)
        for yy in (5, 7, 9):
            d.line(surf, col, (x + 3, y + yy), (x + 11, y + yy))
    elif name == "service":               # spanner
        d.circle(surf, col, (x + 4, y + 4), 3, 1)
        d.line(surf, col, (x + 6, y + 6), (x + 13, y + 13))
        d.line(surf, col, (x + 7, y + 6), (x + 13, y + 12))
    elif name == "fuel":                  # pump
        d.rect(surf, col, (x + 2, y + 1, 7, 13), 1)
        d.line(surf, col, (x + 3, y + 5), (x + 7, y + 5))
        d.lines(surf, col, False, [(x + 9, y + 3), (x + 12, y + 6), (x + 12, y + 12)])
    elif name == "fault":                 # engine block outline with "!"
        d.rect(surf, col, (x + 1, y + 4, 13, 8), 1)
        d.line(surf, col, (x + 4, y + 2), (x + 10, y + 2))
        d.line(surf, col, (x + 7, y + 6), (x + 7, y + 8))
        surf.set_at((x + 7, y + 10), col)
    elif name == "lane":                  # two lane lines, one crossed
        d.line(surf, col, (x + 2, y + 14), (x + 4, y))
        d.line(surf, col, (x + 12, y + 14), (x + 10, y))
        d.line(surf, col, (x + 9, y + 9), (x + 14, y + 6))
    elif name == "door":                  # car outline with an open door
        d.rect(surf, col, (x + 1, y + 5, 10, 6), 1)
        d.line(surf, col, (x + 11, y + 5), (x + 14, y + 2))
    else:                                 # "info": i in a box
        d.rect(surf, col, (x + 1, y + 1, 13, 13), 1)
        d.line(surf, col, (x + 7, y + 6), (x + 7, y + 11))
        surf.set_at((x + 7, y + 4), col)


def severity_color(sev: str, colors: Mapping[str, RGB]) -> RGB:
    if sev == "warning":
        return colors["red_bright"]
    if sev == "notice":
        return colors["amber"]
    return colors["cyan_bright"]


class AlertBanner:
    """Cached banner surface for the strip row."""

    def __init__(self, size: Tuple[int, int], colors: Optional[Mapping[str, RGB]] = None):
        import pygame
        from ..colors import COLORS
        self._pg = pygame
        self.w, self.h = size
        self.colors = dict(colors or COLORS)
        self.surface = pygame.Surface(size)
        self._sig: Optional[tuple] = None
        self._chip = None
        self._chip_sig: Optional[tuple] = None
        self.redraws = 0

    @staticmethod
    def signature(a: Optional[Mapping[str, Any]]) -> Optional[tuple]:
        if a is None:
            return None
        return (a.get("id"), a.get("text"), a.get("severity"), a.get("icon"), bool(a.get("advisory")))

    def render(self, a: Mapping[str, Any]):
        sig = self.signature(a)
        if sig != self._sig:
            self._sig = sig
            self._draw(a)
            self.redraws += 1
        return self.surface

    def _draw(self, a: Mapping[str, Any]) -> None:
        from ..fonts import get_mono_font, get_tiny_font
        pg = self._pg
        s = self.surface
        c = self.colors
        col = severity_color(str(a.get("severity") or "notice"), c)
        s.fill((0, 0, 0))
        pg.draw.rect(s, col, (0, 0, self.w, self.h), 1)
        _icon(pg, s, str(a.get("icon") or "info"), 4, (self.h - 15) // 2, col)
        x = 23
        right = self.w - 4
        if a.get("advisory"):
            tag = get_tiny_font(8).render("ADVISORY", False, c["text_secondary"])
            right -= tag.get_width() + 3
            s.blit(tag, (right + 3, (self.h - tag.get_height()) // 2))
        font = get_mono_font(14)
        text = str(a.get("text") or "")
        img = font.render(text, False, col)
        while img.get_width() > right - x and len(text) > 4:
            text = text[:-2].rstrip() + "~"
            img = font.render(text, False, col)
        s.blit(img, (x, (self.h - img.get_height()) // 2))

    def chip(self, real_kmh: Any):
        """Small framed ``REAL nn`` (the true speed next to the limit)."""
        try:
            v = int(round(float(real_kmh)))
        except (TypeError, ValueError):
            return None
        sig = (v,)
        if sig != self._chip_sig:
            from ..fonts import get_mono_font, get_tiny_font
            pg = self._pg
            c = self.colors
            cap = get_tiny_font(8).render("REAL", False, c["text_secondary"])
            num = get_mono_font(14).render(str(v), False, c["amber"])
            w = cap.get_width() + num.get_width() + 9
            surf = pg.Surface((w, 18))
            surf.fill((0, 0, 0))
            pg.draw.rect(surf, c["amber"], (0, 0, w, 18), 1)
            surf.blit(cap, (3, (18 - cap.get_height()) // 2))
            surf.blit(num, (cap.get_width() + 6, 2))
            self._chip = surf
            self._chip_sig = sig
        return self._chip
