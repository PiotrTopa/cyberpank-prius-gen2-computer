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
from ..widgets.vfd_display import VFDDisplayWidget, VFD_WIDTH, VFD_HEIGHT
from ..colors import COLORS
from ..fonts import get_font
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
        
        # AVC bridge and store
        self._avc_bridge = None
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
        # Excluded: self.focus_manager.add_widget(self._vehicle_frame)
        # Excluded: self.focus_manager.add_widget(self._battery_frame)
        
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
        
        # Vehicle/Engine frame
        self._vehicle_frame = Frame(
            Rect(x, self.FRAME_HEIGHT * 2, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="ENGINE",
            focusable=True
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
        
        self._speed_display = ValueDisplay(
            Rect(content.x + w_half, content.y + h_half, w_half, h_half),
            label="SPD",
            value="--",
            unit="km",
            compact=True
        )
        self._vehicle_frame.add_child(self._speed_display)
        
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
        
        # Battery frame (bottom right)
        self._battery_frame = Frame(
            Rect(x, self.FRAME_HEIGHT * 2, self.SIDE_PANEL_WIDTH, self.FRAME_HEIGHT),
            title="BATTERY",
            focusable=True
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
    
    def _update_center_widgets(self) -> None:
        """Update center area widgets with current state."""
        # Update connection indicator
        if self._avc_bridge:
             connected = self._avc_bridge.is_connected
             self._connection_indicator.set_connected(connected)
    
    def _create_center_area(self) -> None:
        """Create center area with connection indicator and status bar."""
        center_x = self.SIDE_PANEL_WIDTH
        center_width = self.width - 2 * self.SIDE_PANEL_WIDTH
        
        # Connection indicator (moved slightly)
        self._connection_indicator = ConnectionIndicator(
            Rect(center_x + center_width - 16, 6, 12, 12)
        )
        self.add_widget(self._connection_indicator)
        
        # Status Bar Frame (Visual Only for now, or simple displays)
        # Using ValueDisplay for clock for now, positioned top center
        
        clock_width = 100
        self._clock_display = ValueDisplay(
            Rect(center_x + (center_width - clock_width) // 2, 0, clock_width, 25),
            label="",
            value="12:00",
            unit="",
            compact=True
        )
        self.add_widget(self._clock_display)
        
        # Gear Display (Left of clock)
        self._gear_display = ValueDisplay(
            Rect(center_x + 10, 0, 40, 25),
            label="",
            value="P",
            unit="",
            compact=True,
            value_size=16
        )
        self.add_widget(self._gear_display)
        
        # Center Content Area (Pages)
        self._content_rect = Rect(center_x, 30, center_width, self.height - 30 - 30)
        
        # Initial pages labels
        self._page_label = ValueDisplay(
            Rect(self._content_rect.x, self._content_rect.centery - 15, self._content_rect.width, 30),
            label="",
            value="VFD ENERGY",  # Page 1 initial label
            unit="",
            compact=False,
            value_size=20
        )
        self.add_widget(self._page_label)
        
        # Pagination Control
        self._pagination_control = PaginationControl(
            Rect(center_x + (center_width - 100) // 2, self.height - 25, 100, 20),
            num_pages=self._num_pages,
            current_page=self._current_page,
            on_change=self._on_page_change
        )
        self.add_widget(self._pagination_control)
        
        # VFD Display Simulator (Page 1 content)
        # Display dimensions: 256x48 with 3px frame
        # Using scale=1 for now (native resolution)
        vfd_scale = 1
        vfd_frame_width = 3
        vfd_total_width = (VFD_WIDTH + vfd_frame_width * 2) * vfd_scale
        vfd_total_height = (VFD_HEIGHT + vfd_frame_width * 2) * vfd_scale
        
        # Position centered in the content area
        vfd_x = center_x + (center_width - vfd_total_width) // 2
        vfd_y = self._content_rect.y + (self._content_rect.height - vfd_total_height) // 2
        
        self._vfd_display = VFDDisplayWidget(
            Rect(vfd_x, vfd_y, vfd_total_width, vfd_total_height),
            scale=vfd_scale
        )
        # Note: VFD widget is managed separately, not added to widget list
        # It's only rendered on page 1

    def _on_page_change(self, page_index: int) -> None:
        """Handle page change."""
        self._current_page = page_index
        # Verify page index valid (though control handles it)
        self._current_page = max(0, min(self._current_page, self._num_pages - 1))
        
        # Update content based on page
        if self._current_page == 0:
            self._page_label.set_value("VFD ENERGY")
        elif self._current_page == 1:
            self._page_label.set_value("PAGE 2")
            
        # Page visibility is handled in render()

    
    def set_avc_bridge(self, bridge) -> None:
        """
        Connect AVC-LAN UI bridge for live updates.
        
        Args:
            bridge: AVCUIBridge instance
        """
        self._avc_bridge = bridge
        
        # Subscribe to state changes
        bridge.subscribe("audio", self._on_avc_audio_update)
        bridge.subscribe("climate", self._on_avc_climate_update)
        bridge.subscribe("vehicle", self._on_avc_vehicle_update)
        bridge.subscribe("energy", self._on_avc_energy_update)
        bridge.subscribe("connection", self._on_avc_connection_update)
    
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
    
    def _on_store_update(self, state) -> None:
        """Handle state update from Store."""
        # Update audio
        self._volume = state.audio.volume
        if hasattr(self, '_volume_bar') and self._volume_bar:
            self._volume_bar.set_value(state.audio.volume)
        if hasattr(self, '_volume_label') and self._volume_label:
            self._volume_label.set_value(str(state.audio.volume))
        
        # Update climate state variables
        self._temp_target = f"{state.climate.target_temp:.0f}"
        if state.climate.inside_temp is not None:
            self._temp_in = f"{state.climate.inside_temp:.0f}"
        else:
            self._temp_in = "N/A"
        if state.climate.outside_temp is not None:
            self._temp_out = f"{state.climate.outside_temp:.0f}"
        else:
            self._temp_out = "N/A"
        self._climate_ac = state.climate.ac_on
        self._climate_auto = state.climate.auto_mode
        self._climate_recirc = getattr(state.climate, 'recirculation', False)
        
        # Update climate display widgets
        if hasattr(self, '_temp_target_display') and self._temp_target_display:
            self._temp_target_display.set_value(self._temp_target)
        if hasattr(self, '_temp_in_display') and self._temp_in_display:
            self._temp_in_display.set_value(self._temp_in)
        if hasattr(self, '_temp_out_display') and self._temp_out_display:
            self._temp_out_display.set_value(self._temp_out)
        if hasattr(self, '_ac_icon') and self._ac_icon:
            self._ac_icon.set_active(self._climate_ac)
        if hasattr(self, '_auto_icon') and self._auto_icon:
            self._auto_icon.set_active(self._climate_auto)
        if hasattr(self, '_recirc_icon') and self._recirc_icon:
            self._recirc_icon.set_active(self._climate_recirc)
            
        # Update Gear
        if hasattr(self, '_gear_display') and self._gear_display:
            from ...state.app_state import GearPosition
            gear = state.vehicle.gear
            text = "P"
            if gear == GearPosition.PARK: text = "P"
            elif gear == GearPosition.REVERSE: text = "R"
            elif gear == GearPosition.NEUTRAL: text = "N"
            elif gear == GearPosition.DRIVE: text = "D"
            elif gear == GearPosition.B: text = "B"
            self._gear_display.set_value(text)

        # Update Engine Telemetry
        if hasattr(self, '_rpm_display') and self._rpm_display:
             val = str(int(state.vehicle.rpm)) if state.vehicle.rpm is not None else "0"
             self._rpm_display.set_value(val)
        if hasattr(self, '_ice_temp_display') and self._ice_temp_display:
             val = str(int(state.vehicle.ice_coolant_temp)) if state.vehicle.ice_coolant_temp is not None else "--"
             self._ice_temp_display.set_value(val)
        if hasattr(self, '_speed_display') and self._speed_display:
             val = str(int(state.vehicle.speed_kmh)) if state.vehicle.speed_kmh is not None else "--"
             self._speed_display.set_value(val)
        if hasattr(self, '_fuel_display') and self._fuel_display:
             consumption = state.vehicle.instant_consumption
             unit = state.vehicle.consumption_unit
             
             # If consumption is effectively 0, show placeholder to match previous behavior
             if consumption > 0.0:
                 val = f"{consumption:.1f}"
             else:
                 val = "--.-"

             self._fuel_display.set_value(val)
             self._fuel_display.set_label(unit)

             
        # Update Battery Telemetry
        if hasattr(self, '_batt_power_display') and self._batt_power_display:
             power_kw = state.energy.battery_power_kw
             if power_kw is not None:
                 # Show sign: + for discharge, - for charge
                 val = f"{power_kw:+.1f}" if abs(power_kw) >= 0.1 else "0.0"
             else:
                 val = "--.-"
             self._batt_power_display.set_value(val)
        if hasattr(self, '_batt_volt_display') and self._batt_volt_display:
             val = f"{state.energy.hv_battery_voltage:.0f}" if state.energy.hv_battery_voltage is not None else "---"
             self._batt_volt_display.set_value(val)
        if hasattr(self, '_batt_curr_display') and self._batt_curr_display:
             val = f"{state.energy.hv_battery_current:.0f}" if state.energy.hv_battery_current is not None else "--"
             self._batt_curr_display.set_value(val)
        if hasattr(self, '_batt_temp_display') and self._batt_temp_display:
             val = str(int(state.energy.battery_temp)) if state.energy.battery_temp is not None else "--"
             self._batt_temp_display.set_value(val)
        if hasattr(self, '_batt_soc_display') and self._batt_soc_display:
             soc_pct = int(state.energy.battery_soc * 100)
             val = str(soc_pct) if state.energy.battery_soc > 0 else "--"
             self._batt_soc_display.set_value(val)
        
        # Update connection
        if hasattr(self, '_connection_indicator') and self._connection_indicator:
            self._connection_indicator.set_connected(state.connection.connected)
        
        # Update VFD Energy Monitor
        if hasattr(self, '_vfd_display') and self._vfd_display:
            # Calculate MG power from battery data (positive = assist, negative = regen)
            mg_power_kw = state.energy.battery_power_kw or 0.0
            # Invert sign: positive battery current = discharge = MG assist
            # So battery_power_kw > 0 means MG is consuming power (assist)
            
            # Get ICE data
            ice_rpm = state.vehicle.rpm or 0
            ice_running = state.vehicle.ice_running if hasattr(state.vehicle, 'ice_running') else False
            
            # Estimate ICE load from fuel consumption
            # Rough estimate: idle ~0.5 L/h, max load ~8 L/h for Prius 1.5L
            fuel_flow = state.vehicle.fuel_flow_rate if hasattr(state.vehicle, 'fuel_flow_rate') else 0.0
            if fuel_flow > 0:
                ice_load_percent = min(100.0, (fuel_flow / 8.0) * 100.0)
            elif ice_rpm > 0:
                # Fallback: estimate from RPM (idle 1000, redline ~5000)
                ice_load_percent = min(100.0, (ice_rpm / 4500.0) * 100.0)
            else:
                ice_load_percent = 0.0
            
            # Get brake pressure
            brake_pressure = state.vehicle.brake_pressed if hasattr(state.vehicle, 'brake_pressed') else 0
            
            self._vfd_display.update_energy(
                mg_power_kw, 
                ice_rpm, 
                ice_load_percent,
                fuel_flow,
                brake_pressure,
                ice_running
            )
        
        # Update AVC Input visualization (touch and button events)
        if hasattr(state, 'input'):
            if state.input.last_touch_time > self._last_touch_time:
                self._last_touch_x = state.input.last_touch_x
                self._last_touch_y = state.input.last_touch_y
                self._last_touch_time = state.input.last_touch_time
            if state.input.last_button_time > self._last_button_time:
                self._last_button_name = state.input.last_button_name
                self._last_button_time = state.input.last_button_time
        
        self._dirty = True
        
    def _on_avc_audio_update(self, state) -> None:
        """Handle audio state update from AVC-LAN."""
        self._volume = state.volume
        
        # Update volume bar in audio frame
        if hasattr(self, '_volume_bar') and self._volume_bar:
            self._volume_bar.set_value(state.volume)
        self._dirty = True
        
    def _on_avc_climate_update(self, state) -> None:
        """Handle climate state update from AVC-LAN."""
        self._temp_target = f"{state.target_temp:.0f}"
        self._climate_ac = state.ac_on
        self._climate_auto = state.auto_mode
        self._climate_recirc = state.recirculation
        
        if state.inside_temp is not None:
            self._temp_in = f"{state.inside_temp:.0f}"
        else:
            self._temp_in = "N/A"
        if state.outside_temp is not None:
            self._temp_out = f"{state.outside_temp:.0f}"
        else:
            self._temp_out = "N/A"
        self._dirty = True
        
    def _on_avc_vehicle_update(self, state) -> None:
        """Handle vehicle state update from AVC-LAN."""
        # Vehicle state updates can be handled here if needed
        self._dirty = True
        
    def _on_avc_energy_update(self, state) -> None:
        """Handle energy state update from AVC-LAN."""
        # Energy state updates can be handled here if needed
        self._dirty = True
        
    def _on_avc_connection_update(self, state) -> None:
        """Handle connection state update."""
        if state.connected:
            self._connection_indicator.on_message_received()
        self._connection_indicator.set_connected(state.connected)
        self._dirty = True
    
    def update(self, dt: float) -> None:
        """Update screen and check for focus timeout."""
        super().update(dt)
        
        # Update clock
        if hasattr(self, '_clock_display') and self._clock_display:
            import time
            current_time = time.strftime("%H:%M")
            self._clock_display.set_value(current_time)
        
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
        
        # Render page-specific content
        if self._current_page == 0:
            # Page 1: VFD Energy Monitor
            self._render_vfd_page(surface, center_x, center_width)
        else:
            # Page 2+: Default placeholder
            self._render_default_page(surface, center_x, center_width)
        
        # Render AVC Input visualization (touch and button events)
        self._render_avc_input_visualization(surface, center_x, center_width)
    
    def _render_vfd_page(self, surface: pygame.Surface, center_x: int, center_width: int) -> None:
        """Render Page 1: VFD Energy Monitor."""
        if hasattr(self, '_vfd_display') and self._vfd_display:
            self._vfd_display.render(surface)
    
    def _render_default_page(self, surface: pygame.Surface, center_x: int, center_width: int) -> None:
        """Render default page with logo placeholder."""
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
        if self._last_touch_time > 0 and touch_age < self._touch_display_duration:
            # Calculate alpha fade (1.0 -> 0.0)
            alpha = 1.0 - (touch_age / self._touch_display_duration)
            
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
        if self._last_button_time > 0 and button_age < self._button_display_duration:
            # Calculate alpha fade
            alpha = 1.0 - (button_age / self._button_display_duration)
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
