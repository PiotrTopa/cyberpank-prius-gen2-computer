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
from .engine_screen import EngineScreen
from ..widgets.base import Rect
from ..widgets.frame import Frame
from ..widgets.controls import VolumeBar, ToggleSwitch, ValueDisplay, ModeIcon, StatusIcon
from ..widgets.vehicle_status import ConnectionIndicator
from ..widgets.pagination import PaginationControl
# VFD widget removed - now runs as separate satellite app (device 110)
# See vfd_satellite/ and docs/VFD_SATELLITE_PROTOCOL.md
from ..colors import COLORS, dim_color
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
        self._num_pages = 4
        
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
        
        # AVC-LAN byte debug display (for flow arrow correlation)
        self._avc_110_490_bytes = [0] * 8  # Last 0x110→0x490 message bytes
        self._avc_a00_258_bytes = [0] * 32  # Last 0xA00→0x258 message bytes (SOC/flow data)
        
        # Engine page history buffers (1 hour @ 1 sample/sec = 3600 entries)
        self._engine_history_max = 3600
        self._fuel_consumption_history = []   # (timestamp, avg L/h over period)
        self._ice_temp_history = []            # (timestamp, °C value)
        self._rpm_history = []                 # (timestamp, RPM value) for smoothing
        self._last_engine_sample_time = 0.0
        
        # Fuel flow averaging accumulator (collects all updates between 1s snapshots)
        self._fuel_flow_accumulator = []       # raw fuel_flow_rate samples within current 1s window
        self._fuel_consumed_liters = 0.0       # cumulative fuel consumed (L) via integration
        self._fuel_last_integrate_time = 0.0   # last integration timestamp
        
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
            focusable=True,
            on_action=self._on_engine_action,
            on_select=self._on_diag_action
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
            focusable=True,
            on_action=self._on_battery_action,
            on_select=self._on_avc_monitor_action
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
        
        # Status Bar: Gear | Speed | Pagination | Connection
        
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
        
        # Pagination Control (moved to right side)
        self._pagination_control = PaginationControl(
            Rect(center_x + center_width - 100, 3, 100, 20),
            num_pages=self._num_pages,
            current_page=self._current_page,
            on_change=self._on_page_change
        )
        self.add_widget(self._pagination_control)
        
        # Center Content Area (Pages) - full height below status bar
        self._content_rect = Rect(center_x, 30, center_width, self.height - 30)
        
        # VFD Display has been moved to separate satellite app (device 110)
        # Page 1 now shows placeholder indicating VFD is on external display
        # See: vfd_satellite/ package and docs/VFD_SATELLITE_PROTOCOL.md

    def _on_page_change(self, page_index: int) -> None:
        """Handle page change."""
        self._current_page = page_index
        # Verify page index valid (though control handles it)
        self._current_page = max(0, min(self._current_page, self._num_pages - 1))
        
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
             rpm_val = state.vehicle.rpm
             val = str(int(rpm_val)) if rpm_val is not None else "0"
             self._rpm_display.set_value(val)
        if hasattr(self, '_ice_temp_display') and self._ice_temp_display:
             val = str(int(state.vehicle.ice_coolant_temp)) if state.vehicle.ice_coolant_temp is not None else "--"
             self._ice_temp_display.set_value(val)
        if hasattr(self, '_inverter_temp_display') and self._inverter_temp_display:
             val = str(int(state.vehicle.inverter_temp)) if state.vehicle.inverter_temp is not None else "--"
             self._inverter_temp_display.set_value(val)
        if hasattr(self, '_speed_display') and self._speed_display:
             val = str(int(state.vehicle.speed_kmh)) if state.vehicle.speed_kmh is not None else "0"
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
        
        # VFD Energy Monitor removed - handled by VFDDisplayRule and satellite app
        # See: VFDDisplayRule in state/rules/vfd_display.py
        
        # Update AVC Input visualization (touch and button events)
        if hasattr(state, 'input'):
            if state.input.last_touch_time > self._last_touch_time:
                self._last_touch_x = state.input.last_touch_x
                self._last_touch_y = state.input.last_touch_y
                self._last_touch_time = state.input.last_touch_time
            if state.input.last_button_time > self._last_button_time:
                self._last_button_name = state.input.last_button_name
                self._last_button_time = state.input.last_button_time
        
        # Accumulate fuel flow rate for averaging (runs on every store update)
        # Only count fuel flow when engine is actually running
        now = time.time()
        raw_flow = state.vehicle.fuel_flow_rate if state.vehicle.fuel_flow_rate is not None else 0.0
        flow_rate = raw_flow if state.vehicle.ice_running else 0.0
        self._fuel_flow_accumulator.append(flow_rate)
        
        # Integrate fuel consumed (cumulative liters)
        if self._fuel_last_integrate_time > 0.0:
            dt_hours = (now - self._fuel_last_integrate_time) / 3600.0
            self._fuel_consumed_liters += flow_rate * dt_hours
        self._fuel_last_integrate_time = now
        
        # Snapshot engine history data (1 sample per second)
        if now - self._last_engine_sample_time >= 1.0:
            self._last_engine_sample_time = now
            
            # Fuel consumption: average of all samples in this 1s window
            if self._fuel_flow_accumulator:
                avg_flow = sum(self._fuel_flow_accumulator) / len(self._fuel_flow_accumulator)
            else:
                avg_flow = 0.0
            self._fuel_flow_accumulator.clear()
            self._fuel_consumption_history.append((now, avg_flow))
            if len(self._fuel_consumption_history) > self._engine_history_max:
                self._fuel_consumption_history.pop(0)
            
            # ICE coolant temperature history
            ice_temp = state.vehicle.ice_coolant_temp
            if ice_temp is not None:
                self._ice_temp_history.append((now, ice_temp))
                if len(self._ice_temp_history) > self._engine_history_max:
                    self._ice_temp_history.pop(0)
        
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
        elif self._current_page == 1:
            # Page 2: Vehicle Dynamics
            self._render_dynamics_page(surface, center_x, center_width)
        elif self._current_page == 2:
            # Page 3: Engine Status
            self._render_engine_page(surface, center_x, center_width)
        elif self._current_page == 3:
            # Page 4: EV / Battery
            self._render_ev_page(surface, center_x, center_width)
        else:
            # Page 5+: Default placeholder
            self._render_default_page(surface, center_x, center_width)
        
        # Render AVC Input visualization (touch and button events)
        self._render_avc_input_visualization(surface, center_x, center_width)
    
    def _render_vfd_page(self, surface: pygame.Surface, center_x: int, center_width: int) -> None:
        """Render Page 1: VFD moved to satellite - show default page."""
        # VFD display has been moved to separate satellite app.
        # Just show the default page content here.
        self._render_default_page(surface, center_x, center_width)
    
    def _render_default_page(self, surface: pygame.Surface, center_x: int, center_width: int) -> None:
        """Render default page with logo placeholder."""
        cr = self._content_rect
        # Center logo/title (placeholder)
        font = get_font(16, "title")
        title = "CYBERPUNK"
        title_surf = font.render(title, True, COLORS["cyan_dim"])
        title_x = center_x + (center_width - title_surf.get_width()) // 2
        title_y = cr.y + cr.height // 2 - 20
        surface.blit(title_surf, (title_x, title_y))
        
        font_small = get_font(10)
        subtitle = "PRIUS GEN2"
        sub_surf = font_small.render(subtitle, True, COLORS["text_secondary"])
        sub_x = center_x + (center_width - sub_surf.get_width()) // 2
        surface.blit(sub_surf, (sub_x, title_y + 20))
    
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
        
        # "185 p/rev" label
        unit_surf = font_small.render("185p/rev", True, COLORS["text_dim"])
        surface.blit(unit_surf, (left_x + col_width - unit_surf.get_width(), y + 2))
        y += row_h + 2
        
        # Front wheels
        lbl = font_label.render("FR", True, COLORS["text_secondary"])
        surface.blit(lbl, (left_x, y))
        fr_text = f"{dyn.front_right_pulses:5d}"
        fl_text = f"{dyn.front_left_pulses:5d}"
        val_surf = font_value.render(f"R{fr_text} L{fl_text}", True, COLORS["green_bright"])
        surface.blit(val_surf, (left_x + col_width - val_surf.get_width(), y))
        y += row_h
        
        # Rear wheels
        lbl = font_label.render("RR", True, COLORS["text_secondary"])
        surface.blit(lbl, (left_x, y))
        rr_text = f"{dyn.rear_right_pulses:5d}"
        rl_text = f"{dyn.rear_left_pulses:5d}"
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
        if hl_state == "HIGH":
            hl_color = COLORS["blue_bright"]
            hl_icon = "\u2588\u2588 HIGH"  # Full block + HIGH
        elif hl_state == "LOW":
            hl_color = COLORS["green_bright"]
            hl_icon = "\u2593\u2593 LOW"   # Medium shade + LOW
        elif hl_state == "PARK":
            hl_color = COLORS["yellow"]
            hl_icon = "\u2592\u2592 PARK"  # Light shade + PARK
        else:
            hl_color = COLORS["text_dim"]
            hl_icon = "\u2591\u2591 OFF"   # Lightest shade + OFF
        val_surf = font_value.render(hl_icon, True, hl_color)
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
        bar_text = "\u2588" * bars + "\u2591" * (8 - bars)  # Filled + empty blocks
        if bars >= 6:
            bar_color = COLORS["green_bright"]
        elif bars >= 3:
            bar_color = COLORS["yellow"]
        else:
            bar_color = COLORS["red_bright"]
        val_surf = font_value.render(f"{bar_text} {bars}", True, bar_color)
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
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
            lbl = font_label.render("\u26a0 WARNING", True, COLORS["red_bright"])
            surface.blit(lbl, (right_x, ry))
        ry += row_h
    
    def _render_ev_page(self, surface: pygame.Surface, center_x: int, center_width: int) -> None:
        """Render Page 4: EV / Battery dashboard.
        
        Shows:
        - Hybrid system temperatures (MG1/MG2 inverter, motor, converter, ICE, HV batt)
        - MG1/MG2/ICE RPMs with solicited vs unsolicited comparison
        - HV battery details (SOC, delta SOC, voltage, current, power, temp)
        """
        if not self._store:
            self._render_default_page(surface, center_x, center_width)
            return
        
        state = self._store.state
        v = state.vehicle
        e = state.energy
        
        font_label = get_font(8)
        font_value = get_font(11, "mono")
        font_title = get_font(10, "title")
        font_small = get_font(7)
        
        cr = self._content_rect
        pad = 6
        col_width = (center_width - pad * 3) // 2
        left_x = center_x + pad
        right_x = center_x + pad * 2 + col_width
        y = cr.y + 2
        row_h = 13
        
        # ─── LEFT COLUMN: Temperatures ───
        
        title_surf = font_title.render("TEMPERATURES", True, COLORS["cyan_bright"])
        surface.blit(title_surf, (left_x, y))
        y += row_h + 2
        
        temps = [
            ("MG1 INV", v.mg1_inverter_temp),
            ("MG2 INV", v.mg2_inverter_temp),
            ("MG1 MOT", v.mg1_motor_temp),
            ("MG2 MOT", v.mg2_motor_temp),
            ("CONVERT", v.converter_temp),
            ("ICE CLT", v.ice_coolant_temp),
            ("HV BATT", e.battery_temp),
        ]
        
        for label_text, temp_val in temps:
            lbl = font_label.render(label_text, True, COLORS["text_secondary"])
            surface.blit(lbl, (left_x, y))
            
            if temp_val is not None:
                val_str = f"{int(temp_val)}°C"
                if temp_val < 40:
                    color = COLORS["green_bright"]
                elif temp_val < 70:
                    color = COLORS["text_value"]
                elif temp_val < 90:
                    color = COLORS["yellow"]
                else:
                    color = COLORS["red_bright"]
            else:
                val_str = "--°C"
                color = COLORS["text_dim"]
            
            val_surf = font_value.render(val_str, True, color)
            surface.blit(val_surf, (left_x + col_width - val_surf.get_width(), y))
            y += row_h
        
        # ─── RIGHT COLUMN: Motors + Battery ───
        
        ry = cr.y + 2
        title_surf = font_title.render("MOTORS", True, COLORS["cyan_bright"])
        surface.blit(title_surf, (right_x, ry))
        ry += row_h + 2
        
        motors = [
            ("MG1 GEN", v.mg1_rpm),
            ("MG2 MOT", v.mg2_rpm),
        ]
        
        for label_text, rpm_val in motors:
            lbl = font_label.render(label_text, True, COLORS["text_secondary"])
            surface.blit(lbl, (right_x, ry))
            
            if rpm_val is not None:
                val_str = f"{int(rpm_val)} rpm"
                color = COLORS["green_bright"] if abs(rpm_val) > 0 else COLORS["text_dim"]
            else:
                val_str = "-- rpm"
                color = COLORS["text_dim"]
            
            val_surf = font_value.render(val_str, True, color)
            surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
            ry += row_h
        
        # ICE RPM
        lbl = font_label.render("ICE RPM", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        ice_rpm = v.rpm
        
        if ice_rpm is not None and ice_rpm > 0:
            val_str = f"{int(ice_rpm)}"
            color = COLORS["green_bright"]
            if ice_rpm > 3500:
                color = COLORS["red_bright"]
            elif ice_rpm > 2000:
                color = COLORS["yellow"]
        else:
            val_str = "0"
            color = COLORS["text_dim"]
        
        val_surf = font_value.render(val_str, True, color)
        surface.blit(val_surf, (right_x + col_width - val_surf.get_width(), ry))
        ry += row_h
        ry += row_h  # Keep spacing consistent
        
        # ─── HV BATTERY section ───
        
        title_surf = font_title.render("HV BATTERY", True, COLORS["cyan_bright"])
        surface.blit(title_surf, (right_x, ry))
        ry += row_h + 2
        
        # SOC + Delta SOC
        lbl = font_label.render("SOC", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        soc_pct = int(e.battery_soc * 100) if e.battery_soc > 0 else None
        if soc_pct is not None:
            soc_str = f"{soc_pct}%"
            soc_color = COLORS["green_bright"] if soc_pct >= 40 else COLORS["yellow"]
        else:
            soc_str = "--%"
            soc_color = COLORS["text_dim"]
        val_surf = font_value.render(soc_str, True, soc_color)
        surface.blit(val_surf, (right_x + 50, ry))
        
        # Delta SOC on same line
        if e.battery_delta_soc is not None:
            dsoc_str = f"\u0394{e.battery_delta_soc:.2f}"
        else:
            dsoc_str = "\u0394--"
        dsoc_surf = font_small.render(dsoc_str, True, COLORS["text_dim"])
        surface.blit(dsoc_surf, (right_x + col_width - dsoc_surf.get_width(), ry))
        ry += row_h
        
        # Voltage / Current / Power
        v_str = f"{e.hv_battery_voltage:.0f}V" if e.hv_battery_voltage is not None else "---V"
        a_str = f"{e.hv_battery_current:.0f}A" if e.hv_battery_current is not None else "--A"
        p_str = f"{e.battery_power_kw:+.1f}kW" if e.battery_power_kw is not None else "--kW"
        line_surf = font_value.render(f"{v_str} {a_str} {p_str}", True, COLORS["text_value"])
        surface.blit(line_surf, (right_x, ry))
        ry += row_h
        
        # Battery temp with min/max
        if e.battery_temp is not None:
            temp_str = f"{int(e.battery_temp)}°C"
            temp_color = COLORS["green_bright"] if e.battery_temp < 40 else COLORS["yellow"]
        else:
            temp_str = "--°C"
            temp_color = COLORS["text_dim"]
        lbl = font_label.render("TEMP", True, COLORS["text_secondary"])
        surface.blit(lbl, (right_x, ry))
        val_surf = font_value.render(temp_str, True, temp_color)
        surface.blit(val_surf, (right_x + 50, ry))
        
        # Min/max range
        min_t = f"{int(e.battery_min_cell_temp)}" if e.battery_min_cell_temp is not None else "--"
        max_t = f"{int(e.battery_max_cell_temp)}" if e.battery_max_cell_temp is not None else "--"
        range_surf = font_small.render(f"({min_t}/{max_t})", True, COLORS["text_dim"])
        surface.blit(range_surf, (right_x + col_width - range_surf.get_width(), ry))
        ry += row_h
    
    def _render_engine_page(self, surface: pygame.Surface, center_x: int, center_width: int) -> None:
        """Render Page 3: Engine Status dashboard.
        
        Shows:
        - ENGINE section data (RPM, Consumption, ICE temp, Inverter temp)
        - RPM arc gauge
        - Fuel consumption line graph (1 hour)
        - ICE temperature line graph (1 hour)
        """
        import math
        
        if not self._store:
            self._render_default_page(surface, center_x, center_width)
            return
        
        state = self._store.state
        font_label = get_font(8)
        font_value = get_font(11, "mono")
        font_title = get_font(10, "title")
        font_small = get_font(7)
        font_large = get_font(14, "mono")
        
        cr = self._content_rect
        pad = 6
        
        # ─── TOP ROW: RPM Gauge (left) + Value Readouts (right) ───
        gauge_size = min(cr.height // 2 - pad, center_width // 2 - pad * 2)
        gauge_cx = center_x + pad + gauge_size // 2 + 10
        gauge_cy = cr.y + 4 + gauge_size // 2
        gauge_radius = gauge_size // 2 - 4
        
        rpm = state.vehicle.rpm or 0
        max_rpm = 5000  # Prius Gen2 ICE max RPM
        rpm_ratio = min(1.0, rpm / max_rpm)
        
        self._render_rpm_gauge(
            surface, gauge_cx, gauge_cy, gauge_radius,
            rpm, max_rpm, rpm_ratio
        )
        
        # ─── RIGHT SIDE: Engine data readouts ───
        readout_x = center_x + center_width // 2 + pad
        readout_w = center_width // 2 - pad * 2
        ry = cr.y + 4
        row_h = 14
        
        # Title
        title_surf = font_title.render("ENGINE", True, COLORS["cyan_bright"])
        surface.blit(title_surf, (readout_x, ry))
        ry += row_h + 2
        
        # RPM
        lbl = font_label.render("RPM", True, COLORS["text_secondary"])
        surface.blit(lbl, (readout_x, ry))
        rpm_text = str(int(rpm)) if rpm > 0 else "0"
        color = COLORS["green_bright"]
        if rpm > 3500:
            color = COLORS["red_bright"]
        elif rpm > 2000:
            color = COLORS["yellow"]
        val_surf = font_value.render(rpm_text, True, color)
        surface.blit(val_surf, (readout_x + readout_w - val_surf.get_width(), ry))
        ry += row_h
        
        # Fuel Flow Rate (raw L/h)
        lbl = font_label.render("FLOW", True, COLORS["text_secondary"])
        surface.blit(lbl, (readout_x, ry))
        flow_rate = state.vehicle.fuel_flow_rate
        if flow_rate is not None and flow_rate > 0.05:
            flow_text = f"{flow_rate:.1f} L/h"
            color = COLORS["green_bright"]
            if flow_rate > 5.0:
                color = COLORS["yellow"]
            if flow_rate > 10.0:
                color = COLORS["red_bright"]
        else:
            flow_text = "--.- L/h"
            color = COLORS["text_dim"]
        val_surf = font_value.render(flow_text, True, color)
        surface.blit(val_surf, (readout_x + readout_w - val_surf.get_width(), ry))
        ry += row_h
        
        # Instant consumption (L/100km or L/h)
        lbl = font_label.render("CONS", True, COLORS["text_secondary"])
        surface.blit(lbl, (readout_x, ry))
        consumption = state.vehicle.instant_consumption
        unit = state.vehicle.consumption_unit
        if consumption > 0.0:
            cons_text = f"{consumption:.1f} {unit}"
            color = COLORS["green_bright"]
            if consumption > 5.0:
                color = COLORS["yellow"]
            if consumption > 10.0:
                color = COLORS["red_bright"]
        else:
            cons_text = f"--.- {unit}"
            color = COLORS["text_dim"]
        val_surf = font_value.render(cons_text, True, color)
        surface.blit(val_surf, (readout_x + readout_w - val_surf.get_width(), ry))
        ry += row_h
        
        # Cumulative fuel consumed
        lbl = font_label.render("USED", True, COLORS["text_secondary"])
        surface.blit(lbl, (readout_x, ry))
        if self._fuel_consumed_liters > 0.001:
            used_text = f"{self._fuel_consumed_liters:.2f} L"
            color = COLORS["yellow"]
        else:
            used_text = "0.00 L"
            color = COLORS["text_dim"]
        val_surf = font_value.render(used_text, True, color)
        surface.blit(val_surf, (readout_x + readout_w - val_surf.get_width(), ry))
        ry += row_h
        
        # ICE Coolant Temperature
        lbl = font_label.render("ICE TEMP", True, COLORS["text_secondary"])
        surface.blit(lbl, (readout_x, ry))
        ice_temp = state.vehicle.ice_coolant_temp
        if ice_temp is not None:
            temp_text = f"{int(ice_temp)}°C"
            if ice_temp < 60:
                color = COLORS["blue_bright"]
            elif ice_temp < 100:
                color = COLORS["green_bright"]
            else:
                color = COLORS["red_bright"]
        else:
            temp_text = "--°C"
            color = COLORS["text_dim"]
        val_surf = font_value.render(temp_text, True, color)
        surface.blit(val_surf, (readout_x + readout_w - val_surf.get_width(), ry))
        ry += row_h
        
        # Inverter Temperature
        lbl = font_label.render("INV TEMP", True, COLORS["text_secondary"])
        surface.blit(lbl, (readout_x, ry))
        inv_temp = state.vehicle.inverter_temp
        if inv_temp is not None:
            temp_text = f"{int(inv_temp)}°C"
            if inv_temp < 60:
                color = COLORS["green_bright"]
            elif inv_temp < 80:
                color = COLORS["yellow"]
            else:
                color = COLORS["red_bright"]
        else:
            temp_text = "--°C"
            color = COLORS["text_dim"]
        val_surf = font_value.render(temp_text, True, color)
        surface.blit(val_surf, (readout_x + readout_w - val_surf.get_width(), ry))
        ry += row_h
        
        # ICE Running status
        lbl = font_label.render("ICE", True, COLORS["text_secondary"])
        surface.blit(lbl, (readout_x, ry))
        if state.vehicle.ice_running:
            val_surf = font_value.render("RUNNING", True, COLORS["orange"])
        else:
            val_surf = font_value.render("OFF", True, COLORS["text_dim"])
        surface.blit(val_surf, (readout_x + readout_w - val_surf.get_width(), ry))
        ry += row_h
        
        # Active Fuel
        lbl = font_label.render("FUEL TYPE", True, COLORS["text_secondary"])
        surface.blit(lbl, (readout_x, ry))
        from ...state.app_state import FuelType
        fuel = state.vehicle.active_fuel
        if fuel == FuelType.PETROL:
            val_surf = font_value.render("PETROL", True, COLORS["yellow"])
        elif fuel == FuelType.LPG:
            val_surf = font_value.render("LPG", True, COLORS["green_bright"])
        else:
            val_surf = font_value.render("OFF", True, COLORS["text_dim"])
        surface.blit(val_surf, (readout_x + readout_w - val_surf.get_width(), ry))
        
        # ─── BOTTOM ROW: Graphs ───
        graph_y = cr.y + cr.height // 2 + 20
        graph_h = cr.height // 2 - pad - 20  # Full height, no bottom pagination
        half_w = (center_width - pad * 3) // 2
        
        # Fuel Consumption Graph (left)
        self._render_time_graph(
            surface,
            x=center_x + pad,
            y=graph_y,
            w=half_w,
            h=graph_h,
            title="FUEL FLOW (1h)",
            data=self._fuel_consumption_history,
            time_window=3600,
            min_val=0.0,
            max_val=15.0,
            unit="L/h",
            color=COLORS["yellow"],
            warn_threshold=5.0,
            crit_threshold=10.0
        )
        
        # ICE Temperature Graph (right)
        self._render_time_graph(
            surface,
            x=center_x + pad * 2 + half_w,
            y=graph_y,
            w=half_w,
            h=graph_h,
            title="ICE TEMPERATURE (1h)",
            data=self._ice_temp_history,
            time_window=3600,
            min_val=0.0,
            max_val=120.0,
            unit="°C",
            color=COLORS["cyan_bright"],
            warn_threshold=95.0,
            crit_threshold=105.0
        )
    
    def _render_rpm_gauge(
        self,
        surface: pygame.Surface,
        cx: int, cy: int, radius: int,
        rpm: int, max_rpm: int, rpm_ratio: float
    ) -> None:
        """Render an arc-style RPM gauge."""
        import math
        
        font_small = get_font(7)
        font_value = get_font(14, "mono")
        font_label = get_font(8)
        
        # Gauge arc: 225° to -45° (270° sweep, opens at bottom)
        start_angle = math.radians(225)
        end_angle = math.radians(-45)
        sweep = math.radians(270)
        
        # Draw background arc (dark)
        num_segments = 60
        for i in range(num_segments):
            t = i / num_segments
            angle = start_angle - t * sweep
            x1 = cx + int((radius - 2) * math.cos(angle))
            y1 = cy - int((radius - 2) * math.sin(angle))
            x2 = cx + int(radius * math.cos(angle))
            y2 = cy - int(radius * math.sin(angle))
            pygame.draw.line(surface, COLORS["border_normal"], (x1, y1), (x2, y2), 1)
        
        # Draw tick marks and labels
        tick_values = [0, 1000, 2000, 3000, 4000, 5000]
        for val in tick_values:
            t = val / max_rpm
            angle = start_angle - t * sweep
            # Outer tick
            ox = cx + int((radius + 1) * math.cos(angle))
            oy = cy - int((radius + 1) * math.sin(angle))
            ix = cx + int((radius - 5) * math.cos(angle))
            iy = cy - int((radius - 5) * math.sin(angle))
            pygame.draw.line(surface, COLORS["text_secondary"], (ix, iy), (ox, oy), 1)
            
            # Label (outside)
            lbl_text = str(val // 1000)
            lbl_surf = font_small.render(lbl_text, True, COLORS["text_secondary"])
            lx = cx + int((radius + 8) * math.cos(angle)) - lbl_surf.get_width() // 2
            ly = cy - int((radius + 8) * math.sin(angle)) - lbl_surf.get_height() // 2
            surface.blit(lbl_surf, (lx, ly))
        
        # Draw active arc (colored based on RPM)
        if rpm > 0:
            active_segments = int(num_segments * rpm_ratio)
            for i in range(active_segments):
                t = i / num_segments
                # Color gradient: green -> yellow -> red
                if t < 0.5:
                    seg_color = COLORS["green_bright"]
                elif t < 0.75:
                    seg_color = COLORS["yellow"]
                else:
                    seg_color = COLORS["red_bright"]
                
                angle = start_angle - t * sweep
                for r_off in range(-1, 3):
                    x1 = cx + int((radius - 2 + r_off) * math.cos(angle))
                    y1 = cy - int((radius - 2 + r_off) * math.sin(angle))
                    angle2 = start_angle - (i + 1) / num_segments * sweep
                    x2 = cx + int((radius - 2 + r_off) * math.cos(angle2))
                    y2 = cy - int((radius - 2 + r_off) * math.sin(angle2))
                    pygame.draw.line(surface, seg_color, (x1, y1), (x2, y2), 1)
        
        # Needle
        needle_angle = start_angle - rpm_ratio * sweep
        nx = cx + int((radius - 8) * math.cos(needle_angle))
        ny = cy - int((radius - 8) * math.sin(needle_angle))
        needle_color = COLORS["red_bright"] if rpm > 4000 else COLORS["text_highlight"]
        pygame.draw.line(surface, needle_color, (cx, cy), (nx, ny), 2)
        
        # Center dot
        pygame.draw.circle(surface, COLORS["cyan_dim"], (cx, cy), 3)
        
        # RPM value text in center
        rpm_text = str(int(rpm))
        rpm_surf = font_value.render(rpm_text, True, COLORS["text_highlight"])
        surface.blit(rpm_surf, (cx - rpm_surf.get_width() // 2, cy + 6))
        
        # RPM label below value
        lbl_surf = font_label.render("RPM", True, COLORS["text_secondary"])
        surface.blit(lbl_surf, (cx - lbl_surf.get_width() // 2, cy + 20))
    
    def _render_time_graph(
        self,
        surface: pygame.Surface,
        x: int, y: int, w: int, h: int,
        title: str,
        data: list,
        time_window: int,
        min_val: float, max_val: float,
        unit: str,
        color: tuple,
        warn_threshold: float = None,
        crit_threshold: float = None
    ) -> None:
        """Render a time-series line graph with cyberpunk styling.
        
        Args:
            x, y, w, h: Graph area bounds
            title: Graph title
            data: List of (timestamp, value) tuples
            time_window: Time window in seconds
            min_val, max_val: Y-axis range
            unit: Value unit label
            color: Primary line color
            warn_threshold: Yellow warning threshold
            crit_threshold: Red critical threshold
        """
        font_title = get_font(8, "title")
        font_label = get_font(7)
        font_value_sm = get_font(7, "mono")
        
        # Title
        title_surf = font_title.render(title, True, COLORS["cyan_bright"])
        surface.blit(title_surf, (x, y))
        
        # Graph area (below title)
        gy = y + 12
        gh = h - 12
        gw = w - 24  # Leave room for Y-axis labels
        gx = x + 24
        
        # Background
        bg_rect = pygame.Rect(gx, gy, gw, gh)
        pygame.draw.rect(surface, COLORS["bg_panel"], bg_rect)
        pygame.draw.rect(surface, COLORS["border_normal"], bg_rect, 1)
        
        # Y-axis grid lines and labels
        num_y_lines = 4
        val_range = max_val - min_val
        for i in range(num_y_lines + 1):
            t = i / num_y_lines
            line_y = gy + gh - int(t * gh)
            val = min_val + t * val_range
            
            # Grid line (dotted effect)
            for gx_dot in range(gx, gx + gw, 4):
                pygame.draw.rect(surface, COLORS["border_normal"], (gx_dot, line_y, 1, 1))
            
            # Label
            lbl_text = f"{val:.0f}"
            lbl_surf = font_value_sm.render(lbl_text, True, COLORS["text_dim"])
            surface.blit(lbl_surf, (x, line_y - lbl_surf.get_height() // 2))
        
        # Threshold lines
        if warn_threshold is not None and val_range > 0:
            warn_y = gy + gh - int(((warn_threshold - min_val) / val_range) * gh)
            if gy <= warn_y <= gy + gh:
                for gx_dot in range(gx, gx + gw, 6):
                    pygame.draw.rect(surface, COLORS["yellow"], (gx_dot, warn_y, 3, 1))
        
        if crit_threshold is not None and val_range > 0:
            crit_y = gy + gh - int(((crit_threshold - min_val) / val_range) * gh)
            if gy <= crit_y <= gy + gh:
                for gx_dot in range(gx, gx + gw, 6):
                    pygame.draw.rect(surface, COLORS["red_bright"], (gx_dot, crit_y, 3, 1))
        
        # Plot data - bucket-average per pixel column for accurate representation
        if len(data) >= 2:
            now = time.time()
            
            # Bucket data: each pixel column = time_window/gw seconds
            # Collect values per pixel column, then average
            buckets = {}  # px -> list of values
            
            for ts, val in data:
                age = now - ts
                if age > time_window or age < 0:
                    continue
                
                # X position: right = now, left = oldest
                px = gx + gw - int((age / time_window) * gw)
                px = max(gx, min(gx + gw, px))
                
                if px not in buckets:
                    buckets[px] = []
                buckets[px].append(val)
            
            if buckets:
                # Average each bucket and build points
                points = []
                for px in sorted(buckets.keys()):
                    vals = buckets[px]
                    avg_val = sum(vals) / len(vals)
                    
                    # Y position: bottom = min, top = max
                    clamped = max(min_val, min(max_val, avg_val))
                    py = gy + gh - int(((clamped - min_val) / val_range) * gh)
                    points.append((px, py))
                
                if len(points) >= 2:
                    # Draw glow effect (thicker, dimmer line behind)
                    glow_color = dim_color(color, 0.3)
                    pygame.draw.lines(surface, glow_color, False, points, 3)
                    
                    # Draw main line
                    pygame.draw.lines(surface, color, False, points, 1)
                
                # Draw current value at the top-right
                if data:
                    last_val = data[-1][1]
                    val_color = color
                    if crit_threshold is not None and last_val >= crit_threshold:
                        val_color = COLORS["red_bright"]
                    elif warn_threshold is not None and last_val >= warn_threshold:
                        val_color = COLORS["yellow"]
                    
                    val_text = f"{last_val:.1f}{unit}"
                    val_surf = font_label.render(val_text, True, val_color)
                    surface.blit(val_surf, (gx + gw - val_surf.get_width(), gy - 1))
        else:
            # No data - show placeholder
            no_data = font_label.render("NO DATA", True, COLORS["text_dim"])
            surface.blit(no_data, (gx + (gw - no_data.get_width()) // 2, gy + gh // 2 - 5))
        
        # X-axis time labels
        time_labels = [(0, "now"), (time_window // 4, f"-{time_window // 240}m"),
                       (time_window // 2, f"-{time_window // 120}m"),
                       (time_window * 3 // 4, f"-{time_window * 3 // 240}m"),
                       (time_window, f"-{time_window // 60}m")]
        for offset, label in time_labels:
            lx = gx + gw - int((offset / time_window) * gw)
            if gx <= lx <= gx + gw:
                lbl_surf = font_value_sm.render(label, True, COLORS["text_dim"])
                surface.blit(lbl_surf, (lx - lbl_surf.get_width() // 2, gy + gh + 2))

    def _render_avc_lan_debug(
        self,
        surface: pygame.Surface,
        center_x: int,
        center_width: int
    ) -> None:
        """
        Render AVC-LAN byte values for manual correlation with driving state.
        
        Shows:
        - 0x110→0x490 bytes (MFD status - flow arrows)
        - 0xA00→0x258 bytes (SOC/energy data)
        """
        font_small = get_font(8, "mono")
        font_label = get_font(9)
        
        # Position in top-right of center area
        debug_x = center_x + center_width - 200
        debug_y = 5
        
        # Draw semi-transparent background
        bg_rect = pygame.Rect(debug_x - 5, debug_y - 2, 195, 58)
        bg_surface = pygame.Surface((bg_rect.width, bg_rect.height), pygame.SRCALPHA)
        bg_surface.fill((0, 0, 0, 180))
        surface.blit(bg_surface, (bg_rect.x, bg_rect.y))
        
        # Title
        title_surf = font_label.render("AVC-LAN DEBUG", True, COLORS["cyan_bright"])
        surface.blit(title_surf, (debug_x, debug_y))
        debug_y += 12
        
        # 0x110→0x490 (MFD Status - Flow Arrows)
        label_surf = font_label.render("110→490:", True, COLORS["text_secondary"])
        surface.blit(label_surf, (debug_x, debug_y))
        
        # Show bytes in hex
        bytes_text = " ".join(f"{b:02X}" for b in self._avc_110_490_bytes)
        bytes_surf = font_small.render(bytes_text, True, COLORS["green_bright"])
        surface.blit(bytes_surf, (debug_x + 50, debug_y))
        debug_y += 11
        
        # Highlight key discriminating bytes
        key_bytes_text = f"[1]={self._avc_110_490_bytes[1]:02X} [2]={self._avc_110_490_bytes[2]:02X} [3]={self._avc_110_490_bytes[3]:02X} [5]={self._avc_110_490_bytes[5]:02X}"
        key_surf = font_small.render(key_bytes_text, True, COLORS["yellow"])
        surface.blit(key_surf, (debug_x + 50, debug_y))
        debug_y += 13
        
        # 0xA00→0x258 (SOC/Energy) - show first 8 bytes
        label_surf = font_label.render("A00→258:", True, COLORS["text_secondary"])
        surface.blit(label_surf, (debug_x, debug_y))
        
        bytes_text = " ".join(f"{b:02X}" for b in self._avc_a00_258_bytes[:8])
        bytes_surf = font_small.render(bytes_text, True, COLORS["green_bright"])
        surface.blit(bytes_surf, (debug_x + 50, debug_y))
        debug_y += 11
    
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
    
    def _on_engine_action(self) -> None:
        """Handle engine frame action (open engine settings screen)."""
        if not self.app:
            return
        
        # Get current time base from state (default to 60s)
        current_timebase = 60
        if self._store:
            current_timebase = self._store.state.display.power_chart_time_base
        
        engine_screen = EngineScreen(
            (self.width, self.height),
            self.app,
            initial_timebase=current_timebase
        )
        self.app.push_screen(engine_screen)

    def _on_battery_action(self) -> None:
        """Handle battery frame action (open EV/Battery detail screen)."""
        if not self.app:
            return

        from .ev_screen import EVScreen

        ev_screen = EVScreen(
            (self.width, self.height),
            self.app,
            store=self._store
        )
        self.app.push_screen(ev_screen)

    def _on_avc_monitor_action(self) -> None:
        """Handle AVC monitor action (open AVC-LAN bus monitor)."""
        if not self.app:
            return

        from .avc_monitor_screen import AVCMonitorScreen

        avc_screen = AVCMonitorScreen(
            (self.width, self.height),
            self.app,
            store=self._store,
        )
        self.app.push_screen(avc_screen)

    def _on_diag_action(self) -> None:
        """Handle diagnostics action (open DTC screen)."""
        if not self.app:
            return

        from .dtc_screen import DTCScreen

        dtc_screen = DTCScreen(
            (self.width, self.height),
            self.app,
            store=self._store
        )
        self.app.push_screen(dtc_screen)
