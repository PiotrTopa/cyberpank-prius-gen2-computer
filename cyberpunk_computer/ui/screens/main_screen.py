"""
Main dashboard screen.

The primary display showing Audio, Ambient, Climate, and Lights frames.
"""

import pygame
from typing import Tuple

from .base import Screen
from .audio_screen import AudioScreen
from .climate_screen import ClimateScreen
from ..widgets.base import Rect
from ..widgets.frame import Frame
from ..widgets.controls import VolumeBar, ToggleSwitch, ValueDisplay, ModeIcon, StatusIcon
from ..colors import COLORS
from ..fonts import get_font


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
        self._drl_on = True
        self._biled_on = False
        
        # Editing mode states
        self._editing_volume = False
        self._editing_ambient = False
        self._editing_drl = False
        self._audio_frame = None  # Will be set in _create_left_panels
        self._ambient_frame = None
        self._lights_frame = None
        
        # Create frames
        self._create_left_panels()
        self._create_right_panels()
        self._create_center_area()
    
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
                content.x + (content.width - 60) // 2,
                content.y + (content.height - 20) // 2,
                60,
                20
            ),
            state=self._ambient_on
        )
        self._ambient_frame.add_child(self._ambient_toggle)
        
        self.add_widget(self._ambient_frame)
        
        # Spare frame (placeholder for future use)
        spare_frame = Frame(
            Rect(x, self.FRAME_HEIGHT * 2, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="SYSTEM",
            focusable=True
        )
        self.add_widget(spare_frame)
    
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
        half_width = content.width // 2
        
        # DRL toggle (controllable)
        self._drl_toggle = ToggleSwitch(
            Rect(content.x + 4, content.y + 10, half_width - 8, 20),
            state=self._drl_on,
            on_text="DRL",
            off_text="DRL"
        )
        self._lights_frame.add_child(self._drl_toggle)
        
        # BiLED status (info only, not toggleable)
        self._biled_status = StatusIcon(
            Rect(content.x + half_width + 4, content.y + 10, half_width - 8, 20),
            label="biLED",
            active=self._biled_on
        )
        self._lights_frame.add_child(self._biled_status)
        
        self.add_widget(self._lights_frame)
        
        # Spare frame (placeholder)
        spare_frame = Frame(
            Rect(x, self.FRAME_HEIGHT * 2, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="VEHICLE",
            focusable=True
        )
        self.add_widget(spare_frame)
    
    def _create_center_area(self) -> None:
        """Create center area (reserved for future content)."""
        # The center area is currently empty
        # Will be used for main content display
        pass
    
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
        font = get_font(16, bold=True)
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
        
        # Ambient editing mode (toggle with arrows)
        if self._editing_ambient:
            if event == IE.ROTATE_LEFT or event == IE.ROTATE_RIGHT:
                self._ambient_on = self._ambient_toggle.toggle()
                return True
            elif event == IE.PRESS_LIGHT or event == IE.PRESS_STRONG:
                self._exit_ambient_edit()
                return True
            return True
        
        # DRL editing mode (toggle with arrows)
        if self._editing_drl:
            if event == IE.ROTATE_LEFT or event == IE.ROTATE_RIGHT:
                self._drl_on = self._drl_toggle.toggle()
                return True
            elif event == IE.PRESS_LIGHT or event == IE.PRESS_STRONG:
                self._exit_drl_edit()
                return True
            return True
        
        # Normal input handling
        return super().handle_input(event)
    
    def _adjust_volume(self, delta: int) -> None:
        """Adjust volume by delta amount."""
        self._volume = max(0, min(100, self._volume + delta))
        self._volume_bar.set_value(self._volume)
        self._volume_label.set_value(str(self._volume))
    
    def _enter_volume_edit(self) -> None:
        """Enter volume editing mode."""
        self._editing_volume = True
        self._audio_frame.active = True
    
    def _exit_volume_edit(self) -> None:
        """Exit volume editing mode."""
        self._editing_volume = False
        self._audio_frame.active = False
    
    def _enter_ambient_edit(self) -> None:
        """Enter ambient toggle editing mode."""
        self._editing_ambient = True
        self._ambient_frame.active = True
        self._ambient_toggle.start_editing()
    
    def _exit_ambient_edit(self) -> None:
        """Exit ambient toggle editing mode."""
        self._editing_ambient = False
        self._ambient_frame.active = False
        self._ambient_toggle.stop_editing()
    
    def _enter_drl_edit(self) -> None:
        """Enter DRL toggle editing mode."""
        self._editing_drl = True
        self._lights_frame.active = True
        self._drl_toggle.start_editing()
    
    def _exit_drl_edit(self) -> None:
        """Exit DRL toggle editing mode."""
        self._editing_drl = False
        self._lights_frame.active = False
        self._drl_toggle.stop_editing()
    
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
        """Handle ambient frame selection (enter toggle edit mode)."""
        self._enter_ambient_edit()
    
    def _on_ambient_action(self) -> None:
        """Handle ambient frame action (open ambient settings)."""
        # TODO: Open ambient settings screen
        pass
    
    def _on_climate_select(self) -> None:
        """Handle climate frame selection (go to climate screen)."""
        self._on_climate_action()
    
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
        """Handle lights frame selection (enter DRL toggle edit mode)."""
        self._enter_drl_edit()
    
    def _on_lights_action(self) -> None:
        """Handle lights frame action (open lights settings screen)."""
        # TODO: Open lights settings screen
        pass
