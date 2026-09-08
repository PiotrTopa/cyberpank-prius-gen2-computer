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
from ..widgets.vehicle_status import ConnectionIndicator
from ..widgets.pagination import PaginationControl
# VFD widget removed - now runs as separate satellite app (device 110)
# See vfd_satellite/ and docs/VFD_SATELLITE_PROTOCOL.md
from ..colors import COLORS
from ..fonts import get_font, get_title_font, get_tiny_font, get_mono_font
from ...persistence import get_settings, save_settings
from ...state.actions import (
    ActionSource, SetVolumeAction, SetBassAction, SetMidAction, SetTrebleAction,
    SetBalanceAction, SetFaderAction, SetMuteAction,
    SetTargetTempAction, SetFanSpeedAction, SetACAction, SetAutoModeAction,
    SetRecirculationAction, SetAirDirectionAction
)


class MainScreen(Screen):
    """
    Main dashboard screen.
    
    Layout:
    ┌───────────┬─────────────────────────────┬───────────────────┐
    │  AUDIO    │                             │   CLIMATE         │
    │  120×80   │                             │   120×80          │
    ├───────────┤      CENTER AREA            ├───────────────────┤
    │  AMBIENT  │      (available)            │   LIGHTS          │
    │  120×80   │                             │   120×80          │
    ├───────────┤                             ├───────────────────┤
    │  ENGINE   │                             │   BATTERY         │
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
        self._temp_in = "N/A"  # Inside temp not available on AVC-LAN
        self._temp_out = "N/A"  # Updated from AVC-LAN 10C->310
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
        
        # State store
        self._store = None
        
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
        
        # Pagination
        self._current_page = 0
        self._num_pages = 2
        
        # Focus visibility tracking
        self._last_activity_time = time.time()
        
        # AVC Input visualization (touch and button events)
        self._last_touch_x = 0
        self._last_touch_y = 0
        self._last_touch_time = 0.0
        self._last_button_name = ""
        self._last_button_time = 0.0
        self._touch_display_duration = 1.0  # How long to show touch indicator
        self._button_display_duration = 2.0  # How long to show button text

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
        self.focus_manager.add_widget(self._vehicle_frame)
        self.focus_manager.add_widget(self._battery_frame)
        
        # Add pagination control to focus loop
        if hasattr(self, '_pagination_control'):
            self.focus_manager.add_widget(self._pagination_control)
    
    def _create_left_panels(self) -> None:
        """Create left side panels (Audio, Ambient, Engine)."""
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
            segments=10,
            show_value=False  # numeric value already shown by the VOL label above
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
        # Create compact toggle for title bar
        ambient_title_rect = Rect(
            x + self.SIDE_PANEL_WIDTH - 46, self.FRAME_HEIGHT + 4, 40, 14
        )
        self._ambient_toggle = ToggleSwitch(
            ambient_title_rect,
            state=self._ambient_on
        )
        self._ambient_frame = Frame(
            Rect(x, self.FRAME_HEIGHT, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="AMBIENT",
            on_select=self._on_ambient_select,
            on_action=self._on_ambient_action,
            title_widget=self._ambient_toggle
        )
        
        self.add_widget(self._ambient_frame)
        
        # Vehicle/Engine frame
        self._vehicle_frame = Frame(
            Rect(x, self.FRAME_HEIGHT * 2, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="ENGINE",
            focusable=True,
            on_action=self._on_engine_action,
            on_select=self._on_engine_menu_select
        )
        
        content = self._vehicle_frame.content_rect
        
        # 2x2 Grid
        h_half = content.height // 2
        w_half = content.width // 2
        
        self._rpm_display = ValueDisplay(
            Rect(content.x, content.y, w_half, h_half),
            label="RPM",
            value="0",
            unit="",
            compact=True
        )
        self._vehicle_frame.add_child(self._rpm_display)
        
        self._fuel_display = ValueDisplay(
            Rect(content.x + w_half, content.y, w_half, h_half),
            label="CONS",
            value="--.-",
            unit="L", # L/100
            compact=True
        )
        self._vehicle_frame.add_child(self._fuel_display)
        
        self._ice_temp_display = ValueDisplay(
            Rect(content.x, content.y + h_half, w_half, h_half),
            label="ICE",
            value="--",
            unit="°C",
            compact=True
        )
        self._vehicle_frame.add_child(self._ice_temp_display)
        
        self._inverter_temp_display = ValueDisplay(
            Rect(content.x + w_half, content.y + h_half, w_half, h_half),
            label="INV",
            value="--",
            unit="°C",
            compact=True
        )
        self._vehicle_frame.add_child(self._inverter_temp_display)
        
        self.add_widget(self._vehicle_frame)
    
    def _create_right_panels(self) -> None:
        """Create right side panels (Climate, Lights, Battery)."""
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
            active=self._climate_ac
        )
        self._climate_frame.add_child(self._ac_icon)
        
        self._auto_icon = ModeIcon(
            Rect(content.x + icon_width, icon_y, icon_width, icon_height),
            icon="auto",
            active=self._climate_auto
        )
        self._climate_frame.add_child(self._auto_icon)
        
        self._recirc_icon = ModeIcon(
            Rect(content.x + icon_width * 2, icon_y, icon_width, icon_height),
            icon="recirc",
            active=self._climate_recirc
        )
        self._climate_frame.add_child(self._recirc_icon)
        
        self.add_widget(self._climate_frame)
        
        # Lights frame
        lights_title_rect = Rect(
            x + self.SIDE_PANEL_WIDTH - 46, self.FRAME_HEIGHT + 4, 40, 14
        )
        self._lights_toggle = ToggleSwitch(
            lights_title_rect,
            state=self._lights_mode != "OFF",
            on_text=self._lights_mode if self._lights_mode != "OFF" else "AUTO",
            off_text="OFF"
        )
        self._lights_frame = Frame(
            Rect(x, self.FRAME_HEIGHT, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="LIGHTS",
            on_select=self._on_lights_select,
            on_action=self._on_lights_action,
            title_widget=self._lights_toggle
        )
        
        content = self._lights_frame.content_rect
        
        # Status indicators in a row (full content area now available)
        third_width = content.width // 3
        status_y = content.y
        status_height = content.height
        
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
        
        # Battery frame (bottom right)
        self._battery_frame = Frame(
            Rect(x, self.FRAME_HEIGHT * 2, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="BATTERY",
            focusable=True,
            on_action=self._on_battery_action,
            on_select=self._on_battery_action
        )
        
        content = self._battery_frame.content_rect
        third_height = content.height // 3
        half_width = content.width // 2
        
        # Row 1: Power (kW) - full width, most important
        self._batt_power_display = ValueDisplay(
            Rect(content.x, content.y, content.width, third_height),
            label="",
            value="--.-",
            unit="kW",
            compact=True,
            value_size=16  # Slightly larger
        )
        self._battery_frame.add_child(self._batt_power_display)
        
        # Row 2: Voltage and Current side by side
        self._batt_volt_display = ValueDisplay(
            Rect(content.x, content.y + third_height, half_width, third_height),
            label="",
            value="---",
            unit="V",
            compact=True,
            value_size=12  # Slightly smaller
        )
        self._battery_frame.add_child(self._batt_volt_display)
        
        self._batt_curr_display = ValueDisplay(
            Rect(content.x + half_width, content.y + third_height, half_width, third_height),
            label="",
            value="--",
            unit="A",
            compact=True,
            value_size=12  # Slightly smaller
        )
        self._battery_frame.add_child(self._batt_curr_display)
        
        # Row 3: Temperature with SOC
        self._batt_temp_display = ValueDisplay(
            Rect(content.x, content.y + third_height * 2, half_width, third_height),
            label="",
            value="--",
            unit="°C",
            compact=True
        )
        self._battery_frame.add_child(self._batt_temp_display)
        
        self._batt_soc_display = ValueDisplay(
            Rect(content.x + half_width, content.y + third_height * 2, half_width, third_height),
            label="",
            value="--",
            unit="%",
            compact=True
        )
        self._battery_frame.add_child(self._batt_soc_display)
        
        self.add_widget(self._battery_frame)
    
    # Bottom bar height for cruise + pagination
    BOTTOM_BAR_HEIGHT = 16
    
    def _create_center_area(self) -> None:
        """Create center area with connection indicator and status bar."""
        center_x = self.SIDE_PANEL_WIDTH
        center_width = self.width - 2 * self.SIDE_PANEL_WIDTH
        
        # Connection indicator (top-right)
        self._connection_indicator = ConnectionIndicator(
            Rect(center_x + center_width - 16, 6, 12, 12)
        )
        self.add_widget(self._connection_indicator)
        
        # Top Status Bar: Gear | Speed | Connection
        
        # Gear Display (left of status bar)
        self._gear_display = ValueDisplay(
            Rect(center_x + 10, 0, 30, 25),
            label="",
            value="P",
            unit="",
            compact=True,
            value_size=16
        )
        self.add_widget(self._gear_display)
        
        # Speed Display (right of gear)
        self._speed_display = ValueDisplay(
            Rect(center_x + 45, 0, 55, 25),
            label="",
            value="0",
            unit="km/h",
            compact=True,
            value_size=14
        )
        self.add_widget(self._speed_display)
        
        # Pagination Control (bottom bar, right side)
        bottom_y = self.height - self.BOTTOM_BAR_HEIGHT
        self._pagination_control = PaginationControl(
            Rect(center_x + center_width - 60, bottom_y, 60, self.BOTTOM_BAR_HEIGHT),
            num_pages=self._num_pages,
            current_page=self._current_page,
            on_change=self._on_page_change
        )
        self.add_widget(self._pagination_control)
        
        # Center Content Area (Pages) - between top bar and bottom bar
        self._content_rect = Rect(
            center_x, 30, center_width, self.height - 30 - self.BOTTOM_BAR_HEIGHT
        )
        
        # VFD Display has been moved to separate satellite app (device 110)
        # Page 1 now shows placeholder indicating VFD is on external display
        # See: vfd_satellite/ package and docs/VFD_SATELLITE_PROTOCOL.md

    def _on_page_change(self, page_index: int) -> None:
        """Handle page change."""
        self._current_page = page_index
        # Verify page index valid (though control handles it)
        self._current_page = max(0, min(self._current_page, self._num_pages - 1))
        
        # Page visibility is handled in render()

    
    def set_store(self, store) -> None:
        """
        Connect state store for live updates.
        
        Args:
            store: State Store instance
        """
        from ...state.store import StateSlice
        
        self._store = store
        
        # Subscribe to all state changes
        store.subscribe(StateSlice.ALL, self._on_store_update)
    
    # Gear enum -> display letter
    _GEAR_LETTERS = {
        "PARK": "P", "REVERSE": "R", "NEUTRAL": "N", "DRIVE": "D", "B": "B",
    }

    def _gear_letter(self, gear) -> str:
        return self._GEAR_LETTERS.get(getattr(gear, "name", ""), "P")

    def _on_store_update(self, state) -> None:
        """Handle state update from Store."""
        # Audio
        self._volume = state.audio.volume
        self._volume_bar.set_value(state.audio.volume)
        self._volume_label.set_value(str(state.audio.volume))

        # Climate
        self._temp_target = f"{state.climate.target_temp:.0f}"
        self._temp_in = (
            f"{state.climate.inside_temp:.0f}"
            if state.climate.inside_temp is not None else "N/A"
        )
        self._temp_out = (
            f"{state.climate.outside_temp:.0f}"
            if state.climate.outside_temp is not None else "N/A"
        )
        self._climate_ac = state.climate.ac_on
        self._climate_auto = state.climate.auto_mode
        self._climate_recirc = state.climate.recirculation

        self._temp_target_display.set_value(self._temp_target)
        self._temp_in_display.set_value(self._temp_in)
        self._temp_out_display.set_value(self._temp_out)
        self._ac_icon.set_active(self._climate_ac)
        self._auto_icon.set_active(self._climate_auto)
        self._recirc_icon.set_active(self._climate_recirc)

        # Gear / speed (top bar, hidden on the home page)
        self._gear_display.set_value(self._gear_letter(state.vehicle.gear))
        speed = state.vehicle.speed_kmh
        self._speed_display.set_value(str(int(speed)) if speed is not None else "0")

        # Engine telemetry
        rpm_val = state.vehicle.rpm
        self._rpm_display.set_value(str(int(rpm_val)) if rpm_val is not None else "0")
        ice_t = state.vehicle.ice_coolant_temp
        self._ice_temp_display.set_value(str(int(ice_t)) if ice_t is not None else "--")

        v = state.vehicle
        hybrid_temps = [t for t in (
            v.converter_temp, v.mg1_inverter_temp, v.mg2_inverter_temp,
            v.mg1_motor_temp, v.mg2_motor_temp,
        ) if t is not None]
        self._inverter_temp_display.set_value(
            str(int(max(hybrid_temps))) if hybrid_temps else "--")

        consumption = v.instant_consumption
        self._fuel_display.set_value(
            f"{consumption:.1f}" if consumption > 0.0 else "--.-")
        self._fuel_display.set_label(v.consumption_unit)

        # Battery telemetry
        power_kw = state.energy.battery_power_kw
        if power_kw is not None:
            # Show sign: + for discharge, - for charge
            val = f"{power_kw:+.1f}" if abs(power_kw) >= 0.1 else "0.0"
        else:
            val = "--.-"
        self._batt_power_display.set_value(val)

        volt = state.energy.hv_battery_voltage
        self._batt_volt_display.set_value(f"{volt:.0f}" if volt is not None else "---")
        curr = state.energy.hv_battery_current
        self._batt_curr_display.set_value(f"{curr:.0f}" if curr is not None else "--")
        batt_t = state.energy.battery_temp
        self._batt_temp_display.set_value(str(int(batt_t)) if batt_t is not None else "--")
        soc = state.energy.battery_soc
        self._batt_soc_display.set_value(str(int(soc * 100)) if soc > 0 else "--")

        # Connection
        self._connection_indicator.set_connected(state.connection.connected)

        # AVC input visualization (touch and button events)
        if state.input.last_touch_time > self._last_touch_time:
            self._last_touch_x = state.input.last_touch_x
            self._last_touch_y = state.input.last_touch_y
            self._last_touch_time = state.input.last_touch_time
        if state.input.last_button_time > self._last_button_time:
            self._last_button_name = state.input.last_button_name
            self._last_button_time = state.input.last_button_time

        self._dirty = True
    
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
        # The home page draws its own large gear/speed readout; the small
        # top-bar duplicates are only useful on the other pages.
        on_home = self._current_page == 0
        self._gear_display.visible = not on_home
        self._speed_display.visible = not on_home

        # Render all widgets
        super().render(surface)

        center_x = self.SIDE_PANEL_WIDTH
        center_width = self.width - self.SIDE_PANEL_WIDTH * 2

        # Subtle border for center area
        pygame.draw.rect(
            surface,
            COLORS["border_normal"],
            (center_x, 0, center_width, self.height),
            1
        )

        # Render page-specific content
        if self._current_page == 1:
            self._render_dynamics_page(surface, center_x, center_width)
        else:
            self._render_home_page(surface, center_x, center_width)

        # Render bottom status bar (cruise control + pagination)
        self._render_bottom_bar(surface, center_x, center_width)

        # Render AVC input visualization (dev/debug aid only)
        if self.app and getattr(self.app, "config", None) and self.app.config.dev_mode:
            self._render_avc_input_visualization(surface, center_x, center_width)
    
    def _render_bottom_bar(
        self, surface: pygame.Surface, center_x: int, center_width: int
    ) -> None:
        """Render bottom status bar with cruise control info.
        
        Layout: [CRU SET:80 MEM:80 ENGAGED] .............. [● ○]
        """
        bar_y = self.height - self.BOTTOM_BAR_HEIGHT
        
        # Separator line
        pygame.draw.line(
            surface,
            COLORS["border_normal"],
            (center_x + 1, bar_y),
            (center_x + center_width - 2, bar_y),
            1
        )
        
        if not self._store:
            return
        
        v = self._store.state.vehicle
        font = get_font(8, "mono")
        text_y = bar_y + 3
        x = center_x + 6
        
        # Cruise active indicator
        if v.cruise_active:
            indicator = "CRU"
            ind_color = COLORS["green_bright"]
        elif v.cruise_main_switch:
            indicator = "CRU"
            ind_color = COLORS["yellow"]
        else:
            indicator = "CRU"
            ind_color = COLORS["text_dim"]
        
        ind_surf = font.render(indicator, True, ind_color)
        surface.blit(ind_surf, (x, text_y))
        x += ind_surf.get_width() + 3
        
        # Set speed
        if v.cruise_set_speed is not None and v.cruise_set_speed > 0:
            set_str = f"SET:{v.cruise_set_speed}"
            set_color = COLORS["green_bright"] if v.cruise_active else COLORS["text_secondary"]
        else:
            set_str = "SET:--"
            set_color = COLORS["text_dim"]
        set_surf = font.render(set_str, True, set_color)
        surface.blit(set_surf, (x, text_y))
        x += set_surf.get_width() + 4
        
        # Memory speed
        if v.cruise_memory_speed is not None and v.cruise_memory_speed > 0:
            mem_str = f"MEM:{v.cruise_memory_speed}"
            mem_color = COLORS["cyan_bright"] if v.cruise_active else COLORS["text_secondary"]
        else:
            mem_str = "MEM:--"
            mem_color = COLORS["text_dim"]
        mem_surf = font.render(mem_str, True, mem_color)
        surface.blit(mem_surf, (x, text_y))
        x += mem_surf.get_width() + 4
        
        # Engaged/Off status text
        if v.cruise_active:
            status_surf = font.render("ON", True, COLORS["green_bright"])
            surface.blit(status_surf, (x, text_y))
    
    # Battery power range shown on the home page power bar (kW).
    HOME_POWER_RANGE_KW = 25.0

    def _render_home_page(self, surface: pygame.Surface, center_x: int, center_width: int) -> None:
        """Render Page 1: drive dashboard (speed, gear, power flow, SOC, fuel)."""
        state = self._store.state if self._store else None
        mid_x = center_x + center_width // 2

        # ── Speed (unit drawn beside the digits) ──
        speed = 0
        if state and state.vehicle.speed_kmh is not None:
            speed = int(state.vehicle.speed_kmh)
        font_speed = get_title_font(24)
        speed_surf = font_speed.render(str(speed), True, COLORS["text_highlight"])
        unit_surf = get_tiny_font(8).render("km/h", True, COLORS["text_secondary"])
        speed_y = 34
        speed_x = mid_x - (speed_surf.get_width() + 4 + unit_surf.get_width()) // 2
        surface.blit(speed_surf, (speed_x, speed_y))
        surface.blit(
            unit_surf,
            (speed_x + speed_surf.get_width() + 4,
             speed_y + speed_surf.get_height() - unit_surf.get_height() - 4)
        )

        # ── Gear strip: P R N D B ──
        gear_letter = self._gear_letter(state.vehicle.gear) if state else "P"
        font_gear = get_mono_font(14)
        gears = ["P", "R", "N", "D", "B"]
        step = 26
        gx = mid_x - (step * (len(gears) - 1)) // 2
        gy = speed_y + speed_surf.get_height() + 18
        for g in gears:
            active = g == gear_letter
            color = COLORS["cyan_bright"] if active else COLORS["text_dim"]
            g_surf = font_gear.render(g, True, color)
            g_rect = g_surf.get_rect(center=(gx, gy))
            if active:
                box = g_rect.inflate(8, 4)
                pygame.draw.rect(surface, COLORS["cyan_dark"], box)
                pygame.draw.rect(surface, COLORS["cyan_mid"], box, 1)
            surface.blit(g_surf, g_rect)
            gx += step

        # ── Battery power bar: charge (left) <- 0 -> discharge (right) ──
        bar_y = gy + 28
        bar_w = center_width - 48
        bar_x = center_x + (center_width - bar_w) // 2
        bar_h = 10
        power_kw = state.energy.battery_power_kw if state else None

        pygame.draw.rect(surface, COLORS["bg_panel"], (bar_x, bar_y, bar_w, bar_h))
        pygame.draw.rect(surface, COLORS["border_normal"], (bar_x, bar_y, bar_w, bar_h), 1)
        half_w = bar_w // 2
        if power_kw is not None and abs(power_kw) >= 0.1:
            frac = max(-1.0, min(1.0, power_kw / self.HOME_POWER_RANGE_KW))
            fill = int(abs(frac) * (half_w - 2))
            if frac >= 0:  # discharge: battery -> wheels
                pygame.draw.rect(
                    surface, COLORS["cyan_mid"],
                    (bar_x + half_w, bar_y + 2, fill, bar_h - 4))
            else:  # charge / regen
                pygame.draw.rect(
                    surface, COLORS["green_bright"],
                    (bar_x + half_w - fill, bar_y + 2, fill, bar_h - 4))
        # Center zero marker
        pygame.draw.line(
            surface, COLORS["text_secondary"],
            (bar_x + half_w, bar_y - 2), (bar_x + half_w, bar_y + bar_h + 1))

        font_tiny = get_tiny_font(8)
        chg_surf = font_tiny.render("CHG", True, COLORS["text_secondary"])
        surface.blit(chg_surf, (bar_x, bar_y + bar_h + 3))
        pwr_surf = font_tiny.render("PWR", True, COLORS["text_secondary"])
        surface.blit(pwr_surf, (bar_x + bar_w - pwr_surf.get_width(), bar_y + bar_h + 3))
        kw_text = f"{power_kw:+.1f} kW" if power_kw is not None else "--.- kW"
        kw_surf = font_tiny.render(kw_text, True, COLORS["text_value"])
        surface.blit(kw_surf, (mid_x - kw_surf.get_width() // 2, bar_y + bar_h + 3))

        # ── SOC bar (8 segments, mirrors the factory MFD bars) ──
        soc_y = bar_y + bar_h + 24
        soc = state.energy.battery_soc if state else 0.0
        soc_pct = int(soc * 100)
        lbl_surf = font_tiny.render("SOC", True, COLORS["text_secondary"])
        surface.blit(lbl_surf, (bar_x, soc_y + 1))
        seg_area_x = bar_x + 24
        seg_area_w = bar_w - 24 - 30
        self._draw_segment_bar(
            surface, seg_area_x, soc_y, seg_area_w, 9, 8, soc,
            fill_color=self._soc_color(soc_pct))
        pct_surf = font_tiny.render(f"{soc_pct}%", True, COLORS["text_value"])
        surface.blit(pct_surf, (bar_x + bar_w - pct_surf.get_width(), soc_y + 1))

        # ── Fuel levels ──
        if state:
            fuel_y = soc_y + 22
            active_fuel = getattr(state.vehicle.active_fuel, "name", "OFF")
            for label, liters, x_pos in (
                ("PET", state.vehicle.fuel_level, bar_x),
                ("LPG", state.vehicle.lpg_level, mid_x + 12),
            ):
                is_active = active_fuel.startswith(label[:1] if label == "PET" else label)
                color = COLORS["text_value"] if is_active else COLORS["text_dim"]
                text = f"{label} {liters}L"
                f_surf = font_tiny.render(text, True, color)
                surface.blit(f_surf, (x_pos, fuel_y))

    @staticmethod
    def _soc_color(soc_pct: int):
        """Color for SOC level: green when healthy, amber low, red critical."""
        if soc_pct >= 45:
            return COLORS["green_bright"]
        if soc_pct >= 30:
            return COLORS["yellow"]
        return COLORS["red_bright"]

    @staticmethod
    def _draw_segment_bar(
        surface, x, y, width, height, segments, fraction, fill_color
    ) -> None:
        """Draw a segmented level bar (replaces block-glyph text bars)."""
        gap = 2
        seg_w = (width - gap * (segments - 1)) / segments
        filled = round(fraction * segments)
        for i in range(segments):
            seg_x = int(x + i * (seg_w + gap))
            rect = pygame.Rect(seg_x, y, int(seg_w), height)
            if i < filled:
                pygame.draw.rect(surface, fill_color, rect)
            else:
                pygame.draw.rect(surface, COLORS["bg_panel"], rect)
                pygame.draw.rect(surface, COLORS["border_normal"], rect, 1)
    
    def _render_dynamics_page(self, surface: pygame.Surface, center_x: int, center_width: int) -> None:
        """Render Page 2: Vehicle Dynamics dashboard.
        
        Shows steering angle, accelerations, yaw rate, wheel pulses,
        headlight status, SOC bars, and EV mode.
        """
        if not self._store:
            self._render_default_page(surface, center_x, center_width)
            return
        
        dyn = self._store.state.dynamics
        font_label = get_font(8)
        font_value = get_font(11, "mono")
        font_title = get_font(10, "title")
        font_small = get_font(7)
        
        # Layout: left column and right column within content area (below status bar)
        cr = self._content_rect
        pad = 6
        col_width = (center_width - pad * 3) // 2
        left_x = center_x + pad
        right_x = center_x + pad * 2 + col_width
        y = cr.y + 2
        row_h = 13  # Row height for compact layout
        
        # ─── LEFT COLUMN: Motion sensors ───
        
        # Title
        title_surf = font_title.render("MOTION", True, COLORS["cyan_bright"])
        surface.blit(title_surf, (left_x, y))
        y += row_h + 2
        
        # Steering Angle
        lbl = font_label.render("STEER", True, COLORS["text_secondary"])
        surface.blit(lbl, (left_x, y))
        if dyn.steering_angle is not None:
            angle = dyn.steering_angle
            # Color: green near center, yellow/red at extremes
            if abs(angle) < 5:
                color = COLORS["green_bright"]
            elif abs(angle) < 30:
                color = COLORS["yellow"]
            else:
                color = COLORS["red_bright"]
            val_text = f"{angle:+6.1f}\xb0"
        else:
            color = COLORS["text_dim"]
            val_text = "  --.-\xb0"
        val_surf = font_value.render(val_text, True, color)
        surface.blit(val_surf, (left_x + col_width - val_surf.get_width(), y))
        y += row_h
        
        # Lateral Acceleration
        lbl = font_label.render("LAT G", True, COLORS["text_secondary"])
        surface.blit(lbl, (left_x, y))
        if dyn.lateral_accel_raw is not None:
            val_text = f"{dyn.lateral_accel_raw:+5d}"
            color = COLORS["green_bright"] if abs(dyn.lateral_accel_raw) < 50 else COLORS["yellow"]
        else:
            val_text = "   --"
            color = COLORS["text_dim"]
        val_surf = font_value.render(val_text, True, color)
        surface.blit(val_surf, (left_x + col_width - val_surf.get_width(), y))
        y += row_h
        
        # Longitudinal Acceleration
        lbl = font_label.render("LON G", True, COLORS["text_secondary"])
        surface.blit(lbl, (left_x, y))
        if dyn.longitudinal_accel_raw is not None:
            val_text = f"{dyn.longitudinal_accel_raw:+5d}"
            color = COLORS["green_bright"] if abs(dyn.longitudinal_accel_raw) < 50 else COLORS["yellow"]
        else:
            val_text = "   --"
            color = COLORS["text_dim"]
        val_surf = font_value.render(val_text, True, color)
        surface.blit(val_surf, (left_x + col_width - val_surf.get_width(), y))
        y += row_h
        
        # Yaw Rate
        lbl = font_label.render("YAW", True, COLORS["text_secondary"])
        surface.blit(lbl, (left_x, y))
        if dyn.yaw_rate_raw is not None:
            val_text = f"{dyn.yaw_rate_raw:+5d}"
            color = COLORS["green_bright"] if abs(dyn.yaw_rate_raw) < 30 else COLORS["yellow"]
        else:
            val_text = "   --"
            color = COLORS["text_dim"]
        val_surf = font_value.render(val_text, True, color)
        surface.blit(val_surf, (left_x + col_width - val_surf.get_width(), y))
        y += row_h + 4
        
        # ─── LEFT COLUMN: Wheel Pulses ───
        
        title_surf = font_title.render("WHEELS", True, COLORS["cyan_bright"])
        surface.blit(title_surf, (left_x, y))
        
        # "km/h" unit label
        unit_surf = font_small.render("km/h", True, COLORS["text_dim"])
        surface.blit(unit_surf, (left_x + col_width - unit_surf.get_width(), y + 2))
        y += row_h + 2
        
        # Front wheels
        lbl = font_label.render("FR", True, COLORS["text_secondary"])
        surface.blit(lbl, (left_x, y))
        fr_text = f"{dyn.front_right_speed:5.1f}"
        fl_text = f"{dyn.front_left_speed:5.1f}"
        val_surf = font_value.render(f"R{fr_text} L{fl_text}", True, COLORS["green_bright"])
        surface.blit(val_surf, (left_x + col_width - val_surf.get_width(), y))
        y += row_h
        
        # Rear wheels
        lbl = font_label.render("RR", True, COLORS["text_secondary"])
        surface.blit(lbl, (left_x, y))
        rr_text = f"{dyn.rear_right_speed:5.1f}"
        rl_text = f"{dyn.rear_left_speed:5.1f}"
        val_surf = font_value.render(f"R{rr_text} L{rl_text}", True, COLORS["green_bright"])
        surface.blit(val_surf, (left_x + col_width - val_surf.get_width(), y))
        y += row_h
        
        # ─── RIGHT COLUMN: Status ───
        
        ry = cr.y + 2
        title_surf = font_title.render("STATUS", True, COLORS["cyan_bright"])
        surface.blit(title_surf, (right_x, ry))
        ry += row_h + 2
        
        # Headlight State
        lbl = font_label.render("LIGHTS", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        hl_state = dyn.headlight_state
        hl_colors = {
            "HIGH": COLORS["blue_bright"],
            "LOW": COLORS["green_bright"],
            "PARK": COLORS["yellow"],
        }
        hl_color = hl_colors.get(hl_state, COLORS["text_dim"])
        val_surf = font_value.render(hl_state, True, hl_color)
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
        ry += row_h
        
        # DRL indicator
        lbl = font_label.render("DRL", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        if dyn.drl_active:
            val_surf = font_value.render("ON", True, COLORS["green_bright"])
        else:
            val_surf = font_value.render("OFF", True, COLORS["text_dim"])
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
        ry += row_h
        
        # Parking lights
        lbl = font_label.render("PARK", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        if dyn.parking_lights:
            val_surf = font_value.render("ON", True, COLORS["yellow"])
        else:
            val_surf = font_value.render("OFF", True, COLORS["text_dim"])
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
        ry += row_h + 4
        
        # ─── RIGHT COLUMN: Battery Bars ───
        
        title_surf = font_title.render("BATTERY", True, COLORS["cyan_bright"])
        surface.blit(title_surf, (right_x, ry))
        ry += row_h + 2
        
        # SOC Bars visualization (0-8 bars)
        lbl = font_label.render("SOC BAR", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        bars = dyn.soc_bars
        if bars >= 6:
            bar_color = COLORS["green_bright"]
        elif bars >= 3:
            bar_color = COLORS["yellow"]
        else:
            bar_color = COLORS["red_bright"]
        cnt_surf = font_value.render(str(bars), True, bar_color)
        cnt_x = right_x + col_width - cnt_surf.get_width()
        surface.blit(cnt_surf, (cnt_x, ry))
        self._draw_segment_bar(
            surface, cnt_x - 60, ry + 2, 54, 8, 8, bars / 8.0, bar_color)
        ry += row_h
        
        # EV Mode
        lbl = font_label.render("EV MODE", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        if dyn.ev_mode_active:
            val_surf = font_value.render("ACTIVE", True, COLORS["green_bright"])
        else:
            val_surf = font_value.render("--", True, COLORS["text_dim"])
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
        ry += row_h
        
        # Warning Triangle
        if dyn.warning_triangle:
            lbl = font_label.render("! WARNING", True, COLORS["red_bright"])
            surface.blit(lbl, (right_x, ry))
        ry += row_h
    
    def _render_avc_input_visualization(
        self, 
        surface: pygame.Surface, 
        center_x: int, 
        center_width: int
    ) -> None:
        """
        Render AVC-LAN input events (touch and button) for debugging.
        
        Shows:
        - Touch events as a crosshair in the center area
        - Button names as text at the bottom
        """
        current_time = time.time()
        
        # Draw touch indicator if recent touch event
        touch_age = current_time - self._last_touch_time
        if self._last_touch_time > 0 and 0 <= touch_age < self._touch_display_duration:
            # Calculate alpha fade (1.0 -> 0.0)
            alpha = max(0.0, min(1.0, 1.0 - (touch_age / self._touch_display_duration)))
            
            # Map touch coordinates (0-255) to center area
            # Touch area is in center: center_x to center_x + center_width
            touch_screen_x = center_x + int((self._last_touch_x / 255.0) * center_width)
            touch_screen_y = int((self._last_touch_y / 255.0) * self.height)
            
            # Clamp to center area
            touch_screen_x = max(center_x, min(center_x + center_width, touch_screen_x))
            touch_screen_y = max(0, min(self.height, touch_screen_y))
            
            # Draw crosshair
            color = (0, int(255 * alpha), int(255 * alpha))  # Cyan with fade
            line_len = 15
            
            # Horizontal line
            pygame.draw.line(
                surface, color,
                (touch_screen_x - line_len, touch_screen_y),
                (touch_screen_x + line_len, touch_screen_y),
                2
            )
            # Vertical line
            pygame.draw.line(
                surface, color,
                (touch_screen_x, touch_screen_y - line_len),
                (touch_screen_x, touch_screen_y + line_len),
                2
            )
            # Circle in center
            pygame.draw.circle(surface, color, (touch_screen_x, touch_screen_y), 5, 1)
            
            # Draw coordinate text
            coord_font = get_font(9)
            coord_text = f"TOUCH: {self._last_touch_x},{self._last_touch_y}"
            coord_surf = coord_font.render(coord_text, True, color)
            coord_x = center_x + (center_width - coord_surf.get_width()) // 2
            coord_y = self.height - 45
            surface.blit(coord_surf, (coord_x, coord_y))
        
        # Draw button text if recent button event
        button_age = current_time - self._last_button_time
        if self._last_button_time > 0 and 0 <= button_age < self._button_display_duration:
            # Calculate alpha fade
            alpha = max(0.0, min(1.0, 1.0 - (button_age / self._button_display_duration)))
            color = (int(255 * alpha), int(200 * alpha), 0)  # Yellow/orange with fade
            
            btn_font = get_font(12, "title")
            btn_text = f"BTN: {self._last_button_name}"
            btn_surf = btn_font.render(btn_text, True, color)
            btn_x = center_x + (center_width - btn_surf.get_width()) // 2
            btn_y = self.height - 25
            surface.blit(btn_surf, (btn_x, btn_y))
    
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
        
        # Dispatch action to Store -> Gateway
        if self._store:
            self._store.dispatch(SetVolumeAction(self._volume, source=ActionSource.UI))
    
    def _adjust_target_temp(self, delta: int) -> None:
        """Adjust target temperature by delta."""
        new_temp = int(self._temp_target) + delta
        new_temp = max(16, min(28, new_temp))
        self._temp_target = str(new_temp)
        self._temp_target_display.set_value(self._temp_target)
        
        # Dispatch action to Store -> Gateway
        if self._store:
            self._store.dispatch(SetTargetTempAction(float(new_temp), source=ActionSource.UI))
    
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
            
            # Connect Store for value changes (dispatches actions to gateway)
            if self._store:
                store = self._store  # Capture for closure
                
                # When user changes value in AudioScreen, dispatch to Store
                def on_audio_value_changed(label: str, value) -> None:
                    if label == "VOLUME":
                        store.dispatch(SetVolumeAction(value, source=ActionSource.UI))
                    elif label == "BASS":
                        store.dispatch(SetBassAction(value, source=ActionSource.UI))
                    elif label == "MID":
                        store.dispatch(SetMidAction(value, source=ActionSource.UI))
                    elif label == "TREBLE":
                        store.dispatch(SetTrebleAction(value, source=ActionSource.UI))
                    elif label == "BALANCE":
                        store.dispatch(SetBalanceAction(value, source=ActionSource.UI))
                    elif label == "FADER":
                        store.dispatch(SetFaderAction(value, source=ActionSource.UI))
                    
                audio_screen.set_on_value_changed(on_audio_value_changed)
                
                # Sync current state from Store
                state = store.state
                audio_screen.set_value_from_avc("VOLUME", state.audio.volume)
                audio_screen.set_value_from_avc("BASS", state.audio.bass)
                audio_screen.set_value_from_avc("MID", state.audio.mid)
                audio_screen.set_value_from_avc("TREBLE", state.audio.treble)
                audio_screen.set_value_from_avc("BALANCE", state.audio.balance)
                audio_screen.set_value_from_avc("FADER", state.audio.fader)
            
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
            # Parse temp values safely (could be "N/A" or numeric strings)
            try:
                temp_out = int(self._temp_out)
            except (ValueError, TypeError):
                temp_out = 0  # Default if not available
            try:
                temp_in = int(self._temp_in)
            except (ValueError, TypeError):
                temp_in = 0
            
            climate_screen = ClimateScreen(
                (self.width, self.height),
                self.app,
                temp_target=int(self._temp_target),
                temp_in=temp_in,
                temp_out=temp_out,
                ac_on=self._climate_ac,
                auto_mode=self._climate_auto,
                recirc=self._climate_recirc
            )
            
            # Connect Store for value changes (dispatches actions to gateway)
            if self._store:
                store = self._store  # Capture for closure
                
                # When user changes value in ClimateScreen, dispatch to Store
                def on_climate_value_changed(label: str, value) -> None:
                    if label == "TARGET TEMP":
                        store.dispatch(SetTargetTempAction(float(value), source=ActionSource.UI))
                    elif label == "FAN SPEED":
                        store.dispatch(SetFanSpeedAction(value, source=ActionSource.UI))
                    elif label == "A/C":
                        # value is 0=ON, 1=OFF, convert to bool
                        store.dispatch(SetACAction(value == 0, source=ActionSource.UI))
                    elif label == "MODE":
                        # value is 0=AUTO, 1=MANUAL, 2=OFF
                        store.dispatch(SetAutoModeAction(value == 0, source=ActionSource.UI))
                    elif label == "AIR INTAKE":
                        # value is 0=FRESH, 1=RECIRC
                        store.dispatch(SetRecirculationAction(value == 1, source=ActionSource.UI))
                    elif label == "AIR DIRECTION":
                        # value is 0=FACE, 1=FACE+FEET, 2=FEET, 3=DEFROST
                        store.dispatch(SetAirDirectionAction(value, source=ActionSource.UI))
                
                climate_screen.set_on_value_changed(on_climate_value_changed)
            
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
    
    def _on_engine_action(self) -> None:
        """Handle engine frame strong press (open engine detail diagnostic)."""
        if not self.app:
            return

        from .engine_detail_screen import EngineDetailScreen

        engine_detail = EngineDetailScreen(
            (self.width, self.height),
            self.app,
            store=self._store
        )
        self.app.push_screen(engine_detail)

    def _on_engine_menu_select(self) -> None:
        """Handle engine frame light press (open engine settings menu)."""
        if not self.app:
            return

        from .engine_menu_screen import EngineMenuScreen

        current_timebase = 60
        if self._store:
            current_timebase = self._store.state.display.power_chart_time_base

        engine_menu = EngineMenuScreen(
            (self.width, self.height),
            self.app,
            store=self._store,
            initial_timebase=current_timebase,
        )
        self.app.push_screen(engine_menu)

    def _on_battery_action(self) -> None:
        """Handle battery frame strong press (open battery diagnostic)."""
        if not self.app:
            return

        from .battery_screen import BatteryScreen

        battery_screen = BatteryScreen(
            (self.width, self.height),
            self.app,
            store=self._store
        )
        self.app.push_screen(battery_screen)
