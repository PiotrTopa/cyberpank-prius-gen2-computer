"""
Power box: the strip under the scene (HV battery power, history graph, SOC).

``PowerHistory`` (pure Python) keeps one averaged sample per second for an
hour; ``PowerBox`` draws the strip into a cached surface that is re-drawn
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


class PowerBox:
    """Draws the power strip. ``render()`` returns the cached surface."""

    RANGE_KW = 25.0
    COLS = 41
    COL_W = 3                       # 2 px bar + 1 px gap

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

    def render(self, power_kw: Optional[float], soc: Optional[float], bars: Optional[int],
               ice_running: Optional[bool], ready: bool, time_base_s: int):
        kw_txt = "--.-" if power_kw is None else ("%+.1f" % power_kw if abs(power_kw) >= 0.05 else "0.0")
        soc_pct = None if soc is None else int(soc * 100)
        sig = (kw_txt, soc_pct, bars, ice_running, ready, time_base_s, self.history.version)
        if sig != self._sig:
            self._sig = sig
            self._draw(power_kw, kw_txt, soc_pct, bars, ice_running, ready, time_base_s)
            self.redraws += 1
        return self.surface

    def _draw(self, power_kw, kw_txt, soc_pct, bars, ice_running, ready, time_base_s) -> None:
        pg = self._pg
        c = self.colors
        s = self.surface
        s.fill((0, 0, 0))
        w, h = self.w, self.h
        label = c["text_secondary"]

        # ---- left: live value ----
        s.blit(self._t("HV POWER", 8, label, False), (5, 3))
        if power_kw is None:
            vcol = c["text_dim"]
        elif power_kw < -0.05:
            vcol = c["green_bright"]
        else:
            vcol = c["text_value"]
        val = self._t(kw_txt, 16, vcol, True)
        s.blit(val, (5, 13))
        s.blit(self._t("kW", 8, label, False), (5 + val.get_width() + 2, 20))
        x = 5
        if ice_running:
            t = self._t("ICE", 8, (0, 0, 0), False)
            pg.draw.rect(s, c["orange"], (x, 36, t.get_width() + 6, 11))
            s.blit(t, (x + 3, 38))
            x += t.get_width() + 9
        elif ice_running is False and ready:
            t = self._t("EV", 8, c["green_bright"], False)
            pg.draw.rect(s, c["green_bright"], (x, 36, t.get_width() + 6, 11), 1)
            s.blit(t, (x + 3, 38))
            x += t.get_width() + 9
        if power_kw is not None and power_kw < -0.05:
            s.blit(self._t("CHG", 8, c["green_bright"], False), (x, 38))

        # ---- middle: history bars, discharge up / charge down ----
        gx, gy, gh = 68, 4, h - 9
        gw = self.COLS * self.COL_W
        mid = gy + gh // 2
        half = gh // 2 - 1
        pg.draw.line(s, c["cyan_dark"], (gx - 2, gy), (gx - 2, gy + gh - 1))
        pg.draw.line(s, c["cyan_dark"], (gx + gw, gy), (gx + gw, gy + gh - 1))
        for yy in (gy, gy + gh - 1):
            pg.draw.line(s, c["cyan_dark"], (gx - 4, yy), (gx - 2, yy))
            pg.draw.line(s, c["cyan_dark"], (gx + gw, yy), (gx + gw + 2, yy))
        pg.draw.line(s, c["cyan_dim"], (gx - 2, mid), (gx + gw, mid))
        cols = self.history.columns(self.COLS, time_base_s)
        up, up_hi = c["cyan_mid"], c["cyan_bright"]
        dn, dn_hi = (0, 150, 78), c["green_bright"]
        rng = self.RANGE_KW
        for i, v in enumerate(cols):
            if v is None:
                continue
            x0 = gx + i * self.COL_W
            n = int(round(min(1.0, abs(v) / rng) * half))
            if n <= 0:
                continue
            if v > 0:
                pg.draw.rect(s, up, (x0, mid - n, 2, n))
                pg.draw.line(s, up_hi, (x0, mid - n), (x0 + 1, mid - n))
            else:
                pg.draw.rect(s, dn, (x0, mid + 1, 2, n))
                pg.draw.line(s, dn_hi, (x0, mid + n), (x0 + 1, mid + n))
        tb = "%ds" % time_base_s if time_base_s < 120 else "%dm" % (time_base_s // 60)
        t = self._t(tb, 8, label, False)
        s.blit(t, (gx + 1, gy + gh - 8))

        # ---- right: SOC, 8 segments like the factory gauge ----
        sx = gx + gw + 9
        sw = w - sx - 5
        s.blit(self._t("SOC", 8, label, False), (sx, 3))
        if soc_pct is not None:
            pt = self._t("%d" % soc_pct, 8, c["text_value"], False)
            s.blit(pt, (sx + sw - pt.get_width(), 3))
        if bars is None and soc_pct is not None:
            bars = int(round(soc_pct / 100.0 * 8))
        fill = soc_color(soc_pct if soc_pct is not None else 50, c)
        seg_h, gap = 3, 1
        y0 = 13
        for i in range(8):
            yy = y0 + (7 - i) * (seg_h + gap)
            if bars is not None and i < bars:
                pg.draw.rect(s, fill, (sx, yy, sw, seg_h))
            else:
                pg.draw.rect(s, c["cyan_dark"], (sx, yy, sw, seg_h), 1)
