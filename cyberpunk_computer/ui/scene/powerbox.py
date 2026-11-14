"""
Status strip under the scene: cruise slot, HV battery power, history, SOC.

``PowerHistory`` (pure Python) keeps one averaged sample per second for an
hour; ``StatusStrip`` draws the strip into a cached surface that is re-drawn
only when a shown value changes (about once a second while driving).
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable, Deque, List, Mapping, Optional, Tuple

RGB = Tuple[int, int, int]


class PowerHistory:
    """Per-second averages of the HV battery power (kW; + discharge, - charge)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, seconds: int = 3600):
        self._clock = clock
        self._bins: Deque[Tuple[int, Optional[float]]] = deque(maxlen=seconds)
        self._sec: Optional[int] = None
        self._sum = 0.0
        self._n = 0
        self.version = 0            # bumps once per stored second

    def add(self, kw: Optional[float]) -> None:
        sec = int(self._clock())
        if self._sec is None:
            self._sec = sec
        if sec != self._sec:
            self._flush()
            self._sec = sec
        if kw is not None:
            self._sum += kw
            self._n += 1

    def _flush(self) -> None:
        if self._sec is None:
            return
        self._bins.append((self._sec, self._sum / self._n if self._n else None))
        self._sum = 0.0
        self._n = 0
        self.version += 1

    def current(self) -> Optional[float]:
        return self._sum / self._n if self._n else None

    def columns(self, n: int, time_base_s: float) -> List[Optional[float]]:
        """``n`` column averages over the last ``time_base_s`` s, newest last.
        The newest column includes the running second."""
        now = int(self._clock())
        period = max(1e-6, float(time_base_s) / n)
        sums = [0.0] * n
        cnts = [0] * n
        cur = self.current()
        if cur is not None and self._sec is not None and now - self._sec < period:
            sums[n - 1] += cur
            cnts[n - 1] += 1
        for sec, val in reversed(self._bins):
            age = now - sec
            if age >= time_base_s:
                break
            if val is None:
                continue
            c = n - 1 - int(age / period)
            if 0 <= c < n:
                sums[c] += val
                cnts[c] += 1
        return [sums[i] / cnts[i] if cnts[i] else None for i in range(n)]


def soc_color(pct: int, colors: Mapping[str, RGB]) -> RGB:
    if pct >= 45:
        return colors["green_bright"]
    if pct >= 30:
        return colors["yellow"]
    return colors["red_bright"]


class StatusStrip:
    """The slim strip under the scene: cruise slot | HV power | (page dots).

        x 0..71     CRUISE  state (-- / OFF / READY / ON), set speed, gap bars
        x 74..      HV power in kW with ICE / EV / CHG flag, history bars
                    (1 px bar + 1 px gap, discharge up, charge down), SOC as
                    the eight segments of the factory gauge

    The cruise slot is reserved for the cruise settings: today it shows what
    the state has (active flag, main switch, set / memory speed) and "--" for
    what it does not; the gap bars appear only when a gap setting exists.
    ``render()`` returns a cached surface, redrawn when a shown value changes.
    """

    RANGE_KW = 25.0
    COLS = 22
    CRUISE_W = 72

    def __init__(self, size: Tuple[int, int], history: PowerHistory,
                 colors: Optional[Mapping[str, RGB]] = None):
        import pygame
        from ..colors import COLORS
        self._pg = pygame
        self.w, self.h = size
        self.history = history
        self.colors = dict(colors or COLORS)
        self.surface = pygame.Surface(size)
        self.surface.fill((0, 0, 0))
        self._sig: Optional[tuple] = None
        self._text = {}
        self.redraws = 0

    def _t(self, s: str, size: int, color: RGB, mono: bool):
        key = (s, size, color, mono)
        t = self._text.get(key)
        if t is None:
            from ..fonts import get_mono_font, get_tiny_font
            if len(self._text) > 120:
                self._text.clear()
            t = (get_mono_font(size) if mono else get_tiny_font(size)).render(s, False, color)
            self._text[key] = t
        return t

    @staticmethod
    def cruise_view(active: bool, main: Optional[bool], set_kmh: Optional[int],
                    mem_kmh: Optional[int]) -> Tuple[str, str, str]:
        """(state word, speed text, speed kind) of the cruise slot.

        state: ON (engaged) / READY (main switch on) / OFF (main switch off) /
        "--" (nothing known). speed kind: "set", "mem" (memory speed, not
        engaged) or "" (unknown, shown as "--")."""
        if active:
            state = "ON"
        elif main is True:
            state = "READY"
        elif main is False:
            state = "OFF"
        else:
            state = "--"
        if set_kmh:
            return state, str(set_kmh), "set"
        if mem_kmh:
            return state, str(mem_kmh), "mem"
        return state, "--", ""

    def render(self, power_kw: Optional[float], soc: Optional[float], bars: Optional[int],
               ice_running: Optional[bool], ready: bool, time_base_s: int,
               cruise_active: bool = False, cruise_main: Optional[bool] = None,
               cruise_set: Optional[int] = None, cruise_mem: Optional[int] = None,
               cruise_gap: Optional[int] = None):
        kw_txt = "--.-" if power_kw is None else ("%+.1f" % power_kw if abs(power_kw) >= 0.05 else "0.0")
        soc_pct = None if soc is None else int(soc * 100)
        sig = (kw_txt, soc_pct, bars, ice_running, ready, time_base_s, self.history.version,
               cruise_active, cruise_main, cruise_set, cruise_mem, cruise_gap)
        if sig != self._sig:
            self._sig = sig
            self._draw(power_kw, kw_txt, soc_pct, bars, ice_running, ready, time_base_s,
                       self.cruise_view(cruise_active, cruise_main, cruise_set, cruise_mem),
                       cruise_gap)
            self.redraws += 1
        return self.surface

    def _draw(self, power_kw, kw_txt, soc_pct, bars, ice_running, ready, time_base_s,
              cruise, cruise_gap) -> None:
        pg = self._pg
        c = self.colors
        s = self.surface
        s.fill((0, 0, 0))
        h = self.h
        label = c["text_secondary"]

        # ---- cruise slot ----
        state, speed, kind = cruise
        s.blit(self._t("CRUISE", 8, label, False), (3, 2))
        if state == "ON":
            st = self._t("ON", 8, (0, 0, 0), False)
            pg.draw.rect(s, c["green_bright"], (3, 10, st.get_width() + 5, 10))
            s.blit(st, (6, 12))
        else:
            col = c["cyan_bright"] if state == "READY" else c["text_dim"]
            s.blit(self._t(state, 8, col, False), (3, 12))
        if kind == "set":
            vcol = c["green_bright"] if state == "ON" else c["text_value"]
        elif kind == "mem":
            vcol = c["text_dim"]
        else:
            vcol = c["cyan_dark"]
        val = self._t(speed, 14, vcol, True)
        s.blit(val, (58 - val.get_width(), 4))
        if kind == "mem":
            s.blit(self._t("M", 8, label, False), (59, 12))
        if cruise_gap:
            # adaptive-cruise gap setting: 1..3 bars (drawn only when known)
            for i in range(3):
                bh = 4 + i * 3
                rect = (61 + i * 3, 17 - bh, 2, bh)
                pg.draw.rect(s, c["green_bright"] if i < cruise_gap else c["cyan_dark"], rect)
        pg.draw.line(s, c["border_normal"], (self.CRUISE_W, 0), (self.CRUISE_W, h - 1))

        # ---- HV power ----
        x0 = self.CRUISE_W + 4
        if power_kw is None:
            vcol = c["text_dim"]
        elif power_kw < -0.05:
            vcol = c["green_bright"]
        else:
            vcol = c["text_value"]
        val = self._t(kw_txt, 14, vcol, True)
        s.blit(val, (x0, 4))
        fx = x0 + 37
        s.blit(self._t("kW", 8, label, False), (fx, 3))
        if ice_running:
            s.blit(self._t("ICE", 8, c["orange"], False), (fx, 12))
        elif power_kw is not None and power_kw < -0.05:
            s.blit(self._t("CHG", 8, c["green_bright"], False), (fx, 12))
        elif ice_running is False and ready:
            s.blit(self._t("EV", 8, c["green_bright"], False), (fx, 12))

        # ---- history: 1 px bars, discharge up / charge down ----
        gx = x0 + 57
        gw = self.COLS * 2
        mid = h // 2
        half = mid - 2
        pg.draw.line(s, c["cyan_dark"], (gx - 2, mid - half), (gx - 2, mid + half))
        pg.draw.line(s, c["cyan_dim"], (gx - 2, mid), (gx + gw, mid))
        cols = self.history.columns(self.COLS, time_base_s)
        up, up_hi = c["cyan_mid"], c["cyan_bright"]
        dn, dn_hi = (0, 150, 78), c["green_bright"]
        for i, v in enumerate(cols):
            if v is None:
                continue
            n = int(round(min(1.0, abs(v) / self.RANGE_KW) * half))
            if n <= 0:
                continue
            x = gx + i * 2
            if v > 0:
                pg.draw.line(s, up, (x, mid - 1), (x, mid - n))
                s.set_at((x, mid - n), up_hi)
            else:
                pg.draw.line(s, dn, (x, mid + 1), (x, mid + n))
                s.set_at((x, mid + n), dn_hi)

        # ---- SOC: 8 segments ----
        sx = gx + gw + 5
        sw = max(6, self.w - sx - 3)
        if bars is None and soc_pct is not None:
            bars = int(round(soc_pct / 100.0 * 8))
        fill = soc_color(soc_pct if soc_pct is not None else 50, c)
        y0 = mid - 8
        for i in range(8):
            yy = y0 + (7 - i) * 2
            if bars is not None and i < bars:
                pg.draw.line(s, fill, (sx, yy), (sx + sw - 1, yy))
            else:
                pg.draw.line(s, c["cyan_dark"], (sx, yy), (sx + 1, yy))
                pg.draw.line(s, c["cyan_dark"], (sx + sw - 2, yy), (sx + sw - 1, yy))
