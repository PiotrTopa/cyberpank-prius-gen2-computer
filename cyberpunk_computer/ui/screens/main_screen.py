"""
Main dashboard screen.

The primary display showing Audio, Ambient, Climate, and Lights frames.
"""

import pygame
from typing import Tuple

from .base import Screen
from ..widgets.base import Rect
from ..widgets.frame import Frame
from ..widgets.controls import VolumeBar, ToggleSwitch, ValueDisplay
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
        self._drl_on = True
        self._biled_on = False
        
        # Create frames
        self._create_left_panels()
        self._create_right_panels()
        self._create_center_area()
    
    def _create_left_panels(self) -> None:
        """Create left side panels (Audio, Ambient, spare)."""
        x = 0
        
        # Audio frame
        audio_frame = Frame(
            Rect(x, 0, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="AUDIO",
            on_select=self._on_audio_select,
            on_action=self._on_audio_action
        )
        
        # Volume bar inside audio frame
        content = audio_frame.content_rect
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
        audio_frame.add_child(self._volume_bar)
        
        # Volume label
        self._volume_label = ValueDisplay(
            Rect(content.x, content.y, content.width, 30),
            label="VOL",
            value=str(self._volume),
            unit=""
        )
        audio_frame.add_child(self._volume_label)
        
        self.add_widget(audio_frame)
        
        # Ambient frame
        ambient_frame = Frame(
            Rect(x, self.FRAME_HEIGHT, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="AMBIENT",
            on_select=self._on_ambient_select,
            on_action=self._on_ambient_action
        )
        
        # ON/OFF toggle inside ambient frame
        content = ambient_frame.content_rect
        self._ambient_toggle = ToggleSwitch(
            Rect(
                content.x + (content.width - 50) // 2,
                content.y + (content.height - 20) // 2,
                50,
                20
            ),
            state=self._ambient_on
        )
        ambient_frame.add_child(self._ambient_toggle)
        
        self.add_widget(ambient_frame)
        
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
        climate_frame = Frame(
            Rect(x, 0, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="CLIMATE",
            on_select=self._on_climate_select
        )
        
        # Temperature displays inside climate frame
        content = climate_frame.content_rect
        third_width = content.width // 3
        
        self._temp_in_display = ValueDisplay(
            Rect(content.x, content.y, third_width, content.height),
            label="IN",
            value=self._temp_in,
            unit="°"
        )
        climate_frame.add_child(self._temp_in_display)
        
        self._temp_out_display = ValueDisplay(
            Rect(content.x + third_width, content.y, third_width, content.height),
            label="OUT",
            value=self._temp_out,
            unit="°"
        )
        climate_frame.add_child(self._temp_out_display)
        
        self._temp_target_display = ValueDisplay(
            Rect(content.x + third_width * 2, content.y, third_width, content.height),
            label="SET",
            value=self._temp_target,
            unit="°"
        )
        climate_frame.add_child(self._temp_target_display)
        
        self.add_widget(climate_frame)
        
        # Lights frame
        lights_frame = Frame(
            Rect(x, self.FRAME_HEIGHT, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="LIGHTS",
            on_select=self._on_lights_select
        )
        
        content = lights_frame.content_rect
        half_width = content.width // 2
        
        # DRL toggle
        self._drl_toggle = ToggleSwitch(
            Rect(content.x, content.y + 10, half_width - 4, 20),
            state=self._drl_on,
            on_text="DRL",
            off_text="DRL"
        )
        lights_frame.add_child(self._drl_toggle)
        
        # BiLED toggle
        self._biled_toggle = ToggleSwitch(
            Rect(content.x + half_width, content.y + 10, half_width - 4, 20),
            state=self._biled_on,
            on_text="LED",
            off_text="LED"
        )
        lights_frame.add_child(self._biled_toggle)
        
        self.add_widget(lights_frame)
        
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
    
    def _on_audio_select(self) -> None:
        """Handle audio frame selection (enter volume edit mode)."""
        # TODO: Enter volume adjustment mode
        print("Audio selected - enter volume mode")
    
    def _on_audio_action(self) -> None:
        """Handle audio frame action (open audio submenu)."""
        # TODO: Open audio settings screen
        print("Audio action - open audio screen")
    
    def _on_ambient_select(self) -> None:
        """Handle ambient frame selection (toggle on/off)."""
        self._ambient_on = self._ambient_toggle.toggle()
        print(f"Ambient toggled: {self._ambient_on}")
    
    def _on_ambient_action(self) -> None:
        """Handle ambient frame action (open ambient settings)."""
        # TODO: Open ambient settings screen
        print("Ambient action - open ambient screen")
    
    def _on_climate_select(self) -> None:
        """Handle climate frame selection."""
        # TODO: Open climate control
        print("Climate selected")
    
    def _on_lights_select(self) -> None:
        """Handle lights frame selection."""
        # TODO: Cycle through light controls
        print("Lights selected")
