"""
Solicited CAN PID monitor screen.

Live view of all solicited ECU values for debugging and verification.
Grouped by ECU source:
  - Engine ECU (0x7E0): intake/ambient temp, MAF, lambda, O2, baro, aux voltage
  - Cruise Control (PID 21D3): active, set/mem speed, switches
  - Hybrid ECU (0x7E2): A/C power, crank, relays, request bitmasks
  - HV Battery ECU (0x7E3): fan speed, block resistances
  - 21C3 drivetrain: MG torques, regen, master cylinder

Access: ENGINE menu → SOLICITED MONITOR

Layout (480x240):
┌──────────────────────────────────────────────────────────┐
│  SOLICITED CAN MONITOR                                   │
├────────────────────────┬─────────────────────────────────┤
│  ENGINE ECU 7E0        │  CRUISE 21D3                    │
│  INTAKE AIR    12°C    │  ACTIVE        ENGAGED          │
│  AMBIENT        8°C    │  SET SPD       80 km/h          │
│  MAF FLOW    2.31 g/s  │  MEM SPD       80 km/h          │
│  ...                   │  SWITCHES  SW:1 RDY:1 ...       │
│  HV BATT ECU 7E3       │  HYBRID ECU 7E2                 │
│  FAN  ██████  3        │  A/C POWER    0.42 kW           │
│  RESIST (mΩ)           │  21C3 DRIVETRAIN                │
│  01:22 02:21 ...       │  MG2 TORQ      +42 Nm           │
├────────────────────────┴─────────────────────────────────┤
│  [HOLD] BACK                                             │
└──────────────────────────────────────────────────────────┘
"""

import pygame
import time
from typing import Tuple, Optional

from .base import Screen
from ..colors import COLORS
from ..fonts import get_title_font, get_mono_font, get_font
from ...input.manager import InputEvent as IE
from ...state.store import Store


class SolicitedMonitorScreen(Screen):
    """
    Live solicited CAN PID value monitor.

    Shows all decoded solicited values grouped by ECU.
    Auto-dismisses after inactivity timeout.
    """

    HEADER_H = 22
    FOOTER_H = 14

    def __init__(
        self,
        size: Tuple[int, int],
        app=None,
        store: Optional[Store] = None,
    ):
        super().__init__(size, app)
        self._store = store
        self._last_activity = time.time()

    def on_enter(self) -> None:
        self._last_activity = time.time()

    def _get_timeout(self) -> float:
        if self.app and hasattr(self.app, 'config'):
            return self.app.config.timeout_screen_exit
        return 30.0

    def update(self, dt: float) -> None:
        super().update(dt)
        if time.time() - self._last_activity > self._get_timeout():
            self._exit()

    def _exit(self) -> None:
        if self.app:
            self.app.pop_screen()

    def handle_input(self, event) -> bool:
        self._last_activity = time.time()

        if event in (IE.PRESS_STRONG, IE.BACK, IE.PRESS_LIGHT):
            self._exit()
            return True

        return False

    # ─── Rendering ────────────────────────────────────────────────────

    def render(self, surface: pygame.Surface) -> None:
        surface.fill(COLORS["bg_dark"])
        self._render_header(surface)
        self._render_content(surface)
        self._render_footer(surface)

    def _render_header(self, surface: pygame.Surface) -> None:
        pygame.draw.rect(
            surface, COLORS["bg_panel"],
            (0, 0, self.width, self.HEADER_H),
        )
        font = get_title_font(12)
        title = "SOLICITED CAN MONITOR"
        s = font.render(title, True, COLORS["cyan"])
        surface.blit(s, (8, (self.HEADER_H - s.get_height()) // 2))
        pygame.draw.line(
            surface, COLORS["border_focus"],
            (0, self.HEADER_H - 1), (self.width, self.HEADER_H - 1),
        )

    def _render_footer(self, surface: pygame.Surface) -> None:
        font = get_mono_font(8)
        hint = "[PRESS/HOLD] BACK"
        s = font.render(hint, True, COLORS["text_tertiary"])
        surface.blit(s, ((self.width - s.get_width()) // 2,
                         self.height - s.get_height() - 2))

    def _render_content(self, surface: pygame.Surface) -> None:
        """Render all solicited PID values grouped by ECU."""
        if not self._store:
            return

        v = self._store.state.vehicle
        e = self._store.state.energy
        font_label = get_font(8)
        font_value = get_font(10, "mono")
        font_title = get_font(10, "title")
        font_small = get_font(7, "mono")

        pad = 6
        col_width = (self.width - pad * 3) // 2
        left_x = pad
        right_x = pad * 2 + col_width
        row_h = 12
        content_y = self.HEADER_H + 4

        # ─── LEFT COLUMN: Engine ECU (0x7E0) ───

        y = content_y
        title_surf = font_title.render("ENGINE ECU 7E0", True, COLORS["cyan_bright"])
        surface.blit(title_surf, (left_x, y))
        y += row_h + 2

        engine_rows = [
            ("INTAKE AIR",  v.intake_air_temp,     lambda x: f"{x:.0f}\u00b0C",   None),
            ("AMBIENT",     v.ambient_air_temp,     lambda x: f"{x:.0f}\u00b0C",   None),
            ("MAF FLOW",    v.maf_air_flow,         lambda x: f"{x:.2f} g/s",      None),
            ("LAMBDA",      v.lambda_ratio,         lambda x: f"{x:.4f}",
             lambda x: COLORS["green_bright"] if 0.95 <= x <= 1.05 else COLORS["yellow"]),
            ("O2 SENSOR",   v.o2_sensor_voltage,    lambda x: f"{x:.3f} V",        None),
            ("BARO",        v.barometric_pressure,  lambda x: f"{x} kPa",          None),
            ("AUX BATT",    v.aux_battery_voltage,  lambda x: f"{x:.2f} V",
             lambda x: COLORS["green_bright"] if x >= 12.0 else COLORS["red_bright"]),
            ("ODO DTC",     v.odometer_dtc_clear,   lambda x: f"{x} km",           None),
        ]

        for label_text, val, fmt_fn, color_fn in engine_rows:
            lbl = font_label.render(label_text, True, COLORS["text_secondary"])
            surface.blit(lbl, (left_x, y))
            if val is not None:
                val_str = fmt_fn(val)
                color = color_fn(val) if color_fn else COLORS["green_bright"]
            else:
                val_str = "--"
                color = COLORS["text_dim"]
            val_surf = font_value.render(val_str, True, color)
            surface.blit(val_surf, (left_x + col_width - val_surf.get_width(), y))
            y += row_h

        # ─── LEFT COLUMN continued: HV Battery ECU (0x7E3) ───

        y += 4
        title_surf = font_title.render("HV BATT ECU 7E3", True, COLORS["cyan_bright"])
        surface.blit(title_surf, (left_x, y))
        y += row_h + 2

        # Fan speed with bar indicator
        lbl = font_label.render("FAN", True, COLORS["text_secondary"])
        surface.blit(lbl, (left_x, y))
        if e.battery_fan_speed is not None:
            fan = e.battery_fan_speed
            if fan == 0:
                fc = COLORS["text_dim"]
            elif fan <= 2:
                fc = COLORS["green_bright"]
            elif fan <= 4:
                fc = COLORS["yellow"]
            else:
                fc = COLORS["red_bright"]
            bar_x = left_x + 40
            for i in range(6):
                c = fc if i < fan else COLORS["text_dim"]
                pygame.draw.rect(surface, c, pygame.Rect(bar_x + i * 9, y + 2, 6, 7))
            val_surf = font_value.render(f"{fan}", True, fc)
        else:
            val_surf = font_value.render("--", True, COLORS["text_dim"])
        surface.blit(val_surf, (left_x + col_width - val_surf.get_width(), y))
        y += row_h

        # Block resistances (compact: 2 rows of 7)
        if e.block_resistances is not None:
            lbl = font_label.render("RESIST (m\u03A9)", True, COLORS["text_secondary"])
            surface.blit(lbl, (left_x, y))
            y += row_h
            res = e.block_resistances
            for row_idx in range(2):
                rx = left_x
                for col_idx in range(7):
                    idx = row_idx * 7 + col_idx
                    if idx < len(res):
                        r_val = res[idx]
                        r_mohm = r_val * 1000
                        r_str = f"{idx+1:02d}:{r_mohm:.0f}"
                        if r_mohm > 30:
                            rc = COLORS["red_bright"]
                        elif r_mohm > 25:
                            rc = COLORS["yellow"]
                        else:
                            rc = COLORS["green_bright"]
                        cell_surf = font_small.render(r_str, True, rc)
                        surface.blit(cell_surf, (rx, y))
                    rx += col_width // 7
                y += row_h

        # ─── RIGHT COLUMN: Cruise Control (PID 21D3) + Hybrid ECU ───

        ry = content_y
        title_surf = font_title.render("CRUISE 21D3", True, COLORS["cyan_bright"])
        surface.blit(title_surf, (right_x, ry))
        ry += row_h + 2

        # Cruise active status
        lbl = font_label.render("ACTIVE", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        if v.cruise_active:
            val_surf = font_value.render("ENGAGED", True, COLORS["green_bright"])
        else:
            val_surf = font_value.render("OFF", True, COLORS["text_dim"])
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
        ry += row_h

        # Cruise set speed
        lbl = font_label.render("SET SPD", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        if v.cruise_set_speed is not None and v.cruise_set_speed > 0:
            val_surf = font_value.render(f"{v.cruise_set_speed} km/h", True, COLORS["green_bright"])
        else:
            val_surf = font_value.render("--", True, COLORS["text_dim"])
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
        ry += row_h

        # Cruise memory speed
        lbl = font_label.render("MEM SPD", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        if v.cruise_memory_speed is not None and v.cruise_memory_speed > 0:
            val_surf = font_value.render(f"{v.cruise_memory_speed} km/h", True, COLORS["cyan_bright"])
        else:
            val_surf = font_value.render("--", True, COLORS["text_dim"])
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
        ry += row_h

        # Cruise switches row: MAIN | RDY | RES | SET | CAN
        lbl = font_label.render("SWITCHES", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        sw_parts = []
        sw_items = [
            ("SW", v.cruise_main_switch),
            ("RDY", v.cruise_main_ready),
            ("IND", v.cruise_indicator),
            ("R/A", v.cruise_res_acc_switch),
            ("S/C", v.cruise_set_coast_switch),
            ("CAN", v.cruise_cancel_switch),
        ]
        for tag, val in sw_items:
            if val is not None:
                sw_parts.append(f"{tag}:{'1' if val else '0'}")
            else:
                sw_parts.append(f"{tag}:-")
        sw_str = " ".join(sw_parts)
        sw_color = COLORS["green_bright"] if v.cruise_active else COLORS["text_dim"]
        val_surf = font_small.render(sw_str, True, sw_color)
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
        ry += row_h

        # ─── Hybrid ECU extras (21C4) ───
        ry += 4
        title_surf = font_title.render("HYBRID ECU 7E2", True, COLORS["cyan_bright"])
        surface.blit(title_surf, (right_x, ry))
        ry += row_h + 2
        lbl = font_label.render("A/C POWER", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        if v.aircon_power_kw is not None:
            ac_str = f"{v.aircon_power_kw:.2f} kW"
            ac_color = COLORS["green_bright"] if v.aircon_power_kw < 1.0 else COLORS["yellow"]
        else:
            ac_str = "--"
            ac_color = COLORS["text_dim"]
        val_surf = font_value.render(ac_str, True, ac_color)
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
        ry += row_h

        # Crank position
        lbl = font_label.render("CRANK POS", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        if v.crank_position is not None:
            val_surf = font_value.render(f"{v.crank_position:.1f}", True, COLORS["green_bright"])
        else:
            val_surf = font_value.render("--", True, COLORS["text_dim"])
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
        ry += row_h

        # System Relays
        lbl = font_label.render("RELAYS", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        relay_parts = []
        for i, relay in enumerate([v.system_relay_1, v.system_relay_2, v.system_relay_3], 1):
            if relay is not None:
                state = "ON" if relay else "off"
                relay_parts.append(f"R{i}:{state}")
            else:
                relay_parts.append(f"R{i}:--")
        relay_str = " ".join(relay_parts)
        relay_color = COLORS["green_bright"] if any(
            r for r in [v.system_relay_1, v.system_relay_2, v.system_relay_3] if r
        ) else COLORS["text_dim"]
        val_surf = font_small.render(relay_str, True, relay_color)
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
        ry += row_h

        # Engine Requests (bitmask bytes A and B)
        lbl = font_label.render("REQ BYTE A", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        if v.engine_requests_a is not None:
            req_a = v.engine_requests_a
            val_surf = font_value.render(
                f"0x{req_a:04X}  {req_a:016b}", True, COLORS["yellow"]
            )
        else:
            val_surf = font_value.render("--", True, COLORS["text_dim"])
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
        ry += row_h

        lbl = font_label.render("REQ BYTE B", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        if v.engine_requests_b is not None:
            req_b = v.engine_requests_b
            val_surf = font_value.render(
                f"0x{req_b:04X}  {req_b:016b}", True, COLORS["yellow"]
            )
        else:
            val_surf = font_value.render("--", True, COLORS["text_dim"])
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
        ry += row_h

        # ─── 21C3 Drivetrain section ───
        ry += 4
        title_surf = font_title.render("21C3 DRIVETRAIN", True, COLORS["cyan_bright"])
        surface.blit(title_surf, (right_x, ry))
        ry += row_h + 2

        drivetrain_rows = [
            ("MG2 TORQ",   v.mg2_torque,              lambda x: f"{x:+.0f} Nm"),
            ("MG1 TORQ",   v.mg1_torque,              lambda x: f"{x:+.0f} Nm"),
            ("REGEN ACT",  v.regen_torque_actual,      lambda x: f"{x:.0f} Nm"),
            ("REGEN REQ",  v.regen_torque_request,     lambda x: f"{x:.0f} Nm"),
            ("MSTR CYL",   v.master_cylinder_torque,   lambda x: f"{x:+.0f} Nm"),
        ]

        for label_text, val, fmt_fn in drivetrain_rows:
            lbl = font_label.render(label_text, True, COLORS["text_secondary"])
            surface.blit(lbl, (right_x, ry))
            if val is not None:
                val_surf = font_value.render(fmt_fn(val), True, COLORS["green_bright"])
            else:
                val_surf = font_value.render("--", True, COLORS["text_dim"])
            surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
            ry += row_h
