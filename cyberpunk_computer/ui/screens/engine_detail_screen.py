"""
Engine detail screen — multi-page diagnostic view.

Four sub-pages navigated by ROTATE_LEFT / ROTATE_RIGHT:
  Page 0: Engine — RPM bar, fuel flow, consumption, tank levels, motor RPMs
  Page 1: Temps — hybrid system temperatures (ICE, MG1, MG2, converter)
  Page 2: Fuel charts — fuel flow time-series (1h), trip consumed stat
  Page 3: Temp charts — ICE coolant + inverter temps (1h)

This is a diagnostic screen — stays on until strong-press exit (no auto-timeout).
"""

import pygame
import time
from typing import Tuple, Optional

from .base import Screen
from ..colors import COLORS, dim_color
from ..fonts import get_title_font, get_mono_font, get_font
from ...input.manager import InputEvent as IE
from ...state.store import Store, StateSlice


class EngineDetailScreen(Screen):
    """Multi-page engine diagnostic screen."""

    HEADER_HEIGHT = 22
    RPM_BAR_HEIGHT = 28
    NUM_PAGES = 4
    PAGE_LABELS = ["ENGINE", "TEMPS", "FUEL CHARTS", "TEMP CHARTS"]

    def __init__(self, size: Tuple[int, int], app=None, store: Optional[Store] = None):
        super().__init__(size, app)
        self._store = store
        self._page = 0

        # Engine data
        self._rpm: int = 0
        self._fuel_flow_rate: Optional[float] = None
        self._instant_consumption: float = 0.0
        self._consumption_unit: str = "L/100"
        self._fuel_level: int = 0
        self._lpg_level: int = 0
        self._ice_temp: Optional[float] = None
        self._inverter_temp: Optional[float] = None
        self._ice_running: bool = False
        self._ev_mode: bool = False
        self._trip_fuel_consumed: float = 0.0

        # Hybrid motor data
        self._mg1_rpm: Optional[int] = None
        self._mg2_rpm: Optional[int] = None
        self._mg1_inv_temp: Optional[float] = None
        self._mg2_inv_temp: Optional[float] = None
        self._mg1_mot_temp: Optional[float] = None
        self._mg2_mot_temp: Optional[float] = None
        self._converter_temp: Optional[float] = None

        # Fuel type
        self._active_fuel = None

        # History data is in store.chart_data (collected by ChartDataRule)

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
        v = state.vehicle

        self._rpm = v.rpm or 0
        self._fuel_flow_rate = v.fuel_flow_rate
        self._instant_consumption = v.instant_consumption
        self._consumption_unit = v.consumption_unit
        self._fuel_level = v.fuel_level
        self._lpg_level = v.lpg_level
        self._ice_temp = v.ice_coolant_temp
        self._inverter_temp = v.inverter_temp
        self._ice_running = v.ice_running
        self._ev_mode = getattr(state.dynamics, 'ev_mode', False)
        self._active_fuel = v.active_fuel
        self._trip_fuel_consumed = v.trip_fuel_consumed

        self._mg1_rpm = v.mg1_rpm
        self._mg2_rpm = v.mg2_rpm
        self._mg1_inv_temp = v.mg1_inverter_temp
        self._mg2_inv_temp = v.mg2_inverter_temp
        self._mg1_mot_temp = v.mg1_motor_temp
        self._mg2_mot_temp = v.mg2_motor_temp
        self._converter_temp = v.converter_temp

        # History data is collected by ChartDataRule continuously

    def handle_input(self, event) -> bool:
        if event == IE.ROTATE_RIGHT:
            self._page = (self._page + 1) % self.NUM_PAGES
            return True
        if event == IE.ROTATE_LEFT:
            self._page = (self._page - 1) % self.NUM_PAGES
            return True
        if event in (IE.PRESS_LIGHT, IE.PRESS_STRONG, IE.BACK):
            if self.app:
                self.app.pop_screen()
            return True
        return False

    # ─── Rendering ────────────────────────────────────────────────────

    def render(self, surface: pygame.Surface) -> None:
        surface.fill(COLORS["bg_dark"])
        self._render_header(surface)

        if self._page == 0:
            self._render_overview(surface)
        elif self._page == 1:
            self._render_temperatures(surface)
        elif self._page == 2:
            self._render_fuel_charts(surface)
        elif self._page == 3:
            self._render_temp_charts(surface)

        self._render_footer(surface)

    # ─── Header ───────────────────────────────────────────────────────

    def _render_header(self, surface: pygame.Surface) -> None:
        pygame.draw.rect(
            surface, COLORS["bg_panel"],
            (0, 0, self.width, self.HEADER_HEIGHT),
        )
        font = get_title_font(14)

        # Page label centered
        label = self.PAGE_LABELS[self._page]
        s = font.render(f"ENGINE \u2022 {label}", True, COLORS["cyan"])
        surface.blit(s, ((self.width - s.get_width()) // 2,
                         (self.HEADER_HEIGHT - s.get_height()) // 2))

        # ICE status left
        font_sm = get_mono_font(10)
        if self._ice_running:
            st_surf = font_sm.render("ICE ON", True, COLORS["active"])
        else:
            st_surf = font_sm.render("ICE OFF", True, COLORS["text_tertiary"])
        surface.blit(st_surf, (6, (self.HEADER_HEIGHT - st_surf.get_height()) // 2))

        # EV mode right
        if self._ev_mode:
            ev_surf = font_sm.render("EV", True, COLORS["active"])
            surface.blit(ev_surf, (self.width - ev_surf.get_width() - 8,
                                   (self.HEADER_HEIGHT - ev_surf.get_height()) // 2))

        # Page dots
        dot_y = self.HEADER_HEIGHT - 5
        dot_total_w = self.NUM_PAGES * 8
        dot_x0 = (self.width - dot_total_w) // 2
        for i in range(self.NUM_PAGES):
            color = COLORS["cyan"] if i == self._page else COLORS["border_normal"]
            pygame.draw.circle(surface, color, (dot_x0 + i * 8 + 3, dot_y), 2)

        pygame.draw.line(
            surface, COLORS["border_focus"],
            (0, self.HEADER_HEIGHT - 1), (self.width, self.HEADER_HEIGHT - 1),
        )

    # ─── Page 0: Overview ─────────────────────────────────────────────

    def _render_overview(self, surface: pygame.Surface) -> None:
        self._render_rpm_bar(surface)
        self._render_engine_data(surface)
        self._render_motor_rpms(surface)

    def _render_rpm_bar(self, surface: pygame.Surface) -> None:
        """Horizontal RPM bar with tick marks."""
        y0 = self.HEADER_HEIGHT + 4
        bar_h = self.RPM_BAR_HEIGHT
        pad_x = 10
        bar_w = self.width - pad_x * 2

        max_rpm = 5000
        rpm_ratio = min(1.0, self._rpm / max_rpm) if max_rpm > 0 else 0.0

        font_val = get_mono_font(14)
        font_tiny = get_mono_font(8)

        # RPM value text (left)
        rpm_text = str(int(self._rpm))
        rpm_color = COLORS["green_bright"]
        if self._rpm > 3500:
            rpm_color = COLORS["red_bright"]
        elif self._rpm > 2000:
            rpm_color = COLORS["yellow"]

        rpm_surf = font_val.render(rpm_text, True, rpm_color)
        surface.blit(rpm_surf, (pad_x, y0))

        lbl_surf = font_tiny.render("RPM", True, COLORS["text_secondary"])
        surface.blit(lbl_surf, (pad_x + rpm_surf.get_width() + 4,
                                y0 + rpm_surf.get_height() - lbl_surf.get_height()))

        # Bar area (right portion)
        bar_x = pad_x + 80
        bar_actual_w = bar_w - 80
        bar_y = y0 + 4
        bar_actual_h = bar_h - 12

        pygame.draw.rect(
            surface, COLORS["bg_panel"],
            (bar_x, bar_y, bar_actual_w, bar_actual_h),
        )
        pygame.draw.rect(
            surface, COLORS["border_normal"],
            (bar_x, bar_y, bar_actual_w, bar_actual_h), 1,
        )

        # Filled portion
        fill_w = int(bar_actual_w * rpm_ratio)
        if fill_w > 0:
            for px in range(fill_w):
                t = px / bar_actual_w
                if t < 0.5:
                    color = COLORS["green_bright"]
                elif t < 0.75:
                    color = COLORS["yellow"]
                else:
                    color = COLORS["red_bright"]
                pygame.draw.line(
                    surface, color,
                    (bar_x + px, bar_y + 1),
                    (bar_x + px, bar_y + bar_actual_h - 2),
                )

        # Tick marks at 1k intervals
        for rpm_mark in range(0, max_rpm + 1, 1000):
            tx = bar_x + int(rpm_mark / max_rpm * bar_actual_w)
            pygame.draw.line(
                surface, COLORS["text_tertiary"],
                (tx, bar_y + bar_actual_h), (tx, bar_y + bar_actual_h + 3),
            )
            if rpm_mark % 2000 == 0:
                t_surf = font_tiny.render(str(rpm_mark // 1000) + "k", True, COLORS["text_tertiary"])
                surface.blit(t_surf, (tx - t_surf.get_width() // 2, bar_y + bar_actual_h + 3))

    def _render_engine_data(self, surface: pygame.Surface) -> None:
        """Engine data readouts: fuel flow and tank levels."""
        y0 = self.HEADER_HEIGHT + self.RPM_BAR_HEIGHT + 14
        pad_x = 12
        col_w = self.width // 2 - pad_x

        font_label = get_mono_font(11)
        font_value = get_mono_font(14)
        font_section = get_title_font(11)
        row_h = 22

        # ── Left column: Fuel flow ──
        lx = pad_x
        y = y0

        s = font_section.render("FUEL", True, COLORS["cyan"])
        surface.blit(s, (lx, y))
        y += 16

        # Flow rate
        lbl = font_label.render("FLOW", True, COLORS["text_secondary"])
        surface.blit(lbl, (lx + 4, y + 3))
        if self._fuel_flow_rate is not None and self._fuel_flow_rate > 0.05:
            val_str = f"{self._fuel_flow_rate:.1f} L/h"
            color = COLORS["green_bright"]
            if self._fuel_flow_rate > 5.0:
                color = COLORS["yellow"]
            if self._fuel_flow_rate > 10.0:
                color = COLORS["red_bright"]
        else:
            val_str = "--.- L/h"
            color = COLORS["text_tertiary"]
        val_surf = font_value.render(val_str, True, color)
        surface.blit(val_surf, (lx + col_w - val_surf.get_width(), y))
        y += row_h

        # Instant consumption
        lbl = font_label.render("CONS", True, COLORS["text_secondary"])
        surface.blit(lbl, (lx + 4, y + 3))
        if self._instant_consumption > 0.0:
            val_str = f"{self._instant_consumption:.1f} {self._consumption_unit}"
            color = COLORS["green_bright"]
            if self._instant_consumption > 5.0:
                color = COLORS["yellow"]
            if self._instant_consumption > 10.0:
                color = COLORS["red_bright"]
        else:
            val_str = f"--.- {self._consumption_unit}"
            color = COLORS["text_tertiary"]
        val_surf = font_value.render(val_str, True, color)
        surface.blit(val_surf, (lx + col_w - val_surf.get_width(), y))
        y += row_h

        # Fuel type
        lbl = font_label.render("TYPE", True, COLORS["text_secondary"])
        surface.blit(lbl, (lx + 4, y + 3))
        from ...state.app_state import FuelType
        if self._active_fuel == FuelType.PETROL:
            val_surf = font_value.render("PETROL", True, COLORS["yellow"])
        elif self._active_fuel == FuelType.LPG:
            val_surf = font_value.render("LPG", True, COLORS["green_bright"])
        else:
            val_surf = font_value.render("OFF", True, COLORS["text_tertiary"])
        surface.blit(val_surf, (lx + col_w - val_surf.get_width(), y))

        # ── Right column: Tank levels ──
        rx = self.width // 2 + pad_x
        y = y0

        s = font_section.render("LEVELS", True, COLORS["cyan"])
        surface.blit(s, (rx, y))
        y += 16

        # Petrol level
        lbl = font_label.render("PETROL", True, COLORS["text_secondary"])
        surface.blit(lbl, (rx + 4, y + 3))
        val_str = f"{self._fuel_level} L"
        color = COLORS["yellow"] if self._fuel_level < 10 else COLORS["text_value"]
        val_surf = font_value.render(val_str, True, color)
        surface.blit(val_surf, (rx + col_w - val_surf.get_width(), y))
        y += row_h

        # LPG level
        lbl = font_label.render("LPG", True, COLORS["text_secondary"])
        surface.blit(lbl, (rx + 4, y + 3))
        val_str = f"{self._lpg_level} L"
        color = COLORS["yellow"] if self._lpg_level < 10 else COLORS["text_value"]
        val_surf = font_value.render(val_str, True, color)
        surface.blit(val_surf, (rx + col_w - val_surf.get_width(), y))
        y += row_h

        # Trip consumed
        lbl = font_label.render("TRIP", True, COLORS["text_secondary"])
        surface.blit(lbl, (rx + 4, y + 3))
        if self._trip_fuel_consumed > 0.001:
            val_str = f"{self._trip_fuel_consumed:.2f} L"
            color = COLORS["yellow"]
        else:
            val_str = "0.00 L"
            color = COLORS["text_dim"]
        val_surf = font_value.render(val_str, True, color)
        surface.blit(val_surf, (rx + col_w - val_surf.get_width(), y))

    def _render_motor_rpms(self, surface: pygame.Surface) -> None:
        """MG1/MG2/ICE RPMs in a row at the bottom."""
        footer_h = 22
        row_h = 30
        y0 = self.height - footer_h - row_h - 4
        pad_x = 12
        font_label = get_mono_font(10)
        font_value = get_mono_font(12)

        col_w = (self.width - pad_x * 2) // 3

        motors = [
            ("MG1 GEN", self._mg1_rpm),
            ("MG2 MOT", self._mg2_rpm),
            ("ICE", self._rpm if self._rpm > 0 else None),
        ]

        for i, (label, rpm_val) in enumerate(motors):
            x = pad_x + i * col_w
            lbl = font_label.render(label, True, COLORS["text_secondary"])
            surface.blit(lbl, (x, y0))

            if rpm_val is not None:
                val_str = f"{int(rpm_val)} rpm"
                color = COLORS["text_value"] if abs(rpm_val) > 0 else COLORS["text_tertiary"]
            else:
                val_str = "-- rpm"
                color = COLORS["text_tertiary"]

            val_surf = font_value.render(val_str, True, color)
            surface.blit(val_surf, (x, y0 + 14))

    # ─── Page 1: Temperatures ─────────────────────────────────────────

    def _render_temperatures(self, surface: pygame.Surface) -> None:
        """Full-page temperature display organized by subsystem."""
        font_label = get_mono_font(11)
        font_value = get_mono_font(18)
        font_section = get_title_font(11)

        pad_x = 14
        col_w = (self.width - pad_x * 3) // 2
        y_start = self.HEADER_HEIGHT + 10
        row_h = 56

        # ── Left column: ICE + MG1 ──
        lx = pad_x
        y = y_start

        s = font_section.render("ENGINE & MG1", True, COLORS["cyan"])
        surface.blit(s, (lx, y))
        y += 18

        left_temps = [
            ("ICE COOLANT", self._ice_temp),
            ("MG1 INVERTER", self._mg1_inv_temp),
            ("MG1 MOTOR", self._mg1_mot_temp),
        ]

        for label_text, temp_val in left_temps:
            lbl = font_label.render(label_text, True, COLORS["text_secondary"])
            surface.blit(lbl, (lx + 4, y))

            if temp_val is not None:
                val_str = f"{int(temp_val)}\u00b0C"
                color = self._temp_color(temp_val)
            else:
                val_str = "--\u00b0C"
                color = COLORS["text_tertiary"]

            val_surf = font_value.render(val_str, True, color)
            surface.blit(val_surf, (lx + 4, y + 16))
            y += row_h

        # ── Right column: MG2 + Converter ──
        rx = pad_x * 2 + col_w
        y = y_start

        s = font_section.render("MG2 & DC/DC", True, COLORS["cyan"])
        surface.blit(s, (rx, y))
        y += 18

        right_temps = [
            ("DC/DC CONV", self._converter_temp),
            ("MG2 INVERTER", self._mg2_inv_temp),
            ("MG2 MOTOR", self._mg2_mot_temp),
        ]

        for label_text, temp_val in right_temps:
            lbl = font_label.render(label_text, True, COLORS["text_secondary"])
            surface.blit(lbl, (rx + 4, y))

            if temp_val is not None:
                val_str = f"{int(temp_val)}\u00b0C"
                color = self._temp_color(temp_val)
            else:
                val_str = "--\u00b0C"
                color = COLORS["text_tertiary"]

            val_surf = font_value.render(val_str, True, color)
            surface.blit(val_surf, (rx + 4, y + 16))
            y += row_h

    # ─── Page 2: Fuel Charts ──────────────────────────────────────────

    def _render_fuel_charts(self, surface: pygame.Surface) -> None:
        pad = 6
        y_top = self.HEADER_HEIGHT + pad
        footer_h = 14
        available_h = self.height - y_top - footer_h - pad

        # Trip consumed stat bar at top
        stat_h = 20
        self._render_trip_stat(surface, pad, y_top, self.width - pad * 2, stat_h)

        # Fuel flow chart below — data from model
        chart_y = y_top + stat_h + pad
        chart_h = available_h - stat_h - pad

        chart = self._store.chart_data if self._store else None
        data = chart.fuel_flow_history if chart else []
        replay_speed = self._store.replay_speed if self._store else 1.0
        tw = max(10, int(3600 / replay_speed))

        self._render_time_graph(
            surface,
            x=pad,
            y=chart_y,
            w=self.width - pad * 2,
            h=chart_h,
            title="FUEL FLOW (1h)",
            data=data,
            time_window=tw,
            min_val=0.0,
            max_val=15.0,
            unit="L/h",
            color=COLORS["yellow"],
            warn_threshold=5.0,
            crit_threshold=10.0,
        )

    def _render_trip_stat(self, surface: pygame.Surface, x: int, y: int, w: int, h: int) -> None:
        """Render trip fuel consumed stat bar."""
        font_label = get_mono_font(10)
        font_value = get_mono_font(14)

        pygame.draw.rect(surface, COLORS["bg_panel"], (x, y, w, h))
        pygame.draw.rect(surface, COLORS["border_normal"], (x, y, w, h), 1)

        lbl = font_label.render("TRIP CONSUMED", True, COLORS["text_secondary"])
        surface.blit(lbl, (x + 6, y + (h - lbl.get_height()) // 2))

        if self._trip_fuel_consumed > 0.001:
            val_text = f"{self._trip_fuel_consumed:.2f} L"
            color = COLORS["yellow"]
        else:
            val_text = "0.00 L"
            color = COLORS["text_dim"]
        val_surf = font_value.render(val_text, True, color)
        surface.blit(val_surf, (x + w - val_surf.get_width() - 6,
                                y + (h - val_surf.get_height()) // 2))

    # ─── Page 3: Temperature Charts ───────────────────────────────────

    def _render_temp_charts(self, surface: pygame.Surface) -> None:
        pad = 6
        y_top = self.HEADER_HEIGHT + pad
        footer_h = 14
        available_h = self.height - y_top - footer_h - pad
        half_w = (self.width - pad * 3) // 2

        chart = self._store.chart_data if self._store else None
        replay_speed = self._store.replay_speed if self._store else 1.0
        tw = max(10, int(3600 / replay_speed))

        # ICE Temperature (left) — data from model
        ice_data = chart.ice_temp_history if chart else []
        self._render_time_graph(
            surface,
            x=pad,
            y=y_top,
            w=half_w,
            h=available_h,
            title="ICE COOLANT (1h)",
            data=ice_data,
            time_window=tw,
            min_val=0.0,
            max_val=120.0,
            unit="\u00b0C",
            color=COLORS["cyan_bright"],
            warn_threshold=95.0,
            crit_threshold=105.0,
        )

        # Inverter Temperature (right) — data from model
        inv_data = chart.inverter_temp_history if chart else []
        self._render_time_graph(
            surface,
            x=pad * 2 + half_w,
            y=y_top,
            w=half_w,
            h=available_h,
            title="INVERTER (1h)",
            data=inv_data,
            time_window=tw,
            min_val=0.0,
            max_val=100.0,
            unit="\u00b0C",
            color=COLORS.get("warm_bright", COLORS["active"]),
            warn_threshold=70.0,
            crit_threshold=85.0,
        )

    # ─── Time Graph ───────────────────────────────────────────────────

    def _render_time_graph(
        self,
        surface: pygame.Surface,
        x: int, y: int, w: int, h: int,
        title: str,
        data,
        time_window: int,
        min_val: float, max_val: float,
        unit: str,
        color: tuple,
        warn_threshold: Optional[float] = None,
        crit_threshold: Optional[float] = None,
    ) -> None:
        """Render a time-series line graph with cyberpunk styling."""
        font_title = get_font(8, "title")
        font_label = get_font(7)
        font_value_sm = get_font(7, "mono")

        # Title
        title_surf = font_title.render(title, True, COLORS["cyan_bright"])
        surface.blit(title_surf, (x, y))

        # Graph area (below title)
        gy = y + 12
        gh = h - 22  # Room for title and X-axis labels
        gw = w - 24  # Room for Y-axis labels
        gx = x + 24

        if gh < 10 or gw < 10:
            return

        # Background
        bg_rect = pygame.Rect(gx, gy, gw, gh)
        pygame.draw.rect(surface, COLORS["bg_panel"], bg_rect)
        pygame.draw.rect(surface, COLORS["border_normal"], bg_rect, 1)

        val_range = max_val - min_val
        if val_range <= 0:
            return

        # Y-axis grid lines and labels
        num_y_lines = 4
        for i in range(num_y_lines + 1):
            t = i / num_y_lines
            line_y = gy + gh - int(t * gh)
            val = min_val + t * val_range

            # Grid line (dotted)
            for gx_dot in range(gx, gx + gw, 4):
                pygame.draw.rect(surface, COLORS["border_normal"], (gx_dot, line_y, 1, 1))

            # Label
            lbl_text = f"{val:.0f}"
            lbl_surf = font_value_sm.render(lbl_text, True, COLORS["text_dim"])
            surface.blit(lbl_surf, (x, line_y - lbl_surf.get_height() // 2))

        # Threshold lines
        if warn_threshold is not None:
            warn_y = gy + gh - int(((warn_threshold - min_val) / val_range) * gh)
            if gy <= warn_y <= gy + gh:
                for gx_dot in range(gx, gx + gw, 6):
                    pygame.draw.rect(surface, COLORS["yellow"], (gx_dot, warn_y, 3, 1))

        if crit_threshold is not None:
            crit_y = gy + gh - int(((crit_threshold - min_val) / val_range) * gh)
            if gy <= crit_y <= gy + gh:
                for gx_dot in range(gx, gx + gw, 6):
                    pygame.draw.rect(surface, COLORS["red_bright"], (gx_dot, crit_y, 3, 1))

        # Plot data
        if len(data) >= 2:
            # Snap to integer second so bucket boundaries are stable between frames
            now = float(int(time.time()))

            # Bucket data per pixel column
            buckets = {}
            for ts, val in data:
                age = now - ts
                if age > time_window or age < 0:
                    continue
                px = gx + gw - int((age / time_window) * gw)
                px = max(gx, min(gx + gw, px))
                if px not in buckets:
                    buckets[px] = []
                buckets[px].append(val)

            if buckets:
                points = []
                for px in sorted(buckets.keys()):
                    vals = buckets[px]
                    avg_val = sum(vals) / len(vals)
                    clamped = max(min_val, min(max_val, avg_val))
                    py = gy + gh - int(((clamped - min_val) / val_range) * gh)
                    points.append((px, py))

                if len(points) >= 2:
                    # Glow effect
                    glow_color = dim_color(color, 0.3)
                    pygame.draw.lines(surface, glow_color, False, points, 3)
                    # Main line
                    pygame.draw.lines(surface, color, False, points, 1)

                # Current value at top-right
                if data:
                    last_val = data[-1][1]
                    val_color = color
                    if crit_threshold is not None and last_val >= crit_threshold:
                        val_color = COLORS["red_bright"]
                    elif warn_threshold is not None and last_val >= warn_threshold:
                        val_color = COLORS["yellow"]

                    val_text = f"{last_val:.1f}{unit}"
                    val_surf = font_label.render(val_text, True, val_color)
                    surface.blit(val_surf, (gx + gw - val_surf.get_width(), gy - 1))
        else:
            # No data placeholder
            no_data = font_label.render("NO DATA", True, COLORS["text_dim"])
            surface.blit(no_data, (gx + (gw - no_data.get_width()) // 2, gy + gh // 2 - 5))

        # X-axis time labels
        time_labels = [
            (0, "now"),
            (time_window // 4, f"-{time_window // 240}m"),
            (time_window // 2, f"-{time_window // 120}m"),
            (time_window * 3 // 4, f"-{time_window * 3 // 240}m"),
            (time_window, f"-{time_window // 60}m"),
        ]
        for offset, label in time_labels:
            lx = gx + gw - int((offset / time_window) * gw)
            if gx <= lx <= gx + gw:
                lbl_surf = font_value_sm.render(label, True, COLORS["text_dim"])
                surface.blit(lbl_surf, (lx - lbl_surf.get_width() // 2, gy + gh + 2))

    # ─── Footer ───────────────────────────────────────────────────────

    def _render_footer(self, surface: pygame.Surface) -> None:
        """Render bottom bar with [BACK] button and page navigation hint."""
        font = get_mono_font(10)
        bar_h = 22
        bar_y = self.height - bar_h

        # Dark bar background
        pygame.draw.rect(surface, COLORS["bg_panel"], (0, bar_y, self.width, bar_h))
        pygame.draw.line(surface, COLORS["border_dim"], (0, bar_y), (self.width, bar_y))

        # BACK button — always focused (single action)
        btn_width = 80
        bx = 8
        by = bar_y + 3
        bh = bar_h - 6
        pygame.draw.rect(surface, COLORS["cyan"], (bx, by, btn_width, bh))
        s = font.render("BACK", True, COLORS["bg_dark"])
        surface.blit(s, (bx + (btn_width - s.get_width()) // 2, by + (bh - s.get_height()) // 2))

        # Page dots on the right
        dot_font = get_mono_font(9)
        page_hint = "\u25C0 ROTATE \u25B6"
        hint_surf = dot_font.render(page_hint, True, COLORS["text_tertiary"])
        surface.blit(hint_surf, (self.width - hint_surf.get_width() - 8,
                                 bar_y + (bar_h - hint_surf.get_height()) // 2))

    # ─── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _temp_color(temp: Optional[float]) -> Tuple[int, int, int]:
        if temp is None:
            return COLORS["text_tertiary"]
        if temp < 40:
            return COLORS.get("green_bright", (0, 200, 100))
        elif temp < 70:
            return COLORS["text_value"]
        elif temp < 90:
            return COLORS.get("warm_bright", COLORS["active"])
        else:
            return COLORS.get("alert_bright", (255, 60, 60))
