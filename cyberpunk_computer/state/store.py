"""
Store - Central state container with subscriptions.

The Store is the single source of truth. It:
- Holds the current AppState
- Processes Actions to create new state
- Notifies subscribers of state changes
- Supports middleware for side effects
"""

import logging
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Set
from enum import Enum, auto

from .app_state import (
    AppState, AudioState, ClimateState, VehicleState, 
    EnergyState, ConnectionState, GearPosition, InputState, DisplayState,
    DynamicsState
)
from .chart_data import ChartDataStore
from .actions import (
    Action, ActionType, ActionSource, BatchAction,
    SetVolumeAction, SetBassAction, SetMidAction, SetTrebleAction,
    SetBalanceAction, SetFaderAction, SetMuteAction,
    SetTargetTempAction, SetFanSpeedAction, SetACAction, SetAutoModeAction,
    SetReadyModeAction, SetParkModeAction,
    SetBatterySOCAction, SetChargingStateAction,
    SetConnectionStateAction,
    SetSpeedAction, SetICECoolantTempAction, SetInverterTempAction,
    SetBatteryVoltageAction, SetBatteryCurrentAction, SetBatteryTempAction,
    SetBatteryDeltaSOCAction, SetBlockVoltagesAction,
    SetStoredDTCsAction, SetPendingDTCsAction, SetDTCScanStateAction,
    UpdateDebugInfoAction, AVCButtonPressAction, AVCTouchEventAction,
    SetPowerChartTimeBaseAction,
    SetPowerboxTelemetryAction, SetPowerboxIgnitionAction,
    SetPowerboxConnectionAction, SetPowerboxUndervoltageAction,
    RequestPowerboxShutdownAction,
)

logger = logging.getLogger(__name__)


class StateSlice(Enum):
    """State slices for selective subscriptions."""
    AUDIO = auto()
    CLIMATE = auto()
    VEHICLE = auto()
    ENERGY = auto()
    CONNECTION = auto()
    DEBUG = auto()
    INPUT = auto()  # AVC button/touch input events
    DISPLAY = auto()  # Display settings (power chart time base, etc.)
    DYNAMICS = auto()  # Vehicle dynamics (steering, accel, wheel pulses, etc.)
    VFD_SATELLITE = auto()  # VFD satellite display state (device 110)
    DIAGNOSTICS = auto()  # OBD-II DTC diagnostics
    POWERBOX = auto()  # Powerbox: ignition (ACC/BATT) + INA219 telemetry (devices 200-202)
    ALL = auto()


# Type aliases
Subscriber = Callable[[AppState], None]
Middleware = Callable[[Action, "Store"], None]


class Store:
    """
    Central state store.
    
    Usage:
        store = Store()
        
        # Subscribe to changes
        store.subscribe(StateSlice.AUDIO, on_audio_change)
        
        # Dispatch actions
        store.dispatch(SetVolumeAction(50, source=ActionSource.UI))
        
        # Add middleware for side effects
        store.add_middleware(gateway_middleware)
        
        # Enable verbose logging for debugging
        store.verbose = True
    """
    
    def __init__(self, initial_state: Optional[AppState] = None, verbose: bool = False):
        """
        Initialize store with optional initial state.
        
        Args:
            initial_state: Starting state (defaults to AppState())
            verbose: If True, log all state changes to console
        """
        self._state = initial_state or AppState()
        self._subscribers: Dict[StateSlice, List[Subscriber]] = {
            slice_: [] for slice_ in StateSlice
        }
        self._middleware: List[Middleware] = []
        self._dispatching = False
        self._pending_actions: List[Action] = []
        self._verbose = verbose
        self._replay_speed: float = 1.0
        
        # Mutable time-series store for chart data (fed by ChartDataRule)
        self.chart_data = ChartDataStore()
    
    @property
    def verbose(self) -> bool:
        """Get verbose logging mode."""
        return self._verbose
    
    @verbose.setter
    def verbose(self, value: bool) -> None:
        """Set verbose logging mode."""
        self._verbose = value

    @property
    def replay_speed(self) -> float:
        """Playback speed multiplier (1.0 = realtime, 10.0 = fast-forward)."""
        return self._replay_speed

    @replay_speed.setter
    def replay_speed(self, value: float) -> None:
        """Set playback speed multiplier."""
        self._replay_speed = max(0.1, value)

    @property
    def state(self) -> AppState:
        """Get current state (read-only)."""
        return self._state
    
    def subscribe(
        self, 
        slice_: StateSlice, 
        callback: Subscriber
    ) -> Callable[[], None]:
        """
        Subscribe to state changes.
        
        Args:
            slice_: Which part of state to watch
            callback: Function called with new state
            
        Returns:
            Unsubscribe function
        """
        self._subscribers[slice_].append(callback)
        
        def unsubscribe():
            if callback in self._subscribers[slice_]:
                self._subscribers[slice_].remove(callback)
        
        return unsubscribe
    
    def add_middleware(self, middleware: Middleware) -> None:
        """
        Add middleware for side effects.
        
        Middleware is called AFTER state is updated.
        Use for: sending commands to Gateway, logging, etc.
        
        Args:
            middleware: Function(action, store) called after dispatch
        """
        self._middleware.append(middleware)
    
    def dispatch(self, action: Action) -> None:
        """
        Dispatch an action to update state.
        
        Args:
            action: Action describing the state change
        """
        if self._dispatching:
            # Queue action if we're already dispatching
            self._pending_actions.append(action)
            return
        
        self._dispatching = True
        old_state = self._state
        
        try:
            # Handle batch actions
            if isinstance(action, BatchAction):
                affected_slices: Set[StateSlice] = set()
                for sub_action in action.actions:
                    slices = self._reduce(sub_action)
                    affected_slices.update(slices)
                    if self._verbose:
                        self._log_state_change(sub_action, old_state, self._state)
                        old_state = self._state
                self._notify(affected_slices)
            else:
                affected_slices = self._reduce(action)
                if self._verbose and affected_slices:
                    self._log_state_change(action, old_state, self._state)
                self._notify(affected_slices)
            
            # Run middleware
            for middleware in self._middleware:
                try:
                    middleware(action, self)
                except Exception as e:
                    logger.error(f"Middleware error: {e}")
                    
        finally:
            self._dispatching = False
            
            # Process any queued actions
            while self._pending_actions:
                pending = self._pending_actions.pop(0)
                self.dispatch(pending)
    
    def _reduce(self, action: Action) -> Set[StateSlice]:
        """
        Apply action to state, return affected slices.
        
        Args:
            action: Action to process
            
        Returns:
            Set of affected state slices
        """
        affected: Set[StateSlice] = set()
        
        # Audio reducers
        if action.type == ActionType.SET_VOLUME:
            a = action  # type: SetVolumeAction
            self._state = replace(
                self._state,
                audio=self._state.audio.with_volume(a.volume)
            )
            affected.add(StateSlice.AUDIO)
            
        elif action.type == ActionType.SET_BASS:
            a = action  # type: SetBassAction
            self._state = replace(
                self._state,
                audio=self._state.audio.with_bass(a.bass)
            )
            affected.add(StateSlice.AUDIO)
            
        elif action.type == ActionType.SET_MID:
            a = action  # type: SetMidAction
            self._state = replace(
                self._state,
                audio=self._state.audio.with_mid(a.mid)
            )
            affected.add(StateSlice.AUDIO)
            
        elif action.type == ActionType.SET_TREBLE:
            a = action  # type: SetTrebleAction
            self._state = replace(
                self._state,
                audio=self._state.audio.with_treble(a.treble)
            )
            affected.add(StateSlice.AUDIO)
            
        elif action.type == ActionType.SET_BALANCE:
            a = action  # type: SetBalanceAction
            self._state = replace(
                self._state,
                audio=self._state.audio.with_balance(a.balance)
            )
            affected.add(StateSlice.AUDIO)
            
        elif action.type == ActionType.SET_FADER:
            a = action  # type: SetFaderAction
            self._state = replace(
                self._state,
                audio=self._state.audio.with_fader(a.fader)
            )
            affected.add(StateSlice.AUDIO)
            
        elif action.type == ActionType.SET_MUTE:
            a = action  # type: SetMuteAction
            self._state = replace(
                self._state,
                audio=replace(self._state.audio, muted=a.muted)
            )
            affected.add(StateSlice.AUDIO)
            
        # Climate reducers
        elif action.type == ActionType.SET_TARGET_TEMP:
            a = action  # type: SetTargetTempAction
            self._state = replace(
                self._state,
                climate=self._state.climate.with_target_temp(a.temp)
            )
            affected.add(StateSlice.CLIMATE)
            
        elif action.type == ActionType.SET_FAN_SPEED:
            a = action  # type: SetFanSpeedAction
            self._state = replace(
                self._state,
                climate=self._state.climate.with_fan_speed(a.speed)
            )
            affected.add(StateSlice.CLIMATE)
            
        elif action.type == ActionType.SET_AC:
            a = action  # type: SetACAction
            self._state = replace(
                self._state,
                climate=replace(self._state.climate, ac_on=a.ac_on)
            )
            affected.add(StateSlice.CLIMATE)
            
        elif action.type == ActionType.SET_AUTO_MODE:
            a = action  # type: SetAutoModeAction
            self._state = replace(
                self._state,
                climate=replace(self._state.climate, auto_mode=a.auto_mode)
            )
            affected.add(StateSlice.CLIMATE)
            
        elif action.type == ActionType.SET_OUTSIDE_TEMP:
            from .actions import SetOutsideTempAction
            a = action  # type: SetOutsideTempAction
            self._state = replace(
                self._state,
                climate=replace(self._state.climate, outside_temp=a.temp)
            )
            affected.add(StateSlice.CLIMATE)
            
        # Vehicle reducers
        elif action.type == ActionType.SET_READY_MODE:
            a = action  # type: SetReadyModeAction
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, ready_mode=a.ready)
            )
            affected.add(StateSlice.VEHICLE)
            
        elif action.type == ActionType.SET_PARK_MODE:
            a = action  # type: SetParkModeAction
            gear = GearPosition.PARK if a.parked else GearPosition.DRIVE
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, gear=gear)
            )
            affected.add(StateSlice.VEHICLE)
            
        elif action.type == ActionType.SET_GEAR:
            a = action  # type: SetGearAction
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, gear=a.gear)
            )
            affected.add(StateSlice.VEHICLE)
        
        elif action.type == ActionType.SET_SPEED:
            speed = action.speed_kmh
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, speed_kmh=speed)
            )
            affected.add(StateSlice.VEHICLE)
        
        elif action.type == ActionType.SET_RPM:
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, rpm=action.rpm)
            )
            affected.add(StateSlice.VEHICLE)
        
        elif action.type == ActionType.SET_ICE_RUNNING:
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, ice_running=action.running)
            )
            affected.add(StateSlice.VEHICLE)
        
        elif action.type == ActionType.SET_ICE_COOLANT_TEMP:
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, ice_coolant_temp=action.temp)
            )
            affected.add(StateSlice.VEHICLE)
        
        elif action.type == ActionType.SET_INVERTER_TEMP:
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, inverter_temp=action.temp)
            )
            affected.add(StateSlice.VEHICLE)

        elif action.type == ActionType.SET_HYBRID_TEMPS:
            updates = {}
            if action.mg1_inverter_temp is not None:
                updates["mg1_inverter_temp"] = action.mg1_inverter_temp
            if action.mg2_inverter_temp is not None:
                updates["mg2_inverter_temp"] = action.mg2_inverter_temp
            if action.mg1_motor_temp is not None:
                updates["mg1_motor_temp"] = action.mg1_motor_temp
            if action.mg2_motor_temp is not None:
                updates["mg2_motor_temp"] = action.mg2_motor_temp
            if action.converter_temp is not None:
                updates["converter_temp"] = action.converter_temp
            if updates:
                self._state = replace(
                    self._state,
                    vehicle=replace(self._state.vehicle, **updates)
                )
                affected.add(StateSlice.VEHICLE)

        elif action.type == ActionType.SET_MG_RPMS:
            updates = {}
            if action.mg1_rpm is not None:
                updates["mg1_rpm"] = action.mg1_rpm
            if action.mg2_rpm is not None:
                updates["mg2_rpm"] = action.mg2_rpm
            if updates:
                self._state = replace(
                    self._state,
                    vehicle=replace(self._state.vehicle, **updates)
                )
                affected.add(StateSlice.VEHICLE)

        elif action.type == ActionType.SET_DRIVETRAIN_TORQUES:
            updates = {}
            for field_name in (
                "engine_load_percent",
                "mg2_torque", "mg1_torque",
                "regen_torque_actual", "regen_torque_request",
                "master_cylinder_torque",
                "ice_rpm_target", "ice_rpm_actual",
                "drive_condition", "shift_sensor_position",
            ):
                val = getattr(action, field_name, None)
                if val is not None:
                    updates[field_name] = val
            if updates:
                self._state = replace(
                    self._state,
                    vehicle=replace(self._state.vehicle, **updates)
                )
                affected.add(StateSlice.VEHICLE)

        elif action.type == ActionType.SET_ENGINE_DATA:
            updates = {}
            for field_name in (
                "intake_air_temp", "maf_air_flow",
                "lambda_ratio", "o2_sensor_voltage",
                "odometer_dtc_clear", "barometric_pressure",
                "aux_battery_voltage", "ambient_air_temp",
            ):
                val = getattr(action, field_name, None)
                if val is not None:
                    updates[field_name] = val
            if updates:
                self._state = replace(
                    self._state,
                    vehicle=replace(self._state.vehicle, **updates)
                )
                affected.add(StateSlice.VEHICLE)

        elif action.type == ActionType.SET_HYBRID_EXTRAS:
            updates = {}
            for field_name in (
                "aircon_power_kw", "crank_position",
                "system_relay_1", "system_relay_2", "system_relay_3",
                "engine_requests_a", "engine_requests_b",
            ):
                val = getattr(action, field_name, None)
                if val is not None:
                    updates[field_name] = val
            if updates:
                self._state = replace(
                    self._state,
                    vehicle=replace(self._state.vehicle, **updates)
                )
                affected.add(StateSlice.VEHICLE)

        elif action.type == ActionType.SET_CRUISE_CONTROL:
            updates = {}
            if action.cruise_active is not None:
                updates["cruise_active"] = action.cruise_active
                if not action.cruise_active:
                    updates["cruise_set_speed"] = None
            if action.cruise_set_speed is not None:
                updates["cruise_set_speed"] = action.cruise_set_speed
            if action.cruise_memory_speed is not None:
                updates["cruise_memory_speed"] = action.cruise_memory_speed
            if action.cruise_main_switch is not None:
                updates["cruise_main_switch"] = action.cruise_main_switch
            if action.cruise_main_ready is not None:
                updates["cruise_main_ready"] = action.cruise_main_ready
            if action.cruise_indicator is not None:
                updates["cruise_indicator"] = action.cruise_indicator
            if action.cruise_cancel_switch is not None:
                updates["cruise_cancel_switch"] = action.cruise_cancel_switch
            if action.cruise_res_acc_switch is not None:
                updates["cruise_res_acc_switch"] = action.cruise_res_acc_switch
            if action.cruise_set_coast_switch is not None:
                updates["cruise_set_coast_switch"] = action.cruise_set_coast_switch
            if action.cruise_5c8_debug is not None:
                updates["cruise_5c8_debug"] = action.cruise_5c8_debug
            if updates:
                self._state = replace(
                    self._state,
                    vehicle=replace(self._state.vehicle, **updates)
                )
                affected.add(StateSlice.VEHICLE)

        elif action.type == ActionType.SET_THROTTLE_POSITION:
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, throttle_position=action.position)
            )
            affected.add(StateSlice.VEHICLE)

        elif action.type == ActionType.SET_BRAKE_PRESSED:
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, brake_pressed=action.pressure)
            )
            affected.add(StateSlice.VEHICLE)

        elif action.type == ActionType.SET_FUEL_LEVEL:
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, fuel_level=action.liters)
            )
            affected.add(StateSlice.VEHICLE)

        elif action.type == ActionType.SET_LPG_LEVEL:
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, lpg_level=action.liters)
            )
            affected.add(StateSlice.VEHICLE)

        elif action.type == ActionType.SET_ACTIVE_FUEL:
            from .app_state import FuelType
            fuel_type = FuelType[action.fuel_type] if action.fuel_type in FuelType.__members__ else FuelType.OFF
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, active_fuel=fuel_type)
            )
            affected.add(StateSlice.VEHICLE)

        elif action.type == ActionType.SET_FUEL_FLOW:
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, fuel_flow_rate=action.flow_rate)
            )
            affected.add(StateSlice.VEHICLE)

        elif action.type == ActionType.SET_INSTANT_CONSUMPTION:
            self._state = replace(
                self._state,
                vehicle=replace(
                    self._state.vehicle, 
                    instant_consumption=action.value,
                    consumption_unit=action.unit
                )
            )
            affected.add(StateSlice.VEHICLE)

        elif action.type == ActionType.SET_TRIP_FUEL_CONSUMED:
            self._state = replace(
                self._state,
                vehicle=replace(self._state.vehicle, trip_fuel_consumed=action.liters)
            )
            affected.add(StateSlice.VEHICLE)
            
        # Energy reducers
        elif action.type == ActionType.SET_BATTERY_SOC:
            a = action  # type: SetBatterySOCAction
            soc = max(0.0, min(1.0, a.soc))
            self._state = replace(
                self._state,
                energy=replace(self._state.energy, battery_soc=soc)
            )
            affected.add(StateSlice.ENERGY)
            
        elif action.type == ActionType.SET_CHARGING_STATE:
            a = action  # type: SetChargingStateAction
            self._state = replace(
                self._state,
                energy=replace(
                    self._state.energy,
                    charging=a.charging,
                    discharging=a.discharging
                )
            )
            affected.add(StateSlice.ENERGY)
        
        elif action.type == ActionType.SET_BATTERY_VOLTAGE:
            self._state = replace(
                self._state,
                energy=replace(self._state.energy, hv_battery_voltage=action.voltage)
            )
            affected.add(StateSlice.ENERGY)
        
        elif action.type == ActionType.SET_BATTERY_CURRENT:
            self._state = replace(
                self._state,
                energy=replace(self._state.energy, hv_battery_current=action.current)
            )
            affected.add(StateSlice.ENERGY)
        
        elif action.type == ActionType.SET_BATTERY_TEMP:
            self._state = replace(
                self._state,
                energy=replace(self._state.energy, battery_temp=action.temp)
            )
            affected.add(StateSlice.ENERGY)
            
        elif action.type == ActionType.SET_BATTERY_MAX_TEMP:
            self._state = replace(
                self._state,
                energy=replace(self._state.energy, battery_max_cell_temp=action.temp)
            )
            affected.add(StateSlice.ENERGY)
        
        elif action.type == ActionType.SET_BATTERY_DELTA_SOC:
            self._state = replace(
                self._state,
                energy=replace(self._state.energy, battery_delta_soc=action.delta_soc)
            )
            affected.add(StateSlice.ENERGY)

        elif action.type == ActionType.SET_BLOCK_VOLTAGES:
            self._state = replace(
                self._state,
                energy=replace(self._state.energy, block_voltages=action.voltages)
            )
            affected.add(StateSlice.ENERGY)

        elif action.type == ActionType.SET_BLOCK_RESISTANCES:
            self._state = replace(
                self._state,
                energy=replace(self._state.energy, block_resistances=action.resistances)
            )
            affected.add(StateSlice.ENERGY)

        elif action.type == ActionType.SET_BATTERY_FAN_SPEED:
            self._state = replace(
                self._state,
                energy=replace(self._state.energy, battery_fan_speed=action.fan_speed)
            )
            affected.add(StateSlice.ENERGY)

        elif action.type == ActionType.SET_ENERGY_FLOW_FLAGS:
            from .actions import SetEnergyFlowFlagsAction
            a = action # type: SetEnergyFlowFlagsAction
            self._state = replace(
                self._state,
                energy=replace(
                    self._state.energy,
                    flow_engine_to_wheels=a.engine_to_wheels,
                    flow_battery_to_motor=a.battery_to_motor,
                    flow_motor_to_battery=a.motor_to_battery,
                    flow_engine_to_battery=a.engine_to_battery,
                    flow_battery_to_wheels=a.battery_to_wheels
                )
            )
            affected.add(StateSlice.ENERGY)
            
        # Connection reducers
        elif action.type == ActionType.SET_CONNECTION_STATE:
            a = action  # type: SetConnectionStateAction
            self._state = replace(
                self._state,
                connection=replace(
                    self._state.connection,
                    connected=a.connected,
                    gateway_version=a.gateway_version or self._state.connection.gateway_version
                )
            )
            affected.add(StateSlice.CONNECTION)
        
        # AVC Input reducers (buttons and touch)
        elif action.type == ActionType.AVC_BUTTON_PRESS:
            import time
            a = action  # type: AVCButtonPressAction
            # Keep recent buttons history (max 5)
            recent = self._state.input.recent_buttons
            new_recent = (a.button_code,) + recent[:4]
            
            self._state = replace(
                self._state,
                input=replace(
                    self._state.input,
                    last_button_code=a.button_code,
                    last_button_name=a.button_name,
                    last_button_time=time.time(),
                    button_press_count=self._state.input.button_press_count + 1,
                    recent_buttons=new_recent
                )
            )
            affected.add(StateSlice.INPUT)
            
        elif action.type == ActionType.AVC_TOUCH_EVENT:
            import time
            a = action  # type: AVCTouchEventAction
            self._state = replace(
                self._state,
                input=replace(
                    self._state.input,
                    last_touch_x=a.x,
                    last_touch_y=a.y,
                    last_touch_type=a.touch_type,
                    last_touch_time=time.time(),
                    touch_event_count=self._state.input.touch_event_count + 1
                )
            )
            affected.add(StateSlice.INPUT)
        
        elif action.type == ActionType.AVC_DEBUG_BYTES:
            from ..state.actions import AVCDebugBytesAction
            a = action  # type: AVCDebugBytesAction
            
            # Update appropriate byte array based on message address
            new_input = self._state.input
            if a.master_addr == 0x110 and a.slave_addr == 0x490:
                # MFD status/flow arrows
                new_input = replace(
                    new_input,
                    last_avc_110_490_bytes=tuple(a.data[:8])
                )
            elif a.master_addr == 0xA00 and a.slave_addr == 0x258:
                # SOC/energy broadcast
                new_input = replace(
                    new_input,
                    last_avc_a00_258_bytes=tuple(a.data[:32])
                )
            
            if new_input != self._state.input:
                self._state = replace(self._state, input=new_input)
                affected.add(StateSlice.INPUT)
        
        # Display reducers
        elif action.type == ActionType.SET_POWER_CHART_TIME_BASE:
            a = action  # type: SetPowerChartTimeBaseAction
            self._state = replace(
                self._state,
                display=self._state.display.with_time_base(a.time_base)
            )
            affected.add(StateSlice.DISPLAY)
        
        # Diagnostics reducers
        elif action.type == ActionType.SET_STORED_DTCS:
            import time
            self._state = replace(
                self._state,
                diagnostics=replace(
                    self._state.diagnostics,
                    stored_dtcs=action.dtcs,
                    dtc_count=len(action.dtcs),
                    mil_on=action.mil_on,
                    last_scan_time=time.time(),
                    scan_in_progress=False
                )
            )
            affected.add(StateSlice.DIAGNOSTICS)
        
        elif action.type == ActionType.SET_PENDING_DTCS:
            self._state = replace(
                self._state,
                diagnostics=replace(
                    self._state.diagnostics,
                    pending_dtcs=action.dtcs
                )
            )
            affected.add(StateSlice.DIAGNOSTICS)
        
        elif action.type == ActionType.SET_DTC_SCAN_STATE:
            self._state = replace(
                self._state,
                diagnostics=replace(
                    self._state.diagnostics,
                    scan_in_progress=action.in_progress
                )
            )
            affected.add(StateSlice.DIAGNOSTICS)
        
        # Dynamics reducers
        elif action.type == ActionType.SET_STEERING_ANGLE:
            self._state = replace(
                self._state,
                dynamics=replace(
                    self._state.dynamics,
                    steering_angle=action.angle,
                    steering_angle_raw=action.angle_raw
                )
            )
            affected.add(StateSlice.DYNAMICS)
        
        elif action.type == ActionType.SET_ACCELERATION:
            kwargs = {}
            if action.lateral_raw is not None:
                kwargs["lateral_accel_raw"] = action.lateral_raw
            if action.longitudinal_raw is not None:
                kwargs["longitudinal_accel_raw"] = action.longitudinal_raw
            if kwargs:
                self._state = replace(
                    self._state,
                    dynamics=replace(self._state.dynamics, **kwargs)
                )
                affected.add(StateSlice.DYNAMICS)
        
        elif action.type == ActionType.SET_YAW_RATE:
            self._state = replace(
                self._state,
                dynamics=replace(
                    self._state.dynamics,
                    yaw_rate_raw=action.yaw_rate_raw
                )
            )
            affected.add(StateSlice.DYNAMICS)
        
        elif action.type == ActionType.SET_WHEEL_SPEED:
            kwargs = {}
            if action.front_right is not None:
                kwargs["front_right_speed"] = action.front_right
            if action.front_left is not None:
                kwargs["front_left_speed"] = action.front_left
            if action.rear_right is not None:
                kwargs["rear_right_speed"] = action.rear_right
            if action.rear_left is not None:
                kwargs["rear_left_speed"] = action.rear_left
            if kwargs:
                self._state = replace(
                    self._state,
                    dynamics=replace(self._state.dynamics, **kwargs)
                )
                affected.add(StateSlice.DYNAMICS)
        
        elif action.type == ActionType.SET_HEADLIGHT_STATUS:
            self._state = replace(
                self._state,
                dynamics=replace(
                    self._state.dynamics,
                    headlight_state=action.headlight_state,
                    parking_lights=action.parking_lights,
                    low_beam=action.low_beam,
                    high_beam=action.high_beam,
                    drl_active=action.drl_active
                )
            )
            affected.add(StateSlice.DYNAMICS)
        
        elif action.type == ActionType.SET_SOC_BARS_EVENT:
            self._state = replace(
                self._state,
                dynamics=replace(
                    self._state.dynamics,
                    soc_bars=action.soc_bars,
                    ev_mode_active=action.ev_mode_active,
                    warning_triangle=action.warning_triangle
                )
            )
            affected.add(StateSlice.DYNAMICS)
        
        # VFD Satellite reducers
        elif action.type == ActionType.UPDATE_VFD_SATELLITE:
            from .actions import UpdateVFDSatelliteAction
            a = action  # type: UpdateVFDSatelliteAction
            
            # Build kwargs for fields that are set (not None)
            kwargs = {}
            for field_name in [
                'mg_power', 'fuel_flow', 'brake', 'speed', 'battery_soc',
                'petrol_level', 'lpg_level', 'ice_running',
                'active_fuel', 'gear', 'ready_mode',
                'time_base', 'brightness',
                'last_energy_send_time', 'last_state_send_time', 'needs_config_send'
            ]:
                value = getattr(a, field_name, None)
                if value is not None:
                    kwargs[field_name] = value
            
            if kwargs:
                self._state = replace(
                    self._state,
                    vfd_satellite=replace(self._state.vfd_satellite, **kwargs)
                )
                affected.add(StateSlice.VFD_SATELLITE)

        # Powerbox reducers (second RP2040, devices 200-202)
        elif action.type == ActionType.SET_POWERBOX_TELEMETRY:
            import time as _t
            kwargs = {"connected": True, "last_update_time": _t.time()}
            if action.voltage is not None:
                kwargs["system_voltage"] = action.voltage
            if action.current is not None:
                kwargs["current_draw_a"] = action.current
            if action.power is not None:
                kwargs["power_draw_w"] = action.power
            self._state = replace(
                self._state,
                powerbox=replace(self._state.powerbox, **kwargs),
            )
            affected.add(StateSlice.POWERBOX)

        elif action.type == ActionType.SET_POWERBOX_IGNITION:
            import time as _t
            self._state = replace(
                self._state,
                powerbox=replace(
                    self._state.powerbox,
                    connected=True,
                    acc_on=action.acc_on,
                    batt_present=action.batt_present,
                    last_update_time=_t.time(),
                ),
            )
            affected.add(StateSlice.POWERBOX)

        elif action.type == ActionType.SET_POWERBOX_CONNECTION:
            self._state = replace(
                self._state,
                powerbox=replace(self._state.powerbox, connected=action.connected),
            )
            affected.add(StateSlice.POWERBOX)

        elif action.type == ActionType.SET_POWERBOX_UNDERVOLTAGE:
            self._state = replace(
                self._state,
                powerbox=replace(self._state.powerbox, undervoltage=action.undervoltage),
            )
            affected.add(StateSlice.POWERBOX)

        elif action.type == ActionType.REQUEST_POWERBOX_SHUTDOWN:
            self._state = replace(
                self._state,
                powerbox=replace(
                    self._state.powerbox,
                    shutdown_requested=True,
                    shutdown_reason=action.reason,
                ),
            )
            affected.add(StateSlice.POWERBOX)

        return affected
    
    def _log_state_change(self, action: Action, old_state: AppState, new_state: AppState) -> None:
        """Log state changes when verbose mode is enabled."""
        action_name = action.type.name if hasattr(action, 'type') else type(action).__name__
        source = action.source.name if hasattr(action, 'source') else 'UNKNOWN'
        
        # Build a summary of what changed
        changes = []
        
        # Check vehicle state changes
        if old_state.vehicle != new_state.vehicle:
            v_old, v_new = old_state.vehicle, new_state.vehicle
            if v_old.speed_kmh != v_new.speed_kmh:
                changes.append(f"speed: {v_old.speed_kmh} -> {v_new.speed_kmh} km/h")
            if v_old.rpm != v_new.rpm:
                changes.append(f"rpm: {v_old.rpm} -> {v_new.rpm}")
            if v_old.ready_mode != v_new.ready_mode:
                changes.append(f"ready: {v_old.ready_mode} -> {v_new.ready_mode}")
            if v_old.gear != v_new.gear:
                changes.append(f"gear: {v_old.gear.name} -> {v_new.gear.name}")
            if v_old.ice_running != v_new.ice_running:
                changes.append(f"ice_running: {v_old.ice_running} -> {v_new.ice_running}")
            if v_old.ice_coolant_temp != v_new.ice_coolant_temp:
                changes.append(f"ice_temp: {v_old.ice_coolant_temp} -> {v_new.ice_coolant_temp}°C")
            if v_old.inverter_temp != v_new.inverter_temp:
                changes.append(f"inv_temp: {v_old.inverter_temp} -> {v_new.inverter_temp}°C")
        
        # Check energy state changes
        if old_state.energy != new_state.energy:
            e_old, e_new = old_state.energy, new_state.energy
            if e_old.battery_soc != e_new.battery_soc:
                changes.append(f"soc: {e_old.battery_soc:.1%} -> {e_new.battery_soc:.1%}")
            if e_old.hv_battery_voltage != e_new.hv_battery_voltage:
                changes.append(f"voltage: {e_old.hv_battery_voltage} -> {e_new.hv_battery_voltage}V")
            if e_old.hv_battery_current != e_new.hv_battery_current:
                changes.append(f"current: {e_old.hv_battery_current} -> {e_new.hv_battery_current}A")
            if e_old.battery_temp != e_new.battery_temp:
                changes.append(f"batt_temp: {e_old.battery_temp} -> {e_new.battery_temp}°C")
            if e_old.charging != e_new.charging:
                changes.append(f"charging: {e_old.charging} -> {e_new.charging}")
        
        # Check audio state changes
        if old_state.audio != new_state.audio:
            a_old, a_new = old_state.audio, new_state.audio
            if a_old.volume != a_new.volume:
                changes.append(f"volume: {a_old.volume} -> {a_new.volume}")
            if a_old.muted != a_new.muted:
                changes.append(f"muted: {a_old.muted} -> {a_new.muted}")
        
        # Check climate state changes
        if old_state.climate != new_state.climate:
            c_old, c_new = old_state.climate, new_state.climate
            if c_old.target_temp != c_new.target_temp:
                changes.append(f"target_temp: {c_old.target_temp} -> {c_new.target_temp}°C")
            if c_old.fan_speed != c_new.fan_speed:
                changes.append(f"fan: {c_old.fan_speed} -> {c_new.fan_speed}")
            if c_old.ac_on != c_new.ac_on:
                changes.append(f"ac: {c_old.ac_on} -> {c_new.ac_on}")
        
        # Check connection state changes
        if old_state.connection != new_state.connection:
            cn_old, cn_new = old_state.connection, new_state.connection
            if cn_old.connected != cn_new.connected:
                changes.append(f"connected: {cn_old.connected} -> {cn_new.connected}")
        
        if changes:
            change_str = ", ".join(changes)
            logger.info(f"[STATE] {action_name} ({source}): {change_str}")
    
    def _notify(self, slices: Set[StateSlice]) -> None:
        """Notify subscribers of affected slices."""
        notified: Set[Subscriber] = set()
        
        for slice_ in slices:
            for callback in self._subscribers[slice_]:
                if callback not in notified:
                    try:
                        callback(self._state)
                    except Exception as e:
                        logger.error(f"Subscriber error: {e}")
                    notified.add(callback)
        
        # Always notify ALL subscribers
        for callback in self._subscribers[StateSlice.ALL]:
            if callback not in notified:
                try:
                    callback(self._state)
                except Exception as e:
                    logger.error(f"Subscriber error: {e}")
                notified.add(callback)
