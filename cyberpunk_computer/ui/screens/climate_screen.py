"""
Climate settings screen.

Full-screen menu for climate/HVAC configuration.
"""

import pygame
from typing import Tuple, List, Optional, Any
import time

from .base import Screen
from ..widgets.base import Rect
from ..colors import COLORS
from ..fonts import get_font, get_title_font, get_mono_font


class ClimateMenuItem:
    """A single climate menu item with label and value."""
    
    def __init__(
        self,
        label: str,
        value: Any,
        min_val: Any = None,
        max_val: Any = None,
        step: Any = 1,
        options: Optional[List[str]] = None,
        unit: str = "",
        readonly: bool = False
    ):
        """
        Initialize menu item.
        
        Args:
            label: Display label
            value: Current value
            min_val: Minimum value (for numeric)
            max_val: Maximum value (for numeric)
            step: Step size for adjustments
            options: List of options (for select type)
            unit: Unit suffix (°C, %, etc.)
            readonly: If true, item is display-only
        """
        self.label = label
        self.value = value
        self.min_val = min_val
        self.max_val = max_val
        self.step = step
        self.options = options
        self.unit = unit
        self.readonly = readonly
        
        if options and isinstance(value, int):
            self._option_index = value
        else:
            self._option_index = 0
    
    @property
    def display_value(self) -> str:
        """Get value as display string."""
        if self.options:
            return self.options[self._option_index]
        if self.value is None or self.value == "--":
            return "--"
        return f"{self.value}{self.unit}"
    
    def adjust(self, delta: int) -> None:
        """Adjust value by delta (positive = right/increase)."""
        if self.readonly:
            return
            
        if self.options:
            self._option_index = (self._option_index + delta) % len(self.options)
            self.value = self._option_index
        elif self.min_val is not None and self.max_val is not None:
            self.value = max(self.min_val, min(self.max_val, self.value + delta * self.step))


class ClimateScreen(Screen):
    """
    Climate settings screen.

    Live on AVC-LAN through :class:`~cyberpunk_computer.state.climate_model.ClimateController`
    when a store is given: every row is re-read from the store each frame
    (optimistic values shown until the next AVC status confirms them) and
    every edit is a climate command (see the table in climate_model.py).

    Rows: target temperature (0.5 °C steps), fan (1-7, "AUTO n" under AUTO),
    mode AUTO / MANUAL / OFF, A/C, intake, air direction (FACE, FACE+FEET,
    FEET, FEET+WS, DEFROST), front defrost, rear defogger, outside and inside
    temperature (read-only; inside temperature is not on the bus). The list
    scrolls to keep the selected row visible.

    Without a store (tests, demo) the rows are static values set through the
    legacy ``set_value_from_avc`` / ``update_temperatures`` helpers.
    """

    # Layout constants
    HEADER_HEIGHT = 30
    FOOTER_HEIGHT = 22
    ITEM_HEIGHT = 24
    ITEM_PADDING = 2
    SIDE_MARGIN = 16

    MODE_OPTIONS = ["AUTO", "MANUAL", "OFF"]
    ON_OFF = ["ON", "OFF"]
    INTAKE_OPTIONS = ["FRESH", "RECIRC"]

    def __init__(
        self,
        size: Tuple[int, int],
        app=None,
        temp_target: float = 22.0,
        temp_in: Optional[float] = None,
        temp_out: Optional[float] = None,
        ac_on: bool = True,
        auto_mode: bool = True,
        recirc: bool = False,
        store=None,
        controller=None,
    ):
        """Initialize climate screen (``store`` or ``controller`` = live)."""
        super().__init__(size, app)
        from ...state.climate_model import AIR_DIRECTIONS, AIR_DIRECTION_LABELS, ClimateController

        self._air_modes = list(AIR_DIRECTIONS)
        if controller is None and store is not None:
            controller = ClimateController(store)
        self._ctl = controller

        self.items: List[ClimateMenuItem] = [
            ClimateMenuItem("TARGET TEMP", float(temp_target), 18.0, 28.0, 0.5, unit="°C"),
            ClimateMenuItem("FAN SPEED", 3, 1, 7, 1),
            ClimateMenuItem("MODE", 0 if auto_mode else 1, options=list(self.MODE_OPTIONS)),
            ClimateMenuItem("A/C", 0 if ac_on else 1, options=list(self.ON_OFF)),
            ClimateMenuItem("AIR INTAKE", 1 if recirc else 0, options=list(self.INTAKE_OPTIONS)),
            ClimateMenuItem("AIR DIRECTION", 0,
                            options=[AIR_DIRECTION_LABELS[m] for m in AIR_DIRECTIONS]),
            ClimateMenuItem("FRONT DEFROST", 1, options=list(self.ON_OFF)),
            ClimateMenuItem("REAR DEFOGGER", 1, options=list(self.ON_OFF)),
            # Outside temp: AVC-LAN only (1C6->110 00 E0 5D F7); -- while no
            # fresh AVC value. Inside temp is not on AVC-LAN.
            ClimateMenuItem("OUTSIDE TEMP",
                            int(temp_out) if temp_out is not None else "--",
                            unit="°C", readonly=True),
            ClimateMenuItem("INSIDE TEMP",
                            int(temp_in) if temp_in is not None else "--",
                            unit="°C", readonly=True),
        ]
        self._by_label = {item.label: item for item in self.items}
        self._text: dict = {}           # label -> live display text (store mode)
        self._live = False
        self._stale = True

        # Navigation
        self._selected_index = 0
        self._editing = False
        self._scroll_row = 0

        # Inactivity tracking
        self._last_activity = time.time()

        # Legacy callback (label, value) — used only without a store
        self._on_value_changed = None
        self.sync_from_model()

    # ── live data ─────────────────────────────────────────────────────────

    @property
    def controller(self):
        return self._ctl

    def sync_from_model(self) -> None:
        """Refresh every row from the climate view-model (no-op without one)."""
        if self._ctl is None:
            return
        v = self._ctl.view()
        self._live, self._stale = v.live, v.stale
        it = self._by_label
        it["TARGET TEMP"].value = v.target_temp
        it["FAN SPEED"].value = v.fan
        self._set_option("MODE", {"auto": 0, "manual": 1, "off": 2}.get(v.mode))
        self._set_option("A/C", 0 if v.ac else 1)
        self._set_option("AIR INTAKE", 1 if v.recirculation else 0)
        self._set_option("AIR DIRECTION", self._air_modes.index(v.air_direction)
                         if v.air_direction in self._air_modes else None)
        self._set_option("FRONT DEFROST", 0 if v.front_defrost else 1)
        self._set_option("REAR DEFOGGER", 0 if v.rear_defogger else 1)
        it["OUTSIDE TEMP"].value = int(v.outside_temp) if v.outside_temp is not None else "--"
        it["INSIDE TEMP"].value = int(v.inside_temp) if v.inside_temp is not None else "--"
        unknown = "--" if not v.live else None
        self._text = {
            "TARGET TEMP": v.target_text + ("°C" if v.target_text not in ("OFF", "--") else ""),
            "FAN SPEED": v.fan_text,
            "MODE": v.mode_text,
            "A/C": unknown or ("ON" if v.ac else "OFF"),
            "AIR INTAKE": unknown or ("RECIRC" if v.recirculation else "FRESH"),
            "AIR DIRECTION": v.air_direction_text,
            "FRONT DEFROST": unknown or ("ON" if v.front_defrost else "OFF"),
            "REAR DEFOGGER": unknown or ("ON" if v.rear_defogger else "OFF"),
        }

    def _set_option(self, label: str, index: Optional[int]) -> None:
        item = self._by_label[label]
        if index is None:
            return
        item._option_index = index
        item.value = index

    def _display_text(self, item: ClimateMenuItem) -> str:
        text = self._text.get(item.label) if self._ctl is not None else None
        if text is not None:
            return text
        if item.label == "TARGET TEMP" and isinstance(item.value, (int, float)):
            from ...state.climate_model import format_temp
            return f"{format_temp(item.value)}{item.unit}"
        return item.display_value

    def _command(self, item: ClimateMenuItem, delta: int) -> None:
        """Turn an encoder step on ``item`` into a climate command."""
        ctl = self._ctl
        label = item.label
        if label == "TARGET TEMP":
            ctl.step_target_temp(delta)
        elif label == "FAN SPEED":
            ctl.step_fan(delta)
        elif label == "MODE":
            v = ctl.view()
            i = {"auto": 0, "manual": 1, "off": 2}.get(v.mode, -1 if delta > 0 else 0)
            ctl.set_mode(("auto", "manual", "off")[(i + delta) % 3])
        elif label == "A/C":
            ctl.toggle_ac()
        elif label == "AIR INTAKE":
            ctl.toggle_recirculation()
        elif label == "AIR DIRECTION":
            ctl.step_air_direction(delta)
        elif label == "FRONT DEFROST":
            ctl.toggle_front_defrost()
        elif label == "REAR DEFOGGER":
            ctl.toggle_rear_defogger()
        self.sync_from_model()

    def _adjust(self, item: ClimateMenuItem, delta: int) -> None:
        if self._ctl is not None:
            self._command(item, delta)
            return
        item.adjust(delta)
        self._notify_value_changed(item.label, item.value)

    @property
    def target_temp(self) -> float:
        """Get current target temperature."""
        return self.items[0].value

    @property
    def ac_on(self) -> bool:
        """Get A/C state."""
        return self.items[3].value == 0

    @property
    def auto_mode(self) -> bool:
        """Get auto mode state."""
        return self.items[2].value == 0

    @property
    def recirc(self) -> bool:
        """Get recirculation state."""
        return self.items[4].value == 1

    def on_enter(self) -> None:
        """Reset activity timer on enter."""
        self._last_activity = time.time()
        self.sync_from_model()

    def _get_timeout(self) -> float:
        """Get screen exit timeout from config."""
        if self.app and hasattr(self.app, 'config'):
            return self.app.config.timeout_screen_exit
        return 30.0  # Default fallback

    def update(self, dt: float) -> None:
        """Keep rows live; check for inactivity timeout."""
        super().update(dt)
        self.sync_from_model()

        if time.time() - self._last_activity > self._get_timeout():
            self._exit_screen()

    def _reset_activity(self) -> None:
        """Reset inactivity timer."""
        self._last_activity = time.time()

    def _exit_screen(self) -> None:
        """Exit back to main screen."""
        if self.app:
            self.app.pop_screen()

    def _notify_value_changed(self, item_label: str, value: Any) -> None:
        """Legacy hook (no-store mode): report a user change."""
        if self._on_value_changed:
            self._on_value_changed(item_label, value)

    def set_on_value_changed(self, callback) -> None:
        """
        Set callback for value changes (used only without a store).

        Args:
            callback: Function(label: str, value: Any)
        """
        self._on_value_changed = callback

    def set_value_from_avc(self, label: str, value: Any) -> None:
        """
        Update value from AVC-LAN (without triggering callback). With a store
        the rows follow the store anyway and this is overwritten next frame.
        """
        item = self._by_label.get(label)
        if item is None:
            return
        if item.options:
            if isinstance(value, int) and 0 <= value < len(item.options):
                item._option_index = value
                item.value = value
        else:
            item.value = value

    def update_temperatures(self, inside: Optional[float] = None,
                          outside: Optional[float] = None) -> None:
        """
        Update temperature readings (no-store mode).

        Args:
            inside: Inside temperature (°C)
            outside: Outside temperature (°C)
        """
        if inside is not None:
            self._by_label["INSIDE TEMP"].value = int(inside)
        self._by_label["OUTSIDE TEMP"].value = int(outside) if outside is not None else "--"

    # ── scrolling ─────────────────────────────────────────────────────────

    @property
    def _stride(self) -> int:
        return self.ITEM_HEIGHT + self.ITEM_PADDING

    @property
    def _list_top(self) -> int:
        return self.HEADER_HEIGHT + self.ITEM_PADDING + 4

    @property
    def visible_rows(self) -> int:
        return max(1, (self.height - self.FOOTER_HEIGHT - self._list_top) // self._stride)

    def _ensure_visible(self) -> None:
        n = self.visible_rows
        if self._selected_index < self._scroll_row:
            self._scroll_row = self._selected_index
        elif self._selected_index >= self._scroll_row + n:
            self._scroll_row = self._selected_index - n + 1
        self._scroll_row = max(0, min(self._scroll_row, max(0, len(self.items) - n)))

    _ensure_selected_visible = _ensure_visible   # MenuListZone hook

    def _scroll_px(self) -> int:
        return self._scroll_row * self._stride

    def handle_input(self, event) -> bool:
        """Handle input events."""
        from ...input.manager import InputEvent as IE
        
        self._reset_activity()

        # BACK always exits
        if event == IE.BACK:
            self._editing = False
            self._exit_screen()
            return True
        
        current_item = self.items[self._selected_index]
        
        if self._editing:
            if current_item.readonly:
                # Readonly items can't be edited
                self._editing = False
                return True
            
            if event == IE.ROTATE_LEFT:
                self._adjust(current_item, -1)
                return True
            elif event == IE.ROTATE_RIGHT:
                self._adjust(current_item, 1)
                return True
            elif event == IE.PRESS_LIGHT:
                self._editing = False
                return True
            elif event == IE.PRESS_STRONG:
                self._editing = False
                self._exit_screen()
                return True
        else:
            if event == IE.ROTATE_LEFT:
                self._selected_index = (self._selected_index - 1) % len(self.items)
                self._ensure_visible()
                return True
            elif event == IE.ROTATE_RIGHT:
                self._selected_index = (self._selected_index + 1) % len(self.items)
                self._ensure_visible()
                return True
            elif event == IE.PRESS_LIGHT:
                if not current_item.readonly:
                    self._editing = True
                return True
            elif event == IE.PRESS_STRONG:
                self._exit_screen()
                return True
        
        return False
    
    def render(self, surface: pygame.Surface) -> None:
        """Render the climate settings screen."""
        surface.fill(COLORS["bg_dark"])
        
        self._render_header(surface)
        self._render_menu(surface)
        self._render_footer(surface)
    
    def _render_header(self, surface: pygame.Surface) -> None:
        """Render screen header."""
        pygame.draw.rect(
            surface,
            COLORS["bg_panel"],
            (0, 0, self.width, self.HEADER_HEIGHT)
        )
        
        font = get_title_font(16)
        title = "CLIMATE CONTROL"
        title_surf = font.render(title, True, COLORS["cyan"])
        title_x = (self.width - title_surf.get_width()) // 2
        title_y = (self.HEADER_HEIGHT - title_surf.get_height()) // 2
        surface.blit(title_surf, (title_x, title_y))

        if self._ctl is not None and self._stale:
            tag = get_mono_font(10).render("NO AVC" if not self._live else "STALE",
                                           True, COLORS["amber"])
            surface.blit(tag, (self.width - tag.get_width() - 8,
                               (self.HEADER_HEIGHT - tag.get_height()) // 2))

        pygame.draw.line(
            surface,
            COLORS["border_focus"],
            (0, self.HEADER_HEIGHT - 1),
            (self.width, self.HEADER_HEIGHT - 1)
        )
    
    def _render_menu(self, surface: pygame.Surface) -> None:
        """Render menu items."""
        self._ensure_visible()
        top = self._list_top
        bottom = self.height - self.FOOTER_HEIGHT
        y = top - self._scroll_px()

        # Terminus (same as the audio page); 11 px fell back to the pixel font
        font_label = get_mono_font(12)
        font_value = get_mono_font(12)

        prev_clip = surface.get_clip()
        surface.set_clip(pygame.Rect(0, top - 2, self.width, bottom - top + 2))
        for i, item in enumerate(self.items):
            is_selected = i == self._selected_index
            is_editing = is_selected and self._editing
            is_readonly = item.readonly
            
            item_rect = pygame.Rect(
                self.SIDE_MARGIN,
                y,
                self.width - self.SIDE_MARGIN * 2,
                self.ITEM_HEIGHT
            )
            
            if is_selected:
                bg_color = COLORS["bg_frame_focus"]
                border_color = COLORS["border_active"] if is_editing else COLORS["border_focus"]
                pygame.draw.rect(surface, bg_color, item_rect)
                pygame.draw.rect(surface, border_color, item_rect, 1)
            
            # Label
            if is_readonly:
                label_color = COLORS["text_secondary"]
            elif is_selected:
                label_color = COLORS["cyan"]
            else:
                label_color = COLORS["text_secondary"]
            
            label_surf = font_label.render(item.label, True, label_color)
            label_y = y + (self.ITEM_HEIGHT - label_surf.get_height()) // 2
            surface.blit(label_surf, (item_rect.x + 6, label_y))
            
            # Value
            if is_readonly:
                value_color = COLORS["inactive"]
            elif is_editing:
                value_color = COLORS["active"]
            elif is_selected:
                value_color = COLORS["text_value"]
            else:
                value_color = COLORS["text_secondary"]
            
            value_text = self._display_text(item)
            if is_editing:
                value_text = f"< {value_text} >"
            
            value_surf = font_value.render(value_text, True, value_color)
            value_x = item_rect.right - value_surf.get_width() - 6
            value_y = y + (self.ITEM_HEIGHT - value_surf.get_height()) // 2
            surface.blit(value_surf, (value_x, value_y))
            
            y += self.ITEM_HEIGHT + self.ITEM_PADDING
        surface.set_clip(prev_clip)
        self._render_scroll_marks(surface, top, bottom)

    def _render_scroll_marks(self, surface: pygame.Surface, top: int, bottom: int) -> None:
        """Small triangles at the right edge when rows are hidden above/below."""
        x = self.width - self.SIDE_MARGIN // 2
        color = COLORS["text_tertiary"]
        if self._scroll_row > 0:
            pygame.draw.polygon(surface, color, [(x - 3, top + 4), (x + 3, top + 4), (x, top)])
        if self._scroll_row + self.visible_rows < len(self.items):
            pygame.draw.polygon(surface, color,
                                [(x - 3, bottom - 6), (x + 3, bottom - 6), (x, bottom - 2)])
    
    def _render_footer(self, surface: pygame.Surface) -> None:
        """Render footer with hints."""
        font = get_mono_font(10)
        bar_h = 22
        bar_y = self.height - bar_h

        pygame.draw.rect(surface, COLORS["bg_panel"], (0, bar_y, self.width, bar_h))
        pygame.draw.line(surface, COLORS["border_dim"], (0, bar_y), (self.width, bar_y))

        if self._editing:
            hint = "[TURN] ADJUST   [OK] DONE   [BACK] EXIT"
        else:
            hint = "[TURN] SELECT   [OK] EDIT   [BACK] EXIT"
        
        hint_surf = font.render(hint, True, COLORS["text_tertiary"])
        hint_x = (self.width - hint_surf.get_width()) // 2
        surface.blit(hint_surf, (hint_x, bar_y + (bar_h - hint_surf.get_height()) // 2))

    # ─── Pointer / touch ──────────────────────────────────────────────

    def setup_pointer(self) -> None:
        """Rows: tap selects + activates (encoder semantics), see
        pointer_helpers.MenuListZone. Footer: "< BACK" chip."""
        from .pointer_helpers import add_menu_list
        add_menu_list(
            self,
            x=self.SIDE_MARGIN, width=self.width - self.SIDE_MARGIN * 2,
            first_y=self._list_top,
            stride=self._stride, row_h=self.ITEM_HEIGHT,
            bottom=self.height - self.FOOTER_HEIGHT, count=lambda: len(self.items),
            scroll_px=self._scroll_px, scrollable=True,
        )
        self.add_back_chip()
