"""
Battery detail screen — multi-page diagnostic view.

Three sub-pages navigated by ROTATE_LEFT / ROTATE_RIGHT:
  Page 0: Overview — SOC, voltage/current/power, temps, charge state
  Page 1: Block voltages — bar chart of 14 block deviations from mean
  Page 2: Delta-V history — time-series trend of max block deviation

This is a diagnostic screen — stays on until strong-press exit (no auto-timeout).
"""

import pygame
import time
from collections import deque
from typing import Tuple, Optional, List

from .base import Screen
from ..colors import COLORS
from ..fonts import get_title_font, get_mono_font
from ...input.manager import InputEvent as IE
from ...state.store import Store, StateSlice


class BatteryScreen(Screen):
    """Multi-page HV battery diagnostic screen."""

    HEADER_HEIGHT = 22
    NUM_PAGES = 3
    PAGE_LABELS = ["OVERVIEW", "BLOCK VOLTAGES", "DELTA-V HISTORY"]
    HISTORY_MAX = 120  # samples for delta-V history

    def __init__(self, size: Tuple[int, int], app=None, store: Optional[Store] = None):
        super().__init__(size, app)
        self._store = store
        self._page = 0

        # Battery data
        self._soc: float = 0.0
        self._delta_soc: Optional[float] = None
        self._voltage: Optional[float] = None
        self._current: Optional[float] = None
        self._power_kw: Optional[float] = None
        self._charging: bool = False
        self._discharging: bool = False
        self._temp: Optional[float] = None
        self._min_temp: Optional[float] = None
        self._max_temp: Optional[float] = None
        self._block_voltages: Optional[tuple] = None
        self._ev_mode: bool = False

        # Delta-V history
        self._delta_v_history: deque = deque(maxlen=self.HISTORY_MAX)
        self._last_sample_time: float = 0.0
        self._sample_interval: float = 1.0  # seconds

        self._unsub_fns: list = []

    # ─── Lifecycle ────────────────────────────────────────────────────

    def on_enter(self) -> None:
        if self._store:
            self._unsub_fns.append(
                self._store.subscribe(StateSlice.ALL, self._on_state_update)
            )
            self._on_state_update(self._store.state)

    def on_exit(self) -> None:
        for unsub in self._unsub_fns:
            unsub()
        self._unsub_fns.clear()

    def _on_state_update(self, state) -> None:
        e = state.energy
        self._soc = e.battery_soc
        self._delta_soc = e.battery_delta_soc
        self._voltage = e.hv_battery_voltage
        self._current = e.hv_battery_current
        self._power_kw = e.battery_power_kw
        self._charging = e.charging
        self._discharging = e.discharging
        self._temp = e.battery_temp
        self._min_temp = e.battery_min_cell_temp
        self._max_temp = e.battery_max_cell_temp
        self._block_voltages = e.block_voltages
        self._ev_mode = getattr(state.dynamics, "ev_mode", False)

    # ─── Input ────────────────────────────────────────────────────────

    def handle_input(self, event) -> bool:
        if event == IE.PRESS_STRONG:
            self._exit_screen()
            return True
        if event == IE.BACK:
            self._exit_screen()
            return True
        if event == IE.ROTATE_RIGHT:
            self._page = (self._page + 1) % self.NUM_PAGES
            return True
        if event == IE.ROTATE_LEFT:
            self._page = (self._page - 1) % self.NUM_PAGES
            return True
        # Light press — no action (diagnostic screen)
        return False

    def _exit_screen(self) -> None:
        if self.app:
            self.app.pop_screen()

    # ─── Update ───────────────────────────────────────────────────────

    def update(self, dt: float) -> None:
        super().update(dt)
        # No auto-timeout — diagnostic screen stays until dismissed
        self._record_delta_v_sample()

    def _record_delta_v_sample(self) -> None:
        """Record delta-V sample for history graph."""
        now = time.time()
        if now - self._last_sample_time < self._sample_interval:
            return
        self._last_sample_time = now

        voltages = self._get_block_voltages()
        if voltages and len(voltages) >= 2:
            delta = max(voltages) - min(voltages)
            self._delta_v_history.append(delta)

    # ─── Block voltage helpers ────────────────────────────────────────

    def _get_block_voltages(self) -> Optional[tuple]:
        """Return real block voltages or None if unavailable."""
        if self._block_voltages and len(self._block_voltages) == 14:
            return self._block_voltages
        return None

    # ─── Rendering ────────────────────────────────────────────────────

    def render(self, surface: pygame.Surface) -> None:
        surface.fill(COLORS["bg_dark"])
        self._render_header(surface)

        body_y = self.HEADER_HEIGHT + 2
        body_h = self.height - body_y

        if self._page == 0:
            self._render_overview(surface, body_y, body_h)
        elif self._page == 1:
            self._render_block_voltages(surface, body_y, body_h)
        elif self._page == 2:
            self._render_delta_v_history(surface, body_y, body_h)

    # ─── Header ───────────────────────────────────────────────────────

    def _render_header(self, surface: pygame.Surface) -> None:
        """Title bar with page indicator dots."""
        pygame.draw.rect(
            surface, COLORS["bg_panel"], (0, 0, self.width, self.HEADER_HEIGHT)
        )

        font = get_title_font(12)
        title = f"BATTERY — {self.PAGE_LABELS[self._page]}"
        s = font.render(title, True, COLORS["cyan"])
        surface.blit(s, (8, (self.HEADER_HEIGHT - s.get_height()) // 2))

        # EV mode indicator
        if self._ev_mode:
            ev = font.render("EV", True, COLORS["active"])
            surface.blit(
                ev,
                (self.width - ev.get_width() - 8,
                 (self.HEADER_HEIGHT - ev.get_height()) // 2),
            )

        # Page dots
        dot_r = 3
        dot_gap = 12
        total_w = self.NUM_PAGES * dot_gap
        dot_x = self.width // 2 - total_w // 2
        dot_y = self.HEADER_HEIGHT // 2
        for i in range(self.NUM_PAGES):
            cx = dot_x + i * dot_gap + dot_r
            color = COLORS["cyan"] if i == self._page else COLORS["text_tertiary"]
            pygame.draw.circle(surface, color, (cx, dot_y), dot_r)

        pygame.draw.line(
            surface,
            COLORS["border_focus"],
            (0, self.HEADER_HEIGHT - 1),
            (self.width, self.HEADER_HEIGHT - 1),
        )

    # ─── Page 0: Overview ─────────────────────────────────────────────

    def _render_overview(self, surface: pygame.Surface, y0: int, h: int) -> None:
        """SOC, voltage/current/power, temperatures, charge state."""
        font_section = get_title_font(11)
        font_label = get_mono_font(11)
        font_value = get_mono_font(14)
        font_small = get_mono_font(10)

        pad = 10
        col1_x = pad
        col2_x = self.width // 2 + pad
        y = y0 + 6

        # ── Left column: SOC + Electrical ──
        s = font_section.render("STATE OF CHARGE", True, COLORS["cyan"])
        surface.blit(s, (col1_x, y))
        y += 18

        # SOC bar
        soc_pct = self._soc * 100 if self._soc > 0 else 0
        bar_x = col1_x
        bar_w = self.width // 2 - pad * 2
        bar_h = 14
        pygame.draw.rect(surface, COLORS["bg_panel"], (bar_x, y, bar_w, bar_h), border_radius=2)
        fill_w = int(bar_w * min(soc_pct / 100, 1.0))
        if fill_w > 0:
            bar_color = COLORS.get("green_bright", (0, 230, 118))
            if soc_pct < 40:
                bar_color = COLORS["active"]
            pygame.draw.rect(surface, bar_color, (bar_x, y, fill_w, bar_h), border_radius=2)

        soc_str = f"{soc_pct:.0f}%"
        soc_surf = font_value.render(soc_str, True, COLORS["text_value"])
        surface.blit(soc_surf, (bar_x + bar_w + 6, y - 2))
        y += bar_h + 6

        # Delta SOC
        if self._delta_soc is not None:
            dsoc_str = f"DELTA SOC: {self._delta_soc:+.2f}"
        else:
            dsoc_str = "DELTA SOC: --"
        dsoc_surf = font_small.render(dsoc_str, True, COLORS["text_secondary"])
        surface.blit(dsoc_surf, (col1_x + 4, y))
        y += 18

        # Electrical
        s = font_section.render("ELECTRICAL", True, COLORS["cyan"])
        surface.blit(s, (col1_x, y))
        y += 16

        rows = [
            ("VOLTAGE", f"{self._voltage:.1f} V" if self._voltage else "--- V"),
            ("CURRENT", f"{self._current:+.1f} A" if self._current is not None else "-- A"),
            ("POWER", f"{self._power_kw:+.1f} kW" if self._power_kw is not None else "-- kW"),
        ]
        for lbl, val in rows:
            ls = font_label.render(lbl, True, COLORS["text_secondary"])
            vs = font_value.render(val, True, COLORS["text_value"])
            surface.blit(ls, (col1_x + 4, y + 2))
            surface.blit(vs, (col1_x + 90, y))
            y += 22

        # ── Right column: Temperatures + state ──
        ry = y0 + 6
        s = font_section.render("TEMPERATURES", True, COLORS["cyan"])
        surface.blit(s, (col2_x, ry))
        ry += 18

        temp_rows = [
            ("PACK", self._temp),
            ("MIN CELL", self._min_temp),
            ("MAX CELL", self._max_temp),
        ]
        for lbl, tv in temp_rows:
            ls = font_label.render(lbl, True, COLORS["text_secondary"])
            surface.blit(ls, (col2_x + 4, ry + 2))
            if tv is not None:
                vs = font_value.render(f"{int(tv)}°C", True, self._temp_color(tv))
            else:
                vs = font_value.render("--°C", True, COLORS["text_tertiary"])
            surface.blit(vs, (col2_x + 100, ry))
            ry += 22

        # Temp spread
        if self._min_temp is not None and self._max_temp is not None:
            spread = self._max_temp - self._min_temp
            sp_str = f"SPREAD: {spread:.1f}°C"
            sp_color = COLORS.get("green_bright", (0, 200, 100))
            if spread > 3:
                sp_color = COLORS["active"]
            if spread > 5:
                sp_color = COLORS.get("alert_bright", (255, 60, 60))
            sp_surf = font_small.render(sp_str, True, sp_color)
            surface.blit(sp_surf, (col2_x + 4, ry))
        ry += 20

        # Charge state
        s = font_section.render("STATUS", True, COLORS["cyan"])
        surface.blit(s, (col2_x, ry))
        ry += 16

        if self._charging:
            state_str, state_color = "CHARGING", COLORS.get("green_bright", COLORS["cyan"])
        elif self._discharging:
            state_str, state_color = "DISCHARGING", COLORS["active"]
        else:
            state_str, state_color = "IDLE", COLORS["text_tertiary"]
        st_surf = font_value.render(state_str, True, state_color)
        surface.blit(st_surf, (col2_x + 4, ry))

        # Footer hint
        hint_font = get_mono_font(9)
        hint = "[SCROLL] PAGE   [HOLD] EXIT"
        hint_surf = hint_font.render(hint, True, COLORS["text_tertiary"])
        surface.blit(
            hint_surf,
            ((self.width - hint_surf.get_width()) // 2, self.height - hint_surf.get_height() - 3),
        )

    # ─── Page 1: Block Voltages ───────────────────────────────────────

    def _render_block_voltages(self, surface: pygame.Surface, y0: int, h: int) -> None:
        """Bar chart of 14 block voltage deviations from mean."""
        font_label = get_mono_font(10)
        font_value = get_mono_font(12)
        font_tiny = get_mono_font(9)

        voltages = self._get_block_voltages()
        if voltages is None or len(voltages) < 2:
            msg = font_label.render("NO BLOCK VOLTAGE DATA", True, COLORS["text_tertiary"])
            surface.blit(
                msg,
                ((self.width - msg.get_width()) // 2, y0 + h // 2 - msg.get_height() // 2),
            )
            return

        avg_v = sum(voltages) / len(voltages)
        deviations = [v - avg_v for v in voltages]
        max_dev = max(abs(d) for d in deviations)
        scale = max(max_dev, 0.05)
        delta_v = max(voltages) - min(voltages)

        # Stats line
        stats_y = y0 + 4
        stats_str = f"AVG {avg_v:.3f}V  \u0394V {delta_v:.3f}V  MIN {min(voltages):.3f}V  MAX {max(voltages):.3f}V"
        st_surf = font_label.render(stats_str, True, COLORS["text_secondary"])
        surface.blit(st_surf, (8, stats_y))

        # Bar chart area
        chart_x = 8
        chart_w = self.width - 16
        chart_top = stats_y + 18
        chart_bottom = self.height - 28  # room for block numbers
        chart_h = chart_bottom - chart_top

        num_blocks = len(voltages)
        gap = 3
        bar_w = (chart_w - (num_blocks - 1) * gap) // num_blocks
        mid_y = chart_top + chart_h // 2
        max_bar_h = chart_h // 2 - 4

        # Zero line
        pygame.draw.line(
            surface,
            COLORS["text_tertiary"],
            (chart_x, mid_y),
            (chart_x + chart_w, mid_y),
            1,
        )

        # Scale labels
        pos_label = f"+{scale:.3f}V"
        neg_label = f"-{scale:.3f}V"
        pl_surf = font_tiny.render(pos_label, True, COLORS["text_tertiary"])
        nl_surf = font_tiny.render(neg_label, True, COLORS["text_tertiary"])
        surface.blit(pl_surf, (chart_x + chart_w - pl_surf.get_width(), chart_top))
        surface.blit(nl_surf, (chart_x + chart_w - nl_surf.get_width(), chart_bottom - nl_surf.get_height()))

        for i, dev in enumerate(deviations):
            x = chart_x + i * (bar_w + gap)
            bar_h = int(abs(dev) / scale * max_bar_h)
            bar_h = max(bar_h, 1)

            abs_dev = abs(dev)
            if abs_dev < 0.02:
                color = COLORS.get("green_bright", (0, 230, 118))
            elif abs_dev < 0.05:
                color = COLORS.get("warm_bright", COLORS["active"])
            else:
                color = COLORS.get("alert_bright", (255, 60, 60))

            if dev >= 0:
                rect = pygame.Rect(x, mid_y - bar_h, bar_w, bar_h)
            else:
                rect = pygame.Rect(x, mid_y, bar_w, bar_h)
            pygame.draw.rect(surface, color, rect)

            # Voltage label on top of each bar
            v_str = f"{voltages[i]:.2f}"
            v_surf = font_tiny.render(v_str, True, COLORS["text_secondary"])
            v_x = x + (bar_w - v_surf.get_width()) // 2
            if dev >= 0:
                v_y = mid_y - bar_h - v_surf.get_height() - 1
            else:
                v_y = mid_y + bar_h + 1
            # Clamp to chart area
            v_y = max(chart_top, min(v_y, chart_bottom - v_surf.get_height()))
            surface.blit(v_surf, (v_x, v_y))

        # Block numbers at bottom
        for i in range(num_blocks):
            x = chart_x + i * (bar_w + gap)
            num_str = str(i + 1)
            num_surf = font_tiny.render(num_str, True, COLORS["text_tertiary"])
            surface.blit(
                num_surf,
                (x + (bar_w - num_surf.get_width()) // 2, chart_bottom + 2),
            )

        # Footer hint
        hint_surf = font_tiny.render("[SCROLL] PAGE   [HOLD] EXIT", True, COLORS["text_tertiary"])
        surface.blit(
            hint_surf,
            ((self.width - hint_surf.get_width()) // 2, self.height - hint_surf.get_height() - 3),
        )

    # ─── Page 2: Delta-V History ──────────────────────────────────────

    def _render_delta_v_history(self, surface: pygame.Surface, y0: int, h: int) -> None:
        """Time-series line graph of max block voltage deviation."""
        font_label = get_mono_font(10)
        font_tiny = get_mono_font(9)
        font_value = get_mono_font(14)

        # Current delta-V value
        voltages = self._get_block_voltages()
        current_dv: Optional[float] = None
        if voltages and len(voltages) >= 2:
            current_dv = max(voltages) - min(voltages)

        # Big current value
        val_y = y0 + 6
        if current_dv is not None:
            dv_str = f"{current_dv:.3f} V"
            dv_color = COLORS.get("green_bright", (0, 230, 118))
            if current_dv > 0.05:
                dv_color = COLORS.get("warm_bright", COLORS["active"])
            if current_dv > 0.10:
                dv_color = COLORS.get("alert_bright", (255, 60, 60))
        else:
            dv_str = "--- V"
            dv_color = COLORS["text_tertiary"]

        lbl = font_label.render("CURRENT \u0394V", True, COLORS["text_secondary"])
        surface.blit(lbl, (8, val_y))
        val_surf = font_value.render(dv_str, True, dv_color)
        surface.blit(val_surf, (8, val_y + 14))

        # Stats  
        if self._delta_v_history:
            avg_dv = sum(self._delta_v_history) / len(self._delta_v_history)
            max_dv = max(self._delta_v_history)
            min_dv = min(self._delta_v_history)
            stats = f"AVG {avg_dv:.3f}  MIN {min_dv:.3f}  MAX {max_dv:.3f}"
        else:
            stats = "COLLECTING DATA..."
        st_surf = font_tiny.render(stats, True, COLORS["text_secondary"])
        surface.blit(st_surf, (self.width - st_surf.get_width() - 8, val_y + 2))

        # Graph area
        graph_x = 40
        graph_w = self.width - graph_x - 12
        graph_top = val_y + 40
        graph_bottom = self.height - 20
        graph_h = graph_bottom - graph_top

        if graph_h < 20 or graph_w < 40:
            return

        # Graph border
        pygame.draw.rect(
            surface,
            COLORS["border_dim"],
            (graph_x, graph_top, graph_w, graph_h),
            1,
        )

        if len(self._delta_v_history) < 2:
            msg = font_label.render("COLLECTING...", True, COLORS["text_tertiary"])
            surface.blit(
                msg,
                (graph_x + (graph_w - msg.get_width()) // 2,
                 graph_top + (graph_h - msg.get_height()) // 2),
            )
            return

        # Y-axis scale
        data = list(self._delta_v_history)
        y_min = max(0, min(data) - 0.01)
        y_max = max(data) + 0.01
        if y_max - y_min < 0.02:
            y_max = y_min + 0.02
        y_range = y_max - y_min

        # Y-axis labels
        for frac in (0.0, 0.5, 1.0):
            val = y_min + y_range * (1.0 - frac)
            py = graph_top + int(frac * graph_h)
            lbl_str = f"{val:.2f}"
            lbl_surf = font_tiny.render(lbl_str, True, COLORS["text_tertiary"])
            surface.blit(lbl_surf, (graph_x - lbl_surf.get_width() - 3, py - lbl_surf.get_height() // 2))
            # Grid line
            pygame.draw.line(
                surface,
                COLORS["bg_panel"],
                (graph_x + 1, py),
                (graph_x + graph_w - 2, py),
                1,
            )

        # Plot line
        n = len(data)
        points = []
        for i, val in enumerate(data):
            px = graph_x + int(i / (n - 1) * (graph_w - 2)) + 1
            frac = (val - y_min) / y_range
            py = graph_bottom - 1 - int(frac * (graph_h - 2))
            points.append((px, py))

        if len(points) >= 2:
            # Filled area under curve
            fill_points = list(points) + [(points[-1][0], graph_bottom - 1), (points[0][0], graph_bottom - 1)]
            fill_color = (*COLORS["cyan"][:3], 30) if len(COLORS["cyan"]) >= 3 else (0, 255, 255, 30)
            fill_surf = pygame.Surface((graph_w, graph_h), pygame.SRCALPHA)
            shifted = [(p[0] - graph_x, p[1] - graph_top) for p in fill_points]
            try:
                pygame.draw.polygon(fill_surf, fill_color, shifted)
                surface.blit(fill_surf, (graph_x, graph_top))
            except (ValueError, TypeError):
                pass

            pygame.draw.lines(surface, COLORS["cyan"], False, points, 2)

        # Footer hint
        hint_surf = font_tiny.render("[SCROLL] PAGE   [HOLD] EXIT", True, COLORS["text_tertiary"])
        surface.blit(
            hint_surf,
            ((self.width - hint_surf.get_width()) // 2, self.height - hint_surf.get_height() - 3),
        )

    # ─── Utilities ────────────────────────────────────────────────────

    @staticmethod
    def _temp_color(temp: Optional[float]) -> Tuple[int, int, int]:
        if temp is None:
            return COLORS["text_tertiary"]
        if temp < 40:
            return COLORS.get("green_bright", (0, 200, 100))
        if temp < 70:
            return COLORS["text_value"]
        if temp < 90:
            return COLORS.get("warm_bright", COLORS["active"])
        return COLORS.get("alert_bright", (255, 60, 60))
