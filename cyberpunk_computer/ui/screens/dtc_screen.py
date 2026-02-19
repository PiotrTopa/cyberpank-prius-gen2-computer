"""
Diagnostics / DTC (Diagnostic Trouble Code) screen.

Shows OBD-II stored and pending DTCs read from vehicle ECUs.
Allows triggering a DTC scan and displays results.

Layout (480x240):
┌───────────────────────────────────────────────────────┐
│              DIAGNOSTICS / DTC                        │
├───────────────────────────────────────────────────────┤
│  MIL: OFF / ON        SCAN: [PRESS TO SCAN]          │
│  ─────────────────────────────────────────────────    │
│  STORED DTCs (N)                                      │
│   P0171  ENGINE    Fuel System Lean                   │
│   P0420  ENGINE    Catalyst Efficiency Below          │
│   P3000  HV_BATT  HV Battery Pack                    │
│  ─────────────────────────────────────────────────    │
│  PENDING DTCs (N)                                     │
│   P0301  ENGINE    Cylinder 1 Misfire                 │
├───────────────────────────────────────────────────────┤
│            [PRESS] SCAN   [HOLD] BACK                 │
└───────────────────────────────────────────────────────┘
"""

import pygame
import time
from typing import Tuple, Optional

from .base import Screen
from ..colors import COLORS
from ..fonts import get_title_font, get_mono_font
from ...input.manager import InputEvent as IE
from ...state.store import Store, StateSlice


# Common Prius Gen2 DTC descriptions (subset)
DTC_DESCRIPTIONS = {
    # Powertrain (P)
    "P0010": "Camshaft Position Actuator Circuit",
    "P0016": "Crank/Cam Position Correlation",
    "P0100": "MAF Circuit Malfunction",
    "P0101": "MAF Circuit Range/Performance",
    "P0110": "Intake Air Temp Circuit",
    "P0115": "Coolant Temp Circuit",
    "P0120": "Throttle Position Sensor",
    "P0128": "Thermostat Coolant Temp Below",
    "P0171": "Fuel System Lean (Bank 1)",
    "P0172": "Fuel System Rich (Bank 1)",
    "P0300": "Random/Multiple Cylinder Misfire",
    "P0301": "Cylinder 1 Misfire",
    "P0302": "Cylinder 2 Misfire",
    "P0303": "Cylinder 3 Misfire",
    "P0304": "Cylinder 4 Misfire",
    "P0325": "Knock Sensor Circuit",
    "P0335": "Crankshaft Position Sensor",
    "P0340": "Camshaft Position Sensor",
    "P0351": "Ignition Coil A Primary",
    "P0401": "EGR Insufficient Flow",
    "P0420": "Catalyst Efficiency Below Threshold",
    "P0430": "Catalyst Efficiency Below (Bank 2)",
    "P0441": "EVAP System Incorrect Purge Flow",
    "P0442": "EVAP System Small Leak",
    "P0446": "EVAP System Vent Control",
    "P0455": "EVAP System Large Leak",
    "P0500": "Vehicle Speed Sensor",
    "P0505": "Idle Control System",
    "P0606": "ECM/PCM Processor Fault",
    "P0A80": "Replace Hybrid Battery Pack",
    "P0A7F": "Hybrid Battery Pack Deterioration",
    # Prius hybrid specific
    "P3000": "HV Battery Pack Malfunction",
    "P3004": "Battery Block 1 Weak",
    "P3005": "Battery Block 2 Weak",
    "P3006": "Battery Block 3 Weak",
    "P3007": "Battery Block 4 Weak",
    "P3008": "Battery Block 5 Weak",
    "P3009": "Battery Block 6 Weak",
    "P3010": "Battery Block 7 Weak",
    "P3011": "Battery Block 8 Weak",
    "P3012": "Battery Block 9 Weak",
    "P3013": "Battery Block 10 Weak",
    "P3014": "Battery Block 11 Weak",
    "P3015": "Battery Block 12 Weak",
    "P3016": "Battery Block 13 Weak",
    "P3017": "Battery Block 14 Weak",
    "P3030": "HV Battery Blower Malfunction",
    "P3100": "HV Battery ECU Malfunction",
    "P3101": "HV Battery Voltage Sensor",
    "P3102": "HV Battery Current Sensor",
    "P0A0F": "Engine Failed To Start",
    "P3190": "Engine Does Not Start",
    "P3191": "Engine Does Not Start (No Fuel)",
    # Chassis (C) and Body (B) codes
    "C1201": "Engine Control System Malfunction",
    "C1241": "Low Battery Positive Voltage",
    "C1249": "Stop Light Switch Open",
    "C1256": "Speed Sensor Malfunction",
}


def get_dtc_description(code: str) -> str:
    """Get human-readable description for a DTC code."""
    desc = DTC_DESCRIPTIONS.get(code)
    if desc:
        return desc
    # Generic category hint
    prefix = code[0] if code else "?"
    categories = {"P": "Powertrain", "C": "Chassis", "B": "Body", "U": "Network"}
    category = categories.get(prefix, "Unknown")
    return f"{category} Code"


class DTCScreen(Screen):
    """
    Diagnostics / DTC display screen.
    
    Shows stored and pending DTCs with descriptions.
    Press triggers a new DTC scan. Hold/Back exits.
    """

    HEADER_HEIGHT = 24
    LINE_HEIGHT = 18
    MAX_VISIBLE_DTCS = 9  # Max DTCs visible at once (scrollable)

    def __init__(
        self,
        size: Tuple[int, int],
        app=None,
        store: Optional[Store] = None,
    ):
        """Initialize DTC screen."""
        super().__init__(size, app)
        self._store = store
        self._last_activity = time.time()

        # DTC state
        self._stored_dtcs: tuple = ()
        self._pending_dtcs: tuple = ()
        self._mil_on: bool = False
        self._scan_in_progress: bool = False
        self._last_scan_time: Optional[float] = None
        self._scroll_offset: int = 0
        self._unsub_fns: list = []

        # Clear confirmation state
        self._clear_confirm: bool = False
        self._clear_confirm_time: float = 0.0
        self._CLEAR_CONFIRM_TIMEOUT: float = 3.0  # seconds to confirm

    def on_enter(self) -> None:
        """Subscribe to state and trigger initial scan."""
        self._last_activity = time.time()
        if self._store:
            self._unsub_fns.append(self._store.subscribe(StateSlice.DIAGNOSTICS, self._on_state_update))
            self._unsub_fns.append(self._store.subscribe(StateSlice.ALL, self._on_state_update))
            # Read current state
            self._on_state_update(self._store.state)
        
        # Auto-trigger scan on enter
        self._trigger_scan()

    def on_exit(self) -> None:
        """Unsubscribe from state."""
        for unsub in self._unsub_fns:
            unsub()
        self._unsub_fns.clear()

    def _on_state_update(self, state) -> None:
        """Update local values from diagnostics state."""
        d = state.diagnostics
        self._stored_dtcs = d.stored_dtcs
        self._pending_dtcs = d.pending_dtcs
        self._mil_on = d.mil_on
        self._scan_in_progress = d.scan_in_progress
        self._last_scan_time = d.last_scan_time

    def _get_timeout(self) -> float:
        if self.app and hasattr(self.app, 'config'):
            return self.app.config.timeout_screen_exit
        return 60.0  # Longer timeout for diagnostics

    def update(self, dt: float) -> None:
        """Check inactivity timeout."""
        super().update(dt)
        if time.time() - self._last_activity > self._get_timeout():
            self._exit_screen()

    def _exit_screen(self) -> None:
        if self.app:
            self.app.pop_screen()

    def _trigger_scan(self) -> None:
        """Trigger a DTC scan via the virtual twin."""
        if self._scan_in_progress:
            return
        if self.app and hasattr(self.app, '_virtual_twin') and self.app._virtual_twin:
            vt = self.app._virtual_twin
            vt.ingress.start_dtc_scan(output_port=vt.output_port, mode=0x03)

    def _trigger_clear(self) -> None:
        """Send Mode 04 (Clear DTCs) to all ECUs via the virtual twin."""
        if self._scan_in_progress:
            return
        if self.app and hasattr(self.app, '_virtual_twin') and self.app._virtual_twin:
            vt = self.app._virtual_twin
            vt.ingress.clear_dtcs(output_port=vt.output_port)
            self._clear_confirm = False

    def handle_input(self, event) -> bool:
        """Handle input."""
        self._last_activity = time.time()

        # If in clear confirmation mode, handle confirm/cancel
        if self._clear_confirm:
            if time.time() - self._clear_confirm_time > self._CLEAR_CONFIRM_TIMEOUT:
                self._clear_confirm = False  # Timed out
            elif event == IE.PRESS_STRONG:
                # Confirm clear
                self._trigger_clear()
                return True
            elif event in (IE.BACK, IE.PRESS_LIGHT):
                # Cancel confirmation
                self._clear_confirm = False
                return True
            return True  # Swallow all input during confirmation

        if event == IE.PRESS_LIGHT:
            # Light press = scan or scroll
            if self._total_dtc_count() > self.MAX_VISIBLE_DTCS:
                self._scroll_offset += 1
                max_scroll = max(0, self._total_dtc_count() - self.MAX_VISIBLE_DTCS)
                self._scroll_offset = min(self._scroll_offset, max_scroll)
            else:
                self._trigger_scan()
            return True
        elif event == IE.PRESS_STRONG:
            # Strong press = trigger scan (even if scrollable)
            self._trigger_scan()
            self._scroll_offset = 0
            return True
        elif event == IE.ROTATE_RIGHT:
            # Rotate right = initiate clear DTCs confirmation
            if self._total_dtc_count() > 0 and not self._scan_in_progress:
                self._clear_confirm = True
                self._clear_confirm_time = time.time()
            return True
        elif event == IE.BACK:
            self._exit_screen()
            return True
        return False

    def _total_dtc_count(self) -> int:
        return len(self._stored_dtcs) + len(self._pending_dtcs)

    # ─── Rendering ────────────────────────────────────────────────────────

    def render(self, surface: pygame.Surface) -> None:
        """Render the DTC screen."""
        surface.fill(COLORS["bg_dark"])
        self._render_header(surface)
        self._render_status_bar(surface)
        self._render_dtc_list(surface)
        self._render_footer(surface)

    def _render_header(self, surface: pygame.Surface) -> None:
        """Render title bar."""
        pygame.draw.rect(surface, COLORS["bg_panel"], (0, 0, self.width, self.HEADER_HEIGHT))

        font = get_title_font(14)
        title = "DIAGNOSTICS / DTC"
        s = font.render(title, True, COLORS["cyan"])
        surface.blit(s, ((self.width - s.get_width()) // 2, (self.HEADER_HEIGHT - s.get_height()) // 2))

        # MIL indicator
        if self._mil_on:
            mil_surf = font.render("MIL", True, COLORS.get("alert_bright", (255, 60, 60)))
            surface.blit(mil_surf, (self.width - mil_surf.get_width() - 8,
                                    (self.HEADER_HEIGHT - mil_surf.get_height()) // 2))

        pygame.draw.line(surface, COLORS["border_focus"], (0, self.HEADER_HEIGHT - 1),
                         (self.width, self.HEADER_HEIGHT - 1))

    def _render_status_bar(self, surface: pygame.Surface) -> None:
        """Render MIL status and scan info."""
        font = get_mono_font(11)
        font_small = get_mono_font(10)
        y = self.HEADER_HEIGHT + 4
        x = 8

        # MIL status
        mil_label = font.render("MIL:", True, COLORS["text_secondary"])
        surface.blit(mil_label, (x, y))
        
        if self._mil_on:
            mil_val = font.render("ON", True, COLORS.get("alert_bright", (255, 60, 60)))
        else:
            mil_val = font.render("OFF", True, COLORS.get("green_bright", (0, 230, 118)))
        surface.blit(mil_val, (x + mil_label.get_width() + 6, y))

        # Scan status
        if self._scan_in_progress:
            scan_str = "SCANNING..."
            scan_color = COLORS["cyan"]
        elif self._last_scan_time is not None:
            elapsed = time.time() - self._last_scan_time
            if elapsed < 60:
                scan_str = f"SCANNED {int(elapsed)}s AGO"
            else:
                scan_str = f"SCANNED {int(elapsed / 60)}m AGO"
            scan_color = COLORS["text_tertiary"]
        else:
            scan_str = "NOT SCANNED"
            scan_color = COLORS["text_tertiary"]

        scan_surf = font_small.render(scan_str, True, scan_color)
        surface.blit(scan_surf, (self.width - scan_surf.get_width() - 8, y + 2))

        # DTC count summary
        y += 18
        total = self._total_dtc_count()
        count_str = f"STORED: {len(self._stored_dtcs)}   PENDING: {len(self._pending_dtcs)}   TOTAL: {total}"
        count_surf = font_small.render(count_str, True, COLORS["text_secondary"])
        surface.blit(count_surf, (x, y))

        # Separator line
        y += 16
        pygame.draw.line(surface, COLORS["border_dim"], (4, y), (self.width - 4, y))

    def _render_dtc_list(self, surface: pygame.Surface) -> None:
        """Render the DTC list."""
        font_section = get_title_font(10)
        font_code = get_mono_font(11)
        font_desc = get_mono_font(9)

        y_start = self.HEADER_HEIGHT + 42
        y = y_start
        x = 8
        max_y = self.height - 20  # Reserve space for footer

        # Build combined list with section markers
        items = []  # (type, data) where type is 'header' or 'dtc'
        
        if self._stored_dtcs:
            items.append(("header", f"STORED ({len(self._stored_dtcs)})"))
            for code, ecu in self._stored_dtcs:
                items.append(("dtc", (code, ecu, "stored")))
        
        if self._pending_dtcs:
            items.append(("header", f"PENDING ({len(self._pending_dtcs)})"))
            for code, ecu in self._pending_dtcs:
                items.append(("dtc", (code, ecu, "pending")))

        if not items:
            # No DTCs
            if self._scan_in_progress:
                msg = "SCANNING ECUs..."
                color = COLORS["cyan"]
            elif self._last_scan_time is not None:
                msg = "NO TROUBLE CODES FOUND"
                color = COLORS.get("green_bright", (0, 230, 118))
            else:
                msg = "PRESS TO SCAN FOR CODES"
                color = COLORS["text_tertiary"]
            
            s = font_section.render(msg, True, color)
            surface.blit(s, ((self.width - s.get_width()) // 2, y + 40))
            return

        # Apply scroll offset
        visible_start = self._scroll_offset
        visible_items = items[visible_start:]

        for item_type, data in visible_items:
            if y >= max_y:
                # Show "more" indicator
                more = font_desc.render(f"... {len(visible_items) - items.index((item_type, data)) + visible_start} more (press to scroll)", True, COLORS["text_tertiary"])
                surface.blit(more, (x, y))
                break

            if item_type == "header":
                # Section header
                s = font_section.render(data, True, COLORS["cyan"])
                surface.blit(s, (x, y + 1))
                y += self.LINE_HEIGHT
            else:
                code, ecu, dtc_type = data
                desc = get_dtc_description(code)
                
                # Color based on type
                if dtc_type == "stored":
                    code_color = COLORS.get("alert_bright", (255, 60, 60))
                else:
                    code_color = COLORS.get("warm_bright", COLORS["active"])

                # Code
                code_surf = font_code.render(code, True, code_color)
                surface.blit(code_surf, (x + 4, y + 1))

                # ECU name
                ecu_surf = font_desc.render(ecu, True, COLORS["text_secondary"])
                surface.blit(ecu_surf, (x + 70, y + 3))

                # Description (truncate if too long)
                max_desc_w = self.width - x - 140
                desc_surf = font_desc.render(desc, True, COLORS["text_tertiary"])
                if desc_surf.get_width() > max_desc_w:
                    # Truncate
                    while desc and desc_surf.get_width() > max_desc_w:
                        desc = desc[:-1]
                        desc_surf = font_desc.render(desc + "...", True, COLORS["text_tertiary"])
                surface.blit(desc_surf, (x + 130, y + 3))

                y += self.LINE_HEIGHT

    def _render_footer(self, surface: pygame.Surface) -> None:
        """Render footer with controls hint."""
        font = get_mono_font(10)
        footer_y = self.height - 3

        if self._clear_confirm:
            # Confirmation overlay
            elapsed = time.time() - self._clear_confirm_time
            if elapsed > self._CLEAR_CONFIRM_TIMEOUT:
                self._clear_confirm = False
            else:
                # Flashing warning bar
                flash = int(elapsed * 4) % 2 == 0
                bar_color = COLORS.get("alert_bright", (255, 60, 60)) if flash else COLORS["bg_panel"]
                bar_h = 28
                bar_y = self.height - bar_h
                pygame.draw.rect(surface, bar_color, (0, bar_y, self.width, bar_h))

                confirm_text = "CLEAR ALL DTCs?  [HOLD] YES  [PRESS/BACK] NO"
                text_color = (0, 0, 0) if flash else COLORS.get("alert_bright", (255, 60, 60))
                s = font.render(confirm_text, True, text_color)
                surface.blit(s, ((self.width - s.get_width()) // 2, bar_y + (bar_h - s.get_height()) // 2))
                return

        if self._total_dtc_count() > 0:
            hint = "[PRESS] SCAN  [ROTATE\u2192] CLEAR  [BACK] EXIT"
        elif self._total_dtc_count() > self.MAX_VISIBLE_DTCS:
            hint = "[PRESS] SCROLL  [HOLD] SCAN  [BACK] EXIT"
        else:
            hint = "[PRESS] SCAN  [BACK] EXIT"

        s = font.render(hint, True, COLORS["text_secondary"])
        surface.blit(s, ((self.width - s.get_width()) // 2, footer_y - s.get_height()))
