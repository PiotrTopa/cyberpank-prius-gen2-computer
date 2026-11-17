"""
Audio settings screen.

Full-screen menu for audio configuration.
"""

import pygame
from typing import Tuple, List, Optional, Any
import time

from .base import Screen
from ..widgets.base import Rect
from ..colors import COLORS
from ..fonts import get_font, get_title_font, get_mono_font


class MenuItem:
    """
    A single menu item with label and value.
    
    Supports numeric values with min/max bounds and option-based values.
    Provides formatted display strings for audio parameters.
    """
    
    def __init__(
        self,
        label: str,
        value: Any,
        min_val: Any = None,
        max_val: Any = None,
        step: Any = 1,
        options: Optional[List[str]] = None,
        display_format: Optional[str] = None
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
            display_format: Format string for display ("balance", "fade", "tone", None)
        """
        self.label = label
        self.value = value
        self.min_val = min_val
        self.max_val = max_val
        self.step = step
        self.options = options  # For select-type items
        self.display_format = display_format
        
        # If options provided, value is an index
        if options and isinstance(value, int):
            self._option_index = value
        else:
            self._option_index = 0
    
    @property
    def display_value(self) -> str:
        """
        Get value as display string.
        
        For audio parameters, provides formatted display:
        - Balance: "L7" to "R7" with "C" for center
        - Fade: "F7" to "R7" with "C" for center
        - Tone (bass/mid/treble): "-5" to "+5" with "0" for flat
        """
        if self.options:
            return self.options[self._option_index]
        
        # Special formatting for audio parameters
        if self.label == "BALANCE":
            if self.value < 0:
                return f"L{abs(self.value)}"
            elif self.value > 0:
                return f"R{self.value}"
            else:
                return "C"  # Center
        
        elif self.label == "FADER":
            if self.value < 0:
                return f"F{abs(self.value)}"  # Front
            elif self.value > 0:
                return f"R{self.value}"       # Rear
            else:
                return "C"  # Center
        
        elif self.label in ("BASS", "MID", "TREBLE"):
            if self.value > 0:
                return f"+{self.value}"
            else:
                return str(self.value)
        
        return str(self.value)
    
    def adjust(self, delta: int) -> None:
        """Adjust value by delta (positive = right/increase)."""
        if self.options:
            # Cycle through options
            self._option_index = (self._option_index + delta) % len(self.options)
            self.value = self._option_index
        elif self.min_val is not None and self.max_val is not None:
            # Numeric value with bounds
            self.value = max(self.min_val, min(self.max_val, self.value + delta * self.step))


class AudioScreen(Screen):
    """
    Audio settings screen.
    
    Shows a list of audio settings (volume 0-63, mute, media keys,
    bass/mid/treble -5..+5, balance/fader -7..+7). Volume, mute and the media
    keys drive the Android head unit over the backend's adb link
    (backend/headunit.py, HEADUNIT_INTEGRATION.md §8.3); the aftermarket radio
    is not on the AVC-LAN (AVC_MAP.md, io/egress.py), so bass/mid/treble/
    balance/fader stay UI/state values only.
    """

    # Layout constants (8 rows fit 480x240 above the 22 px footer)
    HEADER_HEIGHT = 30
    ITEM_HEIGHT = 20
    ITEM_PADDING = 3

    #: MEDIA row options -> backend headunit_media keys
    MEDIA_OPTIONS = ["PLAY/PAUSE", "NEXT", "PREV"]
    MEDIA_KEYS = ["play_pause", "next", "prev"]
    SIDE_MARGIN = 20
    
    def __init__(self, size: Tuple[int, int], app=None, initial_volume: int = 25):
        """Initialize audio screen."""
        super().__init__(size, app)
        
        self.items: List[MenuItem] = [
            # Volume: 0-63 (Toyota uses 6-bit volume), step 1
            MenuItem("VOLUME", initial_volume, 0, 63, 1),

            # Head unit mute (absolute; the backend reads the unit back)
            MenuItem("MUTE", 0, options=["OFF", "ON"]),

            # Head unit media keys: turn picks, OK sends
            MenuItem("MEDIA", 0, options=list(self.MEDIA_OPTIONS)),
            
            # Bass: -5 to +5
            MenuItem("BASS", 0, -5, 5, 1),
            
            # Mid: -5 to +5  
            MenuItem("MID", 0, -5, 5, 1),
            
            # Treble: -5 to +5
            MenuItem("TREBLE", 0, -5, 5, 1),
            
            # Balance: -7 (full left) to +7 (full right), center 0
            MenuItem("BALANCE", 0, -7, 7, 1),
            
            # Fader: -7 (full front) to +7 (full rear), center 0
            MenuItem("FADER", 0, -7, 7, 1),
        ]
        
        # Navigation
        self._selected_index = 0
        self._editing = False  # True when adjusting a value
        
        # Inactivity tracking
        self._last_activity = time.time()
        
        # Value-change callback (set by the main screen; dispatches actions)
        self._on_value_changed = None
        # Store for the live head-unit status line (optional)
        self._store = None

    def attach_store(self, store) -> None:
        """Follow the store: header shows the head unit's state, volume and
        mute rows track the unit's real level while not being edited."""
        self._store = store

    def _hu_status(self) -> Optional[str]:
        st = getattr(self._store, "state", None)
        hu = getattr(st, "headunit", None)
        if hu is None or not hu.enabled:
            return None
        if not hu.present:
            return "NO HEAD UNIT"
        if st.audio.muted:
            return "HU MUTE"
        return "HU %d" % st.audio.volume

    def _sync_from_store(self) -> None:
        st = getattr(self._store, "state", None)
        if st is None:
            return
        editing = self.items[self._selected_index].label if self._editing else None
        if editing != "VOLUME":
            self.set_value_from_avc("VOLUME", st.audio.volume)
        if editing != "MUTE":
            self.set_value_from_avc("MUTE", 1 if st.audio.muted else 0)
    
    @property
    def volume(self) -> int:
        """Get current volume value."""
        return self.items[0].value
    
    def on_enter(self) -> None:
        """Reset activity timer on enter."""
        self._last_activity = time.time()
    
    def _get_timeout(self) -> float:
        """Get screen exit timeout from config."""
        if self.app and hasattr(self.app, 'config'):
            return self.app.config.timeout_screen_exit
        return 30.0  # Default fallback
    
    def update(self, dt: float) -> None:
        """Check for inactivity timeout."""
        super().update(dt)
        self._sync_from_store()

        # Check inactivity
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
        """
        Notify that a value was changed by user.
        
        Args:
            item_label: The item that changed (VOLUME, BASS, etc.)
            value: New value
        """
        if self._on_value_changed:
            self._on_value_changed(item_label, value)
    
    def set_on_value_changed(self, callback) -> None:
        """
        Set callback for value changes.
        
        Args:
            callback: Function(label: str, value: Any)
        """
        self._on_value_changed = callback
    
    def set_value_from_avc(self, label: str, value: Any) -> None:
        """
        Update value from AVC-LAN (without triggering callback).
        
        Args:
            label: Item label (VOLUME, BASS, etc.)
            value: New value from vehicle
        """
        for item in self.items:
            if item.label == label:
                if item.options:
                    # For option items, value is index
                    if isinstance(value, int) and 0 <= value < len(item.options):
                        item._option_index = value
                        item.value = value
                else:
                    item.value = value
                break

    def handle_input(self, event) -> bool:
        """Handle input events."""
        from ...input.manager import InputEvent as IE
        
        self._reset_activity()

        # BACK always exits
        if event == IE.BACK:
            self._editing = False
            self._exit_screen()
            return True
        
        if self._editing:
            # Editing mode: arrows adjust value
            if event in (IE.ROTATE_LEFT, IE.ROTATE_RIGHT):
                item = self.items[self._selected_index]
                item.adjust(-1 if event == IE.ROTATE_LEFT else 1)
                if item.label != "MEDIA":          # MEDIA: turn picks, OK sends
                    self._notify_value_changed(item.label, item.value)
                return True
            elif event == IE.PRESS_LIGHT:
                item = self.items[self._selected_index]
                if item.label == "MEDIA":
                    self._notify_value_changed("MEDIA", self.MEDIA_KEYS[item.value])
                # Exit editing mode
                self._editing = False
                return True
            elif event == IE.PRESS_STRONG:
                # Exit editing and screen
                self._editing = False
                self._exit_screen()
                return True
        else:
            # Navigation mode
            if event == IE.ROTATE_LEFT:
                self._selected_index = (self._selected_index - 1) % len(self.items)
                return True
            elif event == IE.ROTATE_RIGHT:
                self._selected_index = (self._selected_index + 1) % len(self.items)
                return True
            elif event == IE.PRESS_LIGHT:
                # Enter editing mode
                self._editing = True
                return True
            elif event == IE.PRESS_STRONG:
                # Exit screen
                self._exit_screen()
                return True
        
        return False
    
    def render(self, surface: pygame.Surface) -> None:
        """Render the audio settings screen."""
        # Background
        surface.fill(COLORS["bg_dark"])
        
        # Header
        self._render_header(surface)
        
        # Menu items
        self._render_menu(surface)
        
        # Footer hint
        self._render_footer(surface)
    
    def _render_header(self, surface: pygame.Surface) -> None:
        """Render screen header."""
        # Title bar background
        pygame.draw.rect(
            surface,
            COLORS["bg_panel"],
            (0, 0, self.width, self.HEADER_HEIGHT)
        )
        
        # Title
        font = get_title_font(16)
        title = "AUDIO SETTINGS"
        title_surf = font.render(title, True, COLORS["cyan"])
        title_x = (self.width - title_surf.get_width()) // 2
        title_y = (self.HEADER_HEIGHT - title_surf.get_height()) // 2
        surface.blit(title_surf, (title_x, title_y))

        # Head unit status (right): "HU 32" / "HU MUTE" / "NO HEAD UNIT"
        hu = self._hu_status()
        if hu:
            hfont = get_mono_font(10)
            color = COLORS["text_tertiary"] if hu.startswith("NO") else COLORS["text_value"]
            hs = hfont.render(hu, False, color)
            surface.blit(hs, (self.width - hs.get_width() - 8,
                              (self.HEADER_HEIGHT - hs.get_height()) // 2))
        
        # Separator line
        pygame.draw.line(
            surface,
            COLORS["border_focus"],
            (0, self.HEADER_HEIGHT - 1),
            (self.width, self.HEADER_HEIGHT - 1)
        )
    
    def _render_menu(self, surface: pygame.Surface) -> None:
        """Render menu items."""
        y = self.HEADER_HEIGHT + self.ITEM_PADDING
        
        font_label = get_mono_font(12)
        font_value = get_mono_font(12)
        
        for i, item in enumerate(self.items):
            is_selected = i == self._selected_index
            is_editing = is_selected and self._editing
            
            # Item background
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
            
            # Label (left side)
            label_color = COLORS["cyan"] if is_selected else COLORS["text_secondary"]
            label_surf = font_label.render(item.label, True, label_color)
            label_y = y + (self.ITEM_HEIGHT - label_surf.get_height()) // 2
            surface.blit(label_surf, (item_rect.x + 8, label_y))
            
            # Value (right side)
            value_color = COLORS["active"] if is_editing else (COLORS["text_value"] if is_selected else COLORS["text_secondary"])
            value_text = item.display_value
            
            # For numeric values, show with arrows if editing
            if is_editing and not item.options:
                value_text = f"< {value_text} >"
            elif is_editing and item.options:
                value_text = f"< {value_text} >"
            
            value_surf = font_value.render(value_text, True, value_color)
            value_x = item_rect.right - value_surf.get_width() - 8
            value_y = y + (self.ITEM_HEIGHT - value_surf.get_height()) // 2
            surface.blit(value_surf, (value_x, value_y))
            
            # Progress bar for volume-like items (not options)
            if is_selected and not item.options and item.min_val is not None:
                self._render_progress_bar(surface, item, item_rect)
            
            y += self.ITEM_HEIGHT + self.ITEM_PADDING
    
    def _render_progress_bar(self, surface: pygame.Surface, item: MenuItem, rect: pygame.Rect) -> None:
        """Render a small progress bar under the item."""
        bar_height = 3
        bar_y = rect.bottom - bar_height - 2
        bar_width = rect.width - 16
        bar_x = rect.x + 8
        
        # Background
        pygame.draw.rect(
            surface,
            COLORS["bg_dark"],
            (bar_x, bar_y, bar_width, bar_height)
        )
        
        # Fill
        if item.max_val != item.min_val:
            fill_ratio = (item.value - item.min_val) / (item.max_val - item.min_val)
            fill_width = int(bar_width * fill_ratio)
            if fill_width > 0:
                pygame.draw.rect(
                    surface,
                    COLORS["cyan"],
                    (bar_x, bar_y, fill_width, bar_height)
                )
    
    def _render_footer(self, surface: pygame.Surface) -> None:
        """Render footer with hints."""
        font = get_mono_font(10)
        bar_h = 22
        bar_y = self.height - bar_h

        # Dark bar background
        pygame.draw.rect(surface, COLORS["bg_panel"], (0, bar_y, self.width, bar_h))
        pygame.draw.line(surface, COLORS["border_dim"], (0, bar_y), (self.width, bar_y))

        if self._editing and self.items[self._selected_index].label == "MEDIA":
            hint = "[TURN] KEY   [OK] SEND   [BACK] EXIT"
        elif self._editing:
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
            first_y=self.HEADER_HEIGHT + self.ITEM_PADDING,
            stride=self.ITEM_HEIGHT + self.ITEM_PADDING, row_h=self.ITEM_HEIGHT,
            bottom=self.height - 22, count=lambda: len(self.items),
        )
        self.add_back_chip()
