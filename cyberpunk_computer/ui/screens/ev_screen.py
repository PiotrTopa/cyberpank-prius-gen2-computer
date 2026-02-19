"""
EV & Battery detail screen.

Full-screen dashboard showing detailed hybrid/EV system data:
- MG1/MG2 inverter and motor temperatures
- MG1/MG2 RPMs
- Converter temperature
- HV battery details (voltage, current, power, SOC, temps)
"""

import pygame
import time
from typing import Tuple, Optional

from .base import Screen
from ..widgets.base import Rect
from ..colors import COLORS
from ..fonts import get_title_font, get_mono_font
from ...input.manager import InputEvent as IE
from ...state.store import Store, StateSlice


class EVScreen(Screen):
    """
    EV & Battery detail screen.

    Read-only dashboard with all hybrid system telemetry.
    Auto-updates from Store subscriptions.

    Layout (480x240):
    ┌───────────────────────────────────────────────────────┐
    │                   EV / BATTERY                        │
    ├──────────────────────┬────────────────────────────────┤
    │   TEMPERATURES       │   MOTORS / GENERATORS         │
    │  MG1 INV:  -- °C     │   MG1:  ---- RPM              │
    │  MG2 INV:  -- °C     │   MG2:  ---- RPM              │
    │  MG1 MOT:  -- °C     │                               │
    │  MG2 MOT:  -- °C     │   BATTERY                     │
    │  CONV:     -- °C     │   SOC:  --%   ΔSOC: --.--     │
    │  ICE:      -- °C     │   V: ---V  A: --A  P: --kW    │
    │  BATT:     -- °C     │   TEMP: --°C (--/-- min/max)  │
    └──────────────────────┴────────────────────────────────┘
    """

    HEADER_HEIGHT = 24
    SECTION_PAD = 6
    LINE_HEIGHT = 22
    COL_DIVIDER_X = 220  # Left column width

    def __init__(self, size: Tuple[int, int], app=None, store: Optional[Store] = None):
        """Initialize EV screen."""
        super().__init__(size, app)
        self._store = store
        self._last_activity = time.time()

        # Temperature values
        self._mg1_inv_temp: Optional[float] = None
        self._mg2_inv_temp: Optional[float] = None
        self._mg1_mot_temp: Optional[float] = None
        self._mg2_mot_temp: Optional[float] = None
        self._conv_temp: Optional[float] = None
        self._ice_temp: Optional[float] = None
        self._batt_temp: Optional[float] = None
        self._batt_min_temp: Optional[float] = None
        self._batt_max_temp: Optional[float] = None

        # Motor/Generator RPMs
        self._mg1_rpm: Optional[int] = None
        self._mg2_rpm: Optional[int] = None
        self._ice_rpm: int = 0
        self._ice_solicited_rpm: Optional[int] = None

        # Battery
        self._batt_soc: float = 0.0
        self._batt_delta_soc: Optional[float] = None
        self._batt_voltage: Optional[float] = None
        self._batt_current: Optional[float] = None
        self._batt_power: Optional[float] = None
        self._ev_mode: bool = False
        self._charging: bool = False
        self._discharging: bool = False

        # Block voltages for deltaV chart
        self._block_voltages: Optional[tuple] = None
        self._unsub_fns: list = []

    def on_enter(self) -> None:
        """Subscribe to state when entering the screen."""
        self._last_activity = time.time()
        if self._store:
            self._unsub_fns.append(self._store.subscribe(StateSlice.ALL, self._on_state_update))
            # Initial state read
            self._on_state_update(self._store.state)

    def on_exit(self) -> None:
        """Unsubscribe from state when leaving."""
        for unsub in self._unsub_fns:
            unsub()
        self._unsub_fns.clear()

    def _on_state_update(self, state) -> None:
        """Update local values from state."""
        v = state.vehicle
        e = state.energy

        # Temperatures
        self._mg1_inv_temp = v.mg1_inverter_temp
        self._mg2_inv_temp = v.mg2_inverter_temp
        self._mg1_mot_temp = v.mg1_motor_temp
        self._mg2_mot_temp = v.mg2_motor_temp
        self._conv_temp = v.converter_temp
        self._ice_temp = v.ice_coolant_temp
        self._batt_temp = e.battery_temp
        self._batt_min_temp = e.battery_min_cell_temp
        self._batt_max_temp = e.battery_max_cell_temp

        # RPMs
        self._mg1_rpm = v.mg1_rpm
        self._mg2_rpm = v.mg2_rpm
        self._ice_rpm = v.rpm
        self._ice_solicited_rpm = v.solicited_rpm

        # Battery / Energy
        self._batt_soc = e.battery_soc
        self._batt_delta_soc = e.battery_delta_soc
        self._batt_voltage = e.hv_battery_voltage
        self._batt_current = e.hv_battery_current
        self._batt_power = e.battery_power_kw
        self._charging = e.charging
        self._discharging = e.discharging
        self._block_voltages = e.block_voltages

        # EV mode from dynamics
        self._ev_mode = getattr(state.dynamics, 'ev_mode', False)

    def _get_timeout(self) -> float:
        if self.app and hasattr(self.app, 'config'):
            return self.app.config.timeout_screen_exit
        return 30.0

    def update(self, dt: float) -> None:
        """Check inactivity timeout."""
        super().update(dt)
        if time.time() - self._last_activity > self._get_timeout():
            self._exit_screen()

    def _exit_screen(self) -> None:
        if self.app:
            self.app.pop_screen()

    def handle_input(self, event) -> bool:
        """Handle input - any press exits."""
        self._last_activity = time.time()
        if event in (IE.PRESS_LIGHT, IE.PRESS_STRONG, IE.BACK):
            self._exit_screen()
            return True
        return False

    # ─── Rendering ────────────────────────────────────────────────────────

    def render(self, surface: pygame.Surface) -> None:
        """Render the EV detail screen."""
        surface.fill(COLORS["bg_dark"])
        self._render_header(surface)
        self._render_left_column(surface)
        self._render_right_column(surface)
        self._render_divider(surface)
        self._render_delta_v_chart(surface)

    def _render_header(self, surface: pygame.Surface) -> None:
        """Render title bar."""
        pygame.draw.rect(surface, COLORS["bg_panel"], (0, 0, self.width, self.HEADER_HEIGHT))

        font = get_title_font(14)
        title = "EV / BATTERY"
        s = font.render(title, True, COLORS["cyan"])
        surface.blit(s, ((self.width - s.get_width()) // 2, (self.HEADER_HEIGHT - s.get_height()) // 2))

        # EV mode indicator
        if self._ev_mode:
            ev_surf = font.render("EV", True, COLORS["active"])
            surface.blit(ev_surf, (self.width - ev_surf.get_width() - 8, (self.HEADER_HEIGHT - ev_surf.get_height()) // 2))

        pygame.draw.line(surface, COLORS["border_focus"], (0, self.HEADER_HEIGHT - 1), (self.width, self.HEADER_HEIGHT - 1))

    def _render_divider(self, surface: pygame.Surface) -> None:
        """Render vertical divider between columns."""
        x = self.COL_DIVIDER_X
        pygame.draw.line(surface, COLORS["border_dim"], (x, self.HEADER_HEIGHT + 2), (x, self.height - 48), 1)

    def _render_left_column(self, surface: pygame.Surface) -> None:
        """Render temperature column."""
        x0 = 8
        y = self.HEADER_HEIGHT + self.SECTION_PAD

        font_section = get_title_font(11)
        font_label = get_mono_font(11)
        font_value = get_mono_font(12)

        # Section title
        s = font_section.render("TEMPERATURES", True, COLORS["cyan"])
        surface.blit(s, (x0, y))
        y += 16

        label_x = x0 + 4
        value_x = 130  # Right-align column for values

        temps = [
            ("MG1 INV", self._mg1_inv_temp),
            ("MG2 INV", self._mg2_inv_temp),
            ("MG1 MOT", self._mg1_mot_temp),
            ("MG2 MOT", self._mg2_mot_temp),
            ("CONVERT", self._conv_temp),
            ("ICE CLT", self._ice_temp),
            ("HV BATT", self._batt_temp),
        ]

        for label_text, temp_val in temps:
            # Label
            label_surf = font_label.render(label_text, True, COLORS["text_secondary"])
            surface.blit(label_surf, (label_x, y + 2))

            # Value with color coding
            if temp_val is not None:
                val_str = f"{int(temp_val)}°C"
                color = self._temp_color(temp_val)
            else:
                val_str = "--°C"
                color = COLORS["text_tertiary"]

            val_surf = font_value.render(val_str, True, color)
            surface.blit(val_surf, (value_x, y + 1))

            y += self.LINE_HEIGHT

    def _render_right_column(self, surface: pygame.Surface) -> None:
        """Render motors and battery column."""
        x0 = self.COL_DIVIDER_X + 10
        y = self.HEADER_HEIGHT + self.SECTION_PAD

        font_section = get_title_font(11)
        font_label = get_mono_font(11)
        font_value = get_mono_font(12)
        font_small = get_mono_font(10)

        value_x = x0 + 95

        # ── MOTORS section ──
        s = font_section.render("MOTORS", True, COLORS["cyan"])
        surface.blit(s, (x0, y))
        y += 16

        motors = [
            ("MG1 GEN", self._mg1_rpm),
            ("MG2 MOT", self._mg2_rpm),
        ]

        for label_text, rpm_val in motors:
            label_surf = font_label.render(label_text, True, COLORS["text_secondary"])
            surface.blit(label_surf, (x0 + 4, y + 2))

            if rpm_val is not None:
                val_str = f"{int(rpm_val)} rpm"
                color = COLORS["text_value"] if abs(rpm_val) > 0 else COLORS["text_tertiary"]
            else:
                val_str = "-- rpm"
                color = COLORS["text_tertiary"]

            val_surf = font_value.render(val_str, True, color)
            surface.blit(val_surf, (value_x, y + 1))
            y += self.LINE_HEIGHT

        # ICE RPM with solicited/unsolicited comparison
        label_surf = font_label.render("ICE", True, COLORS["text_secondary"])
        surface.blit(label_surf, (x0 + 4, y + 2))
        sol = self._ice_solicited_rpm
        unsol = self._ice_rpm
        if sol is not None and sol > 0:
            val_str = f"{int(sol)} rpm"
            color = COLORS["text_value"]
        elif unsol is not None and unsol > 0:
            val_str = f"{int(unsol)} rpm"
            color = COLORS["text_tertiary"]  # unsolicited only - less accurate
        else:
            val_str = "-- rpm"
            color = COLORS["text_tertiary"]
        val_surf = font_value.render(val_str, True, color)
        surface.blit(val_surf, (value_x, y + 1))
        y += self.LINE_HEIGHT

        # Show comparison when both available
        if sol is not None and unsol is not None and (sol > 0 or unsol > 0):
            cmp_str = f"sol:{int(sol)} bus:{int(unsol)}"
            cmp_surf = font_small.render(cmp_str, True, COLORS["text_tertiary"])
            surface.blit(cmp_surf, (x0 + 4, y + 2))
        y += self.LINE_HEIGHT - 6

        # ── BATTERY section ──
        s = font_section.render("HV BATTERY", True, COLORS["cyan"])
        surface.blit(s, (x0, y))
        y += 16

        # SOC + Delta SOC on one line
        soc_pct = int(self._batt_soc * 100) if self._batt_soc > 0 else None
        soc_str = f"{soc_pct}%" if soc_pct is not None else "--%"
        soc_color = COLORS["text_value"]
        if soc_pct is not None and soc_pct < 40:
            soc_color = COLORS["active"]  # amber warning

        label_surf = font_label.render("SOC", True, COLORS["text_secondary"])
        surface.blit(label_surf, (x0 + 4, y + 2))
        val_surf = font_value.render(soc_str, True, soc_color)
        surface.blit(val_surf, (x0 + 50, y + 1))

        # Delta SOC on same line
        if self._batt_delta_soc is not None:
            dsoc_str = f"\u0394 {self._batt_delta_soc:.2f}"
        else:
            dsoc_str = "\u0394 --"
        dsoc_surf = font_small.render(dsoc_str, True, COLORS["text_tertiary"])
        surface.blit(dsoc_surf, (x0 + 110, y + 3))
        y += self.LINE_HEIGHT

        # Voltage / Current / Power
        v_str = f"{self._batt_voltage:.0f}V" if self._batt_voltage is not None else "---V"
        a_str = f"{self._batt_current:.0f}A" if self._batt_current is not None else "--A"
        p_str = f"{self._batt_power:+.1f}kW" if self._batt_power is not None else "--kW"

        line_str = f"{v_str}  {a_str}  {p_str}"
        line_surf = font_value.render(line_str, True, COLORS["text_value"])
        surface.blit(line_surf, (x0 + 4, y + 1))
        y += self.LINE_HEIGHT

        # Battery temp with min/max
        if self._batt_temp is not None:
            temp_str = f"{int(self._batt_temp)}\u00b0C"
        else:
            temp_str = "--\u00b0C"

        min_max_parts = []
        if self._batt_min_temp is not None:
            min_max_parts.append(f"{int(self._batt_min_temp)}")
        else:
            min_max_parts.append("--")
        if self._batt_max_temp is not None:
            min_max_parts.append(f"{int(self._batt_max_temp)}")
        else:
            min_max_parts.append("--")

        range_str = f"({min_max_parts[0]}/{min_max_parts[1]})"

        label_surf = font_label.render("TEMP", True, COLORS["text_secondary"])
        surface.blit(label_surf, (x0 + 4, y + 2))
        val_surf = font_value.render(temp_str, True, self._temp_color(self._batt_temp) if self._batt_temp is not None else COLORS["text_tertiary"])
        surface.blit(val_surf, (x0 + 55, y + 1))
        range_surf = font_small.render(range_str, True, COLORS["text_tertiary"])
        surface.blit(range_surf, (x0 + 110, y + 3))
        y += self.LINE_HEIGHT

        # Charge state indicator
        if self._charging:
            state_str = "CHARGING"
            state_color = COLORS.get("green_bright", COLORS["cyan"])
        elif self._discharging:
            state_str = "DISCHARGING"
            state_color = COLORS["active"]
        else:
            state_str = "IDLE"
            state_color = COLORS["text_tertiary"]

        state_surf = font_small.render(state_str, True, state_color)
        surface.blit(state_surf, (x0 + 4, y + 2))

    def _render_footer(self, surface: pygame.Surface) -> None:
        """Render footer hint."""
        font = get_mono_font(10)
        hint = "[PRESS] BACK"
        s = font.render(hint, True, COLORS["text_secondary"])
        surface.blit(s, ((self.width - s.get_width()) // 2, self.height - s.get_height() - 3))

    def _render_delta_v_chart(self, surface: pygame.Surface) -> None:
        """Render deltaV bar chart showing per-block voltage deviation from mean.
        
        14 blocks displayed as vertical bars. Each bar shows how far
        the block voltage deviates from the pack average.
        Green = close to mean, yellow = moderate, red = large deviation.
        """
        CHART_HEIGHT = 42
        chart_y = self.height - CHART_HEIGHT
        chart_x = 8
        chart_w = self.width - 16
        
        # Horizontal separator
        pygame.draw.line(
            surface, COLORS["border_dim"],
            (0, chart_y - 2), (self.width, chart_y - 2), 1
        )
        
        font_tiny = get_mono_font(9)
        
        if self._block_voltages is None or len(self._block_voltages) != 14:
            # No data — show placeholder
            label = font_tiny.render("\u0394V  NO DATA", True, COLORS["text_tertiary"])
            surface.blit(label, (chart_x, chart_y + (CHART_HEIGHT - label.get_height()) // 2))
            return
        
        voltages = self._block_voltages
        avg_v = sum(voltages) / len(voltages)
        deviations = [v - avg_v for v in voltages]
        max_dev = max(abs(d) for d in deviations)
        
        # Clamp scale: at least 0.05V range so bars are visible at near-zero delta
        scale = max(max_dev, 0.05)
        
        # Label: "ΔV" and numeric delta
        delta_v = max(voltages) - min(voltages)
        label_str = f"\u0394V {delta_v:.2f}V"
        label_surf = font_tiny.render(label_str, True, COLORS["cyan"])
        surface.blit(label_surf, (chart_x, chart_y))
        
        # Bar area
        label_w = label_surf.get_width() + 6
        bar_area_x = chart_x + label_w
        bar_area_w = chart_w - label_w
        bar_area_y = chart_y + 2
        bar_area_h = CHART_HEIGHT - 4
        
        num_blocks = 14
        gap = 2
        bar_w = (bar_area_w - (num_blocks - 1) * gap) // num_blocks
        mid_y = bar_area_y + bar_area_h // 2
        max_bar_h = (bar_area_h // 2) - 1
        
        # Draw center line (zero deviation)
        pygame.draw.line(
            surface, COLORS["text_tertiary"],
            (bar_area_x, mid_y), (bar_area_x + bar_area_w, mid_y), 1
        )
        
        for i, dev in enumerate(deviations):
            x = bar_area_x + i * (bar_w + gap)
            
            # Bar height proportional to deviation
            bar_h = int(abs(dev) / scale * max_bar_h)
            bar_h = max(bar_h, 1)  # At least 1px
            
            # Color based on deviation magnitude
            abs_dev = abs(dev)
            if abs_dev < 0.02:
                color = COLORS.get("green_bright", (0, 230, 118))
            elif abs_dev < 0.05:
                color = COLORS.get("warm_bright", COLORS["active"])
            else:
                color = COLORS.get("alert_bright", (255, 60, 60))
            
            # Draw bar above or below center line
            if dev >= 0:
                rect = pygame.Rect(x, mid_y - bar_h, bar_w, bar_h)
            else:
                rect = pygame.Rect(x, mid_y, bar_w, bar_h)
            
            pygame.draw.rect(surface, color, rect)
            
            # Block number below chart (every other to save space)
            if i % 2 == 0:
                num_surf = font_tiny.render(str(i + 1), True, COLORS["text_tertiary"])
                surface.blit(num_surf, (x + (bar_w - num_surf.get_width()) // 2, bar_area_y + bar_area_h - num_surf.get_height()))

    @staticmethod
    def _temp_color(temp: Optional[float]) -> Tuple[int, int, int]:
        """Get color for temperature value (green→yellow→red)."""
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
