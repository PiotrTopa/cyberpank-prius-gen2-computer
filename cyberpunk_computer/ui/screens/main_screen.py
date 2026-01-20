"""
Main dashboard screen.

The primary display showing Audio, Ambient, Climate, and Lights frames.
"""

import pygame
import time
from typing import Tuple

from .base import Screen
from .audio_screen import AudioScreen
from .climate_screen import ClimateScreen
from .lights_screen import LightsScreen
from .ambient_screen import AmbientScreen
from ..widgets.base import Rect
from ..widgets.frame import Frame
from ..widgets.controls import VolumeBar, ToggleSwitch, ValueDisplay, ModeIcon, StatusIcon
from ..colors import COLORS
from ..fonts import get_font
from ...persistence import get_settings, save_settings


class MainScreen(Screen):
    """
    Main dashboard screen.
    
    Layout:
    ┌───────────┬─────────────────────────────┬───────────────────┐
    │  AUDIO    │                             │   CLIMATE         │
    │  120×80   │                             │   120×80          │
    ├───────────┤        MAIN AREA            ├───────────────────┤
    │  AMBIENT  │         240×240             │   LIGHTS          │
    │  120×80   │       (reserved)            │   120×80          │
    ├───────────┤                             ├───────────────────┤
    │  (spare)  │                             │   (spare)         │
    │  120×80   │                             │   120×80          │
    └───────────┴─────────────────────────────┴───────────────────┘
    """
    
    # Layout constants
    SIDE_PANEL_WIDTH = 120
    FRAME_HEIGHT = 80
    
    # Lights modes
    LIGHTS_MODES = ["AUTO", "MANUAL", "OFF"]
    
    # Ambient modes
    AMBIENT_MODES = ["OFF", "MANUAL", "CYBER", "SMOOTH", "ROMANCE", "MUSIC"]
    
    def __init__(self, size: Tuple[int, int], app=None):
        """Initialize the main screen."""
        super().__init__(size, app)
        
        # Sample data (will be replaced with live data from Gateway)
        self._volume = 35
        self._ambient_on = True
        self._temp_in = "22"
        self._temp_out = "-5"
        self._temp_target = "21"
        self._climate_ac = True
        self._climate_auto = True
        self._climate_recirc = False
        
        # Lights data
        self._lights_mode = "AUTO"  # AUTO, MANUAL, OFF
        self._drl_on = True
        self._biled_on = False
        self._biled_mode = "OFF"  # OFF, ON, PWM
        self._biled_brightness = 100
        self._lowbeam_on = False
        
        # Ambient data
        self._ambient_mode = "OFF"  # OFF, MANUAL, CYBER, SMOOTH, ROMANCE, MUSIC
        self._ambient_hue = 180
        self._ambient_saturation = 100
        self._ambient_brightness = 80
        
        # Editing mode states
        self._editing_volume = False
        self._editing_target_temp = False
        self._editing_lights = False
        self._editing_ambient = False
        self._editing_start_time = 0.0  # When editing started
        self._audio_frame = None
        self._ambient_frame = None
        self._lights_frame = None
        self._climate_frame = None
        
        # Focus visibility tracking
        self._last_activity_time = time.time()
        
        # Create frames (order of creation doesn't affect focus order)
        self._create_left_panels()
        self._create_right_panels()
        self._create_center_area()
        
        # Set focus order: Audio -> Climate -> Ambient -> Lights -> System -> Vehicle
        self._set_focus_order()
        
        # Start with focus hidden (visually)
        self.focus_manager.hide_focus()
    
    def _set_focus_order(self) -> None:
        """Set the focus navigation order for frames."""
        # Clear default focus order and set custom order
        self.focus_manager.clear()
        
        # Add frames in desired focus order
        self.focus_manager.add_widget(self._audio_frame)
        self.focus_manager.add_widget(self._climate_frame)
        self.focus_manager.add_widget(self._ambient_frame)
        self.focus_manager.add_widget(self._lights_frame)
        self.focus_manager.add_widget(self._system_frame)
        self.focus_manager.add_widget(self._vehicle_frame)
    
    def _create_left_panels(self) -> None:
        """Create left side panels (Audio, Ambient, spare)."""
        x = 0
        
        # Audio frame
        self._audio_frame = Frame(
            Rect(x, 0, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="AUDIO",
            on_select=self._on_audio_select,
            on_action=self._on_audio_action
        )
        
        # Volume bar inside audio frame
        content = self._audio_frame.content_rect
        self._volume_bar = VolumeBar(
            Rect(
                content.x + 4,
                content.y + content.height - 16,
                content.width - 8,
                12
            ),
            value=self._volume,
            segments=10
        )
        self._audio_frame.add_child(self._volume_bar)
        
        # Volume label
        self._volume_label = ValueDisplay(
            Rect(content.x, content.y, content.width, 30),
            label="VOL",
            value=str(self._volume),
            unit=""
        )
        self._audio_frame.add_child(self._volume_label)
        
        self.add_widget(self._audio_frame)
        
        # Ambient frame
        self._ambient_frame = Frame(
            Rect(x, self.FRAME_HEIGHT, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="AMBIENT",
            on_select=self._on_ambient_select,
            on_action=self._on_ambient_action
        )
        
        # ON/OFF toggle inside ambient frame
        content = self._ambient_frame.content_rect
        self._ambient_toggle = ToggleSwitch(
            Rect(
                content.x + (content.width - 80) // 2,
                content.y + (content.height - 20) // 2,
                80,
                20
            ),
            state=self._ambient_on
        )
        self._ambient_frame.add_child(self._ambient_toggle)
        
        self.add_widget(self._ambient_frame)
        
        # System frame (placeholder for future use)
        self._system_frame = Frame(
            Rect(x, self.FRAME_HEIGHT * 2, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="SYSTEM",
            focusable=True
        )
        self.add_widget(self._system_frame)
    
    def _create_right_panels(self) -> None:
        """Create right side panels (Climate, Lights, spare)."""
        x = self.width - self.SIDE_PANEL_WIDTH
        
        # Climate frame
        self._climate_frame = Frame(
            Rect(x, 0, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="CLIMATE",
            on_select=self._on_climate_select,
            on_action=self._on_climate_action
        )
        
        # Temperature displays inside climate frame - compact layout at top
        content = self._climate_frame.content_rect
        third_width = content.width // 3
        temp_height = 28  # Compact height for temperature displays
        
        self._temp_in_display = ValueDisplay(
            Rect(content.x, content.y, third_width, temp_height),
            label="IN",
            value=self._temp_in,
            unit="°",
            compact=True
        )
        self._climate_frame.add_child(self._temp_in_display)
        
        self._temp_out_display = ValueDisplay(
            Rect(content.x + third_width, content.y, third_width, temp_height),
            label="OUT",
            value=self._temp_out,
            unit="°",
            compact=True
        )
        self._climate_frame.add_child(self._temp_out_display)
        
        self._temp_target_display = ValueDisplay(
            Rect(content.x + third_width * 2, content.y, third_width, temp_height),
            label="SET",
            value=self._temp_target,
            unit="°",
            compact=True
        )
        self._climate_frame.add_child(self._temp_target_display)
        
        # Mode icons in the lower portion
        icon_y = content.y + temp_height + 4
        icon_height = content.height - temp_height - 4
        icon_width = content.width // 3
        
        self._ac_icon = ModeIcon(
            Rect(content.x, icon_y, icon_width, icon_height),
            icon="ac",
            active=self._climate_ac,
            label="A/C"
        )
        self._climate_frame.add_child(self._ac_icon)
        
        self._auto_icon = ModeIcon(
            Rect(content.x + icon_width, icon_y, icon_width, icon_height),
            icon="auto",
            active=self._climate_auto,
            label="AUTO"
        )
        self._climate_frame.add_child(self._auto_icon)
        
        self._recirc_icon = ModeIcon(
            Rect(content.x + icon_width * 2, icon_y, icon_width, icon_height),
            icon="recirc",
            active=self._climate_recirc,
            label="RECIRC"
        )
        self._climate_frame.add_child(self._recirc_icon)
        
        self.add_widget(self._climate_frame)
        
        # Lights frame
        self._lights_frame = Frame(
            Rect(x, self.FRAME_HEIGHT, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="LIGHTS",
            on_select=self._on_lights_select,
            on_action=self._on_lights_action
        )
        
        content = self._lights_frame.content_rect
        
        # Top: MODE toggle (AUTO/MANUAL/OFF) - same as AMBIENT
        self._lights_toggle = ToggleSwitch(
            Rect(
                content.x + (content.width - 80) // 2,
                content.y + 2,
                80,
                20
            ),
            state=self._lights_mode != "OFF",
            on_text=self._lights_mode if self._lights_mode != "OFF" else "AUTO",
            off_text="OFF"
        )
        self._lights_frame.add_child(self._lights_toggle)
        
        # Below: Status indicators in a row
        third_width = content.width // 3
        status_y = content.y + 20
        status_height = content.height - 24
        
        # DRL status
        self._drl_status = StatusIcon(
            Rect(content.x, status_y, third_width, status_height),
            label="DRL",
            active=self._drl_on
        )
        self._lights_frame.add_child(self._drl_status)
        
        # BiLED status  
        self._biled_status = StatusIcon(
            Rect(content.x + third_width, status_y, third_width, status_height),
            label="LED",
            active=self._biled_on
        )
        self._lights_frame.add_child(self._biled_status)
        
        # Low beam status (Mijania)
        self._lowbeam_status = StatusIcon(
            Rect(content.x + third_width * 2, status_y, third_width, status_height),
            label="LOW",
            active=self._lowbeam_on
        )
        self._lights_frame.add_child(self._lowbeam_status)
        
        self.add_widget(self._lights_frame)
        
        # Vehicle frame (placeholder)
        self._vehicle_frame = Frame(
            Rect(x, self.FRAME_HEIGHT * 2, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="VEHICLE",
            focusable=True
        )
        self.add_widget(self._vehicle_frame)
    
    def _create_center_area(self) -> None:
        """Create center area (reserved for future content)."""
        # The center area is currently empty
        # Will be used for main content display
        pass
    
    def update(self, dt: float) -> None:
        """Update screen and check for focus timeout."""
        super().update(dt)
        
        # Get timeout from config
        focus_timeout = 15.0  # Default fallback
        editing_timeout = 60.0  # Default fallback
        if self.app and hasattr(self.app, 'config'):
            focus_timeout = self.app.config.timeout_focus_hide
            editing_timeout = self.app.config.timeout_editing_exit
        
        # Check for focus timeout (only when not editing)
        if not self._is_editing():
            if self.focus_manager.focus_visible:
                if time.time() - self._last_activity_time > focus_timeout:
                    self.focus_manager.hide_focus()
                    # Reset focus to AUDIO (index 0) when hiding
                    self.focus_manager.focus_index = 0
        else:
            # Check editing timeout
            if time.time() - self._editing_start_time > editing_timeout:
                self._exit_all_edit_modes()
    
    def _is_editing(self) -> bool:
        """Check if any editing mode is active."""
        return (self._editing_volume or self._editing_target_temp or 
                self._editing_lights or self._editing_ambient)
    
    def _exit_all_edit_modes(self) -> None:
        """Exit all editing modes."""
        if self._editing_volume:
            self._exit_volume_edit()
        if self._editing_target_temp:
            self._exit_target_temp_edit()
        if self._editing_lights:
            self._exit_lights_edit()
        if self._editing_ambient:
            self._exit_ambient_edit()
    
    def _reset_activity(self) -> None:
        """Reset activity timer and ensure focus is visible."""
        self._last_activity_time = time.time()
        if not self.focus_manager.focus_visible:
            self.focus_manager.show_focus()
    
    def render(self, surface: pygame.Surface) -> None:
        """Render the main screen."""
        # Render all widgets
        super().render(surface)
        
        # Draw center area placeholder
        center_x = self.SIDE_PANEL_WIDTH
        center_width = self.width - self.SIDE_PANEL_WIDTH * 2
        
        # Subtle border for center area
        pygame.draw.rect(
            surface,
            COLORS["border_normal"],
            (center_x, 0, center_width, self.height),
            1
        )
        
        # Center logo/title (placeholder)
        font = get_font(16, "title")
        title = "CYBERPUNK"
        title_surf = font.render(title, True, COLORS["cyan_dim"])
        title_x = center_x + (center_width - title_surf.get_width()) // 2
        title_y = self.height // 2 - 20
        surface.blit(title_surf, (title_x, title_y))
        
        font_small = get_font(10)
        subtitle = "PRIUS GEN2"
        sub_surf = font_small.render(subtitle, True, COLORS["text_secondary"])
        sub_x = center_x + (center_width - sub_surf.get_width()) // 2
        surface.blit(sub_surf, (sub_x, title_y + 20))
    
    # ─────────────────────────────────────────────────────────────────────────
    # Event Handlers
    # ─────────────────────────────────────────────────────────────────────────
    
    def handle_input(self, event) -> bool:
        """Handle input events with editing mode support."""
        from ...input.manager import InputEvent as IE
        
        # Reset activity on any input
        self._reset_activity()
        
        # Volume editing mode
        if self._editing_volume:
            if event == IE.ROTATE_LEFT:
                self._adjust_volume(-5)
                return True
            elif event == IE.ROTATE_RIGHT:
                self._adjust_volume(5)
                return True
            elif event == IE.PRESS_LIGHT or event == IE.PRESS_STRONG:
                self._exit_volume_edit()
                return True
            return True
        
        # Climate target temp editing mode
        if self._editing_target_temp:
            if event == IE.ROTATE_LEFT:
                self._adjust_target_temp(-1)
                return True
            elif event == IE.ROTATE_RIGHT:
                self._adjust_target_temp(1)
                return True
            elif event == IE.PRESS_LIGHT or event == IE.PRESS_STRONG:
                self._exit_target_temp_edit()
                return True
            return True
        
        # Lights mode editing
        if self._editing_lights:
            if event == IE.ROTATE_LEFT:
                self._adjust_lights_mode(-1)
                return True
            elif event == IE.ROTATE_RIGHT:
                self._adjust_lights_mode(1)
                return True
            elif event == IE.PRESS_LIGHT or event == IE.PRESS_STRONG:
                self._exit_lights_edit()
                return True
            return True
        
        # Ambient mode editing
        if self._editing_ambient:
            if event == IE.ROTATE_LEFT:
                self._adjust_ambient_mode(-1)
                return True
            elif event == IE.ROTATE_RIGHT:
                self._adjust_ambient_mode(1)
                return True
            elif event == IE.PRESS_LIGHT or event == IE.PRESS_STRONG:
                self._exit_ambient_edit()
                return True
            return True
        
        # Normal input handling
        return super().handle_input(event)
    
    def _adjust_volume(self, delta: int) -> None:
        """Adjust volume by delta amount."""
        self._volume = max(0, min(100, self._volume + delta))
        self._volume_bar.set_value(self._volume)
        self._volume_label.set_value(str(self._volume))
    
    def _adjust_target_temp(self, delta: int) -> None:
        """Adjust target temperature by delta."""
        new_temp = int(self._temp_target) + delta
        new_temp = max(16, min(28, new_temp))
        self._temp_target = str(new_temp)
        self._temp_target_display.set_value(self._temp_target)
    
    def _enter_volume_edit(self) -> None:
        """Enter volume editing mode."""
        self._editing_volume = True
        self._editing_start_time = time.time()
        self._audio_frame.active = True
    
    def _exit_volume_edit(self) -> None:
        """Exit volume editing mode."""
        self._editing_volume = False
        self._audio_frame.active = False
    
    def _enter_target_temp_edit(self) -> None:
        """Enter target temperature editing mode."""
        self._editing_target_temp = True
        self._editing_start_time = time.time()
        self._climate_frame.active = True
        self._temp_target_display.set_active(True)  # Amber accent on SET label
    
    def _exit_target_temp_edit(self) -> None:
        """Exit target temperature editing mode."""
        self._editing_target_temp = False
        self._climate_frame.active = False
        self._temp_target_display.set_active(False)  # Remove amber accent
    
    def _enter_lights_edit(self) -> None:
        """Enter lights mode editing."""
        self._editing_lights = True
        self._editing_start_time = time.time()
        self._lights_frame.active = True
        self._lights_toggle.start_editing()
    
    def _exit_lights_edit(self) -> None:
        """Exit lights mode editing."""
        self._editing_lights = False
        self._lights_frame.active = False
        self._lights_toggle.stop_editing()
    
    def _adjust_lights_mode(self, delta: int) -> None:
        """Adjust lights mode by delta (cycle through modes)."""
        idx = self.LIGHTS_MODES.index(self._lights_mode)
        idx = (idx + delta) % len(self.LIGHTS_MODES)
        self._lights_mode = self.LIGHTS_MODES[idx]
        
        # Update toggle display
        is_on = self._lights_mode != "OFF"
        self._lights_toggle.on_text = self._lights_mode if is_on else "AUTO"
        self._lights_toggle.off_text = "OFF"
        self._lights_toggle.set_state(is_on)
        
        # Save to persistence
        settings = get_settings()
        settings.lights.mode = self._lights_mode
        save_settings()
    
    def _enter_ambient_edit(self) -> None:
        """Enter ambient mode editing."""
        self._editing_ambient = True
        self._editing_start_time = time.time()
        self._ambient_frame.active = True
        self._ambient_toggle.start_editing()
    
    def _exit_ambient_edit(self) -> None:
        """Exit ambient mode editing."""
        self._editing_ambient = False
        self._ambient_frame.active = False
        self._ambient_toggle.stop_editing()
    
    def _adjust_ambient_mode(self, delta: int) -> None:
        """Adjust ambient mode by delta (cycle through modes)."""
        idx = self.AMBIENT_MODES.index(self._ambient_mode)
        idx = (idx + delta) % len(self.AMBIENT_MODES)
        self._ambient_mode = self.AMBIENT_MODES[idx]
        
        # Update toggle display
        is_on = self._ambient_mode != "OFF"
        self._ambient_toggle.on_text = self._ambient_mode if is_on else "OFF"
        self._ambient_toggle.off_text = "OFF"
        self._ambient_toggle.set_state(is_on)
        
        # Save to persistence
        settings = get_settings()
        settings.ambient.mode = self._ambient_mode
        save_settings()
    
    def _on_audio_select(self) -> None:
        """Handle audio frame selection (enter volume edit mode)."""
        self._enter_volume_edit()
    
    def _on_audio_action(self) -> None:
        """Handle audio frame action (open audio settings screen)."""
        if self.app:
            audio_screen = AudioScreen(
                (self.width, self.height),
                self.app,
                initial_volume=self._volume
            )
            self.app.push_screen(audio_screen)
    
    def _on_ambient_select(self) -> None:
        """Handle ambient frame selection (enter edit mode)."""
        self._enter_ambient_edit()
    
    def _on_ambient_action(self) -> None:
        """Handle ambient frame action (open ambient settings)."""
        if self.app:
            settings = get_settings()
            ambient_screen = AmbientScreen(
                (self.width, self.height),
                self.app,
                mode=self._ambient_mode,
                hue=settings.ambient.hue,
                saturation=settings.ambient.saturation,
                brightness=settings.ambient.brightness
            )
            self.app.push_screen(ambient_screen)
    
    def _on_climate_select(self) -> None:
        """Handle climate frame selection (enter target temp edit mode)."""
        self._enter_target_temp_edit()
    
    def _on_climate_action(self) -> None:
        """Handle climate frame action (open climate settings screen)."""
        if self.app:
            climate_screen = ClimateScreen(
                (self.width, self.height),
                self.app,
                temp_target=int(self._temp_target),
                temp_in=int(self._temp_in),
                temp_out=int(self._temp_out),
                ac_on=self._climate_ac,
                auto_mode=self._climate_auto,
                recirc=self._climate_recirc
            )
            self.app.push_screen(climate_screen)
    
    def _on_lights_select(self) -> None:
        """Handle lights frame selection (enter edit mode)."""
        self._enter_lights_edit()
    
    def _on_lights_action(self) -> None:
        """Handle lights frame action (open lights settings screen)."""
        if self.app:
            settings = get_settings()
            lights_screen = LightsScreen(
                (self.width, self.height),
                self.app,
                mode=self._lights_mode,
                drl_enabled=settings.lights.drl_enabled,
                biled_mode=settings.lights.biled_mode,
                biled_brightness=settings.lights.biled_brightness
            )
            self.app.push_screen(lights_screen)
